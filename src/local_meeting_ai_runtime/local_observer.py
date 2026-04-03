"""Local observation helpers that arm the local AI body with screen and audio capture."""

from __future__ import annotations

import importlib.util
import io
import os
from pathlib import Path
from typing import Any

import numpy as np


class LocalObserverError(RuntimeError):
    """Raised when local observation cannot proceed."""


class LocalObserver:
    def __init__(self) -> None:
        self._ocr_lang = os.getenv("DELEGATE_LOCAL_OCR_LANGUAGE", "kor+eng").strip() or "kor+eng"
        self._ocr_psm = os.getenv("DELEGATE_LOCAL_OCR_PSM", "6").strip() or "6"
        self._audio_sample_rate = int(os.getenv("DELEGATE_LOCAL_AUDIO_SAMPLE_RATE", "16000"))
        self._audio_channels = int(os.getenv("DELEGATE_LOCAL_AUDIO_CHANNELS", "1"))
        self._audio_blocksize_ms = max(int(os.getenv("DELEGATE_LOCAL_AUDIO_BLOCKSIZE_MS", "1200")), 100)
        self._audio_rms_threshold = float(os.getenv("DELEGATE_LOCAL_AUDIO_RMS_THRESHOLD", "0.003"))
        self._microphone_device_name = os.getenv("DELEGATE_LOCAL_MICROPHONE_DEVICE", "").strip()
        self._system_audio_device_name = os.getenv("DELEGATE_LOCAL_SYSTEM_AUDIO_DEVICE", "").strip()
        self._meeting_output_device_name = (
            os.getenv("DELEGATE_LOCAL_MEETING_OUTPUT_DEVICE", "").strip()
            or self._system_audio_device_name
            or "스피커(USB Audio Device)"
        )
        self._strict_audio_device_selection = self._env_bool("DELEGATE_LOCAL_AUDIO_STRICT_DEVICE", False)
        self._artifact_dir = Path(os.getenv("DELEGATE_LOCAL_OBSERVER_DIR", ".tmp/local-observer"))
        self._artifact_dir.mkdir(parents=True, exist_ok=True)

    @property
    def capabilities(self) -> dict[str, bool]:
        return {
            "window_capture": importlib.util.find_spec("mss") is not None and importlib.util.find_spec("pygetwindow") is not None,
            "ocr": importlib.util.find_spec("pytesseract") is not None,
            "system_audio_capture": importlib.util.find_spec("soundcard") is not None and importlib.util.find_spec("soundfile") is not None,
            "window_automation": importlib.util.find_spec("pywinauto") is not None or importlib.util.find_spec("pyautogui") is not None,
        }

    @property
    def audio_devices(self) -> dict[str, Any]:
        if importlib.util.find_spec("soundcard") is None:
            return {"microphones": [], "speakers": [], "configured": {}}
        configured = {
            "microphone_device_name": self._microphone_device_name or None,
            "system_audio_device_name": self._system_audio_device_name or None,
            "meeting_output_device_name": self._meeting_output_device_name or None,
            "strict_device_selection": self._strict_audio_device_selection,
        }
        try:
            soundcard = self._import_module("soundcard")
            microphones = self._device_descriptors(getattr(soundcard, "all_microphones", lambda: [])())
            speakers = self._device_descriptors(getattr(soundcard, "all_speakers", lambda: [])())
        except Exception as exc:
            return {
                "microphones": [],
                "speakers": [],
                "configured": {**configured, "enumeration_error": str(exc)},
            }
        return {
            "microphones": microphones,
            "speakers": speakers,
            "configured": configured,
        }

    @property
    def meeting_output_device_name(self) -> str:
        return self._meeting_output_device_name

    def audio_quality_readiness(self) -> dict[str, Any]:
        if importlib.util.find_spec("soundcard") is None:
            return {
                "microphone_device_ready": False,
                "meeting_output_device_ready": False,
                "configured_meeting_output_device": self._meeting_output_device_name or None,
                "blocking_reasons": ["soundcard dependency is not installed."],
            }
        try:
            soundcard = self._import_module("soundcard")
            microphones = list(getattr(soundcard, "all_microphones", lambda: [])())
            speakers = list(getattr(soundcard, "all_speakers", lambda: [])())
        except Exception as exc:
            return {
                "microphone_device_ready": False,
                "meeting_output_device_ready": False,
                "configured_meeting_output_device": self._meeting_output_device_name or None,
                "blocking_reasons": [str(exc)],
            }
        microphone_device_ready = bool(
            self._match_named_device(microphones, self._microphone_device_name)
            if self._microphone_device_name
            else getattr(soundcard, "default_microphone", lambda: None)() is not None
        )
        meeting_output_device_ready = bool(self._match_named_device(speakers, self._meeting_output_device_name))
        blocking_reasons: list[str] = []
        if not microphone_device_ready:
            if self._microphone_device_name:
                blocking_reasons.append(
                    f"Configured microphone device was not found: {self._microphone_device_name}"
                )
            else:
                blocking_reasons.append("No default microphone device is available.")
        if not meeting_output_device_ready:
            blocking_reasons.append(
                f"Configured meeting output device was not found: {self._meeting_output_device_name}"
            )
        return {
            "microphone_device_ready": microphone_device_ready,
            "meeting_output_device_ready": meeting_output_device_ready,
            "configured_meeting_output_device": self._meeting_output_device_name or None,
            "blocking_reasons": blocking_reasons,
        }

    def microphone_device_available(self, device_name: str | None = None) -> bool:
        if importlib.util.find_spec("soundcard") is None:
            return False
        try:
            soundcard = self._import_module("soundcard")
            microphones = list(getattr(soundcard, "all_microphones", lambda: [])())
            hint = str(device_name or self._microphone_device_name or "").strip()
            if hint:
                return self._match_named_device(microphones, hint) is not None
            return getattr(soundcard, "default_microphone", lambda: None)() is not None
        except Exception:
            return False

    def speaker_device_available(self, device_name: str | None = None) -> bool:
        if importlib.util.find_spec("soundcard") is None:
            return False
        try:
            soundcard = self._import_module("soundcard")
            speakers = list(getattr(soundcard, "all_speakers", lambda: [])())
            hint = str(device_name or self._meeting_output_device_name or "").strip()
            if hint:
                return self._match_named_device(speakers, hint) is not None
            return getattr(soundcard, "default_speaker", lambda: None)() is not None
        except Exception:
            return False

    def capture_window_text(
        self,
        *,
        window_title: str,
        crop: dict[str, int] | None = None,
        save_image: bool = True,
    ) -> dict[str, Any]:
        mss = self._import_module("mss")
        gw = self._import_module("pygetwindow")
        pytesseract = self._import_module("pytesseract")
        pil_image_module = self._import_module("PIL.Image")

        window = self._find_window(gw, window_title)
        bounds = {
            "left": int(window.left),
            "top": int(window.top),
            "width": int(window.width),
            "height": int(window.height),
        }
        if crop:
            bounds = self._apply_crop(bounds, crop)

        with mss.mss() as sct:
            raw = sct.grab(bounds)
        image = pil_image_module.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
        prepared = self._prepare_ocr_image(image)
        config = f"--psm {self._ocr_psm}"
        text = str(pytesseract.image_to_string(prepared, lang=self._ocr_lang, config=config) or "").strip()

        artifact_path = None
        if save_image:
            artifact_path = self._artifact_dir / f"window-capture-{self._safe_name(window_title)}.png"
            image.save(artifact_path)

        return {
            "window_title": window.title,
            "bounds": bounds,
            "text": text,
            "image_path": str(artifact_path) if artifact_path else None,
        }

    def capture_audio(
        self,
        *,
        seconds: float,
        sample_rate: int | None = None,
        source: str = "system",
        device_name: str | None = None,
        strict_device: bool | None = None,
    ) -> dict[str, Any]:
        soundcard = self._import_module("soundcard")
        soundfile = self._import_module("soundfile")

        normalized_source = str(source or "system").strip().lower() or "system"
        rate = int(sample_rate or self._audio_sample_rate)
        duration = max(float(seconds), 0.5)
        frames = max(int(rate * duration), 1)
        requested_device_name = str(device_name or "").strip()
        device_hint = requested_device_name or (
            self._microphone_device_name if normalized_source == "microphone" else self._system_audio_device_name
        )
        strict = self._strict_audio_device_selection if strict_device is None else bool(strict_device)
        blocksize = max(int(rate * (self._audio_blocksize_ms / 1000.0)), frames)
        if normalized_source == "microphone":
            recorder_device = self._select_microphone(soundcard, device_hint, strict=strict)
            filename = "microphone-capture.wav"
            capture_mode = "local_microphone"
        else:
            speaker = self._select_speaker(soundcard, device_hint, strict=strict)
            recorder_device = soundcard.get_microphone(str(speaker.name), include_loopback=True)
            filename = "loopback-capture.wav"
            capture_mode = "local_system_audio"

        with recorder_device.recorder(
            samplerate=rate,
            channels=self._audio_channels,
            blocksize=blocksize,
        ) as recorder:
            audio = recorder.record(numframes=frames)

        if audio is None or len(audio) == 0:
            raise LocalObserverError("Local audio capture returned no samples.")

        if getattr(audio, "ndim", 1) > 1 and self._audio_channels == 1:
            audio = audio[:, 0]

        audio_rms = float(np.sqrt(np.mean(np.square(audio.astype(np.float32))))) if len(audio) else 0.0
        audio_peak = float(np.max(np.abs(audio.astype(np.float32)))) if len(audio) else 0.0

        buffer = io.BytesIO()
        soundfile.write(buffer, audio, rate, format="WAV")
        wav_bytes = buffer.getvalue()
        artifact_path = self._artifact_dir / filename
        artifact_path.write_bytes(wav_bytes)

        return {
            "audio_bytes": wav_bytes,
            "filename": filename,
            "sample_rate": rate,
            "seconds": duration,
            "artifact_path": str(artifact_path),
            "capture_mode": capture_mode,
            "audio_source": normalized_source,
            "audio_channels": int(self._audio_channels),
            "channel_layout": "stereo" if int(self._audio_channels) >= 2 else "mono",
            "device_name": str(getattr(recorder_device, "name", "") or getattr(speaker if normalized_source != "microphone" else recorder_device, "name", "") or device_hint or ""),
            "audio_rms": audio_rms,
            "audio_peak": audio_peak,
            "below_rms_threshold": audio_rms < self._audio_rms_threshold,
        }

    def _import_module(self, name: str) -> Any:
        if importlib.util.find_spec(name) is None:
            raise LocalObserverError(f"Required local observation dependency is missing: {name}")
        module = __import__(name, fromlist=["*"])
        if name == "soundcard":
            self._patch_soundcard_numpy_compat(module)
        return module

    def _patch_soundcard_numpy_compat(self, soundcard: Any) -> None:
        try:
            mediafoundation = getattr(soundcard, "mediafoundation", None)
            if mediafoundation is None:
                mediafoundation = __import__("soundcard.mediafoundation", fromlist=["*"])
            if getattr(mediafoundation, "_delegate_numpy_binary_patch", False):
                return
            original_fromstring = np.fromstring

            def _compat_fromstring(
                data: Any,
                dtype: Any = float,
                count: int = -1,
                sep: str = "",
                *,
                like: Any = None,
            ) -> Any:
                if sep not in ("", b""):
                    kwargs: dict[str, Any] = {"dtype": dtype, "count": count, "sep": sep}
                    if like is not None:
                        kwargs["like"] = like
                    return original_fromstring(data, **kwargs)
                kwargs = {"dtype": dtype}
                if count != -1:
                    kwargs["count"] = count
                if like is not None:
                    kwargs["like"] = like
                return np.frombuffer(data, **kwargs).copy()

            mediafoundation.numpy.fromstring = _compat_fromstring
            mediafoundation._delegate_numpy_binary_patch = True
        except Exception:
            return

    def _find_window(self, gw: Any, title_fragment: str) -> Any:
        fragment = str(title_fragment or "").strip()
        if not fragment:
            raise LocalObserverError("A window title fragment is required for window observation.")
        matches = [window for window in gw.getWindowsWithTitle(fragment) if getattr(window, "width", 0) > 0 and getattr(window, "height", 0) > 0]
        if not matches:
            raise LocalObserverError(f"No visible window matched title fragment: {fragment}")
        matches.sort(key=lambda item: (item.width * item.height), reverse=True)
        return matches[0]

    def _apply_crop(self, bounds: dict[str, int], crop: dict[str, int]) -> dict[str, int]:
        left = bounds["left"] + int(crop.get("left", 0))
        top = bounds["top"] + int(crop.get("top", 0))
        width = int(crop.get("width", bounds["width"]))
        height = int(crop.get("height", bounds["height"]))
        return {
            "left": max(left, 0),
            "top": max(top, 0),
            "width": max(width, 1),
            "height": max(height, 1),
        }

    def _prepare_ocr_image(self, image: Any) -> Any:
        cv2 = self._import_module("cv2")
        pil_image_module = self._import_module("PIL.Image")
        pil_image = pil_image_module.Image if hasattr(pil_image_module, "Image") else None
        if pil_image is not None and isinstance(image, pil_image):
            np_image = np.array(image)
        else:
            np_image = np.array(image)
        gray = cv2.cvtColor(np_image, cv2.COLOR_RGB2GRAY)
        thresholded = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
        return thresholded

    def _safe_name(self, value: str) -> str:
        return "".join(char if char.isalnum() else "-" for char in value)[:80].strip("-") or "window"

    def _env_bool(self, name: str, default: bool) -> bool:
        raw = os.getenv(name)
        if raw is None:
            return default
        return raw.strip().lower() not in {"0", "false", "no", "off"}

    def _select_microphone(self, soundcard: Any, device_name: str, *, strict: bool) -> Any:
        candidates = list(getattr(soundcard, "all_microphones", lambda: [])())
        selected = self._match_named_device(candidates, device_name)
        if selected is not None:
            return selected
        if device_name and strict:
            raise LocalObserverError(f"Configured microphone device was not found: {device_name}")
        recorder_device = soundcard.default_microphone()
        if recorder_device is None:
            raise LocalObserverError("No default microphone is available for local microphone capture.")
        return recorder_device

    def _select_speaker(self, soundcard: Any, device_name: str, *, strict: bool) -> Any:
        candidates = list(getattr(soundcard, "all_speakers", lambda: [])())
        selected = self._match_named_device(candidates, device_name)
        if selected is not None:
            return selected
        if device_name and strict:
            raise LocalObserverError(f"Configured system audio device was not found: {device_name}")
        speaker = soundcard.default_speaker()
        if speaker is None:
            raise LocalObserverError("No default system speaker is available for loopback capture.")
        return speaker

    def _match_named_device(self, candidates: list[Any], device_name: str) -> Any | None:
        hint = str(device_name or "").strip().lower()
        if not hint:
            return None
        exact = [device for device in candidates if str(getattr(device, "name", "")).strip().lower() == hint]
        if exact:
            return exact[0]
        partial = [device for device in candidates if hint in str(getattr(device, "name", "")).strip().lower()]
        if partial:
            return partial[0]
        return None

    def _device_descriptors(self, devices: list[Any]) -> list[dict[str, Any]]:
        descriptors: list[dict[str, Any]] = []
        for device in devices:
            descriptors.append(
                {
                    "name": str(getattr(device, "name", "") or ""),
                    "id": str(getattr(device, "id", "") or ""),
                }
            )
        return descriptors
