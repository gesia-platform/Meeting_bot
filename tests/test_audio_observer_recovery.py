from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import soundfile as sf


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from local_meeting_ai_runtime.models import DelegateSession
from local_meeting_ai_runtime.models import utcnow_iso
from local_meeting_ai_runtime.service import DelegateService
from local_meeting_ai_runtime.storage import RunnerQueueStore, SessionStore


class _DummyAiClient:
    def quality_readiness(self) -> dict[str, object]:
        return {
            "blocking_reasons": [],
            "provider": "faster_whisper_cuda",
            "model": "large-v3",
            "gpu_ready": True,
            "faster_whisper_ready": True,
            "pyannote_ready": False,
            "diarization_provider": "",
        }

    def release_quality_runtime_resources(self) -> None:
        return


class AudioObserverRecoveryTest(unittest.IsolatedAsyncioTestCase):
    def _build_service(self, temp_dir: str) -> DelegateService:
        base = Path(temp_dir)
        store = SessionStore(path=str(base / "delegate_sessions.json"))
        runner_store = RunnerQueueStore(path=str(base / "runner_queue.json"))
        local_observer = mock.Mock()
        local_observer.audio_quality_readiness.return_value = {
            "blocking_reasons": [],
            "microphone_device_ready": True,
            "meeting_output_device_ready": True,
            "configured_meeting_output_device": "Test Speaker",
        }
        local_observer.meeting_output_device_name = "Test Speaker"
        local_observer._artifact_dir = str(base / ".tmp" / "local-observer")
        return DelegateService(
            store=store,
            runner_store=runner_store,
            zoom_client=mock.Mock(),
            ai_client=_DummyAiClient(),
            meeting_adapter=mock.Mock(),
            local_observer=local_observer,
            summary_pipeline=mock.Mock(),
            artifact_exporter=mock.Mock(),
            export_dir=base / "exports",
        )

    def _write_wav(self, path: Path, *, seconds: float, sample_rate: int = 16000, channels: int = 1) -> None:
        frames = max(int(sample_rate * seconds), 1)
        audio = np.zeros((frames,), dtype="float32")
        if channels == 2:
            audio = np.column_stack([audio, audio]).astype("float32")
        path.parent.mkdir(parents=True, exist_ok=True)
        sf.write(str(path), audio, sample_rate, format="WAV")

    def _create_orphan_helper_output(self, service: DelegateService, session_id: str) -> tuple[Path, Path]:
        helper_dir = Path(service._observer._artifact_dir) / f"windows-audio-helper-{session_id}"
        microphone_path = helper_dir / "microphone-full-track.wav"
        system_path = helper_dir / "system-full-track.wav"
        self._write_wav(microphone_path, seconds=1.25)
        self._write_wav(system_path, seconds=1.25)
        return microphone_path, system_path

    async def test_recover_live_audio_observers_salvages_orphan_segment_and_restarts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self._build_service(temp_dir)
            session = DelegateSession(
                session_id="recovery-session",
                delegate_mode="answer_on_ask",
                bot_display_name="WooBIN_bot",
                status="active",
                ai_state={
                    "shell_liveness": {
                        "last_heartbeat_at": utcnow_iso(),
                        "joined": True,
                        "completion_armed": True,
                        "audio_observer_running": True,
                    },
                    "audio_observer": {
                        "running": True,
                        "continuous_mode": True,
                        "capture_backend": "windows_native_helper",
                        "restart_payload": {
                            "seconds": 12.0,
                            "interval_ms": 250,
                            "audio_mode": "conversation",
                            "metadata": {"capture_profile": "continuous_audio_observer"},
                            "system_device_name": "Test Speaker",
                            "meeting_output_device_name": "Test Speaker",
                        },
                    },
                    "full_track_capture": {
                        "running": False,
                        "strategy": "windows_native_full_track",
                        "chunks": 0,
                    },
                    "full_track_capture_baseline": "2026-04-22T11:10:01+09:00",
                },
            )
            service.persist_session(session)
            self._create_orphan_helper_output(service, session.session_id)

            async def fake_start_audio_observer(session_id: str, payload: dict[str, object]):
                latest = service.get_session(session_id)
                assert latest is not None
                latest.ai_state["audio_observer"] = {
                    **dict(latest.ai_state.get("audio_observer") or {}),
                    "running": True,
                    "restart_payload": dict(payload),
                }
                return service.persist_session(latest), {"running": True}

            service.start_audio_observer = mock.AsyncMock(side_effect=fake_start_audio_observer)  # type: ignore[method-assign]

            result = await service.recover_live_audio_observers(requested_by="startup_recovery")

            self.assertEqual(result["restarted_session_ids"], [session.session_id])
            self.assertEqual(result["salvaged_session_ids"], [session.session_id])
            service.start_audio_observer.assert_awaited_once()  # type: ignore[union-attr]

            refreshed = service.get_session(session.session_id)
            self.assertIsNotNone(refreshed)
            assert refreshed is not None
            archives = refreshed.ai_state.get("full_track_archive_paths") or []
            self.assertEqual(len(archives), 2)
            full_track_state = dict(refreshed.ai_state.get("full_track_capture") or {})
            self.assertEqual(full_track_state.get("strategy"), "windows_native_full_track")
            self.assertEqual(int(full_track_state.get("chunks") or 0), 1)
            recovery_state = dict(refreshed.ai_state.get("audio_observer_recovery") or {})
            self.assertEqual(int(recovery_state.get("restart_count") or 0), 1)
            self.assertEqual(str(recovery_state.get("last_status") or ""), "restarted")

    def test_salvage_orphan_windows_helper_output_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self._build_service(temp_dir)
            session = DelegateSession(
                session_id="salvage-once",
                delegate_mode="answer_on_ask",
                bot_display_name="WooBIN_bot",
                status="active",
                ai_state={
                    "audio_observer": {
                        "running": False,
                        "continuous_mode": True,
                        "capture_backend": "windows_native_helper",
                        "restart_payload": {
                            "system_device_name": "Test Speaker",
                            "meeting_output_device_name": "Test Speaker",
                        },
                    },
                    "full_track_capture": {
                        "running": False,
                        "strategy": "windows_native_full_track",
                        "chunks": 0,
                    },
                    "full_track_capture_baseline": "2026-04-22T11:10:01+09:00",
                },
            )
            session = service.persist_session(session)
            self._create_orphan_helper_output(service, session.session_id)

            recovered, salvaged_first = service._salvage_orphan_windows_helper_output(session)
            recovered = service.persist_session(recovered)
            recovered_again, salvaged_second = service._salvage_orphan_windows_helper_output(recovered)

            self.assertTrue(salvaged_first)
            self.assertFalse(salvaged_second)
            archives = recovered_again.ai_state.get("full_track_archive_paths") or []
            self.assertEqual(len(archives), 2)


if __name__ == "__main__":
    unittest.main()
