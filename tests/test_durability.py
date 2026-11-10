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
    monkeypatch.setattr(agent, "_BAK_FAIL", {}, raising=False)
    monkeypatch.setattr(agent, "_REPLACE_WAIT_S", 0, raising=False)


def _read(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ── D1: an unreadable state.json is never silently replaced by an empty one ──

_GOOD = {"seen": ["old-1"], "positions": {"SPY|2030-01-18|700.0|C": {
    "option_id": "o1", "qty": 2}}, "l402": {"token": "L402 a:b", "expires_at": 1}}
_TORN = b"\x00" * 64  # what a power cut mid-write leaves behind


def _run_one_loop(home, monkeypatch, events=(), sent=None):
    """One pass of `agent.py run` with a stub broker and source. Returns
    (calls, alerts): what the loop did, and what the owner was told (also
    collected in `sent`, for a run that ends before it returns)."""
    import brokers
    import keep_running
    import webui
    calls, sent = [], ([] if sent is None else sent)

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
    (home / "state.json").write_bytes(_TORN)
    calls, sent = _run_one_loop(home, monkeypatch, [make_event()])
    # paused: the feed isn't fetched (so nothing is paid) and nothing ordered
    assert calls == []
    assert agent._state_held() is True
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
    assert calls == [] and agent._state_held() is True
    assert not [m for m in sent if "PAUSED" in m]
    assert len(_damaged(home)) == 1

    # only the owner's own "resume" ends it
    handled, reply = agent._apply_command("resume")
    assert handled and "empty memory" in reply
    assert agent._state_held() is False
    assert agent._durability_banner() == []
    calls, _sent = _run_one_loop(home, monkeypatch)
    assert calls == ["poll"]


def test_unreadable_state_is_restored_from_the_backup(home, monkeypatch):
    agent._save(agent.STATE_PATH, _GOOD, private=True, backup=True)
    _tear(home / "state.json")
    calls, sent = _run_one_loop(home, monkeypatch)
    # the backup written right after it: same save, so the agent carries on
    assert calls == ["poll"] and agent._state_held() is False
    state = _read(home / "state.json")
    assert state["positions"] == _GOOD["positions"]
    assert state["seen"] == ["old-1"] and state["l402"]["token"] == "L402 a:b"
    assert [p.read_bytes() for p in _damaged(home)] == [_TORN]
    told = [m for m in sent if "restored" in m]
    assert len(told) == 1 and "PAUSED" not in told[0]


def test_restoring_from_an_old_backup_pauses(home, monkeypatch):
    """A backup much older than the damaged file may miss trades: the agent
    loads it (better than nothing) but waits for the owner."""
    agent._save(agent.STATE_PATH, _GOOD, private=True, backup=True)
    bak = agent.STATE_PATH + ".bak"
    old = os.path.getmtime(bak) - 3 * 3600
    os.utime(bak, (old, old))
    (home / "state.json").write_bytes(_TORN)
    calls, sent = _run_one_loop(home, monkeypatch, [make_event()])
    assert calls == [] and agent._state_held() is True
    assert _read(home / "state.json")["positions"] == _GOOD["positions"]
    alert = next(m for m in sent if "PAUSED" in m)
    assert "older backup" in alert
    handled, reply = agent._apply_command("resume")
    assert handled and "backup" in reply


def test_missing_state_is_a_fresh_install_not_damage(home, monkeypatch):
    calls, sent = _run_one_loop(home, monkeypatch)
    assert calls == ["poll"] and agent._state_held() is False
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
    monkeypatch.setattr(agent, "_IS_MAC", False, raising=False)  # macOS: below
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
    real_set_aside = agent._set_aside
    monkeypatch.setattr(agent, "_set_aside", lambda path: None)
    sent = []
    for _ in range(2):  # a supervisor restarts it: one alert, not one per try
        _not_started(home, monkeypatch, sent)
    assert (home / "state.json").read_bytes() == _TORN
    assert len(sent) == 1 and "did NOT start" in sent[0]
    assert "could not be moved aside" in sent[0] and "PAUSED" not in sent[0]
    # no "memory lost" record yet: nothing has been given up
    assert agent._state_hold() is None
    # once the file can be moved, the usual damaged-file start follows, and
    # its own alert is not swallowed by the one already sent today
    monkeypatch.setattr(agent, "_set_aside", real_set_aside)
    calls, sent = _run_one_loop(home, monkeypatch, [make_event()])
    assert calls == [] and agent._state_held() is True
    assert len([m for m in sent if "PAUSED" in m]) == 1
    assert [p.read_bytes() for p in _damaged(home)] == [_TORN]


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


# ── review fixes ─────────────────────────────────────────────────────────────

def _tear(path):
    """Damage a file the way a power cut or a bad disk does: its bytes are
    gone, its date is still the one of its last save."""
    st = os.stat(path)
    path.write_bytes(_TORN)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))


