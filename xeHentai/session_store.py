#!/usr/bin/env python
# coding:utf-8

import json
import os
import sqlite3
import time
from typing import Any, Dict

from .const import TASK_STATE_WAITING

TASKS_FILE = 'h.tasks.json'
TASKS_DB_FILE = 'h.tasks.db'
COOKIES_FILE = 'h.cookies.json'
PROXY_FILE = 'h.proxy.json'
LEGACY_SESSION_FILE = 'h.json'

SQL_SCHEMA = '''
CREATE TABLE IF NOT EXISTS tasks (
    guid TEXT PRIMARY KEY,
    gid TEXT NOT NULL UNIQUE,
    url TEXT NOT NULL,
    phase_state INTEGER NOT NULL,
    payload TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tasks_phase_state ON tasks(phase_state);
CREATE INDEX IF NOT EXISTS idx_tasks_updated_at ON tasks(updated_at);
'''

_TASKS_CACHE: Dict[str, Dict[str, Any]] = {}


def _connect(db_path: str = TASKS_DB_FILE) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA synchronous=NORMAL')
    return conn


def _ensure_schema(db_path: str = TASKS_DB_FILE) -> None:
    conn = _connect(db_path)
    try:
        conn.executescript(SQL_SCHEMA)

        cols = [
            str(row['name'])
            for row in conn.execute("PRAGMA table_info(tasks)").fetchall()
        ]
        if 'top_status' in cols:
            conn.execute('ALTER TABLE tasks RENAME TO tasks_old')
            conn.executescript(SQL_SCHEMA)
            conn.execute(
                '''
                INSERT OR REPLACE INTO tasks (guid, gid, url, phase_state, payload, updated_at)
                SELECT guid, gid, url, phase_state, payload, updated_at
                FROM tasks_old
                '''
            )
            conn.execute('DROP TABLE tasks_old')

        conn.commit()
    finally:
        conn.close()


def _coerce_payload_dict(payload: Any) -> Dict[str, Any]:
    return payload if isinstance(payload, dict) else {}


def _extract_task_row(task_payload: Dict[str, Any], fallback_guid: str) -> Dict[str, Any]:
    payload = _coerce_payload_dict(task_payload)
    guid = str(payload.get('guid', fallback_guid) or fallback_guid)
    gid = str(payload.get('gid', '') or '')
    url = str(payload.get('url', '') or '')
    phase_state = int(payload.get('state', TASK_STATE_WAITING) or TASK_STATE_WAITING)
    payload['guid'] = guid
    payload['gid'] = gid
    payload['url'] = url
    payload['state'] = phase_state
    return {
        'guid': guid,
        'gid': gid,
        'url': url,
        'phase_state': phase_state,
        'payload': json.dumps(payload, ensure_ascii=False),
    }


def _save_tasks_sqlite(tasks: Dict[str, Any], db_path: str = TASKS_DB_FILE) -> None:
    _ensure_schema(db_path)
    conn = _connect(db_path)
    now_ts = int(time.time())
    try:
        cached = _TASKS_CACHE.get(db_path)
        if cached is None:
            cached = _load_tasks_sqlite(db_path)

        to_delete = [guid for guid in cached.keys() if guid not in tasks]
        to_upsert = [
            guid
            for guid, payload in tasks.items()
            if guid not in cached or cached.get(guid) != payload
        ]

        rows = [_extract_task_row(tasks[guid], guid) for guid in to_upsert]

        with conn:
            for row in rows:
                conn.execute(
                    '''
                    INSERT INTO tasks (guid, gid, url, phase_state, payload, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(guid) DO UPDATE SET
                        gid = excluded.gid,
                        url = excluded.url,
                        phase_state = excluded.phase_state,
                        payload = excluded.payload,
                        updated_at = excluded.updated_at
                    ''',
                    (
                        row['guid'],
                        row['gid'],
                        row['url'],
                        row['phase_state'],
                        row['payload'],
                        now_ts,
                    ),
                )
            for guid in to_delete:
                conn.execute('DELETE FROM tasks WHERE guid = ?', (str(guid),))

        _TASKS_CACHE[db_path] = dict(tasks)
    finally:
        conn.close()


