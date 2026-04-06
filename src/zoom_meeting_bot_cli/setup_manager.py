from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from local_meeting_ai_runtime.assets import (
    bundled_whisper_cpp_roots,
    find_whisper_cpp_cli,
    find_whisper_cpp_model,
    find_whisper_cpp_vad_model,
    prepare_whisper_cpp_assets,
    whisper_cpp_asset_root,
    whisper_cpp_asset_status,
)

from .config import DEFAULT_WHISPER_CPP_MODEL_NAME
from .cuda_support import build_torch_cuda_install_commands, detect_cuda_gpu, inspect_torch_runtime, torch_cuda_index_url
from .model_manager import prepare_models
from .platform_support import (
    MACOS,
    ToolInstallPlan,
    command_candidates_exist,
    current_platform_id,
    current_platform_label,
    editable_install_target,
    tool_install_plans,
)


PACKAGE_ROOT = Path(__file__).resolve().parents[2]


def run_setup(
    *,
    config: dict[str, Any] | None = None,
    yes: bool = False,
    install_python: bool = True,
    install_tools: bool = True,
    create_directories: bool = True,
    prepare_local_models: bool = True,
) -> dict[str, Any]:
    steps: list[dict[str, Any]] = []

    steps.append(
        {
            "step": "platform",
            "status": "ok",
            "platform_label": current_platform_label(),
            "python_dependency_target": editable_install_target(),
        }
    )

    if create_directories:
        created_paths = _ensure_default_directories()
        steps.append(
            {
                "step": "workspace_directories",
                "status": "ok",
                "paths": [str(path) for path in created_paths],
            }
        )

    if install_python:
        if _confirm(
            f"회의 엔진과 오디오/전사 패키지를 설치할까요? ({current_platform_label()} 경로)",
            yes=yes,
            default=True,
        ):
            _run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    "-e",
                    editable_install_target(),
                ],
                step_name="python_dependencies",
                steps=steps,
            )
        else:
            steps.append(
                {
                    "step": "python_dependencies",
                    "status": "skipped",
                    "reason": "user_declined",
                }
            )

    if install_tools:
        for plan in tool_install_plans():
            _maybe_install_tool(plan=plan, yes=yes, steps=steps)

    _ensure_cuda_torch_runtime(yes=yes, steps=steps)

    if config is None:
        steps.append(
            {
                "step": "whisper_cpp_assets",
                "status": "warning",
                "detail": "Config is missing, so bundled whisper.cpp assets were not prepared.",
            }
        )
    else:
        _prepare_whisper_cpp_runtime_assets(config=config, steps=steps)

    if prepare_local_models:
        if config is None:
            steps.append(
                {
                    "step": "prepare_models",
                    "status": "warning",
                    "detail": "설정 파일이 아직 없어 모델 준비를 건너뜁니다.",
                }
            )
        elif _confirm(
            "전사와 화자 분리 모델을 미리 준비할까요?",
            yes=yes,
            default=False,
        ):
            model_result = prepare_models(config, yes=yes)
            steps.extend(list(model_result.get("steps") or []))
        else:
            steps.append(
                {
                    "step": "prepare_models",
                    "status": "skipped",
                    "reason": "user_declined",
                }
            )

    return {
        "status": "ok",
        "workspace_dir": str(PACKAGE_ROOT),
        "steps": steps,
    }


def _ensure_default_directories() -> list[Path]:
    targets = [
        PACKAGE_ROOT / "data" / "exports",
        PACKAGE_ROOT / "data" / "audio",
        PACKAGE_ROOT / ".tmp" / "zoom-meeting-bot",
    ]
    for target in targets:
        target.mkdir(parents=True, exist_ok=True)
    return targets


