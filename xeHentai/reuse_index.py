#!/usr/bin/env python
# coding:utf-8

import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

REUSE_INDEX_DB = 'h.reuse.db'
MAX_ARCHIVE_REUSE_CANDIDATES = 20

_RE_MULTI_SPACE = re.compile(r'\s+')
_RE_TITLE_STATUS = re.compile(
    r'[\[\(]\s*(?:ongoing|wip|complete|completed|end|fin|ended|finished|update|updated|進行中|连载中|連載中|完结|已完结|完結|已完結)(?:\s*[:：-]?\s*\d{4}[./-]\d{1,2}[./-]\d{1,2})?\s*[\]\)]',
    re.IGNORECASE,
)
_RE_TITLE_UPDATE_BLOCK = re.compile(
    r'[\[\(（](?=[^\]\)）]*\d)(?=[^\]\)）]*(?:updated?|更新))[^\]\)）]*[\]\)）]',
    re.IGNORECASE,
)
_RE_TITLE_RANGE = re.compile(r'(?<!\d)(?:\d+(?:[./]\d+){0,2})\s*[-~]\s*(?:\d+(?:[./]\d+){0,2})(?!\d)')
_RE_TITLE_GID_PREFIX = re.compile(r'^\d+\s*-\s*')
_RE_GID_FROM_URL = re.compile(r'/g/(\d+)/([0-9a-fA-F]+)')
_RE_TITLE_SEPARATORS = re.compile(r'\s*[/|｜]\s*')
_RE_LEGACY_TITLE_STATUS = re.compile(
    r'\[(?:ongoing|wip|complete|completed|end|fin|ended|finished|update|updated|進行中|连载中|連載中|完结|已完结|完結|已完結)\]',
    re.IGNORECASE,
)
_RE_LEGACY_TITLE_RANGE = re.compile(r'(?<!\d)(\d+)\s*[-~]\s*(\d+)(?!\d)')

_RE_TITLE_CHAPTER_RANGE = re.compile(
    r'(?:\s*[-~]\s*)?'
    r'(?:chapter|ch\.|chap\.|vol\.|volume|part|ep\.|episode)\s*'
    r'\d+(?:[./]\d+){0,2}'
    r'\s*[-~]\s*'
    r'(?:(?:chapter|ch\.|chap\.|vol\.|volume|part|ep\.|episode)\s*)?'
    r'\d+(?:[./]\d+){0,2}',
    re.IGNORECASE,
)

SQL_SCHEMA = '''
CREATE TABLE IF NOT EXISTS title_index (
    normalized_title TEXT NOT NULL,
    index_type TEXT NOT NULL,
    gid TEXT NOT NULL,
    url TEXT,
    source_path TEXT,
    title TEXT,
    updated_at INTEGER,
    PRIMARY KEY (normalized_title, index_type, gid)
);

CREATE INDEX IF NOT EXISTS idx_title_normalized ON title_index(normalized_title, index_type);
'''


@dataclass(frozen=True)
class TitleIndexEntry:
    normalized_title: str
    index_type: str
    gid: str
    url: str
    source_path: str
    title: str
    updated_at: int


@dataclass(frozen=True)
class ArchiveReuseCandidate:
    archive_path: str
    gid: str
    url: str
    match_reason: str
    title: str
    updated_at: int
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            'archive_path': self.archive_path,
            'gid': self.gid,
            'url': self.url,
            'match_reason': self.match_reason,
            'title': self.title,
            'updated_at': self.updated_at,
        }
        
    @staticmethod
    def from_dict(data: Dict[str, Any]) -> 'ArchiveReuseCandidate':
        return ArchiveReuseCandidate(
            archive_path=str(data.get('archive_path', '') or ''),
            gid=str(data.get('gid', '') or ''),
            url=str(data.get('url', '') or ''),
            match_reason=str(data.get('match_reason', '') or ''),
            title=str(data.get('title', '') or ''),
            updated_at=int(data.get('updated_at', 0) or 0),
        )


