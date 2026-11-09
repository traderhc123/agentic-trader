"""Expiry guard: the agent's own positions that expire today are sold from
expiry_guard_et (default 15:45 ET) until the close, with a loud alert at
expiry_alert_et (default 15:50) for anything still open, and startup/wake
alerts for positions that expire today or expired while it was offline.
Zero network."""

import json
from datetime import datetime, timezone

import pytest

import agent
import notifications
from brokers import alpaca, robinhood
from brokers.pricing import marketable_limit, quote_number
from conftest import StubBroker

TZ = agent.MARKET_TZ
DAY = "2026-11-06"                       # a Friday (EST, after the DST change)
KEY = f"RIVN|{DAY}|13.5|C"
LATER = "RIVN|2026-11-13|13.5|C"
CONTRACT = f"RIVN {DAY} $13.5 C"


def et(h, m, day=(2026, 11, 6), s=0):
    return datetime(*day, h, m, s, tzinfo=TZ)


class GuardBroker(StubBroker):
    """StubBroker + the exit_bid hook; `fail` execute calls return False
    (an exit that wasn't placed) before it starts selling."""

    def __init__(self, bid=1.0, fail=0):
        super().__init__()
        self.bid = bid
        self.fail = fail
        self.bids = []

    def exit_bid(self, client, cfg, event, option_id):
        self.bids.append(option_id)
        return self.bid

    def execute(self, client, cfg, event, state):
        if self.fail:
            self.fail -= 1
            self.calls.append(event)
            return False
        return super().execute(client, cfg, event, state)


@pytest.fixture(autouse=True)
def quiet(monkeypatch, home):
    sent = []
    monkeypatch.setattr(agent, "notify",
                        lambda cfg, msg, priority=None: sent.append((msg, priority)))
    monkeypatch.setattr(agent, "_problems_logged", set())
    monkeypatch.setattr(agent, "_et_now", lambda: et(15, 45))
    return sent


def _state(*keys, **row):
    base = {"option_id": "opt-1", "qty": 2}
    base.update(row)
    return {"positions": {k: dict(base) for k in keys}}


def _tick(state, broker, now, cfg=None, paused=False):
    return agent.expiry_guard_tick(cfg or {}, state, lambda s: None, broker,
                                   object(), paused=paused, now_et=now)


def _log():
    try:
        with open(agent.TRADES_PATH, encoding="utf-8") as f:
            return [json.loads(x) for x in f if x.strip()]
    except OSError:
        return []


# ── when it sells ───────────────────────────────────────────────────────────

def test_guard_sells_expiring_ledger_row_at_1545(quiet):
    state = _state(KEY, LATER)
    b = GuardBroker()
    out = _tick(state, b, et(15, 45))
    assert out == [f"EXIT: RIVN {DAY} $13.5 C"]
    assert [e["event"] for e in b.calls] == ["EXITED"]
    ev = b.calls[0]
    assert (ev["ticker"], ev["expiry"], ev["strike"], ev["type"]) == ("RIVN", DAY, 13.5, "C")
    assert ev["exit_limit"] == "marketable"
    assert KEY not in state["positions"] and LATER in state["positions"]
    assert any("expiry guard sold" in m for m, _ in quiet)


def test_guard_not_before_1545():
    state = _state(KEY)
    b = GuardBroker()
    assert _tick(state, b, et(15, 44, s=59)) == []
    assert b.calls == [] and KEY in state["positions"]


def test_guard_still_sells_late_in_the_window():
    state = _state(KEY)
    b = GuardBroker()
    _tick(state, b, et(15, 59, s=50))
    assert len(b.calls) == 1


def test_guard_does_nothing_at_or_after_the_close():
    state = _state(KEY)
    b = GuardBroker()
    assert _tick(state, b, et(16, 0)) == []
    assert b.calls == []


def test_guard_ignores_positions_not_expiring_today():
    state = _state(LATER, "SPY|2026-11-05|600.0|P")
    b = GuardBroker()
    assert _tick(state, b, et(15, 50)) == []
    assert b.calls == [] and b.bids == []


def test_guard_only_touches_the_agents_own_ledger():
    """Account holdings the agent didn't open never enter the guard: it
    only walks the ledger, and only asks the broker about ledger rows."""
    state = _state(KEY, option_id="opt-ledger")
    b = GuardBroker()
    _tick(state, b, et(15, 46))
    assert b.bids == ["opt-ledger"]
    assert len(b.calls) == 1
    assert b.calls[0]["event_id"] == f"expiry-guard|{KEY}|{DAY}"


