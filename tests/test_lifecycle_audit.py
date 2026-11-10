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
    # Like stop(): keep the request up until they are all gone. A stand-in
    # that is only just starting removes the request (as the real agent
    # does), and would otherwise run on after the test, for ever.
    deadline = time.time() + 30
    while any(s.poll() is None for s in made) and time.time() < deadline:
        keep_running.request_stop()
        time.sleep(0.1)
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


# ═════════════════════════════════════════════════════════════════════════════
# Review fixes
# ═════════════════════════════════════════════════════════════════════════════

# ── L1: the restart-loop alert must not go silent ────────────────────────────

def _supervise_runs(monkeypatch, runs):
    """The real supervise() loop on a fake clock; each (seconds, exit code)
    in `runs` is one life of the agent. Returns [(when, alert text)]."""
    clock = _Clock(1_700_000_000)
    left, told = list(runs) + [(1, 1)], []   # (the last run is never judged)

    def run_child(_cmd):
        seconds, code = left.pop(0)
        clock.now += seconds
        return code
    monkeypatch.setattr(keep_running, "time", clock)
    monkeypatch.setattr(keep_running, "_run_child", run_child)
    monkeypatch.setattr(keep_running, "_child_said", lambda: AUTH_EXPIRED)
    monkeypatch.setattr(keep_running, "_tell", lambda notify, text: notify(text))
    keep_running.supervise(
        cmd=["agent", "run"], sleep=clock.sleep, max_runs=len(left),
        notify=lambda text: told.append((clock.now - 1_700_000_000, text)))
    assert not left
    return told


def test_owner_is_told_again_when_it_fails_again_after_working(home, monkeypatch):
    """09:00 login expired -> alert. The owner fixes it, the agent works for
    30 minutes, then fails 11 times in a row over the next hour."""
    told = _supervise_runs(
        monkeypatch, [(5, 1)] * 3 + [(1800, 1)] + [(5, 1)] * 11)
    assert len(told) == 2                  # was 1: silent for the next 6 hours
    assert "3 times in a row" in told[1][1] and "NOT running" in told[1][1]
    assert told[1][0] - told[0][0] < 3600  # soon after it began, not hours on
    assert told[1][0] < 6 * 3600


def test_one_long_failure_loop_is_still_one_alert_per_six_hours(home, monkeypatch):
    told = _supervise_runs(monkeypatch, [(5, 1)] * 20)   # ~2.5 h of retries
    assert len(told) == 1


@pytest.mark.parametrize("minutes", [22, 35])   # frozen at once / after 15 min
def test_owner_is_told_when_the_agent_keeps_freezing(home, monkeypatch, minutes):
    """Every frozen run lasts over 20 minutes, which used to count as "it
    ran fine for 10 minutes" and reset the count: never an alert."""
    told = _supervise_runs(
        monkeypatch, [(minutes * 60, keep_running.HUNG_EXIT)] * 10)
    assert len(told) == 1                  # was 0
    when, text = told[0]
    assert when < 3 * minutes * 60 + 60    # at the third freeze
    assert "keeps freezing" in text and "3 times in the last 6 hours" in text
    assert "does NOT act on exit signals" in text
    assert "python agent.py stop" in text


def test_a_freeze_now_and_then_is_not_a_loop(home, monkeypatch):
    week = 7 * 86400
    assert _supervise_runs(
        monkeypatch, [(week, keep_running.HUNG_EXIT)] * 5) == []


# ── L2: start/stop while the supervisor is only just launching the agent ────

def _slow_to_start(delay):
    """`agent.py run` on a slow computer: `delay` seconds of importing before
    it even takes the one-copy lock, then healthy until asked to stop."""
    return [sys.executable, "-c",
            "import sys, time\n"
            f"sys.path.insert(0, {REPO!r})\n"
            "import keep_running\n"
            f"end = time.time() + {delay}\n"
            "while time.time() < end:\n"
            "    time.sleep(0.05)\n"
            "lock = keep_running.InstanceLock()\n"
            "assert lock.acquire(wait=10)\n"
            "keep_running.clear_stop()\n"
            "keep_running.clear_alive()\n"
            "while not keep_running.stop_requested():\n"
            "    keep_running.touch_alive(force=True)\n"
            "    time.sleep(0.1)\n"
            "keep_running.clear_stop()\n"]


