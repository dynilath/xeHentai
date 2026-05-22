import math
import os
import shutil
import time
import traceback
from queue import Empty, Queue
from typing import Optional

from xeHentai.exceptions import FilterException

from . import filters, reuse_index, util
from .async_woker import ArchiveBuildWorker, GalleryCrawlerWorker, ProxyExhaustionGate, WorkerRuntime
from .const import ERR_CANNOT_MAKE_ARCHIVE, ERR_GALLERY_REMOVED, ERR_IP_BANNED, ERR_ONLY_VISIBLE_EXH
from .const import TASK_STATE_DOWNLOAD, TASK_STATE_FAILED, TASK_STATE_FINISHED, TASK_STATE_GET_META, TASK_STATE_MAKE_ARCHIVE, TASK_STATE_PAUSED, TASK_STATE_SCAN_IMG, TASK_STATE_SCAN_PAGE, TASK_STATE_WAITING, XEH_STATE_FULL_EXIT
from .const import RE_GALLERY
from .util.checkfile import extract_img_url_info, check_file
from .host_interface import HostInterface
from .i18n import i18n
from .task import Task
from .worker import ArchiveWorker, Empty, HttpReq, HttpWorker, Monitor, Queue


class TaskControl:
    """Handles the core task execution pipeline: stage execution, worker threads, and the task loop."""

    def __init__(self, host: HostInterface):
        self._host = host
        self._all_threads = [[] for i in range(20)]
        self._exit = 0
        self._monitor = None
        self._v2_proxy_gate = ProxyExhaustionGate()

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

    def _get_httpreq(self, proxy_policy):
        return HttpReq(self.headers, logger=self.logger, proxy=self.proxy, proxy_policy=proxy_policy)

    def _stage_get_meta(self, task: Task, task_guid: str, req: HttpReq):
        """Stage: Fetch gallery metadata from E-H site."""
        task.failcode = 0
        try:
            r = req.request(method="GET", url=task.url,
                            _filter=filters.flt_metadata,
                            suc=lambda x: task.update_meta(x),
                            fail=lambda x: task.set_fail(x))
        except Exception as ex:
            self.logger.error(i18n.TASK_ERROR %
                              (task.guid, traceback.format_exc()))
            task.state = TASK_STATE_FAILED
            return False

        if task.failcode in (ERR_ONLY_VISIBLE_EXH, ERR_GALLERY_REMOVED) and self.has_login and \
                task.migrate_exhentai():
            self.logger.info(i18n.TASK_MIGRATE_EXH % task_guid)
            self._host.tasks.put(task_guid)
            return False
        elif task.failcode == ERR_IP_BANNED:
            self.logger.error(i18n.c(ERR_IP_BANNED) % r)
            task.state = TASK_STATE_FAILED
            return False

        if task.config['download_range']:
            task_total = task.meta.total
            for dRange in task.config['download_range']:
                task.download_range.extend(range(dRange[0],
                                                 dRange[1] + 1 if dRange[1] < task_total else task_total + 1))

        task.state = TASK_STATE_SCAN_PAGE
        return True

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
        task.state = TASK_STATE_FINISHED

    def _stage_check_archive_phase1(self, task: Task, task_guid: str):
        """Stage: Check if exact matching archive exists after GET_META (Phase 1).

        Phase 1: Requires fid_page_hash_map in archive metadata. If found archive has
        fid_page_hash_map, reuse it and skip page scanning.
        """
        if not task.meta:
            task.state = TASK_STATE_GET_META
            return 'retry_meta'

        # Phase 1: Try to find exact matching archive with fid_page_hash_map
        is_exact_match, found_archive = task.exact_downloaded_exits(
            require_fid_page_hash_map=True)

        if is_exact_match:
            self._handle_exact_match_found(task, task_guid, found_archive)
            return 'finished'

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
        return 'continue_scan_page'

    def _stage_scan_page(self, task: Task, task_guid: str, req: HttpReq):
        """Stage: Scan gallery pages for image URLs and check archive (Phase 2)."""
        temp_fid_2_page_url_map = {}
        self.logger.info(i18n.DF_STATE_START_SCAN_PAGE % (task_guid))

        def page_scan_success(x: tuple[str, str, str]):
            # This callback is called for each page scanned, with x containing page info
            # We use it to populate temp_fid_2_page_url_map for later checks
            task.queue_wrapper(temp_fid_2_page_url_map.setdefault, img_tuble=x)

        for x in range(0,
                       int(math.ceil(1.0 * task.meta.total / int(task.meta.thumbnail_cnt)))):
            req.request("GET",
                        "%s/?p=%d" % (task.url, x),
                        filters.flt_pageurl,
                        page_scan_success,
                        lambda x: task.set_fail(x))
            if task.failcode:
                break

        if task.state == TASK_STATE_FAILED:
            return False

        # Phase 2: After scanning pages, try exact match again (now fid_page_hash_map is built from scan)
        is_exact_match, found_archive = task.exact_downloaded_exits(
            require_fid_page_hash_map=False)
        if is_exact_match:
            # Ensure old archives get updated with the hash map collected during page scanning
            # (exact_downloaded_exits preserves populated hash map from queue_wrapper calls above)
            self._handle_exact_match_found(task, task_guid, found_archive)
            return False  # Stop processing, task is complete

        # No exact match, check if all files are already downloaded
        if task.scan_downloaded(temp_fid_2_page_url_map):
            # All files found, update archive and finish
            self.logger.info(i18n.TASK_TITLE % (task_guid, task.meta.title))
            try:
                arc = task.make_archive(remove=False)
                self.logger.info(i18n.DF_FULLY_MATCHED_UPDATED %
                                 (task.guid, arc))
            except Exception as ex:
                self.logger.error(i18n.TASK_ERROR %
                                  (task.guid, traceback.format_exc()))
            self._update_task_reuse_index(task)
            task.state = TASK_STATE_FINISHED
            return False  # Stop processing, task is complete

        task.state = TASK_STATE_SCAN_IMG
        return True

    def _stage_img_scan_then_download(self, task: Task, task_guid: str, req: HttpReq, mon):
        """
        Stage: Scan the individual image page, and download the image.
        This will make it easier to fallback individual page scan when download fails, which is a common case
        """
        # print here so that see it after we can join former threads
        self.logger.info(i18n.TASK_TITLE % (task_guid, task.meta.title))

        # log at here is quite too early
        # finished file counting will be cleared after page scan
        self.logger.info(i18n.TASK_WILL_DOWNLOAD_CNT % (
            task_guid, task.meta.total - task.meta.finished,
            task.meta.total))

        # spawn thread to scan images
        task.img_q.queue.clear()

        self.logger.debug("%s: page_q size before scan: %d" %
                          (task_guid, task.page_q.qsize()))
        
        def log_task_state():
            self.logger.info(f"{task_guid}: P={task.page_q.qsize()} {task.meta.finished}/{task.meta.total}")

        while not task.page_q.empty():
            cur_page = task.page_q.get()

            img_url: Optional[str] = None

            def set_img_url(x: str):
                nonlocal img_url
                img_url = x

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
                if os.path.exists(target_file_path) and check_file(target_file_path, info):
                    task.set_fid_done(this_fid)
                    return

                task.reload_map.setdefault(
                    image_url, [reload_url, real_file_name])
                set_img_url(image_url)
                log_task_state()

            def simple_img_scan_fail(x: tuple[int, str]):
                self.logger.debug(
                    "%s: Failed to scan image page, url=%s" % (task_guid, x[1]))
                task.page_q.put(x[1])
                log_task_state()

            req.request("GET",
                        cur_page,
                        filters.flt_imgurl_wrapper(
                            task.config['download_ori'] and self.has_login),
                        img_scan_success,
                        simple_img_scan_fail)

            if not img_url:
                # image scan failed, the url will be put back to page_q by simple_img_scan_fail,
                # so just continue to next loop and wait for it
                continue

            saved: bool = False

            def set_saved(x: bool):
                nonlocal saved
                saved = x

            def create_download_success(tid: str) -> function[[tuple[str, str, str, str, str]], None]:
                def download_success(x: tuple[str, str, str, str, str]) -> None:
                    # This callback is called for each image downloaded, with x containing download info
                    # We use it to save the file and log the download
                    saved = task.save_file(
                        imgurl=x[1],
                        redirect_url=x[2],
                        binary_iter=x[0],
                        content_type=x[3],
                        original_hash=x[4])
                    if saved:
                        self.logger.debug(i18n.XEH_FILE_DOWNLOADED.format(
                            tid, *task.get_fname(x[1])))
                        set_saved(saved)
                    log_task_state()
                return download_success

            def create_download_fail(tid: str) -> function[[tuple[str, str]], None]:
                def download_fail(x: tuple[str, str]) -> None:
                    reload_url = task.reload_map[x[1]][0]
                    if 'hentai.org/img/509.gif' not in x[1]:
                        task.page_q.put(reload_url)
                    # delete old url in reload_map if exists
                    task.reload_map.pop(x[1])
                    self.logger.debug(i18n.XEH_DOWNLOAD_HAS_ERROR % (
                        tid, i18n.c(x[0]) + ' (' + x[1] + ') ', reload_url))
                    log_task_state()
                return download_fail

            def noop(x):
                pass

            req.request("GET",
                        url=img_url,
                        _filter=filters.download_file_wrapper(
                            task.config['dir']),
                        suc=create_download_success('main'),
                        fail=create_download_fail('main'),
                        stream_cb=noop)

            if not saved:
                # download failed, the url will be put back to page_q by create_download_fail,
                # so just continue to next loop and wait for it
                continue

        self._update_task_reuse_index(task)

        self.logger.info(i18n.TASK_START_MAKE_ARCHIVE % task.guid)
        task.state = TASK_STATE_MAKE_ARCHIVE
        t = time.time()
        try:
            pth = task.make_archive()
        except Exception as ex:
            task.state = TASK_STATE_FAILED
            self.logger.error(i18n.TASK_ERROR % (task.guid, i18n.c(
                ERR_CANNOT_MAKE_ARCHIVE) % traceback.format_exc()))
        else:
            task.state = TASK_STATE_FINISHED
            self.logger.info(i18n.TASK_MAKE_ARCHIVE_FINISHED %
                             (task.guid, pth, time.time() - t))

        return False  # End of coroutine

    def _do_task_coroutine(self, task: Task, task_guid: str, req: HttpReq, mon_ref):
        """Coroutine that progresses through task stages, yielding after each stage."""
        # Stage 1: GET_META
        if task.state <= TASK_STATE_GET_META:
            task.state = TASK_STATE_GET_META
            if not self._stage_get_meta(task, task_guid, req):
                return
            yield 'meta_complete'

        # Stage 2: CHECK_ARCHIVE Phase 1 (immediately after GET_META)
        if task.state == TASK_STATE_SCAN_PAGE:
            result = self._stage_check_archive_phase1(task, task_guid)
            if result == 'finished':
                return  # Task complete, early exit
            elif result == 'retry_meta':
                yield 'retry'
                return
            else:  # 'continue_scan_page'
                yield 'need_scan_page'

        # Stage 3: SCAN_PAGE (Phase 2 archive check inside)
        if task.state == TASK_STATE_SCAN_PAGE:
            if not self._stage_scan_page(task, task_guid, req):
                return  # Task complete or failed
            yield 'page_scan_complete'

        # Stage 4: SCAN_IMG
        # Stage 5: DOWNLOAD
        if task.state == TASK_STATE_SCAN_IMG:
            if not self._stage_img_scan_then_download(task, task_guid, req, mon_ref[0]):
                return  # Task complete or failed

    def _do_task(self, task_guid):
        """Execute a task using coroutine-based stages instead of manual state machine."""
        task = self._host._all_tasks[task_guid]
        task._reuse_index = self._host.global_reuse_index
        if task.state == TASK_STATE_WAITING:
            task.state = TASK_STATE_GET_META
        req = self._get_httpreq(util.get_proxy_policy(task.config))
        if not task.page_q:
            task.page_q = Queue()  # per image page queue
        if not task.img_q:
            task.img_q = Queue()  # (image url, savepath) queue

        # Monitor reference container (to pass by reference to coroutine)
        mon_ref = [None]

        # Create the coroutine generator
        coro = self._do_task_coroutine(task, task_guid, req, mon_ref)

        while self._exit < XEH_STATE_FULL_EXIT:
            # Wait for threads from former task to stop
            if self._all_threads[task.state]:
                self.logger.verbose("wait %d threads in state %s" % (
                    len(self._all_threads[task.state]), task.state))
                for t in self._all_threads[task.state]:
                    t.join()
                self._all_threads[task.state] = []
                # Check again before we bring up new threads
                continue

            # Start monitor if we're at SCAN_IMG stage and it hasn't started yet
            if task.state >= TASK_STATE_SCAN_IMG and not mon_ref[0]:
                self.logger.verbose("state %d >= %d, bring up monitor" % (
                    task.state, TASK_STATE_SCAN_IMG))
                # Bring up the monitor here, ahead of workers
                mon = Monitor(req, self.proxy, self.logger, task,
                              ignored_errors=task.config['ignored_errors'])
                _ = ['down-%d' % (i + 1)
                     for i in range(task.config['download_thread_cnt'])]
                if task.state >= TASK_STATE_SCAN_IMG:
                    _ += ['scan-%d' % (i + 1)
                          for i in range(task.config['scan_thread_cnt'])]
                mon.set_vote_ns(_)
                self._monitor = mon
                task._monitor = mon
                # mon.start()
                # Put in the lowest state
                # self._all_threads[TASK_STATE_SCAN_IMG].append(mon)
                mon_ref[0] = mon

            # Drive the coroutine forward
            try:
                stage_result = next(coro)
                self.logger.verbose("Stage completed: %s" % stage_result)
            except StopIteration:
                # Coroutine completed
                break

            # Check for errors
            if task.failcode:
                self.logger.error(i18n.TASK_ERROR %
                                  (task_guid, i18n.c(task.failcode)))
                break

            # Check if task reached terminal state
            if task.state in (TASK_STATE_FINISHED, TASK_STATE_FAILED):
                break

    def run(self):
        """Main task execution loop."""
        task_guid = None
        cnt = 0
        while not self._exit:
            if cnt == 10:
                self._host.save_session()
                cnt = 0
            try:
                _ = self._host.tasks.get(False)
                self._host.last_task_guid = task_guid
                task_guid = _
            except Empty:
                time.sleep(1)
                cnt += 1
                continue
            else:
                task = self._host._all_tasks[task_guid]
                if TASK_STATE_PAUSED < task.state < TASK_STATE_FINISHED:
                    self.logger.info(i18n.TASK_START % task_guid)
                    self._host.save_session()
                    cnt = 0
                    self._do_task(task_guid)
                    self._update_task_reuse_index(task)
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