def _load_tasks_sqlite(db_path: str = TASKS_DB_FILE) -> Dict[str, Any]:
    _ensure_schema(db_path)
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            'SELECT guid, payload FROM tasks ORDER BY updated_at DESC'
        ).fetchall()
    finally:
        conn.close()

    tasks: Dict[str, Any] = {}
    for row in rows:
        guid = str(row['guid'] or '')
        if not guid:
            continue
        payload_text = row['payload']
        try:
            payload = json.loads(payload_text)
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        payload['guid'] = str(payload.get('guid', guid) or guid)
        phase_state = int(payload.get('state', TASK_STATE_WAITING) or TASK_STATE_WAITING)
        payload['state'] = phase_state
        payload.pop('top_status', None)
        tasks[guid] = payload
    _TASKS_CACHE[db_path] = dict(tasks)
    return tasks


def _atomic_save_json(path: str, data: Dict[str, Any]) -> None:
    tmp_path = '%s.next' % path
    with open(tmp_path, 'w') as f:
        f.write(json.dumps(data))
    if os.path.exists(path):
        os.remove(path)
    os.rename(tmp_path, path)


def _load_json(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return json.loads(f.read())


def save_tasks(tasks: Dict[str, Any], path: str = TASKS_DB_FILE) -> None:
    _save_tasks_sqlite(tasks, path)


def load_tasks(path: str = TASKS_DB_FILE) -> Dict[str, Any]:
    return _load_tasks_sqlite(path)


def save_cookies(cookies: Dict[str, Any], path: str = COOKIES_FILE) -> None:
    _atomic_save_json(path, {'cookies': cookies})


def load_cookies(path: str = COOKIES_FILE) -> Dict[str, Any]:
    data = _load_json(path)
    cookies = data.get('cookies', {})
    return cookies if isinstance(cookies, dict) else {}


def load_legacy_session(path: str = LEGACY_SESSION_FILE) -> Dict[str, Any]:
    return _load_json(path)


def save_proxy_store(proxy_store: Dict[str, Any], path: str = PROXY_FILE) -> None:
    _atomic_save_json(path, {'proxies': proxy_store})


def load_proxy_store(path: str = PROXY_FILE) -> Dict[str, Any]:
    data = _load_json(path)
    proxies = data.get('proxies', {})
    return proxies if isinstance(proxies, dict) else {}


def has_tasks_file(path: str = TASKS_DB_FILE) -> bool:
    if not os.path.exists(path):
        return False
    conn = _connect(path)
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='tasks'"
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def has_cookies_file(path: str = COOKIES_FILE) -> bool:
    return os.path.exists(path)


def has_proxy_file(path: str = PROXY_FILE) -> bool:
    return os.path.exists(path)


def has_legacy_session_file(path: str = LEGACY_SESSION_FILE) -> bool:
    return os.path.exists(path)


def import_tasks_from_json(json_path: str = TASKS_FILE, db_path: str = TASKS_DB_FILE) -> Dict[str, int]:
    data = _load_json(json_path)
    raw_tasks = data.get('tasks', {})
    tasks = raw_tasks if isinstance(raw_tasks, dict) else {}

    deduped: Dict[str, Dict[str, Any]] = {}
    gid_seen: Dict[str, str] = {}
    skipped = 0

    for fallback_guid, payload in tasks.items():
        row = _extract_task_row(payload, str(fallback_guid or ''))
        if not row['guid'] or not row['gid']:
            skipped += 1
            continue
        if row['guid'] in deduped:
            skipped += 1
            continue
        if row['gid'] in gid_seen:
            skipped += 1
            continue
        gid_seen[row['gid']] = row['guid']
        deduped[row['guid']] = json.loads(row['payload'])

    _save_tasks_sqlite(deduped, db_path)
    return {
        'source': len(tasks),
        'imported': len(deduped),
        'skipped': skipped,
    }