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

@pytest.mark.parametrize("bid,limit", [
    (1.00, 0.95), (2.37, 2.25), (0.10, 0.05), (0.11, 0.10), (0.20, 0.15),
    # under $0.10: at the bid itself (a nickel-tick class rejects $0.01)
    (0.09, 0.09), (0.06, 0.06), (0.05, 0.05), (0.03, 0.03), (0.01, 0.01),
    # from $3.00: $0.10 ticks, which every class accepts
    (3.30, 3.10), (3.25, 3.00), (3.20, 3.00), (3.15, 2.95), (10.00, 9.50),
    (0.0, None), (None, None), (0.001, None)])
def test_marketable_sell_limit(bid, limit):
    assert marketable_limit(bid, "sell") == limit


@pytest.mark.parametrize("ask,limit", [
    (1.00, 1.05), (0.01, 0.05), (2.80, 2.95), (2.90, 3.10), (3.00, 3.20),
    (3.05, 3.30), (10.00, 10.50)])
def test_marketable_buy_limit(ask, limit):
    assert marketable_limit(ask, "buy") == limit


def test_marketable_limit_is_always_a_valid_tick():
    """Every price it returns is valid for a nickel/dime-tick class (or is
    the bid itself, under $0.10), and a sell never goes below $0.05 when
    the bid is at least that."""
    for cents in range(1, 2000):
        p = cents / 100
        for side in ("buy", "sell"):
            got = round(marketable_limit(p, side) * 100)
            if side == "sell" and cents < 10:
                assert got == cents
                continue
            assert got % (10 if got >= 300 else 5) == 0, (p, side, got)
            if side == "sell":
                assert 5 <= got <= cents
            else:
                assert got >= cents


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
    monkeypatch.setattr(robinhood, "_before_close", lambda now=None, cfg=None: True)
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


# ── review fixes ────────────────────────────────────────────────────────────

def _entry(expiry=DAY, **over):
    ev = {"event": "ENTERED", "ticker": "RIVN", "expiry": expiry, "strike": 13.5,
          "type": "C", "event_id": "e-1"}
    ev.update(over)
    return ev


def test_no_new_0dte_entry_past_the_guard_time(monkeypatch, no_policy):
    monkeypatch.setattr(agent, "_et_now", lambda: et(15, 46))
    state = {"positions": {}, "seen": []}
    b = StubBroker()
    out = agent.handle_event(_entry(), {}, state, b, object(), lambda s: None)
    assert out.startswith("SKIPPED") and "expiry guard time (15:45 ET)" in out
    assert b.calls == [] and state["positions"] == {}
    (row,) = [r for r in _log() if r.get("event_id") == "e-1"]
    assert row["action"] == "skip_rule" and "expires today" in row["reason"]


def test_0dte_entry_before_the_guard_time_and_later_expiries_still_buy(
        monkeypatch, no_policy):
    monkeypatch.setattr(agent, "_et_now", lambda: et(15, 44))
    state = {"positions": {}, "seen": []}
    b = StubBroker()
    assert agent.handle_event(_entry(), {}, state, b, object(), lambda s: None)
    monkeypatch.setattr(agent, "_et_now", lambda: et(15, 50))
    agent.handle_event(_entry(expiry="2026-11-13", event_id="e-2"), {}, state, b,
                       object(), lambda s: None)
    assert [e["event_id"] for e in b.calls] == ["e-1", "e-2"]


def test_0dte_entry_late_is_allowed_when_the_guard_is_off(monkeypatch, no_policy):
    monkeypatch.setattr(agent, "_et_now", lambda: et(15, 50))
    b = StubBroker()
    agent.handle_event(_entry(), {"expiry_guard_et": "off"},
                       {"positions": {}, "seen": []}, b, object(), lambda s: None)
    assert len(b.calls) == 1


def test_0dte_entry_block_follows_an_early_close(monkeypatch, no_policy):
    monkeypatch.setattr(agent, "_et_now", lambda: et(12, 46, day=(2026, 11, 27)))
    b = StubBroker()
    out = agent.handle_event(_entry(expiry="2026-11-27"), {},
                             {"positions": {}, "seen": []}, b, object(),
                             lambda s: None)
    assert out.startswith("SKIPPED") and "12:45" in out and b.calls == []


