from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from .launcher_env import build_launcher_env, launcher_state_path
from .paths import workspace_root
from .runtime_env import package_root


def start_launcher(config: dict[str, Any], *, config_path: Path) -> dict[str, Any]:
    return _run_launcher_command(config, config_path=config_path, command="start")


def stop_launcher(config: dict[str, Any], *, config_path: Path) -> dict[str, Any]:
    return _run_launcher_command(config, config_path=config_path, command="stop")


def read_launcher_status(config: dict[str, Any], *, config_path: Path) -> dict[str, Any]:
    return _run_launcher_command(config, config_path=config_path, command="status")


def _run_launcher_command(config: dict[str, Any], *, config_path: Path, command: str) -> dict[str, Any]:
    env = build_launcher_env(config)
    process = subprocess.run(
        [sys.executable, "-m", "lush_local_ai_launcher", command],
        cwd=str(package_root()),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if process.returncode != 0:
        detail = process.stderr.strip() or process.stdout.strip() or f"exit code {process.returncode}"
        raise RuntimeError(f"Launcher {command} failed: {detail}")
    payload = process.stdout.strip()
    if not payload:
        return {
            "status": "unknown",
            "config_path": str(config_path.resolve()),
            "workspace_dir": str(workspace_root()),
            "package_dir": str(package_root()),
            "state_path": str(launcher_state_path(config)),
        }
    parsed = json.loads(payload)
    if isinstance(parsed, dict):
        parsed["config_path"] = str(config_path.resolve())
        parsed.setdefault("workspace_dir", str(workspace_root()))
        parsed.setdefault("package_dir", str(package_root()))
        parsed.setdefault("state_path", str(launcher_state_path(config)))
        return parsed
    raise RuntimeError("Launcher output was not a JSON object.")
