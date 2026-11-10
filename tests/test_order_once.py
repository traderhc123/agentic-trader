"""One signal, one order.

An order request that gets no clear answer (a timeout, a dropped
connection, a gateway error) may or may not have reached the broker. The
agent must never answer that by sending the order again: the account would
hold twice what the ledger shows, and nothing would ever sell the extra
contracts. These tests drive the real adapters against fake brokers that
lose the answer after taking the order. Zero network."""

import json
import time
from datetime import datetime, timezone

import pytest
import requests

import agent
import broker_events
from brokers import alpaca, order_guard, robinhood
from conftest import make_event

KEY = "SPY|2026-07-10|752.0|C"
OCC = "SPY260710C00752000"
OPT = "14a8e49b-0f10-4d64-9cf9-bd1a68e9da44"


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    """No real waiting in the adapters' fill checks."""
    monkeypatch.setattr(alpaca, "_CONFIRM_WAIT_S", 0)
    monkeypatch.setattr(robinhood, "_SETTLE_WAIT_S", 0)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    monkeypatch.setattr(robinhood, "_in_opening_blackout", lambda now=None: False)


@pytest.fixture
def notes(monkeypatch):
    sent = []
    monkeypatch.setattr(agent, "notify", lambda cfg, msg, **kw: sent.append(msg))
    monkeypatch.setattr(agent, "_problems_logged", set())
    return sent


def _log(home):
    p = home / "trades.jsonl"
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()]


class Feed:
    """What `agent.py run` does with the feed's events on every poll: an
    event whose handling raised is NOT marked seen, so it comes back on the
    next poll (cmd_run's loop)."""

    def __init__(self, broker, client, cfg, state):
        self.broker, self.client, self.cfg, self.state = broker, client, cfg, state
        self.seen, self.errors = set(), []

    def poll(self, *events):
        if hasattr(self.broker, "reconcile"):
            self.broker.reconcile(self.client, self.cfg, self.state, lambda s: None)
        for ev in events:
            if ev["event_id"] in self.seen:
                continue
            try:
                agent.handle_event(dict(ev), self.cfg, self.state, self.broker,
                                   self.client, lambda s: None)
            except Exception as exc:
                self.errors.append(exc)
                continue
            self.seen.add(ev["event_id"])


# ── Alpaca ──────────────────────────────────────────────────────────────────

class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body, self.text = status, body, json.dumps(body)

    def json(self):
        return self._body


class FakeAlpaca:
    """Alpaca's HTTP API. `plan` scripts the next order requests:
    "ok", "timeout" (the order is taken, the answer never arrives), "cut"
    (the same, and Alpaca can't be reached from then on), "lost" (the
    request never reaches Alpaca), or an HTTP status that is answered AFTER
    the order is taken (a gateway error). `down` fails every read."""

    def __init__(self, monkeypatch, plan=()):
        self.plan, self.orders, self.posts, self.down = list(plan), [], [], False
        self.fills = None  # None = orders fill completely
        monkeypatch.setattr(alpaca.requests, "post", self.post)
        monkeypatch.setattr(alpaca.requests, "get", self.get)
        monkeypatch.setattr(alpaca.requests, "delete", self.delete)

    def _find(self, **want):
        return next((o for o in self.orders
                     if all(o.get(k) == v for k, v in want.items())), None)

    def post(self, url, **kw):
        assert url.endswith("/v2/orders")
        body = dict(kw["json"])
        self.posts.append(body)
        mode = self.plan.pop(0) if self.plan else "ok"
        if mode == "lost":
            raise requests.ConnectionError("connection reset before sending")
        cid = body.get("client_order_id")
        if cid and self._find(client_order_id=cid):
            return _Resp(422, {"code": 40010001,
                               "message": "client_order_id must be unique"})
        qty = int(body["qty"])
        filled = qty if self.fills is None else min(qty, self.fills)
        order = {"id": f"a-{len(self.orders) + 1}", "client_order_id": cid,
                 "submitted_at": datetime.now(timezone.utc).isoformat()
                 .replace("+00:00", "Z"),
                 "symbol": body["symbol"], "side": body["side"], "qty": str(qty),
                 "status": "filled" if filled >= qty else "canceled",
                 "filled_qty": str(filled)}
        self.orders.append(order)
        if mode in ("timeout", "cut"):
            self.down = mode == "cut"
            raise requests.ReadTimeout("read timed out")
        if isinstance(mode, int):
            return _Resp(mode, {"message": "upstream error"})
        return _Resp(200, order)

    def get(self, url, **kw):
        if self.down:
            raise requests.ConnectionError("no route to host")
        if url.endswith("/v2/orders:by_client_order_id"):
            hit = self._find(client_order_id=kw["params"]["client_order_id"])
            return _Resp(200, hit) if hit else _Resp(404, {"message": "order not found"})
        if "/v2/orders/" in url:
            hit = self._find(id=url.rsplit("/", 1)[-1])
            return _Resp(200, hit) if hit else _Resp(404, {})
        if "quotes/latest" in url:
            return _Resp(200, {"quotes": {kw["params"]["symbols"]:
                                          {"ap": 2.50, "bp": 2.40}}})
        return _Resp(404, {})

    def delete(self, url, **kw):
        return _Resp(204, {})

    def held(self):
        """Net contracts the account holds (buys minus sells that filled)."""
        return sum(int(o["filled_qty"]) * (1 if o["side"] == "buy" else -1)
                   for o in self.orders)


