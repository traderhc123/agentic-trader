"""Signal source: your own commands, appended to a local file.

Fully self-directed: YOU decide the trades; the agent just executes them on
the heartbeat. Append one JSON object per line to
``~/.agentic-trader/commands.jsonl``:

    {"action": "enter", "ticker": "SPY", "expiry": "2026-07-10", "strike": 752, "type": "C"}
    {"action": "exit",  "ticker": "SPY", "expiry": "2026-07-10", "strike": 752, "type": "C"}

Each line is executed once (tracked by a content hash). ``type`` is "C" or
"P". Exits only close positions this agent opened.
"""

import hashlib
import json
import os

NAME = "manual"
DESCRIPTION = "Your own commands from a local commands.jsonl file (self-directed)"


def _commands_path():
    home = os.path.expanduser(os.getenv("AGENT_HOME", "~/.agentic-trader"))
    return os.path.join(home, "commands.jsonl")


def poll(cfg, state, save_state=lambda s: None):
    path = _commands_path()
    if not os.path.exists(path):
        return []
    events = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                cmd = json.loads(line)
            except ValueError:
                continue
            action = str(cmd.get("action", "")).lower()
            if action not in ("enter", "exit"):
                continue
            try:
                events.append({
                    "event": "ENTERED" if action == "enter" else "EXITED",
                    "ticker": str(cmd["ticker"]).upper(),
                    "expiry": str(cmd["expiry"]),
                    "strike": float(cmd["strike"]),
                    "type": "C" if str(cmd["type"]).upper().startswith("C") else "P",
                    "event_id": "manual-" + hashlib.sha1(line.encode()).hexdigest()[:16],
                    "message": f"manual {action}: {line[:120]}",
                })
            except (KeyError, TypeError, ValueError):
                continue
    return events


def setup(cfg):
    print("\n-- Manual command file --")
    print(f"Append JSON lines to: {_commands_path()}")
    print('Example: {"action": "enter", "ticker": "SPY", "expiry": "2026-07-10",'
          ' "strike": 752, "type": "C"}')
    os.makedirs(os.path.dirname(_commands_path()), exist_ok=True)
    return cfg
