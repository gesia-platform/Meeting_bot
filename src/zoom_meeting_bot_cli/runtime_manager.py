from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
import signal
import subprocess
import sys
import time
from typing import Any

import httpx

from .runtime_env import build_runtime_env, package_root, runtime_host, runtime_port, runtime_state_path


def start_runtime(config: dict[str, Any], *, config_path: Path) -> dict[str, Any]:
    state_path = runtime_state_path(config)
    current = read_runtime_status(config, config_path=config_path)
    if bool(current.get("alive")):
        return current

    state_path.parent.mkdir(parents=True, exist_ok=True)
    log_dir = state_path.parent / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "runtime.log"

    env = build_runtime_env(config)
    command = [sys.executable, "-m", "local_meeting_ai_runtime"]
    kwargs: dict[str, Any] = {
        "cwd": str(package_root()),
        "env": env,
        "stdin": subprocess.DEVNULL,
    }

    with log_path.open("ab") as handle:
        kwargs["stdout"] = handle
        kwargs["stderr"] = handle
        if os.name == "nt":
            kwargs["creationflags"] = (
                getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
                | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            )
            process = subprocess.Popen(command, **kwargs)
        else:  # pragma: no cover
            kwargs["start_new_session"] = True
            process = subprocess.Popen(command, **kwargs)

    state = {
        "status": "starting",
        "pid": process.pid,
        "alive": True,
        "host": runtime_host(config),
        "port": runtime_port(config),
        "config_path": str(config_path.resolve()),
        "workspace_dir": str(package_root()),
        "log_path": str(log_path),
        "started_at": _utcnow_iso(),
    }
    _write_json_atomic(state_path, state)

    deadline = time.time() + 10.0
    while time.time() < deadline:
        live = read_runtime_status(config, config_path=config_path)
        if bool(live.get("alive")) and bool(live.get("health", {}).get("ok")):
            return live
        time.sleep(0.3)
    return read_runtime_status(config, config_path=config_path)


def stop_runtime(config: dict[str, Any], *, config_path: Path) -> dict[str, Any]:
    state_path = runtime_state_path(config)
    state = _read_json(state_path)
    pid = int((state or {}).get("pid") or 0)
    if pid > 0:
        _terminate_pid(pid, force=False)
        deadline = time.time() + 8.0
        while time.time() < deadline:
            if not _pid_alive(pid):
                break
            time.sleep(0.3)
        if _pid_alive(pid):
            _terminate_pid(pid, force=True)

    stopped = {
        "status": "stopped",
        "pid": pid or None,
        "alive": False,
        "host": runtime_host(config),
        "port": runtime_port(config),
        "config_path": str(config_path.resolve()),
        "workspace_dir": str(package_root()),
        "stopped_at": _utcnow_iso(),
    }
    _write_json_atomic(state_path, stopped)
    return stopped


def read_runtime_status(config: dict[str, Any], *, config_path: Path) -> dict[str, Any]:
    state_path = runtime_state_path(config)
    state = _read_json(state_path)
    host = runtime_host(config)
    port = runtime_port(config)
    if not state:
        return {
            "status": "stopped",
            "pid": None,
            "alive": False,
            "host": host,
            "port": port,
            "config_path": str(config_path.resolve()),
            "workspace_dir": str(package_root()),
            "state_path": str(state_path),
            "health": {"ok": False},
        }

    pid = int(state.get("pid") or 0)
    alive = bool(pid > 0 and _pid_alive(pid))
    health = _query_health(host=host, port=port) if alive else {"ok": False}
    overview = _query_overview(host=host, port=port) if alive else None
    status = "running" if alive and health.get("ok") else "starting" if alive else "stopped"
    return {
        **state,
        "status": status,
        "alive": alive,
        "host": host,
        "port": port,
        "config_path": str(config_path.resolve()),
        "workspace_dir": str(package_root()),
        "state_path": str(state_path),
        "health": health,
        "overview": overview,
    }


def create_runtime_session(
    config: dict[str, Any],
    *,
    join_url: str,
    passcode: str,
    meeting_number: str = "",
    meeting_topic: str = "",
    requested_by: str = "",
    instructions: str = "",
    delegate_mode: str = "answer_on_ask",
) -> dict[str, Any]:
    payload = {
        "join_url": join_url,
        "passcode": passcode,
        "meeting_number": meeting_number,
        "meeting_topic": meeting_topic,
        "requested_by": requested_by,
        "instructions": instructions,
        "delegate_mode": delegate_mode,
        "bot_display_name": str(dict(config.get("profile") or {}).get("bot_name") or "").strip(),
    }
    with httpx.Client(timeout=30.0) as client:
        response = client.post(
            f"http://{runtime_host(config)}:{runtime_port(config)}/delegate/sessions",
            json=payload,
        )
        response.raise_for_status()
        return response.json()


def list_runtime_sessions(config: dict[str, Any]) -> dict[str, Any]:
    with httpx.Client(timeout=30.0) as client:
        response = client.get(
            f"http://{runtime_host(config)}:{runtime_port(config)}/delegate/sessions",
        )
        response.raise_for_status()
        return response.json()


def get_runtime_session(config: dict[str, Any], session_id: str) -> dict[str, Any]:
    with httpx.Client(timeout=30.0) as client:
        response = client.get(
            f"http://{runtime_host(config)}:{runtime_port(config)}/delegate/sessions/{session_id}",
        )
        response.raise_for_status()
        return response.json()


def _query_health(*, host: str, port: int) -> dict[str, Any]:
    try:
        with httpx.Client(timeout=3.0) as client:
            response = client.get(f"http://{host}:{port}/health")
            response.raise_for_status()
            payload = response.json()
            return payload if isinstance(payload, dict) else {"ok": False}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _query_overview(*, host: str, port: int) -> dict[str, Any] | None:
    try:
        with httpx.Client(timeout=3.0) as client:
            response = client.get(f"http://{host}:{port}/delegate/runtime/overview")
            response.raise_for_status()
            payload = response.json()
            return payload if isinstance(payload, dict) else None
    except Exception:
        return None


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp_path.replace(path)


def _utcnow_iso() -> str:
    return datetime.now(UTC).isoformat()


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        if os.name == "nt":
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=False,
                **_windows_hidden_process_kwargs(),
            )
            output = result.stdout.decode(errors="ignore").strip()
            if not output or output.lower().startswith("info:"):
                return False
            return f'"{pid}"' in output
        os.kill(pid, 0)
    except (OSError, ValueError, subprocess.SubprocessError):
        return False
    return True


def _terminate_pid(pid: int, *, force: bool) -> None:
    if pid <= 0:
        return
    if os.name == "nt":
        args = ["taskkill", "/PID", str(pid), "/T"]
        if force:
            args.append("/F")
        subprocess.run(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            **_windows_hidden_process_kwargs(),
        )
        return
    try:  # pragma: no cover
        os.kill(pid, signal.SIGKILL if force else signal.SIGTERM)
    except OSError:
        return


def _windows_hidden_process_kwargs() -> dict[str, Any]:
    if os.name != "nt":
        return {}
    return {
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
        "startupinfo": _windows_hidden_startupinfo(),
    }


def _windows_hidden_startupinfo() -> Any:
    if os.name != "nt" or not hasattr(subprocess, "STARTUPINFO"):
        return None
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 0x00000001)
    startupinfo.wShowWindow = 0
    return startupinfo
