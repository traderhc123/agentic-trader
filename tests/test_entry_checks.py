"""What the agent records on its trade-log rows for the dashboard (the "ui"
key: mode at the time, which entry check stopped a signal, the numbers behind
the reason), and that recording it changes nothing the agent does:
the checks run in the listed order, the limits and the P&L count the same
with or without it, the AI policy check is shown the rows as they always
were, and logs written by older versions still load."""

import inspect
import json
from datetime import datetime, timedelta, timezone

import pytest

import agent
import rules
from conftest import StubBroker, make_event

NOSAVE = staticmethod(lambda s: None)


def rows(home):
    p = home / "trades.jsonl"
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x]


def last(home):
    return rows(home)[-1]


def run(ev, cfg, state=None, broker=None, client=None):
    state = {"positions": {}} if state is None else state
    out = agent.handle_event(ev, cfg, state, broker, client, lambda s: None)
    return out, state


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    monkeypatch.setattr(agent, "notify", lambda *a, **k: None)
    monkeypatch.setattr(agent, "_problems_logged", set())
    monkeypatch.setattr(agent, "_WATCH_SINCE", {})


# ── one ordered list, next to the code that runs it ─────────────────────────

def test_handle_event_runs_the_checks_in_the_listed_order():
    """ENTRY_CHECKS is what the dashboard's "stopped here" drawer shows. Each
    check's id is written at the place handle_event runs it, so the order of
    the ids in its source IS the order they run in."""
    src = inspect.getsource(agent.handle_event)
    ids = [c for c, _kinds in agent.ENTRY_CHECKS]
    where = [src.index(f'"{c}"') for c in ids]  # ValueError = never stamped
    assert where == sorted(where), list(zip(ids, where))
    assert len(set(ids)) == len(ids)
    # ...and every row it logs for an entry that didn't happen says which
    kinds = {k for _c, logged in agent.ENTRY_CHECKS for k in logged}
    for kind in ("skip_paused", "skip_daily_cap", "skip_position_cap",
                 "skip_daily_loss", "skip_rule", "skip_ai_needed",
                 "policy_veto", "skip_stale", "skip_sizing"):
        assert kind in kinds and f'"{kind}"' in src


def test_checks_come_right_before_handle_event_in_the_file():
    src = inspect.getsource(agent)
    assert src.index("ENTRY_CHECKS = (") < src.index("def handle_event(")
    between = src[src.index("ENTRY_CHECKS = ("):src.index("def handle_event(")]
    assert between.count("\ndef ") <= 6  # the list and its row helpers only


def test_check_of_kind():
    assert agent._check_of_kind("skip_daily_cap") == "daily_cap"
    assert agent._check_of_kind("policy_veto") == "written_rules"
    assert agent._check_of_kind("skip_rule") is None   # two checks log it
    assert agent._check_of_kind("exit") is None
    assert agent._check_of_kind(None) is None


# ── each stop, as the row says it ───────────────────────────────────────────

def stopped(home, kind):
    row = last(home)
    assert row["action"] == kind, row
    return row["ui"]


def test_stale_signal(home, no_policy):
    old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    run(make_event(occurred_at=old), {"dry_run": False})
    assert stopped(home, "skip_stale") == {
        "mode": "live", "check": "signal_fresh", "params": {"max_age_s": 300}}


def test_entry_whose_exit_already_arrived(home, no_policy):
    agent._skip_exited_entry(make_event(), {"dry_run": True},
                             {"positions": {}}, lambda s: None)
    assert stopped(home, "skip_stale") == {
        "mode": "practice", "check": "signal_fresh",
        "code": "exit_already_arrived"}


def test_paused(home, no_policy):
    run(make_event(), {"paused": True, "dry_run": True})
    assert stopped(home, "skip_paused") == {"mode": "practice",
                                            "check": "not_paused"}


def test_past_the_expiry_guard(home, no_policy, monkeypatch):
    monkeypatch.setattr(agent, "_past_expiry_guard", lambda cfg, expiry: 945)
    run(make_event(), {"dry_run": False})
    assert stopped(home, "skip_rule") == {
        "mode": "live", "check": "before_expiry_guard",
        "code": "past_expiry_guard", "params": {"guard_et": "15:45"}}


def test_daily_cap(home, no_policy):
    for i in range(2):
        agent._log_trade({"action": "entry", "contract": f"X{i}", "dry": False})
    out, _ = run(make_event(), {"max_entries_per_day": 2, "dry_run": False},
                 broker=StubBroker(), client=object())
    assert "daily entry cap" in out
    assert stopped(home, "skip_daily_cap") == {
        "mode": "live", "check": "daily_cap", "params": {"cap": 2}}


