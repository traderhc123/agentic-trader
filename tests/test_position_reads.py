"""Broker read hooks used to notice trades the owner closed in the broker app:
held_option_qty / closed_fills per adapter, plus account_tag. Shapes follow
the live Robinhood MCP captures (data.positions / data.orders, decimal
strings, price "" on market orders). Every unclear answer must read as
unknown (None or an omitted id), never as "not held". Zero network."""

import json
import time

import pytest

import broker_events
from brokers import account_tag, alpaca, moomoo, robinhood
from conftest import make_event

U1 = "14a8e49b-0f10-4d64-9cf9-bd1a68e9da44"
U2 = "7f9e2c1a-3b4d-4e5f-8a9b-0c1d2e3f4a5b"
U3 = "0b6c7d8e-9f01-4a2b-8c3d-4e5f6a7b8c9d"
U4 = "5d4c3b2a-1f0e-4d9c-8b7a-6f5e4d3c2b1a"
U5 = "9a8b7c6d-5e4f-4a3b-9c2d-1e0f9a8b7c6d"
POSID = "e3a1d2c4-0000-4b1b-9c9c-123456789abc"
CFG = {"robinhood_account": "A1"}


class FakeRH:
    """Scripted MCP client. Each call takes the next step: a result dict,
    None, an exception (raised), or a callable(rh, name, args) -> result
    (e.g. to set last_error mid-call, like a dropped login)."""

    def __init__(self, *script):
        self.script = list(script)
        self.calls = []
        self.last_error = None

    def call_tool(self, name, args):
        self.calls.append((name, dict(args)))
        step = self.script.pop(0) if self.script else {"content": []}
        if isinstance(step, Exception):
            raise step
        if callable(step):
            return step(self, name, args)
        return step


def _mcp(payload, is_error=False):
    return {"content": [{"type": "text", "text": json.dumps(payload)}],
            "isError": is_error}


def _err(detail="API error 400"):
    return _mcp({"detail": detail}, is_error=True)


def _pos(oid, qty="1.0000", **kw):
    row = {"option_id": oid, "chain_id": "c", "chain_symbol": "SPY",
           "type": "long", "quantity": qty, "average_price": "190.0000",
           "trade_value_multiplier": "100.0000",
           "pending_buy_quantity": "0.0000", "pending_sell_quantity": "0.0000",
           "pending_exercise_quantity": "0.0000",
           "pending_assignment_quantity": "0.0000",
           "pending_expiration_quantity": "0.0000"}
    row.update(kw)
    return row


def _positions(*rows, nxt=None):
    data = {"positions": list(rows)}
    if nxt is not None:
        data["next"] = nxt
    return _mcp({"data": data, "guide": "positions"})


def _down(rh, name, args):
    rh.last_error = "can't reach Robinhood (ConnectionError)"
    return None


# ── account_tag ─────────────────────────────────────────────────────────────

def test_account_tag():
    a = account_tag({"broker": "robinhood", "robinhood_account": "5PY12345"})
    b = account_tag({"broker": "robinhood", "robinhood_account": "5PY99999"})
    assert a != b and a.startswith("robinhood:") and "5PY12345" not in a
    assert account_tag({"robinhood_account": "5PY12345"}) == a  # default broker
    assert account_tag({"broker": "alpaca"}) == "alpaca:paper"
    assert account_tag({"broker": "alpaca", "alpaca_paper": False}) == "alpaca:live"
    assert (account_tag({"broker": "alpaca", "alpaca_key_id": "k1"})
            == account_tag({"broker": "alpaca", "alpaca_key_id": "k2"}))
    assert account_tag({"broker": "moomoo"}) == "moomoo:paper"
    assert account_tag({"broker": "moomoo", "moomoo_paper": False}) == "moomoo:real"
    assert (account_tag({"broker": "moomoo", "moomoo_host": "10.0.0.2"})
            == account_tag({"broker": "moomoo", "moomoo_host": "127.0.0.1"}))
    assert account_tag(None) == account_tag({})


# ── robinhood: held_option_qty ──────────────────────────────────────────────

def test_rh_positions_live_shape():
    rh = FakeRH(_positions(_pos(U1, "2.0000")))
    assert robinhood.held_option_qty(rh, CFG, [U1, U2]) == {U1: 2, U2: 0}
    (name, args), = rh.calls
    assert name == "get_option_positions"
    assert args == {"account_number": "A1", "nonzero": True}
    assert args["nonzero"] is True and "option_ids" not in args


