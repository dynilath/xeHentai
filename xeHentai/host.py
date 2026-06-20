"""HostProtocol — the interface that WebServer (and its route handlers) needs
from the host (xeHentai instance).

Defined as a typing.Protocol for structural subtyping: xeHentai satisfies this
protocol implicitly without inheriting from it.  This breaks the type-level
circular dependency that would exist if web/__init__.py imported xeHentai
directly from core.py.
"""

from __future__ import annotations

from typing import Any, Optional, Protocol

# Forward references for types defined in xeHentai.task and xeHentai.task_ctrl.
# They aren't imported at runtime — only used for static analysis.
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from .task import Task
    from .task_ctrl import TaskControl


class HostProtocol(Protocol):
    """Structural interface describing everything WebServer routes access."""

    # ── read-only properties ────────────────────────────────────────────
    @property
    def logger(self) -> Any: ...
    @property
    def verstr(self) -> str: ...
    @property
    def config(self) -> Any: ...
    @property
    def proxy(self) -> Optional[Any]: ...
    @property
    def has_login(self) -> bool: ...
    @property
    def _task_control(self) -> "TaskControl": ...

    # ── task hydration / eviction ───────────────────────────────────────
    def _get_active_task(self, guid: str) -> Optional["Task"]: ...
    def _hydrate_task(self, guid: str) -> Optional["Task"]: ...
    def _dehydrate_task(self, guid: str) -> None: ...

    # ── public task API ─────────────────────────────────────────────────
    def add_task(self, url: str, **cfg_overrides: Any) -> tuple[int, Optional[str]]: ...
    def add_tasks(self, urls: list[str], **cfg_overrides: Any) -> tuple[int, list[str]]: ...
    def del_task(self, guid: str) -> tuple[int, Any]: ...
    def pause_task(self, guid: str) -> tuple[int, Any]: ...
    def resume_task(self, guid: str) -> tuple[int, Any]: ...
    def retry_tasks(
        self,
        *,
        guid: Optional[str] = None,
        guids: Optional[list[str]] = None,
        gid: Optional[str] = None,
        url: Optional[str] = None,
    ) -> tuple[int, list[dict[str, Any]]]: ...

    # ── config / status ─────────────────────────────────────────────────
    def update_config(self, **cfg_dict: Any) -> tuple[int, str]: ...
    def system_status(self) -> tuple[int, dict[str, Any]]: ...
