#!/usr/bin/env python
# coding:utf-8
"""Host interface for TaskControl - defines the contract that xeHentai must satisfy."""

from typing import Protocol, Any, Dict, Optional
from queue import Queue

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

    global_reuse_index: Dict[str, Any]
    """Global reuse index for task optimization."""

    tasks: Queue[str]
    """Queue for task GUIDs to be processed."""

    last_task_guid: Optional[str]
    """Last processed task GUID."""

    _all_tasks: Dict[str, Task]
    """Dictionary of all tasks by GUID."""

    # Methods
    def save_session(self) -> None:
        """Save the current session state."""
        ...

    def _cleanup(self) -> None:
        """Clean up resources."""
        ...