def test_rh_positions_empty_list_is_explicit_zero():
    rh = FakeRH(_positions())
    assert robinhood.held_option_qty(rh, CFG, [U1, U2]) == {U1: 0, U2: 0}


@pytest.mark.parametrize("script", [
    [_down],                                               # login/network down
    [_err(), _err()],                                      # error, retry error
    [{"content": [{"type": "text", "text": "not json"}]}],
    [_mcp([1, 2])],                                        # body is a list
    [_mcp({"data": {"positions": None}})],                 # null is unknown
    [_mcp({"data": {"positions": {}}})],
    [_positions("not-a-row")],
    [_positions({"id": U1, "quantity": "1.0000"})],        # id only
    [_positions({"option_id": U1, "type": "long"})],       # no quantity
    [_positions(_pos(U1, "abc"))],                         # junk on our id
    [RuntimeError("boom")],
], ids=["down", "iserror", "non-json", "list", "null", "dict", "row",
        "id-only", "no-qty", "junk-qty", "raises"])
def test_rh_positions_unknown_cases_return_none(script):
    rh = FakeRH(*script)
    assert robinhood.held_option_qty(rh, CFG, [U1, U2]) is None


def test_rh_positions_without_account_makes_no_call():
    rh = FakeRH(_positions())
    assert robinhood.held_option_qty(rh, {}, [U1]) is None
    assert rh.calls == []


def test_rh_positions_last_error_before_call_makes_no_call():
    rh = FakeRH(_positions())
    rh.last_error = "login expired"
    assert robinhood.held_option_qty(rh, CFG, [U1]) is None
    assert rh.calls == []


def test_rh_positions_rest_row_keyed_by_option_url_not_id():
    row = {"id": POSID, "option": f"https://api.robinhood.com/options/instruments/{U1}/",
           "type": "long", "quantity": "1.0000"}
    rh = FakeRH(_positions(row))
    assert robinhood.held_option_qty(rh, CFG, [U1, POSID]) == {U1: 1, POSID: 0}


def test_rh_positions_short_ignored_duplicates_summed_null_items_skipped():
    rh = FakeRH(_positions(_pos(U1, "1.0000"), None, _pos(U1, "2.0000"),
                           _pos(U2, "3.0000", type="short"),
                           _pos(U3, "junk")))  # junk on an id we didn't ask for
    assert robinhood.held_option_qty(rh, CFG, [U1, U2]) == {U1: 3, U2: 0}


def test_rh_positions_pending_activity_makes_id_unknown():
    rh = FakeRH(_positions(
        _pos(U1, "2.0000", pending_sell_quantity="1.0000"),
        _pos(U2, "1.0000", pending_expiration_quantity="1.0000"),
        _pos(U3, "1.0000", pending_assignment_quantity="??"),
        _pos(U4, "1.0000")))
    assert robinhood.held_option_qty(rh, CFG, [U1, U2, U3, U4, U5]) == {U4: 1, U5: 0}


def test_rh_positions_pagination():
    rh = FakeRH(_positions(_pos(U1), nxt="cur-2"), _positions(_pos(U2, "2.0000")))
    assert robinhood.held_option_qty(rh, CFG, [U1, U2]) == {U1: 1, U2: 2}
    assert rh.calls[1][1] == {"account_number": "A1", "nonzero": True,
                              "cursor": "cur-2"}
    # more pages than we read: unknown, never a partial answer
    rh = FakeRH(*[_positions(_pos(U1), nxt=f"c{i}") for i in range(6)])
    assert robinhood.held_option_qty(rh, CFG, [U1]) is None
    assert len(rh.calls) == 5
    rh = FakeRH(_positions(_pos(U1), nxt="https://api.robinhood.com/options/positions/?cursor=x"))
    assert robinhood.held_option_qty(rh, CFG, [U1]) is None
    rh = FakeRH(_positions(_pos(U1), nxt=""))
    assert robinhood.held_option_qty(rh, CFG, [U1]) == {U1: 1}
    rh = FakeRH(_positions(_pos(U1), nxt="c2"), _err())
    assert robinhood.held_option_qty(rh, CFG, [U1, U2]) is None
    rh = FakeRH(_positions(_pos(U1), nxt="c2"), _down)
    assert robinhood.held_option_qty(rh, CFG, [U1, U2]) is None


def test_rh_positions_iserror_retries_without_nonzero():
    rh = FakeRH(_err("unknown argument nonzero"),
                _positions(_pos(U1, "0.0000"), _pos(U2, "1.0000")))
    assert robinhood.held_option_qty(rh, CFG, [U1, U2, U3]) == {U1: 0, U2: 1, U3: 0}
    assert rh.calls[1][1] == {"account_number": "A1"}


