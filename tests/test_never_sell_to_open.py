"""The agent never sells to open.

It buys options to open a long position and sells to close what the
account holds — nothing else, at any broker, live or practice, whatever the
signal, the state file or a lost answer says (brokers/order_guard.py). These
tests drive the real adapters against fake brokers that, unlike the real
ones, WOULD let a plain sell open a short, so any request that could is
caught. Zero network."""

import ast
import json
import os
import random
import time
from datetime import datetime, timezone

import pytest
import requests

import agent
import broker_events
from brokers import alpaca, moomoo, order_guard, robinhood
from brokers.order_guard import RefusedOrder, check_order
from brokers.robinhood_mcp import RobinhoodMCP, order_tool_refusal
from conftest import make_event

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXPIRY = "2099-12-18"
KEY = f"SPY|{EXPIRY}|752.0|C"
OCC = "SPY991218C00752000"
CODE = "US.SPY991218C752000"
RH_ID = "14a8e49b-0f10-4d64-9cf9-bd1a68e9da44"


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(alpaca, "_CONFIRM_WAIT_S", 0)
    monkeypatch.setattr(robinhood, "_SETTLE_WAIT_S", 0)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    monkeypatch.setattr(robinhood, "_in_opening_blackout", lambda now=None: False)
    broker_events.begin()


def _exit_ev(**over):
    return make_event(event="EXITED", expiry=EXPIRY, event_id="x-1", **over)


def _reasons():
    return [r.get("reason") for r in broker_events.drain()]


# ── Layer 1: the one check ──────────────────────────────────────────────────

def test_buy_to_open_and_sell_to_close_pass():
    assert check_order("buy", "open", 3, None) == 3
    assert check_order("sell", "close", 3, 5) == 3
    assert check_order("sell", "close", 3, 1) == 1          # never more than held
    assert check_order("buy", "open", 2.0, None) == 2       # a whole float is fine


@pytest.mark.parametrize("side,effect", [("sell", "open"), ("buy", "close"),
                                         ("sell", None), ("short", "open"),
                                         ("SELL", "close"), ("", "")])
def test_any_other_side_and_effect_is_refused(side, effect):
    with pytest.raises(RefusedOrder) as e:
        check_order(side, effect, 1, 5)
    assert e.value.code == "invalid"


@pytest.mark.parametrize("qty", [0, -1, -3.0, float("nan"), float("inf"), True,
                                 False, 1.5, "2", None])
def test_a_quantity_that_isnt_a_positive_whole_number_is_refused(qty):
    for side, effect in (("buy", "open"), ("sell", "close")):
        with pytest.raises(RefusedOrder) as e:
            check_order(side, effect, qty, 5)
        assert e.value.code == "invalid"


@pytest.mark.parametrize("held", [0, -2])
def test_a_sell_with_nothing_held_is_refused(held):
    with pytest.raises(RefusedOrder) as e:
        check_order("sell", "close", 1, held)
    assert e.value.code == "nothing_held"


@pytest.mark.parametrize("held", [None, float("nan"), True, "3"])
def test_unknown_holdings_only_pass_where_the_broker_refuses_an_opening_sell(held):
    with pytest.raises(RefusedOrder) as e:
        check_order("sell", "close", 2, held)
    assert e.value.code == "holdings_unknown"
    assert check_order("sell", "close", 2, held, broker_refuses_open=True) == 2


# ── Alpaca ──────────────────────────────────────────────────────────────────

class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body
        self.text = json.dumps(body)

    def json(self):
        return self._body


class AlpacaAPI:
    """Alpaca's REST API for one account. `held`/`avail` script the
    position (avail < held: an open sell is working on it); `pos_status`
    the positions read; `reject` the next order's rejection."""

    def __init__(self, monkeypatch, held=0, avail=None, side="long", pos_status=200):
        self.held, self.avail, self.side, self.pos_status = held, avail, side, pos_status
        self.posts, self.reject = [], None
        monkeypatch.setattr(alpaca.requests, "post", self.post)
        monkeypatch.setattr(alpaca.requests, "get", self.get)
        monkeypatch.setattr(alpaca.requests, "delete", lambda url, **kw: _Resp(204, {}))

    def post(self, url, **kw):
        assert url.endswith("/v2/orders")
        self.posts.append(dict(kw["json"]))
        if self.reject:
            return _Resp(*self.reject)
        return _Resp(200, {"id": f"a-{len(self.posts)}", "status": "filled"})

    def get(self, url, **kw):
        if url.endswith("/v2/positions"):
            if self.pos_status != 200:
                return _Resp(self.pos_status, {"message": "down"})
            if not self.held:
                return _Resp(200, [])
            avail = self.held if self.avail is None else self.avail
            return _Resp(200, [{"symbol": OCC, "qty": str(self.held),
                                "qty_available": str(avail), "side": self.side,
                                "asset_class": "us_option"}])
        if "/v2/orders/" in url:
            return _Resp(200, {"id": url.rsplit("/", 1)[-1], "status": "filled",
                               "filled_qty": self.posts[-1]["qty"]})
        if url.endswith("/v2/orders:by_client_order_id"):
            return _Resp(404, {"message": "order not found"})
        if "quotes/latest" in url:
            return _Resp(200, {"quotes": {OCC: {"ap": 1.2, "bp": 1.0}}})
        return _Resp(404, {})


ALP = {"broker": "alpaca", "alpaca_key_id": "k", "alpaca_secret": "s",
       "contracts_per_trade": 3}


def _alp_sell(api, qty=3):
    st = {"positions": {KEY: {"option_id": OCC, "qty": qty}}}
    changed = alpaca.execute(alpaca.client(ALP), ALP, _exit_ev(), st)
    return changed, st


def test_alpaca_sell_with_nothing_held_sends_no_order(monkeypatch):
    api = AlpacaAPI(monkeypatch, held=0)
    changed, st = _alp_sell(api)
    assert api.posts == [] and not changed and st["positions"][KEY]["qty"] == 3
    assert _reasons() == ["nothing_held"]


def test_alpaca_sells_only_what_the_account_holds(monkeypatch):
    api = AlpacaAPI(monkeypatch, held=1)
    changed, st = _alp_sell(api, qty=3)
    (body,) = api.posts
    assert body["qty"] == "1" and body["side"] == "sell"
    assert body["position_intent"] == "sell_to_close"
    assert changed and st["positions"][KEY]["qty"] == 2   # the rest: settled later


