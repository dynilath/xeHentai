#!/usr/bin/env python
# coding:utf-8

from __future__ import annotations

import time
import traceback
from threading import Thread
from typing import Any, Optional

from ..const import *
from ..i18n import i18n
from ..task import Task
from .managed_worker import ManagedWorker
from .worker_runtime import WorkerRuntime


class ArchiveBuildWorker(Thread):
    """Asynchronous archive builder that does not block the main task flow."""

    def __init__(
        self,
        logger: Any,
        task: Task,
        runtime: Optional[WorkerRuntime] = None,
        poll_interval: float = 1.0,
    ) -> None:
        runtime = runtime or WorkerRuntime()
        Thread.__init__(self, name="archiver-v2-%s" % task.guid, daemon=True)
        self.logger = logger
        self._managed = ManagedWorker(owner=self, runtime=runtime, logger=logger)
        self.zombie_threshold = 30
        self.task = task
        self.poll_interval = poll_interval

    def is_working(self) -> bool:
        return self._managed.is_working()

    def _should_exit(self, notify_exit: bool = False) -> bool:
        return self._managed.should_exit(notify_exit)

    def _ready_to_archive(self) -> bool:
        if self.task.state in (TASK_STATE_PAUSED, TASK_STATE_FAILED):
            return False
        return self.task.meta.finished >= self.task.meta.total

    def run(self) -> None:
        self.logger.verbose("t-%s start" % self.name)
        self._managed.set_working(True)
        while not self._should_exit() and not self._ready_to_archive():
            if not self._wait_gate():
                break
            time.sleep(self.poll_interval)

        if self._should_exit() or self.task.state in (TASK_STATE_PAUSED, TASK_STATE_FAILED):
            self._managed.set_working(False)
            self.logger.verbose("t-%s exit" % self.name)
            self._should_exit(notify_exit=True)
            return

        start = time.time()
        self.logger.info(i18n.TASK_START_MAKE_ARCHIVE % self.task.guid)
        prev_state = self.task.state

        try:
            self.task.state = TASK_STATE_MAKE_ARCHIVE
            archive_path = self.task.make_archive()
        except Exception:
            self.logger.error(i18n.TASK_ERROR % (self.task.guid, i18n.c(ERR_CANNOT_MAKE_ARCHIVE) % traceback.format_exc()))
            self.task.state = prev_state
        else:
            self.task.state = TASK_STATE_FINISHED
            self.logger.info(i18n.TASK_MAKE_ARCHIVE_FINISHED % (self.task.guid, archive_path, time.time() - start))

        self._managed.set_working(False)
        self.logger.verbose("t-%s exit" % self.name)
        self._should_exit(notify_exit=True)

    def _wait_gate(self) -> bool:
        gate = self._managed.runtime.proxy_gate
        if not gate or not gate.is_blocked():
            return True
        while gate.is_blocked():
            if self._should_exit():
                return False
            time.sleep(0.5)
        return True