def test_rh_positions_non_uuid_ids_omitted_and_no_call_when_none():
    rh = FakeRH(_positions(_pos(U1)))
    assert robinhood.held_option_qty(rh, CFG, ["dry", "opt-123",
                                               "SPY260710C00752000"]) == {}
    assert rh.calls == []
    assert robinhood.held_option_qty(rh, CFG, ["dry", U1]) == {U1: 1}


# ── robinhood: closed_fills ─────────────────────────────────────────────────

def _stc(oid="o-user", option=U1, execs=(("1.80000000", "1.00000",
                                          "2026-10-08T17:27:05.234000Z"),),
         legs=None, **kw):
    """A filled market sell-to-close in the live get_option_orders shape."""
    o = {"id": oid, "chain_symbol": "NFLX", "state": "filled", "type": "market",
         "direction": "credit", "quantity": "1.00000",
         "processed_quantity": "1.00000", "pending_quantity": "0.00000",
         "price": "", "premium": "", "processed_premium": "180",
         "trade_value_multiplier": "100.0000", "placed_agent": "user",
         "created_at": "2026-10-08T17:27:05.074736Z",
         "updated_at": "2026-10-08T17:27:05.491363Z",
         "last_transaction_at": None}
    leg = {"id": "leg-1", "option_id": option, "side": "sell",
           "position_effect": "close", "ratio_quantity": 1,
           "option_type": "call", "strike_price": "70.0000"}
    if execs is not None:
        leg["executions"] = [{"id": f"x{i}", "price": p, "quantity": q,
                              "timestamp": t, "trade_date": "2026-10-08"}
                             for i, (p, q, t) in enumerate(execs)]
    o["legs"] = legs if legs is not None else [leg]
    o.update(kw)
    return o


def _orders(*orders, nxt=None):
    data = {"orders": list(orders)}
    if nxt is not None:
        data["next"] = nxt
    return _mcp({"data": data, "guide": "orders"})


SINCE = "2026-10-08T17:25:56+00:00"


def test_rh_closed_fills_live_market_stc():
    rh = FakeRH(_orders(_stc()))
    got = robinhood.closed_fills(rh, CFG, U1, since_iso=SINCE,
                                 own_order_ids=["o-agent"])
    assert got == [{"order_id": "o-user", "qty": 1, "price": 1.8,
                    "at": "2026-10-08T17:27:05.234000Z", "by": "user"}]
    (name, args), = rh.calls
    assert name == "get_option_orders"
    assert args == {"account_number": "A1", "created_at_gte": "2026-10-07"}


def test_rh_closed_fills_vwap_and_premium_fallback():
    rh = FakeRH(_orders(_stc(execs=(("1.80", "1.00000", "2026-10-08T17:27:05Z"),
                                    ("2.10", "2.00000", "2026-10-08T17:27:09Z")))))
    (f,) = robinhood.closed_fills(rh, CFG, U1)
    assert f["qty"] == 3 and f["price"] == pytest.approx(2.0)
    assert f["at"] == "2026-10-08T17:27:09Z"
    rh = FakeRH(_orders(_stc(execs=None, processed_premium="360",
                             processed_quantity="2.00000",
                             trade_value_multiplier="100.0000")))
    (f,) = robinhood.closed_fills(rh, CFG, U1)
    assert f["qty"] == 2 and f["price"] == pytest.approx(1.8)
    assert f["at"] == "2026-10-08T17:27:05.491363Z"   # updated_at
    # executions with no usable price: quantity counts, price unknown
    rh = FakeRH(_orders(_stc(execs=(("", "1.00000", "2026-10-08T17:27:05Z"),))))
    (f,) = robinhood.closed_fills(rh, CFG, U1)
    assert f["qty"] == 1 and f["price"] is None


def test_rh_closed_fills_filters():
    url_leg = {"option": f"https://api.robinhood.com/options/instruments/{U1}/",
               "side": "sell", "position_effect": "close",
               "executions": [{"price": "1.50", "quantity": "1.00000",
                               "timestamp": "2026-10-08T18:00:00Z"}]}
    two_legs = [{"option_id": U1, "side": "sell", "position_effect": "close"},
                {"option_id": U2, "side": "buy", "position_effect": "open"}]
    rh = FakeRH(_orders(
        _stc(oid="o-agent"),                                    # our own order
        _stc(oid="o-buy", legs=[{"option_id": U1, "side": "buy",
                                 "position_effect": "open"}]),
        _stc(oid="o-open", legs=[{"option_id": U1, "side": "sell",
                                  "position_effect": "open"}]),
        _stc(oid="o-other", option=U2),
        _stc(oid="o-cancel", execs=None, state="cancelled",
             processed_quantity="0.00000", processed_premium="0"),
        _stc(oid="o-multi", execs=None, legs=two_legs),
        "not-an-order",
        _stc(oid="o-url", legs=[url_leg])))
    got = robinhood.closed_fills(rh, CFG, U1, own_order_ids=["o-agent"])
    assert [f["order_id"] for f in got] == ["o-url"]
    assert got[0]["price"] == 1.5


