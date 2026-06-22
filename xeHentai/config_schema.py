"""Pydantic v2 models for xeHentai YAML configuration.

Defines the nested config structure and provides ``to_flat_dict()`` /
``update_from_flat_dict()`` methods for backward compatibility with all
existing code that accesses config as a flat dict (e.g. ``config["dir"]``).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# ═══════════════════════════════════════════════════════════════════════════
# Sub-models
# ═══════════════════════════════════════════════════════════════════════════


class GatewayConfig(BaseModel):
    """Gateway server settings (serves both Web UI and REST API)."""

    host: str = Field(default="localhost", description="Bind address")
    port: int = Field(default=8010, ge=1, le=65535, description="Listen port")


class DownloadConfig(BaseModel):
    """Download behaviour settings."""

    dir: str = Field(default="./download", description="Download root directory")
    download_ori: bool = Field(default=False, description="Download original images (requires login)")
    jpn_title: bool = Field(default=True, description="Use Japanese title if available")
    delete_task_files: bool = Field(default=False, description="Delete files when deleting a task")


class ProxyConfig(BaseModel):
    """Proxy / network settings."""

    servers: List[str] = Field(default_factory=list, description="Proxy server URLs (socks5/http/glype)")
    image: bool = Field(default=True, description="Use proxy for image downloads too")
    image_only: bool = Field(default=False, description="Only proxy image downloads, not pages")
    disable_threshold: int = Field(default=16, ge=0, description="Consecutive failures before disabling a proxy")
    good_threshold: int = Field(default=16, ge=0, description="Consecutive successes before marking proxy good")


class PerformanceConfig(BaseModel):
    """Concurrency / throttling settings."""

    scan_thread_cnt: int = Field(default=1, ge=1, description="Threads for scanning pages")
    download_thread_cnt: int = Field(default=5, ge=1, description="Threads for downloading images")
    async_task_concurrency: int = Field(default=1, ge=1, description="Max concurrent task pipelines")
    page_interval: float = Field(default=0.5, ge=0, description="Interval between page requests (seconds)")
    page_retry: int = Field(default=3, ge=0, description="Page request retry count")
    page_timeout: int = Field(default=10, ge=1, description="Page request timeout (seconds)")
    download_retry: int = Field(default=5, ge=0, description="Image download retry count")
    download_timeout: int = Field(default=8, ge=1, description="Image download timeout (seconds)")


class LoggingConfig(BaseModel):
    """Logging settings."""

    path: str = Field(default="eh.log", description="Log file path")
    level_console: str = Field(default="DEBUG", description="Console log level (DEBUG/INFO/WARNING/ERROR/CRITICAL)")
    level_file: str = Field(default="DEBUG", description="File log level (DEBUG/INFO/WARNING/ERROR/CRITICAL)")


# ═══════════════════════════════════════════════════════════════════════════
# Root model
# ═══════════════════════════════════════════════════════════════════════════


class XeHentaiConfig(BaseModel):
    """Root configuration model for xeHentai."""

    gateway: GatewayConfig = Field(default_factory=GatewayConfig)
    download: DownloadConfig = Field(default_factory=DownloadConfig)
    proxy: ProxyConfig = Field(default_factory=ProxyConfig)
    performance: PerformanceConfig = Field(default_factory=PerformanceConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)

    # ── Flat-dict bridge ─────────────────────────────────────────────────

    # Mapping from nested keys to flat keys consumed by legacy code.
    _FLAT_MAP: Dict[str, str] = {
        # gateway
        "gateway.host": "webui_host",
        "gateway.port": "webui_port",
        # download
        "download.dir": "dir",
        "download.download_ori": "download_ori",
        "download.jpn_title": "jpn_title",
        "download.delete_task_files": "delete_task_files",
        # proxy
        "proxy.servers": "proxy",
        "proxy.image": "proxy_image",
        "proxy.image_only": "proxy_image_only",
        "proxy.disable_threshold": "proxy_disable_threshold",
        "proxy.good_threshold": "proxy_good_threshold",
        # performance
        "performance.scan_thread_cnt": "scan_thread_cnt",
        "performance.download_thread_cnt": "download_thread_cnt",
        "performance.async_task_concurrency": "async_task_concurrency",
        "performance.page_interval": "page_interval",
        "performance.page_retry": "page_retry",
        "performance.page_timeout": "page_timeout",
        "performance.download_retry": "download_retry",
        "performance.download_timeout": "download_timeout",
        # logging
        "logging.path": "log_path",
        "logging.level_console": "log_level_console",
        "logging.level_file": "log_level_file",
    }

    # Reverse mapping: flat → nested (dot-separated)
    _REVERSE_MAP: Dict[str, str] = {v: k for k, v in _FLAT_MAP.items()}

    def to_flat_dict(self) -> Dict[str, Any]:
        """Convert the nested Pydantic model to a flat dict for legacy consumers.

        Produces keys like ``"dir"``, ``"proxy"``, ``"webui_host"``, etc.
        """
        flat: Dict[str, Any] = {}
        model_dict = self.model_dump()
        for nested_key, flat_key in self._FLAT_MAP.items():
            parts = nested_key.split(".")
            value = model_dict
            try:
                for part in parts:
                    value = value[part]
                flat[flat_key] = value
            except (KeyError, TypeError):
                continue
        return flat

    def update_from_flat_dict(self, updates: Dict[str, Any]) -> XeHentaiConfig:
        """Update this config from a flat dict of changes (e.g. from the API).

        Returns a **new** ``XeHentaiConfig`` instance (Pydantic models are
        immutable by default, but this creates a fresh copy with merged data).
        """
        # Build a nested dict from the flat updates
        model_dict = self.model_dump()
        for flat_key, value in updates.items():
            nested_key = self._REVERSE_MAP.get(flat_key)
            if nested_key is None:
                continue
            parts = nested_key.split(".")
            target = model_dict
            for part in parts[:-1]:
                target = target.setdefault(part, {})
            target[parts[-1]] = value

        return XeHentaiConfig.model_validate(model_dict)
