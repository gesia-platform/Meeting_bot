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
    reboot_required: bool = False


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
                prompt="Install `pandoc` with Homebrew for docx/PDF export support?",
                command_candidates=("pandoc", "/opt/homebrew/bin/pandoc", "/usr/local/bin/pandoc"),
                install_command=("brew", "install", "pandoc"),
            ),
            ToolInstallPlan(
                step_name="libreoffice",
                prompt="Install `LibreOffice` with Homebrew Cask for PDF export support?",
                command_candidates=("soffice", "/Applications/LibreOffice.app/Contents/MacOS/soffice"),
                install_command=("brew", "install", "--cask", "libreoffice"),
            ),
            ToolInstallPlan(
                step_name="ffmpeg",
                prompt="Install `ffmpeg` with Homebrew for local audio processing support?",
                command_candidates=("ffmpeg", "/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"),
                install_command=("brew", "install", "ffmpeg"),
            ),
            ToolInstallPlan(
                step_name="whisper_cpp",
                prompt="Install `whisper-cpp` with Homebrew so macOS can use the same local fallback ASR path?",
                command_candidates=("whisper-cli", "/opt/homebrew/bin/whisper-cli", "/usr/local/bin/whisper-cli"),
                install_command=("brew", "install", "whisper-cpp"),
            ),
            ToolInstallPlan(
                step_name="blackhole_2ch",
                prompt="Install `BlackHole 2ch` with Homebrew Cask for meeting-output loopback capture?",
                command_candidates=(
                    "/Library/Audio/Plug-Ins/HAL/BlackHole2ch.driver",
                    "/Library/Audio/Plug-Ins/HAL/BlackHole16ch.driver",
                ),
                install_command=("brew", "install", "--cask", "blackhole-2ch"),
                reboot_required=True,
            ),
        ]

    if platform_id == WINDOWS:
        return [
            ToolInstallPlan(
                step_name="pandoc",
                prompt="Install `pandoc` for docx/PDF export support?",
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
                prompt="Install `LibreOffice` for PDF export support?",
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
                prompt="Install `ffmpeg` for local audio processing support?",
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
            prompt="Install `pandoc` for docx/PDF export support?",
            command_candidates=("pandoc",),
            install_command=(),
        ),
        ToolInstallPlan(
            step_name="libreoffice",
            prompt="Install `LibreOffice` for PDF export support?",
            command_candidates=("soffice",),
            install_command=(),
        ),
        ToolInstallPlan(
            step_name="ffmpeg",
            prompt="Install `ffmpeg` for local audio processing support?",
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
