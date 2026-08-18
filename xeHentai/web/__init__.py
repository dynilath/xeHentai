"""FastAPI application factory and uvicorn server lifecycle for xeHentai WebUI.

Provides `create_app(xeH)` to build the FastAPI app with all REST routers,
WebSocket endpoint, and UI page routes. `WebServer` wraps uvicorn in a daemon
thread, replacing the legacy RPCServer.
"""

from __future__ import annotations

import asyncio
import os
import threading
from typing import List, Optional, TYPE_CHECKING
from urllib.parse import quote
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader

from xeHentai.const import TASK_STATE_FINISHED

if TYPE_CHECKING:
    from ..host import HostProtocol

from .api_tasks import router as tasks_router
from .api_config import router as config_router
from .api_system import router as system_router
from .api_media import router as media_router
from .api_subscriptions import router as subscriptions_router
from .userscript import router as userscript_router
from .ws import manager

# Template directory relative to this file
_TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), "templates")
_jinja_env = Environment(
    loader=FileSystemLoader(_TEMPLATE_DIR),
    auto_reload=True,      # reload on change, also disables bytecode cache
    cache_size=0,          # belt and suspenders
)


def _render(name: str, context: dict) -> HTMLResponse:
    """Render a Jinja2 template to an HTMLResponse, bypassing Starlette's
    Jinja2Templates which can trigger unhashable-key TypeError in Jinja2's
    global-key-based template cache."""
    template = _jinja_env.get_template(name)
    return HTMLResponse(template.render(**context))


