#!/usr/bin/env python
# coding:utf-8

import json
import os
import threading
from typing import Any, Dict, Optional

TASKS_FILE = 'h.tasks.json'
COOKIES_FILE = 'h.cookies.json'
LEGACY_SESSION_FILE = 'h.json'
SAVE_TASKS_DEBOUNCE_SECONDS = 5

class _TaskSessionStore(object):
    def __init__(self, debounce_seconds: int = SAVE_TASKS_DEBOUNCE_SECONDS):
        self.debounce_seconds = debounce_seconds
        self._lock = threading.Lock()
        self._loaded = False
        self._tasks: Dict[str, Any] = {}
        self._path = TASKS_FILE
        self._timer: Optional[threading.Timer] = None

    def _flush(self, tasks: Dict[str, Any], path: str) -> None:
        _atomic_save_json(path, {'tasks': tasks})

    def _flush_scheduled(self) -> None:
        with self._lock:
            tasks = dict(self._tasks)
            path = self._path
            self._timer = None
        self._flush(tasks, path)

    def _schedule_flush_locked(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
        self._timer = threading.Timer(self.debounce_seconds, self._flush_scheduled)
        self._timer.daemon = True
        self._timer.start()

    def save(self, tasks: Dict[str, Any], path: str = TASKS_FILE) -> None:
        with self._lock:
            self._tasks = dict(tasks)
            self._path = path
            self._loaded = True
            self._schedule_flush_locked()

    def load(self, path: str = TASKS_FILE) -> Dict[str, Any]:
        with self._lock:
            if self._loaded and self._path == path:
                return dict(self._tasks)

        data = _load_json(path)
        tasks = data.get('tasks', {})
        tasks = tasks if isinstance(tasks, dict) else {}

        with self._lock:
            self._tasks = dict(tasks)
            self._path = path
            self._loaded = True
            return dict(self._tasks)


def _atomic_save_json(path: str, data: Dict[str, Any]) -> None:
    tmp_path = '%s.next' % path
    with open(tmp_path, 'w') as f:
        f.write(json.dumps(data))
    os.path.exists(path) and os.remove(path)
    os.rename(tmp_path, path)


def _load_json(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return json.loads(f.read())


_task_session_store = _TaskSessionStore()


def save_tasks(tasks: Dict[str, Any], path: str = TASKS_FILE) -> None:
    _task_session_store.save(tasks, path)


def load_tasks(path: str = TASKS_FILE) -> Dict[str, Any]:
    return _task_session_store.load(path)


def save_cookies(cookies: Dict[str, Any], path: str = COOKIES_FILE) -> None:
    _atomic_save_json(path, {'cookies': cookies})


def load_cookies(path: str = COOKIES_FILE) -> Dict[str, Any]:
    data = _load_json(path)
    cookies = data.get('cookies', {})
    return cookies if isinstance(cookies, dict) else {}


def load_legacy_session(path: str = LEGACY_SESSION_FILE) -> Dict[str, Any]:
    return _load_json(path)


def has_tasks_file(path: str = TASKS_FILE) -> bool:
    return os.path.exists(path)


def has_cookies_file(path: str = COOKIES_FILE) -> bool:
    return os.path.exists(path)


def has_legacy_session_file(path: str = LEGACY_SESSION_FILE) -> bool:
    return os.path.exists(path)