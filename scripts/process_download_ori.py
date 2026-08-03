#!/usr/bin/env python
# coding:utf-8
"""
Fix archives where ``download_ori`` was incorrectly recorded as ``False``.

These archives contain original (un-resampled) image files whose internal
names do not follow the standard zero-padded fid naming (e.g. ``001.jpg``).
The archive metadata has a valid ``fid_page_hash_map``, which this script uses
to match every file by its content hash and rename it correctly.  After
rebuilding the zip the ``ArchiveMeta`` comment is updated with
``download_ori=True``.

Workflow:
1. Walk the download directory for zip archives named ``{gid} - {title}.zip``.
2. Parse the zip comment into ``ArchiveMeta``.
3. Skip archives that are already fid-named (nothing to fix).
4. Verify ``fid_page_hash_map`` exists and its length matches ``total``.
5. Hash every file in the zip, match against ``fid_page_hash_map``, and
   extract to a temp directory with the correct fid-based name.
6. Rebuild the zip with renamed files and ``download_ori=True`` in the comment.
"""

import argparse
import importlib.util
import json
import os
import re
import shutil
import sys
import traceback
import zipfile
from typing import Dict, List, Optional, Set, Tuple

# Allow running from anywhere: put the project root (parent of scripts/) on
# sys.path so `xeHentai` and `config` can be imported.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from xeHentai.const import __version__
from xeHentai.task import ArchiveMeta
from xeHentai.util.checkfile import buffer_hash, detect_image_ext_buffer


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

_RE_GID_PREFIX = re.compile(r"^\d+\s*-\s*")


def _default_search_root() -> str:
    """Read the download directory from ``config.py``, falling back to CWD."""
    config_path = os.path.join(_PROJECT_ROOT, "config.py")
    if os.path.isfile(config_path):
        try:
            spec = importlib.util.spec_from_file_location(
                "xehentai_user_config", config_path
            )
            if spec and spec.loader:
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                root = str(getattr(module, "dir", "") or "")
                if root:
                    return root
        except Exception:
            pass

    try:
        import config as user_config  # type: ignore[import-not-found,unused-ignore]

        root = str(getattr(user_config, "dir", "") or "")
        if root:
            return root
    except Exception:
        pass
    return os.getcwd()


def _find_all_archives(root_dir: str) -> List[str]:
    """Return every ``.zip`` whose basename starts with ``{digits} - ``."""
    if not os.path.isdir(root_dir):
        return []
    matched: List[str] = []
    for walk_root, _dirs, files in os.walk(root_dir):
        for file_name in files:
            if not file_name.lower().endswith(".zip"):
                continue
            if not _RE_GID_PREFIX.match(file_name):
                continue
            matched.append(os.path.join(walk_root, file_name))
    return sorted(matched)


def _get_gid_bucket_dir(root_dir: str, gid: str) -> str:
    """Return the ``3+3`` bucket directory for a numeric gallery id."""
    gid_padded = str(gid).zfill(9)
    return os.path.join(root_dir, gid_padded[:3], gid_padded[3:6])


def _find_archives_by_gid(root_dir: str, gid: str) -> List[str]:
    """Locate zip archives under *root_dir* whose basename starts with *gid*."""
    bucket_dir = _get_gid_bucket_dir(root_dir, gid)
    if not os.path.isdir(bucket_dir):
        return []

    prefix = "%s - " % gid
    matched: List[str] = []
    for name in os.listdir(bucket_dir):
        full_path = os.path.join(bucket_dir, name)
        if not os.path.isfile(full_path):
            continue
        if not name.startswith(prefix):
            continue
        if not name.lower().endswith(".zip"):
            continue
        matched.append(full_path)
    return sorted(matched)


def _resolve_targets(
    root_dir: str,
    gids: Optional[List[str]] = None,
    paths: Optional[List[str]] = None,
) -> List[str]:
    """Resolve gid / path filters into a deduplicated list of archive paths."""
    if gids:
        results: List[str] = []
        for gid in gids:
            results.extend(_find_archives_by_gid(root_dir, gid))
        return sorted(set(results))

    if paths:
        results = []
        for p in paths:
            abs_p = os.path.abspath(p)
            if os.path.isfile(abs_p) and abs_p.lower().endswith(".zip"):
                results.append(abs_p)
        return sorted(set(results))

    return _find_all_archives(root_dir)


def _build_saving_file_name(total: int, fid: str, ext: str) -> str:
    """Build a zero-padded fid filename (e.g. ``001.jpg``)."""
    return f"{fid.zfill(len(str(total)))}{ext}"


