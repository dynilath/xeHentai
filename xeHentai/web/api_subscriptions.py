"""Gallery subscription REST endpoints."""

import re
from typing import List, Optional
from fastapi import APIRouter, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from .. import session_store
from ..const import RESTR_SITE, RE_INDEX
from .models import (
    SubscriptionCreateRequest,
    SubscriptionItemResponse,
    SuccessResponse,
)

router = APIRouter(prefix="/api/subscriptions", tags=["subscriptions"])


def _parse_gallery_url(url: str):
    """Validate a gallery URL and extract (gid, sethash). Returns None if invalid."""
    url = (url or "").strip()
    if not re.match(r"^%s/[^/]+/\d+/[^/]+/*#*$" % RESTR_SITE, url):
        return None
    matches = RE_INDEX.findall(url)
    if not matches:
        return None
    gid, sethash = matches[0]
    return url, str(gid), str(sethash)


def _to_item(row: dict) -> SubscriptionItemResponse:
    task_guid = session_store.find_guid_by_gid(str(row.get("gid", "")))
    return SubscriptionItemResponse(
        id=int(row.get("id", 0)),
        gid=str(row.get("gid", "")),
        url=str(row.get("url", "")),
        title=str(row.get("title", "") or ""),
        enabled=bool(row.get("enabled", True)),
        last_check_at=row.get("last_check_at"),
        next_check_at=int(row.get("next_check_at", 0) or 0),
        last_status=str(row.get("last_status", "") or ""),
        last_error=str(row.get("last_error", "") or ""),
        last_new_version_url=str(row.get("last_new_version_url", "") or ""),
        version_count=int(row.get("version_count", 0) or 0),
        created_at=int(row.get("created_at", 0) or 0),
        task_guid=task_guid,
    )


@router.get("", response_model=List[SubscriptionItemResponse])
async def list_subscriptions(request: Request):
    """List all gallery subscriptions."""
    return [_to_item(row) for row in session_store.list_subscriptions()]


@router.post("", response_model=SuccessResponse, status_code=201)
async def create_subscription(body: SubscriptionCreateRequest, request: Request):
    """Subscribe to a gallery. The first check is scheduled immediately."""
    xeH = request.app.state.xeH
    parsed = _parse_gallery_url(body.url)
    if parsed is None:
        raise HTTPException(status_code=400, detail="Invalid gallery URL")
    url, gid, sethash = parsed

    existing = session_store.get_subscription_by_gid(gid)
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail="Gallery already subscribed (id=%d)" % existing["id"],
        )

    row = session_store.add_subscription(
        gid, url, sethash=sethash, next_check_at=0
    )
    if row is None:
        raise HTTPException(status_code=409, detail="Gallery already subscribed")

    xeH._subscriptions.wake()
    return SuccessResponse(message="Subscription created", guid=str(row["id"]))


@router.delete("/{sub_id}", response_model=SuccessResponse)
async def delete_subscription(sub_id: int, request: Request):
    """Delete a subscription (does not touch the downloaded tasks)."""
    if not session_store.delete_subscription(sub_id):
        raise HTTPException(status_code=404, detail="Subscription not found")
    return SuccessResponse(message="Subscription deleted")


@router.post("/{sub_id}/check", response_model=SuccessResponse)
async def check_subscription(sub_id: int, request: Request):
    """Run a check for a subscription now and wait for it to finish."""
    xeH = request.app.state.xeH
    status = await run_in_threadpool(xeH._subscriptions.check_now_sync, sub_id)
    if status is None:
        raise HTTPException(status_code=404, detail="Subscription not found")
    return SuccessResponse(message="Check complete: %s" % status)


@router.post("/{sub_id}/pause", response_model=SuccessResponse)
async def pause_subscription(sub_id: int, request: Request):
    """Pause periodic checks for a subscription."""
    if not session_store.update_subscription_fields(sub_id, {"enabled": 0}):
        raise HTTPException(status_code=404, detail="Subscription not found")
    return SuccessResponse(message="Subscription paused")


@router.post("/{sub_id}/resume", response_model=SuccessResponse)
async def resume_subscription(sub_id: int, request: Request):
    """Resume periodic checks for a subscription (next check runs soon)."""
    xeH = request.app.state.xeH
    if not session_store.update_subscription_fields(
        sub_id, {"enabled": 1, "next_check_at": 0}
    ):
        raise HTTPException(status_code=404, detail="Subscription not found")
    xeH._subscriptions.wake()
    return SuccessResponse(message="Subscription resumed")