def test_rh_closed_fills_legacy_excludes_agentic():
    rh = FakeRH(_orders(_stc(oid="o-a", placed_agent="agentic"), _stc(oid="o-u")))
    got = robinhood.closed_fills(rh, CFG, U1, exclude_agentic=True)
    assert [f["order_id"] for f in got] == ["o-u"]


def test_rh_closed_fills_errors():
    rh = FakeRH(_err("bad created_at_gte"), _orders(_stc()))
    got = robinhood.closed_fills(rh, CFG, U1, since_iso=SINCE)
    assert [f["order_id"] for f in got] == ["o-user"]
    assert rh.calls[1][1] == {"account_number": "A1"}
    rh = FakeRH(_err(), _err())
    assert robinhood.closed_fills(rh, CFG, U1, since_iso=SINCE) is None
    rh = FakeRH(_orders(_stc(oid="p1"), nxt="c2"), _err())
    assert [f["order_id"] for f in robinhood.closed_fills(rh, CFG, U1)] == ["p1"]
    assert rh.calls[1][1]["cursor"] == "c2"
    rh = FakeRH(*[_orders(_stc(oid=f"p{i}"), nxt=f"c{i}") for i in range(5)])
    assert len(robinhood.closed_fills(rh, CFG, U1)) == 3
    assert len(rh.calls) == 3
    rh = FakeRH(_down)
    assert robinhood.closed_fills(rh, CFG, U1) is None
    assert len(rh.calls) == 1
    assert robinhood.closed_fills(FakeRH(), CFG, "not-a-uuid") is None
    assert robinhood.closed_fills(FakeRH(), {}, U1) is None
    assert robinhood.closed_fills(FakeRH(RuntimeError("x")), CFG, U1) is None


# ── robinhood: own-order lookups and stamping ───────────────────────────────

def test_rh_order_status_sends_order_id_and_falls_back():
    live = _orders({"id": "o-7", "state": "filled", "processed_quantity": "2.00000",
                    "legs": [{"id": "leg", "option_id": U1}]})
    rh = FakeRH(live)
    assert robinhood._order_status(rh, "A1", "o-7") == ("filled", 2)
    assert rh.calls == [("get_option_orders",
                         {"account_number": "A1", "order_id": "o-7"})]
    rh = FakeRH(_err("unknown order_id"), live)
    assert robinhood._order_status(rh, "A1", "o-7") == ("filled", 2)
    assert rh.calls[1][1] == {"account_number": "A1"}
    legacy = _mcp({"data": {"results": [{"id": "o-7", "state": "cancelled",
                                         "processed_quantity": "1"}]}})
    assert robinhood._order_status(FakeRH(legacy), "A1", "o-7") == ("cancelled", 1)
    rh = FakeRH(_down)
    assert robinhood._order_status(rh, "A1", "o-7") == (None, 0)
    assert len(rh.calls) == 1   # connection down: no second call


def test_rh_record_pending_stamps_order_ids_and_last_order_at():
    key = "SPY|2099-12-18|752.0|C"
    state = {"positions": {key: {"option_id": U1, "qty": 0,
                                 "order_ids": [f"old{i}" for i in range(20)]}}}
    robinhood._record_pending(state, key, {"order_id": "o-1", "was_limit": False},
                              "buy", 1)
    robinhood._record_pending(state, key, {"order_id": "o-1", "was_limit": False},
                              "buy", 1)
    pos = state["positions"][key]
    assert pos["order_ids"][-1] == "o-1" and pos["order_ids"].count("o-1") == 1
    assert len(pos["order_ids"]) == 20 and "old0" not in pos["order_ids"]
    assert pos["last_order_at"] == pytest.approx(robinhood.time.time(), abs=5)
    robinhood._record_pending(state, key, {"order_id": "", "was_limit": False},
                              "buy", 1)
    assert "" not in pos["order_ids"]


