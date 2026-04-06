#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV_DIR="$REPO_ROOT/.venv"
PYTHON_BIN="$VENV_DIR/bin/python"

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

ensure_homebrew() {
  if [[ "$(uname -s)" != "Darwin" ]]; then
    return
  fi
  bootstrap_homebrew_path
  if command -v brew >/dev/null 2>&1; then
    return
  fi
  if ! command -v curl >/dev/null 2>&1; then
    echo "curl was not found. Install curl first or install Homebrew manually." >&2
    exit 1
  fi
  echo "Homebrew was not found. Installing Homebrew with the official installer..."
  NONINTERACTIVE=1 /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  bootstrap_homebrew_path
  if ! command -v brew >/dev/null 2>&1; then
    echo "Homebrew installation did not complete successfully." >&2
    exit 1
  fi
}

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 was not found. Install Python 3.11+ first." >&2
  exit 1
fi

ensure_homebrew

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "Creating virtual environment: $VENV_DIR"
  python3 -m venv "$VENV_DIR"
fi

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "Virtual environment Python was not found: $PYTHON_BIN" >&2
  exit 1
fi

"$PYTHON_BIN" -m pip install --upgrade pip "setuptools<82" wheel
"$PYTHON_BIN" -m pip install -e "$REPO_ROOT"

echo
echo "Bootstrap finished."
if [[ "$(uname -s)" == "Darwin" ]]; then
  bootstrap_homebrew_path
  echo "- Homebrew is ready: $(command -v brew)"
  echo "- quickstart will install LibreOffice, ffmpeg, whisper-cpp, and BlackHole when needed."
  echo "- If BlackHole is installed for the first time, macOS may require one reboot before meeting-output capture becomes available."
fi
echo "Next commands:"
echo "  ./scripts/zoom-meeting-bot.sh quickstart --preset launcher_dm --yes"
echo "  ./scripts/zoom-meeting-bot.sh create-session \"meeting_link\" --passcode \"passcode\" --open"