def test_alpaca_no_second_sell_while_one_works_on_the_whole_holding(monkeypatch):
    api = AlpacaAPI(monkeypatch, held=2, avail=0)
    changed, _st = _alp_sell(api, qty=2)
    assert api.posts == [] and not changed
    assert _reasons() == ["nothing_held"]


def test_alpaca_short_position_is_never_traded(monkeypatch):
    api = AlpacaAPI(monkeypatch, held=-2, side="short")
    changed, _st = _alp_sell(api)
    assert api.posts == [] and not changed
    (rep,) = broker_events.drain()
    assert rep["reason"] == "short_seen" and rep["short"] == 2
    assert "SHORT 2x" in rep["message"]


def test_alpaca_unreadable_holdings_still_sell_to_close(monkeypatch):
    """Alpaca itself refuses a sell_to_close of contracts that aren't there,
    so an unreadable positions list doesn't hold the exit back."""
    api = AlpacaAPI(monkeypatch, pos_status=503)
    changed, _st = _alp_sell(api, qty=2)
    (body,) = api.posts
    assert body["qty"] == "2" and body["position_intent"] == "sell_to_close"


def test_alpaca_every_order_carries_its_position_intent(monkeypatch):
    api = AlpacaAPI(monkeypatch, held=3)
    st = {"positions": {}}
    assert alpaca.execute(alpaca.client(ALP), ALP,
                          make_event(expiry=EXPIRY), st)
    assert alpaca.execute(alpaca.client(ALP), ALP, _exit_ev(), st)
    assert [(b["side"], b["position_intent"]) for b in api.posts] == [
        ("buy", "buy_to_open"), ("sell", "sell_to_close")]


@pytest.mark.parametrize("side,effect", [("sell", "open"), ("buy", "close")])
def test_alpaca_order_function_refuses_wrong_pairs(monkeypatch, side, effect):
    api = AlpacaAPI(monkeypatch, held=5)
    assert alpaca._order(ALP, OCC, side, 1, effect=effect) is None
    assert api.posts == [] and _reasons() == ["invalid"]


@pytest.mark.parametrize("qty", [0, -1, float("nan"), True])
def test_alpaca_order_function_refuses_bad_quantities(monkeypatch, qty):
    api = AlpacaAPI(monkeypatch, held=5)
    assert alpaca._order(ALP, OCC, "sell", qty, effect="close") is None
    assert alpaca._order(ALP, OCC, "buy", qty, effect="open") is None
    assert api.posts == []


def test_alpaca_order_without_an_effect_is_refused(monkeypatch):
    api = AlpacaAPI(monkeypatch, held=5)
    assert alpaca._order(ALP, OCC, "buy", 1) is None
    assert api.posts == []


def test_alpaca_no_position_to_close_answer_means_gone_not_unknown(monkeypatch):
    """Alpaca turns the sell_to_close down: the contracts aren't there. A
    clear answer — not an unconfirmed order to look up, and nothing to
    send again until a read shows the contracts held."""
    api = AlpacaAPI(monkeypatch, pos_status=503)   # holdings unreadable: sent
    api.reject = (403, {"code": 40310000, "message": "insufficient qty available "
                                                     "for order (requested: 2, "
                                                     "available: 0)"})
    changed, st = _alp_sell(api, qty=2)
    assert len(api.posts) == 1 and not changed
    assert order_guard.get(st, KEY) is None            # not "unconfirmed"
    (rep,) = broker_events.drain()
    assert rep["reason"] == "nothing_held" and rep["kind"] == "exit_failed"
    assert "doesn't hold" in rep["message"] and "no short position" in rep["message"]
    api.pos_status = 200                               # readable now: holds none
    changed, st = _alp_sell(api, qty=2)
    assert len(api.posts) == 1 and not changed         # not sent again


# ── Robinhood ───────────────────────────────────────────────────────────────

def _mcp(payload, is_error=False):
    return {"content": [{"type": "text", "text": json.dumps(payload)}],
            "isError": is_error}


class RH:
    """Robinhood's MCP tools for one account."""

    def __init__(self, held=0, pending=0, short=False, positions_ok=True):
        self.held, self.pending, self.short = held, pending, short
        self.positions_ok, self.calls, self.last_error = positions_ok, [], None

    def placed(self):
        return [a for n, a in self.calls if n == "place_option_order"]

    def call_tool(self, name, args):
        self.calls.append((name, args))
        if name == "get_option_positions":
            if not self.positions_ok:
                return _mcp({"error": "down"}, is_error=True)
            rows = []
            if self.held:
                rows.append({"option_id": RH_ID, "quantity": f"{abs(self.held)}.0000",
                             "pending_sell_quantity": f"{self.pending}.0000",
                             "type": "short" if self.short else "long"})
            return _mcp({"data": {"positions": rows}})
        if name == "place_option_order":
            return _mcp({"data": {"id": f"o-{len(self.placed())}"}})
        if name == "get_option_orders":
            return _mcp({"data": {"orders": [
                {"id": f"o-{len(self.placed())}", "state": "filled",
                 "processed_quantity": self.placed()[-1]["quantity"]
                 if self.placed() else "0"}]}})
        return {"content": []}


RH_CFG = {"robinhood_account": "A1"}


def _rh_sell(rh, qty=3):
    st = {"positions": {KEY: {"option_id": RH_ID, "qty": qty}}}
    return robinhood.execute(rh, RH_CFG, _exit_ev(), st), st


def test_robinhood_sell_with_nothing_held_sends_no_order():
    rh = RH(held=0)
    changed, st = _rh_sell(rh)
    assert rh.placed() == [] and not changed and st["positions"][KEY]["qty"] == 3
    assert _reasons() == ["nothing_held"]


def test_robinhood_sells_only_what_the_account_holds():
    rh = RH(held=1)
    changed, st = _rh_sell(rh, qty=3)
    (args,) = rh.placed()
    assert args["quantity"] == "1"
    assert args["legs"][0]["side"] == "sell"
    assert args["legs"][0]["position_effect"] == "close"
    assert changed and st["positions"][KEY]["qty"] == 2


def test_robinhood_no_second_sell_while_one_works_on_the_whole_holding():
    rh = RH(held=2, pending=2)
    changed, _st = _rh_sell(rh, qty=2)
    assert rh.placed() == [] and not changed


def test_robinhood_short_position_is_never_traded():
    rh = RH(held=1, short=True)
    changed, _st = _rh_sell(rh)
    assert rh.placed() == [] and not changed
    assert _reasons() == ["short_seen"]