def test_position_cap(home, no_policy):
    state = {"positions": {"A|2026-07-10|1.0|C": {"qty": 1}}}
    run(make_event(), {"max_open_positions": 1, "dry_run": False}, state)
    assert stopped(home, "skip_position_cap") == {
        "mode": "live", "check": "position_cap", "params": {"max_open": 1}}


def test_daily_loss(home, no_policy):
    agent._log_trade({"action": "exit", "contract": "X", "dry": False,
                      "est_pnl_usd": -316.0})
    run(make_event(), {"max_daily_loss_usd": 300, "dry_run": False})
    assert stopped(home, "skip_daily_loss") == {
        "mode": "live", "check": "daily_loss",
        "params": {"max_loss_usd": 300.0, "loss_usd": 316.0}}


def test_your_rules_say_which_rule(home, no_policy):
    run(make_event(ticker="TSLA"), {"rule_skip_tickers": ["TSLA"],
                                    "dry_run": True})
    assert stopped(home, "skip_rule") == {
        "mode": "practice", "check": "your_rules", "code": "rule_skip_list",
        "params": {"ticker": "TSLA"}}
    assert last(home)["reason"] == "TSLA is on your skip list"
    run(make_event(strike=9.0, event_id="t2"), {"rule_min_strike": 10})
    ui = stopped(home, "skip_rule")
    assert ui["code"] == "rule_min_strike"
    assert ui["params"] == {"strike": 9.0, "min_strike": 10.0}


def test_written_rules_veto_and_ai_needed(home, monkeypatch):
    import llm_policy
    monkeypatch.setattr(llm_policy, "evaluate", lambda c, e, log: {
        "act": False, "reason": "earnings week"})
    run(make_event(), {"dry_run": False})
    assert stopped(home, "policy_veto") == {"mode": "live",
                                            "check": "written_rules"}
    monkeypatch.setattr(llm_policy, "evaluate", lambda c, e, log: {
        "act": False, "ai_problem": True, "reason": "no AI model connected"})
    run(make_event(event_id="t2"), {"dry_run": True})
    assert stopped(home, "skip_ai_needed") == {"mode": "practice",
                                               "check": "written_rules"}


def test_trade_size_unknown(home, no_policy):
    run(make_event(), {"sizing_mode": "percent", "percent_per_trade": 2,
                       "dry_run": False}, broker=StubBroker(), client=object())
    assert stopped(home, "skip_sizing") == {"mode": "live",
                                            "check": "trade_size"}


def test_practice_entry_over_the_trade_size(home, no_policy):
    class Sizer:
        resolve_instrument = staticmethod(lambda client, ev: "opt-1")
        size_contracts = staticmethod(lambda client, cfg, oid: (
            0, "1 contract ≈ $1,240 exceeds your $500 budget"))
    run(make_event(), {"dry_run": True}, broker=Sizer(), client=object())
    assert stopped(home, "skip_budget") == {"mode": "practice",
                                            "check": "trade_size"}


def test_broker_problems_on_an_entry_stop_at_the_broker_check(home, no_policy):
    import broker_events

    class Rejects:
        @staticmethod
        def execute(client, cfg, ev, state):
            broker_events.report("order_failed", "insufficient buying power")
            broker_events.report("entry_unfilled", "cancelled", placed=True)
            return False
    run(make_event(), {"dry_run": False}, broker=Rejects(), client=None)
    failed, unfilled = rows(home)[-2:]
    # a problem with an order records the account kind (a Robinhood
    # account is never a paper-trading one)
    assert failed["ui"] == {"mode": "live", "check": "broker_order",
                            "paper": False}
    # placed=True: it may have (partly) filled — never "nothing was bought"
    assert unfilled["ui"] == {"mode": "live", "check": "broker_order",
                              "sent": True, "code": "buy_partly_filled",
                              "paper": False}


def test_broker_that_gives_no_reason(home, no_policy):
    run(make_event(), {"dry_run": False}, broker=StubBroker(changed=False),
        client=object())
    assert stopped(home, "broker_no_order") == {"mode": "live",
                                                "check": "broker_order"}


