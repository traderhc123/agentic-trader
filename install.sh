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

# git is preferred but NOT required — a stock Mac has no git until the Xcode
# CLT dialog is answered, which is exactly the wall a first-time user hits.
# Without git we download a snapshot tarball instead (curl+tar are always
# present on macOS/Linux). `.venv` and any local edits are left in place.
fetch_snapshot() {
  echo "git not found — downloading a snapshot of the code instead…"
  TMP="$(mktemp -d)"
  curl -fsSL "https://codeload.github.com/traderhc123/agentic-trader/tar.gz/refs/heads/main" \
    | tar -xz -C "$TMP"
  mkdir -p "$DIR"
  cp -R "$TMP"/agentic-trader-*/. "$DIR"/
  rm -rf "$TMP"
}

if command -v git >/dev/null 2>&1; then
  if [ -d "$DIR/.git" ]; then
    echo "Existing install found at $DIR — updating…"
    git -C "$DIR" pull --ff-only
  elif [ -f "$DIR/agent.py" ]; then
    fetch_snapshot   # tarball install being re-run on a box that now has git
  else
    git clone --depth 1 "$REPO" "$DIR"
  fi
else
  fetch_snapshot
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

# One-paste experience: on a desktop, launch the browser setup wizard now.
# The wizard collects consent IN THE BROWSER (you still read DISCLAIMER.md and
# type the acceptance phrase yourself), so auto-launching does not bypass the
# consent gate. On a headless server (no display) we print instructions
# instead, since the browser must be reached over an SSH tunnel there.
# Opt out with AGENTIC_TRADER_NO_LAUNCH=1.
is_desktop() {
  [ "$(uname -s)" = "Darwin" ] && return 0        # macOS
  [ -n "${DISPLAY:-}" ] || [ -n "${WAYLAND_DISPLAY:-}" ] && return 0
  return 1
}

if [ -z "${AGENTIC_TRADER_NO_LAUNCH:-}" ] && is_desktop; then
  echo "Launching the setup wizard in your browser…"
  echo "(read DISCLAIMER.md in the page and accept it yourself; Ctrl-C here to stop)"
  echo
  exec ./.venv/bin/python agent.py setup --web
else
  echo "Next steps (run these yourself — the agent will not act without your"
  echo "explicit, in-browser consent to DISCLAIMER.md):"
  echo
  echo "  cd $DIR"
  echo "  ./.venv/bin/python agent.py setup --web   # browser wizard"
  echo "  ./.venv/bin/python agent.py doctor        # verify everything is ready"
  echo "  ./.venv/bin/python agent.py run           # heartbeat + dashboard"
  echo
  echo "On a remote server, tunnel first, then open http://127.0.0.1:8721 :"
  echo "  ssh -L 8721:127.0.0.1:8721 user@your-server"
  echo
  echo "Not investment advice. You accept all liability — read DISCLAIMER.md."
fi
