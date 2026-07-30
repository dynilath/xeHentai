#!/usr/bin/env python
# coding:utf-8

import json
import logging
import os
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

_log = logging.getLogger('xeHentai.session_store')

from .const import TASK_STATE_FINISHED, TASK_STATE_PAUSED, TASK_STATE_WAITING

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
CREATE INDEX IF NOT EXISTS idx_tasks_state_updated ON tasks(phase_state, updated_at);

CREATE TABLE IF NOT EXISTS task_tags (
    guid TEXT NOT NULL,
    tag  TEXT NOT NULL,
    PRIMARY KEY (guid, tag)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_task_tags_tag ON task_tags(tag);

CREATE VIRTUAL TABLE IF NOT EXISTS tasks_fts USING fts5(
    title,
    content='',
    contentless_delete=1,
    tokenize='trigram',
    detail='full'
);
'''

_TASKS_CACHE: Dict[str, Dict[str, Any]] = {}
_schema_lock = threading.Lock()
_schema_ensured = False


def _ensure_schema_once(db_path: str = TASKS_DB_FILE) -> None:
    """Run schema migration once per process lifetime (thread-safe)."""
    global _schema_ensured
    if _schema_ensured:
        return
    with _schema_lock:
        if _schema_ensured:
            return
        _ensure_schema(db_path)
        _schema_ensured = True


def _connect(db_path: str = TASKS_DB_FILE) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA synchronous=NORMAL')
    conn.execute('PRAGMA busy_timeout=5000')
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

        # Tag normalization: one-time backfill from payload JSON.
        row = conn.execute('SELECT COUNT(*) FROM task_tags').fetchone()
        if row[0] == 0:
            conn.execute(
                '''
                INSERT OR IGNORE INTO task_tags (guid, tag)
                SELECT tasks.guid, j.value
                FROM tasks, json_each(json_extract(tasks.payload, '$.meta.tags')) AS j
                WHERE json_extract(tasks.payload, '$.meta.tags') IS NOT NULL
                '''
            )

        # Backfill bare tag values (text after ':') for tags that have a
        # namespace prefix.  Idempotent via INSERT OR IGNORE.
        conn.execute(
            '''
            INSERT OR IGNORE INTO task_tags (guid, tag)
            SELECT guid,
                   LTRIM(SUBSTR(tag, INSTR(tag, ':') + 1))
            FROM task_tags
            WHERE INSTR(tag, ':') > 0
              AND LTRIM(SUBSTR(tag, INSTR(tag, ':') + 1)) != ''
            '''
        )

        # Backfill tasks_fts from the tasks table so existing rows are
        # searchable.  Idempotent: skips if FTS already populated.
        fts_cnt = conn.execute('SELECT COUNT(*) FROM tasks_fts').fetchone()[0]
        if fts_cnt == 0:
            conn.execute(
                '''
                INSERT INTO tasks_fts(rowid, title)
                SELECT rowid, title FROM tasks WHERE title IS NOT NULL
                '''
            )

        conn.commit()
    finally:
        conn.close()


def _bare_tag_value(tag: str) -> Optional[str]:
    """Extract the value part of a namespace:value tag for exact-match lookups.

    Returns the substring after the first ':', or None if the tag has no ':'
    and should not produce a separate bare-value row.
    """
    colon = tag.find(':')
    if colon >= 0:
        after = tag[colon + 1:]
        return after.strip() if after.strip() else None
    return None


def _sync_task_tags(conn: sqlite3.Connection, guid: str, payload: Dict[str, Any]) -> None:
    """Synchronize task_tags rows from a task payload dict (within a transaction).

    For each namespace:value tag, both the full tag AND the bare value
    (text after ':') are stored.  This enables the hybrid search path:
      - tag = 'namespace:value' → exact namespace:value filter
      - tag = 'value'          → exact value match from free-text terms
    """
    conn.execute('DELETE FROM task_tags WHERE guid = ?', (str(guid),))
    meta = payload.get('meta')
    if isinstance(meta, dict):
        tags = meta.get('tags', [])
        if tags:
            rows = [(str(guid), str(t)) for t in tags]
            # Also insert bare values for tags that have a namespace prefix.
            for t in tags:
                bv = _bare_tag_value(str(t))
                if bv is not None:
                    rows.append((str(guid), bv))
            conn.executemany(
                'INSERT OR IGNORE INTO task_tags (guid, tag) VALUES (?, ?)',
                rows,
            )


def _coerce_payload_dict(payload: Any) -> Dict[str, Any]:
    return payload if isinstance(payload, dict) else {}


def _extract_task_row(task_payload: Dict[str, Any], fallback_guid: str) -> Dict[str, Any]:
    payload = _coerce_payload_dict(task_payload)
    guid = str(payload.get('guid', fallback_guid) or fallback_guid)
    gid = str(payload.get('gid', '') or '')
    url = str(payload.get('url', '') or '')
    phase_state = int(payload.get('state', TASK_STATE_WAITING) or TASK_STATE_WAITING)
    _meta = payload.get('meta')
    meta = _meta if isinstance(_meta, dict) else {}
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
    _ensure_schema_once(db_path)
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
            # Maintain task_tags for upserted tasks.
            for guid in to_upsert:
                _sync_task_tags(conn, guid, tasks[guid])
            for guid in to_delete:
                conn.execute('DELETE FROM task_tags WHERE guid = ?', (str(guid),))
                conn.execute('DELETE FROM tasks WHERE guid = ?', (str(guid),))

        _TASKS_CACHE[db_path] = dict(tasks)
    finally:
        conn.close()


def _load_tasks_sqlite(db_path: str = TASKS_DB_FILE) -> Dict[str, Any]:
    _ensure_schema_once(db_path)
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
    _ensure_schema_once(db_path)
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
            # Maintain task_tags from the payload dict.
            _sync_task_tags(conn, row['guid'], task_payload)
            # Sync FTS5 index: insert/replace the title column.
            conn.execute(
                'INSERT OR REPLACE INTO tasks_fts(rowid, title) '
                'SELECT rowid, ? FROM tasks WHERE guid = ?',
                (row['title'], row['guid']),
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
    'gid': "CAST(NULLIF(gid, '') AS INTEGER)",
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


def _parse_search_query(q: str) -> Tuple[List[str], List[Tuple[str, str]]]:
    """Split a search query into free-text terms and structured tag filters.

    - Terms containing ':' and NOT wrapped in double quotes are treated as
      ``namespace:value`` tag filters.
    - Quoted terms (``"..."``) are ALWAYS free-text, even if they contain ':'.
      Quotes serve as both grouping (multi-word terms stay together) and
      literal-text marker for ':'.
    - All other terms (CJK text, bare words) become free-text terms matched
      against the FTS title index AND via exact tag value lookup.
    - A bare ``namespace:`` (no value after ':') yields a prefix filter.

    Returns (free_terms, tag_filters) where each tag_filter is (ns, val).

    Example:
        'male:feminization "breast expansion" 私は女の子が好'
        → (['breast expansion', '私は女の子が好'], [('male', 'feminization')])
    """
    if not q or not q.strip():
        return [], []

    # Parse raw string preserving quote state so we know which terms were
    # explicitly quoted (→ always free-text, even with ':' inside).
    raw_terms: List[Tuple[str, bool]] = []  # (term, was_quoted)
    i = 0
    q = q.strip()
    while i < len(q):
        if q[i] == ' ':
            i += 1
            continue
        if q[i] == '"':
            j = q.index('"', i + 1) if '"' in q[i + 1:] else len(q)
            term = q[i + 1:j]
            i = j + 1
            if term:
                raw_terms.append((term, True))
        else:
            j = i
            while j < len(q) and q[j] not in (' ', '"'):
                j += 1
            term = q[i:j]
            i = j
            if term:
                raw_terms.append((term, False))

    free_terms: List[str] = []
    tag_filters: List[Tuple[str, str]] = []
    for term, was_quoted in raw_terms:
        if was_quoted:
            # Quoted terms are always free-text (literal text grouping).
            free_terms.append(term)
            continue
        colon = term.find(':')
        if colon >= 0:
            ns = term[:colon].strip()
            val = term[colon + 1:].strip()
            if ns:
                tag_filters.append((ns, val))
                continue
        free_terms.append(term)
    return free_terms, tag_filters


def _parse_search_terms(q: str) -> List[str]:
    """Parse a search query string into space-separated terms, respecting
    double-quoted substrings within a term. Inner quotes are stripped.

    Example: '测试 translation:"chinese text" misc:group'
    → ['测试', 'translation:chinese text', 'misc:group']
    """
    if not q or not q.strip():
        return []
    terms = []
    i = 0
    q = q.strip()
    while i < len(q):
        if q[i] == ' ':
            i += 1
            continue
        j = i
        while j < len(q) and q[j] not in (' ', '"'):
            j += 1
        if j < len(q) and q[j] == '"':
            k = q.index('"', j + 1) if '"' in q[j+1:] else len(q)
            # include the quoted part but strip the quote chars themselves
            term = q[i:j] + q[j+1:k]
            i = k + 1
        else:
            term = q[i:j]
            i = j
        if term:
            terms.append(term)
    return terms


def query_tasks(
    *,
    states: Optional[List[int]] = None,
    tags: Optional[List[str]] = None,
    gid: Optional[str] = None,
    url: Optional[str] = None,
    q: Optional[str] = None,
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
                are normalized into the ``task_tags`` table with an indexed
                ``tag`` column for efficient exact-match and LIKE lookups.
      - gid/url: exact match.
      - q:      space-separated search terms; ALL must match.  Terms containing
                ':' are treated as ``namespace:value`` tag filters (exact or
                prefix match via the task_tags index).  All other terms are
                free-text: matched against title via FTS5 trigram AND against
                tag bare values via exact index lookup.

    Returns (total_count, [light_dict, ...]). The returned dicts never include
    the payload, even when ``tags`` filtering read it internally.
    """
    t0 = time.perf_counter()
    _ensure_schema_once(db_path)
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
        # OR-match tasks that carry any of the requested tags.
        # Tags are normalized into the task_tags table (indexed by tag).
        normalized_tags = [str(t) for t in tags if t]
        if normalized_tags:
            tag_placeholders = ', '.join('?' for _ in normalized_tags)
            where.append(
                'EXISTS (SELECT 1 FROM task_tags WHERE guid = tasks.guid AND tag IN (%s))'
                % tag_placeholders
            )
            params.extend(normalized_tags)
    if gid:
        where.append('gid = ?')
        params.append(str(gid))
    if url:
        where.append('url = ?')
        params.append(str(url))
    if q:
        free_terms, tag_filters = _parse_search_query(q)
        # --- Path A: FTS title + exact tag value for each free-text term ---
        for term in free_terms:
            where.append(
                '(tasks.rowid IN '
                '(SELECT rowid FROM tasks_fts WHERE tasks_fts MATCH ?) '
                'OR EXISTS (SELECT 1 FROM task_tags WHERE guid = tasks.guid AND tag = ?))'
            )
            # FTS5 MATCH with double-quoted term for phrase/substring matching.
            fts_query = '"%s"' % term.replace('"', '""')
            params.extend([fts_query, term])
        # --- Path B: structured namespace:value tag filters ---
        for ns, val in tag_filters:
            if val:
                # Exact match: tag = 'namespace:value'
                where.append(
                    'EXISTS (SELECT 1 FROM task_tags WHERE guid = tasks.guid AND tag = ?)'
                )
                params.append('%s:%s' % (ns, val))
            else:
                # Prefix match: tag LIKE 'namespace:%'
                where.append(
                    'EXISTS (SELECT 1 FROM task_tags WHERE guid = tasks.guid AND tag LIKE ?)'
                )
                params.append('%s:%%' % ns)

    where_clause = (' WHERE ' + ' AND '.join(where)) if where else ''
    offset = max(0, int(offset or 0))
    limit = max(1, min(1000, int(limit or 100)))
    t1 = time.perf_counter()

    conn = _connect(db_path)
    try:
        t2 = time.perf_counter()
        total = conn.execute(
            'SELECT COUNT(*) FROM tasks%s' % where_clause, params
        ).fetchone()[0]
        t3 = time.perf_counter()
        rows = conn.execute(
            'SELECT %s FROM tasks%s ORDER BY %s %s LIMIT ? OFFSET ?'
            % (_LIGHT_COLUMNS, where_clause, order_col, direction),
            params + [limit, offset],
        ).fetchall()
        t4 = time.perf_counter()
    finally:
        conn.close()

    result_total = int(total or 0)
    result_rows = [_row_to_light_dict(r) for r in rows]
    t5 = time.perf_counter()

    _log.info(
        'query_tasks | total=%d returned=%d | build=%.1fms count=%.1fms select=%.1fms rows=%.1fms total=%.1fms | '
        'states=%s tags=%s gid=%s q=%s order=%s/%s limit=%d offset=%d',
        result_total, len(result_rows),
        (t1 - t0) * 1000, (t3 - t2) * 1000, (t4 - t3) * 1000,
        (t5 - t4) * 1000, (t5 - t0) * 1000,
        states, tags, gid, q, order_by, order_dir, limit, offset,
    )

    return result_total, result_rows


def get_task_row(guid: str, db_path: str = TASKS_DB_FILE) -> Optional[Dict[str, Any]]:
    """Return lightweight columns for a single task, or None if not found."""
    _ensure_schema_once(db_path)
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
    _ensure_schema_once(db_path)
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
    _ensure_schema_once(db_path)
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
    _ensure_schema_once(db_path)
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
    _ensure_schema_once(db_path)
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
    _ensure_schema_once(db_path)
    conn = _connect(db_path)
    try:
        with conn:
            conn.execute('DELETE FROM task_tags WHERE guid = ?', (str(guid),))
            conn.execute(
                'DELETE FROM tasks_fts WHERE rowid = (SELECT rowid FROM tasks WHERE guid = ?)',
                (str(guid),),
            )
            conn.execute('DELETE FROM tasks WHERE guid = ?', (str(guid),))
    finally:
        conn.close()
    cached = _TASKS_CACHE.get(db_path)
    if cached is not None:
        cached.pop(str(guid), None)


def count_tasks_by_state(db_path: str = TASKS_DB_FILE) -> Dict[int, int]:
    """Return {phase_state: count} for all tasks. Single GROUP BY query."""
    _ensure_schema_once(db_path)
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            'SELECT phase_state, COUNT(*) FROM tasks GROUP BY phase_state'
        ).fetchall()
    finally:
        conn.close()
    return {int(row[0]): int(row[1]) for row in rows}


def list_waiting_guids(db_path: str = TASKS_DB_FILE) -> List[str]:
    """Return guids of all tasks that should be re-enqueued on startup, oldest
    first.  Includes WAITING (1) as well as mid-processing states (2–19) that
    were interrupted by a previous shutdown.  Excludes PAUSED (0), FINISHED
    (20), and all error/terminal states (<0)."""
    _ensure_schema_once(db_path)
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            'SELECT guid FROM tasks WHERE phase_state > ? AND phase_state < ? ORDER BY updated_at ASC',
            (TASK_STATE_PAUSED, TASK_STATE_FINISHED),
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
    _ensure_schema_once(db_path)
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

    # Backfill tasks_fts for newly imported tasks.
    _ensure_schema_once(db_path)
    conn = _connect(db_path)
    try:
        conn.execute(
            '''
            INSERT OR REPLACE INTO tasks_fts(rowid, title)
            SELECT rowid, title FROM tasks WHERE title IS NOT NULL
            '''
        )
    finally:
        conn.close()

    return {
        'source': len(tasks),
        'imported': len(deduped),
        'skipped': skipped,
    }