def _lock(monkeypatch, *names):
    """These files can be neither opened nor renamed: what a Windows lock
    (antivirus, a backup tool) or a file owned by another user looks like."""
    real_open, real_replace = open, os.replace

    def locked_open(path, *a, **k):
        if os.path.basename(str(path)) in names:
            raise PermissionError(13, "The process cannot access the file "
                                      "because it is being used by another process")
        return real_open(path, *a, **k)

    def locked_replace(src, dst):
        if os.path.basename(str(src)) in names:
            raise PermissionError(13, "locked")
        return real_replace(src, dst)
    monkeypatch.setattr("builtins.open", locked_open)
    monkeypatch.setattr(os, "replace", locked_replace)
    return lambda: (monkeypatch.setattr("builtins.open", real_open),
                    monkeypatch.setattr(os, "replace", real_replace))


def _not_started(home, monkeypatch, sent):
    try:
        _run_one_loop(home, monkeypatch, [make_event()], sent=sent)
    except SystemExit as exc:
        assert exc.code == 1
    else:
        pytest.fail("the agent started")


def test_a_state_file_that_cannot_be_opened_is_not_called_lost(home, monkeypatch):
    """REVIEW: a good state.json that was only locked for a moment (and had
    no backup yet) left a "memory lost" hold behind: the next start loaded
    the file normally, yet started paused and told the owner the agent no
    longer knew its positions."""
    agent._save(agent.STATE_PATH, _GOOD, private=True)  # no backup yet
    before = (home / "state.json").read_bytes()
    unlock = _lock(monkeypatch, "state.json")
    sent = []
    for _ in range(2):  # a supervisor restarts it: one alert, not one per try
        _not_started(home, monkeypatch, sent)
    unlock()
    # nothing was changed, nothing is claimed lost, the owner is told why
    assert (home / "state.json").read_bytes() == before
    assert not _damaged(home) and agent._state_hold() is None
    assert len(sent) == 1 and "did NOT start" in sent[0]
    assert "could not open" in sent[0] and "Nothing was changed" in sent[0]
    for wrong in ("PAUSED", "empty memory", "is damaged", "no backup"):
        assert wrong not in sent[0]
    assert agent._durability_banner() == []

    # the lock is gone: the agent starts normally, with its position
    sent = []
    calls, _ = _run_one_loop(home, monkeypatch, sent=sent)
    assert calls == ["poll"] and agent._state_held() is False
    assert not [m for m in sent if "PAUSED" in m or "did NOT start" in m]
    assert _read(home / "state.json")["positions"] == _GOOD["positions"]
    # no hold to end: the plain reply to the owner's own Resume
    assert agent._apply_command("resume") == (
        True, "Resumed — the agent takes new entries again.")


def test_a_locked_state_file_is_never_replaced_by_its_backup(home, monkeypatch):
    """What a file holds is unknown while it can't be opened: it may be newer
    than the backup, so the backup must not be written over it."""
    agent._save(agent.STATE_PATH, _GOOD, private=True, backup=True)
    newer = dict(_GOOD, seen=["old-1", "new-2"])
    agent._save(agent.STATE_PATH, newer, private=True)
    unlock = _lock(monkeypatch, "state.json")
    assert agent._load_state()[1]["how"] == "unopened"
    _not_started(home, monkeypatch, [])
    unlock()
    assert _read(home / "state.json") == newer and not _damaged(home)


def test_a_locked_backup_is_not_given_up_for_lost(home, monkeypatch):
    agent._save(agent.STATE_PATH, _GOOD, private=True, backup=True)
    _tear(home / "state.json")
    unlock = _lock(monkeypatch, "state.json.bak")
    sent = []
    _not_started(home, monkeypatch, sent)
    unlock()
    assert (home / "state.json").read_bytes() == _TORN
    assert agent._state_hold() is None and not _damaged(home)
    assert len(sent) == 1 and "state.json.bak" in sent[0]
    # readable again: restored from it, nothing lost
    calls, _ = _run_one_loop(home, monkeypatch)
    assert calls == ["poll"]
    assert _read(home / "state.json")["positions"] == _GOOD["positions"]


