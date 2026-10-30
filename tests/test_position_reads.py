"""Broker read hooks used to notice trades the owner closed in the broker app:
held_option_qty / closed_fills per adapter, plus account_tag. Shapes follow
the live Robinhood MCP captures (data.positions / data.orders, decimal
strings, price "" on market orders). Every unclear answer must read as
unknown (None or an omitted id), never as "not held". Zero network."""

import json

import pytest

import broker_events
from brokers import account_tag, robinhood

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