def _maybe_install_tool(*, plan: ToolInstallPlan, yes: bool, steps: list[dict[str, Any]]) -> None:
    found, resolved = command_candidates_exist(plan.command_candidates)
    if found:
        steps.append(
            {
                "step": plan.step_name,
                "status": "ok",
                "detail": f"`{resolved}` already exists.",
            }
        )
        return

    if not plan.install_command:
        steps.append(
            {
                "step": plan.step_name,
                "status": "warning",
                "detail": f"{current_platform_label()} 자동 설치 경로가 아직 준비되지 않았습니다.",
            }
        )
        return

    installer_name = plan.install_command[0]
    available, _resolved_installer = command_candidates_exist((installer_name,))
    if not available:
        steps.append(
            {
                "step": plan.step_name,
                "status": "warning",
                "detail": f"`{installer_name}` 명령을 찾지 못했습니다.",
            }
        )
        return

    if _confirm(plan.prompt, yes=yes, default=True):
        _run(list(plan.install_command), step_name=plan.step_name, steps=steps)
        if plan.reboot_required:
            steps.append(
                {
                    "step": f"{plan.step_name}_reboot",
                    "status": "warning",
                    "detail": "This installation requires one macOS reboot before the new audio driver becomes available.",
                    "reboot_required": True,
                }
            )
        return

    steps.append(
        {
            "step": plan.step_name,
            "status": "skipped",
            "reason": "user_declined",
        }
    )


def _ensure_cuda_torch_runtime(*, yes: bool, steps: list[dict[str, Any]]) -> None:
    gpu_detected, gpu_detail = detect_cuda_gpu()
    runtime_before = inspect_torch_runtime()
    if not gpu_detected:
        detail = "CUDA-capable GPU was not detected, so the current torch runtime was kept."
        if current_platform_id() == MACOS:
            detail = (
                "CUDA-capable GPU was not detected. On macOS, final offline transcription will use the local "
                "CPU faster-whisper path when available."
            )
        steps.append(
            {
                "step": "torch_cuda_runtime",
                "status": "ok",
                "detail": detail,
                "gpu_detected": False,
                "torch_detail": runtime_before.get("detail"),
            }
        )
        return

    if bool(runtime_before.get("cuda_enabled")):
        steps.append(
            {
                "step": "torch_cuda_runtime",
                "status": "ok",
                "detail": f"CUDA-capable torch runtime is already ready via {gpu_detail}.",
                "gpu_detected": True,
                "gpu_detail": gpu_detail,
                "torch_detail": runtime_before.get("detail"),
            }
        )
        return

    if not _confirm(
        "CUDA-capable GPU가 감지되었습니다. final transcription 품질 경로를 위해 CUDA-enabled torch/torchaudio를 설치할까요?",
        yes=yes,
        default=True,
    ):
        steps.append(
            {
                "step": "torch_cuda_runtime",
                "status": "skipped",
                "reason": "user_declined",
                "gpu_detected": True,
                "gpu_detail": gpu_detail,
                "torch_detail": runtime_before.get("detail"),
            }
        )
        return

    install_error = ""
    installed = False
    commands = build_torch_cuda_install_commands()
    for index, command in enumerate(commands, start=1):
        try:
            _run(command, step_name=f"torch_cuda_runtime_install_{index}", steps=steps)
            installed = True
            break
        except RuntimeError as exc:
            install_error = str(exc)
    if not installed:
        raise RuntimeError(
            "CUDA-capable GPU was detected, but the CUDA torch runtime could not be installed automatically. "
            f"Last error: {install_error or 'unknown error'} | index URL: {torch_cuda_index_url()}"
        )

    runtime_after = inspect_torch_runtime()
    if not bool(runtime_after.get("cuda_enabled")):
        raise RuntimeError(
            "CUDA-capable GPU was detected, but torch still cannot use CUDA after installation. "
            f"Tried index URL: {torch_cuda_index_url()}"
        )

    steps.append(
        {
            "step": "torch_cuda_runtime",
            "status": "ok",
            "detail": "CUDA-enabled torch runtime is ready for final transcription quality pass.",
            "gpu_detected": True,
            "gpu_detail": gpu_detail,
            "torch_detail": runtime_after.get("detail"),
            "index_url": torch_cuda_index_url(),
        }
    )


