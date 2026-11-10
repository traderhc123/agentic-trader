"""The /v2 view-model never says more than the agent knows (review of PRs
#78/#79): real positions are not shown as sold on an exit signal when no
exit signal can arrive; an order with no clear answer is not "rejected"; a
sell still working is not "filled"; the expiry sell is only promised when it
is on; a no-bid contract is not "being sold"; a sale with no estimate is not
"nothing closed"; a broker paper account is not real money; and damaged log
or ledger values degrade one field, never the whole page."""

import json
from datetime import datetime, timedelta, timezone

import agent
import view_model as vm

NOW = datetime(2026, 11, 9, 19, 41, 7, tzinfo=timezone.utc)  # Mon 2:41 PM ET
TODAY = "2026-11-09"
ACCT = "robinhood:0123456789"
SPY = "SPY|2026-11-13|700.0|C"
IWM_TODAY = f"IWM|{TODAY}|245.0|C"
LIVE = {"source": "agenthc", "broker": "robinhood", "dry_run": False,
        "agenthc_api_key": ""}


def at(hh, mm=0, day=TODAY):
    d = datetime.strptime(day, "%Y-%m-%d").replace(
        hour=hh, minute=mm, tzinfo=timezone.utc) + timedelta(hours=5)
    return d.isoformat(timespec="seconds")


def runtime(**over):
    t = NOW.timestamp()
    rt = {"running": True, "started_at": t - 7200, "heartbeat_at": t - 12,
          "wait_s": 30.0, "poll_at": t - 12, "poll_ok": True,
          "feed_access": True, "signal_at": None,
          "broker": {"id": "robinhood", "account_tag": ACCT, "connected": True}}
    rt.update(over)
    return rt


def real_pos(**over):
    row = {"option_id": "abc", "qty": 1, "live": True, "entry_mark": 4.12,
           "opened_at": NOW.timestamp() - 90000, "acct": ACCT}
    row.update(over)
    return row


def build(cfg=LIVE, st=None, rows=(), rt=None, now=NOW):
    out = vm.build_status(cfg, st if st is not None else {"positions": {}},
                          [{**r, "n": i} for i, r in enumerate(rows)],
                          runtime() if rt is None else rt, now=now)
    assert vm.validate(out) == [], vm.validate(out)
    json.loads(json.dumps(out, allow_nan=False))
    return out


def tid(token):
    from sources import agenthc_day_trade_ideas as hc
    return hc._token_id(token)


# ── 1. no exit signal can arrive: nothing is "sold on its exit signal" ──────

PREPAID = {**LIVE, "feed_pass": {"token": "tok"}}
LAPSED = "Your AgentHC feed pass has run out — buy more signal days."


def lapsed_state(**pos):
    return {"positions": pos or {SPY: real_pos()},
            "pass_status": {tid("tok"): {"status": "used_up"}},
            "pass_notice": LAPSED}


def test_a_lapsed_pass_leaves_real_positions_unmanaged():
    s = build(PREPAID, lapsed_state(), rt=runtime(feed_access=False))
    assert s["signals"]["pass"]["status"] == "waiting"
    spy = s["positions"][0]
    assert spy["next"]["action"] == "unmanaged"
    assert spy["next"]["reason_code"] == "no_exit_signals"
    assert s["real_positions_managed"] is False
    assert s["unmanaged_reason"] == "no_exit_signals"
    assert s["capabilities"]["closing"] == {"state": "cant",
                                            "reason_code": "no_signal_access"}
    assert s["capabilities"]["listening"] == {"state": "cant",
                                              "reason_code": "no_signal_access"}
    # the agent's own message about the pass, as the classic page shows it
    assert {"level": "warn", "code": "pass", "text": LAPSED} in s["notices"]