def test_guard_skips_weekends():
    sat = "SPY|2026-11-07|600.0|C"
    state = _state(sat)
    b = GuardBroker()
    assert _tick(state, b, et(15, 50, day=(2026, 11, 7))) == []
    assert b.calls == []


def test_guard_writes_an_exit_row_with_its_cause():
    state = _state(KEY, entry_mark=1.0)
    _tick(state, GuardBroker(), et(15, 45))
    rows = [r for r in _log() if r.get("action") == "exit"]
    assert len(rows) == 1
    assert rows[0]["cause"] == "expiry_guard"
    assert rows[0]["contract"] == f"RIVN {DAY} $13.5 C"
    assert "expiry guard" in rows[0]["reason"] and "15:45" in rows[0]["reason"]


def test_guard_exit_counts_in_pnl_like_any_exit(monkeypatch):
    class Marked(GuardBroker):
        def mark_price(self, client, cfg, event, side="buy"):
            return 1.5
    state = _state(KEY, entry_mark=1.0, qty=1)
    _tick(state, Marked(), et(15, 45))
    pnl = agent.pnl_summary({"source": "manual"}, {})
    assert pnl["live"]["exits"] == 1
    assert pnl["live"]["total"] == 50.0


# ── bid 0, failures, retries ────────────────────────────────────────────────

def test_no_bid_means_no_order_and_one_alert(quiet):
    state = _state(KEY)
    b = GuardBroker(bid=0.0)
    _tick(state, b, et(15, 45))
    _tick(state, b, et(15, 46))
    assert b.calls == [] and KEY in state["positions"]
    nobid = [m for m, _ in quiet if "no bid" in m]
    assert len(nobid) == 1


def test_no_bid_then_a_bid_appears_sells():
    state = _state(KEY)
    b = GuardBroker(bid=0.0)
    _tick(state, b, et(15, 45))
    b.bid = 0.4
    _tick(state, b, et(15, 46))
    assert len(b.calls) == 1 and KEY not in state["positions"]


def test_unknown_bid_still_sells():
    state = _state(KEY)
    b = GuardBroker(bid=None)
    _tick(state, b, et(15, 45))
    assert len(b.calls) == 1


def test_broker_without_bid_hook_still_sells():
    state = _state(KEY)
    b = StubBroker()
    _tick(state, b, et(15, 45))
    assert len(b.calls) == 1


def test_failed_sell_is_retried_next_loop():
    state = _state(KEY)
    b = GuardBroker(fail=1)
    assert _tick(state, b, et(15, 45)) == []
    assert KEY in state["positions"]
    assert _tick(state, b, et(15, 45, s=15)) == [f"EXIT: RIVN {DAY} $13.5 C"]
    assert len(b.calls) == 2 and KEY not in state["positions"]


def test_working_sell_is_not_sent_again():
    state = _state(KEY, pending={"side": "sell", "requested": 2, "order_id": "o"})
    b = GuardBroker()
    _tick(state, b, et(15, 47))
    assert b.calls == [] and b.bids == []


def test_broker_error_never_escapes_the_loop():
    class Boom(GuardBroker):
        def execute(self, *a):
            raise RuntimeError("network")
    state = _state(KEY)
    assert _tick(state, Boom(), et(15, 45)) == []
    assert KEY in state["positions"]


# ── the 15:50 alert ─────────────────────────────────────────────────────────

def test_alert_at_1550_once_with_high_priority(quiet):
    state = _state(KEY)
    b = GuardBroker(fail=99)              # never sells
    _tick(state, b, et(15, 49))
    assert not [m for m, _ in quiet if "sell it now" in m]
    _tick(state, b, et(15, 50))
    _tick(state, b, et(15, 55))
    alerts = [(m, p) for m, p in quiet if "sell it now" in m]
    assert len(alerts) == 1
    msg, prio = alerts[0]
    assert prio == "high"
    assert (f"{CONTRACT} expires today and is still open — sell it now in your "
            "broker app or it may be exercised") in msg


