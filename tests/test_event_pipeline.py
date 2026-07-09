"""The run path: validate -> staleness -> caps -> policy -> execute."""

import json
from datetime import datetime, timedelta, timezone

import pytest

import agent
from conftest import StubBroker, make_event


# ── _valid_event ─────────────────────────────────────────────────────────────

def test_valid_event_accepts_good():
    assert agent._valid_event(make_event())
    assert agent._valid_event(make_event(event="EXITED", type="P", strike=0.5))


@pytest.mark.parametrize("over", [
    {"event": "BUY NOW"},                 # imperative, not a journal fact
    {"ticker": "spy"},                    # lowercase
    {"ticker": "SPY; DROP TABLE"},        # junk
    {"ticker": ""},
    {"expiry": "07/10/2026"},             # wrong date format
    {"expiry": "2026-7-1"},
    {"strike": 0},
    {"strike": -5},
    {"strike": 100_000},
    {"strike": "not-a-number"},
    {"type": "CALL"},                     # must be C/P
    {"type": ""},
    {"event_id": ""},
    {"event_id": None},
])
def test_valid_event_rejects(over):
    assert not agent._valid_event(make_event(**over))


def test_valid_event_accepts_numeric_string_strike():
    # float("752") passes validation; sources are responsible for coercion
    # (see sources/__init__.py contract) so the order path gets a real float.
    assert agent._valid_event(make_event(strike="752"))


# ── _stale ───────────────────────────────────────────────────────────────────

def _ts(seconds_ago):
    return (datetime.now(timezone.utc)
            - timedelta(seconds=seconds_ago)).isoformat(timespec="seconds")


def test_stale_blocks_old_entered():
    ev = make_event(occurred_at=_ts(3600))
    assert agent._stale(ev, {})


def test_stale_allows_fresh_entered():
    assert not agent._stale(make_event(occurred_at=_ts(10)), {})


def test_stale_never_blocks_exits():
    ev = make_event(event="EXITED", occurred_at=_ts(86400))
    assert not agent._stale(ev, {})


def test_stale_treats_missing_timestamp_as_fresh():
    assert not agent._stale(make_event(), {})


def test_stale_treats_bad_timestamp_as_fresh():
    assert not agent._stale(make_event(occurred_at="yesterday-ish"), {})


def test_stale_honors_configured_age():
    ev = make_event(occurred_at=_ts(120))
    assert agent._stale(ev, {"max_event_age_s": 60})
    assert not agent._stale(ev, {"max_event_age_s": 300})


# ── daily entry cap ──────────────────────────────────────────────────────────

def _log_n_entries(n):
    for i in range(n):
        agent._log_trade({"action": "entry", "event_id": f"e{i}",
                          "contract": "SPY ..."})


def test_entries_today_counts_only_todays_entries(home):
    _log_n_entries(3)
    agent._log_trade({"action": "skip_stale", "event_id": "s1"})
    # a stale line from another day
    with open(agent.TRADES_PATH, "a") as f:
        f.write(json.dumps({"action": "entry", "ts": "2020-01-01T00:00:00"}) + "\n")
    assert agent._entries_today() == 3


def test_daily_cap_blocks_new_entries(home, broker, no_policy):
    cfg = {"max_entries_per_day": 2, "dry_run": False}
    state = {"positions": {}}
    _log_n_entries(2)
    out = agent.handle_event(make_event(), cfg, state, broker, object(),
                             lambda s: None)
    assert "daily entry cap" in out
    assert broker.calls == []


def test_daily_cap_not_bypassed_by_log_flooding(home, broker, no_policy):
    """REGRESSION: a hostile feed spamming skippable events must not push
    today's real entries out of the cap counter's view."""
    cfg = {"max_entries_per_day": 2, "dry_run": False}
    state = {"positions": {}}
    _log_n_entries(2)
    for i in range(300):  # 300 junk log lines AFTER the real entries
        agent._log_trade({"action": "skip_stale", "event_id": f"junk{i}"})
    assert agent._entries_today() == 2
    out = agent.handle_event(make_event(), cfg, state, broker, object(),
                             lambda s: None)
    assert "daily entry cap" in out
    assert broker.calls == []


def test_open_position_cap_blocks_new_entries(home, broker, no_policy):
    """The daily cap resets every day — the concurrent cap must not."""
    cfg = {"max_open_positions": 2, "dry_run": False}
    state = {"positions": {"A|2026-07-10|1.0|C": {"qty": 1},
                           "B|2026-07-10|1.0|C": {"qty": 1}}}
    out = agent.handle_event(make_event(), cfg, state, broker, object(),
                             lambda s: None)
    assert "max open positions" in out
    assert broker.calls == []
    assert agent._recent_trades(5)[-1]["action"] == "skip_position_cap"


def test_open_position_cap_never_blocks_exits(home, broker, no_policy):
    cfg = {"max_open_positions": 1, "dry_run": False}
    state = {"positions": {"SPY|2026-07-10|752.0|C": {"option_id": "x",
                                                      "qty": 1}}}
    out = agent.handle_event(make_event(event="EXITED"), cfg, state, broker,
                             object(), lambda s: None)
    assert out == "EXIT: SPY 2026-07-10 $752.0 C"
    assert state["positions"] == {}


