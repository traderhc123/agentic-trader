"""State file and trade log durability.

state.json holds what the agent must never forget (open positions, what it
bought, signals already handled, the paid day pass); trades.jsonl is what
the daily entry cap, the max daily loss and the loss-streak rule count
from. A power cut, a full disk or a locked file must not silently lose
either, and must never let a real-money limit be bypassed.
"""

import json
import os

import pytest

import agent
from conftest import StubBroker, make_event


@pytest.fixture(autouse=True)
def _clean_memory(monkeypatch):
    """Rows a failed log write left in memory must not leak between tests."""
    monkeypatch.setattr(agent, "_UNWRITTEN", [], raising=False)
    monkeypatch.setattr(agent, "_LOG_FAIL", {}, raising=False)
    monkeypatch.setattr(agent, "_REPLACE_WAIT_S", 0, raising=False)


def _read(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ── D1: an unreadable state.json is never silently replaced by an empty one ──

_GOOD = {"seen": ["old-1"], "positions": {"SPY|2030-01-18|700.0|C": {
    "option_id": "o1", "qty": 2}}, "l402": {"token": "L402 a:b", "expires_at": 1}}
_TORN = b"\x00" * 64  # what a power cut mid-write leaves behind


def _run_one_loop(home, monkeypatch, events=()):
    """One pass of `agent.py run` with a stub broker and source. Returns
    (calls, alerts): what the loop did, and what the owner was told."""
    import brokers
    import keep_running
    import webui
    calls, sent = [], []

    class Broker:
        @staticmethod
        def client(cfg):
            return object()

        @staticmethod
        def held_option_qty(client, cfg, ids):
            return {}

        @staticmethod
        def execute(client, cfg, event, state):
            calls.append("order")
            return False

    class Source:
        @staticmethod
        def poll(cfg, state, save_state):
            calls.append("poll")
            return list(events)

    def no_dashboard(*a, **k):
        raise OSError("no dashboard in tests")

    def stop(*a, **k):
        raise KeyboardInterrupt
    monkeypatch.setattr(agent, "require_consent_or_exit", lambda: None)
    # the loop records when it started watching: keep that out of other tests
    monkeypatch.setattr(agent, "_WATCH_SINCE", {})
    monkeypatch.setattr(brokers, "broker_ready", lambda c: True)
    monkeypatch.setitem(agent.BROKERS, "stub", Broker)
    monkeypatch.setitem(agent.SOURCES, "stub", Source)
    monkeypatch.setattr(webui, "start_dashboard", no_dashboard)
    monkeypatch.setattr(keep_running, "refresh_autostart", lambda: False)
    monkeypatch.setattr(agent, "_refresh_cash", lambda *a, **k: None)
    monkeypatch.setattr(agent, "_position_upkeep", lambda *a, **k: None)
    monkeypatch.setattr(agent, "exit_remainder_tick", lambda *a, **k: None)
    monkeypatch.setattr(agent, "expiry_guard_tick", lambda *a, **k: None)
    monkeypatch.setattr(agent, "expiry_wake_check", lambda *a, **k: None)
    monkeypatch.setattr(agent, "_maybe_daily_digest", lambda *a, **k: None)
    monkeypatch.setattr(agent, "notify", lambda cfg, msg, **k: sent.append(msg))
    monkeypatch.setitem(webui.CONTROLS, "paused", False)
    monkeypatch.setitem(webui.CONTROLS, "stop", False)
    monkeypatch.setattr(agent, "_sleep_unless_stopped", stop)
    (home / "config.json").write_text(
        json.dumps({"source": "stub", "broker": "stub"}), encoding="utf-8")
    agent.cmd_run()
    return calls, sent


def _damaged(home):
    return sorted(p for p in home.iterdir()
                  if p.name.startswith("state.json.damaged-"))


def test_unreadable_state_with_no_backup_pauses_and_keeps_the_file(home, monkeypatch):
    """REGRESSION: a state.json a power cut left unparseable was read as
    "no state", the agent traded on from an empty memory, and its next save
    made the loss permanent."""
    import webui
    (home / "state.json").write_bytes(_TORN)
    calls, sent = _run_one_loop(home, monkeypatch, [make_event()])
    # paused: the feed isn't fetched (so nothing is paid) and nothing ordered
    assert calls == []
    assert webui.CONTROLS["paused"] is True
    # the damaged file is still there, under a new name
    kept = _damaged(home)
    assert len(kept) == 1 and kept[0].read_bytes() == _TORN
    # the owner is told, in words that say what the agent will not do
    assert len(sent) >= 1
    alert = next(m for m in sent if "PAUSED" in m)
    assert "will NOT sell" in alert and "broker account" in alert
    assert kept[0].name in alert
    assert agent._durability_banner()[0]["level"] == "urgent"

    # a restart (reboot, crash, start-at-login) must not quietly go on from
    # the empty memory either: still paused, and no second alert that day
    calls, sent = _run_one_loop(home, monkeypatch, [make_event()])
    assert calls == [] and webui.CONTROLS["paused"] is True
    assert not [m for m in sent if "PAUSED" in m]
    assert len(_damaged(home)) == 1

    # only the owner's own "resume" ends it
    handled, reply = agent._apply_command("resume")
    assert handled and "empty memory" in reply
    assert webui.CONTROLS["paused"] is False
    assert agent._durability_banner() == []
    calls, _sent = _run_one_loop(home, monkeypatch)
    assert calls == ["poll"]


def test_unreadable_state_is_restored_from_the_backup(home, monkeypatch):
    import webui
    agent._save(agent.STATE_PATH, _GOOD, private=True, backup=True)
    (home / "state.json").write_bytes(_TORN)
    calls, sent = _run_one_loop(home, monkeypatch)
    # same moment's backup: nothing was lost, so the agent carries on
    assert calls == ["poll"] and webui.CONTROLS["paused"] is False
    state = _read(home / "state.json")
    assert state["positions"] == _GOOD["positions"]
    assert state["seen"] == ["old-1"] and state["l402"]["token"] == "L402 a:b"
    assert [p.read_bytes() for p in _damaged(home)] == [_TORN]
    told = [m for m in sent if "restored" in m]
    assert len(told) == 1 and "PAUSED" not in told[0]


def test_restoring_from_an_old_backup_pauses(home, monkeypatch):
    """A backup much older than the damaged file may miss trades: the agent
    loads it (better than nothing) but waits for the owner."""
    import webui
    agent._save(agent.STATE_PATH, _GOOD, private=True, backup=True)
    bak = agent.STATE_PATH + ".bak"
    old = os.path.getmtime(bak) - 3 * 3600
    os.utime(bak, (old, old))
    (home / "state.json").write_bytes(_TORN)
    calls, sent = _run_one_loop(home, monkeypatch, [make_event()])
    assert calls == [] and webui.CONTROLS["paused"] is True
    assert _read(home / "state.json")["positions"] == _GOOD["positions"]
    alert = next(m for m in sent if "PAUSED" in m)
    assert "older backup" in alert
    handled, reply = agent._apply_command("resume")
    assert handled and "backup" in reply


def test_missing_state_is_a_fresh_install_not_damage(home, monkeypatch):
    import webui
    calls, sent = _run_one_loop(home, monkeypatch)
    assert calls == ["poll"] and webui.CONTROLS["paused"] is False
    assert not _damaged(home) and agent._durability_banner() == []
    assert not [m for m in sent if "PAUSED" in m or "restored" in m]
    # the first save leaves a backup beside the state file
    assert _read(agent.STATE_PATH + ".bak") == _read(agent.STATE_PATH)


def test_load_state_tells_missing_from_unreadable(home):
    assert agent._load_state() == ({"seen": [], "positions": {}}, {"how": "fresh"})
    (home / "state.json").write_text("[1, 2]", encoding="utf-8")  # not a state
    state, info = agent._load_state()
    assert info["how"] == "lost" and state == {"seen": [], "positions": {}}
    # a readable file is used as is, whatever the backup says
    agent._save(agent.STATE_PATH, _GOOD, private=True, backup=True)
    agent._save(agent.STATE_PATH, {"seen": ["new"], "positions": {}}, private=True)
    assert agent._load_state() == ({"seen": ["new"], "positions": {}}, {"how": "ok"})
    # other callers of _load keep the old "default on any problem" contract
    (home / "config.json").write_bytes(_TORN)
    assert agent._load(agent.CONFIG_PATH, {"x": 1}) == {"x": 1}
    assert agent._load(os.path.join(str(home), "nope.json")) is None


def test_save_reaches_the_disk_before_it_replaces_the_file(home, monkeypatch):
    """Write temp -> flush + fsync -> swap in: a power cut leaves either the
    old file or the new one, never a half-written one."""
    order = []
    real_fsync, real_replace = os.fsync, os.replace

    def fsync(fd):
        order.append("fsync")
        return real_fsync(fd)

    def replace(src, dst):
        order.append("replace:" + os.path.basename(dst))
        return real_replace(src, dst)
    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(os, "replace", replace)
    agent._save(agent.STATE_PATH, _GOOD, private=True, backup=True)
    assert order.index("fsync") < order.index("replace:state.json")
    assert order.index("replace:state.json") < order.index("replace:state.json.bak")
    assert _read(agent.STATE_PATH) == _GOOD == _read(agent.STATE_PATH + ".bak")
    assert not os.path.exists(agent.STATE_PATH + ".tmp")


def test_a_failed_backup_write_does_not_fail_the_save(home, monkeypatch):
    real_replace = os.replace

    def replace(src, dst):
        if dst.endswith(".bak"):
            raise OSError("disk full")
        return real_replace(src, dst)
    monkeypatch.setattr(os, "replace", replace)
    agent._save(agent.STATE_PATH, _GOOD, private=True, backup=True)
    assert _read(agent.STATE_PATH) == _GOOD


# ── D2: Windows — the file is briefly open elsewhere ─────────────────────────

def test_save_retries_while_another_reader_has_the_file_open(home, monkeypatch):
    """REGRESSION (Windows): os.replace raises PermissionError while the
    dashboard thread (or antivirus) has state.json open; the save was lost."""
    real_replace, fails = os.replace, {"left": 3}

    def replace(src, dst):
        if fails["left"]:
            fails["left"] -= 1
            raise PermissionError(13, "The process cannot access the file "
                                      "because it is being used by another process")
        return real_replace(src, dst)
    monkeypatch.setattr(os, "replace", replace)
    agent._save(agent.STATE_PATH, _GOOD, private=True)
    assert _read(agent.STATE_PATH) == _GOOD and fails["left"] == 0


def test_save_still_fails_loudly_when_the_file_stays_locked(home, monkeypatch):
    def replace(src, dst):
        raise PermissionError(13, "locked")
    monkeypatch.setattr(os, "replace", replace)
    with pytest.raises(PermissionError):
        agent._save(agent.STATE_PATH, _GOOD, private=True)


def test_close_found_in_the_account_keeps_its_exit_row_when_the_save_fails(
        home, monkeypatch):
    """REGRESSION: the position was already dropped in memory, so when the
    state save failed the exit row (and its P&L) was never written."""
    monkeypatch.setattr(agent, "notify", lambda *a, **k: None)
    key = "SPY|2030-01-18|700.0|C"
    state = {"positions": {key: {"option_id": "o1", "qty": 1, "entry_mark": 2.0}}}

    def locked(_state):
        raise PermissionError(13, "locked")
    with pytest.raises(PermissionError):
        agent._close_outside({"broker": "alpaca"}, state, locked, None, key, 0,
                             1.5, "2030-01-10T15:00:00+00:00", ["f1"], "sold",
                             "holdings")
    assert key not in state["positions"]
    rows = [t for t in agent._recent_trades(5) if t.get("action") == "exit"]
    assert len(rows) == 1 and rows[0]["manual"] is True
    assert rows[0]["est_pnl_usd"] == pytest.approx(-50.0)


# ── D3: one bad line must not blank the recent-trades view ───────────────────

def _append_raw(text):
    with open(agent.TRADES_PATH, "a", encoding="utf-8") as f:
        f.write(text)


def test_recent_trades_skips_unreadable_lines(home, broker, no_policy):
    """REGRESSION: one unparseable line in the tail made _recent_trades
    return [] — which switched the loss-streak rule off for the day and
    blanked the dashboard's "why no trade" list."""
    agent._log_trade({"action": "exit", "contract": "x", "est_pnl_usd": -5.0})
    agent._log_trade({"action": "skip_budget", "book": "main", "contract": "y",
                      "reason": "1 contract ≈ $450 exceeds your $100 budget"})
    _append_raw('{"action": "exit", "est_pnl_u\n')   # torn by a crash
    _append_raw('"just a string"\n[1, 2]\n\n')       # valid JSON, not a row
    agent._log_trade({"action": "exit", "contract": "x", "est_pnl_usd": -3.0})

    assert [t["action"] for t in agent._recent_trades(50)] == [
        "exit", "skip_budget", "exit"]
    assert [p["kind"] for p in agent._problems_today()] == ["skip_budget"]
    out = agent.handle_event(make_event(), {"rule_loss_streak": 2},
                             {"positions": {}}, broker, object(), lambda s: None)
    assert out and "losing exit" in out
    assert broker.calls == []


def test_recent_trades_survives_a_bad_byte(home):
    agent._log_trade({"action": "entry", "contract": "x"})
    with open(agent.TRADES_PATH, "ab") as f:
        f.write(b'{"action": "skip_rule", "reason": "caf\xe9"}\n')
    agent._log_trade({"action": "exit", "contract": "x", "est_pnl_usd": 1.0})
    assert [t["action"] for t in agent._recent_trades(50)] == [
        "entry", "skip_rule", "exit"]
    assert agent._entries_today() == 1
    assert agent._est_pnl_today() == (1.0, 0)


# ── D4: real-money limits count real rows only ───────────────────────────────

class _QuotedBroker(StubBroker):
    def __init__(self, marks):
        super().__init__()
        self.marks = list(marks)

    def mark_price(self, client, cfg, event, side="buy"):
        return self.marks.pop(0) if self.marks else 0.0


def test_dry_run_rows_do_not_count_against_real_money_limits(home, no_policy):
    """REGRESSION: practice (dry-run) entries used up the real daily entry
    cap, and practice gains offset real losses against max_daily_loss_usd."""
    for i in range(3):
        agent._log_trade({"action": "entry", "event_id": f"d{i}", "dry": True})
    agent._log_trade({"action": "exit", "event_id": "dx", "dry": True,
                      "est_pnl_usd": 900.0})
    agent._log_trade({"action": "entry", "event_id": "r1", "dry": False})
    agent._log_trade({"action": "exit", "event_id": "rx", "dry": False,
                      "est_pnl_usd": -400.0})
    assert agent._entries_today() == 1
    assert agent._est_pnl_today() == (-400.0, 0)

    # live: 1 real entry so far, so a cap of 2 still has room …
    b = StubBroker()
    out = agent.handle_event(make_event(event_id="n1"),
                             {"dry_run": False, "max_entries_per_day": 2},
                             {"positions": {}}, b, object(), lambda s: None)
    assert out == "ENTRY: SPY 2026-07-10 $752.0 C" and len(b.calls) == 1
    # … and the real $400 loss trips a $300 limit despite the paper gain
    b = StubBroker()
    out = agent.handle_event(make_event(event_id="n2", strike=760.0),
                             {"dry_run": False, "max_entries_per_day": 50,
                              "max_daily_loss_usd": 300},
                             {"positions": {}}, b, object(), lambda s: None)
    assert out and "max daily loss" in out and b.calls == []


def test_dry_run_mode_applies_the_limits_to_its_own_practice_rows(home, no_policy):
    """In dry-run the caps are a rehearsal: counted from the practice rows,
    so the owner sees what the same day would do with real money."""
    for i in range(2):
        agent._log_trade({"action": "entry", "event_id": f"r{i}", "dry": False})
    assert agent._entries_today(dry=True) == 0
    cfg = {"dry_run": True, "max_entries_per_day": 1}
    state = {"positions": {}}
    b = StubBroker()
    out = agent.handle_event(make_event(event_id="p1"), cfg, state, b, object(),
                             lambda s: None)
    assert out.startswith("[DRY-RUN] ENTRY")
    out = agent.handle_event(make_event(event_id="p2", strike=760.0), cfg, state,
                             b, object(), lambda s: None)
    assert "daily entry cap" in out
    assert agent._entries_today(dry=True) == 1 and agent._entries_today() == 2
    assert b.calls == []  # dry-run never reaches the broker


def test_a_practice_win_does_not_reset_a_real_losing_streak(home, broker, no_policy):
    agent._log_trade({"action": "exit", "dry": False, "est_pnl_usd": -5.0})
    agent._log_trade({"action": "exit", "dry": False, "est_pnl_usd": -3.0})
    agent._log_trade({"action": "exit", "dry": True, "est_pnl_usd": 50.0})
    out = agent.handle_event(make_event(), {"dry_run": False, "rule_loss_streak": 2},
                             {"positions": {}}, broker, object(), lambda s: None)
    assert out and "losing exit" in out and broker.calls == []


# ── D5: a trade log that can't be written must not hide today's trades ───────

def test_unwritable_trade_log_cannot_bypass_the_daily_cap(home, monkeypatch, no_policy):
    """REGRESSION: _log_trade swallowed write errors, so with the log
    unwritable every entry looked like the day's first."""
    sent = []
    monkeypatch.setattr(agent, "notify", lambda cfg, msg, **k: sent.append(msg))
    (home / "trades.jsonl").mkdir()  # appending to a folder fails on every OS
    cfg = {"dry_run": False, "max_entries_per_day": 1}
    state = {"positions": {}}
    b = _QuotedBroker([2.00, 1.00])
    out = agent.handle_event(make_event(event_id="e1"), cfg, state, b, object(),
                             lambda s: None)
    assert out.startswith("ENTRY")
    out = agent.handle_event(make_event(event_id="e2", strike=760.0), cfg, state,
                             b, object(), lambda s: None)
    assert out and "daily entry cap" in out
    assert len(b.calls) == 1
    # exits still go through, and their P&L still counts toward the loss limit
    out = agent.handle_event(make_event(event="EXITED", event_id="x1"), cfg, state,
                             b, object(), lambda s: None)
    assert out.startswith("EXIT")
    assert agent._entries_today() == 1
    assert agent._est_pnl_today() == (-100.0, 0)
    assert [t["action"] for t in agent._recent_trades(10)] == [
        "entry", "skip_daily_cap", "exit"]
    # the owner is told once, not once per row
    told = [m for m in sent if "trade log" in m]
    assert len(told) == 1 and "limits" in told[0]
    assert any("trade log" in w["text"] for w in agent._durability_banner())

    # once the log can be written again, the rows held in memory are written
    # first, in order, and nothing is counted twice
    (home / "trades.jsonl").rmdir()
    agent._log_trade({"action": "skip_rule", "contract": "z"})
    with open(agent.TRADES_PATH, encoding="utf-8") as f:
        written = [json.loads(x)["action"] for x in f]
    assert written == ["entry", "skip_daily_cap", "exit", "skip_rule"]
    assert agent._UNWRITTEN == []
    assert agent._entries_today() == 1
    assert agent._est_pnl_today() == (-100.0, 0)
    assert agent._durability_banner() == []


def test_log_row_after_a_torn_line_is_not_swallowed(home):
    """A crash or full disk can leave the last line without its newline:
    the next row must start on its own line."""
    _append_raw('{"action": "entry", "ts": "2020-01-01T00:0')
    agent._log_trade({"action": "entry", "contract": "x"})
    assert agent._entries_today() == 1
    assert [t["action"] for t in agent._recent_trades(5)] == ["entry"]


def test_damaged_state_that_cannot_be_set_aside_is_never_overwritten(home, monkeypatch):
    """If the damaged file can be neither moved nor copied, an empty state
    must not be written over the only copy: the agent does not start."""
    (home / "state.json").write_bytes(_TORN)
    monkeypatch.setattr(agent, "_set_aside", lambda path: None)
    alerts = []
    for _ in range(2):  # a supervisor restarts it: one alert, not one per try
        try:
            _run_one_loop(home, monkeypatch, [make_event()])
        except SystemExit as exc:
            assert exc.code == 1
        else:
            pytest.fail("the agent started")
        finally:
            alerts.append(agent._state_hold().get("notified"))
    assert (home / "state.json").read_bytes() == _TORN
    assert alerts[0] and alerts[0] == alerts[1]


def test_hold_text_warns_when_the_source_does_not_date_its_signals():
    lost = {"why": "lost", "kept": "state.json.damaged-x"}
    feed = agent._state_hold_text(lost, {"source": "agenthc"})
    own = agent._state_hold_text(lost, {"source": "manual"})
    assert "skips signals more than 5 minutes old" in feed
    assert "may pay for today's signals again" in feed
    assert "act on every signal still in it" in own and "may pay" not in own
    for text in (feed, own):
        assert "places no orders" in text and "state.json.damaged-x" in text


def test_half_written_log_rows_are_not_counted_twice(home, monkeypatch):
    """A write that fails after some bytes reached the file (disk full) is
    rolled back, so writing the same rows again later can't double an entry
    or an exit's P&L."""
    monkeypatch.setattr(agent, "notify", lambda *a, **k: None)
    agent._log_trade({"action": "entry", "contract": "a", "dry": False})
    real_open = open

    def flaky_open(path, mode="r", *a, **k):
        f = real_open(path, mode, *a, **k)
        if "a" in mode and str(path) == agent.TRADES_PATH:
            real_close = f.close

            def close():
                real_close()  # the row is in the file …
                raise OSError(28, "No space left on device")  # … then this
            f.close = close
        return f
    monkeypatch.setattr("builtins.open", flaky_open)
    agent._log_trade({"action": "exit", "contract": "a", "dry": False,
                      "est_pnl_usd": 40.0})
    monkeypatch.setattr("builtins.open", real_open)
    with open(agent.TRADES_PATH, encoding="utf-8") as f:
        assert len(f.readlines()) == 1  # rolled back; the row waits in memory
    assert agent._est_pnl_today() == (40.0, 0)
    agent._log_trade({"action": "skip_rule", "contract": "b"})
    with open(agent.TRADES_PATH, encoding="utf-8") as f:
        assert [json.loads(x)["action"] for x in f] == ["entry", "exit", "skip_rule"]
    assert agent._est_pnl_today() == (40.0, 0) and agent._entries_today() == 1
