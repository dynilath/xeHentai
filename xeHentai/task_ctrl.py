import math
import os
import shutil
import time
import traceback
import asyncio
from types import SimpleNamespace
from collections import deque
from functools import wraps

from queue import Empty, Queue
from typing import Optional

from .scheduler import Scheduler
from . import filters, reuse_index, util
from .const import ERR_CANNOT_MAKE_ARCHIVE, ERR_GALLERY_REMOVED, ERR_IP_BANNED, ERR_ONLY_VISIBLE_EXH
from .const import TASK_STATE_FAILED, TASK_STATE_FINISHED, TASK_STATE_GET_META, TASK_STATE_MAKE_ARCHIVE, TASK_STATE_PAUSED, TASK_STATE_SCAN_IMG, TASK_STATE_SCAN_PAGE, TASK_STATE_WAITING, XEH_STATE_FULL_EXIT
from .const import RE_GALLERY
from .util.checkfile import extract_img_url_info, check_file
from .host_interface import HostInterface
from .i18n import i18n
from .task import Task
from .request_wrapper import HttpRequest
from .exceptions import FilterException
from .exceptions import map_exception_policy
from .stage_flow import StageAction
from .stage_flow import GetMetaResult, ScanPageResult, ScanImageResult, DownloadResult
from .stage_flow import TaskControlFlow, TaskReschedule, StageRetry, TaskFailed, TaskFinished, TaskAbort, TaskSkip, ScanDownloadRetry


def stage_retry_scope(func):
    """Retry stage method when StageRetry is raised."""

    @wraps(func)
    async def _wrapped(self, *args, **kwargs):
        while True:
            try:
                return await func(self, *args, **kwargs)
            except StageRetry as ex:
                await asyncio.sleep(ex.delay or 1.0)
                continue

    return _wrapped