def _run(command: list[str], *, step_name: str, steps: list[dict[str, Any]]) -> None:
    print(f"[setup] running: {' '.join(command)}")
    process = subprocess.run(
        command,
        cwd=str(PACKAGE_ROOT),
        check=False,
    )
    if process.returncode != 0:
        raise RuntimeError(f"{step_name} failed with exit code {process.returncode}")
    steps.append(
        {
            "step": step_name,
            "status": "ok",
            "command": command,
        }
    )


def _prepare_whisper_cpp_runtime_assets(*, config: dict[str, Any], steps: list[dict[str, Any]]) -> None:
    local_ai = dict(config.get("local_ai") or {})
    model_name = _derive_whisper_cpp_model_name(str(local_ai.get("whisper_cpp_model") or ""))
    _ensure_bundled_whisper_cpp_model_available(model_name=model_name, steps=steps)

    try:
        payload = prepare_whisper_cpp_assets(
            model_name=model_name,
            overwrite=False,
        )
        status = whisper_cpp_asset_status(model_name=model_name)
        if not bool(status.get("ready")):
            raise RuntimeError("whisper.cpp assets are still not ready after preparation.")
        steps.append(
            {
                "step": "whisper_cpp_assets",
                "status": "ok",
                "model_name": model_name,
                "asset_root": status.get("asset_root"),
                "copied_count": len(payload.get("copied") or []),
                "existing_count": len(payload.get("existing") or []),
                "external_asset_root_ready": bool(status.get("external_asset_root_ready")),
                "whisper_cli_path": status.get("whisper_cli_path"),
                "model_path": status.get("model_path"),
            }
        )
        return
    except (FileNotFoundError, RuntimeError):
        if current_platform_id() == MACOS:
            system_payload = _prepare_whisper_cpp_assets_from_system_install(
                config=config,
                model_name=model_name,
            )
            if system_payload is not None:
                steps.append(system_payload)
                return
            _build_bundled_whisper_cpp_for_macos(steps=steps)
            payload = prepare_whisper_cpp_assets(
                model_name=model_name,
                overwrite=False,
            )
            status = whisper_cpp_asset_status(model_name=model_name)
            if not bool(status.get("ready")):
                raise RuntimeError("whisper.cpp assets are still not ready after macOS build and preparation.")
            steps.append(
                {
                    "step": "whisper_cpp_assets",
                    "status": "ok",
                    "model_name": model_name,
                    "asset_root": status.get("asset_root"),
                    "copied_count": len(payload.get("copied") or []),
                    "existing_count": len(payload.get("existing") or []),
                    "external_asset_root_ready": bool(status.get("external_asset_root_ready")),
                    "whisper_cli_path": status.get("whisper_cli_path"),
                    "model_path": status.get("model_path"),
                    "detail": "Prepared whisper.cpp runtime assets from a macOS-local build.",
                }
            )
            return

    configured_command = _resolve_optional_path(str(local_ai.get("whisper_cpp_command") or ""))
    configured_model = _resolve_optional_path(str(local_ai.get("whisper_cpp_model") or ""))
    command_ready = bool(configured_command and configured_command.is_file())
    model_ready = bool(configured_model and configured_model.is_file())
    if command_ready and model_ready:
        steps.append(
            {
                "step": "whisper_cpp_assets",
                "status": "ok",
                "model_name": model_name,
                "detail": "Using configured whisper.cpp paths without asset-root copy.",
                "whisper_cli_path": str(configured_command),
                "model_path": str(configured_model),
            }
        )
        return

    raise RuntimeError(
        "whisper.cpp runtime assets are unavailable. Restore tools/whisper.cpp or configure explicit whisper.cpp paths."
    )


