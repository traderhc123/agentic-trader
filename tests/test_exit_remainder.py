"""Exit remainders and the ledger check.

An exit that leaves contracts held (a partial fill, an order that died
unfilled or was cancelled with quantity left) is re-sold by the agent on the
next loop(s) — marketable limit, sell-to-close, never more than the ledger
remainder and never more than the account holds. Contracts the agent bought
that the account holds but the ledger lost are re-added (only when the
agent's own filled orders prove they're its own). Real `agent.py run` loop
paths with fake brokers. Zero network."""

import json
from datetime import datetime

import pytest

import agent
import broker_events
from brokers import alpaca, moomoo, robinhood
from brokers.pricing import exit_qty

TZ = agent.MARKET_TZ
U1 = "14a8e49b-0f10-4d64-9cf9-bd1a68e9da44"
U2 = "6f3c1e0a-1111-4a4a-9b9b-222233334444"
KEY = "RIVN|2026-11-06|13.5|C"           # expires Friday 2026-11-06
LATER = "RIVN|2026-11-13|13.5|C"
CONTRACT = "RIVN 2026-11-06 $13.5 C"
TAG = "fake:"                            # account_tag() of the "fake" broker


def et(h, m, day=(2026, 11, 5), s=0):
    return datetime(*day, h, m, s, tzinfo=TZ)


def _mcp(payload):
    return {"content": [{"type": "text", "text": json.dumps(payload)}]}


class Client:
    last_error = None


class FakeBroker:
    """A Robinhood-like adapter (close-only exits) with the read hooks.
    `held` is what the account holds per option id (None = unreadable);
    each sell fills the next `plan` entry (default: everything asked)."""
    CLOSE_ONLY_EXITS = True

    def __init__(self, held=None, fills=(), plan=()):
        self.held = held
        self.fills = list(fills)
        self.plan = list(plan)
        self.sells, self.asked = [], []

    def client(self, cfg):
        return Client()

    def execute(self, client, cfg, event, state):
        key = f"{event['ticker']}|{event['expiry']}|{event['strike']}|{event['type']}"
        pos = state["positions"].get(key)
        if event["event"] != "EXITED" or not pos:
            return False
        q = exit_qty(pos, event)
        filled = min(q, self.plan.pop(0)) if self.plan else q
        self.sells.append({"event_id": event["event_id"], "qty": q, "filled": filled,
                           "limit": event.get("exit_limit"),
                           "max_qty": event.get("max_qty")})
        oid = f"s{len(self.sells)}"
        if filled:  # the order history lists the agent's own sale
            self.fills.insert(0, {"order_id": oid, "side": "sell", "qty": filled,
                                  "at": agent._iso_at(agent._now())})
        state["agent_order_ids"] = state.get("agent_order_ids", []) + [oid]
        pos["order_ids"] = pos.get("order_ids", []) + [oid]
        pos["last_order_at"] = agent._now()  # holdings lag fresh orders
        if self.held is not None:
            self.held[pos["option_id"]] = self.held.get(pos["option_id"], 0) - filled
        if filled >= pos["qty"]:
            state["positions"].pop(key)
        else:
            pos["qty"] -= filled
            if filled < q:
                broker_events.report("exit_partial", f"exit filled {filled}/{q}")
        return True

    def held_option_qty(self, client, cfg, option_ids):
        self.asked.append(sorted(option_ids))
        if self.held is None:
            return None
        return {i: max(0, self.held.get(i, 0)) for i in option_ids}

    def closed_fills(self, client, cfg, option_id, since_iso=None, own_order_ids=(),
                     exclude_agentic=False):
        own = {str(o) for o in own_order_ids}
        return [{"order_id": f["order_id"], "qty": f["qty"], "price": 0.5,
                 "at": f.get("at"), "by": "user"}
                for f in self.fills if f["side"] == "sell" and f["order_id"] not in own]

    def option_fills(self, client, cfg, option_id, since_iso=None):
        return list(self.fills)


class NoHistory(FakeBroker):
    option_fills = None  # an adapter without order history


class HistoryDown(FakeBroker):
    def option_fills(self, client, cfg, option_id, since_iso=None):
        return None  # order history can't be read right now


@pytest.fixture
def notes(monkeypatch):
    sent = []
    monkeypatch.setattr(agent, "notify",
                        lambda cfg, msg, priority=None: sent.append((msg, priority)))
    monkeypatch.setattr(agent, "_problems_logged", set())
    return sent


def _loop(home, monkeypatch, state, broker, times, events=None, cfg=None,
          upkeep=False, broker_name="fake", rush=False):
    """`agent.py run` for len(times) loops; loop i runs at ET times[i] (the
    agent's clock follows). events: {loop index: [feed events]}. rush=True
    keeps the real 9:28-9:40 open-rush window. Returns the saved state."""
    import brokers
    import keep_running
    import webui
    i = {"n": 0}

    class Source:
        @staticmethod
        def poll(cfg, st, save_state):
            return list((events or {}).get(i["n"], []))

    def sleep(wait, controls):
        if i["n"] + 1 >= len(times):
            raise KeyboardInterrupt
        i["n"] += 1

    def no_dashboard(*a, **k):
        raise OSError("no dashboard in tests")
    monkeypatch.setattr(agent, "_now", lambda: times[i["n"]].timestamp())
    monkeypatch.setattr(agent, "_et_now", lambda: times[i["n"]])
    monkeypatch.setattr(agent, "require_consent_or_exit", lambda: None)
    monkeypatch.setattr(brokers, "broker_ready", lambda c: True)
    monkeypatch.setitem(agent.BROKERS, broker_name, broker)
    monkeypatch.setitem(agent.SOURCES, "stub", Source)
    monkeypatch.setattr(webui, "start_dashboard", no_dashboard)
    monkeypatch.setattr(keep_running, "refresh_autostart", lambda: False)
    monkeypatch.setattr(agent, "_refresh_cash", lambda *a, **k: None)
    monkeypatch.setattr(agent, "_maybe_daily_digest", lambda *a, **k: None)
    monkeypatch.setattr(agent, "_market_open_now", lambda: True)
    if not rush:
        monkeypatch.setattr(agent, "_in_open_rush", lambda: False)
    monkeypatch.setitem(agent._POS_SYNC, "next_at", 0.0)
    monkeypatch.setitem(agent._LEDGER_CHECK, "next_at", 0.0)
    monkeypatch.setitem(agent._REST_READ, "next_at", 0.0)
    if not upkeep:
        monkeypatch.setattr(agent, "_position_upkeep", lambda *a, **k: None)
    monkeypatch.setitem(webui.CONTROLS, "paused", False)
    monkeypatch.setitem(webui.CONTROLS, "stop", False)
    monkeypatch.setattr(agent, "_sleep_unless_stopped", sleep)
    (home / "config.json").write_text(json.dumps(
        {"source": "stub", "broker": broker_name, **(cfg or {})}))
    (home / "state.json").write_text(json.dumps(state))
    agent.cmd_run()
    return json.loads((home / "state.json").read_text())


