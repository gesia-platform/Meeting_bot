from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from .runtime_env import package_root


EXCLUDED_DIR_NAMES = {
    ".git",
    ".venv",
    ".tmp",
    "__pycache__",
    "data",
}

EXCLUDED_SUFFIXES = {
    ".pyc",
    ".pyo",
}


def build_distribution_bundle(*, output_path: Path | None = None, include_notes: bool = True) -> dict[str, object]:
    root = package_root()
    destination = output_path or _default_bundle_path(root)
    destination = destination.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)

    included_files: list[str] = []
    with ZipFile(destination, "w", compression=ZIP_DEFLATED) as archive:
        for path in sorted(_iter_bundle_files(root, include_notes=include_notes)):
            relative = path.relative_to(root)
            archive.write(path, arcname=str(relative))
            included_files.append(str(relative).replace("\\", "/"))

    return {
        "bundle_path": str(destination),
        "workspace_dir": str(root),
        "included_file_count": len(included_files),
        "included_files": included_files,
    }


def _default_bundle_path(root: Path) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return root / ".dist" / f"zoom-meeting-bot-alpha-{timestamp}.zip"


def _iter_bundle_files(root: Path, *, include_notes: bool) -> list[Path]:
    files: list[Path] = []
    top_level_files = (
        "README.md",
        "pyproject.toml",
        "zoom-meeting-bot.config.example.json",
        "zoom-meeting-bot.config.metheus.example.json",
    )
    for name in top_level_files:
        path = root / name
        if path.exists():
            files.append(path)

    for relative_dir in ("scripts", "schemas", "src", "doc", "tools"):
        directory = root / relative_dir
        if directory.exists():
            files.extend(_walk_files(directory))

    if include_notes:
        notes_dir = root / "notes"
        if notes_dir.exists():
            files.extend(_walk_files(notes_dir))

    return files


def _walk_files(directory: Path) -> list[Path]:
    files: list[Path] = []
    for path in directory.rglob("*"):
        if path.is_dir():
            continue
        if any(part in EXCLUDED_DIR_NAMES or part.endswith(".egg-info") for part in path.parts):
            continue
        if path.suffix.lower() in EXCLUDED_SUFFIXES:
            continue
        files.append(path)
    return files
