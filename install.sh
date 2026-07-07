#!/usr/bin/env bash
# agentic-trader installer — clones the repo and installs dependencies.
#
#   curl -fsSL https://raw.githubusercontent.com/traderhc123/agentic-trader/main/install.sh | bash
#
# Self-sufficient: if Python 3.10+ isn't on the machine (stock macOS ships
# 3.9), it installs a private Python via uv (no sudo, no system changes
# beyond ~/.local). This installer does NOT run the agent and does NOT accept
# any agreement for you — trading requires the interactive consent gate in
# `agent.py setup`, which you must complete yourself afterwards.
set -euo pipefail

REPO="https://github.com/traderhc123/agentic-trader"
DIR="${AGENTIC_TRADER_DIR:-$HOME/agentic-trader}"

echo "== agentic-trader installer =="
command -v git >/dev/null 2>&1 || { echo "ERROR: git is required (macOS: run 'xcode-select --install')"; exit 1; }

py_ok() { "$1" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; }

PYTHON=""
for c in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$c" >/dev/null 2>&1 && py_ok "$c"; then PYTHON="$(command -v "$c")"; break; fi
done

if [ -z "$PYTHON" ]; then
  echo "Python 3.10+ not found — installing a private Python 3.12 via uv (no sudo)…"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
  uv python install 3.12
  PYTHON="$(uv python find 3.12)"
  py_ok "$PYTHON" || { echo "ERROR: automatic Python install failed"; exit 1; }
  echo "Using $PYTHON"
fi

if [ -d "$DIR/.git" ]; then
  echo "Existing install found at $DIR — updating…"
  git -C "$DIR" pull --ff-only
else
  git clone --depth 1 "$REPO" "$DIR"
fi

cd "$DIR"
if [ ! -x .venv/bin/python ] || ! py_ok .venv/bin/python; then
  rm -rf .venv
  "$PYTHON" -m venv .venv
fi
./.venv/bin/python -m pip install --quiet --upgrade pip
./.venv/bin/pip install --quiet -r requirements.txt

echo
echo "Installed to $DIR"
echo
echo "Next steps (run these yourself — the agent will not act without your"
echo "explicit, interactive consent to DISCLAIMER.md):"
echo
echo "  cd $DIR"
echo "  ./.venv/bin/python agent.py setup --web   # browser wizard"
echo "  ./.venv/bin/python agent.py run           # heartbeat + dashboard"
echo
echo "Not investment advice. You accept all liability — read DISCLAIMER.md."
