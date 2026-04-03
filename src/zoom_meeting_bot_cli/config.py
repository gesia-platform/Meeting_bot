from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
import re
from typing import Any


DEFAULT_CONFIG_FILENAME = "zoom-meeting-bot.config.json"

PRESET_CHOICES = (
    "runtime_only",
    "launcher_dm",
    "launcher_metheus",
)

DEFAULT_CONFIG_TEMPLATE: dict[str, Any] = {
    "profile": {
        "bot_name": "",
        "workspace_name": "",
        "language": "ko-KR",
        "timezone": "Asia/Seoul",
    },
    "zoom": {
        "client_id": "",
        "client_secret": "",
        "meeting_sdk_enabled": True,
        "programmatic_join_enabled": True,
    },
    "local_ai": {
        "codex_command": "codex",
        "huggingface_token": "",
        "transcribe_model": "large-v3",
        "diarization_model": "pyannote/speaker-diarization-community-1",
        "meeting_output_device": "",
        "pandoc_command": "pandoc",
        "libreoffice_command": "soffice",
        "whisper_cpp_command": "",
        "whisper_cpp_model": "",
    },
    "telegram": {
        "enabled": False,
        "bot_name": "",
        "bot_token": "",
        "conversation_route": {
            "mode": "metheus_project",
            "project_id": "",
            "destination_label": "",
            "chat_id": "",
        },
        "artifact_route": {
            "mode": "none",
            "project_id": "",
            "destination_label": "",
            "chat_id": "",
        },
    },
    "runtime": {
        "execution_mode": "runtime_only",
        "host": "127.0.0.1",
        "port": 8787,
        "audio_mode": "conversation",
        "store_path": "data/delegate_sessions.json",
        "exports_dir": "data/exports",
        "audio_archive_dir": "data/audio",
        "state_path": ".tmp/zoom-meeting-bot/runtime-state.json",
    },
    "launcher": {
        "telegram_runner_backend": "none",
        "metheus_route_name": "",
        "state_path": ".tmp/zoom-meeting-bot/launcher-state.json",
    },
}


def default_config_path(base_dir: Path | None = None) -> Path:
    root = base_dir or Path.cwd()
    return root / DEFAULT_CONFIG_FILENAME


def build_default_config() -> dict[str, Any]:
    return deepcopy(DEFAULT_CONFIG_TEMPLATE)


def build_preset_config(preset: str) -> dict[str, Any]:
    config = build_default_config()
    selected = str(preset or "runtime_only").strip() or "runtime_only"
    if selected == "launcher_dm":
        config["runtime"]["execution_mode"] = "launcher"
        config["telegram"]["enabled"] = True
        config["telegram"]["conversation_route"]["mode"] = "none"
        config["telegram"]["artifact_route"]["mode"] = "personal_dm"
        return config
    if selected == "launcher_metheus":
        config["runtime"]["execution_mode"] = "launcher"
        config["telegram"]["enabled"] = True
        config["telegram"]["conversation_route"]["mode"] = "metheus_project"
        config["telegram"]["artifact_route"]["mode"] = "metheus_project"
        config["launcher"]["telegram_runner_backend"] = "metheus_cli"
        config["launcher"]["metheus_route_name"] = "telegram-monitor-my-bot"
        return config
    return config


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_config(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def merge_config(base: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(base)
    _merge_dict(merged, updates)
    return normalize_config(merged)


def normalize_config(config: dict[str, Any]) -> dict[str, Any]:
    normalized = deepcopy(config)
    telegram = dict(normalized.get("telegram") or {})
    for route_name in ("conversation_route", "artifact_route"):
        route = dict(telegram.get(route_name) or {})
        mode = str(route.get("mode") or "").strip()
        if mode == "project_channel":
            route["mode"] = "metheus_project"
        telegram[route_name] = route
    normalized["telegram"] = telegram
    return normalized


def suggest_workspace_name(bot_name: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z가-힣_-]+", "-", str(bot_name or "").strip())
    cleaned = re.sub(r"-{2,}", "-", cleaned).strip("-_")
    return cleaned.lower() if cleaned else ""


def _merge_dict(target: dict[str, Any], updates: dict[str, Any]) -> None:
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _merge_dict(target[key], value)
        else:
            target[key] = value
