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
from .host_interface import HostInterface
from .task_ctrl import TaskControl
from .const import *
from .const import __version__
from .worker import *
from .async_woker import (
    ArchiveBuildWorker,
    GalleryCrawlerWorker,
    KeepAliveFn,
    ManagedWorker,
    ProxyExhaustionGate,
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


class xeHentai(HostInterface):
    def __init__(self):
        self.verstr = f"{__version__}{'-dev' if DEVELOPMENT else ''}"
        self.logger = logger.Logger()
        self.tasks: Queue[str] = Queue()  # for queueing, stores gid only
        self.last_task_guid = None
        self._all_tasks: dict[str,Task] = {}  # for saving states
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
        self._task_control = TaskControl(self)
        self.load_session()
        self.rpc = None

    @property
    def _monitor(self):
        """Expose task control monitor for RPC compatibility."""
        return self._task_control._monitor

    @property
    def _exit(self):
        return self._task_control._exit

    @_exit.setter
    def _exit(self, value):
        self._task_control._exit = value

    def _update_task_reuse_index(self, task):
        """Upsert reusable page-hash mappings from a task into global_reuse_index."""
        self._task_control._update_task_reuse_index(task)

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

    def _task_loop(self):
        self._task_control.run()

    def _term_threads(self):
        self._task_control.terminate()

    def _cleanup(self):
        tc = self._task_control
        tc._exit = tc._exit if tc._exit > 0 else XEH_STATE_SOFT_EXIT
        self.save_session()
        tc.join_all()
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
        tc._exit = XEH_STATE_CLEAN

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
            # Load SQLite database
            self.global_reuse_index = reuse_index.load_reuse_index()
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
