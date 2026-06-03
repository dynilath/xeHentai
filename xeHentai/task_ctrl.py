import math
import os
import re
import shutil
import time
import traceback
import asyncio
from types import CoroutineType
from collections import deque
from functools import wraps

from queue import Empty, Queue
from typing import Any, Dict, List, Tuple

import requests

from .scheduler import Scheduler
from . import filters, reuse_index
from .const import (
    ERR_CANNOT_MAKE_ARCHIVE,
    ERR_GALLERY_REMOVED,
    ERR_IP_BANNED,
    ERR_ONLY_VISIBLE_EXH,
)
from .const import (
    TASK_STATE_FAILED,
    TASK_STATE_FINISHED,
    TASK_STATE_GET_META,
    TASK_STATE_MAKE_ARCHIVE,
    TASK_STATE_PAUSED,
    TASK_STATE_SCAN_IMG,
    TASK_STATE_SCAN_PAGE,
    TASK_STATE_WAITING,
    TASK_TOP_STATUS_PROCESSING,
    TASK_TOP_STATUS_WAITING,
    TASK_TOP_STATUS_PROCESSED,
    XEH_STATE_FULL_EXIT,
)
from .const import RE_GALLERY
from .util.checkfile import check_file
from .host_interface import HostInterface
from .i18n import i18n
from .task import DumplicatedFileInfo, Task, TaskReuseResources
from .request_wrapper import HttpRequest, HttpRequestResult
from .exceptions import ImageFileNotFoundException, raise_for_stage_exception
from .stage_flow import GetMetaResult, ScanPageResult, ScanImageResult, DownloadResult
from .stage_flow import (
    TaskControlFlow,
    TaskReschedule,
    StageRetry,
    TaskFailed,
    TaskFinished,
    TaskAbort,
    StageSkip,
    ScanDownloadRetry,
    ScanDownloadSkip,
)


def stage_retry_skip_scope(func):
    """Retry stage method when StageRetry is raised."""

    @wraps(func)
    async def _wrapped(self, *args, **kwargs):
        while True:
            try:
                return await func(self, *args, **kwargs)
            except StageRetry as ex:
                await asyncio.sleep(ex.delay or 1.0)
                continue
            except StageSkip:
                break

    return _wrapped