def test_rh_finalize_pending_stamps_and_clears_miss():
    def row(side, qty):
        return {"option_id": U1, "qty": qty, "last_order_at": 1.0,
                "miss": {"held": 0, "reads": 1},
                "pending": {"order_id": "o", "side": side, "requested": 2}}
    broker_events.begin()
    state = {"positions": {"K": row("buy", 0)}}
    robinhood._finalize_pending(state, "K", 2)
    pos = state["positions"]["K"]
    assert pos["last_order_at"] > 1.0 and "miss" not in pos
    state = {"positions": {"K": row("sell", 3)}}
    robinhood._finalize_pending(state, "K", 1)  # partial exit: 2 left
    pos = state["positions"]["K"]
    assert pos["qty"] == 2 and pos["last_order_at"] > 1.0 and "miss" not in pos
    state = {"positions": {"K": row("sell", 2)}}
    robinhood._finalize_pending(state, "K", 2)
    assert state["positions"] == {}
    state = {"positions": {"K": row("buy", 0)}}
    robinhood._finalize_pending(state, "K", 0)
    assert state["positions"] == {}
    broker_events.drain()


# ── alpaca ──────────────────────────────────────────────────────────────────

OCC1 = "SPY991218C00752000"
OCC2 = "QQQ991218P00500000"
OCC3 = "NVDA991218C01000000"
ALPACA = {"alpaca_key_id": "k", "alpaca_secret": "s"}


class _Resp:
    text = ""

    def __init__(self, status=200, body=None):
        self.status_code, self._body = status, body

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def _alpaca_get(monkeypatch, *responses):
    calls, script = [], list(responses)

    def get(url, **kw):
        calls.append((url, kw))
        r = script.pop(0) if len(script) > 1 else script[0]
        if isinstance(r, Exception):
            raise r
        return r
    monkeypatch.setattr(alpaca.requests, "get", get)
    return calls


def _apos(sym, qty="2", **kw):
    row = {"asset_id": "a1", "symbol": sym, "exchange": "", "asset_class": "us_option",
           "avg_entry_price": "1.5", "qty": qty, "qty_available": qty,
           "side": "long", "market_value": "300"}
    row.update(kw)
    return row


def test_alpaca_positions_parse(monkeypatch):
    calls = _alpaca_get(monkeypatch, _Resp(200, [
        _apos(OCC1, "2"),
        _apos(OCC2, "-1", side="short"),
        {"symbol": "SPY", "asset_class": "us_equity", "qty": "10"},
        _apos(OCC3, "1", qty_available="0"),        # a sell is working on it
    ]))
    cl = alpaca._Client(ALPACA)
    assert alpaca.held_option_qty(cl, {}, [OCC1, OCC2, OCC3]) == {OCC1: 2, OCC2: 0}
    url, kw = calls[0]
    assert url == "https://paper-api.alpaca.markets/v2/positions"   # paper by default
    assert kw["headers"]["APCA-API-KEY-ID"] == "k" and kw["timeout"] == alpaca._TIMEOUT
    # the cfg passed in wins over the client's stored copy, like execute()
    alpaca.held_option_qty(cl, {"alpaca_paper": False, "alpaca_key_id": "k2"}, [OCC1])
    assert calls[1][0] == "https://api.alpaca.markets/v2/positions"
    assert calls[1][1]["headers"]["APCA-API-KEY-ID"] == "k2"


@pytest.mark.parametrize("resp", [
    _Resp(401, []), _Resp(403, []), _Resp(429, []), _Resp(500, []),
    RuntimeError("network down"), _Resp(200, ValueError("not json")),
    _Resp(200, {"positions": []}),
    _Resp(200, [{"qty": "1"}]),                    # no symbol
    _Resp(200, [{"symbol": OCC1}]),                # no qty
    _Resp(200, [_apos(OCC1, "abc")]),              # junk on our symbol
    _Resp(200, ["row"]),
], ids=["401", "403", "429", "500", "raises", "non-json", "dict", "no-symbol",
        "no-qty", "junk-qty", "non-dict-row"])
def test_alpaca_positions_unknown(monkeypatch, resp):
    _alpaca_get(monkeypatch, resp)
    assert alpaca.held_option_qty(alpaca._Client(ALPACA), {}, [OCC1]) is None


def test_alpaca_positions_non_occ_ids_omitted(monkeypatch):
    calls = _alpaca_get(monkeypatch, _Resp(200, [_apos(OCC1, "1"), _apos(OCC2, "junk")]))
    cl = alpaca._Client(ALPACA)
    assert alpaca.held_option_qty(cl, {}, ["dry", U1, "US.SPY991218C752000"]) == {}
    assert calls == []
    assert alpaca.held_option_qty(cl, {}, ["dry", OCC1]) == {OCC1: 1}