def test_robinhood_unreadable_holdings_still_sell_to_close():
    rh = RH(positions_ok=False)
    changed, _st = _rh_sell(rh, qty=2)
    (args,) = rh.placed()
    assert args["quantity"] == "2" and args["legs"][0]["position_effect"] == "close"


@pytest.mark.parametrize("side,effect", [("sell", "open"), ("buy", "close")])
def test_robinhood_place_refuses_wrong_pairs(side, effect):
    rh = RH(held=5)
    assert robinhood.place(rh, RH_CFG, RH_ID, side, effect, 1) is None
    assert rh.placed() == [] and _reasons() == ["invalid"]


@pytest.mark.parametrize("qty", [0, -1, float("nan"), True])
def test_robinhood_place_refuses_bad_quantities(qty):
    rh = RH(held=5)
    assert robinhood.place(rh, RH_CFG, RH_ID, "sell", "close", qty) is None
    assert robinhood.place(rh, RH_CFG, RH_ID, "buy", "open", qty) is None
    assert rh.placed() == []


def test_robinhood_replaced_sell_is_capped_at_what_is_held(monkeypatch):
    """reconcile() re-places an expiry-guard sell: the same check applies."""
    rh = RH(held=1)
    monkeypatch.setattr(robinhood, "_order_status", lambda rh, a, o: ("confirmed", 0))
    monkeypatch.setattr(robinhood, "_cancel_order", lambda rh, a, o: True)
    st = {"positions": {KEY: {"option_id": RH_ID, "qty": 3, "pending": {
        "order_id": "o-old", "side": "sell", "requested": 3, "filled": 0,
        "was_limit": True, "placed_at": time.time() - 60, "reprice": True}}}}
    monkeypatch.setattr(robinhood, "_before_close", lambda cfg=None: True)
    robinhood.reconcile(rh, RH_CFG, st, lambda s: None)
    (args,) = rh.placed()
    assert args["quantity"] == "1" and args["legs"][0]["position_effect"] == "close"
    assert st["positions"][KEY]["pending"]["requested"] == 1


@pytest.mark.parametrize("odd", [
    {"quantity": "2.0000", "pending_sell_quantity": "0.0000", "type": "long"},
    {"option_id": "0b6e1c1e-1111-4222-8333-444455556666", "type": "long"}])
def test_robinhood_position_row_it_cannot_identify_reads_as_unknown(odd):
    """A row with no contract id or no quantity could be this contract: the
    holdings are unknown, not "none" — the sell goes out as a close-only
    sell (Robinhood refuses it if nothing is held), not refused as gone."""
    class OddRH(RH):
        def call_tool(self, name, args):
            if name == "get_option_positions":
                self.calls.append((name, args))
                return _mcp({"data": {"positions": [odd]}})
            return super().call_tool(name, args)
    rh = OddRH()
    assert robinhood._sell_holding(rh, RH_CFG, RH_ID) == (None, None)
    changed, _st = _rh_sell(rh, qty=2)
    (args,) = rh.placed()
    assert args["quantity"] == "2" and args["legs"][0]["position_effect"] == "close"
    assert "nothing_held" not in _reasons()


class LostAnswerRH(RH):
    """The sell goes in, its answer is lost; the order list shows it."""

    def call_tool(self, name, args):
        if name == "place_option_order":
            self.calls.append((name, args))
            self.sent = dict(args, at=datetime.now(timezone.utc).isoformat())
            raise requests.ReadTimeout("answer lost")
        if name == "get_option_orders" and getattr(self, "sent", None):
            self.calls.append((name, args))
            s = self.sent
            return _mcp({"data": {"orders": [{
                "id": "o-lost", "ref_id": s["ref_id"], "state": "confirmed",
                "quantity": s["quantity"], "processed_quantity": "0",
                "created_at": s["at"], "type": s["type"], "legs": s["legs"]}]}})
        return super().call_tool(name, args)


def test_robinhood_adopted_sell_is_booked_at_the_capped_quantity(monkeypatch):
    """Ledger 3, account 1: the sell goes out for 1. Its answer is lost and
    the order is found — the pending sell is for 1, not the ledger's 3."""
    monkeypatch.setattr(robinhood, "_settle_fast", lambda *a, **kw: None)
    rh = LostAnswerRH(held=1)
    changed, st = _rh_sell(rh, qty=3)
    (args,) = rh.placed()
    assert args["quantity"] == "1" and changed
    pend = st["positions"][KEY]["pending"]
    assert pend["order_id"] == "o-lost" and pend["requested"] == 1


@pytest.mark.parametrize("args,ok", [
    ({"legs": [{"option_id": RH_ID, "side": "buy", "position_effect": "open"}],
      "quantity": "1"}, True),
    ({"legs": [{"option_id": RH_ID, "side": "sell", "position_effect": "close"}],
      "quantity": "2"}, True),
    ({"legs": [{"option_id": RH_ID, "side": "sell", "position_effect": "open"}],
      "quantity": "1"}, False),
    ({"legs": [{"option_id": RH_ID, "side": "buy", "position_effect": "close"}],
      "quantity": "1"}, False),
    ({"legs": [{"option_id": RH_ID, "side": "sell"}], "quantity": "1"}, False),
    ({"legs": [{"side": "buy", "position_effect": "open"},
               {"side": "sell", "position_effect": "open"}], "quantity": "1"}, False),
    ({"legs": [{"option_id": RH_ID, "side": "buy", "position_effect": "open"}],
      "quantity": "-1"}, False),
    ({"legs": [{"option_id": RH_ID, "side": "buy", "position_effect": "open"}],
      "quantity": "nan"}, False),
])
def test_mcp_client_backstop_only_sends_buy_to_open_and_sell_to_close(
        tmp_path, monkeypatch, args, ok):
    assert (order_tool_refusal("place_option_order", args) == "") is ok
    if ok:
        return
    sent = []
    monkeypatch.setattr(requests, "post", lambda *a, **kw: sent.append(a))
    client = RobinhoodMCP(str(tmp_path / "token.json"))
    result = client.call_tool("place_option_order", args)
    assert result["isError"] is True and sent == []


@pytest.mark.parametrize("name", ["place_equity_order", "place_multi_leg_order",
                                  "submit_option_order", "replace_option_order"])
def test_mcp_client_never_sends_any_other_order_tool(name):
    assert order_tool_refusal(name, {}) != ""
    assert order_tool_refusal("get_option_orders", {}) == ""
    assert order_tool_refusal("cancel_option_order", {}) == ""


# ── moomoo ──────────────────────────────────────────────────────────────────

class _DF:
    def __init__(self, rows):
        self._rows = rows

    def __len__(self):
        return len(self._rows)

    @property
    def iloc(self):
        return self._rows