def _prepare_whisper_cpp_assets_from_system_install(
    *,
    config: dict[str, Any],
    model_name: str,
) -> dict[str, Any] | None:
    system_cli = _resolve_optional_path("whisper-cli")
    if system_cli is None or not system_cli.is_file():
        return None

    local_ai = dict(config.get("local_ai") or {})
    configured_model = _resolve_optional_path(str(local_ai.get("whisper_cpp_model") or ""))
    model_source = configured_model if configured_model and configured_model.is_file() else None
    if model_source is None:
        discovered_model = find_whisper_cpp_model(model_name)
        if discovered_model is not None:
            model_source = discovered_model
    if model_source is None or not model_source.is_file():
        return None

    resolved_asset_root = whisper_cpp_asset_root()
    copied_count = 0
    existing_count = 0

    cli_destination = resolved_asset_root / "bin" / system_cli.name
    if _copy_optional_asset(system_cli, cli_destination, executable=True):
        copied_count += 1
    else:
        existing_count += 1

    model_destination = resolved_asset_root / "models" / model_source.name
    if _copy_optional_asset(model_source, model_destination):
        copied_count += 1
    else:
        existing_count += 1

    vad_source = find_whisper_cpp_vad_model(preferred_model_path=str(model_source))
    if vad_source is not None:
        vad_destination = resolved_asset_root / "models" / vad_source.name
        if _copy_optional_asset(vad_source, vad_destination):
            copied_count += 1
        else:
            existing_count += 1

    status = whisper_cpp_asset_status(model_name=model_name)
    if not bool(status.get("ready")):
        return None

    return {
        "step": "whisper_cpp_assets",
        "status": "ok",
        "model_name": model_name,
        "asset_root": status.get("asset_root"),
        "copied_count": copied_count,
        "existing_count": existing_count,
        "external_asset_root_ready": bool(status.get("external_asset_root_ready")),
        "whisper_cli_path": status.get("whisper_cli_path"),
        "model_path": status.get("model_path"),
        "detail": "Prepared whisper.cpp runtime assets from a system Homebrew install.",
    }


def _copy_optional_asset(source: Path, destination: Path, *, executable: bool = False) -> bool:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        return False
    shutil.copy2(source, destination)
    if executable and not destination.name.endswith(".exe"):
        destination.chmod(0o755)
    return True


def _build_bundled_whisper_cpp_for_macos(*, steps: list[dict[str, Any]]) -> None:
    root = next((candidate for candidate in bundled_whisper_cpp_roots() if candidate.exists()), None)
    if root is None:
        raise RuntimeError("Bundled tools/whisper.cpp source root was not found for macOS asset preparation.")
    existing_cli = find_whisper_cpp_cli()
    if existing_cli is not None:
        steps.append(
            {
                "step": "whisper_cpp_build",
                "status": "ok",
                "detail": f"Bundled whisper.cpp CLI already exists at `{existing_cli}`.",
            }
        )
        return
    cmake_available, cmake_resolved = command_candidates_exist(("cmake",))
    if not cmake_available:
        raise RuntimeError(
            "cmake is required to build bundled whisper.cpp on macOS. "
            "Install it first or re-run quickstart/setup so Homebrew can install `cmake`."
        )
    toolchain_available, toolchain_resolved = command_candidates_exist(("xcodebuild", "xcrun"))
    if not toolchain_available:
        raise RuntimeError(
            "Xcode Command Line Tools are required to build bundled whisper.cpp on macOS. "
            "Run `xcode-select --install` first."
        )
    build_dir = root / "build-macos"
    build_dir.mkdir(parents=True, exist_ok=True)
    _run(
        [
            str(cmake_resolved or "cmake"),
            "-S",
            str(root),
            "-B",
            str(build_dir),
            "-DWHISPER_BUILD_TESTS=OFF",
            "-DWHISPER_BUILD_EXAMPLES=ON",
        ],
        step_name="whisper_cpp_configure",
        steps=steps,
    )
    _run(
        [
            str(cmake_resolved or "cmake"),
            "--build",
            str(build_dir),
            "-j",
            str(max(os.cpu_count() or 4, 2)),
            "--config",
            "Release",
            "--target",
            "whisper-cli",
        ],
        step_name="whisper_cpp_build",
        steps=steps,
    )
    cli_path = root / "build-macos" / "bin" / "whisper-cli"
    if not cli_path.exists():
        raise RuntimeError(
            "Bundled whisper.cpp build completed, but build-macos/bin/whisper-cli was not produced."
        )
    steps.append(
        {
            "step": "whisper_cpp_build",
            "status": "ok",
            "detail": (
                f"Built bundled whisper.cpp CLI for macOS with `{toolchain_resolved or 'xcodebuild/xcrun'}` "
                f"and `{cmake_resolved or 'cmake'}`."
            ),
            "whisper_cli_path": str(cli_path),
        }
    )