def _log():
    try:
        with open(agent.TRADES_PATH, encoding="utf-8") as f:
            return [json.loads(x) for x in f if x.strip()]
    except OSError:
        return []


def _rest_state(key=KEY, qty=6, **over):
    """A ledger row the agent was exiting: `qty` contracts left."""
    t = et(9, 0).timestamp() - 3600
    row = {"option_id": U1, "qty": qty, "acct": TAG, "opened_event": "open-1",
           "opened_at": t, "last_order_at": t, "entry_mark": 1.0,
           "order_ids": ["buy-1"],
           "exiting": {"at": t, "cause": "signal", "event_id": "feed-x"}}
    row.update(over)
    return {"seen": [], "positions": {key: row}, "agent_order_ids": ["buy-1"],
            "position_sync": {"proven": {TAG: "2026-11-01T15:00:00+00:00"}}}


def _texts(notes, part):
    return [m for m, _ in notes if part in m]


# ── 1. 10 sold 4: the remaining 6 are re-sold on the next loop ──────────────

class ScriptRH:
    """Robinhood's MCP, scripted: each placed order fills the next `plan`
    entry and is then cancelled (or filled when it's everything)."""
    last_error = None

    def __init__(self, held, plan):
        self.held, self.plan = held, list(plan)
        self.orders, self.placed = {}, []

    def call_tool(self, name, args):
        if name == "place_option_order":
            oid = f"rh-{len(self.placed) + 1}"
            self.placed.append(dict(args))
            q = int(args["quantity"])
            f = min(q, self.plan.pop(0)) if self.plan else q
            self.held -= f
            self.orders[oid] = {"id": oid, "state": "filled" if f >= q else "cancelled",
                                "processed_quantity": str(f)}
            return _mcp({"data": {"id": oid}})
        if name == "get_option_orders":
            oid = args.get("order_id")
            rows = [self.orders[oid]] if oid in self.orders else list(self.orders.values())
            return _mcp({"data": {"orders": rows}})
        if name == "get_option_positions":
            return _mcp({"data": {"positions": [
                {"option_id": U1, "quantity": f"{self.held}.0000", "type": "long"}]}})
        if name == "get_option_quotes":
            return _mcp({"data": {"results": [{"bid_price": "1.00", "ask_price": "1.10",
                                               "mark_price": "1.05"}]}})
        if name == "get_option_instruments":
            return _mcp({"data": {"results": [{"id": U1, "chain_symbol": "RIVN"}]}})
        if name == "cancel_option_order":
            return _mcp({"ok": True})
        return {"content": []}


def test_robinhood_10_sold_4_the_remaining_6_resold_next_loop(home, monkeypatch,
                                                               notes, capsys):
    rh = ScriptRH(held=10, plan=[4])
    monkeypatch.setattr(robinhood, "client", lambda cfg: rh)
    monkeypatch.setattr(robinhood.time, "sleep", lambda s: None)
    monkeypatch.setattr(robinhood, "_in_opening_blackout", lambda now=None: False)
    monkeypatch.setattr(robinhood, "_before_close", lambda now=None, cfg=None: True)
    from brokers import account_tag
    cfg = {"robinhood_account": "A1"}
    tag = account_tag({"broker": "robinhood", **cfg})
    t = et(15, 0).timestamp()
    state = {"seen": [], "positions": {KEY: {
        "option_id": U1, "qty": 10, "acct": tag, "opened_event": "open-1",
        "opened_at": t - 86400, "last_order_at": t - 86400, "entry_mark": 1.2,
        "order_ids": ["rh-0"]}}, "agent_order_ids": ["rh-0"],
        "position_sync": {"proven": {tag: "2026-11-01T15:00:00+00:00"}}}
    feed_exit = {"event": "EXITED", "ticker": "RIVN", "expiry": "2026-11-06",
                 "strike": 13.5, "type": "C", "event_id": "feed-x"}
    saved = _loop(home, monkeypatch, state, robinhood,
                  [et(15, 55), et(15, 55, s=30), et(15, 56)],
                  events={0: [feed_exit]}, cfg=cfg, broker_name="robinhood")
    first, second = rh.placed
    assert first["quantity"] == "10" and first["type"] == "market"
    assert second["quantity"] == "6" and second["type"] == "limit"
    assert second["price"] == "0.95"                      # bid 1.00 - 5%
    assert second["legs"][0]["position_effect"] == "close"
    assert saved["positions"] == {} and rh.held == 0
    out = capsys.readouterr().out
    assert ("exit only filled 4/10 — 6x still held "
            f"({KEY}); re-selling the remaining 6x next loop") in out
    (found,) = _texts(notes, "didn't sell everything")
    assert found.startswith(f"⚠️ agentic-trader: the agent's exit for {CONTRACT} "
                            "didn't sell everything — 6x still held.")
    assert "re-sells the remaining 6x next loop" in found
    (done,) = _texts(notes, "nothing left to sell")
    assert "remaining contracts from the agent's exit are sold" in done
    exits = [r for r in _log() if r.get("action") == "exit"]
    assert [(r["qty"], r.get("cause")) for r in exits] == [(4, None), (6, "exit_remainder")]
    assert exits[0]["partial"] is True and exits[0]["remaining"] == 6
    partial = [r for r in _log() if r.get("action") == "exit_partial"]
    assert len(partial) == 1 and "re-sells the remaining 6x next loop" in partial[0]["reason"]
    assert not [m for m, _ in notes if "NOT placed" in m]  # no misleading alert


def test_robinhood_remainder_left_working_is_not_sold_twice(home, monkeypatch, notes):
    """The re-sell is still working: nothing else is sent for that row."""
    rh = ScriptRH(held=6, plan=[0])
    rh_place = rh.call_tool

    def working(name, args):
        out = rh_place(name, args)
        for o in rh.orders.values():
            o["state"] = "confirmed"          # still working, nothing filled
        return out
    rh.call_tool = working
    monkeypatch.setattr(robinhood, "client", lambda cfg: rh)
    monkeypatch.setattr(robinhood.time, "sleep", lambda s: None)
    monkeypatch.setattr(robinhood, "_in_opening_blackout", lambda now=None: False)
    monkeypatch.setattr(robinhood, "_before_close", lambda now=None, cfg=None: False)
    from brokers import account_tag
    cfg = {"robinhood_account": "A1"}
    st = _rest_state(acct=account_tag({"broker": "robinhood", **cfg}))
    saved = _loop(home, monkeypatch, st, robinhood,
                  [et(14, 0), et(14, 0, s=30), et(14, 1)], cfg=cfg,
                  broker_name="robinhood")
    assert len(rh.placed) == 1
    assert saved["positions"][KEY]["pending"]["side"] == "sell"


