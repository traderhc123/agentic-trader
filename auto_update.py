"""Automatic code updates: OFF unless the owner turns them on (config
"auto_update": true; dashboard Settings, the chat's "auto update on|off",
or `python agent.py auto-update on|off`).

Updates stay manual by default. When this is on, the running agent installs
an update by itself, through the SAME code as the Update button
(webui.pull_code_updates, then webui._restart_onto_update), so every rule of
a manual update still holds: code only from the official repository, a
fast-forward only (local changes or a diverged checkout are refused, never
merged over), no restart in the middle of an order. On top of those:

  - only outside market hours: on a trading day before 08:00 or from 20:00
    Eastern, or any time on a weekend or a market holiday (the owner's
    market_holidays count). Never from 08:00 to 20:00 on a trading day;
  - only when no order is being placed and no restart, import or move is
    under way. Positions held overnight are fine: they are kept, as with a
    manual update;
  - only a commit this agent itself has seen on the official branch for at
    least COOL_OFF_S (two hours): a bad or hostile push has time to be
    noticed and reverted before anyone's agent installs it. The time is
    measured from when this agent first saw the commit there, never from a
    date written in the commit (anyone pushing can write any date);
  - at most one try per night: after an install, or a failed one, nothing is
    tried again until the next night. A failure is shown on the dashboard
    and sent to the owner once.

After an install the trade log gets an activity row ("code_update") and the
owner's notifications say what was installed (old -> new commit and the
first line of each change), then the agent restarts like a manual update.

git installs may install any commit the official branch has had for two
hours (the newest such one). Zip installs (snapshot_update, no git) can
only install the newest commit on the official branch, so there it has to
have been the newest for two hours.

Everything that touches git, GitHub, orders or the restart is handed in
(`backend`), and so are the clocks, so the rules here are tested without
either."""

import json
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

KEY = "auto_update"
MARKET_TZ = ZoneInfo("America/New_York")
WINDOW_FROM_H = 20      # trading days: installs from 20:00 ET ...
WINDOW_UNTIL_H = 8      # ... until 08:00 ET
COOL_OFF_S = 2 * 3600   # seen on the official branch at least this long
CHECK_EVERY_S = 30 * 60  # one check (git fetch / GitHub API call) per 30 min
TICK_S = 60
SEEN_KEPT = 40
FILE = "auto_update.json"

TEXT = ("Installs official updates automatically between 8 PM and 8 AM ET or "
        "on weekends, only when no order is being placed. Off by default.")
SNAPSHOT_TEXT = ("This install has no git, so it installs only the newest "
                 "version, once that has been out for two hours.")
_SHA = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")


def wanted(cfg):
    """On only when the owner turned it on (exactly true)."""
    return isinstance(cfg, dict) and cfg.get(KEY) is True


def in_window(cfg, now_et):
    """True when an update may be installed: not a trading day (weekend,
    NYSE or the owner's holiday), or before 08:00 / from 20:00 ET."""
    from brokers.market_hours import is_trading_day
    if not is_trading_day(cfg, now_et.date()):
        return True
    return now_et.hour >= WINDOW_FROM_H or now_et.hour < WINDOW_UNTIL_H


def night_of(now_et):
    """The night a moment belongs to (one install per night): noon to noon
    ET, named by the first day. 20:00 Monday to 08:00 Tuesday is Monday's."""
    return (now_et - timedelta(hours=12)).date().isoformat()


# ── what is remembered between ticks and restarts (AGENT_HOME/auto_update.json)

def path(home):
    return os.path.join(home, FILE)


def load(home):
    try:
        with open(path(home), encoding="utf-8") as f:
            st = json.load(f)
    except (OSError, ValueError):
        st = {}
    if not isinstance(st, dict):
        st = {}
    seen = st.get("seen")
    st["seen"] = {str(k): float(v) for k, v in (seen or {}).items()
                  if isinstance(seen, dict) and _SHA.match(str(k))
                  and isinstance(v, (int, float)) and not isinstance(v, bool)}
    return st


def save(home, st):
    """Write the file whole (temp file + rename); never raises."""
    try:
        os.makedirs(home, exist_ok=True)
        tmp = path(home) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, indent=1, sort_keys=True)
        os.replace(tmp, path(home))
    except OSError as exc:
        print(f"auto update: could not save its notes ({str(exc)[:120]})")


def _iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds")


def _et_clock(epoch):
    return datetime.fromtimestamp(epoch, MARKET_TZ).strftime("%a %H:%M ET")


# ── one tick ─────────────────────────────────────────────────────────────────

def tick(cfg, backend, home, now=None, now_et=None):
    """Do what is due now. Returns what happened, one word:
    off, outside, done_tonight, not_due, busy, check_failed, current,
    waiting, installed, failed. Never raises."""
    now = time.time() if now is None else now
    now_et = now_et or datetime.fromtimestamp(now, MARKET_TZ)
    try:
        return _tick(cfg, backend, home, now, now_et)
    except Exception as exc:  # never stops the agent
        print(f"auto update error (non-fatal): {str(exc)[:150]}")
        return "error"


