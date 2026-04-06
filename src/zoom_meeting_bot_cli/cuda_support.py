from __future__ import annotations

import importlib.metadata
import importlib.util
import os
import shutil
import subprocess
import sys
from typing import Any


DEFAULT_TORCH_CUDA_INDEX_URL = "https://download.pytorch.org/whl/cu128"


def detect_cuda_gpu() -> tuple[bool, str]:
    try:
        if importlib.util.find_spec("torch") is not None:
            import torch  # type: ignore[import-not-found]

            if bool(torch.cuda.is_available()):
                return True, "torch.cuda"
    except Exception:
        pass

    try:
        nvidia_smi = shutil.which("nvidia-smi")
        if not nvidia_smi:
            return False, "nvidia-smi not found"
        result = subprocess.run(
            [nvidia_smi, "-L"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            check=False,
            timeout=5,
        )
        if result.returncode == 0 and str(result.stdout or "").strip():
            return True, "nvidia-smi"
        return False, "nvidia-smi returned no devices"
    except Exception as exc:
        return False, str(exc).strip() or exc.__class__.__name__


def inspect_torch_runtime() -> dict[str, Any]:
    payload: dict[str, Any] = {
        "installed": False,
        "distribution_version": "",
        "module_version": "",
        "cuda_enabled": False,
        "cuda_version": "",
        "device_count": 0,
        "detail": "torch is not installed.",
    }
    if importlib.util.find_spec("torch") is None:
        return payload

    payload["installed"] = True
    try:
        payload["distribution_version"] = importlib.metadata.version("torch")
    except Exception:
        payload["distribution_version"] = ""

    try:
        import torch  # type: ignore[import-not-found]

        module_version = str(getattr(torch, "__version__", "") or "")
        cuda_enabled = bool(torch.cuda.is_available())
        cuda_version = str(getattr(getattr(torch, "version", None), "cuda", "") or "")
        device_count = int(torch.cuda.device_count()) if cuda_enabled else 0
        payload.update(
            {
                "module_version": module_version,
                "cuda_enabled": cuda_enabled,
                "cuda_version": cuda_version,
                "device_count": device_count,
            }
        )
        if cuda_enabled:
            payload["detail"] = (
                f"torch {module_version or payload['distribution_version']} can use CUDA"
                f" ({device_count} visible device(s), CUDA {cuda_version or 'unknown'})."
            )
        else:
            payload["detail"] = (
                f"torch {module_version or payload['distribution_version']} is installed, "
                "but CUDA is not available in this Python environment."
            )
    except Exception as exc:
        payload["detail"] = f"torch is installed, but inspection failed: {exc}"
    return payload


def torch_cuda_index_url() -> str:
    configured = str(os.environ.get("ZOOM_MEETING_BOT_TORCH_CUDA_INDEX_URL") or "").strip()
    if configured:
        return configured
    return DEFAULT_TORCH_CUDA_INDEX_URL


def build_torch_cuda_install_commands() -> list[list[str]]:
    torch_version = _distribution_version("torch")
    torchaudio_version = _distribution_version("torchaudio")
    base_command = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--upgrade",
        "--force-reinstall",
        "--index-url",
        torch_cuda_index_url(),
    ]
    commands: list[list[str]] = [[*base_command, "torch", "torchaudio"]]
    if torch_version or torchaudio_version:
        pinned = list(base_command)
        pinned.append(f"torch=={torch_version}" if torch_version else "torch")
        pinned.append(f"torchaudio=={torchaudio_version}" if torchaudio_version else "torchaudio")
        commands.append(pinned)
    deduped: list[list[str]] = []
    seen: set[tuple[str, ...]] = set()
    for command in commands:
        key = tuple(command)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(command)
    return deduped


def _distribution_version(package_name: str) -> str:
    try:
        return str(importlib.metadata.version(package_name) or "").strip()
    except Exception:
        return ""