def test_a_failed_exit_is_not_an_entry_check(home, no_policy):
    import broker_events

    class DeadSell:
        @staticmethod
        def execute(client, cfg, ev, state):
            broker_events.report("order_failed", "rejected")
            return False
    key = "SPY|2026-07-10|752.0|C"
    state = {"positions": {key: {"option_id": "x", "qty": 1, "live": True}}}
    with pytest.raises(agent.ExitNotDone):
        run(make_event(event="EXITED"), {"dry_run": False,
                                         "position_sync": False},
            state, broker=DeadSell(), client=None)
    row = last(home)
    assert row["action"] == "order_failed" and row["ui"] == {"mode": "live",
                                                             "paper": False}


# ── entries and exits ───────────────────────────────────────────────────────

def test_practice_entry_and_exit_rows(home, no_policy):
    _, state = run(make_event(track="other"), {"dry_run": True,
                                               "contracts_per_trade": 3})
    e = last(home)
    assert e["action"] == "entry" and e["dry"] is True
    assert e["ui"] == {"mode": "practice", "book": "other", "sent": False,
                       "filled": False, "qty": 3}
    run(make_event(event="EXITED", event_id="x1", track="other"),
        {"dry_run": True}, state)
    x = last(home)
    assert x["action"] == "exit" and x["ui"] == {
        "mode": "practice", "book": "other", "sent": False, "filled": False,
        "qty": 3}


def test_live_entry_row_with_its_quote(home, no_policy):
    class Quoting(StubBroker):
        @staticmethod
        def mark_price(client, cfg, ev, side):
            return 4.123456 if side == "buy" else 4.5
    b = Quoting()
    _, state = run(make_event(), {"dry_run": False}, broker=b, client=object())
    e = last(home)
    # the row is what it always was, plus the one new key
    assert {k: v for k, v in e.items() if k not in ("ui", "ts")} == {
        "action": "entry", "event_id": "test-1",
        "contract": "SPY 2026-07-10 $752.0 C", "dry": False,
        "reason": "test: mechanical"}
    assert e["ui"] == {"mode": "live", "book": "main", "sent": True,
                       "filled": True, "qty": 1, "mark": 4.1235,
                       "paper": False}
    run(make_event(event="EXITED", event_id="x1"),
        {"dry_run": True, "position_sync": False}, state, broker=b,
        client=object())
    x = last(home)
    # a real position sold while the agent is in practice mode
    assert x["dry"] is False and x["qty"] == 1 and x["est_pnl_usd"] == 37.65
    assert x["ui"] == {"mode": "practice", "book": "main", "sent": True,
                       "filled": True, "qty": 1, "mark": 4.5, "paper": False}


def test_a_buy_still_working_at_the_broker_is_not_filled(home, no_policy):
    class Working(StubBroker):
        def execute(self, client, cfg, ev, state):
            super().execute(client, cfg, ev, state)
            state["positions"]["SPY|2026-07-10|752.0|C"]["pending"] = {
                "side": "buy", "requested": 1, "filled": 0}
            return True
    run(make_event(), {"dry_run": False}, broker=Working(), client=object())
    assert last(home)["ui"]["filled"] is False


def test_mode_stamp_is_the_engines_own_reading_of_the_settings(home, no_policy):
    """Settings that never chose a mode trade for real today; the stamp and
    the view-model say what the engine does, not what a default would."""
    run(make_event(), {}, broker=StubBroker(), client=object())
    e = last(home)
    assert e["dry"] is False and e["ui"]["mode"] == "live"
    assert agent._mode({}) == "live" and agent._mode(None) == "live"
    assert agent._mode({"dry_run": True}) == "practice"


# ── recording can't break or change anything ────────────────────────────────

def test_row_facts_never_raise_and_drop_what_is_not_data():
    class Angry:
        def get(self, *a):
            raise RuntimeError("no")
    assert agent._row_facts({"action": "x"}, Angry()) == {"action": "x"}
    rec = agent._row_facts({"action": "x"}, None, "daily_cap", params={
        "cap": 2, "flag": True, "deep": {"a": 1}, "none": None, 5: "five"},
        code=None, sent=None)
    assert rec["ui"] == {"mode": "live", "check": "daily_cap",
                         "params": {"cap": 2, "5": "five"}}
    assert agent._row_facts({}, {}, params="not a dict") == {}
    assert agent._trade_facts({"action": "entry"}, {}, "main", False, None,
                              None, "x", None)["ui"]["sent"] is True
    assert agent._trade_facts({"action": "exit"}, Angry(), None, True,
                              "row", "prior", object(), "sold") == {"action": "exit"}
    assert agent._rule_facts(None, None, None) == {}