# ── 2. never more than the account holds ────────────────────────────────────

BOUGHT_10 = {"order_id": "buy-1", "side": "buy", "qty": 10,
             "at": "2026-11-04T15:00:00+00:00"}
SOLD_4 = {"order_id": "sell-0", "side": "sell", "qty": 4,
          "at": "2026-11-05T15:00:00+00:00"}


def test_remainder_capped_by_holdings(home, monkeypatch, notes):
    """Holds 4 of the 6 left; the agent's own fills (bought 10, 4 sold)
    account for 6, so 4 are sold."""
    b = FakeBroker(held={U1: 4}, fills=[SOLD_4, BOUGHT_10])
    saved = _loop(home, monkeypatch, _rest_state(qty=6), b,
                  [et(14, 0), et(14, 0, s=30), et(14, 1)])
    assert [(s["qty"], s["max_qty"], s["limit"]) for s in b.sells] == [
        (4, 4, "marketable")]
    assert saved["positions"][KEY]["qty"] == 2          # never sold: not held


@pytest.mark.parametrize("fills,sold", [
    ([], []),                                            # no agent buy listed
    ([{"order_id": "u-1", "side": "buy", "qty": 4,       # only the owner's buy
       "at": "2026-11-05T15:00:00+00:00"}], []),
    (None, []),                                          # history unreadable
    ([BOUGHT_10, {"order_id": "sell-0", "side": "sell", "qty": 8,
                  "at": "2026-11-05T15:00:00+00:00"}], [(2, 2)]),  # only 2 are its
], ids=["no-agent-buy", "owner-buy", "unreadable", "partly-its-own"])
def test_shortfall_sells_only_what_the_agents_fills_account_for(
        home, monkeypatch, notes, fills, sold):
    """The account holds fewer than the ledger's remainder: the agent sells
    only what its own filled orders account for (never the owner's)."""
    class B(FakeBroker):
        def option_fills(self, client, cfg, option_id, since_iso=None):
            return None if fills is None else self.fills + list(fills)
    b = B(held={U1: 4})
    saved = _loop(home, monkeypatch, _rest_state(qty=6), b,
                  [et(14, 0), et(14, 0, s=30), et(14, 1), et(14, 2)])
    assert [(s["qty"], s["max_qty"]) for s in b.sells] == sold
    assert saved["positions"][KEY]["qty"] == 6 - sum(q for q, _ in sold)


def test_feed_event_cannot_carry_a_cap_or_a_marketable_limit(home, monkeypatch, notes):
    """max_qty / exit_limit / exit_cause are the agent's own: stripped from
    feed events, so a feed exit sells the ledger row as before."""
    t = et(9, 0).timestamp() - 3600
    st = {"seen": [], "agent_order_ids": ["buy-1"],
          "position_sync": {"proven": {TAG: "2026-11-01T15:00:00+00:00"}},
          "positions": {LATER: {"option_id": U1, "qty": 6, "acct": TAG,
                                "opened_event": "open-1", "opened_at": t,
                                "last_order_at": t, "order_ids": ["buy-1"]}}}
    b = FakeBroker(held={U1: 6})
    feed_exit = {"event": "EXITED", "ticker": "RIVN", "expiry": "2026-11-13",
                 "strike": 13.5, "type": "C", "event_id": "feed-q", "max_qty": 2,
                 "exit_limit": "marketable", "exit_cause": "exit_remainder"}
    saved = _loop(home, monkeypatch, st, b, [et(14, 0)], events={0: [feed_exit]})
    assert [(s["qty"], s["max_qty"], s["limit"]) for s in b.sells] == [(6, None, None)]
    assert saved["positions"] == {}
    (row,) = [r for r in _log() if r.get("action") == "exit"]
    assert "cause" not in row


def test_remainder_never_sells_more_than_the_ledger(home, monkeypatch, notes):
    b = FakeBroker(held={U1: 9})                        # the owner holds more
    saved = _loop(home, monkeypatch, _rest_state(qty=6), b, [et(14, 0), et(14, 1)])
    assert [s["qty"] for s in b.sells] == [6] and b.sells[0]["max_qty"] is None
    assert KEY not in saved["positions"]


def test_unreadable_holdings_wait_then_close_only_sells_ledger_qty(
        home, monkeypatch, notes):
    b = FakeBroker(held=None)
    times = [et(14, 0), et(14, 5), et(14, 9, s=59), et(14, 10)]
    _loop(home, monkeypatch, _rest_state(qty=6), b, times)
    assert [s["qty"] for s in b.sells] == [6]           # only after 10 min


def test_unreadable_holdings_plain_sell_adapter_never_sells_blind(
        home, monkeypatch, notes):
    class Plain(FakeBroker):
        CLOSE_ONLY_EXITS = False
    b = Plain(held=None)
    _loop(home, monkeypatch, _rest_state(qty=6), b,
          [et(14, 0), et(14, 11), et(14, 30)])
    assert b.sells == []


# ── 3. no double sell with the guard or a feed exit ─────────────────────────

@pytest.mark.parametrize("at,seller", [((15, 46), "expiry-guard"),
                                       ((15, 30), "exit-rest")])
def test_remainder_and_expiry_guard_sell_once(home, monkeypatch, notes, at, seller):
    b = FakeBroker(held={U1: 6})
    day = (2026, 11, 6)
    saved = _loop(home, monkeypatch, _rest_state(qty=6), b,
                  [et(*at, day=day), et(*at, day=day, s=10), et(*at, day=day, s=20)])
    assert [s["qty"] for s in b.sells] == [6]
    assert b.sells[0]["event_id"].startswith(seller)
    assert saved["positions"] == {}


def test_remainder_and_feed_exit_in_one_loop_sell_once(home, monkeypatch, notes):
    b = FakeBroker(held={U1: 6})
    feed_exit = {"event": "EXITED", "ticker": "RIVN", "expiry": "2026-11-13",
                 "strike": 13.5, "type": "C", "event_id": "feed-y"}
    saved = _loop(home, monkeypatch, _rest_state(key=LATER, qty=6), b,
                  [et(14, 0), et(14, 0, s=30)], events={0: [feed_exit], 1: [feed_exit]})
    assert [s["qty"] for s in b.sells] == [6]
    assert saved["positions"] == {}


