"""Per-book sizing: fixed dollars, fixed contracts, or % of account — main
book and shadow ("other trades") book each choose their own."""

import pytest

import agent
import setup_plan
import webui
from brokers import alpaca, moomoo, robinhood
from conftest import make_event


class AcctBroker:
    """Stub broker exposing account_value; records the cfg execute() sees."""

    def __init__(self, value):
        self.value = value
        self.seen = []

    def account_value(self, client, cfg):
        if isinstance(self.value, Exception):
            raise self.value
        return self.value

    def execute(self, client, cfg, event, state):
        self.seen.append(cfg)
        key = f"{event['ticker']}|{event['expiry']}|{event['strike']}|{event['type']}"
        state["positions"][key] = {"option_id": "stub", "qty": 1}
        return True


# ── set_book_sizing ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("mode,raw,key,val", [
    ("budget", "$1,500", "budget_per_trade_usd", 1500.0),
    ("contracts", "3", "contracts_per_trade", 3),
    ("percent", "2.5%", "percent_per_trade", 2.5),
])
def test_main_book_modes(mode, raw, key, val):
    cfg = {}
    assert agent.set_book_sizing(cfg, "main", mode, raw) == (True, "")
    assert cfg["sizing_mode"] == mode and cfg[key] == val
    assert cfg["max_contracts_per_trade"] == 100


@pytest.mark.parametrize("mode,raw", [
    ("percent", "0"), ("percent", "101"), ("contracts", "1.5"),
    ("contracts", "0"), ("budget", "-5"), ("budget", "abc"), ("bogus", "5"),
])
def test_invalid_values_rejected(mode, raw):
    cfg = {"sizing_mode": "budget", "budget_per_trade_usd": 500}
    ok, err = agent.set_book_sizing(cfg, "main", mode, raw)
    assert not ok and err
    assert cfg == {"sizing_mode": "budget", "budget_per_trade_usd": 500}


def test_shadow_book_independent_mode_and_clear():
    cfg = {"sizing_mode": "budget", "budget_per_trade_usd": 500}
    assert agent.set_book_sizing(cfg, "other", "budget", "100")[0]
    assert agent.set_book_sizing(cfg, "other", "percent", "1")[0]
    # switching modes drops the stale override value
    assert "other_budget_per_trade_usd" not in cfg
    assert cfg["other_sizing_mode"] == "percent" and cfg["other_percent_per_trade"] == 1
    assert agent.set_book_sizing(cfg, "other", "same", "")[0]
    assert not any(k.startswith("other_") for k in cfg)
    assert cfg["budget_per_trade_usd"] == 500  # main untouched


def test_shadow_percent_overlays_only_other_track():
    cfg = {"sizing_mode": "contracts", "contracts_per_trade": 2}
    agent.set_book_sizing(cfg, "other", "percent", "1")
    other = agent._sizing_cfg(cfg, make_event(track="other"))
    assert other["sizing_mode"] == "percent" and other["percent_per_trade"] == 1
    assert agent._sizing_cfg(cfg, make_event(track="main"))["sizing_mode"] == "contracts"


# ── _resolve_percent ────────────────────────────────────────────────────────

def test_percent_resolves_to_budget():
    cfg = {"sizing_mode": "percent", "percent_per_trade": 2}
    out, skip = agent._resolve_percent(cfg, AcctBroker(25_000), object())
    assert skip == ""
    assert out["sizing_mode"] == "budget" and out["budget_per_trade_usd"] == 500
    assert cfg["sizing_mode"] == "percent"  # caller's cfg not mutated


@pytest.mark.parametrize("value", [0.0, RuntimeError("down")])
def test_percent_skips_when_account_value_unknown(value):
    cfg = {"sizing_mode": "percent", "percent_per_trade": 2}
    _, skip = agent._resolve_percent(cfg, AcctBroker(value), object())
    assert "account value" in skip


def test_percent_skips_for_broker_without_account_value():
    cfg = {"sizing_mode": "percent", "percent_per_trade": 2}
    _, skip = agent._resolve_percent(cfg, object(), object())
    assert skip


def test_non_percent_modes_pass_through():
    cfg = {"sizing_mode": "budget", "budget_per_trade_usd": 300}
    assert agent._resolve_percent(cfg, None, None) == (cfg, "")


# ── handle_event wiring ─────────────────────────────────────────────────────

def test_live_entry_gets_budget_from_percent(home, no_policy):
    b = AcctBroker(10_000)
    cfg = {"dry_run": False, "sizing_mode": "budget", "budget_per_trade_usd": 999}
    agent.set_book_sizing(cfg, "other", "percent", "3")
    state = {"positions": {}}
    out = agent.handle_event(make_event(track="other"), cfg, state, b,
                             object(), lambda s: None)
    assert out.startswith("ENTRY")
    assert b.seen[-1]["sizing_mode"] == "budget"
    assert b.seen[-1]["budget_per_trade_usd"] == 300


