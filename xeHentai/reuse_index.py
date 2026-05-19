#!/usr/bin/env python
# coding:utf-8

import os
import re
import time
import json
import sqlite3
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from .const import RE_INDEX

REUSE_INDEX_DB = 'h.reuse.db'
MAX_PRESCAN_CANDIDATES = 20  # Limit candidates to improve performance

_RE_MULTI_SPACE = re.compile(r'\s+')
_RE_TITLE_STATUS = re.compile(r'\[(?:ongoing|wip|complete|completed|end|fin|ended|finished|update|updated|進行中|连载中|連載中|完结|已完结|完結|已完結)\]', re.IGNORECASE)
_RE_TITLE_RANGE = re.compile(r'(?<!\d)(\d+)\s*[-~]\s*(\d+)(?!\d)')
_RE_TITLE_GID_PREFIX = re.compile(r'^\d+\s*-\s*')

# SQLite schema
SQL_SCHEMA = '''
CREATE TABLE IF NOT EXISTS galleries (
    gid TEXT PRIMARY KEY,
    url TEXT,
    source_type TEXT,
    source_path TEXT,
    title TEXT,
    updated_at INTEGER,
    fid_page_hash_map TEXT,
    fid_size_map TEXT
);

CREATE TABLE IF NOT EXISTS page_hashes (
    page_hash TEXT NOT NULL,
    gid TEXT NOT NULL,
    fid TEXT NOT NULL,
    source_type TEXT,
    source_path TEXT,
    member_name TEXT,
    size_text TEXT,
    updated_at INTEGER,
    PRIMARY KEY (page_hash, gid, fid)
);

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

CREATE TABLE IF NOT EXISTS version_graph (
    node_from TEXT NOT NULL,
    node_to TEXT NOT NULL,
    PRIMARY KEY (node_from, node_to)
);

CREATE INDEX IF NOT EXISTS idx_page_hash ON page_hashes(page_hash);
CREATE INDEX IF NOT EXISTS idx_title_normalized ON title_index(normalized_title, index_type);
CREATE INDEX IF NOT EXISTS idx_galleries_gid ON galleries(gid);
CREATE INDEX IF NOT EXISTS idx_version_from ON version_graph(node_from);
CREATE INDEX IF NOT EXISTS idx_version_to ON version_graph(node_to);
'''