def test_a_refused_day_pass_leaves_real_positions_unmanaged():
    wallet = {**LIVE, "lnbits_url": "https://wallet.example",
              "day_pass_recurring": True}
    l402 = {"token": "t", "expires_at": NOW.timestamp() + 3600,
            "refused_at": NOW.timestamp() - 60}
    s = build(wallet, {"positions": {SPY: real_pos()}, "l402": l402},
              rt=runtime(feed_access=False))
    assert s["signals"]["pass"]["status"] == "active"   # paid, but refused
    assert s["positions"][0]["next"]["reason_code"] == "no_exit_signals"
    assert s["capabilities"]["closing"]["state"] == "cant"
    # accepted again: looked after
    ok = build(wallet, {"positions": {SPY: real_pos()},
                        "l402": {**l402, "refused_at": None}},
               rt=runtime(feed_access=True))
    assert ok["positions"][0]["next"]["action"] == "sell_on_exit"


def test_an_unreachable_feed_is_shown_as_such():
    s = build({**LIVE, "agenthc_api_key": "k"}, {"positions": {SPY: real_pos()}},
              rt=runtime(poll_ok=False, feed_access=None))
    assert s["positions"][0]["next"] == {
        "action": "unknown", "at": None, "reason_code": "feed_unreachable",
        "expiry_guard_at": "2026-11-13T20:45:00Z"}
    assert s["real_positions_managed"] is False
    assert s["capabilities"]["closing"] == {"state": "cant",
                                            "reason_code": "feed_unreachable"}


def test_exit_signals_that_still_arrive_keep_positions_managed():
    # used up, but in its exits-only days: exits still come
    st = {"positions": {SPY: real_pos()},
          "pass_status": {tid("tok"): {"status": "exits_only"}}}
    s = build(PREPAID, st, rt=runtime(feed_access=False))
    assert s["positions"][0]["next"]["action"] == "sell_on_exit"
    assert s["real_positions_managed"] is True
    # wallet auto-pay overnight: it buys a pass at the next market-day read
    wallet = {**LIVE, "lnbits_url": "https://wallet.example",
              "day_pass_recurring": True}
    night = NOW.replace(hour=2)
    s = build(wallet, {"positions": {SPY: real_pos()}},
              rt=runtime(feed_access=False, heartbeat_at=night.timestamp()),
              now=night)
    assert s["signals"]["pass"]["status"] == "waiting"
    assert s["real_positions_managed"] is True
    # ...but with the market open and still no access, nothing reaches it
    s = build(wallet, {"positions": {SPY: real_pos()}},
              rt=runtime(feed_access=False))
    assert s["positions"][0]["next"]["reason_code"] == "no_exit_signals"


def test_the_expiry_sell_still_runs_without_signals():
    s = build(PREPAID, lapsed_state(**{IWM_TODAY: real_pos()}),
              rt=runtime(feed_access=False))
    nxt = s["positions"][0]["next"]
    assert nxt["reason_code"] == "no_exit_signals"
    assert nxt["expiry_guard_at"] == "2026-11-09T20:45:00Z"
    # past the guard time the guard is selling it: that is still said
    late = NOW.replace(hour=20, minute=50)
    s = build(PREPAID, lapsed_state(**{IWM_TODAY: real_pos()}),
              rt=runtime(feed_access=False, heartbeat_at=late.timestamp()),
              now=late)
    assert s["positions"][0]["next"]["action"] == "selling_now"


def test_the_pass_notice_is_not_repeated_while_a_pass_has_days_left():
    st = {"positions": {}, "pass_notice": "3 signal days left",
          "pass_status": {tid("tok"): {"status": "active", "days_left": 3}}}
    assert not [n for n in build(PREPAID, st)["notices"] if n["code"] == "pass"]


WALLET = {**LIVE, "lnbits_url": "https://wallet.example",
          "day_pass_recurring": True}


def test_a_running_wallet_day_pass_is_the_pass_the_agent_reads_with():
    # prepaid pass used up, wallet auto-pay on and today's day-pass running:
    # hc.poll reads with the day-pass (hc._live_daypass comes first)
    st = {**lapsed_state(), "l402": {"token": "t",
                                     "expires_at": NOW.timestamp() + 3600}}
    s = build({**WALLET, "feed_pass": {"token": "tok"}}, st)
    assert s["signals"]["pass"] == {"kind": "day_pass", "status": "active",
                                    "days_left": None, "auto_pay": True}
    assert s["positions"][0]["next"]["action"] == "sell_on_exit"
    assert s["real_positions_managed"] is True
    caps = s["capabilities"]
    assert caps["closing"]["state"] == caps["listening"]["state"] == "yes"
    # the lapse notice from before is not shown while the feed is read
    assert not [n for n in s["notices"] if n["code"] == "pass"]
    # an expired day-pass (or one with no token) is not "running"
    for l402 in ({"token": "t", "expires_at": NOW.timestamp() - 1},
                 {"expires_at": NOW.timestamp() + 3600}):
        s = build({**WALLET, "feed_pass": {"token": "tok"}},
                  {**lapsed_state(), "l402": l402},
                  rt=runtime(feed_access=False))
        assert s["signals"]["pass"]["status"] == "waiting"


