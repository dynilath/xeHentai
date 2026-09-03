#!/usr/bin/env python
# coding:utf-8
# Contributor:
#      fffonion        <fffonion@gmail.com>

import os
import re
import json
import hashlib
import traceback
import uuid
import shutil
import zipfile
from typing import Any, Dict, List, Literal, Optional, Set, Tuple
from threading import RLock
from dataclasses import dataclass, asdict, field

import requests
from .util.checkfile import buffer_hash, check_file
from .util.logger import Logger
from . import util
from . import reuse_index
from .task_config import CoreConfig, TaskConfig
from .const import *
from .const import __version__
from queue import Queue

from .util import checkfile


@dataclass
class GalleryMeta:
    title_japanese: str = ""
    title_primary: str = ""
    title: str = ""
    total: int = 0
    thumbnail_cnt: int = 0
    has_ori: bool = False
    tags: List[Any] = field(default_factory=list)
    newer_versions: List[Dict[str, Any]] = field(default_factory=list)
    filelist: Dict[str, Any] = field(default_factory=dict)
    resampled: Dict[str, Any] = field(default_factory=dict)
    sample_hash: List[str] = field(default_factory=list)
    extra: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "GalleryMeta":
        meta = cls()
        meta.update_from_dict(data or {})
        return meta

    def to_dict(self) -> Dict[str, Any]:
        payload = {
            "title_japanese": self.title_japanese,
            "title_primary": self.title_primary,
            "title": self.title,
            "total": self.total,
            "thumbnail_cnt": self.thumbnail_cnt,
            "has_ori": self.has_ori,
            "tags": list(self.tags),
            "newer_versions": [dict(item) for item in self.newer_versions],
            "filelist": dict(self.filelist),
            "resampled": dict(self.resampled),
            "sample_hash": list(self.sample_hash),
            "gjname": self.title_japanese,
            "gnname": self.title_primary,
        }
        payload.update(self.extra)
        return payload

    def update_from_dict(self, data: Dict[str, Any]) -> None:
        self.title_japanese = str(
            data.get("title_japanese", data.get("gjname", self.title_japanese)) or ""
        )
        self.title_primary = str(
            data.get("title_primary", data.get("gnname", self.title_primary)) or ""
        )
        self.title = str(data.get("title", self.title) or "")
        self.total = int(data.get("total", self.total) or 0)
        self.thumbnail_cnt = int(data.get("thumbnail_cnt", self.thumbnail_cnt) or 0)
        self.has_ori = bool(data.get("has_ori", self.has_ori))
        self.tags = list(data.get("tags", self.tags) or [])
        self.newer_versions = [
            dict(item)
            for item in (data.get("newer_versions", self.newer_versions) or [])
        ]
        self.filelist = dict(data.get("filelist", self.filelist) or {})
        self.resampled = dict(data.get("resampled", self.resampled) or {})
        self.sample_hash = list(data.get("sample_hash", self.sample_hash) or [])

        known_keys = {
            "title_japanese",
            "title_primary",
            "gjname",
            "gnname",
            "title",
            "total",
            "thumbnail_cnt",
            "has_ori",
            "tags",
            "newer_versions",
            "filelist",
            "resampled",
            "sample_hash",
        }
        for key, value in data.items():
            if key not in known_keys:
                self.extra[key] = value

    def select_display_title(self, use_japanese_title: bool) -> None:
        if use_japanese_title and self.title_japanese:
            self.title = self.title_japanese
        else:
            self.title = self.title_primary


