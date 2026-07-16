"""Order builders and sizing for both broker adapters. Zero network."""

import json

import pytest

from brokers import BROKERS, alpaca, broker_ready, key_brokers, moomoo, robinhood
from conftest import make_event


# ── registry ─────────────────────────────────────────────────────────────────

def test_registry_contains_all():
    assert set(BROKERS) == {"robinhood", "alpaca", "moomoo"}
    for mod in BROKERS.values():
        assert callable(mod.setup) and callable(mod.client) and callable(mod.execute)


def test_key_brokers_descriptor():
    descs = key_brokers()
    assert any(d["id"] == "alpaca" for d in descs)
    assert any(d["id"] == "moomoo" for d in descs)
    alp = next(d for d in descs if d["id"] == "alpaca")
    assert {f["id"] for f in alp["fields"]} >= {"alpaca_key_id", "alpaca_secret"}


def test_broker_ready_is_registry_driven(home):
    assert broker_ready({}) is False
    assert broker_ready({"broker": "moomoo"}) is True          # chosen adapter owns readiness
    assert broker_ready({"alpaca_key_id": "k", "alpaca_secret": "s"}) is True  # pre-`broker`-key config
    assert broker_ready({"broker": "nope"}) is False


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


# ── moomoo: option code builder ──────────────────────────────────────────────

@pytest.mark.parametrize("ev,expected", [
    (dict(ticker="SPY", expiry="2026-07-10", strike=752, type="C"),
     "US.SPY260710C752000"),
    (dict(ticker="SPY", expiry="2026-07-10", strike=752.5, type="P"),
     "US.SPY260710P752500"),
    (dict(ticker="A", expiry="2026-12-19", strike=0.5, type="C"),
     "US.A261219C500"),
])
def test_moomoo_code(ev, expected):
    assert moomoo._moomoo_code(make_event(**ev)) == expected


def test_moomoo_size_budget():
    cfg = {"sizing_mode": "budget", "budget_per_trade_usd": 500,
           "contracts_per_trade": 1, "max_contracts_per_trade": 25}
    assert moomoo._size(cfg, 2.50)[0] == 2
    assert moomoo._size(cfg, 6.00)[0] == 0          # 1 contract > budget
    assert moomoo._size(cfg, 0.0)[0] == 1           # no quote -> fixed fallback
    assert moomoo._size({"contracts_per_trade": 3}, 2.5)[0] == 3


def test_moomoo_connect_applies_defaults():
    cfg = {}
    # verify() will fail (no SDK/OpenD in CI) — connect must still normalize
    ok, note = moomoo.connect(cfg, {"moomoo_host": "", "moomoo_port": "",
                                    "moomoo_paper": True})
    assert cfg["moomoo_host"] == "127.0.0.1"
    assert cfg["moomoo_port"] == 11111
    assert cfg["broker"] == "moomoo"
    assert cfg["moomoo_paper"] is True
    assert isinstance(ok, bool) and note


class _FakeDF:
    def __init__(self, rows):
        self._rows = rows

    def __len__(self):
        return len(self._rows)

    @property
    def iloc(self):
        return self._rows


class _FakeTradeCtx:
    def __init__(self, sdk):
        self.sdk = sdk

    def unlock_trade(self, pwd):
        self.sdk.unlocked.append(pwd)
        return (self.sdk.RET_OK if pwd == "good" else -1), "unlock"

    def place_order(self, **kw):
        self.sdk.orders.append(kw)
        return self.sdk.RET_OK, _FakeDF([{"order_id": "mm-1"}])

    def get_acc_list(self):
        return self.sdk.RET_OK, _FakeDF([{"acc_id": 1}])

    def close(self):
        pass


class _FakeSDK:
    """Duck-typed stand-in for the moomoo-api package."""
    RET_OK = 0

    class TrdEnv:
        SIMULATE, REAL = "SIM", "REAL"

    class TrdSide:
        BUY, SELL = "BUY", "SELL"

    class OrderType:
        NORMAL = "NORMAL"

    class TrdMarket:
        US = "US"

    class SecurityFirm:
        FUTUINC = "FUTUINC"

    def __init__(self, ask=1.5):
        self.orders = []
        self.unlocked = []
        self._ask = ask

    def OpenSecTradeContext(self, **kw):
        return _FakeTradeCtx(self)

    def OpenQuoteContext(self, **kw):
        sdk = self

        class _Q:
            def get_market_snapshot(self, codes):
                if sdk._ask <= 0:
                    return -1, None
                return sdk.RET_OK, _FakeDF([{"ask_price": sdk._ask,
                                             "last_price": sdk._ask}])

            def close(self):
                pass
        return _Q()


