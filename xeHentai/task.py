#!/usr/bin/env python
# coding:utf-8
# Contributor:
#      fffonion        <fffonion@gmail.com>

import os
import re
import copy
import json
import uuid
import shutil
import zipfile
from typing import Any, Dict, List, Optional, Set, Tuple
from threading import RLock
from dataclasses import dataclass, asdict, field
from . import util
from . import reuse_index
from .const import *
from .const import __version__
from queue import Queue, Empty


@dataclass
class GalleryMeta:
    title_japanese: str = ''
    title_primary: str = ''
    title: str = ''
    total: int = 0
    finished: int = 0
    thumbnail_cnt: int = 0
    has_ori: bool = False
    tags: List[Any] = field(default_factory=list)
    newer_versions: List[Dict[str, Any]] = field(default_factory=list)
    filelist: Dict[str, Any] = field(default_factory=dict)
    resampled: Dict[str, Any] = field(default_factory=dict)
    sample_hash: List[str] = field(default_factory=list)
    extra: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> 'GalleryMeta':
        meta = cls()
        meta.update_from_dict(data or {})
        return meta

    def to_dict(self) -> Dict[str, Any]:
        payload = {
            'title_japanese': self.title_japanese,
            'title_primary': self.title_primary,
            'title': self.title,
            'total': self.total,
            'finished': self.finished,
            'thumbnail_cnt': self.thumbnail_cnt,
            'has_ori': self.has_ori,
            'tags': list(self.tags),
            'newer_versions': [dict(item) for item in self.newer_versions],
            'filelist': dict(self.filelist),
            'resampled': dict(self.resampled),
            'sample_hash': list(self.sample_hash),
            'gjname': self.title_japanese,
            'gnname': self.title_primary,
        }
        payload.update(self.extra)
        return payload

    def update_from_dict(self, data: Dict[str, Any]) -> None:
        self.title_japanese = str(data.get('title_japanese', data.get('gjname', self.title_japanese)) or '')
        self.title_primary = str(data.get('title_primary', data.get('gnname', self.title_primary)) or '')
        self.title = str(data.get('title', self.title) or '')
        self.total = int(data.get('total', self.total) or 0)
        self.finished = int(data.get('finished', self.finished) or 0)
        self.thumbnail_cnt = int(data.get('thumbnail_cnt', self.thumbnail_cnt) or 0)
        self.has_ori = bool(data.get('has_ori', self.has_ori))
        self.tags = list(data.get('tags', self.tags) or [])
        self.newer_versions = [dict(item) for item in (data.get('newer_versions', self.newer_versions) or [])]
        self.filelist = dict(data.get('filelist', self.filelist) or {})
        self.resampled = dict(data.get('resampled', self.resampled) or {})
        self.sample_hash = list(data.get('sample_hash', self.sample_hash) or [])

        known_keys = {
            'title_japanese', 'title_primary', 'gjname', 'gnname', 'title',
            'total', 'finished', 'thumbnail_cnt', 'has_ori', 'tags',
            'newer_versions', 'filelist', 'resampled', 'sample_hash',
        }
        for key, value in data.items():
            if key not in known_keys:
                self.extra[key] = value

    def has_title(self) -> bool:
        return bool(self.title)

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
    rename_ori: bool
    download_ori: bool
    url: str
    fid_page_hash_map: Optional[Dict[str, str]] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'ArchiveMeta':
        """Create ArchiveMeta from dictionary with validation"""
        required_fields = {'tags', 'total', 'title',
                           'rename_ori', 'download_ori', 'url'}
        missing = required_fields - set(data.keys())
        if missing:
            raise ValueError(f"Missing required archive metadata fields: {missing}")
        
        try:
            fid_page_hash_map = data.get('fid_page_hash_map')
            if fid_page_hash_map is not None:
                fid_page_hash_map = dict(fid_page_hash_map)

            title_japanese = str(data.get('title_japanese', data.get('gjname', '')))
            title_primary = str(data.get('title_primary', data.get('gnname', '')))

            return cls(
                title_japanese=title_japanese,
                title_primary=title_primary,
                tags=data['tags'],
                total=int(data['total']),
                title=str(data['title']),
                rename_ori=bool(data['rename_ori']),
                download_ori=bool(data['download_ori']),
                url=str(data['url']),
                fid_page_hash_map=fid_page_hash_map,
            )
        except (TypeError, ValueError) as e:
            raise ValueError(f"Invalid archive metadata types: {e}")

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization with legacy aliases."""
        data = asdict(self)
        # Legacy field aliases for backward compatibility.
        data['gjname'] = self.title_japanese
        data['gnname'] = self.title_primary
        return data


class Task(object):
    def __init__(self, url: str, cfgdict: Dict[str, Any]):
        # Original gallery URL.
        self.url: str = url
        if url:
            _ = RE_INDEX.findall(url)
            if _:
                self.gid, self.sethash = _[0]
        # Last task failure code.
        self.failcode: int = 0
        # Current lifecycle state (TASK_STATE_*).
        self.state: int = TASK_STATE_WAITING
        # Short runtime task identifier.
        self.guid: str = str(uuid.uuid4())[:8]
        # Task-level merged config.
        self.config: Dict[str, Any] = cfgdict
        # Parsed gallery metadata payload.
        self.meta: GalleryMeta = GalleryMeta()
        # Whether original-quality image variants are detected.
        self.has_ori: bool = False
        # Maps image URL to [reload URL, resolved filename].
        self.reload_map: Dict[str, List[str]] = {}
        # map same hash to different ids, {url:((id, fname), )}
        self.filehash_map: Dict[str, List[Tuple[str, Any]]] = {}

        # renamed map just don't work well with extension part

        # this situation happens especially with animated galleries
        # in which you would download an original file even you dont use the Download original link
        # renamed map still thinks the .gif file is a .jpg file
        # thus you can only view the first frame of the gif

        # when downloading original file, renamed map still choose the extension in single image page
        # you will get an png file renamed to be a jpg file
        # well, in fact you can view the file just fine
        # but photoshop says "no, png file have to be a png file"

        # besides, in some rare cases, the original png file is so small
        # that you will get an original png file when not download original

        # it is somehow hard to upgrade the old method
        # i choose to write a new one
        # self.renamed_map = {} # map fid to renamed file name, used in finding a file by id in RPC

        # original file name only appears in gallery page
        # in single image page it shows a formated image other than original file name

        # file that was in the folder, used to check downloaded files
        # map file name to file size

        # file size check grant more precision in downloaded file check
        self._file_in_download_folder: List[str] = []

        # map fid to file original name, which appears on gallery pages
        self.fid_2_original_file_name_map: Dict[str, str] = {}

        # map fid to resolved runtime file name
        self.fid_2_file_name_map: Dict[str, str] = {}

        # map fid to file extension; file name is derived from fid padding + ext
        self.fid_2_file_ext_map: Dict[str, str] = {}

        # download range list, former method is too hard to maintain
        self.download_range: List[int] = []

        # times of image page loading is used by ehentai for counting bandwidth limit
        self.fid_2_file_size_map: Dict[str, str] = {}  # map fid to file size text, reduce image page load

        # map fid to 10-char page hash, extracted from /s/<hash>/<gid>-<fid>
        self.fid_2_page_hash_map: Dict[str, str] = {}

        # lazy-loaded mapping: page_hash -> (archive_path, archive_member_name)
        self._related_archive_hash_index: Dict[str, Tuple[str, str]] = {}
        self._related_archive_hash_index_ready: bool = False

        # shared, process-level reuse index injected by core
        self._reuse_index: Optional[Dict[str, Any]] = None

        # and, the fid in these map will all be str
        # when int key dumps into files by python, it is somehow transformed into str
        # and an error would occur when you load it again

        # Download work queue (image URLs).
        self.img_q: Optional[Queue] = None
        # Single-image page queue.
        self.page_q: Optional[Queue] = None
        # Gallery list page queue.
        self.list_q: Optional[Queue] = None
        # Finished image IDs (rebuilt on scan, not persisted directly).
        self._flist_done: Set[int] = set()
        # Task monitor thread reference.
        self._monitor: Any = None
        # Lock for counters/state transitions.
        self._cnt_lock: Any = RLock()
        # Lock for file-system writes and renames.
        self._f_lock: Any = RLock()

    def cleanup(self, before_delete=False):
        if before_delete:
            if 'delete_task_files' in self.config and self.config['delete_task_files'] and \
                    self.meta.has_title():  # maybe it's a error task and meta is empty
                fpath = self.get_task_dir()
                # TODO: ascii can't decode? locale not enus, also check save_file
                if os.path.exists(fpath):
                    shutil.rmtree(fpath)
                zippath = "%s.zip" % fpath
                if os.path.exists(zippath):
                    os.remove(zippath)
        elif self.state in (TASK_STATE_FINISHED, TASK_STATE_FAILED):
            self.img_q = None
            self.page_q = None
            self.list_q = None
            self.reload_map = {}

            self._file_in_download_folder = []
            self.fid_2_file_size_map = {}
            self.fid_2_original_file_name_map = {}
            self.fid_2_file_name_map = {}
            self.fid_2_page_hash_map = {}
            self.fid_2_file_ext_map = {}
            self._related_archive_hash_index = {}
            self._related_archive_hash_index_ready = False
            self.download_range = []
            # if 'filelist' in self.meta:
            #     del self.meta['filelist']
            # if 'resampled' in self.meta:
            #     del self.meta['resampled']

    def set_fail(self, code):
        self.state = TASK_STATE_FAILED
        self.failcode = code
        # cleanup all we cached
        self.meta = GalleryMeta()

    def migrate_exhentai(self):
        _ = re.findall(r"(?:https*://[g\.]*e\-hentai\.org)(.+)", self.url)
        if not _:
            return False
        self.url = "https://exhentai.org%s" % _[0]
        self.state = TASK_STATE_WAITING if self.state == TASK_STATE_FAILED else self.state
        self.failcode = 0
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
            rename_ori=self.config['rename_ori'],
            download_ori=self.config['download_ori'],
            url=self.url,
            fid_page_hash_map=self.fid_2_page_hash_map,
        )
        json_zip_meta = json.dumps(archive_meta.to_dict())
        return ("xeHentai Archiver v%s r1\n%s" % (__version__, json_zip_meta)).encode('UTF-8')

    @staticmethod
    def decode_meta(comment_str: str) -> Optional[ArchiveMeta]:
        """Decode and validate metadata from zip file comment"""
        lbrace = comment_str.find('{')
        rbrace = comment_str.rfind('}')
        if lbrace == -1 or rbrace == -1 or lbrace > rbrace:
            return None
        
        meta_str = comment_str[lbrace:rbrace+1]
        if not meta_str:
            return None
        
        try:
            data = json.loads(meta_str)
            return ArchiveMeta.from_dict(data)
        except (json.JSONDecodeError, ValueError, KeyError, TypeError):
            return None

    def update_meta(self, meta: Dict[str, Any]) -> None:
        """Update metadata with type validation for critical fields"""
        self.meta.update_from_dict(meta)
        self.meta.select_display_title(bool(self.config.get('jpn_title')))


    # def guess_ori(self):
    #     # guess if this gallery has resampled files depending on some sample hashes
    #     # return True if it's ori
    #     if 'sample_hash' not in self.meta:
    #         return
    #     all_keys = map(lambda x:x[:10], self.meta['filelist'].keys())
    #     for h in self.meta['sample_hash']:
    #         if h not in all_keys:
    #             self.has_ori = True
    #             break
    #     del self.meta['sample_hash']

    def base_url(self):
        return re.findall(RESTR_SITE, self.url)[0]

    # def get_picpage_url(self, pichash):
    #     # if file resized, this url not works
    #     # http://%s.org/s/hash_s/gid-picid'
    #     return "%s/s/%s/%s-%s" % (
    #         self.base_url(), pichash[:10], self.gid, self.meta['filelist'][pichash][0]
    #     )

    def get_size_range(self, size_text):
        _ = re.findall(r'(\d+(?:\.(\d+))?) *([M|K]?i?B)', size_text)
        if _:
            _number, _decimal, _unit = _[0]
        else:
            return 0, 0
        number = float(_number)
        uncertain = 0.5

        if _decimal:
            for i in range(0, len(_decimal)):
                uncertain /= 10

        unit = 1
        if _unit == 'KiB' or _unit == 'KB':
            unit *= 1024
        elif _unit == 'MiB' or _unit == 'MB':
            unit *= 1048576
        return (number - uncertain) * unit, (number + uncertain) * unit

    def check_size_range(self, test_file_path, file_size_text):
        size_bottom, size_top = self.get_size_range(file_size_text)
        existed_file_size = os.stat(test_file_path).st_size
        return size_bottom <= existed_file_size < size_top

    def set_fid_done(self, fid):
        self._cnt_lock.acquire()
        self._flist_done.add(int(fid))
        self.meta.finished = len(self._flist_done)
        self._cnt_lock.release()

    def _build_fid_file_name(self, fid, ext='.jpg'):
        fid = int(fid)
        _ = "%%0%dd%%s" % len(str(self.meta.total))
        return _ % (fid, ext)

    def _content_type_to_ext(self, content_type):
        content_type = (content_type or '').strip().lower()
        content_type_map = {
            'image/jpeg': '.jpg',
            'image/jpg': '.jpg',
            'image/png': '.png',
            'image/gif': '.gif',
            'image/bmp': '.bmp',
            'image/webp': '.webp',
        }
        return content_type_map.get(content_type)

    def _infer_ext_from_url(self, url):
        if not url:
            return None
        _ = RE_IMGHASH.findall(url)
        if _ and _[-1] and _[-1][4]:
            return '.%s' % _[-1][4].lower()
        _ = re.findall(r"/([^/\?]+)(?:\?|$)", url)
        if _:
            ext = os.path.splitext(_[0])[1].lower()
            if ext:
                return ext
        return None

    def _resolve_download_ext(self, fid, content_type=None, redirect_url=None, fallback_name=None):
        ext = self._content_type_to_ext(content_type)
        if ext:
            return ext
        ext = self._infer_ext_from_url(redirect_url)
        if ext:
            return ext
        if fallback_name:
            ext = os.path.splitext(fallback_name)[1].lower()
            if ext:
                return ext
        if fid in self.fid_2_file_ext_map:
            ext = self.fid_2_file_ext_map[fid].lower()
            if ext:
                return ext
        return '.jpg'

    def _set_final_file_ext(self, fid, ext):
        ext = ext or '.jpg'
        fid = str(fid)
        file_name = self._build_fid_file_name(fid, ext)
        self.fid_2_file_ext_map[fid] = ext
        self.fid_2_file_name_map[fid] = file_name
        return file_name

    def _build_fid_filename_map(self, file_names):
        fid_name_map = {}
        fid_ext_map = {}
        for file_name in file_names:
            if not file_name or file_name.endswith('/'):
                continue
            _ = os.path.basename(file_name)
            name, ext = os.path.splitext(_)
            if not name.isdigit():
                continue
            fid = str(int(name))
            fid_name_map[fid] = _
            fid_ext_map[fid] = ext or '.jpg'
        return fid_name_map, fid_ext_map

    def set_reload_url(self, image_url, reload_url, fname, filesize):
        """Register image reload metadata and try local/related-archive reuse before download.

        Reuse priority:
        1) same-task existing file checks (legacy behavior)
        2) related gallery archives discovered from #gnd, matched by page hash and verified by size
        3) fallback to queue download
        """
        # if same file occurs several times in a gallery
        # to be done with new rename logic

        this_fid = RE_GALLERY.findall(reload_url)[0][1]
        real_file_name = self.fid_2_original_file_name_map[this_fid]

        ext = os.path.splitext(fname)[1]
        if self.config['download_ori']:
            ext = os.path.splitext(real_file_name)[1]

        real_file_name = self._set_final_file_ext(this_fid, ext or '.jpg')

        if this_fid not in self.fid_2_file_size_map:
            self.fid_2_file_size_map.setdefault(this_fid, filesize)
        else:
            self.fid_2_file_size_map[this_fid] = filesize

        # two files have same url
        if image_url in self.reload_map:
            existed_image_url, existed_file_name = self.reload_map[image_url]
            folder_path = self.get_task_dir()
            existed_file = os.path.join(folder_path, existed_file_name)
            file_existed = False
            unexpected_file = False
            existed_file_id = RE_GALLERY.findall(existed_image_url)
            if os.path.exists(existed_file):
                file_existed = True
                unexpected_file = not self.check_size_range(
                    existed_file, filesize)
                print('>> file existed, expected size: %s, %s' %
                      (filesize, 'unexpected' if unexpected_file else 'expected'))

            if file_existed and not unexpected_file:
                new_file = os.path.join(folder_path, real_file_name)
                # we can just copy old file if already downloaded
                if not existed_file == new_file:
                    self._f_lock.acquire()
                    shutil.copy2(existed_file, new_file)
                    self._f_lock.release()
                self.set_fid_done(this_fid)
                return

            if file_existed and unexpected_file:
                # target file is not what we wanted
                # download it again
                self.img_q.put(image_url)

            del self.reload_map[image_url]

            # whether file not exist or is unexpected file
            # set a copy sequence
            # we will copy them in save_file
            if image_url not in self.filehash_map:
                self.filehash_map[image_url] = []
            self.filehash_map[image_url].append((this_fid, existed_file_id))
        else:
            self.reload_map.setdefault(image_url, [reload_url, real_file_name])

            # check file size for downloaded file
            # i would like a hash check
            # but i cant get a hash before downloading the file
            file_existed = False
            unexpected_file = False
            folder_path = self.get_task_dir()
            target_file_path = os.path.join(folder_path, real_file_name)
            if os.path.exists(target_file_path):
                file_existed = True
                unexpected_file = not self.check_size_range(
                    target_file_path, filesize)

            if file_existed and not unexpected_file:
                # well that's definitely the file we need
                self.set_fid_done(this_fid)
                return

            page_hash = self.fid_2_page_hash_map.get(this_fid)
            if page_hash and self._try_reuse_from_related_archive(page_hash, real_file_name, filesize):
                self.set_fid_done(this_fid)
                return

            # otherwise add it to download queue
            self.img_q.put(image_url)

    def get_reload_url(self, imgurl):
        """Return queued reload URL for an image URL, if present."""
        if not imgurl or imgurl not in self.reload_map:
            return
        return self.reload_map[imgurl][0]

    def _get_gid_bucket_dir(self, gid: str) -> Optional[str]:
        """Return the 3+3 bucket directory path for a numeric gallery id."""
        gid = str(gid)
        if not gid.isdigit():
            return None
        gid_padded = gid.zfill(9)
        return os.path.join(self.config['dir'], gid_padded[:3], gid_padded[3:6])

    def _find_archive_by_gid(self, gid: str) -> Optional[str]:
        """Locate a gallery archive zip by gid under the current bucketed layout."""
        bucket_dir = self._get_gid_bucket_dir(gid)
        if not bucket_dir or not os.path.isdir(bucket_dir):
            return None

        prefix = "%s - " % gid
        for name in os.listdir(bucket_dir):
            if name.startswith(prefix) and name.endswith('.zip'):
                return os.path.join(bucket_dir, name)
        return None

    def _build_related_archive_hash_index(self) -> None:
        """Build a fallback hash index from #gnd-related archives.

        This index is optional. Primary lookup should use global by_page_hash index.
        """
        if self._related_archive_hash_index_ready:
            return

        idx: Dict[str, Tuple[str, str]] = {}
        for version in reversed(self.meta.newer_versions):
            gid = str(version.get('gid', ''))
            if not gid or gid == str(getattr(self, 'gid', '')):
                continue
            arc = self._find_archive_by_gid(gid)
            if not arc or not os.path.exists(arc):
                continue

            try:
                with zipfile.ZipFile(arc, 'r') as zf:
                    metadata = self.decode_meta(
                        zf.comment.decode('UTF-8', errors='ignore'))
                    if not metadata or not metadata.fid_page_hash_map:
                        continue

                    member_name_map, _ = self._build_fid_filename_map(zf.namelist())

                    for src_fid, src_hash in metadata.fid_page_hash_map.items():
                        member_name = member_name_map.get(str(src_fid))
                        if not member_name:
                            continue
                        if src_hash not in idx:
                            idx[src_hash] = (arc, member_name)
            except (zipfile.BadZipFile, OSError, RuntimeError):
                continue

        self._related_archive_hash_index = idx
        self._related_archive_hash_index_ready = True

    def _iter_global_hash_candidates(self, page_hash: str):
        """Yield valid source candidates from shared global index for a page hash."""
        if not self._reuse_index:
            return
        by_hash = self._reuse_index.get('by_page_hash', {})
        entries = by_hash.get(page_hash, [])
        entries = sorted(entries, key=lambda x: int(x.get('updated_at', 0)), reverse=True)
        for entry in entries:
            source_path = entry.get('source_path')
            if source_path and os.path.exists(source_path):
                yield entry

    def _try_copy_from_source(self, entry: Dict[str, Any], target_path: str, size_text: str) -> bool:
        """Copy from a source entry (zip member or plain file) and verify size range."""
        source_type = entry.get('source_type')
        source_path = entry.get('source_path')
        member_name = entry.get('member_name')

        tmp_path = "%s.xeh" % target_path
        try:
            if source_type == 'zip':
                with zipfile.ZipFile(source_path, 'r') as zf:
                    with zf.open(member_name, 'r') as src, open(tmp_path, 'wb') as dst:
                        shutil.copyfileobj(src, dst)
            elif source_type == 'file':
                shutil.copyfile(source_path, tmp_path)
            else:
                return False

            if not self.check_size_range(tmp_path, size_text):
                os.remove(tmp_path)
                return False

            if os.path.exists(target_path):
                os.remove(target_path)
            os.rename(tmp_path, target_path)
            return True
        except (KeyError, OSError, zipfile.BadZipFile):
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            return False

    def _try_reuse_from_related_archive(self, page_hash: str, target_name: str, size_text: str) -> bool:
        """Try to reuse by hash from global index first, then #gnd fallback."""
        target_dir = self.get_task_dir()
        if not os.path.exists(target_dir):
            os.makedirs(target_dir)
        target_path = os.path.join(target_dir, target_name)

        for entry in self._iter_global_hash_candidates(page_hash) or []:
            if self._try_copy_from_source(entry, target_path, size_text):
                return True

        self._build_related_archive_hash_index()
        source = self._related_archive_hash_index.get(page_hash)
        if not source:
            return False
        archive_path, member_name = source
        return self._try_copy_from_source({
            'source_type': 'zip',
            'source_path': archive_path,
            'member_name': member_name,
        }, target_path, size_text)

    def _collect_prescan_archive_candidates(self, current_arc: str) -> List[Dict[str, Any]]:
        """Collect archive candidates for prescan from the shared search index."""
        return reuse_index.collect_prescan_candidates(
            self._reuse_index,
            current_arc,
            self.meta.title,
            str(getattr(self, 'gid', '')),
            self.meta.newer_versions,
        )

    def _can_extract_foreign_archive(self, metadata: Optional[ArchiveMeta], candidate: Dict[str, Any]) -> bool:
        """Validate whether a foreign archive can seed the current task directory."""
        if metadata is None:
            return False

        candidate_url = metadata.url or str(candidate.get('candidate_url', ''))
        candidate_gid = reuse_index.extract_gid_from_url(candidate_url) or str(candidate.get('candidate_gid', ''))
        if not candidate_url and not candidate_gid:
            return False

        return reuse_index.is_known_related(
            self._reuse_index,
            self.url,
            str(getattr(self, 'gid', '')),
            candidate_url,
            candidate_gid,
            self.meta.newer_versions,
        )

    # scan folder or zip file before all worker start working
    # it is designed mainly to remove truncated file and extract those outdated zip files
    def prescan_downloaded(self):
        """Prescan zip/folder and optionally seed current folder from related archives.

        The method trusts current archive only when marker/url/count all match.
        For related archive candidates, it extracts as baseline but never deletes
        foreign zip files.
        """

        # fpath requires title
        if not self.meta.has_title():
            return False
        folder_path = self.get_task_dir()

        # Quick trust check for existing zip:
        # 1) has xeHentai archive marker in comment
        # 2) embedded url matches current task url
        # 3) file count matches expected total
        arc = "%s.zip" % folder_path

        current_arc_abs = os.path.abspath(arc)
        for candidate in self._collect_prescan_archive_candidates(arc):
            candidate_arc = candidate.get('archive_path')
            if not candidate_arc:
                continue
            if not os.path.exists(candidate_arc):
                continue

            is_foreign_arc = os.path.abspath(candidate_arc) != current_arc_abs
            try:
                with zipfile.ZipFile(candidate_arc, 'r') as zipfile_target:
                    comment_str = zipfile_target.comment.decode('UTF-8', errors='ignore')
                    metadata = self.decode_meta(comment_str)

                    # Verify archive marker
                    marker_ok = comment_str.startswith('xeHentai Archiver v')

                    # Verify URL matches current task and metadata is valid
                    url_ok = metadata is not None and metadata.url == self.url

                    # Count actual files in zip (excluding directories)
                    file_count = len([_n for _n in zipfile_target.namelist() if not _n.endswith('/')])

                    # Verify file count in zip metadata matches actual file count
                    count_ok = metadata is not None and file_count == metadata.total

                    if marker_ok and url_ok and count_ok:
                        # All three checks pass: zip is trusted as complete
                        assert metadata is not None
                        member_name_map, member_ext_map = self._build_fid_filename_map(zipfile_target.namelist())
                        self._flist_done.update(range(1, metadata.total + 1))
                        self.fid_2_file_name_map = member_name_map
                        self.fid_2_file_ext_map = member_ext_map
                        self.fid_2_page_hash_map = metadata.fid_page_hash_map or {}
                        self.meta.finished = len(self._flist_done)
                        return self.meta.finished == self.meta.total

                    # Checks failed: only validated related archives may seed baseline files.
                    if is_foreign_arc and not self._can_extract_foreign_archive(metadata, candidate):
                        continue

                    # Extract as reusable baseline.
                    # Only delete when this is the current task archive.
                    if not os.path.exists(folder_path):
                        os.makedirs(folder_path)
                    zipfile_target.extractall(folder_path)
                    zipfile_target.close()
                    if not is_foreign_arc:
                        os.remove(candidate_arc)
                    break
            except zipfile.BadZipFile:
                if not is_foreign_arc:
                    os.remove(candidate_arc)
                continue

        self.meta.finished = len(self._flist_done)
        if self.meta.finished == self.meta.total:
            return True
        return False

    def scan_downloaded(self, fid_2_page_url_map, scaled=True):
        """Scan existing download folder and mark already-valid files as finished."""
        folder_path = self.get_task_dir()
        is_done_file = False
        _range_idx = 0

        scanning_zip = False
        scanning_folder = False

        # scan folder only
        # if there is any problem in zip
        # prescan should have extracted it

        if os.path.exists(folder_path):
            scanning_folder = True
        else:
            for _fid, _page_url in fid_2_page_url_map.items():
                self.page_q.put(_page_url)
            return False

        file_name = ''

        guess_fid_2_file_name_map = {}

        re_name_filter = re.compile(
            r'^(\d{%d})\..+$' % len(str(self.meta.total)))
        self._file_in_download_folder = []

        for _file_name in os.listdir(folder_path):
            _ext = os.path.splitext(_file_name)[1]
            if _ext == '.xeh':
                self._f_lock.acquire()
                os.remove(os.path.join(folder_path, _file_name))
                self._f_lock.release()
            else:
                self._file_in_download_folder.append(_file_name)

        for _file_name in self._file_in_download_folder:
            _ = re_name_filter.findall(_file_name)
            if _:
                guess_fid_2_file_name_map.setdefault(
                    str(int(_[0])), _file_name)

        for _fid, _file_name in self.fid_2_original_file_name_map.items():
            if _fid not in guess_fid_2_file_name_map and os.path.exists(os.path.join(folder_path, _file_name)):
                guess_fid_2_file_name_map.setdefault(_fid, _file_name)

        for _fid, _url in fid_2_page_url_map.items():
            image_done_file = False
            if _fid in self.fid_2_file_size_map\
                    and _fid in guess_fid_2_file_name_map:
                size_text = self.fid_2_file_size_map[_fid]
                guess_file_name = guess_fid_2_file_name_map[_fid]
                bottom, top = self.get_size_range(size_text)
                size = os.stat(os.path.join(
                    folder_path, guess_file_name)).st_size
                if bottom <= size < top:
                    file_name = guess_file_name
                    image_done_file = True

            if not image_done_file:
                self.page_q.put(_url)
            else:
                file_ext = os.path.splitext(file_name)[1] or '.jpg'
                final_file_name = self._build_fid_file_name(_fid, file_ext)
                if file_name != final_file_name:
                    src_path = os.path.join(folder_path, file_name)
                    dst_path = os.path.join(folder_path, final_file_name)
                    if not os.path.exists(dst_path):
                        os.rename(src_path, dst_path)
                    file_name = final_file_name
                self.fid_2_file_name_map[_fid] = file_name
                self.fid_2_file_ext_map[_fid] = os.path.splitext(file_name)[1] or '.jpg'
                self._flist_done.add(int(_fid))

        self.meta.finished = len(self._flist_done)
        if self.config['download_range']:
            self.meta.finished += (self.meta.total -
                                      len(self.download_range))
        if self.meta.finished == self.meta.total:
            return True
        return False

    def queue_wrapper(self, callback_page_url_setdefault, pichash=None, img_tuble=None):
        """Normalize per-page tuple and capture fid->page_hash for later reuse matching."""
        # if url is not finished, call callback to put into queue
        # type 1: normal file; type 2: resampled url
        # if pichash:
        #     fid = int(self.meta['filelist'][pichash][0])
        #     if fid not in self._flist_done:
        #         callback(self.get_picpage_url(pichash))
        # elif url:
        # fhash, fid = RE_GALLERY.findall(img_tuble[0])[0]

        # if fhash not in self.meta['filelist']:
        #     self.meta['resampled'][fhash] = int(fid)
        #     self.has_ori = True]
        # if int(fid) not in self._flist_done:
        #    callback1(img_tuble[0])

        _page_url, _fid, _original_file_name = img_tuble

        _match = RE_GALLERY.findall(_page_url)
        if _match:
            self.fid_2_page_hash_map.setdefault(_fid, _match[0][0])

        if self.config['download_range']:
            if not int(_fid) in self.download_range:
                return

        # assuming image files are not changed
        # image file may have changed
        # if int(_fid) in self._flist_done:
        #    self._cnt_lock.acquire()
        #    self._flist_done.remove(int(_fid))
        #    self.meta['finished'] = len(self._flist_done)
        #    self._cnt_lock.release()

        if _fid not in self.fid_2_original_file_name_map:
            _is_crashed = False
            for fid_in_list, file_name_in_list in self.fid_2_original_file_name_map.items():
                if _original_file_name == file_name_in_list:
                    _is_crashed = True
                    break

            # if same original name occurs several times
            # this will solve it
            if _is_crashed:
                _file_name, _ext = os.path.splitext(_original_file_name)
                _append_quote = 1
                _assume_file_name = _original_file_name
                while _is_crashed:
                    _is_crashed = False
                    _assume_file_name = '%s_%d%s' % (
                        _file_name, _append_quote, _ext)
                    for fid_in_list, file_name_in_list in self.fid_2_original_file_name_map.items():
                        if _assume_file_name == file_name_in_list:
                            _is_crashed = True
                            break
                    if _is_crashed:
                        _append_quote += 1
                _original_file_name = _assume_file_name
            self.fid_2_original_file_name_map.setdefault(
                _fid, _original_file_name)

        callback_page_url_setdefault(_fid, _page_url)

    def save_file(self, imgurl, redirect_url, binary_iter, content_type=None, original_hash=None):
        # TODO: Rlock for finished += 1
        fpath = self.get_task_dir()
        self._f_lock.acquire()
        if not os.path.exists(fpath):
            os.makedirs(fpath)
        self._f_lock.release()

        if imgurl not in self.reload_map:
            return

        pageurl, fname = self.reload_map[imgurl]
        _, fid = RE_GALLERY.findall(pageurl)[0]
        ext = self._resolve_download_ext(fid, content_type=content_type, redirect_url=redirect_url, fallback_name=fname)
        fname = self._set_final_file_ext(fid, ext)
        self.reload_map[imgurl][1] = fname

        # if a same file exists
        # assuming that file is downloaded by other means
        # for example, another instance of xehentai
        # or user just downloaded herself, by dragging from browser

        fn = os.path.join(fpath, fname)
        if os.path.exists(fn):
            os.remove(fn)

        # create a femp file first
        # we don't need _f_lock because this will not be in a sequence
        # and we can't do that otherwise we are breaking the multi threading
        fn_tmp = os.path.join(fpath, ".%s.xeh" % fname)
        try:
            with open(fn_tmp, "wb") as f:
                for binary in binary_iter():
                    if self._monitor._exit(None):
                        raise DownloadAbortedException()
                    f.write(binary)
        except DownloadAbortedException as ex:
            os.remove(fn_tmp)
            return

        self._f_lock.acquire()
        try:
            os.rename(fn_tmp, fn)
            self._cnt_lock.acquire()
            self.meta.finished += 1
            self._cnt_lock.release()
            if imgurl in self.filehash_map:
                for _fid, _ in self.filehash_map[imgurl]:
                    # if a file download is interrupted, it will appear in self.filehash_map as well
                    if int(_fid) == int(fid):
                        continue
                    rep_name = self._set_final_file_ext(_fid, ext)
                    fn_rep = os.path.join(fpath, rep_name)
                    if not fn == fn_rep:
                        shutil.copyfile(fn, fn_rep)
                        self._cnt_lock.acquire()
                        self.meta.finished += 1
                        self._cnt_lock.release()
                del self.filehash_map[imgurl]
        except Exception as ex:
            self._f_lock.release()
            raise ex

        self._f_lock.release()
        return True

    def get_fname(self, imgurl):
        pageurl, fname = self.reload_map[imgurl]
        _, fid = RE_GALLERY.findall(pageurl)[0]
        return int(fid), self.fid_2_file_name_map.get(fid, self.get_fidpad(fid))

    def get_task_dir(self) -> str:
        """
        Gets file path for the task.
        If the download is not done, this is the folder path for downloading files.
        If the download is done, this is the zip file path without extension.
        Uses 9-digit padded gallery_id and splits into 3+3 directory structure to avoid too many files in one folder.
        """
        gallery_id = str(self.gid if hasattr(self, 'gid') else 'unknown')
        # Only pad if gallery_id is all digits
        if gallery_id.isdigit():
            id_str = gallery_id.zfill(9)
            dir1 = id_str[:3]
            dir2 = id_str[3:6]
            base_dir = os.path.join(self.config['dir'], dir1, dir2)
            folder_name = f"{gallery_id} - {util.legalpath(self.meta.title)}"
            return os.path.join(base_dir, folder_name)
        else:
            # fallback for unknown/non-numeric id
            return os.path.join(self.config['dir'], f"{gallery_id} - {util.legalpath(self.meta.title)}")

    def get_fidpad(self, fid, ext='.jpg'):
        if fid in self.fid_2_file_ext_map:
            ext = self.fid_2_file_ext_map[fid]
        return self._build_fid_file_name(fid, ext)

    def make_archive(self, remove=True):
        dpath = self.get_task_dir()
        arc = "%s.zip" % dpath
        if os.path.exists(arc):
            # [s]when truncated images not exist, the zip file is considered fully downloaded[\s]
            # [s]but tags still need  update[\s]
            # in fact you can not edit the comment without rezip files, just leave it
            nochange = True
            with zipfile.ZipFile(arc, 'r') as zipfile_target:
                if zipfile_target.comment == self.encode_meta():
                    return arc
                else:
                    zipfile_target.extractall(dpath)

        with zipfile.ZipFile(arc, 'w') as zipfile_target:
            # zip comment created
            # store json info in respective zip file
            # thus metadata can be packed with comic it self in a single file
            zipfile_target.comment = self.encode_meta()

            for _i in range(1, len(self.fid_2_file_ext_map)+1):
                t_fid = "%d" % _i
                _f_name = self.get_fidpad(t_fid)
                full_path = os.path.join(dpath, _f_name)
                zipfile_target.write(full_path, _f_name, zipfile.ZIP_STORED)

        if remove:
            self._f_lock.acquire()
            shutil.rmtree(dpath)
            self._f_lock.release()
        return arc

    def from_dict(self, j):
        for k in self.__dict__:
            if k not in j:
                continue
            if k == 'meta':
                setattr(self, k, GalleryMeta.from_dict(j[k]))
                continue
            if k.endswith('_q') and j[k]:
                setattr(self, k, Queue())
                [getattr(self, k).put(e, False) for e in j[k]]
            else:
                setattr(self, k, j[k])
        _ = RE_INDEX.findall(self.url)
        if _:
            self.gid, self.sethash = _[0]
        return self

    def to_dict(self):
        d = dict({k: v for k, v in self.__dict__.items()
                  if not k.endswith('_q') and not k.startswith("_")})
        d['meta'] = self.meta.to_dict()
        for k in ['img_q', 'page_q', 'list_q']:
            if getattr(self, k):
                d[k] = [e for e in getattr(self, k).queue]
        return d