def _aorder(oid, sym=OCC1, side="sell", filled="2", price="1.80", **kw):
    o = {"id": oid, "client_order_id": "c", "symbol": sym, "side": side,
         "qty": filled, "filled_qty": filled, "filled_avg_price": price,
         "status": "filled", "filled_at": "2026-10-30T15:01:00.123456789Z",
         "updated_at": "2026-10-30T15:01:01Z", "position_intent": "sell_to_close",
         "asset_class": "us_option"}
    o.update(kw)
    return o


def test_alpaca_closed_fills(monkeypatch):
    calls = _alpaca_get(monkeypatch, _Resp(200, [
        _aorder("u1"), _aorder("own"), _aorder("sto", position_intent="sell_to_open"),
        _aorder("buy", side="buy", position_intent="buy_to_open"),
        _aorder("unfilled", filled="0", price=None, status="canceled"),
        _aorder("other", sym=OCC2), _aorder("noprice", price=None, filled_at=None),
        "junk"]))
    cl = alpaca._Client(ALPACA)
    got = alpaca.closed_fills(cl, {}, OCC1, since_iso="2026-10-30T14:00:00+00:00",
                              own_order_ids=["own"])
    assert got == [{"order_id": "u1", "qty": 2, "price": 1.8,
                    "at": "2026-10-30T15:01:00.123456789Z", "by": None},
                   {"order_id": "noprice", "qty": 2, "price": None,
                    "at": "2026-10-30T15:01:01Z", "by": None}]
    url, kw = calls[0]
    assert url == "https://paper-api.alpaca.markets/v2/orders"
    assert kw["params"] == {"status": "closed", "symbols": OCC1, "direction": "desc",
                            "limit": 100, "after": "2026-10-30T14:00:00+00:00"}
    for bad in (_Resp(500, []), _Resp(200, {"orders": []}), RuntimeError("down")):
        _alpaca_get(monkeypatch, bad)
        assert alpaca.closed_fills(cl, {}, OCC1) is None
    calls = _alpaca_get(monkeypatch, _Resp(200, []))
    assert alpaca.closed_fills(cl, {}, "not-an-occ") is None and calls == []


def test_alpaca_execute_stamps_order_ids(monkeypatch):
    monkeypatch.setattr(alpaca, "_confirm_fill", lambda cfg, oid, qty, side="buy": qty)
    monkeypatch.setattr(alpaca, "_order", lambda cfg, occ, side, qty: "oid-1")
    st = {"positions": {}}
    cl = alpaca._Client(ALPACA)
    assert alpaca.execute(cl, {"contracts_per_trade": 3},
                          make_event(expiry="2099-12-18"), st)
    pos = st["positions"]["SPY|2099-12-18|752.0|C"]
    assert pos["order_ids"] == ["oid-1"]
    assert pos["last_order_at"] == pytest.approx(time.time(), abs=5)
    # a partial exit keeps the rest of the row: its order is stamped too
    pos["last_order_at"] = 1.0
    monkeypatch.setattr(alpaca, "_confirm_fill", lambda cfg, oid, qty, side="buy": 1)
    monkeypatch.setattr(alpaca, "_order", lambda cfg, occ, side, qty: "oid-2")
    broker_events.begin()
    assert alpaca.execute(cl, {}, make_event(event="EXITED", expiry="2099-12-18"), st)
    broker_events.drain()
    assert pos["qty"] == 2 and pos["order_ids"] == ["oid-1", "oid-2"]
    assert pos["last_order_at"] > 1.0
    # no order id from Alpaca: nothing to remember, but the time still counts
    st = {"positions": {}}
    monkeypatch.setattr(alpaca, "_confirm_fill", lambda cfg, oid, qty, side="buy": qty)
    monkeypatch.setattr(alpaca, "_order", lambda cfg, occ, side, qty: "submitted")
    alpaca.execute(cl, {}, make_event(expiry="2099-12-18"), st)
    pos = st["positions"]["SPY|2099-12-18|752.0|C"]
    assert "order_ids" not in pos and pos["last_order_at"] > 1.0


# ── moomoo ──────────────────────────────────────────────────────────────────

MM1, MM2, MM3, MM4, MM5 = (f"US.SPY991218C75{i}000" for i in range(5))


class _FakeDF:
    def __init__(self, rows):
        self._rows = rows

    def __len__(self):
        return len(self._rows)

    @property
    def iloc(self):
        return self._rows


