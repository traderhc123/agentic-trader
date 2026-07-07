#!/usr/bin/env bash
# agentic-trader installer — clones the repo and installs dependencies.
#
#   curl -fsSL https://raw.githubusercontent.com/traderhc123/agentic-trader/main/install.sh | bash
#
# This installer does NOT run the agent and does NOT accept any agreement for
# you. Trading actions require the interactive consent gate in
# `python agent.py setup`, which you must run yourself afterwards.
set -euo pipefail

REPO="https://github.com/traderhc123/agentic-trader"
DIR="${AGENTIC_TRADER_DIR:-$HOME/agentic-trader}"

echo "== agentic-trader installer =="

command -v git >/dev/null 2>&1 || { echo "ERROR: git is required"; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "ERROR: python3 is required"; exit 1; }
python3 - <<'PY' || { echo "ERROR: Python 3.10+ is required"; exit 1; }
import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)
PY

if [ -d "$DIR/.git" ]; then
  echo "Existing install found at $DIR — updating…"
  git -C "$DIR" pull --ff-only
else
  git clone --depth 1 "$REPO" "$DIR"
fi

cd "$DIR"
if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
./.venv/bin/pip install --quiet --upgrade pip
./.venv/bin/pip install --quiet -r requirements.txt

echo
echo "Installed to $DIR"
echo
echo "Next steps (run these yourself — the agent will not act without your"
echo "explicit, interactive consent to DISCLAIMER.md):"
echo
echo "  cd $DIR"
echo "  ./.venv/bin/python agent.py setup"
echo "  ./.venv/bin/python agent.py run"
echo
echo "Not investment advice. You accept all liability — read DISCLAIMER.md."