def test_a_backup_one_save_behind_is_not_the_same_save(home, monkeypatch):
    """REVIEW: saves come milliseconds apart, so "the backup is at most 5 s
    older" passed off a backup that missed the last save as the same save:
    the agent carried on, unpaused, without the position that save added."""
    told = []
    monkeypatch.setattr(agent, "notify", lambda cfg, msg, **k: told.append(msg))
    agent._save(agent.STATE_PATH, _GOOD, private=True, backup=True)
    real_replace = os.replace

    def replace(src, dst):
        if str(dst).endswith(".bak"):
            raise OSError(28, "No space left on device")
        return real_replace(src, dst)
    monkeypatch.setattr(os, "replace", replace)
    newer = json.loads(json.dumps(_GOOD))
    newer["positions"]["QQQ|2030-01-18|500.0|C"] = {"option_id": "o2", "qty": 1}
    agent._save(agent.STATE_PATH, newer, private=True, backup=True)
    agent._save(agent.STATE_PATH, newer, private=True, backup=True)
    monkeypatch.setattr(os, "replace", real_replace)
    # the owner is told the backup is falling behind: once, not per save
    assert len(told) == 1 and "state.json.bak" in told[0]
    assert "keeps running" in told[0]
    assert [b["level"] for b in agent._durability_banner()] == ["warn"]

    _tear(home / "state.json")
    assert agent._load_state()[1]["how"] == "restored_old"
    calls, sent = _run_one_loop(home, monkeypatch, [make_event()])
    assert calls == [] and agent._state_held() is True
    alert = next(m for m in sent if "PAUSED" in m)
    assert "older backup" in alert and "will NOT sell" in alert
    # the startup save refreshed the backup: the warning is gone
    assert [b["level"] for b in agent._durability_banner()] == ["urgent"]


def test_restored_alert_does_not_claim_the_backup_is_identical(home, monkeypatch):
    agent._save(agent.STATE_PATH, _GOOD, private=True, backup=True)
    _tear(home / "state.json")
    _calls, sent = _run_one_loop(home, monkeypatch)
    told = next(m for m in sent if "restored" in m)
    assert "same contents" not in told
    assert "cannot check" in told and "broker account" in told
    assert "will not sell" in told


def test_a_backup_written_before_the_file_is_an_older_backup(home):
    agent._save(agent.STATE_PATH, _GOOD, private=True, backup=True)
    bak = agent.STATE_PATH + ".bak"
    st = os.stat(bak)
    os.utime(bak, ns=(st.st_atime_ns, st.st_mtime_ns - 2_000_000_000))  # 2 s
    _tear(home / "state.json")
    assert agent._load_state()[1]["how"] == "restored_old"


def test_a_save_the_disk_refused_keeps_the_old_file_and_its_backup(home, monkeypatch):
    """REVIEW: every fsync error was ignored, so when the system said it
    could not put the bytes on the disk (failing disk, quota) the save still
    replaced state.json AND its only backup with them."""
    import errno
    agent._save(agent.STATE_PATH, _GOOD, private=True, backup=True)

    def fsync(fd):
        raise OSError(errno.EIO, "Input/output error")
    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(agent, "_IS_MAC", False, raising=False)
    with pytest.raises(OSError):
        agent._save(agent.STATE_PATH, {"seen": [], "positions": {}},
                    private=True, backup=True)
    assert _read(agent.STATE_PATH) == _GOOD == _read(agent.STATE_PATH + ".bak")
    assert not os.path.exists(agent.STATE_PATH + ".tmp")
    assert not os.path.exists(agent.STATE_PATH + ".bak.tmp")


def test_a_filesystem_without_fsync_can_still_save(home, monkeypatch):
    import errno

    def fsync(fd):
        raise OSError(errno.EINVAL, "Invalid argument")
    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(agent, "_IS_MAC", False, raising=False)
    agent._save(agent.STATE_PATH, _GOOD, private=True, backup=True)
    assert _read(agent.STATE_PATH) == _GOOD == _read(agent.STATE_PATH + ".bak")


def test_macos_flushes_the_drive_cache(tmp_path, monkeypatch):
    """On macOS fsync alone leaves the bytes in the drive's own cache."""
    import errno
    import sys
    import types
    done = []
    fake = types.ModuleType("fcntl")
    fake.F_FULLFSYNC = 51
    fake.fcntl = lambda fd, op: done.append(("full", op)) or 0
    monkeypatch.setitem(sys.modules, "fcntl", fake)
    monkeypatch.setattr(agent, "_IS_MAC", True, raising=False)
    monkeypatch.setattr(os, "fsync", lambda fd: done.append(("fsync", fd)))
    with open(tmp_path / "f", "w", encoding="utf-8") as f:
        agent._fsync(f.fileno())
        assert done == [("full", 51)]

        def unsupported(fd, op):  # a network share: fall back to fsync
            raise OSError(errno.ENOTTY, "Inappropriate ioctl for device")
        fake.fcntl = unsupported
        del done[:]
        agent._fsync(f.fileno())
        assert done == [("fsync", f.fileno())]

        def failing(fd, op):  # a real write error is never papered over
            raise OSError(errno.EIO, "Input/output error")
        fake.fcntl = failing
        del done[:]
        with pytest.raises(OSError):
            agent._fsync(f.fileno())
        assert done == []


