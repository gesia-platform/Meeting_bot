from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

from .paths import workspace_root
from .runtime_env import build_runtime_env, package_root


def prepare_models(
    config: dict[str, Any],
    *,
    yes: bool = False,
    prepare_transcribe: bool = True,
    prepare_diarization: bool = True,
) -> dict[str, Any]:
    steps: list[dict[str, Any]] = []
    env = build_runtime_env(config)
    local_ai = dict(config.get("local_ai") or {})

    transcribe_model = str(local_ai.get("transcribe_model") or "large-v3").strip() or "large-v3"
    diarization_model = (
        str(local_ai.get("diarization_model") or "pyannote/speaker-diarization-community-1").strip()
        or "pyannote/speaker-diarization-community-1"
    )
    huggingface_token = str(local_ai.get("huggingface_token") or "").strip()

    if prepare_transcribe:
        if _confirm(
            f"전사 모델 `{transcribe_model}`를 미리 다운로드할까요?",
            yes=yes,
            default=True,
        ):
            _run_model_prep(
                _transcribe_prepare_code(transcribe_model),
                env=env,
                step_name="transcribe_model",
                steps=steps,
                detail={"model": transcribe_model},
            )
        else:
            steps.append(
                {
                    "step": "transcribe_model",
                    "status": "skipped",
                    "reason": "user_declined",
                    "model": transcribe_model,
                }
            )

    if prepare_diarization:
        if not huggingface_token:
            steps.append(
                {
                    "step": "diarization_model",
                    "status": "warning",
                    "detail": "huggingface_token is empty, so pyannote model download was skipped.",
                    "model": diarization_model,
                }
            )
        elif _confirm(
            f"화자 분리 모델 `{diarization_model}`를 미리 다운로드할까요?",
            yes=yes,
            default=True,
        ):
            _run_model_prep(
                _diarization_prepare_code(diarization_model, huggingface_token),
                env=env,
                step_name="diarization_model",
                steps=steps,
                detail={"model": diarization_model},
            )
        else:
            steps.append(
                {
                    "step": "diarization_model",
                    "status": "skipped",
                    "reason": "user_declined",
                    "model": diarization_model,
                }
            )

    return {
        "status": "ok",
        "workspace_dir": str(workspace_root()),
        "package_dir": str(package_root()),
        "steps": steps,
    }


def _run_model_prep(
    code: str,
    *,
    env: dict[str, str],
    step_name: str,
    steps: list[dict[str, Any]],
    detail: dict[str, Any],
) -> None:
    process = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(package_root()),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if process.returncode != 0:
        error_text = process.stderr.strip() or process.stdout.strip() or f"exit code {process.returncode}"
        raise RuntimeError(f"{step_name} failed: {error_text}")

    steps.append(
        {
            "step": step_name,
            "status": "ok",
            **detail,
        }
    )


def _transcribe_prepare_code(model_name: str) -> str:
    return (
        "from faster_whisper import WhisperModel\n"
        f"WhisperModel({model_name!r}, device='cpu', compute_type='int8')\n"
        f"print('prepared:{model_name}')\n"
    )


def _diarization_prepare_code(model_name: str, token: str) -> str:
    return (
        "from pyannote.audio import Pipeline\n"
        f"Pipeline.from_pretrained({model_name!r}, token={token!r})\n"
        f"print('prepared:{model_name}')\n"
    )


def _confirm(prompt: str, *, yes: bool, default: bool) -> bool:
    if yes:
        return True
    marker = "Y/n" if default else "y/N"
    answer = input(f"{prompt} [{marker}]: ").strip().lower()
    if not answer:
        return default
    return answer in {"y", "yes"}