class MM:
    """The moomoo SDK + OpenD for one account."""
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

    class ModifyOrderOp:
        CANCEL = "CANCEL"

    def __init__(self, held=0, can_sell=None, working=0, short=False,
                 pos_ok=True, ord_ok=True):
        self.held, self.can_sell, self.working, self.short = held, can_sell, working, short
        self.pos_ok, self.ord_ok, self.placed = pos_ok, ord_ok, []

    def OpenSecTradeContext(self, **kw):
        sdk = self

        class Ctx:
            def position_list_query(self, **kw):
                if not sdk.pos_ok:
                    return -1, "disconnected"
                if not sdk.held:
                    return sdk.RET_OK, _DF([])
                row = {"code": CODE, "qty": float(sdk.held),
                       "position_side": "SHORT" if sdk.short else "LONG"}
                if sdk.can_sell is not None:
                    row["can_sell_qty"] = float(sdk.can_sell)
                return sdk.RET_OK, _DF([row])

            def order_list_query(self, **kw):
                if kw.get("order_id"):
                    return sdk.RET_OK, _DF([{"order_id": kw["order_id"],
                                             "order_status": "FILLED_ALL",
                                             "dealt_qty": sdk.placed[-1]["qty"]}])
                if not sdk.ord_ok:
                    return -1, "disconnected"
                rows = []
                if sdk.working:
                    rows.append({"code": CODE, "order_id": "owner-1",
                                 "order_status": "SUBMITTED", "trd_side": "TrdSide.SELL",
                                 "qty": float(sdk.working), "dealt_qty": 0.0})
                return sdk.RET_OK, _DF(rows)

            def place_order(self, **kw):
                sdk.placed.append(kw)
                return sdk.RET_OK, _DF([{"order_id": f"mm-{len(sdk.placed)}"}])

            def close(self):
                pass
        return Ctx()

    def OpenQuoteContext(self, **kw):
        sdk = self

        class Q:
            def get_market_snapshot(self, codes):
                return sdk.RET_OK, _DF([{"bid_price": 1.0, "ask_price": 1.2,
                                         "last_price": 1.1}])

            def close(self):
                pass
        return Q()


MM_CFG = {"broker": "moomoo", "moomoo_paper": True, "contracts_per_trade": 3}


def _mm_sell(monkeypatch, sdk, qty=3, ev=None):
    monkeypatch.setattr(moomoo, "_sdk", lambda: sdk)
    st = {"positions": {KEY: {"option_id": CODE, "qty": qty}}}
    return moomoo.execute(None, MM_CFG, ev or _exit_ev(), st), st


def test_moomoo_sell_with_nothing_held_sends_no_order(monkeypatch):
    sdk = MM(held=0)
    changed, st = _mm_sell(monkeypatch, sdk)
    assert sdk.placed == [] and not changed and st["positions"][KEY]["qty"] == 3
    assert _reasons() == ["nothing_held"]


def test_moomoo_sells_only_what_the_account_holds(monkeypatch):
    sdk = MM(held=1)
    changed, st = _mm_sell(monkeypatch, sdk, qty=3)
    (order,) = sdk.placed
    assert order["qty"] == 1 and order["trd_side"] == "SELL"
    assert changed and st["positions"][KEY]["qty"] == 2


@pytest.mark.parametrize("over", [{"working": 2}, {"can_sell": 0}])
def test_moomoo_no_second_sell_while_one_works_on_the_whole_holding(monkeypatch, over):
    sdk = MM(held=2, **over)
    changed, _st = _mm_sell(monkeypatch, sdk, qty=2)
    assert sdk.placed == [] and not changed


@pytest.mark.parametrize("can_sell", [3, 5, None])
def test_moomoo_working_sell_is_subtracted_once(monkeypatch, can_sell):
    """moomoo documents can_sell_qty as "holding quantity - frozen
    quantity", and a working sell freezes what it sells. Holding 5 with the
    owner's 2-lot sell working leaves 3 to sell — whether can_sell_qty
    already excludes the working sell (3), doesn't (5), or is missing."""
    sdk = MM(held=5, can_sell=can_sell, working=2)
    changed, st = _mm_sell(monkeypatch, sdk, qty=5)
    (order,) = sdk.placed
    assert order["qty"] == 3
    assert changed and st["positions"][KEY]["qty"] == 2


def test_moomoo_short_position_is_never_traded(monkeypatch):
    sdk = MM(held=2, short=True)
    changed, _st = _mm_sell(monkeypatch, sdk)
    assert sdk.placed == [] and _reasons() == ["short_seen"]


@pytest.mark.parametrize("over", [{"pos_ok": False}, {"ord_ok": False}])
def test_moomoo_holdings_unreadable_holds_the_sell_back_until_readable(
        monkeypatch, over):
    """moomoo can't be told to only close: with the holdings unreadable a
    sell could open a short, so it is NOT sent — the owner is told — and it
    goes out once the account can be read again."""
    sdk = MM(held=2, **over)
    changed, st = _mm_sell(monkeypatch, sdk, qty=2)
    assert sdk.placed == [] and not changed
    (rep,) = broker_events.drain()
    assert rep["reason"] == "holdings_unknown" and rep["kind"] == "exit_failed"
    assert "couldn't confirm what your moomoo account holds" in rep["message"]
    assert "held back" in rep["message"] and "EXPIRES TODAY" not in rep["message"]
    sdk.pos_ok = sdk.ord_ok = True
    assert moomoo.execute(None, MM_CFG, _exit_ev(), st)
    assert [o["qty"] for o in sdk.placed] == [2] and st["positions"] == {}


class LimitedMM(MM):
    """OpenD answering the refresh_cache queries with a frequency-limit
    error the first `limited` times (10 per 30 s per account)."""

    def __init__(self, limited, said="request too frequent, please try again later",
                 **kw):
        super().__init__(**kw)
        self.limited, self.said, self.asked, self.ctxs = limited, said, [], []

    def OpenSecTradeContext(self, **kw):
        ctx, sdk = super().OpenSecTradeContext(**kw), self
        self.ctxs.append(ctx)
        real_pos, real_ord, real_place = (ctx.position_list_query,
                                          ctx.order_list_query, ctx.place_order)

        def limited(name, real):
            def call(**q):
                sdk.asked.append((name, ctx))
                if sdk.limited > 0 and not q.get("order_id"):
                    sdk.limited -= 1
                    return -1, sdk.said
                return real(**q)
            return call
        ctx.position_list_query = limited("pos", real_pos)
        ctx.order_list_query = limited("ord", real_ord)
        ctx.place_order = limited("place", real_place)
        return ctx