def _get_db_connection(db_path: str = REUSE_INDEX_DB) -> sqlite3.Connection:
    """Get SQLite database connection with proper settings."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')  # Write-Ahead Logging for better concurrency
    conn.execute('PRAGMA synchronous=NORMAL')  # Balance safety and speed
    return conn


def _init_database(db_path: str = REUSE_INDEX_DB) -> None:
    """Initialize SQLite database with schema."""
    conn = _get_db_connection(db_path)
    try:
        conn.executescript(SQL_SCHEMA)
        conn.commit()
    finally:
        conn.close()





def _normalize_whitespace(text: str) -> str:
    return _RE_MULTI_SPACE.sub(' ', text).strip().lower()


def _strip_gid_prefix(title: str) -> str:
    """Remove GID prefix (e.g., '3000151 - ') from title to enable cross-gallery series matching."""
    return _RE_TITLE_GID_PREFIX.sub('', title or '')


def normalize_title_exact(title: str) -> str:
    """Normalize title for exact matching (removes status markers, GID, normalizes whitespace)."""
    title = _strip_gid_prefix(title or '')
    title = _RE_TITLE_STATUS.sub(' ', title)
    # Remove trailing "updated", "update!" etc.
    title = re.sub(r'\s+(?:updated?|new|latest)!?\s*$', ' ', title, flags=re.IGNORECASE)
    return _normalize_whitespace(title)


def normalize_title_series(title: str) -> str:
    """Normalize title for series matching (removes status, GID, chapter ranges)."""
    title = normalize_title_exact(title)
    title = _RE_TITLE_RANGE.sub(' ', title)
    return _normalize_whitespace(title)


def _node_gid(gid: str) -> Optional[str]:
    gid = str(gid or '')
    return 'gid:%s' % gid if gid else None


def _node_url(url: str) -> Optional[str]:
    url = str(url or '').strip()
    return 'url:%s' % url if url else None


def extract_gid_from_url(url: str) -> str:
    if not url:
        return ''
    matched = RE_INDEX.findall(url)
    if not matched:
        return ''
    return str(matched[0][0])


def ensure_reuse_index(index: Optional[Dict[str, Any]] = None, rebuild_missing_titles: bool = True) -> Dict[str, Any]:
    """Ensure index is valid SQLite index marker dict.
    
    For SQLite-only mode, just validates or returns a new SQLite marker dict.
    """
    if not isinstance(index, dict):
        index = {}
    
    # If already SQLite mode, return as-is
    if index.get('_sqlite'):
        return index
    
    # Return new SQLite marker dict
    return {
        '_sqlite': True,
        '_db_path': REUSE_INDEX_DB,
        'by_gid': {},
        'by_page_hash': {},
        'by_title_exact': {},
        'by_title_series': {},
        'version_graph': {'adjacency': {}}
    }


def load_reuse_index(db_path: str = REUSE_INDEX_DB) -> Dict[str, Any]:
    """Load reuse index from SQLite database.
    
    Note: Returns a database path reference rather than loading entire index into memory.
    """
    # Initialize database if it doesn't exist
    if not os.path.exists(db_path):
        _init_database(db_path)
    
    # Return a marker dict indicating SQLite mode
    return {
        '_sqlite': True,
        '_db_path': db_path,
        'by_gid': {},
        'by_page_hash': {},
        'by_title_exact': {},
        'by_title_series': {},
        'version_graph': {'adjacency': {}}
    }





def save_reuse_index(index: Optional[Dict[str, Any]]) -> None:
    """Save reuse index. For SQLite mode, this is a no-op since data is already persisted."""
    # SQLite mode - data is persisted to database immediately, no save needed
    pass





def collect_prescan_candidates(index: Optional[Dict[str, Any]], current_arc: str,
                               current_title: str, current_gid: str,
                               newer_versions: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    """Collect candidate archives for prescan based on title matching and version graph.
    
    Returns candidates sorted by priority, limited to MAX_PRESCAN_CANDIDATES.
    """
    candidates = [{
        'archive_path': current_arc,
        'candidate_gid': str(current_gid or ''),
        'candidate_url': '',
        'match_reason': 'current',
        'title': str(current_title or ''),
        'updated_at': 0,
    }]
    
    if not index:
        return candidates
    
    exact_key = normalize_title_exact(current_title)
    series_key = normalize_title_series(current_title)
    
    # Query SQLite database
    db_path = index.get('_db_path', REUSE_INDEX_DB)
    conn = _get_db_connection(db_path)
    try:
        # Query exact title matches
        rows = conn.execute('''
            SELECT gid, url, source_path, title, updated_at
            FROM title_index
            WHERE normalized_title = ? AND index_type = 'exact'
            ORDER BY updated_at DESC
            LIMIT ?
        ''', (exact_key, MAX_PRESCAN_CANDIDATES)).fetchall()
        
        for row in rows:
            source_path = row['source_path']
            if source_path and source_path.endswith('.zip') and os.path.exists(source_path):
                candidates.append({
                    'archive_path': source_path,
                    'candidate_gid': row['gid'],
                    'candidate_url': row['url'],
                    'match_reason': 'exact_title',
                    'title': row['title'],
                    'updated_at': row['updated_at']
                })
        
        # Query series title matches (avoid duplicates)
        existing_gids = {c.get('candidate_gid') for c in candidates}
        rows = conn.execute('''
            SELECT gid, url, source_path, title, updated_at
            FROM title_index
            WHERE normalized_title = ? AND index_type = 'series'
            ORDER BY updated_at DESC
            LIMIT ?
        ''', (series_key, MAX_PRESCAN_CANDIDATES)).fetchall()
        
        for row in rows:
            if row['gid'] in existing_gids:
                continue
            source_path = row['source_path']
            if source_path and source_path.endswith('.zip') and os.path.exists(source_path):
                candidates.append({
                    'archive_path': source_path,
                    'candidate_gid': row['gid'],
                    'candidate_url': row['url'],
                    'match_reason': 'series_title',
                    'title': row['title'],
                    'updated_at': row['updated_at']
                })
                existing_gids.add(row['gid'])
        
        # Query from version graph (newer_versions)
        for version in reversed(newer_versions or []):
            if len(candidates) >= MAX_PRESCAN_CANDIDATES:
                break
            gid = str(version.get('gid', ''))
            if not gid or gid == str(current_gid or '') or gid in existing_gids:
                continue
            
            row = conn.execute('''
                SELECT gid, url, source_type, source_path, title, updated_at
                FROM galleries WHERE gid = ?
            ''', (gid,)).fetchone()
            
            if row and row['source_type'] == 'zip':
                source_path = row['source_path']
                if source_path and os.path.exists(source_path):
                    candidates.append({
                        'archive_path': source_path,
                        'candidate_gid': gid,
                        'candidate_url': row['url'] or version.get('url', ''),
                        'match_reason': 'gnd_gid',
                        'title': row['title'],
                        'updated_at': row['updated_at']
                    })
                    existing_gids.add(gid)
    finally:
        conn.close()
    
    # Deduplicate by archive path
    uniq = []
    seen = set()
    for candidate in candidates:
        if len(uniq) >= MAX_PRESCAN_CANDIDATES + 1:  # +1 for current
            break
        archive_path = candidate.get('archive_path')
        if not archive_path:
            continue
        norm = os.path.normcase(os.path.abspath(archive_path))
        if norm in seen:
            continue
        seen.add(norm)
        uniq.append(candidate)
    
    return uniq


def prescan_extract_from_candidates(
    index: Optional[Dict[str, Any]],
    candidates: List[Dict[str, Any]],
    task: Any,
    require_relation: bool = True
) -> Dict[str, Any]:
    """Extract matching files from candidate archives to task directory before download.
    
    Args:
        index: Global reuse index
        candidates: List of candidate archives from collect_prescan_candidates()
        task: Task object with meta, fid maps, and methods
        require_relation: If True, only extract from candidates proven related via version graph
        
    Returns:
        Dict with 'extracted_count', 'sources' (list of source archives used)
    """
    import zipfile
    extracted_count = 0
    sources_used = []
    
    if not candidates or not hasattr(task, 'meta') or not task.meta:
        return {'extracted_count': 0, 'sources': []}
    
    # Get target directory and ensure it exists
    target_dir = task.get_task_dir()
    if not os.path.exists(target_dir):
        os.makedirs(target_dir)
    
    # Build hash -> (fid, file_name, size_text) map from task's expected files
    hash_to_fid_map = {}
    if hasattr(task, 'fid_2_page_hash_map') and task.fid_2_page_hash_map:
        for fid, page_hash in task.fid_2_page_hash_map.items():
            if not page_hash:
                continue
            file_name = task.fid_2_file_name_map.get(fid) if hasattr(task, 'fid_2_file_name_map') else None
            size_text = task.fid_2_file_size_map.get(fid) if hasattr(task, 'fid_2_file_size_map') else None
            if file_name and size_text:
                hash_to_fid_map[page_hash] = (fid, file_name, size_text)
    
    if not hash_to_fid_map:
        return {'extracted_count': 0, 'sources': []}
    
    # Process each candidate archive
    for candidate in candidates:
        archive_path = candidate.get('archive_path')
        if not archive_path or not os.path.exists(archive_path):
            continue
        
        # Skip if not a zip file
        if not archive_path.endswith('.zip'):
            continue
        
        # Check relationship if required
        if require_relation:
            match_reason = candidate.get('match_reason', '')
            if match_reason not in ('exact_title', 'series_title', 'gnd_gid', 'current'):
                continue
            
            # For title-based matches, validate via version graph or skip if paranoid
            if match_reason in ('exact_title', 'series_title'):
                candidate_gid = str(candidate.get('candidate_gid', ''))
                candidate_url = str(candidate.get('candidate_url', ''))
                current_gid = str(getattr(task, 'gid', ''))
                current_url = str(getattr(task, 'url', ''))
                
                # Allow if in newer_versions or connected in version graph
                is_related = is_known_related(
                    index, current_url, current_gid,
                    candidate_url, candidate_gid,
                    task.meta.newer_versions if hasattr(task.meta, 'newer_versions') else []
                )
                
                # For series_title matches without proven relationship, skip if require_relation
                if match_reason == 'series_title' and not is_related:
                    continue
        
        try:
            with zipfile.ZipFile(archive_path, 'r') as zf:
                # Try to read metadata
                try:
                    comment_str = zf.comment.decode('UTF-8', errors='ignore')
                    # Check if it's an xeHentai archive
                    if not comment_str.startswith('xeHentai Archiver v'):
                        continue
                    
                    # Parse metadata to get fid_page_hash_map
                    metadata_json_start = comment_str.find('{')
                    if metadata_json_start < 0:
                        continue
                    metadata_dict = json.loads(comment_str[metadata_json_start:])
                    source_hash_map = metadata_dict.get('fid_page_hash_map', {})
                    
                    if not source_hash_map:
                        continue
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                
                # Build fid -> member_name map from archive
                member_name_map = {}
                for member in zf.namelist():
                    if member.endswith('/'):
                        continue
                    # Try to extract fid from filename (e.g., "00001.jpg" -> "1")
                    basename = os.path.basename(member)
                    match = re.match(r'^(\d+)\..+$', basename)
                    if match:
                        fid_str = str(int(match.group(1)))
                        member_name_map[fid_str] = member
                
                # Extract matching files
                for source_fid, source_hash in source_hash_map.items():
                    if source_hash not in hash_to_fid_map:
                        continue
                    
                    target_fid, target_name, size_text = hash_to_fid_map[source_hash]
                    target_path = os.path.join(target_dir, target_name)
                    
                    # Skip if already exists
                    if os.path.exists(target_path):
                        continue
                    
                    # Find member in archive
                    member_name = member_name_map.get(str(source_fid))
                    if not member_name:
                        continue
                    
                    # Extract and verify size
                    tmp_path = "%s.xeh" % target_path
                    try:
                        with zf.open(member_name, 'r') as src:
                            with open(tmp_path, 'wb') as dst:
                                import shutil
                                shutil.copyfileobj(src, dst)
                        
                        # Verify size matches expected range
                        if hasattr(task, 'check_size_range') and not task.check_size_range(tmp_path, size_text):
                            os.remove(tmp_path)
                            continue
                        
                        # Rename to final name
                        os.rename(tmp_path, target_path)
                        extracted_count += 1
                        
                        # Mark as done in task
                        if hasattr(task, 'set_fid_done'):
                            task.set_fid_done(target_fid)
                        
                    except (KeyError, OSError, IOError):
                        if os.path.exists(tmp_path):
                            os.remove(tmp_path)
                        continue
                
                if extracted_count > 0 and archive_path not in sources_used:
                    sources_used.append(archive_path)
                    
        except (zipfile.BadZipFile, OSError, IOError):
            continue
    
    return {
        'extracted_count': extracted_count,
        'sources': sources_used
    }





def record_version_graph(index: Optional[Dict[str, Any]], current_url: str,
                         current_gid: str, newer_versions: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Record version relationships in the graph."""
    if not index:
        return index or {}
    
    current_gid_node = _node_gid(current_gid)
    current_url_node = _node_url(current_url)
    
    # Store in SQLite database
    db_path = index.get('_db_path', REUSE_INDEX_DB)
    conn = _get_db_connection(db_path)
    try:
        # Helper to insert edges
        def insert_edge(left, right):
            if not left or not right or left == right:
                return
            # Store canonicalized (smaller < larger)
            node_a, node_b = (left, right) if left < right else (right, left)
            conn.execute('''
                INSERT OR IGNORE INTO version_graph (node_from, node_to)
                VALUES (?, ?)
            ''', (node_a, node_b))
        
        insert_edge(current_gid_node, current_url_node)
        
        for version in newer_versions or []:
            version_gid = str(version.get('gid', ''))
            version_url = str(version.get('url', ''))
            version_gid_node = _node_gid(version_gid)
            version_url_node = _node_url(version_url)
            
            insert_edge(version_gid_node, version_url_node)
            insert_edge(current_gid_node, version_gid_node)
            insert_edge(current_url_node, version_url_node)
            insert_edge(current_gid_node, version_url_node)
            insert_edge(current_url_node, version_gid_node)
        
        conn.commit()
    finally:
        conn.close()
    return index





