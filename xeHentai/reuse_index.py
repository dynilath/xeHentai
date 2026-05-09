#!/usr/bin/env python
# coding:utf-8

import os
import re
import time
import json
from collections import deque
from typing import Any, Dict, Iterable, List, Optional, Set

from .const import RE_INDEX

REUSE_INDEX_FILE = 'h.reuse.json'

_RE_MULTI_SPACE = re.compile(r'\s+')
_RE_TITLE_STATUS = re.compile(r'\[(?:ongoing|complete|completed|end|fin)\]', re.IGNORECASE)
_RE_TITLE_RANGE = re.compile(r'(?<!\d)(\d+)\s*[-~]\s*(\d+)(?!\d)')


def _normalize_whitespace(text: str) -> str:
    return _RE_MULTI_SPACE.sub(' ', text).strip().lower()


def normalize_title_exact(title: str) -> str:
    title = _RE_TITLE_STATUS.sub(' ', title or '')
    return _normalize_whitespace(title)


def normalize_title_series(title: str) -> str:
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
    if not isinstance(index, dict):
        index = {}

    if not isinstance(index.get('by_gid'), dict):
        index['by_gid'] = {}
    if not isinstance(index.get('by_page_hash'), dict):
        index['by_page_hash'] = {}
    if not isinstance(index.get('by_title_exact'), dict):
        index['by_title_exact'] = {}
    if not isinstance(index.get('by_title_series'), dict):
        index['by_title_series'] = {}
    version_graph = index.get('version_graph')
    if not isinstance(version_graph, dict):
        version_graph = {}
        index['version_graph'] = version_graph
    if not isinstance(version_graph.get('adjacency'), dict):
        version_graph['adjacency'] = {}

    if rebuild_missing_titles and not index['by_title_exact'] and not index['by_title_series'] and index['by_gid']:
        rebuild_title_indexes(index)
    return index


def load_reuse_index(path: str = REUSE_INDEX_FILE) -> Dict[str, Any]:
    if not os.path.exists(path):
        return ensure_reuse_index({})
    with open(path) as f:
        return ensure_reuse_index(json.loads(f.read()))


def save_reuse_index(index: Optional[Dict[str, Any]], path: str = REUSE_INDEX_FILE) -> None:
    index = ensure_reuse_index(index)
    tmp_path = '%s.next' % path
    with open(tmp_path, 'w') as f:
        f.write(json.dumps(index))
    os.path.exists(path) and os.remove(path)
    os.rename(tmp_path, path)