@dataclass(frozen=True)
class ReuseIndexHandle:
    db_path: str = REUSE_INDEX_DB


class ReuseIndexStore:
    def __init__(self, db_path: str = REUSE_INDEX_DB):
        self.db_path = db_path

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA synchronous=NORMAL')
        return conn

    def ensure_schema(self) -> None:
        conn = self._connect()
        try:
            conn.executescript(SQL_SCHEMA)
            conn.commit()
        finally:
            conn.close()

    def list_title_entries(self, normalized_title: str, index_type: str,
                           limit: int = MAX_ARCHIVE_REUSE_CANDIDATES) -> List[TitleIndexEntry]:
        if not normalized_title:
            return []

        conn = self._connect()
        try:
            rows = conn.execute(
                '''
                SELECT normalized_title, index_type, gid, url, source_path, title, updated_at
                FROM title_index
                WHERE normalized_title = ? AND index_type = ?
                ORDER BY updated_at DESC
                LIMIT ?
                ''',
                (normalized_title, index_type, limit),
            ).fetchall()
        finally:
            conn.close()

        return [self._row_to_title_entry(row) for row in rows]

    def replace_gid_title_entries(self, gid: str, entries: List[TitleIndexEntry]) -> None:
        gid = str(gid or '')
        if not gid:
            return

        conn = self._connect()
        try:
            conn.execute('DELETE FROM title_index WHERE gid = ?', (gid,))
            for entry in entries:
                conn.execute(
                    '''
                    INSERT OR REPLACE INTO title_index
                    (normalized_title, index_type, gid, url, source_path, title, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ''',
                    (
                        entry.normalized_title,
                        entry.index_type,
                        entry.gid,
                        entry.url,
                        entry.source_path,
                        entry.title,
                        entry.updated_at,
                    ),
                )
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _row_to_title_entry(row: sqlite3.Row) -> TitleIndexEntry:
        return TitleIndexEntry(
            normalized_title=str(row['normalized_title'] or ''),
            index_type=str(row['index_type'] or ''),
            gid=str(row['gid'] or ''),
            url=str(row['url'] or ''),
            source_path=str(row['source_path'] or ''),
            title=str(row['title'] or ''),
            updated_at=int(row['updated_at'] or 0),
        )


def _normalize_whitespace(text: str) -> str:
    return _RE_MULTI_SPACE.sub(' ', text).strip().lower()


def _strip_gid_prefix(title: str) -> str:
    return _RE_TITLE_GID_PREFIX.sub('', title or '')


def normalize_title_exact(title: str) -> str:
    title = _strip_gid_prefix(title or '')
    title = _RE_TITLE_UPDATE_BLOCK.sub(' ', title)
    title = _RE_TITLE_STATUS.sub(' ', title)
    title = _RE_TITLE_SEPARATORS.sub(' ', title)
    title = re.sub(r'\s+(?:updated?|new|latest)!?\s*$', ' ', title, flags=re.IGNORECASE)
    return _normalize_whitespace(title)


def normalize_title_series(title: str) -> str:
    title = normalize_title_exact(title)
    title = _RE_TITLE_CHAPTER_RANGE.sub(' ', title)
    title = _RE_TITLE_RANGE.sub(' ', title)
    return _normalize_whitespace(title)


def _legacy_normalize_title_exact(title: str) -> str:
    title = _strip_gid_prefix(title or '')
    title = _RE_LEGACY_TITLE_STATUS.sub(' ', title)
    title = re.sub(r'\s+(?:updated?|new|latest)!?\s*$', ' ', title, flags=re.IGNORECASE)
    return _normalize_whitespace(title)