def _encode_archive_comment(meta: ArchiveMeta) -> bytes:
    """Encode *meta* as a zip comment string (same format as ``Task.encode_meta``)."""
    payload = json.dumps(meta.to_dict())
    return ("xeHentai Archiver v%s\n%s" % (__version__, payload)).encode("UTF-8")


# ---------------------------------------------------------------------------
# per-archive processing
# ---------------------------------------------------------------------------

class ArchiveSkipReason:
    """Reasons an archive may be skipped (not an error, just not applicable)."""

    BAD_COMMENT = "unparseable zip comment"
    FILE_COUNT_MISMATCH = "zip file count does not match meta.total"
    ALREADY_FID_NAMED = "all files are already fid-named"
    NO_HASH_MAP = "fid_page_hash_map missing or empty"
    HASH_MAP_LENGTH_MISMATCH = "fid_page_hash_map length != total"


def _check_fid_named(total: int, members: List[str]) -> bool:
    """Return ``True`` when **every** member name is already fid-based.

    A fid-based name looks like ``001.jpg``, ``042.png`` — the stem is a
    zero-padded integer and the suffix is an image extension.
    """
    digit_width = len(str(total))
    image_exts = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"}
    for member in members:
        stem, ext = os.path.splitext(member)
        if len(stem) != digit_width:
            return False
        if not stem.isdigit():
            return False
        if ext.lower() not in image_exts:
            return False
    return True


def process_archive(
    zip_path: str,
    *,
    dry_run: bool = False,
    verbose: bool = False,
) -> Tuple[bool, str]:
    """Fix a single archive if it qualifies.  Returns ``(changed, message)``.

    *changed* is ``True`` when the archive was actually rewritten.
    """

    def _log(msg: str) -> None:
        if verbose:
            print("  [%s] %s" % (os.path.basename(zip_path), msg))

    # ---- 1. open & parse comment ------------------------------------------
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            comment_str = zf.comment.decode("UTF-8", errors="ignore")
            meta = ArchiveMeta.decode_meta(comment_str)
    except (OSError, zipfile.BadZipFile) as exc:
        return False, "%s: %s" % (ArchiveSkipReason.BAD_COMMENT, exc)

    if meta is None:
        return False, ArchiveSkipReason.BAD_COMMENT

    # ---- 2. enumerate zip members -----------------------------------------
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            all_names = zf.namelist()
            file_members = [n for n in all_names if not n.endswith("/")]
    except (OSError, zipfile.BadZipFile) as exc:
        return False, "%s (re-read): %s" % (ArchiveSkipReason.BAD_COMMENT, exc)

    # ---- 3. file count vs total -------------------------------------------
    if len(file_members) != meta.total:
        return False, "%s (zip=%d, meta=%d)" % (
            ArchiveSkipReason.FILE_COUNT_MISMATCH,
            len(file_members),
            meta.total,
        )

    # ---- 4. already fid-named? --------------------------------------------
    if _check_fid_named(meta.total, file_members):
        return False, ArchiveSkipReason.ALREADY_FID_NAMED

    # ---- 5. fid_page_hash_map sanity --------------------------------------
    fphm = meta.fid_page_hash_map
    if not fphm:
        return False, ArchiveSkipReason.NO_HASH_MAP

    if len(fphm) != meta.total:
        return False, "%s (hash_map=%d, total=%d)" % (
            ArchiveSkipReason.HASH_MAP_LENGTH_MISMATCH,
            len(fphm),
            meta.total,
        )

    _log(
        "candidate: total=%d, files=%d, hash_map=%d"
        % (meta.total, len(file_members), len(fphm))
    )

    if dry_run:
        return False, "dry-run: would process"

    # ---- 6. hash every member & build fid → (member_name, ext) map --------
    # Reverse map: page_hash → fid
    hash_to_fid: Dict[str, str] = {ph: fid for fid, ph in fphm.items()}

    fid_member_map: Dict[str, Tuple[str, str]] = {}  # fid → (member_name, ext)
    unmatched: List[str] = []

    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            for member in file_members:
                with zf.open(member, "r") as src:
                    ph = buffer_hash(src)
                fid = hash_to_fid.get(ph)
                if fid is None:
                    unmatched.append("%s (hash=%s)" % (member, ph))
                    continue

                # Re-read to detect extension
                with zf.open(member, "r") as src:
                    ext = detect_image_ext_buffer(src)
                if ext is None:
                    # Fall back to original extension
                    ext = os.path.splitext(member)[1].lower() or ".jpg"

                fid_member_map[fid] = (member, ext)
    except (OSError, zipfile.BadZipFile) as exc:
        return False, "failed to hash zip members: %s" % exc

    if unmatched:
        _log("WARNING: %d file(s) could not be matched to any fid:" % len(unmatched))
        for item in unmatched:
            _log("  - %s" % item)
        return False, "hash matching incomplete (%d unmatched)" % len(unmatched)

    _log("matched all %d files to fids" % len(fid_member_map))

    # ---- 7. extract to temp dir with fid-based names ----------------------
    tmp_dir = "%s_ori_fix_tmp" % zip_path
    if os.path.exists(tmp_dir):
        shutil.rmtree(tmp_dir)
    os.makedirs(tmp_dir)

    written: Set[str] = set()
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            for fid, (member_name, ext) in fid_member_map.items():
                dst_name = _build_saving_file_name(meta.total, fid, ext)
                dst_path = os.path.join(tmp_dir, dst_name)
                with zf.open(member_name, "r") as src, open(
                    dst_path, "wb"
                ) as dst:
                    shutil.copyfileobj(src, dst)
                written.add(dst_name)
    except Exception as exc:
        shutil.rmtree(tmp_dir)
        return False, "extraction failed: %s" % exc

    # ---- 8. rebuild zip ---------------------------------------------------
    # Preserve original metadata; set download_ori=True explicitly.
    meta_dict = meta.to_dict()
    meta_dict["download_ori"] = True
    meta_dict["fid_page_hash_map"] = fphm

    # Re-encode fresh to get version header
    new_comment = _encode_archive_comment(ArchiveMeta.from_dict(meta_dict))

    backup_path = "%s.bak" % zip_path
    if os.path.exists(backup_path):
        os.remove(backup_path)

    try:
        os.rename(zip_path, backup_path)

        with zipfile.ZipFile(zip_path, "w") as new_zf:
            new_zf.comment = new_comment
            for dst_name in sorted(os.listdir(tmp_dir)):
                full = os.path.join(tmp_dir, dst_name)
                new_zf.write(full, dst_name, zipfile.ZIP_STORED)
    except Exception:
        # Restore original on failure
        if os.path.exists(zip_path):
            os.remove(zip_path)
        if os.path.exists(backup_path):
            os.rename(backup_path, zip_path)
        shutil.rmtree(tmp_dir)
        return False, "zip rebuild failed: %s" % traceback.format_exc()

    # ---- 9. cleanup ------------------------------------------------------
    if os.path.exists(backup_path):
        os.remove(backup_path)
    shutil.rmtree(tmp_dir)

    return True, "rebuilt with %d fid-named files" % len(written)


