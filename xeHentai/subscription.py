#!/usr/bin/env python
# coding:utf-8
"""Gallery subscription manager.

Periodically re-fetches the gallery page of each subscription, looks for the
site's "new version" hints (the ``<div id="gnd">`` block, parsed by
``filters.flt_metadata`` into ``meta['newer_versions']``), and when a newer
version exists:

  1. adds a download task for the newest version URL (dedup by gid), and
  2. replaces the subscription's tracked link with the newest version URL.

Old tasks and their files are kept (same policy as the in-task
``TaskNewVersion`` flow); use ``scripts/cleanup_old_versions.py`` to prune
them manually.
"""

from __future__ import annotations

import threading
import time
import traceback
from typing import Any, Dict, List, Optional

from . import filters
from . import session_store
from .exceptions import (
    GalleryNotFoundException,
    GalleryRemovedException,
    IPBannedException,
    VisibleOnlyInExhentaiException,
)
from .i18n import i18n
from .request_wrapper import HttpRequest

# How long the remaining due checks are deferred when a round is aborted
# because the IP got banned mid-round.
SUB_BAN_DEFER_S = 3600

# Task config keys inherited from the old version's task when adding the
# new-version download task (mirrors xeHentai._TASK_CONFIG_KEYS).
_INHERITED_TASK_CONFIG_KEYS = ("download_ori", "delete_task_files", "jpn_title")


