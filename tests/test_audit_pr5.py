"""Audit batch PR5: config/state integrity.

1. `python agent.py run` must be the ONE `agent` module the dashboard uses:
   one CONFIG_LOCK, so a settings save can't undo a chat "dry on" (or the
   loop's safety auto-pause); config saves never share a temp file.
2. "Move computer" holds the heartbeat from the snapshot until "I saved the
   file", so nothing traded here is missing on the new computer.
3. Importing a move file is all or nothing: a failed swap part-way leaves
   this computer's setup as it was (never the bundle's LIVE config).
"""

import json
import os
import subprocess
import sys
import textwrap
import threading
import time

import pytest

import agent
import keep_running
import provision
import transfer
import webui

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PW = "correct horse battery"
LIVE = {"source": "agenthc", "broker": "stub", "dry_run": False,
        "max_entries_per_day": 2, "max_daily_loss_usd": 300.0,
        "sizing_mode": "budget", "budget_per_trade_usd": 100.0}


def _write(path, obj):
    path.write_text(obj if isinstance(obj, str) else json.dumps(obj),
                    encoding="utf-8")


def _read(path):
    return json.loads(path.read_text(encoding="utf-8"))


# ── 1. one agent module, one CONFIG_LOCK ────────────────────────────────────

_AS_SCRIPT = textwrap.dedent(r"""
    import contextlib, io, json, os, runpy, sys, threading
    src = sys.argv[1]
    sys.path.insert(0, src)
    # exactly what `python agent.py <cmd>` and platform_compat's restart
    # bootstrap do: run agent.py as __main__ (an unknown command, with
    # sys.exit neutralised so its globals come back)
    sys.argv = [os.path.join(src, "agent.py"), "__noop__"]
    real_exit, sys.exit = sys.exit, (lambda *a: None)
    with contextlib.redirect_stdout(io.StringIO()):
        MAIN = runpy.run_path(os.path.join(src, "agent.py"), run_name="__main__")
    sys.exit = real_exit
    import agent as A                # what webui / view_model handlers do
    out = {"same_lock": MAIN["CONFIG_LOCK"] is A.CONFIG_LOCK,
           "same_hold": MAIN["_STATE_HELD"] is A._STATE_HELD}
    with open(A.CONFIG_PATH, "w") as f:
        json.dump({"source": "agenthc", "broker": "alpaca", "dry_run": False,
                   "budget_per_trade_usd": 100}, f)
    read_done, chat_done = threading.Event(), threading.Event()

    def settings_page_save():        # webui: with A.CONFIG_LOCK: update_config
        def own_keys_only(disk):
            read_done.set()
            chat_done.wait(1.0)      # the read -> write window
            disk["budget_per_trade_usd"] = 250
        with A.CONFIG_LOCK:
            A.update_config(own_keys_only)

    def chat_dry_on():               # the function cmd_run hands to webui
        read_done.wait()
        MAIN["_apply_command"]("dry on")
        chat_done.set()
    ts = [threading.Thread(target=settings_page_save),
          threading.Thread(target=chat_dry_on)]
    [t.start() for t in ts]
    [t.join(20) for t in ts]
    cfg = json.load(open(A.CONFIG_PATH))
    out["dry_run"] = cfg["dry_run"]
    out["budget"] = cfg["budget_per_trade_usd"]
    print("RESULT " + json.dumps(out))
""")


