"""Supervisor, start/stop and setup-plan audit fixes (L1-L6).

No test starts the real agent, the real supervisor process, or a server: the
supervisor loop runs in a thread of the test with a small stand-in for
`agent.py run`, inside a throwaway AGENT_HOME.
"""

import json
import os
import sys
import threading
import time

import pytest

import agent
import keep_running
import platform_compat
import setup_plan
from conftest import StubBroker, make_event

REPO = os.path.dirname(os.path.abspath(keep_running.__file__))
AUTH_EXPIRED = "Broker auth missing/expired — run: python agent.py setup"


# ── stand-ins for `agent.py run` ─────────────────────────────────────────────

def _counting_child(count_file, codes):
    """Exits with the next code from `codes` each time it is run."""
    return [sys.executable, "-c",
            "import os, sys\n"
            f"p = {str(count_file)!r}\n"
            "n = int(open(p, encoding='utf-8').read()) if os.path.exists(p) else 0\n"
            "open(p, 'w', encoding='utf-8').write(str(n + 1))\n"
            f"codes = {list(codes)!r}\n"
            "print('boom: something broke', flush=True)\n"
            "sys.exit(codes[min(n, len(codes) - 1)])\n"]


def _dies_in_startup(hold=1.5):
    """What `agent.py run` does when the broker login has expired: take the
    one-copy lock, spend a moment on its startup checks, say why, exit 1."""
    return [sys.executable, "-c",
            "import sys, time\n"
            f"sys.path.insert(0, {REPO!r})\n"
            "import keep_running\n"
            "lock = keep_running.InstanceLock()\n"
            "assert lock.acquire(wait=10)\n"
            "keep_running.clear_stop()\n"
            "getattr(keep_running, 'clear_alive', lambda: None)()\n"
            f"time.sleep({hold})\n"
            f"print({AUTH_EXPIRED!r}, flush=True)\n"
            "sys.exit(1)\n"]


def _healthy(startup=1.0):
    """A copy that passes its startup checks after `startup` seconds, then
    signals it is alive every loop until it is asked to stop."""
    return [sys.executable, "-c",
            "import sys, time\n"
            f"sys.path.insert(0, {REPO!r})\n"
            "import keep_running\n"
            "lock = keep_running.InstanceLock()\n"
            "assert lock.acquire(wait=10)\n"
            "keep_running.clear_stop()\n"
            "keep_running.clear_alive()\n"
            f"time.sleep({startup})\n"
            "while not keep_running.stop_requested():\n"
            "    keep_running.touch_alive(force=True)\n"
            "    time.sleep(0.1)\n"
            "keep_running.clear_stop()\n"]


class _Supervisor:
    """keep_running.supervise() in a thread; quacks like the Popen handle
    start_background() returns."""

    def __init__(self, cmd, **kw):
        self.result = None
        self._t = threading.Thread(target=self._run, args=(cmd, kw), daemon=True)
        self._t.start()

    def _run(self, cmd, kw):
        self.result = keep_running.supervise(cmd=cmd, **kw)

    def poll(self):
        return None if self._t.is_alive() else (self.result or 0)

    def join(self, timeout=30):
        self._t.join(timeout)
        return not self._t.is_alive()


def _fast(_seconds):
    """The supervisor's backoff sleeps 1 s at a time; make it 0.05 s."""
    time.sleep(0.05)


@pytest.fixture
def no_kill(monkeypatch):
    """The supervisor thread shares this test's pid: never let stop() kill."""
    monkeypatch.setattr(keep_running, "_kill",
                        lambda pid: pytest.fail(f"stop() tried to kill {pid}"))


@pytest.fixture
def supervisors(home):
    """Every supervisor a test starts is stopped before its home goes away."""
    made = []
    yield made
    keep_running.request_stop()
    for s in made:
        s.join(30)
    keep_running.clear_stop()


# ── L1: "running" means past the startup checks ──────────────────────────────