def _legacy_normalize_title_series(title: str) -> str:
    title = _legacy_normalize_title_exact(title)
    title = _RE_TITLE_CHAPTER_RANGE.sub(' ', title)
    title = _RE_LEGACY_TITLE_RANGE.sub(' ', title)
    return _normalize_whitespace(title)


def _unique_title_keys(*keys: str) -> List[str]:
    uniq: List[str] = []
    seen = set()
    for key in keys:
        key = str(key or '')
        if not key or key in seen:
            continue
        seen.add(key)
        uniq.append(key)
    return uniq


def extract_gid_from_url(url: str) -> str:
    if not url:
        return ''
    matched = _RE_GID_FROM_URL.search(url)
    return matched.group(1) if matched else ''


def _get_store(index: Optional[ReuseIndexHandle] = None,
               db_path: Optional[str] = None) -> ReuseIndexStore:
    if db_path:
        return ReuseIndexStore(db_path)
    if index is not None:
        return ReuseIndexStore(index.db_path)
    return ReuseIndexStore(REUSE_INDEX_DB)


def _make_index_handle(db_path: str) -> ReuseIndexHandle:
    return ReuseIndexHandle(db_path=db_path)


def _build_title_entries(gid: str, url: str, source_path: str,
                         title: str, updated_at: int) -> List[TitleIndexEntry]:
    title = str(title or '')
    if not title:
        return []

    gid = str(gid or '')
    url = str(url or '')
    source_path = str(source_path or '')
    entries: List[TitleIndexEntry] = []

    exact_key = normalize_title_exact(title)
    if exact_key:
        entries.append(TitleIndexEntry(
            normalized_title=exact_key,
            index_type='exact',
            gid=gid,
            url=url,
            source_path=source_path,
            title=title,
            updated_at=updated_at,
        ))

    series_key = normalize_title_series(title)
    if series_key:
        entries.append(TitleIndexEntry(
            normalized_title=series_key,
            index_type='series',
            gid=gid,
            url=url,
            source_path=source_path,
            title=title,
            updated_at=updated_at,
        ))

    return entries


def ensure_reuse_index(index: Optional[ReuseIndexHandle] = None) -> ReuseIndexHandle:
    if index is not None:
        store = _get_store(index=index)
        store.ensure_schema()
        return index

    db_path = REUSE_INDEX_DB
    store = _get_store(db_path=db_path)
    store.ensure_schema()
    return _make_index_handle(db_path)


def load_reuse_index(db_path: str = REUSE_INDEX_DB) -> ReuseIndexHandle:
    store = _get_store(db_path=db_path)
    store.ensure_schema()
    return _make_index_handle(db_path)


def save_reuse_index(index: ReuseIndexHandle) -> None:
    store = _get_store(index=index)
    store.ensure_schema()


def collect_archive_reuse_candidates(index: ReuseIndexHandle,
                                     current_title: str, current_gid: str) -> List[ArchiveReuseCandidate]:
    store = _get_store(index=index)
    exact_entries: List[TitleIndexEntry] = []
    series_entries: List[TitleIndexEntry] = []

    for exact_key in _unique_title_keys(
        normalize_title_exact(current_title),
        _legacy_normalize_title_exact(current_title),
    ):
        exact_entries.extend(store.list_title_entries(exact_key, 'exact'))

    for series_key in _unique_title_keys(
        normalize_title_series(current_title),
        _legacy_normalize_title_series(current_title),
    ):
        series_entries.extend(store.list_title_entries(series_key, 'series'))

    candidates: List[ArchiveReuseCandidate] = []
    seen_gids = set()
    current_gid = str(current_gid or '')

    for entry in exact_entries:
        if not entry.source_path or not entry.source_path.endswith('.zip') or not os.path.exists(entry.source_path):
            continue
        candidates.append(ArchiveReuseCandidate(
            archive_path=entry.source_path,
            gid=entry.gid,
            url=entry.url,
            match_reason='exact_title',
            title=entry.title,
            updated_at=entry.updated_at,
        ))
        seen_gids.add(entry.gid)

    for entry in series_entries:
        if not entry.gid or entry.gid in seen_gids:
            continue
        if not entry.source_path or not entry.source_path.endswith('.zip') or not os.path.exists(entry.source_path):
            continue
        candidates.append(ArchiveReuseCandidate(
            archive_path=entry.source_path,
            gid=entry.gid,
            url=entry.url,
            match_reason='series_title',
            title=entry.title,
            updated_at=entry.updated_at,
        ))
        seen_gids.add(entry.gid)

    uniq: List[ArchiveReuseCandidate] = []
    seen_paths = set()
    for candidate in candidates:
        if candidate.gid == current_gid:
            continue
        norm = os.path.normcase(os.path.abspath(candidate.archive_path))
        if norm in seen_paths:
            continue
        seen_paths.add(norm)
        uniq.append(candidate)
        if len(uniq) >= MAX_ARCHIVE_REUSE_CANDIDATES:
            break
    return uniq