class SubscriptionManager:
    """Background thread that runs subscription checks on a schedule."""

    def __init__(self, host):
        # Host is the xeHentai core instance (config/logger/proxy/add_task).
        self._host = host
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # Serializes rounds against explicit checks triggered from the web API.
        self._round_lock = threading.Lock()

    @property
    def logger(self):
        return self._host.logger

    # ── lifecycle ───────────────────────────────────────────────────────

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="subscription-loop", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=10.0)

    def wake(self) -> None:
        """Break the sleep so a newly-due check runs immediately."""
        self._wake.set()

    def check_now_sync(self, sub_id: int) -> Optional[str]:
        """Run a check for one subscription immediately and block until it
        finishes, so the caller sees up-to-date status/error fields right
        away. Returns None if the subscription does not exist."""
        sub = session_store.get_subscription(sub_id)
        if sub is None:
            return None
        with self._round_lock:
            try:
                return self.check_one(sub)
            except IPBannedException:
                self.logger.warning(
                    i18n.SUB_ROUND_ABORT_BANNED.format(defer=SUB_BAN_DEFER_S)
                )
                session_store.defer_due_subscriptions(
                    int(time.time()), SUB_BAN_DEFER_S
                )
                return self._record_error(
                    int(sub["id"]), str(sub.get("gid", "")), "error", "IP banned"
                )

    # ── main loop ───────────────────────────────────────────────────────

    def _loop(self) -> None:
        self.logger.info(i18n.SUB_STARTED)
        while not self._stop.is_set():
            try:
                if self._enabled():
                    self._wake.clear()
                    with self._round_lock:
                        self._run_round()
                sleep_s = self._compute_sleep()
            except Exception:
                self.logger.warning(traceback.format_exc())
                sleep_s = 30.0
            if self._stop.is_set():
                break
            self._wake.wait(timeout=sleep_s)
        self.logger.info(i18n.SUB_STOPPED)

    def _enabled(self) -> bool:
        return bool(self._host.config.get("subscription_enabled", True))

    def _interval_seconds(self) -> float:
        try:
            hours = float(self._host.config.get("subscription_check_interval", 24.0))
        except (TypeError, ValueError):
            hours = 24.0
        return max(0.1, hours) * 3600.0

    def _compute_sleep(self) -> float:
        if not self._enabled():
            return 30.0
        next_at = session_store.next_subscription_check_time()
        if next_at is None:
            return 60.0
        delta = next_at - time.time()
        # Re-check at least every minute so runtime config / DB changes
        # propagate without restarting; never spin faster than 5s.
        return max(5.0, min(60.0, delta))

    def _run_round(self) -> None:
        due = session_store.list_due_subscriptions(int(time.time()))
        if not due:
            return
        pacing = float(self._host.config.get("subscription_check_pacing", 5.0) or 0)
        for index, sub in enumerate(due):
            if self._stop.is_set():
                break
            if index > 0 and pacing > 0:
                # Interruptible pacing between gallery fetches.
                self._wake.wait(timeout=pacing)
                self._wake.clear()
            try:
                self.check_one(sub)
            except IPBannedException:
                self.logger.warning(
                    i18n.SUB_ROUND_ABORT_BANNED.format(defer=SUB_BAN_DEFER_S)
                )
                session_store.defer_due_subscriptions(
                    int(time.time()), SUB_BAN_DEFER_S
                )
                break

    # ── single subscription check ───────────────────────────────────────

    def check_one(self, sub: Dict[str, Any]) -> str:
        """Fetch the gallery page once and act on newer versions.

        Returns the recorded last_status. Raises IPBannedException to the
        caller (round-level condition); all other failures are recorded on
        the subscription row.
        """
        sub_id = int(sub["id"])
        gid = str(sub.get("gid", ""))
        url = str(sub.get("url", ""))

        req = HttpRequest(self._host.headers, self.logger, logger_prefix="sub-%d" % sub_id)
        try:
            r = req.request(
                "GET",
                url,
                retry=self._host.config.get("page_retry", 3),
                timeout=self._host.config.get("page_timeout", 10),
                proxy=self._host.proxy,
                proxy_wait=False,
            )
            meta = filters.flt_metadata(r)
        except IPBannedException:
            raise
        except GalleryRemovedException as ex:
            return self._record_error(sub_id, gid, "removed", str(ex))
        except GalleryNotFoundException as ex:
            return self._record_error(sub_id, gid, "not_found", str(ex))
        except VisibleOnlyInExhentaiException as ex:
            return self._record_error(sub_id, gid, "exh_only", str(ex))
        except Exception as ex:
            return self._record_error(sub_id, gid, "error", str(ex))

        now = int(time.time())
        interval_s = self._interval_seconds()

        # Fill the display title on first successful check (or keep the one
        # recorded from the previous version).
        title = ""
        if bool(self._host.config.get("jpn_title", True)) and meta.get("title_japanese"):
            title = str(meta["title_japanese"])
        else:
            title = str(meta.get("title_primary") or meta.get("title_japanese") or "")

        newer_versions: List[Dict[str, Any]] = meta.get("newer_versions") or []
        if newer_versions:
            latest = max(newer_versions, key=lambda x: int(x.get("gid", 0)))
            new_url = str(latest.get("url", ""))
            self.logger.info(
                i18n.SUB_NEW_VERSION.format(
                    sid=sub_id, gid=gid, url=new_url,
                    added=latest.get("added", ""),
                )
            )
            # Mirror the in-task TaskNewVersion flow so the OLD task's own
            # detail page also shows the "newer version" pointer.
            self._mark_old_task_newer_version(gid, newer_versions)

            cfg_overrides = self._inherited_task_config(gid)
            ret, new_task_guid = self._host.add_task(new_url, **cfg_overrides)
            if ret != 0:
                # Adding the download task failed (bad URL, exh without
                # login, ...). Keep tracking the OLD url so the next round
                # re-detects the newer version and retries.
                self.logger.warning(
                    i18n.SUB_ADD_TASK_FAIL.format(sid=sub_id, ret=ret, url=new_url)
                )
                return self._record_error(
                    sub_id, gid, "error",
                    "add new-version task failed (ret=%d): %s" % (ret, new_url),
                )

            self.logger.info(
                i18n.SUB_ADD_TASK_OK.format(sid=sub_id, guid=new_task_guid, url=new_url)
            )
            session_store.replace_subscription_link(
                sub_id,
                str(latest.get("gid", "")),
                new_url,
                str(latest.get("sethash", "")),
                str(latest.get("title", "")) or title,
                last_new_version_url=new_url,
            )
            self.logger.info(
                i18n.SUB_LINK_REPLACED.format(
                    sid=sub_id, new_gid=latest.get("gid", ""), old_gid=gid, url=new_url
                )
            )
            session_store.update_subscription_fields(
                sub_id,
                {"last_check_at": now, "next_check_at": now + int(interval_s),
                 "last_error": ""},
            )
            return "new_version"

        fields: Dict[str, Any] = {
            "last_check_at": now,
            "next_check_at": now + int(interval_s),
            "last_status": "ok",
            "last_error": "",
        }
        # No newer version: if the gallery still has no task, create one
        # now. Task creation is deferred to the check round (instead of
        # subscription creation) so failures are retried here periodically.
        if session_store.find_guid_by_gid(gid) is None:
            ret, task_guid = self._host.add_task(url)
            if ret != 0:
                self.logger.warning(
                    i18n.SUB_TASK_ADD_FAIL.format(sid=sub_id, gid=gid, ret=ret, url=url)
                )
                fields["last_error"] = "add task failed (ret=%d): %s" % (ret, url)
            else:
                self.logger.info(
                    i18n.SUB_TASK_ADDED.format(sid=sub_id, gid=gid, guid=task_guid, url=url)
                )
        if title and not sub.get("title"):
            fields["title"] = title
        session_store.update_subscription_fields(sub_id, fields)
        self.logger.debug(i18n.SUB_CHECK_OK.format(sid=sub_id, gid=gid))
        return "ok"

    def _record_error(self, sub_id: int, gid: str, status: str, error: str) -> str:
        now = int(time.time())
        session_store.update_subscription_fields(
            sub_id,
            {
                "last_check_at": now,
                "next_check_at": now + int(self._interval_seconds()),
                "last_status": status,
                "last_error": (error or "")[:500],
            },
        )
        self.logger.warning(i18n.SUB_CHECK_ERROR.format(sid=sub_id, gid=gid, error=error))
        return status

    def _inherited_task_config(self, old_gid: str) -> Dict[str, Any]:
        """Pull download preferences from the old version's task, if any."""
        guid = session_store.find_guid_by_gid(str(old_gid))
        if not guid:
            return {}
        payload = session_store.load_task_payload(guid)
        if not payload:
            return {}
        cfg = payload.get("config")
        if not isinstance(cfg, dict):
            return {}
        return {k: cfg[k] for k in _INHERITED_TASK_CONFIG_KEYS if k in cfg}

    def _mark_old_task_newer_version(
        self, old_gid: str, newer_versions: List[Dict[str, Any]]
    ) -> None:
        """Record the detected newer-version pointer on the OLD task, if one
        exists, so its detail page shows the same hint a running task would
        set for itself via the TaskNewVersion flow."""
        old_guid = session_store.find_guid_by_gid(str(old_gid))
        if not old_guid:
            return
        host = self._host
        active = host._active_tasks.get(old_guid)
        cold = active is None
        task = active if active is not None else host._hydrate_task(old_guid)
        if task is None:
            return
        task.meta.newer_versions = [dict(item) for item in newer_versions]
        if cold:
            host._dehydrate_task(old_guid)
        else:
            session_store.save_task_from_active(task)