# ---------------------------------------------------------------------------
# scan / url-only modes (merged from the former gather_old_meta.py)
# ---------------------------------------------------------------------------


def _extract_url(zip_path: str) -> Optional[str]:
    """Parse the zip comment and return ``meta.url``, or *None* on failure."""
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            comment_str = zf.comment.decode("UTF-8", errors="ignore")
            meta = ArchiveMeta.decode_meta(comment_str)
    except (OSError, zipfile.BadZipFile):
        return None
    if meta is None:
        return None
    return meta.url or ""


def _archive_issue_url(zip_path: str) -> Optional[str]:
    """Return the archive ``url`` if it looks out of sync, or ``None``.

    An archive is considered out of sync when:
    - ``meta.total`` != number of non-directory members in the zip.
    - ``fid_page_hash_map`` is missing or empty.

    *None* is returned for parse failures and for archives that pass all
    checks (the caller should stay silent in both cases).
    """
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            comment_str = zf.comment.decode("UTF-8", errors="ignore")
            meta = ArchiveMeta.decode_meta(comment_str)
            file_count = sum(1 for n in zf.namelist() if not n.endswith("/"))
    except (OSError, zipfile.BadZipFile):
        return None

    if meta is None:
        return None

    count_mismatch = file_count != meta.total
    no_hash_map = not meta.fid_page_hash_map
    if count_mismatch or no_hash_map:
        return meta.url or ""
    return None


def _cmd_url_only(args: argparse.Namespace) -> int:
    """Print the comment url of every parseable archive (legacy --url-only)."""
    missing_url = 0
    for zip_path in _find_all_archives(os.path.abspath(args.root)):
        url = _extract_url(zip_path)
        if url is None:
            continue
        if url:
            print(url)
        else:
            missing_url += 1
            print("NO_URL: %s" % zip_path, file=sys.stderr)
    if missing_url:
        print(
            "\n%d archive(s) had no url in metadata (see stderr above)." % missing_url,
            file=sys.stderr,
        )
    return 0