def test_partial_feed_exit_resold_next_loop_not_the_same_one(home, monkeypatch, notes):
    t = et(9, 0).timestamp() - 3600
    st = {"seen": [], "agent_order_ids": ["buy-1"],
          "position_sync": {"proven": {TAG: "2026-11-01T15:00:00+00:00"}},
          "positions": {LATER: {"option_id": U1, "qty": 10, "acct": TAG,
                                "opened_event": "open-1", "opened_at": t,
                                "last_order_at": t, "order_ids": ["buy-1"]}}}
    b = FakeBroker(held={U1: 10}, plan=[4])
    feed_exit = {"event": "EXITED", "ticker": "RIVN", "expiry": "2026-11-13",
                 "strike": 13.5, "type": "C", "event_id": "feed-z"}
    saved = _loop(home, monkeypatch, st, b, [et(14, 0)], events={0: [feed_exit]})
    assert [s["qty"] for s in b.sells] == [10]          # nothing more this loop
    assert saved["positions"][LATER]["qty"] == 6
    assert saved["positions"][LATER]["exiting"]["event_id"] == "feed-z"
    b2 = FakeBroker(held={U1: 6})
    saved = _loop(home, monkeypatch, saved, b2, [et(14, 0, s=30)])
    assert [(s["qty"], s["limit"]) for s in b2.sells] == [(6, "marketable")]
    assert saved["positions"] == {}


# ── 4. the owner closed the rest ────────────────────────────────────────────

def test_owner_closed_the_remainder_nothing_sold(home, monkeypatch, notes):
    t0 = et(14, 0)
    fill = {"order_id": "owner-1", "side": "sell", "qty": 6,
            "at": datetime.fromtimestamp(t0.timestamp() - 600, TZ).isoformat()}
    b = FakeBroker(held={U1: 0}, fills=[fill])
    saved = _loop(home, monkeypatch, _rest_state(qty=6), b,
                  [t0, et(14, 1), et(14, 2), et(14, 3)], upkeep=True)
    assert b.sells == []
    assert KEY not in saved["positions"]
    assert saved["closed_outside"][KEY]["cause"] == "sold"
    (done,) = _texts(notes, "nothing left to sell")
    assert "closed outside the agent" in done


# ── 5. after the close: resumes at the next open ────────────────────────────

def test_after_hours_remainder_resumes_next_morning(home, monkeypatch, notes):
    b = FakeBroker(held={U1: 6})
    nxt = (2026, 11, 6)
    saved = _loop(home, monkeypatch, _rest_state(key=LATER, qty=6), b,
                  [et(16, 5), et(9, 25, day=nxt), et(9, 29, day=nxt, s=50),
                   et(9, 30, day=nxt), et(9, 30, day=nxt, s=30)])
    assert len(b.sells) == 1 and b.sells[0]["limit"] == "marketable"
    assert saved["positions"] == {}
    (found,) = _texts(notes, "didn't sell everything")
    assert "The market is closed — the agent re-sells them after the 9:30 ET open" in found


def test_paused_agent_notifies_but_does_not_resell(home, monkeypatch, notes):
    b = FakeBroker(held={U1: 6})
    st = _rest_state(qty=6)
    monkeypatch.setattr(agent, "_now", lambda: et(14, 0).timestamp())
    for m in (0, 1, 2):
        agent.exit_remainder_tick({"broker": "fake"}, st, lambda s: None, b,
                                  Client(), paused=True, now_et=et(14, m))
    assert b.sells == [] and b.asked == []
    (found,) = _texts(notes, "didn't sell everything")
    assert "The agent is paused — it re-sells them once you resume." in found


def test_still_open_at_1550_alerts_once(home, monkeypatch, notes):
    b = FakeBroker(held={U1: 6}, plan=[0, 0, 0, 0])     # nothing ever fills
    _loop(home, monkeypatch, _rest_state(key=LATER, qty=6), b,
          [et(15, 49), et(15, 50), et(15, 55)])
    alerts = [(m, p) for m, p in notes if "still open at" in m]
    assert len(alerts) == 1 and alerts[0][1] == "high"
    assert alerts[0][0].startswith(
        "⚠️ agentic-trader: 6x RIVN 2026-11-13 $13.5 C from the agent's exit are "
        "still open at 15:50 ET — the agent keeps re-selling them until the 16:00 "
        "ET close")


def test_other_account_remainder_alerts_and_is_never_sold(home, monkeypatch, notes):
    b = FakeBroker(held={U1: 6})
    _loop(home, monkeypatch, _rest_state(qty=6, acct="robinhood:abc"), b,
          [et(14, 0), et(14, 1)])
    assert b.sells == []
    (found,) = _texts(notes, "didn't sell everything")
    assert "different broker account" in found


# ── 6. the ledger check ─────────────────────────────────────────────────────

def _bought_state(held_key=KEY, exited=False, order_ids=("buy-1",)):
    t = et(9, 0).timestamp() - 3600
    return {"seen": [], "positions": {}, "agent_order_ids": list(order_ids),
            "position_sync": {"proven": {TAG: "2026-11-01T15:00:00+00:00"}},
            "agent_bought": {U1: {"key": held_key, "acct": TAG,
                                  "order_ids": list(order_ids), "exited": exited,
                                  "opened_event": "open-1", "opened_at": t,
                                  "last_order_at": t, "entry_mark": 1.0}}}


def _at(dt):
    return dt.isoformat()


def test_ledger_missing_agent_contract_adopted_and_guard_sold(home, monkeypatch, notes):
    day = (2026, 11, 6)
    fills = [{"order_id": "buy-1", "side": "buy", "qty": 10,
              "at": _at(et(10, 0, day=(2026, 11, 4)))}]
    b = FakeBroker(held={U1: 10}, fills=fills)
    saved = _loop(home, monkeypatch, _bought_state(), b,
                  [et(15, 46, day=day), et(15, 46, day=day, s=30),
                   et(15, 46, day=day, s=40)])
    assert [(s["event_id"], s["qty"]) for s in b.sells] == [
        (f"expiry-guard|{KEY}|2026-11-06", 10)]
    assert saved["positions"] == {}
    (row,) = [r for r in _log() if r.get("action") == "ledger_adopt"]
    assert row["qty"] == 10 and row["held"] == 10
    (msg, prio) = [(m, p) for m, p in notes if "re-added" in m][0]
    assert msg.startswith(f"⚠️ agentic-trader: your broker account holds 10x {CONTRACT} "
                          "that the agent bought, but its ledger listed 0. The agent "
                          "re-added 10x (checked against its own filled orders).")
    assert "the expiry guard sells it at 15:45 ET" in msg and prio == "high"