@dataclass
class ArchiveMeta:
    """Type-safe archive metadata with validation"""

    title_japanese: str
    title_primary: str
    tags: Any
    total: int
    title: str
    download_ori: bool
    url: str
    fid_page_hash_map: Optional[Dict[str, str]] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ArchiveMeta":
        """Create ArchiveMeta from dictionary with validation"""
        required_fields = {"tags", "total", "title", "download_ori", "url"}
        missing = required_fields - set(data.keys())
        if missing:
            raise ValueError(f"Missing required archive metadata fields: {missing}")

        try:
            fid_page_hash_map = data.get("fid_page_hash_map")
            if fid_page_hash_map is not None:
                fid_page_hash_map = dict(fid_page_hash_map)

            title_japanese = str(data.get("title_japanese", data.get("gjname", "")))
            title_primary = str(data.get("title_primary", data.get("gnname", "")))

            return cls(
                title_japanese=title_japanese,
                title_primary=title_primary,
                tags=data["tags"],
                total=int(data["total"]),
                title=str(data["title"]),
                download_ori=bool(data["download_ori"]),
                url=str(data["url"]),
                fid_page_hash_map=fid_page_hash_map,
            )
        except (TypeError, ValueError) as e:
            raise ValueError(f"Invalid archive metadata types: {e}")

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization with legacy aliases."""
        data = asdict(self)
        # Legacy field aliases for backward compatibility.
        data["gjname"] = self.title_japanese
        data["gnname"] = self.title_primary
        return data

    @staticmethod
    def decode_meta(comment_str: str) -> Optional[ArchiveMeta]:
        """Decode and validate metadata from zip file comment"""
        lbrace = comment_str.find("{")
        rbrace = comment_str.rfind("}")
        if lbrace == -1 or rbrace == -1 or lbrace > rbrace:
            return None

        meta_str = comment_str[lbrace : rbrace + 1]
        if not meta_str:
            return None

        try:
            data = json.loads(meta_str)
            return ArchiveMeta.from_dict(data)
        except (json.JSONDecodeError, ValueError, KeyError, TypeError):
            return None


@dataclass
class DumplicatedFileInfo:
    fid: str
    existed_fid: str
    file_name: str
    existed_file_name: str

    def to_dict(self) -> Dict[str, str]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DumplicatedFileInfo":
        return cls(
            fid=str(data.get("fid", "")),
            existed_fid=str(data.get("existed_fid", "")),
            file_name=str(data.get("file_name", "")),
            existed_file_name=str(data.get("existed_file_name", "")),
        )


@dataclass
class TaskReuseSource:
    archive_path: str
    member_name: str
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TaskReuseSource":
        return cls(
            archive_path=str(data.get("archive_path", "")),
            member_name=str(data.get("member_name", "")),
        )
    
    def to_dict(self) -> Dict[str, str]:
        return asdict(self)


@dataclass
class TaskReuseResources:
    """Task reuse resources.

    Attributes:
        page_hash_file_map: A mapping from page hash to file path.
        pending_archives: A list of pending archive reuse candidates that require page hash crawling.
    """

    page_hash_source_map: Dict[str, TaskReuseSource] = field(default_factory=dict)
    pending_archives: List[reuse_index.ArchiveReuseCandidate] = field(
        default_factory=list
    )

    def reset(self) -> None:
        self.page_hash_source_map.clear()
        self.pending_archives.clear()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "page_hash_source_map": {k: v.to_dict() for k, v in self.page_hash_source_map.items()},
            "pending_archives": [item.to_dict() for item in self.pending_archives],
        }

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "TaskReuseResources":
        payload = data if isinstance(data, dict) else {}
        return cls(
            page_hash_source_map={
                str(page_hash): TaskReuseSource.from_dict(source)
                for page_hash, source in dict(
                    payload.get("page_hash_source_map", {}) or {}
                ).items()
                if page_hash and isinstance(source, dict)
            },
            pending_archives=[
                reuse_index.ArchiveReuseCandidate.from_dict(item)
                for item in list(payload.get("pending_archives", []) or [])
                if isinstance(item, dict)
            ],
        )
        
    def use_archive_with_page_hash_map(self, candidate: reuse_index.ArchiveReuseCandidate, fid_page_hash_map: Dict[str, str]) -> None:
        """Register page hash sources from an archive candidate with fid_page_hash_map."""
        page_hash_source_map: Dict[str, TaskReuseSource] = {}
        with zipfile.ZipFile(candidate.archive_path, "r") as zf:
            for member in zf.namelist():
                sem, ext = os.path.splitext(member)
                if not sem.isdigit():
                    continue
                fid = str(int(sem))
                if fid in fid_page_hash_map:
                    page_hash = fid_page_hash_map[fid]
                    page_hash_source_map[page_hash] = TaskReuseSource(
                        archive_path=candidate.archive_path,
                        member_name=member
                    )
        self.register_page_hash_source(page_hash_source_map)

    def register_page_hash_source(self, hash_to_source: Dict[str, TaskReuseSource]) -> None:
        self.page_hash_source_map.update(hash_to_source)

    def add_pending_archive(
        self, archive_info: reuse_index.ArchiveReuseCandidate
    ) -> None:
        self.pending_archives.append(archive_info)


    def _materialize_reuse_file(
        self,
        target_dir: str,
        page_hash: str,
        total: int,
        fid: str,
    ) -> Optional[str]:
        """Copy a matched reuse file into the current fid slot before page queue build.
        Returns:
            The target file path if materialization is done, otherwise None.
        """
        source_path = self.page_hash_source_map.get(page_hash)
        if not source_path:
            return None
        
        if not os.path.exists(source_path.archive_path):
            return None
        
        if not os.path.exists(target_dir):
            os.makedirs(target_dir)
        
        with zipfile.ZipFile(source_path.archive_path, "r") as zf:
            info = zf.NameToInfo.get(source_path.member_name)
            if not info:
                return None
            
            with zf.open(info, "r") as src:
                ext = checkfile.detect_image_ext_buffer(src)
                
            if not ext:
                return None
                
            target_fname = Task._build_saving_file_name(total, fid, ext)
            target_path = os.path.join(target_dir, target_fname)
            
            with zf.open(info, "r") as src, open(target_path, "wb+") as dst:
                shutil.copyfileobj(src, dst)
        
            return target_path

    @staticmethod
    def _build_archive_resource(
        archive_path: str,
    ) -> Tuple[Optional[ArchiveMeta], Dict[str, TaskReuseSource]]:
        """Extract a candidate archive into the task directory and map fid to local file.
        Arguments:
            candidate_gid: The gid of the candidate archive
            archive_path: The file path to the candidate archive zip.
        Returns:
            Tuple of (metadata, page hash to reuse source map)
        """
        with zipfile.ZipFile(archive_path, "r") as zf:
            comment_str = zf.comment.decode("UTF-8", errors="ignore")
            metadata = ArchiveMeta.decode_meta(comment_str)
            if metadata is None:
                return None, {}

            fid_page_hash = metadata.fid_page_hash_map or {}

            page_hash_to_reuse_source: Dict[str, TaskReuseSource] = {}
            for member in zf.namelist():
                if member.endswith("/"):
                    continue

                basename = os.path.basename(member)
                if not basename:
                    continue
                
                stem, _ = os.path.splitext(basename)
                fid = str(int(stem)) if stem.isdigit() else None
                    
                if fid in fid_page_hash:
                    page_hash_to_reuse_source[fid_page_hash[fid]] = TaskReuseSource(
                        archive_path=archive_path, member_name=member
                    )
                elif metadata.download_ori:
                    with zf.open(member, "r") as src:
                        hash = buffer_hash(src)
                        page_hash_to_reuse_source[hash] = TaskReuseSource(
                            archive_path=archive_path, member_name=member
                        )

            return metadata, page_hash_to_reuse_source


class Task(object):

    def __init__(
        self, url: str, cfgdict: Dict[str, Any], logger: Logger, core_config=None
    ):

        self._logger = logger

        # Original gallery URL.
        self.url: str = url

        if not url:
            raise ValueError("Task URL cannot be empty")
        _ = RE_INDEX.findall(url)
        if not _:
            raise ValueError(f"Invalid task URL format: {url}")
        self.gid: str = _[0][0]
        self.sethash: str = _[0][1]

        # Current lifecycle state (TASK_STATE_*).
        # Negative values encode failure reasons (TASK_STATE_ERR_*).
        self.state: int = TASK_STATE_WAITING
        # Short runtime task identifier.
        self.guid: str = str(uuid.uuid4())[:8]
        # Task-level config view with fallback to the core config.
        self.config = (
            core_config.create_task_config(cfgdict)
            if core_config
            else TaskConfig(cfgdict)
        )
        # Parsed gallery metadata payload.
        self.meta: GalleryMeta = GalleryMeta()

        # Maps image URL to [reload URL, saving file name].
        self.reload_map: Dict[str, Tuple[str, str]] = {}

        # map fid to 10-char page hash, extracted from /s/<hash>/<gid>-<fid>
        self.fid_2_page_hash_map: Dict[str, str] = {}

        # map fid to 10-char image hash
        # this will help to check file existence before page scan
        # especially when the task is restarted
        # this map should not be in archive meta
        self.fid_2_img_hash_map: Dict[str, str] = {}

        # map fid to file name (without path)
        # just like fid_2_img_hash_map, this will help when the task is restarted
        # and we can check file existence by name before page scan
        # Also, this map is used in making archive for collecting files
        # this map should not be in archive meta too
        self.fid_2_file_name_map: Dict[str, str] = {}

        # map dumplicated page hash (original file hash) to list of file info
        # when the download is done, copy the file to those dumplicated
        # thus we don't need to download the same file twice
        self.dumplicated_file_map: Dict[str, List[DumplicatedFileInfo]] = {}

        # shared, process-level reuse index injected by core
        self._reuse_index: Optional[reuse_index.ReuseIndexHandle] = None

        # task-local reuse cache built after SCAN_PAGE
        self.reuse: TaskReuseResources = TaskReuseResources()

        # and, the fid in these map will all be str
        # when int key dumps into files by python, it is somehow transformed into str
        # and an error would occur when you load it again

        # Single-image page queue.
        self.page_q: Optional[Queue[str]] = None
        # Finished image IDs (rebuilt on scan, not persisted directly).
        self._flist_done: Set[int] = set()

        # Lock for counters/state transitions.
        self._cnt_lock: RLock = RLock()

    @property
    def logger(self) -> Logger:
        return self._logger

    def cleanup(self, before_delete=False):
        if before_delete:
            if (
                "delete_task_files" in self.config
                and self.config["delete_task_files"]
                and self.meta.title
            ):  # maybe it's a error task and meta is empty
                fpath = self.get_task_dir()
                # TODO: ascii can't decode? locale not enus, also check save_file
                if os.path.exists(fpath):
                    shutil.rmtree(fpath)
                zippath = "%s.zip" % fpath
                if os.path.exists(zippath):
                    os.remove(zippath)
        elif self.state == TASK_STATE_FINISHED or self.state < 0:
            self.page_q = None
            self.reload_map = {}
            # if 'filelist' in self.meta:
            #     del self.meta['filelist']
            # if 'resampled' in self.meta:
            #     del self.meta['resampled']

    def set_phase_state(self, phase_state: int) -> None:
        self.state = phase_state

    def set_fail(self, task_state):
        self.set_phase_state(task_state)
        # cleanup all we cached
        self.meta = GalleryMeta()

    def migrate_exhentai(self):
        _ = re.findall(RE_STR_EHENTAI_MIGRATE, self.url)
        if not _:
            return False
        self.url = HOST_EXHENTAI + _[0]
        if self.state < 0:
            self.set_phase_state(TASK_STATE_WAITING)
        return True

    # write some metadata into zip file
    def encode_meta(self) -> bytes:
        """Encode task metadata for zip file comment"""
        archive_meta = ArchiveMeta(
            title_japanese=self.meta.title_japanese,
            title_primary=self.meta.title_primary,
            tags=self.meta.tags,
            total=self.meta.total,
            title=self.meta.title,
            download_ori=self.config["download_ori"],
            url=self.url,
            fid_page_hash_map=self.fid_2_page_hash_map,
        )
        json_zip_meta = json.dumps(archive_meta.to_dict())
        return ("xeHentai Archiver v%s\n%s" % (__version__, json_zip_meta)).encode(
            "UTF-8"
        )

    def update_meta(self, meta: Dict[str, Any]) -> None:
        """Update metadata with type validation for critical fields"""
        self.meta.update_from_dict(meta)
        self.meta.select_display_title(bool(self.config.get("jpn_title")))

    def base_url(self):
        return re.findall(RESTR_SITE, self.url)[0]

    def set_fid_done(self, fid: str):
        with self._cnt_lock:
            self._flist_done.add(int(fid))

    @staticmethod
    def _build_saving_file_name(total: int, fid: str, ext: str):
        return f"{fid.zfill(len(str(total)))}{ext}"

    def _zero_pad_fid(self, fid: str) -> str:
        """Return the fid zero-padded to the width of meta.total (e.g. 1 -> "01")."""
        width = len(str(self.meta.total)) if self.meta.total else 0
        return str(fid).zfill(width) if width else str(fid)

    def get_fid_filename(self, fid: str) -> Optional[str]:
        """Resolve the saved file name (with extension) for a fid.

        Files are stored zero-padded by fid (see ``_build_saving_file_name``),
        but the extension is only known at download time, so resolution is:

          1. ``fid_2_file_name_map`` — populated during download / hydration.
             For active tasks this always hits.
          2. The task download folder ``<task_dir>/<padded_fid>.*`` — used while
             a task is still downloading (folder exists, files on disk).
          3. The finished archive ``<task_dir>.zip`` — a member whose stem is
             ``padded_fid``. Finished tasks have their folder removed by
             ``make_archive`` and ``fid_2_file_name_map`` cleared by
             ``cleanup_download_info``, so this is the authoritative source for
             cold finished tasks.

        Lookups from (2)/(3) are cached into ``fid_2_file_name_map`` so a range
        scan (e.g. RPC ``get_image``) is cheap and only walks the disk/zip once
        per fid. Returns None if the fid cannot be resolved.
        """
        fid = str(fid)
        cached = self.fid_2_file_name_map.get(fid)
        if cached:
            return cached

        padded = self._zero_pad_fid(fid)
        task_dir = self.get_task_dir()

        # 2. Download folder (task still downloading, files on disk).
        if os.path.isdir(task_dir):
            for name in os.listdir(task_dir):
                stem, _ = os.path.splitext(name)
                if stem == padded:
                    self.fid_2_file_name_map[fid] = name
                    return name

        # 3. Finished archive zip.
        zip_path = "%s.zip" % task_dir
        if os.path.isfile(zip_path):
            try:
                with zipfile.ZipFile(zip_path, "r") as zf:
                    for member in zf.namelist():
                        if member.endswith("/"):
                            continue
                        stem, _ = os.path.splitext(os.path.basename(member))
                        if stem == padded:
                            self.fid_2_file_name_map[fid] = member
                            return member
            except (OSError, zipfile.BadZipFile):
                pass

        return None

    def _content_type_to_ext(self, content_type):
        """Map HTTP content type to file extension, result contains leading dot."""
        content_type = (content_type or "").strip().lower()
        content_type_map = {
            "image/jpeg": ".jpg",
            "image/jpg": ".jpg",
            "image/png": ".png",
            "image/gif": ".gif",
            "image/bmp": ".bmp",
            "image/webp": ".webp",
        }
        return content_type_map.get(content_type)

    def set_file_dumplicated(
        self,
        fhash: str,
        this_fid: str,
        existed_fid: str,
        real_file_name: str,
        existed_file_name: str,
    ):
        if fhash not in self.dumplicated_file_map:
            self.dumplicated_file_map[fhash] = []
        self.dumplicated_file_map[fhash].append(
            DumplicatedFileInfo(
                fid=this_fid,
                existed_fid=existed_fid,
                file_name=real_file_name,
                existed_file_name=existed_file_name,
            )
        )

    def _get_gid_bucket_dir(self, gid: str) -> Optional[str]:
        """Return the 3+3 bucket directory path for a numeric gallery id."""
        gid = str(gid)
        if not gid.isdigit():
            return None
        gid_padded = gid.zfill(9)
        return os.path.join(self.config["dir"], gid_padded[:3], gid_padded[3:6])

    def _find_archive_by_gid(self, gid: str) -> Optional[str]:
        """Locate a gallery archive zip by gid under the current bucketed layout."""
        bucket_dir = self._get_gid_bucket_dir(gid)
        if not bucket_dir or not os.path.isdir(bucket_dir):
            return None

        prefix = "%s - " % gid
        for name in os.listdir(bucket_dir):
            if name.startswith(prefix) and name.endswith(".zip"):
                return os.path.join(bucket_dir, name)
        return None

    def _file_sha1_prefix(self, file_path: str, length: int = 10) -> str:
        with open(file_path, "rb") as handle:
            digest = hashlib.sha1()
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest()[:length]

    def prepare_reuse_files(self) -> Tuple[Set[str], int, int]:
        """Build a task-local page_hash -> local file map from candidate archives."""
        self.reuse.reset()

        if self._reuse_index is None:
            raise RuntimeError("Reuse index not available for task %s" % self.guid)

        candidates = reuse_index.collect_archive_reuse_candidates(
            self._reuse_index,
            self.meta.title,
            self.gid,
        )

        archives_considered = set()
        for candidate in candidates:
            if candidate.gid == str(self.gid):
                continue
            if not os.path.exists(candidate.archive_path):
                continue

            try:
                metadata, hash_to_rsrc = TaskReuseResources._build_archive_resource(
                    candidate.archive_path
                )
            except (OSError, zipfile.BadZipFile, RuntimeError) as ex:
                self.logger.warning(
                    "%s: cannot reuse archive %s: %s",
                    self.guid,
                    candidate.archive_path,
                    str(ex),
                )
                continue

            if metadata is None or not metadata.url:
                self.logger.warning(
                    "%s: cannot reuse archive %s: unparseable archive comment",
                    self.guid,
                    candidate.archive_path,
                )
                continue
            
            archives_considered.add(candidate.archive_path)
            
            self.reuse.register_page_hash_source(hash_to_rsrc)
            
            if not metadata.download_ori and metadata.fid_page_hash_map is None:
                # if the archive doesn't have page hash map and also doesn't download ori
                # we can still try recover page hash by page scan
                # this is not very efficient but better than nothing
                self.reuse.add_pending_archive(candidate)

        return (
            archives_considered,
            len(self.reuse.pending_archives),
            len(self.reuse.page_hash_source_map),
        )

    # scan folder or zip file before all worker start working
    # it is designed mainly to remove truncated file and extract those outdated zip files
    def exact_downloaded_exits(
        self,
        require_fid_page_hash_map: bool = False,
        extract_non_exact_match: bool = False,
    ) -> Tuple[Literal[True], str] | Tuple[Literal[False], Optional[str]]:
        """Check if an exact matching archive exists (by gid/hash).

        This simplified version only checks for exact gid+hash matches without
        extracting files or scanning related archives.

        Args:
            require_fid_page_hash_map: If True (Phase 1), requires the found archive to have
                                      fid_page_hash_map in its metadata. If False (Phase 2),
                                      accepts match regardless of fid_page_hash_map presence.
            extract_non_exact_match: If True, allows extraction of files from non-exact matching archives.

        Returns:
            (bool, archive_path or None): (True, path) if exact match found, (False, None) for
                                          no match, (False, path) for non-exact match.
        """
        # fpath requires title
        if not self.meta.title:
            return False, None

        folder_path = self.get_task_dir()
        archive_path = f"{folder_path}.zip"

        def _check_exact_match(zipfile_target):
            """Check if zip is an exact gid+hash match with valid metadata."""
            try:
                comment_str = zipfile_target.comment.decode("UTF-8", errors="ignore")
                metadata = ArchiveMeta.decode_meta(comment_str)

                # Check marker, url, and file count
                marker_ok = comment_str.startswith("xeHentai Archiver v")
                url_ok = metadata is not None and metadata.url == self.url
                file_count = len(
                    [_n for _n in zipfile_target.namelist() if not _n.endswith("/")]
                )
                count_ok = metadata is not None and file_count == metadata.total

                if not (marker_ok and url_ok and count_ok):
                    return "bad_file"

                # Check gid+hash match
                assert metadata is not None

                arc_index = RE_INDEX.findall(metadata.url)
                if arc_index and self.gid and self.sethash:
                    arc_gid, arc_hash = arc_index[0]
                    if arc_gid == self.gid and arc_hash == self.sethash:
                        # Phase 1: Require fid_page_hash_map in archive metadata
                        if require_fid_page_hash_map:
                            if not metadata.fid_page_hash_map:
                                return "bad_meta"

                        # Validate fid_page_hash_map count matches total if present
                        if metadata.fid_page_hash_map:
                            if len(metadata.fid_page_hash_map) != metadata.total:
                                return "bad_meta"

                        # Preserve existing populated hash map from page scan; only load from archive if currently empty
                        if not self.fid_2_page_hash_map:
                            self.fid_2_page_hash_map = metadata.fid_page_hash_map or {}
                        
                        if metadata.download_ori:
                            self.config.update({"download_ori": metadata.download_ori})
                        
                        return "exact_match"

                return "bad_file"
            except Exception:
                return "bad_file"

        def _reuse_not_exact_zip(arc_path):
            """Extract files from an existing archive if it matches the current task's gid+hash.
            This just extracts files, it does not check file integrity or update metadata.
            """
            self.logger.debug(
                f"[guid={self.guid}] Extracting files from non-exact matching archive for reuse: {arc_path}"
            )

            task_dir = self.get_task_dir()
            if not os.path.exists(task_dir):
                os.makedirs(task_dir)

            with zipfile.ZipFile(arc_path, "r") as zf:
                for member in zf.namelist():
                    if member.endswith("/"):
                        continue

                    member_path = os.path.join(task_dir, member)
                    if os.path.exists(member_path):
                        os.remove(member_path)
                    with zf.open(member, "r") as src, open(member_path, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    ext = checkfile.detect_image_ext(member_path)
                    if ext:
                        stem = os.path.splitext(os.path.basename(member))[0]
                        reexted_name = f"{stem}{ext}"
                        reexted_path = os.path.join(task_dir, reexted_name)
                        if member_path != reexted_path:
                            os.rename(member_path, reexted_path)

            if os.path.exists(arc_path):
                os.remove(arc_path)

        current_arc_abs = os.path.abspath(archive_path)

        # Check current task directory zip
        if os.path.exists(archive_path):
            try:
                with zipfile.ZipFile(archive_path, "r") as current_zip:
                    is_exact = _check_exact_match(current_zip)
                    if is_exact == "exact_match":
                        return True, archive_path
                if is_exact == "bad_file" and extract_non_exact_match:
                    _reuse_not_exact_zip(archive_path)
                if is_exact == "bad_meta":
                    return False, archive_path
                return False, None
            except zipfile.BadZipFile:
                try:
                    os.remove(archive_path)
                except:
                    pass

        # Check gid-based lookup for directory name variations
        current_gid = str(getattr(self, "gid", "") or "")
        if current_gid and current_gid.isdigit():
            gid_arc = self._find_archive_by_gid(current_gid)
            if (
                gid_arc
                and os.path.abspath(gid_arc) != current_arc_abs
                and os.path.exists(gid_arc)
            ):
                try:
                    with zipfile.ZipFile(gid_arc, "r") as gid_zip:
                        is_exact = _check_exact_match(gid_zip)
                        if is_exact == "exact_match":
                            return True, gid_arc
                    if is_exact == "bad_file" and extract_non_exact_match:
                        _reuse_not_exact_zip(gid_arc)
                    if is_exact == "bad_meta":
                        return False, gid_arc
                    return False, None
                except zipfile.BadZipFile:
                    pass

        return False, None

    def get_task_dir(self) -> str:
        """
        Gets file path for the task.
        If the download is not done, this is the folder path for downloading files.
        If the download is done, this is the zip file path without extension.
        Uses 9-digit padded gallery_id and splits into 3+3 directory structure to avoid too many files in one folder.
        """
        gallery_id = str(self.gid if hasattr(self, "gid") else "unknown")
        # Only pad if gallery_id is all digits
        if gallery_id.isdigit():
            id_str = gallery_id.zfill(9)
            dir1 = id_str[:3]
            dir2 = id_str[3:6]
            base_dir = os.path.join(self.config.get("dir"), dir1, dir2)
            folder_name = f"{gallery_id} - {util.legalpath(self.meta.title)}"
            return os.path.join(base_dir, folder_name)
        else:
            # fallback for unknown/non-numeric id
            return os.path.join(
                self.config.get("dir"),
                f"{gallery_id} - {util.legalpath(self.meta.title)}",
            )

    def build_page_queue(self):
        """Build the page queue based on fid_page_hash_map and existing files."""

        missing = [
            str(i + 1)
            for i in range(self.meta.total)
            if str(i + 1) not in self.fid_2_page_hash_map
        ]
        if len(missing) > 0:
            self.logger.error("Missing page hash for fids: %s", ", ".join(missing))
            raise ValueError("Missing page hash for some fids, cannot build page queue")

        self.page_q = Queue()  # per image page queue
        # start rebuild page queue for later stages
        self.page_q.queue.clear()
        task_dir = self.get_task_dir()

        if len(self.reuse.page_hash_source_map) > 0:
            self.logger.debug(
                f"[guid={self.guid}] Reuse page hash source map has {len(self.reuse.page_hash_source_map)} entries before building page queue."
            )

        for fid, page_hash in self.fid_2_page_hash_map.items():
            # first try to materialize reuse file for this page hash, if hit, we can skip page scan and directly mark fid done
            if page_hash in self.reuse.page_hash_source_map:
                target_path = self.reuse._materialize_reuse_file(
                    task_dir, page_hash, self.meta.total, fid
                )
                if target_path:
                    self.logger.debug(
                        f"[guid={self.guid}] Materialized reuse file for fid={fid} from page hash {page_hash}."
                    )
                    basename = os.path.basename(target_path)
                    self.fid_2_file_name_map[fid] = basename
                    self.fid_2_img_hash_map[fid] = checkfile.file_hash(target_path)
                    self.set_fid_done(fid)
                    continue

            # if reuse not hit, then check if the file already exists by name and hash, if hit, we can also skip page scan and mark fid done
            # this is for the case when the task is restarted and files are already downloaded, we can avoid re-downloading and just check file existence and integrity by hash
            expected_file_name = self.fid_2_file_name_map.get(fid)
            expected_file_hash = self.fid_2_img_hash_map.get(fid)
            if expected_file_hash and expected_file_name:
                expected_path = os.path.join(task_dir, expected_file_name)
                if check_file(expected_path, expected_file_hash):
                    # file exists and matches expected hash, skip adding to page queue
                    self.logger.debug(
                        f"[guid={self.guid}] Found existing file for fid={fid} with matching hash, skipping."
                    )
                    self.set_fid_done(fid)
                    continue

            base_site = self.url.split(".org/")[0] + ".org"
            page_url = f"{base_site}/s/{page_hash}/{self.gid}-{fid}"
            # file not found or hash mismatch, add to page queue for scanning and downloading
            self.page_q.put(page_url)

    def save_image_response_content(self, res: requests.Response, img_url: str) -> str:
        """Save the content of a response to the appropriate file path based on the image URL and fid.

        Args:
            res (requests.Response): The HTTP response containing the image content and headers.
            img_url (str): The URL of the image, used to look up the reload URL and file name from the reload_map.
            fid (str): The file ID associated with the image, used to mark it as done after saving.

        Returns:
            str: The file path where the image was saved.
        """

        content = res.content
        content_type = res.headers.get("Content-Type", "")

        fpath = self.get_task_dir()
        if not os.path.exists(fpath):
            os.makedirs(fpath)

        reload_url, fname = self.reload_map[img_url]
        _, fid = RE_GALLERY.findall(reload_url)[0]
        ext = self._content_type_to_ext(content_type)
        if ext:
            fname = Task._build_saving_file_name(self.meta.total, fid, ext)
            self.reload_map[img_url] = (reload_url, fname)
            self.fid_2_file_name_map[fid] = fname
        else:
            ext = os.path.splitext(fname)[1]

        fn = os.path.join(fpath, fname)
        fn_tmp = os.path.join(fpath, ".%s.xeh" % fname)

        try:
            with open(fn_tmp, "wb+") as f:
                f.write(content)
            if os.path.exists(fn):
                os.remove(fn)
            os.rename(fn_tmp, fn)
        except Exception:
            self.logger.warning(
                "Failed to save file for fid %s:\n %s", fid, traceback.format_exc()
            )
            os.remove(fn_tmp)
            raise

        self.set_fid_done(fid)

        page_hash = self.fid_2_page_hash_map.get(fid)

        if page_hash in self.dumplicated_file_map:
            for info in self.dumplicated_file_map[page_hash]:
                if int(info.fid) == int(fid):
                    continue
                rep_name = Task._build_saving_file_name(self.meta.total, info.fid, ext)
                fn_rep = os.path.join(fpath, rep_name)
                if not fn == fn_rep:
                    shutil.copyfile(fn, fn_rep)
                    self.set_fid_done(info.fid)
            del self.dumplicated_file_map[page_hash]

        return fn

    def make_archive(self, remove=True) -> Tuple[str, bool]:
        """Create a zip archive of the downloaded files with metadata in the comment.

        Args:
            remove (bool): Whether to remove the original files after creating the archive.

        Returns:
            Tuple[str, bool]: A tuple containing the path to the zip archive and a boolean indicating
                              whether the archive was already up-to-date.
        """
        dpath = self.get_task_dir()
        arc = "%s.zip" % dpath
        expected_meta = self.encode_meta()

        def _():
            if os.path.exists(arc):
                file_count_match = False
                # make archive might be called without page scan
                # we can only check file count in this case, but it is better than nothing
                with zipfile.ZipFile(arc, "r") as zipfile_target:
                    if len(zipfile_target.namelist()) == self.meta.total:
                        file_count_match = True

                if file_count_match:
                    # if the archive already exists, we check if the comment matches expected meta
                    with zipfile.ZipFile(arc, "r") as zipfile_target:
                        if zipfile_target.comment == expected_meta:
                            return arc, True
                    # if comment is different, we need to update the comment
                    # but zipfile module does not support editing comment, we need to rewrite the zip file
                    with zipfile.ZipFile(arc, "a") as zipfile_target:
                        zipfile_target.comment = expected_meta

                    return arc, False

            with zipfile.ZipFile(arc, "w") as zipfile_target:
                # zip comment created
                # store json info in respective zip file
                # thus metadata can be packed with comic it self in a single file
                zipfile_target.comment = expected_meta

                if len(self.fid_2_file_name_map) != self.meta.total:
                    raise ValueError(
                        "File count mismatch when making archive: expected %d, got %d"
                        % (self.meta.total, len(self.fid_2_file_name_map))
                    )

                for fid, name in self.fid_2_file_name_map.items():
                    full_path = os.path.join(dpath, name)
                    zipfile_target.write(full_path, name, zipfile.ZIP_STORED)

                return arc, False

        ret = _()
        if remove:
            if os.path.exists(dpath):
                shutil.rmtree(dpath)
        return ret

    def cleanup_download_info(self):
        """Clean up download-related info to save memory after the task is finished.

        Retains fid_2_img_hash_map and fid_2_file_name_map so image URLs remain
        resolvable after task completion (needed by the WebUI REST API).
        """
        if self.state != TASK_STATE_FINISHED:
            return
        if len(self._flist_done) != self.meta.total:
            return

        self.page_q = None
        self.reload_map = {}
        # Keep: self.fid_2_img_hash_map (content-addressable image URLs)
        # Keep: self.fid_2_file_name_map (locate files in dir/archive)
        self.dumplicated_file_map = {}
        self.reuse.reset()

    def rebuild_file_maps_from_zip(self):
        """Rebuild fid_2_img_hash_map and fid_2_file_name_map from the zip archive.

        Used for old tasks whose maps were cleared by a previous version of
        cleanup_download_info, or for tasks imported from external archives.
        """
        import zipfile, hashlib
        task_dir = self.get_task_dir()
        zip_path = f"{task_dir}.zip"
        if not os.path.isfile(zip_path):
            return
        with zipfile.ZipFile(zip_path, "r") as zf:
            members = sorted(
                [m for m in zf.namelist() if not m.endswith("/")],
                key=lambda m: os.path.splitext(m)[0]
            )
            for i, name in enumerate(members, 1):
                fid_str = str(i)
                data = zf.read(name)
                file_hash = hashlib.md5(data).hexdigest()[:10]
                self.fid_2_img_hash_map[fid_str] = file_hash
                self.fid_2_file_name_map[fid_str] = name
        # Update total if meta is missing
        if self.meta and not self.meta.total:
            self.meta.total = len(members)

    def from_dict(self, j, core_config=None):
        for k in self.__dict__:
            if k not in j:
                continue
            if k == "meta":
                setattr(self, k, GalleryMeta.from_dict(j[k]))
                continue
            if k == "config":
                cfg = j[k] if isinstance(j[k], dict) else {}
                if core_config is not None:
                    setattr(self, k, core_config.create_task_config(cfg))
                else:
                    setattr(self, k, TaskConfig(cfg))
                continue
            if k == "dumplicated_file_map":
                raw_map = j[k] if isinstance(j[k], dict) else {}
                restored_map = {
                    str(file_hash): [
                        DumplicatedFileInfo.from_dict(info)
                        for info in infos
                        if isinstance(info, dict)
                    ]
                    for file_hash, infos in raw_map.items()
                    if isinstance(infos, list)
                }
                setattr(self, k, restored_map)
                continue
            if k == "reuse":
                setattr(self, k, TaskReuseResources.from_dict(j[k]))
                continue
            if k == "failcode":
                # legacy key superseded by negative TASK_STATE_ERR_*
                pass
            elif k.endswith("_q"):
                pass
            else:
                setattr(self, k, j[k])
        return self

    def to_dict(self):
        d = dict(
            {
                k: v
                for k, v in self.__dict__.items()
                if not k.endswith("_q") and not k.startswith("_")
            }
        )
        d["meta"] = self.meta.to_dict()
        if hasattr(self.config, "to_local_dict"):
            d["config"] = self.config.to_local_dict()
        d["reuse"] = self.reuse.to_dict()
        d["dumplicated_file_map"] = {
            fhash: [
                info.to_dict()
                for info in infos
                if isinstance(info, DumplicatedFileInfo)
            ]
            for fhash, infos in self.dumplicated_file_map.items()
        }

        queues = [k for k in self.__dict__.keys() if k.endswith("_q")]
        for k in queues:
            if getattr(self, k):
                d[k] = [e for e in getattr(self, k).queue]
        return d
