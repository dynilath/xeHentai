#!/usr/bin/env python
# coding:utf-8
# Contributor:
#      fffonion        <fffonion@gmail.com>

import os
import re
import sys
import traceback

from .request_wrapper import HttpRequest
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
from queue import Queue

from .task_config import CoreConfig
from . import config as default_config

sys.path.insert(1, FILEPATH)
try:
    import config
except ImportError:
    config = default_config
sys.path.pop(1)


class xeHentai(HostInterface):
    _TASK_CONFIG_KEYS = (
        "download_ori",
        "make_archive",
        "delete_task_files",
        "jpn_title",
        "download_range",
    )

    def __init__(self):
        self.verstr = f"{__version__}{'-dev' if DEVELOPMENT else ''}"
        self.logger = logger.Logger()
        self.tasks: Queue[str] = Queue()  # for queueing, stores gid only
        self.last_task_guid = None
        self._all_tasks: dict[str, Task] = {}  # for saving states
        _cfg = {
            k: v for k, v in default_config.__dict__.items() if not k.startswith("_")
        }
        # note that ignored_errors are overwritten using val from custom config
        _cfg.update({k: v for k, v in config.__dict__.items() if not k.startswith("_")})
        self.config = CoreConfig(_cfg)
        # backward compatibility for older code paths
        self.cfg = self.config
        self.proxy = None
        self.cookies = {"nw": "1"}
        self.headers = {
            "User-Agent": util.make_ua(),
            "Accept-Charset": "utf-8;q=0.7,*;q=0.7",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Connection": "keep-alive",
        }
        self.has_login = False
        self.global_reuse_index = reuse_index.ensure_reuse_index()
        self._task_control = TaskControl(self)
        self.load_session()
        self.rpc = None

    @property
    def _exit(self):
        return self._task_control._exit

    @_exit.setter
    def _exit(self, value):
        self._task_control._exit = value

    def _load_proxy_store(self):
        try:
            return (
                session_store.load_proxy_store()
                if session_store.has_proxy_file()
                else {}
            )
        except Exception:
            self.logger.warning(i18n.SESSION_LOAD_EXCEPTION % traceback.format_exc())
            return {}

    def _save_proxy_store(self, proxy_store):
        try:
            session_store.save_proxy_store(proxy_store)
        except Exception:
            self.logger.warning(i18n.SESSION_WRITE_EXCEPTION % traceback.format_exc())

    def _merge_proxy_store(self):
        stored = self._load_proxy_store()
        runtime = self.proxy.export_store() if self.proxy else {}
        merged = dict(stored)
        merged.update(runtime)
        return merged

    def _rebuild_proxy_pool(self, configured_proxies):
        store = self._merge_proxy_store()
        active = []
        for addr in configured_proxies:
            if addr not in active:
                active.append(addr)

        for addr in active:
            store.setdefault(addr, {})

        if not active:
            self.proxy = None
            self._save_proxy_store(store)
            return

        rebuilt_pool = proxy.ProxyPool(self.logger)
        for addr in active:
            try:
                rebuilt_pool.add_proxy(addr, state=store.get(addr, {}))
            except Exception:
                self.logger.warning(traceback.format_exc())

        self.proxy = rebuilt_pool
        self._save_proxy_store(store)

    def update_config(self, **cfg_dict):
        self.config.update(
            {k: v for k, v in cfg_dict.items() if k not in ("ignored_errors",)}
        )
        # merge ignored errors list
        if "ignored_errors" in cfg_dict and cfg_dict["ignored_errors"]:
            self.config["ignored_errors"] = list(
                set(self.config["ignored_errors"] + cfg_dict["ignored_errors"])
            )
        self.logger.set_level(logger.Logger.WARNING - self.config["log_verbose"])
        self.logger.verbose("cfg %s" % self.config)
        if "proxy" in cfg_dict:
            self._rebuild_proxy_pool(self.config["proxy"])
            self.logger.debug(
                i18n.PROXY_CANDIDATE_CNT
                % (0 if not self.proxy else len(self.proxy.proxies))
            )
        if self.config["dir"] and not os.path.exists(self.config["dir"]):
            try:
                os.makedirs(self.config["dir"])
            except OSError as ex:  # Python >2.5
                self.logger.error(i18n.ERR_CANNOT_CREATE_DIR % self.config["dir"])
        if not self.rpc and self.config["rpc_port"] and self.config["rpc_interface"]:
            self.rpc = RPCServer(
                self,
                (self.config["rpc_interface"], int(self.config["rpc_port"])),
                secret=(
                    None
                    if "rpc_secret" not in self.config
                    else self.config["rpc_secret"]
                ),
                logger=self.logger,
            )
            if (
                not RE_LOCAL_ADDR.match(self.config["rpc_interface"])
                and not self.config["rpc_secret"]
            ):
                self.logger.warning(i18n.RPC_TOO_OPEN % self.config["rpc_interface"])
            self.rpc.start()
        self.logger.set_logfile(self.config["log_path"])
        return ERR_NO_ERROR, ""

    def add_task(self, url, **cfg_dict):
        url = url.strip()
        cfg = {k: v for k, v in cfg_dict.items() if k in self._TASK_CONFIG_KEYS}
        download_ori = cfg.get("download_ori", self.config.get("download_ori"))
        if download_ori and not self.has_login:
            self.logger.warning(i18n.XEH_DOWNLOAD_ORI_NEED_LOGIN)
        t = Task(url, cfg, self.logger, core_config=self.config)

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
            if self._all_tasks[t.guid].state in (
                TASK_STATE_FINISHED,
                TASK_STATE_FAILED,
            ):
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
            self._save_session(task=True)
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
        self._save_session(task=True)
        tc.join_all()
        self.logger.cleanup()
        # let's send a request to rpc server to unblock it
        if self.rpc:
            self.rpc._exit = lambda x: True
            import requests

            try:
                requests.get(
                    "http://%s:%s/" % (self.cfg["rpc_interface"], self.cfg["rpc_port"])
                )
            except:
                pass
            self.rpc.join()
        # save it again in case we miss something
        self._save_session(task=True)
        tc._exit = XEH_STATE_CLEAN

    def _save_session(self,*, task=False, proxy_store=False, cookies=False):
        errors = []
        if task:
            try:
                session_store.save_tasks(
                    {}
                    if not self.config["save_tasks"]
                    else {k: v.to_dict() for k, v in self._all_tasks.items()}
                )
            except Exception as ex:
                errors.append(str(ex))
                self.logger.warning(i18n.SESSION_WRITE_EXCEPTION % traceback.format_exc())

        if cookies:
            try:
                session_store.save_cookies(self.cookies)
            except Exception as ex:
                errors.append(str(ex))
                self.logger.warning(i18n.SESSION_WRITE_EXCEPTION % traceback.format_exc())
        
        if proxy_store and self.proxy: 
            try:
                self._save_proxy_store(self._merge_proxy_store())
            except Exception as ex:
                errors.append(str(ex))
                self.logger.warning(i18n.SESSION_WRITE_EXCEPTION % traceback.format_exc())
        
        return errors

    
    def system_status(self):
        state_2_names = {
            TASK_STATE_PAUSED: "paused",
            TASK_STATE_WAITING: "waiting",
            TASK_STATE_GET_META: "getting meta",
            TASK_STATE_SCAN_PAGE: "scanning page",
            TASK_STATE_SCAN_IMG: "scanning images",
            TASK_STATE_SCAN_ARCHIVE: "scanning archive",
            TASK_STATE_DOWNLOAD: "downloading",
            TASK_STATE_MAKE_ARCHIVE: "making archive",
            TASK_STATE_FINISHED: "finished",
            TASK_STATE_FAILED: "failed",
        }

        working_status: dict[str, int] = {}
        for guid, task in self._all_tasks.items():
            state_name = state_2_names.get(task.state, "unknown")
            working_status[state_name] = working_status.get(state_name, 0) + 1

        return ERR_NO_ERROR, working_status

    def load_session(self):
        legacy_session = {}
        if session_store.has_legacy_session_file():
            try:
                legacy_session = session_store.load_legacy_session()
            except Exception as ex:
                self.logger.warning(
                    i18n.SESSION_LOAD_EXCEPTION % traceback.format_exc()
                )
                return ERR_SAVE_SESSION_FAILED, str(ex)

        try:
            tasks_payload = (
                session_store.load_tasks()
                if session_store.has_tasks_file()
                else legacy_session.get("tasks", {})
            )
        except Exception as ex:
            self.logger.warning(i18n.SESSION_LOAD_EXCEPTION % traceback.format_exc())
            return ERR_SAVE_SESSION_FAILED, str(ex)

        for _ in tasks_payload.values():
            _t = Task(_["url"], {}, self.logger, core_config=self.config).from_dict(
                _, core_config=self.config
            )
            self._all_tasks[_["guid"]] = _t
            self.tasks.put(_["guid"])
        if self._all_tasks:
            self.logger.info(i18n.XEH_LOAD_TASKS_CNT % len(self._all_tasks))

        try:
            loaded_cookies = (
                session_store.load_cookies()
                if session_store.has_cookies_file()
                else legacy_session.get("cookies", {})
            )
        except Exception as ex:
            self.logger.warning(i18n.SESSION_LOAD_EXCEPTION % traceback.format_exc())
            return ERR_SAVE_SESSION_FAILED, str(ex)

        self.cookies.update(loaded_cookies)
        if self.cookies:
            self.headers.update({"Cookie": util.make_cookie(self.cookies)})
            self.has_login = (
                "ipb_member_id" in self.cookies and "ipb_pass_hash" in self.cookies
            )

        try:
            # Load SQLite database
            self.global_reuse_index = reuse_index.load_reuse_index()
        except Exception:
            self.logger.warning(i18n.SESSION_LOAD_EXCEPTION % traceback.format_exc())
        _1xcookie = os.path.join(FILEPATH, ".ehentai.cookie")  # 1.x cookie file
        if not self.has_login and os.path.exists(_1xcookie):
            with open(_1xcookie) as f:
                try:
                    cid, cpw = f.read().strip().split(",")
                    self.cookies.update({"ipb_member_id": cid, "ipb_pass_hash": cpw})
                    self.headers.update({"Cookie": util.make_cookie(self.cookies)})
                    self.has_login = True
                    self.logger.info(i18n.XEH_LOAD_OLD_COOKIE)
                except:
                    pass

        return ERR_NO_ERROR, None

    def login_exhentai(self, name, pwd):
        if "ipb_member_id" in self.cookies and "ipb_pass_hash" in self.cookies:
            return
        self.logger.debug(i18n.XEH_LOGIN_EXHENTAI)
        logindata = {
            "UserName": name,
            "returntype": "8",
            "CookieDate": "1",
            "b": "d",
            "bt": "pone",
            "PassWord": pwd,
        }
        req = HttpRequest({}, self.logger, "main")
        
        r = req.request(
            "POST",
            "https://forums.e-hentai.org/index.php?act=Login&CODE=01",
            data=logindata
        )
        
        coo = r.response.headers.get('set-cookie')
        if not coo:
            raise Exception("No set-cookie header found in login response")
        
        try:
            cooid = re.findall('ipb_member_id=(.*?);', coo)[0]
            coopw = re.findall('ipb_pass_hash=(.*?);', coo)[0]
        except (IndexError, ) as ex:
            errmsg = re.findall('<span class="postcolor">([^<]+)</span>', r.response.text)
            if errmsg:
                raise Exception(errmsg[0])
            raise Exception("Login failed: %s" % str(ex))
        
        self.cookies.update({'ipb_member_id': cooid, 'ipb_pass_hash': coopw})
        self.headers.update({"Cookie": util.make_cookie(self.cookies)})
        self.has_login = True
        self._save_session(cookies=True)
        self.logger.info(i18n.XEH_LOGIN_OK)
        
        return ERR_NO_ERROR, self.has_login

    def set_cookie(self, cookie):
        self.cookies.update(util.parse_cookie(cookie))
        self.headers.update({"Cookie": util.make_cookie(self.cookies)})
        if "ipb_member_id" in self.cookies and "ipb_pass_hash" in self.cookies:
            self.has_login = True
        return ERR_NO_ERROR, None


if __name__ == "__main__":
    pass