def _ensure_bundled_whisper_cpp_model_available(*, model_name: str, steps: list[dict[str, Any]]) -> None:
    root = next((candidate for candidate in bundled_whisper_cpp_roots() if candidate.exists()), None)
    if root is None:
        return
    model_path = _bundled_whisper_cpp_model_path(root=root, model_name=model_name)
    if model_path is not None:
        steps.append(
            {
                "step": "whisper_cpp_model_download",
                "status": "ok",
                "detail": f"Bundled whisper.cpp model `{model_name}` is already present.",
                "model_path": str(model_path),
            }
        )
        return

    script_path = root / "models" / ("download-ggml-model.cmd" if os.name == "nt" else "download-ggml-model.sh")
    if not script_path.exists():
        raise RuntimeError(
            f"Bundled whisper.cpp download script is missing, so `{model_name}` cannot be prepared automatically: {script_path}"
        )

    models_dir = root / "models"
    models_dir.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        command = ["cmd", "/c", str(script_path), model_name, str(models_dir)]
    else:
        bash_path = shutil.which("bash") or "/bin/bash"
        command = [bash_path, str(script_path), model_name, str(models_dir)]
    _run(command, step_name="whisper_cpp_model_download_exec", steps=steps)

    downloaded_model = _bundled_whisper_cpp_model_path(root=root, model_name=model_name)
    if downloaded_model is None:
        raise RuntimeError(
            f"whisper.cpp model `{model_name}` download completed, but `ggml-{model_name}.bin` was not found in {models_dir}."
        )
    steps.append(
        {
            "step": "whisper_cpp_model_download",
            "status": "ok",
            "detail": f"Downloaded bundled whisper.cpp model `{model_name}`.",
            "model_path": str(downloaded_model),
        }
    )


def _bundled_whisper_cpp_model_path(*, root: Path, model_name: str) -> Path | None:
    for candidate in (
        root / "models" / f"ggml-{model_name}.bin",
        root / f"ggml-{model_name}.bin",
    ):
        if candidate.exists():
            return candidate.resolve()
    return None


def _derive_whisper_cpp_model_name(configured_model_path: str) -> str:
    text = str(configured_model_path or "").strip()
    if text:
        name = Path(text).name
        if name.startswith("ggml-") and name.endswith(".bin"):
            derived = name[len("ggml-") : -len(".bin")].strip()
            if derived:
                return derived
    return DEFAULT_WHISPER_CPP_MODEL_NAME


def _resolve_optional_path(value: str) -> Path | None:
    text = str(value or "").strip()
    if not text:
        return None
    path = Path(text).expanduser()
    if path.is_absolute():
        return path.resolve()
    if "/" in text or "\\" in text:
        return (PACKAGE_ROOT / path).resolve()
    resolved = shutil.which(text)
    if resolved:
        return Path(resolved).resolve()
    return None


def _confirm(prompt: str, *, yes: bool, default: bool) -> bool:
    if yes:
        return True
    marker = "Y/n" if default else "y/N"
    answer = input(f"{prompt} [{marker}]: ").strip().lower()
    if not answer:
        return default
    return answer in {"y", "yes"}
