#!/usr/bin/env python
# coding:utf-8

from __future__ import annotations

import time
import traceback
from queue import Empty, Queue
from threading import Thread
from typing import Any, Callable, Optional

from .. import filters
from ..const import *
from ..exceptions import FilterException
from ..i18n import i18n
from ..proxy import ProxyPoolDepleted
from ..task import Task
from ..worker import HttpReq
from .managed_worker import ManagedWorker
from .worker_runtime import WorkerRuntime


class GalleryCrawlerWorker(Thread):
    """Dedicated worker for gallery metadata and gallery page crawling."""

    MODE_META = "meta"
    MODE_PAGE = "page"

    def __init__(
        self,
        tname: str,
        mode: str,
        task: Task,
        task_queue: Queue,
        logger: Any,
        headers: Optional[dict] = None,
        proxy: Optional[Any] = None,
        proxy_policy: Optional[Any] = None,
        runtime: Optional[WorkerRuntime] = None,
        retry: int = 3,
        timeout: int = 10,
        page_setdefault: Optional[Callable[[str, str], Any]] = None,
    ) -> None:
        if mode not in (self.MODE_META, self.MODE_PAGE):
            raise ValueError("mode must be one of: meta, page")

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
        self.zombie_threshold = timeout * (retry + 1)

        self.mode = mode
        self.task = task
        self.task_queue = task_queue
        self.page_setdefault = page_setdefault

    def is_working(self) -> bool:
        return self._managed.is_working()

    def _should_exit(self, notify_exit: bool = False) -> bool:
        return self._managed.should_exit(notify_exit)

    def _vote(self, code: int) -> None:
        self._managed.vote(code)

    def _wait_proxy(self, ex: ProxyPoolDepleted) -> bool:
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

    def _crawl_meta(self, url: str) -> None:
        self._http.request(
            method="GET",
            url=url,
            _filter=filters.flt_metadata,
            suc=lambda meta: (self.task.update_meta(meta), self._vote(ERR_NO_ERROR)),
            fail=lambda code: (self.task.set_fail(code), self._vote(code if isinstance(code, int) else ERR_CONNECTION_ERROR)),
        )

    def _crawl_gallery_page(self, page_url: str) -> None:
        def _on_page_tuple(page_tuple: tuple[str, str, str]) -> None:
            callback = self.page_setdefault if self.page_setdefault else {}.setdefault
            self.task.queue_wrapper(callback, img_tuble=page_tuple)

        self._http.request(
            method="GET",
            url=page_url,
            _filter=filters.flt_pageurl,
            suc=lambda page_tuple: (_on_page_tuple(page_tuple), self._vote(ERR_NO_ERROR)),
            fail=lambda code: (self.task.set_fail(code), self._vote(code if isinstance(code, int) else ERR_CONNECTION_ERROR)),
        )

    def run(self) -> None:
        self.logger.verbose("t-%s start" % self.name)
        while not self._should_exit() and self.task.state not in (TASK_STATE_PAUSED, TASK_STATE_FAILED):
            if not self._wait_gate():
                break

            try:
                url = self.task_queue.get(False)
            except Empty:
                self._managed.set_working(False)
                time.sleep(0.5)
                continue

            if not url:
                self._managed.set_working(False)
                time.sleep(0.2)
                continue

            self._managed.set_working(True)
            try:
                if self.mode == self.MODE_META:
                    self._crawl_meta(url)
                else:
                    self._crawl_gallery_page(url)
            except ProxyPoolDepleted as ex:
                self.logger.warning("%s-%s %s" % (i18n.THREAD, self.name, str(ex)))
                if self._wait_proxy(ex):
                    continue
                self._vote(ERR_CONNECTION_ERROR)
                break
            except FilterException as ex:
                if ex.reason:
                    self.logger.debug("%s-%s filter rejected: %s" % (i18n.THREAD, self.name, ex.reason))
                self.task.set_fail(ex.code)
                self._vote(ex.code)
            except Exception:
                self.logger.warning(i18n.THREAD_UNCAUGHT_EXCEPTION % (self.name, traceback.format_exc()))
                self.task.set_fail(ERR_CONNECTION_ERROR)
                self._vote(ERR_CONNECTION_ERROR)

            self._managed.set_working(False)
        self.logger.verbose("t-%s exit" % self.name)
        self._should_exit(notify_exit=True)