def test_lost_exit_remainder_adopted_and_resold(home, monkeypatch, notes):
    """The agent's exit was booked as sold but only 4 of 10 filled: the 6
    still held are re-added (marked as already counted) and re-sold."""
    fills = [{"order_id": "sell-1", "side": "sell", "qty": 4,
              "at": _at(et(11, 0))},
             {"order_id": "buy-1", "side": "buy", "qty": 10,
              "at": _at(et(10, 0, day=(2026, 11, 4)))}]
    b = FakeBroker(held={U1: 6}, fills=fills)
    saved = _loop(home, monkeypatch,
                  _bought_state(held_key=LATER, exited=True,
                                order_ids=("buy-1", "sell-1")), b,
                  [et(14, 0), et(14, 0, s=30), et(14, 1)])
    assert [(s["qty"], s["limit"]) for s in b.sells] == [(6, "marketable")]
    assert saved["positions"] == {}
    (resold,) = [r for r in _log() if r.get("action") == "exit"]
    assert resold["qty"] == 6 and resold["counted_before"] == 6
    assert "est_pnl_usd" not in resold
    pnl = agent.pnl_summary({"source": "manual"}, {})
    assert pnl["live"]["unmeasured"] == 0          # counted with the first exit


def test_user_bought_contract_is_never_adopted(home, monkeypatch, notes):
    """The agent bought 2 and sold 2; the owner then bought 3 in the app.
    And a contract the agent never bought (U2) is never even asked about."""
    fills = [{"order_id": "u-9", "side": "buy", "qty": 3, "at": _at(et(12, 0))},
             {"order_id": "sell-1", "side": "sell", "qty": 2, "at": _at(et(11, 0))},
             {"order_id": "buy-1", "side": "buy", "qty": 2, "at": _at(et(10, 0))}]
    b = FakeBroker(held={U1: 3, U2: 5}, fills=fills)
    saved = _loop(home, monkeypatch,
                  _bought_state(held_key=LATER, exited=True,
                                order_ids=("buy-1", "sell-1")), b,
                  [et(14, 0), et(14, 0, s=30), et(14, 1), et(14, 2)])
    assert saved["positions"] == {} and b.sells == []
    assert not [r for r in _log() if r.get("action") == "ledger_adopt"]
    assert all(ids == [U1] for ids in b.asked)
    assert not _texts(notes, "re-added")


def test_owner_contract_never_adopted_without_an_agent_buy(home, monkeypatch, notes):
    fills = [{"order_id": "u-9", "side": "buy", "qty": 3, "at": _at(et(12, 0))}]
    b = FakeBroker(held={U1: 3}, fills=fills)
    saved = _loop(home, monkeypatch, _bought_state(held_key=LATER), b,
                  [et(14, 0), et(14, 0, s=30), et(14, 1)])
    assert saved["positions"] == {} and b.sells == []


def test_broker_without_order_history_alerts_only(home, monkeypatch, notes):
    day = (2026, 11, 6)
    b = NoHistory(held={U1: 4})
    saved = _loop(home, monkeypatch, _bought_state(), b,
                  [et(15, 46, day=day), et(15, 46, day=day, s=30),
                   et(15, 47, day=day), et(15, 48, day=day)])
    assert saved["positions"] == {} and b.sells == []
    alerts = [(m, p) for m, p in notes if "can't read your order history" in m]
    assert len(alerts) == 1 and alerts[0][1] == "high"
    assert alerts[0][0].startswith(
        f"⚠️ agentic-trader: your broker account holds 4x {CONTRACT}, a contract the "
        "agent bought earlier, but the agent's ledger lists 0.")
    assert "it expires today: sell it before the 16:00 ET close" in alerts[0][0]


def test_order_history_down_alerts_once_and_keeps_trying(home, monkeypatch, notes):
    day = (2026, 11, 6)
    b = HistoryDown(held={U1: 4})
    calls = []
    b.option_fills = lambda *a, **k: calls.append(1)   # always None
    times = [et(15, 46, day=day), et(15, 46, day=day, s=30), et(15, 47, day=day),
             et(15, 48, day=day), et(15, 49, day=day)]
    saved = _loop(home, monkeypatch, _bought_state(), b, times)
    assert saved["positions"] == {} and b.sells == []
    assert len(calls) == 4               # every check from the confirming read on
    alerts = [(m, p) for m, p in notes if "order history" in m]
    assert len(alerts) == 1 and alerts[0][1] == "high"
    assert "couldn't read your order history just now" in alerts[0][0]
    assert "keeps trying" in alerts[0][0]


def test_failed_history_read_near_the_guard_is_retried_and_guard_sells(
        home, monkeypatch, notes):
    """Review finding: one failed order-history read at 15:43 on expiry day
    used to block the adoption until after the close (exercise risk)."""
    day = (2026, 11, 6)
    b = FakeBroker(held={U1: 10}, fills=[BOUGHT_10])
    real = b.option_fills
    n = {"calls": 0}

    def flaky(*a, **k):
        n["calls"] += 1
        return None if n["calls"] == 1 else real(*a, **k)
    b.option_fills = flaky
    times = [et(15, 43, day=day), et(15, 43, day=day, s=40), et(15, 43, day=day, s=50),
             et(15, 46, day=day), et(15, 46, day=day, s=10)]
    saved = _loop(home, monkeypatch, _bought_state(), b, times)
    assert n["calls"] == 2
    assert [(s["event_id"], s["qty"]) for s in b.sells] == [
        (f"expiry-guard|{KEY}|2026-11-06", 10)]
    assert saved["positions"] == {}
    assert len(_texts(notes, "couldn't read your order history")) == 1
    assert len(_texts(notes, "re-added 10x")) == 1


def test_failed_history_read_is_retried_on_the_next_check(home, monkeypatch, notes):
    """Not 15 minutes later: a read that failed is retried on the next check
    (every minute), and the contracts are re-added once it answers."""
    b = FakeBroker(held={U1: 3}, fills=[{"order_id": "buy-1", "side": "buy", "qty": 3,
                                         "at": "2026-11-04T15:00:00+00:00"}])
    real = b.option_fills
    n = {"calls": 0}

    def flaky(*a, **k):
        n["calls"] += 1
        return None if n["calls"] == 1 else real(*a, **k)
    b.option_fills = flaky
    saved = _loop(home, monkeypatch, _bought_state(held_key=LATER), b,
                  [et(14, 0), et(14, 1), et(14, 2)])
    assert n["calls"] == 2
    assert saved["positions"][LATER]["qty"] == 3
    assert saved["positions"][LATER]["adopted"]["bought"] == 3


