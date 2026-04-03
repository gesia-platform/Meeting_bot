from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

from .model_manager import prepare_models
from .platform_support import (
    ToolInstallPlan,
    command_candidates_exist,
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
        return

    steps.append(
        {
            "step": plan.step_name,
            "status": "skipped",
            "reason": "user_declined",
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


def _confirm(prompt: str, *, yes: bool, default: bool) -> bool:
    if yes:
        return True
    marker = "Y/n" if default else "y/N"
    answer = input(f"{prompt} [{marker}]: ").strip().lower()
    if not answer:
        return default
    return answer in {"y", "yes"}