def _moomoo_cfg(**over):
    cfg = {"broker": "moomoo", "moomoo_paper": True, "contracts_per_trade": 1}
    cfg.update(over)
    return cfg


def test_moomoo_execute_entry_places_and_records(monkeypatch):
    sdk = _FakeSDK(ask=1.5)
    monkeypatch.setattr(moomoo, "_sdk", lambda: sdk)
    state = {"positions": {}}
    assert moomoo.execute(None, _moomoo_cfg(), make_event(), state)
    assert len(sdk.orders) == 1
    order = sdk.orders[0]
    assert order["code"] == "US.SPY260710C752000"
    assert order["trd_side"] == "BUY" and order["price"] == 1.5
    assert state["positions"]["SPY|2026-07-10|752.0|C"]["qty"] == 1
    assert sdk.unlocked == []  # SIMULATE never needs the trade password


def test_moomoo_execute_entry_no_quote_skips(monkeypatch):
    sdk = _FakeSDK(ask=0.0)
    monkeypatch.setattr(moomoo, "_sdk", lambda: sdk)
    state = {"positions": {}}
    assert not moomoo.execute(None, _moomoo_cfg(), make_event(), state)
    assert sdk.orders == [] and state["positions"] == {}


def test_moomoo_execute_entry_dedupes(monkeypatch):
    sdk = _FakeSDK()
    monkeypatch.setattr(moomoo, "_sdk", lambda: sdk)
    state = {"positions": {"SPY|2026-07-10|752.0|C": {"qty": 1}}}
    assert not moomoo.execute(None, _moomoo_cfg(), make_event(), state)
    assert sdk.orders == []


def test_moomoo_execute_exit_closes_what_it_opened(monkeypatch):
    sdk = _FakeSDK(ask=2.0)
    monkeypatch.setattr(moomoo, "_sdk", lambda: sdk)
    state = {"positions": {"SPY|2026-07-10|752.0|C":
                           {"option_id": "US.SPY260710C752000", "qty": 2}}}
    assert moomoo.execute(None, _moomoo_cfg(), make_event(event="EXITED"), state)
    assert sdk.orders[0]["trd_side"] == "SELL" and sdk.orders[0]["qty"] == 2
    assert state["positions"] == {}


def test_moomoo_live_without_trade_password_blocks(monkeypatch, capsys):
    sdk = _FakeSDK(ask=1.5)
    monkeypatch.setattr(moomoo, "_sdk", lambda: sdk)
    state = {"positions": {}}
    cfg = _moomoo_cfg(moomoo_paper=False)  # LIVE, no moomoo_trade_pwd
    assert not moomoo.execute(None, cfg, make_event(), state)
    assert sdk.orders == [] and state["positions"] == {}
    assert "trade password" in capsys.readouterr().out


def test_moomoo_live_unlocks_before_order(monkeypatch):
    sdk = _FakeSDK(ask=1.5)
    monkeypatch.setattr(moomoo, "_sdk", lambda: sdk)
    state = {"positions": {}}
    cfg = _moomoo_cfg(moomoo_paper=False, moomoo_trade_pwd="good")
    assert moomoo.execute(None, cfg, make_event(), state)
    assert sdk.unlocked == ["good"]
    assert sdk.orders[0]["trd_env"] == "REAL"


def test_moomoo_sdk_missing_is_actionable(monkeypatch, capsys):
    def boom():
        raise RuntimeError("moomoo SDK not installed — run:  pip install moomoo-api")
    monkeypatch.setattr(moomoo, "_sdk", boom)
    assert not moomoo.execute(None, _moomoo_cfg(), make_event(), {"positions": {}})
    assert "moomoo-api" in capsys.readouterr().out