def test_whole_number_strike_key_round_trips_and_sells():
    key = f"SPY|{DAY}|600|C"               # a custom source's int strike
    state = _state(key)
    b = GuardBroker()
    assert _tick(state, b, et(15, 45)) == [f"EXIT: SPY {DAY} $600 C"]
    assert b.calls[0]["strike"] == 600 and key not in state["positions"]


def test_unreadable_row_notifies_once_with_high_priority(quiet):
    key = f"SPY|{DAY}|0600|C"              # can't be rebuilt exactly
    state = _state(key)
    b = GuardBroker()
    _tick(state, b, et(15, 45))
    _tick(state, b, et(15, 46))
    assert b.calls == []
    notes = [(m, p) for m, p in quiet if "can't read" in m]
    assert len(notes) == 1 and notes[0][1] == "high"
    assert "sell it in your broker app" in notes[0][0]


@pytest.mark.parametrize("day,early", [
    ("2026-11-27", True), ("2027-11-26", True), ("2028-11-24", True),
    ("2031-11-28", True), ("2026-11-26", False), ("2026-11-20", False),
    ("2026-12-24", True), ("2030-12-24", True), ("2027-12-24", False),  # Fri: closed
    ("2025-07-03", True), ("2029-07-03", True), ("2026-07-03", False),  # Fri: closed
    ("2026-12-23", False), ("2026-07-02", False)])
def test_early_closes_by_rule(day, early):
    from brokers import market_hours
    assert market_hours.is_rule_early_close(day) is early
    assert market_hours.close_minute({}, day) == (780 if early else 960)


def test_early_close_by_rule_after_the_old_list_ends():
    key = "SPY|2028-11-24|600.0|C"
    state = _state(key)
    b = GuardBroker()
    _tick(state, b, et(12, 44, day=(2028, 11, 24)))
    assert b.calls == []
    _tick(state, b, et(12, 45, day=(2028, 11, 24)))
    assert len(b.calls) == 1


def test_early_close_never_moves_the_guard_before_the_open():
    cfg = {"expiry_guard_et": "10:00", "expiry_alert_et": "10:05"}
    assert agent._expiry_times(cfg, "2026-11-27") == (570, 570, 780)
    key = "SPY|2026-11-27|600.0|C"
    state = _state(key)
    b = GuardBroker()
    _tick(state, b, et(9, 0, day=(2026, 11, 27)), cfg=cfg)
    _tick(state, b, et(9, 29, day=(2026, 11, 27)), cfg=cfg)
    assert b.calls == []
    _tick(state, b, et(9, 30, day=(2026, 11, 27)), cfg=cfg)
    assert len(b.calls) == 1


def test_robinhood_before_close_knows_early_closes():
    assert robinhood._before_close(et(12, 59, day=(2026, 11, 27)))
    assert not robinhood._before_close(et(13, 0, day=(2026, 11, 27)))
    assert not robinhood._before_close(et(13, 30, day=(2026, 11, 27)))
    assert robinhood._before_close(et(15, 59))
    assert not robinhood._before_close(et(16, 0))
    extra = {"early_close_days": ["2027-07-02"]}
    assert robinhood._before_close(et(12, 30, day=(2027, 7, 2)), cfg=extra)
    assert not robinhood._before_close(et(13, 30, day=(2027, 7, 2)), cfg=extra)


def test_robinhood_reconcile_stops_repricing_after_an_early_close(monkeypatch):
    monkeypatch.setattr(robinhood.time, "sleep", lambda s: None)
    monkeypatch.setattr(robinhood, "_in_opening_blackout", lambda now=None: False)
    real = robinhood._before_close
    monkeypatch.setattr(robinhood, "_before_close",
                        lambda now=None, cfg=None: real(et(13, 5, day=(2026, 11, 27)), cfg))
    rh = FakeRH({"get_option_orders": _mcp({"data": {"results": [
        {"id": "o-1", "state": "confirmed", "processed_quantity": "0"}]}})})
    state = {"positions": {KEY: {"option_id": "opt-1", "qty": 2, "pending": {
        "order_id": "o-1", "side": "sell", "requested": 2, "filled": 0,
        "was_limit": True, "reprice": True,
        "placed_at": robinhood.time.time() - 60}}}}
    robinhood.reconcile(rh, {"robinhood_account": "1"}, state, lambda s: None)
    assert not [n for n, _ in rh.calls if n == "cancel_option_order"]


def test_startup_notice_runs_with_the_alert_off():
    state = _state(KEY)
    sent = _wake(state, None, et(10, 0), cfg={"expiry_alert_et": "off"})
    assert len(sent) == 1 and "sells it at 15:45" in sent[0]


