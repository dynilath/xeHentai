"""System info / status REST endpoints."""

from typing import Dict
from fastapi import APIRouter, Request

from .models import InfoResponse, SystemStatusResponse, SystemStatusGroup

router = APIRouter(prefix="/api", tags=["system"])


@router.get("/info", response_model=InfoResponse)
async def get_info(request: Request):
    """Get server information including version and live queue stats."""
    xeH = request.app.state.xeH
    tc = xeH._task_control

    return InfoResponse(
        version=xeH.verstr,
        threads_zombie=0,
        threads_running=0,
        queue_pending=len(tc._waiting_set),
        queue_finished=len(tc._runtime_top_status) - len(tc._waiting_set) - len(tc._running_set),
        queue_waiting=len(tc._waiting_set),
        queue_processing=len(tc._running_set),
        proxy_enabled=xeH.proxy is not None,
        proxy_count=len(xeH.proxy.proxies) if xeH.proxy else 0,
        has_login=xeH.has_login,
    )


@router.get("/system/status", response_model=SystemStatusResponse)
async def get_system_status(request: Request):
    """Get grouped task counts by top status and phase state."""
    xeH = request.app.state.xeH
    raw = xeH.system_status()

    def _convert(groups: Dict[str, dict]) -> Dict[str, SystemStatusGroup]:
        result: Dict[str, SystemStatusGroup] = {}
        for state_name, group_data in groups.items():
            result[state_name] = SystemStatusGroup(
                count=group_data.get("count", 0),
                state_name=group_data.get("state_name", state_name),
            )
        return result

    return SystemStatusResponse(
        waiting=_convert(raw.get("waiting", {})),
        processing=_convert(raw.get("processing", {})),
        processed=_convert(raw.get("processed", {})),
    )
