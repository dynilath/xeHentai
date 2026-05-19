#!/usr/bin/env python
# coding:utf-8

import time
from threading import Event, Lock


class ProxyExhaustionGate:
    """Shared gate used to block all v2 workers while proxy pool is exhausted."""

    def __init__(self) -> None:
        self._available = Event()
        self._available.set()
        self._blocked_since = 0.0
        self._lock = Lock()

    def is_blocked(self) -> bool:
        return not self._available.is_set()

    def block(self) -> None:
        with self._lock:
            if self._available.is_set():
                self._blocked_since = time.time()
                self._available.clear()

    def unblock(self) -> float:
        with self._lock:
            elapsed = 0.0
            if not self._available.is_set():
                elapsed = max(0.0, time.time() - self._blocked_since)
                self._available.set()
                self._blocked_since = 0.0
            return elapsed