@pytest.mark.parametrize("said", ["request too frequent, please try again later",
                                  "Frequency limit exceeded",
                                  "请求频率太高，请稍后再试"])
def test_moomoo_frequency_limit_waits_and_sells_instead_of_holding_back(
        monkeypatch, said):
    """A frequency-limit answer to the holdings read is not "unknown": the
    sell waits for the limit's window and goes out in the same loop."""
    slept = []
    monkeypatch.setattr(moomoo.time, "sleep", slept.append)
    sdk = LimitedMM(limited=2, said=said, held=3)
    changed, st = _mm_sell(monkeypatch, sdk, qty=3)
    assert [o["qty"] for o in sdk.placed] == [3] and changed
    assert len(slept) >= 2 and _reasons() == []


def test_moomoo_frequency_limit_that_never_lifts_holds_the_sell_back(monkeypatch):
    slept = []
    monkeypatch.setattr(moomoo.time, "sleep", slept.append)
    sdk = LimitedMM(limited=10 ** 6, held=3)
    changed, _st = _mm_sell(monkeypatch, sdk, qty=3)
    assert sdk.placed == [] and not changed
    assert _reasons() == ["holdings_unknown"]
    assert 30 <= sum(slept) <= 60  # waited out one 30 s window, no longer


def test_moomoo_other_read_errors_hold_back_at_once(monkeypatch):
    slept = []
    monkeypatch.setattr(moomoo.time, "sleep", slept.append)
    sdk = LimitedMM(limited=1, said="disconnected", held=3)
    changed, _st = _mm_sell(monkeypatch, sdk, qty=3)
    assert sdk.placed == [] and slept == [] and _reasons() == ["holdings_unknown"]


def test_moomoo_holdings_are_read_in_the_order_s_own_context(monkeypatch):
    """The read and the order go through one trade context, back to back:
    the window in which the owner could sell first is as short as it gets."""
    sdk = LimitedMM(limited=0, held=3)
    _mm_sell(monkeypatch, sdk, qty=3)
    sell = [(n, c) for n, c in sdk.asked][:3]
    assert [n for n, _c in sell] == ["pos", "ord", "place"]
    assert len({id(c) for _n, c in sell}) == 1


def test_moomoo_held_back_sell_says_so_louder_on_expiry_day(monkeypatch):
    today = datetime.now(robinhood.MARKET_TZ).date().isoformat()
    key = f"SPY|{today}|752.0|C"
    sdk = MM(pos_ok=False)
    monkeypatch.setattr(moomoo, "_sdk", lambda: sdk)
    st = {"positions": {key: {"option_id": CODE, "qty": 1}}}
    assert not moomoo.execute(None, MM_CFG, make_event(event="EXITED", expiry=today), st)
    (rep,) = broker_events.drain()
    assert "EXPIRES TODAY" in rep["message"] and "before the close" in rep["message"]


@pytest.mark.parametrize("side,effect", [("sell", "open"), ("buy", "close")])
def test_moomoo_order_function_refuses_wrong_pairs(monkeypatch, side, effect):
    sdk = MM(held=5)
    assert moomoo._order(MM_CFG, sdk, CODE, side, 1, 1.0, effect=effect) is None
    assert sdk.placed == []


@pytest.mark.parametrize("qty", [0, -1, float("nan"), True])
def test_moomoo_order_function_refuses_bad_quantities(monkeypatch, qty):
    sdk = MM(held=5)
    assert moomoo._order(MM_CFG, sdk, CODE, "sell", qty, 1.0, effect="close") is None
    assert moomoo._order(MM_CFG, sdk, CODE, "buy", qty, 1.0, effect="open") is None
    assert sdk.placed == []


# ── the agent: a short at the broker is a fault, never traded ──────────────

class ShortBroker:
    """An adapter that reads a short position in the ledger's contract."""

    def __init__(self, short=2):
        self.short, self.executed = short, []

    def held_option_qty(self, client, cfg, ids):
        return {i: 0 for i in ids}

    def short_option_qty(self, client, cfg, ids):
        return {i: self.short if i == OCC else 0 for i in ids}

    def execute(self, client, cfg, ev, state):
        self.executed.append(ev)
        return False


def test_a_short_found_by_the_holdings_check_pauses_entries_and_alerts(
        home, monkeypatch):
    sent = []
    monkeypatch.setattr(agent, "notify", lambda cfg, msg, **kw: sent.append((msg, kw)))
    monkeypatch.setattr(agent, "_market_open_now", lambda: False)
    monkeypatch.setitem(agent._POS_SYNC, "next_at", 0.0)
    b = ShortBroker()
    st = {"positions": {KEY: {"option_id": OCC, "qty": 1}}}
    cfg = {"broker": "alpaca"}
    agent._position_upkeep(cfg, st, lambda s: None, b, object())
    assert b.executed == []                                # never traded
    assert json.loads((home / "config.json").read_text())["paused"] is True
    assert cfg["paused"] is True
    ((msg, kw),) = sent
    assert "SHORT 2x" in msg and kw.get("priority") == "high"
    assert "won't" in msg and "close it yourself" in msg
    monkeypatch.setitem(agent._POS_SYNC, "next_at", 0.0)
    agent._position_upkeep(cfg, st, lambda s: None, b, object())
    assert len(sent) == 1                                  # told once
    b.short = 0                                            # the owner closed it
    monkeypatch.setitem(agent._POS_SYNC, "next_at", 0.0)
    agent._position_upkeep(cfg, st, lambda s: None, b, object())
    assert "short_faults" not in st
    # resuming is the owner's call: the pause stays
    assert json.loads((home / "config.json").read_text())["paused"] is True


def test_a_short_seen_before_a_sell_is_reported_and_entries_pause(
        home, no_policy, monkeypatch):
    sent = []
    monkeypatch.setattr(agent, "notify", lambda cfg, msg, **kw: sent.append((msg, kw)))
    monkeypatch.setattr(agent, "_problems_logged", set())
    api = AlpacaAPI(monkeypatch, held=-1, side="short")
    st = {"positions": {KEY: {"option_id": OCC, "qty": 1}}}
    cfg = dict(ALP, position_sync=False)
    with pytest.raises(agent.ExitNotDone):
        agent.handle_event(_exit_ev(), cfg, st, alpaca, alpaca.client(cfg),
                           lambda s: None)
    assert api.posts == []
    assert json.loads((home / "config.json").read_text())["paused"] is True
    assert any("SHORT 1x" in m and kw.get("priority") == "high" for m, kw in sent)
    out = agent.handle_event(make_event(ticker="QQQ", expiry=EXPIRY, event_id="e-9"),
                             cfg, st, alpaca, alpaca.client(cfg), lambda s: None)
    assert "paused" in out and api.posts == []            # no new entries