def test_history_alert_not_repeated_when_the_check_restarts(home, monkeypatch, notes):
    """The ledger check's entry is dropped (holdings dip) and recreated: the
    same discrepancy isn't alerted twice the same day."""
    b = NoHistory(held={U1: 4})
    times = [et(14, 0), et(14, 1), et(14, 2), et(14, 3), et(14, 4), et(14, 5)]
    seq = iter([4, 4, 0, 4, 4, 4])

    def held(client, cfg, ids):
        v = next(seq, 4)
        return {i: v for i in ids}
    b.held_option_qty = held
    _loop(home, monkeypatch, _bought_state(held_key=LATER), b, times)
    assert len(_texts(notes, "order history")) == 1


def test_ledger_check_waits_out_the_agents_own_recent_orders(home, monkeypatch, notes):
    """Holdings and order history lag a fresh sell: not adopted right after."""
    st = _bought_state(held_key=LATER, exited=True, order_ids=("buy-1", "sell-1"))
    st["agent_bought"][U1]["last_order_at"] = et(14, 0).timestamp() - 30
    fills = [{"order_id": "buy-1", "side": "buy", "qty": 2, "at": _at(et(10, 0))}]
    b = FakeBroker(held={U1: 2}, fills=fills)        # the sell isn't listed yet
    saved = _loop(home, monkeypatch, st, b, [et(14, 0), et(14, 0, s=30), et(14, 1)])
    assert saved["positions"] == {} and b.asked == []


def test_entry_records_the_contract_for_the_ledger_check(home, monkeypatch, no_policy):
    class Buys(FakeBroker):
        def execute(self, client, cfg, event, state):
            if event["event"] != "ENTERED":
                return super().execute(client, cfg, event, state)
            key = f"{event['ticker']}|{event['expiry']}|{event['strike']}|{event['type']}"
            state["positions"][key] = {"option_id": U1, "qty": 2, "order_ids": ["b-1"]}
            state["agent_order_ids"] = ["b-1"]
            return True
    monkeypatch.setattr(agent, "notify", lambda cfg, msg, priority=None: None)
    monkeypatch.setattr(agent, "_et_now", lambda: et(10, 0))
    st = {"positions": {}, "seen": []}
    ev = {"event": "ENTERED", "ticker": "RIVN", "expiry": "2026-11-13", "strike": 13.5,
          "type": "C", "event_id": "e-1"}
    b = Buys()
    agent.handle_event(ev, {"broker": "fake"}, st, b, Client(), lambda s: None)
    rec = st["agent_bought"][U1]
    assert rec["key"] == LATER and rec["order_ids"] == ["b-1"] and rec["exited"] is False
    assert rec["buy_ids"] == ["b-1"]


# ── adapters ────────────────────────────────────────────────────────────────

def test_exit_qty_caps_at_max_qty_never_above_the_row():
    assert exit_qty({"qty": 6}, {}) == 6
    assert exit_qty({"qty": 6}, {"max_qty": 4}) == 4
    assert exit_qty({"qty": 6}, {"max_qty": 9}) == 6
    assert exit_qty({"qty": 6}, {"max_qty": 0}) == 6
    assert exit_qty({"qty": 6}, {"max_qty": True}) == 6


def test_robinhood_option_fills_lists_buys_and_sells():
    class RH:
        last_error = None

        def call_tool(self, name, args):
            return _mcp({"data": {"orders": [
                {"id": "o-2", "legs": [{"option_id": U1, "side": "sell",
                                        "position_effect": "close",
                                        "executions": [{"quantity": "4", "price": "0.9",
                                                        "timestamp": "2026-11-05T20:55:00Z"}]}]},
                {"id": "o-1", "processed_quantity": "10", "updated_at": "2026-11-04T15:00:00Z",
                 "legs": [{"option": f"https://x/options/instruments/{U1}/",
                           "side": "buy", "position_effect": "open"}]},
                {"id": "o-0", "processed_quantity": "0", "legs": [
                    {"option_id": U1, "side": "buy"}]},
                {"id": "o-x", "processed_quantity": "3", "legs": [
                    {"option_id": U2, "side": "buy"}]}]}})
    got = robinhood.option_fills(RH(), {"robinhood_account": "A1"}, U1)
    assert got == [{"order_id": "o-2", "side": "sell", "qty": 4,
                    "at": "2026-11-05T20:55:00Z"},
                   {"order_id": "o-1", "side": "buy", "qty": 10,
                    "at": "2026-11-04T15:00:00Z"}]
    rh = RH()
    rh.last_error = "down"
    assert robinhood.option_fills(rh, {"robinhood_account": "A1"}, U1) is None


def test_alpaca_option_fills(monkeypatch):
    occ = "RIVN261106C00013500"

    class Resp:
        status_code = 200

        def json(self):
            return [{"id": "a2", "symbol": occ, "side": "sell", "filled_qty": "4",
                     "filled_at": "2026-11-05T20:55:00Z"},
                    {"id": "a1", "symbol": occ, "side": "buy", "filled_qty": "10",
                     "filled_at": "2026-11-04T15:00:00Z"},
                    {"id": "a0", "symbol": occ, "side": "buy", "filled_qty": "0"}]
    seen = []
    monkeypatch.setattr(alpaca.requests, "get",
                        lambda url, **kw: seen.append(kw["params"]) or Resp())
    got = alpaca.option_fills(None, {"alpaca_key_id": "k"}, occ)
    assert [(f["order_id"], f["side"], f["qty"]) for f in got] == [
        ("a2", "sell", 4), ("a1", "buy", 10)]
    assert seen[0]["status"] == "all"


def test_moomoo_exit_partial_fill_keeps_the_rest(monkeypatch):
    from test_position_reads import _MMSDK
    sdk = _MMSDK(sell_status="SUBMITTED", sell_dealt=4)
    monkeypatch.setattr(moomoo, "_sdk", lambda: sdk)
    broker_events.begin()
    key = "SPY|2099-12-18|752.0|C"
    st = {"positions": {key: {"option_id": "US.SPY991218C752000", "qty": 10}}}
    ev = {"event": "EXITED", "ticker": "SPY", "expiry": "2099-12-18", "strike": 752.0,
          "type": "C", "event_id": "x"}
    assert moomoo.execute(None, {"broker": "moomoo", "moomoo_paper": True}, ev, st)
    assert st["positions"][key]["qty"] == 6 and sdk.cancelled == ["mm-1"]
    (rep,) = broker_events.drain()
    assert rep["kind"] == "exit_partial"
    assert "the agent re-sells the remaining 6x next loop" in rep["message"]


