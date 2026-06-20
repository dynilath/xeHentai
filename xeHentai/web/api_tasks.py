"""Task CRUD REST endpoints."""

import traceback
from typing import List, Optional
from fastapi import APIRouter, HTTPException, Query, Request

from .models import (
    TaskCreateRequest,
    TaskBulkRequest,
    TaskListParams,
    TaskRetryRequest,
    TaskItemResponse,
    TaskDetailResponse,
    ImageInfo,
    PaginatedResponse,
    SuccessResponse,
)

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


def _parse_states(states_str: Optional[str]) -> Optional[List[int]]:
    """Parse comma-separated state string into list of ints."""
    if not states_str:
        return None
    result = []
    for part in states_str.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            result.append(int(part))
        except ValueError:
            # Legacy string level — look up the constant directly
            from ..const import (
                TASK_STATE_FINISHED, TASK_STATE_FAILED, TASK_STATE_PAUSED,
                TASK_STATE_WAITING, TASK_STATE_DOWNLOAD,
            )
            _map = {
                "finished": TASK_STATE_FINISHED, "failed": TASK_STATE_FAILED,
                "paused": TASK_STATE_PAUSED, "waiting": TASK_STATE_WAITING,
                "download": TASK_STATE_DOWNLOAD,
            }
            mapped = _map.get(part.strip().lower())
            if mapped is not None:
                result.append(mapped)
    return result or None


def _parse_tags(tags_str: Optional[str]) -> Optional[List[str]]:
    """Parse comma-separated tag string into list of strings."""
    if not tags_str:
        return None
    result = [t.strip() for t in tags_str.split(",") if t.strip()]
    return result or None


def _task_to_item(task_dict: dict) -> TaskItemResponse:
    return TaskItemResponse(
        guid=task_dict.get("guid", ""),
        gid=task_dict.get("gid", ""),
        url=task_dict.get("url", ""),
        phase_state=task_dict.get("phase_state", 0),
        title=task_dict.get("title", "") or "",
        total=task_dict.get("total", 0),
        done=task_dict.get("done", 0),
    )


@router.get("", response_model=PaginatedResponse)
async def list_tasks(
    request: Request,
    states: Optional[str] = Query(None, description="Comma-separated phase_state ints"),
    tags: Optional[str] = Query(None, description="Comma-separated tags (OR match)"),
    gid: Optional[str] = Query(None),
    url: Optional[str] = Query(None),
    q: Optional[str] = Query(None, description="Space-separated search terms (title + tags, ALL must match)"),
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    order_by: str = Query("updated_at"),
    order_dir: str = Query("DESC", pattern="^(ASC|DESC)$"),
):
    """List tasks with optional filtering and pagination."""
    xeH = request.app.state.xeH
    parsed_states = _parse_states(states)
    parsed_tags = _parse_tags(tags)

    from .. import session_store

    total, rows = session_store.query_tasks(
        states=parsed_states,
        tags=parsed_tags,
        gid=gid,
        url=url,
        q=q,
        offset=offset,
        limit=limit,
        order_by=order_by,
        order_dir=order_dir,
    )

    items = []
    for row in rows:
        guid = row.get("guid", "")
        item = {
            "guid": guid,
            "gid": row.get("gid", ""),
            "url": row.get("url", ""),
            "phase_state": int(row.get("phase_state", 0)),
            "title": row.get("title", "") or "",
            "total": int(row.get("total", 0) or 0),
            "done": 0,
        }
        active = xeH._get_active_task(guid)
        if active is not None:
            item["done"] = len(active._flist_done)
            item["phase_state"] = active.state
            item["total"] = active.meta.total if active.meta else item["total"]
        items.append(_task_to_item(item))

    return PaginatedResponse(total=total, items=items)


@router.post("", response_model=SuccessResponse, status_code=201)
async def create_task(body: TaskCreateRequest, request: Request):
    """Add a new download task."""
    xeH = request.app.state.xeH
    cfg_overrides = body.model_dump(exclude_none=True)
    url = cfg_overrides.pop("url")

    ret, guid = xeH.add_task(url, **cfg_overrides)
    if ret != 0:
        raise HTTPException(status_code=400, detail=f"Failed to add task (code={ret})")
    return SuccessResponse(message="Task created", guid=guid)


