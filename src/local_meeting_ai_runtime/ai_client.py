"""AI summarization client for the delegate runtime."""

from __future__ import annotations

import asyncio
from datetime import timedelta
import hashlib
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
import time
from typing import Any, Callable
import wave
import gc

import httpx

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 fallback
    tomllib = None

try:
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client
except Exception:  # pragma: no cover - optional local MCP dependency
    ClientSession = None
    StdioServerParameters = None
    stdio_client = None

from .assets import find_whisper_cpp_cli, find_whisper_cpp_model, find_whisper_cpp_vad_model
from .design_agent import MeetingOutputDesignAgent
from .font_resolver import canonical_font_name
from .meeting_output_skill import (
    build_generated_meeting_output_skill_path,
    load_meeting_output_skill,
    resolve_result_generation_policy,
    resolve_generated_meeting_output_dir,
    write_generated_meeting_output_skill,
)
from .models import DelegateSession, TranscriptChunk, utcnow_iso

SUMMARY_STAGE_EXCLUDED_METADATA_KEYS = {
    "show_postprocess_requests",
    "max_postprocess_requests",
    "postprocess_image_width_inches",
    "postprocess_requests_heading",
    "empty_postprocess_requests_message",
}
SUMMARY_STAGE_VISUAL_KEYWORDS = (
    "이미지",
    "시각 자료",
    "시각자료",
    "비주얼",
    "nano-banana",
    "nanobanana",
    "image brief",
    "image_brief",
    "visual appendix",
    "visuals",
    "postprocess",
    "후속 결과물",
    "추가 결과물",
)