def test_moomoo_exit_filled_is_booked_as_sold(monkeypatch):
    from test_position_reads import _MMSDK
    sdk = _MMSDK()
    monkeypatch.setattr(moomoo, "_sdk", lambda: sdk)
    key = "SPY|2099-12-18|752.0|C"
    st = {"positions": {key: {"option_id": "US.SPY991218C752000", "qty": 2}}}
    ev = {"event": "EXITED", "ticker": "SPY", "expiry": "2099-12-18", "strike": 752.0,
          "type": "C", "event_id": "x", "max_qty": 1}
    assert moomoo.execute(None, {"broker": "moomoo", "moomoo_paper": True}, ev, st)
    assert sdk.placed[0]["qty"] == 1 and st["positions"][key]["qty"] == 1


def test_moomoo_unreadable_exit_is_assumed_sold_and_reported(monkeypatch):
    from test_position_reads import _MMSDK
    sdk = _MMSDK(ord_ret=-1)
    monkeypatch.setattr(moomoo, "_sdk", lambda: sdk)
    broker_events.begin()
    key = "SPY|2099-12-18|752.0|C"
    st = {"positions": {key: {"option_id": "US.SPY991218C752000", "qty": 2}}}
    ev = {"event": "EXITED", "ticker": "SPY", "expiry": "2099-12-18", "strike": 752.0,
          "type": "C", "event_id": "x"}
    assert moomoo.execute(None, {"broker": "moomoo", "moomoo_paper": True}, ev, st)
    assert st["positions"] == {} and sdk.cancelled == []
    (rep,) = broker_events.drain()
    assert rep["kind"] == "exit_failed" and rep.get("placed") is True
    assert "re-checks your holdings" in rep["message"]


def test_moomoo_option_fills_today_and_history(monkeypatch):
    from test_position_reads import _MMSDK
    code = "US.SPY991218C752000"
    sdk = _MMSDK(orders=[{"order_id": "m2", "code": code, "trd_side": "TrdSide.SELL",
                          "dealt_qty": 4.0, "updated_time": "2026-11-05 15:55:00"}],
                 history=[{"order_id": "m2", "code": code, "trd_side": "SELL",
                           "dealt_qty": 4.0},
                          {"order_id": "m1", "code": code, "trd_side": "BUY",
                           "dealt_qty": 10.0, "updated_time": "2026-11-04 10:00:00"},
                          {"order_id": "m0", "code": "US.X", "trd_side": "BUY",
                           "dealt_qty": 1.0}])
    monkeypatch.setattr(moomoo, "_sdk", lambda: sdk)
    got = moomoo.option_fills(moomoo._Client({"moomoo_paper": True}), {}, code)
    assert [(f["order_id"], f["side"], f["qty"]) for f in got] == [
        ("m2", "sell", 4), ("m1", "buy", 10)]
    assert moomoo.option_fills(None, {}, "SPY") is None


# ── review fixes: the re-sell's own safety rails ────────────────────────────

def test_broken_connection_waits_like_an_unreadable_read(home, monkeypatch, notes):
    """last_error set: no read, so the EXIT_REST_UNKNOWN_S wait applies (it
    used to sell 6 on the first loop with nothing held)."""
    class Down(Client):
        last_error = "connection lost"
    b = FakeBroker(held={U1: 0})
    b.client = lambda cfg: Down()
    _loop(home, monkeypatch, _rest_state(key=LATER, qty=6), b,
          [et(14, 0), et(14, 1), et(14, 5)])
    assert b.sells == [] and b.asked == []


def test_blind_resells_are_capped_then_alert(home, monkeypatch, notes):
    """Holdings unreadable 10+ min: a close-only adapter sells blind at most
    EXIT_REST_BLIND_TRIES times, then alerts once and stops."""
    b = FakeBroker(held=None)

    def rejecting(client, cfg, event, state):
        b.sells.append({"event_id": event["event_id"], "qty": 0})
        return False                                     # e.g. nothing to close
    b.execute = rejecting
    times = [et(14, 0), et(14, 10), et(14, 11), et(14, 12), et(14, 13), et(14, 14)]
    saved = _loop(home, monkeypatch, _rest_state(key=LATER, qty=6), b, times)
    assert len(b.sells) == agent.EXIT_REST_BLIND_TRIES == 3
    alerts = [(m, p) for m, p in notes if "stops selling blind" in m]
    assert len(alerts) == 1 and alerts[0][1] == "high"
    assert saved["positions"][LATER]["qty"] == 6


def test_holdings_check_off_alerts_and_never_resells(home, monkeypatch, notes):
    """position_sync: false — the agent can't confirm what's held, so it never
    re-sells (it used to sell blind on every loop)."""
    b = FakeBroker(held={U1: 0})
    _loop(home, monkeypatch, _rest_state(key=LATER, qty=6), b,
          [et(14, 0), et(14, 1), et(14, 2), et(15, 50)], cfg={"position_sync": False})
    assert b.sells == [] and b.asked == []
    (found,) = _texts(notes, "didn't sell everything")
    assert "position_sync is off" in found and "sell them in your broker app" in found
    (late,) = _texts(notes, "still open at")
    assert "sell them in your broker app before the 16:00 ET close" in late


def test_no_resell_on_a_market_holiday(home, monkeypatch, notes):
    """Thanksgiving 2026-11-26: nothing is sent; the next morning (a 1 PM
    early close) it's sold from 09:30."""
    key = "RIVN|2026-12-04|13.5|C"
    b = FakeBroker(held={U1: 6})
    reads = []
    real = b.held_option_qty
    b.held_option_qty = lambda c, cfg, ids: reads.append(agent._now()) or real(c, cfg, ids)
    saved = _loop(home, monkeypatch, _rest_state(key=key, qty=6), b,
                  [et(10, 0, day=(2026, 11, 26)), et(12, 0, day=(2026, 11, 26)),
                   et(9, 30, day=(2026, 11, 27)), et(9, 30, day=(2026, 11, 27), s=30)])
    assert len(b.sells) == 1 and b.sells[0]["limit"] == "marketable"
    assert reads and min(reads) >= et(9, 30, day=(2026, 11, 27)).timestamp()
    assert saved["positions"] == {}
    (found,) = _texts(notes, "didn't sell everything")
    assert "The market is closed" in found