ALP_CFG = {"broker": "alpaca", "alpaca_key_id": "k", "alpaca_secret": "s",
           "sizing_mode": "budget", "budget_per_trade_usd": 500,
           "max_entries_per_day": 5, "position_sync": False}


def _alpaca_feed(state=None):
    state = state if state is not None else {"positions": {}, "seen": []}
    return Feed(alpaca, alpaca.client(ALP_CFG), ALP_CFG, state), state


def test_alpaca_buy_timeout_never_buys_twice(home, no_policy, notes, monkeypatch):
    """THE BUG: Alpaca took the buy but the answer timed out. The event
    stayed unseen and the next poll bought again: 4 contracts in the
    account, 2 in the ledger, the extra 2 never sold."""
    api = FakeAlpaca(monkeypatch, plan=["timeout"])
    feed, state = _alpaca_feed()
    ev = make_event()
    for _ in range(3):
        feed.poll(ev)
    assert [o["side"] for o in api.orders] == ["buy"]
    assert api.held() == 2
    assert state["positions"][KEY]["qty"] == 2          # ledger == account
    assert state["positions"][KEY]["order_ids"] == ["a-1"]
    assert [r["action"] for r in _log(home)] == ["entry"]
    assert ev["event_id"] in feed.seen and not feed.errors


def test_alpaca_buy_unknown_outcome_is_handled_once_and_the_owner_is_told(
        home, no_policy, notes, monkeypatch):
    """The answer is lost AND Alpaca can't be asked: the agent must not buy
    again. It records the signal as handled, tells the owner in plain words
    to check their broker app, and picks the order up once Alpaca answers."""
    api = FakeAlpaca(monkeypatch, plan=["cut"])
    feed, state = _alpaca_feed()
    ev = make_event()
    feed.poll(ev)
    assert ev["event_id"] in feed.seen and not feed.errors
    assert KEY not in state["positions"]
    assert order_guard.get(state, KEY)["side"] == "buy"
    (alert,) = notes
    assert alert.startswith("agentic-trader: check this trade (SPY 2026-07-10")
    assert "can't tell whether the order went through" in alert
    assert "will NOT send this buy again" in alert and "Alpaca" in alert
    (row,) = _log(home)
    assert row["action"] == "order_failed" and row["event_id"] == ev["event_id"]
    for _ in range(3):                                   # still down: nothing new
        feed.poll(ev)
    assert len(api.posts) == 1 and KEY not in state["positions"]
    api.down = False                                     # Alpaca answers again
    feed.poll(ev)
    assert len(api.posts) == 1 and api.held() == 2
    assert state["positions"][KEY]["qty"] == 2
    assert state["positions"][KEY]["opened_event"] == ev["event_id"]
    assert order_guard.get(state, KEY) is None
    # ...and its exit signal sells it like any other position
    feed.poll(make_event(event="EXITED", event_id="x-1"))
    assert api.held() == 0 and state["positions"] == {}


def test_alpaca_buy_that_never_arrived_is_not_sent_again(home, no_policy, notes,
                                                        monkeypatch):
    """The request never reached Alpaca (Alpaca: no such order). Buying on
    a later poll would still be a second order for the same signal if the
    first one shows up late: the signal is skipped, and the owner told."""
    api = FakeAlpaca(monkeypatch, plan=["lost"])
    feed, state = _alpaca_feed()
    ev = make_event()
    for _ in range(3):
        feed.poll(ev)
    assert api.orders == [] and len(api.posts) == 1
    assert state["positions"] == {} and len(notes) == 1
    # the agent stops looking for it after a while
    unc = order_guard.get(state, KEY)
    unc["at"] -= order_guard.BUY_KEEP_S + 1
    feed.poll(ev)
    assert order_guard.get(state, KEY) is None and len(api.posts) == 1


def test_alpaca_gateway_error_after_the_order_was_taken_adopts_it(
        home, no_policy, notes, monkeypatch):
    api = FakeAlpaca(monkeypatch, plan=[504])
    feed, state = _alpaca_feed()
    feed.poll(make_event())
    feed.poll(make_event())
    assert len(api.orders) == 1 and state["positions"][KEY]["qty"] == 2