def test_agent_that_dies_in_startup_is_not_reported_as_running(
        home, no_kill, supervisors):
    """Expired broker login: the copy holds the lock for a moment, then
    exits. Holding the lock must not count as running."""
    sup = _Supervisor(_dies_in_startup(), sleep=_fast, max_runs=3)
    supervisors.append(sup)
    assert keep_running.wait_until_running(timeout=20, proc=sup) is False
    assert AUTH_EXPIRED in keep_running.start_failure()


def test_run_is_not_ready_while_its_startup_checks_can_still_fail(
        home, monkeypatch, capsys):
    keep_running.touch_alive(force=True)  # left by the copy that ran before
    with open(agent.CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump({"source": "manual", "broker": "alpaca", "dry_run": True,
                   "alpaca_key_id": "k", "alpaca_secret": "s"}, f)
    monkeypatch.setattr(agent, "require_consent_or_exit", lambda: None)
    seen = []

    def login(_cfg):
        seen.append((keep_running.is_running(), keep_running.is_ready()))
        # set up fine (first look), but the login itself has expired
        return object() if len(seen) == 1 else None
    monkeypatch.setattr(agent.BROKERS["alpaca"], "client", login)
    with pytest.raises(SystemExit) as e:
        agent.cmd_run()
    assert e.value.code == 1
    assert "Broker auth missing/expired" in capsys.readouterr().out
    # the one-copy lock was held all along, and it never looked "running"
    assert seen == [(True, False), (True, False)]


def test_start_says_it_did_not_start_and_why(home, no_kill, supervisors,
                                              monkeypatch, capsys):
    monkeypatch.setattr(agent, "_is_ready", lambda: True)

    def start_background(port=None):
        supervisors.append(_Supervisor(_dies_in_startup(), sleep=_fast,
                                       max_runs=3))
        return supervisors[-1]
    monkeypatch.setattr(keep_running, "start_background", start_background)
    with pytest.raises(SystemExit) as e:
        agent.cmd_start()
    out = capsys.readouterr().out
    assert e.value.code == 1
    assert "Running." not in out
    assert "did NOT start" in out and AUTH_EXPIRED in out
    # and it is not left retrying in the background with the same problem
    assert "Nothing is left running" in out
    assert supervisors[0].join(1)
    assert not keep_running.is_running()
    assert not keep_running.supervisor_running()


def test_start_says_running_once_the_agent_is_really_up(
        home, no_kill, supervisors, monkeypatch, capsys):
    monkeypatch.setattr(agent, "_is_ready", lambda: True)

    def start_background(port=None):
        supervisors.append(_Supervisor(_healthy(startup=1.0), sleep=_fast))
        return supervisors[-1]
    monkeypatch.setattr(keep_running, "start_background", start_background)
    t0 = time.time()
    agent.cmd_start()
    assert time.time() - t0 >= 0.9         # waited for the startup checks
    assert "Running." in capsys.readouterr().out
    assert keep_running.is_ready()
    said = []
    assert keep_running.stop(timeout=20, out=said.append)
    assert said[-1].startswith("Stopped.")
    assert supervisors[0].join(5) and supervisors[0].result == 0


def test_supervisor_tells_the_owner_when_the_agent_keeps_failing(home, tmp_path):
    told, got = [], threading.Event()

    def notify(text):
        told.append(text)
        got.set()
    keep_running.supervise(cmd=_dies_in_startup(hold=0), sleep=lambda s: None,
                           max_runs=4, notify=notify)
    assert got.wait(10)
    time.sleep(0.3)
    assert len(told) == 1                  # once, not on every retry
    assert "NOT running" in told[0] and "3 times in a row" in told[0]
    assert AUTH_EXPIRED in told[0]
    assert "does NOT act on exit signals" in told[0]
    assert "python agent.py stop" in told[0]
    assert not keep_running.supervisor_running()   # lock freed on the way out


def test_restart_loop_alert_never_carries_the_dashboard_login_link():
    said = ("Dashboard: http://127.0.0.1:8721/dash?k=SECRET-TOKEN\n"
            "ValueError: boom")
    msg = keep_running._loop_message(3, said, 60)
    assert "SECRET-TOKEN" not in msg and "ValueError: boom" in msg
    assert "http://127.0.0.1:8721/…" in msg


def test_status_says_when_the_agent_is_down_and_waiting(home, no_kill,
                                                        supervisors, capsys,
                                                        monkeypatch):
    monkeypatch.setattr(agent, "wallet_from_cfg", lambda cfg: None)
    waiting = threading.Event()

    def backoff(_s):
        waiting.set()
        time.sleep(0.2)
    supervisors.append(_Supervisor(_dies_in_startup(hold=0), sleep=backoff))
    assert waiting.wait(20)
    agent.cmd_status()
    first = capsys.readouterr().out.split("start at login")[0]
    assert "agent running    : NO" in first
    assert "started again" in first and AUTH_EXPIRED in first


# ── L2: stop also stops a supervisor that is waiting to restart ──────────────

def test_stop_during_crash_backoff_stops_the_supervisor(home, tmp_path,
                                                        no_kill, supervisors):
    waiting = threading.Event()

    def backoff(_s):
        waiting.set()
        time.sleep(0.05)
    count = tmp_path / "runs"
    sup = _Supervisor(_counting_child(count, [1]), sleep=backoff, max_runs=3)
    supervisors.append(sup)
    assert waiting.wait(20)                # crashed once, waiting to retry
    said = []
    assert keep_running.stop(timeout=20, out=said.append)
    assert sup.join(20)
    assert count.read_text(encoding="utf-8") == "1"   # never started again
    assert sup.result == 0
    assert not keep_running.stop_requested()
    text = " ".join(said)
    assert "isn't running." not in said    # it said what it actually did
    assert "started again automatically" in text and "Cancelled" in text
    assert "stays off until you start it again" in text


def test_stop_request_outlives_a_copy_that_was_just_starting(
        home, no_kill, supervisors):
    """A copy in its startup clears the stop request (it looks left over);
    the supervisor must still not start another one."""
    sup = _Supervisor(_dies_in_startup(hold=1.0), sleep=_fast)
    supervisors.append(sup)
    deadline = time.time() + 20
    while not keep_running.is_running() and time.time() < deadline:
        time.sleep(0.05)
    assert keep_running.is_running()       # the copy is in its startup
    assert keep_running.stop(timeout=30, out=lambda *_: None)
    assert sup.join(5) and sup.result == 0
    assert not keep_running.is_running()


def test_second_supervisor_asks_the_first_to_retry_now(home, tmp_path,
                                                       supervisors):
    waiting = threading.Event()

    def long_backoff(_s):
        waiting.set()
        time.sleep(1.0)                    # a real 15 s wait
    count = tmp_path / "runs"
    first = _Supervisor(_counting_child(count, [1, 0]), sleep=long_backoff)
    supervisors.append(first)
    assert waiting.wait(20)
    t0 = time.time()
    assert keep_running.supervise(cmd=_counting_child(count, [0])) == 0
    assert first.join(8)                   # retried at once, not after 15 s
    assert time.time() - t0 < 8
    assert count.read_text(encoding="utf-8") == "2"   # the second ran no copy


# ── L5: waking from sleep is not a frozen agent ──────────────────────────────

class _Clock:
    """Stands in for the `time` module inside keep_running."""

    def __init__(self, start, sleep_through=None):
        self.now = float(start)
        self.sleeps = 0
        self.sleep_through = sleep_through or {}   # nth sleep -> extra secs
        self.woke_at = None

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps += 1
        self.now += seconds
        if self.sleeps in self.sleep_through:
            self.now += self.sleep_through[self.sleeps]
            self.woke_at = self.now

    def strftime(self, _fmt):
        return "2026-01-01 00:00:00"


class _Proc:
    def __init__(self, on_poll):
        self.on_poll, self.polls, self.terminated = on_poll, 0, False

    def poll(self):
        self.polls += 1
        return self.on_poll(self.polls)

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0


def _watch(monkeypatch, clock, on_poll):
    proc = _Proc(on_poll)
    monkeypatch.setattr(keep_running, "time", clock)
    monkeypatch.setattr(keep_running.subprocess, "Popen", lambda *a, **k: proc)
    return proc, keep_running._run_child(["agent", "run"])


def _alive_at(when):
    os.makedirs(keep_running.home(), exist_ok=True)
    with open(keep_running._alive_path(), "w", encoding="utf-8") as f:
        f.write(str(when))
    os.utime(keep_running._alive_path(), (when, when))


def test_watchdog_does_not_end_a_healthy_agent_right_after_wake(home, monkeypatch):
    t0 = 1_700_000_000
    clock = _Clock(t0, sleep_through={2: 3 * 3600})   # laptop lid closed 3 h
    _alive_at(t0 + 1)                      # the agent was fine before sleep

    def on_poll(n):
        if n == 6:                         # a few seconds after waking…
            _alive_at(clock.now)           # …its loop gets its next turn
        return 0 if n >= 10 else None      # (then the test ends the copy)
    proc, code = _watch(monkeypatch, clock, on_poll)
    assert not proc.terminated             # was: ended the moment it woke
    assert code == 0


def test_watchdog_still_catches_an_agent_frozen_after_wake(home, monkeypatch):
    t0 = 1_700_000_000
    clock = _Clock(t0, sleep_through={2: 3 * 3600})
    _alive_at(t0 + 1)
    proc, code = _watch(monkeypatch, clock, lambda n: None)   # never answers
    assert proc.terminated and code == keep_running.HUNG_EXIT
    quiet = clock.now - clock.woke_at
    assert keep_running.HANG_SECONDS < quiet < keep_running.HANG_SECONDS + 60


def test_watchdog_unchanged_without_sleep(home, monkeypatch):
    t0 = 1_700_000_000
    clock = _Clock(t0)
    _alive_at(t0 + 1)
    proc, code = _watch(monkeypatch, clock, lambda n: None)
    assert proc.terminated and code == keep_running.HUNG_EXIT
    assert clock.now - (t0 + 1) < keep_running.HANG_SECONDS + 60


# ── L3: no restart while an order is in flight ───────────────────────────────

class _SlowBroker(StubBroker):
    """The order is at the broker; the call hasn't returned yet."""

    def __init__(self):
        super().__init__()
        self.placed, self.finish = threading.Event(), threading.Event()

    def execute(self, client, cfg, event, state):
        self.placed.set()
        assert self.finish.wait(30)
        return super().execute(client, cfg, event, state)


def _order_in_flight(broker, saved):
    state = {"positions": {}}
    t = threading.Thread(
        target=lambda: agent.handle_event(make_event(), {"dry_run": False},
                                          state, broker, object(), saved.append),
        daemon=True)
    t.start()
    assert broker.placed.wait(30)
    return t


@pytest.fixture
def execs(monkeypatch):
    """Record process replacements instead of doing them."""
    calls = []
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
    monkeypatch.setattr(os, "execv", lambda *a: calls.append(a))
    monkeypatch.setattr(platform_compat, "ORDER_WAIT_SECONDS", 0.3,
                        raising=False)
    return calls


def test_restart_waits_for_an_order_that_is_being_placed(home, no_policy, execs):
    broker, saved = _SlowBroker(), []
    order = _order_in_flight(broker, saved)
    try:
        # placed at the broker, not yet in the ledger: replacing the process
        # now would forget the position
        assert platform_compat.restart_self(["agent.py", "run"], 1) is False
        assert execs == [] and saved == []
    finally:
        broker.finish.set()
        order.join(30)
    assert saved and saved[-1]["positions"]         # the order was saved
    assert platform_compat.restart_self(["agent.py", "run"], 1) is not False
    assert len(execs) == 1                          # and then it restarts


def test_update_that_cannot_restart_resumes_and_tells_the_owner(
        home, no_policy, execs, monkeypatch, capsys):
    import webui
    told = []
    monkeypatch.setattr(agent, "notify", lambda cfg, text, **k: told.append(text))
    monkeypatch.delenv("AGENT_UPDATE_RESTARTED", raising=False)
    monkeypatch.setitem(webui.CONTROLS, "paused", True)   # update_pull did this
    broker = _SlowBroker()
    order = _order_in_flight(broker, [])
    try:
        assert webui._restart_after_update(["agent.py", "run"]) is False
    finally:
        broker.finish.set()
        order.join(30)
    assert execs == []
    assert webui.CONTROLS["paused"] is False        # not left paused forever
    assert "AGENT_UPDATE_RESTARTED" not in os.environ
    assert len(told) == 1 and "did NOT restart" in told[0]
    assert "python agent.py stop, then python agent.py start" in told[0]
    assert "Not restarting" in capsys.readouterr().out


def test_update_restart_keeps_an_owner_pause(home, execs, monkeypatch):
    import webui
    monkeypatch.setattr(agent, "notify", lambda *a, **k: None)
    monkeypatch.setitem(webui.CONTROLS, "paused", True)
    held, release = threading.Event(), threading.Event()

    def hold():
        with platform_compat.ORDER_LOCK:
            held.set()
            release.wait(30)
    t = threading.Thread(target=hold, daemon=True)
    t.start()
    assert held.wait(30)
    try:
        assert webui._restart_after_update(["agent.py", "run"],
                                           was_paused=True) is False
    finally:
        release.set()
        t.join(30)
    assert webui.CONTROLS["paused"] is True


# ── L4: a plan never makes a live agent bigger or more live ──────────────────

_LIVE_ON_PAPER = {"dry_run": False, "source": "agenthc", "broker": "alpaca",
                  "alpaca_key_id": "PK-paper", "alpaca_secret": "paper",
                  "alpaca_paper": True, "sizing_mode": "contracts",
                  "contracts_per_trade": 1, "max_contracts_per_trade": 10}


@pytest.fixture
def keys_ok(monkeypatch):
    from brokers import BROKERS
    monkeypatch.setattr(BROKERS["alpaca"], "verify", lambda c: (True, "ok"))


def test_plan_cannot_move_a_live_agent_to_real_money_keys_and_50_contracts(
        keys_ok, tmp_path):
    plan = {"broker": {"type": "alpaca", "alpaca_key_id": "AK-real",
                       "alpaca_secret": "real", "alpaca_paper": False},
            "sizing": {"mode": "contracts", "contracts_per_trade": 50},
            "safety": {"max_contracts_per_trade": 50}}
    new, rep = setup_plan.apply(plan, dict(_LIVE_ON_PAPER), True, str(tmp_path))
    assert new is None and rep["applied"] == [] and not rep["ok"]
    err = " ".join(rep["errors"])
    assert "LIVE" in err and "broker account" in err
    assert "from 1 contract(s) per trade to 50 contract(s) per trade" in err
    assert "max_contracts_per_trade from 10 to 50" in err
    assert "`dry on`" in err and "`dry off`" in err


@pytest.mark.parametrize("plan,needle", [
    ({"sizing": {"mode": "contracts", "contracts_per_trade": 2}}, "trade size"),
    ({"sizing": {"mode": "budget", "budget_per_trade_usd": 50}}, "trade size"),
    ({"safety": {"max_entries_per_day": 6}}, "max_entries_per_day from 5 to 6"),
    ({"safety": {"max_open_positions": 11}}, "max_open_positions"),
    ({"safety": {"max_daily_loss_usd": 0}}, "no limit"),
    ({"safety": {"max_daily_loss_usd": 900}}, "$900"),
    ({"broker": {"type": "robinhood"}}, "broker account"),
])
def test_plan_cannot_make_a_live_agent_bigger(plan, needle, keys_ok, tmp_path):
    cfg = dict(_LIVE_ON_PAPER, max_daily_loss_usd=300)
    new, rep = setup_plan.apply(plan, cfg, True, str(tmp_path))
    assert new is None and any(needle in e for e in rep["errors"])


def test_plan_may_make_a_live_agent_smaller_and_reports_live(keys_ok, tmp_path):
    cfg = dict(_LIVE_ON_PAPER, contracts_per_trade=5, max_daily_loss_usd=300)
    plan = {"broker": {"type": "alpaca", "alpaca_key_id": "PK-paper",
                       "alpaca_secret": "paper", "alpaca_paper": True},
            "sizing": {"mode": "contracts", "contracts_per_trade": 2},
            "safety": {"max_entries_per_day": 3, "max_daily_loss_usd": 100},
            "notifications": {"ntfy_topic": "t"}}
    new, rep = setup_plan.apply(plan, cfg, True, str(tmp_path))
    assert rep["ok"] and new["dry_run"] is False
    assert new["contracts_per_trade"] == 2 and new["alpaca_paper"] is True
    assert rep["mode"] == "LIVE" and rep["sizing"] == "2 contract(s) per trade"


def test_same_plan_is_fine_while_the_agent_is_in_dry_run(keys_ok, tmp_path):
    plan = {"broker": {"type": "alpaca", "alpaca_key_id": "AK-real",
                       "alpaca_secret": "real", "alpaca_paper": False},
            "sizing": {"mode": "contracts", "contracts_per_trade": 50}}
    cfg = dict(_LIVE_ON_PAPER, dry_run=True)
    new, rep = setup_plan.apply(plan, cfg, True, str(tmp_path))
    assert rep["ok"] and new["dry_run"] is True and rep["mode"] == "DRY-RUN"
    assert new["contracts_per_trade"] == 50 and new["alpaca_paper"] is False


def _cli_plan(monkeypatch, tmp_path, cfg, plan):
    monkeypatch.setattr(agent, "consent_ok", lambda: True)
    monkeypatch.setattr(agent, "doctor_checks", lambda: ([], cfg))
    with open(agent.CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f)
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan), encoding="utf-8")
    with pytest.raises(SystemExit) as e:
        agent.cmd_setup_from(str(path))
    return e.value.code


