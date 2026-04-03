#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV_PYTHON="$REPO_ROOT/.venv/bin/python"

if [[ -x "$VENV_PYTHON" ]]; then
  PYTHON_EXE="$VENV_PYTHON"
else
  PYTHON_EXE="python3"
fi

export PYTHONPATH="$REPO_ROOT/src"

cd "$REPO_ROOT"
exec "$PYTHON_EXE" -m zoom_meeting_bot_cli "$@"
