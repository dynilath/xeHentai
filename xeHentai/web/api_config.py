"""Config REST endpoints."""

from typing import Dict, Any
from fastapi import APIRouter, Request

from .models import ConfigResponse, ConfigUpdateRequest

router = APIRouter(prefix="/api", tags=["config"])


def _clean_config_for_response(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Filter out internal-only config keys for the API response."""
    # All config keys are now public; no prefix-based filtering needed.
    return dict(cfg)


@router.get("/config", response_model=ConfigResponse)
async def get_config(request: Request):
    """Get current configuration."""
    xeH = request.app.state.xeH
    return _clean_config_for_response(dict(xeH.config))


@router.patch("/config", response_model=ConfigResponse)
async def update_config(body: ConfigUpdateRequest, request: Request):
    """Update configuration (partial update)."""
    xeH = request.app.state.xeH
    cfg_dict = body.model_dump(exclude_none=True)
    if cfg_dict:
        xeH.update_config(**cfg_dict)
    return _clean_config_for_response(dict(xeH.config))