def test_nyse_holiday_calendar():
    from brokers import market_hours as mh
    assert {d.isoformat() for d in mh.nyse_holidays(2026)} == {
        "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
        "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25"}
    assert {d.isoformat() for d in mh.nyse_holidays(2027)} == {
        "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31",
        "2027-06-18", "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24"}
    assert not mh.is_market_holiday({}, "2027-12-31")    # Sat New Year's: not moved
    assert mh.is_trading_day({}, "2026-11-27")           # early close, still trades
    assert not mh.is_trading_day({}, "2026-11-28")       # Saturday
    assert not mh.is_trading_day({"market_holidays": ["2026-11-10"]}, "2026-11-10")


def test_open_rush_reads_once_a_minute_and_skips_the_ledger_check(
        home, monkeypatch, notes):
    """9:28-9:40 (Robinhood throttles): one holdings read a minute for the
    remainders, no ledger check."""
    b = FakeBroker(held={U1: 6}, plan=[1, 1])
    reads = []
    real = b.held_option_qty
    b.held_option_qty = lambda c, cfg, ids: reads.append(agent._now()) or real(c, cfg, ids)
    times = [et(9, 30), et(9, 30, s=20), et(9, 30, s=40), et(9, 31)]
    st = _rest_state(key=LATER, qty=6)
    st["agent_bought"] = {U2: {"key": LATER.replace("13.5", "14.0"), "acct": TAG,
                               "order_ids": ["buy-2"], "last_order_at": 0}}
    _loop(home, monkeypatch, st, b, times, rush=True)
    assert [s["qty"] for s in b.sells] == [6, 5]
    assert {r for r in reads} <= {et(9, 30).timestamp(), et(9, 31).timestamp()}
    assert not any(U2 in ids for ids in b.asked)         # ledger check waited


def test_no_resell_into_a_zero_bid(home, monkeypatch, notes):
    bids = iter([0.0, 0.0, 0.40])

    class Bids(FakeBroker):
        def exit_bid(self, client, cfg, event, option_id=None):
            return next(bids)
    b = Bids(held={U1: 6})
    saved = _loop(home, monkeypatch, _rest_state(key=LATER, qty=6), b,
                  [et(14, 0), et(14, 1), et(14, 2)])
    assert [s["qty"] for s in b.sells] == [6]           # only once there's a bid
    assert len(_texts(notes, "has no bid right now")) == 1
    assert saved["positions"] == {}


def test_still_open_alert_skipped_when_the_account_holds_none(home, monkeypatch, notes):
    b = FakeBroker(held={U1: 0})
    _loop(home, monkeypatch, _rest_state(key=LATER, qty=6), b,
          [et(15, 49), et(15, 50), et(15, 51)])
    assert b.sells == [] and not _texts(notes, "still open at")


def test_buy_ids_survive_many_resold_sells():
    """Review finding: re-placed sells pushed the buy order id out of the
    kept lists; the buy ids are kept apart."""
    st = {"positions": {}, "agent_order_ids": []}
    row = {"option_id": U1, "qty": 6, "acct": TAG, "order_ids": ["buy-1"]}
    agent._remember_agent_row(st, KEY, row, TAG, 1.0, new_ids=["buy-1"],
                              buy_ids=["buy-1"])
    for i in range(40):
        robinhood._stamp_order(row, f"sell-{i}")
        robinhood._remember_order(st, f"sell-{i}")
        agent._remember_agent_row(st, KEY, row, TAG, 1.0, new_ids=[f"sell-{i}"])
    rec = st["agent_bought"][U1]
    assert "buy-1" not in rec["order_ids"] and rec["buy_ids"] == ["buy-1"]
    # ...and the ledger check still proves the agent's buy with it
    fills = [BOUGHT_10]
    share = agent._agent_share(fills, set(agent._ids(rec.get("buy_ids"))))
    assert share[0] == 10


def test_pending_buy_is_recorded_as_a_buy_id():
    st = {"positions": {}}
    row = {"option_id": U1, "qty": 0, "order_ids": ["b-2"],
           "pending": {"order_id": "b-2", "side": "buy", "requested": 2}}
    agent._remember_agent_row(st, KEY, row, TAG, 1.0)
    assert st["agent_bought"][U1]["buy_ids"] == ["b-2"]


class WorkingRH(ScriptRH):
    """Orders whose plan entry is None stay working until cancelled."""

    def call_tool(self, name, args):
        if name == "place_option_order":
            oid = f"rh-{len(self.placed) + 1}"
            self.placed.append(dict(args))
            q = int(args["quantity"])
            f = self.plan.pop(0) if self.plan else q
            if f is None:
                self.orders[oid] = {"id": oid, "state": "confirmed",
                                    "processed_quantity": "0"}
            else:
                self.held -= f
                self.orders[oid] = {"id": oid, "processed_quantity": str(f),
                                    "state": "filled" if f >= q else "cancelled"}
            return _mcp({"data": {"id": oid}})
        if name == "cancel_option_order":
            o = self.orders.get(args.get("order_id"))
            if o and o["state"] == "confirmed":
                o["state"] = "cancelled"
            return _mcp({"ok": True})
        return super().call_tool(name, args)


def test_robinhood_remainder_repriced_until_it_fills(home, monkeypatch, notes):
    """The re-sell keeps not filling: reconcile cancels and re-places it at a
    fresh marketable price each loop until it fills. Dead unfilled orders
    are forgotten, so the agent's buy id is never pushed out."""
    rh = WorkingRH(held=6, plan=[None] * 25 + [6])
    monkeypatch.setattr(robinhood, "client", lambda cfg: rh)
    monkeypatch.setattr(robinhood.time, "sleep", lambda s: None)
    monkeypatch.setattr(robinhood, "_in_opening_blackout", lambda now=None: False)
    monkeypatch.setattr(robinhood, "_before_close", lambda now=None, cfg=None: True)
    monkeypatch.setattr(robinhood, "_REPRICE_AFTER_S", 0)
    from brokers import account_tag
    cfg = {"robinhood_account": "A1"}
    st = _rest_state(acct=account_tag({"broker": "robinhood", **cfg}))
    times = [et(14, 0, s=0)] + [datetime.fromtimestamp(et(14, 0).timestamp() + 20 * k, TZ)
                                for k in range(1, 28)]
    saved = _loop(home, monkeypatch, st, robinhood, times, cfg=cfg,
                  broker_name="robinhood")
    assert len(rh.placed) == 26
    assert {p["quantity"] for p in rh.placed} == {"6"}
    assert all(p["type"] == "limit" and p["legs"][0]["position_effect"] == "close"
               for p in rh.placed)
    assert saved["positions"] == {} and rh.held == 0
    assert "buy-1" in saved["agent_order_ids"]
    assert [i for i in saved["agent_order_ids"] if i.startswith("rh-")] == ["rh-26"]
