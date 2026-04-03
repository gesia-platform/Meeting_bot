from __future__ import annotations

import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


WINDOWS = "windows"
MACOS = "macos"
LINUX = "linux"


@dataclass(frozen=True)
class ToolInstallPlan:
    step_name: str
    prompt: str
    command_candidates: tuple[str, ...]
    install_command: tuple[str, ...]


def current_platform_id() -> str:
    if sys.platform.startswith("win"):
        return WINDOWS
    if sys.platform == "darwin":
        return MACOS
    return LINUX


def current_platform_label() -> str:
    platform_id = current_platform_id()
    if platform_id == WINDOWS:
        return "Windows"
    if platform_id == MACOS:
        return "macOS"
    return "Linux"


def observer_extra_for_current_platform() -> str:
    platform_id = current_platform_id()
    if platform_id == MACOS:
        return "observer-macos"
    if platform_id == WINDOWS:
        return "observer-windows"
    return "observer-core"


def editable_install_target() -> str:
    extras = [observer_extra_for_current_platform(), "meeting-quality"]
    return f".[{','.join(extras)}]"


def tool_install_plans() -> list[ToolInstallPlan]:
    platform_id = current_platform_id()
    if platform_id == MACOS:
        return [
            ToolInstallPlan(
                step_name="pandoc",
                prompt="pandoc가 없으면 PDF/docx 변환 품질이 떨어질 수 있습니다. Homebrew로 설치할까요?",
                command_candidates=("pandoc",),
                install_command=("brew", "install", "pandoc"),
            ),
            ToolInstallPlan(
                step_name="libreoffice",
                prompt="LibreOffice가 없으면 PDF 변환이 되지 않습니다. Homebrew Cask로 설치할까요?",
                command_candidates=("soffice", "/Applications/LibreOffice.app/Contents/MacOS/soffice"),
                install_command=("brew", "install", "--cask", "libreoffice"),
            ),
            ToolInstallPlan(
                step_name="ffmpeg",
                prompt="ffmpeg가 없으면 일부 보조 오디오 처리 경로가 제한될 수 있습니다. Homebrew로 설치할까요?",
                command_candidates=("ffmpeg",),
                install_command=("brew", "install", "ffmpeg"),
            ),
        ]

    if platform_id == WINDOWS:
        return [
            ToolInstallPlan(
                step_name="pandoc",
                prompt="pandoc가 없으면 PDF/docx 변환 품질이 떨어질 수 있습니다. 설치할까요?",
                command_candidates=(
                    "pandoc",
                    r"C:\Users\jung\AppData\Local\Microsoft\WinGet\Packages\JohnMacFarlane.Pandoc_Microsoft.Winget.Source_8wekyb3d8bbwe\pandoc-3.9.0.1\pandoc.exe",
                ),
                install_command=(
                    "winget",
                    "install",
                    "--id",
                    "JohnMacFarlane.Pandoc",
                    "--exact",
                    "--accept-package-agreements",
                    "--accept-source-agreements",
                ),
            ),
            ToolInstallPlan(
                step_name="libreoffice",
                prompt="LibreOffice가 없으면 PDF 변환이 되지 않습니다. 설치할까요?",
                command_candidates=("soffice", r"C:\Program Files\LibreOffice\program\soffice.exe"),
                install_command=(
                    "winget",
                    "install",
                    "--id",
                    "TheDocumentFoundation.LibreOffice",
                    "--exact",
                    "--accept-package-agreements",
                    "--accept-source-agreements",
                ),
            ),
            ToolInstallPlan(
                step_name="ffmpeg",
                prompt="ffmpeg가 없으면 일부 보조 오디오 처리 경로가 제한될 수 있습니다. 설치할까요?",
                command_candidates=(
                    "ffmpeg",
                    r"C:\Users\jung\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-8.0-full_build\bin\ffmpeg.exe",
                ),
                install_command=(
                    "winget",
                    "install",
                    "--id",
                    "Gyan.FFmpeg",
                    "--exact",
                    "--accept-package-agreements",
                    "--accept-source-agreements",
                ),
            ),
        ]

    return [
        ToolInstallPlan(
            step_name="pandoc",
            prompt="pandoc가 없으면 PDF/docx 변환 품질이 떨어질 수 있습니다. 설치할까요?",
            command_candidates=("pandoc",),
            install_command=(),
        ),
        ToolInstallPlan(
            step_name="libreoffice",
            prompt="LibreOffice가 없으면 PDF 변환이 되지 않습니다. 설치할까요?",
            command_candidates=("soffice",),
            install_command=(),
        ),
        ToolInstallPlan(
            step_name="ffmpeg",
            prompt="ffmpeg가 없으면 일부 보조 오디오 처리 경로가 제한될 수 있습니다. 설치할까요?",
            command_candidates=("ffmpeg",),
            install_command=(),
        ),
    ]


def command_candidates_exist(candidates: Iterable[str]) -> tuple[bool, str]:
    for candidate in candidates:
        value = str(candidate or "").strip()
        if not value:
            continue
        path = Path(value).expanduser()
        if path.is_absolute() and path.exists():
            return True, str(path)
        resolved = shutil.which(value)
        if resolved:
            return True, resolved
    return False, ""
