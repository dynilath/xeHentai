#!/usr/bin/env python
# coding:utf-8
"""Cleanup script: remove older gallery versions when a newer version exists.

Iterates over all tasks in the SQLite DB using a streaming cursor — never loads
all payloads at once (safe for ~100K+ tasks).

Logic (per task):
  1. If the task has no ``newer_versions`` in its meta, skip.
  2. Find the latest version (largest gid) among ``newer_versions``.
  3. If the latest version does *not* have a corresponding task in the DB,
     log a message and skip this family.
  4. If it does exist, iterate over **all older versions** (the current task
     plus every entry in ``newer_versions`` except the latest):
     a. Check whether that version's task still exists in the DB; skip if not.
     b. Check whether a zip archive exists for that version; delete if found.
     c. Delete the task row from the DB.
     d. Log the action.
"""

import argparse
import json
import os
import sqlite3
import sys
import time
from typing import Any, Dict, List, Optional, Set, Tuple


# ---------------------------------------------------------------------------
# DB helpers (mirror patterns from xeHentai.session_store)
# ---------------------------------------------------------------------------

def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA synchronous=NORMAL')
    conn.execute('PRAGMA busy_timeout=5000')
    return conn


def _load_all_gids(db_path: str) -> Set[str]:
    """Return the set of every ``gid`` currently in the tasks table."""
    conn = _connect(db_path)
    try:
        rows = conn.execute('SELECT gid FROM tasks').fetchall()
    finally:
        conn.close()
    return {str(row['gid']) for row in rows}


def _iter_tasks_light(db_path: str):
    """Yield ``(guid, gid, payload_text)`` for every task, row by row."""
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            'SELECT guid, gid, payload FROM tasks ORDER BY guid'
        )
        for row in rows:
            yield (str(row['guid'] or ''), str(row['gid'] or ''), row['payload'] or '{}')
    finally:
        conn.close()


def _delete_task_by_gid(db_path: str, gid: str) -> bool:
    """Delete a task row by gid. Returns True if a row was deleted."""
    conn = _connect(db_path)
    try:
        with conn:
            # Clean FTS index before deleting the task row.
            conn.execute(
                'DELETE FROM tasks_fts WHERE rowid = (SELECT rowid FROM tasks WHERE gid = ?)',
                (str(gid),),
            )
            conn.execute('DELETE FROM task_tags WHERE guid = (SELECT guid FROM tasks WHERE gid = ?)', (str(gid),))
            cur = conn.execute('DELETE FROM tasks WHERE gid = ?', (str(gid),))
            return cur.rowcount > 0
    finally:
        conn.close()