def test_the_ai_policy_check_is_shown_the_rows_as_they_always_were(
        home, monkeypatch):
    """The trade log goes into the AI policy prompt: a new key there could
    change a verdict. The rows it gets carry none."""
    import llm_policy
    shown = []

    def evaluate(cfg, ev, log):
        shown.append(log)
        return {"act": True, "reason": "ok"}
    monkeypatch.setattr(llm_policy, "evaluate", evaluate)
    run(make_event(), {"paused": True})                       # a stamped row
    run(make_event(event_id="t2"), {"dry_run": True})          # another
    run(make_event(event_id="t3", strike=700.0), {"dry_run": True})
    on_disk = rows(home)
    assert all("ui" in r for r in on_disk)
    assert shown[-1] == [{k: v for k, v in r.items() if k != "ui"}
                         for r in on_disk[:2]]
    assert not any("ui" in r for log in shown for r in log)
    assert set(shown[-1][1]) == {"action", "event_id", "contract", "dry",
                                 "reason", "ts"}


def test_the_qa_box_gets_rows_without_the_new_key():
    src = inspect.getsource(agent.cmd_run)
    assert "_rows_without_facts(list(reversed(_recent_trades(25))))" in src
    assert agent._rows_without_facts([{"a": 1, "ui": {}}, "x"]) == [{"a": 1}, "x"]


OLD_LOG = [
    {"action": "entry", "event_id": "e1", "contract": "SPY 2026-07-10 $752.0 C",
     "dry": False, "reason": "ok"},
    {"action": "entry", "event_id": "e2", "contract": "QQQ 2026-07-10 $640.0 C",
     "dry": True, "reason": "ok"},
    {"action": "skip_daily_cap", "book": "main", "event_id": "e3",
     "contract": "IWM 2026-07-10 $245.0 C"},
    {"action": "skip_budget", "book": "shadow", "event_id": "e4",
     "contract": "COST 2026-07-10 $1010.0 C",
     "reason": "1 contract ≈ $1,240 exceeds your $500 budget", "dry": True},
    {"action": "exit", "event_id": "x1", "contract": "SPY 2026-07-10 $752.0 C",
     "dry": False, "reason": "exit", "qty": 1, "est_pnl_usd": -64.0},
    {"action": "exit", "event_id": "x2", "contract": "QQQ 2026-07-10 $640.0 C",
     "dry": True, "reason": "exit", "est_pnl_usd": 12.5},
    {"action": "order_failed", "contract": "AAPL 2026-07-10 $282.5 P",
     "reason": "insufficient buying power", "hint": "add cash"},
    {"action": "exit", "manual": True, "cause": "sold", "dry": False,
     "contract": "META 2026-07-10 $745.0 C", "qty": 1, "exit_price": 5.15,
     "est_pnl_usd": 55.0, "reason": "sold in your broker app"},
    {"action": "ledger_adopt", "contract": "AAPL 2026-07-10 $285.0 C",
     "dry": False, "qty": 1, "held": 2, "reason": "re-added"},
]
NEW_FACTS = [
    {"mode": "live", "book": "main", "sent": True, "filled": True, "qty": 1,
     "mark": 4.12},
    {"mode": "practice", "book": "main", "sent": False, "filled": False, "qty": 1},
    {"mode": "live", "check": "daily_cap", "params": {"cap": 1}},
    {"mode": "practice", "check": "trade_size"},
    {"mode": "live", "book": "main", "sent": True, "filled": True, "qty": 1},
    {"mode": "practice", "book": "main", "sent": False, "filled": False},
    {"mode": "live", "check": "broker_order"},
    {}, {},
]


