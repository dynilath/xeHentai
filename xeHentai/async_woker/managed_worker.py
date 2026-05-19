#!/usr/bin/env python
# coding:utf-8

from __future__ import annotations

from threading import Thread
from typing import Any

from .worker_runtime import WorkerRuntime


class ManagedWorker(object):
    """Common lifecycle helper object used by v2 worker threads."""

    def __init__(self, owner: Thread, runtime: WorkerRuntime, logger: Any) -> None:
        self.owner = owner
        self.runtime = runtime
        self.logger = logger
        self._working = False

    def is_working(self) -> bool:
        return self._working

    def set_working(self, value: bool) -> None:
        self._working = value

    def should_exit(self, notify_exit: bool = False) -> bool:
        if self.runtime.keep_alive:
            return self.runtime.keep_alive(self.owner, _exit=notify_exit)
        if self.runtime.exit_check:
            return self.runtime.exit_check(self.owner)
        return False

    def vote(self, code: int) -> None:
        if self.runtime.vote:
            self.runtime.vote(self.owner.name, code)