class TaskControl:
    """Handles the core task execution pipeline: stage execution, worker threads, and the task loop."""

    def __init__(self, host: HostInterface):
        self._host = host
        self._all_threads = [[] for i in range(20)]
        self._exit = 0
        self._runtime_top_status: Dict[str, int] = {}
        self._waiting_set: set[str] = set()
        self._running_set: set[str] = set()

        self._scan_scheduler = Scheduler(
            workers=host.config.get("scan_thread_cnt", 1),
            interval=host.config.get("page_interval", 0.5),
        )
        self._download_scheduler = Scheduler(
            workers=host.config.get("download_thread_cnt", 5)
        )
        self._archive_scheduler = Scheduler(workers=1)

        self._host.logger.info(
            "Scan threads count: {}".format(host.config["scan_thread_cnt"])
        )
        self._host.logger.info(
            "Download threads count: {}".format(host.config["download_thread_cnt"])
        )
        self._host.logger.info(
            "Async task concurrency: {}".format(host.config["async_task_concurrency"])
        )

    @property
    def logger(self):
        return self._host.logger

    @property
    def proxy(self):
        return self._host.proxy

    @property
    def headers(self):
        return self._host.headers

    @property
    def has_login(self):
        return self._host.has_login

    def _get_http_request(self, task_guid: str):
        return HttpRequest(self.headers, logger=self.logger, logger_prefix=task_guid)

    def get_task_top_status(self, task_guid: str, task: Task | None = None) -> int:
        runtime = self._runtime_top_status.get(task_guid)
        if runtime is not None:
            return runtime
        task = task if task is not None else self._host._all_tasks.get(task_guid)
        if not task:
            return TASK_TOP_STATUS_WAITING
        if task.state in (TASK_STATE_FINISHED, TASK_STATE_FAILED, TASK_STATE_PAUSED):
            return TASK_TOP_STATUS_PROCESSED
        return TASK_TOP_STATUS_WAITING

    def set_task_top_status(self, task_guid: str, top_status: int) -> None:
        self._runtime_top_status[task_guid] = top_status

    def enqueue_waiting_task(self, task_guid: str) -> None:
        if task_guid not in self._host._all_tasks:
            return
        self.set_task_top_status(task_guid, TASK_TOP_STATUS_WAITING)
        if task_guid not in self._waiting_set:
            self._waiting_set.add(task_guid)
            self._host.tasks.put(task_guid)

    def mark_task_processing(self, task_guid: str) -> None:
        self._waiting_set.discard(task_guid)
        self._running_set.add(task_guid)
        self.set_task_top_status(task_guid, TASK_TOP_STATUS_PROCESSING)

    def mark_task_processed(self, task_guid: str) -> None:
        self._waiting_set.discard(task_guid)
        self._running_set.discard(task_guid)
        self.set_task_top_status(task_guid, TASK_TOP_STATUS_PROCESSED)

    def clear_task_top_status(self, task_guid: str) -> None:
        self._waiting_set.discard(task_guid)
        self._running_set.discard(task_guid)
        self._runtime_top_status.pop(task_guid, None)

    def _handle_exact_match_found(
        self, task: Task, task_guid: str, found_archive: str
    ) -> None:
        """Handle found exact matching archive: relocate, update zip comment, and finish task."""
        current_arc = "%s.zip" % task.get_task_dir()
        if found_archive and os.path.abspath(found_archive) != os.path.abspath(
            current_arc
        ):
            task_folder = task.get_task_dir()
            if not os.path.exists(task_folder):
                os.makedirs(task_folder)
            shutil.move(found_archive, current_arc)
        self.logger.info(i18n.DF_FULLY_MATCHED.format(guid=task.guid, path=current_arc))
        try:
            arc, up_to_date = task.make_archive()
            if up_to_date:
                self.logger.info(i18n.DF_FULLY_MATCHED_UP_TO_DATE.format(guid=task.guid, path=arc))
            else:
                self.logger.info(i18n.DF_FULLY_MATCHED_UPDATED.format(guid=task.guid, path=arc))
        except Exception as ex:
            self.logger.error(i18n.TASK_ERROR % (task.guid, traceback.format_exc()))

    async def _scan_reuse_archive_page_hashes_async(
        self, task: Task, req: HttpRequest, candidate: reuse_index.ArchiveReuseCandidate
    ) -> Tuple[int, Dict[str, str]]:
        """Fetch fid_page_hash_map for a candidate archive that lacks it in comment metadata."""
        temp_fid_2_page_hash_map = {}

        def page_result(x: tuple[str, str, str]):
            matches = RE_GALLERY.findall(x[0])
            if matches:
                page_hash, fid = matches[0]
                temp_fid_2_page_hash_map[fid] = page_hash

        def meta_grab() -> Tuple[HttpRequestResult, int, int]:
            res = req.request(
                "GET",
                candidate.url,
                retry=task.config.get("page_retry"),
                timeout=task.config.get("page_timeout"),
                proxy=self._host.proxy,
            )
            meta = filters.flt_metadata(res)

            for x in filters.flt_pageurl(res):
                page_result(x)

            return (res, meta["thumbnail_cnt"], meta["total"])

        _, thumbnail_cnt, total = await self._scan_scheduler.submit(meta_grab)

        awaitables: List[CoroutineType[Any, Any, HttpRequestResult]] = []
        for x in range(1, int(math.ceil(1.0 * total / thumbnail_cnt))):

            def work(page_num=x):
                return req.request(
                    "GET",
                    "%s/?p=%d" % (candidate.url, page_num),
                    retry=task.config.get("page_retry"),
                    timeout=task.config.get("page_timeout"),
                    proxy=self._host.proxy,
                )

            awaitables.append(self._scan_scheduler.submit(work))

        results = await asyncio.gather(*awaitables)
        for r in results:
            for x in filters.flt_pageurl(r):
                page_result(x)
        return (total, temp_fid_2_page_hash_map)

    async def _stage_try_reuse_async(
        self, task: Task, task_guid: str, req: HttpRequest
    ) -> None:
        """Build task-local reuse matches after current task page hashes are known."""
        try:
            archives_set, pending_count, page_map_count = task.prepare_reuse_files()
            if len(archives_set) > 0:
                self.logger.debug(
                    f"[guid={task_guid}] candidate reuse archives: {archives_set}, pending_count: {pending_count}, page_map_count: {page_map_count}"
                )
            else:
                self.logger.debug(
                    f"[guid={task_guid}] no candidate archive found for reuse"
                )
                return None

            for candidate in list(task.reuse.pending_archives):
                try:
                    candidate_total, fid_page_hash_map = (
                        await self._scan_reuse_archive_page_hashes_async(
                            task, req, candidate
                        )
                    )

                    target_dir = task.get_task_dir()
                    for fid, page_hash in fid_page_hash_map.items():
                        for ext in [".jpg", ".png", ".gif", ".bmp", ".webp"]:
                            basename = Task._build_saving_file_name(
                                candidate_total, fid, ext
                            )
                            file_path = TaskReuseResources._reuse_file_path(
                                target_dir, candidate.gid, basename
                            )
                            if os.path.exists(file_path):
                                task.reuse.register_page_hash_file(page_hash, file_path)
                            break

                    self.logger.debug(
                        f"[guid={task_guid}] crawled reuse archive {os.path.basename(candidate.archive_path)}"
                    )
                except Exception as ex:
                    self.logger.warning(
                        f"[guid={task_guid}] failed to crawl reuse archive {os.path.basename(candidate.archive_path)}: {ex}"
                    )
        except Exception as ex:
            self.logger.warning(f"[guid={task_guid}] try_reuse stage failed: {ex}")

    @stage_retry_skip_scope
    async def _get_meta_async(self, task: Task, task_guid: str, req: HttpRequest):
        """Async version of Stage: Fetch gallery metadata from E-H site."""

        task.failcode = 0
        try:

            def work():
                return req.request(
                    "GET",
                    task.url,
                    proxy=self._host.proxy,
                    retry=task.config.get("page_retry"),
                    timeout=task.config.get("page_timeout"),
                    proxy_wait=False,
                )

            r = await self._scan_scheduler.submit(work)
            meta = filters.flt_metadata(r)
            task.update_meta(meta)

            temp_fid_2_page_url_map = {}

            def page_scan_success(x: tuple[str, str, str]):
                page_url, unpad_fid, original_file_name = x
                temp_fid_2_page_url_map[unpad_fid] = page_url

            for x in filters.flt_pageurl(r):
                page_scan_success(x)

            for fid, page_url in temp_fid_2_page_url_map.items():
                page_hash = RE_GALLERY.findall(page_url)[0][0]
                task.fid_2_page_hash_map.setdefault(fid, page_hash)

            if (
                task.failcode in (ERR_ONLY_VISIBLE_EXH, ERR_GALLERY_REMOVED)
                and self.has_login
                and task.migrate_exhentai()
            ):
                self.logger.info(i18n.TASK_MIGRATE_EXH % task_guid)
                raise TaskReschedule(
                    "gallery migrated to exhentai",
                    delay=0.0,
                    result=GetMetaResult(migrated=True),
                )
                
            elif task.failcode == ERR_IP_BANNED:
                self.logger.error(i18n.c(ERR_IP_BANNED) % r.response.text)
                raise TaskFailed(i18n.c(ERR_IP_BANNED), failcode=ERR_IP_BANNED)
            
        except Exception as ex:
            raise_for_stage_exception(
                "get_meta", ex, task.config.get("ignored_errors"), default_code=task.failcode
            )

        return GetMetaResult()

    @stage_retry_skip_scope
    async def _scan_page_async(self, task: Task, task_guid: str, req: HttpRequest):

        temp_fid_2_page_url_map = {}

        def page_scan_success(x: tuple[str, str, str]):
            page_url, unpad_fid, original_file_name = x
            temp_fid_2_page_url_map[unpad_fid] = page_url

        page_count = 0
        try:
            do_proxy_page = not task.config.get("proxy_image_only")
            awaitables: List[CoroutineType[Any, Any, HttpRequestResult]] = []
            for x in range(
                1, int(math.ceil(1.0 * task.meta.total / task.meta.thumbnail_cnt))
            ):

                def work(page_num=x):
                    return req.request(
                        "GET",
                        "%s/?p=%d" % (task.url, page_num),
                        retry=task.config.get("page_retry"),
                        timeout=task.config.get("page_timeout"),
                        proxy=self._host.proxy if do_proxy_page else None,
                        proxy_wait=False,
                    )

                awaitables.append(self._scan_scheduler.submit(work))

            results = await asyncio.gather(*awaitables)

            for r in results:
                for x in filters.flt_pageurl(r):
                    page_scan_success(x)
                    page_count += 1
        except Exception as ex:
            raise_for_stage_exception(
                "scan_page", ex, task.config.get("ignored_errors"), default_code=task.failcode
            )

        for fid, page_url in temp_fid_2_page_url_map.items():
            page_hash = RE_GALLERY.findall(page_url)[0][0]
            task.fid_2_page_hash_map.setdefault(fid, page_hash)

        return ScanPageResult(page_count=page_count)

    @stage_retry_skip_scope
    async def _scan_img_async(
        self, page_url: str, task: Task, task_guid: str, req: HttpRequest
    ) -> ScanImageResult:

        _ = RE_GALLERY.findall(page_url)
        if not _:
            raise ValueError(f"failed to parse page URL: <{page_url}>")
        page_hash: str = _[0][0]
        unpad_fid: str = _[0][1]

        def img_scan_success(x: filters.ImgUrlFilterResult):
            # This callback is called for each image page scanned, with x containing image info
            # We use it to populate task's reload_map and page_q for later downloading

            expected_saved = Task._build_saving_file_name(
                task.meta.total, x.unpad_fid, x.file_ext
            )
            expected = os.path.join(task.get_task_dir(), expected_saved)

            task.fid_2_img_hash_map[x.unpad_fid] = x.file_hash
            task.fid_2_file_name_map[x.unpad_fid] = expected_saved

            # STEP 1: Check if this image URL has been scanned before (dumplicated with another page)
            if x.img_url in task.reload_map:
                other_reload_url, other_file_name = task.reload_map[x.img_url]

                _, other_unpad_fid = RE_GALLERY.findall(other_reload_url)[0]

                other = os.path.join(task.get_task_dir(), other_file_name)

                if not other == expected:
                    # the same file exists with different name
                    # this is from dumplicated images in a gallery
                    if os.path.exists(other):
                        # copy the file to expected path
                        # and mark fid done, so later scan/download stages will skip this image
                        shutil.copy(other, expected)
                        task.set_fid_done(x.unpad_fid)
                    else:
                        # the file from other page is not downloaded yet
                        # add to dumplicated_file_map for later handling in download stage
                        task.dumplicated_file_map.setdefault(page_hash, []).append(
                            DumplicatedFileInfo(
                                fid=x.unpad_fid,
                                existed_fid=other_unpad_fid,
                                file_name=expected_saved,
                                existed_file_name=other_file_name,
                            )
                        )
                    self.logger.debug(
                        f"{task_guid}: found dumplicated image URL:  \nsrc {other} <-> \ntarger: {expected}\n URL: {x.img_url}"
                    )

                    raise ScanDownloadSkip(
                        i18n.CF_SCANDOWNLOADSKIP_DUPLICATE,
                        result=ScanImageResult(
                            fid=x.unpad_fid,
                            page_url=page_url,
                            reload_url=x.reload_url,
                            img_url=x.img_url,
                        ),
                    )

            # STEP 2: Check if the file for this image URL already exists with correct hash
            # Might be downloaded restored from previous runs, or reuse other downloaded archives with same file (hash) but different URLs
            # Some previous download might mistakenly set the file ext to .jpg/.png, so check them as well
            for ext in [".jpg", ".png"]:
                wf_expected_file = Task._build_saving_file_name(
                    task.meta.total, x.unpad_fid, ext
                )
                wf_expected = os.path.join(task.get_task_dir(), wf_expected_file)
                if wf_expected == expected:
                    break
                if check_file(wf_expected, x.file_hash):
                    shutil.copy(wf_expected, expected)
                    task.set_fid_done(x.unpad_fid)
                    raise ScanDownloadSkip(
                        i18n.CF_SCANDOWNLOADSKIP_EXISTING,
                        result=ScanImageResult(
                            fid=x.unpad_fid,
                            page_url=page_url,
                            reload_url=x.reload_url,
                            img_url=x.img_url,
                        ),
                    )

            if check_file(expected, x.file_hash):
                task.set_fid_done(x.unpad_fid)
                raise ScanDownloadSkip(
                    i18n.CF_SCANDOWNLOADSKIP_EXISTING,
                    result=ScanImageResult(
                        fid=x.unpad_fid,
                        page_url=page_url,
                        reload_url=x.reload_url,
                        img_url=x.img_url,
                    ),
                )

            # STEP 3: New file that needs to be downloaded, add to reload_map for later download stage
            task.reload_map.setdefault(x.img_url, (x.reload_url, expected_saved))
            return ScanImageResult(
                fid=x.unpad_fid,
                page_url=page_url,
                img_url=x.img_url,
                reload_url=x.reload_url,
            )

        try:
            do_proxy_scan = not task.config.get("proxy_image_only")

            def work():
                return req.request(
                    "GET",
                    page_url,
                    retry=task.config.get("page_retry"),
                    timeout=task.config.get("page_timeout"),
                    proxy=self._host.proxy if do_proxy_scan else None,
                    proxy_wait=False,
                )

            r = await self._scan_scheduler.submit(work)
            result = filters.flt_imgurl_wrapper(r, 
                task.config["download_ori"] and self.has_login
            )
            return img_scan_success(result)
        except Exception as ex:
            result = ScanImageResult(
                fid=unpad_fid, page_url=page_url, img_url="", reload_url=""
            )
            raise_for_stage_exception(
                "scan_img", ex, task.config.get("ignored_errors"), default_code=task.failcode, result=result
            )

    @stage_retry_skip_scope
    async def _download_img_async(
        self, img_url: str, task: Task, task_guid: str, req: HttpRequest
    ):

        result = DownloadResult(img_url=img_url, reload_url=task.reload_map[img_url][0])

        try:
            do_proxy_image = task.config.get("proxy_image_only") or task.config.get(
                "proxy_image"
            )

            def work():
                res = req.request(
                    "GET",
                    url=img_url,
                    retry=task.config.get("download_retry"),
                    timeout=task.config.get("download_timeout"),
                    proxy=self._host.proxy if do_proxy_image else None,
                    proxy_wait=False,
                )

                if res.response.status_code != 200:
                    raise ImageFileNotFoundException(res.final_url)

                return task.save_image_response_content(res.response, img_url)

            await self._download_scheduler.submit(work)

            return result
        except Exception as ex:
            raise_for_stage_exception(
                "download_img", ex, task.config.get("ignored_errors"), default_code=task.failcode, result=result
            )

    async def _image_scan_download_async(
        self, task: Task, task_guid: str, req: HttpRequest
    ):
        """Combined async pipeline for scanning image pages and downloading images."""

        page_urls = []
        if not task.page_q:
            raise ValueError("page_q is not initialized for task %s" % task_guid)

        while not task.page_q.empty():
            page_urls.append(task.page_q.get())

        inflight_limit = task.config.get("download_thread_cnt", 5)

        pending = deque(page_urls)
        running = set()

        def log_task_state(label: str):
            self.logger.debug(
                f"{task_guid}: {label} P={len(pending)} W={len(running)} {len(task._flist_done)}/{task.meta.total}"
            )

        log_task_state("pipeline_start")

        async def process_page(start_page_url: str):
            current_page_url = start_page_url
            while True:
                try:
                    scan_result: ScanImageResult = await self._scan_img_async(
                        current_page_url, task, task_guid, req
                    )
                    await self._download_img_async(
                        scan_result.img_url, task, task_guid, req
                    )

                    img_file_name = task.reload_map[scan_result.img_url][1]
                    self.logger.info(
                        i18n.XEH_FILE_DOWNLOADED.format(
                            guid=task_guid, fid=scan_result.fid, fname=img_file_name
                        )
                    )

                    log_task_state("img_downloaded")
                    return
                except ScanDownloadSkip as ex:
                    self.logger.info(
                        i18n.DF_FILE_DOWNLOADED_SKIPPED.format(
                            guid=task_guid,
                            fid=ex.result.fid if ex.result else None,
                            reason=ex.reason,
                        )
                    )
                    return
                except ScanDownloadRetry as ex:
                    self.logger.debug(
                        f"{task_guid}: scan/download retry for page {current_page_url}"
                    )
                    current_page_url = (
                        ex.result.reload_url
                        if ex.result and ex.result.reload_url
                        else current_page_url
                    )
                    if ex.delay:
                        await asyncio.sleep(ex.delay)

        async def run_one(url: str):
            try:
                await process_page(url)
            except asyncio.CancelledError:
                raise

        def schedule_next():
            while pending and len(running) < inflight_limit:
                url = pending.popleft()
                running.add(asyncio.create_task(run_one(url)))

        schedule_next()

        try:
            while running:
                done, _ = await asyncio.wait(
                    running,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for fut in done:
                    running.discard(fut)
                    await fut
                schedule_next()
        except asyncio.CancelledError:
            for fut in running:
                fut.cancel()
            await asyncio.gather(*running, return_exceptions=True)
            raise
        except Exception:
            for fut in running:
                fut.cancel()
            await asyncio.gather(*running, return_exceptions=True)
            raise

        log_task_state("pipeline_done")
        return

    @stage_retry_skip_scope
    async def _make_archive_async(self, task: Task):

        def work():
            try:
                return task.make_archive()
            except Exception:
                raise TaskFailed(
                    traceback.format_exc(),
                    failcode=ERR_CANNOT_MAKE_ARCHIVE,
                )

        return await self._archive_scheduler.submit(work)

    def _task_should_abort(self, task: Task) -> bool:
        return self._exit >= XEH_STATE_FULL_EXIT or task.state == TASK_STATE_PAUSED

    async def _do_task_async(self, task_guid: str):
        """Execute a task using async/await for stages instead of manual state machine."""
        task = self._host._all_tasks[task_guid]
        task._reuse_index = self._host.global_reuse_index

        req = self._get_http_request(task_guid)

        if not task.page_q:
            task.page_q = Queue()  # per image page queue

        if self._task_should_abort(task):
            raise TaskAbort("task aborted before stage_get_meta")

        if task.state == TASK_STATE_WAITING:
            task.set_phase_state(TASK_STATE_GET_META)

        # Stage 1: GET_META
        # GET_META should always run for any task
        await self._get_meta_async(task, task_guid, req)
        if self._task_should_abort(task):
            raise TaskAbort("task aborted after stage_get_meta")

        # Stage 1.1: CHECK_ARCHIVE (immediately after GET_META)
        if not task.meta:
            raise TaskReschedule(
                "metadata not ready for archive check, reschedule task", delay=1.0
            )
        is_exact_match, found_archive = task.exact_downloaded_exits(
            require_fid_page_hash_map=True
        )
        if is_exact_match and found_archive:
            self._handle_exact_match_found(task, task_guid, found_archive)
            raise TaskFinished("phase1 exact-match completed")

        if self._task_should_abort(task):
            raise TaskAbort("task aborted after stage_check_archive_phase1")
        self._host._save_session(task=True, proxy_store=True)

        self.logger.info(
            i18n.TASK_TITLE.format(guid=task_guid, gid=task.gid, title=task.meta.title)
        )

        if task.state == TASK_STATE_GET_META:
            task.set_phase_state(TASK_STATE_SCAN_PAGE)
        else:
            missing = [
                str(i + 1)
                for i in range(task.meta.total)
                if str(i + 1) not in task.fid_2_page_hash_map
                or str(i + 1) not in task.fid_2_file_name_map
            ]
            if len(missing) > 0:
                self.logger.warning(
                    f"[guid={task_guid}] some pages are missing in task data, continue to scan pages, previous state: {task.state}, missing pages: {missing}"
                )
                task.set_phase_state(TASK_STATE_GET_META)

        if task.state <= TASK_STATE_SCAN_PAGE:
            self.logger.info(
                i18n.DF_STATE_START_SCAN_PAGE.format(guid=task_guid, gid=task.gid)
            )
            # Stage 2: SCAN_PAGE (Phase 2 archive check inside)
            await self._scan_page_async(task, task_guid, req)
            if self._task_should_abort(task):
                raise TaskAbort("task aborted before scan/download pipeline")

            # Stage 2.1: After scanning pages, try exact match again (now fid_page_hash_map is built from scan)
            is_exact_match, found_archive = task.exact_downloaded_exits(
                require_fid_page_hash_map=False,
                extract_non_exact_match=True,
            )
            if is_exact_match and found_archive:
                # Ensure old archives get updated with the hash map collected during page scanning
                # (exact_downloaded_exits preserves populated hash map from queue_wrapper calls above)
                self._handle_exact_match_found(task, task_guid, found_archive)
                raise TaskFinished("phase2 exact-match completed")

            self.logger.debug(
                f"[guid={task_guid}] scaned {len(task.fid_2_page_hash_map)} pages"
            )

            if not found_archive:
                self.logger.debug(f"[guid={task_guid}] no exact match found after page scan")
                await self._stage_try_reuse_async(task, task_guid, req)

            if self._task_should_abort(task):
                raise TaskAbort("task aborted before build_page_queue")
            self._host._save_session(task=True, proxy_store=True)

            task.set_phase_state(TASK_STATE_SCAN_IMG)

        # build page queue for later stages, do this after scan_page to ensure the queue is up to date with scanned pages
        task.build_page_queue()

        if task.state <= TASK_STATE_SCAN_IMG and not task.page_q.empty():
            # Stage 4: SCAN_DOWNLOAD_IMG
            self.logger.info(
                i18n.TASK_WILL_DOWNLOAD_CNT.format(
                    guid=task_guid,
                    gid=task.gid,
                    count=task.meta.total - len(task._flist_done),
                    total=task.meta.total,
                )
            )
            await self._image_scan_download_async(task, task_guid, req)
            if self._task_should_abort(task):
                raise TaskAbort("task aborted before make_archive")
            self._host._save_session(task=True, proxy_store=True)
            if task.config.get("make_archive"):
                task.set_phase_state(TASK_STATE_MAKE_ARCHIVE)
            else:
                task.set_phase_state(TASK_STATE_FINISHED)

        # After all pages are processed, make archive
        if task.state <= TASK_STATE_MAKE_ARCHIVE:
            self.logger.info(
                i18n.TASK_START_MAKE_ARCHIVE.format(guid=task.guid, gid=task.gid)
            )
            start_time = time.time()
            pth, _ = await self._make_archive_async(task)
            self.logger.info(
                i18n.TASK_MAKE_ARCHIVE_FINISHED.format(
                    guid=task.guid,
                    gid=task.gid,
                    path=pth,
                    time=time.time() - start_time,
                )
            )
            self._host._save_session(task=True, proxy_store=True)
            if pth:
                reuse_index.add_zip_to_reuse_index(self._host.global_reuse_index, pth)

        task.set_phase_state(TASK_STATE_FINISHED)
        self.mark_task_processed(task_guid)
        self.logger.info(i18n.TASK_FINISHED.format(guid=task.guid, gid=task.gid))

        task.cleanup_download_info()
        self._host._save_session(task=True, proxy_store=True)

        return

    async def _run_task_entry_async(self, task_guid: str):
        """Run one async task entry and keep failure/cleanup handling in one place."""

        # initial await, allow the caller to do some quick scheduling of multiple tasks
        # without immediately blocking on the first one
        await asyncio.sleep(0)

        try:
            while True:
                try:
                    await self._do_task_async(task_guid)
                    return
                except TaskReschedule as ex:
                    task = self._host._all_tasks.get(task_guid)
                    if not task:
                        return

                    if ex.delay:
                        await asyncio.sleep(ex.delay)

                    if self._task_should_abort(task):
                        if task.state == TASK_STATE_PAUSED:
                            self.mark_task_processed(task_guid)
                        else:
                            # keep non-paused task phase intact; runtime status will be inferred
                            self.clear_task_top_status(task_guid)
                        self._host._save_session(task=True, proxy_store=True)
                        return

                    self.enqueue_waiting_task(task_guid)
                    self._host._save_session(task=True, proxy_store=True)
                    return
                except TaskFinished:
                    task = self._host._all_tasks.get(task_guid)
                    if task:
                        task.set_phase_state(TASK_STATE_FINISHED)
                        self.mark_task_processed(task_guid)
                        self._host._save_session(task=True, proxy_store=True)
                    return
                except TaskAbort as ex:
                    self.logger.debug(
                        "%s: task aborted: %s"
                        % (task_guid, ex.reason or "control flow")
                    )
                    task = self._host._all_tasks.get(task_guid)
                    if task:
                        if task.state == TASK_STATE_PAUSED:
                            self.mark_task_processed(task_guid)
                        else:
                            # TaskAbort is pure stop-flow; no re-dispatch behavior here.
                            self.clear_task_top_status(task_guid)
                        self._host._save_session(task=True, proxy_store=True)
                    return
                except TaskFailed as ex:
                    task = self._host._all_tasks.get(task_guid)
                    if task:
                        task.set_phase_state(TASK_STATE_FAILED)
                        self.mark_task_processed(task_guid)
                        task.failcode = ex.failcode or task.failcode
                    fail_desc = ex.reason or (
                        i18n.c(task.failcode)
                        if task and task.failcode
                        else "task failed"
                    )
                    self.logger.error(i18n.TASK_ERROR % (task_guid, fail_desc))
                    self._host._save_session(task=True, proxy_store=True)
                    return
                except TaskControlFlow as ex:
                    task = self._host._all_tasks.get(task_guid)
                    if task:
                        task.set_phase_state(TASK_STATE_FAILED)
                        self.mark_task_processed(task_guid)
                        task.failcode = ex.failcode or task.failcode
                    self.logger.error(
                        f"{task_guid}: unexpected control flow exception: {traceback.format_exc()}"
                    )
                    self.logger.error(
                        i18n.TASK_ERROR
                        % (task_guid, ex.reason or "unexpected task control flow")
                    )
                    self._host._save_session(task=True, proxy_store=True)
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger.error(i18n.TASK_ERROR % (task_guid, traceback.format_exc()))
            task = self._host._all_tasks.get(task_guid)
            if task:
                task.set_phase_state(TASK_STATE_FAILED)
                self.mark_task_processed(task_guid)
                self._host._save_session(task=True, proxy_store=True)

    async def _run_loop_async(self):
        """Main async scheduling loop with a cap on concurrent _do_task_async executions."""
        cnt = 0
        raw_limit = self._host.config.get("async_task_concurrency", 1)
        try:
            concurrency_limit = int(raw_limit or 1)
        except (TypeError, ValueError):
            concurrency_limit = 1
        concurrency_limit = max(1, concurrency_limit)

        running = {}

        try:
            while not self._exit:
                if cnt == 10:
                    self._host._save_session(task=True, proxy_store=True)
                    cnt = 0

                while not self._exit and len(running) < concurrency_limit:
                    try:
                        task_guid = self._host.tasks.get(False)
                    except Empty:
                        break

                    self._waiting_set.discard(task_guid)

                    task = self._host._all_tasks.get(task_guid)
                    if not task:
                        continue
                    if self.get_task_top_status(task_guid, task) != TASK_TOP_STATUS_WAITING:
                        continue
                    if not (TASK_STATE_PAUSED < task.state < TASK_STATE_FINISHED):
                        continue
                    if task_guid in self._running_set:
                        continue

                    self._host.last_task_guid = task_guid
                    self.mark_task_processing(task_guid)
                    self.logger.info(
                        i18n.TASK_START.format(guid=task_guid, gid=task.gid)
                    )
                    self._host._save_session(task=True, proxy_store=True)
                    cnt = 0

                    fut = asyncio.create_task(self._run_task_entry_async(task_guid))
                    running[fut] = task_guid

                if not running:
                    await asyncio.sleep(1)
                    cnt += 1
                    continue

                done, _ = await asyncio.wait(
                    set(running.keys()),
                    timeout=1.0,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for fut in done:
                    done_guid = running.pop(fut, None)
                    if done_guid:
                        self._running_set.discard(done_guid)
                    try:
                        fut.result()
                    except asyncio.CancelledError:
                        pass
                    except Exception:
                        # _run_task_entry_async already logs details.
                        pass

                cnt += 1
        finally:
            if running:
                for fut in running.keys():
                    fut.cancel()
                await asyncio.gather(*running.keys(), return_exceptions=True)
                self._running_set.clear()

    def run(self):
        """Main task execution loop."""
        asyncio.run(self._run_loop_async())
        self.logger.info(i18n.XEH_LOOP_FINISHED)
        self._host._cleanup()

    def terminate(self):
        """Signal all threads to exit immediately."""
        self._exit = XEH_STATE_FULL_EXIT
        for l in self._all_threads:
            for p in l:
                p._exit = lambda x: True

    def join_all(self):
        """Wait for all worker threads to finish."""
        for l in self._all_threads:
            for p in l:
                p.join()