def test_a_failed_move_file_import_does_not_lift_the_pause(home, monkeypatch):
    """REVIEW: a failed import from the dashboard set paused = False whatever
    it was before, lifting the damaged-memory pause (or the owner's own)
    without anyone pressing Resume."""
    import base64
    import time
    import transfer
    import webui
    made = {}

    class NoServer:
        def __init__(self, addr, handler):
            made["routes"] = handler.routes_post

        def serve_forever(self):
            pass
    monkeypatch.setattr(webui, "_AppServer", NoServer)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    monkeypatch.setattr(transfer, "decrypt", lambda blob, pw: b"")
    monkeypatch.setattr(transfer, "existing_setup", lambda: False)

    def broken(blob, pw, replace=False):
        raise transfer.TransferError("the move file is damaged")
    monkeypatch.setattr(transfer, "import_bundle", broken)
    webui.start_app(get_status=lambda: {}, get_trades=lambda *a: [],
                    apply_command=lambda t: (False, ""), cfg_getter=lambda: {})
    route = made["routes"]["/api/move/import"]
    data = {"data": base64.b64encode(b"x").decode("ascii"), "passphrase": "pw"}
    (home / "config.json").write_text(json.dumps({"paused": True}),
                                      encoding="utf-8")
    for was in (True, False):
        # the damaged-memory hold is kept in memory (agent._STATE_HELD), the
        # owner's own Pause in config.json: the import touches neither
        monkeypatch.setitem(agent._STATE_HELD, "on", was)
        reply = route(None, dict(data))
        assert reply["ok"] is False and "damaged" in reply["error"]
        assert agent._state_held() is was
        assert _read(home / "config.json") == {"paused": True}
        # and its own hold (no polls, no orders while the files are replaced)
        # is released: nothing changed, the agent carries on as before
        assert webui.orders_held() is False


def test_both_damaged_files_are_named_in_the_alert(home, monkeypatch):
    (home / "state.json").write_bytes(_TORN)
    (home / "state.json.bak").write_bytes(_TORN)
    _calls, sent = _run_one_loop(home, monkeypatch)
    alert = next(m for m in sent if "PAUSED" in m)
    names = [p.name for p in home.iterdir() if ".damaged-" in p.name]
    assert len(names) == 2
    for name in names:
        assert name in alert


def test_old_backup_text_warns_the_feed_may_be_paid_for_again():
    hold = {"why": "old_backup", "backup_at": "2026-10-30T13:00:00+00:00"}
    feed = agent._state_hold_text(hold, {"source": "agenthc"})
    own = agent._state_hold_text(hold, {"source": "manual"})
    assert "may pay for today's signals again" in feed
    assert "may pay" not in own and "act again on any signal" in own


def test_a_stale_backup_is_dated_before_the_file_it_no_longer_matches(home, monkeypatch):
    """Where file dates are coarse (2 s on some disks) a backup one save
    behind can carry the same date as the newer file: a failed backup
    refresh dates it back, so it can't pass for the same save."""
    monkeypatch.setattr(agent, "notify", lambda *a, **k: None)
    agent._save(agent.STATE_PATH, _GOOD, private=True, backup=True)
    bak = agent.STATE_PATH + ".bak"
    later = os.stat(bak).st_mtime_ns + 2_000_000_000
    os.utime(bak, ns=(later, later))
    real_replace = os.replace

    def replace(src, dst):
        if str(dst).endswith(".bak"):
            raise OSError(28, "No space left on device")
        return real_replace(src, dst)
    monkeypatch.setattr(os, "replace", replace)
    agent._save(agent.STATE_PATH, dict(_GOOD, seen=["old-1", "new-2"]),
                private=True, backup=True)
    monkeypatch.setattr(os, "replace", real_replace)
    assert _read(bak) == _GOOD  # still the older save, and still there
    _tear(home / "state.json")
    state, info = agent._load_state()
    assert info["how"] == "restored_old" and state == _GOOD


def test_a_disk_read_error_is_damage_and_the_backup_is_used(home, monkeypatch):
    """A file the disk can no longer read (bad sector, failing card) is
    damaged, not merely busy: the backup takes over."""
    import errno
    agent._save(agent.STATE_PATH, _GOOD, private=True, backup=True)
    real_open = open

    def bad_sector(path, *a, **k):
        if os.path.basename(str(path)) == "state.json":
            raise OSError(errno.EIO, "Input/output error")
        return real_open(path, *a, **k)
    monkeypatch.setattr("builtins.open", bad_sector)
    state, info = agent._load_state()
    assert info["how"] == "restored" and state == _GOOD
