#!/usr/bin/env python
# coding:utf-8
"""Host interface for TaskControl - defines the contract that xeHentai must satisfy."""

from typing import List, Protocol, Any, Dict, Optional, Tuple
from queue import Queue

from .reuse_index import ReuseIndexHandle
from .task import Task

from .util.logger import Logger
from .proxy import ProxyPool


class HostInterface(Protocol):
    """Protocol defining the interface that TaskControl expects from its host object."""

    # Mutable attributes
    logger: Logger
    """Logger instance for logging messages."""

    config: Dict[str, Any]
    """Global runtime configuration."""

    proxy: Optional[ProxyPool]
    """Proxy configuration/pool."""

    headers: Dict[str, str]
    """HTTP headers to use in requests."""

    has_login: bool
    """Whether the host is logged in."""

    global_reuse_index: ReuseIndexHandle
    """Global reuse index for task optimization."""

    tasks: Queue[str]
    """Queue for task GUIDs to be processed."""

    last_task_guid: Optional[str]
    """Last processed task GUID."""

    _active_tasks: Dict[str, Task]
    """Dictionary of currently-executing tasks by GUID (bounded by async_task_concurrency).

    Cold (waiting/finished/failed/paused) tasks live only in the SQLite store
    and are hydrated into this dict on demand.
    """

    # Methods
    def _add_task(self, url: str,*, enqueue_existed=False, **cfg_dict: Any) -> Tuple[int, Optional[str]]:
        """Create, register, and enqueue a new task. Returns (error_code, guid_or_none)."""
        ...

    def _save_session(self,*, task=False, proxy_store=False, cookies=False, guid: Optional[str]=None) -> List[str]:
        """Save the current session state."""
        ...

    def _cleanup(self) -> None:
        """Clean up resources."""
        ...

    def _hydrate_task(self, guid: str) -> Optional[Task]:
        """Load a single Task from the DB into the active set and return it."""
        ...

    def _dehydrate_task(self, guid: str) -> None:
        """Persist an active Task back to the DB and evict it from memory."""
        ...

    def _get_active_task(self, guid: str) -> Optional[Task]:
        """Return an in-memory active Task without hydrating. None if cold."""
        ...