def test_a_pass_that_could_not_be_checked_is_not_called_used_up():
    # the status check failed: no record, and hc.poll reads nothing
    st = {"positions": {SPY: real_pos()}, "pass_status": {}}
    s = build(PREPAID, st, rt=runtime(feed_access=False))
    assert s["signals"]["pass"]["status"] == "unknown"
    assert s["positions"][0]["next"]["action"] == "unknown"
    assert s["positions"][0]["next"]["reason_code"] == "pass_unchecked"
    assert s["unmanaged_reason"] == "pass_unchecked"
    for which in ("opening", "closing", "listening"):
        assert s["capabilities"][which] == {"state": "cant",
                                            "reason_code": "pass_unchecked"}
    # one pass checked and used up, another not checked: still not known
    two = {**PREPAID, "feed_pass_pending": {"token": "new"}}
    st = {"positions": {}, "pass_status": {tid("tok"): {"status": "used_up"}}}
    assert build(two, st, rt=runtime(feed_access=False))[
        "signals"]["pass"]["status"] == "unknown"


def test_a_stale_pass_notice_is_not_shown_once_it_is_no_longer_true():
    def pass_notes(cfg, st, **rt):
        return [n for n in build(cfg, st, rt=runtime(**rt))["notices"]
                if n["code"] == "pass"]
    # an API key now
    assert not pass_notes({**PREPAID, "agenthc_api_key": "k"},
                          lapsed_state(), feed_access=False)
    # the feed was read with entry access
    assert not pass_notes(PREPAID, lapsed_state(), feed_access=True)
    # wallet auto-pay switched on since: the wallet buys the day-pass
    assert not pass_notes({**WALLET, "feed_pass": {"token": "tok"}},
                          lapsed_state(), feed_access=False)
    # ...but a current day-pass alert is shown
    alert = {**lapsed_state(), "daypass_alert": "daypass_refused_notified"}
    assert pass_notes({**WALLET, "feed_pass": {"token": "tok"}}, alert,
                      feed_access=False)
    # and the lapse itself, with no wallet, is shown
    assert pass_notes(PREPAID, lapsed_state(), feed_access=False)


# ── 2. an order with no clear answer is not "rejected" ──────────────────────

def test_a_placed_problem_is_recorded_as_unconfirmed(home, monkeypatch):
    monkeypatch.setattr(agent, "notify", lambda *a, **k: None)
    monkeypatch.setattr(agent, "_problems_logged", set())
    for kind, placed in (("order_failed", True), ("exit_failed", True),
                         ("order_failed", False), ("entry_unfilled", True)):
        agent._report_problem(LIVE, {}, lambda s: None, kind, "SPY", "x",
                              placed=placed)
    rows = agent._read_trade_log()
    got = [(r["ui"].get("code"), r["ui"].get("sent")) for r in rows]
    assert got == [("buy_unconfirmed", True), ("sell_unconfirmed", True),
                   (None, None), ("buy_partly_filled", True)]
    a = [vm.activity_row(r) for r in rows]
    assert [x["reason_code"] for x in a] == [
        "buy_unconfirmed", "sell_unconfirmed", "order_rejected",
        "buy_partly_filled"]
    # sent, and whether it filled is not known
    assert (a[0]["order_sent"], a[0]["filled"]) == (True, None)
    assert (a[1]["order_sent"], a[1]["filled"]) == (True, None)


# ── 3. a sell still working is not "filled" ─────────────────────────────────

