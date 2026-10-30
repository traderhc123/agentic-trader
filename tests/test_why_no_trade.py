"""'Polling but never trading' must always say why: every dropped trade
lands in the dashboard log with a fix, one notification per kind per day,
failed exits retry, and low buying power is flagged before it bites."""

import json

import pytest

import agent
import broker_events
from conftest import make_event


@pytest.fixture
def notes(monkeypatch):
    sent = []
    monkeypatch.setattr(agent, "notify", lambda cfg, msg: sent.append(msg))
    monkeypatch.setattr(agent, "_problems_logged", set())
    return sent


def _log(home):
    p = home / "trades.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


class RejectingBroker:
    """Like Robinhood when the account has no cash."""

    def __init__(self, kind="order_failed",
                 msg="Robinhood rejected the buy order: Not enough buying power."):
        self.kind, self.msg = kind, msg

    def execute(self, client, cfg, event, state):
        broker_events.report(self.kind, self.msg)
        return False

    def buying_power(self, client, cfg):
        return 12.5


class SilentBroker:
    def execute(self, client, cfg, event, state):
        return False


CFG = {"max_entries_per_day": 5}


def test_rejected_order_is_logged_with_fix_and_notified_once(home, no_policy, notes):
    state = {"positions": {}, "seen": []}
    for i in range(3):
        agent.handle_event(make_event(event_id=f"e{i}"), CFG, state,
                           RejectingBroker(), object(), lambda s: None)
    rows = _log(home)
    assert [r["action"] for r in rows] == ["order_failed"] * 3
    assert "buying power now $12.50" in rows[0]["reason"]
    assert "add cash" in rows[0]["hint"]
    assert len(notes) == 1                     # once per kind per day
    assert "trade NOT placed" in notes[0] and "Fix:" in notes[0]


def test_budget_skip_is_visible(home, no_policy, notes):
    state = {"positions": {}, "seen": []}
    b = RejectingBroker("skip_budget",
                        "1 contract ≈ $420 exceeds your $100 per-trade budget — skipped")
    agent.handle_event(make_event(), CFG, state, b, object(), lambda s: None)
    (row,) = _log(home)
    assert row["action"] == "skip_budget" and "exceeds" in row["reason"]
    assert len(notes) == 1


def test_adapter_without_reason_gets_generic_entry(home, no_policy, notes):
    state = {"positions": {}, "seen": []}
    agent.handle_event(make_event(), CFG, state, SilentBroker(), object(),
                       lambda s: None)
    (row,) = _log(home)
    assert row["action"] == "broker_no_order"


def test_failed_exit_retries_and_logs_once(home, no_policy, notes):
    key = "SPY|2026-07-10|752.0|C"
    state = {"positions": {key: {"option_id": "x", "qty": 1}}, "seen": []}
    b = RejectingBroker("exit_failed", "Robinhood rejected the sell order: halted")
    ev = make_event(event="EXITED", event_id="x1")
    for _ in range(3):  # three polls
        with pytest.raises(agent.ExitNotDone):
            agent.handle_event(ev, CFG, state, b, object(), lambda s: None)
    assert key in state["positions"]           # still held, still trying
    assert [r["action"] for r in _log(home)] == ["exit_failed"]
    assert len(notes) == 1


def test_exit_for_position_never_opened_is_not_retried(home, no_policy, notes):
    state = {"positions": {}, "seen": []}
    out = agent.handle_event(make_event(event="EXITED"), CFG, state,
                             SilentBroker(), object(), lambda s: None)
    assert out is None and _log(home) == []


def test_dry_run_never_reports_broker_problems(home, no_policy, notes):
    state = {"positions": {}, "seen": []}
    agent.handle_event(make_event(), {**CFG, "dry_run": True}, state,
                       RejectingBroker(), object(), lambda s: None)
    assert not any(r["action"] == "order_failed" for r in _log(home))