def test_no_alert_when_the_guard_sold_it(quiet):
    state = _state(KEY)
    _tick(state, GuardBroker(), et(15, 50))
    assert not [m for m, _ in quiet if "sell it now" in m]


def test_paused_agent_alerts_but_does_not_sell(quiet):
    state = _state(KEY)
    b = GuardBroker()
    _tick(state, b, et(15, 50), paused=True)
    assert b.calls == []
    assert any("paused" in m for m, _ in quiet if "sell it now" in m)


def test_dry_rows_are_simulated_and_never_alerted(quiet):
    state = _state(KEY, dry=True, option_id="dry")
    b = GuardBroker()
    _tick(state, b, et(15, 50))
    assert b.calls == [] and KEY not in state["positions"]  # dry-run exit
    rows = [r for r in _log() if r.get("action") == "exit"]
    assert rows and rows[0]["dry"] and rows[0]["cause"] == "expiry_guard"
    assert not [m for m, _ in quiet if "sell it now" in m]


def test_real_row_in_dry_run_mode_is_not_dropped_unsold(quiet):
    state = _state(KEY)
    b = GuardBroker()
    _tick(state, b, et(15, 50), cfg={"dry_run": True})
    assert b.calls == [] and KEY in state["positions"]
    assert any("dry-run" in m for m, _ in quiet if "sell it now" in m)


# ── config ──────────────────────────────────────────────────────────────────

def test_guard_off_disables_sells_but_alert_stays(quiet):
    state = _state(KEY)
    b = GuardBroker()
    _tick(state, b, et(15, 50), cfg={"expiry_guard_et": "off"})
    assert b.calls == []
    assert [m for m, _ in quiet if "sell it now" in m]


def test_both_off_does_nothing(quiet):
    state = _state(KEY)
    b = GuardBroker()
    cfg = {"expiry_guard_et": "off", "expiry_alert_et": "off"}
    _tick(state, b, et(15, 55), cfg=cfg)
    assert b.calls == [] and quiet == []
    assert not agent.expiry_guard_busy(cfg, state, et(15, 55))


def test_custom_guard_time():
    state = _state(KEY)
    b = GuardBroker()
    _tick(state, b, et(15, 39), cfg={"expiry_guard_et": "15:40"})
    assert b.calls == []
    _tick(state, b, et(15, 40), cfg={"expiry_guard_et": "15:40"})
    assert len(b.calls) == 1


@pytest.mark.parametrize("raw", ["25:00", "3:45pm", "16:30", "08:00", "", None, 7])
def test_invalid_time_uses_the_default(raw):
    assert agent._et_hhmm({"expiry_guard_et": raw}, "expiry_guard_et", "15:45") == 15 * 60 + 45


@pytest.mark.parametrize("raw", ["off", "OFF", False, "disabled"])
def test_off_values(raw):
    assert agent._et_hhmm({"expiry_guard_et": raw}, "expiry_guard_et", "15:45") is None


def test_chat_command_switches_it(home):
    agent._save(agent.CONFIG_PATH, {})
    ok, reply = agent._apply_command("expiry guard off")
    assert ok and agent._load(agent.CONFIG_PATH)["expiry_guard_et"] == "off"
    ok, reply = agent._apply_command("expiry guard 9:45")
    assert ok and agent._load(agent.CONFIG_PATH)["expiry_guard_et"] == "09:45"
    ok, reply = agent._apply_command("expiry guard on")
    assert agent._load(agent.CONFIG_PATH)["expiry_guard_et"] == "15:45"
    ok, reply = agent._apply_command("expiry alert 17:00")
    assert ok and "Usage" in reply
    assert "expiry_alert_et" not in agent._load(agent.CONFIG_PATH)


# ── loop cadence ────────────────────────────────────────────────────────────

def test_busy_only_in_the_window_with_an_expiring_row():
    state = _state(KEY)
    assert not agent.expiry_guard_busy({}, state, et(15, 43))
    assert agent.expiry_guard_busy({}, state, et(15, 44))
    assert agent.expiry_guard_busy({}, state, et(15, 59))
    assert not agent.expiry_guard_busy({}, state, et(16, 0))
    assert not agent.expiry_guard_busy({}, _state(LATER), et(15, 50))


# ── time zones, DST, early closes ───────────────────────────────────────────