def test_practice_sell_of_an_empty_position_is_refused(home):
    st = {"positions": {KEY: {"option_id": "dry", "qty": 0, "dry": True}}}
    assert agent._execute_dry({}, _exit_ev(), st) is None
    assert KEY not in st["positions"]
    st = {"positions": {KEY: {"option_id": "dry", "qty": 2, "dry": True}}}
    assert agent._execute_dry({}, _exit_ev(), st) == "exit"
    assert agent._execute_dry({}, _exit_ev(), st) is None  # nothing held: nothing


# ── sources: a signal can't ask for a short ─────────────────────────────────

def test_sources_drop_signals_that_would_mean_a_short(home, monkeypatch, capsys):
    import sources
    from sources import agenthc_day_trade_ideas, generic_json_url, manual_file
    monkeypatch.setattr(sources, "_LOGGED", set())
    good = {"event": "ENTERED", "ticker": "SPY", "expiry": EXPIRY, "strike": 752,
            "type": "C", "event_id": "g"}
    bad = [dict(good, event="SHORTED", event_id="b1"),
           dict(good, event="SELL_TO_OPEN", event_id="b2"),
           dict(good, side="sell", event_id="b3"),
           dict(good, position_intent="sell_to_open", event_id="b4"),
           dict(good, qty=-2, event_id="b5"),
           dict(good, quantity="-1", event_id="b6"),
           dict(good, action="write", event_id="b7")]
    exit_ok = dict(good, event="EXITED", side="sell", event_id="x")
    body = {"events": [good, exit_ok] + bad}

    evs = agenthc_day_trade_ideas._parse_events(body)
    assert sorted(e["event"] for e in evs) == ["ENTERED", "EXITED"]

    class R:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return body
    monkeypatch.setattr(generic_json_url.requests, "get", lambda *a, **kw: R())
    monkeypatch.setattr(generic_json_url, "_SIGHT", {})
    evs = generic_json_url.poll({"source_url": "https://feed.invalid/x"}, {})
    assert sorted(e["event_id"] for e in evs) == ["g", "x"]

    lines = [{"action": "enter", "ticker": "SPY", "expiry": EXPIRY, "strike": 752,
              "type": "C"},
             {"action": "exit", "ticker": "SPY", "expiry": EXPIRY, "strike": 752,
              "type": "C"},
             {"action": "sell", "ticker": "SPY", "expiry": EXPIRY, "strike": 752,
              "type": "C"},
             {"action": "short", "ticker": "SPY", "expiry": EXPIRY, "strike": 752,
              "type": "C"},
             {"action": "enter", "side": "sell", "ticker": "SPY", "expiry": EXPIRY,
              "strike": 752, "type": "C"},
             {"action": "enter", "qty": -1, "ticker": "QQQ", "expiry": EXPIRY,
              "strike": 500, "type": "P"}]
    (home / "commands.jsonl").write_text("\n".join(json.dumps(x) for x in lines))
    evs = manual_file.poll({}, {})
    assert [e["event"] for e in evs] == ["ENTERED", "EXITED"]
    out = capsys.readouterr().out
    assert out.count("ignored a signal") == len(bad) * 2 + 4
    assert "never" in out

    # the next poll sees the same lines: each was logged once already
    agenthc_day_trade_ideas._parse_events(body)
    generic_json_url.poll({"source_url": "https://feed.invalid/x"}, {})
    manual_file.poll({}, {})
    assert "ignored a signal" not in capsys.readouterr().out


def test_sources_keep_an_entry_a_feed_labels_short(home):
    """A third-party feed may label a bearish idea (a put to BUY) "short".
    The entry is still a buy to open — only explicit sell-to-open words
    (sell, sell_to_open, sto, write…) drop an entry."""
    from sources import agenthc_day_trade_ideas, never_short_reason
    ev = {"event": "ENTERED", "ticker": "SPY", "expiry": EXPIRY, "strike": 700,
          "type": "P", "event_id": "bear", "side": "short"}
    assert never_short_reason(ev, "ENTERED") == ""
    (kept,) = agenthc_day_trade_ideas._parse_events({"events": [ev]})
    assert kept["event"] == "ENTERED" and kept["type"] == "P"
    for word in ("sell", "sell_to_open", "STO", "write", "sell_short"):
        assert never_short_reason(dict(ev, side=word), "ENTERED")


# ── no order leaves by any other road ──────────────────────────────────────

# (module, the one function allowed to send orders, what sending looks like)
_SENDERS = [
    ("brokers/alpaca.py", "_order",
     lambda call, src: _is_attr(call, "post") and "/v2/orders" in src),
    ("brokers/moomoo.py", "_order",
     lambda call, src: _is_attr(call, "place_order")),
    ("brokers/robinhood.py", "place",
     lambda call, src: "place_option_order" in src),
]


def _is_attr(call, name):
    f = call.func
    return (isinstance(f, ast.Attribute) and f.attr == name) or (
        isinstance(f, ast.Name) and f.id == name)


def _calls_by_function(path):
    src = open(os.path.join(ROOT, path), encoding="utf-8").read()
    tree = ast.parse(src)
    out = []

    def walk(node, fn):
        for child in ast.iter_child_nodes(node):
            name = child.name if isinstance(
                child, (ast.FunctionDef, ast.AsyncFunctionDef)) else fn
            if isinstance(child, ast.Call):
                out.append((fn, child, ast.get_source_segment(src, child) or ""))
            walk(child, name)
    walk(tree, None)
    return out


@pytest.mark.parametrize("path,allowed,sends", _SENDERS)
def test_orders_are_sent_only_from_the_guarded_function(path, allowed, sends):
    hits = [fn for fn, call, src in _calls_by_function(path) if sends(call, src)]
    assert hits and set(hits) == {allowed}, hits
    # ...and that function runs the check before anything is sent
    body = [(fn, src) for fn, _c, src in _calls_by_function(path) if fn == allowed]
    assert any("order_guard.check_order(" in s for _f, s in body)