def _parse_archive_metadata(comment_str: str) -> Optional[Dict[str, Any]]:
    lbrace = comment_str.find('{')
    rbrace = comment_str.rfind('}')
    if lbrace == -1 or rbrace == -1 or lbrace > rbrace:
        return None

    meta_str = comment_str[lbrace:rbrace + 1]
    if not meta_str:
        return None

    try:
        data = json.loads(meta_str)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None

    if not isinstance(data, dict):
        return None
    if 'url' not in data or 'title' not in data or 'download_ori' not in data:
        return None
    return data


def add_zip_to_reuse_index(index: ReuseIndexHandle, zip_path: str) -> Dict[str, Any]:
    import zipfile

    result = {
        'ok': False,
        'zip_path': zip_path,
        'reason': '',
        'title_indexed': False,
    }

    if not zip_path or not os.path.exists(zip_path) or not zip_path.endswith('.zip'):
        result['reason'] = 'invalid zip path'
        return result

    updated_at = int(time.time())

    try:
        with zipfile.ZipFile(zip_path, 'r') as zf:
            comment_str = zf.comment.decode('UTF-8', errors='ignore')
            metadata = _parse_archive_metadata(comment_str)
            if metadata is None:
                result['reason'] = 'unparseable archive comment'
                return result

            archive_url = str(metadata.get('url', '') or '')
            archive_gid = extract_gid_from_url(archive_url)
            if not archive_gid:
                basename = os.path.basename(zip_path)
                matched = re.match(r'^(\d+)\s*-\s*', basename)
                if matched:
                    archive_gid = matched.group(1)
            if not archive_gid:
                archive_gid = os.path.splitext(os.path.basename(zip_path))[0]

            title = str(metadata.get('title', '') or '')
            entries = _build_title_entries(
                gid=archive_gid,
                url=archive_url,
                source_path=zip_path,
                title=title,
                updated_at=updated_at,
            )
            _get_store(index=index).replace_gid_title_entries(archive_gid, entries)
            result['title_indexed'] = bool(entries)
            result['ok'] = True
            return result
    except (OSError, zipfile.BadZipFile, sqlite3.Error, json.JSONDecodeError) as ex:
        result['reason'] = str(ex)
        return result


def record_task_reuse(index: ReuseIndexHandle, task: Any) -> ReuseIndexHandle:
    gid = str(getattr(task, 'gid', ''))
    if not gid:
        return index

    title = task.meta.title if hasattr(task, 'meta') else ''
    if not title:
        return index

    folder_path = task.get_task_dir()
    zip_path = '%s.zip' % folder_path
    if not os.path.exists(zip_path):
        return index

    entries = _build_title_entries(
        gid=gid,
        url=str(getattr(task, 'url', '') or ''),
        source_path=zip_path,
        title=title,
        updated_at=int(time.time()),
    )
    _get_store(index=index).replace_gid_title_entries(gid, entries)
    return index