def test_agent_run_as_a_script_is_the_module_the_dashboard_imports(tmp_path):
    """REVIEW: agent.py ran as __main__ and webui imported a second copy
    with its own CONFIG_LOCK: a settings-page save and a chat "dry on"
    didn't exclude each other, and the save wrote LIVE back."""
    script = tmp_path / "as_script.py"
    script.write_text(_AS_SCRIPT, encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    env = {**os.environ, "AGENT_HOME": str(home), "AGENT_REMOTE": "0"}
    p = subprocess.run([sys.executable, str(script), REPO], env=env,
                       capture_output=True, text=True, timeout=120)
    line = [x for x in p.stdout.splitlines() if x.startswith("RESULT ")]
    assert line, p.stdout[-2000:] + p.stderr[-2000:]
    out = json.loads(line[-1][len("RESULT "):])
    assert out["same_lock"] is True and out["same_hold"] is True
    assert out["dry_run"] is True          # the owner's "dry on" was kept
    assert out["budget"] == 250            # and so was the settings change


def test_two_saves_of_one_file_at_once_both_land(home, monkeypatch):
    """Both writers used `config.json.tmp`: one truncated the other's temp
    file and then swapped it away, so the other save failed."""
    path = str(home / "config.json")
    first_in_fsync, second_done = threading.Event(), threading.Event()
    real = agent._fsync
    calls = []

    def fsync(fd):
        calls.append(fd)
        if len(calls) == 1:              # the first writer, mid-save
            first_in_fsync.set()
            second_done.wait(10)
        return real(fd)
    monkeypatch.setattr(agent, "_fsync", fsync)
    errors = []

    def save(payload, after=None):
        try:
            agent._write_json(path, payload, private=True)
        except Exception as exc:          # pragma: no cover - the bug
            errors.append(repr(exc))
        finally:
            if after:
                after.set()
    a = threading.Thread(target=save, args=({"who": "A"},))
    a.start()
    assert first_in_fsync.wait(10)
    save({"who": "B"}, after=second_done)
    a.join(10)
    assert errors == []
    assert _read(home / "config.json") == {"who": "A"}   # the later swap
    assert not [n for n in os.listdir(home) if n.endswith(".tmp")]


# ── 2. "Move computer" holds the heartbeat until "I saved the file" ─────────

@pytest.fixture
def mover(home, monkeypatch):
    """The real move routes of a dashboard (running=True: the agent runs in
    this process). No port is opened, nothing leaves the machine."""
    made = {}

    class NoPort:
        def __init__(self, addr, handler):
            made["handler"] = handler

        def serve_forever(self):
            pass

        def shutdown(self):
            pass
    monkeypatch.setattr(webui, "_AppServer", NoPort)
    monkeypatch.setattr(keep_running, "disable_autostart", lambda: None)
    monkeypatch.setattr(keep_running, "is_running", lambda: False)
    monkeypatch.setattr(agent, "notify", lambda *a, **k: None)
    monkeypatch.setitem(webui.CONTROLS, "stop", False)

    def start(running=True):
        _write(home / "config.json", LIVE)
        agent._save(agent.STATE_PATH, {"seen": [], "positions": {}},
                    private=True, backup=True)
        if running:
            webui.start_app(lambda: {"mode": "TEST", "fields": {}}, lambda: [],
                            agent._apply_command,
                            lambda: agent._load(agent.CONFIG_PATH, {}) or {})
        else:
            webui.start_app()
        h = made["handler"]
        return {**h.routes_get, **h.routes_post}
    yield start
    webui.CONTROLS["stop"] = True


def test_move_file_is_made_with_the_heartbeat_held_until_finish(mover, home,
                                                                 tmp_path,
                                                                 monkeypatch):
    """REVIEW: the snapshot was taken with the agent still trading: a buy
    between "Create move file" and "I saved the file" existed only on the
    old computer, which was then retired — nobody ever sold it."""
    routes = mover()
    r = routes["/api/move/export"](None, {"passphrase": PW})
    assert r["ok"], r
    # the heartbeat places no orders now (exits included): nothing can be
    # traded here that the file doesn't hold
    assert transfer.HANDOFF["hold"] is True
    assert agent._full_hold_note()
    blob = routes["/api/move/download"](None).body   # the owner saves it
    f = routes["/api/move/finish"](None, {})
    assert f["ok"] is True and transfer.moved_away()
    assert transfer.HANDOFF["hold"] is True      # retired: never trades again
    assert webui.CONTROLS["stop"] is True
    # the new computer gets exactly what this one had when it stopped
    old_state = _read(home / "state.json")
    monkeypatch.setenv("AGENT_HOME", str(tmp_path / "new"))
    transfer.import_bundle(blob, PW)
    assert _read(tmp_path / "new" / "state.json") == old_state


def test_export_waits_for_the_cycle_in_progress_or_refuses(mover, monkeypatch):
    routes = mover()
    monkeypatch.setattr(provision, "CYCLE_WAIT_S", 0.05)
    transfer.CYCLE_IDLE.clear()          # an order is being placed right now
    try:
        r = routes["/api/move/export"](None, {"passphrase": PW})
    finally:
        transfer.CYCLE_IDLE.set()
    assert r["ok"] is False and "middle of handling" in r["error"]
    assert transfer.HANDOFF["hold"] is False     # carries on as before
    f = routes["/api/move/finish"](None, {})
    assert f["ok"] is False and transfer.moved_away() is None


def test_an_unfinished_move_lifts_the_hold_and_needs_a_new_file(mover,
                                                                monkeypatch):
    """A move left half-way must not stop exits for good: the hold lifts by
    itself, and the file made under it is then out of date."""
    monkeypatch.setattr(transfer, "MOVE_HOLD_S", 0.05)
    told = []
    monkeypatch.setattr(agent, "notify", lambda cfg, msg, *a, **k: told.append(msg))
    routes = mover()
    assert routes["/api/move/export"](None, {"passphrase": PW})["ok"]
    deadline = time.time() + 10
    while transfer.HANDOFF["hold"] and time.time() < deadline:
        time.sleep(0.01)
    assert transfer.HANDOFF["hold"] is False     # trading (and exits) resume
    assert told and "out of date" in told[0]
    f = routes["/api/move/finish"](None, {})
    assert f["ok"] is False and "move file" in f["error"]
    assert transfer.moved_away() is None and webui.CONTROLS["stop"] is False


def test_finish_after_trading_resumed_is_refused(mover, monkeypatch):
    """Whatever lifted the hold (a failed server hand-off's carry-on, …),
    the agent may have traded since the file was made: no retiring on it."""
    routes = mover()
    assert routes["/api/move/export"](None, {"passphrase": PW})["ok"]
    transfer.HANDOFF["hold"] = False
    f = routes["/api/move/finish"](None, {})
    assert f["ok"] is False and transfer.moved_away() is None


def test_export_never_lifts_a_server_hand_off(mover):
    routes = mover()
    transfer.HANDOFF["hold"] = True              # a server move holds it
    assert routes["/api/move/export"](None, {"passphrase": "short"})["ok"] is False
    assert routes["/api/move/export"](None, {"passphrase": PW})["ok"] is True
    transfer.HANDOFF["hold"] = True
    routes["/api/move/export"](None, {"passphrase": PW})
    assert transfer.HANDOFF["hold"] is True


def test_export_from_the_wizard_is_refused_while_the_agent_runs_elsewhere(
        mover, monkeypatch):
    routes = mover(running=False)
    monkeypatch.setattr(keep_running, "is_running", lambda: True)
    r = routes["/api/move/export"](None, {"passphrase": PW})
    assert r["ok"] is False and "python agent.py stop" in r["error"]


def test_cli_export_stops_a_running_agent_before_the_copy(home, monkeypatch,
                                                          tmp_path):
    _write(home / "config.json", LIVE)
    order, up = [], {"on": True}
    monkeypatch.setattr(keep_running, "is_running", lambda: up["on"])
    monkeypatch.setattr(keep_running, "supervisor_running", lambda: False)
    monkeypatch.setattr(keep_running, "disable_autostart", lambda: None)

    def stop(*a, **k):
        order.append("stop")
        up["on"] = False
        return True
    monkeypatch.setattr(keep_running, "stop", stop)
    real_export = transfer.export_bundle
    monkeypatch.setattr(transfer, "export_bundle",
                        lambda pw: (order.append("export"), real_export(pw))[1])
    import getpass
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": PW)
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")
    agent.cmd_export(str(tmp_path / "move.atmove"))
    assert order[:2] == ["stop", "export"]


def test_cli_export_refuses_when_the_owner_keeps_the_agent_running(
        home, monkeypatch, tmp_path):
    _write(home / "config.json", LIVE)
    monkeypatch.setattr(keep_running, "is_running", lambda: True)
    monkeypatch.setattr(keep_running, "supervisor_running", lambda: False)
    monkeypatch.setattr(keep_running, "stop",
                        lambda *a, **k: pytest.fail("stopped without asking"))
    import getpass
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": PW)
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    with pytest.raises(SystemExit):
        agent.cmd_export(str(tmp_path / "move.atmove"))
    assert not (tmp_path / "move.atmove").exists()


# ── 3. import: all of the move file, or none of it ──────────────────────────

@pytest.fixture
def two_homes(tmp_path, monkeypatch):
    """A LIVE setup with a position on the old computer, packed; this
    computer's own setup in practice mode. Returns (blob, new home)."""
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    new.mkdir()
    _write(old / "config.json", {"source": "agenthc", "broker": "alpaca",
                                 "dry_run": False, "alpaca_key": "BUNDLE"})
    _write(old / "state.json", {"seen": [], "positions": {"QQQ|x": {"qty": 2}}})
    _write(old / "trades.jsonl", '{"action": "entry"}\n')
    monkeypatch.setenv("AGENT_HOME", str(old))
    blob = transfer.export_bundle(PW)
    _write(new / "config.json", {"source": "agenthc", "broker": "alpaca",
                                 "dry_run": True, "alpaca_key": "MINE"})
    _write(new / "state.json", {"seen": [], "positions": {}})
    _write(new / "trades.jsonl", "")
    monkeypatch.setenv("AGENT_HOME", str(new))
    monkeypatch.setattr(agent, "_REPLACE_WAIT_S", 0)
    return blob, new


def _snapshot(h):
    return {n: (h / n).read_bytes() for n in sorted(os.listdir(h))}


def _failing_replace(monkeypatch, fails):
    real = os.replace

    def replace(src, dst):
        if fails(src, dst):
            raise PermissionError(13, "[WinError 5] Access is denied", dst)
        return real(src, dst)
    monkeypatch.setattr(os, "replace", replace)


def test_a_file_that_cannot_be_replaced_part_way_changes_nothing(two_homes,
                                                                 monkeypatch):
    """REVIEW: config.json (sorted first) was already the bundle's — LIVE,
    the forced dry-run never ran — when trades.jsonl failed to swap."""
    blob, new = two_homes
    before = _snapshot(new)
    _failing_replace(monkeypatch, lambda s, d: d.endswith("trades.jsonl"))
    with pytest.raises(transfer.TransferError) as e:
        transfer.import_bundle(blob, PW, replace=True)
    assert not isinstance(e.value, transfer.ImportIncomplete)
    assert "nothing was imported" in str(e.value)
    assert _snapshot(new) == before            # every file as it was
    assert _read(new / "config.json")["alpaca_key"] == "MINE"


def test_a_settings_file_that_cannot_be_replaced_changes_nothing(two_homes,
                                                                 monkeypatch):
    blob, new = two_homes
    before = _snapshot(new)
    _failing_replace(monkeypatch, lambda s, d: d.endswith("config.json")
                     and "bak" not in s)        # the last swap, not a put-back
    with pytest.raises(transfer.TransferError):
        transfer.import_bundle(blob, PW, replace=True)
    assert _snapshot(new) == before


def test_no_imported_file_is_ever_on_disk_with_live_settings(two_homes,
                                                             monkeypatch):
    """The forced dry-run is in the staged config.json itself, and that one
    goes in last."""
    blob, new = two_homes
    seen = []
    real = os.replace

    def replace(src, dst):
        r = real(src, dst)
        seen.append((os.path.basename(dst), _read(new / "config.json")))
        return r
    monkeypatch.setattr(os, "replace", replace)
    info = transfer.import_bundle(blob, PW, replace=True)
    assert info["was_live"] is True
    assert seen[-1][0] == "config.json"
    assert all(cfg["dry_run"] is True for _n, cfg in seen)
    assert _read(new / "config.json")["alpaca_key"] == "BUNDLE"
    assert _read(new / "state.json")["positions"] == {"QQQ|x": {"qty": 2}}
    assert not [n for n in os.listdir(new) if n.endswith(".tmp")]


def test_a_rollback_that_fails_says_so_and_leaves_practice_mode(two_homes,
                                                                monkeypatch):
    blob, new = two_homes
    _write(new / "config.json", {"source": "agenthc", "broker": "alpaca",
                                 "dry_run": False, "alpaca_key": "MINE"})
    _failing_replace(monkeypatch, lambda s, d: d.endswith("trades.jsonl") or (
        s.endswith(transfer._BACKUP_SUFFIX) and d.endswith("state.json")))
    with pytest.raises(transfer.ImportIncomplete):
        transfer.import_bundle(blob, PW, replace=True)
    cfg = _read(new / "config.json")
    assert cfg["dry_run"] is True                # never LIVE on a mix


def _import_route(app_routes_factory, monkeypatch):
    import platform_compat
    monkeypatch.setitem(webui.CONTROLS, "hold_until", 0.0)
    monkeypatch.setenv("AGENT_UPDATE_RESTARTED", "")
    monkeypatch.setattr("time.sleep", lambda s: None)
    monkeypatch.setattr(transfer, "decrypt", lambda blob, pw: b"")
    monkeypatch.setattr(transfer, "existing_setup", lambda: False)
    monkeypatch.setattr(platform_compat, "restart_self", lambda argv, port: None)
    return app_routes_factory()["/api/move/import"]


def test_dashboard_import_that_stopped_part_way_never_releases_the_agent(
        mover, monkeypatch):
    route = _import_route(mover, monkeypatch)
    assert transfer.ImportIncomplete   # (missing before this fix)

    def half(blob, pw, replace=False):
        raise transfer.ImportIncomplete("the import stopped part-way")
    monkeypatch.setattr(transfer, "import_bundle", half)
    r = route(None, {"data": "", "passphrase": "pw"})
    assert r["ok"] is False and "part-way" in r["error"]
    assert webui.orders_held()                   # no orders on a mixed setup
    assert webui.setup_replaced()                # memory never saved over it


def test_dashboard_import_that_changed_nothing_carries_on(mover, monkeypatch):
    route = _import_route(mover, monkeypatch)

    def clean(blob, pw, replace=False):
        raise transfer.TransferError("nothing was imported")
    monkeypatch.setattr(transfer, "import_bundle", clean)
    r = route(None, {"data": "", "passphrase": "pw"})
    assert r["ok"] is False and not webui.orders_held()
    assert not webui.setup_replaced()


# ── review fixups: a Move never leaves exits / the expiry guard off ─────────

def _cli_agent(monkeypatch, answers, pws=(PW, PW)):
    """A running agent for `agent.py export`; records stop / start."""
    calls, up = [], {"on": True}
    monkeypatch.setattr(keep_running, "is_running", lambda: up["on"])
    monkeypatch.setattr(keep_running, "supervisor_running", lambda: False)
    monkeypatch.setattr(keep_running, "disable_autostart", lambda: None)
    monkeypatch.setattr(keep_running, "supervisor_status", lambda: {})

    def stop(*a, **k):
        calls.append("stop")
        up["on"] = False
        return True

    def start_background(port=None):
        calls.append("start")
        up["on"] = True
    monkeypatch.setattr(keep_running, "stop", stop)
    monkeypatch.setattr(keep_running, "start_background", start_background)
    monkeypatch.setattr(keep_running, "wait_until_running",
                        lambda *a, **k: up["on"])
    import getpass
    pw_iter = iter(pws)
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": next(pw_iter))
    ans = iter(answers)
    monkeypatch.setattr("builtins.input", lambda prompt="": next(ans))
    return calls, up


def test_cli_export_passphrase_typo_never_stops_the_agent(home, monkeypatch,
                                                          tmp_path, capsys):
    """REVIEW: the agent was stopped first, then a passphrase mismatch
    exited and left it stopped (no exits, no expiry guard)."""
    _write(home / "config.json", LIVE)
    calls, up = _cli_agent(monkeypatch, ["y"], pws=(PW, PW + "x"))
    with pytest.raises(SystemExit):
        agent.cmd_export(str(tmp_path / "move.atmove"))
    assert "stop" not in calls and up["on"] is True
    assert not (tmp_path / "move.atmove").exists()


def test_cli_export_that_fails_after_the_stop_starts_the_agent_again(
        home, monkeypatch, tmp_path, capsys):
    _write(home / "config.json", LIVE)
    calls, up = _cli_agent(monkeypatch, ["y"])

    def boom(pw):
        raise transfer.TransferError("disk full")
    monkeypatch.setattr(transfer, "export_bundle", boom)
    with pytest.raises(SystemExit):
        agent.cmd_export(str(tmp_path / "move.atmove"))
    assert calls == ["stop", "start"] and up["on"] is True
    assert transfer.moved_away() is None
    assert "running again" in capsys.readouterr().out


def test_cli_export_kept_here_starts_the_agent_again(home, monkeypatch,
                                                     tmp_path, capsys):
    """REVIEW: answering "n" (keep trading here) left the agent stopped."""
    _write(home / "config.json", LIVE)
    calls, up = _cli_agent(monkeypatch, ["y", "n"])
    agent.cmd_export(str(tmp_path / "move.atmove"))
    assert (tmp_path / "move.atmove").exists()
    assert calls == ["stop", "start"] and up["on"] is True
    assert transfer.moved_away() is None
    assert "out of date" in capsys.readouterr().out


def test_cli_export_that_cannot_restart_says_it_is_stopped(home, monkeypatch,
                                                           tmp_path, capsys):
    _write(home / "config.json", LIVE)
    calls, up = _cli_agent(monkeypatch, ["y", "n"])
    monkeypatch.setattr(keep_running, "start_background",
                        lambda port=None: calls.append("start"))
    monkeypatch.setattr(keep_running, "wait_until_running",
                        lambda *a, **k: False)
    agent.cmd_export(str(tmp_path / "move.atmove"))
    out = capsys.readouterr().out
    assert "STOPPED" in out and "python agent.py start" in out


def test_cli_export_retired_does_not_start_it_again(home, monkeypatch,
                                                    tmp_path):
    _write(home / "config.json", LIVE)
    calls, up = _cli_agent(monkeypatch, ["y", "y"])
    agent.cmd_export(str(tmp_path / "move.atmove"))
    assert calls == ["stop"] and up["on"] is False
    assert transfer.moved_away()


def _expiring_today(monkeypatch, hh, mm):
    from datetime import datetime
    now = datetime(2026, 11, 17, hh, mm, tzinfo=agent.MARKET_TZ)  # a Tuesday
    monkeypatch.setattr(agent, "_et_now", lambda: now)
    agent._save(agent.STATE_PATH, {"seen": [], "positions": {
        "SPY|2026-11-17|600|call": {"qty": 1}}}, private=True, backup=True)


def test_export_refused_when_the_hold_could_reach_the_expiry_sell(
        mover, monkeypatch):
    """REVIEW: an unfinished move holds the expiry guard too — pressed at
    15:40 with a contract expiring today, nothing would sell it at 15:45."""
    monkeypatch.setattr(transfer, "MOVE_HOLD_S", 600)
    routes = mover()
    _expiring_today(monkeypatch, 15, 40)
    r = routes["/api/move/export"](None, {"passphrase": PW})
    assert r["ok"] is False and "expires today" in r["error"]
    assert "15:45" in r["error"]
    assert transfer.HANDOFF["hold"] is False           # exits carry on
    assert routes["/api/move/download"](None)["ok"] is False
    _expiring_today(monkeypatch, 10, 0)                # hours to go: fine
    assert routes["/api/move/export"](None, {"passphrase": PW})["ok"] is True


def test_finish_while_the_time_limit_runs_out_stays_retired(mover,
                                                            monkeypatch):
    """REVIEW: the 10-minute timer firing between finish's checks and its
    retiring lifted the hold of a computer that was being retired."""
    told = []
    monkeypatch.setattr(agent, "notify",
                        lambda cfg, msg, *a, **k: told.append(msg))
    timers = []

    class Timer:
        def __init__(self, s, fn, args=()):
            timers.append((fn, args))
            self.daemon = True

        def start(self):
            pass
    monkeypatch.setattr(webui.threading, "Timer", Timer)
    routes = mover()
    assert routes["/api/move/export"](None, {"passphrase": PW})["ok"]
    fn, args = timers[0]
    real_mark = transfer.mark_moved_away
    fired = []

    def mark():
        t = threading.Thread(target=fn, args=args)
        t.start()
        t.join(0.3)        # the timer, at exactly this moment
        fired.append(t)
        return real_mark()
    monkeypatch.setattr(transfer, "mark_moved_away", mark)
    assert routes["/api/move/finish"](None, {})["ok"] is True
    fired[0].join(5)
    assert transfer.HANDOFF["hold"] is True and transfer.moved_away()
    assert not told                                   # no "carries on"


def test_a_second_request_during_an_export_never_lifts_its_hold(mover):
    """REVIEW: a finish arriving while the export waited for the cycle
    lifted the hold; the export then made a file nothing held."""
    routes = mover()
    transfer.CYCLE_IDLE.clear()        # a cycle is running
    out = {}
    t1 = threading.Thread(target=lambda: out.setdefault(
        "export", routes["/api/move/export"](None, {"passphrase": PW})))
    t1.start()
    deadline = time.time() + 10
    while not transfer.HANDOFF["hold"] and time.time() < deadline:
        time.sleep(0.01)
    t2 = threading.Thread(target=lambda: out.setdefault(
        "finish", routes["/api/move/finish"](None, {})))
    t2.start()
    time.sleep(0.2)
    transfer.CYCLE_IDLE.set()          # the cycle ends
    t1.join(10)
    t2.join(10)
    assert out["export"]["ok"] is True
    # whatever the order, nothing trades here behind a file that was handed
    # out: either retired (held for good), or the file is still held
    assert transfer.HANDOFF["hold"] is True
    d = routes["/api/move/download"](None)
    assert getattr(d, "body", None) is None or transfer.HANDOFF["hold"]


def test_wizard_export_refused_while_a_supervisor_may_start_the_agent(
        mover, monkeypatch):
    routes = mover(running=False)
    monkeypatch.setattr(keep_running, "supervisor_running", lambda: True)
    r = routes["/api/move/export"](None, {"passphrase": PW})
    assert r["ok"] is False and "python agent.py stop" in r["error"]


def test_wizard_finish_refused_when_the_agent_started_after_the_file(
        mover, monkeypatch):
    routes = mover(running=False)
    up = {"on": False}
    monkeypatch.setattr(keep_running, "is_running", lambda: up["on"])
    monkeypatch.setattr(keep_running, "supervisor_running", lambda: False)
    assert routes["/api/move/export"](None, {"passphrase": PW})["ok"]
    up["on"] = True                    # start-at-login, `agent.py start`, ...
    r = routes["/api/move/finish"](None, {})
    assert r["ok"] is False and transfer.moved_away() is None


def test_server_hand_off_after_an_export_keeps_the_hold(mover, monkeypatch):
    """REVIEW (probe-confirmed): Create move file, then Move to a server:
    when the export's 10 minutes ran out its timer lifted the hold the
    server hand-off relied on — this computer traded alongside the
    server."""
    import llm_policy
    told = []
    monkeypatch.setattr(agent, "notify",
                        lambda cfg, msg, *a, **k: told.append(msg))
    monkeypatch.setattr(transfer, "MOVE_HOLD_S", 0.3)
    monkeypatch.setattr(llm_policy, "entries_blocked", lambda cfg: False)
    monkeypatch.setattr(provision, "create_droplet",
                        lambda token, region: (7, "droplet 7 creating"))
    monkeypatch.setattr(provision, "LOCAL_STOP_CAP_S", 0)
    monkeypatch.setattr(provision, "LOCAL_STOP_AFTER_SHOWN_S", 0)
    before = set(threading.enumerate())
    routes = mover()
    assert routes["/api/move/export"](None, {"passphrase": PW})["ok"]
    r = routes["/api/deploy"](None, {"token": "t", "region": "nyc3"})
    assert r["ok"], r
    time.sleep(0.8)                    # the export's time limit runs out
    assert transfer.HANDOFF["hold"] is True and transfer.moved_away()
    assert not [m for m in told if "carries on trading" in m]
    assert routes["/api/move/download"](None)["ok"] is False   # stale file
    assert routes["/api/move/finish"](None, {})["ok"] is False
    assert transfer.HANDOFF["hold"] is True
    for t in set(threading.enumerate()) - before:
        t.join(5)


def test_import_never_leaves_live_settings_beside_imported_files(two_homes,
                                                                 monkeypatch):
    """REVIEW: a crash/kill part-way through the swaps (no rollback runs)
    left this computer's own LIVE settings next to the old computer's
    positions. They are practice mode on disk before anything is swapped."""
    blob, new = two_homes
    _write(new / "config.json", {"source": "agenthc", "broker": "alpaca",
                                 "dry_run": False, "alpaca_key": "MINE"})
    seen = []
    real = os.replace

    def replace(src, dst):
        if not os.path.basename(dst).startswith("config.json"):
            seen.append((os.path.basename(dst),
                         _read(new / "config.json")["dry_run"]))
        return real(src, dst)
    monkeypatch.setattr(os, "replace", replace)
    transfer.import_bundle(blob, PW, replace=True)
    assert seen and all(dry is True for _n, dry in seen), seen
    assert _read(new / "config.json")["alpaca_key"] == "BUNDLE"
    assert not [n for n in os.listdir(new) if n.endswith(".tmp")]


def test_clean_rollback_puts_live_settings_back_exactly(two_homes,
                                                        monkeypatch):
    blob, new = two_homes
    _write(new / "config.json", {"source": "agenthc", "broker": "alpaca",
                                 "dry_run": False, "alpaca_key": "MINE"})
    before = _snapshot(new)
    _failing_replace(monkeypatch, lambda s, d: d.endswith("trades.jsonl"))
    with pytest.raises(transfer.TransferError) as e:
        transfer.import_bundle(blob, PW, replace=True)
    assert not isinstance(e.value, transfer.ImportIncomplete)
    assert _snapshot(new) == before


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_saved_files_keep_their_usual_permissions(home):
    """REVIEW: mkstemp made every saved file 0600, private or not."""
    mask = os.umask(0o022)
    os.umask(mask)
    p = str(home / "preview.json")
    agent._save(p, {"a": 1})
    assert os.stat(p).st_mode & 0o777 == 0o666 & ~mask
    agent._save(str(home / "secret.json"), {"a": 1}, private=True)
    assert os.stat(str(home / "secret.json")).st_mode & 0o777 == 0o600
