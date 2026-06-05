#!/usr/bin/env python
# coding:utf-8

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


class TaskControlFlow(BaseException):
    """Base type for task control-flow exceptions.
    It's not inherited from Exception class to distinguish 
    from regular exceptions that should be treated as task failures.
    """

    def __init__(self, reason: Optional[str] = None, *, result=None):
        super().__init__(reason or self.__class__.__name__)
        self.reason = reason
        self.result = result


class TaskControlFlowWithDelay(TaskControlFlow):
    """Base for control-flow exceptions that carry a retry delay."""

    def __init__(self, reason: Optional[str] = None, *, delay: float = 0.0, result=None):
        super().__init__(reason, result=result)
        self.delay = delay


class TaskReschedule(TaskControlFlowWithDelay):
    """Trigger task-level reschedule handled by task entry loop."""


class StageRetry(TaskControlFlowWithDelay):
    """Trigger stage-level retry handled by stage_retry_scope."""


class TaskNewVersion(TaskControlFlow):
    """Signal task migration to a new version of the same gallery."""
    
    def __init__(self, new_version_url: str, reason: Optional[str] = None, *, result=None):
        super().__init__(reason, result=result)
        self.new_version_url = new_version_url


class TaskFailed(TaskControlFlow):
    """Signal terminal task failure."""

    def __init__(self, reason: Optional[str] = None, *, failcode: Optional[int] = None, result=None):
        super().__init__(reason, result=result)
        self.failcode = failcode


class TaskFinished(TaskControlFlow):
    """Signal normal task completion."""


class TaskAbort(TaskControlFlow):
    """Signal non-failure abort flow (pause/shutdown/migration control)."""


class TaskRetry(TaskControlFlowWithDelay):
    """Trigger task-level retry handled by task entry loop."""


class StageSkip(TaskControlFlow):
    """Signal a local skip in stage-level flow."""


class ScanDownloadRetry(TaskControlFlowWithDelay):
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
