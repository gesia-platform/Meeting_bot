"""File-backed storage for delegate sessions and runner jobs."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Lock

from .models import DelegateSession, RunnerJob, session_from_dict, utcnow_iso

try:
    import msvcrt
except ImportError:  # pragma: no cover - non-Windows fallback
    msvcrt = None

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None


class _InterProcessFileLock:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._handle = None

    def __enter__(self) -> "_InterProcessFileLock":
        self._path.parent.mkdir(parents=True, exist_ok=True)
        handle = self._path.open("a+b")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if msvcrt is not None:
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        elif fcntl is not None:  # pragma: no cover - POSIX fallback
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        self._handle = handle
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        handle = self._handle
        if handle is None:
            return
        handle.seek(0)
        if msvcrt is not None:
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        elif fcntl is not None:  # pragma: no cover - POSIX fallback
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()
        self._handle = None


def _decode_json_payload(raw_text: str) -> dict[str, dict]:
    normalized = raw_text.lstrip("\ufeff").strip()
    if not normalized:
        return {}
    try:
        payload = json.loads(normalized)
        if not isinstance(payload, dict):
            raise ValueError("Expected JSON object payload.")
        return payload
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        cursor = 0
        merged: dict[str, dict] = {}
        parsed_any = False
        while cursor < len(normalized):
            while cursor < len(normalized) and normalized[cursor].isspace():
                cursor += 1
            if cursor >= len(normalized):
                break
            try:
                value, cursor = decoder.raw_decode(normalized, cursor)
            except json.JSONDecodeError:
                if parsed_any:
                    return merged
                raise
            if not isinstance(value, dict):
                if parsed_any:
                    return merged
                raise ValueError("Expected concatenated JSON documents to be objects.")
            merged.update(value)
            parsed_any = True
        return merged


def _write_json_atomic(path: Path, payload: dict[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(payload, ensure_ascii=False, indent=2)
    fd, temp_path = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


class SessionStore:
    def __init__(self, path: str = "data/delegate_sessions.json") -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._file_lock = _InterProcessFileLock(self._path.with_suffix(f"{self._path.suffix}.lock"))

    def _load_raw(self) -> dict[str, dict]:
        if not self._path.exists():
            return {}
        return _decode_json_payload(self._path.read_text(encoding="utf-8"))

    def _save_raw(self, payload: dict[str, dict]) -> None:
        _write_json_atomic(self._path, payload)

    def list_sessions(self) -> list[DelegateSession]:
        with self._lock:
            with self._file_lock:
                payload = self._load_raw()
        return [session_from_dict(item) for item in payload.values()]

    def get_session(self, session_id: str) -> DelegateSession | None:
        with self._lock:
            with self._file_lock:
                payload = self._load_raw()
        raw = payload.get(session_id)
        if raw is None:
            return None
        return session_from_dict(raw)

    def save_session(self, session: DelegateSession) -> DelegateSession:
        with self._lock:
            with self._file_lock:
                payload = self._load_raw()
                payload[session.session_id] = session.to_dict()
                self._save_raw(payload)
        return session

    def mutate_session(
        self,
        session_id: str,
        mutator,
    ) -> DelegateSession | None:
        with self._lock:
            with self._file_lock:
                payload = self._load_raw()
                raw = payload.get(session_id)
                if raw is None:
                    return None
                session = session_from_dict(raw)
                mutator(session)
                session.touch()
                payload[session.session_id] = session.to_dict()
                self._save_raw(payload)
        return session


class RunnerQueueStore:
    def __init__(self, path: str = "data/runner_queue.json") -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._file_lock = _InterProcessFileLock(self._path.with_suffix(f"{self._path.suffix}.lock"))

    def _load_raw(self) -> dict[str, dict]:
        if not self._path.exists():
            return {}
        return _decode_json_payload(self._path.read_text(encoding="utf-8"))

    def _save_raw(self, payload: dict[str, dict]) -> None:
        _write_json_atomic(self._path, payload)

    def list_jobs(
        self,
        *,
        status: str | None = None,
        session_id: str | None = None,
        limit: int | None = None,
    ) -> list[RunnerJob]:
        with self._lock:
            with self._file_lock:
                payload = self._load_raw()
                self._requeue_expired_leases(payload)
                self._save_raw(payload)
                jobs = [RunnerJob(**item) for item in payload.values()]
        if status:
            jobs = [job for job in jobs if job.status == status]
        if session_id:
            jobs = [job for job in jobs if job.session_id == session_id]
        jobs.sort(key=lambda item: item.created_at)
        if limit is not None:
            jobs = jobs[: max(limit, 0)]
        return jobs

    def get_job(self, job_id: str) -> RunnerJob | None:
        with self._lock:
            with self._file_lock:
                payload = self._load_raw()
        raw = payload.get(job_id)
        if raw is None:
            return None
        return RunnerJob(**raw)

    def enqueue_job(self, job: RunnerJob) -> RunnerJob:
        with self._lock:
            with self._file_lock:
                payload = self._load_raw()
                payload[job.job_id] = job.to_dict()
                self._save_raw(payload)
        return job

    def count_jobs(self, *, session_id: str | None = None, statuses: set[str] | None = None) -> int:
        with self._lock:
            with self._file_lock:
                payload = self._load_raw()
                self._requeue_expired_leases(payload)
                self._save_raw(payload)
        count = 0
        for raw in payload.values():
            if session_id and raw.get("session_id") != session_id:
                continue
            if statuses and raw.get("status") not in statuses:
                continue
            count += 1
        return count

    def lease_jobs(
        self,
        *,
        limit: int = 10,
        runner_id: str = "delegate-runner",
        lease_seconds: int = 120,
    ) -> list[RunnerJob]:
        leased: list[RunnerJob] = []
        with self._lock:
            with self._file_lock:
                payload = self._load_raw()
                self._requeue_expired_leases(payload, lease_seconds=lease_seconds)
                queued_items = sorted(
                    (item for item in payload.values() if item.get("status") == "queued"),
                    key=lambda item: item.get("created_at", ""),
                )
                for raw in queued_items[: max(limit, 0)]:
                    raw["status"] = "leased"
                    raw["lease_owner"] = runner_id
                    raw["leased_at"] = utcnow_iso()
                    raw["attempts"] = int(raw.get("attempts") or 0) + 1
                    leased.append(RunnerJob(**raw))
                self._save_raw(payload)
        return leased

    def complete_job(self, job_id: str, result: dict) -> RunnerJob:
        with self._lock:
            with self._file_lock:
                payload = self._load_raw()
                raw = payload.get(job_id)
                if raw is None:
                    raise KeyError(f"Unknown runner job: {job_id}")
                raw["status"] = "completed"
                raw["result"] = result
                raw["completed_at"] = utcnow_iso()
                raw["lease_owner"] = None
                self._save_raw(payload)
                return RunnerJob(**raw)

    def fail_job(self, job_id: str, error: str, *, requeue: bool = False) -> RunnerJob:
        with self._lock:
            with self._file_lock:
                payload = self._load_raw()
                raw = payload.get(job_id)
                if raw is None:
                    raise KeyError(f"Unknown runner job: {job_id}")
                raw["last_error"] = error
                raw["lease_owner"] = None
                if requeue:
                    raw["status"] = "queued"
                    raw["leased_at"] = None
                else:
                    raw["status"] = "failed"
                    raw["completed_at"] = utcnow_iso()
                self._save_raw(payload)
                return RunnerJob(**raw)

    def _requeue_expired_leases(self, payload: dict[str, dict], *, lease_seconds: int = 120) -> None:
        now = datetime.now(UTC)
        for raw in payload.values():
            if raw.get("status") != "leased":
                continue
            leased_at = self._parse_timestamp(raw.get("leased_at"))
            if leased_at is None:
                raw["status"] = "queued"
                raw["lease_owner"] = None
                continue
            if leased_at + timedelta(seconds=max(lease_seconds, 1)) <= now:
                raw["status"] = "queued"
                raw["lease_owner"] = None
                raw["leased_at"] = None
                raw["last_error"] = "Previous runner lease expired before completion."

    def _parse_timestamp(self, value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
