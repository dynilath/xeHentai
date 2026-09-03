#!/usr/bin/env python
# coding:utf-8
# Contributor:
#      fffonion        <fffonion@gmail.com>

from threading import RLock
import os
import re
import sys
import traceback
from typing import List, Optional

from .request_wrapper import HttpRequest
from .task import Task
from . import reuse_index
from . import session_store
from . import util
from . import proxy
from . import filters
from .subscription import SubscriptionManager
from .i18n import i18n
from .util import logger
from .host_interface import HostInterface
from .task_ctrl import TaskControl
from .const import *
from .const import __version__
from queue import Queue

from .task_config import CoreConfig
from .config_loader import load_config


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
    TASK_STATE_HAS_NEW_VERSION: "has new version",
    TASK_STATE_FAILED: "failed",
    TASK_STATE_ERR_GALLERY_REMOVED: "gallery removed",
    TASK_STATE_ERR_GALLERY_NOT_FOUND: "gallery not found",
}


def parse_task(t: Task):
    return {
        "guid": t.guid,
        "gid": t.gid,
        "url": t.url,
        "state": state_2_names.get(t.state, "unknown"),
        "phase_state": t.state,
        "done": len(t._flist_done),
        "total": t.meta.total if t.meta else 0,
    }


def _state_name(state: int) -> str:
    return state_2_names.get(state, "unknown" if state >= 0 else f"error.{state}")


def parse_task_row(row: dict):
    """Build a task status dict from a lightweight DB row.

    `done` is only accurate for active tasks; for cold tasks we report 0 (the
    accurate done count is not persisted as a column, by design).
    """
    return {
        "guid": row.get("guid", ""),
        "gid": row.get("gid", ""),
        "url": row.get("url", ""),
        "state": _state_name(int(row.get("phase_state", TASK_STATE_WAITING))),
        "phase_state": int(row.get("phase_state", TASK_STATE_WAITING)),
        "done": 0,
        "total": int(row.get("total", 0) or 0),
        "title": row.get("title", "") or "",
    }


def _derive_top_status(state: int) -> int:
    """Derive a coarse top_status from a phase_state without an in-memory task.

    WAITING -> WAITING; FINISHED/FAILED/PAUSED/negative errors -> PROCESSED;
    everything in between (active stages) -> PROCESSING.
    """
    if state == TASK_STATE_WAITING:
        return TASK_TOP_STATUS_WAITING
    if (
        state == TASK_STATE_FINISHED
        or state == TASK_STATE_HAS_NEW_VERSION
        or state < 0
        or state == TASK_STATE_PAUSED
    ):
        return TASK_TOP_STATUS_PROCESSED
    return TASK_TOP_STATUS_PROCESSING


def _top_name_for_state(state: int) -> str:
    return task_top_status_name(_derive_top_status(state))


