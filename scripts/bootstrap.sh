#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV_DIR="$REPO_ROOT/.venv"
PYTHON_BIN="$VENV_DIR/bin/python"

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "가상환경을 생성합니다: $VENV_DIR"
  python3 -m venv "$VENV_DIR"
fi

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "가상환경 Python을 찾지 못했습니다: $PYTHON_BIN" >&2
  exit 1
fi

"$PYTHON_BIN" -m pip install --upgrade pip setuptools wheel
"$PYTHON_BIN" -m pip install -e "$REPO_ROOT"

echo
echo "설치가 완료되었습니다."
echo "다음 예시:"
echo "  ./scripts/zoom-meeting-bot.sh setup"
echo "  ./scripts/zoom-meeting-bot.sh init --preset launcher_dm"
echo "  ./scripts/zoom-meeting-bot.sh configure"
echo "  ./scripts/zoom-meeting-bot.sh doctor --mode launcher"