def is_known_related(index: Optional[Dict[str, Any]], current_url: str, current_gid: str,
                     candidate_url: str, candidate_gid: str,
                     newer_versions: Optional[List[Dict[str, Any]]] = None) -> bool:
    """Check if candidate is known to be related via version graph."""
    candidate_gid = str(candidate_gid or extract_gid_from_url(candidate_url))
    candidate_url = str(candidate_url or '')

    for version in newer_versions or []:
        if candidate_gid and candidate_gid == str(version.get('gid', '')):
            return True
        if candidate_url and candidate_url == str(version.get('url', '')):
            return True

    if not index:
        return False
    
    starts = [node for node in (_node_gid(current_gid), _node_url(current_url)) if node]
    targets = {node for node in (_node_gid(candidate_gid), _node_url(candidate_url)) if node}
    if not starts or not targets:
        return False
    
    # Query SQLite database for path using BFS
    db_path = index.get('_db_path', REUSE_INDEX_DB)
    conn = _get_db_connection(db_path)
    try:
        # BFS to find path
        visited = set(starts)
        queue = list(starts)
        
        while queue:
            node = queue.pop(0)
            if node in targets:
                return True
            
            # Query neighbors (both directions due to canonicalization)
            rows = conn.execute('''
                SELECT node_to FROM version_graph WHERE node_from = ?
                UNION
                SELECT node_from FROM version_graph WHERE node_to = ?
            ''', (node, node)).fetchall()
            
            for row in rows:
                neighbor = row[0]
                if neighbor not in visited:
                    visited.add(neighbor)
                    queue.append(neighbor)
        
        return False
    finally:
        conn.close()