@pytest.mark.parametrize("utc,day,fires", [
    (datetime(2026, 11, 6, 20, 45, tzinfo=timezone.utc), DAY, True),          # EST 15:45
    (datetime(2026, 11, 6, 19, 45, tzinfo=timezone.utc), DAY, False),         # EST 14:45
    (datetime(2026, 7, 10, 19, 45, tzinfo=timezone.utc), "2026-07-10", True),  # EDT 15:45
    (datetime(2026, 7, 10, 20, 45, tzinfo=timezone.utc), "2026-07-10", False), # EDT 16:45
])
def test_guard_time_is_new_york_time_across_dst(utc, day, fires):
    key = f"SPY|{day}|600.0|C"
    state = _state(key)
    b = GuardBroker()
    _tick(state, b, utc.astimezone(TZ))
    assert bool(b.calls) is fires


def test_close_epoch_follows_dst():
    assert agent._close_epoch({}, DAY) == datetime(2026, 11, 6, 21, 0, tzinfo=timezone.utc).timestamp()
    assert agent._close_epoch({}, "2026-07-10") == datetime(2026, 7, 10, 20, 0, tzinfo=timezone.utc).timestamp()


def test_early_close_day_moves_the_guard_to_1245():
    key = "SPY|2026-11-27|600.0|C"        # day after Thanksgiving: 1 PM close
    state = _state(key)
    b = GuardBroker()
    _tick(state, b, et(12, 44, day=(2026, 11, 27)))
    assert b.calls == []
    _tick(state, b, et(12, 45, day=(2026, 11, 27)))
    assert len(b.calls) == 1


def test_early_close_day_nothing_after_1300(quiet):
    key = "SPY|2026-12-24|600.0|C"
    state = _state(key)
    b = GuardBroker()
    _tick(state, b, et(13, 0, day=(2026, 12, 24)))
    assert b.calls == [] and quiet == []


def test_configured_early_close_day():
    key = "SPY|2027-07-02|600.0|C"
    state = _state(key)
    b = GuardBroker()
    cfg = {"early_close_days": ["2027-07-02"]}
    _tick(state, b, et(12, 45, day=(2027, 7, 2)), cfg=cfg)
    assert len(b.calls) == 1


# ── startup / wake check ────────────────────────────────────────────────────

def _wake(state, prev, now, cfg=None):
    return agent.expiry_wake_check(cfg or {}, state, lambda s: None, prev,
                                   now_et=now)


def test_startup_alerts_expires_today_once(quiet):
    state = _state(KEY)
    sent = _wake(state, None, et(10, 0))
    assert len(sent) == 1 and "expires today" in sent[0] and "15:45" in sent[0]
    assert _wake(state, None, et(11, 0)) == []


def test_startup_after_guard_time_leaves_it_to_the_guard(quiet):
    state = _state(KEY)
    assert _wake(state, None, et(15, 47)) == []
    assert quiet == []


def test_startup_expires_today_with_guard_off_says_sell_it():
    state = _state(KEY)
    sent = _wake(state, None, et(10, 0), cfg={"expiry_guard_et": "off"})
    assert "guard is off" in sent[0] and "16:00" in sent[0]


def test_startup_alerts_expired_while_offline_once(quiet):
    state = _state("RIVN|2026-11-05|13.5|C")
    prev = et(15, 0, day=(2026, 11, 5)).timestamp()   # last ran before that close
    sent = _wake(state, prev, et(9, 0))
    assert sent == ["RIVN 2026-11-05 $13.5 C expired while the agent was offline "
                    "— check your broker account for an exercise/assignment"]
    assert [p for m, p in quiet if "offline" in m] == ["high"]
    assert _wake(state, et(9, 1).timestamp(), et(9, 5)) == []


def test_expired_today_after_the_close_counts_as_expired():
    state = _state(KEY)
    sent = _wake(state, et(15, 30).timestamp(), et(18, 0))
    assert len(sent) == 1 and "offline" in sent[0]


def test_no_offline_alert_when_the_agent_ran_through_that_close():
    state = _state("RIVN|2026-11-05|13.5|C")
    prev = et(17, 0, day=(2026, 11, 5)).timestamp()
    assert _wake(state, prev, et(9, 0)) == []


def test_wake_check_skips_dry_rows():
    state = _state(KEY, dry=True)
    assert _wake(state, None, et(10, 0)) == []