def _sorted_zip_candidates(entries: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    valid = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        source_path = entry.get('source_path')
        if source_path and source_path.endswith('.zip'):
            valid.append(entry)
    valid.sort(key=lambda item: int(item.get('updated_at', 0)), reverse=True)
    return valid


def _candidate_from_gid_entry(entry: Dict[str, Any], match_reason: str) -> Optional[Dict[str, Any]]:
    if entry.get('source_type') != 'zip':
        return None
    source_path = entry.get('source_path')
    if not source_path:
        return None
    return {
        'archive_path': source_path,
        'candidate_gid': str(entry.get('gid', '')),
        'candidate_url': str(entry.get('url', '')),
        'match_reason': match_reason,
        'title': str(entry.get('title', '')),
        'updated_at': int(entry.get('updated_at', 0)),
    }


def collect_prescan_candidates(index: Optional[Dict[str, Any]], current_arc: str,
                               current_title: str, current_gid: str,
                               newer_versions: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    index = ensure_reuse_index(index)
    candidates = [{
        'archive_path': current_arc,
        'candidate_gid': str(current_gid or ''),
        'candidate_url': '',
        'match_reason': 'current',
        'title': str(current_title or ''),
        'updated_at': 0,
    }]

    exact_key = normalize_title_exact(current_title)
    series_key = normalize_title_series(current_title)

    for entry in _sorted_zip_candidates(index['by_title_exact'].get(exact_key, [])):
        candidates.append(dict(entry, match_reason='exact_title'))

    for entry in _sorted_zip_candidates(index['by_title_series'].get(series_key, [])):
        candidates.append(dict(entry, match_reason='series_title'))

    by_gid = index.get('by_gid', {})
    for version in reversed(newer_versions or []):
        gid = str(version.get('gid', ''))
        if not gid or gid == str(current_gid or ''):
            continue
        gid_entry = by_gid.get(gid)
        if not isinstance(gid_entry, dict):
            continue
        candidate = _candidate_from_gid_entry(dict(gid_entry, gid=gid), 'gnd_gid')
        if candidate is None:
            continue
        if not candidate.get('candidate_url'):
            candidate['candidate_url'] = str(version.get('url', ''))
        candidates.append(candidate)

    uniq = []
    seen = set()
    for candidate in candidates:
        archive_path = candidate.get('archive_path')
        if not archive_path:
            continue
        norm = os.path.normcase(os.path.abspath(archive_path))
        if norm in seen:
            continue
        seen.add(norm)
        uniq.append(candidate)
    return uniq


def _link_nodes(adjacency: Dict[str, List[str]], left: Optional[str], right: Optional[str]) -> None:
    if not left or not right:
        return
    adjacency.setdefault(left, [])
    adjacency.setdefault(right, [])
    if right not in adjacency[left]:
        adjacency[left].append(right)
    if left not in adjacency[right]:
        adjacency[right].append(left)


def record_version_graph(index: Optional[Dict[str, Any]], current_url: str,
                         current_gid: str, newer_versions: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    index = ensure_reuse_index(index)
    adjacency = index['version_graph']['adjacency']

    current_gid_node = _node_gid(current_gid)
    current_url_node = _node_url(current_url)
    _link_nodes(adjacency, current_gid_node, current_url_node)

    for version in newer_versions or []:
        version_gid = str(version.get('gid', ''))
        version_url = str(version.get('url', ''))
        version_gid_node = _node_gid(version_gid)
        version_url_node = _node_url(version_url)
        _link_nodes(adjacency, version_gid_node, version_url_node)
        _link_nodes(adjacency, current_gid_node, version_gid_node)
        _link_nodes(adjacency, current_url_node, version_url_node)
        _link_nodes(adjacency, current_gid_node, version_url_node)
        _link_nodes(adjacency, current_url_node, version_gid_node)
    return index


def _has_relation_path(adjacency: Dict[str, List[str]], starts: List[str], targets: Set[str]) -> bool:
    queue = deque(node for node in starts if node in adjacency)
    visited = set(queue)
    while queue:
        node = queue.popleft()
        if node in targets:
            return True
        for nxt in adjacency.get(node, []):
            if nxt in visited:
                continue
            visited.add(nxt)
            queue.append(nxt)
    return False


def is_known_related(index: Optional[Dict[str, Any]], current_url: str, current_gid: str,
                     candidate_url: str, candidate_gid: str,
                     newer_versions: Optional[List[Dict[str, Any]]] = None) -> bool:
    candidate_gid = str(candidate_gid or extract_gid_from_url(candidate_url))
    candidate_url = str(candidate_url or '')

    for version in newer_versions or []:
        if candidate_gid and candidate_gid == str(version.get('gid', '')):
            return True
        if candidate_url and candidate_url == str(version.get('url', '')):
            return True

    index = ensure_reuse_index(index)
    adjacency = index['version_graph']['adjacency']
    starts = [node for node in (_node_gid(current_gid), _node_url(current_url)) if node]
    targets = {node for node in (_node_gid(candidate_gid), _node_url(candidate_url)) if node}
    if not starts or not targets:
        return False
    return _has_relation_path(adjacency, starts, targets)


def rebuild_title_indexes(index: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    index = ensure_reuse_index(index, rebuild_missing_titles=False)
    by_title_exact = {}
    by_title_series = {}

    for gid, entry in index.get('by_gid', {}).items():
        if not isinstance(entry, dict) or entry.get('source_type') != 'zip':
            continue
        source_path = entry.get('source_path')
        if not source_path:
            continue
        candidate = {
            'gid': str(gid),
            'url': str(entry.get('url', '')),
            'source_path': source_path,
            'title': str(entry.get('title', '')),
            'updated_at': int(entry.get('updated_at', 0)),
        }
        exact_key = normalize_title_exact(candidate['title'])
        series_key = normalize_title_series(candidate['title'])
        if exact_key:
            by_title_exact.setdefault(exact_key, []).append(dict(candidate))
        if series_key:
            by_title_series.setdefault(series_key, []).append(dict(candidate))

    for bucket in (by_title_exact, by_title_series):
        for key in list(bucket.keys()):
            entries = _sorted_zip_candidates(bucket[key])
            deduped = []
            seen = set()
            for entry in entries:
                dedup_key = (entry.get('gid'), entry.get('source_path'))
                if dedup_key in seen:
                    continue
                seen.add(dedup_key)
                deduped.append(entry)
            bucket[key] = deduped

    index['by_title_exact'] = by_title_exact
    index['by_title_series'] = by_title_series
    return index


def record_task_reuse(index: Optional[Dict[str, Any]], task: Any) -> Dict[str, Any]:
    index = ensure_reuse_index(index)
    gid = str(getattr(task, 'gid', ''))
    if not gid:
        return index

    newer_versions = task.meta.get('newer_versions', []) if getattr(task, 'meta', None) else []
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
    by_gid = index.setdefault('by_gid', {})
    by_hash = index.setdefault('by_page_hash', {})

    by_gid[gid] = {
        'gid': gid,
        'url': getattr(task, 'url', ''),
        'source_type': source_type,
        'source_path': source_path,
        'title': task.meta.get('title', ''),
        'updated_at': updated_at,
        'fid_page_hash_map': dict(task.fid_2_page_hash_map),
        'fid_fname_map': dict(task.fid_2_file_name_map),
        'fid_size_map': dict(task.fid_2_file_size_map),
    }

    for fid, page_hash in task.fid_2_page_hash_map.items():
        if not page_hash:
            continue
        file_name = task.fid_2_file_name_map.get(fid)
        if not file_name:
            continue

        entry = {
            'gid': gid,
            'fid': str(fid),
            'source_type': 'zip' if source_type == 'zip' else 'file',
            'source_path': source_path if source_type == 'zip' else os.path.join(source_path, file_name),
            'member_name': file_name if source_type == 'zip' else None,
            'size_text': task.fid_2_file_size_map.get(fid),
            'updated_at': updated_at,
        }

        entries = by_hash.setdefault(page_hash, [])
        dedup_key = (entry['gid'], entry['fid'], entry['source_path'])
        replaced = False
        for idx, existed in enumerate(entries):
            if (existed.get('gid'), existed.get('fid'), existed.get('source_path')) == dedup_key:
                entries[idx] = entry
                replaced = True
                break
        if not replaced:
            entries.append(entry)

    rebuild_title_indexes(index)
    return index