def test_an_exit_whose_sell_is_still_working_is_not_filled():
    prior = {"qty": 3, "acct": ACCT}
    working = {"qty": 3, "acct": ACCT,
               "pending": {"side": "sell", "requested": 3}}
    rec = agent._trade_facts({"action": "exit"}, LIVE, "main", False,
                             working, prior, 1.2, 3)
    assert rec["ui"]["filled"] is False and rec["ui"]["qty"] == 3
    assert rec["ui"]["sent"] is True
    done = agent._trade_facts({"action": "exit"}, LIVE, "main", False,
                              None, prior, 1.2, 3)
    assert done["ui"]["filled"] is True
    # a damaged ledger row never costs the trade-log row
    odd = agent._trade_facts({"action": "exit"}, LIVE, "main", False,
                             {"pending": "?"}, prior, 1.2, 3)
    assert odd["ui"]["filled"] is True


# ── 4. the expiry sell is promised only when it is on ───────────────────────

def test_the_status_says_whether_the_expiry_sell_is_on():
    st = {"positions": {SPY: real_pos()}}
    assert build(st=st)["expiry_guard_on"] is True
    off = build({**LIVE, "expiry_guard_et": "off"}, st)
    assert off["expiry_guard_on"] is False
    assert off["positions"][0]["next"]["expiry_guard_at"] is None


# ── 5. no bid: the guard is not selling it ──────────────────────────────────

def test_a_position_the_guard_left_for_no_bid_is_not_being_sold():
    cfg = {**LIVE, "agenthc_api_key": "k", "expiry_guard_et": "14:30"}
    st = {"positions": {IWM_TODAY: real_pos(expiry_no_bid=TODAY)}}
    s = build(cfg, st)
    assert s["positions"][0]["next"]["action"] == "unmanaged"
    assert s["positions"][0]["next"]["reason_code"] == "no_bid"
    assert s["real_positions_managed"] is False
    assert s["unmanaged_reason"] == "no_bid"
    # a flag from an earlier day says nothing about today
    st = {"positions": {IWM_TODAY: real_pos(expiry_no_bid="2026-11-06")}}
    assert build(cfg, st)["positions"][0]["next"]["action"] == "selling_now"


# ── 6. a sale with no estimate is still a sale ──────────────────────────────

def test_sales_today_with_no_estimate_are_counted_for_today():
    c = "SPY 2026-11-13 $700.0 C"
    rows = [{"action": "exit", "contract": c, "dry": False, "ts": at(10)},
            {"action": "exit", "contract": c, "dry": False,
             "ts": at(10, 0, "2026-11-06")}]
    real = build(rows=rows)["results"]["real"]
    assert real["unmeasured"] == 2 and real["unmeasured_today"] == 1
    assert real["days"] == [] and real["closed"] == 0


# ── 7. a broker paper account is not real money ─────────────────────────────

def test_paper_account_fills_are_booked_apart():
    rec = agent._trade_facts({"action": "exit"}, LIVE, "main", False, None,
                             {"qty": 1, "acct": "alpaca:paper"}, 1.0, 1)
    assert rec["ui"]["paper"] is True
    live = agent._trade_facts({"action": "exit"}, LIVE, "main", False, None,
                              {"qty": 1, "acct": "alpaca:live"}, 1.0, 1)
    assert live["ui"]["paper"] is False
    unknown = agent._trade_facts({"action": "exit"}, LIVE, "main", False,
                                 None, {"qty": 1}, 1.0, 1)
    assert "paper" not in unknown["ui"]
    c = "SPY 2026-11-13 $700.0 C"
    rows = [{**rec, "contract": c, "dry": False, "ts": at(10),
             "est_pnl_usd": 50.0},
            {**live, "contract": c, "dry": False, "ts": at(11),
             "est_pnl_usd": -20.0}]
    s = build(rows=rows)
    assert s["results"]["paper_account"]["total"] == 50.0
    assert s["results"]["real"]["total"] == -20.0
    assert [r["paper_account"] for r in s["activity"]["rows"]] == [False, True]