def test_live_entry_skipped_when_account_unreadable(home, no_policy):
    b = AcctBroker(0.0)
    cfg = {"dry_run": False, "sizing_mode": "percent", "percent_per_trade": 2}
    state = {"positions": {}}
    out = agent.handle_event(make_event(), cfg, state, b, object(),
                             lambda s: None)
    assert "SKIPPED" in out and b.seen == []
    assert agent._recent_trades(1)[-1]["action"] == "skip_sizing"


def test_exit_never_needs_account_value(home, no_policy):
    b = AcctBroker(RuntimeError("down"))
    b.execute = lambda c, cfg, ev, st: bool(st["positions"].pop(
        "SPY|2026-07-10|752.0|C", None))
    cfg = {"dry_run": False, "sizing_mode": "percent", "percent_per_trade": 2}
    state = {"positions": {"SPY|2026-07-10|752.0|C": {"option_id": "x", "qty": 1}}}
    out = agent.handle_event(make_event(event="EXITED"), cfg, state, b,
                             object(), lambda s: None)
    assert out.startswith("EXIT")


# ── brokers ─────────────────────────────────────────────────────────────────

def test_brokers_never_size_unresolved_percent(monkeypatch):
    cfg = {"sizing_mode": "percent", "percent_per_trade": 2, "contracts_per_trade": 5}
    assert alpaca._size(cfg, "OCC")[0] == 0
    assert moomoo._size(cfg, 2.0)[0] == 0
    assert robinhood.size_contracts(None, cfg, "id")[0] == 0


def test_alpaca_account_value(monkeypatch):
    class R:
        status_code = 200

        def json(self):
            return {"equity": "12345.67"}
    monkeypatch.setattr(alpaca.requests, "get", lambda *a, **k: R())
    assert alpaca.account_value(None, {"alpaca_key_id": "k", "alpaca_secret": "s"}) == 12345.67


def test_robinhood_account_value_uses_total_value(monkeypatch):
    """Shape of a real get_portfolio read (2026-07-27), with holdings added:
    total_value is the account; equity_value (stocks only) must be ignored."""
    monkeypatch.setattr(robinhood, "content_json", lambda r: r)
    seen = {}

    class RH:
        def call_tool(self, name, args):
            seen.update(name=name, args=args)
            return {"data": {"total_value": "1500", "cash": "200",
                             "buying_power": "200", "equity_value": "1000",
                             "options_value": "300"}}
    assert robinhood.account_value(RH(), {"robinhood_account": "693"}) == 1500
    assert seen == {"name": "get_portfolio", "args": {"account_number": "693"}}


def test_robinhood_account_value_falls_back_to_buying_power(monkeypatch):
    monkeypatch.setattr(robinhood, "content_json", lambda r: r)

    class RH:
        def call_tool(self, name, args):
            return {"buying_power": "800"}
    assert robinhood.account_value(RH(), {}) == 800


# ── wizard + plan + descriptions ────────────────────────────────────────────

def test_sizing_desc_both_books():
    cfg = {"sizing_mode": "percent", "percent_per_trade": 2,
           "other_sizing_mode": "contracts", "other_contracts_per_trade": 1,
           "include_other_trades": True}
    assert agent._sizing_desc(cfg) == \
        "2% of account/trade (other trades: 1 contract(s)/trade)"


def test_wizard_detail_and_notes():
    cfg = {"sizing_mode": "percent", "percent_per_trade": 2,
           "other_sizing_mode": "budget", "other_budget_per_trade_usd": 100}
    assert webui._sizing_detail(cfg) == {
        "main": {"mode": "percent", "value": 2},
        "other": {"mode": "budget", "value": 100}}
    n = webui._restore_notes(cfg, {})
    assert n["sizing"] == ("Sizing saved: 2% of account per trade"
                           " · shadow book up to $100 per trade")


def test_wizard_has_percent_option_for_both_books():
    html = webui._WIZARD_HTML
    assert 'name="szmode" value="percent"' in html
    assert 'name="szomode" value="percent"' in html
    assert 'name="szomode" value="contracts"' in html


def test_setup_plan_percent(tmp_path):
    new, rep = setup_plan.apply({"sizing": {"mode": "percent", "percent_per_trade": 2}},
                                {}, True, str(tmp_path))
    assert rep["errors"] == []
    assert new["sizing_mode"] == "percent" and new["percent_per_trade"] == 2
    new, rep = setup_plan.apply({"sizing": {"mode": "percent", "percent_per_trade": 150}},
                                {}, True, str(tmp_path))
    assert new is None and any("at most 100" in e for e in rep["errors"])


def test_chat_set_percent(home):
    ok, reply = agent._apply_command("set other percent 1.5")
    assert ok and "1.5%" in reply
    cfg = agent._load(agent.CONFIG_PATH, {})
    assert cfg["other_sizing_mode"] == "percent"
    assert cfg["other_percent_per_trade"] == 1.5
