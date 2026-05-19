#!/usr/bin/env python
# coding:utf-8
# Contributor:
#      fffonion        <fffonion@gmail.com>

import os
import re
import sys
import math
import json
import time
import shutil
import traceback
from .task import Task
from . import reuse_index
from . import session_store
from . import util
from . import proxy
from . import filters
from .rpc import RPCServer
from .i18n import i18n
from .util import logger
from .const import *
from .const import __version__
from .worker import *
from .async_woker import (
    ArchiveBuildWorker,
    GalleryCrawlerWorker,
    KeepAliveFn,
    ManagedWorker,
    ProxyExhaustionGate,
    SinglePageDownloadWorker,
    VoteFn,
    WorkerRuntime,
)
from queue import Queue, Empty

from . import config as default_config
sys.path.insert(1, FILEPATH)
try:
    import config
except ImportError:
    config = default_config
sys.path.pop(1)


class xeHentai(object):
    def __init__(self):
        self.verstr = f"{__version__}{'-dev' if DEVELOPMENT else ''}"
        self.logger = logger.Logger()
        self._exit = False
        self.tasks: Queue[str] = Queue()  # for queueing, stores gid only
        self.last_task_guid = None
        self._all_tasks: dict[str,Task] = {}  # for saving states
        self._all_threads = [[] for i in range(20)]
        self.cfg = {k: v for k, v in default_config.__dict__.items()
                    if not k.startswith("_")}
        # note that ignored_errors are overwritten using val from custom config
        self.cfg.update(
            {k: v for k, v in config.__dict__.items() if not k.startswith("_")})
        self.proxy = None
        self.cookies = {"nw": "1"}
        self.headers = {
            'User-Agent': util.make_ua(),
            'Accept-Charset': 'utf-8;q=0.7,*;q=0.7',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Connection': 'keep-alive'
        }
        self.has_login = False
        self.global_reuse_index = reuse_index.ensure_reuse_index({})
        self.load_session()
        self.rpc = None
        self._v2_proxy_gate = ProxyExhaustionGate()

    def _update_task_reuse_index(self, task):
        """Upsert reusable page-hash mappings from a task into global_reuse_index."""
        self.global_reuse_index = reuse_index.record_task_reuse(self.global_reuse_index, task)

    def update_config(self, **cfg_dict):
        self.cfg.update({k: v for k, v in cfg_dict.items()
                        if k in cfg_dict and k not in ('ignored_errors',)})
        # merge ignored errors list
        if 'ignored_errors' in cfg_dict and cfg_dict['ignored_errors']:
            self.cfg['ignored_errors'] = list(
                set(self.cfg['ignored_errors'] + cfg_dict['ignored_errors']))
        self.logger.set_level(logger.Logger.WARNING - self.cfg['log_verbose'])
        self.logger.verbose("cfg %s" % self.cfg)
        if cfg_dict['proxy']:
            if not self.proxy:  # else we keep it None
                self.proxy = proxy.Pool(self.logger)
            for p in self.cfg['proxy']:
                try:
                    self.proxy.add_proxy(p)
                except Exception as ex:
                    self.logger.warning(traceback.format_exc())
            self.logger.debug(i18n.PROXY_CANDIDATE_CNT %
                              len(self.proxy.proxies))
            if cfg_dict['proxy_disable_threshold']:
                self.proxy.set_max_fail(cfg_dict['proxy_disable_threshold'])
            if cfg_dict['proxy_good_threshold']:
                self.proxy.set_good_threshold(cfg_dict['proxy_good_threshold'])
        if cfg_dict['dir'] and not os.path.exists(cfg_dict['dir']):
            try:
                os.makedirs(cfg_dict['dir'])
            except OSError as ex:  # Python >2.5
                self.logger.error(i18n.ERR_CANNOT_CREATE_DIR % cfg_dict['dir'])
        if not self.rpc and self.cfg['rpc_port'] and self.cfg['rpc_interface']:
            self.rpc = RPCServer(self, (self.cfg['rpc_interface'], int(self.cfg['rpc_port'])),
                                 secret=None if 'rpc_secret' not in self.cfg else self.cfg['rpc_secret'],
                                 logger=self.logger)
            if not RE_LOCAL_ADDR.match(self.cfg['rpc_interface']) and \
                    not self.cfg['rpc_secret']:
                self.logger.warning(i18n.RPC_TOO_OPEN %
                                    self.cfg['rpc_interface'])
            self.rpc.start()
        self.logger.set_logfile(self.cfg['log_path'])
        return ERR_NO_ERROR, ""

    def _get_httpreq(self, proxy_policy):
        return HttpReq(self.headers, logger=self.logger, proxy=self.proxy, proxy_policy=proxy_policy)

    def _get_httpworker(self, tid, task_q, flt, suc, fail, keep_alive, proxy_policy, timeout, stream_mode):
        return HttpWorker(tid, task_q, flt, suc, fail,
                          headers=self.headers, proxy=self.proxy, logger=self.logger,
                          keep_alive=keep_alive, proxy_policy=proxy_policy, timeout=timeout, stream_mode=stream_mode)

    def _get_gallery_crawler_worker(self, tid, mode, task, task_q, keep_alive, vote, proxy_policy, timeout=10):
        """Factory for new gallery metadata/page crawler worker (v2, not wired by default)."""
        runtime = WorkerRuntime(
            keep_alive=lambda wrk, _exit=False: keep_alive(wrk, _exit=_exit),
            vote=lambda tname, code: vote(tname, code),
            proxy_gate=self._v2_proxy_gate,
            proxy_pool=self.proxy,
        )
        return GalleryCrawlerWorker(
            tname=tid,
            mode=mode,
            task=task,
            task_queue=task_q,
            logger=self.logger,
            headers=self.headers,
            proxy=self.proxy,
            proxy_policy=proxy_policy,
            runtime=runtime,
            timeout=timeout,
        )

    def _get_single_page_download_worker(self, tid, task, page_q, keep_alive, vote, proxy_policy, timeout=10):
        """Factory for new single-page immediate downloader worker (v2, not wired by default)."""
        runtime = WorkerRuntime(
            keep_alive=lambda wrk, _exit=False: keep_alive(wrk, _exit=_exit),
            vote=lambda tname, code: vote(tname, code),
            proxy_gate=self._v2_proxy_gate,
            proxy_pool=self.proxy,
        )
        return SinglePageDownloadWorker(
            tname=tid,
            task=task,
            page_queue=page_q,
            logger=self.logger,
            headers=self.headers,
            proxy=self.proxy,
            proxy_policy=proxy_policy,
            runtime=runtime,
            timeout=timeout,
            download_timeout=task.config['download_timeout'],
            download_ori=task.config['download_ori'] and self.has_login,
        )

    def _get_archive_build_worker(self, task:Task):
        """Factory for asynchronous archive builder (v2, not wired by default)."""
        return ArchiveBuildWorker(
            logger=self.logger,
            task=task,
            runtime=WorkerRuntime(
                proxy_gate=self._v2_proxy_gate,
                proxy_pool=self.proxy,
            ),
        )

    def _stage_get_meta(self, task:Task, task_guid:str, req:HttpReq):
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
            self.tasks.put(task_guid)
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

    def _handle_exact_match_found(self, task:Task, task_guid:str, found_archive:str) -> None:
        """Handle found exact matching archive: relocate, update zip comment, and finish task."""
        # Relocate found archive from old directory to new task directory
        current_arc = "%s.zip" % task.get_task_dir()
        if found_archive and os.path.abspath(found_archive) != os.path.abspath(current_arc):
            task_folder = task.get_task_dir()
            if not os.path.exists(task_folder):
                os.makedirs(task_folder)
            shutil.move(found_archive, current_arc)
        self.logger.info(i18n.DF_FULLY_MATCHED % (task.guid, task.meta.title, found_archive))
        # Directly update zip metadata without scanning/downloading
        try:
            arc = task.make_archive(remove=False)
            self.logger.info(i18n.DF_FULLY_MATCHED_UPDATED % (task.guid, arc))
        except Exception as ex:
            self.logger.error(i18n.TASK_ERROR % (task.guid, traceback.format_exc()))
        self._update_task_reuse_index(task)
        task.state = TASK_STATE_FINISHED

    def _stage_check_archive_phase1(self, task:Task, task_guid:str):
        """Stage: Check if exact matching archive exists after GET_META (Phase 1).
        
        Phase 1: Requires fid_page_hash_map in archive metadata. If found archive has
        fid_page_hash_map, reuse it and skip page scanning.
        """
        if not task.meta:
            task.state = TASK_STATE_GET_META
            return 'retry_meta'
        
        # Phase 1: Try to find exact matching archive with fid_page_hash_map
        is_exact_match, found_archive = task.exact_downloaded_exits(require_fid_page_hash_map=True)
        
        if is_exact_match:
            self._handle_exact_match_found(task, task_guid, found_archive)
            return 'finished'
        
        # No exact match found, continue to SCAN_PAGE
        return 'continue_scan_page'

    def _stage_scan_page(self, task:Task, task_guid:str, req:HttpReq):
        """Stage: Scan gallery pages for image URLs and check archive (Phase 2)."""
        temp_fid_2_page_url_map = {}
        for x in range(0,
                       int(math.ceil(1.0 * task.meta.total / int(task.meta.thumbnail_cnt)))):
            r = req.request("GET",
                            "%s/?p=%d" % (task.url, x),
                            filters.flt_pageurl,
                            lambda x: task.queue_wrapper(
                                temp_fid_2_page_url_map.setdefault, img_tuble=x),
                            lambda x: task.set_fail(x))
            if task.failcode:
                break

        if task.state == TASK_STATE_FAILED:
            return False
        
        # Phase 2: After scanning pages, try exact match again (now fid_page_hash_map is built from scan)
        is_exact_match, found_archive = task.exact_downloaded_exits(require_fid_page_hash_map=False)
        if is_exact_match:
            self._handle_exact_match_found(task, task_guid, found_archive)
            return False  # Stop processing, task is complete
        
        # No exact match, check if all files are already downloaded
        if task.scan_downloaded(temp_fid_2_page_url_map):
            # All files found, update archive and finish
            self.logger.info(i18n.TASK_TITLE % (task_guid, task.meta.title))
            try:
                arc = task.make_archive(remove=False)
                self.logger.info(i18n.DF_FULLY_MATCHED_UPDATED % (task.guid, arc))
            except Exception as ex:
                self.logger.error(i18n.TASK_ERROR % (task.guid, traceback.format_exc()))
            self._update_task_reuse_index(task)
            task.state = TASK_STATE_FINISHED
            return False  # Stop processing, task is complete
        
        task.state = TASK_STATE_SCAN_IMG
        return True

    def _stage_scan_img(self, task:Task, task_guid:str, mon):
        """Stage: Spawn worker threads to scan individual image pages."""
        # print here so that see it after we can join former threads
        self.logger.info(i18n.TASK_TITLE % (task_guid, task.meta.title))

        # log at here is quite too early
        # finished file counting will be cleared after page scan
        self.logger.info(i18n.TASK_WILL_DOWNLOAD_CNT % (
            task_guid, task.meta.total - task.meta.finished,
            task.meta.total))
        
        # spawn thread to scan images
        task.img_q.queue.clear()
        for i in range(task.config['scan_thread_cnt']):
            tid = 'scan-%d' % (i + 1)
            _ = self._get_httpworker(tid, task.page_q,
                                     filters.flt_imgurl_wrapper(
                                         task.config['download_ori'] and self.has_login),
                                     lambda x, tid=tid: (task.set_reload_url(x[0], x[1], x[2], x[3]),
                                                         mon.vote(tid, 0)),
                                     lambda x, tid=tid: (
                                         mon.vote(tid, x[0])),
                                     mon.wrk_keepalive,
                                     util.get_proxy_policy(
                                         task.config),
                                     10,
                                     False)
            # we don't need proxy_image in the scan thread
            # we use default timeout in the scan thread
            self._all_threads[TASK_STATE_SCAN_IMG].append(_)
            _.start()
        
        task.state = TASK_STATE_DOWNLOAD
        return True

    def _stage_download(self, task:Task, task_guid:str, mon):
        """Stage: Spawn worker threads to download images."""
        # spawn thread to download all urls
        for i in range(task.config['download_thread_cnt']):
            tid = 'down-%d' % (i + 1)
            _ = self._get_httpworker(tid, task.img_q,
                                     filters.download_file_wrapper(
                                         task.config['dir']),
                                     lambda _x, _tid=tid: (task.save_file(_x[1], _x[2], _x[0], _x[3], _x[4]) and
                                                           (self.logger.debug(i18n.XEH_FILE_DOWNLOADED.format(_tid, *task.get_fname(_x[1]))),
                                                            mon.vote(_tid, 0))),
                                     lambda _x, _tid=tid: (
                                         task.page_q.put(task.get_reload_url(
                                             _x[1])) if 'hentai.org/img/509.gif' not in _x[1] else None,
                                         # delete old url in reload_map
                                         task.reload_map.pop(
                                             _x[1]) if _x[1] in task.reload_map else None,
                                         self.logger.debug(
                                             i18n.XEH_DOWNLOAD_HAS_ERROR % (tid,
                                                                            i18n.c(_x[0]) + ' (' + _x[1] + ') ')),
                                         mon.vote(_tid, _x[0])),
                                     mon.wrk_keepalive,
                                     util.get_proxy_policy(
                                         task.config),
                                     task.config['download_timeout'],
                                     True)
            self._all_threads[TASK_STATE_DOWNLOAD].append(_)
            _.start()
        
        # spawn archiver if we need
        if task.config['make_archive']:
            if self._all_threads[TASK_STATE_MAKE_ARCHIVE]:
                self._all_threads[TASK_STATE_MAKE_ARCHIVE][0].join()
                self._all_threads[TASK_STATE_MAKE_ARCHIVE] = []
            _a = ArchiveWorker(self.logger, task)
            self._all_threads[TASK_STATE_MAKE_ARCHIVE].append(_a)
            _a.start()
        
        self._update_task_reuse_index(task)
        return False  # End of coroutine

    def _do_task_coroutine(self, task:Task, task_guid:str, req:HttpReq, mon_ref):
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
        if task.state == TASK_STATE_SCAN_IMG:
            # Monitor should be started by _do_task before calling stages
            if not mon_ref[0]:
                return
            if not self._stage_scan_img(task, task_guid, mon_ref[0]):
                return
            yield 'scan_img_complete'
        
        # Stage 5: DOWNLOAD
        if task.state == TASK_STATE_DOWNLOAD:
            if not mon_ref[0]:
                return
            self._stage_download(task, task_guid, mon_ref[0])
            # Workers now running in background
            return

    def add_task(self, url, **cfg_dict):
        url = url.strip()
        cfg = {k: v for k, v in self.cfg.items() if k in (
            "dir", "download_ori", "download_thread_cnt", "scan_thread_cnt",
            "proxy_image", "proxy_image_only", "ignored_errors",
            "rename_ori", "make_archive", "delete_task_files", "jpn_title", "download_range", "download_timeout")}
        cfg.update(cfg_dict)
        if cfg['download_ori'] and not self.has_login:
            self.logger.warning(i18n.XEH_DOWNLOAD_ORI_NEED_LOGIN)
        t = Task(url, cfg)

        # check if task on same url already exists
        # well, you may need to download from a link and save images in different zip files
        # but in fact, this program doesnt support auto rename zip files
        # and as for me, i prefer restart the same task when i click 'add to xehentai'
        # in order to repair truncated images
        for taskitem in self._all_tasks.items():
            if url == taskitem[1].url:
                rguid = taskitem[0]
                self._all_tasks.pop(rguid)
                t.guid = rguid
                self._all_tasks[t.guid] = t
                self._all_tasks[t.guid].state = TASK_STATE_GET_META
                self.tasks.put(t.guid)
                return 0, t.guid

        # task don't exists
        if t.guid in self._all_tasks:
            if self._all_tasks[t.guid].state in (TASK_STATE_FINISHED, TASK_STATE_FAILED):
                self.logger.debug(i18n.TASK_PUT_INTO_WAIT % t.guid)
                self._all_tasks[t.guid].state = TASK_STATE_WAITING
                self._all_tasks[t.guid].cleanup()
            return 0, t.guid
        self._all_tasks[t.guid] = t
        if not re.match(r"^%s/[^/]+/\d+/[^/]+/*#*$" % RESTR_SITE, url):
            t.set_fail(ERR_URL_NOT_RECOGNIZED)
        elif not self.has_login and re.match(r"^https*://exhentai\.org", url):
            t.set_fail(ERR_CANT_DOWNLOAD_EXH)
        else:
            self.tasks.put(t.guid)
            return 0, t.guid
        self.logger.error(i18n.TASK_ERROR % (t.guid, i18n.c(t.failcode)))
        return t.failcode, None

    def del_task(self, guid):
        if guid not in self._all_tasks:
            return ERR_TASK_NOT_FOUND, None
        if TASK_STATE_PAUSED < self._all_tasks[guid].state < TASK_STATE_FINISHED:
            return ERR_DELETE_RUNNING_TASK, None
        self._all_tasks[guid].cleanup(before_delete=True)
        del self._all_tasks[guid]
        return ERR_NO_ERROR, ""

    def pause_task(self, guid):
        if guid not in self._all_tasks:
            return ERR_TASK_NOT_FOUND, None
        t = self._all_tasks[guid]
        if t.state in (TASK_STATE_PAUSED, TASK_STATE_FINISHED, TASK_STATE_FAILED):
            return ERR_TASK_CANNOT_PAUSE, None
        if t._monitor:
            t._monitor._exit = lambda x: True
        t.state = TASK_STATE_PAUSED
        return ERR_NO_ERROR, ""

    def resume_task(self, guid):
        if guid not in self._all_tasks:
            return ERR_TASK_NOT_FOUND, None
        t = self._all_tasks[guid]
        if TASK_STATE_PAUSED < t.state < TASK_STATE_FINISHED:
            return ERR_TASK_CANNOT_RESUME, None
        t.state = max(t.state, TASK_STATE_WAITING)

        # image link is changed everytime the page is reloaded
        # so we need to re scan them
        if t.state > TASK_STATE_SCAN_PAGE:
            t.state = TASK_STATE_SCAN_PAGE
        self.tasks.put(guid)
        return ERR_NO_ERROR, ""

    def _do_task(self, task_guid):
        """Execute a task using coroutine-based stages instead of manual state machine."""
        task = self._all_tasks[task_guid]
        task._reuse_index = self.global_reuse_index
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
                mon.start()
                # Put in the lowest state
                self._all_threads[TASK_STATE_SCAN_IMG].append(mon)
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

    def _task_loop(self):
        task_guid = None
        cnt = 0
        while not self._exit:
            # get a new task
            if cnt == 10:
                self.save_session()
                cnt = 0
            try:
                _ = self.tasks.get(False)
                self.last_task_guid = task_guid
                task_guid = _
            except Empty:
                time.sleep(1)
                cnt += 1
                continue
            else:
                task = self._all_tasks[task_guid]
                if TASK_STATE_PAUSED < task.state < TASK_STATE_FINISHED:
                    self.logger.info(i18n.TASK_START % task_guid)
                    self.save_session()
                    cnt = 0
                    self._do_task(task_guid)
                    self._update_task_reuse_index(task)
        self.logger.info(i18n.XEH_LOOP_FINISHED)
        self._cleanup()

    def _term_threads(self):
        self._exit = XEH_STATE_FULL_EXIT
        for l in self._all_threads:
            for p in l:
                p._exit = lambda x: True

    def _cleanup(self):
        self._exit = self._exit if self._exit > 0 else XEH_STATE_SOFT_EXIT
        self.save_session()
        self._join_all()
        self.logger.cleanup()
        # let's send a request to rpc server to unblock it
        if self.rpc:
            self.rpc._exit = lambda x: True
            import requests
            try:
                requests.get("http://%s:%s/" %
                             (self.cfg['rpc_interface'], self.cfg['rpc_port']))
            except:
                pass
            self.rpc.join()
        # save it again in case we miss something
        self.save_session()
        self._exit = XEH_STATE_CLEAN

    def _join_all(self):
        for l in self._all_threads:
            for p in l:
                p.join()

    def save_session(self):
        errors = []
        try:
            session_store.save_tasks(
                {} if not self.cfg['save_tasks'] else
                {k: v.to_dict() for k, v in self._all_tasks.items()})
        except Exception as ex:
            errors.append(str(ex))
            self.logger.warning(i18n.SESSION_WRITE_EXCEPTION %
                                traceback.format_exc())

        try:
            session_store.save_cookies(self.cookies)
        except Exception as ex:
            errors.append(str(ex))
            self.logger.warning(i18n.SESSION_WRITE_EXCEPTION %
                                traceback.format_exc())

        try:
            reuse_index.save_reuse_index(self.global_reuse_index)
        except Exception as ex:
            errors.append(str(ex))
            self.logger.warning(i18n.SESSION_WRITE_EXCEPTION %
                                traceback.format_exc())
        if errors:
            return ERR_SAVE_SESSION_FAILED, errors[0]
        return ERR_NO_ERROR, None

    def load_session(self):
        legacy_session = {}
        if session_store.has_legacy_session_file():
            try:
                legacy_session = session_store.load_legacy_session()
            except Exception as ex:
                self.logger.warning(
                    i18n.SESSION_LOAD_EXCEPTION % traceback.format_exc())
                return ERR_SAVE_SESSION_FAILED, str(ex)

        try:
            tasks_payload = session_store.load_tasks() if session_store.has_tasks_file() else legacy_session.get('tasks', {})
        except Exception as ex:
            self.logger.warning(
                i18n.SESSION_LOAD_EXCEPTION % traceback.format_exc())
            return ERR_SAVE_SESSION_FAILED, str(ex)

        for _ in tasks_payload.values():
            _t = Task("", {}).from_dict(_)
            if _t.meta.filelist:
                _t.scan_downloaded()
                # _t.meta['has_ori'] and task.config['download_ori'])

            # page may have changed by the uploader, rescan pages (rescan from metadata in practice) instead
            # meta can be changed too
            # besides, ip address of exhentai server may have changed, rescan on reload is essential
            if _t.state == TASK_STATE_SCAN_PAGE or _t.state == TASK_STATE_SCAN_IMG or _t.state == TASK_STATE_DOWNLOAD:
                _t.page_q = Queue()
                _t.reload_map = {}
                _t.filehash_map = {}
                _t.fid_2_file_name_map = {}
                _t.fid_2_file_ext_map = {}
                _t.state = TASK_STATE_GET_META
            self._all_tasks[_['guid']] = _t
            self.tasks.put(_['guid'])
        if self._all_tasks:
            self.logger.info(i18n.XEH_LOAD_TASKS_CNT %
                             len(self._all_tasks))

        try:
            loaded_cookies = session_store.load_cookies() if session_store.has_cookies_file() else legacy_session.get('cookies', {})
        except Exception as ex:
            self.logger.warning(
                i18n.SESSION_LOAD_EXCEPTION % traceback.format_exc())
            return ERR_SAVE_SESSION_FAILED, str(ex)

        self.cookies.update(loaded_cookies)
        if self.cookies:
            self.headers.update(
                {'Cookie': util.make_cookie(self.cookies)})
            self.has_login = 'ipb_member_id' in self.cookies and 'ipb_pass_hash' in self.cookies

        try:
            if os.path.exists(reuse_index.REUSE_INDEX_FILE):
                self.global_reuse_index = reuse_index.load_reuse_index()
            else:
                _index = legacy_session.get('global_reuse_index', {})
                if isinstance(_index, dict) and _index:
                    self.global_reuse_index = reuse_index.ensure_reuse_index(_index)
        except Exception:
            self.logger.warning(
                i18n.SESSION_LOAD_EXCEPTION % traceback.format_exc())
        _1xcookie = os.path.join(
            FILEPATH, ".ehentai.cookie")  # 1.x cookie file
        if not self.has_login and os.path.exists(_1xcookie):
            with open(_1xcookie) as f:
                try:
                    cid, cpw = f.read().strip().split(",")
                    self.cookies.update(
                        {'ipb_member_id': cid, 'ipb_pass_hash': cpw})
                    self.headers.update(
                        {'Cookie': util.make_cookie(self.cookies)})
                    self.has_login = True
                    self.logger.info(i18n.XEH_LOAD_OLD_COOKIE)
                except:
                    pass

        return ERR_NO_ERROR, None

    def login_exhentai(self, name, pwd):
        if 'ipb_member_id' in self.cookies and 'ipb_pass_hash' in self.cookies:
            return
        self.logger.debug(i18n.XEH_LOGIN_EXHENTAI)
        logindata = {
            'UserName': name,
            'returntype': '8',
            'CookieDate': '1',
            'b': 'd',
            'bt': 'pone',
            'PassWord': pwd
        }
        req = self._get_httpreq(util.get_proxy_policy(self.cfg))
        req.request("POST", "https://forums.e-hentai.org/index.php?act=Login&CODE=01",
                    filters.login_exhentai,
                    lambda x: (
                        setattr(self, 'cookies', x),
                        setattr(self, 'has_login', True),
                        self.headers.update(
                            {'Cookie': util.make_cookie(self.cookies)}),
                        self.save_session(),
                        self.logger.info(i18n.XEH_LOGIN_OK)),
                    lambda x: (self.logger.warning(str(x)),
                               self.logger.info(i18n.XEH_LOGIN_FAILED)),
                    logindata)
        return ERR_NO_ERROR, self.has_login

    def set_cookie(self, cookie):
        self.cookies.update(util.parse_cookie(cookie))
        self.headers.update({'Cookie': util.make_cookie(self.cookies)})
        if 'ipb_member_id' in self.cookies and 'ipb_pass_hash' in self.cookies:
            self.has_login = True
        return ERR_NO_ERROR, None


if __name__ == '__main__':
    pass