def test_problem_rows_record_a_paper_account(home, monkeypatch):
    monkeypatch.setattr(agent, "notify", lambda *a, **k: None)
    monkeypatch.setattr(agent, "_problems_logged", set())
    paper = {**LIVE, "broker": "alpaca", "alpaca_paper": True}
    real = {**LIVE, "broker": "alpaca", "alpaca_paper": False}
    agent._report_problem(paper, {}, lambda s: None, "order_failed", "SPY",
                          "x", placed=True)
    agent._report_problem(paper, {}, lambda s: None, "entry_unfilled", "SPY",
                          "x", placed=True)
    agent._report_problem(real, {}, lambda s: None, "exit_failed", "SPY",
                          "x", placed=True)
    agent._report_problem({**paper, "dry_run": True}, {}, lambda s: None,
                          "order_failed", "SPY", "x", dry=True)
    # a skipped signal sent nothing: no account to record
    agent._report_problem(paper, {}, lambda s: None, "skip_stale", "SPY", "x")
    rows = agent._read_trade_log()
    assert [r["ui"].get("paper") for r in rows] == [True, True, False, None,
                                                    None]
    a = [vm.activity_row(r) for r in rows]
    assert [x["paper_account"] for x in a] == [True, True, False, None, None]
    # an account tag that can't be read records nothing, and never raises
    monkeypatch.setattr(agent, "_account_tag",
                        lambda *a, **k: (_ for _ in ()).throw(ValueError()))
    assert agent._paper_account(paper) is None
    agent._report_problem(paper, {}, lambda s: None, "exit_failed", "QQQ",
                          "y", placed=True)
    last = agent._read_trade_log()[-1]
    assert last["action"] == "exit_failed" and "paper" not in last["ui"]


def test_today_reads_the_paper_account_book_in_live_mode():
    c = "QQQ 2026-11-13 $600.0 C"
    paper = {**LIVE, "broker": "alpaca", "alpaca_paper": True}
    rows = [{"action": "entry", "contract": c, "dry": False, "ts": at(9),
             "ui": {"mode": "live", "paper": True, "sent": True}},
            {"action": "exit", "contract": c, "dry": False, "ts": at(10),
             "est_pnl_usd": 50.0,
             "ui": {"mode": "live", "paper": True, "sent": True}}]
    s = build(paper, rows=rows,
              rt=runtime(broker={"id": "alpaca", "account_tag": "alpaca:paper",
                                 "connected": True}))
    assert s["broker"]["paper_account"] is True
    assert s["results"]["real"]["days"] == []
    assert s["results"]["paper_account"]["days"] == [
        {"day": TODAY, "pnl": 50.0, "closed": 1}]


# ── robustness: damaged values degrade a field, never the page ──────────────

def test_damaged_log_and_ledger_values_never_break_the_status():
    c = "SPY 2026-11-13 $700.0 C"
    huge = 10 ** 400          # json.loads reads a 401-digit number as an int
    rows = [{"action": "exit", "contract": c, "dry": False, "ts": at(10),
             "est_pnl_usd": huge, "qty": huge},
            {"action": "entry", "contract": ["not", "text"], "ts": at(9)},
            {"action": "entry", "contract": {"a": 1}, "ts": at(9)},
            {"action": "exit", "contract": c, "dry": False, "ts": at(11),
             "est_pnl_usd": 1.7e308},
            {"action": "exit", "contract": c, "dry": False, "ts": at(12),
             "est_pnl_usd": 1.7e308},
            {"action": "exit", "contract": c, "dry": True, "ts": at(12),
             "est_pnl_usd": -huge, "ui": {"qty": huge, "mark": huge,
                                          "params": {"x": huge}}}]
    st = {"positions": {SPY: real_pos(entry_mark=1.7e308, qty=huge),
                        IWM_TODAY: real_pos(entry_mark=huge)}}
    s = build({**LIVE, "agenthc_api_key": "k", "sizing_mode": "budget",
               "budget_per_trade_usd": 1e-300},
              st, rows, rt=runtime(broker={**runtime()["broker"],
                                           "buying_power": 1e300}))
    real = s["results"]["real"]
    assert real["unmeasured"] == 3 and real["total"] == 0.0
    assert s["limits"]["realised_pnl_today"]["real"] == 0.0
    assert all(p["entry_cost_usd"] is None for p in s["positions"])
    assert s["limits"]["cash"]["trades_affordable"] is None