class _MMCtx:
    def __init__(self, sdk):
        self.sdk, self.closed, self.queries = sdk, False, []

    def _answer(self, ret, rows):
        return ret, (None if rows is None else _FakeDF(rows))

    def position_list_query(self, **kw):
        self.queries.append(("positions", kw))
        return self._answer(self.sdk.pos_ret, self.sdk.positions)

    def order_list_query(self, **kw):
        self.queries.append(("orders", kw))
        return self._answer(self.sdk.ord_ret, self.sdk.orders)

    def place_order(self, **kw):
        self.sdk.placed.append(kw)
        return self.sdk.RET_OK, _FakeDF([{"order_id": "mm-1"}])

    def unlock_trade(self, pwd):
        return self.sdk.RET_OK, None

    def close(self):
        self.closed = True


class _MMSDK:
    """Duck-typed moomoo-api stand-in with scripted positions and orders."""
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

    def __init__(self, positions=(), orders=(), pos_ret=0, ord_ret=0):
        self.positions = None if positions is None else list(positions)
        self.orders = None if orders is None else list(orders)
        self.pos_ret, self.ord_ret = pos_ret, ord_ret
        self.ctxs, self.placed = [], []

    def OpenSecTradeContext(self, **kw):
        self.ctxs.append(_MMCtx(self))
        return self.ctxs[-1]

    def OpenQuoteContext(self, **kw):
        sdk = self

        class _Q:
            def get_market_snapshot(self, codes):
                return sdk.RET_OK, _FakeDF([{"ask_price": 1.5, "last_price": 1.5}])

            def close(self):
                pass
        return _Q()


MOOMOO = {"broker": "moomoo", "moomoo_paper": True, "contracts_per_trade": 1}


def test_moomoo_positions_parse(monkeypatch):
    sdk = _MMSDK(
        positions=[{"code": MM1, "qty": 2.0, "can_sell_qty": 2.0, "position_side": "LONG"},
                   {"code": MM2, "qty": 1.0, "position_side": "PositionSide.SHORT"},
                   {"code": MM3, "qty": 1.0},
                   {"code": MM4, "qty": 3.0}],
        orders=[{"code": MM3, "order_status": "SUBMITTED"},       # still working
                {"code": MM1, "order_status": "OrderStatus.FILLED_ALL"},
                {"code": MM4, "order_status": "CANCELLED_ALL"}])
    monkeypatch.setattr(moomoo, "_sdk", lambda: sdk)
    got = moomoo.held_option_qty(moomoo._Client(MOOMOO), {}, [MM1, MM2, MM3, MM4, MM5])
    assert got == {MM1: 2, MM2: 0, MM4: 3, MM5: 0}
    (ctx,) = sdk.ctxs
    assert ctx.closed
    # fresh from the broker, not OpenD's cache
    assert ctx.queries == [("positions", {"trd_env": "SIM", "refresh_cache": True}),
                           ("orders", {"trd_env": "SIM", "refresh_cache": True})]


def _mm_order(code, side, dealt, status="FILLED_ALL"):
    return {"code": code, "order_status": f"OrderStatus.{status}",
            "trd_side": f"TrdSide.{side}", "qty": 2.0, "dealt_qty": dealt}


@pytest.mark.parametrize("positions, orders, want", [
    # the agent's buy filled today but the position list doesn't show it yet
    ([], [_mm_order(MM1, "BUY", 1.0)], {}),
    ([{"code": MM1, "qty": 1.0}], [_mm_order(MM1, "BUY", 2.0)], {}),
    ([{"code": MM1, "qty": 2.0}], [_mm_order(MM1, "BUY", 2.0)], {MM1: 2}),
    # bought 2, sold 1 in the app: 1 held is consistent
    ([{"code": MM1, "qty": 1.0}],
     [_mm_order(MM1, "BUY", 2.0), _mm_order(MM1, "SELL", 1.0)], {MM1: 1}),
    ([], [_mm_order(MM1, "BUY", 2.0), _mm_order(MM1, "SELL", 2.0)], {MM1: 0}),
    # a partly filled buy counts what filled
    ([{"code": MM1, "qty": 1.0}], [_mm_order(MM1, "BUY", 1.0, "CANCELLED_PART")],
     {MM1: 1}),
    ([], [_mm_order(MM1, "BUY", 0.0, "CANCELLED_ALL")], {MM1: 0}),
    # a filled order that can't be read makes that code unknown
    ([], [_mm_order(MM1, "", 1.0)], {}),
    ([], [_mm_order(MM1, "BUY", "junk")], {}),
    ([], [_mm_order(MM2, "BUY", 1.0)], {MM1: 0}),     # another contract
], ids=["unlisted-buy", "short-of-buy", "matches-buy", "owner-sold-1",
        "owner-sold-all", "partial-buy", "unfilled-buy", "no-side", "junk-qty",
        "other-code"])
