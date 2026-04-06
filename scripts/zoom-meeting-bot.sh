#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV_PYTHON="$REPO_ROOT/.venv/bin/python"

bootstrap_homebrew_path() {
  if command -v brew >/dev/null 2>&1; then
    eval "$(brew shellenv)"
    return
  fi
  if [[ -x /opt/homebrew/bin/brew ]]; then
    eval "$(/opt/homebrew/bin/brew shellenv)"
    return
  fi
  if [[ -x /usr/local/bin/brew ]]; then
    eval "$(/usr/local/bin/brew shellenv)"
  fi
}

if [[ "$(uname -s)" == "Darwin" ]]; then
  bootstrap_homebrew_path
fi

if [[ -x "$VENV_PYTHON" ]]; then
  PYTHON_EXE="$VENV_PYTHON"
else
  PYTHON_EXE="python3"
fi

export PYTHONPATH="$REPO_ROOT/src"

cd "$REPO_ROOT"
exec "$PYTHON_EXE" -m zoom_meeting_bot_cli "$@"