def _wait_for(check, timeout=20):
    deadline = time.time() + timeout
    while not check() and time.time() < deadline:
        time.sleep(0.05)
    return check()


def test_second_supervisor_leaves_no_retry_request_during_a_normal_start(
        home, no_kill, supervisors):
    first = _Supervisor(_slow_to_start(4.0), sleep=_fast)
    supervisors.append(first)
    assert _wait_for(keep_running.supervisor_running)
    assert not keep_running.is_running()   # the agent is still "importing"
    assert keep_running.supervise(cmd=_counting_child(home / "n", [0])) == 0
    # nothing failed, so there is nothing to "try again now" — a request left
    # here would skip the wait after the agent's first real crash, days on
    assert not os.path.exists(keep_running._retry_path())
    assert not (home / "n").exists()       # and it ran no second copy


def test_a_left_over_retry_request_does_not_skip_the_crash_backoff(
        home, tmp_path, supervisors):
    slept = []

    def backoff(_s):
        if not slept:                      # as if written before this wait
            with open(keep_running._retry_path(), "w", encoding="utf-8") as f:
                f.write(str(time.time() - 3600))
        slept.append(1)
    keep_running.supervise(cmd=_counting_child(tmp_path / "n", [1, 0]),
                           sleep=backoff)
    assert len(slept) == 15                # the whole 15 s wait, not cut short


def test_start_during_a_normal_start_does_not_claim_an_earlier_failure(
        home, no_kill, supervisors, monkeypatch, capsys):
    monkeypatch.setattr(agent, "_is_ready", lambda: True)
    monkeypatch.setattr(
        keep_running, "start_background",
        lambda port=None: pytest.fail("started a second supervisor"))
    supervisors.append(_Supervisor(_slow_to_start(1.5), sleep=_fast))
    assert _wait_for(keep_running.supervisor_running)
    assert not keep_running.is_running()
    agent.cmd_start()
    out = capsys.readouterr().out
    assert "earlier start failed" not in out
    assert "already starting" in out and "Running." in out


def test_stop_during_a_normal_start_says_what_it_did(home, no_kill, supervisors):
    sup = _Supervisor(_slow_to_start(1.5), sleep=_fast)
    supervisors.append(sup)
    assert _wait_for(keep_running.supervisor_running)
    said = []
    assert keep_running.stop(timeout=30, out=said.append)
    text = " ".join(said)
    assert "stopped with an error" not in text       # it had not
    assert "just being started" in text and said[-1].startswith("Stopped.")
    assert sup.join(5) and sup.result == 0


def test_stop_right_after_start_does_not_miss_the_new_supervisor(
        home, no_kill, supervisors, monkeypatch):
    """`stop` a second after `start`: the supervisor has not taken its lock
    yet. By the end of stop's short wait it has, but its agent has not come
    up. stop said "The agent isn't running." and the agent then started."""
    monkeypatch.setattr(keep_running, "OLD_SUPERVISOR_WAIT", 1.0)
    launch = threading.Timer(
        0.3, lambda: supervisors.append(_Supervisor(_slow_to_start(2.0),
                                                    sleep=_fast)))
    launch.start()
    said = []
    try:
        assert keep_running.stop(timeout=30, out=said.append)
    finally:
        launch.join(10)
    assert "The agent isn't running." not in said
    assert supervisors and supervisors[0].join(10)
    assert not keep_running.supervisor_running()
    assert not keep_running.is_running()


