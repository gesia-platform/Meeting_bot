from __future__ import annotations

import os
from pathlib import Path


WORKSPACE_ROOT_ENV = "ZOOM_MEETING_BOT_HOME"


def package_root() -> Path:
    return Path(__file__).resolve().parents[2]


def workspace_root() -> Path:
    configured = os.getenv(WORKSPACE_ROOT_ENV, "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return Path.cwd().resolve()


def resolve_package_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path.resolve()
    return (package_root() / path).resolve()


def resolve_workspace_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path.resolve()
    return (workspace_root() / path).resolve()


def resolve_relative_path(value: str | Path, *, prefer: str = "workspace") -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path.resolve()

    preferred_root = workspace_root if prefer != "package" else package_root
    fallback_root = package_root if prefer != "package" else workspace_root
    preferred_candidate = (preferred_root() / path).resolve()
    if preferred_candidate.exists():
        return preferred_candidate
    fallback_candidate = (fallback_root() / path).resolve()
    if fallback_candidate.exists():
        return fallback_candidate
    return preferred_candidate