def _tick(cfg, backend, home, now, now_et):
    if not wanted(cfg):
        return "off"
    if not in_window(cfg, now_et):
        return "outside"
    st = load(home)
    night = night_of(now_et)
    if st.get("night") == night:
        return "done_tonight"
    try:
        checked = float(st.get("checked_at") or 0)
    except (TypeError, ValueError):
        checked = 0.0
    if 0 <= now - checked < CHECK_EVERY_S:
        return "not_due"
    why = backend.busy()
    if why:
        # nothing is asked of GitHub either: try again in a minute
        st["note"] = f"Waiting: {why}."
        save(home, st)
        return "busy"
    st["checked_at"] = now
    res = backend.check()
    if not isinstance(res, dict) or not res.get("ok"):
        err = str((res or {}).get("error") or "the update check failed")[:300]
        st["note"] = f"Couldn't check for updates: {err}"
        save(home, st)
        return "check_failed"
    target = str(res.get("target") or "").strip().lower()
    if not res.get("behind") or not _SHA.match(target):
        st["note"] = "Up to date."
        save(home, st)
        return "current"
    seen = st["seen"]
    seen.setdefault(target, now)
    for old in sorted(seen, key=seen.get)[:max(0, len(seen) - SEEN_KEPT)]:
        seen.pop(old, None)
    cooled = sorted((s for s, t in seen.items() if now - t >= COOL_OFF_S),
                    key=lambda s: -seen[s])
    pick = next(iter(backend.candidates(res, cooled) if cooled else ()), None)
    if pick is None:
        due = seen[target] + COOL_OFF_S
        st["note"] = (f"Update {target[:7]} found {_et_clock(seen[target])}; "
                      f"it can install from {_et_clock(due)} (two hours "
                      "after it was first seen), between 8 PM and 8 AM ET "
                      "or on a weekend.")
        save(home, st)
        return "waiting"
    why = backend.busy()  # the check can take a while: asked again
    if why:
        st["checked_at"] = 0  # check again on the next tick
        st["note"] = f"Waiting: {why}."
        save(home, st)
        return "busy"
    # one try tonight, whatever comes of it: written before the install, so
    # a crash part-way can't make it try again and again
    st["night"] = night
    save(home, st)
    old, new, subjects = backend.changes(pick, res)
    ok, msg = backend.pull(pick)
    if not ok:
        msg = str(msg or "the update failed")[:300]
        st["last"] = {"at": _iso(now), "ok": False, "from": old, "to": new,
                      "text": msg}
        st["note"] = None
        tell = st.get("told_error") != msg  # the same failure: told once
        st["told_error"] = msg
        save(home, st)
        if tell:
            backend.notify(
                "agentic-trader: the automatic update did not install "
                f"({msg}). Nothing was changed; it tries again tomorrow "
                "night. You can also update from the dashboard.")
        return "failed"
    seen.pop(pick, None)
    st["last"] = {"at": _iso(now), "ok": True, "from": old, "to": new,
                  "text": f"Installed {old} → {new}",
                  "changes": subjects[:10]}
    st["note"] = None
    st.pop("told_error", None)
    save(home, st)
    lines = "\n".join(f"• {s}" for s in subjects[:10])
    more = len(subjects) - 10
    backend.log({"action": "code_update", "auto": True, "from": old,
                 "to": new, "changes": subjects[:10],
                 "reason": (f"Updated itself from {old} to {new}"
                            + (": " + "; ".join(subjects[:5]) if subjects
                               else ""))[:600]})
    backend.notify(
        f"agentic-trader updated itself: {old} → {new}\n"
        + (lines + "\n" if lines else "")
        + (f"…and {more} more\n" if more > 0 else "")
        + "It restarts onto the new version now (after any order being "
        "placed). Your settings, positions and trade history are kept. To "
        "turn automatic updates off: say \"auto update off\" in the chat or "
        "use Settings.")
    backend.finish(msg)
    return "installed"


# ── what the dashboard shows ─────────────────────────────────────────────────

def status(cfg, home, kind=None):
    """{on, text, kind, last, note} for the dashboard. Never raises."""
    try:
        st = load(home)
    except Exception:
        st = {}
    last = st.get("last") if isinstance(st.get("last"), dict) else None
    out = {"on": wanted(cfg), "text": TEXT, "kind": kind,
           "last": ({k: last.get(k) for k in ("at", "ok", "text", "from", "to")}
                    if last else None),
           "note": st.get("note") if isinstance(st.get("note"), str) else None}
    if kind == "snapshot":
        out["text"] = TEXT + " " + SNAPSHOT_TEXT
    return out


def describe(cfg, home, kind=None):
    """One plain line for the chat and the CLI."""
    s = status(cfg, home, kind)
    line = ("Auto update: ON. " if s["on"] else "Auto update: off. ") + s["text"]
    last = s["last"]
    if last:
        line += (f" Last automatic update {last.get('at')}: "
                 + ("installed " if last.get("ok") else "FAILED: ")
                 + str(last.get("text") or ""))
    if s["on"] and s["note"]:
        line += " " + s["note"]
    return line


# ── the background thread ────────────────────────────────────────────────────

class Runner:
    """Ticks once a minute in its own thread, so a slow GitHub never holds
    up the heartbeat. Reads the config from disk every time (the switch
    takes effect without a restart)."""

    def __init__(self, get_cfg, backend, home, every=TICK_S):
        self.get_cfg = get_cfg
        self.backend = backend
        self.home = home
        self.every = every
        self.stop = threading.Event()
        self.thread = None

    def start(self):
        self.thread = threading.Thread(target=self._run, name="auto-update",
                                       daemon=True)
        self.thread.start()
        return self

    def _run(self):
        while not self.stop.wait(self.every):
            try:
                cfg = self.get_cfg() or {}
            except Exception:
                continue
            if tick(cfg, self.backend, self.home) == "installed":
                return  # restarting onto the new code