@pytest.mark.parametrize("note_pid,ended", [(4242, [4242]), (999, []), (None, [])])
def test_stop_only_ends_a_supervisor_its_own_note_vouches_for(
        home, monkeypatch, note_pid, ended):
    """supervisor.pid copied from another computer names some other program
    here: never end a process the supervisor's own note doesn't confirm."""
    killed = []
    monkeypatch.setattr(keep_running, "time", _Clock(1_700_000_000))
    monkeypatch.setattr(keep_running, "is_running", lambda: False)
    monkeypatch.setattr(keep_running, "supervisor_running", lambda: True)
    monkeypatch.setattr(keep_running, "supervisor_pid", lambda: 4242)
    monkeypatch.setattr(keep_running, "_kill", killed.append)
    if note_pid is not None:
        with open(keep_running._status_path(), "w", encoding="utf-8") as f:
            json.dump({"state": "running", "pid": note_pid}, f)
    said = []
    assert keep_running.stop(timeout=5, out=said.append) is False
    assert killed == ended
    assert said[-1].startswith("Could not stop it")
    keep_running.clear_stop()


@pytest.mark.parametrize("ends_when_asked,kills", [
    (True, [(4242, False)]), (False, [(4242, False), (4242, True)])])
def test_stop_still_ends_an_agent_that_ignores_being_asked(
        home, monkeypatch, ends_when_asked, kills):
    """SIGTERM is now a polite stop request to the agent, so `stop`'s last
    resort must go on to end a copy that is too frozen to react to it."""
    killed, alive = [], [True]

    def kill(pid, force=False):
        killed.append((pid, force))
        if ends_when_asked or force:
            alive[0] = False
    monkeypatch.setattr(keep_running, "time", _Clock(1_700_000_000))
    monkeypatch.setattr(keep_running, "is_running", lambda: alive[0])
    monkeypatch.setattr(keep_running, "running_pid",
                        lambda: 4242 if alive[0] else None)
    monkeypatch.setattr(keep_running, "supervisor_running", lambda: False)
    monkeypatch.setattr(keep_running, "_kill", kill)
    said = []
    assert keep_running.stop(timeout=5, out=said.append) is True
    assert killed == kills
    assert said[-1] == "Stopped."


def test_retry_asked_the_moment_the_note_says_waiting_counts(
        home, tmp_path, monkeypatch, supervisors):
    """A second `start` reacts to the supervisor's "waiting" note. A request
    written at that very moment must not be thrown away as left over."""
    slept = []
    note = keep_running._set_status

    def set_status(**fields):
        note(**fields)
        if fields.get("state") == "waiting":
            keep_running.request_retry()
    monkeypatch.setattr(keep_running, "_set_status", set_status)
    keep_running.supervise(cmd=_counting_child(tmp_path / "n", [1, 0]),
                           sleep=slept.append)
    assert slept == []                     # tried again at once, no 15 s wait
    assert (tmp_path / "n").read_text(encoding="utf-8") == "2"


def test_status_does_not_claim_a_failure_while_it_is_being_started(
        home, no_kill, supervisors, capsys, monkeypatch):
    monkeypatch.setattr(agent, "wallet_from_cfg", lambda cfg: None)
    supervisors.append(_Supervisor(_slow_to_start(3.0), sleep=_fast))
    assert _wait_for(keep_running.supervisor_running)
    assert _wait_for(
        lambda: keep_running.supervisor_status().get("state") == "running")
    assert not keep_running.is_running()   # the agent is still "importing"
    agent.cmd_status()
    first = capsys.readouterr().out.split("start at login")[0]
    assert "agent running    : starting" in first and "not trading yet" in first
    assert "stopped with an error" not in first


def test_move_file_never_carries_this_computers_supervisor_files():
    import transfer
    for name in ("supervisor.pid", "supervisor.status", "retry.request",
                 "agent.alive", "agent.pid", "supervisor.lock"):
        assert not transfer._travels(name), name
    assert transfer._travels("config.json")


# ── L1: the launcher must not say "already running" for a starting copy ─────