def _load_task_by_gid(db_path: str, gid: str) -> Optional[Dict[str, Any]]:
    """Load the full payload + phase_state for a task by gid."""
    conn = _connect(db_path)
    try:
        row = conn.execute(
            'SELECT payload FROM tasks WHERE gid = ?', (str(gid),)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    try:
        return json.loads(row['payload'])
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Archive helpers (mirror xeHentai.task._get_gid_bucket_dir / _find_archive_by_gid)
# ---------------------------------------------------------------------------

def _gid_bucket_dir(config_dir: str, gid: str) -> Optional[str]:
    """Return the 3+3 bucket directory for a numeric gid under *config_dir*."""
    gid = str(gid)
    if not gid.isdigit():
        return None
    padded = gid.zfill(9)
    return os.path.join(config_dir, padded[:3], padded[3:6])


def _find_archive(config_dir: str, gid: str) -> Optional[str]:
    """Locate a ``<gid> - *.zip`` archive in the expected bucket directory."""
    bucket = _gid_bucket_dir(config_dir, gid)
    if not bucket or not os.path.isdir(bucket):
        return None
    prefix = '%s - ' % gid
    try:
        for name in os.listdir(bucket):
            if name.startswith(prefix) and name.endswith('.zip'):
                return os.path.join(bucket, name)
    except OSError:
        pass
    return None


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def _parse_newer_versions(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    meta = payload.get('meta')
    if not isinstance(meta, dict):
        return []
    nv = meta.get('newer_versions')
    return nv if isinstance(nv, list) else []


def cleanup(
    db_path: str = 'h.tasks.db',
    dry_run: bool = False,
    base_dir: str = '',
) -> Dict[str, int]:
    """Run the version-cleanup pass. Returns summary counters.

    *base_dir* overrides ``config.dir`` from individual task payloads when
    provided (useful when the payload field is empty or the dir structure has
    moved).
    """

    # Snapshot of all gids currently in the DB (kept in sync as we delete).
    gid_set = _load_all_gids(db_path)

    processed_gids: Set[str] = set()
    stats = {
        'scanned': 0,
        'no_newer_versions': 0,
        'latest_not_found': 0,
        'cleaned_tasks': 0,
        'cleaned_zips': 0,
    }

    for guid, gid, payload_text in _iter_tasks_light(db_path):
        stats['scanned'] += 1

        # Skip gids we already removed earlier in this run.
        if gid in processed_gids:
            continue

        try:
            payload = json.loads(payload_text)
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue

        newer_versions = _parse_newer_versions(payload)
        if not newer_versions:
            stats['no_newer_versions'] += 1
            continue

        # --- Find the latest version (largest gid) ---
        try:
            latest = max(newer_versions, key=lambda x: int(x.get('gid', 0)))
        except (ValueError, TypeError):
            stats['no_newer_versions'] += 1
            continue

        latest_gid = str(latest.get('gid', ''))
        if not latest_gid:
            stats['no_newer_versions'] += 1
            continue

        # --- Check that the latest version actually exists as a task ---
        if latest_gid not in gid_set:
            print(
                '[SKIP] gid=%s  title=%s  → latest gid=%s not found in DB'
                % (gid, _safe_title(payload), latest_gid)
            )
            stats['latest_not_found'] += 1
            continue

        # --- Collect all older gids to clean ---
        older_gids: List[str] = [gid]  # include self
        for nv in newer_versions:
            nv_gid = str(nv.get('gid', ''))
            if nv_gid and nv_gid != latest_gid:
                older_gids.append(nv_gid)

        # --- Determine config dir: explicit arg wins, then payload ---
        if base_dir:
            config_dir = base_dir
        else:
            config = payload.get('config')
            config_dir = ''
            if isinstance(config, dict):
                config_dir = str(config.get('dir', '') or '')

        # --- Clean up each older version ---
        for older_gid in older_gids:
            if older_gid in processed_gids:
                continue
            processed_gids.add(older_gid)

            if older_gid not in gid_set:
                print(
                    '[SKIP] older gid=%s (family of %s) → not in DB'
                    % (older_gid, gid)
                )
                continue

            # Resolve config dir: prefer the older task's own dir, fall back
            # to the current task's dir (they should be identical within a
            # family, but be safe).
            resolve_dir = config_dir
            if older_gid != gid:
                older_payload = _load_task_by_gid(db_path, older_gid)
                if older_payload:
                    older_config = older_payload.get('config')
                    if isinstance(older_config, dict):
                        od = str(older_config.get('dir', '') or '')
                        if od:
                            resolve_dir = od

            # Delete zip archive if present.
            if resolve_dir:
                zip_path = _find_archive(resolve_dir, older_gid)
                if zip_path:
                    if dry_run:
                        print('[DRY-RUN] Would delete zip: %s' % zip_path)
                    else:
                        try:
                            os.remove(zip_path)
                            print('[DEL ZIP] %s' % zip_path)
                            stats['cleaned_zips'] += 1
                        except OSError as exc:
                            print('[WARN] Cannot delete zip %s: %s' % (zip_path, exc))
                            # Continue with task deletion even if zip removal fails.

            # Delete task row.
            if dry_run:
                print('[DRY-RUN] Would delete task gid=%s' % older_gid)
            else:
                deleted = _delete_task_by_gid(db_path, older_gid)
                if deleted:
                    print('[DEL TASK] gid=%s' % older_gid)
                    gid_set.discard(older_gid)
                    stats['cleaned_tasks'] += 1
                else:
                    print('[WARN] Failed to delete task gid=%s' % older_gid)

        # Progress heartbeat every 5000 scanned tasks.
        if stats['scanned'] % 5000 == 0:
            print(
                '[PROGRESS] scanned=%d  cleaned=%d  skipped_no_nv=%d  skipped_no_latest=%d'
                % (
                    stats['scanned'],
                    stats['cleaned_tasks'],
                    stats['no_newer_versions'],
                    stats['latest_not_found'],
                )
            )

    return stats


def _safe_title(payload: Dict[str, Any]) -> str:
    meta = payload.get('meta')
    if isinstance(meta, dict):
        return str(meta.get('title', '') or '')[:60]
    return ''


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description='Clean up old gallery versions whose newer version already exists.'
    )
    parser.add_argument(
        '--db', default='h.tasks.db',
        help='Path to the SQLite tasks database (default: h.tasks.db)',
    )
    parser.add_argument(
        '--base-dir', default='',
        help='Base download directory (overrides config.dir from payload).',
    )
    parser.add_argument(
        '--dry-run', action='store_true',
        help='Simulate only; do not delete any files or DB rows.',
    )
    args = parser.parse_args()

    db_path = args.db
    if not os.path.exists(db_path):
        print('[ERROR] Database not found: %s' % db_path, file=sys.stderr)
        sys.exit(1)

    mode = 'DRY-RUN' if args.dry_run else 'LIVE'
    print('=== Gallery Version Cleanup (%s) ===' % mode)
    print('Database: %s' % os.path.abspath(db_path))
    print()

    started = time.time()
    stats = cleanup(db_path, dry_run=args.dry_run, base_dir=args.base_dir)
    elapsed = time.time() - started

    print()
    print('=== Summary ===')
    print('  Tasks scanned:               %d' % stats['scanned'])
    print('  No newer_versions (skipped): %d' % stats['no_newer_versions'])
    print('  Latest not found (skipped):  %d' % stats['latest_not_found'])
    print('  Zip archives deleted:        %d' % stats['cleaned_zips'])
    print('  Task rows deleted:           %d' % stats['cleaned_tasks'])
    print('  Elapsed:                     %.1f s' % elapsed)
    if args.dry_run:
        print('  (dry-run — nothing was actually changed)')


if __name__ == '__main__':
    main()