@router.post("/bulk", response_model=SuccessResponse, status_code=201)
async def create_tasks_bulk(body: TaskBulkRequest, request: Request):
    """Add multiple download tasks in bulk."""
    xeH = request.app.state.xeH
    cfg_overrides = body.model_dump(exclude_none=True)
    urls = cfg_overrides.pop("urls")

    ret, guids = xeH.add_tasks(urls, **cfg_overrides)
    if ret != 0:
        raise HTTPException(status_code=400, detail=f"Failed to add tasks (code={ret})")
    return SuccessResponse(message=f"{len(guids)} tasks created")


@router.post("/retry", response_model=SuccessResponse)
async def retry_tasks(body: TaskRetryRequest, request: Request):
    """Retry failed tasks matching the given criteria."""
    xeH = request.app.state.xeH
    ret, _ = xeH.retry_tasks(guid=body.guid, gid=body.gid, url=body.url)
    if ret != 0:
        raise HTTPException(status_code=400, detail=f"Retry failed (code={ret})")
    return SuccessResponse(message="Tasks retried")


@router.get("/{guid}", response_model=TaskDetailResponse)
async def get_task(guid: str, request: Request):
    """Get full task detail by GUID."""
    xeH = request.app.state.xeH

    # Try active first, then hydrate from DB
    task = xeH._get_active_task(guid)
    cold = task is None
    if cold:
        task = xeH._hydrate_task(guid)

    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")

    try:
        return TaskDetailResponse(
            guid=task.guid,
            gid=task.gid,
            url=task.url,
            phase_state=task.state,
            title=task.meta.title if task.meta else "",
            title_japanese=task.meta.title_japanese if task.meta else None,
            total=task.meta.total if task.meta else 0,
            done=len(task._flist_done),
            tags=task.meta.tags if task.meta else [],
            newer_versions=task.meta.newer_versions if task.meta else [],
            make_archive=task.config.get("make_archive", False),
            download_ori=task.config.get("download_ori", False),
        )
    finally:
        if cold:
            xeH._dehydrate_task(guid)


@router.delete("/{guid}", response_model=SuccessResponse)
async def delete_task(guid: str, request: Request):
    """Delete a task. Cannot delete a running task."""
    xeH = request.app.state.xeH

    # Check if running
    tc = xeH._task_control
    if guid in tc._running_set:
        raise HTTPException(status_code=409, detail="Cannot delete a running task")

    xeH.del_task(guid)
    return SuccessResponse(message="Task deleted")


@router.post("/{guid}/pause", response_model=SuccessResponse)
async def pause_task(guid: str, request: Request):
    """Pause a task."""
    xeH = request.app.state.xeH
    ret = xeH.pause_task(guid)
    if ret != 0:
        raise HTTPException(status_code=400, detail=f"Failed to pause task (code={ret})")
    return SuccessResponse(message="Task paused")


@router.post("/{guid}/resume", response_model=SuccessResponse)
async def resume_task(guid: str, request: Request):
    """Resume a paused task."""
    xeH = request.app.state.xeH
    ret = xeH.resume_task(guid)
    if ret != 0:
        raise HTTPException(status_code=400, detail=f"Failed to resume task (code={ret})")
    return SuccessResponse(message="Task resumed")


@router.get("/{guid}/images", response_model=List[ImageInfo])
async def get_task_images(guid: str, request: Request):
    """Get list of image URLs for a task (content-addressed URLs)."""
    xeH = request.app.state.xeH

    task = xeH._get_active_task(guid)
    cold = task is None
    if cold:
        task = xeH._hydrate_task(guid)

    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")

    try:
        total = task.meta.total if task.meta else 0
        if total == 0:
            return []

        images: List[ImageInfo] = []
        zfill_width = len(str(total))

        for fid in range(1, total + 1):
            fid_str = str(fid).zfill(zfill_width)
            file_hash = task.fid_2_img_hash_map.get(str(fid), "")
            file_name = task.fid_2_file_name_map.get(str(fid), "")

            if not file_name or not file_hash:
                continue

            ext = file_name.rsplit(".", 1)[-1] if "." in file_name else "jpg"
            url = f"/api/img/{task.gid}/{fid_str}-{file_hash}.{ext}"

            images.append(ImageInfo(
                fid=fid_str,
                url=url,
                file_name=file_name,
                file_hash=file_hash,
            ))

        return images
    finally:
        if cold:
            xeH._dehydrate_task(guid)
