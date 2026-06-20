"""Pydantic models for xeHentai REST API request/response schemas."""

from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


# ── Task ────────────────────────────────────────────────────────────────────

class TaskCreateRequest(BaseModel):
    url: str = Field(..., description="Gallery URL to download")
    download_ori: Optional[bool] = None
    make_archive: Optional[bool] = None
    delete_task_files: Optional[bool] = None
    jpn_title: Optional[bool] = None
    download_range: Optional[str] = None
    dir: Optional[str] = None
    proxy_image_only: Optional[bool] = None
    proxy_image: Optional[bool] = None
    scan_thread_cnt: Optional[int] = None
    download_thread_cnt: Optional[int] = None
    page_retry: Optional[int] = None
    page_timeout: Optional[int] = None
    download_retry: Optional[int] = None
    download_timeout: Optional[int] = None


class TaskBulkRequest(BaseModel):
    urls: List[str] = Field(..., min_length=1, description="List of gallery URLs")
    download_ori: Optional[bool] = None
    make_archive: Optional[bool] = None
    delete_task_files: Optional[bool] = None
    jpn_title: Optional[bool] = None
    download_range: Optional[str] = None
    dir: Optional[str] = None
    proxy_image_only: Optional[bool] = None
    proxy_image: Optional[bool] = None
    scan_thread_cnt: Optional[int] = None
    download_thread_cnt: Optional[int] = None
    page_retry: Optional[int] = None
    page_timeout: Optional[int] = None
    download_retry: Optional[int] = None
    download_timeout: Optional[int] = None
    enqueue_existed: Optional[bool] = None


class TaskListParams(BaseModel):
    states: Optional[str] = Field(None, description="Comma-separated phase_state ints or legacy level name")
    tags: Optional[str] = Field(None, description="Comma-separated tag strings (OR match)")
    gid: Optional[str] = None
    url: Optional[str] = None
    offset: int = Field(0, ge=0)
    limit: int = Field(100, ge=1, le=1000)
    order_by: str = Field("updated_at", description="Column to sort by")
    order_dir: str = Field("DESC", pattern="^(ASC|DESC)$")


class TaskRetryRequest(BaseModel):
    guid: Optional[str] = None
    gid: Optional[str] = None
    url: Optional[str] = None


class ImageInfo(BaseModel):
    fid: str
    url: str
    file_name: str
    file_hash: str


class TaskItemResponse(BaseModel):
    guid: str
    gid: str
    url: str
    phase_state: int
    title: str
    total: int
    done: int


class TaskDetailResponse(BaseModel):
    guid: str
    gid: str
    url: str
    phase_state: int
    title: str
    title_japanese: Optional[str] = None
    total: int
    done: int
    tags: List[Any] = Field(default_factory=list)
    newer_versions: List[Dict[str, Any]] = Field(default_factory=list)
    make_archive: bool = False
    download_ori: bool = False


class PaginatedResponse(BaseModel):
    total: int
    items: List[TaskItemResponse]


# ── Config ──────────────────────────────────────────────────────────────────

class ConfigResponse(BaseModel):
    dir: str = "."
    download_ori: bool = False
    jpn_title: bool = True
    rename_ori: bool = False
    proxy: List[str] = Field(default_factory=list)
    proxy_image: bool = True
    proxy_image_only: bool = False
    make_archive: bool = False
    scan_thread_cnt: int = 1
    download_thread_cnt: int = 5
    async_task_concurrency: int = 1
    page_interval: float = 0.5
    page_retry: int = 3
    page_timeout: int = 10
    download_retry: int = 5
    download_timeout: int = 8
    log_level_console: str = "DEBUG"
    log_level_file: str = "DEBUG"
    save_tasks: bool = False
    delete_task_files: bool = False
    download_range: Optional[str] = None
    ignored_errors: List[int] = Field(default_factory=list)
    web_ui_enabled: bool = True


class ConfigUpdateRequest(BaseModel):
    dir: Optional[str] = None
    download_ori: Optional[bool] = None
    jpn_title: Optional[bool] = None
    rename_ori: Optional[bool] = None
    proxy: Optional[List[str]] = None
    proxy_image: Optional[bool] = None
    proxy_image_only: Optional[bool] = None
    make_archive: Optional[bool] = None
    scan_thread_cnt: Optional[int] = None
    download_thread_cnt: Optional[int] = None
    async_task_concurrency: Optional[int] = None
    page_interval: Optional[float] = None
    page_retry: Optional[int] = None
    page_timeout: Optional[int] = None
    download_retry: Optional[int] = None
    download_timeout: Optional[int] = None
    log_level_console: Optional[str] = None
    log_level_file: Optional[str] = None
    save_tasks: Optional[bool] = None
    delete_task_files: Optional[bool] = None
    download_range: Optional[str] = None
    ignored_errors: Optional[List[int]] = None
    web_ui_enabled: Optional[bool] = None
    proxy_disable_threshold: Optional[int] = None
    proxy_good_threshold: Optional[int] = None


# ── System ──────────────────────────────────────────────────────────────────

class InfoResponse(BaseModel):
    version: str
    threads_zombie: int = 0
    threads_running: int = 0
    queue_pending: int = 0
    queue_finished: int = 0
    queue_waiting: int = 0
    queue_processing: int = 0
    proxy_enabled: bool = False
    proxy_count: int = 0
    has_login: bool = False


class SystemStatusGroup(BaseModel):
    count: int
    state_name: str


class SystemStatusResponse(BaseModel):
    waiting: Dict[str, SystemStatusGroup] = Field(default_factory=dict)
    processing: Dict[str, SystemStatusGroup] = Field(default_factory=dict)
    processed: Dict[str, SystemStatusGroup] = Field(default_factory=dict)


# ── Generic ─────────────────────────────────────────────────────────────────

class ErrorResponse(BaseModel):
    detail: str


class SuccessResponse(BaseModel):
    message: str
    guid: Optional[str] = None