@pytest.mark.parametrize("msg,want", [
    ("Insufficient buying power for this order", "add cash"),
    ("Options trading is not enabled on this account", "Options trading"),
    ("HTTP 401 unauthorized: token expired", "reconnect"),
    ("something odd", ""),
    ("not enough contracts to close", "already closed this position yourself"),
    ("insufficient qty available for order (requested: 2, available: 0)",
     "already closed this position yourself"),
    ("position does not exist", "already closed this position yourself"),
])
def test_hints(msg, want):
    h = broker_events.hint(msg)
    assert (want in h) if want else h == ""


def test_selling_more_than_held_is_not_a_cash_problem():
    assert not broker_events.is_cash_problem("insufficient qty available")
    assert not broker_events.is_cash_problem("Not enough contracts to close")
    assert broker_events.is_cash_problem("Not enough buying power")
    assert broker_events.is_cash_problem("insufficient funds")


def test_cash_warning_thresholds():
    assert agent._cash_warning({}, None) == ""
    assert "$0.00 buying power" in agent._cash_warning({}, 0.0)
    assert agent._cash_warning({}, 500.0) == ""
    w = agent._cash_warning({"sizing_mode": "budget", "budget_per_trade_usd": 1000}, 400.0)
    assert "more than your buying power" in w


def test_problems_today_groups_for_dashboard(home, no_policy, notes):
    state = {"positions": {}, "seen": []}
    for i in range(2):
        agent.handle_event(make_event(event_id=f"p{i}"), CFG, state,
                           RejectingBroker(), object(), lambda s: None)
    agent._log_trade({"action": "entry", "contract": "x"})
    (p,) = agent._problems_today()
    assert p["kind"] == "order_failed" and p["count"] == 2 and p["hint"]


def test_low_cash_warns_once_a_day_in_live_mode_only(home, notes):
    state, cash = {}, {"at": 0}
    agent._refresh_cash({"dry_run": True}, state, lambda s: None,
                        RejectingBroker(), object(), cash)
    assert cash["bp"] == 12.5 and cash["warning"] and notes == []
    for _ in range(2):
        agent._refresh_cash({}, state, lambda s: None, RejectingBroker(),
                            object(), cash)
    assert len(notes) == 1 and "add cash" in notes[0]


# ── adapters report their drop points ───────────────────────────────────────

def test_robinhood_rejection_reports_reason(monkeypatch):
    from brokers import robinhood

    class RH:
        def call_tool(self, name, args):
            return {"isError": True, "content": [{"type": "text", "text": json.dumps(
                {"detail": "Not enough buying power."})}]}
    monkeypatch.setattr(robinhood, "_blackout_limit_price", lambda *a: None)
    broker_events.begin()
    assert robinhood.place(RH(), {"robinhood_account": "1"}, "oid", "buy",
                           "open", 1) is None
    (ev,) = broker_events.drain()
    assert ev["kind"] == "order_failed" and "buying power" in ev["message"]


def test_robinhood_unfilled_entry_reports(monkeypatch):
    from brokers import robinhood
    state = {"positions": {"K": {"qty": 0, "pending": {"side": "buy", "requested": 1}}}}
    broker_events.begin()
    robinhood._finalize_pending(state, "K", 0)
    assert [e["kind"] for e in broker_events.drain()] == ["entry_unfilled"]
    state = {"positions": {"K": {"qty": 0, "pending": {"side": "buy", "requested": 1}}}}
    robinhood._finalize_pending(state, "K", 0, report_unfilled=False)
    assert broker_events.drain() == []


def test_robinhood_buying_power_zero_vs_unknown():
    from brokers import robinhood

    class RH:
        def __init__(self, body):
            self.body = body

        def call_tool(self, name, args):
            return {"content": [{"type": "text", "text": json.dumps(self.body)}]}
    assert robinhood.buying_power(RH({"buying_power": "0.00"}), {}) == 0.0
    assert robinhood.buying_power(RH({"buying_power": "250.5"}), {}) == 250.5
    assert robinhood.buying_power(RH({"other": 1}), {}) is None