class TaskControl:
    """Handles the core task execution pipeline: stage execution, worker threads, and the task loop."""

    def __init__(self, host: HostInterface):
        self._host = host
        self._all_threads = [[] for i in range(20)]
        self._exit = 0

        self._scan_scheduler = Scheduler(
            workers=host.config['scan_thread_cnt'], interval=1)
        self._download_scheduler = Scheduler(
            workers=host.config['download_thread_cnt'])
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
            ignored_errors=task.config.get('ignored_errors') or [],
        )
        if isinstance(ex, FilterException):
            failcode = ex.code
            reason = ex.reason or str(ex)
        else:
            failcode = default_code or 0
            reason = str(ex)
        if policy.action == StageAction.RETRY:
            raise StageRetry(reason, delay=policy.delay,
                             failcode=failcode, result=result)
        if policy.action == StageAction.PIPELINE_RETRY:
            raise ScanDownloadRetry(
                reason, delay=policy.delay, failcode=failcode, result=result)
        if policy.action == StageAction.SKIP:
            raise TaskSkip(reason, delay=policy.delay,
                           failcode=failcode, result=result)
        if policy.action == StageAction.ABORT:
            raise TaskAbort(reason, delay=policy.delay,
                            failcode=failcode, result=result)
        if policy.action == StageAction.FAIL:
            raise TaskFailed(reason, delay=policy.delay,
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

    def _task_cfg(self, task: Task, key: str, default=None):
        if key in task.config:
            return task.config[key]
        return self._host.config.get(key, default)

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
            arc = task.make_archive(remove=False)
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
            raise TaskReschedule("metadata not ready for archive check, reschedule task", delay=1.0)

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

    @stage_retry_scope
    async def _get_meta_async(self, task: Task, task_guid: str, req: HttpRequest):
        """Async version of Stage: Fetch gallery metadata from E-H site."""

        def work():
            task.failcode = 0
            try:
                r = req.request("GET", task.url,
                                proxy=self._host.proxy,
                                retry=self._task_cfg(task, 'page_retry', 3),
                                timeout=self._task_cfg(
                                    task, 'page_timeout', 10),
                                proxy_wait=False)

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

            if task.config['download_range']:
                task_total = task.meta.total
                for dRange in task.config['download_range']:
                    rg = range(dRange[0], dRange[1] + 1 if dRange[1]
                               < task_total else task_total + 1)
                    task.download_range.extend(rg)

            return GetMetaResult()

        return await self._scan_scheduler.submit(work)

    @stage_retry_scope
    async def _scan_page_async(self, task: Task, task_guid: str, req: HttpRequest):

        temp_fid_2_page_url_map = {}

        def page_scan_success(x: tuple[str, str, str]):
            # This callback is called for each page scanned, with x containing page info
            # We use it to populate temp_fid_2_page_url_map for later checks
            task.queue_wrapper(temp_fid_2_page_url_map.setdefault, img_tuble=x)

        def work():
            page_count = 0
            try:
                for x in range(0, int(math.ceil(1.0 * task.meta.total / int(task.meta.thumbnail_cnt)))):
                    r = req.request("GET",
                                    "%s/?p=%d" % (task.url, x),
                                    retry=self._task_cfg(
                                        task, 'page_retry', 3),
                                    timeout=self._task_cfg(
                                        task, 'page_timeout', 10),
                                    proxy=self._host.proxy,
                                    proxy_wait=False)
                    filters.flt_pageurl(r, suc=page_scan_success)
                    page_count += 1
            except Exception as ex:
                self._raise_mapped_stage_exception(
                    'scan_page', task, ex, default_code=task.failcode)

            if task.state == TASK_STATE_FAILED:
                raise TaskFailed(
                    'task already failed during scan_page', failcode=task.failcode)

            # Phase 2: After scanning pages, try exact match again (now fid_page_hash_map is built from scan)
            is_exact_match, found_archive = task.exact_downloaded_exits(
                require_fid_page_hash_map=False)
            if is_exact_match:
                # Ensure old archives get updated with the hash map collected during page scanning
                # (exact_downloaded_exits preserves populated hash map from queue_wrapper calls above)
                self._handle_exact_match_found(task, task_guid, found_archive)
                raise TaskFinished('phase2 exact-match completed',
                                   result=ScanPageResult(page_count=page_count))

            # No exact match, check if all files are already downloaded
            if task.scan_downloaded(temp_fid_2_page_url_map):
                # All files found, update archive and finish
                self.logger.info(i18n.TASK_TITLE %
                                 (task_guid, task.meta.title))
                try:
                    arc = task.make_archive(remove=False)
                    self.logger.info(i18n.DF_FULLY_MATCHED_UPDATED %
                                     (task.guid, arc))
                except Exception as ex:
                    self.logger.error(i18n.TASK_ERROR %
                                      (task.guid, traceback.format_exc()))
                self._update_task_reuse_index(task)
                raise TaskFinished('all files already downloaded',
                                   result=ScanPageResult(page_count=page_count))

            return ScanPageResult(page_count=page_count)

        return await self._scan_scheduler.submit(work)

    @stage_retry_scope
    async def _scan_img_async(self, page_url: str, task: Task, task_guid: str, req: HttpRequest):

        def img_scan_success(x: tuple[str, str, str, str]):
            # This callback is called for each image page scanned, with x containing image info
            # We use it to populate task's reload_map and page_q for later downloading
            image_url, reload_url, fname, filesize = x

            this_fid: str = RE_GALLERY.findall(reload_url)[0][1]
            original_file_name: str = task.fid_2_original_file_name_map[this_fid]
            ext: str = os.path.splitext(original_file_name)[
                1] if task.config['download_ori'] else os.path.splitext(fname)[1]
            real_file_name = task._set_final_file_ext(
                this_fid, ext or '.jpg')

            info = extract_img_url_info(image_url)

            task.set_file_size(this_fid, filesize)

            folder_path = task.get_task_dir()
            target_file_path = os.path.join(folder_path, real_file_name)
            if check_file(target_file_path, info):
                task.set_fid_done(this_fid)
                raise TaskSkip(result=ScanImageResult(
                    page_url=page_url, reload_url=reload_url))

            task.reload_map.setdefault(
                image_url, [reload_url, real_file_name])

            return ScanImageResult(page_url=page_url, img_url=image_url, reload_url=reload_url)

        def work():
            try:
                r = req.request("GET", page_url,
                                retry=self._task_cfg(task, 'page_retry', 3),
                                timeout=self._task_cfg(
                                    task, 'page_timeout', 10),
                                proxy=self._host.proxy,
                                proxy_wait=False)

                filter = filters.flt_imgurl_wrapper(
                    task.config['download_ori'] and self.has_login)
                return filter(r, suc=img_scan_success)
            except Exception as ex:
                result = ScanImageResult(page_url=page_url)
                self._raise_mapped_stage_exception(
                    'scan_img', task, ex, default_code=task.failcode, result=result)

        return await self._scan_scheduler.submit(work)

    @stage_retry_scope
    async def _download_img_async(self, img_url: str, task: Task, task_guid: str, req: HttpRequest):

        def download_image(x: tuple[str, str, str, str, str]):
            # This callback is called for each image downloaded, with x containing download info
            # We use it to save the file and log the download
            saved = task.save_file(
                imgurl=x[1],
                redirect_url=x[2],
                binary_iter=x[0],
                content_type=x[3],
                original_hash=x[4])
            reload_url = task.get_reload_url(img_url)
            if saved:
                return DownloadResult(img_url=img_url, reload_url=reload_url)
            raise StageRetry(
                'save_file returned False',
                delay=1.0,
                result=DownloadResult(img_url=img_url, reload_url=reload_url),
            )

        def work():
            try:
                r = req.request("GET",
                                url=img_url,
                                retry=self._task_cfg(
                                    task, 'download_retry', 5),
                                timeout=self._task_cfg(
                                    task, 'download_timeout', 10),
                                proxy=self._host.proxy,
                                proxy_wait=False,
                                stream=True)

                filter = filters.download_file_wrapper()
                return filter(r, suc=download_image)
            except Exception as ex:
                result = DownloadResult(
                    img_url=img_url, reload_url=task.get_reload_url(img_url))
                self._raise_mapped_stage_exception('download_img', task, ex,
                                                   default_code=task.failcode,
                                                   result=result,)

        return await self._download_scheduler.submit(work)

    @stage_retry_scope
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

    async def _run_scan_and_download_concurrent(self, task: Task, task_guid: str, req: HttpRequest):
        page_urls = deque()
        while not task.page_q.empty():
            page_urls.append(task.page_q.get())

        self.logger.debug("%s: page_q size before scan: %d" %
                          (task_guid, len(page_urls)))

        pending = {}

        def log_task_state(label: str):
            self.logger.verbose(
                "%s: %s P=%d W=%d %d/%d" % (
                    task_guid,
                    label,
                    len(page_urls),
                    len(pending),
                    task.meta.finished,
                    task.meta.total,
                )
            )

        log_task_state('pipeline_start')

        async def process_page(start_page_url: str):
            current_page_url = start_page_url

            while True:
                if self._task_should_abort(task):
                    raise TaskAbort('task aborted before scan/download page')

                try:
                    scan_result = await self._scan_img_async(current_page_url, task, task_guid, req)
                except TaskSkip:
                    log_task_state('scan_skip')
                    return
                except ScanDownloadRetry as ex:
                    if ex.delay:
                        await asyncio.sleep(ex.delay)
                    continue

                if not scan_result or not scan_result.img_url:
                    log_task_state('scan_unexpected_skip')
                    return

                img_url = scan_result.img_url
                try:
                    await self._download_img_async(img_url, task, task_guid, req)
                except TaskSkip:
                    log_task_state('download_skip')
                    return
                except ScanDownloadRetry as ex:
                    if ex.delay:
                        await asyncio.sleep(ex.delay)

                    reload_url = ex.result.reload_url if ex.result else task.get_reload_url(
                        img_url)
                    task.reload_map.pop(img_url, None)
                    self.logger.debug(i18n.XEH_DOWNLOAD_HAS_ERROR % (
                        task_guid, img_url, reload_url or img_url))
                    log_task_state('pipeline_retry_rescan')
                    current_page_url = reload_url or current_page_url
                    continue

                if img_url:
                    self.logger.debug(i18n.XEH_FILE_DOWNLOADED.format(
                        task_guid, *task.get_fname(img_url)))
                    log_task_state('download_ok')
                    return

                log_task_state('download_unexpected_skip')
                return

        # Do not mirror Scheduler thread counts here; scheduler itself already limits execution.
        inflight_limit = int(self._task_cfg(
            task, 'pipeline_inflight_pages', 0) or 0)

        while page_urls or pending:
            while page_urls and (inflight_limit <= 0 or len(pending) < inflight_limit):
                page_url = page_urls.popleft()
                fut = asyncio.create_task(process_page(page_url))
                pending[fut] = page_url
                log_task_state('dispatch')

            if not pending:
                continue

            done, _ = await asyncio.wait(set(pending.keys()), return_when=asyncio.FIRST_COMPLETED)
            for fut in done:
                pending.pop(fut, None)
                try:
                    fut.result()
                except (TaskFailed, TaskAbort) as ex:
                    raise
                except TaskControlFlow as ex:
                    raise TaskFailed(
                        ex.reason or 'unexpected control flow in process_page', failcode=ex.failcode)
                except Exception as ex:
                    self._raise_mapped_stage_exception(
                        'scan_img', task, ex, default_code=task.failcode)

        log_task_state('pipeline_done')
        return

    def _task_should_abort(self, task: Task) -> bool:
        return self._exit >= XEH_STATE_FULL_EXIT or task.state == TASK_STATE_PAUSED

    async def _do_task_async(self, task_guid: str):
        """Execute a task using async/await for stages instead of manual state machine."""
        task = self._host._all_tasks[task_guid]
        task._reuse_index = self._host.global_reuse_index

        if task.state == TASK_STATE_WAITING:
            task.state = TASK_STATE_GET_META

        req = self._get_http_request(task_guid)

        if not task.page_q:
            task.page_q = Queue()  # per image page queue
        if not task.img_q:
            task.img_q = Queue()  # (image url, savepath) queue

        if self._task_should_abort(task):
            raise TaskAbort('task aborted before stage_get_meta')

        task.state = TASK_STATE_GET_META
            
        # Stage 1: GET_META
        # GET_META should always run for any task
        await self._get_meta_async(task, task_guid, req)
        if self._task_should_abort(task):
            raise TaskAbort('task aborted after stage_get_meta')
        # Stage 2: CHECK_ARCHIVE Phase 1 (immediately after GET_META)
        self._stage_check_archive_phase1(task, task_guid)
        if self._task_should_abort(task):
            raise TaskAbort('task aborted after stage_get_meta')
        self._host.save_session()

        task.state = TASK_STATE_SCAN_PAGE
        if task.state <= TASK_STATE_SCAN_PAGE:
            # Stage 3: SCAN_PAGE (Phase 2 archive check inside)
            await self._scan_page_async(task, task_guid, req)
            if self._task_should_abort(task):
                raise TaskAbort('task aborted before scan/download pipeline')
            self._host.save_session()

        self.logger.info(i18n.TASK_TITLE % (task_guid, task.meta.title))
        self.logger.info(i18n.TASK_WILL_DOWNLOAD_CNT % (
            task_guid, task.meta.total - task.meta.finished,
            task.meta.total))

        task.state = TASK_STATE_SCAN_IMG
        if task.state <= TASK_STATE_SCAN_IMG and not task.page_q.empty():
            # Stage 4: SCAN_IMG
            # Stage 5: DOWNLOAD
            if self._task_should_abort(task):
                raise TaskAbort('task aborted before page pipeline starts')
            await self._run_scan_and_download_concurrent(task, task_guid, req)
            self._update_task_reuse_index(task)
            if self._task_should_abort(task):
                raise TaskAbort('task aborted before make_archive')
            self._host.save_session()

        # After all pages are processed, make archive
        task.state = TASK_STATE_MAKE_ARCHIVE
        if task.state <= TASK_STATE_MAKE_ARCHIVE:
            start_time = time.time()
            self.logger.info(i18n.TASK_START_MAKE_ARCHIVE % task.guid)
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
