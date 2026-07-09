"""Order builders and sizing for both broker adapters. Zero network."""

import json

import pytest

from brokers import BROKERS, alpaca, key_brokers, robinhood
from conftest import make_event


# ── registry ─────────────────────────────────────────────────────────────────

def test_registry_contains_both():
    assert set(BROKERS) == {"robinhood", "alpaca"}
    for mod in BROKERS.values():
        assert callable(mod.setup) and callable(mod.client) and callable(mod.execute)


def test_key_brokers_descriptor():
    descs = key_brokers()
    assert any(d["id"] == "alpaca" for d in descs)
    alp = next(d for d in descs if d["id"] == "alpaca")
    assert {f["id"] for f in alp["fields"]} >= {"alpaca_key_id", "alpaca_secret"}


# ── alpaca: OCC symbol builder ───────────────────────────────────────────────

@pytest.mark.parametrize("ev,expected", [
    (dict(ticker="SPY", expiry="2026-07-10", strike=752, type="C"),
     "SPY260710C00752000"),
    (dict(ticker="SPY", expiry="2026-07-10", strike=752.5, type="P"),
     "SPY260710P00752500"),
    (dict(ticker="A", expiry="2026-12-19", strike=0.5, type="C"),
     "A261219C00000500"),
    (dict(ticker="NVDA", expiry="2026-01-02", strike=1234.567, type="P"),
     "NVDA260102P01234567"),
])
def test_alpaca_occ_symbol(ev, expected):
    assert alpaca._occ_symbol(make_event(**ev)) == expected


# ── alpaca: budget sizing ────────────────────────────────────────────────────

def _budget_cfg(**over):
    cfg = {"sizing_mode": "budget", "budget_per_trade_usd": 500,
           "contracts_per_trade": 1, "max_contracts_per_trade": 25}
    cfg.update(over)
    return cfg


def test_alpaca_size_budget_buys_what_fits(monkeypatch):
    monkeypatch.setattr(alpaca, "_quote_price", lambda cfg, occ: 2.50)
    qty, note = alpaca._size(_budget_cfg(), "OCC")
    assert qty == 2  # $500 // ($2.50 * 100)


def test_alpaca_size_skips_when_one_contract_exceeds_budget(monkeypatch):
    monkeypatch.setattr(alpaca, "_quote_price", lambda cfg, occ: 6.00)
    qty, note = alpaca._size(_budget_cfg(), "OCC")
    assert qty == 0
    assert "exceeds" in note


def test_alpaca_size_respects_max_contracts(monkeypatch):
    monkeypatch.setattr(alpaca, "_quote_price", lambda cfg, occ: 0.05)
    qty, _ = alpaca._size(_budget_cfg(budget_per_trade_usd=10_000), "OCC")
    assert qty == 25  # 2000 fit, capped


def test_alpaca_size_no_quote_falls_back_to_fixed(monkeypatch):
    monkeypatch.setattr(alpaca, "_quote_price", lambda cfg, occ: 0.0)
    qty, note = alpaca._size(_budget_cfg(contracts_per_trade=3), "OCC")
    assert qty == 3
    assert "no quote" in note


def test_alpaca_size_contracts_mode():
    qty, note = alpaca._size({"sizing_mode": "contracts",
                              "contracts_per_trade": 4}, "OCC")
    assert (qty, note) == (4, "fixed contract count")


# ── alpaca: execute ──────────────────────────────────────────────────────────

def test_alpaca_execute_entry_places_buy(monkeypatch):
    orders = []
    monkeypatch.setattr(alpaca, "_order",
                        lambda cfg, occ, side, qty:
                        orders.append((occ, side, qty)) or "oid-1")
    monkeypatch.setattr(alpaca, "_quote_price", lambda cfg, occ: 2.50)
    state = {"positions": {}}
    changed = alpaca.execute(alpaca._Client(_budget_cfg()), {}, make_event(), state)
    assert changed
    assert orders == [("SPY260710C00752000", "buy", 2)]
    pos = state["positions"]["SPY|2026-07-10|752.0|C"]
    assert pos["qty"] == 2 and pos["option_id"] == "SPY260710C00752000"


def test_alpaca_execute_never_double_enters(monkeypatch):
    monkeypatch.setattr(alpaca, "_order",
                        lambda *a: pytest.fail("must not order"))
    state = {"positions": {"SPY|2026-07-10|752.0|C": {"qty": 1}}}
    assert not alpaca.execute(alpaca._Client({}), {}, make_event(), state)


def test_alpaca_execute_exit_sells_stored_position(monkeypatch):
    orders = []
    monkeypatch.setattr(alpaca, "_order",
                        lambda cfg, occ, side, qty:
                        orders.append((occ, side, qty)) or "oid-2")
    state = {"positions": {"SPY|2026-07-10|752.0|C":
                           {"option_id": "STORED_OCC", "qty": 3}}}
    changed = alpaca.execute(alpaca._Client({}), {},
                             make_event(event="EXITED"), state)
    assert changed
    assert orders == [("STORED_OCC", "sell", 3)]  # sells what it OPENED
    assert state["positions"] == {}


def test_alpaca_execute_exit_without_position_is_noop(monkeypatch):
    monkeypatch.setattr(alpaca, "_order",
                        lambda *a: pytest.fail("must not order"))
    assert not alpaca.execute(alpaca._Client({}), {},
                              make_event(event="EXITED"), {"positions": {}})