def test_open_position_cap_default_allows_normal_use(home, broker, no_policy):
    cfg = {"dry_run": True}
    state = {"positions": {f"T{i}|2026-07-10|1.0|C": {"qty": 1, "dry": True}
                           for i in range(9)}}
    out = agent.handle_event(make_event(), cfg, state, None, None,
                             lambda s: None)
    assert out.startswith("[DRY-RUN] ENTRY")  # 9 open < default 10


def test_exits_bypass_cap_and_policy(home, broker, monkeypatch):
    import llm_policy

    def boom(cfg, ev, log):
        raise AssertionError("policy brain must not run for EXITED")

    monkeypatch.setattr(llm_policy, "evaluate", boom)
    cfg = {"max_entries_per_day": 0, "dry_run": False}
    state = {"positions": {"SPY|2026-07-10|752.0|C": {"option_id": "x", "qty": 1}}}
    out = agent.handle_event(make_event(event="EXITED"), cfg, state, broker,
                             object(), lambda s: None)
    assert out == "EXIT: SPY 2026-07-10 $752.0 C"
    assert len(broker.calls) == 1


# ── invalid / stale / veto short-circuits ────────────────────────────────────

def test_invalid_event_never_reaches_broker(home, broker, no_policy):
    out = agent.handle_event(make_event(ticker="bad ticker"), {},
                             {"positions": {}}, broker, object(), lambda s: None)
    assert out is None
    assert broker.calls == []


def test_stale_event_skipped_and_logged(home, broker, no_policy):
    ev = make_event(occurred_at=_ts(3600))
    out = agent.handle_event(ev, {}, {"positions": {}}, broker, object(),
                             lambda s: None)
    assert "stale" in out
    assert broker.calls == []
    assert agent._recent_trades(5)[-1]["action"] == "skip_stale"


def test_policy_veto_blocks_and_logs(home, broker, monkeypatch):
    import llm_policy
    monkeypatch.setattr(llm_policy, "evaluate",
                        lambda cfg, ev, log: {"act": False, "reason": "skip puts"})
    out = agent.handle_event(make_event(type="P"), {}, {"positions": {}},
                             broker, object(), lambda s: None)
    assert out.startswith("POLICY VETO")
    assert broker.calls == []
    assert agent._recent_trades(5)[-1]["action"] == "policy_veto"


# ── dry-run bookkeeping ──────────────────────────────────────────────────────

def test_dry_run_tracks_positions_without_broker(home, broker, no_policy):
    cfg = {"dry_run": True, "contracts_per_trade": 2}
    state = {"positions": {}}
    out = agent.handle_event(make_event(), cfg, state, None, None, lambda s: None)
    assert out == "[DRY-RUN] ENTRY: SPY 2026-07-10 $752.0 C"
    assert state["positions"]["SPY|2026-07-10|752.0|C"]["dry"] is True
    assert broker.calls == []

    out = agent.handle_event(make_event(event="EXITED", event_id="test-2"),
                             cfg, state, None, None, lambda s: None)
    assert out == "[DRY-RUN] EXIT: SPY 2026-07-10 $752.0 C"
    assert state["positions"] == {}


def test_dry_position_never_closed_by_real_order(home, broker, no_policy):
    """Going live with a dry position open must not sell it for real."""
    cfg = {"dry_run": False}  # user flipped to LIVE
    state = {"positions": {"SPY|2026-07-10|752.0|C": {"option_id": "dry",
                                                      "qty": 1, "dry": True}}}
    out = agent.handle_event(make_event(event="EXITED"), cfg, state, broker,
                             object(), lambda s: None)
    assert out.startswith("[DRY-RUN]")
    assert broker.calls == []
    assert state["positions"] == {}


def test_dry_run_double_entry_is_noop(home, no_policy):
    cfg = {"dry_run": True}
    state = {"positions": {}}
    agent.handle_event(make_event(), cfg, state, None, None, lambda s: None)
    out = agent.handle_event(make_event(event_id="test-2"), cfg, state, None,
                             None, lambda s: None)
    assert out is None
    assert len(state["positions"]) == 1


# ── consent gate ─────────────────────────────────────────────────────────────

def test_consent_false_when_never_accepted(home):
    assert not agent.consent_ok()


def _accept(home, **over):
    import hashlib
    rec = {
        "accepted": True,
        "terms_version": agent.TERMS_VERSION,
        "disclaimer_sha256": hashlib.sha256(
            agent._disclaimer_text().encode()).hexdigest(),
    }
    rec.update(over)
    agent._save(agent.ACCEPTANCE_PATH, rec)


def test_consent_true_on_exact_acceptance(home):
    _accept(home)
    assert agent.consent_ok()


def test_consent_false_on_stale_terms_version(home):
    _accept(home, terms_version="agent-terms-2020.01.1")
    assert not agent.consent_ok()


def test_consent_false_when_disclaimer_text_changed(home):
    _accept(home, disclaimer_sha256="0" * 64)
    assert not agent.consent_ok()


def test_consent_false_when_not_accepted(home):
    _accept(home, accepted=False)
    assert not agent.consent_ok()