def test_cli_summary_states_the_real_mode_and_size(home, monkeypatch, tmp_path,
                                                   capsys):
    code = _cli_plan(monkeypatch, tmp_path, dict(_LIVE_ON_PAPER),
                     {"notifications": {"ntfy_topic": "t"}})
    out = capsys.readouterr().out
    assert code == 0
    assert "dry-run stays ON" not in out   # it was live and still is
    assert "Mode: LIVE" in out and "REAL orders" in out
    assert "Trade size now: 1 contract(s)/trade" in out


def test_cli_summary_for_a_dry_run_install(home, monkeypatch, tmp_path, capsys):
    code = _cli_plan(monkeypatch, tmp_path, {},
                     {"source": {"type": "manual"},
                      "sizing": {"mode": "contracts", "contracts_per_trade": 3}})
    out = capsys.readouterr().out
    assert code == 0 and "Mode: DRY-RUN — no real orders" in out
    assert "Trade size now: 3 contract(s)/trade" in out
    assert agent._load(agent.CONFIG_PATH)["dry_run"] is True


def test_cli_refuses_and_changes_nothing_on_a_live_agent(home, monkeypatch,
                                                         tmp_path, capsys):
    code = _cli_plan(monkeypatch, tmp_path, dict(_LIVE_ON_PAPER),
                     {"sizing": {"mode": "contracts", "contracts_per_trade": 50}})
    out = capsys.readouterr().out
    assert code == 1 and "Plan NOT applied (nothing was changed)" in out
    assert agent._load(agent.CONFIG_PATH) == _LIVE_ON_PAPER


# ── L6: Docker must not undo the owner's Stop ────────────────────────────────

def test_docker_instructions_let_a_deliberate_stop_stay_stopped():
    """The agent exits 0 when stopped on purpose and non-zero on a crash, so
    `on-failure` restarts crashes only; `unless-stopped`/`always` would start
    it trading again right after the owner pressed Stop."""
    for name in ("Dockerfile", "GETTING_STARTED.md"):
        with open(os.path.join(REPO, name), encoding="utf-8") as f:
            text = f.read()
        runs = [ln for ln in text.splitlines()
                if "docker run" in ln and "--restart" in ln]
        assert runs, name
        for ln in runs:
            assert "--restart on-failure" in ln, (name, ln)