def record_task_reuse(index: Optional[Dict[str, Any]], task: Any) -> Dict[str, Any]:
    """Record task data for reuse in future downloads."""
    if not index:
        index = {'_sqlite': True, '_db_path': REUSE_INDEX_DB}
    
    gid = str(getattr(task, 'gid', ''))
    if not gid:
        return index

    newer_versions = task.meta.newer_versions if getattr(task, 'meta', None) else []
    record_version_graph(index, getattr(task, 'url', ''), gid, newer_versions)

    if not getattr(task, 'fid_2_page_hash_map', None) or not getattr(task, 'fid_2_file_name_map', None):
        return index

    folder_path = task.get_task_dir()
    zip_path = '%s.zip' % folder_path
    source_type = None
    source_path = None
    if os.path.exists(zip_path):
        source_type = 'zip'
        source_path = zip_path
    elif os.path.exists(folder_path):
        source_type = 'folder'
        source_path = folder_path
    else:
        return index

    updated_at = int(time.time())
    
    # Store in SQLite database
    db_path = index.get('_db_path', REUSE_INDEX_DB)
    conn = _get_db_connection(db_path)
    try:
        # Insert/update gallery
        conn.execute('''
            INSERT OR REPLACE INTO galleries
            (gid, url, source_type, source_path, title, updated_at, fid_page_hash_map, fid_size_map)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            gid,
            getattr(task, 'url', ''),
            source_type,
            source_path,
            task.meta.title if hasattr(task, 'meta') else '',
            updated_at,
            json.dumps(dict(task.fid_2_page_hash_map)),
            json.dumps(dict(task.fid_2_file_size_map) if hasattr(task, 'fid_2_file_size_map') else {})
        ))
        
        # Insert/update page hashes
        for fid, page_hash in task.fid_2_page_hash_map.items():
            if not page_hash:
                continue
            file_name = task.fid_2_file_name_map.get(fid, task.get_fidpad(fid) if hasattr(task, 'get_fidpad') else str(fid))
            size_text = task.fid_2_file_size_map.get(fid) if hasattr(task, 'fid_2_file_size_map') else ''
            
            conn.execute('''
                INSERT OR REPLACE INTO page_hashes
                (page_hash, gid, fid, source_type, source_path, member_name, size_text, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                page_hash,
                gid,
                str(fid),
                'zip' if source_type == 'zip' else 'file',
                source_path if source_type == 'zip' else os.path.join(source_path, file_name),
                file_name if source_type == 'zip' else None,
                size_text,
                updated_at
            ))
        
        # Rebuild title indexes for this task
        title = task.meta.title if hasattr(task, 'meta') else ''
        if title and source_type == 'zip':
            exact_key = normalize_title_exact(title)
            series_key = normalize_title_series(title)
            
            # Delete old entries for this gid
            conn.execute('DELETE FROM title_index WHERE gid = ?', (gid,))
            
            # Insert exact match
            if exact_key:
                conn.execute('''
                    INSERT OR REPLACE INTO title_index
                    (normalized_title, index_type, gid, url, source_path, title, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                ''', (exact_key, 'exact', gid, getattr(task, 'url', ''), source_path, title, updated_at))
            
            # Insert series match
            if series_key:
                conn.execute('''
                    INSERT OR REPLACE INTO title_index
                    (normalized_title, index_type, gid, url, source_path, title, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                ''', (series_key, 'series', gid, getattr(task, 'url', ''), source_path, title, updated_at))
        
        conn.commit()
    finally:
        conn.close()
    return index
