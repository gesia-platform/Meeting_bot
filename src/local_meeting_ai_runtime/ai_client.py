"""AI summarization client for the delegate runtime."""

from __future__ import annotations

import importlib.util
import json
import locale
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any
import wave
import gc

import httpx

from .assets import find_whisper_cpp_cli, find_whisper_cpp_model, find_whisper_cpp_vad_model
from .models import DelegateSession, TranscriptChunk


class AiDelegateClient:
    def __init__(self) -> None:
        self._prefer_codex = self._env_bool("DELEGATE_PREFER_CODEX", True)
        self._enable_codex_search = self._env_bool("DELEGATE_ENABLE_CODEX_SEARCH", True)
        self._codex_path = self._resolve_codex()
        self._codex_workdir = self._resolve_codex_workdir()
        self._api_key = (
            os.getenv("DELEGATE_AI_API_KEY", "").strip()
            or os.getenv("OPENAI_API_KEY", "").strip()
        )
        self._base_url = (
            os.getenv("DELEGATE_AI_BASE_URL", "").strip()
            or os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").strip()
        ).rstrip("/")
        self._summary_model = (
            os.getenv("DELEGATE_AI_MODEL", "").strip()
            or os.getenv("DELEGATE_OPENAI_MODEL", "gpt-5.4").strip()
        )
        self._transcribe_model = (
            os.getenv("DELEGATE_TRANSCRIBE_MODEL", "").strip()
            or os.getenv("DELEGATE_OPENAI_TRANSCRIBE_MODEL", "gpt-4o-transcribe").strip()
        )
        self._transcribe_language = os.getenv("DELEGATE_TRANSCRIBE_LANGUAGE", "ko").strip()
        self._reasoning_effort = (
            os.getenv("DELEGATE_AI_REASONING_EFFORT", "").strip()
            or os.getenv("DELEGATE_OPENAI_REASONING_EFFORT", "medium").strip()
            or "medium"
        )
        self._timeout = float(os.getenv("DELEGATE_AI_TIMEOUT_SECONDS", "45"))
        self._reply_timeout = float(os.getenv("DELEGATE_CODEX_REPLY_TIMEOUT_SECONDS", "300"))
        self._summary_timeout = float(os.getenv("DELEGATE_CODEX_SUMMARY_TIMEOUT_SECONDS", "600"))
        self._ffmpeg = self._resolve_ffmpeg()
        self._prefer_local_transcription = self._env_bool("DELEGATE_PREFER_LOCAL_TRANSCRIPTION", True)
        self._prefer_whisper_cpp = self._env_bool("DELEGATE_PREFER_WHISPER_CPP", True)
        self._local_whisper_model_name = os.getenv("DELEGATE_LOCAL_WHISPER_MODEL", "base").strip() or "base"
        self._local_whisper_language = os.getenv("DELEGATE_LOCAL_WHISPER_LANGUAGE", self._transcribe_language).strip()
        self._whisper_cpp_path = self._resolve_whisper_cpp()
        self._whisper_cpp_model_path = self._resolve_whisper_cpp_model()
        self._whisper_cpp_threads = max(
            self._env_int("DELEGATE_WHISPER_CPP_THREADS", max(2, min(os.cpu_count() or 4, 8))),
            1,
        )
        self._whisper_cpp_best_of = max(self._env_int("DELEGATE_WHISPER_CPP_BEST_OF", 5), 1)
        self._whisper_cpp_beam_size = max(self._env_int("DELEGATE_WHISPER_CPP_BEAM_SIZE", 5), 1)
        self._whisper_cpp_temperature = min(
            max(self._env_float("DELEGATE_WHISPER_CPP_TEMPERATURE", 0.0), 0.0),
            1.0,
        )
        self._whisper_cpp_split_on_word = self._env_bool("DELEGATE_WHISPER_CPP_SPLIT_ON_WORD", True)
        self._whisper_cpp_suppress_nst = self._env_bool("DELEGATE_WHISPER_CPP_SUPPRESS_NST", True)
        self._whisper_cpp_no_fallback = self._env_bool("DELEGATE_WHISPER_CPP_NO_FALLBACK", True)
        self._whisper_cpp_enable_vad = self._env_bool("DELEGATE_WHISPER_CPP_ENABLE_VAD", True)
        self._whisper_cpp_vad_model_path = self._resolve_whisper_cpp_vad_model()
        self._whisper_cpp_enable_tinydiarize = self._env_bool(
            "DELEGATE_WHISPER_CPP_ENABLE_TINYDIARIZE",
            True,
        )
        self._whisper_cpp_enable_stereo_diarize = self._env_bool(
            "DELEGATE_WHISPER_CPP_ENABLE_STEREO_DIARIZE",
            True,
        )
        self._local_whisper_available = importlib.util.find_spec("whisper") is not None
        self._local_whisper_word_timestamps = self._env_bool(
            "DELEGATE_LOCAL_WHISPER_WORD_TIMESTAMPS",
            True,
        )
        self._local_whisper_best_of = max(self._env_int("DELEGATE_LOCAL_WHISPER_BEST_OF", 5), 1)
        self._local_whisper_beam_size = max(self._env_int("DELEGATE_LOCAL_WHISPER_BEAM_SIZE", 5), 1)
        self._recording_transcription_providers = self._resolve_recording_transcription_providers()
        self._final_transcribe_model_name = (
            os.getenv("DELEGATE_FINAL_TRANSCRIBE_MODEL", "").strip()
            or os.getenv("DELEGATE_FASTER_WHISPER_MODEL", "").strip()
            or "large-v3"
        )
        self._final_transcribe_language = (
            os.getenv("DELEGATE_FINAL_TRANSCRIBE_LANGUAGE", "").strip()
            or self._transcribe_language
            or "ko"
        )
        self._faster_whisper_compute_type = (
            os.getenv("DELEGATE_FASTER_WHISPER_COMPUTE_TYPE", "").strip()
            or "float16"
        )
        self._faster_whisper_fallback_compute_type = (
            os.getenv("DELEGATE_FASTER_WHISPER_FALLBACK_COMPUTE_TYPE", "").strip()
            or "int8_float16"
        )
        self._faster_whisper_compute_types = self._resolve_faster_whisper_compute_types()
        if self._faster_whisper_compute_types:
            self._faster_whisper_compute_type = self._faster_whisper_compute_types[0]
            self._faster_whisper_fallback_compute_type = (
                self._faster_whisper_compute_types[1] if len(self._faster_whisper_compute_types) > 1 else ""
            )
        self._faster_whisper_cpu_compute_types = self._resolve_faster_whisper_cpu_compute_types()
        self._faster_whisper_beam_size = max(self._env_int("DELEGATE_FASTER_WHISPER_BEAM_SIZE", 5), 1)
        self._faster_whisper_vad_filter = self._env_bool("DELEGATE_FASTER_WHISPER_VAD_FILTER", True)
        self._pyannote_model_name = (
            os.getenv("DELEGATE_PYANNOTE_MODEL", "").strip()
            or "pyannote/speaker-diarization-community-1"
        )
        self._huggingface_token = (
            os.getenv("DELEGATE_HUGGINGFACE_TOKEN", "").strip()
            or os.getenv("HUGGINGFACE_TOKEN", "").strip()
            or os.getenv("HF_TOKEN", "").strip()
            or os.getenv("HUGGING_FACE_HUB_TOKEN", "").strip()
        )
        self._whisper_module: Any | None = None
        self._whisper_model: Any | None = None
        self._faster_whisper_models: dict[str, Any] = {}
        self._pyannote_pipeline: Any | None = None

    async def summarize_session(self, session: DelegateSession) -> dict[str, Any]:
        if not self._codex_ready:
            raise RuntimeError("The local Codex body is unavailable for meeting summarization.")
        return self._codex_summarize(session)

    async def draft_reply(self, session: DelegateSession, request_text: str) -> dict[str, Any]:
        cleaned_request_text = str(request_text or "").strip()
        if not cleaned_request_text:
            raise ValueError("A live meeting request is required.")
        if not self._codex_ready:
            raise RuntimeError("The local Codex body is unavailable for live meeting replies.")
        return self._codex_draft_reply(session, cleaned_request_text)

    @property
    def can_transcribe_audio(self) -> bool:
        return any(self._recording_provider_ready(provider) for provider in self._recording_transcription_providers)

    def recording_transcription_strategy(self) -> dict[str, Any]:
        provider_order = list(self._recording_transcription_providers)
        ready_providers = [provider for provider in provider_order if self._recording_provider_ready(provider)]
        blocking_reasons: list[str] = []
        for provider in provider_order:
            if self._recording_provider_ready(provider):
                continue
            if provider == "whisper_cpp":
                blocking_reasons.append("whisper.cpp runtime assets are not ready for local capture transcription.")
            elif provider == "local_whisper":
                blocking_reasons.append("local whisper is not installed for local capture transcription.")
            elif provider == "openai":
                blocking_reasons.append("OpenAI API key is not configured for API capture transcription fallback.")
        return {
            "provider_order": provider_order,
            "ready_providers": ready_providers,
            "can_transcribe": bool(ready_providers),
            "prefer_local_transcription": bool(self._prefer_local_transcription),
            "prefer_whisper_cpp": bool(self._prefer_whisper_cpp),
            "blocking_reasons": blocking_reasons,
        }

    def quality_runtime_cache_state(self) -> dict[str, Any]:
        return {
            "faster_whisper_compute_types": list(self._faster_whisper_models.keys()),
            "faster_whisper_model_count": len(self._faster_whisper_models),
            "pyannote_pipeline_loaded": self._pyannote_pipeline is not None,
        }

    def release_quality_runtime_resources(self) -> dict[str, Any]:
        released_compute_types = list(self._faster_whisper_models.keys())
        released_pyannote = self._pyannote_pipeline is not None
        self._faster_whisper_models.clear()
        self._pyannote_pipeline = None
        gc.collect()

        cuda_cache_cleared = False
        if importlib.util.find_spec("torch") is not None:
            try:
                import torch  # type: ignore[import-not-found]

                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
                    cuda_cache_cleared = True
            except Exception:
                cuda_cache_cleared = False
        return {
            "released_compute_types": released_compute_types,
            "released_model_count": len(released_compute_types),
            "released_pyannote_pipeline": released_pyannote,
            "cuda_cache_cleared": cuda_cache_cleared,
        }

    def quality_readiness(self) -> dict[str, Any]:
        blocking_reasons: list[str] = []
        backend = self._quality_backend(final_pass=True)
        gpu_ready = bool(backend.get("gpu_ready"))
        faster_whisper_ready = self._faster_whisper_ready
        pyannote_ready = self._pyannote_ready
        if not backend.get("ready"):
            blocking_reasons.append("CUDA GPU is not available for the final transcription quality pass.")
        if not faster_whisper_ready:
            blocking_reasons.append("faster-whisper is not installed for the final transcription quality pass.")
        if not pyannote_ready:
            blocking_reasons.append("pyannote.audio or Hugging Face token is not ready for diarization.")
        return {
            "gpu_ready": gpu_ready,
            "backend_device": str(backend.get("device") or ""),
            "cpu_final_fallback_allowed": bool(backend.get("cpu_final_fallback_allowed")),
            "faster_whisper_ready": faster_whisper_ready,
            "pyannote_ready": pyannote_ready,
            "provider": str(backend.get("provider") or "faster_whisper_cuda"),
            "model": self._final_transcribe_model_name,
            "compute_types": list(backend.get("compute_types") or self._faster_whisper_compute_types),
            "diarization_provider": self._pyannote_model_name,
            "blocking_reasons": blocking_reasons,
        }

    def live_transcription_readiness(self) -> dict[str, Any]:
        blocking_reasons: list[str] = []
        backend = self._quality_backend(final_pass=False)
        gpu_ready = bool(backend.get("gpu_ready"))
        faster_whisper_ready = self._faster_whisper_ready
        if not backend.get("ready"):
            blocking_reasons.append("CUDA GPU is not available for live high-quality transcription.")
        if not faster_whisper_ready:
            blocking_reasons.append("faster-whisper is not installed for live high-quality transcription.")
        return {
            "gpu_ready": gpu_ready,
            "backend_device": str(backend.get("device") or ""),
            "faster_whisper_ready": faster_whisper_ready,
            "provider": str(backend.get("provider") or "faster_whisper_cuda"),
            "model": self._final_transcribe_model_name,
            "compute_types": list(backend.get("compute_types") or self._faster_whisper_compute_types),
            "blocking_reasons": blocking_reasons,
        }

    def transcribe_final_session_audio(
        self,
        *,
        microphone_path: str | Path | None,
        meeting_output_path: str | Path | None,
    ) -> dict[str, Any]:
        readiness = self.quality_readiness()
        if readiness["blocking_reasons"]:
            raise RuntimeError("Final transcription quality pass is blocked: " + " | ".join(readiness["blocking_reasons"]))

        chunks: list[TranscriptChunk] = []
        dropped_segment_count = 0
        provider = str(readiness.get("provider") or "faster_whisper_cuda")
        compute_types = list(readiness.get("compute_types") or self._faster_whisper_compute_types)
        used_compute_type = compute_types[0] if compute_types else self._faster_whisper_compute_type

        if microphone_path:
            microphone_result = self._transcribe_final_channel_with_faster_whisper(
                Path(microphone_path),
                fallback_speaker="local_user",
                source="microphone_final_offline",
                channel_origin="local_user",
                diarization_segments=None,
                transcription_provider=provider,
                compute_types=compute_types,
            )
            chunks.extend(microphone_result["chunks"])
            dropped_segment_count += int(microphone_result["dropped_segment_count"])
            used_compute_type = str(microphone_result.get("compute_type") or used_compute_type)

        if meeting_output_path:
            diarization_segments = self._run_pyannote_diarization(Path(meeting_output_path))
            meeting_output_result = self._transcribe_final_channel_with_faster_whisper(
                Path(meeting_output_path),
                fallback_speaker="remote_participant",
                source="meeting_output_final_offline",
                channel_origin="meeting_output",
                diarization_segments=diarization_segments,
                transcription_provider=provider,
                compute_types=compute_types,
            )
            chunks.extend(meeting_output_result["chunks"])
            dropped_segment_count += int(meeting_output_result["dropped_segment_count"])
            used_compute_type = str(meeting_output_result.get("compute_type") or used_compute_type)

        chunks.sort(
            key=lambda chunk: (
                float(dict(chunk.metadata or {}).get("session_start_offset_seconds") or 0.0),
                str(chunk.text or ""),
            )
        )
        return {
            "chunks": chunks,
            "provider": provider,
            "model": self._final_transcribe_model_name,
            "compute_type": used_compute_type,
            "diarization_provider": self._pyannote_model_name,
            "quality_pass": "final_offline",
            "dropped_segment_count": dropped_segment_count,
        }

    def transcribe_live_conversation_audio(
        self,
        *,
        microphone_path: str | Path | None,
        meeting_output_path: str | Path | None,
        microphone_start_offset_seconds: float | None = None,
        meeting_output_start_offset_seconds: float | None = None,
    ) -> dict[str, Any]:
        readiness = self.live_transcription_readiness()
        if readiness["blocking_reasons"]:
            raise RuntimeError(
                "Live high-quality transcription is blocked: "
                + " | ".join(readiness["blocking_reasons"])
            )

        chunks: list[TranscriptChunk] = []
        provider = str(readiness.get("provider") or "faster_whisper_cuda")
        compute_types = list(readiness.get("compute_types") or self._faster_whisper_compute_types)
        used_compute_type = compute_types[0] if compute_types else self._faster_whisper_compute_type

        if microphone_path:
            microphone_result = self._transcribe_channel_with_faster_whisper(
                Path(microphone_path),
                fallback_speaker="local_user",
                source=provider,
                channel_origin="local_user",
                diarization_segments=None,
                quality_pass="live_high_quality",
                session_offset_base_seconds=microphone_start_offset_seconds,
                transcription_provider=provider,
                compute_types=compute_types,
            )
            chunks.extend(microphone_result["chunks"])
            used_compute_type = str(microphone_result.get("compute_type") or used_compute_type)

        if meeting_output_path:
            meeting_output_result = self._transcribe_channel_with_faster_whisper(
                Path(meeting_output_path),
                fallback_speaker="remote_participant",
                source=provider,
                channel_origin="meeting_output",
                diarization_segments=None,
                quality_pass="live_high_quality",
                session_offset_base_seconds=meeting_output_start_offset_seconds,
                transcription_provider=provider,
                compute_types=compute_types,
            )
            chunks.extend(meeting_output_result["chunks"])
            used_compute_type = str(meeting_output_result.get("compute_type") or used_compute_type)

        chunks.sort(
            key=lambda chunk: (
                float(dict(chunk.metadata or {}).get("session_start_offset_seconds") or 0.0),
                str(chunk.text or ""),
            )
        )
        return {
            "chunks": chunks,
            "provider": provider,
            "model": self._final_transcribe_model_name,
            "compute_type": used_compute_type,
            "quality_pass": "live_high_quality",
        }

    async def transcribe_recording_bytes(
        self,
        recording_bytes: bytes,
        *,
        filename: str,
    ) -> list[TranscriptChunk]:
        with tempfile.TemporaryDirectory(prefix="delegate-audio-") as temp_dir:
            temp_path = Path(temp_dir)
            raw_suffix = Path(filename).suffix or ".mp4"
            raw_path = temp_path / f"recording{raw_suffix}"
            raw_path.write_bytes(recording_bytes)
            return await self._transcribe_recording_path(raw_path)

    async def transcribe_recording_path(
        self,
        recording_path: str | Path,
        *,
        filename: str | None = None,
    ) -> list[TranscriptChunk]:
        raw_path = Path(recording_path)
        if not raw_path.exists():
            label = str(filename or raw_path.name or raw_path)
            raise FileNotFoundError(f"Audio recording file was not found for transcription: {label}")
        return await self._transcribe_recording_path(raw_path)

    async def _transcribe_recording_path(self, raw_path: Path) -> list[TranscriptChunk]:
        prepared_path = self._prepare_transcription_input(raw_path)
        last_error: Exception | None = None
        attempted_providers: list[str] = []
        for provider in self._recording_transcription_providers:
            if not self._recording_provider_ready(provider):
                continue
            attempted_providers.append(provider)
            try:
                return await self._transcribe_recording_with_provider(provider, prepared_path)
            except Exception as exc:
                last_error = exc
        if last_error is not None:
            raise last_error
        if attempted_providers:
            raise RuntimeError("Configured transcription providers could not transcribe this recording.")
        raise RuntimeError("No compatible local or API transcription provider is configured for the active provider order.")

    def _codex_summarize(self, session: DelegateSession) -> dict[str, Any]:
        request_text = (
            "Task: summarize the meeting session payload below into structured JSON.\n"
            "Write the result as meeting notes, not as a separate bot self-description.\n\n"
            "Required JSON keys:\n"
            "- `title`: short Korean meeting agenda headline.\n"
            "  It must name the actual meeting topic in a compact noun phrase, not a full sentence.\n"
            "  Good examples: `Zoom 봇 결과물 품질 개선 논의`, `회의 컨텍스트 자동 반영 검토`, `범용 Zoom Meeting Bot 패키징 논의`.\n"
            "  Bad examples: `회의 요약`, `Zoom 회의`, `오늘은 이런 이야기를 나눴다`, `범위와 우선순위 정리`.\n"
            "- `summary`: concise but high-signal Korean meeting summary.\n"
            "- `action_items`: array of short concrete follow-ups.\n"
            "- `decisions`: array of concrete decisions that were actually made.\n"
            "- `open_questions`: array of unresolved questions that remain open.\n"
            "- `risk_signals`: array of concrete blockers, risks, or concerns.\n"
            "- `sections`: array of 3 to 6 topic blocks. Each item must contain `heading`, `summary`, and `timestamp_refs`.\n"
            "- `timestamp_refs`: short clock labels like `00:42.50` or `01:12:05.00` that help locate the evidence.\n\n"
            f"Session payload:\n{self._session_context_json(session, recent_only=False)}"
        )
        parsed = self._codex_json_response(
            session,
            request_text,
            schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "title": {"type": "string"},
                    "summary": {"type": "string"},
                    "action_items": {"type": "array", "items": {"type": "string"}},
                    "decisions": {"type": "array", "items": {"type": "string"}},
                    "open_questions": {"type": "array", "items": {"type": "string"}},
                    "risk_signals": {"type": "array", "items": {"type": "string"}},
                    "sections": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "heading": {"type": "string"},
                                "summary": {"type": "string"},
                                "timestamp_refs": {"type": "array", "items": {"type": "string"}},
                            },
                            "required": ["heading", "summary", "timestamp_refs"],
                        },
                    },
                },
                "required": ["title", "summary", "action_items", "sections"],
            },
            timeout_seconds=max(self._timeout, self._summary_timeout),
        )
        title = str(parsed.get("title") or "").strip()
        action_items = parsed.get("action_items")
        if not isinstance(action_items, list):
            action_items = []
        decisions = parsed.get("decisions")
        if not isinstance(decisions, list):
            decisions = []
        open_questions = parsed.get("open_questions")
        if not isinstance(open_questions, list):
            open_questions = []
        risk_signals = parsed.get("risk_signals")
        if not isinstance(risk_signals, list):
            risk_signals = []
        sections = parsed.get("sections")
        if not isinstance(sections, list):
            sections = []
        summary = str(parsed.get("summary") or "").strip()
        if not summary:
            raise RuntimeError("The local Codex body returned an empty meeting summary.")
        return {
            "title": title,
            "summary": summary,
            "action_items": [str(item).strip() for item in action_items if str(item).strip()],
            "decisions": [str(item).strip() for item in decisions if str(item).strip()],
            "open_questions": [str(item).strip() for item in open_questions if str(item).strip()],
            "risk_signals": [str(item).strip() for item in risk_signals if str(item).strip()],
            "sections": [
                {
                    "heading": str(item.get("heading") or "").strip(),
                    "summary": str(item.get("summary") or "").strip(),
                    "timestamp_refs": [
                        str(ref).strip()
                        for ref in list(item.get("timestamp_refs") or [])
                        if str(ref).strip()
                    ],
                }
                for item in sections
                if isinstance(item, dict)
                and str(item.get("heading") or "").strip()
                and str(item.get("summary") or "").strip()
            ],
            "provider": "codex_exec",
        }

    def _codex_draft_reply(self, session: DelegateSession, request_text: str) -> dict[str, Any]:
        request_payload = (
            "Task: answer the participant's current meeting message as the same local AI body present in this meeting.\n"
            "Keep the reply grounded in the session memory and assistant presence facts below.\n"
            "Use natural Korean.\n"
            "Do not mention internal mode names, payload keys, runtime field names, or system flags unless the participant explicitly asks for technical debugging.\n"
            "Required JSON keys: `draft` and `confidence_note`.\n\n"
            f"Session payload:\n{self._reply_context_json(session)}\n\n"
            f"Current participant request:\n{request_text}"
        )
        parsed = self._codex_json_response(
            session,
            request_payload,
            schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "draft": {"type": "string"},
                    "confidence_note": {"type": "string"},
                },
                "required": ["draft", "confidence_note"],
            },
            timeout_seconds=max(self._timeout, self._reply_timeout),
        )
        draft = str(parsed.get("draft") or "").strip()
        if not draft:
            raise RuntimeError("The local Codex body returned an empty meeting reply.")
        return {
            "request_text": request_text,
            "draft": draft,
            "confidence_note": str(parsed.get("confidence_note") or "").strip(),
            "provider": "codex_exec",
        }

    async def _openai_transcribe(self, input_path: Path) -> dict[str, Any] | str:
        data: dict[str, Any] = {
            "model": self._transcribe_model,
            "response_format": "json",
        }
        if self._transcribe_language:
            data["language"] = self._transcribe_language

        headers = {"Authorization": f"Bearer {self._api_key}"}
        with input_path.open("rb") as handle:
            files = {"file": (input_path.name, handle, self._mime_type_for_suffix(input_path.suffix))}
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(
                    f"{self._base_url}/audio/transcriptions",
                    data=data,
                    files=files,
                    headers=headers,
                )
                if response.is_error:
                    raise RuntimeError(f"OpenAI transcription request failed: {response.text}")
                content_type = response.headers.get("content-type", "").lower()
                if "application/json" in content_type:
                    return response.json()
                return response.text

    def _extract_openai_text(self, payload: dict[str, Any]) -> str:
        output_text = str(payload.get("output_text") or "").strip()
        if output_text:
            return output_text

        chunks: list[str] = []
        for item in payload.get("output", []) or []:
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            for content in item.get("content", []) or []:
                if not isinstance(content, dict):
                    continue
                if content.get("type") in {"output_text", "text"}:
                    text = str(content.get("text") or "").strip()
                    if text:
                        chunks.append(text)
        return "\n".join(chunks).strip()

    def _parse_json_text(self, text: str) -> dict[str, Any]:
        cleaned = (text or "").strip()
        if not cleaned:
            return {}
        try:
            parsed = json.loads(cleaned)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            pass

        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start >= 0 and end > start:
            try:
                parsed = json.loads(cleaned[start : end + 1])
                return parsed if isinstance(parsed, dict) else {}
            except json.JSONDecodeError:
                return {}
        return {}

    def _session_context_json(self, session: DelegateSession, *, recent_only: bool) -> str:
        transcript = session.transcript[-10:] if recent_only else session.transcript
        chat_history = session.chat_history[-10:] if recent_only else session.chat_history
        payload = {
            "meeting": {
                "topic": session.meeting_topic,
                "delegate_mode": session.delegate_mode,
                "bot_display_name": session.bot_display_name,
                "status": session.status,
            },
            "summary_packet": session.summary_packet,
            "recent_transcript": [chunk.to_dict() for chunk in transcript],
            "recent_chat_history": [turn.to_dict() for turn in chat_history],
            "instructions": session.instructions,
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)

    def _reply_context_json(self, session: DelegateSession) -> str:
        audio_observer_state = dict(session.ai_state.get("audio_observer") or {})
        final_transcription_state = dict(session.ai_state.get("final_transcription") or {})
        transcript = [chunk.to_dict() for chunk in session.transcript]
        chat_history = [turn.to_dict() for turn in session.chat_history]
        payload = {
            "meeting": {
                "topic": session.meeting_topic,
                "bot_display_name": session.bot_display_name,
                "status": session.status,
            },
            "assistant_presence": {
                "role_description": self._assistant_role_description(session),
                "collection_scope": self._assistant_collection_scope(session),
                "current_status": self._assistant_current_status(
                    session,
                    audio_observer_state=audio_observer_state,
                    final_transcription_state=final_transcription_state,
                ),
            },
            "meeting_memory": {
                "transcript": transcript,
                "chat_history": chat_history,
            },
            "instructions": session.instructions,
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)

    def _assistant_role_description(self, session: DelegateSession) -> str:
        topic = str(session.meeting_topic or "이 회의").strip() or "이 회의"
        return (
            f"저는 {topic}의 대화와 맥락을 함께 따라가면서, 필요하실 때 질문에 답하고 "
            "회의가 끝나면 정리까지 돕는 존재입니다. 쉽게 말하면 지금 여기서 대화하고 있는 제가 회의 안에 들어와 있는 상태입니다."
        )

    def _assistant_collection_scope(self, session: DelegateSession) -> str:
        collected_inputs: list[str] = []
        if session.chat_history:
            collected_inputs.append("채팅")
        if session.transcript or dict(session.ai_state.get("audio_observer") or {}).get("running"):
            collected_inputs.append("음성")
        if not collected_inputs:
            return "아직 뚜렷하게 쌓인 회의 입력은 많지 않지만, 이 회의를 이해하기 위한 채팅과 음성 흐름을 함께 따라가고 있습니다."
        joined = "와 ".join(collected_inputs) if len(collected_inputs) == 2 else collected_inputs[0]
        return f"{joined} 모두 이 회의를 이해하고 답하기 위한 입력으로 함께 반영되고 있습니다."

    def _assistant_current_status(
        self,
        session: DelegateSession,
        *,
        audio_observer_state: dict[str, Any],
        final_transcription_state: dict[str, Any],
    ) -> str:
        transcript_count = len(session.transcript)
        chat_count = len(session.chat_history)
        audio_running = bool(audio_observer_state.get("running"))
        final_status = str(final_transcription_state.get("status") or "").strip().lower()
        if final_status == "success":
            return (
                f"지금까지 이 세션에는 채팅 {chat_count}건과 음성 전사 {transcript_count}개가 쌓여 있고, "
                "회의 전체 음성도 정리된 상태입니다."
            )
        if audio_running:
            return (
                f"지금은 회의 채팅 {chat_count}건과 음성 전사 {transcript_count}개를 바탕으로 "
                "회의 흐름을 계속 따라가고 있습니다."
            )
        return (
            f"현재는 채팅 {chat_count}건과 음성 전사 {transcript_count}개가 이 세션에 남아 있고, "
            "그 기록을 바탕으로 대화를 이어가고 있습니다."
        )

    def transcribe_audio_path_high_quality(
        self,
        *,
        input_path: str | Path,
        fallback_speaker: str,
        source: str,
        channel_origin: str,
        session_offset_base_seconds: float | None,
        quality_pass: str,
        enable_diarization: bool = False,
    ) -> dict[str, Any]:
        is_final_quality_pass = str(quality_pass or "").strip().startswith("final_offline")
        readiness = self.quality_readiness() if is_final_quality_pass else self.live_transcription_readiness()
        if readiness["blocking_reasons"]:
            raise RuntimeError(" | ".join(str(item) for item in readiness["blocking_reasons"]))
        diarization_segments: list[dict[str, Any]] | None = None
        if enable_diarization and self._pyannote_ready:
            diarization_segments = self._run_pyannote_diarization(Path(input_path))
        provider = str(readiness.get("provider") or "faster_whisper_cuda")
        compute_types = list(readiness.get("compute_types") or self._faster_whisper_compute_types)
        result = self._transcribe_channel_with_faster_whisper(
            Path(input_path),
            fallback_speaker=fallback_speaker,
            source=source,
            channel_origin=channel_origin,
            diarization_segments=diarization_segments,
            quality_pass=quality_pass,
            session_offset_base_seconds=session_offset_base_seconds,
            transcription_provider=provider,
            compute_types=compute_types,
        )
        return {
            "chunks": list(result.get("chunks") or []),
            "provider": provider,
            "model": self._final_transcribe_model_name,
            "compute_type": str(result.get("compute_type") or (compute_types[0] if compute_types else self._faster_whisper_compute_type)),
            "diarization_provider": self._pyannote_model_name if diarization_segments is not None else None,
            "quality_pass": quality_pass,
            "dropped_segment_count": int(result.get("dropped_segment_count") or 0),
        }

    def _codex_json_response(
        self,
        session: DelegateSession,
        request_text: str,
        *,
        schema: dict[str, Any] | None = None,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        if not self._codex_ready:
            raise RuntimeError("codex exec is not available.")
        thread_id = str(session.ai_state.get("codex_thread_id") or "").strip() or None
        with tempfile.NamedTemporaryFile(prefix="delegate-codex-", suffix=".txt", delete=False) as handle:
            output_path = Path(handle.name)
        schema_path: Path | None = None
        try:
            if schema is not None:
                with tempfile.NamedTemporaryFile(prefix="delegate-codex-schema-", suffix=".json", delete=False) as handle:
                    schema_path = Path(handle.name)
                schema_path.write_text(json.dumps(schema, ensure_ascii=False, indent=2), encoding="utf-8")
            attempts = [request_text]
            if schema is not None:
                attempts.append(
                    "Required output: valid JSON that matches the schema exactly. "
                    "Do not include markdown, prose, or any explanation.\n\n"
                    + request_text
                )
            last_error = "codex exec did not complete successfully."
            for attempt_request_text in attempts:
                command = self._build_codex_command(
                    output_path=output_path,
                    thread_id=thread_id,
                    schema_path=schema_path,
                )
                completed = subprocess.run(
                    command,
                    input=attempt_request_text,
                    capture_output=True,
                    text=True,
                    check=False,
                    cwd=str(self._codex_workdir),
                    timeout=timeout_seconds or self._timeout,
                )
                if completed.returncode != 0:
                    last_error = completed.stderr.strip() or completed.stdout.strip() or f"exit code {completed.returncode}"
                    continue
                session.ai_state.update(self._extract_codex_session_state(completed.stdout))
                if not output_path.exists():
                    last_error = "codex exec did not produce an output message."
                    continue
                output_text = output_path.read_text(encoding="utf-8").strip()
                parsed = self._parse_json_text(output_text)
                if parsed:
                    return parsed
                last_error = f"codex exec returned non-JSON output: {output_text[:300]}"
            raise RuntimeError(f"codex exec failed: {last_error}")
        finally:
            output_path.unlink(missing_ok=True)
            if schema_path is not None:
                schema_path.unlink(missing_ok=True)

    def _build_codex_command(
        self,
        *,
        output_path: Path,
        thread_id: str | None,
        schema_path: Path | None,
    ) -> list[str]:
        command = [str(self._codex_path), "-C", str(self._codex_workdir)]
        if self._enable_codex_search:
            command.append("--search")
        if thread_id:
            command.extend([
                "exec",
                "resume",
                thread_id,
                "--json",
                "--skip-git-repo-check",
                "--dangerously-bypass-approvals-and-sandbox",
                "--output-last-message",
                str(output_path),
            ])
            command.append("-")
            return command

        command.extend([
            "exec",
            "--json",
            "--skip-git-repo-check",
            "--dangerously-bypass-approvals-and-sandbox",
            "--output-last-message",
            str(output_path),
        ])
        if schema_path is not None:
            command.extend(["--output-schema", str(schema_path)])
        command.append("-")
        return command

    def _extract_codex_session_state(self, stdout_text: str) -> dict[str, Any]:
        state: dict[str, Any] = {}
        for raw_line in (stdout_text or "").splitlines():
            line = raw_line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "thread.started":
                thread_id = str(event.get("thread_id") or "").strip()
                if thread_id:
                    state["codex_thread_id"] = thread_id
                    state["codex_thread_active"] = True
        return state

    def prime_session_thread(self, session: DelegateSession) -> dict[str, Any]:
        if not self._codex_ready:
            return {"status": "skipped", "reason": "codex_unavailable"}
        if str(session.ai_state.get("codex_thread_id") or "").strip():
            return {"status": "ready", "thread_id": session.ai_state.get("codex_thread_id")}
        parsed = self._codex_json_response(
            session,
            (
                "Task: confirm that the meeting session thread is ready to accept structured requests.\n"
                "Required JSON keys: `ready` and `note`.\n"
                "Set `ready` to true and `note` to `ok`."
            ),
            schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "ready": {"type": "boolean"},
                    "note": {"type": "string"},
                },
                "required": ["ready", "note"],
            },
            timeout_seconds=30,
        )
        return {
            "status": "ready" if parsed.get("ready") else "skipped",
            "thread_id": session.ai_state.get("codex_thread_id"),
            "note": str(parsed.get("note") or "").strip(),
        }

    def _resolve_codex(self) -> str | None:
        if not self._prefer_codex:
            return None
        configured = os.getenv("DELEGATE_CODEX_PATH", "").strip()
        if configured and Path(configured).exists():
            return configured
        discovered = shutil.which("codex")
        if discovered:
            return discovered
        fallback = Path(os.path.expandvars(r"%LOCALAPPDATA%\Programs\codex\codex.exe"))
        if fallback.exists():
            return str(fallback)
        return None

    def _resolve_codex_workdir(self) -> Path:
        configured = os.getenv("DELEGATE_CODEX_WORKDIR", "").strip()
        if configured:
            return Path(configured).resolve()
        return Path.cwd().resolve()

    @property
    def _codex_ready(self) -> bool:
        return bool(self._codex_path and self._prefer_codex)

    def _prepare_transcription_input(self, raw_path: Path) -> Path:
        if raw_path.suffix.lower() in {".wav", ".mp3", ".ogg", ".flac"}:
            return raw_path
        if not self._ffmpeg:
            return raw_path

        output_path = raw_path.with_suffix(".wav")
        completed = subprocess.run(
            [
                self._ffmpeg,
                "-y",
                "-i",
                str(raw_path),
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                str(output_path),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0 or not output_path.exists():
            details = completed.stderr.strip() or completed.stdout.strip() or f"exit code {completed.returncode}"
            raise RuntimeError(f"ffmpeg audio extraction failed: {details}")
        return output_path

    def _chunks_from_transcription_payload(self, payload: dict[str, Any] | str) -> list[TranscriptChunk]:
        if isinstance(payload, str):
            return []
        segments = payload.get("segments")
        if not isinstance(segments, list):
            return []

        chunks: list[TranscriptChunk] = []
        for segment in segments:
            if not isinstance(segment, dict):
                continue
            text = str(segment.get("text") or "").strip()
            if not text:
                continue
            speaker = str(
                segment.get("speaker")
                or segment.get("speaker_name")
                or segment.get("speaker_label")
                or "participant"
            ).strip()
            chunks.append(
                TranscriptChunk(
                    speaker=speaker or "participant",
                    text=text,
                    source="recording_audio_transcription",
                    metadata=self._segment_metadata(segment),
                )
            )
        return chunks

    def _segment_metadata(self, segment: dict[str, Any]) -> dict[str, Any]:
        metadata: dict[str, Any] = {}
        start_offset, end_offset = self._segment_time_bounds(segment)
        if start_offset is not None:
            metadata["start_offset_seconds"] = round(start_offset, 3)
        if end_offset is not None:
            metadata["end_offset_seconds"] = round(end_offset, 3)
        speaker_turn_next = segment.get("speaker_turn_next")
        if isinstance(speaker_turn_next, bool):
            metadata["speaker_turn_next"] = speaker_turn_next
        diarization_speaker = (
            segment.get("speaker")
            or segment.get("speaker_name")
            or segment.get("speaker_label")
        )
        if str(diarization_speaker or "").strip():
            metadata["diarization_speaker"] = str(diarization_speaker).strip()
        no_speech_prob = segment.get("no_speech_prob")
        try:
            if no_speech_prob is not None:
                metadata["confidence"] = round(max(0.0, min(1.0, 1.0 - float(no_speech_prob))), 4)
        except (TypeError, ValueError):
            pass
        avg_logprob = segment.get("avg_logprob")
        try:
            if avg_logprob is not None:
                metadata["avg_logprob"] = round(float(avg_logprob), 4)
        except (TypeError, ValueError):
            pass
        return metadata

    def _segment_time_bounds(self, segment: dict[str, Any]) -> tuple[float | None, float | None]:
        start = self._coerce_time_seconds(segment.get("start"))
        end = self._coerce_time_seconds(segment.get("end"))

        offsets = segment.get("offsets")
        if isinstance(offsets, dict):
            if start is None:
                start = self._coerce_time_seconds(offsets.get("from"), assume_ms=True)
            if end is None:
                end = self._coerce_time_seconds(offsets.get("to"), assume_ms=True)
            if start is None:
                start = self._coerce_time_seconds(offsets.get("start"), assume_ms=True)
            if end is None:
                end = self._coerce_time_seconds(offsets.get("end"), assume_ms=True)

        timestamps = segment.get("timestamps")
        if isinstance(timestamps, dict):
            if start is None:
                start = self._coerce_time_seconds(timestamps.get("from"))
            if end is None:
                end = self._coerce_time_seconds(timestamps.get("to"))
            if start is None:
                start = self._coerce_time_seconds(timestamps.get("start"))
            if end is None:
                end = self._coerce_time_seconds(timestamps.get("end"))

        if start is not None and end is not None and end < start:
            end = start
        return start, end

    def _coerce_time_seconds(self, value: Any, *, assume_ms: bool = False) -> float | None:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            numeric = float(value)
            return numeric / 1000.0 if assume_ms else numeric

        text = str(value).strip()
        if not text:
            return None
        if ":" in text:
            return self._parse_clock_timestamp(text)
        try:
            numeric = float(text)
        except ValueError:
            return None
        return numeric / 1000.0 if assume_ms else numeric

    def _parse_clock_timestamp(self, value: str) -> float | None:
        text = str(value or "").strip()
        if not text:
            return None
        parts = text.split(":")
        try:
            numbers = [float(part) for part in parts]
        except ValueError:
            return None
        seconds = 0.0
        for item in numbers:
            seconds = seconds * 60 + item
        return seconds

    def _normalize_transcript_text(self, value: str) -> str:
        return re.sub(r"\s+", " ", str(value or "")).strip()

    def _extract_transcription_text(self, payload: dict[str, Any] | str) -> str:
        if isinstance(payload, str):
            return payload.strip()
        return str(payload.get("text") or "").strip()

    def _text_to_chunks(self, transcript_text: str) -> list[TranscriptChunk]:
        normalized = re.sub(r"\s+", " ", transcript_text or "").strip()
        if not normalized:
            return []
        parts = re.split(r"(?<=[.!?])\s+", normalized)
        chunks: list[TranscriptChunk] = []
        buffer: list[str] = []
        for part in parts:
            cleaned = part.strip()
            if not cleaned:
                continue
            buffer.append(cleaned)
            if len(" ".join(buffer)) >= 160 or cleaned.endswith(("?", "!", ".")):
                chunks.append(
                    TranscriptChunk(
                        speaker="participant",
                        text=" ".join(buffer).strip(),
                        source="recording_audio_transcription",
                    )
                )
                buffer = []
        if buffer:
            chunks.append(
                TranscriptChunk(
                    speaker="participant",
                    text=" ".join(buffer).strip(),
                    source="recording_audio_transcription",
                )
            )
        return chunks

    def _resolve_ffmpeg(self) -> str | None:
        configured = os.getenv("DELEGATE_FFMPEG_PATH", "").strip()
        if configured and Path(configured).exists():
            return configured
        discovered = shutil.which("ffmpeg")
        if discovered:
            return discovered
        fallback = Path(
            r"C:\Users\jung\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-8.0-full_build\bin\ffmpeg.exe"
        )
        if fallback.exists():
            return str(fallback)
        return None

    def _resolve_whisper_cpp(self) -> str | None:
        configured = os.getenv("DELEGATE_WHISPER_CPP_PATH", "").strip()
        if configured and Path(configured).exists():
            return configured
        discovered = shutil.which("whisper-cli")
        if discovered:
            return discovered
        fallback = find_whisper_cpp_cli()
        if fallback is not None:
            return str(fallback)
        return None

    def _resolve_whisper_cpp_model(self) -> str | None:
        configured = os.getenv("DELEGATE_WHISPER_CPP_MODEL", "").strip()
        if configured and Path(configured).exists():
            return configured
        configured_model_name = self._model_name_from_whisper_cpp_path(configured)
        model_name = configured_model_name or self._local_whisper_model_name or "base"
        fallback = find_whisper_cpp_model(model_name)
        if fallback is not None:
            return str(fallback)
        return None

    @property
    def _whisper_cpp_ready(self) -> bool:
        return bool(self._whisper_cpp_path and self._whisper_cpp_model_path)

    def _mime_type_for_suffix(self, suffix: str) -> str:
        mapping = {
            ".wav": "audio/wav",
            ".mp3": "audio/mpeg",
            ".m4a": "audio/mp4",
            ".mp4": "video/mp4",
            ".webm": "video/webm",
        }
        return mapping.get(suffix.lower(), "application/octet-stream")

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

    def _env_float(self, name: str, default: float) -> float:
        raw = os.getenv(name)
        if raw is None:
            return default
        try:
            return float(raw.strip())
        except ValueError:
            return default

    def _env_csv(self, name: str) -> list[str]:
        raw = os.getenv(name)
        if raw is None:
            return []
        values: list[str] = []
        for item in raw.split(","):
            cleaned = str(item or "").strip()
            if cleaned:
                values.append(cleaned)
        return values

    def _resolve_recording_transcription_providers(self) -> list[str]:
        configured = [
            provider
            for provider in (item.strip().lower() for item in self._env_csv("DELEGATE_RECORDING_TRANSCRIPTION_PROVIDERS"))
            if provider in {"whisper_cpp", "local_whisper", "openai"}
        ]
        providers = configured or self._default_recording_transcription_providers()
        deduped: list[str] = []
        for provider in providers:
            if provider not in deduped:
                deduped.append(provider)
        return deduped

    def _default_recording_transcription_providers(self) -> list[str]:
        providers: list[str] = []
        if self._prefer_local_transcription:
            if self._prefer_whisper_cpp:
                providers.append("whisper_cpp")
            providers.append("local_whisper")
            if self._api_key:
                providers.append("openai")
            if not self._prefer_whisper_cpp:
                providers.append("whisper_cpp")
            return providers
        if self._api_key:
            providers.append("openai")
        providers.extend(["whisper_cpp", "local_whisper"])
        return providers

    def _resolve_faster_whisper_compute_types(self) -> list[str]:
        configured = [item.strip() for item in self._env_csv("DELEGATE_FASTER_WHISPER_COMPUTE_TYPES") if item.strip()]
        providers = configured or [
            str(self._faster_whisper_compute_type or "").strip(),
            str(self._faster_whisper_fallback_compute_type or "").strip(),
        ]
        deduped: list[str] = []
        for compute_type in providers:
            if compute_type and compute_type not in deduped:
                deduped.append(compute_type)
        return deduped or ["float16"]

    def _resolve_faster_whisper_cpu_compute_types(self) -> list[str]:
        configured = [item.strip() for item in self._env_csv("DELEGATE_FASTER_WHISPER_CPU_COMPUTE_TYPES") if item.strip()]
        providers = configured or ["int8", "float32"]
        deduped: list[str] = []
        for compute_type in providers:
            if compute_type and compute_type not in deduped:
                deduped.append(compute_type)
        return deduped or ["int8"]

    def _recording_provider_ready(self, provider: str) -> bool:
        if provider == "whisper_cpp":
            return self._whisper_cpp_ready
        if provider == "local_whisper":
            return bool(self._local_whisper_available)
        if provider == "openai":
            return bool(self._api_key)
        return False

    async def _transcribe_recording_with_provider(
        self,
        provider: str,
        prepared_path: Path,
    ) -> list[TranscriptChunk]:
        if provider == "whisper_cpp":
            return self._local_transcribe_with_whisper_cpp(prepared_path)
        if provider == "local_whisper":
            return self._local_transcribe_with_whisper(prepared_path)
        if provider == "openai":
            payload = await self._openai_transcribe(prepared_path)
            chunks = self._chunks_from_transcription_payload(payload)
            if chunks:
                return chunks
            transcript_text = self._extract_transcription_text(payload)
            return self._text_to_chunks(transcript_text)
        raise RuntimeError(f"Unsupported recording transcription provider: {provider}")

    def _local_transcribe_with_whisper(self, input_path: Path) -> list[TranscriptChunk]:
        whisper = self._load_whisper_module()
        model = self._load_whisper_model(whisper)
        result = model.transcribe(
            str(input_path),
            task="transcribe",
            language=self._local_whisper_language or None,
            fp16=False,
            verbose=False,
            condition_on_previous_text=False,
            word_timestamps=self._local_whisper_word_timestamps,
            best_of=self._local_whisper_best_of,
            beam_size=self._local_whisper_beam_size,
            temperature=0.0,
        )
        segments = result.get("segments") if isinstance(result, dict) else None
        chunks: list[TranscriptChunk] = []
        if isinstance(segments, list):
            for segment in segments:
                if not isinstance(segment, dict):
                    continue
                text = str(segment.get("text") or "").strip()
                if not text:
                    continue
                chunks.append(
                    TranscriptChunk(
                        speaker="participant",
                        text=text,
                        source="local_whisper_transcription",
                        metadata=self._segment_metadata(segment),
                    )
                )
        if chunks:
            return chunks
        transcript_text = ""
        if isinstance(result, dict):
            transcript_text = str(result.get("text") or "").strip()
        return [
            TranscriptChunk(
                speaker="participant",
                text=chunk.text,
                source="local_whisper_transcription",
            )
            for chunk in self._text_to_chunks(transcript_text)
        ]

    def _local_transcribe_with_whisper_cpp(self, input_path: Path) -> list[TranscriptChunk]:
        if not self._whisper_cpp_ready:
            raise RuntimeError("whisper.cpp CLI is not ready.")

        with tempfile.TemporaryDirectory(prefix="delegate-whispercpp-") as temp_dir:
            output_prefix = Path(temp_dir) / "transcription"
            json_path = output_prefix.with_suffix(".json")
            last_error = "whisper.cpp transcription failed."
            for command in self._whisper_cpp_command_attempts(input_path, output_prefix):
                if json_path.exists():
                    json_path.unlink()
                completed = subprocess.run(
                    command,
                    capture_output=True,
                    text=False,
                    check=False,
                )
                if completed.returncode != 0 or not json_path.exists():
                    last_error = (
                        self._decode_bytes(completed.stderr).strip()
                        or self._decode_bytes(completed.stdout).strip()
                        or f"exit code {completed.returncode}"
                    )
                    continue

                payload = json.loads(self._read_json_text(json_path))
                segments = payload.get("transcription", []) if isinstance(payload, dict) else []
                chunks = self._whisper_cpp_segments_to_chunks(segments)
                if chunks:
                    return chunks

                fallback_text = ""
                if isinstance(payload, dict):
                    fallback_text = " ".join(
                        str(item.get("text") or "").strip()
                        for item in segments
                        if isinstance(item, dict) and str(item.get("text") or "").strip()
                    ).strip()
                if fallback_text:
                    return [
                        TranscriptChunk(
                            speaker="participant",
                            text=chunk.text,
                            source="whisper_cpp_transcription",
                        )
                        for chunk in self._text_to_chunks(fallback_text)
                    ]
            raise RuntimeError(f"whisper.cpp transcription failed: {last_error}")

    def _whisper_cpp_command_attempts(self, input_path: Path, output_prefix: Path) -> list[list[str]]:
        supports_tinydiarize = str(self._whisper_cpp_model_path or "").lower().endswith("-tdrz.bin")
        input_channels = self._audio_channel_count(input_path)
        use_vad = bool(self._whisper_cpp_enable_vad and self._whisper_cpp_vad_model_path)
        use_tinydiarize = bool(self._whisper_cpp_enable_tinydiarize and supports_tinydiarize and input_channels != 2)
        use_stereo_diarize = bool(
            self._whisper_cpp_enable_stereo_diarize and not use_tinydiarize and input_channels == 2
        )

        attempts: list[tuple[bool, bool, bool]] = [
            (use_vad, use_tinydiarize, use_stereo_diarize),
            (use_vad, False, False),
            (False, use_tinydiarize, use_stereo_diarize),
            (False, False, False),
        ]
        commands: list[list[str]] = []
        seen: set[tuple[str, ...]] = set()
        for use_vad_flag, use_tinydiarize_flag, use_stereo_flag in attempts:
            command = self._build_whisper_cpp_command(
                input_path,
                output_prefix,
                use_vad=use_vad_flag,
                use_tinydiarize=use_tinydiarize_flag,
                use_stereo_diarize=use_stereo_flag and not use_tinydiarize_flag,
            )
            signature = tuple(command)
            if signature in seen:
                continue
            seen.add(signature)
            commands.append(command)
        return commands

    def _build_whisper_cpp_command(
        self,
        input_path: Path,
        output_prefix: Path,
        *,
        use_vad: bool,
        use_tinydiarize: bool,
        use_stereo_diarize: bool,
    ) -> list[str]:
        command = [
            str(self._whisper_cpp_path),
            "-m",
            str(self._whisper_cpp_model_path),
            "-f",
            str(input_path),
            "-l",
            self._local_whisper_language or self._transcribe_language or "ko",
            "-ojf",
            "-of",
            str(output_prefix),
            "-np",
            "-t",
            str(self._whisper_cpp_threads),
            "-bo",
            str(self._whisper_cpp_best_of),
            "-bs",
            str(self._whisper_cpp_beam_size),
            "-tp",
            f"{self._whisper_cpp_temperature:.2f}",
        ]
        if self._whisper_cpp_split_on_word:
            command.append("-sow")
        if self._whisper_cpp_suppress_nst:
            command.append("-sns")
        if self._whisper_cpp_no_fallback:
            command.append("-nf")
        if use_vad and self._whisper_cpp_vad_model_path:
            command.extend(["--vad", "--vad-model", str(self._whisper_cpp_vad_model_path)])
        if use_tinydiarize:
            command.append("-tdrz")
        elif use_stereo_diarize:
            command.append("-di")
        return command

    def _whisper_cpp_segments_to_chunks(self, segments: Any) -> list[TranscriptChunk]:
        chunks: list[TranscriptChunk] = []
        current_turn = 1
        if not isinstance(segments, list):
            return chunks
        has_turn_markers = any(
            isinstance(item, dict) and "speaker_turn_next" in item
            for item in segments
        )
        for item in segments:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            metadata = self._segment_metadata(item)
            speaker = self._speaker_from_whisper_cpp_segment(
                item,
                current_turn=current_turn,
                has_turn_markers=has_turn_markers,
            )
            chunks.append(
                TranscriptChunk(
                    speaker=speaker,
                    text=text,
                    source="whisper_cpp_transcription",
                    metadata=metadata,
                )
            )
            if metadata.get("speaker_turn_next") is True:
                current_turn = 2 if current_turn == 1 else 1
        return chunks

    def _speaker_from_whisper_cpp_segment(
        self,
        segment: dict[str, Any],
        *,
        current_turn: int,
        has_turn_markers: bool,
    ) -> str:
        explicit_speaker = self._normalize_diarized_speaker(
            segment.get("speaker") or segment.get("speaker_name") or segment.get("speaker_label")
        )
        if explicit_speaker:
            return explicit_speaker
        if has_turn_markers:
            return f"speaker_{current_turn}"
        return "participant"

    def _normalize_diarized_speaker(self, value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        normalized = text.lower()
        if normalized in {"participant", "speaker", "unknown"}:
            return ""
        if re.fullmatch(r"\d+", text):
            return f"speaker_{int(text) + 1}"
        match = re.fullmatch(r"speaker[_\-\s]*(\d+)", normalized)
        if match:
            index = int(match.group(1))
            return f"speaker_{index if index >= 1 else index + 1}"
        cleaned = re.sub(r"[^0-9a-zA-Z가-힣_-]+", "_", text).strip("_")
        return cleaned or ""

    def _audio_channel_count(self, input_path: Path) -> int:
        try:
            with wave.open(str(input_path), "rb") as handle:
                return int(handle.getnchannels() or 1)
        except (wave.Error, FileNotFoundError, OSError):
            return 1

    def _resolve_whisper_cpp_vad_model(self) -> str | None:
        configured = os.getenv("DELEGATE_WHISPER_CPP_VAD_MODEL", "").strip()
        if configured and Path(configured).exists():
            return configured
        fallback = find_whisper_cpp_vad_model(preferred_model_path=self._whisper_cpp_model_path)
        if fallback is not None:
            return str(fallback)
        return None

    def _model_name_from_whisper_cpp_path(self, configured_path: str) -> str:
        name = Path(str(configured_path or "").strip()).name
        if name.startswith("ggml-") and name.endswith(".bin"):
            return name[len("ggml-") : -len(".bin")].strip()
        return ""

    @property
    def _faster_whisper_ready(self) -> bool:
        try:
            return importlib.util.find_spec("faster_whisper") is not None
        except Exception:
            return False

    @property
    def _pyannote_ready(self) -> bool:
        try:
            return bool(importlib.util.find_spec("pyannote.audio") is not None and self._huggingface_token)
        except Exception:
            return False

    def _gpu_ready(self) -> bool:
        if importlib.util.find_spec("torch") is None:
            return False
        try:
            import torch  # type: ignore[import-not-found]

            return bool(torch.cuda.is_available())
        except Exception:
            return False

    def _platform_supports_cpu_final_transcription(self) -> bool:
        if self._env_bool("DELEGATE_ALLOW_CPU_FINAL_TRANSCRIPTION", False):
            return True
        return sys.platform == "darwin"

    def _quality_backend(self, *, final_pass: bool) -> dict[str, Any]:
        gpu_ready = self._gpu_ready()
        cpu_final_fallback_allowed = bool(final_pass and self._platform_supports_cpu_final_transcription())
        if gpu_ready:
            return {
                "ready": True,
                "gpu_ready": True,
                "device": "cuda",
                "provider": "faster_whisper_cuda",
                "compute_types": list(self._faster_whisper_compute_types),
                "cpu_final_fallback_allowed": cpu_final_fallback_allowed,
            }
        if cpu_final_fallback_allowed:
            return {
                "ready": True,
                "gpu_ready": False,
                "device": "cpu",
                "provider": "faster_whisper_cpu",
                "compute_types": list(self._faster_whisper_cpu_compute_types),
                "cpu_final_fallback_allowed": True,
            }
        return {
            "ready": False,
            "gpu_ready": False,
            "device": "",
            "provider": "faster_whisper_cuda",
            "compute_types": list(self._faster_whisper_compute_types),
            "cpu_final_fallback_allowed": cpu_final_fallback_allowed,
        }

    def _load_faster_whisper_model(self, compute_type: str) -> Any:
        if compute_type not in self._faster_whisper_models:
            from faster_whisper import WhisperModel  # type: ignore[import-not-found]

            device = "cuda" if self._gpu_ready() else "cpu"
            self._faster_whisper_models[compute_type] = WhisperModel(
                self._final_transcribe_model_name,
                device=device,
                compute_type=compute_type,
            )
        return self._faster_whisper_models[compute_type]

    def _load_pyannote_pipeline(self) -> Any:
        if self._pyannote_pipeline is None:
            from pyannote.audio import Pipeline  # type: ignore[import-not-found]

            token = self._huggingface_token or None
            try:
                pipeline = Pipeline.from_pretrained(
                    self._pyannote_model_name,
                    token=token,
                )
            except TypeError:
                pipeline = Pipeline.from_pretrained(
                    self._pyannote_model_name,
                    use_auth_token=token,
                )
            if self._gpu_ready():
                try:
                    import torch  # type: ignore[import-not-found]

                    pipeline.to(torch.device("cuda"))
                except Exception:
                    pass
            self._pyannote_pipeline = pipeline
        return self._pyannote_pipeline

    def _run_pyannote_diarization(self, input_path: Path) -> list[dict[str, Any]]:
        pipeline = self._load_pyannote_pipeline()
        diarization_input = self._load_pyannote_waveform(input_path)
        diarization = pipeline(diarization_input)
        annotation = getattr(diarization, "speaker_diarization", diarization)
        segments: list[dict[str, Any]] = []
        for segment, _track, label in annotation.itertracks(yield_label=True):
            start = max(float(getattr(segment, "start", 0.0) or 0.0), 0.0)
            end = max(float(getattr(segment, "end", start) or start), start)
            speaker = str(label or "").strip()
            if not speaker:
                continue
            segments.append(
                {
                    "start": round(start, 3),
                    "end": round(end, 3),
                    "speaker": speaker,
                }
            )
        return segments

    def _load_pyannote_waveform(self, input_path: Path) -> dict[str, Any]:
        if importlib.util.find_spec("soundfile") is None:
            raise RuntimeError("soundfile is required to load audio for pyannote diarization.")
        import soundfile as sf  # type: ignore[import-not-found]
        import torch  # type: ignore[import-not-found]

        waveform, sample_rate = sf.read(str(input_path), dtype="float32", always_2d=True)
        waveform_tensor = torch.from_numpy(waveform.T.copy())
        return {
            "waveform": waveform_tensor,
            "sample_rate": int(sample_rate),
        }

    def _transcribe_final_channel_with_faster_whisper(
        self,
        input_path: Path,
        *,
        fallback_speaker: str,
        source: str,
        channel_origin: str,
        diarization_segments: list[dict[str, Any]] | None,
        transcription_provider: str,
        compute_types: list[str] | None = None,
    ) -> dict[str, Any]:
        return self._transcribe_channel_with_faster_whisper(
            input_path,
            fallback_speaker=fallback_speaker,
            source=source,
            channel_origin=channel_origin,
            diarization_segments=diarization_segments,
            quality_pass="final_offline",
            session_offset_base_seconds=0.0,
            transcription_provider=transcription_provider,
            compute_types=compute_types,
        )

    def _transcribe_channel_with_faster_whisper(
        self,
        input_path: Path,
        *,
        fallback_speaker: str,
        source: str,
        channel_origin: str,
        diarization_segments: list[dict[str, Any]] | None,
        quality_pass: str,
        session_offset_base_seconds: float | None,
        transcription_provider: str,
        compute_types: list[str] | None = None,
    ) -> dict[str, Any]:
        attempts = list(compute_types or self._faster_whisper_compute_types)
        last_error = "faster-whisper transcription failed."
        dropped_segment_count = 0
        base_offset = max(float(session_offset_base_seconds or 0.0), 0.0)

        for compute_type in attempts:
            try:
                model = self._load_faster_whisper_model(compute_type)
                segments, _info = model.transcribe(
                    str(input_path),
                    language=self._final_transcribe_language or None,
                    beam_size=self._faster_whisper_beam_size,
                    temperature=0.0,
                    word_timestamps=True,
                    vad_filter=self._faster_whisper_vad_filter,
                    condition_on_previous_text=False,
                )
                diarization_lookup = self._build_diarization_label_map(diarization_segments or [])
                chunks: list[TranscriptChunk] = []
                for segment in segments:
                    text = self._normalize_transcript_text(str(getattr(segment, "text", "") or ""))
                    if not text:
                        continue
                    start = max(float(getattr(segment, "start", 0.0) or 0.0), 0.0)
                    end = max(float(getattr(segment, "end", start) or start), start)
                    metadata: dict[str, Any] = {
                        "start_offset_seconds": round(start, 3),
                        "end_offset_seconds": round(end, 3),
                        "session_start_offset_seconds": round(base_offset + start, 3),
                        "session_end_offset_seconds": round(base_offset + end, 3),
                        "channel_origin": channel_origin,
                        "audio_source": "microphone" if channel_origin == "local_user" else "system",
                        "transcription_provider": transcription_provider,
                        "diarization_provider": self._pyannote_model_name if diarization_segments is not None else None,
                        "quality_pass": quality_pass,
                        "avg_logprob": self._round_optional(getattr(segment, "avg_logprob", None), 4),
                        "no_speech_prob": self._round_optional(getattr(segment, "no_speech_prob", None), 4),
                    }
                    confidence = self._segment_confidence_from_stats(
                        getattr(segment, "avg_logprob", None),
                        getattr(segment, "no_speech_prob", None),
                    )
                    if confidence is not None:
                        metadata["segment_confidence"] = confidence
                    speaker = fallback_speaker
                    if diarization_segments is not None:
                        diarized_speaker = self._speaker_for_interval(
                            start,
                            end,
                            diarization_segments,
                            diarization_lookup,
                        )
                        if diarized_speaker:
                            speaker = diarized_speaker
                            metadata["diarization_speaker"] = diarized_speaker
                    chunks.append(
                        TranscriptChunk(
                            speaker=speaker,
                            text=text,
                            source=source,
                            metadata={key: value for key, value in metadata.items() if value is not None},
                        )
                    )
                return {
                    "chunks": chunks,
                    "compute_type": compute_type,
                    "dropped_segment_count": dropped_segment_count,
                }
            except Exception as exc:
                last_error = str(exc)
                continue
        raise RuntimeError(f"{last_error}")

    def _build_diarization_label_map(self, segments: list[dict[str, Any]]) -> dict[str, str]:
        label_map: dict[str, str] = {}
        next_index = 1
        for item in segments:
            speaker = str(item.get("speaker") or "").strip()
            if not speaker or speaker in label_map:
                continue
            label_map[speaker] = f"remote_participant_{next_index}"
            next_index += 1
        return label_map

    def _speaker_for_interval(
        self,
        start: float,
        end: float,
        diarization_segments: list[dict[str, Any]],
        label_map: dict[str, str],
    ) -> str | None:
        best_overlap = 0.0
        best_speaker: str | None = None
        for item in diarization_segments:
            diarized_start = float(item.get("start") or 0.0)
            diarized_end = float(item.get("end") or diarized_start)
            overlap = max(0.0, min(end, diarized_end) - max(start, diarized_start))
            if overlap <= best_overlap:
                continue
            speaker = str(item.get("speaker") or "").strip()
            if not speaker:
                continue
            best_overlap = overlap
            best_speaker = label_map.get(speaker) or speaker
        return best_speaker

    def _segment_confidence_from_stats(self, avg_logprob: Any, no_speech_prob: Any) -> float | None:
        try:
            no_speech = float(no_speech_prob) if no_speech_prob is not None else None
        except (TypeError, ValueError):
            no_speech = None
        try:
            avg_log = float(avg_logprob) if avg_logprob is not None else None
        except (TypeError, ValueError):
            avg_log = None

        score = 1.0
        if no_speech is not None:
            score *= max(0.0, min(1.0, 1.0 - no_speech))
        if avg_log is not None:
            log_score = max(0.0, min(1.0, (avg_log + 1.5) / 1.5))
            score *= log_score
        return round(max(0.0, min(1.0, score)), 4)

    def _round_optional(self, value: Any, digits: int) -> float | None:
        try:
            if value is None:
                return None
            return round(float(value), digits)
        except (TypeError, ValueError):
            return None

    def _load_whisper_module(self) -> Any:
        if self._whisper_module is None:
            import whisper  # type: ignore[import-not-found]

            self._whisper_module = whisper
        return self._whisper_module

    def _load_whisper_model(self, whisper_module: Any) -> Any:
        if self._whisper_model is None:
            self._whisper_model = whisper_module.load_model(self._local_whisper_model_name)
        return self._whisper_model

    def _read_json_text(self, path: Path) -> str:
        raw = path.read_bytes()
        candidates = [
            "utf-8",
            "utf-8-sig",
            locale.getpreferredencoding(False) or "utf-8",
            "cp949",
            "cp1252",
        ]
        seen: set[str] = set()
        for encoding in candidates:
            if not encoding or encoding in seen:
                continue
            seen.add(encoding)
            try:
                return raw.decode(encoding)
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", errors="replace")

    def _decode_bytes(self, value: bytes | str | None) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        candidates = [
            locale.getpreferredencoding(False) or "utf-8",
            "utf-8",
            "cp949",
            "cp1252",
        ]
        seen: set[str] = set()
        for encoding in candidates:
            if not encoding or encoding in seen:
                continue
            seen.add(encoding)
            try:
                return value.decode(encoding)
            except UnicodeDecodeError:
                continue
        return value.decode("utf-8", errors="replace")