def test_launcher_does_not_call_a_starting_copy_running(home, monkeypatch, capsys):
    import webbrowser
    monkeypatch.setattr(webbrowser, "open",
                        lambda *a, **k: pytest.fail("opened a dead dashboard"))
    lock = keep_running.InstanceLock()     # a copy in its startup checks
    assert lock.acquire()
    try:
        keep_running.clear_alive()
        assert agent._open_running_dashboard() is False
        assert "already running" not in capsys.readouterr().out
        keep_running.touch_alive(force=True)   # past them: really running
        opened = []
        monkeypatch.setattr(webbrowser, "open", lambda link: opened.append(link))
        assert agent._open_running_dashboard() is True
        assert opened and "already running" in capsys.readouterr().out
    finally:
        lock.release()


def test_launcher_goes_on_to_start_which_reports_the_failed_start(
        home, monkeypatch):
    import webbrowser
    calls = []
    monkeypatch.setattr(webbrowser, "open", lambda *a, **k: None)
    monkeypatch.setattr(agent.sys, "argv", ["agent.py"])
    monkeypatch.setattr(agent, "_is_ready", lambda: True)
    monkeypatch.setattr(agent, "cmd_start", lambda **k: calls.append(k))
    import net_check
    monkeypatch.setattr(net_check, "use_system_trust", lambda: None)
    lock = keep_running.InstanceLock()
    assert lock.acquire()
    try:
        keep_running.clear_alive()
        agent.main()
    finally:
        lock.release()
    assert calls == [{"open_dashboard": True}]


def test_setup_page_is_not_opened_on_top_of_a_starting_copy(home, monkeypatch,
                                                            capsys):
    import net_check
    import webui
    monkeypatch.setattr(net_check, "use_system_trust", lambda: None)
    monkeypatch.setattr(agent.sys, "argv", ["agent.py", "setup", "--web"])
    monkeypatch.setattr(webui, "run_wizard",
                        lambda *a, **k: pytest.fail("wizard took the port"))
    monkeypatch.setattr(agent, "_open_running_dashboard", lambda wait=0.0: False)
    lock = keep_running.InstanceLock()
    assert lock.acquire()
    try:
        keep_running.clear_alive()
        agent.main()
    finally:
        lock.release()
    out = capsys.readouterr().out
    assert "still starting" in out and "not trading yet" in out


# ── L3: order reconcile is an order in flight too ────────────────────────────

def _lock_is_free():
    """Asked from another thread, like the restart is."""
    got = []

    def ask():
        if platform_compat.ORDER_LOCK.acquire(blocking=False):
            platform_compat.ORDER_LOCK.release()
            got.append(True)
    t = threading.Thread(target=ask)
    t.start()
    t.join(10)
    return bool(got)


