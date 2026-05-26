#!/usr/bin/env python
# coding:utf-8

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Generic, Optional, TypeVar


class StageAction(str, Enum):
    CONTINUE = "continue"
    RETRY = "retry"
    PIPELINE_RETRY = "pipeline_retry"
    SKIP = "skip"
    FINISH = "finish"
    FAIL = "fail"
    ABORT = "abort"


T = TypeVar("T")


@dataclass
class StageOutcome(Generic[T]):
    action: StageAction
    result: Optional[T] = None
    reason: Optional[str] = None
    delay: float = 0.0
    failcode: Optional[int] = None


class TaskControlFlow(BaseException):
    """Base type for task control-flow exceptions.
    It's not inherited from Exception class to distinguish 
    from regular exceptions that should be treated as task failures.
    """

    def __init__(self, reason: Optional[str] = None, *, delay: float = 0.0, failcode: Optional[int] = None, result=None):
        super().__init__(reason or self.__class__.__name__)
        self.reason = reason
        self.delay = delay
        self.failcode = failcode
        self.result = result


class TaskReschedule(TaskControlFlow):
    """Trigger task-level reschedule handled by task entry loop."""


class StageRetry(TaskControlFlow):
    """Trigger stage-level retry handled by stage_retry_scope."""


class TaskFailed(TaskControlFlow):
    """Signal terminal task failure."""


class TaskFinished(TaskControlFlow):
    """Signal normal task completion."""


class TaskAbort(TaskControlFlow):
    """Signal non-failure abort flow (pause/shutdown/migration control)."""


class TaskRetry(TaskControlFlow):
    """Trigger task-level retry handled by task entry loop."""


class StageSkip(TaskControlFlow):
    """Signal a local skip in stage-level flow."""


class ScanDownloadRetry(TaskControlFlow):
    """Retry the combined scan-download pipeline step with refreshed context."""


class ScanDownloadSkip(TaskControlFlow):
    """Skip the combined scan-download pipeline step, usually due to dumplicated or existing files."""

@dataclass
class GetMetaResult:
    migrated: bool = False


@dataclass
class ScanPageResult:
    page_count: int = 0


@dataclass
class ScanImageResult:
    fid: str
    page_url: str
    img_url: str
    reload_url: str


@dataclass
class DownloadResult:
    img_url: str
    reload_url: Optional[str] = None


@dataclass
class ArchiveResult:
    archive_path: Optional[str] = None