def create_app(xeH: HostProtocol) -> FastAPI:
    """Build the FastAPI application with all routers and state."""
    app = FastAPI(
        title="xeHentai WebUI",
        version=xeH.verstr,
        docs_url="/api/docs",
        redoc_url=None,
    )

    # Store xeH reference for route handlers
    app.state.xeH = xeH

    # REST API routers
    app.include_router(tasks_router)
    app.include_router(config_router)
    app.include_router(system_router)
    app.include_router(media_router)
    app.include_router(subscriptions_router)
    app.include_router(userscript_router)

    # Static files
    static_dir = os.path.join(os.path.dirname(__file__), "static")
    if os.path.isdir(static_dir):
        app.mount("/static", StaticFiles(directory=static_dir), name="static")

    # ── WebSocket endpoint ──────────────────────────────────────────────
    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket):
        await manager.connect(ws)
        try:
            while True:
                # Keep connection alive, handle incoming messages if needed
                await ws.receive_text()
        except WebSocketDisconnect:
            manager.disconnect(ws)
        except Exception:
            manager.disconnect(ws)

    # ── UI Page Routes ──────────────────────────────────────────────────

    def _state_name(state: int) -> str:
        from ..const import (
            TASK_STATE_PAUSED, TASK_STATE_WAITING, TASK_STATE_GET_META,
            TASK_STATE_SCAN_PAGE, TASK_STATE_SCAN_IMG, TASK_STATE_SCAN_ARCHIVE,
            TASK_STATE_DOWNLOAD, TASK_STATE_MAKE_ARCHIVE, TASK_STATE_FINISHED,
            TASK_STATE_FAILED, TASK_STATE_ERR_URL_NOT_RECOGNIZED,
            TASK_STATE_ERR_CANT_DOWNLOAD_EXH, TASK_STATE_ERR_ONLY_VISIBLE_EXH,
            TASK_STATE_ERR_GALLERY_REMOVED, TASK_STATE_ERR_QUOTA_EXCEEDED,
            TASK_STATE_ERR_KEY_EXPIRED, TASK_STATE_ERR_NO_PAGEURL_FOUND,
            TASK_STATE_ERR_CONNECTION_ERROR, TASK_STATE_ERR_IP_BANNED,
            TASK_STATE_ERR_IMAGE_BROKEN, TASK_STATE_ERR_GALLERY_NOT_FOUND,
            TASK_STATE_ERR_CANNOT_MAKE_ARCHIVE,
        )
        names = {
            TASK_STATE_PAUSED: "paused",
            TASK_STATE_WAITING: "waiting",
            TASK_STATE_GET_META: "getting meta",
            TASK_STATE_SCAN_PAGE: "scanning page",
            TASK_STATE_SCAN_IMG: "scanning images",
            TASK_STATE_SCAN_ARCHIVE: "scanning archive",
            TASK_STATE_DOWNLOAD: "downloading",
            TASK_STATE_MAKE_ARCHIVE: "making archive",
            TASK_STATE_FINISHED: "finished",
            TASK_STATE_FAILED: "failed",
            TASK_STATE_ERR_URL_NOT_RECOGNIZED: "url not recognized",
            TASK_STATE_ERR_CANT_DOWNLOAD_EXH: "cant download exh",
            TASK_STATE_ERR_ONLY_VISIBLE_EXH: "only visible exh",
            TASK_STATE_ERR_GALLERY_REMOVED: "gallery removed",
            TASK_STATE_ERR_QUOTA_EXCEEDED: "quota exceeded",
            TASK_STATE_ERR_KEY_EXPIRED: "key expired",
            TASK_STATE_ERR_NO_PAGEURL_FOUND: "no page url",
            TASK_STATE_ERR_CONNECTION_ERROR: "connection error",
            TASK_STATE_ERR_IP_BANNED: "ip banned",
            TASK_STATE_ERR_IMAGE_BROKEN: "image broken",
            TASK_STATE_ERR_GALLERY_NOT_FOUND: "gallery not found",
            TASK_STATE_ERR_CANNOT_MAKE_ARCHIVE: "cannot archive",
        }
        return names.get(state, f"error {state}" if state < 0 else f"state {state}")

    def _state_css(state: int) -> str:
        from ..const import TASK_STATE_FINISHED, TASK_STATE_FAILED, TASK_STATE_PAUSED
        if state == TASK_STATE_FINISHED:
            return "bg-green-100 text-green-800"
        if state < 0 or state == TASK_STATE_FAILED:
            return "bg-red-100 text-red-800"
        if state == TASK_STATE_PAUSED:
            return "bg-yellow-100 text-yellow-800"
        return "bg-blue-100 text-blue-800"

    def _fmt_ts(ts) -> str:
        import time as _time
        if not ts:
            return "—"
        try:
            return _time.strftime("%Y-%m-%d %H:%M", _time.localtime(int(ts)))
        except (TypeError, ValueError):
            return "—"

    def _sub_status(sub: dict) -> tuple[str, str]:
        """(label, css) for a subscription row's display status."""
        if not sub.get("enabled", True):
            return "paused", "bg-gray-100 text-gray-600"
        return {
            "": ("pending", "bg-gray-100 text-gray-600"),
            "ok": ("up to date", "bg-green-100 text-green-800"),
            "new_version": ("new version found", "bg-yellow-100 text-yellow-800"),
            "removed": ("gallery removed", "bg-red-100 text-red-800"),
            "not_found": ("not found", "bg-red-100 text-red-800"),
            "exh_only": ("exh only", "bg-orange-100 text-orange-800"),
            "error": ("error", "bg-red-100 text-red-800"),
        }.get(str(sub.get("last_status", "")), (str(sub.get("last_status", "")) or "unknown", "bg-gray-100 text-gray-600"))

    @app.get("/", response_class=HTMLResponse)
    async def ui_dashboard(request: Request):
        xeH = request.app.state.xeH
        tc = xeH._task_control
        from .. import session_store

        # Recent tasks (last 10)
        _, recent_rows = session_store.query_tasks(
            limit=10, order_by="updated_at", order_dir="DESC"
        )
        recent = []
        for row in recent_rows:
            guid = row.get("guid", "")
            state = int(row.get("phase_state", 0))
            recent.append({
                "guid": guid,
                "gid": row.get("gid", ""),
                "title": row.get("title", "") or row.get("url", ""),
                "state": state,
                "state_name": _state_name(state),
                "state_css": _state_css(state),
                "total": int(row.get("total", 0) or 0),
            })

        ctx = {
            "current_path": str(request.url.path),
            "version": xeH.verstr,
            "waiting_count": len(tc._waiting_set),
            "processing_count": len(tc._running_set),
            "proxy_enabled": xeH.proxy is not None,
            "proxy_count": len(xeH.proxy.proxies) if xeH.proxy else 0,
            "recent_tasks": recent,
        }

        # htmx requests: return just the content block
        if request.headers.get("HX-Request"):
            return _render("dashboard_partial.html.j2", ctx)

        return _render("dashboard.html.j2", ctx)

    @app.get("/tasks", response_class=HTMLResponse)
    async def ui_task_list(
        request: Request,
        states: Optional[List[str]] = Query(None),
        gid: Optional[str] = Query(None),
        f_search: Optional[str] = Query(None, description="Search title + tags"),
        offset: int = Query(0),
        limit: int = Query(20),
        order_by: str = Query("gid"),
        order_dir: str = Query("DESC", pattern="^(ASC|DESC)$"),
    ):
        xeH = request.app.state.xeH
        from .. import session_store
        from ..const import TASK_STATE_FINISHED
        from .api_tasks import _parse_states

        if order_by not in ("gid", "updated_at"):
            order_by = "gid"
        if str(order_dir).upper() not in ("ASC", "DESC"):
            order_dir = "DESC"

        # Multi-select checkboxes submit ?states=1&states=-1 (multiple params),
        # while pagination links use ?states=1,-1 (comma-joined). Normalize both.
        states_str = ",".join(states) if states else None
        parsed_states = _parse_states(states_str)
        if f_search:
            f_search = f_search.replace('$', '')
        total, rows = session_store.query_tasks(
            states=parsed_states, gid=gid, q=f_search,
            offset=offset, limit=limit,
            order_by=order_by, order_dir=order_dir,
        )

        items = []
        retryable_guids = []
        for row in rows:
            guid = row.get("guid", "")
            state = int(row.get("phase_state", 0))
            total_pages = int(row.get("total", 0) or 0)
            done = 0
            active = xeH._get_active_task(guid)
            if active is not None:
                done = len(active._flist_done)
                state = active.state
                total_pages = active.meta.total if active.meta else total_pages
            elif state == TASK_STATE_FINISHED:
                done = total_pages

            if state < 0:
                retryable_guids.append(guid)

            items.append({
                "guid": guid,
                "gid": row.get("gid", ""),
                "url": row.get("url", ""),
                "title": row.get("title", "") or row.get("url", ""),
                "state": state,
                "state_name": _state_name(state),
                "state_css": _state_css(state),
                "total": total_pages,
                "done": done,
            })

        # Available states for filter dropdown
        from ..const import (
            TASK_STATE_PAUSED, TASK_STATE_WAITING, TASK_STATE_GET_META,
            TASK_STATE_SCAN_PAGE, TASK_STATE_SCAN_IMG, TASK_STATE_DOWNLOAD,
            TASK_STATE_MAKE_ARCHIVE, TASK_STATE_FINISHED, TASK_STATE_FAILED,
            TASK_STATE_ERR_URL_NOT_RECOGNIZED, TASK_STATE_ERR_CANT_DOWNLOAD_EXH,
            TASK_STATE_ERR_ONLY_VISIBLE_EXH, TASK_STATE_ERR_GALLERY_REMOVED,
            TASK_STATE_ERR_QUOTA_EXCEEDED, TASK_STATE_ERR_KEY_EXPIRED,
            TASK_STATE_ERR_NO_PAGEURL_FOUND, TASK_STATE_ERR_CONNECTION_ERROR,
            TASK_STATE_ERR_IP_BANNED, TASK_STATE_ERR_IMAGE_BROKEN,
            TASK_STATE_ERR_GALLERY_NOT_FOUND, TASK_STATE_ERR_CANNOT_MAKE_ARCHIVE,
        )
        available_states = [
            (TASK_STATE_WAITING, "waiting"),
            (TASK_STATE_GET_META, "getting meta"),
            (TASK_STATE_SCAN_PAGE, "scanning page"),
            (TASK_STATE_SCAN_IMG, "scanning images"),
            (TASK_STATE_DOWNLOAD, "downloading"),
            (TASK_STATE_MAKE_ARCHIVE, "making archive"),
            (TASK_STATE_FINISHED, "finished"),
            (TASK_STATE_FAILED, "failed"),
            (TASK_STATE_PAUSED, "paused"),
            (TASK_STATE_ERR_URL_NOT_RECOGNIZED, "err: url not recognized"),
            (TASK_STATE_ERR_CANT_DOWNLOAD_EXH, "err: cant download exh"),
            (TASK_STATE_ERR_ONLY_VISIBLE_EXH, "err: only visible exh"),
            (TASK_STATE_ERR_GALLERY_REMOVED, "err: gallery removed"),
            (TASK_STATE_ERR_QUOTA_EXCEEDED, "err: quota exceeded"),
            (TASK_STATE_ERR_KEY_EXPIRED, "err: key expired"),
            (TASK_STATE_ERR_NO_PAGEURL_FOUND, "err: no page url"),
            (TASK_STATE_ERR_CONNECTION_ERROR, "err: connection"),
            (TASK_STATE_ERR_IP_BANNED, "err: ip banned"),
            (TASK_STATE_ERR_IMAGE_BROKEN, "err: image broken"),
            (TASK_STATE_ERR_GALLERY_NOT_FOUND, "err: gallery not found"),
            (TASK_STATE_ERR_CANNOT_MAKE_ARCHIVE, "err: cannot archive"),
        ]

        ctx = {
            "current_path": str(request.url.path),
            "tasks": items,
            "total": total,
            "offset": offset,
            "limit": limit,
            "current_states": states_str or "",
            "current_states_list": [s for s in (states_str or "").split(",") if s],
            "current_gid": gid or "",
            "current_q": f_search or "",
            "current_q_encoded": quote(f_search, safe='') if f_search else "",
            "current_order_by": order_by,
            "current_order_dir": order_dir,
            "retryable_guids": retryable_guids,
            "available_states": available_states,
        }
        if request.headers.get("HX-Request"):
            return _render("task_list_partial.html.j2", ctx)
        return _render("task_list.html.j2", ctx)

    @app.get("/tasks/{guid}/row", response_class=HTMLResponse)
    async def ui_task_row(guid: str, request: Request):
        """Return just the HTML for one task row (used by htmx for in-place refresh)."""
        xeH = request.app.state.xeH
        task = xeH._get_active_task(guid)
        cold = task is None
        if cold:
            task = xeH._hydrate_task(guid)

        if task is None:
            return HTMLResponse("", status_code=404)

        try:
            total = task.meta.total if task.meta else 0
            done = len(task._flist_done)
            # _flist_done is not serialized; cold finished tasks hydrate with done=0
            from ..const import TASK_STATE_FINISHED
            if done == 0 and task.state == TASK_STATE_FINISHED:
                done = total
            return _render("task_row.html.j2", {
                "guid": task.guid,
                "gid": task.gid,
                "url": task.url,
                "title": task.meta.title if task.meta else task.gid,
                "state": task.state,
                "state_name": _state_name(task.state),
                "state_css": _state_css(task.state),
                "total": total,
                "done": done,
            })
        finally:
            if cold:
                xeH._dehydrate_task(guid)

    @app.get("/tasks/{guid}/read", response_class=HTMLResponse)
    async def ui_task_read(guid: str, request: Request,
                            scroll: int = Query(0, description="1=scroll mode"),
                            page: int = Query(1, description="Start at page")):
        """Legacy query-param URL: redirect to the path-based reader URL."""
        target = f"/tasks/{guid}/read/{max(1, page)}"
        if scroll:
            target += "?scroll=1"
        return RedirectResponse(target, status_code=307)

    @app.get("/tasks/{guid}/read/{page:int}", response_class=HTMLResponse)
    async def ui_task_read_page(guid: str, page: int, request: Request,
                                 scroll: int = Query(0, description="1=scroll mode")):
        xeH = request.app.state.xeH
        task = xeH._get_active_task(guid)
        cold = task is None
        if cold:
            task = xeH._hydrate_task(guid)
        if task is None:
            return HTMLResponse("<h1>Task not found</h1>", status_code=404)
        try:
            total = task.meta.total if task.meta else 0
            zfill = len(str(total)) if total > 0 else 1
            # Use stored maps if available, otherwise rebuild from zip
            if not task.fid_2_img_hash_map or not task.fid_2_file_name_map:
                task.rebuild_file_maps_from_zip()
            if not total:
                total = len(task.fid_2_img_hash_map)

            sp = max(1, min(page, total or 1))
            images = []
            win_start = 1
            win_end = total
            if task.fid_2_img_hash_map and task.fid_2_file_name_map:
                if scroll:
                    # Scroll mode: all images (but lazy-loaded)
                    fid_range = range(1, total + 1)
                else:
                    # Comic mode: 20-page batches, shift by 10 pages.
                    # sp=1 → batch 0: pages  1-20
                    # sp=11→ batch 1: pages 11-30  (triggers at page 11)
                    # sp=21→ batch 2: pages 21-40
                    BATCH = 20
                    HALF = 10
                    batch_num = (sp - 1) // HALF
                    win_start = batch_num * HALF + 1
                    win_end = min(total, win_start + BATCH - 1)
                    fid_range = range(win_start, win_end + 1)
                for fid in fid_range:
                    fid_str = str(fid).zfill(zfill)
                    file_hash = task.fid_2_img_hash_map.get(str(fid), "")
                    file_name = task.fid_2_file_name_map.get(str(fid), "")
                    if file_name and file_hash:
                        ext = file_name.rsplit(".", 1)[-1] if "." in file_name else "jpg"
                        images.append({
                            "fid": fid_str,
                            "url": f"/api/img/{task.gid}/{fid_str}-{file_hash}.{ext}",
                        })

            return _render("reader.html.j2", {
                "current_path": str(request.url.path),
                "guid": task.guid,
                "gid": task.gid,
                "title": task.meta.title if task.meta else task.gid,
                "total": total,
                "images": images,
                "scroll_mode": bool(scroll),
                "start_page": sp,
                "win_start": win_start if not scroll else 1,
                "win_end": win_end if not scroll else total,
            })
        finally:
            if cold:
                xeH._dehydrate_task(guid)

    @app.get("/tasks/{guid}/pages", response_class=HTMLResponse)
    async def ui_task_pages(guid: str, request: Request,
                             fr: int = Query(..., alias="from", ge=1),
                             to: int = Query(..., ge=1)):
        """Return page-div HTML fragment for htmx batch preloading in comic mode."""
        xeH = request.app.state.xeH
        task = xeH._get_active_task(guid)
        cold = task is None
        if cold:
            task = xeH._hydrate_task(guid)
        if task is None:
            return HTMLResponse("", status_code=404)
        try:
            total = task.meta.total if task.meta else 0
            zfill = len(str(total)) if total > 0 else 1
            if not task.fid_2_img_hash_map or not task.fid_2_file_name_map:
                task.rebuild_file_maps_from_zip()

            fragments = []
            for fid in range(max(1, fr), min(total, to) + 1):
                fid_str = str(fid).zfill(zfill)
                file_hash = task.fid_2_img_hash_map.get(str(fid), "")
                file_name = task.fid_2_file_name_map.get(str(fid), "")
                if file_name and file_hash:
                    ext = file_name.rsplit(".", 1)[-1] if "." in file_name else "jpg"
                    fragments.append(
                        f'<div id="page-{fid_str}" class="reader-page flex flex-col items-center justify-center min-h-screen hidden">'
                        f'<img src="/api/img/{task.gid}/{fid_str}-{file_hash}.{ext}"'
                        f' alt="Page {fid_str}" class="max-w-full max-h-screen object-contain cursor-pointer select-none"'
                        f' onclick="handleImageClick(event)"'
                        f' draggable="false" />'
                        f'</div>'
                    )
            return HTMLResponse("".join(fragments))
        finally:
            if cold:
                xeH._dehydrate_task(guid)

    @app.get("/tasks/{guid}", response_class=HTMLResponse)
    async def ui_task_detail(guid: str, request: Request):
        xeH = request.app.state.xeH
        task = xeH._get_active_task(guid)
        cold = task is None
        if cold:
            task = xeH._hydrate_task(guid)

        if task is None:
            return HTMLResponse("<h1>Task not found</h1>", status_code=404)

        try:
            total = task.meta.total if task.meta else 0
            zfill = len(str(total)) if total > 0 else 1
            # Rebuild maps from zip for old tasks
            if not task.fid_2_img_hash_map or not task.fid_2_file_name_map:
                task.rebuild_file_maps_from_zip()
            # Build image list (may be large, preview handled in template)
            all_images = []
            for fid in range(1, total + 1):
                fid_str = str(fid).zfill(zfill)
                file_hash = task.fid_2_img_hash_map.get(str(fid), "")
                file_name = task.fid_2_file_name_map.get(str(fid), "")
                if file_name and file_hash:
                    ext = file_name.rsplit(".", 1)[-1] if "." in file_name else "jpg"
                    all_images.append({
                        "fid": fid_str,
                        "url": f"/api/img/{task.gid}/{fid_str}-{file_hash}.{ext}",
                        "file_name": file_name,
                    })

            # Enrich newer_versions with task guids for linking
            from .. import session_store
            nv_enriched = []
            for nv in (task.meta.newer_versions if task.meta else []):
                nv_guid = session_store.find_guid_by_gid(str(nv.get("gid", "")))
                nv_enriched.append({**nv, "guid": nv_guid})

            # Subscription state for the subscribe/unsubscribe button
            sub_row = session_store.get_subscription_by_gid(str(task.gid))
            subscription_id = int(sub_row["id"]) if sub_row else None

            # Group tags by prefix. Each entry carries the display label and
            # the full namespace:value tag (URL-encoded) so the template can
            # link it to /tasks?f_search=<tag> for tag-based search.
            # Tags containing spaces are double-quoted so the search parser
            # keeps them as a single term (still exact-matched via task_tags).
            _tag_categories = [
                "language", "parody", "character", "cosplayer",
                "group", "artist", "male", "female", "mixed",
            ]
            _tag_groups = {c: [] for c in _tag_categories}
            _tag_groups["other"] = []
            for tag in (task.meta.tags if task.meta else []):
                tag_str = str(tag)
                search_tag = '"%s"' % tag_str if " " in tag_str else tag_str
                placed = False
                for cat in _tag_categories:
                    prefix = cat + ":"
                    if tag_str.startswith(prefix):
                        _tag_groups[cat].append({
                            "label": tag_str[len(prefix):],
                            "search": quote(search_tag, safe=''),
                        })
                        placed = True
                        break
                if not placed:
                    _tag_groups["other"].append({
                        "label": tag_str,
                        "search": quote(search_tag, safe=''),
                    })

            # Get updated_at for finished tasks
            finished_at = ""
            if task.state == TASK_STATE_FINISHED:
                row = session_store.get_task_row(guid)
                if row:
                    finished_at = row.get("updated_at", "") or ""

            return _render("task_detail.html.j2", {
                "current_path": str(request.url.path),
                "guid": task.guid,
                "gid": task.gid,
                "url": task.url,
                "state": task.state,
                "state_name": _state_name(task.state),
                "state_css": _state_css(task.state),
                "title": task.meta.title if task.meta else "",
                "title_japanese": task.meta.title_japanese if task.meta else "",
                "total": total,
                "done": len(task._flist_done),
                "finished_at": finished_at,
                "tag_groups": _tag_groups,
                "newer_versions": nv_enriched,
                "subscription_id": subscription_id,
                "subscribed": subscription_id is not None,
                "all_images": all_images,
                "image_offset": 0,
                "page_size": 18,
            })
        finally:
            if cold:
                xeH._dehydrate_task(guid)

    @app.get("/tasks/{guid}/images", response_class=HTMLResponse)
    async def ui_task_images(guid: str, request: Request,
                              offset: int = Query(0, ge=0)):
        """Return a page of the image grid fragment for htmx pagination."""
        xeH = request.app.state.xeH
        task = xeH._get_active_task(guid)
        cold = task is None
        if cold:
            task = xeH._hydrate_task(guid)
        if task is None:
            return HTMLResponse("", status_code=404)
        try:
            total = task.meta.total if task.meta else 0
            zfill = len(str(total)) if total > 0 else 1
            if not task.fid_2_img_hash_map or not task.fid_2_file_name_map:
                task.rebuild_file_maps_from_zip()
            all_images = []
            for fid in range(1, total + 1):
                fid_str = str(fid).zfill(zfill)
                file_hash = task.fid_2_img_hash_map.get(str(fid), "")
                file_name = task.fid_2_file_name_map.get(str(fid), "")
                if file_name and file_hash:
                    ext = file_name.rsplit(".", 1)[-1] if "." in file_name else "jpg"
                    all_images.append({
                        "fid": fid_str,
                        "url": f"/api/img/{task.gid}/{fid_str}-{file_hash}.{ext}",
                    })
            return _render("task_images.html.j2", {
                "guid": guid,
                "total": total,
                "all_images": all_images,
                "image_offset": offset,
                "page_size": 18,
            })
        finally:
            if cold:
                xeH._dehydrate_task(guid)

    @app.get("/add", response_class=HTMLResponse)
    async def ui_add_task(request: Request):
        return _render("add_task.html.j2", {
            "current_path": str(request.url.path),
        })

    @app.get("/subscriptions", response_class=HTMLResponse)
    async def ui_subscriptions(request: Request):
        """Gallery subscriptions management page."""
        xeH = request.app.state.xeH
        from .. import session_store

        items = []
        for row in session_store.list_subscriptions():
            status_label, status_css = _sub_status(row)
            items.append({
                "id": int(row["id"]),
                "gid": row.get("gid", ""),
                "url": row.get("url", ""),
                "title": row.get("title", "") or row.get("url", ""),
                "enabled": bool(row.get("enabled", True)),
                "status_label": status_label,
                "status_css": status_css,
                "last_error": row.get("last_error", ""),
                "last_check": _fmt_ts(row.get("last_check_at")),
                "next_check": _fmt_ts(row.get("next_check_at")) if row.get("enabled", True) else "—",
                "version_count": int(row.get("version_count", 0) or 0),
                "task_guid": session_store.find_guid_by_gid(str(row.get("gid", ""))),
            })

        ctx = {
            "current_path": str(request.url.path),
            "items": items,
            "check_interval": xeH.config.get("subscription_check_interval", 24.0),
            "subscription_enabled": xeH.config.get("subscription_enabled", True),
        }
        return _render("subscriptions.html.j2", ctx)

    @app.get("/config", response_class=HTMLResponse)
    async def ui_config(request: Request):
        xeH = request.app.state.xeH
        cfg = {k: v for k, v in xeH.config.items() if not k.startswith("rpc_")}
        return _render("config.html.j2", {
            "current_path": str(request.url.path),
            "config": cfg,
        })

    return app


