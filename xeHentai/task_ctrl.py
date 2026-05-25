import math
import os
import shutil
import time
import traceback
import asyncio
from types import CoroutineType, SimpleNamespace
from collections import deque
from functools import wraps

from queue import Empty, Queue
from typing import Any, Callable, Generator, List, Optional

import requests

from .scheduler import Scheduler
from . import filters, reuse_index, util
from .const import ERR_CANNOT_MAKE_ARCHIVE, ERR_GALLERY_REMOVED, ERR_IP_BANNED, ERR_ONLY_VISIBLE_EXH, RE_IMGHASH
from .const import TASK_STATE_FAILED, TASK_STATE_FINISHED, TASK_STATE_GET_META, TASK_STATE_MAKE_ARCHIVE, TASK_STATE_PAUSED, TASK_STATE_SCAN_IMG, TASK_STATE_SCAN_PAGE, TASK_STATE_WAITING, XEH_STATE_FULL_EXIT
from .const import RE_GALLERY
from .util.checkfile import extract_img_url_info, check_file
from .host_interface import HostInterface
from .i18n import i18n
from .task import DumplicatedFileInfo, Task
from .request_wrapper import HttpRequest
from .exceptions import FilterException, ImagePageInfoParseException
from .exceptions import map_exception_policy
from .stage_flow import StageAction
from .stage_flow import GetMetaResult, ScanPageResult, ScanImageResult, DownloadResult
from .stage_flow import TaskControlFlow, TaskReschedule, StageRetry, TaskFailed, TaskFinished, TaskAbort, StageSkip, ScanDownloadRetry, ScanDownloadSkip


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

        self._scan_scheduler = Scheduler(
            workers=host.config.get('scan_thread_cnt', 1),
            interval=host.config.get('page_interval', 0.5))
        self._download_scheduler = Scheduler(
            workers=host.config.get('download_thread_cnt', 2))
        self._archive_scheduler = Scheduler(workers=1)

        self._host.logger.info("Scan threads count: {}".format(
            host.config['scan_thread_cnt']))
        self._host.logger.info("Download threads count: {}".format(
            host.config['download_thread_cnt']))
        self._host.logger.info("Async task concurrency: {}".format(
            host.config['async_task_concurrency']))

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

    def _update_task_reuse_index(self, task):
        """Upsert reusable page-hash mappings from a task into global_reuse_index."""
        self._host.global_reuse_index = reuse_index.record_task_reuse(
            self._host.global_reuse_index, task)

    def _get_http_request(self, task_guid: str):
        return HttpRequest(self.headers, logger=self.logger, logger_prefix=task_guid)

    def _raise_mapped_stage_exception(self, stage: str, task: Task, ex: Exception, *, default_code: int = 0, result=None):
        policy = map_exception_policy(
            stage,
            ex,
            ignored_errors=task.config.get('ignored_errors'),
        )
        if isinstance(ex, FilterException):
            failcode = ex.code
            reason = ex.reason or traceback.format_exc()
        else:
            failcode = default_code or 0
            reason = traceback.format_exc()
        if policy.action == StageAction.RETRY:
            raise StageRetry(reason, delay=policy.delay,
                             failcode=failcode, result=result)
        if policy.action == StageAction.PIPELINE_RETRY:
            raise ScanDownloadRetry(
                reason, delay=policy.delay, failcode=failcode, result=result)
        if policy.action == StageAction.SKIP:
            raise StageSkip(reason, delay=policy.delay,
                           failcode=failcode, result=result)
        if policy.action == StageAction.ABORT:
            raise TaskAbort(reason, delay=policy.delay,
                            failcode=failcode, result=result)
        if policy.action == StageAction.FAIL:
            raise TaskFailed(policy.fail_detail or reason, delay=policy.delay,
                             failcode=failcode, result=result)
        if policy.action == StageAction.FINISH:
            raise TaskFinished(reason, delay=policy.delay,
                               failcode=failcode, result=result)
        raise TaskFailed(
            "unexpected policy action: %s" % policy.action,
            delay=policy.delay,
            failcode=failcode,
            result=result,
        )

    def _handle_exact_match_found(self, task: Task, task_guid: str, found_archive: str) -> None:
        """Handle found exact matching archive: relocate, update zip comment, and finish task."""
        current_arc = "%s.zip" % task.get_task_dir()
        if found_archive and os.path.abspath(found_archive) != os.path.abspath(current_arc):
            task_folder = task.get_task_dir()
            if not os.path.exists(task_folder):
                os.makedirs(task_folder)
            shutil.move(found_archive, current_arc)
        self.logger.info(i18n.DF_FULLY_MATCHED % (task.guid, found_archive))
        try:
            arc = task.make_archive()
            self.logger.info(i18n.DF_FULLY_MATCHED_UPDATED % (task.guid, arc))
        except Exception as ex:
            self.logger.error(i18n.TASK_ERROR %
                              (task.guid, traceback.format_exc()))
        self._update_task_reuse_index(task)

    def _stage_check_archive_phase1(self, task: Task, task_guid: str):
        """Stage: Check if exact matching archive exists after GET_META (Phase 1).

        Phase 1: Requires fid_page_hash_map in archive metadata. If found archive has
        fid_page_hash_map, reuse it and skip page scanning.
        """
        if not task.meta:
            raise TaskReschedule(
                "metadata not ready for archive check, reschedule task", delay=1.0)

        # Phase 1: Try to find exact matching archive with fid_page_hash_map
        is_exact_match, found_archive = task.exact_downloaded_exits(
            require_fid_page_hash_map=True)

        if is_exact_match:
            self._handle_exact_match_found(task, task_guid, found_archive)
            raise TaskFinished('phase1 exact-match completed')

        # Phase 1.5: Prescan extraction from series archives (NEW)
        # This runs after exact match fails, to populate task dir from related archives
        if hasattr(task, 'prescan_extract_series_files'):
            try:
                prescan_result = task.prescan_extract_series_files()
                extracted_count = prescan_result.get('extracted_count', 0)
                if extracted_count > 0:
                    sources = prescan_result.get('sources', [])
                    self.logger.info(i18n.PRESCAN_EXTRACTED %
                                     (task_guid, extracted_count, len(sources)))
                    for src in sources[:3]:
                        self.logger.debug(
                            f"{task_guid}: Reused files from {os.path.basename(src)}")
            except Exception as ex:
                self.logger.warning(
                    f"{task_guid}: Prescan extraction error: {str(ex)}")

        # No exact match found, continue to SCAN_PAGE
        pass

    @stage_retry_skip_scope
    async def _get_meta_async(self, task: Task, task_guid: str, req: HttpRequest):
        """Async version of Stage: Fetch gallery metadata from E-H site."""

        task.failcode = 0
        try:
            def work():
                return req.request("GET", task.url,
                                   proxy=self._host.proxy,
                                   retry=task.config.get('page_retry'),
                                   timeout=task.config.get('page_timeout'),
                                   proxy_wait=False)

            r = await self._scan_scheduler.submit(work)

            filters.flt_metadata(r, suc=lambda x: task.update_meta(
                x), fail=lambda x: task.set_fail(x))
        except Exception as ex:
            self._raise_mapped_stage_exception(
                'get_meta', task, ex, default_code=task.failcode)

        if task.failcode in (ERR_ONLY_VISIBLE_EXH, ERR_GALLERY_REMOVED) and self.has_login and \
                task.migrate_exhentai():
            self.logger.info(i18n.TASK_MIGRATE_EXH % task_guid)
            self._host.tasks.put(task_guid)
            raise TaskAbort('gallery migrated to exhentai',
                            result=GetMetaResult(migrated=True))
        elif task.failcode == ERR_IP_BANNED:
            self.logger.error(i18n.c(ERR_IP_BANNED) % r)
            raise TaskFailed(i18n.c(ERR_IP_BANNED), failcode=ERR_IP_BANNED)

        return GetMetaResult()

    @stage_retry_skip_scope
    async def _scan_page_async(self, task: Task, task_guid: str, req: HttpRequest):

        temp_fid_2_page_url_map = {}

        def page_scan_success(x: tuple[str, str, str]):
            page_url, unpad_fid, original_file_name = x
            temp_fid_2_page_url_map[unpad_fid] = page_url

        page_count = 0
        try:
            do_proxy_page = not task.config.get('proxy_image_only')
            awaitables:List[CoroutineType[Any, Any, requests.Response]] = []
            for x in range(0, int(math.ceil(1.0 * task.meta.total / int(task.meta.thumbnail_cnt)))):
                def work(page_num=x):
                    return req.request("GET",
                                       "%s/?p=%d" % (task.url, page_num),
                                       retry=task.config.get('page_retry'),
                                       timeout=task.config.get('page_timeout'),
                                       proxy=self._host.proxy if do_proxy_page else None,
                                       proxy_wait=False)
                awaitables.append(self._scan_scheduler.submit(work))

            results = await asyncio.gather(*awaitables)
            for r in results:
                filters.flt_pageurl(r, suc=page_scan_success)
                page_count += 1
        except Exception as ex:
            self._raise_mapped_stage_exception(
                'scan_page', task, ex, default_code=task.failcode)

        for fid, page_url in temp_fid_2_page_url_map.items():
            page_hash = RE_GALLERY.findall(page_url)[0][0]
            task.fid_2_page_hash_map.setdefault(fid, page_hash)
            
        # Phase 2: After scanning pages, try exact match again (now fid_page_hash_map is built from scan)
        is_exact_match, found_archive = task.exact_downloaded_exits(
            require_fid_page_hash_map=False)
        if is_exact_match:
            # Ensure old archives get updated with the hash map collected during page scanning
            # (exact_downloaded_exits preserves populated hash map from queue_wrapper calls above)
            self._handle_exact_match_found(task, task_guid, found_archive)
            raise TaskFinished('phase2 exact-match completed',
                               result=ScanPageResult(page_count=page_count))

        
        # start rebuild page queue for later stages
        task.page_q.queue.clear()
        task_dir = task.get_task_dir()
        for fid, page_url in temp_fid_2_page_url_map.items():
            expected_file_name = task.fid_2_file_name_map.get(fid)
            expected_file_hash = task.fid_2_img_hash_map.get(fid)
            if expected_file_hash and expected_file_name:
                expected_path = os.path.join(task_dir, expected_file_name)
                if check_file(expected_path, expected_file_hash):
                    # file exists and matches expected hash, skip adding to page queue
                    task.set_fid_done(fid)
                    continue
            
            # file not found or hash mismatch, add to page queue for scanning and downloading
            task.page_q.put(page_url)

        return ScanPageResult(page_count=page_count)

    @stage_retry_skip_scope
    async def _scan_img_async(self, page_url: str, task: Task, task_guid: str, req: HttpRequest):

        _ = RE_GALLERY.findall(page_url)
        if not _:
            raise ValueError(f"failed to parse page URL: <{page_url}>")
        page_hash:str = _[0][0]
        unpad_fid:str = _[0][1]

        def img_scan_success(x: List[str]):
            # This callback is called for each image page scanned, with x containing image info
            # We use it to populate task's reload_map and page_q for later downloading
            
            unpad_fid, file_hash, file_ext, image_url, reload_url = x
            
            expected_saved = task._build_saving_file_name(unpad_fid, file_ext)
            expected = os.path.join(task.get_task_dir(), expected_saved)
            
            task.fid_2_img_hash_map[unpad_fid] = file_hash
            task.fid_2_file_name_map[unpad_fid] = expected_saved
            
            # STEP 1: Check if this image URL has been scanned before (dumplicated with another page)
            if image_url in task.reload_map:
                # 已经存在，说明之前扫描过这个图片了
                other_reload_url, other_file_name = task.reload_map[image_url]

                _, other_unpad_fid = RE_GALLERY.findall(other_reload_url)[0]

                # 先检查文件是否存在
                other = os.path.join(task.get_task_dir(), other_file_name)
                if os.path.exists(other):
                    # 文件存在，说明之前下载过了，设置这个文件下载完成
                    shutil.copy(other, expected)
                    task.set_fid_done(unpad_fid)
                else:
                    # 文件不存在，可能还没下载，添加到 dumpicated map
                    task.dumplicated_file_map.setdefault(page_hash, []).append(DumplicatedFileInfo(
                        fid=unpad_fid,
                        existed_fid=other_unpad_fid,
                        file_name=expected_saved,
                        existed_file_name=other_file_name,
                    ))
                
                raise ScanDownloadSkip("file is dumplicated with another page, skip scan and download", result=ScanImageResult(
                    fid=unpad_fid, page_url=page_url, reload_url=reload_url))
                
            # STEP 2: Check if the file for this image URL already exists with correct hash
            # Might be downloaded restored from previous runs, or reuse other downloaded archives with same file (hash) but different URLs
            if check_file(expected, file_hash):
                task.set_fid_done(unpad_fid)
                raise ScanDownloadSkip("file already exists with correct hash, skip scan and download", result=ScanImageResult(
                    fid=unpad_fid, page_url=page_url, reload_url=reload_url))
            
            # STEP 3: New file that needs to be downloaded, add to reload_map for later download stage
            task.reload_map.setdefault(
                image_url, [reload_url, expected_saved])
            return ScanImageResult(fid=unpad_fid, page_url=page_url, img_url=image_url, reload_url=reload_url)
                

        try:
            do_proxy_scan = not task.config.get('proxy_image_only')

            def work():
                return req.request("GET", page_url,
                                   retry=task.config.get('page_retry'),
                                   timeout=task.config.get('page_timeout'),
                                   proxy=self._host.proxy if do_proxy_scan else None,
                                   proxy_wait=False)
            r = await self._scan_scheduler.submit(work)
            filter = filters.flt_imgurl_wrapper(
                task.config['download_ori'] and self.has_login)
            return filter(r, suc=img_scan_success)
        except Exception as ex:
            result = ScanImageResult(fid=unpad_fid,page_url=page_url)
            self._raise_mapped_stage_exception(
                'scan_img', task, ex, default_code=task.failcode, result=result)

    @stage_retry_skip_scope
    async def _download_img_async(self, img_url: str, task: Task, task_guid: str, req: HttpRequest):

        download_retry = task.config.get('download_retry', 5)
        download_timeout = task.config.get('download_timeout', 10)
        
        stream_retried = 0

        def download_image(x: tuple[Callable[[int, requests.Response], Generator], str, str, str, str]):
            nonlocal download_retry, download_timeout, stream_retried
            # This callback is called for each image downloaded, with x containing download info
            # We use it to save the file and log the download
            saved = task.save_file(
                imgurl=x[1],
                redirect_url=x[2],
                binary_iter=x[0],
                content_type=x[3],
                original_hash=x[4],
                timeout_time=time.time() + download_timeout)
            reload_url = task.reload_map[img_url][0]
            result = DownloadResult(img_url=img_url, reload_url=reload_url)

            if saved:
                return result
            
            if stream_retried < download_retry:
                self.logger.warning(f"{task_guid}: Stream download failed for {img_url}, retrying... ({stream_retried + 1}/{download_retry})")
                stream_retried += 1
                raise StageRetry('stream download failed, retrying', delay=1.0, result=result)
            
            raise ScanDownloadRetry('failed to save downloaded image after retries, retry from scan', failcode=task.failcode, result=result)

        try:
            do_proxy_image = task.config.get('proxy_image_only') or task.config.get('proxy_image')

            def work():
                return req.request("GET",
                                   url=img_url,
                                   retry=task.config.get('page_retry'),
                                   timeout=task.config.get('page_timeout'),
                                   proxy=self._host.proxy if do_proxy_image else None,
                                   proxy_wait=False,
                                   stream=True)
            r = await self._download_scheduler.submit(work)

            filter = filters.download_file_wrapper()
            return filter(r, suc=download_image)
        except Exception as ex:
            result = DownloadResult(
                img_url=img_url, reload_url=task.reload_map[img_url][0])
            self._raise_mapped_stage_exception('download_img', task, ex,
                                               default_code=task.failcode,
                                               result=result)

    async def _image_download_async(self, task: Task, task_guid: str, req: HttpRequest):
        """Combined async pipeline for scanning image pages and downloading images."""

        page_urls = []
        while not task.page_q.empty():
            page_urls.append(task.page_q.get())

        inflight_limit = int(task.config.get('pipeline_inflight_pages')) or 0
        if inflight_limit <= 0:
            inflight_limit = max(1, len(page_urls))

        pending = deque(page_urls)
        running = set()

        def log_task_state(label: str):
            self.logger.verbose(
                f"{task_guid}: {label} P={len(pending)} W={len(running)} {task.meta.finished}/{task.meta.total}")

        log_task_state('pipeline_start')

        async def process_page(start_page_url: str):
            current_page_url = start_page_url
            while True:
                try:
                    scan_result: ScanImageResult = await self._scan_img_async(
                        current_page_url, task, task_guid, req)
                    await self._download_img_async(
                        scan_result.img_url, task, task_guid, req)

                    img_file_name = task.reload_map[scan_result.img_url][1]
                    self.logger.info(i18n.XEH_FILE_DOWNLOADED.format(
                        task_guid, scan_result.fid, img_file_name))

                    log_task_state('img_downloaded')
                    return
                except ScanDownloadSkip as ex:
                    log_task_state('task_skipped')
                    return
                except ScanDownloadRetry as ex:
                    log_task_state('scan_download_retry')
                    current_page_url = ex.result.reload_url if ex.result and ex.result.reload_url else current_page_url
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
                log_task_state('pipeline_wait_begin')
                done, _ = await asyncio.wait(
                    running,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for fut in done:
                    running.discard(fut)
                    await fut
                log_task_state('pipeline_wait_end')
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

        log_task_state('pipeline_done')
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
            raise TaskAbort('task aborted before stage_get_meta')

        if task.state == TASK_STATE_WAITING:
            task.state = TASK_STATE_GET_META

        # Stage 1: GET_META
        # GET_META should always run for any task
        await self._get_meta_async(task, task_guid, req)
        if self._task_should_abort(task):
            raise TaskAbort('task aborted after stage_get_meta')
        # Stage 2: CHECK_ARCHIVE Phase 1 (immediately after GET_META)
        self._stage_check_archive_phase1(task, task_guid)
        if self._task_should_abort(task):
            raise TaskAbort('task aborted after stage_check_archive_phase1')
        self._host.save_session()

        self.logger.info(i18n.TASK_TITLE % (task_guid, task.meta.title))

        if task.state == TASK_STATE_GET_META:
            task.state = TASK_STATE_SCAN_PAGE

        if task.state <= TASK_STATE_SCAN_PAGE:
            self.logger.info(i18n.DF_STATE_START_SCAN_PAGE % task_guid)
            # Stage 3: SCAN_PAGE (Phase 2 archive check inside)
            await self._scan_page_async(task, task_guid, req)
            if self._task_should_abort(task):
                raise TaskAbort('task aborted before scan/download pipeline')
            self._host.save_session()

            task.state = TASK_STATE_SCAN_IMG

        if task.state <= TASK_STATE_SCAN_IMG and not task.page_q.empty():
            # Stage 4: SCAN_IMG
            # Stage 5: DOWNLOAD
            self.logger.info(i18n.TASK_WILL_DOWNLOAD_CNT % (
                task_guid, task.meta.total - task.meta.finished,
                task.meta.total))
            await self._image_download_async(task, task_guid, req)
            self._update_task_reuse_index(task)
            if self._task_should_abort(task):
                raise TaskAbort('task aborted before make_archive')
            self._host.save_session()
            if task.config.get('make_archive'):
                task.state = TASK_STATE_MAKE_ARCHIVE
            else:
                task.state = TASK_STATE_FINISHED

        # After all pages are processed, make archive
        if task.state <= TASK_STATE_MAKE_ARCHIVE:
            self.logger.info(i18n.TASK_START_MAKE_ARCHIVE % task.guid)
            start_time = time.time()
            pth = await self._make_archive_async(task)
            self.logger.info(i18n.TASK_MAKE_ARCHIVE_FINISHED %
                             (task.guid, pth, time.time() - start_time))
            self._host.save_session()

        task.state = TASK_STATE_FINISHED
        self.logger.info(i18n.TASK_FINISHED % task.guid)

        return

    async def _run_task_entry_async(self, task_guid: str):
        """Run one async task entry and keep failure/cleanup handling in one place."""
        try:
            while True:
                try:
                    await self._do_task_async(task_guid)
                    return
                except TaskReschedule as ex:
                    task = self._host._all_tasks.get(task_guid)
                    if task and self._task_should_abort(task):
                        raise TaskAbort(ex.reason, delay=ex.delay,
                                        failcode=ex.failcode, result=ex.result)
                    await asyncio.sleep(ex.delay or 1.0)
                    continue
                except TaskFinished:
                    task = self._host._all_tasks.get(task_guid)
                    if task:
                        task.state = TASK_STATE_FINISHED
                    return
                except TaskAbort as ex:
                    self.logger.info("%s: task aborted: %s" %
                                     (task_guid, ex.reason or 'control flow'))
                    return
                except TaskFailed as ex:
                    task = self._host._all_tasks.get(task_guid)
                    if task:
                        task.state = TASK_STATE_FAILED
                        task.failcode = ex.failcode or task.failcode
                    fail_desc = ex.reason or (
                        i18n.c(task.failcode) if task and task.failcode else 'task failed')
                    self.logger.error(i18n.TASK_ERROR % (task_guid, fail_desc))
                    return
                except TaskControlFlow as ex:
                    task = self._host._all_tasks.get(task_guid)
                    if task:
                        task.state = TASK_STATE_FAILED
                        task.failcode = ex.failcode or task.failcode
                    self.logger.error(
                        f"{task_guid}: unexpected control flow exception: {traceback.format_exc()}")
                    self.logger.error(i18n.TASK_ERROR % (
                        task_guid, ex.reason or 'unexpected task control flow'))
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger.error(i18n.TASK_ERROR %
                              (task_guid, traceback.format_exc()))
            task = self._host._all_tasks.get(task_guid)
            if task:
                task.state = TASK_STATE_FAILED
        finally:
            task = self._host._all_tasks.get(task_guid)
            if task:
                self._update_task_reuse_index(task)

    async def _run_loop_async(self):
        """Main async scheduling loop with a cap on concurrent _do_task_async executions."""
        cnt = 0
        raw_limit = self._host.config.get('async_task_concurrency', 1)
        try:
            concurrency_limit = int(raw_limit or 1)
        except (TypeError, ValueError):
            concurrency_limit = 1
        concurrency_limit = max(1, concurrency_limit)

        running = {}

        try:
            while not self._exit:
                if cnt == 10:
                    self._host.save_session()
                    cnt = 0

                while not self._exit and len(running) < concurrency_limit:
                    try:
                        task_guid = self._host.tasks.get(False)
                    except Empty:
                        break

                    task = self._host._all_tasks.get(task_guid)
                    if not task:
                        continue
                    if not (TASK_STATE_PAUSED < task.state < TASK_STATE_FINISHED):
                        continue
                    if task_guid in running.values():
                        continue

                    self._host.last_task_guid = task_guid
                    self.logger.info(i18n.TASK_START % task_guid)
                    self._host.save_session()
                    cnt = 0

                    fut = asyncio.create_task(
                        self._run_task_entry_async(task_guid))
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
                    running.pop(fut, None)
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
