#!/usr/bin/env python
# coding:utf-8

import json
import os
import sqlite3
import time
from typing import Any, Dict, List, Optional, Tuple

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
    title TEXT,
    total INTEGER,
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
            cols = [
                str(row['name'])
                for row in conn.execute("PRAGMA table_info(tasks)").fetchall()
            ]

        # Additive migration: promote title/total to top-level columns for
        # lightweight listing queries that avoid parsing the payload JSON.
        if 'title' not in cols:
            conn.execute('ALTER TABLE tasks ADD COLUMN title TEXT')
        if 'total' not in cols:
            conn.execute('ALTER TABLE tasks ADD COLUMN total INTEGER')
        # One-time backfill from existing payload JSON. Guarded by IS NULL so
        # re-runs are cheap; idempotent across restarts.
        conn.execute(
            '''
            UPDATE tasks
            SET title = json_extract(payload, '$.meta.title'),
                total = CAST(json_extract(payload, '$.meta.total') AS INTEGER)
            WHERE title IS NULL
            '''
        )

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
    meta = payload.get('meta') if isinstance(payload.get('meta'), dict) else {}
    title = str(meta.get('title', '') or '')
    total = int(meta.get('total', 0) or 0)
    payload['guid'] = guid
    payload['gid'] = gid
    payload['url'] = url
    payload['state'] = phase_state
    return {
        'guid': guid,
        'gid': gid,
        'url': url,
        'phase_state': phase_state,
        'title': title,
        'total': total,
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
                    INSERT INTO tasks (guid, gid, url, phase_state, title, total, payload, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(guid) DO UPDATE SET
                        gid = excluded.gid,
                        url = excluded.url,
                        phase_state = excluded.phase_state,
                        title = excluded.title,
                        total = excluded.total,
                        payload = excluded.payload,
                        updated_at = excluded.updated_at
                    ''',
                    (
                        row['guid'],
                        row['gid'],
                        row['url'],
                        row['phase_state'],
                        row['title'],
                        row['total'],
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
            'SELECT guid, payload, phase_state FROM tasks ORDER BY updated_at DESC'
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
        phase_state = int(row['phase_state'] or TASK_STATE_WAITING)
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


def save_single_task(guid: str, task_payload: Dict[str, Any], db_path: str = TASKS_DB_FILE) -> None:
    _ensure_schema(db_path)
    conn = _connect(db_path)
    now_ts = int(time.time())
    try:
        row = _extract_task_row(task_payload, guid)
        with conn:
            conn.execute(
                '''
                INSERT INTO tasks (guid, gid, url, phase_state, title, total, payload, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(guid) DO UPDATE SET
                    gid = excluded.gid,
                    url = excluded.url,
                    phase_state = excluded.phase_state,
                    title = excluded.title,
                    total = excluded.total,
                    payload = excluded.payload,
                    updated_at = excluded.updated_at
                ''',
                (
                    row['guid'],
                    row['gid'],
                    row['url'],
                    row['phase_state'],
                    row['title'],
                    row['total'],
                    row['payload'],
                    now_ts,
                ),
            )
        cached = _TASKS_CACHE.get(db_path)
        if cached is not None:
            cached[guid] = dict(task_payload)
    finally:
        conn.close()


def load_tasks(path: str = TASKS_DB_FILE) -> Dict[str, Any]:
    return _load_tasks_sqlite(path)


# ---------------------------------------------------------------------------
# On-demand query / access functions.
#
# These never touch `_TASKS_CACHE` (except save_task_from_active) and never
# load more than the requested rows. They are the foundation for the
# DB-as-source-of-truth model: callers hydrate a single Task only when they
# actually need to execute it, and listing/status APIs read lightweight
# columns directly from SQLite.
# ---------------------------------------------------------------------------

_LIGHT_COLUMNS = 'guid, gid, url, phase_state, title, total'

_ORDER_BY_WHITELIST = {
    'updated_at': 'updated_at',
    'gid': 'gid',
    'phase_state': 'phase_state',
    'title': 'title',
    'guid': 'guid',
}


def _row_to_light_dict(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        'guid': str(row['guid'] or ''),
        'gid': str(row['gid'] or ''),
        'url': str(row['url'] or ''),
        'phase_state': int(row['phase_state'] if row['phase_state'] is not None else TASK_STATE_WAITING),
        'title': str(row['title'] or '') if row['title'] is not None else '',
        'total': int(row['total'] or 0) if row['total'] is not None else 0,
    }


def query_tasks(
    *,
    states: Optional[List[int]] = None,
    tags: Optional[List[str]] = None,
    gid: Optional[str] = None,
    url: Optional[str] = None,
    offset: int = 0,
    limit: int = 100,
    order_by: str = 'updated_at',
    order_dir: str = 'DESC',
    db_path: str = TASKS_DB_FILE,
) -> Tuple[int, List[Dict[str, Any]]]:
    """Query lightweight task columns with optional filters and pagination.

    Filters (all AND-combined):
      - states: match tasks whose phase_state is in this list (OR within the list).
      - tags:   match tasks having ANY of these tags (OR within the list). Tags
                live in the payload JSON (``$.meta.tags`` array); filtering uses
                ``json_each`` so it has to read the payload column, making it
                somewhat more expensive than the other filters.
      - gid/url: exact match.

    Returns (total_count, [light_dict, ...]). The returned dicts never include
    the payload, even when ``tags`` filtering read it internally.
    """
    _ensure_schema(db_path)
    order_col = _ORDER_BY_WHITELIST.get(order_by, 'updated_at')
    direction = 'DESC' if str(order_dir).upper() == 'DESC' else 'ASC'

    where: List[str] = []
    params: List[Any] = []
    if states:
        # OR-match any of the given phase_states.
        normalized = [int(s) for s in states if s is not None]
        if normalized:
            placeholders = ', '.join('?' for _ in normalized)
            where.append('phase_state IN (%s)' % placeholders)
            params.extend(normalized)
    if tags:
        # OR-match tasks that carry any of the requested tags. Tags are stored
        # as a JSON array under payload.meta.tags.
        normalized_tags = [str(t) for t in tags if t]
        if normalized_tags:
            tag_placeholders = ', '.join('?' for _ in normalized_tags)
            where.append(
                'EXISTS (SELECT 1 FROM json_each(json_extract(payload, \'$.meta.tags\')) '
                'WHERE value IN (%s))' % tag_placeholders
            )
            params.extend(normalized_tags)
    if gid:
        where.append('gid = ?')
        params.append(str(gid))
    if url:
        where.append('url = ?')
        params.append(str(url))

    where_clause = (' WHERE ' + ' AND '.join(where)) if where else ''
    offset = max(0, int(offset or 0))
    limit = max(1, min(1000, int(limit or 100)))

    conn = _connect(db_path)
    try:
        total = conn.execute(
            'SELECT COUNT(*) FROM tasks%s' % where_clause, params
        ).fetchone()[0]
        rows = conn.execute(
            'SELECT %s FROM tasks%s ORDER BY %s %s LIMIT ? OFFSET ?'
            % (_LIGHT_COLUMNS, where_clause, order_col, direction),
            params + [limit, offset],
        ).fetchall()
    finally:
        conn.close()

    return int(total or 0), [_row_to_light_dict(r) for r in rows]


def get_task_row(guid: str, db_path: str = TASKS_DB_FILE) -> Optional[Dict[str, Any]]:
    """Return lightweight columns for a single task, or None if not found."""
    _ensure_schema(db_path)
    conn = _connect(db_path)
    try:
        row = conn.execute(
            'SELECT %s FROM tasks WHERE guid = ?' % _LIGHT_COLUMNS, (str(guid),)
        ).fetchone()
    finally:
        conn.close()
    return _row_to_light_dict(row) if row else None


def find_guid_by_gid(gid: str, db_path: str = TASKS_DB_FILE) -> Optional[str]:
    """Return the guid for a given gallery id, or None. Uses the UNIQUE gid index."""
    _ensure_schema(db_path)
    conn = _connect(db_path)
    try:
        row = conn.execute(
            'SELECT guid FROM tasks WHERE gid = ?', (str(gid),)
        ).fetchone()
    finally:
        conn.close()
    return str(row['guid']) if row else None


def find_guid_by_url(url: str, db_path: str = TASKS_DB_FILE) -> Optional[str]:
    """Return the guid for a given gallery url, or None."""
    _ensure_schema(db_path)
    conn = _connect(db_path)
    try:
        row = conn.execute(
            'SELECT guid FROM tasks WHERE url = ?', (str(url),)
        ).fetchone()
    finally:
        conn.close()
    return str(row['guid']) if row else None


def load_task_payload(guid: str, db_path: str = TASKS_DB_FILE) -> Optional[Dict[str, Any]]:
    """Load and json-parse the full payload for a single task. Used for hydration."""
    _ensure_schema(db_path)
    conn = _connect(db_path)
    try:
        row = conn.execute(
            'SELECT payload, phase_state FROM tasks WHERE guid = ?', (str(guid),)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    try:
        payload = json.loads(row['payload'])
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    payload['guid'] = str(payload.get('guid', guid) or guid)
    payload['state'] = int(row['phase_state'] or TASK_STATE_WAITING)
    payload.pop('top_status', None)
    return payload


def save_task_from_active(task, db_path: str = TASKS_DB_FILE) -> None:
    """Persist an active Task object back to the DB (single-row upsert).

    Maintains `_TASKS_CACHE` so subsequent save_single_task diffing is cheap.
    The cache now only ever holds active tasks, so its size is bounded by the
    async task concurrency cap.
    """
    payload = task.to_dict()
    save_single_task(str(task.guid), payload, db_path)


def update_task_state(guid: str, phase_state: int, db_path: str = TASKS_DB_FILE) -> None:
    """Lightweight state-only update (pause/resume/retry). Does not rewrite payload."""
    _ensure_schema(db_path)
    conn = _connect(db_path)
    now_ts = int(time.time())
    try:
        with conn:
            conn.execute(
                'UPDATE tasks SET phase_state = ?, updated_at = ? WHERE guid = ?',
                (int(phase_state), now_ts, str(guid)),
            )
    finally:
        conn.close()


def delete_task(guid: str, db_path: str = TASKS_DB_FILE) -> None:
    """Delete a single task row by guid."""
    _ensure_schema(db_path)
    conn = _connect(db_path)
    try:
        with conn:
            conn.execute('DELETE FROM tasks WHERE guid = ?', (str(guid),))
    finally:
        conn.close()
    cached = _TASKS_CACHE.get(db_path)
    if cached is not None:
        cached.pop(str(guid), None)


def count_tasks_by_state(db_path: str = TASKS_DB_FILE) -> Dict[int, int]:
    """Return {phase_state: count} for all tasks. Single GROUP BY query."""
    _ensure_schema(db_path)
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            'SELECT phase_state, COUNT(*) FROM tasks GROUP BY phase_state'
        ).fetchall()
    finally:
        conn.close()
    return {int(row[0]): int(row[1]) for row in rows}


def list_waiting_guids(db_path: str = TASKS_DB_FILE) -> List[str]:
    """Return guids of all WAITING tasks, oldest first. Used to rebuild the
    in-memory waiting queue on startup without loading any Task objects."""
    _ensure_schema(db_path)
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            'SELECT guid FROM tasks WHERE phase_state = ? ORDER BY updated_at ASC',
            (TASK_STATE_WAITING,),
        ).fetchall()
    finally:
        conn.close()
    return [str(row['guid']) for row in rows]


def count_active_tasks(
    state_low: int = TASK_STATE_WAITING,
    state_high: int = 20,
    db_path: str = TASKS_DB_FILE,
) -> int:
    """Count tasks whose phase_state is in [state_low, state_high) — i.e. tasks
    that are still being worked on (waiting through make_archive). Used by the
    CLI to decide whether the process can exit."""
    _ensure_schema(db_path)
    conn = _connect(db_path)
    try:
        row = conn.execute(
            'SELECT COUNT(*) FROM tasks WHERE phase_state >= ? AND phase_state < ?',
            (int(state_low), int(state_high)),
        ).fetchone()
    finally:
        conn.close()
    return int(row[0] or 0)


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