class WebServer:
    """Wrapper around uvicorn.Server that runs in a daemon thread.

    Replaces the legacy RPCServer thread. Uses programmatic uvicorn API
    so it can run in-process alongside the main asyncio task loop.

    The `xeH` parameter receives the xeHentai host instance.  It is typed
    as ``HostProtocol`` (defined in ``xeHentai.host``) rather than the
    concrete ``xeHentai`` class, which avoids a type-level import cycle
    between ``core.py`` and ``web/__init__.py``.
    """

    def __init__(self, xeH: HostProtocol, bind_host: str, bind_port: int, secret: str | None = None):
        self._xeH = xeH
        self._host = bind_host
        self._port = bind_port
        self._secret = secret
        self._thread: threading.Thread | None = None
        self._server = None
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def logger(self):
        return self._xeH.logger

    def start(self):
        """Start the uvicorn server in a daemon thread."""
        import uvicorn

        app = create_app(self._xeH)

        config = uvicorn.Config(
            app=app,
            host=self._host,
            port=self._port,
            log_level="info",
            # Don't let uvicorn manage the signal handlers — the main
            # process handles signals.
        )
        self._server = uvicorn.Server(config)

        def _run(server: uvicorn.Server):
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            drain_task = self._loop.create_task(manager.drain_queues())
            try:
                self._loop.run_until_complete(server.serve())
            except Exception:
                self.logger.warning("WebServer: uvicorn stopped with error")
            finally:
                drain_task.cancel()
                try:
                    self._loop.run_until_complete(drain_task)
                except (asyncio.CancelledError, Exception):
                    pass
                self._loop.close()

        self._thread = threading.Thread(target=_run, args=(self._server,), name="web-ui", daemon=True)
        self._thread.start()
        self.logger.info(f"WebUI started at http://{self._host}:{self._port}")

    def stop(self):
        """Signal the uvicorn server to shut down."""
        if self._server:
            self._server.should_exit = True
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5.0)