def test_prune_does_not_alert_again_for_an_alerted_row(quiet):
    state = _state("RIVN|2026-11-05|13.5|C")
    _wake(state, None, et(9, 0))
    n = len(quiet)
    agent._prune_expired_positions({}, state, lambda s: None)
    assert state["positions"] == {}
    assert len(quiet) == n


# ── dashboard banner ────────────────────────────────────────────────────────

def test_banner_levels():
    state = _state(KEY)
    assert agent.expiry_banner({}, state, et(10, 0))[0]["level"] == "info"
    assert agent.expiry_banner({}, state, et(15, 46))[0]["level"] == "warn"
    urgent = agent.expiry_banner({}, state, et(15, 50))[0]
    assert urgent["level"] == "urgent" and "may be exercised" in urgent["text"]
    assert agent.expiry_banner({}, state, et(16, 30))[0]["level"] == "urgent"
    assert agent.expiry_banner({}, _state(LATER), et(15, 50)) == []
    assert agent.expiry_banner({}, _state(KEY, dry=True), et(15, 50)) == []


# ── pricing + adapters ──────────────────────────────────────────────────────

@pytest.mark.parametrize("bid,limit", [(1.00, 0.95), (2.37, 2.25), (0.10, 0.05),
                                       (0.03, 0.01), (0.0, None), (None, None)])
def test_marketable_sell_limit(bid, limit):
    assert marketable_limit(bid, "sell") == limit


def test_quote_number_tells_zero_from_unknown():
    assert quote_number({"data": [{"bid_price": "0.0000"}]}, "bid_price") == 0.0
    assert quote_number({"data": [{"bid_price": None}]}, "bid_price") is None
    assert quote_number({"data": [{"ask_price": "1"}]}, "bid_price") is None
    assert quote_number({"bid_price": "1.25"}, "bid_price") == 1.25


class FakeRH:
    def __init__(self, results):
        self.results = results
        self.calls = []

    def call_tool(self, name, args):
        self.calls.append((name, args))
        return self.results.get(name, {"content": []})


def _mcp(payload):
    return {"content": [{"type": "text", "text": json.dumps(payload)}]}


def test_robinhood_guard_sell_is_a_bid_based_limit(monkeypatch):
    monkeypatch.setattr(robinhood, "_in_opening_blackout", lambda now=None: False)
    rh = FakeRH({"get_option_quotes": _mcp({"data": {"results": [
                     {"ask_price": "1.20", "bid_price": "1.00"}]}}),
                 "place_option_order": _mcp({"data": {"id": "o1"}})})
    placed = robinhood.place(rh, {"robinhood_account": "1"}, "opt-1", "sell",
                             "close", 2, marketable=True)
    assert placed == {"order_id": "o1", "was_limit": True, "reprice": True}
    args = [a for n, a in rh.calls if n == "place_option_order"][-1]
    assert args["type"] == "limit" and args["price"] == "0.95"
    assert args["legs"][0]["position_effect"] == "close"


def test_robinhood_guard_sell_without_bid_goes_market(monkeypatch):
    monkeypatch.setattr(robinhood, "_in_opening_blackout", lambda now=None: False)
    rh = FakeRH({"place_option_order": _mcp({"data": {"id": "o1"}})})
    robinhood.place(rh, {"robinhood_account": "1"}, "opt-1", "sell", "close", 1,
                    marketable=True)
    args = [a for n, a in rh.calls if n == "place_option_order"][-1]
    assert args["type"] == "market"


def test_robinhood_exit_event_flag_reaches_place(monkeypatch):
    seen = []
    monkeypatch.setattr(robinhood, "place",
                        lambda *a, **kw: seen.append(kw) or None)
    state = {"positions": {KEY: {"option_id": "opt-1", "qty": 1}}}
    robinhood.execute(None, {}, agent._guard_event(KEY, DAY), state)
    assert seen == [{"marketable": True}]


def test_robinhood_exit_bid_reads_the_bid():
    rh = FakeRH({"get_option_quotes": _mcp({"data": {"results": [
        {"bid_price": "0.0000", "mark_price": "0.02"}]}})})
    uid = "14a8e49b-0f10-4d64-9cf9-bd1a68e9da44"
    assert robinhood.exit_bid(rh, {}, {}, uid) == 0.0


