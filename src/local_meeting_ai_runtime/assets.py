"""Helpers for locating and preparing external runtime assets."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import sys
from typing import Any


def runtime_asset_root() -> Path:
    configured = os.getenv("DELEGATE_ASSET_ROOT", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    local_appdata = os.getenv("LOCALAPPDATA", "").strip()
    if local_appdata:
        return (Path(local_appdata).expanduser() / "local-meeting-ai-runtime").resolve()
    xdg_cache_home = os.getenv("XDG_CACHE_HOME", "").strip()
    if xdg_cache_home:
        return (Path(xdg_cache_home).expanduser() / "local-meeting-ai-runtime").resolve()
    return (Path.home() / ".cache" / "local-meeting-ai-runtime").resolve()


def whisper_cpp_asset_root(asset_root: str | Path | None = None) -> Path:
    base = Path(asset_root).expanduser() if asset_root is not None else runtime_asset_root()
    if base.name.lower() == "whisper.cpp":
        return base.resolve()
    return (base / "whisper.cpp").resolve()


def bundled_whisper_cpp_roots() -> list[Path]:
    candidates = [
        Path(__file__).resolve().parents[2] / "tools" / "whisper.cpp",
        Path.cwd().resolve() / "tools" / "whisper.cpp",
    ]
    roots: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        key = str(resolved).casefold()
        if key in seen:
            continue
        seen.add(key)
        roots.append(resolved)
    return roots


def whisper_cpp_search_roots(asset_root: str | Path | None = None) -> list[Path]:
    return [whisper_cpp_asset_root(asset_root), *bundled_whisper_cpp_roots()]


def find_whisper_cpp_cli(asset_root: str | Path | None = None) -> Path | None:
    for root in whisper_cpp_search_roots(asset_root):
        match = _find_whisper_cpp_cli_in_root(root)
        if match is not None:
            return match
    return None


def find_whisper_cpp_model(model_name: str, asset_root: str | Path | None = None) -> Path | None:
    normalized_model_name = str(model_name or "").strip() or "base"
    for root in whisper_cpp_search_roots(asset_root):
        match = _find_whisper_cpp_model_in_root(root, normalized_model_name)
        if match is not None:
            return match
    return None


def find_whisper_cpp_vad_model(
    *,
    asset_root: str | Path | None = None,
    preferred_model_path: str | Path | None = None,
) -> Path | None:
    candidate_roots: list[Path] = []
    if preferred_model_path:
        preferred = Path(preferred_model_path).expanduser()
        if preferred.exists():
            candidate_roots.append(preferred.parent.resolve())
    candidate_roots.extend(whisper_cpp_search_roots(asset_root))

    seen: set[str] = set()
    deduped_roots: list[Path] = []
    for root in candidate_roots:
        resolved = root.resolve()
        key = str(resolved).casefold()
        if key in seen:
            continue
        seen.add(key)
        deduped_roots.append(resolved)

    matches: list[Path] = []
    for root in deduped_roots:
        matches.extend(_find_whisper_cpp_vad_models_in_root(root))

    if not matches:
        return None
    matches.sort(
        key=lambda path: (
            1 if "for-tests" in path.name.lower() else 0,
            -path.stat().st_size if path.exists() else 0,
            path.name.lower(),
        )
    )
    return matches[0].resolve()


def whisper_cpp_asset_status(
    *,
    model_name: str,
    asset_root: str | Path | None = None,
) -> dict[str, Any]:
    resolved_asset_root = whisper_cpp_asset_root(asset_root)
    cli_path = find_whisper_cpp_cli(asset_root)
    model_path = find_whisper_cpp_model(model_name, asset_root)
    vad_path = find_whisper_cpp_vad_model(asset_root=asset_root, preferred_model_path=model_path)
    cli_in_asset_root = _path_under_root(cli_path, resolved_asset_root)
    model_in_asset_root = _path_under_root(model_path, resolved_asset_root)
    vad_in_asset_root = _path_under_root(vad_path, resolved_asset_root)
    return {
        "asset_root": str(resolved_asset_root),
        "model_name": str(model_name or "").strip() or "base",
        "search_roots": [str(root) for root in whisper_cpp_search_roots(asset_root)],
        "whisper_cli_path": str(cli_path) if cli_path else None,
        "model_path": str(model_path) if model_path else None,
        "vad_model_path": str(vad_path) if vad_path else None,
        "cli_in_asset_root": cli_in_asset_root,
        "model_in_asset_root": model_in_asset_root,
        "vad_in_asset_root": vad_in_asset_root,
        "external_asset_root_ready": bool(cli_in_asset_root and model_in_asset_root),
        "ready": bool(cli_path and model_path),
    }


def prepare_whisper_cpp_assets(
    *,
    model_name: str,
    asset_root: str | Path | None = None,
    source_root: str | Path | None = None,
    include_vad: bool = True,
    overwrite: bool = False,
) -> dict[str, Any]:
    normalized_model_name = str(model_name or "").strip() or "base"
    resolved_asset_root = whisper_cpp_asset_root(asset_root)
    resolved_source_root = _resolve_source_root(source_root)

    cli_source = _find_whisper_cpp_cli_in_root(resolved_source_root)
    model_source = _find_whisper_cpp_model_in_root(resolved_source_root, normalized_model_name)
    vad_source = _find_preferred_vad_source(resolved_source_root) if include_vad else None

    copied: list[dict[str, str]] = []
    existing: list[dict[str, str]] = []
    missing: list[str] = []

    if cli_source is None:
        missing.append("whisper_cli")
    else:
        destination = resolved_asset_root / "bin" / cli_source.name
        _copy_asset(cli_source, destination, overwrite=overwrite, copied=copied, existing=existing)

    if model_source is None:
        missing.append(f"model:{normalized_model_name}")
    else:
        destination = resolved_asset_root / "models" / model_source.name
        _copy_asset(model_source, destination, overwrite=overwrite, copied=copied, existing=existing)

    if include_vad:
        if vad_source is None:
            missing.append("vad_model")
        else:
            destination = resolved_asset_root / "models" / vad_source.name
            _copy_asset(vad_source, destination, overwrite=overwrite, copied=copied, existing=existing)

    return {
        "asset_root": str(resolved_asset_root),
        "source_root": str(resolved_source_root),
        "model_name": normalized_model_name,
        "copied": copied,
        "existing": existing,
        "missing": missing,
        "ready": not missing or missing == ["vad_model"],
    }


def _resolve_source_root(source_root: str | Path | None) -> Path:
    if source_root is not None:
        resolved = Path(source_root).expanduser().resolve()
        if not resolved.exists():
            raise FileNotFoundError(f"whisper.cpp source root was not found: {resolved}")
        return resolved
    for candidate in bundled_whisper_cpp_roots():
        if candidate.exists():
            return candidate
    raise FileNotFoundError("No bundled whisper.cpp source root is available for asset preparation.")


def _find_whisper_cpp_cli_in_root(root: Path) -> Path | None:
    if sys.platform.startswith("win"):
        candidates = (
            Path("bin/whisper-cli.exe"),
            Path("build/bin/Release/whisper-cli.exe"),
            Path("build/bin/whisper-cli.exe"),
            Path("bin/whisper-cli"),
            Path("build/bin/Release/whisper-cli"),
            Path("build/bin/whisper-cli"),
        )
    else:
        candidates = (
            Path("build-macos/bin/whisper-cli"),
            Path("build-macos/bin/Release/whisper-cli"),
            Path("bin/whisper-cli"),
            Path("build/bin/Release/whisper-cli"),
            Path("build/bin/whisper-cli"),
        )
    for relative in candidates:
        candidate = root / relative
        if candidate.exists():
            return candidate.resolve()
    return None


def _find_whisper_cpp_model_in_root(root: Path, model_name: str) -> Path | None:
    for candidate in (
        root / "models" / f"ggml-{model_name}.bin",
        root / f"ggml-{model_name}.bin",
    ):
        if candidate.exists():
            return candidate.resolve()
    return None


def _find_whisper_cpp_vad_models_in_root(root: Path) -> list[Path]:
    matches: list[Path] = []
    for relative in (
        Path("models"),
        Path("."),
    ):
        search_root = (root / relative).resolve()
        if not search_root.exists():
            continue
        for pattern in ("ggml-silero-v*.bin", "silero-v*-ggml.bin", "for-tests-silero-v*-ggml.bin"):
            matches.extend(path.resolve() for path in search_root.glob(pattern))
    return matches


def _find_preferred_vad_source(root: Path) -> Path | None:
    matches = _find_whisper_cpp_vad_models_in_root(root)
    if not matches:
        return None
    matches.sort(
        key=lambda path: (
            1 if "for-tests" in path.name.lower() else 0,
            -path.stat().st_size if path.exists() else 0,
            path.name.lower(),
        )
    )
    return matches[0]


def _copy_asset(
    source: Path,
    destination: Path,
    *,
    overwrite: bool,
    copied: list[dict[str, str]],
    existing: list[dict[str, str]],
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and not overwrite:
        existing.append({"source": str(source), "destination": str(destination)})
        return
    shutil.copy2(source, destination)
    copied.append({"source": str(source), "destination": str(destination)})


def _path_under_root(path: Path | None, root: Path) -> bool:
    if path is None:
        return False
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False