def test_startup_notice_silent_when_both_are_off():
    state = _state(KEY)
    cfg = {"expiry_guard_et": "off", "expiry_alert_et": "off"}
    assert _wake(state, None, et(10, 0), cfg=cfg) == []


def test_banner_says_who_is_not_selling():
    state = _state(KEY)
    paused = agent.expiry_banner({}, state, et(15, 46), paused=True)[0]
    assert paused["level"] == "urgent" and "paused" in paused["text"]
    assert "guard is selling" not in paused["text"]
    dry = agent.expiry_banner({"dry_run": True}, state, et(15, 46))[0]
    assert dry["level"] == "urgent" and "dry-run" in dry["text"]
    other = agent.expiry_banner({}, _state(KEY, acct="alpaca:live"), et(15, 46),
                                acct="robinhood:abc")[0]
    assert other["level"] == "urgent" and "different broker account" in other["text"]
    early = agent.expiry_banner({}, state, et(10, 0), paused=True)[0]
    assert early["level"] == "warn" and "paused" in early["text"]
    same = agent.expiry_banner({}, _state(KEY, acct="robinhood:abc"), et(15, 46),
                               acct="robinhood:abc")[0]
    assert same["level"] == "warn" and "guard is selling" in same["text"]


def test_alert_names_a_different_account(quiet):
    state = _state(KEY, acct="alpaca:live")
    _tick(state, GuardBroker(), et(15, 50), cfg={"broker": "robinhood"})
    (msg,) = [m for m, _ in quiet if "sell it now" in m]
    assert "different broker account" in msg


# heartbeat persistence

def test_beat_save_due_every_loop_while_busy_and_once_after_the_close():
    state = _state(KEY)
    t = et(15, 46).timestamp()
    assert agent._beat_save_due({}, state, t - 10, t, et(15, 46))      # busy
    assert not agent._beat_save_due({}, {"positions": {}}, t - 10, t, et(15, 46))
    close = et(16, 0).timestamp()
    after = et(16, 0, s=20)
    assert agent._beat_save_due({}, {"positions": {}}, close - 30,
                                after.timestamp(), after)
    assert not agent._beat_save_due({}, {"positions": {}}, close + 5,
                                    after.timestamp(), after)
    assert agent._beat_save_due({}, {"positions": {}}, t - 130, t, et(15, 46))


def test_no_false_offline_alert_after_a_stop_just_past_the_close():
    """Stopped at 16:01 with the heartbeat saved on the stop: the next start
    doesn't call the 16:00 expiry 'offline'."""
    state = _state(KEY)
    agent._save_heartbeat(state, lambda s: None, et(16, 1).timestamp())
    assert _wake(state, state["heartbeat_at"], et(16, 30)) == []


# ── holdings-reading broker: the guard goes through the exit checks ─────────

U1 = "14a8e49b-0f10-4d64-9cf9-bd1a68e9da44"
HCFG = {"broker": "robinhood", "robinhood_account": "A1"}


class HeldBroker(GuardBroker):
    """GuardBroker that reads holdings (and the owner's sells)."""

    def __init__(self, held, fills=(), bid=1.0):
        super().__init__(bid=bid)
        self.held, self.fills, self.asked = held, list(fills), []

    def held_option_qty(self, client, cfg, option_ids):
        self.asked.append(list(option_ids))
        return {i: self.held for i in option_ids}

    def closed_fills(self, client, cfg, option_id, since_iso=None,
                     own_order_ids=(), exclude_agentic=False):
        return list(self.fills)


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def held_clock(monkeypatch):
    c = Clock(et(15, 45).timestamp())
    monkeypatch.setattr(agent, "_now", c)
    monkeypatch.setattr(agent, "_market_open_now", lambda: True)
    monkeypatch.setattr(agent, "_in_open_rush", lambda: False)
    monkeypatch.setitem(agent._POS_SYNC, "next_at", 0.0)
    return c


def _held_state(c, **over):
    from brokers import account_tag
    tag = account_tag(HCFG)
    row = {"option_id": U1, "qty": 2, "opened_event": "open-1", "entry_mark": 1.0,
           "acct": tag, "opened_at": c.t - 3600, "last_order_at": c.t - 3600,
           "order_ids": ["o-agent"]}
    row.update(over)
    return {"positions": {KEY: row}, "seen": [],
            "position_sync": {"proven": {tag: "2026-11-05T15:00:00+00:00"}}}


