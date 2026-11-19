"""Automatic code updates: OFF unless the owner turns them on (config
"auto_update": true; dashboard Settings, the chat's "auto update on|off",
or `python agent.py auto-update on|off`).

Updates stay manual by default. When this is on, the running agent installs
an update by itself, through the SAME code as the Update button
(webui.pull_code_updates, then webui._restart_onto_update), so every rule of
a manual update still holds: code only from the official repository, a
fast-forward only (a diverged checkout is refused, never merged over), no
restart in the middle of an order. On top of those:

  - only outside market hours: on a trading day before 08:00 or from 20:00
    Eastern, or any time on a weekend or a market holiday (the owner's
    market_holidays count). Never from 08:00 to 20:00 on a trading day, and
    no install STARTS after 07:30 on a trading day (START_MARGIN_MIN covers
    the download, the package install and the wait for an order), the
    window is asked again with a fresh clock right before the install;
  - only when no order is being placed and no restart, import or move is
    under way. The heartbeat is put on hold (webui.hold_orders) BEFORE any
    file changes, and the order lock is asked again after that, so no order
    starts while the code under the running agent is being replaced.
    Positions held overnight are fine: they are kept, as with a manual
    update;
  - stricter than the Update button about local changes: ANY change the
    owner made to the installed code (a self-edit, an edited file, a commit
    of their own) stops it, even one a fast-forward could carry along. Those
    owners update by hand, where they see what happens;
  - only a commit this agent itself has seen on the official branch for at
    least COOL_OFF_S (two hours): a bad or hostile push has time to be
    noticed and reverted before anyone's agent installs it. The time is
    measured from when this agent first saw the commit there, never from a
    date written in the commit (anyone pushing can write any date);
  - the new code must load before the agent restarts onto it: new Python
    packages (when requirements.txt changed) must install, and agent, webui
    and the changed modules must import in a separate Python process
    (self_edit's own check). If not, the previous version is put back and
    the agent keeps running as it was;
  - at most one try per night: after an install, or a failed one, nothing is
    tried again until the next night. A "night" runs noon to noon ET, so a
    weekend or holiday has one try per day. A failure is shown on the
    dashboard and sent to the owner once.

After an install has passed those checks the trade log gets an activity row
("code_update") and the owner's notifications say what was installed (old ->
new commit and the first line of each change), then the agent restarts like
a manual update.

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
START_MARGIN_MIN = 30   # ... but none starts in the last 30 minutes
COOL_OFF_S = 2 * 3600   # seen on the official branch at least this long
CHECK_EVERY_S = 30 * 60  # one check (git fetch / GitHub API call) per 30 min
TICK_S = 60
SEEN_KEPT = 40
FILE = "auto_update.json"
# what is also kept in memory, in case the notes file can't be written
_MEM_KEYS = ("night", "checked_at", "told_error")

TEXT = ("Installs official updates automatically between 8 PM and 8 AM ET or "
        "on weekends, only when no order is being placed. Off by default.")
SNAPSHOT_TEXT = ("This install has no git, so it installs only the newest "
                 "version, once that has been out for two hours.")
_SHA = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")


def _say(text):
    """A log line that can never raise (cp1252 consoles, closed stdout)."""
    try:
        import platform_compat
        platform_compat.safe_print(text)
    except Exception:
        pass


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


def may_start(cfg, now_et):
    """True when an install may START now: inside the window, and on a
    trading day not in its last START_MARGIN_MIN minutes before 08:00 (a
    fetch, a package install and the wait for an order can take that long;
    none of it may run into the trading day)."""
    if not in_window(cfg, now_et):
        return False
    minute = now_et.hour * 60 + now_et.minute
    end = WINDOW_UNTIL_H * 60
    if end - START_MARGIN_MIN <= minute < end:
        from brokers.market_hours import is_trading_day
        return not is_trading_day(cfg, now_et.date())
    return True


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
    """Write the file whole (temp file + rename). True when it was written;
    never raises."""
    try:
        os.makedirs(home, exist_ok=True)
        tmp = path(home) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, indent=1, sort_keys=True)
        os.replace(tmp, path(home))
        return True
    except (OSError, TypeError, ValueError) as exc:
        _say(f"auto update: could not save its notes ({str(exc)[:120]})")
        return False


def _store(home, st, mem):
    """save(), and keep what limits the tries in memory as well: a notes
    file that can't be written must not mean a try (and a message to the
    owner) every minute."""
    if mem is not None:
        for k in _MEM_KEYS:
            mem[k] = st.get(k)
    return save(home, st)


def _recall(st, mem):
    """What this process last meant to save is at least as recent as the
    file (which may not have been written)."""
    for k in _MEM_KEYS:
        if mem and k in mem:
            if mem[k] is None:
                st.pop(k, None)
            else:
                st[k] = mem[k]


def _iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds")


def _et_clock(epoch):
    return datetime.fromtimestamp(epoch, MARKET_TZ).strftime("%a %H:%M ET")


def _et_when(iso):
    """'Fri 2026-11-20 03:00 ET' for a stored UTC time, else None."""
    try:
        at = datetime.fromisoformat(str(iso))
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        return at.astimezone(MARKET_TZ).strftime("%a %Y-%m-%d %H:%M ET")
    except (TypeError, ValueError):
        return None


# ── one tick ─────────────────────────────────────────────────────────────────

def tick(cfg, backend, home, now=None, now_et=None, mem=None, clock=None):
    """Do what is due now. Returns what happened, one word:
    off, outside, done_tonight, not_due, busy, check_failed, current,
    waiting, save_failed, installed, failed, error. Never raises.
    `mem`: a dict kept between ticks (Runner's). `clock`: the time right
    before the install (by default `now` when it was given, else the wall
    clock)."""
    if clock is None:
        clock = time.time if now is None else (lambda: now)
    now = time.time() if now is None else now
    now_et = now_et or datetime.fromtimestamp(now, MARKET_TZ)
    try:
        return _tick(cfg, backend, home, now, now_et, mem, clock)
    except Exception as exc:  # never stops the agent
        _say(f"auto update error (non-fatal): {str(exc)[:150]}")
        return "error"


def _tick(cfg, backend, home, now, now_et, mem, clock):
    if not wanted(cfg):
        return "off"
    if not may_start(cfg, now_et):
        return "outside"
    st = load(home)
    _recall(st, mem)
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
        _store(home, st, mem)
        return "busy"
    # saved before the check: a check that fails or raises still waits
    # CHECK_EVERY_S before the next one
    st["checked_at"] = now
    _store(home, st, mem)
    res = backend.check()
    if not isinstance(res, dict) or not res.get("ok"):
        err = str((res or {}).get("error") or "the update check failed")[:300]
        st["note"] = f"Couldn't check for updates: {err}"
        _store(home, st, mem)
        return "check_failed"
    target = str(res.get("target") or "").strip().lower()
    if not res.get("behind") or not _SHA.match(target):
        st["note"] = "Up to date."
        _store(home, st, mem)
        return "current"
    seen = st["seen"]
    seen.setdefault(target, now)
    for old in sorted(seen, key=seen.get)[:max(0, len(seen) - SEEN_KEPT)]:
        seen.pop(old, None)
    cooled = sorted((s for s, t in seen.items() if now - t >= COOL_OFF_S),
                    key=lambda s: -seen[s])
    _store(home, st, mem)  # what was seen, before anything that can raise
    pick = next(iter(backend.candidates(res, cooled) if cooled else ()), None)
    if pick is None:
        due = seen[target] + COOL_OFF_S
        st["note"] = (f"Update {target[:7]} found {_et_clock(seen[target])}; "
                      f"it can install from {_et_clock(due)} (two hours "
                      "after it was first seen), between 8 PM and 8 AM ET "
                      "or on a weekend.")
        _store(home, st, mem)
        return "waiting"
    # the heartbeat goes on hold first, then the order lock is asked again:
    # no order may start while the code under the agent is replaced
    why = backend.hold()
    if why:
        st["checked_at"] = 0  # check again on the next tick
        st["note"] = f"Waiting: {why}."
        _store(home, st, mem)
        return "busy"
    handed_over = False
    try:
        # the check (and the wait for the hold) took time: is it still
        # the night?
        later = clock()
        if not may_start(cfg, datetime.fromtimestamp(later, MARKET_TZ)):
            return "outside"
        # one try tonight, whatever comes of it: written before the
        # install, so a crash part-way can't make it try again and again
        st["night"] = night
        if not _store(home, st, mem):
            return "save_failed"  # in memory: not tried again tonight
        result = _install(backend, home, st, mem, pick, res, now)
        handed_over = result == "installed"
        return result
    finally:
        if not handed_over:
            backend.release()


def _install(backend, home, st, mem, pick, res, now):
    """Install `pick`; on any failure put back what was there. The heartbeat
    is on hold while this runs."""
    old, new, subjects = "unknown", pick[:7], []
    pulled, prep = False, None
    try:
        problem = backend.local_changes()
        if problem:
            return _failed(backend, home, st, mem, now, old, new, problem)
        old, new, subjects = backend.changes(pick, res)
        prep = backend.prepare()
        ok, msg = backend.pull(pick)
        if not ok:
            return _failed(backend, home, st, mem, now, old, new,
                           str(msg or "the update failed"))
        pulled = True
        problem, extra = backend.verify(prep)
        if problem:
            return _failed(backend, home, st, mem, now, old, new,
                           *_put_back(backend, prep, old, problem))
        msg = str(msg or "") + str(extra or "")
    except Exception as exc:
        problem = f"the update stopped part-way ({str(exc)[:200]})"
        if pulled:
            return _failed(backend, home, st, mem, now, old, new,
                           *_put_back(backend, prep, old, problem))
        return _failed(backend, home, st, mem, now, old, new, problem)
    st["seen"].pop(pick, None)
    st["last"] = {"at": _iso(now), "ok": True, "from": old, "to": new,
                  "text": f"Installed {old} → {new}",
                  "changes": subjects[:10]}
    st["note"] = None
    st.pop("told_error", None)
    _store(home, st, mem)
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


def _put_back(backend, prep, old, problem):
    """(text, changed) after the new code failed its checks: the previous
    version put back, or why it could not be."""
    try:
        ok, why = backend.rollback(prep)
    except Exception as exc:
        ok, why = False, str(exc)[:200]
    if ok:
        return (f"{problem} — the previous version ({old}) was put back and "
                "the agent keeps running it", False)
    return (f"{problem} — and the previous version ({old}) could NOT be put "
            f"back ({str(why or '')[:200]}). The agent keeps running it "
            "until its next restart; update or repair it from the dashboard "
            "before restarting", True)


def _failed(backend, home, st, mem, now, old, new, msg, changed=False):
    msg = str(msg)[:600]
    st["last"] = {"at": _iso(now), "ok": False, "from": old, "to": new,
                  "text": msg}
    st["note"] = None
    tell = st.get("told_error") != msg  # the same failure: told once
    st["told_error"] = msg
    _store(home, st, mem)
    if tell or changed:
        backend.notify(
            "agentic-trader: the automatic update did not install "
            f"({msg})."
            + ("" if changed else " Nothing was changed.")
            + " It tries again the next night (on a weekend or holiday, the "
            "next day). You can also update from the dashboard.")
    return "failed"


# ── what the dashboard shows ─────────────────────────────────────────────────

def status(cfg, home, kind=None):
    """{on, text, kind, last, note} for the dashboard. Never raises."""
    try:
        st = load(home)
    except Exception:
        st = {}
    last = st.get("last") if isinstance(st.get("last"), dict) else None
    if last:
        last = {k: last.get(k) for k in ("at", "ok", "text", "from", "to")}
        last["at_et"] = _et_when(last.get("at"))
    out = {"on": wanted(cfg), "text": TEXT, "kind": kind, "last": last,
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
        when = last.get("at_et") or last.get("at") or ""
        line += (f" Last automatic update ({when}): "
                 + ("" if last.get("ok") else "FAILED: ")
                 + str(last.get("text") or "").rstrip(".") + ".")
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
        self.mem = {}
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
            if tick(cfg, self.backend, self.home, mem=self.mem) == "installed":
                return  # restarting onto the new code