class AiDelegateClient:
    def __init__(self) -> None:
        self._prefer_codex = self._env_bool("DELEGATE_PREFER_CODEX", True)
        self._enable_codex_search = self._env_bool("DELEGATE_ENABLE_CODEX_SEARCH", True)
        self._codex_path = self._resolve_codex()
        self._codex_workdir = self._resolve_codex_workdir()
        self._codex_resume_supports_output_schema: bool | None = None
        self._result_image_direct_mcp = self._env_bool("DELEGATE_RESULT_IMAGE_DIRECT_MCP", True)
        self._result_image_mcp_server_name = (
            os.getenv("DELEGATE_RESULT_IMAGE_MCP_SERVER_NAME", "nanobanana").strip()
            or "nanobanana"
        )
        self._result_image_mcp_tool_name = (
            os.getenv("DELEGATE_RESULT_IMAGE_MCP_TOOL_NAME", "generate_image").strip()
            or "generate_image"
        )
        self._result_image_mcp_model_tier = (
            os.getenv("DELEGATE_RESULT_IMAGE_MCP_MODEL_TIER", "nb2").strip()
            or "nb2"
        )
        self._result_image_mcp_resolution = (
            os.getenv("DELEGATE_RESULT_IMAGE_MCP_RESOLUTION", "4k").strip()
            or "4k"
        )
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
        self._design_agent = MeetingOutputDesignAgent()
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
        self._meeting_output_skill = load_meeting_output_skill(
            os.getenv("DELEGATE_MEETING_OUTPUT_SKILL_PATH", "").strip()
        )
        override_skill_path = os.getenv("DELEGATE_MEETING_OUTPUT_OVERRIDE_PATH", "").strip()
        self._meeting_output_customization = os.getenv("DELEGATE_MEETING_OUTPUT_CUSTOMIZATION", "").strip()
        self._generated_meeting_output_dir = resolve_generated_meeting_output_dir(
            os.getenv("DELEGATE_GENERATED_MEETING_OUTPUT_DIR", "").strip()
        )
        self._result_image_max_count = max(
            1,
            min(self._env_int("DELEGATE_RESULT_IMAGE_MAX_COUNT", 8), 20),
        )
        self._result_image_max_concurrency = max(
            1,
            min(self._env_int("DELEGATE_RESULT_IMAGE_MAX_CONCURRENCY", 2), 4),
        )
        self._result_image_mcp_max_concurrency = max(
            1,
            min(
                self._env_int("DELEGATE_RESULT_IMAGE_MCP_MAX_CONCURRENCY", 1),
                self._result_image_max_concurrency,
            ),
        )
        self._result_image_timeout = float(os.getenv("DELEGATE_RESULT_IMAGE_TIMEOUT_SECONDS", "180"))
        self._result_image_review_attempts = max(
            1,
            min(self._env_int("DELEGATE_RESULT_IMAGE_REVIEW_ATTEMPTS", 2), 3),
        )
        self._result_image_review_timeout = float(
            os.getenv("DELEGATE_RESULT_IMAGE_REVIEW_TIMEOUT_SECONDS", "180")
        )
        self._result_image_mcp_server_config = self._resolve_result_image_mcp_server_config()
        self._result_image_mcp_concurrency_loop: asyncio.AbstractEventLoop | None = None
        self._result_image_mcp_concurrency_semaphore: asyncio.Semaphore | None = None
        self._generated_meeting_output_skill: dict[str, object] | None = None
        self._configured_meeting_output_override = False
        if override_skill_path:
            loaded_override = load_meeting_output_skill(override_skill_path)
            if str(loaded_override.get("body") or "").strip():
                self._generated_meeting_output_skill = loaded_override
                self._configured_meeting_output_override = True

    async def summarize_session(self, session: DelegateSession) -> dict[str, Any]:
        if not self._codex_ready:
            raise RuntimeError("The local Codex body is unavailable for meeting summarization.")
        self._ensure_generated_meeting_output_skill(session)
        return self._codex_summarize(session)

    async def draft_reply(self, session: DelegateSession, request_text: str) -> dict[str, Any]:
        cleaned_request_text = str(request_text or "").strip()
        if not cleaned_request_text:
            raise ValueError("A live meeting request is required.")
        if not self._codex_ready:
            raise RuntimeError("The local Codex body is unavailable for live meeting replies.")
        return self._codex_draft_reply(session, cleaned_request_text)

    async def materialize_result_generation(
        self,
        session: DelegateSession,
        ai_result: dict[str, Any],
        *,
        output_dir: str | Path,
        progress_callback: Callable[[str, str, str | None], None] | None = None,
    ) -> dict[str, Any]:
        materialized = dict(ai_result or {})
        self._apply_resolved_renderer_theme(session)
        requests = self._clean_result_generation_requests(materialized.get("postprocess_requests"))
        has_image_request = any(
            str(item.get("kind") or "").strip().lower() == "image_brief"
            for item in requests
        )
        if not has_image_request:
            synthesized_requests = self._synthesize_postprocess_requests_from_skill(
                session,
                ai_result=materialized,
            )
            if synthesized_requests:
                requests.extend(synthesized_requests)
        non_image_request_count = len(
            [
                item
                for item in requests
                if str(item.get("kind") or "").strip().lower() != "image_brief"
                or str(item.get("image_path") or "").strip()
            ]
        )
        timing_started_at = self._now_iso()
        timing_perf = time.perf_counter()
        timing_bucket = self._begin_result_generation_timing(
            session,
            started_at=timing_started_at,
            request_count=len(requests),
            non_image_request_count=non_image_request_count,
        )
        if not requests:
            materialized["postprocess_requests"] = []
            finished_at = self._now_iso()
            timing_bucket.update(
                {
                    "status": "skipped",
                    "reason": "no_postprocess_requests",
                    "image_request_count": 0,
                    "requests": [],
                    "finished_at": finished_at,
                    "elapsed_seconds": round(max(time.perf_counter() - timing_perf, 0.0), 3),
                    "updated_at": finished_at,
                }
            )
            return materialized
        request_timings: list[dict[str, Any]] = []
        resolved_requests = await self._materialize_result_generation_requests(
            session,
            requests,
            output_dir=Path(output_dir),
            title=str(materialized.get("title") or "").strip(),
            context_result=materialized,
            progress_callback=progress_callback,
            timing_entries=request_timings,
        )
        materialized["postprocess_requests"] = resolved_requests
        image_request_count = len(
            [
                item
                for item in requests
                if str(item.get("kind") or "").strip().lower() == "image_brief"
                and not str(item.get("image_path") or "").strip()
            ]
        )
        finished_at = self._now_iso()
        timing_bucket.update(
            {
                "status": "completed" if image_request_count else "skipped",
                "reason": "" if image_request_count else "no_image_generation_requests",
                "image_request_count": image_request_count,
                "requests": request_timings,
                "finished_at": finished_at,
                "elapsed_seconds": round(max(time.perf_counter() - timing_perf, 0.0), 3),
                "updated_at": finished_at,
            }
        )
        return materialized

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
        self._release_faster_whisper_runtime()
        self._release_pyannote_runtime()
        cuda_cache_cleared = self._clear_torch_cuda_cache()
        return {
            "released_compute_types": released_compute_types,
            "released_model_count": len(released_compute_types),
            "released_pyannote_pipeline": released_pyannote,
            "cuda_cache_cleared": cuda_cache_cleared,
        }

    def _release_faster_whisper_runtime(self) -> None:
        self._faster_whisper_models.clear()
        gc.collect()

    def _release_pyannote_runtime(self) -> None:
        self._pyannote_pipeline = None
        gc.collect()

    def _clear_torch_cuda_cache(self) -> bool:
        if importlib.util.find_spec("torch") is None:
            return False
        try:
            import torch  # type: ignore[import-not-found]

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
                return True
        except Exception:
            return False
        return False

    def _release_quality_runtime_phase(
        self,
        *,
        release_faster_whisper: bool = False,
        release_pyannote: bool = False,
    ) -> None:
        if release_faster_whisper:
            self._release_faster_whisper_runtime()
        if release_pyannote:
            self._release_pyannote_runtime()
        if release_faster_whisper or release_pyannote:
            self._clear_torch_cuda_cache()

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
                # Avoid overlapping Whisper GPU residency with diarization GPU residency.
                self._release_quality_runtime_phase(release_faster_whisper=True)

        if meeting_output_path:
            diarization_segments = self._run_pyannote_diarization(Path(meeting_output_path))
            self._release_quality_runtime_phase(release_pyannote=True)
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
            self._release_quality_runtime_phase(release_faster_whisper=True)

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
            "The required JSON schema below is fixed by the system.\n"
            "Return these JSON keys: `title`, `executive_summary`, `summary`, `action_items`, `decisions`, `open_questions`, `risk_signals`, `sections`.\n"
            "Use the active meeting-output skill as the primary source of result-generation guidance.\n"
            "The skill defines how the final result should read, what should be emphasized, how blocks should behave, and what kind of follow-up result work may be requested.\n"
            "The JSON keys are internal transport slots, not rigid user-facing labels.\n"
            "If the skill redefines a block's displayed role or meaning, keep the JSON key but write the content to match that skill meaning.\n"
            "For example, the internal `decisions` slot may hold 검토사항, 논의 포인트, or pending review items when the skill says so.\n"
            "Finish the text briefing first during this step.\n"
            "Do not plan generated images, visual appendix items, or other visual follow-up work while writing the text briefing.\n"
            "`postprocess_requests` is optional at this stage. Use it only for non-visual downstream result work that must survive into final export.\n"
            "Do not emit image briefs, image ideas, visual appendix plans, or other generated-visual requests in `postprocess_requests` during this step.\n"
            "Do not compress a long or conceptually dense meeting into too few sections or too few sentences just to fit a neat short template.\n"
            "When the meeting is explanatory, lecture-like, or strategy-heavy, the sections may read like interpretive briefing notes rather than a bare chronology.\n"
            "Every section must include concrete `timestamp_refs` drawn from the session evidence.\n"
            "Do not return an empty `timestamp_refs` array for a real section; if the evidence is broad, choose the closest 1 to 4 supporting clock labels.\n"
            "If the skill conflicts with the required JSON keys or schema, the schema wins.\n\n"
            f"{self._meeting_output_summary_skill_prompt_block()}"
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
                    "executive_summary": {"type": "string"},
                    "summary": {"type": "string"},
                    "action_items": {"type": "array", "items": {"type": "string"}},
                    "decisions": {"type": "array", "items": {"type": "string"}},
                    "open_questions": {"type": "array", "items": {"type": "string"}},
                    "risk_signals": {"type": "array", "items": {"type": "string"}},
                    "postprocess_requests": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "kind": {"type": "string"},
                                "title": {"type": "string"},
                                "instruction": {"type": "string"},
                                "prompt": {"type": "string"},
                                "tool_hint": {"type": "string"},
                                "caption": {"type": "string"},
                                "image_path": {"type": "string"},
                                "count": {"type": "integer", "minimum": 1, "maximum": self._result_image_max_count},
                                "placement_notes": {"type": "string"},
                                "target_heading": {"type": "string"},
                            },
                            "required": ["kind", "title", "instruction"],
                        },
                    },
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
                "required": ["title", "executive_summary", "summary", "action_items", "sections"],
            },
            timeout_seconds=max(self._timeout, self._summary_timeout),
        )
        title = str(parsed.get("title") or "").strip()
        executive_summary = str(parsed.get("executive_summary") or "").strip()
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
        postprocess_requests = parsed.get("postprocess_requests")
        if not isinstance(postprocess_requests, list):
            postprocess_requests = []
        cleaned_postprocess_requests = [
            item
            for item in self._clean_result_generation_requests(postprocess_requests)
            if str(item.get("kind") or "").strip().lower() != "image_brief"
        ]
        sections = parsed.get("sections")
        if not isinstance(sections, list):
            sections = []
        summary = str(parsed.get("summary") or "").strip()
        if not summary:
            raise RuntimeError("The local Codex body returned an empty meeting summary.")
        if not executive_summary:
            raise RuntimeError("The local Codex body returned an empty executive summary.")
        return {
            "title": title,
            "executive_summary": executive_summary,
            "summary": summary,
            "action_items": [str(item).strip() for item in action_items if str(item).strip()],
            "decisions": [str(item).strip() for item in decisions if str(item).strip()],
            "open_questions": [str(item).strip() for item in open_questions if str(item).strip()],
            "risk_signals": [str(item).strip() for item in risk_signals if str(item).strip()],
            "postprocess_requests": cleaned_postprocess_requests,
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

    def _apply_resolved_renderer_theme(self, session: DelegateSession) -> None:
        state = dict(session.ai_state.get("meeting_output_skill") or {})
        policy = dict(state.get("result_generation_policy") or {})
        theme_name = str(policy.get("renderer_theme_name") or "").strip()
        resolved: dict[str, str] = {}
        if theme_name:
            resolved = dict(state.get("resolved_renderer_theme") or {})
            if not resolved:
                resolved = self._resolve_renderer_theme(
                    theme_name,
                    session=session,
                    current_policy=policy,
                    force_refresh=self._should_force_renderer_theme_refresh(state),
                    skill_state=state,
                )
                if resolved:
                    state["resolved_renderer_theme"] = resolved
        merged = dict(policy)
        merged.update({key: value for key, value in resolved.items() if str(value or "").strip()})
        design_state = self._design_agent.resolve(
            active_skill={
                "name": state.get("name"),
                "description": state.get("description"),
                "metadata": dict(state.get("metadata") or {}),
                "body": str(state.get("body") or ""),
            },
            current_policy=merged,
            source=str(state.get("source") or ""),
        )
        merged = dict(design_state.get("resolved_policy") or merged)
        font_resolution = self._resolve_renderer_fonts(
            session,
            current_policy=merged,
            skill_state=state,
        )
        if font_resolution:
            merged.update(font_resolution)
        state["design_intent_packet"] = dict(design_state.get("intent_packet") or {})
        state["result_generation_policy"] = merged
        session.ai_state["meeting_output_skill"] = state

    def _resolve_renderer_theme(
        self,
        theme_name: str,
        *,
        session: DelegateSession,
        current_policy: dict[str, Any],
        force_refresh: bool = False,
        skill_state: dict[str, Any] | None = None,
    ) -> dict[str, str]:
        normalized_theme = str(theme_name or "").strip()
        if not normalized_theme:
            return {}
        resolved: dict[str, str] = {}
        missing_keys = [
            key
            for key in (
                "renderer_primary_color",
                "renderer_accent_color",
                "renderer_neutral_color",
                "renderer_title_font",
                "renderer_heading_font",
                "renderer_body_font",
                "renderer_cover_align",
                "renderer_surface_tint_color",
            )
            if not str(current_policy.get(key) or resolved.get(key) or "").strip()
        ]
        if (missing_keys or force_refresh) and self._codex_ready:
            try:
                external = self._resolve_renderer_theme_with_codex(
                    session,
                    normalized_theme,
                    current_policy=current_policy,
                    skill_state=skill_state,
                )
            except Exception:
                external = {}
            for key, value in external.items():
                if str(value or "").strip():
                    resolved[key] = str(value).strip()
        return {
            key: str(value).strip()
            for key, value in resolved.items()
            if str(value or "").strip()
        }

    def _should_force_renderer_theme_refresh(self, state: dict[str, Any]) -> bool:
        source = str(state.get("source") or "").strip().lower()
        path_text = str(state.get("path") or "").strip().replace("\\", "/").lower()
        if source.startswith("generated_"):
            return True
        if source == "configured_override" and "/skills/generated/" in f"/{path_text}/":
            return True
        return False

    def _skill_instruction_lines(self, state: dict[str, Any]) -> list[str]:
        lines: list[str] = []
        description = str(state.get("description") or "")
        if description:
            lines.append(description)
        body = str(state.get("body") or "")
        if body:
            lines.extend(body.splitlines())
        return lines

    def _raw_skill_instruction_block(self, state: dict[str, Any]) -> str:
        description = str(state.get("description") or "")
        body = str(state.get("body") or "")
        parts = [part for part in (description, body) if part]
        if not parts:
            return ""
        return "\n".join(parts)

    def _is_visual_postprocess_text(self, text: str) -> bool:
        lowered = str(text or "").strip().casefold()
        if not lowered:
            return False
        return any(keyword in lowered for keyword in SUMMARY_STAGE_VISUAL_KEYWORDS)

    def _strip_visual_postprocess_guidance(self, text: str) -> str:
        lines = str(text or "").splitlines()
        filtered: list[str] = []
        skip_heading_level: int | None = None
        for line in lines:
            stripped = line.strip()
            heading_match = re.match(r"^(#{1,6})\s+(.*)$", stripped)
            if skip_heading_level is not None:
                if heading_match and len(heading_match.group(1)) <= skip_heading_level:
                    skip_heading_level = None
                else:
                    continue
            if heading_match:
                heading_level = len(heading_match.group(1))
                heading_text = heading_match.group(2).strip()
                if self._is_visual_postprocess_text(heading_text):
                    skip_heading_level = heading_level
                    continue
            if stripped and self._is_visual_postprocess_text(stripped):
                continue
            filtered.append(line)
        return "\n".join(filtered).strip()

    def _summary_stage_skill_metadata(self, metadata: dict[str, Any]) -> dict[str, str]:
        filtered: dict[str, str] = {}
        for raw_key, raw_value in dict(metadata or {}).items():
            key = str(raw_key or "").strip()
            value = str(raw_value or "").strip()
            if not key or not value:
                continue
            if key in SUMMARY_STAGE_EXCLUDED_METADATA_KEYS:
                continue
            if key == "result_block_order":
                order = [item.strip() for item in value.split(",") if item.strip()]
                order = [item for item in order if item != "postprocess_requests"]
                if not order:
                    continue
                filtered[key] = ", ".join(order)
                continue
            filtered[key] = value
        return filtered

    def _summary_stage_skill_state(self, skill: dict[str, object] | None) -> dict[str, object]:
        state = dict(skill or {})
        description = str(state.get("description") or "").strip()
        return {
            "name": str(state.get("name") or "").strip(),
            "resolved_path": str(state.get("resolved_path") or state.get("path") or "").strip(),
            "description": "" if self._is_visual_postprocess_text(description) else description,
            "metadata": self._summary_stage_skill_metadata(dict(state.get("metadata") or {})),
            "body": self._strip_visual_postprocess_guidance(str(state.get("body") or "")),
        }

    def _resolve_renderer_theme_with_codex(
        self,
        session: DelegateSession,
        theme_name: str,
        *,
        current_policy: dict[str, Any],
        skill_state: dict[str, Any] | None = None,
    ) -> dict[str, str]:
        skill_theme_notes = self._skill_theme_request_notes(skill_state)
        raw_skill_block = self._raw_skill_instruction_block(dict(skill_state or {}))
        skill_theme_block = ""
        if skill_theme_notes:
            skill_theme_block = "Skill execution notes:\n" + "\n".join(f"- {note}" for note in skill_theme_notes) + "\n\n"
        if raw_skill_block:
            skill_theme_block += "Binding skill directives:\n" + raw_skill_block + "\n\n"
        request_text = (
            "Task: resolve the user's document design theme into concrete renderer hints.\n"
            "If the theme names a real company, brand, or public visual identity, first use web search to infer stable public-facing cues before choosing any renderer hints.\n"
            "Do not rely on built-in company presets; infer the theme from public-facing brand cues instead.\n"
            "Do not rely on prior model memory alone for brand interpretation.\n"
            "Return compact JSON only.\n"
            "Choose Korean-friendly document fonts that are broadly available. If an exact brand font is proprietary or unclear, pick a safe nearby document font.\n"
            "The renderer can directly load SUIT, Pretendard, Noto Sans KR, Noto Serif KR, Nanum Gothic, Nanum Myeongjo, and Spoqa Han Sans Neo as web fonts.\n"
            "Malgun Gothic and Batang remain acceptable local fallbacks when no stronger web-font choice is justified.\n"
            "Do not fall back to Malgun Gothic unless the directives are truly generic and no stronger public-facing match is justified.\n"
            "The goal is a business PDF theme, not a marketing landing page.\n\n"
            f"{skill_theme_block}"
            "Prefer direct document-surface controls over preset-style labels.\n"
            "Use concrete spacing, margins, fills, divider sizing, and typography whenever they help the renderer execute the skill faithfully.\n"
            "Required JSON keys:\n"
            "- `renderer_primary_color`: six-digit hex without #\n"
            "- `renderer_accent_color`: six-digit hex without #\n"
            "- `renderer_neutral_color`: six-digit hex without #\n"
            "- `renderer_title_font`: short font family name\n"
            "- `renderer_heading_font`: short font family name\n"
            "- `renderer_body_font`: short font family name\n"
            "- `renderer_cover_align`: optional alignment hint such as `left` or `center`\n"
            "- `renderer_cover_layout`: structural cover mode such as `panel`, `minimal`, or `split`\n"
            "- `renderer_cover_background_style`: cover background language such as `gradient`, `solid`, or `minimal`\n"
            "- `renderer_panel_style`: outer block shell such as `soft`, `sharp`, or `minimal`\n"
            "- `renderer_heading_style`: section-heading language such as `chip`, `underline`, `plain`, or `band`\n"
            "- `renderer_overview_layout`: overview structure such as `grid`, `inline`, or `stack`\n"
            "- `renderer_section_style`: section-body treatment such as `accent`, `divider`, or `minimal`\n"
            "- `renderer_list_style`: list-block treatment such as `panel`, `divider`, or `minimal`\n"
            "- `renderer_surface_tint_color`: six-digit hex without #, or an empty string when unnecessary\n"
            "- `renderer_cover_kicker`: optional short cover-label text, or an empty string\n"
            "- `renderer_heading1_color`, `renderer_heading2_color`, `renderer_heading3_color`: six-digit hex without #, or an empty string when the base palette is enough\n"
            "- `renderer_body_text_color`, `renderer_muted_text_color`: six-digit hex without #, or an empty string when the base palette is enough\n"
            "- `renderer_title_divider_color`, `renderer_section_border_color`, `renderer_table_header_fill_color`, `renderer_table_label_fill_color`: concrete surface colors when needed, otherwise empty strings\n"
            "- `renderer_cover_fill_color`, `renderer_kicker_fill_color`, `renderer_kicker_text_color`: concrete cover/kicker colors when needed, otherwise empty strings\n"
            "- `renderer_section_band_fill_color`, `renderer_section_panel_fill_color`, `renderer_section_accent_fill_color`: concrete section treatment colors when needed, otherwise empty strings\n"
            "- `renderer_overview_label_fill_color`, `renderer_overview_value_fill_color`, `renderer_overview_panel_fill_color`: concrete overview treatment colors when needed, otherwise empty strings\n"
            "- `renderer_page_top_margin_inches`, `renderer_page_bottom_margin_inches`, `renderer_page_left_margin_inches`, `renderer_page_right_margin_inches`: numeric strings for page margins when the layout needs direct control\n"
            "- `renderer_body_line_spacing`, `renderer_list_line_spacing`: numeric strings for paragraph spacing when the skill calls for looser or tighter composition\n"
            "- `renderer_heading2_space_before_pt`, `renderer_heading2_space_after_pt`, `renderer_heading3_space_before_pt`, `renderer_heading3_space_after_pt`, `renderer_title_space_after_pt`: numeric strings for heading spacing when needed\n"
            "- `renderer_title_divider_size`, `renderer_title_divider_space`: numeric strings for the divider stroke and gap when a title divider is used\n"
            "- `renderer_block_gap_pt`, `renderer_panel_radius_pt`, `renderer_cover_radius_pt`, `renderer_heading_chip_radius_pt`, `renderer_overview_radius_pt`: numeric strings when the layout needs direct shape/rhythm control\n"
            "- `note`: one short Korean sentence summarizing the chosen visual direction\n\n"
            f"Requested theme:\n{theme_name}\n\n"
            "Current renderer hints already set:\n"
            f"{json.dumps({key: current_policy.get(key) for key in ('renderer_primary_color', 'renderer_accent_color', 'renderer_neutral_color', 'renderer_title_font', 'renderer_heading_font', 'renderer_body_font', 'renderer_cover_layout', 'renderer_cover_background_style', 'renderer_panel_style', 'renderer_heading_style', 'renderer_overview_layout', 'renderer_section_style', 'renderer_list_style', 'renderer_surface_tint_color', 'renderer_cover_fill_color', 'renderer_cover_kicker')}, ensure_ascii=False, indent=2)}"
        )
        parsed = self._codex_json_response(
            session,
            request_text,
            schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "renderer_primary_color": {"type": "string"},
                    "renderer_accent_color": {"type": "string"},
                    "renderer_neutral_color": {"type": "string"},
                    "renderer_title_font": {"type": "string"},
                    "renderer_heading_font": {"type": "string"},
                    "renderer_body_font": {"type": "string"},
                    "renderer_cover_align": {"type": "string"},
                    "renderer_cover_layout": {"type": "string"},
                    "renderer_cover_background_style": {"type": "string"},
                    "renderer_panel_style": {"type": "string"},
                    "renderer_heading_style": {"type": "string"},
                    "renderer_overview_layout": {"type": "string"},
                    "renderer_section_style": {"type": "string"},
                    "renderer_list_style": {"type": "string"},
                    "renderer_surface_tint_color": {"type": "string"},
                    "renderer_cover_kicker": {"type": "string"},
                    "renderer_heading1_color": {"type": "string"},
                    "renderer_heading2_color": {"type": "string"},
                    "renderer_heading3_color": {"type": "string"},
                    "renderer_body_text_color": {"type": "string"},
                    "renderer_muted_text_color": {"type": "string"},
                    "renderer_title_divider_color": {"type": "string"},
                    "renderer_section_border_color": {"type": "string"},
                    "renderer_table_header_fill_color": {"type": "string"},
                    "renderer_table_label_fill_color": {"type": "string"},
                    "renderer_cover_fill_color": {"type": "string"},
                    "renderer_kicker_fill_color": {"type": "string"},
                    "renderer_kicker_text_color": {"type": "string"},
                    "renderer_section_band_fill_color": {"type": "string"},
                    "renderer_section_panel_fill_color": {"type": "string"},
                    "renderer_section_accent_fill_color": {"type": "string"},
                    "renderer_overview_label_fill_color": {"type": "string"},
                    "renderer_overview_value_fill_color": {"type": "string"},
                    "renderer_overview_panel_fill_color": {"type": "string"},
                    "renderer_page_top_margin_inches": {"type": "string"},
                    "renderer_page_bottom_margin_inches": {"type": "string"},
                    "renderer_page_left_margin_inches": {"type": "string"},
                    "renderer_page_right_margin_inches": {"type": "string"},
                    "renderer_body_line_spacing": {"type": "string"},
                    "renderer_list_line_spacing": {"type": "string"},
                    "renderer_heading2_space_before_pt": {"type": "string"},
                    "renderer_heading2_space_after_pt": {"type": "string"},
                    "renderer_heading3_space_before_pt": {"type": "string"},
                    "renderer_heading3_space_after_pt": {"type": "string"},
                    "renderer_title_space_after_pt": {"type": "string"},
                    "renderer_title_divider_size": {"type": "string"},
                    "renderer_title_divider_space": {"type": "string"},
                    "renderer_block_gap_pt": {"type": "string"},
                    "renderer_panel_radius_pt": {"type": "string"},
                    "renderer_cover_radius_pt": {"type": "string"},
                    "renderer_heading_chip_radius_pt": {"type": "string"},
                    "renderer_overview_radius_pt": {"type": "string"},
                    "note": {"type": "string"},
                },
                "required": [
                    "renderer_primary_color",
                    "renderer_accent_color",
                    "renderer_neutral_color",
                    "renderer_title_font",
                    "renderer_heading_font",
                    "renderer_body_font",
                    "renderer_surface_tint_color",
                    "note",
                ],
            },
            timeout_seconds=max(self._timeout, 90),
        )
        return {
            "renderer_theme_name": str(theme_name or "").strip(),
            "renderer_primary_color": self._normalize_color_hex(parsed.get("renderer_primary_color")),
            "renderer_accent_color": self._normalize_color_hex(parsed.get("renderer_accent_color")),
            "renderer_neutral_color": self._normalize_color_hex(parsed.get("renderer_neutral_color")),
            "renderer_title_font": str(parsed.get("renderer_title_font") or "").strip(),
            "renderer_heading_font": str(parsed.get("renderer_heading_font") or "").strip(),
            "renderer_body_font": str(parsed.get("renderer_body_font") or "").strip(),
            "renderer_cover_align": str(parsed.get("renderer_cover_align") or "").strip(),
            "renderer_surface_tint_color": self._normalize_color_hex(parsed.get("renderer_surface_tint_color")),
            "renderer_cover_kicker": str(parsed.get("renderer_cover_kicker") or "").strip(),
            "renderer_heading1_color": self._normalize_color_hex(parsed.get("renderer_heading1_color")),
            "renderer_heading2_color": self._normalize_color_hex(parsed.get("renderer_heading2_color")),
            "renderer_heading3_color": self._normalize_color_hex(parsed.get("renderer_heading3_color")),
            "renderer_body_text_color": self._normalize_color_hex(parsed.get("renderer_body_text_color")),
            "renderer_muted_text_color": self._normalize_color_hex(parsed.get("renderer_muted_text_color")),
            "renderer_title_divider_color": self._normalize_color_hex(parsed.get("renderer_title_divider_color")),
            "renderer_section_border_color": self._normalize_color_hex(parsed.get("renderer_section_border_color")),
            "renderer_table_header_fill_color": self._normalize_color_hex(parsed.get("renderer_table_header_fill_color")),
            "renderer_table_label_fill_color": self._normalize_color_hex(parsed.get("renderer_table_label_fill_color")),
            "renderer_cover_fill_color": self._normalize_color_hex(parsed.get("renderer_cover_fill_color")),
            "renderer_kicker_fill_color": self._normalize_color_hex(parsed.get("renderer_kicker_fill_color")),
            "renderer_kicker_text_color": self._normalize_color_hex(parsed.get("renderer_kicker_text_color")),
            "renderer_section_band_fill_color": self._normalize_color_hex(parsed.get("renderer_section_band_fill_color")),
            "renderer_section_panel_fill_color": self._normalize_color_hex(parsed.get("renderer_section_panel_fill_color")),
            "renderer_section_accent_fill_color": self._normalize_color_hex(parsed.get("renderer_section_accent_fill_color")),
            "renderer_overview_label_fill_color": self._normalize_color_hex(parsed.get("renderer_overview_label_fill_color")),
            "renderer_overview_value_fill_color": self._normalize_color_hex(parsed.get("renderer_overview_value_fill_color")),
            "renderer_overview_panel_fill_color": self._normalize_color_hex(parsed.get("renderer_overview_panel_fill_color")),
            "renderer_page_top_margin_inches": str(parsed.get("renderer_page_top_margin_inches") or "").strip(),
            "renderer_page_bottom_margin_inches": str(parsed.get("renderer_page_bottom_margin_inches") or "").strip(),
            "renderer_page_left_margin_inches": str(parsed.get("renderer_page_left_margin_inches") or "").strip(),
            "renderer_page_right_margin_inches": str(parsed.get("renderer_page_right_margin_inches") or "").strip(),
            "renderer_body_line_spacing": str(parsed.get("renderer_body_line_spacing") or "").strip(),
            "renderer_list_line_spacing": str(parsed.get("renderer_list_line_spacing") or "").strip(),
            "renderer_heading2_space_before_pt": str(parsed.get("renderer_heading2_space_before_pt") or "").strip(),
            "renderer_heading2_space_after_pt": str(parsed.get("renderer_heading2_space_after_pt") or "").strip(),
            "renderer_heading3_space_before_pt": str(parsed.get("renderer_heading3_space_before_pt") or "").strip(),
            "renderer_heading3_space_after_pt": str(parsed.get("renderer_heading3_space_after_pt") or "").strip(),
            "renderer_title_space_after_pt": str(parsed.get("renderer_title_space_after_pt") or "").strip(),
            "renderer_title_divider_size": str(parsed.get("renderer_title_divider_size") or "").strip(),
            "renderer_title_divider_space": str(parsed.get("renderer_title_divider_space") or "").strip(),
        }

    def _skill_theme_request_notes(self, skill_state: dict[str, Any] | None) -> list[str]:
        state = dict(skill_state or {})
        intent = dict(state.get("design_intent_packet") or {})
        notes: list[str] = []
        directive_lines = self._skill_instruction_lines(state)
        if str(intent.get("require_brand_research") or "").strip().lower() in {"true", "1", "yes"}:
            notes.append("Perform public web research before choosing the design direction and translate the findings into document cues.")
        if str(intent.get("design_priority") or "").strip().lower() == "strong":
            notes.append("Changing color alone is insufficient; typography, spacing, section treatment, and surface treatment should visibly move away from the base template.")
        if directive_lines:
            notes.extend(f"Binding skill directive: {line}" for line in directive_lines)
        return notes

    def _resolve_renderer_fonts(
        self,
        session: DelegateSession,
        *,
        current_policy: dict[str, Any],
        skill_state: dict[str, Any] | None = None,
    ) -> dict[str, str]:
        policy = dict(current_policy or {})
        missing_keys = [
            key
            for key in ("renderer_title_font", "renderer_heading_font", "renderer_body_font")
            if not str(policy.get(key) or "").strip()
        ]
        if not missing_keys:
            return {}

        state = dict(skill_state or {})
        resolved: dict[str, str] = {}
        if self._codex_ready:
            try:
                external = self._resolve_renderer_fonts_with_codex(
                    session,
                    current_policy=policy,
                    skill_state=state,
                )
            except Exception:
                external = {}
            for key in missing_keys:
                font_name = canonical_font_name(external.get(key))
                if font_name:
                    resolved[key] = font_name
        return resolved

    def _resolve_renderer_fonts_with_codex(
        self,
        session: DelegateSession,
        *,
        current_policy: dict[str, Any],
        skill_state: dict[str, Any] | None = None,
    ) -> dict[str, str]:
        state = dict(skill_state or {})
        raw_skill_block = self._raw_skill_instruction_block(state)
        request_text = (
            "Task: choose practical Korean-friendly document fonts for the meeting summary PDF renderer.\n"
            "If the directives mention a real company, brand, or public visual identity, first use web search to infer stable public-facing cues before choosing fonts.\n"
            "If there is no company or brand, infer the fonts from the full user intent, document tone, and design context alone.\n"
            "Return compact JSON only.\n"
            "The goal is a readable business PDF, not a poster or landing page.\n"
            "Do not leave the font fields empty.\n"
            "Prefer short font family names that can later expand into safe CSS fallback stacks.\n"
            "Prefer families the renderer can load directly as web fonts: SUIT, Pretendard, Noto Sans KR, Noto Serif KR, Nanum Gothic, Nanum Myeongjo, and Spoqa Han Sans Neo.\n"
            "Malgun Gothic and Batang are acceptable only as conservative local fallbacks.\n"
            "If an exact brand font is proprietary or unclear, choose the closest safe public Korean document font instead.\n\n"
            "Required JSON keys:\n"
            "- `renderer_title_font`: short font family name\n"
            "- `renderer_heading_font`: short font family name\n"
            "- `renderer_body_font`: short font family name\n"
            "- `note`: one short Korean sentence summarizing why these fonts fit\n\n"
            "Current renderer hints already set:\n"
            f"{json.dumps({key: current_policy.get(key) for key in ('renderer_theme_name', 'renderer_title_font', 'renderer_heading_font', 'renderer_body_font')}, ensure_ascii=False, indent=2)}\n\n"
            "Existing design direction already resolved:\n"
            f"{json.dumps({key: current_policy.get(key) for key in ('renderer_primary_color', 'renderer_accent_color', 'renderer_neutral_color', 'renderer_surface_tint_color', 'renderer_cover_fill_color', 'renderer_cover_kicker', 'renderer_cover_align')}, ensure_ascii=False, indent=2)}\n\n"
            "Binding skill directives:\n"
            f"{raw_skill_block or '(none)'}"
        )
        parsed = self._codex_json_response(
            session,
            request_text,
            schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "renderer_title_font": {"type": "string"},
                    "renderer_heading_font": {"type": "string"},
                    "renderer_body_font": {"type": "string"},
                    "note": {"type": "string"},
                },
                "required": [
                    "renderer_title_font",
                    "renderer_heading_font",
                    "renderer_body_font",
                    "note",
                ],
            },
            timeout_seconds=max(self._timeout, 90),
        )
        return {
            "renderer_title_font": str(parsed.get("renderer_title_font") or "").strip(),
            "renderer_heading_font": str(parsed.get("renderer_heading_font") or "").strip(),
            "renderer_body_font": str(parsed.get("renderer_body_font") or "").strip(),
        }

    def _clean_optional_string_list(self, value: Any) -> list[str]:
        items = value if isinstance(value, list) else []
        cleaned: list[str] = []
        for item in items:
            text = str(item or "").strip()
            if text:
                cleaned.append(text)
        return cleaned

    def _clean_result_generation_requests(self, value: Any) -> list[dict[str, Any]]:
        requests: list[dict[str, Any]] = []
        for item in list(value or []):
            if not isinstance(item, dict):
                continue
            kind = str(item.get("kind") or "").strip()
            title = str(item.get("title") or "").strip()
            instruction = str(item.get("instruction") or "").strip()
            if not kind or not title or not instruction:
                continue
            requests.append(
                {
                    "kind": kind,
                    "title": title,
                    "instruction": instruction,
                    "prompt": str(item.get("prompt") or "").strip(),
                    "tool_hint": str(item.get("tool_hint") or "").strip(),
                    "caption": str(item.get("caption") or "").strip(),
                    "image_path": str(item.get("image_path") or "").strip(),
                    "count": str(self._coerce_positive_count(item.get("count"), default=1)),
                    "placement_notes": str(item.get("placement_notes") or ""),
                    "target_heading": str(item.get("target_heading") or ""),
                    "agenda_context": str(item.get("agenda_context") or "").strip(),
                    "block_focus": str(item.get("block_focus") or "").strip(),
                    "core_message": str(item.get("core_message") or "").strip(),
                    "visual_archetype": str(item.get("visual_archetype") or "").strip(),
                    "visual_center": str(item.get("visual_center") or "").strip(),
                    "composition_notes": str(item.get("composition_notes") or "").strip(),
                    "style_notes": str(item.get("style_notes") or "").strip(),
                    "review_feedback": str(item.get("review_feedback") or "").strip(),
                    "review_status": str(item.get("review_status") or "").strip(),
                    "review_note": str(item.get("review_note") or "").strip(),
                    "key_entities": self._clean_optional_string_list(item.get("key_entities")),
                    "key_relationships": self._clean_optional_string_list(item.get("key_relationships")),
                    "must_include_labels": self._clean_optional_string_list(item.get("must_include_labels")),
                    "avoid_elements": self._clean_optional_string_list(item.get("avoid_elements")),
                }
            )
        return requests

    def _result_image_context_packet(
        self,
        *,
        title: str,
        request: dict[str, Any],
        context_result: dict[str, Any],
    ) -> dict[str, Any]:
        sections: list[dict[str, Any]] = []
        matched_section: dict[str, Any] | None = None
        target_heading = str(request.get("target_heading") or "").strip()
        for item in list(context_result.get("sections") or []):
            if not isinstance(item, dict):
                continue
            heading = str(item.get("heading") or "").strip()
            summary = str(item.get("summary") or "").strip()
            section_payload = {
                "heading": heading,
                "summary": summary,
                "timestamp_refs": list(item.get("timestamp_refs") or []),
            }
            sections.append(section_payload)
            if target_heading and heading == target_heading and matched_section is None:
                matched_section = section_payload
        return {
            "meeting_title": str(title or context_result.get("title") or "").strip(),
            "meeting_summary": str(
                context_result.get("executive_summary")
                or context_result.get("summary")
                or ""
            ).strip(),
            "requested_visual": {
                "title": str(request.get("title") or "").strip(),
                "instruction": str(request.get("instruction") or "").strip(),
                "placement_notes": str(request.get("placement_notes") or "").strip(),
                "target_heading": target_heading,
                "caption": str(request.get("caption") or "").strip(),
                "prompt": str(request.get("prompt") or "").strip(),
            },
            "target_section": matched_section or {},
            "all_sections": sections,
        }

    def _format_result_image_list(self, value: Any) -> str:
        items = self._clean_optional_string_list(value)
        if not items:
            return ""
        return "\n".join(f"- {item}" for item in items)

    def _build_result_image_structure_plan(
        self,
        session: DelegateSession,
        *,
        title: str,
        request: dict[str, Any],
        context_result: dict[str, Any],
    ) -> dict[str, Any]:
        if not self._codex_ready:
            return {}
        state = dict(session.ai_state.get("meeting_output_skill") or {})
        raw_skill_block = self._raw_skill_instruction_block(state)
        request_text = (
            "Task: understand the whole meeting first, then create one structured visual plan for the requested image.\n"
            "Do not use topic-specific presets, keyword hacks, or narrow heuristics.\n"
            "Ground the plan in the meeting as a whole and explain how the requested block fits inside that whole.\n"
            "The plan should help a downstream image model create a meaningfully relevant visual instead of a generic scene.\n"
            "Choose the visual structure that best explains the content.\n"
            "Prefer diagrams, maps, flows, comparison structures, responsibility structures, or layered concept frames when those better match the meaning than a scenic illustration.\n"
            "If short text labels inside the image would improve comprehension, keep them short, natural, and clean in Korean.\n"
            "Required JSON keys:\n"
            "- `agenda_context`: short Korean explanation of the meeting-wide context\n"
            "- `block_focus`: short Korean explanation of what this target block contributes inside the whole meeting\n"
            "- `core_message`: one Korean sentence describing the single message the image must communicate\n"
            "- `visual_archetype`: one of `comparison_diagram`, `process_flow`, `responsibility_map`, `system_map`, `layered_framework`, `decision_structure`, `timeline_map`, `feedback_loop`, `signal_matrix`, `concept_frame`\n"
            "- `visual_center`: Korean phrase describing the visual center or anchor structure\n"
            "- `key_entities`: array of short Korean entity labels\n"
            "- `key_relationships`: array of short Korean relationship descriptions\n"
            "- `must_include_labels`: array of short Korean labels that may appear inside the image when needed\n"
            "- `avoid_elements`: array of things the final image should avoid\n"
            "- `composition_notes`: short Korean composition guidance\n"
            "- `style_notes`: short Korean style guidance\n\n"
            + ("Binding skill directives:\n" + raw_skill_block + "\n\n" if raw_skill_block else "")
            + "Meeting context:\n"
            + json.dumps(
                self._result_image_context_packet(
                    title=title,
                    request=request,
                    context_result=context_result,
                ),
                ensure_ascii=False,
                indent=2,
            )
        )
        parsed = self._codex_json_response(
            session,
            request_text,
            schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "agenda_context": {"type": "string"},
                    "block_focus": {"type": "string"},
                    "core_message": {"type": "string"},
                    "visual_archetype": {
                        "type": "string",
                        "enum": [
                            "comparison_diagram",
                            "process_flow",
                            "responsibility_map",
                            "system_map",
                            "layered_framework",
                            "decision_structure",
                            "timeline_map",
                            "feedback_loop",
                            "signal_matrix",
                            "concept_frame",
                        ],
                    },
                    "visual_center": {"type": "string"},
                    "key_entities": {"type": "array", "items": {"type": "string"}},
                    "key_relationships": {"type": "array", "items": {"type": "string"}},
                    "must_include_labels": {"type": "array", "items": {"type": "string"}},
                    "avoid_elements": {"type": "array", "items": {"type": "string"}},
                    "composition_notes": {"type": "string"},
                    "style_notes": {"type": "string"},
                },
                "required": [
                    "agenda_context",
                    "block_focus",
                    "core_message",
                    "visual_archetype",
                    "visual_center",
                    "key_entities",
                    "key_relationships",
                    "must_include_labels",
                    "avoid_elements",
                    "composition_notes",
                    "style_notes",
                ],
            },
            timeout_seconds=max(self._timeout, min(self._summary_timeout, self._result_image_review_timeout)),
            reuse_thread=False,
        )
        return {
            "agenda_context": str(parsed.get("agenda_context") or "").strip(),
            "block_focus": str(parsed.get("block_focus") or "").strip(),
            "core_message": str(parsed.get("core_message") or "").strip(),
            "visual_archetype": str(parsed.get("visual_archetype") or "").strip(),
            "visual_center": str(parsed.get("visual_center") or "").strip(),
            "composition_notes": str(parsed.get("composition_notes") or "").strip(),
            "style_notes": str(parsed.get("style_notes") or "").strip(),
            "key_entities": self._clean_optional_string_list(parsed.get("key_entities")),
            "key_relationships": self._clean_optional_string_list(parsed.get("key_relationships")),
            "must_include_labels": self._clean_optional_string_list(parsed.get("must_include_labels")),
            "avoid_elements": self._clean_optional_string_list(parsed.get("avoid_elements")),
        }

    async def _materialize_result_generation_requests(
        self,
        session: DelegateSession,
        requests: list[dict[str, Any]],
        *,
        output_dir: Path,
        title: str,
        context_result: dict[str, Any],
        progress_callback: Callable[[str, str, str | None], None] | None = None,
        timing_entries: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        visuals_dir = output_dir / "visuals"
        visuals_dir.mkdir(parents=True, exist_ok=True)
        policy = dict(
            dict(session.ai_state.get("meeting_output_skill") or {}).get("result_generation_policy") or {}
        )
        errors: list[dict[str, str]] = []
        materialized_slots: list[list[dict[str, Any]] | None] = [None] * len(requests)
        image_request_total = len(
            [
                item
                for item in requests
                if str(item.get("kind") or "").strip().lower() == "image_brief"
                and not str(item.get("image_path") or "").strip()
            ]
        )
        image_request_index = 0
        image_jobs: list[dict[str, Any]] = []
        for request_index, item in enumerate(requests):
            image_path = str(item.get("image_path") or "").strip()
            count = self._coerce_positive_count(item.get("count"), default=1)
            should_generate_image = (
                str(item.get("kind") or "").strip().lower() == "image_brief"
                and not image_path
            )
            if not should_generate_image:
                materialized_slots[request_index] = [dict(item)]
                continue
            image_request_index += 1
            image_jobs.append(
                {
                    "request_index": request_index,
                    "image_request_index": image_request_index,
                    "item": dict(item),
                    "count": count,
                }
            )
        if image_jobs:
            semaphore = asyncio.Semaphore(self._result_image_max_concurrency)

            async def run_image_job(job: dict[str, Any]) -> dict[str, Any]:
                async with semaphore:
                    return await self._materialize_result_generation_image_request(
                        session,
                        request=job["item"],
                        request_index=int(job["request_index"]),
                        image_request_index=int(job["image_request_index"]),
                        image_request_total=image_request_total,
                        count=int(job["count"]),
                        output_dir=output_dir,
                        visuals_dir=visuals_dir,
                        title=title,
                        context_result=context_result,
                        rendering_policy=policy,
                        progress_callback=progress_callback,
                    )

            task_results = await asyncio.gather(
                *(asyncio.create_task(run_image_job(job)) for job in image_jobs)
            )
            for task_result in sorted(task_results, key=lambda item: int(item["request_index"])):
                materialized_slots[int(task_result["request_index"])] = list(task_result["materialized"])
                errors.extend(list(task_result["errors"]))
                if timing_entries is not None:
                    timing_entries.append(dict(task_result["timing"]))
        if errors:
            session.ai_state["result_generation_errors"] = errors
        materialized: list[dict[str, Any]] = []
        for slot in materialized_slots:
            if slot:
                materialized.extend(slot)
        return materialized

    async def _materialize_result_generation_image_request(
        self,
        session: DelegateSession,
        *,
        request: dict[str, Any],
        request_index: int,
        image_request_index: int,
        image_request_total: int,
        count: int,
        output_dir: Path,
        visuals_dir: Path,
        title: str,
        context_result: dict[str, Any],
        rendering_policy: dict[str, Any],
        progress_callback: Callable[[str, str, str | None], None] | None = None,
    ) -> dict[str, Any]:
        request_started_at = self._now_iso()
        request_perf = time.perf_counter()
        request_timing: dict[str, Any] = {
            "title": str(request.get("title") or "").strip(),
            "target_heading": str(request.get("target_heading") or "").strip(),
            "requested_count": count,
            "started_at": request_started_at,
            "attempts": [],
        }
        if progress_callback is not None:
            title_text = str(request.get("title") or "").strip()
            detail = f"{image_request_index}/{image_request_total}번째 이미지를 준비하고 있습니다."
            if title_text:
                detail = f"{detail} ({title_text})"
            progress_callback(
                "generating_images",
                "이미지를 만드는 중입니다.",
                detail,
            )
        preparation_perf = time.perf_counter()
        prepared_request = self._enrich_result_image_request(
            session,
            title=title,
            request=dict(request),
            context_result=context_result,
        )
        request_timing["preparation_seconds"] = round(max(time.perf_counter() - preparation_perf, 0.0), 3)
        approved_paths: list[Path] = []
        review_failures: list[dict[str, str]] = []
        review_feedback = str(prepared_request.get("review_feedback") or "").strip()
        last_generation_error = ""
        for attempt_index in range(self._result_image_review_attempts):
            attempt_started_at = self._now_iso()
            attempt_timing: dict[str, Any] = {
                "attempt_index": attempt_index + 1,
                "started_at": attempt_started_at,
            }
            attempt_request = dict(prepared_request)
            if review_feedback:
                attempt_request["review_feedback"] = review_feedback
            else:
                attempt_request["review_feedback"] = ""
            generate_perf = time.perf_counter()
            try:
                generated_paths = await self._generate_result_images(
                    session,
                    title=title,
                    request=attempt_request,
                    count=count,
                    output_dir=visuals_dir,
                    rendering_policy=rendering_policy,
                )
                attempt_timing["generate_seconds"] = round(max(time.perf_counter() - generate_perf, 0.0), 3)
                attempt_timing["generated_candidate_count"] = len(generated_paths)
            except Exception as exc:
                attempt_timing["generate_seconds"] = round(max(time.perf_counter() - generate_perf, 0.0), 3)
                attempt_timing["generation_error"] = str(exc).strip() or exc.__class__.__name__
                attempt_timing["finished_at"] = self._now_iso()
                request_timing["attempts"].append(attempt_timing)
                last_generation_error = str(exc).strip() or exc.__class__.__name__
                review_failures = []
                break
            if not generated_paths:
                attempt_timing["review_seconds"] = 0.0
                attempt_timing["approved_count"] = 0
                attempt_timing["review_failure_count"] = 0
                attempt_timing["finished_at"] = self._now_iso()
                request_timing["attempts"].append(attempt_timing)
                review_failures = []
                break
            review_perf = time.perf_counter()
            approved_paths, review_failures = await self._review_result_image_candidates(
                session,
                title=title,
                request=attempt_request,
                context_result=context_result,
                candidate_paths=generated_paths,
            )
            attempt_timing["review_seconds"] = round(max(time.perf_counter() - review_perf, 0.0), 3)
            attempt_timing["approved_count"] = len(approved_paths)
            attempt_timing["review_failure_count"] = len(review_failures)
            attempt_timing["finished_at"] = self._now_iso()
            request_timing["attempts"].append(attempt_timing)
            if approved_paths:
                prepared_request = dict(attempt_request)
                prepared_request["review_status"] = "approved"
                prepared_request["review_note"] = self._summarize_result_image_review_issues(review_failures)
                break
            review_feedback = self._summarize_result_image_review_issues(review_failures)
        final_paths = approved_paths
        if not final_paths:
            note = review_feedback or last_generation_error or "The generated image did not pass review."
            prepared_request["review_status"] = "rejected"
            prepared_request["review_note"] = note
            request_timing["status"] = "rejected"
            request_timing["approved_count"] = 0
            request_timing["review_note"] = note
            request_timing["finished_at"] = self._now_iso()
            request_timing["elapsed_seconds"] = round(max(time.perf_counter() - request_perf, 0.0), 3)
            return {
                "request_index": request_index,
                "materialized": [prepared_request],
                "errors": [
                    {
                        "title": str(request.get("title") or "").strip(),
                        "error": note,
                    }
                ],
                "timing": request_timing,
            }
        final_count = len(final_paths)
        request_timing["status"] = "approved"
        request_timing["approved_count"] = final_count
        request_timing["finished_at"] = self._now_iso()
        request_timing["elapsed_seconds"] = round(max(time.perf_counter() - request_perf, 0.0), 3)
        materialized: list[dict[str, Any]] = []
        for index, path in enumerate(final_paths, start=1):
            cloned = dict(prepared_request)
            # Generated visuals are treated as complete artifacts now.
            # Do not rebuild them into a local table/card PNG shell.
            cloned["image_path"] = self._display_result_path(path, base_dir=output_dir)
            if final_count > 1:
                title_text = str(cloned.get("title") or "").strip()
                caption_text = (
                    str(cloned.get("caption") or "").strip()
                    or str(cloned.get("title") or "").strip()
                )
                cloned["title"] = f"{title_text} {index}/{final_count}".strip()
                cloned["caption"] = f"{caption_text} ({index}/{final_count})".strip()
            materialized.append(cloned)
        return {
            "request_index": request_index,
            "materialized": materialized,
            "errors": [],
            "timing": request_timing,
        }

    def _enrich_result_image_request(
        self,
        session: DelegateSession,
        *,
        title: str,
        request: dict[str, Any],
        context_result: dict[str, Any],
    ) -> dict[str, Any]:
        enriched = dict(request)
        required_plan_keys = (
            "agenda_context",
            "block_focus",
            "core_message",
            "visual_archetype",
            "visual_center",
        )
        has_plan = all(str(enriched.get(key) or "").strip() for key in required_plan_keys)
        if not has_plan or not self._clean_optional_string_list(enriched.get("key_entities")):
            plan = self._build_result_image_structure_plan(
                session,
                title=title,
                request=enriched,
                context_result=context_result,
            )
            if plan:
                enriched.update(plan)
        return enriched

    def _summarize_result_image_review_issues(self, failures: list[dict[str, str]]) -> str:
        notes: list[str] = []
        for failure in failures:
            for key in ("revision_hint", "note"):
                text = str(failure.get(key) or "").strip()
                if text and text not in notes:
                    notes.append(text)
        return " | ".join(notes[:3])

    async def _review_result_image_candidates(
        self,
        session: DelegateSession,
        *,
        title: str,
        request: dict[str, Any],
        context_result: dict[str, Any],
        candidate_paths: list[Path],
    ) -> tuple[list[Path], list[dict[str, str]]]:
        approved_paths: list[Path] = []
        failures: list[dict[str, str]] = []
        for candidate_path in candidate_paths:
            review = await asyncio.to_thread(
                self._review_result_image_candidate_with_codex,
                session,
                title=title,
                request=request,
                context_result=context_result,
                image_path=candidate_path,
            )
            decision = str(review.get("decision") or "").strip().lower()
            if decision == "pass":
                approved_paths.append(candidate_path)
                continue
            failures.append(
                {
                    "path": str(candidate_path),
                    "decision": decision or "reject",
                    "note": str(review.get("note") or "").strip(),
                    "revision_hint": str(review.get("revision_hint") or "").strip(),
                }
            )
        return approved_paths, failures

    def _review_result_image_candidate_with_codex(
        self,
        session: DelegateSession,
        *,
        title: str,
        request: dict[str, Any],
        context_result: dict[str, Any],
        image_path: Path,
    ) -> dict[str, str]:
        request_text = (
            "Task: inspect the local image file directly and decide whether it is good enough to place into the meeting result.\n"
            "You must inspect the actual image file at the given path before answering.\n"
            "Judge whether the image is semantically aligned with the whole meeting and the requested block, not just whether it looks polished.\n"
            "Reject or ask for retry when the image is generic, scenic without explanatory value, off-topic, from the wrong domain, contains broken Korean, contains mixed-language junk, or fails to explain the requested block.\n"
            "Approve only when the image helps a reader understand the block in the context of the whole meeting.\n"
            "Required JSON keys:\n"
            "- `decision`: one of `pass`, `retry`, `reject`\n"
            "- `note`: short Korean judgment note\n"
            "- `revision_hint`: short Korean retry hint, or empty string when not needed\n\n"
            f"Image file path:\n{image_path.resolve()}\n\n"
            "Meeting context:\n"
            + json.dumps(
                self._result_image_context_packet(
                    title=title,
                    request=request,
                    context_result=context_result,
                ),
                ensure_ascii=False,
                indent=2,
            )
            + "\n\nStructured visual plan:\n"
            + json.dumps(
                {
                    "agenda_context": str(request.get("agenda_context") or "").strip(),
                    "block_focus": str(request.get("block_focus") or "").strip(),
                    "core_message": str(request.get("core_message") or "").strip(),
                    "visual_archetype": str(request.get("visual_archetype") or "").strip(),
                    "visual_center": str(request.get("visual_center") or "").strip(),
                    "key_entities": self._clean_optional_string_list(request.get("key_entities")),
                    "key_relationships": self._clean_optional_string_list(request.get("key_relationships")),
                    "must_include_labels": self._clean_optional_string_list(request.get("must_include_labels")),
                    "avoid_elements": self._clean_optional_string_list(request.get("avoid_elements")),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        parsed = self._codex_json_response(
            session,
            request_text,
            schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "decision": {
                        "type": "string",
                        "enum": ["pass", "retry", "reject"],
                    },
                    "note": {"type": "string"},
                    "revision_hint": {"type": "string"},
                },
                "required": ["decision", "note", "revision_hint"],
            },
            timeout_seconds=self._result_image_review_timeout,
            reuse_thread=False,
        )
        return {
            "decision": str(parsed.get("decision") or "").strip(),
            "note": str(parsed.get("note") or "").strip(),
            "revision_hint": str(parsed.get("revision_hint") or "").strip(),
        }

    async def _generate_result_images(
        self,
        session: DelegateSession,
        *,
        title: str,
        request: dict[str, Any],
        count: int,
        output_dir: Path,
        rendering_policy: dict[str, Any],
    ) -> list[Path]:
        if self._result_image_direct_mcp:
            if not self._result_image_mcp_ready:
                raise RuntimeError(
                    "The local nanobanana MCP route is not configured on this PC. "
                    "Image generation is unavailable until the local MCP server is configured."
                )
            try:
                # Keep the request pipeline concurrent, but avoid overlapping direct MCP
                # launches beyond the configured backend capacity.
                async with self._get_result_image_mcp_concurrency_semaphore():
                    return await self._generate_result_images_with_nanobanana_mcp(
                        session,
                        title=title,
                        request=request,
                        count=count,
                        output_dir=output_dir,
                        rendering_policy=rendering_policy,
                    )
            except Exception as exc:
                raise RuntimeError(f"Local nanobanana MCP image generation failed: {exc}") from exc
        if self._codex_ready:
            try:
                return await asyncio.to_thread(
                    self._generate_result_images_with_codex,
                    session,
                    title=title,
                    request=request,
                    count=count,
                    output_dir=output_dir,
                    rendering_policy=rendering_policy,
                )
            except Exception as exc:
                raise RuntimeError(f"Local Codex nanobanana image generation failed: {exc}") from exc
        raise RuntimeError(
            "The local nanobanana MCP route is not configured on this PC. "
            "Image generation is unavailable until the local MCP server is configured."
        )

    def _get_result_image_mcp_concurrency_semaphore(self) -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        if (
            self._result_image_mcp_concurrency_loop is not loop
            or self._result_image_mcp_concurrency_semaphore is None
        ):
            self._result_image_mcp_concurrency_loop = loop
            self._result_image_mcp_concurrency_semaphore = asyncio.Semaphore(
                self._result_image_mcp_max_concurrency
            )
        return self._result_image_mcp_concurrency_semaphore

    def _build_result_image_prompt(
        self,
        session: DelegateSession,
        *,
        title: str,
        request: dict[str, Any],
        rendering_policy: dict[str, Any],
    ) -> str:
        target_heading = str(request.get("target_heading") or "").strip()
        placement_notes = str(request.get("placement_notes") or "").strip()
        caption = str(request.get("caption") or "").strip()
        prompt = str(request.get("prompt") or "").strip()
        instruction = str(request.get("instruction") or "").strip()
        title_text = str(request.get("title") or title or session.meeting_topic or "회의 결과물").strip()
        lines = [f"이미지 제목: {title_text}"]
        if instruction:
            lines.append(f"이미지 목적: {instruction}")
        if target_heading:
            lines.append(f"대상 블록: {target_heading}")
        if caption:
            lines.append(f"캡션 참고: {caption}")
        if placement_notes:
            lines.append(f"배치 참고: {placement_notes}")
        if str(request.get("agenda_context") or "").strip():
            lines.append("")
            lines.append("회의 전체 맥락:")
            lines.append(str(request.get("agenda_context") or "").strip())
        if str(request.get("block_focus") or "").strip():
            lines.append("")
            lines.append("이 블록의 역할:")
            lines.append(str(request.get("block_focus") or "").strip())
        if str(request.get("core_message") or "").strip():
            lines.append("")
            lines.append("핵심 메시지:")
            lines.append(str(request.get("core_message") or "").strip())
        if str(request.get("visual_archetype") or "").strip():
            lines.append(f"권장 시각 구조: {str(request.get('visual_archetype') or '').strip()}")
        if str(request.get("visual_center") or "").strip():
            lines.append(f"시각 중심: {str(request.get('visual_center') or '').strip()}")
        key_entities = self._format_result_image_list(request.get("key_entities"))
        if key_entities:
            lines.append("")
            lines.append("핵심 엔티티:")
            lines.append(key_entities)
        key_relationships = self._format_result_image_list(request.get("key_relationships"))
        if key_relationships:
            lines.append("")
            lines.append("보여줘야 할 관계:")
            lines.append(key_relationships)
        labels = self._format_result_image_list(request.get("must_include_labels"))
        if labels:
            lines.append("")
            lines.append("필요 시 들어갈 짧은 라벨:")
            lines.append(labels)
        avoid_elements = self._format_result_image_list(request.get("avoid_elements"))
        if avoid_elements:
            lines.append("")
            lines.append("피해야 할 요소:")
            lines.append(avoid_elements)
        if str(request.get("composition_notes") or "").strip():
            lines.append("")
            lines.append("구성 메모:")
            lines.append(str(request.get("composition_notes") or "").strip())
        if str(request.get("style_notes") or "").strip():
            lines.append("")
            lines.append("스타일 메모:")
            lines.append(str(request.get("style_notes") or "").strip())
        if prompt:
            lines.append("")
            lines.append("상세 이미지 브리프:")
            lines.append(prompt)
        if str(request.get("review_feedback") or "").strip():
            lines.append("")
            lines.append("이전 시도에서 보완해야 할 점:")
            lines.append(str(request.get("review_feedback") or "").strip())
        return "\n".join(lines)

    def _generate_result_images_with_codex(
        self,
        session: DelegateSession,
        *,
        title: str,
        request: dict[str, Any],
        count: int,
        output_dir: Path,
        rendering_policy: dict[str, Any],
    ) -> list[Path]:
        prompt = self._build_result_image_prompt(
            session,
            title=title,
            request=request,
            rendering_policy=rendering_policy,
        )
        tool_hint = str(request.get("tool_hint") or "").strip() or "nano-banana-2"
        stem = self._slugify_for_filename(
            str(request.get("title") or title or session.meeting_topic or "meeting-visual")
        ) or "meeting-visual"
        requested_paths = [
            (output_dir / f"{stem}-{index}.png").resolve()
            for index in range(1, count + 1)
        ]
        request_text = (
            "Task: use the user's local nanobanana MCP route to create the final supporting image files now.\n"
            "You are running inside the user's own local Codex environment.\n"
            "Your role is execution only.\n"
            "First understand the supplied brief as a whole-meeting visual requirement, then execute it faithfully.\n"
            "Do not reinterpret, summarize, redesign, generalize, or replace the supplied brief.\n"
            "Do not fall back to a generic scenic image, stock-style illustration, lifestyle shot, cityscape, map, poster, exhibition scene, office scene, vehicle scene, wedding, picnic, or any other filler content that is not explicitly demanded by the brief.\n"
            "There are no alternative image backends for this task.\n"
            "Do not use any image tool other than nanobanana.\n"
            "Do not use the OpenAI Images API, do not ask for an OpenAI API key, and do not switch to any remote or substitute image-service fallback.\n"
            "Pass the supplied binding brief to nanobanana as the image-generation instruction.\n"
            "Write the generated image files directly to the exact absolute output paths listed below.\n"
            "Prefer PNG outputs. Create one file per requested path.\n"
            "If the local nanobanana route is unavailable, return `unavailable` instead of pretending success.\n"
            "If the brief asks for Korean labels, keep them short, natural, and clean. No ???, no broken Korean, no markdown markers, no mixed-language junk.\n\n"
            f"Tool hint: {tool_hint}\n\n"
            f"Binding image brief (use this directly, do not rewrite it):\n{prompt}\n\n"
            "Exact output paths:\n"
            + "\n".join(f"- {path}" for path in requested_paths)
            + "\n\nRequired JSON keys:\n"
            "- `status`: one of `ok`, `partial`, `unavailable`\n"
            "- `written_paths`: array of absolute file paths you actually wrote\n"
            "- `note`: short Korean status note\n"
        )
        parsed = self._codex_json_response(
            session,
            request_text,
            schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["ok", "partial", "unavailable"],
                    },
                    "written_paths": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "note": {"type": "string"},
                },
                "required": ["status", "written_paths", "note"],
            },
            timeout_seconds=self._result_image_timeout,
            reuse_thread=False,
        )
        written_paths = self._resolve_written_result_image_paths(
            parsed.get("written_paths"),
            output_dir=output_dir,
            requested_paths=requested_paths,
        )
        if not written_paths and str(parsed.get("status") or "").strip().lower() != "unavailable":
            raise RuntimeError("The local image-generation step returned no written image files.")
        return written_paths

    async def _generate_result_images_with_nanobanana_mcp(
        self,
        session: DelegateSession,
        *,
        title: str,
        request: dict[str, Any],
        count: int,
        output_dir: Path,
        rendering_policy: dict[str, Any],
    ) -> list[Path]:
        if not self._result_image_mcp_server_config or not self._result_image_mcp_ready:
            raise RuntimeError("The local nanobanana MCP route is unavailable.")
        prompt = self._build_result_image_prompt(
            session,
            title=title,
            request=request,
            rendering_policy=rendering_policy,
        )
        stem = self._slugify_for_filename(
            str(request.get("title") or title or session.meeting_topic or "meeting-visual")
        ) or "meeting-visual"
        requested_paths = [
            (output_dir / f"{stem}-{index}.png").resolve()
            for index in range(1, count + 1)
        ]
        output_dir.mkdir(parents=True, exist_ok=True)
        server = dict(self._result_image_mcp_server_config or {})
        env = dict(os.environ)
        env.update({key: str(value) for key, value in dict(server.get("env") or {}).items()})
        server_cwd = str(server.get("cwd") or "").strip() or str(self._codex_workdir)
        server_params = StdioServerParameters(
            command=str(server.get("command") or "").strip(),
            args=[str(item) for item in list(server.get("args") or []) if str(item or "").strip()],
            cwd=server_cwd,
            env=env,
        )
        async with stdio_client(server_params) as (read, write):
            async with ClientSession(read, write) as mcp_session:
                async with asyncio.timeout(max(30.0, min(self._result_image_timeout, 90.0))):
                    await mcp_session.initialize()
                    await self._ensure_result_image_mcp_tool(mcp_session)
                written_paths: list[Path] = []
                for output_path in requested_paths:
                    output_path.parent.mkdir(parents=True, exist_ok=True)
                    result = await mcp_session.call_tool(
                        self._result_image_mcp_tool_name,
                        arguments=self._build_result_image_mcp_arguments(
                            request=request,
                            prompt=prompt,
                            output_path=output_path,
                        ),
                        read_timeout_seconds=timedelta(seconds=max(self._result_image_timeout, 30.0)),
                    )
                    self._raise_for_result_image_mcp_error(result, output_path=output_path)
                    if not output_path.exists():
                        raise RuntimeError(
                            "The local nanobanana MCP call completed without writing the requested image file: "
                            + str(output_path)
                        )
                    written_paths.append(output_path.resolve())
        return written_paths

    async def _ensure_result_image_mcp_tool(self, mcp_session: ClientSession) -> None:
        tools_result = await mcp_session.list_tools()
        available = {
            str(getattr(tool, "name", "") or "").strip()
            for tool in list(getattr(tools_result, "tools", []) or [])
            if str(getattr(tool, "name", "") or "").strip()
        }
        if self._result_image_mcp_tool_name not in available:
            raise RuntimeError(
                f"The local MCP server does not expose the required image tool `{self._result_image_mcp_tool_name}`."
            )

    def _build_result_image_mcp_arguments(
        self,
        *,
        request: dict[str, Any],
        prompt: str,
        output_path: Path,
    ) -> dict[str, Any]:
        return {
            "prompt": prompt,
            "n": 1,
            "mode": "generate",
            "model_tier": self._result_image_mcp_model_tier,
            "resolution": self._result_image_mcp_resolution,
            "output_path": str(output_path.resolve()),
        }

    def _raise_for_result_image_mcp_error(self, result: Any, *, output_path: Path) -> None:
        is_error = bool(getattr(result, "isError", False) or getattr(result, "is_error", False))
        if not is_error:
            return
        structured = getattr(result, "structuredContent", None) or getattr(result, "structured_content", None)
        detail_parts: list[str] = []
        if isinstance(structured, dict):
            for key in ("error", "message", "detail", "note"):
                text = str(structured.get(key) or "").strip()
                if text:
                    detail_parts.append(text)
        content = getattr(result, "content", None) or []
        for item in list(content):
            text = str(getattr(item, "text", "") or "").strip()
            if text:
                detail_parts.append(text)
        detail = " | ".join(detail_parts) or f"generate_image failed for {output_path}"
        raise RuntimeError(detail)

    def _resolve_written_result_image_paths(
        self,
        value: Any,
        *,
        output_dir: Path,
        requested_paths: list[Path],
    ) -> list[Path]:
        allowed_suffixes = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
        resolved: list[Path] = []
        seen: set[str] = set()

        def maybe_add(candidate_value: Any) -> None:
            text = str(candidate_value or "").strip()
            if not text:
                return
            candidate = Path(text).expanduser()
            if not candidate.is_absolute():
                candidate = (output_dir / candidate).resolve()
            else:
                candidate = candidate.resolve()
            try:
                candidate.relative_to(output_dir)
            except ValueError:
                return
            if not candidate.exists() or candidate.suffix.lower() not in allowed_suffixes:
                return
            key = str(candidate).casefold()
            if key in seen:
                return
            seen.add(key)
            resolved.append(candidate)

        for item in list(value or []):
            maybe_add(item)
        for path in requested_paths:
            maybe_add(path)
        return resolved

    def _synthesize_postprocess_requests_from_skill(
        self,
        session: DelegateSession,
        *,
        ai_result: dict[str, Any],
    ) -> list[dict[str, Any]]:
        state = dict(session.ai_state.get("meeting_output_skill") or {})
        raw_skill_block = self._raw_skill_instruction_block(state)
        if not raw_skill_block:
            return []
        if not self._codex_ready:
            return []
        skill_state = dict(state)
        theme_name = str(dict(skill_state.get("result_generation_policy") or {}).get("renderer_theme_name") or "").strip()
        request_text = (
            "Task: decide whether the binding meeting-output skill directives require generated images before final export, "
            "and if so, produce the exact postprocess image requests.\n"
            "Treat every line in the skill block as binding. Do not summarize it away, weaken it, or replace it with a preset.\n"
            "Return an empty `requests` array only when the directives do not require generated visuals.\n"
            "When visuals are required, generate image briefs that are directly tied to the current meeting result and preserve the user's placement intent.\n"
            "First understand the whole meeting and only then decide what each requested visual should explain.\n"
            "If the directives imply a placement in natural language, keep that wording in `placement_notes` and fill `target_heading` whenever the current result gives you a concrete heading anchor.\n"
            "Write `title`, `instruction`, `prompt`, `caption`, and `placement_notes` in natural Korean.\n"
            "Write the `prompt` as a content-centered image brief. Describe what the image should explain, show, connect, or contrast.\n"
            "Also include a structured visual plan so downstream generation can stay semantically grounded.\n"
            "Do not mention PDF, slide, briefing document, poster, report template, memo, or other medium words unless the skill explicitly demands them.\n"
            "Avoid canned English template sentences, generic style prompts, and stock-visual phrasing when Korean instructions can describe the same meaning more directly.\n"
            "If text is needed inside the image, prefer short and exact Korean labels over long slogans or mixed-language fragments.\n"
            "Use `tool_hint` = `nano-banana-2`.\n"
            "Keep `kind` = `image_brief` for generated visuals.\n"
            "Required JSON keys:\n"
            "- `requests`: array of objects with keys `kind`, `title`, `instruction`, `prompt`, `tool_hint`, `caption`, `count`, `placement_notes`, `target_heading`, `agenda_context`, `block_focus`, `core_message`, `visual_archetype`, `visual_center`, `key_entities`, `key_relationships`, `must_include_labels`, `avoid_elements`, `composition_notes`, `style_notes`\n\n"
            "Binding skill directives:\n"
            f"{raw_skill_block}\n\n"
            f"Renderer theme name:\n{theme_name or '(none)'}\n\n"
            "Current meeting result:\n"
            f"{json.dumps({'title': ai_result.get('title'), 'executive_summary': ai_result.get('executive_summary') or ai_result.get('summary'), 'sections': ai_result.get('sections') or []}, ensure_ascii=False, indent=2)}"
        )
        parsed = self._codex_json_response(
            session,
            request_text,
            schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "requests": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "kind": {"type": "string"},
                                "title": {"type": "string"},
                                "instruction": {"type": "string"},
                                "prompt": {"type": "string"},
                                "tool_hint": {"type": "string"},
                                "caption": {"type": "string"},
                                "count": {"type": "string"},
                                "placement_notes": {"type": "string"},
                                "target_heading": {"type": "string"},
                                "agenda_context": {"type": "string"},
                                "block_focus": {"type": "string"},
                                "core_message": {"type": "string"},
                                "visual_archetype": {
                                    "type": "string",
                                    "enum": [
                                        "comparison_diagram",
                                        "process_flow",
                                        "responsibility_map",
                                        "system_map",
                                        "layered_framework",
                                        "decision_structure",
                                        "timeline_map",
                                        "feedback_loop",
                                        "signal_matrix",
                                        "concept_frame",
                                    ],
                                },
                                "visual_center": {"type": "string"},
                                "key_entities": {"type": "array", "items": {"type": "string"}},
                                "key_relationships": {"type": "array", "items": {"type": "string"}},
                                "must_include_labels": {"type": "array", "items": {"type": "string"}},
                                "avoid_elements": {"type": "array", "items": {"type": "string"}},
                                "composition_notes": {"type": "string"},
                                "style_notes": {"type": "string"},
                            },
                            "required": [
                                "kind",
                                "title",
                                "instruction",
                                "prompt",
                                "tool_hint",
                                "caption",
                                "count",
                                "placement_notes",
                                "target_heading",
                                "agenda_context",
                                "block_focus",
                                "core_message",
                                "visual_archetype",
                                "visual_center",
                                "key_entities",
                                "key_relationships",
                                "must_include_labels",
                                "avoid_elements",
                                "composition_notes",
                                "style_notes",
                            ],
                        },
                    }
                },
                "required": ["requests"],
            },
            timeout_seconds=self._summary_timeout,
            reuse_thread=False,
        )
        return self._clean_result_generation_requests(parsed.get("requests"))

    def _skill_visual_guidance_text(self, state: dict[str, Any]) -> str:
        return self._raw_skill_instruction_block(state)

    def _coerce_positive_count(self, value: Any, *, default: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return default
        if parsed <= 0:
            return default
        return min(parsed, self._result_image_max_count)

    def _normalize_color_hex(self, value: Any) -> str:
        text = str(value or "").strip().lstrip("#").upper()
        if len(text) == 3 and all(ch in "0123456789ABCDEF" for ch in text):
            text = "".join(ch * 2 for ch in text)
        if len(text) == 6 and all(ch in "0123456789ABCDEF" for ch in text):
            return text
        return ""

    def _slugify_for_filename(self, value: str) -> str:
        normalized = re.sub(r"\s+", "-", str(value or "").strip().lower())
        normalized = re.sub(r"[^0-9a-z가-힣_-]+", "-", normalized)
        normalized = re.sub(r"-{2,}", "-", normalized).strip("-_")
        return normalized[:48]

    def _display_result_path(self, path: Path, *, base_dir: Path) -> str:
        resolved_path = Path(path).resolve()
        resolved_base_dir = Path(base_dir).resolve()
        try:
            return str(resolved_path.relative_to(resolved_base_dir))
        except ValueError:
            return str(resolved_path)

    def _ensure_generated_meeting_output_skill(self, session: DelegateSession) -> None:
        customization = str(self._meeting_output_customization or "").strip()
        if self._generated_meeting_output_skill:
            source = "configured_override" if self._configured_meeting_output_override else "generated_cached"
            self._record_meeting_output_skill_state(session, self._generated_meeting_output_skill, source=source)
            return
        if not customization:
            self._record_meeting_output_skill_state(session, self._meeting_output_skill, source="base_default")
            return

        output_path = build_generated_meeting_output_skill_path(
            customization,
            output_dir=self._generated_meeting_output_dir,
            base_signature=(
                str(dict(self._meeting_output_skill or {}).get("resolved_path") or "").strip()
                or str(dict(self._meeting_output_skill or {}).get("name") or "").strip()
                or "meeting-output-default"
            ),
        )
        if output_path.exists():
            loaded = load_meeting_output_skill(output_path)
            self._generated_meeting_output_skill = loaded
            self._record_meeting_output_skill_state(session, loaded, source="generated_existing")
            return

        try:
            generated = self._expand_meeting_output_skill(session, customization)
            resolved_path = write_generated_meeting_output_skill(
                output_path=output_path,
                name=str(generated.get("name") or "").strip() or "meeting-output-generated",
                description=str(generated.get("description") or "").strip() or "Generated meeting output customization.",
                body=str(generated.get("body") or "").strip(),
                metadata=dict(generated.get("metadata") or {}),
            )
            loaded = load_meeting_output_skill(resolved_path)
            self._generated_meeting_output_skill = loaded
            self._record_meeting_output_skill_state(session, loaded, source="generated_new")
        except Exception as exc:
            session.ai_state["meeting_output_skill_error"] = str(exc)

    def _expand_meeting_output_skill(self, session: DelegateSession, customization: str) -> dict[str, object]:
        request_text = (
            "Task: convert the user's natural-language result-generation preference into a reusable meeting-output skill override.\n"
            "Write the result in Korean except for the skill `name`, which must be lowercase hyphen-case.\n"
            "The override should complement the base skill, not repeat the whole base skill.\n"
            "Focus on result-generation guidance inside the existing engine boundary.\n"
            "That includes summary emphasis, section strategy, final document block order, examples, what to avoid, and optional post-processing guidance for the generated result.\n"
            "Do not describe runtime internals, schemas, environment variables, or tool setup.\n"
            "Do not mention JSON field names unless the user's request truly depends on them.\n"
            "The user should be able to ask for things like 검토사항, 브리핑 메모, 논의 포인트, 숨김, 강조, 이미지, or 더 단정한 PDF without knowing any internal slot names.\n"
            "Translate that natural-language intent into a reusable skill that changes both display and writing semantics when needed.\n\n"
            "Prefer soft guidance over hard sentence counts, page counts, or fixed quotas unless the user explicitly asks for a firm cap.\n"
            "Required JSON keys:\n"
            "- `name`: lowercase hyphen-case skill name under 64 characters.\n"
            "- `description`: one Korean sentence describing when to use this override skill.\n"
            "- `metadata`: optional string-valued frontmatter overrides. Use this only when the user explicitly cares about final document arrangement or block visibility.\n"
            "  Supported keys include `result_block_order`, `result_block_order_mode`, `renderer_theme_name`, `renderer_primary_color`, `renderer_accent_color`, `renderer_neutral_color`, `renderer_title_font`, `renderer_heading_font`, `renderer_body_font`, `renderer_cover_align`, `renderer_surface_tint_color`, `renderer_cover_kicker`, `renderer_heading1_color`, `renderer_heading2_color`, `renderer_heading3_color`, `renderer_body_text_color`, `renderer_muted_text_color`, `renderer_title_divider_color`, `renderer_section_border_color`, `renderer_table_header_fill_color`, `renderer_table_label_fill_color`, `renderer_cover_fill_color`, `renderer_kicker_fill_color`, `renderer_kicker_text_color`, `renderer_section_band_fill_color`, `renderer_section_panel_fill_color`, `renderer_section_accent_fill_color`, `renderer_overview_label_fill_color`, `renderer_overview_value_fill_color`, `renderer_overview_panel_fill_color`, `renderer_page_top_margin_inches`, `renderer_page_bottom_margin_inches`, `renderer_page_left_margin_inches`, `renderer_page_right_margin_inches`, `renderer_body_line_spacing`, `renderer_list_line_spacing`, `renderer_heading2_space_before_pt`, `renderer_heading2_space_after_pt`, `renderer_heading3_space_before_pt`, `renderer_heading3_space_after_pt`, `renderer_title_space_after_pt`, `renderer_title_divider_size`, `renderer_title_divider_space`, `postprocess_image_width_inches`, `show_title`, `show_overview`, `show_executive_summary`, `show_sections`, `show_decisions`, `show_action_items`, `show_open_questions`, `show_risk_signals`, `show_postprocess_requests`, `show_memo`, `show_overview_datetime`, `show_overview_author`, `show_overview_session_id`, `show_overview_participants`, `max_display_sections`, `max_decisions`, `max_action_items`, `max_open_questions`, `max_risk_signals`, `max_postprocess_requests`, `section_numbering`, and user-facing heading text keys already used by the base skill.\n"
            "  `제기자`, `주요 화자`, `타임스탬프` trace fields are core system output and must not be changed, hidden, renamed, or reinterpreted by the skill.\n"
            "  `result_block_order` is a comma-separated order chosen from `overview`, `executive_summary`, `sections`, `decisions`, `action_items`, `open_questions`, `risk_signals`, `postprocess_requests`, `memo`.\n"
            "  `result_block_order_mode` may be `append_missing` for safe defaults or `exact` when the user wants only the listed blocks.\n"
            "  Use concrete colors, fonts, cover treatment, section treatment, overview treatment, and placement guidance when the user wants a distinct visual identity; do not collapse the request into a tiny preset menu.\n"
            "  Every `show_*` value must be one of `always`, `auto`, `never`.\n"
            "  `section_numbering` may be `numbered` or `plain`.\n"
            "  The `max_*` values must be positive integers written as strings.\n"
            "  Omit `max_*` keys unless the user explicitly wants a hard upper bound.\n"
            "  You may also include freeform semantic guidance keys such as `decisions_role_guidance`, `open_questions_role_guidance`, `executive_summary_style_guidance`, `sections_style_guidance`, or `postprocess_style_guidance` when the user is redefining what those blocks should mean.\n"
            "- `body`: Markdown body for a reusable skill override. Keep it concise, practical, and user-facing.\n"
            "  If the user wants a block to behave differently, explain that semantic change in the body.\n"
            "  Example: if the displayed heading becomes `검토사항`, explain that the internal decisions slot should collect review items, pending evaluations, or points still being weighed rather than finalized conclusions.\n"
            "  If the user wants image follow-up, visual appendix ideas, or renderer-specific result polishing, explain that post-processing behavior in the body as well.\n\n"
            "If the user mentions a company or brand feel, first perform web search on that brand or public visual identity and then convert the findings into concrete renderer guidance.\n"
            "Do not rely on prior model memory alone for that brand interpretation.\n"
            "Do that work silently. The user should only receive the resulting reusable skill, not the implementation details.\n\n"
            "Base skill:\n"
            f"{self._meeting_output_skill_prompt_block(include_generated=False)}"
            "User customization request:\n"
            f"{customization}\n"
        )
        parsed = self._codex_json_response(
            session,
            request_text,
            schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "metadata": {
                        "type": "object",
                        "additionalProperties": {"type": "string"},
                    },
                    "body": {"type": "string"},
                },
                "required": ["name", "description", "body"],
            },
            timeout_seconds=max(self._timeout, self._summary_timeout),
        )
        name = re.sub(r"[^a-z0-9-]+", "-", str(parsed.get("name") or "").strip().lower())
        name = re.sub(r"-{2,}", "-", name).strip("-")[:63] or "meeting-output-generated"
        description = str(parsed.get("description") or "").strip()
        body = str(parsed.get("body") or "").strip()
        metadata = {
            str(key).strip(): str(value).strip()
            for key, value in dict(parsed.get("metadata") or {}).items()
            if str(key).strip() and str(value).strip()
        }
        if not description or not body:
            raise RuntimeError("Generated meeting-output skill was incomplete.")
        return {
            "name": name,
            "description": description,
            "metadata": metadata,
            "body": body,
        }

    def _record_meeting_output_skill_state(
        self,
        session: DelegateSession,
        skill: dict[str, object],
        *,
        source: str,
    ) -> None:
        effective_policy = resolve_result_generation_policy(
            self._meeting_output_skill,
            self._generated_meeting_output_skill,
        )
        design_state = self._design_agent.resolve(
            active_skill=skill,
            current_policy=effective_policy,
            source=source,
        )
        effective_policy = dict(design_state.get("resolved_policy") or effective_policy)
        session.ai_state["meeting_output_skill"] = {
            "source": source,
            "path": str(skill.get("resolved_path") or skill.get("path") or "").strip() or None,
            "name": str(skill.get("name") or "").strip() or None,
            "description": str(skill.get("description") or "").strip() or None,
            "metadata": dict(skill.get("metadata") or {}),
            "body": str(skill.get("body") or ""),
            "design_intent_packet": dict(design_state.get("intent_packet") or {}),
            "result_generation_policy": effective_policy,
        }

    def _meeting_output_skill_prompt_block(self, *, include_generated: bool = True) -> str:
        lines: list[str] = []
        base_block = self._one_skill_prompt_block(
            label="Base result-generation skill",
            skill=self._meeting_output_skill,
            fallback_body=self._legacy_meeting_output_guidance(),
        )
        if base_block:
            lines.append(base_block)
        if include_generated:
            generated_block = self._generated_meeting_output_prompt_block()
            if generated_block:
                lines.append(generated_block)
            elif str(self._meeting_output_customization or "").strip():
                lines.append(
                    "User result-generation intent:\n"
                    "- The following natural-language preference could not be expanded into a generated skill file yet.\n\n"
                    + str(self._meeting_output_customization or "").strip()
                )
        if not lines:
            return ""
        return "\n\n".join(lines) + "\n\n"

    def _meeting_output_summary_skill_prompt_block(self) -> str:
        lines: list[str] = []
        base_block = self._one_skill_prompt_block(
            label="Base result-generation skill",
            skill=self._summary_stage_skill_state(self._meeting_output_skill),
            fallback_body=(
                "# Meeting Output Summary Fallback\n\n"
                "- Stay inside the fixed engine and JSON schema.\n"
                "- Treat the JSON keys as internal transport slots rather than rigid user-facing labels.\n"
                "- Write polished Korean meeting output grounded in the actual session.\n"
                "- Preserve concrete details when the meeting supports them.\n"
            ),
        )
        if base_block:
            lines.append(base_block)
        generated_block = self._one_skill_prompt_block(
            label="Generated result-generation skill",
            skill=self._summary_stage_skill_state(self._generated_meeting_output_skill),
        )
        if generated_block:
            lines.append(generated_block)
        elif str(self._meeting_output_customization or "").strip():
            filtered_customization = self._strip_visual_postprocess_guidance(
                str(self._meeting_output_customization or "").strip()
            )
            if filtered_customization:
                lines.append(
                    "User result-generation intent:\n"
                    "- The following natural-language preference could not be expanded into a generated skill file yet.\n\n"
                    + filtered_customization
                )
        if not lines:
            return ""
        return "\n\n".join(lines) + "\n\n"

    def _generated_meeting_output_prompt_block(self) -> str:
        if not self._generated_meeting_output_skill:
            return ""
        return self._one_skill_prompt_block(
            label="Generated result-generation skill",
            skill=self._generated_meeting_output_skill,
        )

    def _one_skill_prompt_block(
        self,
        *,
        label: str,
        skill: dict[str, str] | None,
        fallback_body: str = "",
    ) -> str:
        body = str(dict(skill or {}).get("body") or "").strip() or str(fallback_body or "").strip()
        if not body:
            return ""
        name = str(dict(skill or {}).get("name") or "meeting-output-default").strip()
        resolved_path = str(dict(skill or {}).get("resolved_path") or dict(skill or {}).get("path") or "").strip()
        lines = [
            f"{label}:",
            f"- skill name: {name}",
        ]
        if resolved_path:
            lines.append(f"- skill path: {resolved_path}")
        metadata = {
            str(key).strip(): str(value).strip()
            for key, value in dict(skill or {}).get("metadata", {}).items()
            if str(key).strip() and str(value).strip()
        }
        if metadata:
            lines.append("- skill frontmatter guidance:")
            for key, value in metadata.items():
                lines.append(f"  - {key}: {value}")
        lines.append("- Apply the following reusable result-generation guidance unless it conflicts with the fixed JSON schema.")
        lines.append("")
        lines.append(body)
        return "\n".join(lines)

    def _meeting_output_skill_body(self) -> str:
        body = str(dict(self._meeting_output_skill or {}).get("body") or "").strip()
        if body:
            return body
        return self._legacy_meeting_output_guidance()

    def _legacy_meeting_output_guidance(self) -> str:
        return (
            "# Meeting Output Legacy Fallback\n\n"
            "Use this fallback only when the configured meeting-output skill file could not be loaded.\n\n"
            "- Stay inside the fixed engine and JSON schema.\n"
            "- Treat the JSON keys as internal transport slots rather than rigid user-facing labels.\n"
            "- Write polished Korean meeting output grounded in the actual session.\n"
            "- Preserve concrete details when the meeting supports them.\n"
            "- Use optional `postprocess_requests` only for real downstream result work such as image briefs, renderer polish, or appendix-style follow-up.\n"
        )

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
        reuse_thread: bool = True,
    ) -> dict[str, Any]:
        if not self._codex_ready:
            raise RuntimeError("codex exec is not available.")
        thread_id = (str(session.ai_state.get("codex_thread_id") or "").strip() or None) if reuse_thread else None
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
                    encoding="utf-8",
                    errors="replace",
                    check=False,
                    cwd=str(self._codex_workdir),
                    timeout=timeout_seconds or self._timeout,
                )
                if completed.returncode != 0:
                    last_error = completed.stderr.strip() or completed.stdout.strip() or f"exit code {completed.returncode}"
                    continue
                if reuse_thread:
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
            if schema_path is not None and self._resume_supports_output_schema():
                command.extend(["--output-schema", str(schema_path)])
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

    def _resume_supports_output_schema(self) -> bool:
        if self._codex_resume_supports_output_schema is not None:
            return self._codex_resume_supports_output_schema
        if not self._codex_path:
            self._codex_resume_supports_output_schema = False
            return False
        command = [str(self._codex_path), "-C", str(self._codex_workdir), "exec", "resume", "--help"]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                cwd=str(self._codex_workdir),
                timeout=min(self._timeout, 10.0),
            )
            help_text = "\n".join(
                part for part in [completed.stdout, completed.stderr] if str(part or "").strip()
            )
            self._codex_resume_supports_output_schema = "--output-schema" in help_text
        except Exception:
            self._codex_resume_supports_output_schema = False
        return self._codex_resume_supports_output_schema

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

    @property
    def _result_image_mcp_ready(self) -> bool:
        return bool(
            self._result_image_mcp_server_config
            and self._result_image_direct_mcp
            and ClientSession is not None
            and StdioServerParameters is not None
            and stdio_client is not None
        )

    def _resolve_result_image_mcp_server_config(self) -> dict[str, Any] | None:
        command_override = os.getenv("DELEGATE_RESULT_IMAGE_MCP_COMMAND", "").strip()
        if command_override:
            return {
                "command": command_override,
                "args": self._parse_result_image_mcp_args(os.getenv("DELEGATE_RESULT_IMAGE_MCP_ARGS", "").strip()),
                "env": self._parse_result_image_mcp_env(os.getenv("DELEGATE_RESULT_IMAGE_MCP_ENV", "").strip()),
                "cwd": str(os.getenv("DELEGATE_RESULT_IMAGE_MCP_CWD", "").strip() or self._codex_workdir),
                "source": "env_override",
            }
        resolved = self._resolve_result_image_mcp_server_from_codex_config()
        if resolved:
            return resolved
        return self._resolve_result_image_mcp_server_from_cursor_config()

    def _resolve_result_image_mcp_server_from_codex_config(self) -> dict[str, Any] | None:
        config_path = Path.home() / ".codex" / "config.toml"
        if tomllib is None or not config_path.exists():
            return None
        try:
            payload = tomllib.loads(config_path.read_text(encoding="utf-8"))
        except Exception:
            return None
        servers = payload.get("mcp_servers")
        if not isinstance(servers, dict):
            return None
        entry = servers.get(self._result_image_mcp_server_name)
        return self._normalize_result_image_mcp_server_entry(
            entry,
            source=f"codex:{self._result_image_mcp_server_name}",
            base_dir=config_path.parent,
        )

    def _resolve_result_image_mcp_server_from_cursor_config(self) -> dict[str, Any] | None:
        config_path = Path.home() / ".cursor" / "mcp.json"
        if not config_path.exists():
            return None
        try:
            payload = json.loads(config_path.read_text(encoding="utf-8"))
        except Exception:
            return None
        servers = payload.get("mcpServers")
        if not isinstance(servers, dict):
            return None
        entry = servers.get(self._result_image_mcp_server_name)
        return self._normalize_result_image_mcp_server_entry(
            entry,
            source=f"cursor:{self._result_image_mcp_server_name}",
            base_dir=config_path.parent,
        )

    def _normalize_result_image_mcp_server_entry(
        self,
        entry: Any,
        *,
        source: str,
        base_dir: Path,
    ) -> dict[str, Any] | None:
        if not isinstance(entry, dict):
            return None
        if entry.get("enabled") is False:
            return None
        command = str(entry.get("command") or "").strip()
        if not command:
            return None
        raw_args = entry.get("args")
        args = [str(item) for item in list(raw_args or []) if str(item or "").strip()]
        raw_env = entry.get("env")
        env = {
            str(key).strip(): str(value)
            for key, value in dict(raw_env or {}).items()
            if str(key).strip() and value is not None
        }
        cwd_text = str(entry.get("cwd") or "").strip()
        cwd = ""
        if cwd_text:
            cwd = str((base_dir / cwd_text).resolve()) if not Path(cwd_text).is_absolute() else str(Path(cwd_text))
        return {
            "command": command,
            "args": args,
            "env": env,
            "cwd": cwd or str(self._codex_workdir),
            "source": source,
        }

    def _ensure_performance_timing_root(self, session: DelegateSession) -> dict[str, Any]:
        timing_root = dict(session.ai_state.get("performance_timing") or {})
        session.ai_state["performance_timing"] = timing_root
        return timing_root

    def _begin_result_generation_timing(
        self,
        session: DelegateSession,
        *,
        started_at: str,
        request_count: int,
        non_image_request_count: int,
    ) -> dict[str, Any]:
        timing_root = self._ensure_performance_timing_root(session)
        bucket: dict[str, Any] = {
            "started_at": started_at,
            "status": "running",
            "request_count": request_count,
            "non_image_request_count": non_image_request_count,
            "requests": [],
            "updated_at": started_at,
        }
        timing_root["result_generation"] = bucket
        return bucket

    def _now_iso(self) -> str:
        return utcnow_iso()

    def _parse_result_image_mcp_args(self, raw_text: str) -> list[str]:
        text = str(raw_text or "").strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return [item for item in text.split() if item.strip()]
        if isinstance(parsed, list):
            return [str(item) for item in parsed if str(item or "").strip()]
        return [str(parsed).strip()] if str(parsed).strip() else []

    def _parse_result_image_mcp_env(self, raw_text: str) -> dict[str, str]:
        text = str(raw_text or "").strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return {}
        if not isinstance(parsed, dict):
            return {}
        return {
            str(key).strip(): str(value)
            for key, value in parsed.items()
            if str(key).strip() and value is not None
        }

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
            encoding="utf-8",
            errors="replace",
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
        return sys.platform in {"darwin", "win32"}

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
