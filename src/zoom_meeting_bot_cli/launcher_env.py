from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .runtime_env import build_runtime_env, package_root, resolve_workspace_path


def build_launcher_env(config: dict[str, Any]) -> dict[str, str]:
    env = build_runtime_env(config)
    launcher = dict(config.get("launcher") or {})
    telegram = dict(config.get("telegram") or {})

    _set(env, "LUSH_LAUNCHER_STATE_PATH", str(resolve_workspace_path(str(launcher.get("state_path") or ".tmp/zoom-meeting-bot/launcher-state.json"))))

    runner_backend = str(launcher.get("telegram_runner_backend") or "none").strip() or "none"
    _set(env, "LUSH_TELEGRAM_RUNNER_BACKEND", runner_backend)
    _set(env, "LUSH_TELEGRAM_RUNNER_ENABLED", "true" if runner_backend != "none" else "false")
    _set(env, "LUSH_TELEGRAM_RUNNER_ROUTE_NAME", str(launcher.get("metheus_route_name") or "").strip())

    telegram_enabled = bool(telegram.get("enabled"))
    artifact_route = dict(telegram.get("artifact_route") or {})
    artifact_mode = str(artifact_route.get("mode") or "none").strip() or "none"

    _set(env, "DELEGATE_TELEGRAM_ARTIFACT_BOT_NAME", str(telegram.get("bot_name") or "").strip())
    _set(env, "DELEGATE_TELEGRAM_BOT_TOKEN", str(telegram.get("bot_token") or "").strip())
    _set(env, "DELEGATE_TELEGRAM_ARTIFACT_UPLOAD_ENABLED", "true" if telegram_enabled and artifact_mode != "none" else "false")
    _set(env, "DELEGATE_TELEGRAM_ARTIFACT_ROUTE_MODE", artifact_mode)

    if artifact_mode in {"personal_dm", "telegram_chat"}:
        _set(env, "DELEGATE_TELEGRAM_ARTIFACT_CHAT_ID_OVERRIDE", str(artifact_route.get("chat_id") or "").strip())
    elif artifact_mode == "metheus_project":
        _set(env, "DELEGATE_PROJECT_ID", str(artifact_route.get("project_id") or "").strip())
        _set(env, "DELEGATE_TELEGRAM_ARTIFACT_DESTINATION_LABEL", str(artifact_route.get("destination_label") or "").strip())

    return env


def launcher_state_path(config: dict[str, Any]) -> Path:
    launcher = dict(config.get("launcher") or {})
    configured = str(launcher.get("state_path") or ".tmp/zoom-meeting-bot/launcher-state.json").strip()
    return resolve_workspace_path(configured)


def _set(target: dict[str, str], key: str, value: str) -> None:
    if value is None:
        return
    text = str(value).strip()
    if text:
        target[key] = text
