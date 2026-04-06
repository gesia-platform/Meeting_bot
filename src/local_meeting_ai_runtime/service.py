"""Core service layer for the local meeting AI runtime."""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime, timedelta, timezone
import importlib.util
import json
import os
from pathlib import Path
import queue
import re
import shutil
import threading
from typing import Any
from uuid import uuid4

from .ai_client import AiDelegateClient
from .artifact_exporter import ArtifactExportError, MeetingArtifactExporter
from .local_observer import LocalObserver, LocalObserverError
from .meeting_adapter import MeetingSdkAdapter
from .models import (
    ApprovalRequest,
    ChatTurn,
    DelegateSession,
    MeetingInput,
    RunnerJob,
    TranscriptChunk,
    WorkspaceEvent,
    utcnow_iso,
)
from .storage import RunnerQueueStore, SessionStore
from .summary_pipeline import DelegateSummaryPipeline
from .zoom_client import ZoomRestClient, ZoomRuntimeError

ALLOWED_SESSION_STATUSES = {"planned", "joining", "active", "suspected_ended", "blocked", "completed"}
KST = timezone(timedelta(hours=9), name="KST")


class DelegateService:
    def __init__(
        self,
        *,
        store: SessionStore,
        zoom_client: ZoomRestClient,
        ai_client: AiDelegateClient,
        meeting_adapter: MeetingSdkAdapter,
        local_observer: LocalObserver | None = None,
        summary_pipeline: DelegateSummaryPipeline | None = None,
        artifact_exporter: MeetingArtifactExporter | None = None,
        export_dir: str | Path | None = None,
        runner_store: RunnerQueueStore | None = None,
    ) -> None:
        self._store = store
        self._runner_store = runner_store or RunnerQueueStore(
            path=os.getenv("DELEGATE_RUNNER_QUEUE_PATH", "data/runner_queue.json")
        )
        self._zoom = zoom_client
        self._ai = ai_client
        self._meeting = meeting_adapter
        self._observer = local_observer or LocalObserver()
        self._summary_pipeline = summary_pipeline or DelegateSummaryPipeline()
        self._artifact_exporter = artifact_exporter or MeetingArtifactExporter()
        self._export_dir = Path(export_dir or os.getenv("DELEGATE_EXPORT_DIR", "data/exports"))
        self._export_dir.mkdir(parents=True, exist_ok=True)
        self._audio_archive_dir = Path(os.getenv("DELEGATE_AUDIO_ARCHIVE_DIR", "data/audio"))
        self._audio_archive_dir.mkdir(parents=True, exist_ok=True)
        self._auto_cleanup_enabled = self._env_bool("DELEGATE_AUTO_CLEANUP_ENABLED", True)
        self._audio_keep_session_count = max(self._env_int("DELEGATE_AUDIO_KEEP_SESSION_COUNT", 25), 0)
        self._observer_tmp_retention_hours = max(self._env_int("DELEGATE_OBSERVER_TMP_RETENTION_HOURS", 6), 0)
        self._audio_observer_tasks: dict[str, asyncio.Task[None]] = {}
        self._audio_observer_controls: dict[str, dict[str, Any]] = {}
        self._completion_locks: dict[str, asyncio.Lock] = {}
        self._auto_observe_audio_mode = (
            os.getenv("DELEGATE_AUTO_OBSERVE_AUDIO_MODE", "conversation").strip().lower() or "conversation"
        )
        self._source_aware_speakers = self._env_bool("DELEGATE_SOURCE_AWARE_SPEAKERS", True)
        self._local_user_speaker_name = (
            os.getenv("DELEGATE_LOCAL_USER_SPEAKER_NAME", "local_user").strip() or "local_user"
        )
        self._remote_participant_speaker_name = (
            os.getenv("DELEGATE_REMOTE_PARTICIPANT_SPEAKER_NAME", "remote_participant").strip()
            or "remote_participant"
        )
        self._dedicated_meeting_output_device_name = (
            os.getenv("DELEGATE_LOCAL_MEETING_OUTPUT_DEVICE", "").strip()
            or getattr(self._observer, "meeting_output_device_name", "")
            or "스피커(USB Audio Device)"
        )
        self._continuous_conversation_live = self._env_bool("DELEGATE_CONTINUOUS_CONVERSATION_LIVE", True)
        self._continuous_audio_frame_ms = max(self._env_int("DELEGATE_CONTINUOUS_AUDIO_FRAME_MS", 250), 50)
        self._continuous_audio_preroll_ms = max(self._env_int("DELEGATE_CONTINUOUS_AUDIO_PREROLL_MS", 500), 0)
        self._continuous_audio_silence_ms = max(self._env_int("DELEGATE_CONTINUOUS_AUDIO_SILENCE_MS", 650), 100)
        self._continuous_audio_min_segment_ms = max(self._env_int("DELEGATE_CONTINUOUS_AUDIO_MIN_SEGMENT_MS", 450), 100)
        self._continuous_audio_queue_size = max(self._env_int("DELEGATE_CONTINUOUS_AUDIO_QUEUE_SIZE", 32), 4)
        self._session_heartbeat_timeout_seconds = max(
            self._env_float("DELEGATE_SESSION_HEARTBEAT_TIMEOUT_SECONDS", 90.0),
            5.0,
        )
        self._session_inactivity_grace_seconds = max(
            self._env_float("DELEGATE_SESSION_INACTIVITY_GRACE_SECONDS", 60.0),
            10.0,
        )
        self._session_suspected_end_seconds = max(
            self._env_float("DELEGATE_SESSION_SUSPECTED_END_SECONDS", 30.0),
            10.0,
        )
        self._session_observer_stall_seconds = max(
            self._env_float("DELEGATE_SESSION_OBSERVER_STALL_SECONDS", 75.0),
            10.0,
        )
        self._session_watchdog_poll_seconds = max(
            self._env_float("DELEGATE_SESSION_WATCHDOG_POLL_SECONDS", 5.0),
            1.0,
        )
        if self._auto_cleanup_enabled:
            self._run_storage_housekeeping()

    async def create_session(self, payload: dict[str, Any], *, base_url: str | None = None) -> DelegateSession:
        meeting_payload: dict[str, Any] = {}
        meeting_id = str(payload.get("meeting_id") or "").strip()
        if meeting_id:
            try:
                meeting_payload = await self._zoom.get_meeting(meeting_id)
            except ZoomRuntimeError:
                manual_fields_present = any(
                    str(payload.get(name) or "").strip()
                    for name in ("meeting_topic", "join_url", "meeting_number", "passcode")
                )
                if not manual_fields_present:
                    raise

        session = DelegateSession(
            session_id=uuid4().hex[:12],
            delegate_mode=str(payload.get("delegate_mode") or "answer_on_ask"),
            bot_display_name=str(
                payload.get("bot_display_name")
                or os.getenv("DELEGATE_BOT_DISPLAY_NAME")
                or "WooBIN_bot"
            ),
            meeting_id=str(meeting_payload.get("id") or meeting_id or "") or None,
            meeting_uuid=str(meeting_payload.get("uuid") or payload.get("meeting_uuid") or "").strip() or None,
            meeting_topic=str(meeting_payload.get("topic") or payload.get("meeting_topic") or "").strip() or None,
            join_url=str(meeting_payload.get("join_url") or payload.get("join_url") or "").strip() or None,
            meeting_number=str(
                meeting_payload.get("id")
                or payload.get("meeting_number")
                or payload.get("meeting_id")
                or ""
            ).strip()
            or None,
            passcode=str(meeting_payload.get("password") or payload.get("passcode") or "").strip() or None,
            requested_by=str(payload.get("requested_by") or "").strip() or None,
            instructions=str(payload.get("instructions") or "").strip() or None,
        )
        session.preflight = self._build_preflight(session, meeting_payload)
        session.join_ticket = self._meeting.build_join_ticket(session, base_url=base_url)
        if session.preflight["readiness"] == "blocked":
            session.status = "blocked"
            session.status_reason = "Preflight found blocking issues."
        session.runner_state = {"status": "idle"}
        self._retire_conflicting_sessions(session)
        saved = self.persist_session(session)
        await self._maybe_prime_ai_thread(saved)
        return saved

    def list_sessions(self) -> list[DelegateSession]:
        return self._store.list_sessions()

    def get_session(self, session_id: str) -> DelegateSession | None:
        return self._store.get_session(session_id)

    def persist_session(self, session: DelegateSession) -> DelegateSession:
        session.touch()
        return self._store.save_session(session)

    def find_session_by_meeting(self, meeting_id: str) -> DelegateSession | None:
        normalized = str(meeting_id or "").strip()
        if not normalized:
            return None
        matches = [
            session
            for session in self._store.list_sessions()
            if normalized in {str(session.meeting_id or ""), str(session.meeting_number or "")}
        ]
        if not matches:
            return None
        matches.sort(key=lambda item: item.updated_at, reverse=True)
        return matches[0]

    async def start_session(self, session_id: str, *, base_url: str) -> tuple[DelegateSession, dict[str, Any]]:
        session = self._require_session(session_id)
        launch = await self._meeting.join(session, base_url=base_url)
        session.join_ticket = {**session.join_ticket, **launch}
        session.status = "joining" if launch["status"] in {"browser_ready", "desktop_ready"} else "blocked"
        session.status_reason = launch["message"]
        saved = self.persist_session(session)
        await self._maybe_prime_ai_thread(saved)
        return saved, launch

    def build_meeting_sdk_config(self, session_id: str, *, base_url: str) -> dict[str, Any]:
        session = self._require_session(session_id)
        config = self._meeting.build_client_config(session, base_url=base_url)
        session.join_ticket = self._meeting.build_join_ticket(session, base_url=base_url)
        self.persist_session(session)
        return config

    def refresh_join_ticket(self, session_id: str, *, base_url: str | None = None) -> dict[str, Any]:
        session = self._require_session(session_id)
        session.join_ticket = self._meeting.build_join_ticket(session, base_url=base_url)
        self.persist_session(session)
        return session.join_ticket

    def update_status(self, session_id: str, *, status: str, reason: str | None = None) -> DelegateSession:
        if status not in ALLOWED_SESSION_STATUSES:
            raise ValueError(f"Unsupported session status: {status}")
        session = self._require_session(session_id)
        session.status = status
        session.status_reason = reason
        session.join_ticket["last_status_report"] = {
            "status": status,
            "reason": reason,
            "reported_at": utcnow_iso(),
        }
        return self.persist_session(session)

    def record_shell_heartbeat(self, session_id: str, payload: dict[str, Any] | None = None) -> tuple[DelegateSession, dict[str, Any]]:
        session = self._require_session(session_id)
        data = dict(payload or {})
        heartbeat_at = str(data.get("heartbeat_at") or utcnow_iso()).strip() or utcnow_iso()
        shell_state = dict(session.ai_state.get("shell_liveness") or {})
        joined = bool(data.get("joined")) or bool(shell_state.get("joined"))
        completion_armed = (
            bool(data.get("completion_armed"))
            or bool(shell_state.get("completion_armed"))
            or joined
        )
        shell_state.update(
            {
                "last_heartbeat_at": heartbeat_at,
                "joined": joined,
                "completion_armed": completion_armed,
                "audio_observer_running": bool(data.get("audio_observer_running")),
                "visibility_state": str(data.get("visibility_state") or "").strip() or None,
                "launch_mode": str(data.get("launch_mode") or "").strip() or None,
                "last_reason": str(data.get("reason") or "heartbeat").strip() or "heartbeat",
                "last_user_agent": str(data.get("user_agent") or "").strip() or None,
            }
        )
        shell_state.setdefault("first_heartbeat_at", heartbeat_at)
        if joined and not str(shell_state.get("joined_at") or "").strip():
            shell_state["joined_at"] = heartbeat_at
        if completion_armed and not str(shell_state.get("armed_at") or "").strip():
            shell_state["armed_at"] = heartbeat_at
        session.ai_state["shell_liveness"] = shell_state
        if completion_armed or joined:
            self._reactivate_session(
                session,
                reason="The local control shell heartbeat is still alive.",
                signal="shell_heartbeat",
            )
        saved = self.persist_session(session)
        watch_state = dict(saved.ai_state.get("meeting_end_watch") or {})
        return saved, {
            "heartbeat_at": heartbeat_at,
            "watch_state": watch_state,
            "shell_liveness": dict(saved.ai_state.get("shell_liveness") or {}),
        }

    async def append_transcript(self, session_id: str, payload: dict[str, Any]) -> DelegateSession:
        session = self._require_session(session_id)
        if session.status == "completed":
            raise ValueError("This meeting session is already completed, so new transcript input is not accepted.")
        self._record_input(
            session,
            input_type="spoken_transcript",
            speaker=str(payload.get("speaker") or "unknown").strip() or "unknown",
            text=str(payload.get("text") or "").strip(),
            source=str(payload.get("source") or "manual").strip() or "manual",
            direct_question=bool(payload.get("direct_question", False)),
            metadata=dict(payload.get("metadata") or {}),
        )
        self._append_transcript_chunk(session, payload)
        session.summary_packet = self._summary_pipeline.build(session)
        self._reactivate_session(
            session,
            reason="New meeting transcript arrived.",
            signal="spoken_transcript",
        )
        return self.persist_session(session)

    async def append_chat_turn(self, session_id: str, payload: dict[str, Any]) -> DelegateSession:
        session = self._require_session(session_id)
        if session.status == "completed":
            raise ValueError("This meeting session is already completed, so new chat input is not accepted.")
        if self._should_ignore_chat_input(session, payload):
            return session
        role = str(payload.get("role") or "participant").strip().lower() or "participant"
        source = str(payload.get("source") or "meeting_chat").strip() or "meeting_chat"
        self._record_input(
            session,
            input_type="bot_reply" if role == "bot" else "meeting_chat",
            speaker=str(payload.get("speaker") or role).strip() or role,
            text=str(payload.get("text") or "").strip(),
            source=source,
            direct_question=bool(payload.get("direct_question", False)),
            metadata=dict(payload.get("metadata") or {}),
        )
        text = str(payload.get("text") or "").strip()
        if not text:
            raise ValueError("Meeting chat text is required.")

        if role not in {"participant", "bot", "system"}:
            raise ValueError(f"Unsupported chat role: {role}")

        speaker = str(payload.get("speaker") or role).strip() or role
        source = str(payload.get("source") or "meeting_chat").strip() or "meeting_chat"
        publish_requested = bool(payload.get("publish_requested", False))

        if session.chat_history:
            previous = session.chat_history[-1]
            if previous.speaker == speaker and previous.text == text and previous.source == source:
                return session

        session.chat_history.append(
            ChatTurn(
                turn_id=uuid4().hex[:12],
                role=role,  # type: ignore[arg-type]
                speaker=speaker,
                text=text,
                source=source,
                created_at=str(payload.get("created_at") or utcnow_iso()),
                publish_requested=publish_requested,
            )
        )
        self._remember_chat_input(session, payload)
        session.summary_packet = self._summary_pipeline.build(session)
        self._reactivate_session(
            session,
            reason="New meeting chat arrived.",
            signal="meeting_chat",
        )
        return self.persist_session(session)

    async def handle_chat_message(self, session_id: str, payload: dict[str, Any]) -> tuple[DelegateSession, dict[str, Any]]:
        session = await self.append_chat_turn(session_id, payload)
        latest_turn = session.chat_history[-1]
        try:
            reply = await self._maybe_generate_reply(session, latest_turn, payload)
        except Exception as exc:
            reply = self._record_reply_error(session, latest_turn, exc)
        session.summary_packet = self._summary_pipeline.build(session)
        self._reactivate_session(
            session,
            reason="The meeting session resumed with fresh chat activity.",
            signal="chat_turn",
        )
        session = self.persist_session(session)
        return session, reply

    async def ingest_inputs(self, session_id: str, payload: dict[str, Any]) -> tuple[DelegateSession, dict[str, Any]]:
        session = self._require_session(session_id)
        if session.status == "completed":
            raise ValueError("This meeting session is already completed, so new meeting inputs are not accepted.")
        raw_inputs = payload.get("inputs")
        if raw_inputs is None:
            raw_inputs = [payload]
        if not isinstance(raw_inputs, list) or not raw_inputs:
            raise ValueError("Meeting inputs must be a non-empty list or a single input payload.")

        processed: list[dict[str, Any]] = []
        activated = False
        saw_meeting_closed = False
        for item in raw_inputs:
            if not isinstance(item, dict):
                raise ValueError("Each meeting input must be an object.")
            normalized = self._normalize_input_item(item)
            input_type = str(normalized["input_type"])
            if input_type == "spoken_transcript":
                self._record_input(
                    session,
                    input_type="spoken_transcript",
                    speaker=str(normalized["speaker"] or "unknown"),
                    text=str(normalized["text"] or ""),
                    source=str(normalized["source"]),
                    direct_question=bool(normalized["direct_question"]),
                    metadata=dict(normalized["metadata"]),
                )
                self._append_transcript_chunk(session, normalized)
                pseudo_turn = ChatTurn(
                    turn_id=uuid4().hex[:12],
                    role="participant",
                    speaker=str(normalized["speaker"] or "unknown"),
                    text=str(normalized["text"] or ""),
                    source=str(normalized["source"]),
                    status="captured",
                )
                self.persist_session(session)
                try:
                    reply = await self._maybe_generate_reply(session, pseudo_turn, normalized)
                except Exception as exc:
                    reply = self._record_reply_error(session, pseudo_turn, exc)
                processed.append({"input_type": input_type, "status": "captured", "reply": reply})
                activated = True
                continue

            if input_type == "meeting_chat":
                if self._should_ignore_chat_input(session, normalized):
                    processed.append(
                        {
                            "input_type": input_type,
                            "status": "ignored",
                            "reason": "stale_zoom_chat_replay",
                        }
                    )
                    continue
                self._record_input(
                    session,
                    input_type="meeting_chat",
                    speaker=str(normalized["speaker"] or "participant"),
                    text=str(normalized["text"] or ""),
                    source=str(normalized["source"]),
                    direct_question=bool(normalized["direct_question"]),
                    metadata=dict(normalized["metadata"]),
                )
                turn = self._append_chat_turn_to_session(session, normalized)
                self._remember_chat_input(session, normalized)
                self.persist_session(session)
                try:
                    reply = await self._maybe_generate_reply(session, turn, normalized)
                except Exception as exc:
                    reply = self._record_reply_error(session, turn, exc)
                processed.append({"input_type": input_type, "status": "captured", "reply": reply})
                activated = True
                continue

            if input_type == "bot_reply":
                self._record_input(
                    session,
                    input_type="bot_reply",
                    speaker=str(normalized["speaker"] or session.bot_display_name),
                    text=str(normalized["text"] or ""),
                    source=str(normalized["source"]),
                    metadata=dict(normalized["metadata"]),
                )
                self._append_chat_turn_to_session(
                    session,
                    {
                        **normalized,
                        "role": "bot",
                        "speaker": normalized["speaker"] or session.bot_display_name,
                        "status": normalized["metadata"].get("status", "sent"),
                    },
                )
                processed.append({"input_type": input_type, "status": "captured"})
                activated = True
                continue

            self._record_input(
                session,
                input_type=input_type,
                speaker=str(normalized["speaker"] or "").strip() or None,
                text=str(normalized["text"] or "").strip() or None,
                source=str(normalized["source"]),
                direct_question=bool(normalized["direct_question"]),
                metadata=dict(normalized["metadata"]),
            )
            self._append_workspace_input_event(session, normalized)
            if input_type == "meeting_state":
                state_value = str(
                    normalized["metadata"].get("state")
                    or normalized["metadata"].get("status")
                    or ""
                ).strip().lower()
                if state_value == "closed":
                    saw_meeting_closed = True
            processed.append({"input_type": input_type, "status": "captured"})

        session.summary_packet = self._summary_pipeline.build(session)
        if activated:
            self._reactivate_session(
                session,
                reason="Fresh meeting input arrived.",
                signal="meeting_input",
            )
        session = self.persist_session(session)
        if saw_meeting_closed:
            session, _completion = await self.request_session_completion(
                session_id,
                requested_by="meeting_state_closed",
            )
        return session, {
            "processed_count": len(processed),
            "processed": processed,
            "input_timeline_count": len(session.input_timeline),
        }

    async def observe_window(self, session_id: str, payload: dict[str, Any]) -> tuple[DelegateSession, dict[str, Any]]:
        session = self._require_session(session_id)
        window_title = str(payload.get("window_title") or "").strip()
        if not window_title:
            raise ValueError("window_title is required for local window observation.")

        crop = payload.get("crop")
        if crop is not None and not isinstance(crop, dict):
            raise ValueError("crop must be an object when provided.")

        observation = self._observer.capture_window_text(
            window_title=window_title,
            crop=crop,
            save_image=bool(payload.get("save_image", True)),
        )
        captured_text = str(observation.get("text") or "").strip()
        if not captured_text:
            raise LocalObserverError("No text was captured from the observed window.")

        input_type = str(payload.get("input_type") or "meeting_chat").strip().lower() or "meeting_chat"
        speaker = str(payload.get("speaker") or "").strip() or None
        source = str(payload.get("source") or "local_window_ocr").strip() or "local_window_ocr"
        metadata = dict(payload.get("metadata") or {})
        metadata.update(
            {
                "window_title": observation.get("window_title"),
                "bounds": observation.get("bounds"),
                "image_path": observation.get("image_path"),
                "capture_mode": "local_window_ocr",
            }
        )

        session, ingest_result = await self.ingest_inputs(
            session_id,
            {
                "inputs": [
                    {
                        "input_type": input_type,
                        "speaker": speaker,
                        "text": captured_text,
                        "source": source,
                        "direct_question": bool(payload.get("direct_question", False)),
                        "metadata": metadata,
                    }
                ]
            },
        )
        return session, {
            "capture_mode": "local_window_ocr",
            "captured_text": captured_text,
            "window_title": observation.get("window_title"),
            "bounds": observation.get("bounds"),
            "image_path": observation.get("image_path"),
            "ingest_result": ingest_result,
        }

    async def observe_system_audio(self, session_id: str, payload: dict[str, Any]) -> tuple[DelegateSession, dict[str, Any]]:
        session = self._require_session(session_id)
        if session.status == "completed":
            raise LocalObserverError("This meeting session is already completed, so new audio should not be captured.")
        seconds = float(payload.get("seconds") or 8.0)
        if seconds <= 0:
            raise ValueError("seconds must be greater than zero for local audio observation.")

        sample_rate = payload.get("sample_rate")
        if sample_rate is not None:
            sample_rate = int(sample_rate)
        requested_mode = str(payload.get("audio_mode") or payload.get("audio_source") or "mixed").strip().lower() or "mixed"
        if requested_mode not in {"conversation", "mixed", "microphone", "system"}:
            raise ValueError("audio_mode must be one of: conversation, mixed, microphone, system")

        if requested_mode == "conversation":
            payload = self._prepare_conversation_capture_payload(payload)
            return await self._observe_conversation_audio(
                session,
                seconds=seconds,
                sample_rate=sample_rate,
                payload=payload,
            )

        source_order = ["microphone", "system"] if requested_mode == "mixed" else [requested_mode]

        source_label_map = {
            "microphone": "local_microphone",
            "system": "local_system_audio",
        }

        observations: list[dict[str, Any]] = []
        captured_chunks: list[TranscriptChunk] = []
        archive_state_changed = False
        capture_tasks = []
        for source_name in source_order:
            capture_kwargs: dict[str, Any] = {
                "seconds": seconds,
                "sample_rate": sample_rate,
                "source": source_name,
            }
            device_name = str(
                payload.get(f"{source_name}_device_name")
                or payload.get("device_name")
                or ""
            ).strip()
            if device_name:
                capture_kwargs["device_name"] = device_name
            capture_tasks.append(asyncio.to_thread(self._observer.capture_audio, **capture_kwargs))
        capture_results = await asyncio.gather(*capture_tasks, return_exceptions=True)

        for source_name, capture_result in zip(source_order, capture_results, strict=False):
            if isinstance(capture_result, Exception):
                if requested_mode != "mixed":
                    raise capture_result
                observations.append(
                    {
                        "audio_source": source_name,
                        "capture_mode": source_label_map.get(source_name, "local_audio"),
                        "error": str(capture_result),
                    }
                )
                continue
            observation = dict(capture_result)
            archived_path = self._archive_audio_observation(session, observation)
            observation["archived_path"] = archived_path
            if archived_path:
                archive_state_changed = True
            observation.pop("audio_bytes", None)
            observations.append(observation)
            if observation.get("below_rms_threshold"):
                continue

            chunks = await self._transcribe_audio_observation(observation)
            if not chunks:
                continue

            metadata = dict(payload.get("metadata") or {})
            metadata.update(
                {
                    "artifact_path": observation.get("artifact_path"),
                    "archived_path": archived_path,
                    "sample_rate": observation.get("sample_rate"),
                    "seconds": observation.get("seconds"),
                    "capture_mode": observation.get("capture_mode") or source_label_map.get(source_name, "local_audio"),
                    "audio_source": observation.get("audio_source") or source_name,
                    "device_name": observation.get("device_name"),
                    "audio_rms": observation.get("audio_rms"),
                    "audio_peak": observation.get("audio_peak"),
                    "captured_at": observation.get("archived_at") or utcnow_iso(),
                    "capture_sequence": observation.get("capture_sequence"),
                    "channel_layout": observation.get("channel_layout"),
                    "session_start_offset_seconds": observation.get("session_start_offset_seconds"),
                    "session_end_offset_seconds": observation.get("session_end_offset_seconds"),
                }
            )
            default_source = source_label_map.get(source_name, "local_audio")
            configured_source = str(payload.get("source") or "").strip()
            effective_source = default_source if requested_mode == "mixed" else (configured_source or default_source)
            fallback_speaker = self._default_audio_speaker(
                source_name,
                requested_speaker=str(payload.get("speaker") or "").strip(),
            )
            merged_chunks = self._merge_audio_transcript_chunks(
                chunks,
                fallback_speaker=fallback_speaker,
                output_source=effective_source,
                base_metadata=metadata,
            )
            for chunk in merged_chunks:
                chunk.direct_question = chunk.direct_question or self._contains_bot_mention(session.bot_display_name, chunk.text)
            captured_chunks.extend(merged_chunks)

        captured_chunks = self._dedupe_audio_chunks(captured_chunks)
        if archive_state_changed:
            # Persist archive metadata before ingest_inputs() reloads the session from storage.
            session = self.persist_session(session)
        all_inputs = [
            {
                "input_type": "spoken_transcript",
                "speaker": chunk.speaker,
                "text": chunk.text,
                "source": chunk.source,
                "direct_question": chunk.direct_question,
                "created_at": chunk.created_at,
                "metadata": dict(chunk.metadata or {}),
            }
            for chunk in captured_chunks
        ]

        if not all_inputs:
            return session, {
                "capture_mode": requested_mode,
                "transcript_lines": 0,
                "artifact_paths": [item.get("artifact_path") for item in observations if item.get("artifact_path")],
                "sample_rate": sample_rate or self._observer._audio_sample_rate,
                "seconds": seconds,
                "ingest_result": {"processed_count": 0, "processed": [], "input_timeline_count": len(session.input_timeline)},
                "skipped": True,
                "reason": "No usable local audio transcript was captured.",
                "observations": [
                    {
                        "audio_source": item.get("audio_source"),
                        "capture_mode": item.get("capture_mode"),
                        "audio_rms": item.get("audio_rms"),
                        "audio_peak": item.get("audio_peak"),
                        "below_rms_threshold": item.get("below_rms_threshold"),
                        "archived_path": item.get("archived_path"),
                        "error": item.get("error"),
                    }
                    for item in observations
                ],
            }

        session, ingest_result = await self.ingest_inputs(session_id, {"inputs": all_inputs})
        return session, {
            "capture_mode": requested_mode,
            "transcript_lines": len(all_inputs),
            "artifact_paths": [item.get("artifact_path") for item in observations if item.get("artifact_path")],
            "sample_rate": sample_rate or self._observer._audio_sample_rate,
            "seconds": seconds,
            "ingest_result": ingest_result,
            "observations": [
                {
                    "audio_source": item.get("audio_source"),
                    "capture_mode": item.get("capture_mode"),
                    "audio_rms": item.get("audio_rms"),
                    "audio_peak": item.get("audio_peak"),
                    "below_rms_threshold": item.get("below_rms_threshold"),
                    "archived_path": item.get("archived_path"),
                    "error": item.get("error"),
                }
                for item in observations
            ],
        }

    def _prepare_conversation_capture_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        prepared = dict(payload)
        system_device_name = str(
            prepared.get("system_device_name")
            or prepared.get("meeting_output_device_name")
            or ""
        ).strip()
        if not system_device_name:
            system_device_name = self._dedicated_meeting_output_device_name
        if not system_device_name:
            raise LocalObserverError("Conversation capture requires a dedicated meeting output device.")
        if not self._observer.speaker_device_available(system_device_name):
            raise LocalObserverError(
                f"Configured meeting output device was not found: {system_device_name}"
            )
        prepared["system_device_name"] = system_device_name
        prepared["meeting_output_device_name"] = system_device_name
        return prepared

    async def start_audio_observer(self, session_id: str, payload: dict[str, Any]) -> tuple[DelegateSession, dict[str, Any]]:
        session = self._require_session(session_id)
        if session.status == "completed":
            raise LocalObserverError("This meeting session is already completed, so continuous audio observation cannot start.")
        current = self._audio_observer_tasks.get(session_id)
        if current and not current.done():
            return session, self.audio_observer_status(session_id)

        seconds = max(float(payload.get("seconds") or 12.0), 1.0)
        interval_ms = max(int(payload.get("interval_ms") or 250), 0)
        audio_mode = str(payload.get("audio_mode") or self._auto_observe_audio_mode).strip().lower() or self._auto_observe_audio_mode
        if audio_mode not in {"conversation", "mixed", "microphone", "system"}:
            raise ValueError("audio_mode must be one of: conversation, mixed, microphone, system")
        prepared_payload = dict(payload)
        if audio_mode == "conversation":
            prepared_payload = self._prepare_conversation_capture_payload(prepared_payload)
        metadata = dict(payload.get("metadata") or {})
        metadata.setdefault("capture_profile", "continuous_audio_observer")
        control = {
            "stop_requested": False,
            "seconds": seconds,
            "interval_ms": interval_ms,
            "audio_mode": audio_mode,
            "continuous_mode": bool(audio_mode == "conversation" and self._continuous_conversation_live),
            "frame_ms": self._continuous_audio_frame_ms if audio_mode == "conversation" else None,
            "silence_ms": self._continuous_audio_silence_ms if audio_mode == "conversation" else None,
            "metadata": metadata,
            "payload": {
                key: value
                for key, value in prepared_payload.items()
                if key not in {"seconds", "interval_ms", "audio_mode", "metadata"}
            },
            "started_at": utcnow_iso(),
            "cycles": 0,
            "last_error": None,
            "last_result": {},
        }
        self._audio_observer_controls[session_id] = control
        session.ai_state["audio_observer"] = {
            "running": True,
            "seconds": seconds,
            "interval_ms": interval_ms,
            "audio_mode": audio_mode,
            "continuous_mode": bool(control["continuous_mode"]),
            "frame_ms": control.get("frame_ms"),
            "silence_ms": control.get("silence_ms"),
            "started_at": control["started_at"],
        }
        self._reactivate_session(
            session,
            reason="Continuous meeting audio observation started.",
            signal="audio_observer_started",
        )
        self.persist_session(session)

        async def runner() -> None:
            if bool(control.get("continuous_mode")):
                try:
                    await self._run_continuous_conversation_observer(session_id, control)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    control["last_error"] = str(exc)
                    latest_session = self.get_session(session_id)
                    if latest_session is not None:
                        latest_session.ai_state["audio_observer"] = {
                            **dict(latest_session.ai_state.get("audio_observer") or {}),
                            "running": True,
                            "last_error": str(exc),
                            "last_error_at": utcnow_iso(),
                        }
                        self.persist_session(latest_session)
                return
            while not control["stop_requested"]:
                try:
                    observed_session, result = await self.observe_system_audio(
                        session_id,
                        {
                            "seconds": seconds,
                            "audio_mode": audio_mode,
                            "metadata": metadata,
                            **dict(control.get("payload") or {}),
                        },
                    )
                    control["cycles"] = int(control.get("cycles") or 0) + 1
                    control["last_result"] = {
                        "transcript_lines": result.get("transcript_lines", 0),
                        "capture_mode": result.get("capture_mode"),
                        "captured_at": utcnow_iso(),
                    }
                    control["last_error"] = None
                    observed_session.ai_state["audio_observer"] = {
                        **dict(observed_session.ai_state.get("audio_observer") or {}),
                        "running": True,
                        "cycles": control["cycles"],
                        "last_result": dict(control["last_result"]),
                    }
                    self.persist_session(observed_session)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    control["last_error"] = str(exc)
                    latest_session = self.get_session(session_id)
                    if latest_session is not None:
                        latest_session.ai_state["audio_observer"] = {
                            **dict(latest_session.ai_state.get("audio_observer") or {}),
                            "running": True,
                            "last_error": str(exc),
                            "last_error_at": utcnow_iso(),
                        }
                        self.persist_session(latest_session)
                if control["stop_requested"]:
                    break
                await asyncio.sleep(interval_ms / 1000.0)

        task = asyncio.create_task(runner())
        self._audio_observer_tasks[session_id] = task
        task.add_done_callback(lambda _task, sid=session_id: self._finalize_audio_observer_task(sid))
        return session, self.audio_observer_status(session_id)

    async def stop_audio_observer(self, session_id: str) -> tuple[DelegateSession, dict[str, Any]]:
        session = self._require_session(session_id)
        control = self._audio_observer_controls.get(session_id)
        if control is not None:
            control["stop_requested"] = True
        task = self._audio_observer_tasks.get(session_id)
        if task is not None and not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=8.0)
            except asyncio.TimeoutError:
                task.cancel()
                try:
                    await asyncio.wait_for(task, timeout=1.0)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    pass
            except asyncio.CancelledError:
                pass
        self._finalize_audio_observer_task(session_id)
        refreshed = self.get_session(session_id) or session
        return refreshed, self.audio_observer_status(session_id)

    def audio_observer_status(self, session_id: str) -> dict[str, Any]:
        task = self._audio_observer_tasks.get(session_id)
        control = dict(self._audio_observer_controls.get(session_id) or {})
        return {
            "running": bool(task and not task.done()),
            "seconds": control.get("seconds"),
            "interval_ms": control.get("interval_ms"),
            "audio_mode": control.get("audio_mode"),
            "continuous_mode": bool(control.get("continuous_mode")),
            "frame_ms": control.get("frame_ms"),
            "silence_ms": control.get("silence_ms"),
            "started_at": control.get("started_at"),
            "cycles": control.get("cycles", 0),
            "last_error": control.get("last_error"),
            "last_result": control.get("last_result") or {},
        }

    def _finalize_audio_observer_task(self, session_id: str) -> None:
        task = self._audio_observer_tasks.get(session_id)
        if task is not None and not task.done():
            return
        self._audio_observer_tasks.pop(session_id, None)
        control = self._audio_observer_controls.get(session_id)
        latest_session = self.get_session(session_id)
        if latest_session is not None:
            latest_session.ai_state["audio_observer"] = {
                **dict(latest_session.ai_state.get("audio_observer") or {}),
                "running": False,
                "stopped_at": utcnow_iso(),
                "cycles": int((control or {}).get("cycles") or 0),
                "last_error": (control or {}).get("last_error"),
                "last_result": dict((control or {}).get("last_result") or {}),
            }
            self.persist_session(latest_session)

    async def _run_continuous_conversation_observer(self, session_id: str, control: dict[str, Any]) -> None:
        capture_queue: queue.Queue[dict[str, Any]] = queue.Queue()
        stop_event = threading.Event()
        worker = threading.Thread(
            target=self._continuous_conversation_capture_thread,
            kwargs={
                "control": control,
                "capture_queue": capture_queue,
                "stop_event": stop_event,
            },
            name=f"delegate-conversation-capture-{session_id}",
            daemon=True,
        )
        worker.start()
        try:
            while True:
                if control.get("stop_requested"):
                    stop_event.set()
                try:
                    item = await asyncio.to_thread(capture_queue.get, True, 0.5)
                except queue.Empty:
                    if not worker.is_alive() and capture_queue.empty():
                        break
                    continue

                item_type = str(item.get("type") or "").strip().lower()
                if item_type == "error":
                    raise LocalObserverError(str(item.get("error") or "Continuous conversation capture failed."))
                if item_type != "observation":
                    if stop_event.is_set() and not worker.is_alive():
                        break
                    continue

                session = self._require_session(session_id)
                observed_session, result = await self._process_captured_conversation_observations(
                    session,
                    payload={
                        "metadata": dict(control.get("metadata") or {}),
                        **dict(control.get("payload") or {}),
                    },
                    microphone_observation=dict(item.get("microphone_observation") or {}),
                    system_observation=dict(item.get("system_observation") or {}),
                    allow_mixed_fallback=False,
                )
                control["cycles"] = int(control.get("cycles") or 0) + 1
                control["last_result"] = {
                    "transcript_lines": result.get("transcript_lines", 0),
                    "capture_mode": result.get("capture_mode"),
                    "captured_at": utcnow_iso(),
                }
                control["last_error"] = None
                observed_session.ai_state["audio_observer"] = {
                    **dict(observed_session.ai_state.get("audio_observer") or {}),
                    "running": True,
                    "continuous_mode": True,
                    "frame_ms": control.get("frame_ms"),
                    "silence_ms": control.get("silence_ms"),
                    "cycles": control["cycles"],
                    "last_result": dict(control["last_result"]),
                }
                self.persist_session(observed_session)

                if not worker.is_alive() and capture_queue.empty():
                    break
        finally:
            stop_event.set()
            await asyncio.to_thread(worker.join, 2.0)

    def _continuous_conversation_capture_thread(
        self,
        *,
        control: dict[str, Any],
        capture_queue: queue.Queue[dict[str, Any]],
        stop_event: threading.Event,
    ) -> None:
        try:
            soundcard = self._observer._import_module("soundcard")
            payload = dict(control.get("payload") or {})
            metadata = dict(control.get("metadata") or {})
            rate = int(payload.get("sample_rate") or self._observer._audio_sample_rate)
            max_segment_seconds = max(float(control.get("seconds") or 12.0), 1.0)
            frame_ms = max(int(control.get("frame_ms") or self._continuous_audio_frame_ms), 50)
            preroll_ms = max(self._continuous_audio_preroll_ms, 0)
            silence_ms = max(int(control.get("silence_ms") or self._continuous_audio_silence_ms), 100)
            min_segment_ms = max(self._continuous_audio_min_segment_ms, 100)
            frame_count = max(int(rate * (frame_ms / 1000.0)), 1)
            preroll_frames = max(int(rate * (preroll_ms / 1000.0)), 0)
            silence_frames_limit = max(int(rate * (silence_ms / 1000.0)), frame_count)
            min_segment_frames = max(int(rate * (min_segment_ms / 1000.0)), frame_count)
            max_segment_frames = max(int(rate * max_segment_seconds), frame_count)
            threshold = float(self._observer._audio_rms_threshold)
            recorder_blocksize = max(
                int(rate * (float(getattr(self._observer, "_audio_blocksize_ms", 1200)) / 1000.0)),
                frame_count * 2,
            )

            microphone_device_name = str(
                payload.get("microphone_device_name") or payload.get("device_name") or ""
            ).strip()
            system_device_name = str(
                payload.get("system_device_name") or payload.get("device_name") or ""
            ).strip()
            strict = self._observer._strict_audio_device_selection
            microphone_device = self._observer._select_microphone(soundcard, microphone_device_name, strict=strict)
            speaker = self._observer._select_speaker(soundcard, system_device_name, strict=strict)
            system_device = soundcard.get_microphone(str(speaker.name), include_loopback=True)

            pre_microphone: deque[Any] = deque()
            pre_system: deque[Any] = deque()
            prebuffer_frames = 0
            active_microphone: list[Any] = []
            active_system: list[Any] = []
            active_frames = 0
            trailing_silence_frames = 0
            segment_counter = 0
            microphone_chunk_queue: queue.Queue[Any] = queue.Queue(maxsize=self._continuous_audio_queue_size)
            system_chunk_queue: queue.Queue[Any] = queue.Queue(maxsize=self._continuous_audio_queue_size)

            def normalize_samples(raw: Any) -> Any:
                audio = raw
                if getattr(audio, "ndim", 1) > 1:
                    audio = audio[:, 0]
                return audio.astype("float32", copy=False)

            def publish_chunk(target_queue: queue.Queue[Any], chunk: Any) -> None:
                try:
                    target_queue.put_nowait(chunk)
                    return
                except queue.Full:
                    pass
                try:
                    target_queue.get_nowait()
                except queue.Empty:
                    pass
                try:
                    target_queue.put_nowait(chunk)
                except queue.Full:
                    return

            def append_prebuffer(mic_chunk: Any, sys_chunk: Any) -> None:
                nonlocal prebuffer_frames
                pre_microphone.append(mic_chunk)
                pre_system.append(sys_chunk)
                prebuffer_frames += len(mic_chunk)
                while prebuffer_frames > preroll_frames and pre_microphone:
                    prebuffer_frames -= len(pre_microphone.popleft())
                    pre_system.popleft()

            def reset_active() -> None:
                nonlocal active_microphone, active_system, active_frames, trailing_silence_frames
                active_microphone = []
                active_system = []
                active_frames = 0
                trailing_silence_frames = 0

            def flush_segment() -> None:
                nonlocal segment_counter
                if active_frames < min_segment_frames or not active_microphone or not active_system:
                    reset_active()
                    return
                microphone_audio = self._concatenate_audio_chunks(active_microphone)
                system_audio = self._concatenate_audio_chunks(active_system)
                if microphone_audio is None or system_audio is None:
                    reset_active()
                    return
                segment_counter += 1
                capture_queue.put(
                    {
                        "type": "observation",
                        "microphone_observation": self._build_audio_observation_from_samples(
                            microphone_audio,
                            sample_rate=rate,
                            source="microphone",
                            capture_mode="local_microphone",
                            filename=f"microphone-live-{segment_counter}.wav",
                            device_name=str(getattr(microphone_device, "name", "") or microphone_device_name or ""),
                        ),
                        "system_observation": self._build_audio_observation_from_samples(
                            system_audio,
                            sample_rate=rate,
                            source="system",
                            capture_mode="local_system_audio",
                            filename=f"system-live-{segment_counter}.wav",
                            device_name=str(getattr(speaker, "name", "") or system_device_name or ""),
                        ),
                    }
                )
                reset_active()

            def recorder_loop(*, recorder_device: Any, target_queue: queue.Queue[Any], label: str) -> None:
                try:
                    with recorder_device.recorder(
                        samplerate=rate,
                        channels=1,
                        blocksize=recorder_blocksize,
                    ) as recorder:
                        while not stop_event.is_set():
                            publish_chunk(target_queue, normalize_samples(recorder.record(numframes=frame_count)))
                except Exception as exc:
                    capture_queue.put(
                        {
                            "type": "error",
                            "error": f"{label}: {str(exc).strip() or exc.__class__.__name__}",
                        }
                    )
                    stop_event.set()

            microphone_reader = threading.Thread(
                target=recorder_loop,
                kwargs={
                    "recorder_device": microphone_device,
                    "target_queue": microphone_chunk_queue,
                    "label": "microphone recorder",
                },
                daemon=True,
                name="delegate-microphone-reader",
            )
            system_reader = threading.Thread(
                target=recorder_loop,
                kwargs={
                    "recorder_device": system_device,
                    "target_queue": system_chunk_queue,
                    "label": "meeting-output recorder",
                },
                daemon=True,
                name="delegate-system-reader",
            )
            microphone_reader.start()
            system_reader.start()
            try:
                while not stop_event.is_set():
                    try:
                        microphone_chunk = microphone_chunk_queue.get(timeout=0.5)
                        system_chunk = system_chunk_queue.get(timeout=0.5)
                    except queue.Empty:
                        microphone_stalled = not microphone_reader.is_alive() and microphone_chunk_queue.empty()
                        system_stalled = not system_reader.is_alive() and system_chunk_queue.empty()
                        if microphone_stalled or system_stalled:
                            break
                        continue

                    microphone_rms = float(
                        (microphone_chunk.astype("float32") ** 2).mean() ** 0.5
                    ) if len(microphone_chunk) else 0.0
                    system_rms = float(
                        (system_chunk.astype("float32") ** 2).mean() ** 0.5
                    ) if len(system_chunk) else 0.0
                    is_active = max(microphone_rms, system_rms) >= threshold

                    if active_microphone:
                        active_microphone.append(microphone_chunk)
                        active_system.append(system_chunk)
                        active_frames += len(microphone_chunk)
                        if is_active:
                            trailing_silence_frames = 0
                        else:
                            trailing_silence_frames += len(microphone_chunk)
                        if trailing_silence_frames >= silence_frames_limit or active_frames >= max_segment_frames:
                            flush_segment()
                        if not active_microphone:
                            append_prebuffer(microphone_chunk, system_chunk)
                        continue

                    if is_active:
                        active_microphone = list(pre_microphone) + [microphone_chunk]
                        active_system = list(pre_system) + [system_chunk]
                        active_frames = sum(len(chunk) for chunk in active_microphone)
                        trailing_silence_frames = 0
                        pre_microphone.clear()
                        pre_system.clear()
                        prebuffer_frames = 0
                        if active_frames >= max_segment_frames:
                            flush_segment()
                        continue

                    append_prebuffer(microphone_chunk, system_chunk)
            finally:
                stop_event.set()
                microphone_reader.join(timeout=1.0)
                system_reader.join(timeout=1.0)
                flush_segment()
        except Exception as exc:
            capture_queue.put({"type": "error", "error": str(exc)})

    def _build_audio_observation_from_samples(
        self,
        samples: Any,
        *,
        sample_rate: int,
        source: str,
        capture_mode: str,
        filename: str,
        device_name: str | None,
    ) -> dict[str, Any]:
        if importlib.util.find_spec("soundfile") is None:
            raise RuntimeError("soundfile is required to package captured audio samples.")
        import io
        import numpy as np
        import soundfile as sf

        normalized = np.asarray(samples, dtype="float32")
        if normalized.ndim > 1:
            channel_count = int(normalized.shape[1])
        else:
            channel_count = 1
        duration = float(len(normalized) / float(sample_rate)) if len(normalized) else 0.0
        audio_rms = float(np.sqrt(np.mean(np.square(normalized.astype("float32"))))) if len(normalized) else 0.0
        audio_peak = float(np.max(np.abs(normalized.astype("float32")))) if len(normalized) else 0.0
        buffer = io.BytesIO()
        sf.write(buffer, normalized, int(sample_rate), format="WAV")
        wav_bytes = buffer.getvalue()
        artifact_path = Path(self._observer._artifact_dir) / filename
        artifact_path.write_bytes(wav_bytes)
        return {
            "audio_bytes": wav_bytes,
            "filename": filename,
            "sample_rate": int(sample_rate),
            "seconds": duration,
            "artifact_path": str(artifact_path),
            "capture_mode": capture_mode,
            "audio_source": source,
            "audio_channels": channel_count,
            "channel_layout": "stereo" if channel_count >= 2 else "mono",
            "device_name": str(device_name or ""),
            "audio_rms": audio_rms,
            "audio_peak": audio_peak,
            "below_rms_threshold": audio_rms < self._observer._audio_rms_threshold,
        }

    def _concatenate_audio_chunks(self, chunks: list[Any]) -> Any | None:
        if not chunks:
            return None
        import numpy as np

        arrays = [np.asarray(chunk, dtype="float32") for chunk in chunks if len(chunk)]
        if not arrays:
            return None
        return np.concatenate(arrays, axis=0).astype("float32", copy=False)

    async def _process_captured_conversation_observations(
        self,
        session: DelegateSession,
        *,
        payload: dict[str, Any],
        microphone_observation: dict[str, Any],
        system_observation: dict[str, Any],
        allow_mixed_fallback: bool,
    ) -> tuple[DelegateSession, dict[str, Any]]:
        microphone_archived_path = self._archive_audio_observation(session, microphone_observation)
        microphone_observation["archived_path"] = microphone_archived_path
        system_archived_path = self._archive_audio_observation(session, system_observation)
        system_observation["archived_path"] = system_archived_path
        microphone_observation.pop("audio_bytes", None)
        system_observation.pop("audio_bytes", None)
        duplex_observation = self._build_duplex_audio_observation(
            microphone_observation,
            system_observation,
        )
        if duplex_observation is None:
            if allow_mixed_fallback:
                return await self.observe_system_audio(
                    session.session_id,
                    {
                        **payload,
                        "audio_mode": "mixed",
                        "seconds": max(
                            float(microphone_observation.get("seconds") or 0.0),
                            float(system_observation.get("seconds") or 0.0),
                            1.0,
                        ),
                        "sample_rate": microphone_observation.get("sample_rate") or system_observation.get("sample_rate"),
                    },
                )
            raise LocalObserverError("Conversation audio capture could not be combined into a duplex observation.")

        archived_path = self._archive_audio_observation(session, duplex_observation)
        duplex_observation["archived_path"] = archived_path
        if microphone_archived_path or system_archived_path or archived_path:
            # Persist archive metadata before any early return or ingest_inputs() store reload.
            session = self.persist_session(session)
        if duplex_observation.get("below_rms_threshold"):
            return session, {
                "capture_mode": "conversation",
                "transcript_lines": 0,
                "artifact_paths": [duplex_observation.get("artifact_path")],
                "sample_rate": duplex_observation.get("sample_rate"),
                "seconds": duplex_observation.get("seconds"),
                "ingest_result": {"processed_count": 0, "processed": [], "input_timeline_count": len(session.input_timeline)},
                "skipped": True,
                "reason": "No usable local conversation audio transcript was captured.",
                "observations": [
                    {
                        "audio_source": duplex_observation.get("audio_source"),
                        "capture_mode": duplex_observation.get("capture_mode"),
                        "audio_rms": duplex_observation.get("audio_rms"),
                        "audio_peak": duplex_observation.get("audio_peak"),
                        "below_rms_threshold": duplex_observation.get("below_rms_threshold"),
                        "archived_path": duplex_observation.get("archived_path"),
                        "error": None,
                    }
                ],
            }

        chunks = await self._transcribe_live_conversation_observation(
            duplex_observation=duplex_observation,
            microphone_observation=microphone_observation,
            system_observation=system_observation,
        )
        metadata = dict(payload.get("metadata") or {})
        metadata.update(
            {
                "artifact_path": duplex_observation.get("artifact_path"),
                "archived_path": archived_path,
                "sample_rate": duplex_observation.get("sample_rate"),
                "seconds": duplex_observation.get("seconds"),
                "capture_mode": duplex_observation.get("capture_mode"),
                "audio_source": duplex_observation.get("audio_source"),
                "audio_rms": duplex_observation.get("audio_rms"),
                "audio_peak": duplex_observation.get("audio_peak"),
                "captured_at": duplex_observation.get("archived_at") or utcnow_iso(),
                "capture_sequence": duplex_observation.get("capture_sequence"),
                "audio_channels": duplex_observation.get("audio_channels"),
                "channel_layout": duplex_observation.get("channel_layout"),
                "channel_sources": duplex_observation.get("channel_sources"),
                "microphone_audio_rms": duplex_observation.get("microphone_audio_rms"),
                "microphone_audio_peak": duplex_observation.get("microphone_audio_peak"),
                "system_audio_rms": duplex_observation.get("system_audio_rms"),
                "system_audio_peak": duplex_observation.get("system_audio_peak"),
                "session_start_offset_seconds": duplex_observation.get("session_start_offset_seconds"),
                "session_end_offset_seconds": duplex_observation.get("session_end_offset_seconds"),
            }
        )
        merged_chunks = self._merge_audio_transcript_chunks(
            chunks,
            fallback_speaker="conversation_audio",
            output_source="local_conversation_audio",
            base_metadata=metadata,
        )
        for chunk in merged_chunks:
            chunk.direct_question = chunk.direct_question or self._contains_bot_mention(session.bot_display_name, chunk.text)
        inputs = [
            {
                "input_type": "spoken_transcript",
                "speaker": chunk.speaker,
                "text": chunk.text,
                "source": chunk.source,
                "direct_question": chunk.direct_question,
                "created_at": chunk.created_at,
                "metadata": dict(chunk.metadata or {}),
            }
            for chunk in merged_chunks
        ]
        if not inputs:
            return session, {
                "capture_mode": "conversation",
                "transcript_lines": 0,
                "artifact_paths": [duplex_observation.get("artifact_path")],
                "sample_rate": duplex_observation.get("sample_rate"),
                "seconds": duplex_observation.get("seconds"),
                "ingest_result": {"processed_count": 0, "processed": [], "input_timeline_count": len(session.input_timeline)},
                "skipped": True,
                "reason": "Conversation audio capture produced no transcript.",
                "observations": [
                    {
                        "audio_source": duplex_observation.get("audio_source"),
                        "capture_mode": duplex_observation.get("capture_mode"),
                        "audio_rms": duplex_observation.get("audio_rms"),
                        "audio_peak": duplex_observation.get("audio_peak"),
                        "below_rms_threshold": duplex_observation.get("below_rms_threshold"),
                        "archived_path": duplex_observation.get("archived_path"),
                        "error": None,
                    }
                ],
            }

        session, ingest_result = await self.ingest_inputs(session.session_id, {"inputs": inputs})
        return session, {
            "capture_mode": "conversation",
            "transcript_lines": len(inputs),
            "artifact_paths": [duplex_observation.get("artifact_path")],
            "sample_rate": duplex_observation.get("sample_rate"),
            "seconds": duplex_observation.get("seconds"),
            "ingest_result": ingest_result,
            "observations": [
                {
                    "audio_source": duplex_observation.get("audio_source"),
                    "capture_mode": duplex_observation.get("capture_mode"),
                    "audio_rms": duplex_observation.get("audio_rms"),
                    "audio_peak": duplex_observation.get("audio_peak"),
                    "below_rms_threshold": duplex_observation.get("below_rms_threshold"),
                    "archived_path": duplex_observation.get("archived_path"),
                    "error": None,
                }
            ],
        }

    async def _transcribe_live_conversation_observation(
        self,
        *,
        duplex_observation: dict[str, Any],
        microphone_observation: dict[str, Any],
        system_observation: dict[str, Any],
    ) -> list[TranscriptChunk]:
        threshold = float(getattr(self._observer, "_audio_rms_threshold", 0.0) or 0.0)
        microphone_active = float(microphone_observation.get("audio_rms") or 0.0) >= threshold
        system_active = float(system_observation.get("audio_rms") or 0.0) >= threshold

        try:
            readiness = dict(self._ai.live_transcription_readiness())
        except Exception:
            readiness = {"blocking_reasons": ["Live high-quality transcription readiness check failed."]}

        if not readiness.get("blocking_reasons"):
            try:
                live_result = await asyncio.to_thread(
                    self._ai.transcribe_live_conversation_audio,
                    microphone_path=microphone_observation.get("archived_path") if microphone_active else None,
                    meeting_output_path=system_observation.get("archived_path") if system_active else None,
                    microphone_start_offset_seconds=float(
                        microphone_observation.get("session_start_offset_seconds") or 0.0
                    ),
                    meeting_output_start_offset_seconds=float(
                        system_observation.get("session_start_offset_seconds") or 0.0
                    ),
                )
                live_chunks = list(live_result.get("chunks") or [])
                if live_chunks:
                    duplex_observation["live_transcription_provider"] = str(
                        live_result.get("provider") or "faster_whisper_cuda"
                    )
                    duplex_observation["live_quality_pass"] = str(
                        live_result.get("quality_pass") or "live_high_quality"
                    )
                    return live_chunks
            except Exception as exc:
                duplex_observation["live_transcription_error"] = str(exc).strip() or exc.__class__.__name__

        return await self._transcribe_audio_observation(duplex_observation)

    async def complete_session(self, session_id: str) -> DelegateSession:
        lock = self._completion_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            try:
                return await self._complete_session_locked(session_id)
            finally:
                self._ai.release_quality_runtime_resources()

    async def _complete_session_locked(self, session_id: str) -> DelegateSession:
        session = self._require_session(session_id)
        if session.status == "completed":
            return session
        await self.stop_audio_observer(session_id)
        session = self._require_session(session_id)
        latest_archive_at = self._latest_audio_archive_at(session)
        final_transcription_state = dict(session.ai_state.get("final_transcription") or {})
        final_audio_at = str(final_transcription_state.get("quality_pass_at") or "").strip()
        last_summary_at = str(session.ai_state.get("last_summary_at") or "").strip()
        summary_is_current = bool(
            session.status == "completed"
            and session.summary
            and session.summary_exports
            and str(final_transcription_state.get("status") or "").strip() == "success"
            and (
                not latest_archive_at
                or (
                    (not final_audio_at or latest_archive_at <= final_audio_at)
                    and (not last_summary_at or latest_archive_at <= last_summary_at)
                )
            )
        )
        if summary_is_current:
            return session

        if latest_archive_at:
            try:
                session = await self._refresh_transcript_from_audio_archive(session)
            except Exception as exc:
                failure_reason = str(exc).strip() or "Final transcription quality pass failed."
                session.ai_state["final_transcription"] = {
                    "status": "failed",
                    "provider": str(self._ai.quality_readiness().get("provider") or "faster_whisper_cuda"),
                    "model": self._ai.quality_readiness().get("model"),
                    "diarization_provider": self._ai.quality_readiness().get("diarization_provider"),
                    "quality_pass_at": utcnow_iso(),
                    "dropped_segment_count": 0,
                    "archive_gap_count": 0,
                    "readiness_snapshot": self.quality_readiness(),
                    "failure_reason": failure_reason,
                }
                if not (session.transcript or session.chat_history or session.input_timeline):
                    session.status = "completed"
                    session.status_reason = failure_reason
                    session.summary = None
                    session.action_items = []
                    session.summary_exports = []
                    session.transcript_exports = []
                    return self.persist_session(session)
                session.status_reason = (
                    "Final offline transcription failed, so the exported summary used the current live meeting capture. "
                    + failure_reason
                )
        else:
            session.ai_state["final_transcription"] = {
                "status": "skipped",
                "provider": None,
                "model": None,
                "diarization_provider": None,
                "quality_pass_at": utcnow_iso(),
                "dropped_segment_count": 0,
                "archive_gap_count": 0,
                "readiness_snapshot": self.quality_readiness(),
                "reason": "No archived audio was captured for this session.",
            }

        session.status = "completed"
        if str(session.ai_state.get("final_transcription", {}).get("status") or "").strip().lower() == "success":
            session.status_reason = None
        session.summary_packet = self._summary_pipeline.build(session)
        ai_result = await self._ai.summarize_session(session)
        session.summary = str(ai_result.get("summary") or "").strip()
        session.action_items = [str(item).strip() for item in ai_result.get("action_items", []) if str(item).strip()]
        self._apply_ai_summary_intelligence(session, ai_result)
        if ai_result.get("response_id"):
            session.ai_state["last_model_response_id"] = str(ai_result["response_id"])
        if ai_result.get("provider"):
            session.ai_state["last_provider"] = str(ai_result["provider"])
        session.ai_state["last_summary_at"] = utcnow_iso()
        self._write_exports(session)
        return self.persist_session(session)

    async def request_session_completion(
        self,
        session_id: str,
        *,
        mode: str | None = None,
        requested_by: str = "api",
    ) -> tuple[DelegateSession, dict[str, Any]]:
        completion_mode = self._resolve_completion_mode(mode)
        await self.stop_audio_observer(session_id)
        session = self._require_session(session_id)
        requested_at = utcnow_iso()

        if completion_mode == "inline":
            session = await self.complete_session(session_id)
            self._update_finalization_state(
                session,
                status="completed",
                mode="inline",
                requested_at=requested_at,
                completed_at=utcnow_iso(),
                requested_by=requested_by,
                last_error=None,
            )
            session = self.persist_session(session)
            return session, {
                "mode": "inline",
                "status": "completed",
                "job_id": None,
            }

        if session.status == "completed":
            self._update_finalization_state(
                session,
                status="completed",
                mode="queued",
                requested_at=requested_at,
                completed_at=utcnow_iso(),
                requested_by=requested_by,
                last_error=None,
            )
            session = self.persist_session(session)
            return session, {
                "mode": "queued",
                "status": "completed",
                "job_id": None,
            }

        job = self._find_pending_finalize_job(session.session_id)
        if job is None:
            job = self._runner_store.enqueue_job(
                RunnerJob(
                    job_id=uuid4().hex[:12],
                    job_type="finalize_session",
                    session_id=session.session_id,
                    payload={
                        "requested_by": requested_by,
                        "requested_at": requested_at,
                    },
                )
            )
        queue_status = "processing" if job.status == "leased" else "queued"
        self._update_finalization_state(
            session,
            status=queue_status,
            mode="queued",
            requested_at=requested_at,
            requested_by=requested_by,
            job_id=job.job_id,
            last_error=None,
        )
        session = self.persist_session(session)
        return session, {
            "mode": "queued",
            "status": queue_status,
            "job_id": job.job_id,
        }

    async def process_finalization_queue(
        self,
        *,
        limit: int = 1,
        runner_id: str = "local-meeting-finisher",
    ) -> dict[str, Any]:
        leased_jobs = self._runner_store.lease_jobs(limit=max(limit, 1), runner_id=runner_id)
        processed: list[dict[str, Any]] = []
        failed: list[dict[str, str]] = []

        for job in leased_jobs:
            if job.job_type != "finalize_session":
                self._runner_store.fail_job(
                    job.job_id,
                    f"Unsupported runner job type for finalization worker: {job.job_type}",
                    requeue=False,
                )
                failed.append(
                    {
                        "job_id": job.job_id,
                        "session_id": str(job.session_id or ""),
                        "error": f"Unsupported runner job type: {job.job_type}",
                    }
                )
                continue
            session_id = str(job.session_id or "").strip()
            if not session_id:
                self._runner_store.fail_job(job.job_id, "Finalization job is missing session_id.", requeue=False)
                failed.append(
                    {
                        "job_id": job.job_id,
                        "session_id": "",
                        "error": "Finalization job is missing session_id.",
                    }
                )
                continue
            session = self.get_session(session_id)
            if session is None:
                self._runner_store.fail_job(job.job_id, f"Unknown delegate session: {session_id}", requeue=False)
                failed.append(
                    {
                        "job_id": job.job_id,
                        "session_id": session_id,
                        "error": f"Unknown delegate session: {session_id}",
                    }
                )
                continue

            self._update_finalization_state(
                session,
                status="processing",
                mode="queued",
                job_id=job.job_id,
                runner_id=runner_id,
                started_at=utcnow_iso(),
                last_error=None,
            )
            self.persist_session(session)

            try:
                completed_session = await self.complete_session(session_id)
                completed_at = utcnow_iso()
                self._update_finalization_state(
                    completed_session,
                    status="completed",
                    mode="queued",
                    job_id=job.job_id,
                    runner_id=runner_id,
                    completed_at=completed_at,
                    last_error=None,
                )
                completed_session = self.persist_session(completed_session)
                result = {
                    "job_id": job.job_id,
                    "session_id": completed_session.session_id,
                    "status": completed_session.status,
                    "summary_ready": bool(completed_session.summary),
                    "summary_exports": len(completed_session.summary_exports),
                    "completed_at": completed_at,
                }
                self._runner_store.complete_job(job.job_id, result)
                processed.append(result)
            except Exception as exc:
                error_message = str(exc).strip() or exc.__class__.__name__
                latest_session = self.get_session(session_id)
                if latest_session is not None:
                    self._update_finalization_state(
                        latest_session,
                        status="failed",
                        mode="queued",
                        job_id=job.job_id,
                        runner_id=runner_id,
                        completed_at=utcnow_iso(),
                        last_error=error_message,
                    )
                    self.persist_session(latest_session)
                self._runner_store.fail_job(job.job_id, error_message, requeue=False)
                failed.append(
                    {
                        "job_id": job.job_id,
                        "session_id": session_id,
                        "error": error_message,
                    }
                )

        return {
            "runner_id": runner_id,
            "leased_count": len(leased_jobs),
            "processed_count": len(processed),
            "failed_count": len(failed),
            "processed": processed,
            "failed": failed,
        }

    async def run_auto_completion_watchdog(self) -> None:
        while True:
            try:
                await self.scan_auto_completion_candidates()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            await asyncio.sleep(self._session_watchdog_poll_seconds)

    async def scan_auto_completion_candidates(self) -> None:
        for session in self._store.list_sessions():
            if session.status == "completed":
                continue
            decision = self._auto_completion_watchdog_decision(session)
            action = str(decision.get("action") or "")
            if action == "idle":
                continue
            if action == "reactivate":
                refreshed = self.get_session(session.session_id)
                if refreshed is None or refreshed.status == "completed":
                    continue
                self._reactivate_session(
                    refreshed,
                    reason=str(decision.get("reason") or "Fresh runtime activity arrived."),
                    signal=str(decision.get("signal") or "watchdog_reactivated"),
                )
                self.persist_session(refreshed)
                continue
            if action == "suspect":
                refreshed = self.get_session(session.session_id)
                if refreshed is None or refreshed.status == "completed":
                    continue
                self._mark_session_suspected_ended(
                    refreshed,
                    reasons=list(decision.get("signals") or []),
                    snapshot=dict(decision),
                )
                self.persist_session(refreshed)
                continue
            if action == "complete":
                refreshed = self.get_session(session.session_id)
                if refreshed is None or refreshed.status == "completed":
                    continue
                self._mark_session_suspected_ended(
                    refreshed,
                    reasons=list(decision.get("signals") or []),
                    snapshot=dict(decision),
                )
                watch_state = dict(refreshed.ai_state.get("meeting_end_watch") or {})
                watch_state["completion_requested_at"] = utcnow_iso()
                watch_state["completion_requested_by"] = "server_watchdog"
                refreshed.ai_state["meeting_end_watch"] = watch_state
                self.persist_session(refreshed)
                await self.request_session_completion(
                    refreshed.session_id,
                    requested_by="server_watchdog",
                )

    async def recover_incomplete_sessions(self) -> dict[str, Any]:
        recovered_session_ids: list[str] = []
        queued_session_ids: list[str] = []
        failed: list[dict[str, str]] = []
        for session in self._store.list_sessions():
            if not self._should_attempt_session_recovery(session):
                continue
            try:
                _session, completion = await self.request_session_completion(
                    session.session_id,
                    requested_by="startup_recovery",
                )
                if str(completion.get("status") or "") == "completed":
                    recovered_session_ids.append(session.session_id)
                else:
                    queued_session_ids.append(session.session_id)
            except Exception as exc:
                latest = self.get_session(session.session_id) or session
                latest.ai_state["startup_recovery_error"] = str(exc).strip() or exc.__class__.__name__
                self.persist_session(latest)
                failed.append(
                    {
                        "session_id": session.session_id,
                        "error": str(exc).strip() or exc.__class__.__name__,
                    }
                )
        return {
            "recovered_session_ids": recovered_session_ids,
            "queued_session_ids": queued_session_ids,
            "failed": failed,
        }

    def runtime_overview(self) -> dict[str, Any]:
        sessions = sorted(self._store.list_sessions(), key=lambda item: item.updated_at, reverse=True)
        status_counts: dict[str, int] = {}
        for session in sessions:
            status_counts[session.status] = status_counts.get(session.status, 0) + 1
        queued_jobs = [job for job in self._runner_store.list_jobs() if job.job_type == "finalize_session"]
        queued_finalizations = len([job for job in queued_jobs if job.status == "queued"])
        leased_finalizations = len([job for job in queued_jobs if job.status == "leased"])
        return {
            "service": "local-meeting-ai-runtime",
            "sessions_total": len(sessions),
            "sessions_by_status": status_counts,
            "pdf_export_ready": self._artifact_exporter.pdf_ready,
            "artifact_export_readiness": self._artifact_exporter.readiness(),
            "audio_transcription_ready": self._ai.can_transcribe_audio,
            "recording_transcription_strategy": self._ai.recording_transcription_strategy(),
            "quality_runtime_cache_state": self._ai.quality_runtime_cache_state(),
            "queued_finalizations": queued_finalizations,
            "leased_finalizations": leased_finalizations,
            "local_observer_capabilities": self._observer.capabilities,
            "local_observer_audio_devices": self._observer.audio_devices,
            "quality_readiness": self.quality_readiness(),
            "auto_observe_audio_mode": self._auto_observe_audio_mode,
            "recent_sessions": [
                {
                    "session_id": item.session_id,
                    "meeting_topic": item.meeting_topic,
                    "meeting_id": item.meeting_id,
                    "status": item.status,
                    "updated_at": item.updated_at,
                    "summary_ready": bool(item.summary),
                    "artifacts_ready": len(item.summary_exports),
                    "input_events": len(item.input_timeline),
                }
                for item in sessions[:10]
            ],
        }

    def quality_readiness(self) -> dict[str, Any]:
        ai_readiness = dict(self._ai.quality_readiness())
        observer_readiness = dict(self._observer.audio_quality_readiness())
        blocking_reasons = list(ai_readiness.get("blocking_reasons") or [])
        blocking_reasons.extend(observer_readiness.get("blocking_reasons") or [])
        return {
            "gpu_ready": bool(ai_readiness.get("gpu_ready")),
            "faster_whisper_ready": bool(ai_readiness.get("faster_whisper_ready")),
            "pyannote_ready": bool(ai_readiness.get("pyannote_ready")),
            "provider": str(ai_readiness.get("provider") or "faster_whisper_cuda"),
            "model": str(ai_readiness.get("model") or ""),
            "compute_types": list(ai_readiness.get("compute_types") or []),
            "diarization_provider": str(ai_readiness.get("diarization_provider") or ""),
            "microphone_device_ready": bool(observer_readiness.get("microphone_device_ready")),
            "meeting_output_device_ready": bool(observer_readiness.get("meeting_output_device_ready")),
            "configured_meeting_output_device": (
                str(observer_readiness.get("configured_meeting_output_device") or "").strip()
                or self._dedicated_meeting_output_device_name
                or None
            ),
            "blocking_reasons": blocking_reasons,
        }

    def _resolve_completion_mode(self, requested_mode: str | None) -> str:
        configured = str(
            requested_mode
            or os.getenv("DELEGATE_SESSION_COMPLETION_MODE", "inline")
        ).strip().lower() or "inline"
        if configured not in {"inline", "queued"}:
            raise ValueError("completion mode must be one of: inline, queued")
        return configured

    def _find_pending_finalize_job(self, session_id: str) -> RunnerJob | None:
        jobs = [
            job
            for job in self._runner_store.list_jobs(session_id=session_id)
            if job.job_type == "finalize_session" and job.status in {"queued", "leased"}
        ]
        if not jobs:
            return None
        jobs.sort(key=lambda item: item.created_at, reverse=True)
        return jobs[0]

    def _update_finalization_state(self, session: DelegateSession, **updates: Any) -> None:
        state = dict(session.ai_state.get("finalization") or {})
        state.update({key: value for key, value in updates.items() if value is not None})
        session.ai_state["finalization"] = state

    def _should_attempt_session_recovery(self, session: DelegateSession) -> bool:
        if session.status == "completed":
            return False
        task = self._audio_observer_tasks.get(session.session_id)
        if task is not None and not task.done():
            return False
        for item in session.input_timeline:
            if item.input_type != "meeting_state":
                continue
            metadata = dict(item.metadata or {})
            state_value = str(metadata.get("state") or metadata.get("status") or "").strip().lower()
            if state_value == "closed":
                return True
        return False

    def _reactivate_session(self, session: DelegateSession, *, reason: str, signal: str) -> None:
        if session.status == "completed":
            return
        watch_state = dict(session.ai_state.get("meeting_end_watch") or {})
        now = utcnow_iso()
        should_update_status = (
            session.status != "active"
            or str(watch_state.get("state") or "").strip().lower() == "suspected_ended"
        )
        if should_update_status:
            session.status = "active"
            session.status_reason = reason
        watch_state.update(
            {
                "state": "active",
                "last_evaluated_at": now,
                "last_reactivation_at": now,
                "last_recovery_signal": signal,
                "last_reason": reason,
                "last_signals": [],
                "suspected_ended_at": None,
            }
        )
        session.ai_state["meeting_end_watch"] = watch_state

    def _mark_session_suspected_ended(
        self,
        session: DelegateSession,
        *,
        reasons: list[str],
        snapshot: dict[str, Any],
    ) -> None:
        if session.status == "completed":
            return
        now = utcnow_iso()
        watch_state = dict(session.ai_state.get("meeting_end_watch") or {})
        watch_state.setdefault("suspected_ended_at", now)
        watch_state.update(
            {
                "state": "suspected_ended",
                "last_evaluated_at": now,
                "last_reason": str(snapshot.get("reason") or "The meeting may have ended."),
                "last_signals": list(reasons),
                "last_snapshot": {
                    key: value
                    for key, value in snapshot.items()
                    if key not in {"action"}
                },
            }
        )
        session.ai_state["meeting_end_watch"] = watch_state
        session.status = "suspected_ended"
        session.status_reason = str(snapshot.get("reason") or "Waiting for the meeting-end watchdog grace window.")

    def _auto_completion_watchdog_decision(self, session: DelegateSession) -> dict[str, Any]:
        shell_state = dict(session.ai_state.get("shell_liveness") or {})
        watch_state = dict(session.ai_state.get("meeting_end_watch") or {})
        now_ts = datetime.now().astimezone().timestamp()
        heartbeat_ts = self._iso_to_timestamp(shell_state.get("last_heartbeat_at"))
        heartbeat_missing = (
            heartbeat_ts is None
            or (now_ts - heartbeat_ts) >= self._session_heartbeat_timeout_seconds
        )
        heartbeat_fresh = not heartbeat_missing and heartbeat_ts is not None
        meeting_activity_ts = self._last_meeting_activity_timestamp(session)
        meeting_activity_age = None if meeting_activity_ts is None else max(now_ts - meeting_activity_ts, 0.0)
        observer_signal_ts = self._last_observer_signal_timestamp(session)
        observer_signal_age = None if observer_signal_ts is None else max(now_ts - observer_signal_ts, 0.0)
        observer_state = dict(session.ai_state.get("audio_observer") or {})
        observer_running = bool(observer_state.get("running"))
        activity_quiet = (
            meeting_activity_age is None
            or meeting_activity_age >= self._session_inactivity_grace_seconds
        )
        observer_stalled = (
            not observer_running
            or observer_signal_age is None
            or observer_signal_age >= self._session_observer_stall_seconds
        )
        completion_armed = bool(shell_state.get("completion_armed") or shell_state.get("joined"))
        has_runtime_activity = completion_armed or meeting_activity_ts is not None or bool(observer_state)
        snapshot = {
            "heartbeat_missing": heartbeat_missing,
            "heartbeat_age_seconds": None if heartbeat_ts is None else round(max(now_ts - heartbeat_ts, 0.0), 3),
            "meeting_activity_age_seconds": None if meeting_activity_age is None else round(meeting_activity_age, 3),
            "observer_signal_age_seconds": None if observer_signal_age is None else round(observer_signal_age, 3),
            "observer_running": observer_running,
            "completion_armed": completion_armed,
        }
        if not has_runtime_activity:
            return {"action": "idle", "reason": "The session is not armed for automatic completion yet.", **snapshot}
        if heartbeat_fresh:
            return {
                "action": "reactivate"
                if str(watch_state.get("state") or "").strip().lower() == "suspected_ended"
                else "idle",
                "reason": "The local control shell heartbeat is still alive.",
                "signal": "fresh_heartbeat",
                **snapshot,
            }
        signals: list[str] = []
        if heartbeat_missing:
            signals.append("heartbeat_missing")
        if activity_quiet:
            signals.append("meeting_activity_quiet")
        if observer_stalled:
            signals.append("observer_stalled")
        if not heartbeat_missing or not activity_quiet or len(signals) < 2:
            return {
                "action": "reactivate"
                if str(watch_state.get("state") or "").strip().lower() == "suspected_ended"
                else "idle",
                "reason": "Fresh meeting activity keeps this session active.",
                "signal": "fresh_meeting_activity",
                "signals": signals,
                **snapshot,
            }
        suspected_at_ts = self._iso_to_timestamp(watch_state.get("suspected_ended_at"))
        if suspected_at_ts is not None and (now_ts - suspected_at_ts) >= self._session_suspected_end_seconds:
            return {
                "action": "complete",
                "reason": "Heartbeat disappeared and the meeting stayed quiet long enough to auto-complete safely.",
                "signals": signals,
                **snapshot,
            }
        return {
            "action": "suspect",
            "reason": "Heartbeat disappeared, so the runtime is waiting through a short grace window before auto-completing.",
            "signals": signals,
            **snapshot,
        }

    def _last_meeting_activity_timestamp(self, session: DelegateSession) -> float | None:
        return self._max_timestamp(
            self._latest_item_timestamp(session.input_timeline),
            self._latest_item_timestamp(session.transcript),
            self._latest_item_timestamp(session.chat_history),
            self._iso_to_timestamp(self._latest_audio_archive_at(session)),
            self._latest_nested_timestamp(dict(session.ai_state.get("audio_observer") or {}), "last_result", "captured_at"),
        )

    def _last_observer_signal_timestamp(self, session: DelegateSession) -> float | None:
        observer_state = dict(session.ai_state.get("audio_observer") or {})
        return self._max_timestamp(
            self._iso_to_timestamp(observer_state.get("started_at")),
            self._iso_to_timestamp(observer_state.get("stopped_at")),
            self._latest_nested_timestamp(observer_state, "last_result", "captured_at"),
            self._iso_to_timestamp(self._latest_audio_archive_at(session)),
        )

    def _latest_item_timestamp(self, items: list[Any]) -> float | None:
        if not items:
            return None
        latest: float | None = None
        for item in items:
            created_at = getattr(item, "created_at", None)
            timestamp = self._iso_to_timestamp(created_at)
            if timestamp is None:
                continue
            latest = timestamp if latest is None else max(latest, timestamp)
        return latest

    def _latest_nested_timestamp(self, payload: dict[str, Any], *keys: str) -> float | None:
        current: Any = payload
        for key in keys:
            if not isinstance(current, dict):
                return None
            current = current.get(key)
        return self._iso_to_timestamp(str(current or "").strip() or None)

    def _max_timestamp(self, *timestamps: float | None) -> float | None:
        values = [item for item in timestamps if item is not None]
        return max(values) if values else None

    def _append_transcript_chunk(self, session: DelegateSession, payload: dict[str, Any]) -> None:
        text = str(payload.get("text") or "").strip()
        if not text:
            raise ValueError("Transcript text is required.")
        speaker = str(payload.get("speaker") or "unknown").strip() or "unknown"
        source = str(payload.get("source") or "manual").strip() or "manual"
        metadata = dict(payload.get("metadata") or {})
        chunk = TranscriptChunk(
            speaker=speaker,
            text=text,
            source=source,
            direct_question=bool(payload.get("direct_question", False)),
            created_at=str(payload.get("created_at") or utcnow_iso()),
            metadata=metadata,
        )
        if session.transcript:
            previous = session.transcript[-1]
            if (
                previous.speaker == chunk.speaker
                and previous.text == chunk.text
                and previous.source == chunk.source
                and previous.metadata == chunk.metadata
            ):
                return
            if self._should_merge_adjacent_audio_chunks(previous, chunk):
                self._merge_chunk_into_previous(previous, chunk)
                return
        session.transcript.append(chunk)

    def _merge_audio_transcript_chunks(
        self,
        chunks: list[TranscriptChunk],
        *,
        fallback_speaker: str,
        output_source: str,
        base_metadata: dict[str, Any] | None = None,
    ) -> list[TranscriptChunk]:
        merged: list[TranscriptChunk] = []
        normalized_base = dict(base_metadata or {})
        buffer_texts: list[str] = []
        buffer_speaker: str | None = None
        buffer_start: float | None = None
        buffer_end: float | None = None
        buffer_direct = False
        buffer_metadata: dict[str, Any] = {}
        segment_count = 0

        def flush() -> None:
            nonlocal buffer_texts, buffer_speaker, buffer_start, buffer_end, buffer_direct, buffer_metadata, segment_count
            combined = " ".join(buffer_texts).strip()
            if not combined:
                buffer_texts = []
                return
            metadata = dict(buffer_metadata)
            if buffer_start is not None:
                metadata["start_offset_seconds"] = round(buffer_start, 3)
            if buffer_end is not None:
                metadata["end_offset_seconds"] = round(buffer_end, 3)
            metadata["segment_count"] = segment_count
            created_at = self._chunk_created_at(
                metadata,
                fallback_created_at=str(metadata.get("captured_at") or utcnow_iso()),
            )
            merged.append(
                TranscriptChunk(
                    speaker=buffer_speaker or fallback_speaker,
                    text=combined,
                    source=output_source,
                    direct_question=buffer_direct,
                    created_at=created_at,
                    metadata=metadata,
                )
            )
            buffer_texts = []
            buffer_speaker = None
            buffer_start = None
            buffer_end = None
            buffer_direct = False
            buffer_metadata = {}
            segment_count = 0

        for chunk in chunks:
            text = self._normalize_transcript_text(chunk.text)
            if not text:
                continue
            metadata = self._build_audio_chunk_metadata(
                normalized_base,
                dict(chunk.metadata or {}),
                transcription_provider=str(chunk.source or "").strip() or None,
            )
            speaker = self._audio_chunk_speaker(
                chunk.speaker,
                fallback_speaker,
                audio_source=str(metadata.get("audio_source") or "").strip().lower() or None,
                metadata=metadata,
            )
            start_offset = self._metadata_seconds(metadata, "start_offset_seconds")
            end_offset = self._metadata_seconds(metadata, "end_offset_seconds")
            should_break = bool(buffer_texts and speaker != buffer_speaker)
            if should_break:
                flush()
            if not buffer_texts:
                buffer_speaker = speaker
                buffer_metadata = metadata
                buffer_start = start_offset
                buffer_end = end_offset
            buffer_texts.append(text)
            segment_count += 1
            buffer_direct = buffer_direct or bool(chunk.direct_question)
            if buffer_start is None:
                buffer_start = start_offset
            if end_offset is not None:
                buffer_end = end_offset
            if metadata.get("speaker_turn_next") is True:
                flush()
                continue
            combined = " ".join(buffer_texts).strip()
            if len(combined) >= 180 or text.endswith((".", "!", "?", "…")):
                flush()

        flush()
        return merged

    def _build_audio_chunk_metadata(
        self,
        base_metadata: dict[str, Any],
        chunk_metadata: dict[str, Any],
        *,
        transcription_provider: str | None,
    ) -> dict[str, Any]:
        metadata = dict(base_metadata)
        metadata.update(chunk_metadata)
        if transcription_provider:
            metadata["transcription_provider"] = transcription_provider
        base_session_start = self._metadata_seconds(base_metadata, "session_start_offset_seconds")
        chunk_start = self._metadata_seconds(chunk_metadata, "start_offset_seconds")
        chunk_end = self._metadata_seconds(chunk_metadata, "end_offset_seconds")
        if base_session_start is not None and chunk_start is not None:
            metadata["session_start_offset_seconds"] = round(base_session_start + chunk_start, 3)
        if base_session_start is not None and chunk_end is not None:
            metadata["session_end_offset_seconds"] = round(base_session_start + chunk_end, 3)
        audio_source = str(metadata.get("audio_source") or "").strip().lower()
        if self._source_aware_speakers and "speaker_origin" not in metadata:
            if audio_source == "microphone":
                metadata["speaker_origin"] = "local_user"
            elif audio_source == "system":
                metadata["speaker_origin"] = "remote_participant"
            elif audio_source == "conversation":
                microphone_rms = self._metadata_seconds(metadata, "microphone_audio_rms") or 0.0
                system_rms = self._metadata_seconds(metadata, "system_audio_rms") or 0.0
                threshold = float(getattr(self._observer, "_audio_rms_threshold", 0.0) or 0.0)
                if microphone_rms < threshold and system_rms >= threshold:
                    metadata["speaker_origin"] = "remote_participant"
                elif system_rms < threshold and microphone_rms >= threshold:
                    metadata["speaker_origin"] = "local_user"
        return metadata

    def _audio_chunk_speaker(
        self,
        speaker: str | None,
        fallback_speaker: str,
        *,
        audio_source: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        normalized = str(speaker or "").strip()
        if self._speaker_is_generic(normalized):
            return fallback_speaker
        diarized = self._normalize_diarized_audio_speaker(normalized, audio_source=audio_source, metadata=metadata)
        if diarized:
            return diarized
        return normalized

    def _metadata_seconds(self, metadata: dict[str, Any], key: str) -> float | None:
        value = metadata.get(key)
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _chunk_created_at(self, metadata: dict[str, Any], *, fallback_created_at: str) -> str:
        base = str(metadata.get("captured_at") or fallback_created_at or "").strip() or utcnow_iso()
        start_offset = self._metadata_seconds(metadata, "session_start_offset_seconds")
        if start_offset is None:
            start_offset = self._metadata_seconds(metadata, "start_offset_seconds")
        if start_offset is None:
            return base
        try:
            timestamp = datetime.fromisoformat(base.replace("Z", "+00:00")) + timedelta(seconds=start_offset)
        except ValueError:
            return base
        return timestamp.isoformat()

    def _chunk_sort_key(self, chunk: TranscriptChunk) -> tuple[str, float, str]:
        metadata = dict(chunk.metadata or {})
        start_offset = self._metadata_seconds(metadata, "session_start_offset_seconds")
        if start_offset is None:
            start_offset = self._metadata_seconds(metadata, "start_offset_seconds")
        start_offset = start_offset or 0.0
        return (
            f"{start_offset:012.3f}",
            start_offset,
            self._normalize_transcript_text(chunk.text).lower(),
        )

    def _dedupe_audio_chunks(self, chunks: list[TranscriptChunk]) -> list[TranscriptChunk]:
        deduped: list[TranscriptChunk] = []
        for chunk in sorted(chunks, key=self._chunk_sort_key):
            if not deduped:
                deduped.append(chunk)
                continue
            previous = deduped[-1]
            if self._audio_chunks_look_duplicate(previous, chunk):
                if self._audio_chunk_priority(chunk) >= self._audio_chunk_priority(previous):
                    deduped[-1] = chunk
                continue
            deduped.append(chunk)
        return deduped

    def _combine_adjacent_audio_chunks(self, chunks: list[TranscriptChunk]) -> list[TranscriptChunk]:
        combined: list[TranscriptChunk] = []
        for chunk in sorted(chunks, key=self._chunk_sort_key):
            if combined and self._should_merge_adjacent_audio_chunks(combined[-1], chunk):
                self._merge_chunk_into_previous(combined[-1], chunk)
                continue
            combined.append(chunk)
        return combined

    def _audio_chunks_look_duplicate(self, left: TranscriptChunk, right: TranscriptChunk) -> bool:
        left_text = self._normalize_transcript_text(left.text).lower()
        right_text = self._normalize_transcript_text(right.text).lower()
        if not left_text or not right_text:
            return False
        exact_match = left_text == right_text
        overlap_match = len(left_text) >= 12 and len(right_text) >= 12 and (
            left_text in right_text or right_text in left_text
        )
        if not exact_match and not overlap_match:
            return False
        time_gap = self._chunk_time_gap_seconds(left, right)
        if time_gap is not None and time_gap > 6.0:
            return False
        return True

    def _should_merge_adjacent_audio_chunks(self, previous: TranscriptChunk, current: TranscriptChunk) -> bool:
        if not (
            self._is_audio_transcript_source(previous.source)
            and self._is_audio_transcript_source(current.source)
        ):
            return False
        if previous.source != current.source or previous.speaker != current.speaker:
            return False
        previous_text = self._normalize_transcript_text(previous.text)
        current_text = self._normalize_transcript_text(current.text)
        if not previous_text or not current_text:
            return False
        if dict(previous.metadata or {}).get("speaker_turn_next") is True:
            return False
        if previous_text.endswith((".", "!", "?", "…")):
            return False
        time_gap = self._chunk_time_gap_seconds(previous, current)
        if time_gap is not None and time_gap > 8.0:
            return False
        if len(previous_text) >= 220:
            return False
        return True

    def _merge_chunk_into_previous(self, previous: TranscriptChunk, current: TranscriptChunk) -> None:
        previous.text = f"{self._normalize_transcript_text(previous.text)} {self._normalize_transcript_text(current.text)}".strip()
        previous.direct_question = previous.direct_question or current.direct_question
        merged_metadata = dict(previous.metadata or {})
        current_metadata = dict(current.metadata or {})
        if "end_offset_seconds" in current_metadata:
            merged_metadata["end_offset_seconds"] = current_metadata["end_offset_seconds"]
        if "session_end_offset_seconds" in current_metadata:
            merged_metadata["session_end_offset_seconds"] = current_metadata["session_end_offset_seconds"]
        if "confidence" in current_metadata and "confidence" in merged_metadata:
            try:
                merged_metadata["confidence"] = round(
                    max(float(merged_metadata["confidence"]), float(current_metadata["confidence"])),
                    4,
                )
            except (TypeError, ValueError):
                pass
        elif "confidence" in current_metadata:
            merged_metadata["confidence"] = current_metadata["confidence"]
        if "segment_confidence" in current_metadata and "segment_confidence" in merged_metadata:
            try:
                merged_metadata["segment_confidence"] = round(
                    max(float(merged_metadata["segment_confidence"]), float(current_metadata["segment_confidence"])),
                    4,
                )
            except (TypeError, ValueError):
                pass
        elif "segment_confidence" in current_metadata:
            merged_metadata["segment_confidence"] = current_metadata["segment_confidence"]
        merged_metadata["segment_count"] = int(merged_metadata.get("segment_count") or 1) + int(
            current_metadata.get("segment_count") or 1
        )
        previous.metadata = merged_metadata

    def _audio_chunk_priority(self, chunk: TranscriptChunk) -> int:
        source = str(chunk.source or "").strip().lower()
        if source.endswith("_final_offline"):
            return 4
        if source.endswith("_archive"):
            return 3
        if source.startswith("local_"):
            return 2
        return 1

    def _chunk_time_gap_seconds(self, left: TranscriptChunk, right: TranscriptChunk) -> float | None:
        left_time = self._iso_to_timestamp(left.created_at)
        right_time = self._iso_to_timestamp(right.created_at)
        if left_time is None or right_time is None:
            return None
        return abs(right_time - left_time)

    def _default_audio_speaker(self, audio_source: str, requested_speaker: str | None = None) -> str:
        candidate = str(requested_speaker or "").strip()
        if candidate and not self._speaker_is_generic(candidate):
            return candidate
        if self._source_aware_speakers:
            normalized_source = str(audio_source or "").strip().lower()
            if normalized_source == "microphone":
                return self._local_user_speaker_name
            if normalized_source == "system":
                return self._remote_participant_speaker_name
        return candidate or "meeting_audio"

    def _speaker_is_generic(self, speaker: str | None) -> bool:
        normalized = str(speaker or "").strip().lower()
        if not normalized:
            return True
        return normalized in {"participant", "unknown", "meeting_audio", "speaker", "audio"}

    def _normalize_diarized_audio_speaker(
        self,
        speaker: str,
        *,
        audio_source: str | None,
        metadata: dict[str, Any] | None = None,
    ) -> str | None:
        normalized = str(speaker or "").strip().lower()
        if not normalized:
            return None
        match = None
        if normalized.isdigit():
            match = int(normalized) + 1
        elif normalized.startswith("speaker_") and normalized.split("_")[-1].isdigit():
            index = int(normalized.split("_")[-1])
            match = index if index >= 1 else index + 1
        elif normalized.startswith("speaker-") and normalized.split("-")[-1].isdigit():
            index = int(normalized.split("-")[-1])
            match = index if index >= 1 else index + 1
        if match is None:
            return None
        if not self._source_aware_speakers:
            return f"speaker_{match}"
        source_name = str(audio_source or "").strip().lower()
        if source_name == "microphone":
            return f"{self._local_user_speaker_name}_{match}"
        if source_name == "system":
            return f"{self._remote_participant_speaker_name}_{match}"
        if source_name == "conversation":
            metadata = dict(metadata or {})
            microphone_rms = self._metadata_seconds(metadata, "microphone_audio_rms") or 0.0
            system_rms = self._metadata_seconds(metadata, "system_audio_rms") or 0.0
            threshold = float(getattr(self._observer, "_audio_rms_threshold", 0.0) or 0.0)
            if microphone_rms < threshold and system_rms >= threshold:
                return self._remote_participant_speaker_name if match == 1 else f"{self._remote_participant_speaker_name}_{match}"
            if system_rms < threshold and microphone_rms >= threshold:
                return self._local_user_speaker_name if match == 1 else f"{self._local_user_speaker_name}_{match}"
            return self._local_user_speaker_name if match == 1 else self._remote_participant_speaker_name
        return f"speaker_{match}"

    def _latest_audio_archive_at(self, session: DelegateSession) -> str | None:
        archives = list(session.ai_state.get("audio_archive_paths") or [])
        stamps = sorted(
            str(item.get("created_at") or "").strip()
            for item in archives
            if isinstance(item, dict) and str(item.get("created_at") or "").strip()
        )
        return stamps[-1] if stamps else None

    def _sorted_audio_archives(self, session: DelegateSession) -> list[dict[str, Any]]:
        return sorted(
            [
                item
                for item in list(session.ai_state.get("audio_archive_paths") or [])
                if isinstance(item, dict) and str(item.get("path") or "").strip() and Path(str(item.get("path"))).exists()
            ],
            key=lambda item: (
                float(item.get("session_start_offset_seconds") or 0.0),
                int(item.get("capture_sequence") or 0),
                str(item.get("path") or ""),
            ),
        )

    def _iso_to_timestamp(self, value: str | None) -> float | None:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None

    def _normalize_transcript_text(self, text: str) -> str:
        return " ".join(str(text or "").strip().split())

    def _is_audio_transcript_source(self, source: str) -> bool:
        normalized = str(source or "").strip().lower()
        return (
            normalized.startswith("local_")
            or normalized.endswith("_archive")
            or normalized.endswith("_final_offline")
        )

    def _env_bool(self, name: str, default: bool) -> bool:
        raw = os.getenv(name)
        if raw is None:
            return default
        return raw.strip().lower() not in {"0", "false", "no", "off"}

    def _env_int(self, name: str, default: int) -> int:
        raw = os.getenv(name)
        if raw is None:
            return default
        try:
            return int(raw.strip())
        except ValueError:
            return default

    def _merge_transcript_chunks(self, chunks: list[TranscriptChunk]) -> list[str]:
        merged: list[str] = []
        buffer: list[str] = []
        for chunk in chunks:
            text = " ".join(str(chunk.text or "").strip().split())
            if not text:
                continue
            buffer.append(text)
            combined = " ".join(buffer).strip()
            if len(combined) >= 180 or text.endswith((".", "!", "?")):
                merged.append(combined)
                buffer = []
        if buffer:
            merged.append(" ".join(buffer).strip())
        return [item for item in merged if item]

    def _session_audio_dir(self, session: DelegateSession) -> Path:
        path = self._audio_archive_dir / session.session_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _archive_audio_observation(self, session: DelegateSession, observation: dict[str, Any]) -> str | None:
        artifact_source = self._audio_observation_path(observation, include_archived=False)
        audio_bytes = observation.get("audio_bytes")
        if artifact_source is None and (not isinstance(audio_bytes, (bytes, bytearray)) or not audio_bytes):
            return None
        source_name = str(observation.get("audio_source") or "audio").strip() or "audio"
        archived_at = utcnow_iso()
        capture_seconds = max(float(observation.get("seconds") or 0.0), 0.0)
        archive_end_timestamp = self._iso_to_timestamp(archived_at) or 0.0
        archive_start_timestamp = max(archive_end_timestamp - capture_seconds, 0.0)
        baseline = session.ai_state.get("audio_capture_baseline")
        baseline_timestamp = self._iso_to_timestamp(str(baseline or "")) if baseline else None
        if baseline_timestamp is None:
            baseline_timestamp = archive_start_timestamp
            session.ai_state["audio_capture_baseline"] = datetime.fromtimestamp(
                baseline_timestamp,
            ).astimezone().isoformat()
        stamp = archived_at.replace(":", "").replace(".", "").replace("+00:00", "Z")
        archive_path = self._session_audio_dir(session) / f"{source_name}-{stamp}.wav"
        if artifact_source is not None:
            shutil.copyfile(str(artifact_source), str(archive_path))
        else:
            archive_path.write_bytes(bytes(audio_bytes))
        archives = list(session.ai_state.get("audio_archive_paths") or [])
        capture_sequence = len(archives) + 1
        session_start_offset = round(max(archive_start_timestamp - baseline_timestamp, 0.0), 3)
        session_end_offset = round(max(archive_end_timestamp - baseline_timestamp, session_start_offset), 3)
        channel_layout = observation.get("channel_layout")
        if not channel_layout:
            channel_count = int(observation.get("audio_channels") or 1)
            channel_layout = "stereo" if channel_count >= 2 else "mono"
        archives.append(
            {
                "path": str(archive_path),
                "audio_source": source_name,
                "capture_mode": observation.get("capture_mode"),
                "seconds": capture_seconds,
                "sample_rate": observation.get("sample_rate"),
                "audio_rms": observation.get("audio_rms"),
                "audio_peak": observation.get("audio_peak"),
                "capture_sequence": capture_sequence,
                "created_at": archived_at,
                "device_name": observation.get("device_name"),
                "channel_layout": channel_layout,
                "session_start_offset_seconds": session_start_offset,
                "session_end_offset_seconds": session_end_offset,
            }
        )
        session.ai_state["audio_archive_paths"] = archives
        observation["archived_at"] = archived_at
        observation["capture_sequence"] = capture_sequence
        observation["session_start_offset_seconds"] = session_start_offset
        observation["session_end_offset_seconds"] = session_end_offset
        observation["channel_layout"] = channel_layout
        return str(archive_path)

    def _build_duplex_audio_observation(
        self,
        microphone_observation: dict[str, Any],
        system_observation: dict[str, Any],
    ) -> dict[str, Any] | None:
        if importlib.util.find_spec("soundfile") is None:
            return None
        import io
        import numpy as np
        import soundfile as sf

        def load_audio(observation: dict[str, Any]) -> tuple[Any, int] | None:
            observation_path = self._audio_observation_path(observation)
            if observation_path is not None:
                try:
                    audio, rate = sf.read(str(observation_path), dtype="float32")
                except Exception:
                    audio = None
                    rate = None
                if audio is not None and rate is not None:
                    if getattr(audio, "ndim", 1) > 1:
                        audio = audio[:, 0]
                    return np.asarray(audio, dtype="float32"), int(rate)
            audio_bytes = observation.get("audio_bytes")
            if not isinstance(audio_bytes, (bytes, bytearray)) or not audio_bytes:
                return None
            try:
                audio, rate = sf.read(io.BytesIO(bytes(audio_bytes)), dtype="float32")
            except Exception:
                return None
            if getattr(audio, "ndim", 1) > 1:
                audio = audio[:, 0]
            return np.asarray(audio, dtype="float32"), int(rate)

        microphone_audio = load_audio(microphone_observation)
        system_audio = load_audio(system_observation)
        if microphone_audio is None or system_audio is None:
            return None
        microphone_samples, microphone_rate = microphone_audio
        system_samples, system_rate = system_audio
        if microphone_rate != system_rate:
            return None
        target_length = max(len(microphone_samples), len(system_samples))
        if target_length <= 0:
            return None
        if len(microphone_samples) < target_length:
            microphone_samples = np.pad(microphone_samples, (0, target_length - len(microphone_samples)))
        if len(system_samples) < target_length:
            system_samples = np.pad(system_samples, (0, target_length - len(system_samples)))
        stereo_audio = np.column_stack([microphone_samples, system_samples]).astype("float32")
        output = io.BytesIO()
        sf.write(output, stereo_audio, microphone_rate, format="WAV")
        artifact_path = Path(self._observer._artifact_dir) / "conversation-capture.wav"
        artifact_path.write_bytes(output.getvalue())
        audio_rms = float(max(float(microphone_observation.get("audio_rms") or 0.0), float(system_observation.get("audio_rms") or 0.0)))
        audio_peak = float(max(float(microphone_observation.get("audio_peak") or 0.0), float(system_observation.get("audio_peak") or 0.0)))
        return {
            "filename": "conversation-capture.wav",
            "sample_rate": microphone_rate,
            "seconds": max(float(microphone_observation.get("seconds") or 0.0), float(system_observation.get("seconds") or 0.0)),
            "artifact_path": str(artifact_path),
            "capture_mode": "local_conversation_audio",
            "audio_source": "conversation",
            "audio_channels": 2,
            "channel_layout": "stereo",
            "channel_sources": ["microphone", "system"],
            "audio_rms": audio_rms,
            "audio_peak": audio_peak,
            "microphone_audio_rms": float(microphone_observation.get("audio_rms") or 0.0),
            "microphone_audio_peak": float(microphone_observation.get("audio_peak") or 0.0),
            "system_audio_rms": float(system_observation.get("audio_rms") or 0.0),
            "system_audio_peak": float(system_observation.get("audio_peak") or 0.0),
            "below_rms_threshold": audio_rms < self._observer._audio_rms_threshold,
            "device_name": {
                "microphone": microphone_observation.get("device_name"),
                "system": system_observation.get("device_name"),
            },
        }

    def _audio_observation_path(
        self,
        observation: dict[str, Any],
        *,
        include_archived: bool = True,
    ) -> Path | None:
        candidate_keys = ["artifact_path"]
        if include_archived:
            candidate_keys.insert(0, "archived_path")
        for key in candidate_keys:
            candidate = str(observation.get(key) or "").strip()
            if not candidate:
                continue
            path = Path(candidate)
            if path.exists():
                return path
        return None

    async def _transcribe_audio_observation(self, observation: dict[str, Any]) -> list[TranscriptChunk]:
        filename = str(observation.get("filename") or "audio-capture.wav")
        recording_path = self._audio_observation_path(observation)
        if recording_path is not None:
            return await self._ai.transcribe_recording_path(recording_path, filename=filename)
        audio_bytes = observation.get("audio_bytes")
        if isinstance(audio_bytes, (bytes, bytearray)) and audio_bytes:
            return await self._ai.transcribe_recording_bytes(bytes(audio_bytes), filename=filename)
        raise LocalObserverError("Captured audio is unavailable for transcription.")

    async def _refresh_transcript_from_audio_archive(self, session: DelegateSession) -> DelegateSession:
        merged_audio = await asyncio.to_thread(self._merge_audio_archives_for_final_pass, session)
        final_result: dict[str, Any] | None = None
        rebuilt_chunks: list[TranscriptChunk] = []
        dropped_segment_count = 0
        fallback_reason: str | None = None
        try:
            final_result = await asyncio.to_thread(
                self._ai.transcribe_final_session_audio,
                microphone_path=merged_audio.get("microphone_path"),
                meeting_output_path=merged_audio.get("meeting_output_path"),
            )
            rebuilt_chunks, dropped_segment_count = self._finalize_offline_audio_chunks(
                list(final_result.get("chunks") or []),
                baseline_started_at=str(merged_audio.get("baseline_started_at") or ""),
            )
            if not rebuilt_chunks:
                fallback_reason = "Final merged-track retranscription produced no usable transcript chunks."
        except Exception as exc:
            fallback_reason = str(exc).strip() or "Final merged-track retranscription failed."

        if not rebuilt_chunks:
            if self._should_attempt_archive_segment_fallback(fallback_reason):
                archive_fallback = await asyncio.to_thread(
                    self._transcribe_archived_audio_segments_for_final_pass,
                    session,
                    baseline_started_at=str(merged_audio.get("baseline_started_at") or ""),
                    archives=list(merged_audio.get("archives") or []),
                )
                rebuilt_chunks = list(archive_fallback.get("chunks") or [])
                dropped_segment_count = int(archive_fallback.get("dropped_segment_count") or 0)
                final_result = archive_fallback
                if fallback_reason:
                    final_result["fallback_reason"] = fallback_reason

        if not rebuilt_chunks or final_result is None:
            raise RuntimeError(
                fallback_reason or "Final transcription quality pass produced no usable transcript chunks."
            )

        session.transcript = [
            chunk
            for chunk in session.transcript
            if not self._is_audio_transcript_source(str(chunk.source or ""))
        ] + rebuilt_chunks
        session.transcript.sort(key=self._chunk_sort_key)
        quality_pass_at = utcnow_iso()
        session.ai_state["final_audio_retranscribed_at"] = quality_pass_at
        session.ai_state["final_transcription"] = {
            "status": "success",
            "provider": str(final_result.get("provider") or "faster_whisper_cuda"),
            "model": str(final_result.get("model") or ""),
            "compute_type": str(final_result.get("compute_type") or ""),
            "diarization_provider": str(final_result.get("diarization_provider") or ""),
            "quality_pass_at": quality_pass_at,
            "quality_pass": str(final_result.get("quality_pass") or "final_offline"),
            "dropped_segment_count": dropped_segment_count + int(final_result.get("dropped_segment_count") or 0),
            "archive_gap_count": int(merged_audio.get("archive_gap_count") or 0),
            "readiness_snapshot": self.quality_readiness(),
            "readiness_snapshot_at": quality_pass_at,
            "merged_tracks": {
                "microphone_path": merged_audio.get("microphone_path"),
                "meeting_output_path": merged_audio.get("meeting_output_path"),
                "conversation_path": merged_audio.get("conversation_path"),
            },
            "input_strategy": str(final_result.get("input_strategy") or "merged_tracks"),
            "baseline_started_at": merged_audio.get("baseline_started_at"),
        }
        if final_result.get("fallback_reason"):
            session.ai_state["final_transcription"]["fallback_reason"] = str(final_result.get("fallback_reason"))
        return self.persist_session(session)

    def _merge_audio_archives_for_final_pass(self, session: DelegateSession) -> dict[str, Any]:
        archives = self._sorted_audio_archives(session)
        if not archives:
            raise RuntimeError("No archived audio was available for the final transcription quality pass.")

        readiness = self.quality_readiness()
        if readiness["blocking_reasons"]:
            raise RuntimeError(" | ".join(str(item) for item in readiness["blocking_reasons"]))

        archives.sort(
            key=lambda item: (
                float(item.get("session_start_offset_seconds") or 0.0),
                int(item.get("capture_sequence") or 0),
                str(item.get("path") or ""),
            )
        )
        baseline_started_at = str(
            session.ai_state.get("audio_capture_baseline")
            or archives[0].get("created_at")
            or utcnow_iso()
        )
        microphone_archives = [item for item in archives if str(item.get("audio_source") or "").strip().lower() == "microphone"]
        system_archives = [item for item in archives if str(item.get("audio_source") or "").strip().lower() == "system"]
        conversation_archives = [item for item in archives if str(item.get("audio_source") or "").strip().lower() == "conversation"]
        if not microphone_archives:
            raise RuntimeError("No archived microphone track was available for the final transcription quality pass.")
        if not system_archives:
            raise RuntimeError("No archived meeting-output track was available for the final transcription quality pass.")

        microphone_track = self._compose_final_audio_track(
            session,
            archives=microphone_archives,
            output_name="microphone",
            channels=1,
        )
        system_track = self._compose_final_audio_track(
            session,
            archives=system_archives,
            output_name="meeting-output",
            channels=1,
        )
        conversation_track = None
        if conversation_archives:
            conversation_track = self._compose_final_audio_track(
                session,
                archives=conversation_archives,
                output_name="conversation",
                channels=2,
            )

        return {
            "baseline_started_at": baseline_started_at,
            "microphone_path": microphone_track.get("path"),
            "meeting_output_path": system_track.get("path"),
            "conversation_path": None if conversation_track is None else conversation_track.get("path"),
            "archive_gap_count": int(microphone_track.get("gap_count") or 0) + int(system_track.get("gap_count") or 0),
            "archives": archives,
        }

    def _transcribe_archived_audio_segments_for_final_pass(
        self,
        session: DelegateSession,
        *,
        baseline_started_at: str,
        archives: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        archive_items = list(archives or self._sorted_audio_archives(session))
        rebuilt_input: list[TranscriptChunk] = []
        dropped_segment_count = 0
        compute_type = ""
        diarization_provider: str | None = None
        pyannote_ready = bool(self.quality_readiness().get("pyannote_ready"))

        for item in archive_items:
            raw_source = str(item.get("audio_source") or "").strip().lower()
            raw_path = str(item.get("path") or "").strip()
            if raw_source not in {"microphone", "system"} or not raw_path:
                continue
            session_offset = float(item.get("session_start_offset_seconds") or 0.0)
            enable_diarization = raw_source == "system" and pyannote_ready and float(item.get("seconds") or 0.0) >= 1.0
            result = self._ai.transcribe_audio_path_high_quality(
                input_path=raw_path,
                fallback_speaker="local_user" if raw_source == "microphone" else "remote_participant",
                source="microphone_final_offline" if raw_source == "microphone" else "meeting_output_final_offline",
                channel_origin="local_user" if raw_source == "microphone" else "meeting_output",
                session_offset_base_seconds=session_offset,
                quality_pass="final_offline_archive",
                enable_diarization=enable_diarization,
            )
            rebuilt_input.extend(list(result.get("chunks") or []))
            dropped_segment_count += int(result.get("dropped_segment_count") or 0)
            compute_type = str(result.get("compute_type") or compute_type)
            diarization_provider = str(result.get("diarization_provider") or diarization_provider or "") or diarization_provider

        rebuilt_chunks, cleanup_dropped = self._finalize_offline_audio_chunks(
            rebuilt_input,
            baseline_started_at=baseline_started_at,
        )
        return {
            "chunks": rebuilt_chunks,
            "provider": str(self._ai.quality_readiness().get("provider") or "faster_whisper_cuda"),
            "model": self._ai.quality_readiness().get("model"),
            "compute_type": compute_type,
            "diarization_provider": diarization_provider,
            "quality_pass": "final_offline",
            "dropped_segment_count": dropped_segment_count + cleanup_dropped,
            "input_strategy": "archive_segments",
        }

    def _should_attempt_archive_segment_fallback(self, failure_reason: str | None) -> bool:
        normalized = str(failure_reason or "").strip().lower()
        if not normalized:
            return True
        blocking_markers = (
            "cuda gpu is not available",
            "faster-whisper is not installed",
            "final transcription quality pass is blocked",
            "live high-quality transcription is blocked",
            "no archived audio was available",
            "soundfile is required",
            "pyannote.audio",
            "hugging face token",
        )
        return not any(marker in normalized for marker in blocking_markers)

    def _compose_final_audio_track(
        self,
        session: DelegateSession,
        *,
        archives: list[dict[str, Any]],
        output_name: str,
        channels: int,
    ) -> dict[str, Any]:
        if importlib.util.find_spec("soundfile") is None:
            raise RuntimeError("soundfile is required to build merged session audio tracks.")
        import numpy as np
        import soundfile as sf

        entries: list[dict[str, Any]] = []
        sample_rate: int | None = None
        total_frames = 0
        gap_count = 0
        last_end_frame = 0

        for item in sorted(
            archives,
            key=lambda entry: (
                float(entry.get("session_start_offset_seconds") or 0.0),
                int(entry.get("capture_sequence") or 0),
            ),
        ):
            path = Path(str(item.get("path") or "").strip())
            audio, rate = sf.read(str(path), dtype="float32")
            if sample_rate is None:
                sample_rate = int(rate)
            elif int(rate) != sample_rate:
                raise RuntimeError("Archived audio sample rates did not match for final track composition.")
            normalized_audio = self._normalize_audio_channels(audio, channels)
            start_offset = max(float(item.get("session_start_offset_seconds") or 0.0), 0.0)
            start_frame = max(int(round(start_offset * sample_rate)), 0)
            end_frame = start_frame + int(normalized_audio.shape[0] if getattr(normalized_audio, "ndim", 1) > 1 else len(normalized_audio))
            if start_frame > last_end_frame:
                gap_count += 1
            last_end_frame = max(last_end_frame, end_frame)
            total_frames = max(total_frames, end_frame)
            entries.append(
                {
                    "audio": normalized_audio,
                    "start_frame": start_frame,
                    "end_frame": end_frame,
                }
            )

        if sample_rate is None or total_frames <= 0:
            raise RuntimeError(f"No usable audio frames were available for final {output_name} track composition.")

        if channels == 1:
            track = np.zeros((total_frames,), dtype="float32")
        else:
            track = np.zeros((total_frames, channels), dtype="float32")
        weights = np.zeros((total_frames,), dtype="float32")
        for entry in entries:
            start_frame = int(entry["start_frame"])
            end_frame = int(entry["end_frame"])
            audio = entry["audio"]
            track[start_frame:end_frame] += audio
            weights[start_frame:end_frame] += 1.0
        nonzero = weights > 0
        if channels == 1:
            track[nonzero] = track[nonzero] / weights[nonzero]
        else:
            track[nonzero] = track[nonzero] / weights[nonzero, None]

        output_path = self._session_audio_dir(session) / f"final-{output_name}.wav"
        sf.write(str(output_path), track, sample_rate, format="WAV")
        return {
            "path": str(output_path),
            "sample_rate": sample_rate,
            "gap_count": gap_count,
            "seconds": round(total_frames / sample_rate, 3),
        }

    def _normalize_audio_channels(self, audio: Any, channels: int) -> Any:
        import numpy as np

        normalized = np.asarray(audio, dtype="float32")
        if channels == 1:
            if getattr(normalized, "ndim", 1) == 1:
                return normalized
            return normalized[:, 0]
        if getattr(normalized, "ndim", 1) == 1:
            return np.column_stack([normalized, normalized]).astype("float32")
        if normalized.shape[1] == channels:
            return normalized
        if normalized.shape[1] > channels:
            return normalized[:, :channels]
        if normalized.shape[1] == 1 and channels == 2:
            return np.column_stack([normalized[:, 0], normalized[:, 0]]).astype("float32")
        raise RuntimeError("Unsupported audio channel layout for final track composition.")

    def _finalize_offline_audio_chunks(
        self,
        chunks: list[TranscriptChunk],
        *,
        baseline_started_at: str,
    ) -> tuple[list[TranscriptChunk], int]:
        cleaned: list[TranscriptChunk] = []
        dropped = 0
        for chunk in chunks:
            metadata = dict(chunk.metadata or {})
            start_offset = self._metadata_seconds(metadata, "session_start_offset_seconds")
            if start_offset is None:
                start_offset = self._metadata_seconds(metadata, "start_offset_seconds")
                if start_offset is not None:
                    metadata["session_start_offset_seconds"] = round(start_offset, 3)
            end_offset = self._metadata_seconds(metadata, "session_end_offset_seconds")
            if end_offset is None:
                end_offset = self._metadata_seconds(metadata, "end_offset_seconds")
                if end_offset is not None:
                    metadata["session_end_offset_seconds"] = round(end_offset, 3)
            chunk.text = self._normalize_transcript_text(chunk.text)
            chunk.metadata = metadata
            if self._should_drop_final_audio_chunk(chunk):
                dropped += 1
                continue
            chunk.created_at = self._created_at_from_session_offset(
                baseline_started_at,
                start_offset,
                fallback=chunk.created_at,
            )
            cleaned.append(chunk)

        cleaned = self._dedupe_audio_chunks(cleaned)
        cleaned = self._combine_adjacent_audio_chunks(cleaned)
        return cleaned, dropped

    def _should_drop_final_audio_chunk(self, chunk: TranscriptChunk) -> bool:
        text = self._normalize_transcript_text(chunk.text)
        lowered = text.lower()
        if not text:
            return True
        if re.fullmatch(r"[\W_]+", text):
            return True
        if re.fullmatch(r"(?:\d+[,\s.-]*){2,}", text):
            return True
        if lowered in {"아", "어", "음", "으", "아 아", "어 어", "음 음", "으 으"}:
            duration = self._chunk_duration_seconds(chunk)
            if duration is not None and duration <= 0.8:
                return True
        if any(
            marker in text
            for marker in (
                "시청해주셔서 감사합니다",
                "다음 영상에서 만나요",
                "한글자막 제공",
                "이 시각 세계였습니다",
            )
        ):
            return True
        duration = self._chunk_duration_seconds(chunk)
        confidence = self._metadata_seconds(dict(chunk.metadata or {}), "segment_confidence")
        no_speech_prob = self._metadata_seconds(dict(chunk.metadata or {}), "no_speech_prob")
        if duration is not None and duration <= 0.8:
            if confidence is not None and confidence <= 0.35:
                return True
            if no_speech_prob is not None and no_speech_prob >= 0.6:
                return True
        return False

    def _chunk_duration_seconds(self, chunk: TranscriptChunk) -> float | None:
        metadata = dict(chunk.metadata or {})
        start = self._metadata_seconds(metadata, "session_start_offset_seconds")
        end = self._metadata_seconds(metadata, "session_end_offset_seconds")
        if start is None:
            start = self._metadata_seconds(metadata, "start_offset_seconds")
        if end is None:
            end = self._metadata_seconds(metadata, "end_offset_seconds")
        if start is None or end is None:
            return None
        return max(end - start, 0.0)

    def _created_at_from_session_offset(self, baseline_started_at: str, offset_seconds: float | None, *, fallback: str) -> str:
        if offset_seconds is None:
            return fallback
        baseline_timestamp = self._iso_to_timestamp(baseline_started_at)
        if baseline_timestamp is None:
            return fallback
        return datetime.fromtimestamp(baseline_timestamp + offset_seconds).astimezone().isoformat()

    def _merge_audio_archive_files(self, paths: list[str]) -> bytes:
        if importlib.util.find_spec("soundfile") is None:
            return b""
        import io
        import numpy as np
        import soundfile as sf

        audio_parts: list[np.ndarray] = []
        target_rate: int | None = None
        for raw_path in paths:
            path = Path(raw_path)
            if not path.exists():
                continue
            audio, sample_rate = sf.read(str(path), dtype="float32")
            if audio.ndim > 1:
                audio = audio[:, 0]
            if target_rate is None:
                target_rate = int(sample_rate)
            elif int(sample_rate) != target_rate:
                continue
            audio_parts.append(audio)
        if not audio_parts or target_rate is None:
            return b""
        merged = np.concatenate(audio_parts)
        output = io.BytesIO()
        sf.write(output, merged, target_rate, format="WAV")
        return output.getvalue()

    def _append_chat_turn_to_session(self, session: DelegateSession, payload: dict[str, Any]) -> ChatTurn:
        text = str(payload.get("text") or "").strip()
        if not text:
            raise ValueError("Meeting chat text is required.")

        role = str(payload.get("role") or "participant").strip().lower() or "participant"
        if role not in {"participant", "bot", "system"}:
            raise ValueError(f"Unsupported chat role: {role}")

        speaker = str(payload.get("speaker") or role).strip() or role
        source = str(payload.get("source") or "meeting_chat").strip() or "meeting_chat"
        publish_requested = bool(payload.get("publish_requested", False))
        status = str(payload.get("status") or "sent").strip() or "sent"

        if session.chat_history:
            previous = session.chat_history[-1]
            if previous.speaker == speaker and previous.text == text and previous.source == source:
                return previous

        turn = ChatTurn(
            turn_id=uuid4().hex[:12],
            role=role,  # type: ignore[arg-type]
            speaker=speaker,
            text=text,
            source=source,
            status=status,
            created_at=str(payload.get("created_at") or utcnow_iso()),
            publish_requested=publish_requested,
            delivery=dict(payload.get("delivery") or {}),
        )
        session.chat_history.append(turn)
        return turn

    def _record_input(
        self,
        session: DelegateSession,
        *,
        input_type: str,
        speaker: str | None,
        text: str | None,
        source: str,
        direct_question: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        normalized_text = str(text or "").strip() or None
        normalized_speaker = str(speaker or "").strip() or None
        input_item = MeetingInput(
            input_id=uuid4().hex[:12],
            input_type=input_type,  # type: ignore[arg-type]
            speaker=normalized_speaker,
            text=normalized_text,
            source=str(source or "meeting_input").strip() or "meeting_input",
            direct_question=direct_question,
            metadata=dict(metadata or {}),
        )
        if session.input_timeline:
            previous = session.input_timeline[-1]
            if (
                previous.input_type == input_item.input_type
                and previous.speaker == input_item.speaker
                and previous.text == input_item.text
                and previous.source == input_item.source
            ):
                return
        session.input_timeline.append(input_item)

    def _append_workspace_input_event(self, session: DelegateSession, payload: dict[str, Any]) -> None:
        input_type = str(payload["input_type"])
        speaker = str(payload.get("speaker") or "").strip() or None
        metadata = dict(payload.get("metadata") or {})
        text = str(payload.get("text") or "").strip() or self._workspace_input_text(input_type, speaker, metadata)
        session.workspace_events.append(
            WorkspaceEvent(
                event_id=uuid4().hex[:12],
                event_type=f"meeting_input.{input_type}",
                direction="inbound",
                status="processed",
                source=str(payload.get("source") or "meeting_input"),
                speaker=speaker,
                text=text,
                meeting_id=session.meeting_id or session.meeting_number,
                metadata=metadata,
                processed_at=utcnow_iso(),
            )
        )
        if input_type == "meeting_state":
            reported_status = str(metadata.get("status") or "").strip()
            if reported_status in ALLOWED_SESSION_STATUSES:
                session.status = reported_status  # type: ignore[assignment]
                session.status_reason = text or f"Meeting state updated to {reported_status}."

    def _workspace_input_text(self, input_type: str, speaker: str | None, metadata: dict[str, Any]) -> str:
        if input_type == "participant_state":
            participant = speaker or str(metadata.get("participant") or "participant")
            state = str(metadata.get("state") or metadata.get("status") or "updated").strip() or "updated"
            return f"{participant} participant state changed: {state}."
        if input_type == "meeting_state":
            state = str(metadata.get("state") or metadata.get("status") or "updated").strip() or "updated"
            return f"Meeting state changed: {state}."
        return str(metadata.get("note") or "").strip()

    def _build_preflight(self, session: DelegateSession, meeting_payload: dict[str, Any]) -> dict[str, Any]:
        settings = meeting_payload.get("settings", {})
        if not isinstance(settings, dict):
            settings = {}

        risks = []
        blockers = []
        actions = []

        if not session.join_url:
            blockers.append(
                {
                    "code": "missing_join_url",
                    "message": "No join URL is available for the delegate session.",
                    "mitigation": "Fetch meeting metadata from Zoom or provide a join URL manually.",
                }
            )
        if settings.get("waiting_room") is True:
            risks.append(
                {
                    "code": "waiting_room_enabled",
                    "message": "The delegate will need host admission from the waiting room.",
                    "mitigation": "Coordinate host admission at the start of the meeting.",
                }
            )
            actions.append("Host must admit the delegate from the waiting room.")
        if settings.get("join_before_host") is False:
            risks.append(
                {
                    "code": "join_before_host_disabled",
                    "message": "The host likely needs to join before the delegate can enter.",
                    "mitigation": "Have the host arrive first or enable join before host.",
                }
            )
        if settings.get("meeting_authentication") is True:
            blockers.append(
                {
                    "code": "meeting_authentication_enabled",
                    "message": "Meeting authentication may block the delegate runtime.",
                    "mitigation": "Authorize the app or relax authentication for this meeting.",
                }
            )
        if session.delegate_mode == "approval_required":
            actions.append("Keep a human operator available to approve spoken replies.")
        elif session.delegate_mode == "answer_on_ask":
            actions.append("Limit active replies to direct questions or explicit mentions.")

        readiness = "ready"
        if blockers:
            readiness = "blocked"
        elif risks or actions:
            readiness = "needs_attention"

        return {
            "readiness": readiness,
            "risks": risks,
            "blocking_items": blockers,
            "required_actions": actions,
        }

    def _apply_ai_summary_intelligence(self, session: DelegateSession, ai_result: dict[str, Any]) -> None:
        packet = dict(session.summary_packet or {})
        intelligence = dict(packet.get("meeting_intelligence") or {})
        decisions = [str(item).strip() for item in (ai_result.get("decisions") or []) if str(item).strip()]
        open_questions = [str(item).strip() for item in (ai_result.get("open_questions") or []) if str(item).strip()]
        risk_signals = [str(item).strip() for item in (ai_result.get("risk_signals") or []) if str(item).strip()]
        if decisions:
            intelligence["decisions"] = decisions[:10]
        if open_questions:
            intelligence["open_questions"] = open_questions[:10]
        if risk_signals:
            intelligence["risk_signals"] = risk_signals[:10]
        if decisions or open_questions or risk_signals:
            intelligence["source"] = "ai_summary"
        packet["meeting_intelligence"] = intelligence
        if session.action_items:
            packet["action_candidates"] = list(session.action_items[:10])
        packet["briefing"] = self._summary_pipeline.build_briefing(
            session,
            packet=packet,
            ai_result=ai_result,
        )
        session.summary_packet = packet

    async def _observe_conversation_audio(
        self,
        session: DelegateSession,
        *,
        seconds: float,
        sample_rate: int | None,
        payload: dict[str, Any],
    ) -> tuple[DelegateSession, dict[str, Any]]:
        microphone_kwargs: dict[str, Any] = {
            "seconds": seconds,
            "sample_rate": sample_rate,
            "source": "microphone",
        }
        microphone_device_name = str(payload.get("microphone_device_name") or payload.get("device_name") or "").strip()
        if microphone_device_name:
            microphone_kwargs["device_name"] = microphone_device_name
        system_kwargs: dict[str, Any] = {
            "seconds": seconds,
            "sample_rate": sample_rate,
            "source": "system",
        }
        system_device_name = str(payload.get("system_device_name") or payload.get("device_name") or "").strip()
        if system_device_name:
            system_kwargs["device_name"] = system_device_name
        capture_tasks = [
            asyncio.to_thread(self._observer.capture_audio, **microphone_kwargs),
            asyncio.to_thread(self._observer.capture_audio, **system_kwargs),
        ]
        microphone_result, system_result = await asyncio.gather(*capture_tasks, return_exceptions=True)
        observations: list[dict[str, Any]] = []
        if isinstance(microphone_result, Exception) or isinstance(system_result, Exception):
            errors = []
            if isinstance(microphone_result, Exception):
                errors.append(str(microphone_result))
            if isinstance(system_result, Exception):
                errors.append(str(system_result))
            raise LocalObserverError("Conversation audio capture failed: " + " | ".join(errors))

        microphone_observation = dict(microphone_result)
        system_observation = dict(system_result)
        observed_session, result = await self._process_captured_conversation_observations(
            session,
            payload=payload,
            microphone_observation=microphone_observation,
            system_observation=system_observation,
            allow_mixed_fallback=True,
        )
        observations.extend(list(result.get("observations") or []))
        result["observations"] = observations
        return observed_session, result

    def _write_exports(self, session: DelegateSession) -> None:
        session_dir = self._export_dir / session.session_id
        session_dir.mkdir(parents=True, exist_ok=True)

        export_stem = self._summary_export_stem(session)
        summary_md = session_dir / f"{export_stem}.md"
        summary_json = session_dir / f"{export_stem}.json"
        transcript_md = session_dir / "transcript.md"
        packet_json = session_dir / f"{export_stem}-packet.json"

        summary_md.write_text(self._summary_pipeline.render_summary_markdown(session), encoding="utf-8")
        summary_json.write_text(
            json.dumps(
                {
                    "summary": session.summary,
                    "action_items": session.action_items,
                    "briefing": dict((session.summary_packet or {}).get("briefing") or {}),
                    "meeting_intelligence": dict((session.summary_packet or {}).get("meeting_intelligence") or {}),
                    "meeting": {
                        "session_id": session.session_id,
                        "meeting_id": session.meeting_id,
                        "meeting_number": session.meeting_number,
                        "meeting_topic": session.meeting_topic,
                        "status": session.status,
                    },
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        transcript_md.write_text(self._summary_pipeline.render_transcript_markdown(session), encoding="utf-8")
        packet_json.write_text(json.dumps(session.summary_packet, ensure_ascii=False, indent=2), encoding="utf-8")

        session.summary_exports = [
            {"format": "md", "path": str(summary_md)},
            {"format": "json", "path": str(summary_json)},
            {"format": "packet_json", "path": str(packet_json)},
        ]
        session.transcript_exports = [{"format": "md", "path": str(transcript_md)}]
        try:
            session.summary_exports.extend(self._artifact_exporter.export_summary_bundle(summary_md))
        except ArtifactExportError as exc:
            session.ai_state["artifact_export_error"] = str(exc)
        finally:
            if self._auto_cleanup_enabled:
                self._run_storage_housekeeping()

    def _summary_export_stem(self, session: DelegateSession) -> str:
        briefing = dict((session.summary_packet or {}).get("briefing") or {})
        title = self._summary_pipeline._display_title(session, briefing).strip()
        safe_title = self._sanitize_export_component(title) or "회의 요약"
        timestamp = self._export_timestamp_label(session)
        return f"{timestamp}-{safe_title}"[:140].rstrip(" .-_")

    def _export_timestamp_label(self, session: DelegateSession) -> str:
        raw = str(session.created_at or session.updated_at or "").strip()
        if not raw:
            return "meeting"
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return "meeting"
        return parsed.astimezone(KST).strftime("%Y-%m-%d_%H-%M")

    def _sanitize_export_component(self, value: str) -> str:
        cleaned = re.sub(r"[\\\\/:*?\"<>|]+", "-", str(value or "").strip())
        cleaned = re.sub(r"\s+", "-", cleaned)
        cleaned = re.sub(r"-{2,}", "-", cleaned)
        cleaned = cleaned.strip(" .-_")
        return cleaned[:100]

    def _run_storage_housekeeping(self) -> None:
        try:
            self._cleanup_observer_tmp_artifacts()
        except Exception:
            pass
        try:
            self._cleanup_old_audio_archives()
        except Exception:
            pass

    def _cleanup_observer_tmp_artifacts(self) -> None:
        artifact_dir = Path(
            getattr(self._observer, "_artifact_dir", None)
            or os.getenv("DELEGATE_LOCAL_OBSERVER_DIR", ".tmp/local-observer")
        )
        if not artifact_dir.exists():
            return
        cutoff = datetime.now(timezone.utc) - timedelta(hours=self._observer_tmp_retention_hours)
        for path in artifact_dir.glob("*.wav"):
            try:
                if not path.is_file():
                    continue
                name = path.name.lower()
                if not (
                    name.startswith("microphone-live-")
                    or name.startswith("system-live-")
                    or name.startswith("conversation-live-")
                    or name == "conversation-capture.wav"
                ):
                    continue
                modified = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
                if modified <= cutoff:
                    path.unlink(missing_ok=True)
            except Exception:
                continue

    def _cleanup_old_audio_archives(self) -> None:
        keep_count = self._audio_keep_session_count
        if keep_count <= 0 or not self._audio_archive_dir.exists():
            return
        session_dirs = [item for item in self._audio_archive_dir.iterdir() if item.is_dir()]
        if len(session_dirs) <= keep_count:
            return
        session_dirs.sort(key=lambda item: item.stat().st_mtime, reverse=True)
        for stale_dir in session_dirs[keep_count:]:
            try:
                shutil.rmtree(stale_dir, ignore_errors=True)
            except Exception:
                continue

    def _require_session(self, session_id: str) -> DelegateSession:
        session = self.get_session(session_id)
        if session is None:
            raise KeyError(f"Unknown delegate session: {session_id}")
        return session

    def _retire_conflicting_sessions(self, new_session: DelegateSession) -> None:
        for existing in self._store.list_sessions():
            if existing.session_id == new_session.session_id:
                continue
            if existing.bot_display_name != new_session.bot_display_name:
                continue
            if existing.status not in {"planned", "joining", "active", "suspected_ended", "blocked"}:
                continue
            same_meeting = False
            if new_session.meeting_number and existing.meeting_number == new_session.meeting_number:
                same_meeting = True
            elif new_session.join_url and existing.join_url == new_session.join_url:
                same_meeting = True
            elif new_session.meeting_topic and existing.meeting_topic == new_session.meeting_topic:
                same_meeting = True
            if not same_meeting:
                continue
            existing.status = "completed"
            existing.status_reason = f"Superseded by newer delegate session {new_session.session_id}."
            self.persist_session(existing)

    async def _maybe_prime_ai_thread(self, session: DelegateSession) -> None:
        if not self._env_bool("DELEGATE_PRIME_CODEX_THREAD", True):
            return
        if session.delegate_mode == "listen_only":
            return
        if str(session.ai_state.get("codex_thread_id") or "").strip():
            return
        try:
            await asyncio.to_thread(self._ai.prime_session_thread, session)
        except Exception as exc:
            session.ai_state["codex_thread_prime_error"] = str(exc)
        finally:
            self.persist_session(session)

    def _env_bool(self, name: str, default: bool) -> bool:
        raw = os.getenv(name)
        if raw is None:
            return default
        return raw.strip().lower() not in {"0", "false", "no", "off"}

    def _env_float(self, name: str, default: float) -> float:
        raw = os.getenv(name)
        if raw is None:
            return default
        try:
            return float(raw.strip())
        except ValueError:
            return default

    def _reply_decision(self, session: DelegateSession, turn: ChatTurn, payload: dict[str, Any]) -> dict[str, str | bool]:
        if turn.role == "bot":
            return {"should_reply": False, "trigger": "self_message", "reason": "Bot messages do not trigger replies."}

        explicit_direct = bool(payload.get("direct_question", False))
        mentioned = self._contains_bot_mention(session.bot_display_name, turn.text)

        if session.delegate_mode == "listen_only":
            return {"should_reply": False, "trigger": "listen_only", "reason": "Delegate is in listen-only mode."}
        if session.delegate_mode == "answer_on_ask" and not (explicit_direct or mentioned):
            return {
                "should_reply": False,
                "trigger": "not_direct",
                "reason": "Replying is limited to direct questions or explicit bot mentions.",
            }
        if session.delegate_mode == "approval_required" and not (explicit_direct or mentioned):
            return {
                "should_reply": False,
                "trigger": "not_direct",
                "reason": "Approval mode still requires a direct question or explicit bot mention.",
            }
        return {
            "should_reply": True,
            "trigger": "direct_question" if explicit_direct else "explicit_mention",
            "reason": "The message directly addressed the delegate.",
        }

    async def _maybe_generate_reply(
        self,
        session: DelegateSession,
        latest_turn: ChatTurn,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        decision = self._reply_decision(session, latest_turn, payload)
        if not decision["should_reply"]:
            return {
                "status": "ignored",
                "reason": decision["reason"],
                "trigger": decision["trigger"],
            }

        reply_key = self._reply_key_for_turn(latest_turn, payload)
        request_text = self._build_reply_request_text(session, latest_turn)
        draft = await self._ai.draft_reply(session, request_text)
        draft_text = str(draft.get("draft") or "").strip()
        reply_status = "ready_to_publish"

        if session.delegate_mode == "approval_required":
            approval_id = uuid4().hex[:12]
            session.approvals.append(
                ApprovalRequest(
                    approval_id=approval_id,
                    request_text=request_text,
                    draft=draft_text,
                    speaker=latest_turn.speaker,
                    source=latest_turn.source,
                    status="pending",
                )
            )
            reply_status = "pending_approval"

        if reply_status == "pending_approval":
            session.draft_replies.append(
                {
                    "reply_id": uuid4().hex[:12],
                    "speaker": latest_turn.speaker,
                    "source": latest_turn.source,
                    "trigger": decision["trigger"],
                    "request_text": request_text,
                    "draft": draft_text,
                    "status": reply_status,
                    "provider": draft.get("provider"),
                    "created_at": utcnow_iso(),
                }
            )
            session.workspace_events.append(
                WorkspaceEvent(
                    event_id=uuid4().hex[:12],
                    event_type="delegate.reply",
                    direction="system",
                    status="pending_approval",
                    source="local_ai",
                    speaker=session.bot_display_name,
                    text=draft_text,
                    meeting_id=session.meeting_id or session.meeting_number,
                    response_expected=False,
                    publish_requested=False,
                    metadata={
                        "trigger": decision["trigger"],
                        "reply_status": reply_status,
                        "provider": draft.get("provider"),
                        "reply_preview": draft_text,
                    },
                    processed_at=utcnow_iso(),
                )
            )
        return {
            "status": reply_status,
            "trigger": decision["trigger"],
            "reason": decision["reason"],
            "draft": draft_text,
            "provider": draft.get("provider"),
            "reply_key": reply_key,
        }

    def _record_reply_error(
        self,
        session: DelegateSession,
        latest_turn: ChatTurn,
        exc: Exception,
    ) -> dict[str, Any]:
        message = str(exc).strip() or exc.__class__.__name__
        session.ai_state["last_reply_error"] = message
        session.workspace_events.append(
            WorkspaceEvent(
                event_id=uuid4().hex[:12],
                event_type="delegate.reply_error",
                direction="system",
                status="failed",
                source="local_ai",
                speaker=session.bot_display_name,
                text=message,
                meeting_id=session.meeting_id or session.meeting_number,
                response_expected=False,
                publish_requested=False,
                metadata={
                    "speaker": latest_turn.speaker,
                    "source": latest_turn.source,
                    "error": message,
                },
                processed_at=utcnow_iso(),
            )
        )
        self.persist_session(session)
        return {
            "status": "error",
            "trigger": "direct_question",
            "reason": message,
            "draft": "",
            "provider": "codex_exec",
        }

    def _normalize_input_item(self, item: dict[str, Any]) -> dict[str, Any]:
        metadata = dict(item.get("metadata") or {})
        input_type_raw = str(item.get("input_type") or item.get("type") or "").strip().lower()
        aliases = {
            "transcript": "spoken_transcript",
            "speech": "spoken_transcript",
            "audio": "spoken_transcript",
            "caption": "spoken_transcript",
            "chat": "meeting_chat",
            "message": "meeting_chat",
            "participant": "participant_state",
            "participant_event": "participant_state",
            "meeting": "meeting_state",
            "status": "meeting_state",
            "note": "system_note",
        }
        input_type = aliases.get(input_type_raw, input_type_raw or "system_note")
        if input_type not in {
            "spoken_transcript",
            "meeting_chat",
            "bot_reply",
            "participant_state",
            "meeting_state",
            "system_note",
        }:
            raise ValueError(f"Unsupported meeting input type: {input_type}")
        normalized = {
            "input_type": input_type,
            "speaker": str(item.get("speaker") or metadata.get("speaker") or "").strip() or None,
            "text": str(item.get("text") or "").strip(),
            "source": str(item.get("source") or "meeting_input").strip() or "meeting_input",
            "direct_question": bool(item.get("direct_question", False)),
            "created_at": str(item.get("created_at") or metadata.get("captured_at") or "").strip() or None,
            "metadata": metadata,
        }
        if input_type == "bot_reply":
            normalized["speaker"] = normalized["speaker"] or str(item.get("bot_display_name") or "").strip() or None
        return normalized

    def _contains_bot_mention(self, bot_display_name: str, text: str) -> bool:
        normalized_text = str(text or "").strip().lower()
        if not normalized_text:
            return False
        candidates = {
            bot_display_name.strip().lower(),
            bot_display_name.replace(" ", "").strip().lower(),
            bot_display_name.replace("_", " ").strip().lower(),
            bot_display_name.replace("_", "").strip().lower(),
        }
        for candidate in candidates:
            if candidate and candidate in normalized_text:
                return True
            if candidate and f"@{candidate}" in normalized_text:
                return True
        return False

    def _build_reply_request_text(self, session: DelegateSession, turn: ChatTurn) -> str:
        return (
            f"Meeting topic: {session.meeting_topic or 'Unknown'}\n"
            f"Delegate name: {session.bot_display_name}\n"
            f"Current participant message from {turn.speaker}: {turn.text}\n"
        )

    def _reply_key_for_turn(self, turn: ChatTurn, payload: dict[str, Any]) -> str:
        metadata = dict(payload.get("metadata") or {})
        candidate = (
            metadata.get("id")
            or metadata.get("message_id")
            or metadata.get("timestamp")
            or metadata.get("created_at")
            or turn.turn_id
        )
        return str(candidate or turn.turn_id).strip() or turn.turn_id

    def _should_ignore_chat_input(self, session: DelegateSession, payload: dict[str, Any]) -> bool:
        source = str(payload.get("source") or "meeting_chat").strip().lower()
        if source != "zoom_chat_message":
            return False
        message_key = self._zoom_chat_message_key(payload)
        if not message_key:
            return False
        seen_keys = {
            str(item).strip()
            for item in list(session.ai_state.get("seen_zoom_chat_message_keys") or [])
            if str(item).strip()
        }
        if message_key in seen_keys:
            return True
        for other in self._store.list_sessions():
            if other.session_id == session.session_id:
                continue
            if not self._same_meeting_identity(session, other):
                continue
            for item in other.input_timeline:
                if item.input_type != "meeting_chat" or str(item.source or "").strip().lower() != "zoom_chat_message":
                    continue
                other_key = self._zoom_chat_message_key(
                    {
                        "speaker": item.speaker,
                        "text": item.text,
                        "metadata": dict(item.metadata or {}),
                    }
                )
                if other_key and other_key == message_key:
                    return True
        return False

    def _remember_chat_input(self, session: DelegateSession, payload: dict[str, Any]) -> None:
        source = str(payload.get("source") or "meeting_chat").strip().lower()
        if source != "zoom_chat_message":
            return
        message_key = self._zoom_chat_message_key(payload)
        if not message_key:
            return
        seen_keys = [
            str(item).strip()
            for item in list(session.ai_state.get("seen_zoom_chat_message_keys") or [])
            if str(item).strip()
        ]
        if message_key in seen_keys:
            return
        seen_keys.append(message_key)
        session.ai_state["seen_zoom_chat_message_keys"] = seen_keys[-256:]

    def _zoom_chat_message_key(self, payload: dict[str, Any]) -> str | None:
        metadata = dict(payload.get("metadata") or {})
        message_id = str(
            metadata.get("id")
            or metadata.get("msgId")
            or metadata.get("messageId")
            or ""
        ).strip()
        if message_id:
            return f"id:{message_id}"
        timestamp = str(metadata.get("timestamp") or "").strip()
        if not timestamp:
            return None
        speaker = self._normalize_transcript_text(str(payload.get("speaker") or "")).lower()
        text = self._normalize_transcript_text(str(payload.get("text") or "")).lower()
        if not text:
            return None
        return f"fallback:{timestamp}:{speaker}:{text}"

    def _same_meeting_identity(self, left: DelegateSession, right: DelegateSession) -> bool:
        if left.meeting_number and right.meeting_number and left.meeting_number == right.meeting_number:
            return True
        if left.join_url and right.join_url and left.join_url == right.join_url:
            return True
        return False

