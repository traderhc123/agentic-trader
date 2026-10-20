"""Every signal that didn't become a trade shows why on the dashboard —
including the owner's own rules/caps and dry-run budget skips — and budget
skips come with what the trades cost and a budget that would have caught them."""

import json

import pytest

import agent
import broker_events
from conftest import make_event


@pytest.fixture
def quiet(monkeypatch):
    monkeypatch.setattr(agent, "notify", lambda cfg, msg: None)
    monkeypatch.setattr(agent, "_problems_logged", set())


def _log(home):
    p = home / "trades.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


class PricedBroker:
    """Budget sizing like the real adapters, from a fixed ask per ticker."""

    def __init__(self, asks):
        self.asks = asks

    def resolve_instrument(self, client, ev):
        return ev["ticker"]

    def size_contracts(self, client, cfg, oid):
        budget = float(cfg["budget_per_trade_usd"])
        qty = int(budget // (self.asks[oid] * 100))
        if qty < 1:
            return 0, (f"1 contract ≈ ${self.asks[oid] * 100:,.0f} exceeds your "
                       f"${budget:,.0f} per-trade budget — skipped")
        return qty, "ok"

    def execute(self, client, cfg, ev, state):
        qty, note = self.size_contracts(client, cfg, ev["ticker"])
        if qty < 1:
            broker_events.report("skip_budget", note)
            return False
        key = f"{ev['ticker']}|{ev['expiry']}|{ev['strike']}|{ev['type']}"
        state["positions"][key] = {"option_id": "x", "qty": qty}
        return True


ASKS = {"META": 25.1, "TSLA": 11.8, "AMD": 3.6, "NFLX": 3.05, "WULF": 0.44}
CFG = {"max_entries_per_day": 5, "sizing_mode": "budget",
       "budget_per_trade_usd": 500, "other_sizing_mode": "budget",
       "other_budget_per_trade_usd": 100}


def _run(cfg, home):
    state = {"positions": {}, "seen": []}
    b = PricedBroker(ASKS)
    for t in ASKS:
        agent.handle_event(make_event(ticker=t, event_id=f"e-{t}", track="other"),
                           cfg, state, b, object(), lambda s: None)
    return state


@pytest.mark.parametrize("dry", [False, True])
def test_shadow_budget_skips_logged_with_book_and_cheap_one_taken(home, no_policy, quiet, dry):
    state = _run({**CFG, "dry_run": dry}, home)
    assert any(k.startswith("WULF|") for k in state["positions"])
    skips = [r for r in _log(home) if r["action"] == "skip_budget"]
    assert len(skips) == 4 and all(r["book"] == "shadow" for r in skips)
    assert all(bool(r.get("dry")) == dry for r in skips)


def test_budget_hint_suggests_a_budget(home, no_policy, quiet):
    _run({**CFG, "dry_run": True}, home)
    (h,) = agent._budget_hints(CFG)
    assert h["book"] == "shadow" and h["budget"] == 100 and h["skipped"] == 4
    assert (h["min_cost"], h["max_cost"]) == (305, 2510)
    # 3/4 point of [305, 360, 1180, 2510] is 2510 → $2,550 covers all 4
    assert h["suggest"] % 50 == 0 and h["would_take"] == sum(
        c <= h["suggest"] for c in (305, 360, 1180, 2510))
    assert h["command"] == f"set other budget {h['suggest']}"
    # the button's command really changes the shadow budget, and only that
    agent._save(agent.CONFIG_PATH, dict(CFG))
    agent._apply_command(h["command"])
    saved = agent._load(agent.CONFIG_PATH, {})
    assert saved["other_budget_per_trade_usd"] == h["suggest"]
    assert saved["budget_per_trade_usd"] == 500


def test_no_hint_once_sizing_no_longer_budget(home, no_policy, quiet):
    _run({**CFG, "dry_run": True}, home)
    later = {**CFG, "other_sizing_mode": "contracts", "other_contracts_per_trade": 1}
    later.pop("other_budget_per_trade_usd")
    assert agent._budget_hints(later) == []


def test_own_rule_skips_show_in_didnt_happen_panel(home, no_policy, quiet):
    state = {"positions": {}, "seen": []}
    cfg = {**CFG, "rule_sides": "calls"}
    agent.handle_event(make_event(ticker="WULF", type="P", track="other"),
                       cfg, state, PricedBroker(ASKS), object(), lambda s: None)
    kinds = {p["kind"] for p in agent._problems_today()}
    assert "skip_rule" in kinds
    (row,) = _log(home)
    assert row["book"] == "shadow"
