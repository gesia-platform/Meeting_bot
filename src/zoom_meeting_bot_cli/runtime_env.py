from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from local_meeting_ai_runtime.assets import find_whisper_cpp_model

from .config import DEFAULT_WHISPER_CPP_MODEL_NAME


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
    _augment_macos_homebrew_path(env)
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
    _set(env, "DELEGATE_SESSION_COMPLETION_MODE", str(runtime.get("completion_mode") or "inline"))
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
        _set(env, "DELEGATE_WHISPER_CPP_PATH", _resolve_command_or_path(whisper_cpp_command))
    whisper_cpp_model = str(local_ai.get("whisper_cpp_model") or "").strip()
    resolved_whisper_cpp_model = _resolve_whisper_cpp_model_path(whisper_cpp_model)
    if resolved_whisper_cpp_model is not None:
        _set(env, "DELEGATE_WHISPER_CPP_MODEL", str(resolved_whisper_cpp_model))

    if not bool(telegram.get("enabled")):
        _set(env, "DELEGATE_TELEGRAM_ARTIFACT_UPLOAD_ENABLED", "false")

    return env


def _augment_macos_homebrew_path(env: dict[str, str]) -> None:
    if os.name == "nt":
        return
    candidates = [
        Path("/opt/homebrew/bin"),
        Path("/opt/homebrew/sbin"),
        Path("/usr/local/bin"),
        Path("/usr/local/sbin"),
    ]
    existing_path = str(env.get("PATH") or "")
    parts: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        if candidate.exists():
            text = str(candidate)
            key = text.casefold()
            if key not in seen:
                parts.append(text)
                seen.add(key)
    for value in existing_path.split(os.pathsep):
        text = str(value or "").strip()
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        parts.append(text)
        seen.add(key)
    if parts:
        env["PATH"] = os.pathsep.join(parts)


def _set(target: dict[str, str], key: str, value: str) -> None:
    if value is None:
        return
    text = str(value).strip()
    if text:
        target[key] = text


def _resolve_command_or_path(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    path = Path(text).expanduser()
    if path.is_absolute():
        return str(path.resolve())
    if "/" in text or "\\" in text:
        return str(resolve_workspace_path(text))
    return text


def _resolve_file_path(value: str) -> Path:
    text = str(value or "").strip()
    path = Path(text).expanduser()
    if path.is_absolute():
        return path.resolve()
    return resolve_workspace_path(text)


def _resolve_whisper_cpp_model_path(value: str) -> Path | None:
    text = str(value or "").strip()
    if text:
        configured = _resolve_file_path(text)
        if configured.exists():
            return configured
        derived_name = _derive_whisper_cpp_model_name(text)
        if derived_name:
            discovered = find_whisper_cpp_model(derived_name)
            if discovered is not None:
                return discovered
        return configured
    discovered_default = find_whisper_cpp_model(DEFAULT_WHISPER_CPP_MODEL_NAME)
    if discovered_default is not None:
        return discovered_default
    return None


def _derive_whisper_cpp_model_name(value: str) -> str:
    name = Path(str(value or "").strip()).name
    if name.startswith("ggml-") and name.endswith(".bin"):
        return name[len("ggml-") : -len(".bin")].strip()
    return ""