def _htick(state, b, c, now):
    c.t = now.timestamp()
    return agent.expiry_guard_tick(HCFG, state, lambda s: None, b, object(),
                                   now_et=now)


def test_guard_sells_when_the_account_holds_it(held_clock):
    state = _held_state(held_clock)
    b = HeldBroker(2)
    assert _htick(state, b, held_clock, et(15, 45)) == [f"EXIT: {CONTRACT}"]
    assert len(b.calls) == 1 and b.asked


def _every_10s():
    """The guard's 10 s cadence from 15:45:00 to 15:49:50."""
    return [(m, s) for m in range(45, 50) for s in range(0, 60, 10)]


def test_guard_does_not_sell_a_position_the_owner_closed(held_clock):
    fill = {"order_id": "u1", "qty": 2, "price": 0.2,
            "at": "2026-11-06T19:00:00Z", "by": "user"}
    state = _held_state(held_clock)
    b = HeldBroker(0, fills=[fill])
    for m, s in _every_10s():
        _htick(state, b, held_clock, et(15, m, s=s))
    assert b.calls == []
    assert KEY not in state["positions"]
    assert [r for r in _log() if r.get("action") == "exit" and r.get("manual")]


def test_guard_waits_on_a_partial_shortfall_then_sells_only_what_is_held(
        held_clock, quiet):
    state = _held_state(held_clock)
    sold = []

    class Recording(HeldBroker):
        def execute(self, client, cfg, event, st):
            sold.append(st["positions"][KEY]["qty"])
            return super().execute(client, cfg, event, st)
    b = Recording(1)                      # the ledger says 2
    _htick(state, b, held_clock, et(15, 45))
    assert sold == [] and state["positions"][KEY]["qty"] == 2   # waits
    for m, s in _every_10s()[1:]:
        _htick(state, b, held_clock, et(15, m, s=s))
    assert sold == [1] and KEY not in state["positions"]
    assert any("no longer holds 1x" in m for m, _ in quiet)


def test_guard_never_sells_another_accounts_row(held_clock, quiet):
    state = _held_state(held_clock, acct="alpaca:live")
    b = HeldBroker(2)
    for m in (45, 46, 47):
        _htick(state, b, held_clock, et(15, m))
    assert b.calls == [] and KEY in state["positions"]
    problems = [r for r in _log() if r.get("action") == "exit_failed"]
    assert len(problems) == 1 and "different broker" in problems[0]["reason"]
    assert len([m for m, _ in quiet if "NOT placed" in m]) <= 1


# ── the main loop ───────────────────────────────────────────────────────────

def _run(home, monkeypatch, state, events=(), poll_error=None, sleeps=1,
         execute_ok=True, clock=None, on_sleep=None, on_poll=None):
    """`agent.py run` with a stub feed and broker. Returns (calls, waits,
    saved state)."""
    import brokers
    import keep_running
    import webui
    calls, waits = [], []

    class Broker:
        @staticmethod
        def client(cfg):
            return object()

        @staticmethod
        def execute(client, cfg, event, st):
            calls.append(("execute", event["event_id"]))
            if not execute_ok:
                return False
            key = f"{event['ticker']}|{event['expiry']}|{event['strike']}|{event['type']}"
            return st["positions"].pop(key, None) is not None

    class Source:
        @staticmethod
        def poll(cfg, st, save_state):
            calls.append("poll")
            if on_poll:
                on_poll()
            if poll_error:
                raise poll_error
            return list(events)

    def no_dashboard(*a, **k):
        raise OSError("no dashboard in tests")

    def sleep(wait, controls):
        waits.append(wait)
        if len(waits) >= sleeps:
            raise KeyboardInterrupt
        if on_sleep:
            on_sleep()
    monkeypatch.setattr(agent, "require_consent_or_exit", lambda: None)
    monkeypatch.setattr(brokers, "broker_ready", lambda c: True)
    monkeypatch.setitem(agent.BROKERS, "stub", Broker)
    monkeypatch.setitem(agent.SOURCES, "stub", Source)
    monkeypatch.setattr(webui, "start_dashboard", no_dashboard)
    monkeypatch.setattr(keep_running, "refresh_autostart", lambda: False)
    monkeypatch.setattr(agent, "_refresh_cash", lambda *a, **k: None)
    monkeypatch.setattr(agent, "_position_upkeep", lambda *a, **k: None)
    monkeypatch.setattr(agent, "_market_open_now", lambda: True)
    monkeypatch.setitem(webui.CONTROLS, "paused", False)
    monkeypatch.setitem(webui.CONTROLS, "stop", False)
    monkeypatch.setattr(agent, "_sleep_unless_stopped", sleep)
    if clock is not None:
        monkeypatch.setattr(agent, "_now", clock)
    (home / "config.json").write_text(json.dumps({"source": "stub", "broker": "stub"}))
    (home / "state.json").write_text(json.dumps(state))
    agent.cmd_run()
    return calls, waits, json.loads((home / "state.json").read_text())