def write_log(home, with_facts):
    now = datetime.now(timezone.utc)
    lines = []
    for i, (row, ui) in enumerate(zip(OLD_LOG, NEW_FACTS)):
        r = {**row, "ts": (now - timedelta(seconds=60 - i)).isoformat(
            timespec="seconds")}
        if with_facts and ui:
            r["ui"] = ui
        lines.append(json.dumps(r))
    lines.insert(4, '{"action": "entry", "contr')       # torn by a crash
    lines.insert(2, "[1, 2, 3]")                         # not a row
    (home / "trades.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def everything_the_agent_reads(home):
    cfg = {"dry_run": False, "max_entries_per_day": 5,
           "sizing_mode": "budget", "budget_per_trade_usd": 500}
    strip = agent._rows_without_facts
    pnl = agent.pnl_summary(cfg, {})
    return {
        "entries": (agent._entries_today(False), agent._entries_today(True)),
        "pnl_today": (agent._est_pnl_today(False), agent._est_pnl_today(True)),
        "streak": (rules.loss_streak([t for t in agent._trades_today()
                                      if not t.get("dry")])),
        "pnl": {k: pnl[k] for k in ("live", "dry")},
        "problems": agent._problems_today(),
        "hints": agent._budget_hints(cfg),
        "ai_rows": strip(agent._recent_trades(20)),
        "page": [{k: v for k, v in t.items() if k != "ui"}
                 for t in agent.trade_page()["trades"]],
        "rule": rules.check({"rule_loss_streak": 1}, make_event(),
                            strip([t for t in agent._trades_today()
                                   if not t.get("dry")])),
    }


def test_limits_and_pnl_count_the_same_with_or_without_the_new_key(home):
    write_log(home, with_facts=False)
    old = everything_the_agent_reads(home)
    write_log(home, with_facts=True)
    new = everything_the_agent_reads(home)

    def no_ts(v):  # the two logs are written a moment apart
        return json.loads(json.dumps(v, default=str).replace("T", " "))
    assert old["entries"] == new["entries"] == (1, 1)
    assert old["pnl_today"] == new["pnl_today"] == ((-9.0, 0), (12.5, 0))
    assert old["streak"] == new["streak"] and old["rule"] == new["rule"]
    assert old["hints"] == new["hints"] and len(old["hints"]) == 1
    for key in ("pnl", "problems", "ai_rows", "page"):
        assert _no_times(old[key]) == _no_times(new[key]), key
    assert no_ts(old["entries"]) == no_ts(new["entries"])


def _no_times(v):
    if isinstance(v, dict):
        return {k: _no_times(x) for k, x in v.items()
                if k not in ("ts", "at", "event_at", "event_at_et")}
    if isinstance(v, list):
        return [_no_times(x) for x in v]
    return v


def test_logs_written_by_older_versions_still_load(home):
    """Rows without the new key (and a torn line, and a line that isn't a
    row) are read as before, and the agent goes on writing after them."""
    write_log(home, with_facts=False)
    log = agent._read_trade_log()
    assert [t["n"] for t in log] == [0, 1, 3, 4, 6, 7, 8, 9, 10]
    assert not any("ui" in t for t in log)
    assert agent._entries_today(False) == 1 and agent._entries_today(True) == 1
    assert agent.pnl_summary({}, {})["live"]["total"] == -9.0
    assert len(agent.trade_page()["trades"]) == 9
    agent._log_trade(agent._row_facts({"action": "skip_paused"}, {}, "not_paused"))
    assert agent._read_trade_log()[-1]["ui"] == {"mode": "live",
                                                 "check": "not_paused"}
    assert agent._entries_today(False) == 1


# ── rules.check_detail ──────────────────────────────────────────────────────

@pytest.mark.parametrize("cfg,ev,trades,code,params", [
    ({}, {}, (), "", {}),
    ({"rule_tickers": ["SPY", "QQQ"]}, {"ticker": "NVDA"}, (),
     "rule_not_in_list", {"ticker": "NVDA"}),
    ({"rule_skip_tickers": "tsla"}, {"ticker": "TSLA"}, (),
     "rule_skip_list", {"ticker": "TSLA"}),
    ({"rule_sides": "calls"}, {"type": "P"}, (), "rule_side", {"sides": "calls"}),
    ({"rule_min_strike": 10}, {"strike": 9.5}, (), "rule_min_strike",
     {"strike": 9.5, "min_strike": 10.0}),
    ({"rule_skip_0dte": True}, {"expiry": "2026-07-10"}, (), "rule_0dte", {}),
    ({"rule_loss_streak": 2}, {},
     ({"action": "exit", "est_pnl_usd": -1}, {"action": "exit", "est_pnl_usd": -2}),
     "rule_loss_streak", {"losses": 2}),
    ({"rule_skip_tickers": ["SPY"]}, {"event": "EXITED"}, (), "", {}),
])
def test_check_detail_names_the_rule_and_check_is_unchanged(cfg, ev, trades,
                                                            code, params):
    now = datetime(2026, 7, 10, 15, 0, tzinfo=rules.MARKET_TZ)
    event = make_event(**ev)
    ok, why, got_code, got_params = rules.check_detail(cfg, event, trades, now)
    assert (got_code, got_params) == (code, params)
    assert ok is (code == "") and bool(why) is (code != "")
    assert rules.check(cfg, event, trades, now) == (ok, why)
    assert code == "" or code in rules.RULE_CODES
