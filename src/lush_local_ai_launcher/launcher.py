"""Supervisor and Telegram artifact bridge for the local AI stack."""

from __future__ import annotations

import argparse
import base64
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
from typing import Any

import httpx

from local_meeting_ai_runtime.models import DelegateSession, utcnow_iso
from local_meeting_ai_runtime.storage import SessionStore

PDF_HANDOFF_KIND = "telegram_summary_pdf"
DEFAULT_STATE_PATH = Path(".tmp/local-ai-launcher/state.json")
DEFAULT_POLL_SECONDS = 5.0
RETRY_DELAYS_SECONDS = [0, 60, 300, 900, 1800, 1800]


def main(argv: list[str] | None = None) -> None:
    _load_dotenv()
    parser = argparse.ArgumentParser(prog="lush_local_ai_launcher")
    parser.add_argument(
        "command",
        choices=["start", "status", "stop", "_supervise"],
        help="Launcher command.",
    )
    args = parser.parse_args(argv)

    if args.command == "start":
        status = start_launcher()
        print(json.dumps(status, ensure_ascii=False, indent=2))
        return
    if args.command == "status":
        print(json.dumps(read_launcher_status(), ensure_ascii=False, indent=2))
        return
    if args.command == "stop":
        status = stop_launcher()
        print(json.dumps(status, ensure_ascii=False, indent=2))
        return
    run_supervisor()