def _cmd_scan_only(args: argparse.Namespace) -> int:
    """Print the url of every out-of-sync archive (legacy default mode)."""
    missing_url = 0
    for zip_path in _find_all_archives(os.path.abspath(args.root)):
        result = _archive_issue_url(zip_path)
        if result is None:
            continue
        if result:
            print(result)
        else:
            missing_url += 1
            print("NO_URL: %s" % zip_path, file=sys.stderr)
    if missing_url:
        print(
            "\n%d archive(s) had no url in metadata (see stderr above)." % missing_url,
            file=sys.stderr,
        )
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def cmd_fix(args: argparse.Namespace) -> int:
    """Main entry point for the ``fix`` subcommand."""
    root_dir = os.path.abspath(args.root)
    if not os.path.isdir(root_dir):
        print("ERROR: search root does not exist: %s" % root_dir)
        return 2

    # Scan-only / url-only modes (legacy gather_old_meta.py behaviour).
    if args.url_only:
        return _cmd_url_only(args)
    if args.scan_only:
        return _cmd_scan_only(args)

    gids: Optional[List[str]] = args.gid or None
    paths: Optional[List[str]] = args.path or None

    targets = _resolve_targets(root_dir, gids=gids, paths=paths)

    if not targets:
        print("No matching archives found under %s" % root_dir)
        return 0

    print("Found %d archive(s) to examine." % len(targets))
    if args.dry_run:
        print("[dry-run mode — no archives will be modified]\n")

    processed = 0
    skipped = 0
    failed = 0

    for zip_path in targets:
        changed, message = process_archive(
            zip_path,
            dry_run=args.dry_run,
            verbose=True,
        )

        if changed:
            processed += 1
            print("[FIXED] %s  (%s)" % (zip_path, message))
        elif message.startswith("dry-run"):
            print("[DRY-RUN] %s" % zip_path)
        elif any(
            message.startswith(s)
            for s in (
                ArchiveSkipReason.BAD_COMMENT,
                ArchiveSkipReason.FILE_COUNT_MISMATCH,
                ArchiveSkipReason.ALREADY_FID_NAMED,
                ArchiveSkipReason.NO_HASH_MAP,
                ArchiveSkipReason.HASH_MAP_LENGTH_MISMATCH,
            )
        ):
            skipped += 1
            quiet = (
                args.quiet
                and message.startswith(ArchiveSkipReason.ALREADY_FID_NAMED)
            )
            if not quiet:
                print("[SKIP] %s  (%s)" % (zip_path, message))
        else:
            failed += 1
            print("[FAIL] %s  (%s)" % (zip_path, message))

    print(
        "\nDone: %d processed, %d skipped, %d failed (of %d total)."
        % (processed, skipped, failed, len(targets))
    )
    return 0 if failed == 0 else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fix xeHentai archives where download_ori was incorrectly "
        "set to False by renaming internal files to fid-based names "
        "using fid_page_hash_map and writing download_ori=True.",
    )
    parser.add_argument(
        "--root",
        default=_default_search_root(),
        help="download root directory (default from config.py, or CWD)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="scan and report without modifying any archives",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="suppress [SKIP] messages for archives that are already fid-named (normal archives)",
    )

    scan_group = parser.add_mutually_exclusive_group()
    scan_group.add_argument(
        "--scan-only",
        action="store_true",
        help="do not fix anything; print the url of every out-of-sync archive "
        "(total/file-count mismatch or missing fid_page_hash_map)",
    )
    scan_group.add_argument(
        "--url-only",
        action="store_true",
        help="do not fix anything; only parse each zip comment and print its url "
        "(skip all sync checks)",
    )

    target_group = parser.add_mutually_exclusive_group()
    target_group.add_argument(
        "--gid",
        nargs="+",
        metavar="GID",
        help="process only archives matching these gallery IDs",
    )
    target_group.add_argument(
        "--path",
        nargs="+",
        metavar="ZIP",
        help="process only these specific zip file paths",
    )
    return parser


def main(argv: List[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    # Keep scan/url-only output clean (pure url lines, as the legacy
    # gather_old_meta.py printed).
    if not (args.url_only or args.scan_only):
        print("archive_root: %s" % os.path.abspath(str(args.root)))
    return cmd_fix(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