def test_alpaca_failed_order_leaves_state_clean(monkeypatch):
    monkeypatch.setattr(alpaca, "_order", lambda *a: None)  # HTTP failure
    monkeypatch.setattr(alpaca, "_quote_price", lambda cfg, occ: 2.50)
    state = {"positions": {}}
    assert not alpaca.execute(alpaca._Client(_budget_cfg()), {},
                              make_event(), state)
    assert state["positions"] == {}


def test_alpaca_client_requires_both_keys():
    assert alpaca.client({}) is None
    assert alpaca.client({"alpaca_key_id": "k"}) is None
    assert alpaca.client({"alpaca_key_id": "k", "alpaca_secret": "s"}) is not None


def test_alpaca_base_url_paper_vs_live():
    assert "paper" in alpaca._base({"alpaca_paper": True})
    assert "paper" in alpaca._base({})  # paper is the DEFAULT
    assert "paper" not in alpaca._base({"alpaca_paper": False})


# ── robinhood: sizing + payload walking ─────────────────────────────────────

class FakeRH:
    """Fake MCP client returning canned tool results."""

    def __init__(self, results=None):
        self.results = results or {}
        self.calls = []

    def call_tool(self, name, args):
        self.calls.append((name, args))
        return self.results.get(name, {"content": []})


def _mcp_result(payload, is_error=False):
    return {"content": [{"type": "text", "text": json.dumps(payload)}],
            "isError": is_error}


def test_robinhood_size_budget(monkeypatch):
    monkeypatch.setattr(robinhood, "quote_price", lambda rh, oid: 2.50)
    qty, _ = robinhood.size_contracts(None, _budget_cfg(), "oid")
    assert qty == 2


def test_robinhood_size_skip_over_budget(monkeypatch):
    monkeypatch.setattr(robinhood, "quote_price", lambda rh, oid: 9.99)
    qty, note = robinhood.size_contracts(None, _budget_cfg(), "oid")
    assert qty == 0 and "exceeds" in note


def test_robinhood_size_budget_unset_skips():
    qty, note = robinhood.size_contracts(
        None, {"sizing_mode": "budget"}, "oid")
    assert qty == 0 and "unset" in note


def test_robinhood_size_no_quote_skips(monkeypatch):
    monkeypatch.setattr(robinhood, "quote_price", lambda rh, oid: 0.0)
    qty, note = robinhood.size_contracts(None, _budget_cfg(), "oid")
    assert qty == 0 and "no quote" in note


def test_robinhood_find_number_walks_nested():
    payload = {"data": {"results": [{"ask_price": "2.55", "junk": "x"}]}}
    assert robinhood._find_number(payload, ["ask_price"]) == 2.55
    assert robinhood._find_number(payload, ["missing"]) == 0.0
    assert robinhood._find_number({"ask_price": "not-a-number"},
                                  ["ask_price"]) == 0.0


def test_robinhood_resolve_instrument_finds_id():
    rh = FakeRH({"get_option_instruments": _mcp_result(
        {"data": {"results": [{"id": "opt-123", "chain_symbol": "SPY"}]}})})
    assert robinhood.resolve_instrument(rh, make_event()) == "opt-123"


def test_robinhood_resolve_instrument_empty_payload():
    assert robinhood.resolve_instrument(FakeRH(), make_event()) == ""


def test_robinhood_execute_entry(monkeypatch):
    placed = []
    monkeypatch.setattr(robinhood, "resolve_instrument",
                        lambda rh, ev: "opt-123")
    monkeypatch.setattr(robinhood, "size_contracts",
                        lambda rh, cfg, oid: (2, "test"))
    monkeypatch.setattr(robinhood, "place",
                        lambda rh, cfg, oid, side, effect, qty:
                        placed.append((oid, side, effect, qty)) or "order-1")
    state = {"positions": {}}
    assert robinhood.execute(None, {"robinhood_account": "123"},
                             make_event(), state)
    assert placed == [("opt-123", "buy", "open", 2)]
    assert state["positions"]["SPY|2026-07-10|752.0|C"]["option_id"] == "opt-123"


def test_robinhood_execute_entry_unresolvable_skips(monkeypatch):
    monkeypatch.setattr(robinhood, "resolve_instrument", lambda rh, ev: "")
    monkeypatch.setattr(robinhood, "place",
                        lambda *a: pytest.fail("must not order"))
    assert not robinhood.execute(None, {}, make_event(), {"positions": {}})


def test_robinhood_execute_exit_closes_what_it_opened(monkeypatch):
    placed = []
    monkeypatch.setattr(robinhood, "place",
                        lambda rh, cfg, oid, side, effect, qty:
                        placed.append((oid, side, effect, qty)) or "order-2")
    state = {"positions": {"SPY|2026-07-10|752.0|C":
                           {"option_id": "opt-123", "qty": 2}}}
    assert robinhood.execute(None, {}, make_event(event="EXITED"), state)
    assert placed == [("opt-123", "sell", "close", 2)]
    assert state["positions"] == {}


def test_robinhood_failed_place_keeps_position(monkeypatch):
    monkeypatch.setattr(robinhood, "place", lambda *a: None)
    state = {"positions": {"SPY|2026-07-10|752.0|C":
                           {"option_id": "opt-123", "qty": 2}}}
    assert not robinhood.execute(None, {}, make_event(event="EXITED"), state)
    assert "SPY|2026-07-10|752.0|C" in state["positions"]
