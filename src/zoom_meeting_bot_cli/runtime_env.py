from __future__ import annotations

import os
from pathlib import Path
from typing import Any


def package_root() -> Path:
    return Path(__file__).resolve().parents[2]


def runtime_host(config: dict[str, Any]) -> str:
    runtime = dict(config.get("runtime") or {})
    return str(runtime.get("host") or "127.0.0.1").strip() or "127.0.0.1"


def runtime_port(config: dict[str, Any]) -> int:
    runtime = dict(config.get("runtime") or {})
    raw = runtime.get("port", runtime.get("launcher_port", 8787))
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 8787


def runtime_state_path(config: dict[str, Any]) -> Path:
    runtime = dict(config.get("runtime") or {})
    configured = str(runtime.get("state_path") or ".tmp/zoom-meeting-bot/runtime-state.json").strip()
    return resolve_workspace_path(configured)


def resolve_workspace_path(value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return (package_root() / path).resolve()


def build_runtime_env(config: dict[str, Any]) -> dict[str, str]:
    env = dict(os.environ)
    src_root = package_root() / "src"
    current_pythonpath = env.get("PYTHONPATH", "").strip()
    pythonpath_parts = [str(src_root)]
    if current_pythonpath:
        pythonpath_parts.append(current_pythonpath)
    env["PYTHONPATH"] = os.pathsep.join(pythonpath_parts)

    profile = dict(config.get("profile") or {})
    zoom = dict(config.get("zoom") or {})
    local_ai = dict(config.get("local_ai") or {})
    telegram = dict(config.get("telegram") or {})
    runtime = dict(config.get("runtime") or {})

    _set(env, "DELEGATE_HOST", runtime_host(config))
    _set(env, "DELEGATE_PORT", str(runtime_port(config)))
    _set(env, "DELEGATE_STORE_PATH", str(resolve_workspace_path(str(runtime.get("store_path") or "data/delegate_sessions.json"))))
    _set(env, "DELEGATE_EXPORT_DIR", str(resolve_workspace_path(str(runtime.get("exports_dir") or "data/exports"))))
    _set(env, "DELEGATE_AUDIO_ARCHIVE_DIR", str(resolve_workspace_path(str(runtime.get("audio_archive_dir") or "data/audio"))))
    _set(env, "DELEGATE_REFERENCE_DOC_PATH", str((package_root() / "doc" / "templates" / "meeting-summary-reference.docx").resolve()))
    _set(env, "DELEGATE_BOT_DISPLAY_NAME", str(profile.get("bot_name") or "").strip())
    _set(env, "DELEGATE_LOCAL_USER_SPEAKER_NAME", str(profile.get("bot_name") or "").strip())
    _set(env, "DELEGATE_AUTO_OBSERVE_AUDIO_MODE", str(runtime.get("audio_mode") or "conversation"))
    _set(env, "ZOOM_CLIENT_ID", str(zoom.get("client_id") or "").strip())
    _set(env, "ZOOM_CLIENT_SECRET", str(zoom.get("client_secret") or "").strip())
    _set(env, "ZOOM_MEETING_SDK_KEY", str(zoom.get("client_id") or "").strip())
    _set(env, "ZOOM_MEETING_SDK_SECRET", str(zoom.get("client_secret") or "").strip())
    _set(env, "DELEGATE_HUGGINGFACE_TOKEN", str(local_ai.get("huggingface_token") or "").strip())
    _set(env, "HUGGINGFACE_TOKEN", str(local_ai.get("huggingface_token") or "").strip())
    _set(env, "DELEGATE_FINAL_TRANSCRIBE_MODEL", str(local_ai.get("transcribe_model") or "").strip())
    _set(env, "DELEGATE_PYANNOTE_MODEL", str(local_ai.get("diarization_model") or "").strip())
    _set(env, "DELEGATE_LOCAL_MEETING_OUTPUT_DEVICE", str(local_ai.get("meeting_output_device") or "").strip())

    codex_command = str(local_ai.get("codex_command") or "").strip()
    if codex_command and Path(codex_command).is_absolute():
        _set(env, "DELEGATE_CODEX_PATH", codex_command)
    pandoc_command = str(local_ai.get("pandoc_command") or "").strip()
    if pandoc_command and Path(pandoc_command).is_absolute():
        _set(env, "DELEGATE_PANDOC_PATH", pandoc_command)
    soffice_command = str(local_ai.get("libreoffice_command") or "").strip()
    if soffice_command and Path(soffice_command).is_absolute():
        _set(env, "DELEGATE_SOFFICE_PATH", soffice_command)
    whisper_cpp_command = str(local_ai.get("whisper_cpp_command") or "").strip()
    if whisper_cpp_command:
        _set(env, "DELEGATE_WHISPER_CPP_PATH", whisper_cpp_command)
    whisper_cpp_model = str(local_ai.get("whisper_cpp_model") or "").strip()
    if whisper_cpp_model:
        _set(env, "DELEGATE_WHISPER_CPP_MODEL", whisper_cpp_model)

    if not bool(telegram.get("enabled")):
        _set(env, "DELEGATE_TELEGRAM_ARTIFACT_UPLOAD_ENABLED", "false")

    return env


def _set(target: dict[str, str], key: str, value: str) -> None:
    if value is None:
        return
    text = str(value).strip()
    if text:
        target[key] = text