def start_launcher() -> dict[str, Any]:
    state = read_launcher_status()
    supervisor = dict(state.get("supervisor") or {})
    if supervisor.get("alive"):
        return state
    _terminate_orphan_children(state)

    state_path = _state_path()
    log_dir = state_path.parent / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "supervisor.log"
    command = [sys.executable, "-m", "lush_local_ai_launcher", "_supervise"]
    kwargs: dict[str, Any] = {
        "cwd": str(Path.cwd()),
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
        else:  # pragma: no cover - non-Windows fallback
            kwargs["start_new_session"] = True
            process = subprocess.Popen(command, **kwargs)

    deadline = time.time() + 10.0
    while time.time() < deadline:
        status = read_launcher_status()
        supervisor = dict(status.get("supervisor") or {})
        if supervisor.get("pid") == process.pid and supervisor.get("alive"):
            return status
        time.sleep(0.2)
    return read_launcher_status()


def stop_launcher() -> dict[str, Any]:
    state_path = _state_path()
    state = read_launcher_status()
    supervisor_entry = dict(state.get("supervisor") or {})
    supervisor_pid = int(supervisor_entry.get("pid") or 0) if bool(supervisor_entry.get("alive")) else 0
    zoom_entry = dict(state.get("zoom_runtime") or {})
    runner_entry = dict(state.get("telegram_runner") or {})
    finalizer_entry = dict(state.get("finalizer") or {})
    child_pids = [
        int(zoom_entry.get("pid") or 0) if bool(zoom_entry.get("alive")) else 0,
        int(runner_entry.get("pid") or 0) if bool(runner_entry.get("alive")) else 0,
        int(finalizer_entry.get("pid") or 0) if bool(finalizer_entry.get("alive")) else 0,
    ]
    if supervisor_pid > 0:
        _terminate_pid(supervisor_pid, force=False)
    deadline = time.time() + 10.0
    while time.time() < deadline:
        live = [pid for pid in [supervisor_pid, *child_pids] if pid > 0 and _pid_alive(pid)]
        if not live:
            break
        time.sleep(0.5)
    for pid in [supervisor_pid, *child_pids]:
        if pid > 0 and _pid_alive(pid):
            _terminate_pid(pid, force=True)
    stopped = {
        "status": "stopped",
        "stopped_at": utcnow_iso(),
        "supervisor": {"pid": supervisor_pid, "alive": False},
        "zoom_runtime": {"pid": child_pids[0], "alive": False},
        "telegram_runner": {"pid": child_pids[1], "alive": False, "enabled": _runner_enabled()},
        "finalizer": {
            "pid": child_pids[2],
            "alive": False,
            "enabled": bool((state.get("finalizer") or {}).get("enabled", _launcher_uses_finisher())),
        },
        "artifact_bridge": {
            **dict(state.get("artifact_bridge") or {}),
            "status": "idle",
        },
    }
    _write_json_atomic(state_path, stopped)
    return stopped


def read_launcher_status() -> dict[str, Any]:
    state_path = _state_path()
    state = _read_json(state_path)
    if not state:
        return {
            "status": "stopped",
            "state_path": str(state_path),
            "supervisor": {"pid": None, "alive": False},
            "zoom_runtime": {"pid": None, "alive": False},
            "telegram_runner": {"pid": None, "alive": False, "enabled": _runner_enabled()},
            "finalizer": {"pid": None, "alive": False, "enabled": _launcher_uses_finisher()},
            "artifact_bridge": {"status": "idle"},
        }
    if str(state.get("status") or "").strip().lower() == "stopped":
        bridge = dict(state.get("artifact_bridge") or {})
        bridge["status"] = "idle"
        state["artifact_bridge"] = bridge
        for key in ("supervisor", "zoom_runtime", "telegram_runner", "finalizer"):
            entry = dict(state.get(key) or {})
            entry["alive"] = False
            if key == "telegram_runner":
                entry["enabled"] = bool(entry.get("enabled", _runner_enabled()))
            if key == "finalizer":
                entry["enabled"] = bool(entry.get("enabled", _launcher_uses_finisher()))
            state[key] = entry
        state["state_path"] = str(state_path)
        return state

    for key in ("supervisor", "zoom_runtime", "telegram_runner", "finalizer"):
        entry = dict(state.get(key) or {})
        pid = int(entry.get("pid") or 0)
        entry["alive"] = bool(pid > 0 and _pid_matches_entry(key, pid, entry))
        if key == "telegram_runner":
            entry["enabled"] = bool(entry.get("enabled", _runner_enabled()))
        if key == "finalizer":
            entry["enabled"] = bool(entry.get("enabled", _launcher_uses_finisher()))
        state[key] = entry
    state["status"] = _status_from_process_state(
        supervisor_alive=bool(state["supervisor"]["alive"]),
        zoom_alive=bool(state["zoom_runtime"]["alive"]),
        runner_alive=bool(state["telegram_runner"]["alive"]),
        runner_required=bool(state["telegram_runner"].get("enabled", _runner_enabled())),
        finalizer_alive=bool(state["finalizer"]["alive"]),
        finalizer_required=bool(state["finalizer"].get("enabled", _launcher_uses_finisher())),
    )
    state["state_path"] = str(state_path)
    return state


def _status_from_process_state(
    *,
    supervisor_alive: bool,
    zoom_alive: bool,
    runner_alive: bool,
    runner_required: bool,
    finalizer_alive: bool = False,
    finalizer_required: bool = False,
) -> str:
    runner_ok = runner_alive or not runner_required
    finalizer_ok = finalizer_alive or not finalizer_required
    if supervisor_alive and zoom_alive and runner_ok and finalizer_ok:
        return "running"
    if supervisor_alive or zoom_alive or runner_alive or finalizer_alive:
        return "degraded"
    return "stopped"


def _terminate_orphan_children(state: dict[str, Any]) -> None:
    supervisor_alive = bool(dict(state.get("supervisor") or {}).get("alive"))
    if supervisor_alive:
        return
    zoom_entry = dict(state.get("zoom_runtime") or {})
    runner_entry = dict(state.get("telegram_runner") or {})
    finalizer_entry = dict(state.get("finalizer") or {})
    orphan_pids = [
        int(zoom_entry.get("pid") or 0) if bool(zoom_entry.get("alive")) else 0,
        int(runner_entry.get("pid") or 0) if bool(runner_entry.get("alive")) else 0,
        int(finalizer_entry.get("pid") or 0) if bool(finalizer_entry.get("alive")) else 0,
    ]
    for pid in orphan_pids:
        if pid > 0 and _pid_alive(pid):
            _terminate_pid(pid, force=False)
    deadline = time.time() + 5.0
    while time.time() < deadline:
        if not any(pid > 0 and _pid_alive(pid) for pid in orphan_pids):
            return
        time.sleep(0.2)
    for pid in orphan_pids:
        if pid > 0 and _pid_alive(pid):
            _terminate_pid(pid, force=True)


class LauncherSupervisor:
    def __init__(self) -> None:
        self._workspace_dir = Path.cwd().resolve()
        self._state_path = _state_path()
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        self._log_dir = self._state_path.parent / "logs"
        self._log_dir.mkdir(parents=True, exist_ok=True)
        self._bridge = TelegramArtifactBridge(workspace_dir=self._workspace_dir)
        self._stop_requested = False
        self._zoom_process: subprocess.Popen[Any] | None = None
        self._runner_process: subprocess.Popen[Any] | None = None
        self._finalizer_process: subprocess.Popen[Any] | None = None
        self._log_handles: list[Any] = []
        self._started_at = utcnow_iso()

    def run(self) -> None:
        self._install_signal_handlers()
        self._zoom_process = self._spawn_process(
            "zoom-runtime.log",
            [sys.executable, "-m", "local_meeting_ai_runtime"],
        )
        runner_command = _runner_command()
        if runner_command:
            self._runner_process = self._spawn_process(
                "telegram-runner.log",
                runner_command,
            )
        if _launcher_uses_finisher():
            self._finalizer_process = self._spawn_process(
                "meeting-finisher.log",
                _finisher_command(),
            )
        try:
            self._write_state(status="running")
            while not self._stop_requested:
                try:
                    self._bridge.sync_once()
                except Exception as exc:
                    self._bridge._state["status"] = "degraded"
                    self._bridge._state["last_error"] = str(exc).strip() or exc.__class__.__name__
                self._write_state()
                time.sleep(DEFAULT_POLL_SECONDS)
        finally:
            self._shutdown_children()
            self._write_state(status="stopped")

    def _spawn_process(self, log_name: str, command: list[str]) -> subprocess.Popen[Any]:
        log_path = self._log_dir / log_name
        handle = log_path.open("ab")
        self._log_handles.append(handle)
        kwargs: dict[str, Any] = {
            "cwd": str(self._workspace_dir),
            "stdin": subprocess.DEVNULL,
            "stdout": handle,
            "stderr": handle,
        }
        if os.name == "nt":
            kwargs["creationflags"] = (
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
                | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            )
        return subprocess.Popen(command, **kwargs)

    def _write_state(self, *, status: str | None = None) -> None:
        zoom_pid = int(self._zoom_process.pid) if self._zoom_process else None
        runner_pid = int(self._runner_process.pid) if self._runner_process else None
        finalizer_pid = int(self._finalizer_process.pid) if self._finalizer_process else None
        zoom_alive = bool(zoom_pid and _pid_alive(zoom_pid))
        runner_alive = bool(runner_pid and _pid_alive(runner_pid))
        runner_enabled = _runner_enabled()
        runner_command = _runner_command()
        finalizer_enabled = _launcher_uses_finisher()
        finalizer_alive = bool(finalizer_pid and _pid_alive(finalizer_pid))
        current_status = status or _status_from_process_state(
            supervisor_alive=True,
            zoom_alive=zoom_alive,
            runner_alive=runner_alive,
            runner_required=runner_enabled,
            finalizer_alive=finalizer_alive,
            finalizer_required=finalizer_enabled,
        )
        payload = {
            "status": current_status,
            "started_at": self._started_at,
            "updated_at": utcnow_iso(),
            "workspace_dir": str(self._workspace_dir),
            "supervisor": {
                "pid": os.getpid(),
                "alive": current_status != "stopped",
                "started_at": self._started_at,
                "command": [sys.executable, "-m", "lush_local_ai_launcher", "_supervise"],
            },
            "zoom_runtime": {
                "pid": zoom_pid,
                "alive": zoom_alive,
                "command": [sys.executable, "-m", "local_meeting_ai_runtime"],
            },
            "telegram_runner": {
                "pid": runner_pid,
                "alive": runner_alive,
                "enabled": runner_enabled,
                "command": runner_command or [],
            },
            "finalizer": {
                "pid": finalizer_pid,
                "alive": finalizer_alive,
                "enabled": finalizer_enabled,
                "command": _finisher_command() if finalizer_enabled else [],
            },
            "artifact_bridge": self._bridge.state_snapshot(),
        }
        _write_json_atomic(self._state_path, payload)

    def _shutdown_children(self) -> None:
        for process in (self._zoom_process, self._runner_process, self._finalizer_process):
            if process is None:
                continue
            if process.poll() is None:
                _terminate_pid(process.pid, force=False)
        deadline = time.time() + 8.0
        while time.time() < deadline:
            live = [
                process
                for process in (self._zoom_process, self._runner_process, self._finalizer_process)
                if process is not None and process.poll() is None
            ]
            if not live:
                return
            time.sleep(0.4)
        for process in (self._zoom_process, self._runner_process, self._finalizer_process):
            if process is not None and process.poll() is None:
                _terminate_pid(process.pid, force=True)
        for handle in self._log_handles:
            try:
                handle.close()
            except OSError:
                continue
        self._log_handles.clear()

    def _install_signal_handlers(self) -> None:
        def _handler(_signum: int, _frame: Any) -> None:
            self._stop_requested = True

        signal.signal(signal.SIGINT, _handler)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, _handler)