def test_alpaca_same_signal_always_sends_the_same_order_id(home, no_policy, notes,
                                                          monkeypatch):
    """The id sent with a buy comes from the signal, so even an agent that
    lost its memory (killed mid-order, state file gone) can't buy the same
    signal twice: Alpaca refuses the repeat and the agent adopts the order
    that is already there."""
    api = FakeAlpaca(monkeypatch)
    ev = make_event()
    feed, state = _alpaca_feed()
    feed.poll(ev)
    fresh, state2 = _alpaca_feed()                       # memory lost
    fresh.poll(ev)
    assert len(api.orders) == 1 and len(api.posts) == 2
    assert api.posts[0]["client_order_id"] == api.posts[1]["client_order_id"]
    assert state2["positions"][KEY]["order_ids"] == ["a-1"]
    # a different signal is a different order
    feed3, _ = _alpaca_feed()
    feed3.poll(make_event(event_id="test-2", strike=753.0))
    assert api.posts[2]["client_order_id"] != api.posts[0]["client_order_id"]


def test_alpaca_an_old_order_under_the_same_id_is_never_taken_over_again(
        home, no_policy, notes, monkeypatch):
    """A signal that comes back long after it was traded (and maybe sold):
    Alpaca refuses the repeat. The old order must NOT be booked as a new
    position — the account may no longer hold those contracts, and selling
    them later would open a short."""
    api = FakeAlpaca(monkeypatch)
    ev = make_event()
    first, _ = _alpaca_feed()
    first.poll(ev)
    api.orders[0]["submitted_at"] = "2026-01-05T15:00:00.123456789Z"   # long ago
    again, state = _alpaca_feed()                        # memory lost since
    again.poll(ev)
    assert len(api.orders) == 1 and state["positions"] == {}
    assert order_guard.unconfirmed(state) == {}
    (alert,) = [n for n in notes if "earlier order" in n]
    assert alert.startswith("agentic-trader: check this trade")
    # ...and an order the ledger already knows is not a lost one either
    api.orders[0]["submitted_at"] = datetime.now(timezone.utc).isoformat()
    known, state = _alpaca_feed({"positions": {}, "seen": [],
                                 "agent_order_ids": ["a-1"]})
    known.poll(ev)
    assert len(api.orders) == 1 and state["positions"] == {}


def _held_alpaca_state():
    return {"positions": {KEY: {"option_id": OCC, "qty": 2, "order_ids": ["b-0"],
                                "opened_event": "test-1"}}, "seen": []}


def test_alpaca_sell_timeout_never_sells_twice(home, no_policy, notes, monkeypatch):
    """A second plain sell after the first one filled opens a SHORT."""
    api = FakeAlpaca(monkeypatch, plan=["cut"])          # and can't ask Alpaca yet
    feed, state = _alpaca_feed(_held_alpaca_state())
    ev = make_event(event="EXITED", event_id="x-1")
    for _ in range(3):
        feed.poll(ev)
    assert [o["side"] for o in api.orders] == ["sell"] and len(api.posts) == 1
    assert KEY in state["positions"]                     # not booked as sold yet
    assert ev["event_id"] not in feed.seen               # the exit is not dropped
    assert all(isinstance(e, agent.ExitNotDone) for e in feed.errors)
    assert any("will not sell again until" in n for n in notes)
    api.down = False
    feed.poll(ev)
    assert len(api.posts) == 1 and api.held() == -2      # one sell of the 2 held
    assert state["positions"] == {} and ev["event_id"] in feed.seen
    assert [r["action"] for r in _log(home)][-1] == "exit"


def test_alpaca_sell_that_never_arrived_is_sent_again_under_the_same_id(
        home, no_policy, notes, monkeypatch):
    """An exit is never dropped: once Alpaca says the sell isn't there, it
    goes out again — under the SAME id, so if the first one shows up after
    all Alpaca refuses the repeat."""
    api = FakeAlpaca(monkeypatch, plan=["lost"])
    feed, state = _alpaca_feed(_held_alpaca_state())
    ev = make_event(event="EXITED", event_id="x-1")
    feed.poll(ev)
    assert api.orders == [] and KEY in state["positions"]
    feed.poll(ev)
    assert len(api.orders) == 1 and state["positions"] == {}
    assert api.posts[0]["client_order_id"] == api.posts[1]["client_order_id"]


def test_alpaca_a_later_sell_for_the_same_signal_is_a_new_order(
        home, no_policy, notes, monkeypatch):
    """A sell that got a clear answer but didn't sell everything is over;
    selling the rest must not be refused as a repeat of it."""
    api = FakeAlpaca(monkeypatch)
    api.fills = 1                                        # each sell fills 1
    state = _held_alpaca_state()
    ev = make_event(event="EXITED", event_id="x-1")
    broker_events.begin()
    assert alpaca.execute(alpaca.client(ALP_CFG), ALP_CFG, ev, state)
    assert state["positions"][KEY]["qty"] == 1
    assert alpaca.execute(alpaca.client(ALP_CFG), ALP_CFG, ev, state)
    broker_events.drain()
    assert state["positions"] == {} and len(api.orders) == 2
    assert api.posts[0]["client_order_id"] != api.posts[1]["client_order_id"]


