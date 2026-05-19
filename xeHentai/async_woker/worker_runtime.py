#!/usr/bin/env python
# coding:utf-8

from __future__ import annotations

from dataclasses import dataclass
from threading import Thread
from typing import Any, Callable, Optional

from .proxy_exhaustion_gate import ProxyExhaustionGate


KeepAliveFn = Callable[[Thread, bool], bool]
VoteFn = Callable[[str, int], None]


@dataclass
class WorkerRuntime:
    """Shared runtime hooks for monitor-compatible workers."""

    keep_alive: Optional[KeepAliveFn] = None
    vote: Optional[VoteFn] = None
    exit_check: Optional[Callable[[Any], bool]] = None
    proxy_gate: Optional[ProxyExhaustionGate] = None
    proxy_pool: Optional[Any] = None