def test_restart_waits_while_orders_are_being_reconciled(home, monkeypatch):
    """Reconcile cancels a pending order, places its replacement and saves
    only at the end: a restart in between loses the replacement."""
    import brokers
    import webui
    seen = []

    class Broker:
        @staticmethod
        def client(cfg):
            return object()

        @staticmethod
        def reconcile(client, cfg, state, save_state):
            seen.append(_lock_is_free())

        @staticmethod
        def execute(client, cfg, event, state):
            return False

    class Source:
        @staticmethod
        def poll(cfg, st, save_state):
            return []

    def no_dashboard(*a, **k):
        raise OSError("no dashboard in tests")

    def sleep(wait, controls):
        raise KeyboardInterrupt
    monkeypatch.setattr(agent, "require_consent_or_exit", lambda: None)
    monkeypatch.setattr(brokers, "broker_ready", lambda c: True)
    monkeypatch.setitem(agent.BROKERS, "stub", Broker)
    monkeypatch.setitem(agent.SOURCES, "stub", Source)
    monkeypatch.setattr(webui, "start_dashboard", no_dashboard)
    monkeypatch.setattr(keep_running, "refresh_autostart", lambda: False)
    monkeypatch.setattr(agent, "_refresh_cash", lambda *a, **k: None)
    monkeypatch.setattr(agent, "_position_upkeep", lambda *a, **k: None)
    monkeypatch.setattr(agent, "_maybe_daily_digest", lambda *a, **k: None)
    monkeypatch.setattr(agent, "_market_open_now", lambda: True)
    monkeypatch.setattr(agent, "notify", lambda *a, **k: None)
    monkeypatch.setitem(webui.CONTROLS, "paused", False)
    monkeypatch.setitem(webui.CONTROLS, "stop", False)
    monkeypatch.setattr(agent, "_sleep_unless_stopped", sleep)
    with open(agent.CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump({"source": "stub", "broker": "stub"}, f)
    with open(agent.STATE_PATH, "w", encoding="utf-8") as f:
        json.dump({"seen": [], "positions": {}}, f)
    agent.cmd_run()
    assert seen == [False]                 # held while reconcile ran
    assert _lock_is_free()                 # and released afterwards


def _stuck_order(monkeypatch):
    """restart_self gives up at once, as after a 120 s wait for an order."""
    told = []
    monkeypatch.setattr(platform_compat, "restart_self", lambda argv, port: False)
    monkeypatch.setattr(agent, "notify",
                        lambda cfg, text, **k: told.append((text, k)))
    return told


def test_import_that_cannot_restart_tells_the_owner_it_is_paused(
        home, monkeypatch):
    import webui
    told = _stuck_order(monkeypatch)
    monkeypatch.delenv("AGENT_UPDATE_RESTARTED", raising=False)
    monkeypatch.setitem(webui.CONTROLS, "paused", True)   # the import did this
    assert webui._restart_after_import(["agent.py", "run"]) is False
    assert webui.CONTROLS["paused"] is True   # memory still holds the old setup
    assert "AGENT_UPDATE_RESTARTED" not in os.environ
    (text, kw), = told
    assert "did NOT restart" in text and "PAUSED" in text
    assert "does NOT act on exit signals" in text
    assert "python agent.py stop, then python agent.py start" in text
    assert kw.get("priority") == "high"


def test_self_edit_that_cannot_restart_tells_the_owner(home, tmp_path,
                                                       monkeypatch):
    import self_edit
    import webui
    told = _stuck_order(monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    monkeypatch.setattr(self_edit, "_repo", lambda: str(repo))
    monkeypatch.setitem(webui.CONTROLS, "paused", False)
    monkeypatch.delenv("AGENT_UPDATE_RESTARTED", raising=False)
    with open(os.path.join(str(home), "proposal.json"), "w",
              encoding="utf-8") as f:
        json.dump({"request": "test", "explanation": "", "warnings": [],
                   "changes": [{"path": "module.py",
                                "content": "VALUE = 2\n"}]}, f)
    ok, msg = self_edit.apply_and_restart(restart=True)
    assert ok and "Restarting" in msg
    assert _wait_for(lambda: bool(told), 10)
    text = told[0][0]
    assert "code change" in text and "did NOT restart" in text
    assert "keeps trading as before" in text
    assert "python agent.py stop, then python agent.py start" in text
    assert "AGENT_UPDATE_RESTARTED" not in os.environ


# ── L4: a plan never makes a live agent trade MORE in any way ────────────────

_LIVE_REAL = {"dry_run": False, "source": "agenthc", "broker": "alpaca",
              "alpaca_key_id": "AK-real", "alpaca_secret": "real",
              "alpaca_paper": False, "sizing_mode": "contracts",
              "contracts_per_trade": 5, "include_other_trades": False,
              "rule_tickers": ["SPY", "QQQ"], "rule_skip_tickers": ["TSLA"],
              "rule_sides": "calls", "rule_min_strike": 10.0,
              "rule_skip_0dte": True, "rule_loss_streak": 2}


@pytest.mark.parametrize("cfg_over,plan,needle", [
    # a `manual` install places nothing by itself; a feed makes it trade
    ({"source": "manual"}, {"source": {"type": "url", "url": "https://x.example/f"}},
     "where its trade signals come from (from manual to url)"),
    ({"source": "manual"}, {"source": {"type": "agenthc"}},
     "where its trade signals come from"),
    ({"source": "url", "source_url": "https://a.example/f"},
     {"source": {"type": "url", "url": "https://b.example/f"}},
     "web address its trade signals come from"),
    ({}, {"source": {"type": "agenthc", "include_other_trades": True}},
     "every trade on the feed"),
    ({}, {"rules": {"tickers": []}}, "tickers outside its list (SPY, QQQ)"),
    ({}, {"rules": {"tickers": ["SPY", "QQQ", "NVDA"]}}, "tickers outside its list"),
    ({}, {"rules": {"skip_tickers": []}}, "off its skip list (TSLA)"),
    ({}, {"rules": {"sides": "both"}}, "stop trading calls only"),
    ({}, {"rules": {"sides": "puts"}}, "stop trading calls only"),
    ({}, {"rules": {"min_strike": 5}}, "minimum strike from $10 to $5"),
    ({}, {"rules": {"min_strike": 0}}, "to no minimum"),
    ({}, {"rules": {"skip_0dte": False}}, "expire the same day (0DTE)"),
    ({}, {"rules": {"loss_streak": 0}}, "after 2 losses in a row"),
    ({}, {"rules": {"loss_streak": 4}}, "after 2 losses in a row (only after 4)"),
])
def test_plan_cannot_make_a_live_agent_trade_more(cfg_over, plan, needle,
                                                  tmp_path):
    cfg = dict(_LIVE_REAL, **cfg_over)
    new, rep = setup_plan.apply(plan, dict(cfg), True, str(tmp_path))
    assert new is None and rep["applied"] == [] and not rep["ok"]
    err = " ".join(rep["errors"])
    assert "LIVE" in err and needle in err
    # the same plan is fine once the owner has switched to dry-run
    new, rep = setup_plan.apply(plan, dict(cfg, dry_run=True), True,
                                str(tmp_path))
    assert rep["ok"] and new["dry_run"] is True and rep["mode"] == "DRY-RUN"


def test_plan_cannot_give_a_live_agent_its_first_signal_source(tmp_path):
    """dry_run already false but no source yet: nothing was trading. A plan
    that adds one would start real orders with nobody having confirmed it."""
    cfg = {k: v for k, v in _LIVE_REAL.items() if k != "source"}
    new, rep = setup_plan.apply({"source": {"type": "agenthc"}}, dict(cfg),
                                True, str(tmp_path))
    assert new is None
    assert "(from none to agenthc)" in " ".join(rep["errors"])


def test_plan_may_make_a_live_agents_rules_stricter(tmp_path):
    plan = {"source": {"type": "agenthc", "include_other_trades": False},
            "rules": {"tickers": ["SPY"], "skip_tickers": ["TSLA", "GME"],
                      "sides": "calls", "min_strike": 20, "skip_0dte": True,
                      "loss_streak": 1}}
    new, rep = setup_plan.apply(plan, dict(_LIVE_REAL), True, str(tmp_path))
    assert rep["ok"], rep["errors"]
    assert rep["mode"] == "LIVE" and new["dry_run"] is False
    assert new["rule_tickers"] == ["SPY"] and new["rule_loss_streak"] == 1
    # first rules on an agent that had none are stricter too
    bare = {k: v for k, v in _LIVE_REAL.items() if not k.startswith("rule_")}
    new, rep = setup_plan.apply({"rules": {"tickers": ["SPY"], "sides": "puts",
                                           "skip_0dte": True}},
                                bare, True, str(tmp_path))
    assert rep["ok"], rep["errors"]


def test_plan_cannot_replace_a_live_agents_policy(tmp_path, monkeypatch):
    import llm_client
    monkeypatch.setattr(llm_client, "configured", lambda cfg: True)
    home_dir = tmp_path / "h"
    home_dir.mkdir()
    (home_dir / "policy.md").write_text("Only SPY calls.\n", encoding="utf-8")
    new, rep = setup_plan.apply({"policy": "Trade anything."}, dict(_LIVE_REAL),
                                True, str(home_dir))
    assert new is None and "replace the trading policy" in " ".join(rep["errors"])
    assert (home_dir / "policy.md").read_text(encoding="utf-8") == "Only SPY calls.\n"
    # the same text again changes nothing; a FIRST policy only ever skips more
    new, rep = setup_plan.apply({"policy": "Only SPY calls."}, dict(_LIVE_REAL),
                                True, str(home_dir))
    assert rep["ok"], rep["errors"]
    fresh = tmp_path / "fresh"
    new, rep = setup_plan.apply({"policy": "Only SPY calls."}, dict(_LIVE_REAL),
                                True, str(fresh))
    assert rep["ok"], rep["errors"]


def test_refusal_warns_what_dry_run_does_to_open_positions(tmp_path):
    new, rep = setup_plan.apply(
        {"sizing": {"mode": "contracts", "contracts_per_trade": 6}},
        dict(_LIVE_REAL), True, str(tmp_path))
    err = " ".join(rep["errors"])
    assert "`dry on`" in err and "`dry off`" in err
    assert "BEFORE they do" in err and "close every position" in err
    assert "exit signal" in err and "will NOT sell it later" in err


def test_dry_run_really_drops_a_real_position_on_an_exit_signal(home, no_policy):
    """What that warning says, checked against the agent itself.

    If this fails because dry-run now keeps selling real positions (audit
    finding W3, fixed on another branch), the warning is out of date: reword
    the refusal in setup_plan.apply and the `dry on` paragraph in BOOT.md to
    say what dry-run does then, and change this test and the one above."""
    ev = make_event()
    key = f"{ev['ticker']}|{ev['expiry']}|{ev['strike']}|{ev['type']}"
    state = {"positions": {key: {"option_id": "real", "qty": 2}}}
    broker = StubBroker()
    agent.handle_event(make_event(event="EXITED", event_id="x-2"),
                       {"dry_run": True}, state, broker, object(), lambda s: None)
    assert broker.calls == [] and key not in state["positions"]


def test_naming_the_default_broker_is_not_a_broker_switch(tmp_path):
    cfg = {"dry_run": False, "source": "agenthc", "contracts_per_trade": 1}
    plan = {"broker": {"type": "robinhood"},
            "notifications": {"ntfy_topic": "t"}}
    new, rep = setup_plan.apply(plan, dict(cfg), True, str(tmp_path))
    assert rep["ok"], rep["errors"]        # was: "would switch the broker account"
    assert new["broker"] == "robinhood" and rep["mode"] == "LIVE"


def test_same_alpaca_account_without_the_paper_key_is_not_a_switch(keys_ok,
                                                                   tmp_path):
    cfg = {k: v for k, v in _LIVE_ON_PAPER.items() if k != "alpaca_paper"}
    plan = {"broker": {"type": "alpaca", "alpaca_key_id": "PK-paper",
                       "alpaca_secret": "paper", "alpaca_paper": True}}
    new, rep = setup_plan.apply(plan, dict(cfg), True, str(tmp_path))
    assert rep["ok"], rep["errors"]
    plan["broker"]["alpaca_paper"] = False     # paper -> real money still is
    new, rep = setup_plan.apply(plan, dict(cfg), True, str(tmp_path))
    assert new is None and "broker account" in " ".join(rep["errors"])


# ── L6: SIGTERM (docker stop, systemctl stop, shutdown) is a deliberate stop ─

def test_sigterm_is_a_stop_request_not_a_kill(home, monkeypatch):
    import signal
    deadline = threading.Event()
    monkeypatch.setattr(keep_running, "_sigterm_deadline", deadline.set)
    assert not keep_running.stop_requested()
    assert keep_running.stop_on_sigterm() is True
    handler = signal.getsignal(signal.SIGTERM)
    assert callable(handler)
    handler(signal.SIGTERM, None)          # what `docker stop` delivers
    assert keep_running.stop_requested()   # the loop stops like `agent.py stop`
    assert not os.path.exists(keep_running._stop_path())   # this copy only
    assert deadline.wait(10)               # and a stuck loop gets a deadline


def test_sigterm_deadline_waits_for_an_order_in_flight(home):
    exits = []
    held, release = threading.Event(), threading.Event()

    def order():
        with platform_compat.ORDER_LOCK:
            held.set()
            release.wait(30)
    t = threading.Thread(target=order, daemon=True)
    t.start()
    assert held.wait(30)
    d = threading.Thread(target=keep_running._sigterm_deadline,
                         kwargs={"sleep": lambda s: None,
                                 "leave": exits.append}, daemon=True)
    d.start()
    time.sleep(0.5)
    assert exits == []                     # not in the middle of an order
    release.set()
    t.join(30)
    d.join(30)
    assert exits == [0]                    # then it ends — as a stop, code 0
    assert _lock_is_free()


def test_run_loop_exits_cleanly_after_sigterm(home, monkeypatch, capsys):
    """`agent.py run` itself: SIGTERM mid-loop -> it finishes the loop, says
    it stopped and returns (exit code 0), like `agent.py stop`."""
    import signal

    import brokers
    import webui
    polls = []

    class Broker:
        @staticmethod
        def client(cfg):
            return object()

        @staticmethod
        def execute(client, cfg, event, state):
            return False

    class Source:
        @staticmethod
        def poll(cfg, st, save_state):
            polls.append(1)
            # what the OS does on `docker stop`, minus the real signal
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
            return []

    def no_dashboard(*a, **k):
        raise OSError("no dashboard in tests")
    monkeypatch.setattr(keep_running, "_sigterm_deadline", lambda: None)
    monkeypatch.setattr(agent, "require_consent_or_exit", lambda: None)
    monkeypatch.setattr(brokers, "broker_ready", lambda c: True)
    monkeypatch.setitem(agent.BROKERS, "stub", Broker)
    monkeypatch.setitem(agent.SOURCES, "stub", Source)
    monkeypatch.setattr(webui, "start_dashboard", no_dashboard)
    monkeypatch.setattr(keep_running, "refresh_autostart", lambda: False)
    monkeypatch.setattr(agent, "_refresh_cash", lambda *a, **k: None)
    monkeypatch.setattr(agent, "_position_upkeep", lambda *a, **k: None)
    monkeypatch.setattr(agent, "_maybe_daily_digest", lambda *a, **k: None)
    monkeypatch.setattr(agent, "_market_open_now", lambda: True)
    monkeypatch.setattr(agent, "notify", lambda *a, **k: None)
    monkeypatch.setitem(webui.CONTROLS, "paused", False)
    monkeypatch.setitem(webui.CONTROLS, "stop", False)
    with open(agent.CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump({"source": "stub", "broker": "stub"}, f)
    with open(agent.STATE_PATH, "w", encoding="utf-8") as f:
        json.dump({"seen": [], "positions": {}}, f)
    agent.cmd_run()                        # returns: no SystemExit, no crash
    assert polls == [1]                    # finished that loop, began no other
    assert "Stopped" in capsys.readouterr().out


def test_docker_notes_say_how_to_stop_and_what_a_reboot_does():
    with open(os.path.join(REPO, "GETTING_STARTED.md"), encoding="utf-8") as f:
        text = " ".join(f.read().split())
    assert "docker exec agentic-trader python agent.py stop" in text
    assert "docker stop -t 150 agentic-trader" in text
    assert "After the server or Docker itself restarts, the agent is normally OFF" in text
    # the go-live checklist no longer offers bare `docker stop` as the way
    checklist = text.split("Checklist before going live")[1]
    assert "docker exec agentic-trader python agent.py stop" in checklist
    assert "systemctl disable --now agentic-trader" in checklist
