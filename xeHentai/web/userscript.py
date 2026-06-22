"""Userscript serve endpoint — serves xeh_batch.user.js with BASE_URL auto-configured."""

from functools import lru_cache
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import Response

router = APIRouter(tags=["userscript"])

# The userscript lives in a sibling .js file; __BASE_URL__ is replaced at serve time.
_SCRIPT_PATH = Path(__file__).resolve().parent / "xeh_batch.user.js"


@lru_cache(maxsize=1)
def _read_userscript_template() -> str:
    """Read the userscript template from disk (cached after first read)."""
    return _SCRIPT_PATH.read_text(encoding="utf-8")


def _build_base_url(request: Request) -> str:
    """Derive the server's base URL from the incoming request.

    Uses the Host header when available, falling back to the configured
    webui_host:webui_port from xeH config."""
    xeH = request.app.state.xeH
    host = request.headers.get("Host") or request.headers.get("host") or ""
    if host:
        scheme = "https" if request.url.scheme == "https" else "http"
        return f"{scheme}://{host}"
    # Fallback to configured address
    cfg_host = xeH.config.get("webui_host", "localhost")
    cfg_port = xeH.config.get("webui_port") or 8010
    return f"http://{cfg_host}:{cfg_port}"


@router.get("/userscript/xeh_batch.user.js", response_class=Response)
async def serve_userscript(request: Request):
    """Serve the userscript with BASE_URL set to this server's address."""
    base_url = _build_base_url(request)
    content = _read_userscript_template().replace("__BASE_URL__", base_url)
    return Response(
        content=content,
        media_type="application/javascript; charset=utf-8",
    )
