"""YAML configuration loader for xeHentai.

On first run, if no ``config.yml`` exists in the working directory, a copy
of the default template (``xeHentai/config.default.yml``) is automatically
created.  Users edit the local ``config.yml`` — the template is never
overwritten.

Provides ``load_config()`` (used by the core) and ``bootstrap_config()``
(used by the entry point to detect first-run).
"""

from __future__ import annotations

import os
import shutil
from typing import Optional, Tuple

import yaml

from .config_schema import XeHentaiConfig


def _default_template_path() -> str:
    """Absolute path to ``xeHentai/config.default.yml``."""
    module_dir = os.path.dirname(os.path.abspath(__file__))  # xeHentai/
    return os.path.join(module_dir, "config.default.yml")


def _repo_root() -> str:
    """Absolute path to the repository root (parent of ``xeHentai/``)."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _find_existing_config_yml() -> Optional[str]:
    """Return the path to an existing ``config.yml`` in CWD, or None."""
    cwd_path = os.path.join(os.getcwd(), "config.yml")
    if os.path.isfile(cwd_path):
        return os.path.abspath(cwd_path)
    return None


def _parse_config(path: str) -> XeHentaiConfig:
    with open(path, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    if raw is None:
        raw = {}
    return XeHentaiConfig.model_validate(raw)


def bootstrap_config() -> Tuple[XeHentaiConfig, bool]:
    """Entry-point config loader with first-run detection.

    If ``config.yml`` does not exist, copies the default template into CWD,
    loads it, and returns ``(config, True)`` — the caller should inform the
    user and exit so they can review the generated file.

    If ``config.yml`` already exists, returns ``(config, False)`` — normal
    startup can proceed.

    Returns:
        ``(config, was_just_created)`` tuple.
    """
    existing = _find_existing_config_yml()
    if existing is not None:
        return _parse_config(existing), False

    # No config.yml found — seed from the default template.
    template = _default_template_path()
    if not os.path.isfile(template):
        raise FileNotFoundError(
            "Default config template not found at %s" % template
        )

    cwd_path = os.path.join(os.getcwd(), "config.yml")
    shutil.copy2(template, cwd_path)
    return _parse_config(cwd_path), True


def load_config(path: Optional[str] = None) -> XeHentaiConfig:
    """Load and validate configuration from a YAML file.

    This is used by the core at import time — it assumes ``config.yml``
    already exists (bootstrapping is handled by the entry point).

    Args:
        path: Optional explicit path to a ``config.yml``.  If omitted the
              file is located automatically (CWD → repo root).

    Returns:
        A validated ``XeHentaiConfig`` instance.

    Raises:
        FileNotFoundError: If no config file can be found.
        yaml.YAMLError: If the YAML is malformed.
        pydantic.ValidationError: If the config fails validation.
    """
    if path and os.path.isfile(path):
        return _parse_config(os.path.abspath(path))

    existing = _find_existing_config_yml()
    if existing is None:
        raise FileNotFoundError(
            "config.yml not found. Run the application once to generate it, "
            "then edit it before starting again."
        )
    return _parse_config(existing)