class xeHentai(HostInterface):
    _TASK_CONFIG_KEYS = (
        "download_ori",
        "delete_task_files",
        "jpn_title",
    )

    def __init__(self, config=None, log=None):
        self.verstr = f"{__version__}{'-dev' if DEVELOPMENT else ''}"
        self.logger = log or logger.Logger()
        self.tasks: Queue[str] = Queue()  # for queueing, stores guid only
        self.last_task_guid = None
        self._task_lock: RLock = RLock()
        # Only currently-executing tasks live in memory (bounded by
        # async_task_concurrency). All other tasks live in SQLite and are
        # hydrated on demand. The DB is the single source of truth.
        self._active_tasks: dict[str, Task] = {}
        if config is not None:
            _cfg = dict(config)
        else:
            _yaml_config = load_config()
            _cfg = _yaml_config.to_flat_dict()
        self.config = CoreConfig(_cfg)
        # backward compatibility for older code paths
        self.cfg = self.config
        self.proxy = None
        self.logger.debug("config: %s", dict(self.config))
        self._rebuild_proxy_pool(self.config["proxy"])
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
        self._subscriptions = SubscriptionManager(self)
        self.load_session()

    def _new_guid(self) -> str:
        # Keep backward-compatible 8-char guid while guaranteeing runtime uniqueness.
        # Check both active tasks and the DB (cheap UNIQUE PK lookup).
        while True:
            guid = os.urandom(4).hex()
            if guid in self._active_tasks:
                continue
            if session_store.get_task_row(guid) is not None:
                continue
            return guid

    def _get_active_task(self, guid: str) -> Optional[Task]:
        """Return an in-memory active Task without hydrating. None if cold."""
        return self._active_tasks.get(guid)

    def _hydrate_task(self, guid: str) -> Optional[Task]:
        """Load a single Task from the DB into the active set and return it.

        If the task is already active, returns the existing instance. If the
        guid is unknown, returns None. Hydration parses one payload row (~1ms).
        """
        if guid in self._active_tasks:
            return self._active_tasks[guid]
        payload = session_store.load_task_payload(guid)
        if payload is None:
            return None
        try:
            t = Task(payload.get("url", ""), {}, self.logger, core_config=self.config)
            t.from_dict(payload, core_config=self.config)
        except Exception:
            self.logger.warning(
                "Failed to hydrate task %s:\n%s" % (guid, traceback.format_exc())
            )
            return None
        t._reuse_index = self.global_reuse_index
        self._active_tasks[guid] = t
        return t

    def _dehydrate_task(self, guid: str) -> None:
        """Persist an active Task back to the DB and evict it from memory.

        Idempotent: a no-op if the guid is not active. This is the single
        eviction point called by the run loop after a task finishes/fails/etc.
        """
        t = self._active_tasks.pop(guid, None)
        if t is None:
            return
        try:
            session_store.save_task_from_active(t)
        except Exception:
            self.logger.warning(
                "Failed to dehydrate task %s:\n%s" % (guid, traceback.format_exc())
            )
        t.cleanup()

    def _register_task(self, t: Task) -> None:
        """Persist a freshly created Task to the DB (does not keep it active).

        Kept for compatibility with call sites that build a Task and want it
        stored; the task is NOT placed in _active_tasks here.
        """
        with self._task_lock:
            session_store.save_task_from_active(t)

    def _unregister_task(self, guid: str) -> None:
        """Remove a task from the DB and from the active set."""
        with self._task_lock:
            self._active_tasks.pop(guid, None)
            self._task_control.clear_task_top_status(guid)
            session_store.delete_task(guid)

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
        self.logger.debug("config update: %s", cfg_dict)
        self.config.update(cfg_dict)
        self.logger.set_console_level(self.config["log_level_console"])
        self.logger.set_file_level(self.config["log_level_file"])
        self.logger.debug("config: %s", dict(self.config))
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
        self.logger.set_log_path(self.config["log_path"])
        return ERR_NO_ERROR, ""

    def _add_task(self, url, *, enqueue_existed=False, **cfg_dict):
        """Internal: create, register, and enqueue a new task. Returns (error_code, guid_or_none)."""
        url = url.strip()
        cfg = {k: v for k, v in cfg_dict.items() if k in self._TASK_CONFIG_KEYS}

        download_ori = cfg.get("download_ori", self.config.get("download_ori"))
        if download_ori and not self.has_login:
            self.logger.warning(i18n.XEH_DOWNLOAD_ORI_NEED_LOGIN)

        if not re.match(r"^%s/[^/]+/\d+/[^/]+/*#*$" % RESTR_SITE, url):
            return ERR_URL_NOT_RECOGNIZED, None

        if not self.has_login and re.match(RE_STR_EXHENTAI_PREFIX, url):
            return ERR_CANT_DOWNLOAD_EXH, None

        t = Task(url, cfg, self.logger, core_config=self.config)

        # Dedup by gid via DB (gid has a UNIQUE index). No in-memory map needed.
        existing_guid = session_store.find_guid_by_gid(str(t.gid))
        if existing_guid:
            if enqueue_existed:
                existing = self._hydrate_task(existing_guid)
                if existing is not None and (
                    existing.state in (TASK_STATE_PAUSED, TASK_STATE_FINISHED)
                    or existing.state < 0
                ):
                    existing.url = t.url
                    existing.config = t.config
                    existing.set_phase_state(TASK_STATE_WAITING)
                    existing.cleanup()
                    self._task_control.enqueue_waiting_task(existing.guid)
                    self._dehydrate_task(existing.guid)
                    try:
                        from .web.ws import emit_task_added
                        emit_task_added(existing.guid, existing.gid)
                    except ImportError:
                        pass
            return 0, existing_guid

        # Ensure the new guid is unique across active set and DB.
        if t.guid in self._active_tasks or session_store.get_task_row(t.guid) is not None:
            t.guid = self._new_guid()

        t.set_phase_state(TASK_STATE_WAITING)
        session_store.save_task_from_active(t)
        self._task_control.enqueue_waiting_task(t.guid)
        try:
            from .web.ws import emit_task_added
            emit_task_added(t.guid, t.gid)
        except ImportError:
            pass
        return 0, t.guid

    def add_task(self, url, **cfg_dict):
        """Public/RPC-facing wrapper for adding a task."""
        cfg_dict.setdefault("enqueue_existed", False)
        return self._add_task(url, **cfg_dict)

    def add_tasks(self, urls, **cfg_dict):
        """Add multiple tasks. Returns a list of (error_code, guid_or_none) for each url."""
        results = []
        cfg_dict.setdefault("enqueue_existed", False)
        for url in urls:
            code, guid = self._add_task(url, **cfg_dict)
            if code != ERR_NO_ERROR:
                return code, results
            results.append(guid)
        return ERR_NO_ERROR, results

    def del_task(self, guid):
        row = session_store.get_task_row(guid)
        if row is None:
            return ERR_TASK_NOT_FOUND, None
        active = self._active_tasks.get(guid)
        if active is None:
            return ERR_TASK_NOT_FOUND, None
        cur_state = active.state if active else int(row.get("phase_state", TASK_STATE_WAITING))
        if TASK_STATE_PAUSED < cur_state < TASK_STATE_FINISHED:
            return ERR_DELETE_RUNNING_TASK, None
        if active is None:
            active = self._hydrate_task(guid)
        if active is not None:
            active.cleanup(before_delete=True)
        self._unregister_task(guid)
        return ERR_NO_ERROR, ""

    def pause_task(self, guid):
        row = session_store.get_task_row(guid)
        if row is None:
            return ERR_TASK_NOT_FOUND, None
        active = self._active_tasks.get(guid)
        if active is None:
            return ERR_TASK_NOT_FOUND, None
        cur_state = active.state if active else int(row.get("phase_state", TASK_STATE_WAITING))
        if (
            cur_state in (TASK_STATE_PAUSED, TASK_STATE_FINISHED, TASK_STATE_FAILED)
            or cur_state < 0
        ):
            return ERR_TASK_CANNOT_PAUSE, None
        if active is not None:
            # Active task: set state; the worker will abort via _task_should_abort
            # and the run loop will dehydrate it on return.
            active.set_phase_state(TASK_STATE_PAUSED)
        else:
            # Cold task: lightweight state-only DB update.
            session_store.update_task_state(guid, TASK_STATE_PAUSED)
        self._task_control.mark_task_processed(guid)
        if active is not None:
            self._task_control._emit_ws_task_state_change(active, guid)
        return ERR_NO_ERROR, ""

    def resume_task(self, guid):
        row = session_store.get_task_row(guid)
        if row is None:
            return ERR_TASK_NOT_FOUND, None
        active = self._active_tasks.get(guid)
        if active is None:
            return ERR_TASK_NOT_FOUND, None
        cur_state = active.state if active else int(row.get("phase_state", TASK_STATE_WAITING))
        if TASK_STATE_PAUSED < cur_state < TASK_STATE_FINISHED:
            return ERR_TASK_CANNOT_RESUME, None
        # image link is changed everytime the page is reloaded
        # so we need to re scan them
        new_state = max(cur_state, TASK_STATE_WAITING)
        if new_state > TASK_STATE_SCAN_PAGE:
            new_state = TASK_STATE_SCAN_PAGE
        if active is not None:
            active.set_phase_state(new_state)
        else:
            session_store.update_task_state(guid, new_state)
        self._task_control.enqueue_waiting_task(guid)
        if active is not None:
            self._task_control._emit_ws_task_state_change(active, guid)
        return ERR_NO_ERROR, ""

    def refetch_task_meta(self, guid):
        """Re-fetch the gallery page now and refresh title/tags/newer-version
        info on an existing task, without touching downloaded files.

        Fails while the task itself is actively running its own stages,
        since that stage owns task.meta for the duration of the crawl.
        """
        row = session_store.get_task_row(guid)
        if row is None:
            return ERR_TASK_NOT_FOUND, None
        active = self._active_tasks.get(guid)
        cur_state = active.state if active is not None else int(row.get("phase_state", TASK_STATE_WAITING))
        if TASK_STATE_PAUSED < cur_state < TASK_STATE_FINISHED:
            return ERR_TASK_BUSY, None

        cold = active is None
        task = active if active is not None else self._hydrate_task(guid)
        if task is None:
            return ERR_TASK_NOT_FOUND, None

        req = HttpRequest(self.headers, self.logger, logger_prefix="refetch-%s" % guid)
        try:
            r = req.request(
                "GET",
                task.url,
                retry=self.config.get("page_retry", 3),
                timeout=self.config.get("page_timeout", 10),
                proxy=self.proxy,
                proxy_wait=False,
            )
            meta = filters.flt_metadata(r)
        except Exception as ex:
            if cold:
                self._dehydrate_task(guid)
            return ERR_TASK_REFETCH_FAILED, str(ex)

        task.update_meta(meta)
        if cold:
            self._dehydrate_task(guid)
        else:
            session_store.save_task_from_active(task)
        return ERR_NO_ERROR, ""

    def _task_loop(self):
        self._task_control.run()

    def _term_threads(self):
        self._task_control.terminate()

    def _cleanup(self):
        tc = self._task_control
        tc._exit = tc._exit if tc._exit > 0 else XEH_STATE_SOFT_EXIT
        # Dehydrate any still-active tasks so their latest state is persisted
        # before we tear down the run loop.
        with self._task_lock:
            active_guids = list(self._active_tasks.keys())
        for guid in active_guids:
            self._dehydrate_task(guid)
        tc.join_all()
        self.logger.cleanup()
        tc._exit = XEH_STATE_CLEAN

    def _save_session(
        self,
        *,
        task=False,
        proxy_store=False,
        cookies=False,
        guid: Optional[str] = None,
    ):
        errors = []
        if task:
            # Only active tasks need saving; cold tasks are already persisted
            # (their state changes go through update_task_state / _dehydrate_task).
            if guid:
                t = self._active_tasks.get(guid)
                if t:
                    try:
                        session_store.save_task_from_active(t)
                    except Exception as ex:
                        errors.append(str(ex))
                        self.logger.warning(
                            i18n.SESSION_WRITE_EXCEPTION % traceback.format_exc()
                        )
            # The legacy bulk-save path (no guid) is intentionally a no-op for
            # tasks: each task is saved individually at its lifecycle boundaries.

        if cookies:
            try:
                session_store.save_cookies(self.cookies)
            except Exception as ex:
                errors.append(str(ex))
                self.logger.warning(
                    i18n.SESSION_WRITE_EXCEPTION % traceback.format_exc()
                )

        if proxy_store and self.proxy:
            try:
                self._save_proxy_store(self._merge_proxy_store())
            except Exception as ex:
                errors.append(str(ex))
                self.logger.warning(
                    i18n.SESSION_WRITE_EXCEPTION % traceback.format_exc()
                )

        return errors

    def system_status(self):
        grouped: dict[str, dict[str, int]] = {
            "waiting": {},
            "processing": {},
            "processed": {},
        }
        summary: dict[str, int] = {
            "waiting": 0,
            "processing": 0,
            "processed": 0,
        }

        # Aggregate cold tasks via a single GROUP BY query.
        state_counts = session_store.count_tasks_by_state()
        # Active tasks override their DB phase_state with the live value, so
        # subtract them from the cold aggregate and re-add under the live value.
        active_states: dict[str, int] = {}
        with self._task_lock:
            for guid, task in self._active_tasks.items():
                active_states[guid] = task.state

        for state_val, cnt in state_counts.items():
            # Each guid counted in state_counts reflects the DB row; if that guid
            # is active, its live state may differ. We handle active guids below.
            state_name = _state_name(state_val)
            top_name = _top_name_for_state(state_val)
            grouped[top_name][state_name] = grouped[top_name].get(state_name, 0) + cnt
            summary[top_name] = summary.get(top_name, 0) + cnt

        # Correct for active tasks: remove one from their DB-state bucket and add
        # one to their live-state bucket.
        for guid, live_state in active_states.items():
            row = session_store.get_task_row(guid)
            db_state = int(row.get("phase_state", TASK_STATE_WAITING)) if row else TASK_STATE_WAITING
            if db_state != live_state and row:
                db_name = _state_name(db_state)
                db_top = _top_name_for_state(db_state)
                grouped[db_top][db_name] = max(0, grouped[db_top].get(db_name, 0) - 1)
                summary[db_top] = max(0, summary.get(db_top, 0) - 1)
            live_name = _state_name(live_state)
            live_top = _top_name_for_state(live_state)
            grouped[live_top][live_name] = grouped[live_top].get(live_name, 0) + 1
            summary[live_top] = summary.get(live_top, 0) + 1

        return ERR_NO_ERROR, {
            "summary": summary,
            "detail": grouped,
        }

    def task_status(
        self,
        *,
        guid: Optional[str] = None,
        gid: Optional[str] = None,
        url: Optional[str] = None,
    ):
        if guid:
            active = self._active_tasks.get(guid)
            if active:
                return ERR_NO_ERROR, parse_task(active)
            row = session_store.get_task_row(guid)
            if row:
                return ERR_NO_ERROR, parse_task_row(row)
        if gid:
            found_guid = session_store.find_guid_by_gid(gid)
            if found_guid:
                active = self._active_tasks.get(found_guid)
                if active:
                    return ERR_NO_ERROR, parse_task(active)
                row = session_store.get_task_row(found_guid)
                if row:
                    return ERR_NO_ERROR, parse_task_row(row)
        if url:
            found_guid = session_store.find_guid_by_url(url)
            if found_guid:
                active = self._active_tasks.get(found_guid)
                if active:
                    return ERR_NO_ERROR, parse_task(active)
                row = session_store.get_task_row(found_guid)
                if row:
                    return ERR_NO_ERROR, parse_task_row(row)
        return ERR_TASK_NOT_FOUND, None

    def find_tasks(
        self,
        *,
        state: Optional[int] = None,
        top_status: Optional[int] = None,
        max_count: Optional[int] = None,
    ):
        max_count = max_count or 128
        # DB query covers cold tasks by phase_state.
        states_filter = [state] if state is not None else None
        _, rows = session_store.query_tasks(
            states=states_filter, limit=max_count, order_by="updated_at", order_dir="DESC"
        )
        results = [parse_task_row(row) for row in rows]
        # Enrich with live done/state for any active tasks present in the page.
        for item in results:
            active = self._active_tasks.get(item["guid"])
            if active:
                item["done"] = len(active._flist_done)
                item["phase_state"] = active.state
                item["state"] = _state_name(active.state)
                item["total"] = active.meta.total if active.meta else item["total"]
        # top_status filtering (derived from phase_state) applied client-side.
        if top_status is not None:
            results = [
                item for item in results
                if _derive_top_status(item["phase_state"]) == top_status
            ]
        return ERR_NO_ERROR, results

    def retry_tasks(
        self,
        *,
        guid: Optional[str] = None,
        guids: Optional[list[str]] = None,
        gid: Optional[str] = None,
        url: Optional[str] = None,
    ):
        target_guids: list[str] = []
        if guid:
            target_guids.append(guid)
        if guids:
            target_guids.extend(guids)
        if gid:
            found = session_store.find_guid_by_gid(gid)
            if found:
                target_guids.append(found)
        if url:
            found = session_store.find_guid_by_url(url)
            if found:
                target_guids.append(found)

        results = []
        for g in target_guids:
            active = self._active_tasks.get(g)
            cur_state = active.state if active else None
            if cur_state is None:
                row = session_store.get_task_row(g)
                cur_state = int(row.get("phase_state", TASK_STATE_WAITING)) if row else None
            if cur_state is None or cur_state >= 0:
                continue
            if active is not None:
                active.set_phase_state(TASK_STATE_WAITING)
            else:
                session_store.update_task_state(g, TASK_STATE_WAITING)
            self._task_control.enqueue_waiting_task(g)
            results.append(g)
        # Build status dicts (hydrate only what we need; reuse rows where possible).
        out = []
        for g in results:
            active = self._active_tasks.get(g)
            if active:
                out.append(parse_task(active))
            else:
                row = session_store.get_task_row(g)
                if row:
                    out.append(parse_task_row(row))
        return ERR_NO_ERROR, out

    def count_active_tasks(self) -> int:
        """Count tasks still being worked on (waiting through make_archive).

        Active (in-memory) tasks are counted live; cold tasks via a DB COUNT.
        Used by the CLI to decide whether the process can exit.
        """
        db_count = session_store.count_active_tasks(
            state_low=TASK_STATE_WAITING, state_high=TASK_STATE_FINISHED
        )
        # Active tasks whose live state falls in the active window but whose DB
        # row may already be FINISHED/PAUSED (e.g. just paused) should still
        # count. Conversely active tasks already FINISHED in memory shouldn't.
        live_active = sum(
            1 for t in self._active_tasks.values()
            if TASK_STATE_WAITING <= t.state < TASK_STATE_FINISHED
        )
        # Approximate: take max to avoid undercounting freshly-mutated active
        # tasks whose DB row hasn't been dehydrated yet.
        return max(db_count, live_active)

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

        # Ensure the SQLite schema exists / is migrated before any query.
        try:
            session_store._ensure_schema()
        except Exception as ex:
            self.logger.warning(
                i18n.SESSION_LOAD_EXCEPTION % traceback.format_exc()
            )
            return ERR_SAVE_SESSION_FAILED, str(ex)

        # Rebuild the in-memory waiting queue from the DB. We only load the
        # guids of WAITING tasks — no Task objects, no payload parsing.
        waiting_count = 0
        if session_store.has_tasks_file():
            try:
                waiting_guids = session_store.list_waiting_guids()
            except Exception as ex:
                self.logger.warning(
                    i18n.SESSION_LOAD_EXCEPTION % traceback.format_exc()
                )
                return ERR_SAVE_SESSION_FAILED, str(ex)
            for guid in waiting_guids:
                self._task_control.set_task_top_status(guid, TASK_TOP_STATUS_WAITING)
                self._task_control.enqueue_waiting_task(guid)
            waiting_count = len(waiting_guids)
        else:
            # No DB yet; one-time import from legacy JSON session if present.
            legacy_tasks = legacy_session.get("tasks", {})
            if isinstance(legacy_tasks, dict) and legacy_tasks:
                try:
                    imported = session_store.import_tasks_from_json()
                    self.logger.info(
                        "Imported %d tasks from legacy session (skipped %d)"
                        % (imported.get("imported", 0), imported.get("skipped", 0))
                    )
                except Exception:
                    self.logger.warning(
                        i18n.SESSION_LOAD_EXCEPTION % traceback.format_exc()
                    )
                try:
                    waiting_guids = session_store.list_waiting_guids()
                    for guid in waiting_guids:
                        self._task_control.set_task_top_status(
                            guid, TASK_TOP_STATUS_WAITING
                        )
                        self._task_control.enqueue_waiting_task(guid)
                    waiting_count = len(waiting_guids)
                except Exception:
                    pass

        if waiting_count:
            self.logger.info(i18n.XEH_LOAD_TASKS_CNT % waiting_count)

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
            LOGIN_URL,
            data=logindata,
        )

        coo = r.response.headers.get("set-cookie")
        if not coo:
            raise Exception("No set-cookie header found in login response")

        try:
            cooid = re.findall("ipb_member_id=(.*?);", coo)[0]
            coopw = re.findall("ipb_pass_hash=(.*?);", coo)[0]
        except (IndexError,) as ex:
            errmsg = re.findall(
                '<span class="postcolor">([^<]+)</span>', r.response.text
            )
            if errmsg:
                raise Exception(errmsg[0])
            raise Exception("Login failed: %s" % str(ex))

        self.cookies.update({"ipb_member_id": cooid, "ipb_pass_hash": coopw})
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
