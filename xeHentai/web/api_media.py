"""Content-addressed image serving and archive download endpoints."""

import os
import zipfile
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from starlette.responses import Response

router = APIRouter(tags=["media"])

IMMUTABLE_CACHE = "public, max-age=31536000, immutable"
MIME_MAP = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "gif": "image/gif",
    "bmp": "image/bmp",
    "webp": "image/webp",
}


@router.get("/api/img/{gid}/{fid_and_hash}")
async def get_image(gid: str, fid_and_hash: str, request: Request):
    """Serve an image file by gallery ID and content hash.

    URL format: /api/img/{gid}/{fid}-{file_hash}.{ext}

    The file_hash in the URL is the image content hash, enabling immutable
    browser caching. If the hash doesn't match, returns 404.
    """
    xeH = request.app.state.xeH

    # Parse fid_and_hash: e.g. "0001-abc123def4.jpg"
    try:
        name_part, ext = fid_and_hash.rsplit(".", 1)
        fid, file_hash = name_part.rsplit("-", 1)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid image URL format")

    ext = ext.lower()
    if ext not in MIME_MAP:
        raise HTTPException(status_code=400, detail=f"Unsupported image format: {ext}")

    # Look up task by gid
    from .. import session_store
    guid = session_store.find_guid_by_gid(gid)
    if not guid:
        raise HTTPException(status_code=404, detail="Gallery not found")

    task = xeH._get_active_task(guid)
    cold = task is None
    if cold:
        task = xeH._hydrate_task(guid)

    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")

    try:
        # Get the actual fid (strip leading zeros for lookup)
        lookup_fid = str(int(fid)) if fid.lstrip("0") else fid

        # Validate file_hash matches (skip if map is empty — rebuild from zip)
        expected_hash = task.fid_2_img_hash_map.get(lookup_fid, "")
        if not expected_hash and not task.fid_2_file_name_map:
            task.rebuild_file_maps_from_zip()
            expected_hash = task.fid_2_img_hash_map.get(lookup_fid, "")
        if expected_hash and expected_hash != file_hash:
            raise HTTPException(status_code=404, detail="Image hash mismatch (stale URL)")

        # Find the file
        file_name = task.fid_2_file_name_map.get(lookup_fid, "")
        task_dir = task.get_task_dir()

        if not file_name:
            # Fallback: try to find by fid prefix in task dir
            if os.path.isdir(task_dir):
                for f in sorted(os.listdir(task_dir)):
                    if f.startswith(fid.zfill(len(fid))):
                        file_name = f
                        break

        if not file_name:
            raise HTTPException(status_code=404, detail="Image file not found")

        file_path = os.path.join(task_dir, file_name)

        if os.path.isfile(file_path):
            return FileResponse(
                file_path,
                media_type=MIME_MAP.get(ext, "application/octet-stream"),
                headers={"Cache-Control": IMMUTABLE_CACHE},
            )

        # Try from archive zip
        zip_path = f"{task_dir}.zip"
        if os.path.isfile(zip_path):
            try:
                with zipfile.ZipFile(zip_path, "r") as zf:
                    data = zf.read(file_name)
                return Response(
                    content=data,
                    media_type=MIME_MAP.get(ext, "application/octet-stream"),
                    headers={"Cache-Control": IMMUTABLE_CACHE},
                )
            except KeyError:
                pass

        raise HTTPException(status_code=404, detail="Image file not found on disk")
    finally:
        if cold:
            xeH._dehydrate_task(guid)


def _archive_content_disposition(title: str, gid: str) -> str:
    """Return a browser-safe Content-Disposition for archive downloads."""
    raw_name = str(title or gid).strip() or str(gid)
    fallback_name = "".join(
        ch if ch.isascii() and (ch.isalnum() or ch in " _.-") else "_"
        for ch in raw_name
    ).strip(" ._")[:100]
    fallback_name = fallback_name or gid
    if not fallback_name.lower().endswith(".zip"):
        fallback_name = f"{fallback_name}.zip"

    utf8_name = quote(raw_name, safe="!#$&+-.^_`|~")
    if not utf8_name.lower().endswith(".zip"):
        utf8_name = f"{utf8_name}.zip"
    return f"attachment; filename=\"{fallback_name}\"; filename*=UTF-8''{utf8_name}"


@router.get("/api/archive/{gid}")
async def get_archive(gid: str, request: Request):
    """Download a gallery archive zip by gallery ID."""
    xeH = request.app.state.xeH

    from .. import session_store
    guid = session_store.find_guid_by_gid(gid)
    if not guid:
        raise HTTPException(status_code=404, detail="Gallery not found")

    task = xeH._get_active_task(guid)
    cold = task is None
    if cold:
        task = xeH._hydrate_task(guid)

    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")

    try:
        zip_path = f"{task.get_task_dir()}.zip"
        if not os.path.isfile(zip_path):
            # Try to create it on the fly
            try:
                zip_path, _ = task.make_archive(remove=False)
            except Exception:
                raise HTTPException(status_code=404, detail="Archive not available")

        if not os.path.isfile(zip_path):
            raise HTTPException(status_code=404, detail="Archive not available")

        title = task.meta.title if task.meta else gid
        return FileResponse(
            zip_path,
            media_type="application/zip",
            headers={"Content-Disposition": _archive_content_disposition(title, gid)},
        )
    finally:
        if cold:
            xeH._dehydrate_task(guid)
