#!/usr/bin/env python
# coding:utf-8

import json
import os
from typing import Any, Dict, Optional

TASKS_FILE = 'h.tasks.json'
COOKIES_FILE = 'h.cookies.json'
LEGACY_SESSION_FILE = 'h.json'


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


def save_tasks(tasks: Dict[str, Any], path: str = TASKS_FILE) -> None:
    _atomic_save_json(path, {'tasks': tasks})


def load_tasks(path: str = TASKS_FILE) -> Dict[str, Any]:
    data = _load_json(path)
    tasks = data.get('tasks', {})
    return tasks if isinstance(tasks, dict) else {}


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