class TelegramArtifactBridge:
    def __init__(self, *, workspace_dir: Path) -> None:
        self._workspace_dir = workspace_dir
        self._store = SessionStore(path=os.getenv("DELEGATE_STORE_PATH", "data/delegate_sessions.json"))
        self._enabled = _env_bool("DELEGATE_TELEGRAM_ARTIFACT_UPLOAD_ENABLED", True)
        self._bot_name = os.getenv("DELEGATE_TELEGRAM_ARTIFACT_BOT_NAME", "WooBN_bot").strip() or "WooBN_bot"
        self._bot_token_override = os.getenv("DELEGATE_TELEGRAM_BOT_TOKEN", "").strip()
        self._destination_override = os.getenv("DELEGATE_TELEGRAM_ARTIFACT_DESTINATION_ID", "").strip()
        self._destination_label_override = os.getenv("DELEGATE_TELEGRAM_ARTIFACT_DESTINATION_LABEL", "").strip()
        self._chat_id_override = os.getenv("DELEGATE_TELEGRAM_ARTIFACT_CHAT_ID_OVERRIDE", "").strip()
        self._state: dict[str, Any] = {
            "status": "idle",
            "last_scan_at": None,
            "last_error": None,
            "last_session_id": None,
            "processed_handoffs": 0,
        }

    def state_snapshot(self) -> dict[str, Any]:
        return dict(self._state)

    def sync_once(self) -> None:
        self._state["last_scan_at"] = utcnow_iso()
        if not self._enabled:
            self._state["status"] = "disabled"
            return
        self._state["status"] = "running"
        for session in self._store.list_sessions():
            if not self._session_needs_processing(session):
                continue
            try:
                self._process_session(session.session_id)
                self._state["last_session_id"] = session.session_id
            except Exception as exc:
                self._state["last_error"] = str(exc).strip() or exc.__class__.__name__

    def _session_needs_processing(self, session: DelegateSession) -> bool:
        if session.status != "completed":
            return False
        pdf_export = self._summary_pdf_export(session)
        if not pdf_export:
            return False
        existing = self._handoff_for_session(session)
        if existing is None:
            return True
        status = str(existing.get("status") or "").strip()
        if status in {"sent", "blocked", "failed"}:
            return False
        if status == "sending":
            return False
        next_retry_at = self._parse_timestamp(str(existing.get("next_retry_at") or "").strip())
        if next_retry_at is None:
            return True
        return next_retry_at <= datetime.now(UTC)

    def _process_session(self, session_id: str) -> None:
        session = self._store.get_session(session_id)
        if session is None:
            return
        pdf_export = self._summary_pdf_export(session)
        if not pdf_export:
            return
        pdf_path = Path(str(pdf_export.get("path") or ""))
        if not pdf_path.exists():
            return

        self._ensure_handoff(session_id, pdf_path)
        self._mark_handoff(session_id, status="sending")

        try:
            bot_token = self._load_bot_token()
            project_id, destination = self._resolve_artifact_target()
            if project_id:
                self._mark_handoff(session_id, project_id=project_id)
            caption = self._build_caption(session)
            message_id = self._send_document(
                bot_token=bot_token,
                chat_id=str(destination["chat_id"]),
                file_path=pdf_path,
                caption=caption,
            )
        except BlockingBridgeError as exc:
            self._mark_handoff(
                session_id,
                status="blocked",
                last_error=str(exc),
                retryable=False,
                next_retry_at=None,
            )
            return
        except Exception as exc:
            self._mark_handoff_failure(session_id, str(exc).strip() or exc.__class__.__name__)
            return

        self._mark_handoff(
            session_id,
            status="sent",
            message_id=message_id,
            last_error=None,
            retryable=False,
            next_retry_at=None,
            project_id=project_id,
            destination_id=str(destination["id"]),
            destination_label=str(destination.get("label") or ""),
            chat_id=str(destination["chat_id"]),
            caption=caption,
        )
        self._state["processed_handoffs"] = int(self._state.get("processed_handoffs") or 0) + 1

    def _resolve_artifact_target(self) -> tuple[str | None, dict[str, Any]]:
        if self._chat_id_override:
            return (
                None,
                {
                    "id": None,
                    "label": "personal_dm",
                    "chat_id": self._chat_id_override,
                },
            )
        project_id = self._resolve_project_id()
        return project_id, self._resolve_destination(project_id)

    def _ensure_handoff(self, session_id: str, pdf_path: Path) -> None:
        def _mutator(session: DelegateSession) -> None:
            current = self._handoff_for_session(session)
            if current is not None:
                return
            session.artifact_handoffs.append(
                {
                    "handoff_id": f"{session.session_id}:{PDF_HANDOFF_KIND}",
                    "kind": PDF_HANDOFF_KIND,
                    "status": "queued",
                    "bot_name": self._bot_name,
                    "project_id": None,
                    "destination_id": None,
                    "destination_label": None,
                    "chat_id": None,
                    "export_format": "pdf",
                    "export_path": str(pdf_path),
                    "caption": None,
                    "attempt_count": 0,
                    "message_id": None,
                    "retryable": True,
                    "next_retry_at": utcnow_iso(),
                    "last_error": None,
                    "created_at": utcnow_iso(),
                    "updated_at": utcnow_iso(),
                }
            )

        self._store.mutate_session(session_id, _mutator)

    def _mark_handoff_failure(self, session_id: str, error_message: str) -> None:
        def _mutator(session: DelegateSession) -> None:
            handoff = self._handoff_for_session(session)
            if handoff is None:
                return
            attempt_count = max(int(handoff.get("attempt_count") or 0), 1)
            handoff["attempt_count"] = attempt_count
            handoff["updated_at"] = utcnow_iso()
            handoff["last_error"] = error_message
            if attempt_count >= len(RETRY_DELAYS_SECONDS):
                handoff["status"] = "failed"
                handoff["retryable"] = False
                handoff["next_retry_at"] = None
                return
            handoff["status"] = "queued"
            handoff["retryable"] = True
            delay_seconds = RETRY_DELAYS_SECONDS[attempt_count]
            handoff["next_retry_at"] = (datetime.now(UTC) + timedelta(seconds=delay_seconds)).isoformat()

        self._store.mutate_session(session_id, _mutator)

    def _mark_handoff(self, session_id: str, **updates: Any) -> None:
        def _mutator(session: DelegateSession) -> None:
            handoff = self._handoff_for_session(session)
            if handoff is None:
                return
            increment_attempt = updates.get("status") == "sending"
            if "status" in updates:
                handoff["status"] = updates["status"]
            for key in (
                "project_id",
                "destination_id",
                "destination_label",
                "chat_id",
                "message_id",
                "caption",
                "last_error",
                "retryable",
                "next_retry_at",
            ):
                if key in updates:
                    handoff[key] = updates[key]
            if increment_attempt:
                handoff["attempt_count"] = int(handoff.get("attempt_count") or 0) + 1
            handoff["updated_at"] = utcnow_iso()

        self._store.mutate_session(session_id, _mutator)

    def _resolve_project_id(self) -> str:
        configured = os.getenv("DELEGATE_PROJECT_ID", "").strip()
        if configured:
            return configured
        workspaces_path = Path.home() / ".metheus" / "project-workspaces.json"
        payload = _read_json(workspaces_path)
        bindings = dict(payload.get("project_workspaces") or {})
        current_workspace = str(self._workspace_dir).casefold()
        for project_id, info in bindings.items():
            workspace_dir = str(dict(info or {}).get("workspace_dir") or "").casefold()
            if workspace_dir == current_workspace:
                return str(project_id)
        raise BlockingBridgeError("No bound project_id matched the current workspace for Telegram artifact delivery.")

    def _resolve_destination(self, project_id: str) -> dict[str, Any]:
        destinations = self._list_destinations(project_id)
        if self._destination_override:
            for item in destinations:
                if str(item.get("id") or "") == self._destination_override:
                    return item
            raise BlockingBridgeError("Configured Telegram artifact destination id was not found in project destinations.")
        if self._destination_label_override:
            for item in destinations:
                if str(item.get("label") or "").strip() == self._destination_label_override:
                    return item
            raise BlockingBridgeError("Configured Telegram artifact destination label was not found in project destinations.")
        active = [item for item in destinations if item.get("provider") == "telegram" and bool(item.get("is_active"))]
        if len(active) == 1:
            return active[0]
        if not active:
            raise BlockingBridgeError("No active Telegram destination is configured for this project.")
        raise BlockingBridgeError("Multiple active Telegram destinations exist; explicit destination id is required.")

    def _list_destinations(self, project_id: str) -> list[dict[str, Any]]:
        auth = self._load_governance_auth()
        headers = {"Authorization": f"Bearer {auth['token']}"}
        url = f"{auth['base_url'].rstrip('/')}/api/v1/projects/{project_id}/chat-destinations"
        with httpx.Client(timeout=20.0) as client:
            response = client.get(url, headers=headers)
            if response.status_code == 401:
                auth = self._refresh_governance_auth(auth)
                headers = {"Authorization": f"Bearer {auth['token']}"}
                response = client.get(url, headers=headers)
            response.raise_for_status()
        payload = response.json()
        if isinstance(payload, dict) and isinstance(payload.get("body"), list):
            return [dict(item) for item in payload["body"] if isinstance(item, dict)]
        if isinstance(payload, list):
            return [dict(item) for item in payload if isinstance(item, dict)]
        return []

    def _load_governance_auth(self) -> dict[str, Any]:
        auth_path = Path.home() / ".metheus" / "governance-mcp-auth.json"
        payload = _read_json(auth_path)
        token = str(payload.get("token") or "").strip()
        refresh_token = str(payload.get("refresh_token") or "").strip()
        base_url = str(payload.get("base_url") or "").strip()
        if not token or not refresh_token or not base_url:
            raise BlockingBridgeError("Governance auth is not available for project destination resolution.")
        payload["token_endpoint"] = self._token_endpoint_from_token(token)
        payload["_auth_path"] = str(auth_path)
        return payload

    def _refresh_governance_auth(self, auth: dict[str, Any]) -> dict[str, Any]:
        token_endpoint = str(auth.get("token_endpoint") or "").strip()
        if not token_endpoint:
            raise BlockingBridgeError("Governance auth token endpoint could not be determined.")
        refresh_token = str(auth.get("refresh_token") or "").strip()
        if not refresh_token:
            raise BlockingBridgeError("Governance refresh token is missing.")
        client_id = str(self._jwt_payload(str(auth.get("token") or "")).get("azp") or "metheus-governance").strip()
        with httpx.Client(timeout=20.0) as client:
            response = client.post(
                token_endpoint,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": client_id,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            response.raise_for_status()
            refreshed = response.json()
        auth["token"] = str(refreshed.get("access_token") or "").strip() or str(auth["token"])
        if refreshed.get("refresh_token"):
            auth["refresh_token"] = str(refreshed["refresh_token"]).strip()
        auth["updated_at"] = utcnow_iso()
        auth_path = Path(str(auth.get("_auth_path") or ""))
        if auth_path:
            persisted = {
                "token": auth["token"],
                "refresh_token": auth["refresh_token"],
                "base_url": auth["base_url"],
                "updated_at": auth["updated_at"],
            }
            _write_json_atomic(auth_path, persisted)
        return auth

    def _load_bot_token(self) -> str:
        if self._bot_token_override:
            return self._bot_token_override
        env_path = Path.home() / ".metheus" / "telegram-bots" / f"{self._bot_name}.env"
        if not env_path.exists():
            raise BlockingBridgeError(f"Telegram bot env file was not found for {self._bot_name}.")
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key.strip() == "TELEGRAM_BOT_TOKEN":
                token = value.strip().strip('"').strip("'")
                if token:
                    return token
        raise BlockingBridgeError(f"TELEGRAM_BOT_TOKEN was not found in {env_path}.")

    def _send_document(self, *, bot_token: str, chat_id: str, file_path: Path, caption: str) -> int:
        url = f"https://api.telegram.org/bot{bot_token}/sendDocument"
        with file_path.open("rb") as handle:
            with httpx.Client(timeout=30.0) as client:
                response = client.post(
                    url,
                    data={
                        "chat_id": chat_id,
                        "caption": caption,
                        "disable_content_type_detection": "false",
                    },
                    files={
                        "document": (
                            file_path.name,
                            handle,
                            "application/pdf",
                        )
                    },
                )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(str(payload))
        return int(((payload.get("result") or {}) or {}).get("message_id") or 0)

    def _build_caption(self, session: DelegateSession) -> str:
        briefing = dict((session.summary_packet or {}).get("briefing") or {})
        title = self._display_title(
            primary=str(briefing.get("title") or ""),
            fallback=str(session.meeting_topic or ""),
            meeting_number=str(session.meeting_number or ""),
            sections=list(briefing.get("sections") or []),
            executive_summary=str(briefing.get("executive_summary") or ""),
            session_id=session.session_id,
        )
        completed_at = self._display_time(session.updated_at or session.created_at)
        return f"[회의 요약 PDF] {title}\n완료: {completed_at}"

    def _display_title(
        self,
        *,
        primary: str,
        fallback: str,
        meeting_number: str,
        sections: list[Any],
        executive_summary: str,
        session_id: str,
    ) -> str:
        for candidate in (str(primary or "").strip(), str(fallback or "").strip()):
            if candidate and not self._looks_like_broken_title(candidate):
                return candidate
        section_title = self._section_title_fallback(sections)
        if section_title:
            return section_title
        summary_head = str(executive_summary or "").strip().split(".", 1)[0].strip()
        if summary_head and not self._looks_like_broken_title(summary_head):
            return summary_head[:80]
        if meeting_number:
            return f"Zoom 회의 {meeting_number}"
        return session_id or "회의 요약"

    def _looks_like_broken_title(self, value: str) -> bool:
        text = str(value or "").strip()
        if not text:
            return True
        if "�" in text or text.count("?") >= 3:
            return True
        lowered = text.lower()
        if lowered in {"zoom", "zoom meeting", "zoom 회의", "meeting", "회의"}:
            return True
        return lowered.startswith("zoom") and all(ch in "zoom -:_?？!./" for ch in lowered)

    def _is_generic_title_candidate(self, value: str) -> bool:
        lowered = str(value or "").strip().lower()
        return lowered in {
            "회의 흐름 요약",
            "결정과 후속 작업",
            "남은 질문과 리스크",
            "회의 메모",
            "회의 전체 요약",
            "결정사항",
            "액션 아이템",
            "열린 질문",
        }

    def _section_title_fallback(self, sections: list[Any]) -> str:
        headings: list[str] = []
        for item in sections:
            if not isinstance(item, dict):
                continue
            heading = str(item.get("heading") or "").strip()
            if not heading or self._looks_like_broken_title(heading) or self._is_generic_title_candidate(heading):
                continue
            if heading not in headings:
                headings.append(heading)
        if not headings:
            return ""
        if len(headings) == 1:
            return headings[0]
        return f"{headings[0]} · {headings[1]}"[:80]

    def _display_time(self, value: str) -> str:
        parsed = self._parse_timestamp(value)
        if parsed is None:
            return value
        local_dt = parsed.astimezone().replace(second=0, microsecond=0)
        return local_dt.strftime("%Y-%m-%d %H:%M %Z")

    def _summary_pdf_export(self, session: DelegateSession) -> dict[str, Any] | None:
        for item in session.summary_exports:
            if str(item.get("format") or "").strip().lower() == "pdf":
                return dict(item)
        return None

    def _handoff_for_session(self, session: DelegateSession) -> dict[str, Any] | None:
        for item in reversed(session.artifact_handoffs):
            if str(item.get("kind") or "").strip() == PDF_HANDOFF_KIND:
                return item
        return None

    def _parse_timestamp(self, value: str) -> datetime | None:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            return datetime.fromisoformat(text)
        except ValueError:
            return None

    def _token_endpoint_from_token(self, token: str) -> str:
        issuer = str(self._jwt_payload(token).get("iss") or "").strip().rstrip("/")
        if issuer:
            return f"{issuer}/protocol/openid-connect/token"
        return "https://oauth3.gesia.io/realms/master/protocol/openid-connect/token"

    def _jwt_payload(self, token: str) -> dict[str, Any]:
        parts = str(token or "").split(".")
        if len(parts) < 2:
            return {}
        payload = parts[1]
        padding = "=" * (-len(payload) % 4)
        try:
            decoded = base64.urlsafe_b64decode((payload + padding).encode("utf-8"))
            parsed = json.loads(decoded.decode("utf-8"))
        except (ValueError, json.JSONDecodeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}


class BlockingBridgeError(RuntimeError):
    """Raised when bridge prerequisites are missing and retry should not continue."""


def run_supervisor() -> None:
    LauncherSupervisor().run()


def _runner_command() -> list[str]:
    if not _runner_enabled():
        return []
    backend = os.getenv("LUSH_TELEGRAM_RUNNER_BACKEND", "").strip().lower()
    if backend == "none":
        return []
    route_name = os.getenv("LUSH_TELEGRAM_RUNNER_ROUTE_NAME", "telegram-monitor-woobn-bot").strip()
    resolved = shutil.which("metheus-governance-mcp-cli") or shutil.which("metheus-governance-mcp-cli.cmd")
    if not resolved:
        resolved = str(Path.home() / "AppData" / "Roaming" / "npm" / "metheus-governance-mcp-cli.cmd")
    if resolved.lower().endswith((".cmd", ".bat")):
        return [
            os.environ.get("COMSPEC", "cmd.exe"),
            "/c",
            resolved,
            "runner",
            "start",
            "--route-name",
            route_name,
        ]
    return [resolved, "runner", "start", "--route-name", route_name]


def _runner_enabled() -> bool:
    backend = os.getenv("LUSH_TELEGRAM_RUNNER_BACKEND", "").strip().lower()
    if backend == "none":
        return False
    if backend:
        return True
    return _env_bool("LUSH_TELEGRAM_RUNNER_ENABLED", True)


def _launcher_uses_finisher() -> bool:
    explicit = os.getenv("LUSH_LOCAL_FINISHER_ENABLED")
    if explicit is not None:
        return _env_bool("LUSH_LOCAL_FINISHER_ENABLED", False)
    completion_mode = str(os.getenv("DELEGATE_SESSION_COMPLETION_MODE", "inline") or "").strip().lower()
    return completion_mode == "queued"


def _finisher_command() -> list[str]:
    limit = max(int(os.getenv("LUSH_LOCAL_FINISHER_LIMIT", "1") or 1), 1)
    poll_seconds = max(float(os.getenv("LUSH_LOCAL_FINISHER_POLL_SECONDS", "5") or 5.0), 0.1)
    runner_id = os.getenv("LUSH_LOCAL_FINISHER_RUNNER_ID", "launcher-meeting-finisher").strip()
    if not runner_id:
        runner_id = "launcher-meeting-finisher"
    return [
        sys.executable,
        "-m",
        "local_meeting_ai_runtime",
        "finalizer",
        "run-loop",
        "--limit",
        str(limit),
        "--poll-seconds",
        str(poll_seconds),
        "--runner-id",
        runner_id,
    ]


def _state_path() -> Path:
    configured = os.getenv("LUSH_LAUNCHER_STATE_PATH", "").strip()
    if configured:
        return Path(configured).resolve()
    return (Path.cwd() / DEFAULT_STATE_PATH).resolve()


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


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


def _pid_matches_entry(entry_name: str, pid: int, entry: dict[str, Any]) -> bool:
    if not _pid_alive(pid):
        return False
    command = list(entry.get("command") or [])
    if not command:
        command = _default_entry_command(entry_name, entry)
    if not command or os.name != "nt":
        return True
    info = _windows_process_info(pid)
    if not info:
        return False
    command_line = str(info.get("command_line") or "").strip().lower()
    executable_path = str(info.get("executable_path") or "").strip().lower()
    expected_tokens = [str(item).strip().lower() for item in command if str(item).strip()]
    if not expected_tokens:
        return True
    executable_name = Path(expected_tokens[0]).name.lower()
    if executable_name and executable_path and Path(executable_path).name.lower() != executable_name:
        return False
    for token in expected_tokens[1:]:
        if token not in command_line:
            return False
    return True


def _default_entry_command(entry_name: str, entry: dict[str, Any]) -> list[str]:
    if entry_name == "supervisor":
        return [sys.executable, "-m", "lush_local_ai_launcher", "_supervise"]
    if entry_name == "zoom_runtime":
        return [sys.executable, "-m", "local_meeting_ai_runtime"]
    if entry_name == "finalizer" and bool(entry.get("enabled", _launcher_uses_finisher())):
        return _finisher_command()
    if entry_name == "telegram_runner" and bool(entry.get("enabled", _runner_enabled())):
        return _runner_command()
    return []


def _windows_process_info(pid: int) -> dict[str, str]:
    if os.name != "nt" or pid <= 0:
        return {}
    script = (
        f"$proc = Get-CimInstance Win32_Process -Filter \"ProcessId = {pid}\"; "
        "if ($null -eq $proc) { exit 1 }; "
        "[pscustomobject]@{ExecutablePath=$proc.ExecutablePath; CommandLine=$proc.CommandLine} "
        "| ConvertTo-Json -Compress"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
        **_windows_hidden_process_kwargs(),
    )
    if result.returncode != 0:
        return {}
    payload = result.stdout.decode(errors="ignore").strip()
    if not payload:
        return {}
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {
        "executable_path": str(parsed.get("ExecutablePath") or ""),
        "command_line": str(parsed.get("CommandLine") or ""),
    }


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
    try:  # pragma: no cover - non-Windows fallback
        os.kill(pid, signal.SIGKILL if force else signal.SIGTERM)
    except OSError:
        return


def _load_dotenv() -> None:
    env_path = Path(".env")
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp_path, path)