def test_no_other_module_sends_broker_orders():
    """No order endpoint, SDK order call or order tool anywhere else."""
    patterns = ("/v2/orders\"", "place_order(", "\"place_option_order\"")
    allowed = {"brokers/alpaca.py", "brokers/moomoo.py", "brokers/robinhood.py",
               "brokers/robinhood_mcp.py",
               "self_edit.py"}  # names the patterns to refuse them
    offenders = []
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in (".git", "tests", "node_modules",
                                                "webui_dist", "webui_app", ".venv")]
        for f in files:
            if not f.endswith(".py"):
                continue
            rel = os.path.relpath(os.path.join(base, f), ROOT).replace(os.sep, "/")
            text = open(os.path.join(base, f), encoding="utf-8").read()
            if rel not in allowed and any(p in text for p in patterns):
                offenders.append(rel)
    assert offenders == []
    mcp = open(os.path.join(ROOT, "brokers/robinhood_mcp.py"), encoding="utf-8").read()
    assert "order_tool_refusal(name, arguments)" in mcp  # call_tool's backstop


# ── a random life: the account never goes short ────────────────────────────

CONTRACTS = [("SPY", 752.0, "C"), ("QQQ", 500.0, "P")]


class Account:
    """A broker account that — unlike a real one — lets any sell through,
    so a sell of more than is held shows up as a negative position."""

    def __init__(self):
        self.held, self.bad, self.orders = {}, [], []

    def order(self, sym, side, qty):
        qty = int(qty)
        if side == "sell" and qty > self.held.get(sym, 0):
            self.bad.append((sym, side, qty, self.held.get(sym, 0)))
        self.held[sym] = self.held.get(sym, 0) + (qty if side == "buy" else -qty)
        oid = f"ord-{len(self.orders) + 1}"
        self.orders.append({"id": oid, "sym": sym, "side": side, "qty": qty,
                            "at": datetime.now(timezone.utc).isoformat()
                            .replace("+00:00", "Z")})
        return oid

    def owner_sells(self, rng):
        held = [s for s, q in self.held.items() if q > 0]
        if held:
            sym = rng.choice(held)
            self.order(sym, "sell", rng.randint(1, self.held[sym]))
            self.orders[-1]["owner"] = True


class AlpacaFront:
    def __init__(self, acct, monkeypatch, rng):
        self.acct, self.rng, self.lose_next = acct, rng, False
        self.by_cid = {}
        monkeypatch.setattr(alpaca.requests, "post", self.post)
        monkeypatch.setattr(alpaca.requests, "get", self.get)
        monkeypatch.setattr(alpaca.requests, "delete", lambda url, **kw: _Resp(204, {}))

    @staticmethod
    def sym(ticker, strike, typ):
        return alpaca._occ_symbol({"ticker": ticker, "expiry": EXPIRY,
                                   "strike": strike, "type": typ})

    def post(self, url, **kw):
        body = kw["json"]
        cid = body.get("client_order_id")
        if cid in self.by_cid:
            return _Resp(422, {"message": "client_order_id must be unique"})
        oid = self.acct.order(body["symbol"], body["side"], body["qty"])
        row = {"id": oid, "client_order_id": cid, "symbol": body["symbol"],
               "side": body["side"], "qty": body["qty"], "filled_qty": body["qty"],
               "status": "filled", "submitted_at": self.acct.orders[-1]["at"]}
        self.by_cid[cid] = row
        if self.lose_next:
            self.lose_next = False
            raise requests.ReadTimeout("answer lost")
        return _Resp(200, row)

    def get(self, url, **kw):
        if url.endswith("/v2/positions"):
            return _Resp(200, [{"symbol": s, "qty": str(q), "qty_available": str(q),
                                "side": "long" if q > 0 else "short",
                                "asset_class": "us_option"}
                               for s, q in self.acct.held.items() if q])
        if url.endswith("/v2/orders:by_client_order_id"):
            row = self.by_cid.get(kw["params"]["client_order_id"])
            return _Resp(200, row) if row else _Resp(404, {"message": "not found"})
        if "/v2/orders/" in url:
            oid = url.rsplit("/", 1)[-1]
            row = next((r for r in self.by_cid.values() if r["id"] == oid), None)
            return _Resp(200, row) if row else _Resp(404, {})
        if "quotes/latest" in url:
            return _Resp(200, {"quotes": {kw["params"]["symbols"]: {"ap": 1.2, "bp": 1.0}}})
        return _Resp(200, [])


class MoomooFront(MM):
    def __init__(self, acct, monkeypatch, rng):
        super().__init__()
        self.acct, self.rng, self.lose_next = acct, rng, False
        monkeypatch.setattr(moomoo, "_sdk", lambda: self)

    @staticmethod
    def sym(ticker, strike, typ):
        return moomoo._moomoo_code({"ticker": ticker, "expiry": EXPIRY,
                                    "strike": strike, "type": typ})

    def OpenSecTradeContext(self, **kw):
        sdk = self

        class Ctx:
            def position_list_query(self, **kw):
                return sdk.RET_OK, _DF([
                    {"code": s, "qty": float(abs(q)), "can_sell_qty": float(max(q, 0)),
                     "position_side": "LONG" if q > 0 else "SHORT"}
                    for s, q in sdk.acct.held.items() if q])

            def order_list_query(self, **kw):
                if kw.get("order_id"):
                    o = next((o for o in sdk.acct.orders if o["id"] == kw["order_id"]),
                             None)
                    rows = [] if o is None else [{"order_id": o["id"], "code": o["sym"],
                                                  "order_status": "FILLED_ALL",
                                                  "dealt_qty": o["qty"]}]
                    return sdk.RET_OK, _DF(rows)
                return sdk.RET_OK, _DF([])

            def place_order(self, **kw):
                oid = sdk.acct.order(kw["code"], "buy" if kw["trd_side"] == "BUY"
                                     else "sell", kw["qty"])
                if sdk.lose_next:  # the order went in; the answer is lost
                    sdk.lose_next = False
                    raise ConnectionError("OpenD connection dropped")
                return sdk.RET_OK, _DF([{"order_id": oid}])

            def modify_order(self, *a, **kw):
                return sdk.RET_OK, None

            def close(self):
                pass
        return Ctx()