def test_robinhood_reconcile_reprices_a_stale_guard_sell(monkeypatch):
    monkeypatch.setattr(robinhood.time, "sleep", lambda s: None)
    monkeypatch.setattr(robinhood, "_in_opening_blackout", lambda now=None: False)
    monkeypatch.setattr(robinhood, "_before_close", lambda now=None: True)
    rh = FakeRH({
        "get_option_orders": _mcp({"data": {"results": [
            {"id": "o-1", "state": "confirmed", "processed_quantity": "0"}]}}),
        "cancel_option_order": _mcp({"ok": True}),
        "get_option_quotes": _mcp({"data": {"results": [{"bid_price": "0.80"}]}}),
        "place_option_order": _mcp({"data": {"id": "o-2"}}),
    })
    state = {"positions": {KEY: {"option_id": "opt-1", "qty": 2, "pending": {
        "order_id": "o-1", "side": "sell", "requested": 2, "filled": 0,
        "was_limit": True, "reprice": True,
        "placed_at": robinhood.time.time() - 60}}}}
    robinhood.reconcile(rh, {"robinhood_account": "1"}, state, lambda s: None)
    pend = state["positions"][KEY]["pending"]
    assert pend["order_id"] == "o-2" and pend["reprice"]
    args = [a for n, a in rh.calls if n == "place_option_order"][-1]
    assert args["type"] == "limit" and args["price"] == "0.75"


def test_robinhood_fresh_guard_sell_is_left_working(monkeypatch):
    monkeypatch.setattr(robinhood, "_in_opening_blackout", lambda now=None: False)
    rh = FakeRH({"get_option_orders": _mcp({"data": {"results": [
        {"id": "o-1", "state": "confirmed", "processed_quantity": "0"}]}})})
    state = {"positions": {KEY: {"option_id": "opt-1", "qty": 2, "pending": {
        "order_id": "o-1", "side": "sell", "requested": 2, "filled": 0,
        "was_limit": True, "reprice": True, "placed_at": robinhood.time.time()}}}}
    robinhood.reconcile(rh, {"robinhood_account": "1"}, state, lambda s: None)
    assert state["positions"][KEY]["pending"]["order_id"] == "o-1"
    assert not [n for n, _ in rh.calls if n == "cancel_option_order"]


def test_alpaca_guard_sell_is_a_limit(monkeypatch):
    posted = []

    class Resp:
        status_code = 200

        def json(self):
            return {"id": "a1"}
    monkeypatch.setattr(alpaca, "_bid", lambda cfg, occ: 2.00)
    monkeypatch.setattr(alpaca.requests, "post",
                        lambda url, **kw: posted.append(kw["json"]) or Resp())
    monkeypatch.setattr(alpaca, "_confirm_fill", lambda cfg, oid, qty, side="buy": qty)
    occ = "RIVN261106C00013500"
    state = {"positions": {KEY: {"option_id": occ, "qty": 1}}}
    assert alpaca.execute(None, {"alpaca_key_id": "k"}, agent._guard_event(KEY, DAY), state)
    assert posted[-1]["type"] == "limit" and posted[-1]["limit_price"] == "1.90"
    assert state["positions"] == {}


def test_alpaca_plain_exit_stays_market(monkeypatch):
    posted = []

    class Resp:
        status_code = 200

        def json(self):
            return {"id": "a1"}
    monkeypatch.setattr(alpaca.requests, "post",
                        lambda url, **kw: posted.append(kw["json"]) or Resp())
    monkeypatch.setattr(alpaca, "_confirm_fill", lambda cfg, oid, qty, side="buy": qty)
    state = {"positions": {KEY: {"option_id": "RIVN261106C00013500", "qty": 1}}}
    ev = dict(agent._guard_event(KEY, DAY))
    ev.pop("exit_limit")
    alpaca.execute(None, {}, ev, state)
    assert posted[-1]["type"] == "market" and "limit_price" not in posted[-1]


# ── notifications ───────────────────────────────────────────────────────────

def test_high_priority_reaches_ntfy(monkeypatch):
    posts = []
    monkeypatch.setattr(notifications.requests, "post",
                        lambda url, **kw: posts.append(kw))
    notifications.notify({"ntfy_topic": "t"}, "hi", priority="high")
    notifications.notify({"ntfy_topic": "t"}, "hi")
    assert posts[0]["headers"]["Priority"] == "urgent"
    assert "Priority" not in posts[1]["headers"]
