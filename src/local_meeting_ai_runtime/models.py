"""Data models for a meeting delegate runtime."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

DelegateMode = Literal["listen_only", "answer_on_ask", "approval_required"]
SessionStatus = Literal["planned", "joining", "active", "suspected_ended", "blocked", "completed"]
ApprovalStatus = Literal["pending", "approved", "rejected"]
ChatRole = Literal["participant", "bot", "system"]
MeetingInputType = Literal[
    "spoken_transcript",
    "meeting_chat",
    "bot_reply",
    "participant_state",
    "meeting_state",
    "system_note",
]
WorkspaceEventDirection = Literal["inbound", "outbound", "hook", "system"]
RunnerJobType = Literal["workspace_event", "transcript_chunk", "finalize_session"]
RunnerJobStatus = Literal["queued", "leased", "completed", "failed"]
WorkspaceEventStatus = Literal[
    "queued",
    "processed",
    "ignored",
    "responded",
    "pending_approval",
    "failed",
    "sent",
    "delivery_failed",
]

_LEGACY_REQUEST_TEXT_KEY = "prompt"


def utcnow_iso() -> str:
    return datetime.now(UTC).isoformat()


def _normalize_legacy_request_text(value: Any) -> str:
    text = str(value or "")
    if not text:
        return ""
    normalized = text.replace(
        "Latest direct message from ",
        "Current participant message from ",
    )
    normalized = normalized.replace(
        "Draft one concise reply for the delegate to say in the meeting. Stay grounded in the visible meeting context.",
        "Task: answer the participant's current message using only this meeting context.",
    )
    return normalized


@dataclass(slots=True)
class TranscriptChunk:
    speaker: str
    text: str
    created_at: str = field(default_factory=utcnow_iso)
    source: str = "manual"
    direct_question: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ApprovalRequest:
    approval_id: str
    request_text: str
    draft: str
    speaker: str | None = None
    source: str = "direct_question"
    status: ApprovalStatus = "pending"
    decision_note: str | None = None
    created_at: str = field(default_factory=utcnow_iso)
    decided_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ChatTurn:
    turn_id: str
    role: ChatRole
    speaker: str
    text: str
    source: str = "meeting_chat"
    status: str = "sent"
    created_at: str = field(default_factory=utcnow_iso)
    publish_requested: bool = False
    delivery: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class MeetingInput:
    input_id: str
    input_type: MeetingInputType
    speaker: str | None = None
    text: str | None = None
    source: str = "meeting_input"
    direct_question: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utcnow_iso)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class WorkspaceEvent:
    event_id: str
    event_type: str
    direction: WorkspaceEventDirection = "inbound"
    status: WorkspaceEventStatus = "queued"
    source: str = "workspace"
    speaker: str | None = None
    text: str | None = None
    channel_id: str | None = None
    meeting_id: str | None = None
    response_expected: bool = False
    publish_requested: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utcnow_iso)
    processed_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class RunnerJob:
    job_id: str
    job_type: RunnerJobType
    session_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    status: RunnerJobStatus = "queued"
    attempts: int = 0
    lease_owner: str | None = None
    created_at: str = field(default_factory=utcnow_iso)
    leased_at: str | None = None
    completed_at: str | None = None
    last_error: str | None = None
    result: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class DelegateSession:
    session_id: str
    delegate_mode: DelegateMode
    bot_display_name: str
    meeting_id: str | None = None
    meeting_uuid: str | None = None
    meeting_topic: str | None = None
    join_url: str | None = None
    meeting_number: str | None = None
    passcode: str | None = None
    requested_by: str | None = None
    instructions: str | None = None
    status: SessionStatus = "planned"
    status_reason: str | None = None
    preflight: dict[str, Any] = field(default_factory=dict)
    join_ticket: dict[str, Any] = field(default_factory=dict)
    transcript: list[TranscriptChunk] = field(default_factory=list)
    approvals: list[ApprovalRequest] = field(default_factory=list)
    summary: str | None = None
    action_items: list[str] = field(default_factory=list)
    draft_replies: list[dict[str, Any]] = field(default_factory=list)
    input_timeline: list[MeetingInput] = field(default_factory=list)
    chat_history: list[ChatTurn] = field(default_factory=list)
    workspace_events: list[WorkspaceEvent] = field(default_factory=list)
    runner_state: dict[str, Any] = field(default_factory=dict)
    ai_state: dict[str, Any] = field(default_factory=dict)
    summary_packet: dict[str, Any] = field(default_factory=dict)
    artifact_handoffs: list[dict[str, Any]] = field(default_factory=list)
    transcript_exports: list[dict[str, Any]] = field(default_factory=list)
    summary_exports: list[dict[str, Any]] = field(default_factory=list)
    created_at: str = field(default_factory=utcnow_iso)
    updated_at: str = field(default_factory=utcnow_iso)

    def touch(self) -> None:
        self.updated_at = utcnow_iso()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def session_from_dict(raw: dict[str, Any]) -> DelegateSession:
    transcript = [TranscriptChunk(**item) for item in raw.get("transcript", [])]
    approvals_raw = []
    for item in raw.get("approvals", []):
        normalized = dict(item)
        if "request_text" not in normalized:
            normalized["request_text"] = _normalize_legacy_request_text(
                normalized.pop(_LEGACY_REQUEST_TEXT_KEY, "")
            )
        else:
            normalized.pop(_LEGACY_REQUEST_TEXT_KEY, None)
        normalized["request_text"] = _normalize_legacy_request_text(normalized.get("request_text"))
        approvals_raw.append(normalized)
    approvals = [ApprovalRequest(**item) for item in approvals_raw]
    input_timeline = [MeetingInput(**item) for item in raw.get("input_timeline", [])]

    chat_history_raw = []
    for item in raw.get("chat_history", []):
        normalized = dict(item)
        if normalized.get("status") == "local_only":
            normalized["status"] = "sent"
        chat_history_raw.append(normalized)
    chat_history = [ChatTurn(**item) for item in chat_history_raw]

    workspace_events_raw = []
    for item in raw.get("workspace_events", []):
        normalized = dict(item)
        metadata = dict(normalized.get("metadata") or {})
        if normalized.get("status") == "local_only":
            normalized["status"] = "sent"
        if metadata.get("reply_status") == "local_only":
            metadata["reply_status"] = "sent"
        normalized["metadata"] = metadata
        workspace_events_raw.append(normalized)
    workspace_events = [WorkspaceEvent(**item) for item in workspace_events_raw]
    payload = dict(raw)
    legacy_handoffs = list(payload.pop("report_deliveries", []) or [])
    payload.pop("telegram_chat_id", None)
    payload.pop("telegram_message_thread_id", None)
    payload.pop("report_channel_id", None)
    payload.pop("approval_channel_id", None)
    payload.pop("rtms_ticket", None)
    payload.pop("rtms_events", None)
    payload["transcript"] = transcript
    payload["approvals"] = approvals
    payload["input_timeline"] = input_timeline
    payload["chat_history"] = chat_history
    payload["workspace_events"] = workspace_events
    payload["draft_replies"] = [
        {
            **{
                key: value
                for key, value in dict(item).items()
                if key != _LEGACY_REQUEST_TEXT_KEY
            },
            "request_text": _normalize_legacy_request_text(
                dict(item).get("request_text")
                or dict(item).get(_LEGACY_REQUEST_TEXT_KEY)
                or "",
            ),
            "status": "sent" if dict(item).get("status") == "local_only" else dict(item).get("status"),
        }
        for item in payload.get("draft_replies", [])
    ]
    if "artifact_handoffs" not in payload:
        payload["artifact_handoffs"] = legacy_handoffs
    join_ticket = dict(payload.get("join_ticket") or {})
    join_ticket.pop("recording_sync", None)
    join_ticket.pop("last_zoom_webhook", None)
    payload["join_ticket"] = join_ticket
    runner_state = dict(payload.get("runner_state") or {})
    runner_state.pop("hook_event_count", None)
    runner_state.pop("last_hook_event_at", None)
    runner_state.pop("last_hook_event_type", None)
    payload["runner_state"] = runner_state
    return DelegateSession(**payload)