class RobinhoodFront:
    IDS = {c: f"00000000-0000-4000-8000-00000000000{i}" for i, c in enumerate(CONTRACTS)}

    def __init__(self, acct, monkeypatch, rng):
        self.acct, self.rng, self.lose_next = acct, rng, False
        self.last_error, self.rows = None, []

    @classmethod
    def sym(cls, ticker, strike, typ):
        return cls.IDS[(ticker, strike, typ)]

    def call_tool(self, name, args):
        refused = order_tool_refusal(name, args)  # the real client's backstop
        if refused:
            return _mcp({"detail": refused}, is_error=True)
        if name == "get_option_instruments":
            key = (args["chain_symbol"], float(args["strike_price"]),
                   "C" if args["type"] == "call" else "P")
            return _mcp({"data": {"results": [{"id": self.IDS[key],
                                               "strike_price": args["strike_price"]}]}})
        if name == "get_option_quotes":
            return _mcp({"data": {"results": [{"ask_price": "1.20",
                                               "bid_price": "1.00"}]}})
        if name == "get_option_positions":
            return _mcp({"data": {"positions": [
                {"option_id": s, "quantity": f"{abs(q)}.0000",
                 "pending_sell_quantity": "0.0000",
                 "type": "long" if q > 0 else "short"}
                for s, q in self.acct.held.items() if q]}})
        if name == "place_option_order":
            leg = args["legs"][0]
            oid = self.acct.order(leg["option_id"], leg["side"], args["quantity"])
            self.rows.insert(0, {"id": oid, "ref_id": args["ref_id"], "state": "filled",
                                 "quantity": args["quantity"],
                                 "processed_quantity": args["quantity"],
                                 "created_at": self.acct.orders[-1]["at"],
                                 "legs": [dict(leg)]})
            if self.lose_next:
                self.lose_next = False
                raise requests.ReadTimeout("answer lost")
            return _mcp({"data": {"id": oid}})
        if name == "get_option_orders":
            rows = self.rows
            if args.get("order_id"):
                rows = [r for r in rows if r["id"] == args["order_id"]]
            return _mcp({"data": {"orders": rows}})
        if name == "cancel_option_order":
            return _mcp({"data": {}})
        return {"content": []}


_FRONTS = {"alpaca": (AlpacaFront, alpaca, dict(ALP)),
           "moomoo": (MoomooFront, moomoo, dict(MM_CFG)),
           "robinhood": (RobinhoodFront, robinhood,
                         {"broker": "robinhood", "robinhood_account": "A1",
                          "contracts_per_trade": 3})}


@pytest.mark.parametrize("name", sorted(_FRONTS))
@pytest.mark.parametrize("seed", range(6))
def test_random_life_never_opens_a_short(home, no_policy, monkeypatch, name, seed):
    """Entries, exits, repeated signals, lost answers, restarts from the
    saved state file, the owner selling by hand and a ledger that says more
    than the account holds — in random order. At every step, for every
    contract, the account is never short and no request that would make it
    short ever reached the broker. (position_sync is off: the adapters'
    own check is all that stands between a stale ledger and a short.)"""
    monkeypatch.setattr(agent, "notify", lambda *a, **kw: None)
    monkeypatch.setattr(agent, "_problems_logged", set())
    rng = random.Random(seed)
    acct = Account()
    front_cls, mod, cfg = _FRONTS[name]
    cfg.update(position_sync=False, max_entries_per_day=1000, max_open_positions=50)
    front = front_cls(acct, monkeypatch, rng)
    client = front if name == "robinhood" else mod.client(cfg)
    state = {"positions": {}}
    sent = []
    n = 0
    for _step in range(70):
        roll = rng.random()
        c = rng.choice(CONTRACTS)
        n += 1
        if roll < 0.08:                                   # restart from disk
            state = json.loads(json.dumps(state))
        elif roll < 0.18:                                 # the owner sells
            acct.owner_sells(rng)
        elif roll < 0.26:                                 # the ledger says more
            row = state["positions"].get(f"{c[0]}|{EXPIRY}|{c[1]}|{c[2]}")
            if row and not row.get("pending"):
                row["qty"] = int(row.get("qty") or 0) + rng.randint(1, 3)
        else:
            if roll < 0.40:
                front.lose_next = True                    # lose the next answer
            kind = "ENTERED" if roll < 0.62 else "EXITED"
            if sent and rng.random() < 0.25:
                ev = dict(rng.choice(sent))               # a signal sent again
            else:
                ev = make_event(event=kind, ticker=c[0], strike=c[1], type=c[2],
                                expiry=EXPIRY, event_id=f"{name}-{seed}-{n}")
                sent.append(ev)
            if hasattr(mod, "reconcile"):
                try:
                    mod.reconcile(client, cfg, state, lambda s: None)
                except Exception:
                    pass
                agent._log_adopted_buys(cfg, state, lambda s: None, mod, client)
            try:
                agent.handle_event(dict(ev), cfg, state, mod, client, lambda s: None)
            except Exception:
                pass                                       # retried, like the loop
        assert acct.bad == [], acct.bad
        assert all(q >= 0 for q in acct.held.values()), acct.held
    assert any(o["side"] == "sell" and not o.get("owner") for o in acct.orders), \
        "the scenario never sold: it tested nothing"


# ── self-edit: a broker file the agent writes can't go around the check ────

_SNEAKY = ("import requests\n\ndef execute(cl, cfg, ev, state):\n"
           "    requests.post('https://broker.invalid/v1/orders', json={'side': 'sell'})\n")
_GUARDED = ("from . import order_guard\n\ndef _order(cfg, side, effect, qty, held):\n"
            "    qty = order_guard.check_order(side, effect, qty, held)\n"
            "    return requests.post('https://broker.invalid/v1/orders', json={})\n")


@pytest.mark.parametrize("path,content,refused", [
    ("brokers/tasty.py", _SNEAKY, True),
    ("brokers/__init__.py", "x.place_order(side='SELL')\n", True),
    ("brokers/tasty.py", _GUARDED, False),
    ("brokers/tasty.py", "NAME = 'tasty'\n", False),
    ("sources/rss.py", _SNEAKY, False),            # not a broker file
])
def test_self_edit_refuses_broker_code_that_skips_the_order_check(
        tmp_path, monkeypatch, path, content, refused):
    import self_edit
    assert bool(self_edit._order_path_problem(path, content)) is refused
    repo, home = tmp_path / "repo", tmp_path / "home"
    (repo / "brokers").mkdir(parents=True)
    home.mkdir()
    monkeypatch.setenv("AGENT_HOME", str(home))
    monkeypatch.setattr(self_edit, "_repo", lambda: str(repo))
    with open(home / "proposal.json", "w", encoding="utf-8") as f:
        json.dump({"request": "t", "explanation": "", "warnings": [],
                   "changes": [{"path": path, "content": content, "diff": ""}]}, f)
    if refused:
        ok, msg = self_edit.apply_and_restart(restart=False)
        assert not ok and "order_guard.check_order" in msg
        assert not (repo / path).exists()