def test_moomoo_positions_cross_checked_with_todays_fills(monkeypatch, positions,
                                                          orders, want):
    sdk = _MMSDK(positions=positions, orders=orders)
    monkeypatch.setattr(moomoo, "_sdk", lambda: sdk)
    assert moomoo.held_option_qty(moomoo._Client(MOOMOO), {}, [MM1]) == want


def test_moomoo_uses_the_live_cfg_over_the_clients_startup_copy(monkeypatch):
    """`run` keeps its startup client while config.json reloads: after the
    owner flips paper/real in the dashboard, orders and reads follow the
    live config — the one the agent tags positions with."""
    sdk = _MMSDK()
    monkeypatch.setattr(moomoo, "_sdk", lambda: sdk)
    startup = moomoo._Client({"broker": "moomoo", "moomoo_paper": False,
                              "moomoo_trade_pwd": "x"})
    assert moomoo.held_option_qty(startup, MOOMOO, [MM1]) == {MM1: 0}
    assert [kw["trd_env"] for _, kw in sdk.ctxs[0].queries] == ["SIM", "SIM"]
    st = {"positions": {}}
    assert moomoo.execute(startup, MOOMOO, make_event(expiry="2099-12-18"), st)
    assert sdk.placed[-1]["trd_env"] == "SIM"
    # with no cfg passed, the client's copy still works
    moomoo.held_option_qty(startup, {}, [MM1])
    assert [kw["trd_env"] for _, kw in sdk.ctxs[-1].queries] == ["REAL", "REAL"]


@pytest.mark.parametrize("sdk", [
    _MMSDK(pos_ret=-1),
    _MMSDK(ord_ret=-1),
    _MMSDK(positions=None),
    _MMSDK(orders=None),
    _MMSDK(positions=[{"qty": 1.0}]),            # no code
    _MMSDK(positions=[{"code": MM1}]),           # no qty
    _MMSDK(positions=[{"code": MM1, "qty": "abc"}]),
], ids=["pos-ret", "ord-ret", "pos-none", "ord-none", "no-code", "no-qty", "junk"])
def test_moomoo_positions_unknown(monkeypatch, sdk):
    monkeypatch.setattr(moomoo, "_sdk", lambda: sdk)
    assert moomoo.held_option_qty(moomoo._Client(MOOMOO), {}, [MM1]) is None
    assert all(c.closed for c in sdk.ctxs) and sdk.ctxs


def test_moomoo_positions_without_sdk_is_unknown(monkeypatch):
    def missing():
        raise RuntimeError("moomoo SDK not installed")
    monkeypatch.setattr(moomoo, "_sdk", missing)
    assert moomoo.held_option_qty(moomoo._Client(MOOMOO), {}, [MM1]) is None


def test_moomoo_non_us_ids_omitted(monkeypatch):
    sdk = _MMSDK(positions=[{"code": MM1, "qty": 1.0}])
    monkeypatch.setattr(moomoo, "_sdk", lambda: sdk)
    cl = moomoo._Client(MOOMOO)
    assert moomoo.held_option_qty(cl, {}, ["dry", U1, OCC1]) == {}
    assert sdk.ctxs == []
    assert moomoo.held_option_qty(cl, {}, ["dry", MM1]) == {MM1: 1}


def test_moomoo_verifies_fills_false_and_no_closed_fills():
    assert moomoo.VERIFIES_FILLS is False and not hasattr(moomoo, "closed_fills")
    assert getattr(robinhood, "VERIFIES_FILLS", True) is True
    assert getattr(alpaca, "VERIFIES_FILLS", True) is True


def test_moomoo_execute_stamps_order_ids(monkeypatch):
    sdk = _MMSDK()
    monkeypatch.setattr(moomoo, "_sdk", lambda: sdk)
    st = {"positions": {}}
    assert moomoo.execute(None, MOOMOO, make_event(expiry="2099-12-18"), st)
    pos = st["positions"]["SPY|2099-12-18|752.0|C"]
    assert pos["order_ids"] == ["mm-1"] and pos["qty"] == 1
    assert pos["last_order_at"] == pytest.approx(time.time(), abs=5)