def test_loop_guard_sells_after_the_feed_even_when_the_feed_is_down(
        home, monkeypatch):
    monkeypatch.setattr(agent, "_et_now", lambda: et(15, 46))
    state = {"seen": [], **_state(KEY, option_id="opt-1", qty=1)}
    calls, _w, saved = _run(home, monkeypatch, state,
                            poll_error=ConnectionError("feed unreachable"))
    assert calls == ["poll", ("execute", f"expiry-guard|{KEY}|{DAY}")]
    assert KEY not in saved["positions"]


def test_loop_feed_exit_and_guard_same_row_sells_once(home, monkeypatch):
    monkeypatch.setattr(agent, "_et_now", lambda: et(15, 46))
    state = {"seen": [], **_state(KEY, option_id="opt-1", qty=1)}
    exit_ev = {"event": "EXITED", "ticker": "RIVN", "expiry": DAY, "strike": 13.5,
               "type": "C", "event_id": "feed-x"}
    calls, _w, saved = _run(home, monkeypatch, state, events=[exit_ev])
    assert [c for c in calls if c != "poll"] == [("execute", "feed-x")]
    assert saved["positions"] == {}


def test_loop_polls_every_10s_while_the_guard_has_work(home, monkeypatch):
    monkeypatch.setattr(agent, "_et_now", lambda: et(15, 46))
    state = {"seen": [], **_state(KEY, option_id="opt-1", qty=1)}
    _c, waits, saved = _run(home, monkeypatch, state, execute_ok=False)
    assert waits == [agent.EXPIRY_GUARD_POLL_S]
    assert saved["heartbeat_at"]                 # saved every loop while busy
    monkeypatch.setattr(agent, "_et_now", lambda: et(14, 0))
    _c, waits, _s = _run(home, monkeypatch, state, execute_ok=False)
    assert waits == [30]


def test_loop_saves_the_heartbeat_on_a_stop(home, monkeypatch):
    monkeypatch.setattr(agent, "_et_now", lambda: et(16, 1))
    c = Clock(et(16, 1).timestamp())
    _c, _w, saved = _run(home, monkeypatch, {"seen": [], "positions": {}}, clock=c)
    assert saved["heartbeat_at"] == c.t


def _spy_wake(monkeypatch):
    seen = []
    real = agent.expiry_wake_check
    monkeypatch.setattr(agent, "expiry_wake_check",
                        lambda cfg, st, save, prev, now_et=None:
                        seen.append(prev) or real(cfg, st, save, prev, now_et))
    return seen


def test_loop_runs_the_wake_check_after_a_sleep(home, monkeypatch):
    monkeypatch.setattr(agent, "_et_now", lambda: et(10, 0))
    c = Clock(et(10, 0).timestamp())
    seen = _spy_wake(monkeypatch)

    def slept():
        c.t += 3600                       # the computer slept an hour
    _run(home, monkeypatch, {"seen": [], "positions": {}}, sleeps=2, clock=c,
         on_sleep=slept)
    assert seen == [None, et(10, 0).timestamp()]


def test_loop_runs_the_wake_check_after_a_hang(home, monkeypatch):
    """A loop that hangs inside (a stuck call), not in its sleep, is a gap
    too: measured from the previous loop's start."""
    monkeypatch.setattr(agent, "_et_now", lambda: et(10, 0))
    c = Clock(et(10, 0).timestamp())
    seen = _spy_wake(monkeypatch)
    hung = []

    def hang():
        if not hung:
            hung.append(1)
            c.t += 900                    # the feed call hung 15 min
    _run(home, monkeypatch, {"seen": [], "positions": {}}, sleeps=2, clock=c,
         on_poll=hang)
    assert seen == [None, et(10, 0).timestamp()]
