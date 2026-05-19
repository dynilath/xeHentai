#!/usr/bin/env python
# coding:utf-8

from __future__ import annotations

import time
import traceback
from queue import Empty, Queue
from threading import Thread
from typing import Any, Optional

from .. import filters
from ..const import *
from ..exceptions import FilterException
from ..i18n import i18n
from ..proxy import PoolException
from ..task import Task
from ..worker import HttpReq
from .managed_worker import ManagedWorker
from .worker_runtime import WorkerRuntime


class SinglePageDownloadWorker(Thread):
    """Dedicated worker that resolves page URL and downloads immediately."""

    def __init__(
        self,
        tname: str,
        task: Task,
        page_queue: Queue,
        logger: Any,
        headers: Optional[dict] = None,
        proxy: Optional[Any] = None,
        proxy_policy: Optional[Any] = None,
        runtime: Optional[WorkerRuntime] = None,
        retry: int = 3,
        timeout: int = 10,
        download_timeout: int = 30,
        download_ori: bool = False,
    ) -> None:
        headers = headers or {}
        runtime = runtime or WorkerRuntime()

        Thread.__init__(self, name=tname, daemon=True)
        self.logger = logger
        self._managed = ManagedWorker(owner=self, runtime=runtime, logger=logger)
        self._http = HttpReq(
            headers=headers,
            proxy=proxy,
            proxy_policy=proxy_policy,
            retry=retry,
            timeout=timeout,
            logger=logger,
            tname=tname,
            proxy_wait=False,
        )
        self.zombie_threshold = max(timeout, download_timeout) * (retry + 1)

        self.task = task
        self.page_queue = page_queue
        self.download_timeout = download_timeout
        self.download_ori = download_ori

    def is_working(self) -> bool:
        return self._managed.is_working()

    def _should_exit(self, notify_exit: bool = False) -> bool:
        return self._managed.should_exit(notify_exit)

    def _vote(self, code: int) -> None:
        self._managed.vote(code)

    def _wait_proxy(self, ex: PoolException) -> bool:
        gate = self._managed.runtime.proxy_gate
        pool = self._managed.runtime.proxy_pool
        if gate:
            gate.block()
        wait_for = float(getattr(ex, "retry_after", 0.0) or 0.0)
        if wait_for > 0:
            time.sleep(wait_for)
        recovered = False
        if pool and hasattr(pool, "wait_until_available"):
            recovered = bool(pool.wait_until_available(exit_check=lambda: self._should_exit()))
        if recovered and gate:
            elapsed = gate.unblock()
            self.logger.info("%s-%s proxy recovered after %.2fs" % (i18n.THREAD, self.name, elapsed))
        return recovered

    def _wait_gate(self) -> bool:
        gate = self._managed.runtime.proxy_gate
        if not gate or not gate.is_blocked():
            return True
        while gate.is_blocked():
            if self._should_exit():
                return False
            time.sleep(0.5)
        return True

    def _discard_img_q_url(self, img_url: str) -> bool:
        if not self.task.img_q:
            return False
        removed = False
        with self.task.img_q.mutex:
            q = self.task.img_q.queue
            while True:
                try:
                    q.remove(img_url)
                    removed = True
                except ValueError:
                    break
        return removed

    def _resolve_img_from_page(self, page_url: str) -> Optional[tuple[str, str]]:
        payload: dict[str, str] = {}

        def _on_success(x: tuple[str, str, str, str]) -> None:
            img_url, reload_url, fname, filesize = x
            self.task.set_reload_url(img_url, reload_url, fname, filesize)
            payload["img_url"] = img_url
            payload["reload_url"] = reload_url

        self._http.request(
            method="GET",
            url=page_url,
            _filter=filters.flt_imgurl_wrapper(self.download_ori),
            suc=lambda x: (_on_success(x), self._vote(ERR_NO_ERROR)),
            fail=lambda x: self._vote(x[0] if isinstance(x, tuple) else ERR_CONNECTION_ERROR),
        )

        if "img_url" not in payload or "reload_url" not in payload:
            return None
        return payload["img_url"], payload["reload_url"]

    def _download_immediately(self, img_url: str, reload_url: str) -> None:
        removed = self._discard_img_q_url(img_url)
        if not removed:
            return

        old_timeout = self._http.timeout
        self._http.timeout = self.download_timeout

        try:
            self._http.request(
                method="GET",
                url=img_url,
                _filter=filters.download_file_wrapper(self.task.config["dir"]),
                suc=lambda x: (
                    self.task.save_file(x[1], x[2], x[0], x[3], x[4]) and self._vote(ERR_NO_ERROR)
                ),
                fail=lambda x: self._on_download_fail(x, img_url, reload_url),
                stream_cb=lambda _: self._should_exit(),
            )
        finally:
            self._http.timeout = old_timeout

    def _on_download_fail(self, fail_tuple: Any, img_url: str, reload_url: str) -> None:
        code = fail_tuple[0] if isinstance(fail_tuple, tuple) and fail_tuple else ERR_CONNECTION_ERROR
        failed_url = fail_tuple[1] if isinstance(fail_tuple, tuple) and len(fail_tuple) > 1 else img_url

        if "hentai.org/img/509.gif" not in str(failed_url) and reload_url:
            self.page_queue.put(reload_url)
        if img_url in self.task.reload_map:
            self.task.reload_map.pop(img_url)

        self._vote(code)

    def run(self) -> None:
        self.logger.verbose("t-%s start" % self.name)

        while not self._should_exit() and self.task.state not in (TASK_STATE_PAUSED, TASK_STATE_FAILED):
            if not self._wait_gate():
                break

            try:
                page_url = self.page_queue.get(False)
            except Empty:
                self._managed.set_working(False)
                time.sleep(0.5)
                continue

            if not page_url:
                self._managed.set_working(False)
                time.sleep(0.2)
                continue

            self._managed.set_working(True)
            try:
                resolved = self._resolve_img_from_page(page_url)
                if not resolved:
                    continue
                img_url, reload_url = resolved
                self._download_immediately(img_url, reload_url)
            except PoolException as ex:
                self.logger.warning("%s-%s %s" % (i18n.THREAD, self.name, str(ex)))
                if self._wait_proxy(ex):
                    continue
                self._vote(ERR_CONNECTION_ERROR)
                break
            except FilterException as ex:
                if ex.reason:
                    self.logger.debug("%s-%s filter rejected: %s" % (i18n.THREAD, self.name, ex.reason))
                self.page_queue.put(ex.url)
                self._vote(ex.code)
            except Exception:
                self.logger.warning(i18n.THREAD_UNCAUGHT_EXCEPTION % (self.name, traceback.format_exc()))
                self._vote(ERR_CONNECTION_ERROR)

            self._managed.set_working(False)
        self.logger.verbose("t-%s exit" % self.name)
        self._should_exit(notify_exit=True)