def test_alpaca_exit_waits_while_its_buy_is_unconfirmed(home, no_policy, notes,
                                                       monkeypatch):
    """The exit signal arrives while the agent still can't tell whether the
    buy went through: it is kept, and sells once the buy is found."""
    api = FakeAlpaca(monkeypatch, plan=["cut"])
    feed, state = _alpaca_feed()
    buy, sell = make_event(), make_event(event="EXITED", event_id="x-1")
    feed.poll(buy)
    feed.poll(buy, sell)
    assert sell["event_id"] not in feed.seen and len(api.posts) == 1
    api.down = False
    feed.poll(buy, sell)
    assert api.held() == 0 and state["positions"] == {}
    assert sell["event_id"] in feed.seen


def test_alpaca_reconcile_is_free_when_nothing_is_unconfirmed(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("no network call expected")
    monkeypatch.setattr(alpaca.requests, "get", boom)
    monkeypatch.setattr(alpaca.requests, "post", boom)
    saved = []
    alpaca.reconcile(alpaca.client(ALP_CFG), ALP_CFG,
                     {"positions": {KEY: {"option_id": OCC, "qty": 1}}}, saved.append)
    assert saved == []


# ── Robinhood ───────────────────────────────────────────────────────────────

def _mcp(payload, is_error=False):
    return {"content": [{"type": "text", "text": json.dumps(payload)}],
            "isError": is_error}


class FakeRobinhood:
    """Robinhood's agentic MCP. `plan` scripts the next order requests:
    "ok", "timeout" (order taken, answer lost), "cut" (the same, and
    Robinhood can't be reached from then on), "lost" (never arrives).
    `down` fails every other call. show_ref_id=False lists orders without
    the ref_id the agent sent (the agent can't count on seeing it)."""

    def __init__(self, plan=(), show_ref_id=True):
        self.plan, self.orders, self.placed = list(plan), [], []
        self.down, self.show_ref_id = False, show_ref_id
        self.last_error = None
        self.dedupes_ref_id = False
        self.reports = True   # False behind the real client, which reports itself

    def call_tool(self, name, args):
        if name == "place_option_order":
            return self._place(dict(args))
        if self.down:
            self.last_error = "can't reach Robinhood (ConnectionError)"
            raise requests.ConnectionError("no route to host")
        self.last_error = None
        if name == "get_option_instruments":
            return _mcp({"data": {"results": [{"id": OPT, "chain_symbol": "SPY"}]}})
        if name == "get_option_quotes":
            return _mcp({"data": {"results": [{"ask_price": "2.50",
                                               "bid_price": "2.40"}]}})
        if name == "get_option_orders":
            want = args.get("order_id")
            rows = [self._row(o) for o in reversed(self.orders)
                    if not want or o["id"] == want]
            return _mcp({"data": {"orders": rows}})
        if name == "cancel_option_order":
            return _mcp({"ok": True})
        return _mcp({})

    def _row(self, o):
        return {k: v for k, v in o.items() if self.show_ref_id or k != "ref_id"}

    def _place(self, args):
        self.placed.append(args)
        mode = self.plan.pop(0) if self.plan else "ok"
        if mode == "lost":
            self._lost("ConnectionError")
            raise requests.ConnectionError("connection reset before sending")
        same = next((o for o in self.orders if o["ref_id"] == args["ref_id"]), None)
        if same and self.dedupes_ref_id:
            return _mcp({"data": {"id": same["id"]}})
        leg = dict(args["legs"][0])
        order = {"id": f"rh-{len(self.orders) + 1}", "ref_id": args["ref_id"],
                 "state": "filled", "type": args["type"],
                 "quantity": f"{int(args['quantity'])}.00000",
                 "processed_quantity": f"{int(args['quantity'])}.00000",
                 "placed_agent": "agentic",
                 "created_at": datetime.now(timezone.utc).isoformat()
                 .replace("+00:00", "Z"),
                 "legs": [leg]}
        self.orders.append(order)
        if mode in ("timeout", "cut"):
            self.down = mode == "cut"
            self._lost("ReadTimeout")
            raise requests.ReadTimeout("read timed out")
        self.last_error = None
        return _mcp({"data": {"id": order["id"]}})

    def _lost(self, why):
        """Like RobinhoodMCP._mark_down: note it and report it once."""
        if not self.last_error and self.reports:
            broker_events.report(
                "broker_disconnected", "Robinhood connection lost: can't reach "
                f"Robinhood ({why}). Trades can't be placed until it's back.")
        self.last_error = f"can't reach Robinhood ({why})"

    def held(self):
        return sum(int(float(o["processed_quantity"]))
                   * (1 if o["legs"][0]["side"] == "buy" else -1)
                   for o in self.orders)


RH_CFG = {"broker": "robinhood", "robinhood_account": "A1", "sizing_mode": "budget",
          "budget_per_trade_usd": 500, "max_entries_per_day": 5,
          "position_sync": False}


def _rh_feed(rh, state=None):
    state = state if state is not None else {"positions": {}, "seen": []}
    return Feed(robinhood, rh, RH_CFG, state), state


@pytest.mark.parametrize("show_ref_id", [True, False], ids=["ref-id", "no-ref-id"])
def test_robinhood_buy_timeout_never_buys_twice(home, no_policy, notes, show_ref_id):
    """THE BUG, on Robinhood: place_option_order timed out after Robinhood
    took the order; the next poll placed a second one under a new ref id."""
    rh = FakeRobinhood(plan=["timeout"], show_ref_id=show_ref_id)
    feed, state = _rh_feed(rh)
    ev = make_event()
    for _ in range(3):
        feed.poll(ev)
    assert len(rh.placed) == 1 and rh.held() == 2
    pos = state["positions"][KEY]
    assert pos["qty"] == 2 and "pending" not in pos     # ledger == account
    assert pos["order_ids"] == ["rh-1"]
    assert [r["action"] for r in _log(home)] == ["entry"]
    assert ev["event_id"] in feed.seen and not feed.errors
    assert notes == []          # the order is there: no "trade NOT placed" alert


def test_robinhood_buy_timeout_through_the_real_client(home, no_policy, notes,
                                                      tmp_path, monkeypatch):
    """The same, through the real MCP client over a fake HTTP server: the
    order request times out on the wire after Robinhood took the order."""
    from brokers import robinhood_mcp
    backend = FakeRobinhood(plan=["timeout"], show_ref_id=False)
    backend.reports = False

    class Resp:
        def __init__(self, status, body=None):
            self.status_code, self._body = status, body
            self.text = json.dumps(body) if body is not None else ""
            self.headers = {"Mcp-Session-Id": "s1"}

        def json(self):
            return self._body

    def post(url, json=None, headers=None, data=None, timeout=None):
        if url == robinhood_mcp.TOKEN_URL:
            return Resp(200, {"access_token": "new", "expires_in": 3600})
        call = json or {}
        if call.get("method") != "tools/call":
            return Resp(200, {"jsonrpc": "2.0", "id": call.get("id"), "result": {}})
        result = backend.call_tool(call["params"]["name"], call["params"]["arguments"])
        return Resp(200, {"jsonrpc": "2.0", "id": call.get("id"), "result": result})
    monkeypatch.setattr(robinhood_mcp.requests, "post", post)
    token = tmp_path / "robinhood_oauth.json"
    token.write_text(json.dumps({"access_token": "a", "refresh_token": "r",
                                 "client_id": "c", "expires_at": time.time() + 3600}),
                     encoding="utf-8")
    rh = robinhood_mcp.RobinhoodMCP(str(token))
    feed, state = _rh_feed(rh)
    ev = make_event()
    for _ in range(3):
        feed.poll(ev)
    assert len(backend.placed) == 1 and backend.held() == 2
    assert state["positions"][KEY]["qty"] == 2 and not feed.errors
    assert [r["action"] for r in _log(home)] == ["entry"]
    assert notes == [] and rh.last_error is None


def test_robinhood_buy_unknown_outcome_is_handled_once_and_the_owner_is_told(
        home, no_policy, notes):
    rh = FakeRobinhood(plan=["cut"])
    feed, state = _rh_feed(rh)
    ev = make_event()
    feed.poll(ev)
    assert ev["event_id"] in feed.seen and not feed.errors
    assert KEY not in state["positions"]
    (alert,) = notes            # one alert, and it doesn't claim "NOT placed"
    assert alert.startswith("agentic-trader: check this trade (SPY 2026-07-10")
    assert "can't tell whether the order went through" in alert
    assert "will NOT send this buy again" in alert and "Robinhood" in alert
    assert [r["action"] for r in _log(home)] == ["order_failed"]
    for _ in range(3):
        feed.poll(ev)
    assert len(rh.placed) == 1
    rh.down = False                                      # back: found and adopted
    feed.poll(ev)
    assert len(rh.placed) == 1
    pos = state["positions"][KEY]
    assert pos["qty"] == 2 and pos["opened_event"] == ev["event_id"]
    assert order_guard.get(state, KEY) is None
    feed.poll(make_event(event="EXITED", event_id="x-1"))
    assert rh.held() == 0 and state["positions"] == {}


def test_robinhood_buy_that_never_arrived_is_not_sent_again(home, no_policy, notes):
    rh = FakeRobinhood(plan=["lost"])
    feed, state = _rh_feed(rh)
    ev = make_event()
    for _ in range(3):
        feed.poll(ev)
    assert rh.orders == [] and len(rh.placed) == 1 and state["positions"] == {}
    assert len([n for n in notes if "check this trade" in n]) == 1


def test_robinhood_does_not_adopt_the_owners_own_order(home, no_policy, notes):
    """Without a ref_id on the row, only an order placed through the
    agent's API for this contract, side and size can be the lost one."""
    rh = FakeRobinhood(plan=["lost"], show_ref_id=False)
    rh.orders.append({"id": "owner-1", "ref_id": "x", "state": "filled",
                      "type": "market", "quantity": "2.00000",
                      "processed_quantity": "2.00000", "placed_agent": "user",
                      "created_at": datetime.now(timezone.utc).isoformat(),
                      "legs": [{"option_id": OPT, "side": "buy",
                                "position_effect": "open"}]})
    feed, state = _rh_feed(rh)
    feed.poll(make_event())
    assert state["positions"] == {} and len(rh.placed) == 1


def test_robinhood_same_signal_always_sends_the_same_ref_id(home, no_policy, notes):
    rh = FakeRobinhood()
    rh.dedupes_ref_id = True
    ev = make_event()
    feed, _ = _rh_feed(rh)
    feed.poll(ev)
    fresh, state2 = _rh_feed(rh)                         # memory lost
    fresh.poll(ev)
    assert len(rh.orders) == 1
    assert rh.placed[0]["ref_id"] == rh.placed[1]["ref_id"]
    assert state2["positions"][KEY]["qty"] == 2
    other, _ = _rh_feed(rh)
    other.poll(make_event(event_id="test-2", strike=753.0))
    assert rh.placed[2]["ref_id"] != rh.placed[0]["ref_id"]


def _held_rh_state():
    return {"positions": {KEY: {"option_id": OPT, "qty": 2, "order_ids": ["b-0"],
                                "opened_event": "test-1"}}, "seen": []}


def test_robinhood_sell_timeout_is_adopted_not_repeated(home, no_policy, notes):
    rh = FakeRobinhood(plan=["timeout"])
    feed, state = _rh_feed(rh, _held_rh_state())
    ev = make_event(event="EXITED", event_id="x-1")
    for _ in range(3):
        feed.poll(ev)
    assert len(rh.placed) == 1 and rh.held() == -2
    assert state["positions"] == {} and ev["event_id"] in feed.seen
    assert [r["action"] for r in _log(home)] == ["exit"]


def test_robinhood_a_later_sell_for_the_same_signal_gets_a_new_ref_id(
        home, no_policy, notes, monkeypatch):
    rh = FakeRobinhood()
    state = _held_rh_state()
    ev = make_event(event="EXITED", event_id="x-1")
    # the first sell dies with nothing filled; the retry is a new order
    monkeypatch.setattr(robinhood, "_order_status", lambda rh_, a, oid: ("cancelled", 0))
    broker_events.begin()
    robinhood.execute(rh, RH_CFG, ev, state)
    robinhood.execute(rh, RH_CFG, ev, state)
    broker_events.drain()
    assert len(rh.placed) == 2
    assert rh.placed[0]["ref_id"] != rh.placed[1]["ref_id"]


def test_robinhood_reconcile_replacement_timeout_adopts_the_new_order(monkeypatch):
    """The opening-blackout limit is cancelled and re-placed as a market
    order; that request times out after Robinhood took it. The new order
    is the position's order now — not an orphan the ledger never sells."""
    rh = FakeRobinhood(plan=["timeout"])
    rh.orders.append({"id": "o-1", "ref_id": "r-1", "state": "cancelled",
                      "type": "limit", "quantity": "2.00000",
                      "processed_quantity": "0.00000", "placed_agent": "agentic",
                      "created_at": datetime.now(timezone.utc).isoformat(),
                      "legs": [{"option_id": OPT, "side": "buy",
                                "position_effect": "open"}]})
    monkeypatch.setattr(robinhood, "_cancel_order", lambda rh_, a, oid: True)
    statuses = iter([("confirmed", 0), ("cancelled", 0)])
    real = robinhood._order_status
    monkeypatch.setattr(robinhood, "_order_status",
                        lambda rh_, a, oid: next(statuses, None) or real(rh_, a, oid))
    state = {"positions": {KEY: {
        "option_id": OPT, "qty": 0, "opened_event": "test-1", "order_ids": ["o-1"],
        "pending": {"order_id": "o-1", "side": "buy", "requested": 2, "filled": 0,
                    "was_limit": True, "placed_at": time.time()}}},
        "agent_order_ids": ["o-1"]}
    broker_events.begin()
    robinhood.reconcile(rh, RH_CFG, state, lambda s: None)
    robinhood.reconcile(rh, RH_CFG, state, lambda s: None)
    broker_events.drain()
    assert len(rh.placed) == 1
    pos = state["positions"][KEY]
    assert pos["qty"] == 2 and "pending" not in pos
    assert "rh-2" in pos["order_ids"]


def test_robinhood_an_old_order_under_the_same_ref_id_is_not_taken_over(
        home, no_policy, notes):
    rh = FakeRobinhood(plan=["ok", "lost"])
    ev = make_event()
    first, _ = _rh_feed(rh)
    first.poll(ev)
    rh.orders[0]["created_at"] = "2026-01-05T15:00:00.123456Z"          # long ago
    again, state = _rh_feed(rh)                          # memory lost since
    again.poll(ev)
    assert len(rh.orders) == 1 and state["positions"] == {}


def test_broker_timestamps_parse_on_every_supported_python():
    near = lambda v: pytest.approx(v, abs=1e-5)
    assert order_guard.epoch("2026-10-08T17:27:05Z") == 1791480425.0
    assert order_guard.epoch("2026-10-08T17:27:05.074736Z") == near(1791480425.074736)
    assert order_guard.epoch("2026-10-08T17:27:05.123456789Z") == near(1791480425.123456)
    assert order_guard.epoch("2026-10-08T17:27:05.5+00:00") == 1791480425.5
    assert order_guard.epoch("2026-10-08T13:27:05-04:00") == 1791480425.0
    for bad in (None, "", "soon", 5):
        assert order_guard.epoch(bad) is None


# ── the key itself ──────────────────────────────────────────────────────────

def test_order_key_is_stable_per_signal_and_distinct_otherwise():
    ev = make_event()
    k = order_guard.order_key("A1", ev, "buy")
    assert k == order_guard.order_key("A1", dict(ev), "buy")
    assert len({k,
                order_guard.order_key("A2", ev, "buy"),
                order_guard.order_key("A1", ev, "sell"),
                order_guard.order_key("A1", ev, "sell", 1),
                order_guard.order_key("A1", make_event(event_id="t-2"), "buy")}) == 5
    assert len(k) == 36                                  # a UUID: valid as both ids
    # no event id (a reconcile re-placement): a fresh id every time
    assert order_guard.order_key("A1", {}, "buy") != order_guard.order_key("A1", {}, "buy")


# ── refusals stay refusals; the alerts fit and read plainly ─────────────────

def test_alpaca_plain_refusal_is_not_treated_as_unknown(home, no_policy, notes,
                                                       monkeypatch):
    """HTTP 403 (not enough buying power) is a clear "no": reported as a
    rejected order as before, nothing remembered, no lookup."""
    api = FakeAlpaca(monkeypatch)
    api.post = lambda url, **kw: _Resp(403, {"message": "insufficient buying power"})
    monkeypatch.setattr(alpaca.requests, "post", api.post)
    lookups = []
    real_get = api.get

    def get(url, **kw):
        lookups.append(url)
        return real_get(url, **kw)
    monkeypatch.setattr(alpaca.requests, "get", get)
    feed, state = _alpaca_feed()
    feed.poll(make_event())
    assert order_guard.unconfirmed(state) == {} and state["positions"] == {}
    assert not [u for u in lookups if "by_client_order_id" in u]
    assert "trade NOT placed" in notes[0] and "rejected the buy order" in notes[0]


def test_robinhood_refusals_are_not_treated_as_unknown(home, no_policy, notes):
    """A tool error, a login that didn't get through, and an HTTP 4xx are
    clear answers: nothing went in."""
    for answer, attrs in (
            (_mcp({"detail": "Not enough buying power."}, is_error=True), {}),
            (None, {"last_error": "couldn't open a Robinhood session"}),
            (None, {"_last_status": ("rpc", 429)})):
        rh = FakeRobinhood()

        def refuse(args, rh=rh, answer=answer, attrs=attrs):
            for name, value in attrs.items():
                setattr(rh, name, value)
            return answer
        rh._place = refuse
        state = {"positions": {}}
        broker_events.begin()
        assert not robinhood.execute(rh, RH_CFG, make_event(), state)
        (ev,) = broker_events.drain()
        assert ev["kind"] == "order_failed" and not ev.get("placed")
        assert order_guard.unconfirmed(state) == {}


def test_robinhood_unreadable_answer_is_unknown_not_a_refusal(home, no_policy, notes):
    """Robinhood was reached but the reply can't be read (a gateway error
    after the order went in): the order is looked up, not assumed absent."""
    rh = FakeRobinhood()
    real = rh._place

    def garbled(args):
        real(args)
        return None
    rh._place = garbled
    feed, state = _rh_feed(rh)
    feed.poll(make_event())
    feed.poll(make_event())
    assert len(rh.placed) == 1 and state["positions"][KEY]["qty"] == 2


def test_robinhood_exit_waits_while_its_buy_is_unconfirmed(home, no_policy, notes):
    rh = FakeRobinhood(plan=["cut"])
    feed, state = _rh_feed(rh)
    buy, sell = make_event(), make_event(event="EXITED", event_id="x-1")
    feed.poll(buy)
    feed.poll(buy, sell)
    assert sell["event_id"] not in feed.seen and len(rh.placed) == 1
    assert "its exit signal waits" in str(feed.errors[-1])
    rh.down = False
    feed.poll(buy, sell)
    assert rh.held() == 0 and state["positions"] == {}
    assert sell["event_id"] in feed.seen


def test_robinhood_sell_with_no_answer_goes_out_again_and_cannot_double_count(
        home, no_policy, notes):
    """A Robinhood sell can only close what the account holds, so an exit
    is never held back: the request that never arrived is sent again."""
    rh = FakeRobinhood(plan=["lost"])
    feed, state = _rh_feed(rh, _held_rh_state())
    ev = make_event(event="EXITED", event_id="x-1")
    feed.poll(ev)
    assert KEY in state["positions"] and ev["event_id"] not in feed.seen
    assert any("can only close contracts you hold" in n for n in notes)
    feed.poll(ev)
    assert len(rh.orders) == 1 and state["positions"] == {}
    assert rh.placed[0]["ref_id"] == rh.placed[1]["ref_id"]


def test_reconcile_replacement_with_unknown_outcome_is_remembered(monkeypatch):
    rh = FakeRobinhood(plan=["cut"])
    monkeypatch.setattr(robinhood, "_cancel_order", lambda rh_, a, oid: True)
    statuses = iter([("confirmed", 0), ("cancelled", 0)])
    real = robinhood._order_status
    monkeypatch.setattr(robinhood, "_order_status",
                        lambda rh_, a, oid: next(statuses, None) or real(rh_, a, oid))
    state = {"positions": {KEY: {
        "option_id": OPT, "qty": 0, "opened_event": "test-1", "order_ids": ["o-1"],
        "pending": {"order_id": "o-1", "side": "buy", "requested": 2, "filled": 0,
                    "was_limit": True, "placed_at": time.time()}}},
        "agent_order_ids": ["o-1"]}
    broker_events.begin()
    robinhood.reconcile(rh, RH_CFG, state, lambda s: None)
    kinds = [(e["kind"], e.get("placed")) for e in broker_events.drain()]
    assert kinds == [("order_failed", True)]             # not "never filled"
    assert order_guard.get(state, KEY)["side"] == "buy" and len(rh.placed) == 1
    rh.down = False
    broker_events.begin()
    robinhood.reconcile(rh, RH_CFG, state, lambda s: None)
    broker_events.drain()
    assert len(rh.placed) == 1 and state["positions"][KEY]["qty"] == 2
    assert state["positions"][KEY]["opened_event"] == "test-1"


def test_owner_messages_fit_the_report_and_give_no_wrong_hint():
    """broker_events keeps 400 characters of a message, and attaches a
    "Fix:" line when the words look like a known problem (cash, login…):
    these must arrive whole and without a misleading fix."""
    contract = "GOOGL 2026-12-18 $1234.5 CALL"
    lost = order_guard.lost_text("Robinhood", "sell", 100, contract)
    texts = [lost + robinhood._BUY_NOT_AGAIN, lost + robinhood._SELL_AGAIN,
             lost + alpaca._SELL_WAITS,
             f"the agent still can't tell whether its sell order for {contract} "
             "went through." + alpaca._SELL_WAITS]
    for text in texts:
        assert len(text) <= 400, len(text)
        assert broker_events.hint(text) == ""
        assert not broker_events.is_cash_problem(text)


def test_a_damaged_note_is_dropped_not_checked_forever(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("no network call expected")
    monkeypatch.setattr(alpaca.requests, "get", boom)
    for mod, client in ((alpaca, alpaca.client(ALP_CFG)), (robinhood, FakeRobinhood())):
        state = {"positions": {}, order_guard.STATE_KEY: {KEY: "garbage"}}
        saved = []
        mod.reconcile(client, ALP_CFG if mod is alpaca else RH_CFG, state, saved.append)
        assert order_guard.STATE_KEY not in state and len(saved) == 1
        mod.reconcile(client, ALP_CFG if mod is alpaca else RH_CFG, state, saved.append)
        assert len(saved) == 1


def test_state_from_the_current_release_needs_no_migration(home, no_policy, notes,
                                                         monkeypatch):
    """An upgraded install: rows written before this change have no
    sell_seq and the state has no unconfirmed_orders. Nothing is re-traded,
    and the open position still exits normally."""
    api = FakeAlpaca(monkeypatch)
    state = {"positions": {KEY: {"option_id": OCC, "qty": 2, "order_ids": ["old-1"],
                                 "opened_event": "test-1", "last_order_at": 1.0}},
             "seen": ["test-1"], "agent_order_ids": ["old-1"]}
    feed, _ = _alpaca_feed(state)
    feed.seen.add("test-1")
    feed.poll(make_event())                              # the old entry: seen
    assert api.posts == [] and state["positions"][KEY]["qty"] == 2
    feed.poll(make_event(event="EXITED", event_id="x-1"))
    assert [o["side"] for o in api.orders] == ["sell"] and state["positions"] == {}
