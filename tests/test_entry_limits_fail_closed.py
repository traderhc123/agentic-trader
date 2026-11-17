"""Entry limits that failed open (audit PR6, 2026-11-17).

1. The AI policy check was shown only the last 20 trade-log rows, mixed
   practice and live, with no date: skip/veto rows pushed today's entries
   out of view, so "never more than 2 new entries per day" let a third
   real order through.
2. rule_loss_streak counted one losing position sold in pieces (a partial
   fill + the re-sold remainder, or a partial close in the broker app) as
   several losses, ending the day after a single loss.
3. A trade log that exists but can't be read counted as "no entries, no
   losses today", so the daily cap, max daily loss and loss streak let a
   new real order through. Now entries are skipped (visible row + alert);
   exits still run.
Brokers and the AI are stubs; nothing goes to the network."""
import builtins
import json
import os
from datetime import datetime

import pytest

import agent
import llm_client
import llm_policy
import rules
from conftest import StubBroker, make_event

NOSAVE = staticmethod(lambda s: None)
EXP = "2026-12-19"


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    sent = []
    monkeypatch.setattr(agent, "notify",
                        lambda cfg, msg, **kw: sent.append((msg, kw)))
    monkeypatch.setattr(agent, "_problems_logged", set())
    return sent


class PriceBroker:
    """Stub adapter with quotes; an exit sells `fill` contracts (all if None)."""

    def __init__(self, buy=2.0, sell=1.0, fill=None, qty=5):
        self.buy, self.sell, self.fill, self.qty = buy, sell, fill, qty
        self.calls = []

    def mark_price(self, client, cfg, event, side):
        return self.buy if side == "buy" else self.sell

    def execute(self, client, cfg, ev, state):
        self.calls.append(dict(ev))
        k = f"{ev['ticker']}|{ev['expiry']}|{ev['strike']}|{ev['type']}"
        if ev["event"] == "ENTERED":
            state["positions"][k] = {"option_id": "opt1", "qty": self.qty}
            return True
        row = state["positions"].get(k)
        if not row:
            return False
        n = min(self.fill or row["qty"], row["qty"])
        row["qty"] -= n
        if row["qty"] <= 0:
            state["positions"].pop(k)
        return True


def _policy(home, monkeypatch, text="- Never trade more than 2 new entries per day.\n",
            verdict=None):
    """A written policy and a stub AI that records the prompt it is sent."""
    (home / "policy.md").write_text(text, encoding="utf-8")
    monkeypatch.setattr(llm_client, "configured", lambda cfg: True)
    seen = []

    def complete_json(cfg, system, user, schema, **kw):
        seen.append(user)
        return verdict(user) if verdict else {"act": True, "reason": "stub"}
    monkeypatch.setattr(llm_client, "complete_json", complete_json)
    return seen


def _log_rows(prompt):
    return json.loads(prompt.split("newest last):\n", 1)[1])


def _entry(i, dry=False):
    agent._log_trade({"action": "entry", "event_id": f"e{i}", "dry": dry,
                      "contract": f"SPY {EXP} $70{i}.0 C", "reason": "ok"})


def _skips(n):
    for i in range(n):
        agent._log_trade({"action": "skip_rule", "book": "shadow",
                          "event_id": f"o{i}", "contract": "QQQ x P",
                          "reason": "you trade calls only"})


# ── 1. the AI policy check sees all of today, in the current mode ───────────

def test_policy_check_is_shown_todays_entries_past_many_skip_rows(home, monkeypatch):
    seen = _policy(home, monkeypatch)
    _entry(0)
    _entry(1)
    _skips(20)  # used to push both entries out of the 20-row window
    out = agent.handle_event(make_event(event_id="third", expiry=EXP),
                             {"dry_run": False, "max_entries_per_day": 10},
                             {"positions": {}}, PriceBroker(), object(), NOSAVE)
    assert out.startswith("ENTRY")  # the stub AI approves; we check what it saw
    prompt = seen[-1]
    shown = _log_rows(prompt)
    assert [r["event_id"] for r in shown] == ["e0", "e1"]
    assert "new entries today: 2" in prompt
    assert "NOW: " in prompt and "Mode: LIVE" in prompt


def test_a_per_day_limit_in_the_policy_now_stops_the_third_real_order(
        home, monkeypatch):
    """An AI that applies "max 2 a day" to what it is shown vetoes the third."""
    def verdict(user):
        n = sum(r.get("action") == "entry" for r in _log_rows(user))
        return ({"act": False, "reason": "more than 2 new entries per day"}
                if n >= 2 else {"act": True, "reason": "ok"})
    _policy(home, monkeypatch, verdict=verdict)
    _entry(0)
    _entry(1)
    _skips(25)
    b = PriceBroker()
    out = agent.handle_event(make_event(event_id="third", expiry=EXP),
                             {"dry_run": False, "max_entries_per_day": 10},
                             {"positions": {}}, b, object(), NOSAVE)
    assert out.startswith("POLICY VETO") and b.calls == []


def test_policy_check_counts_only_the_mode_the_agent_is_in(home, monkeypatch):
    seen = _policy(home, monkeypatch)
    for i in range(3):
        _entry(i, dry=True)          # practice entries
    agent.handle_event(make_event(event_id="live-1", expiry=EXP),
                       {"dry_run": False, "max_entries_per_day": 10},
                       {"positions": {}}, PriceBroker(), object(), NOSAVE)
    prompt = seen[-1]
    assert _log_rows(prompt) == []
    assert "new entries today: 0" in prompt
    today_et = agent._et_now().date().isoformat()
    assert today_et in prompt.split("CANDIDATE EVENT")[0]


def test_policy_view_uses_the_et_day_and_the_full_log(home, monkeypatch):
    et = agent.MARKET_TZ
    now = datetime(2026, 11, 17, 21, 30, tzinfo=et)       # 02:30 UTC next day
    monkeypatch.setattr(agent, "_et_now", lambda: now)
    rows = [
        # yesterday ET (and yesterday UTC)
        {"action": "entry", "dry": False, "contract": "A", "ts": "2026-11-16T15:00:00+00:00"},
        # today ET, morning
        {"action": "entry", "dry": False, "contract": "B", "ts": "2026-11-17T15:00:00+00:00"},
        {"action": "exit", "dry": False, "contract": "B", "qty": 2, "partial": True,
         "est_pnl_usd": -50.0, "ts": "2026-11-17T16:00:00+00:00"},
        {"action": "exit", "dry": False, "contract": "B", "qty": 1,
         "est_pnl_usd": -25.0, "ts": "2026-11-17T17:00:00+00:00"},
        # today ET, evening — already the next day in UTC
        {"action": "entry", "dry": False, "contract": "C", "ts": "2026-11-18T01:00:00+00:00"},
        # practice row, and a skip row: not shown
        {"action": "entry", "dry": True, "contract": "D", "ts": "2026-11-17T15:30:00+00:00"},
        {"action": "skip_rule", "contract": "E", "ts": "2026-11-17T15:40:00+00:00"},
    ]
    (home / "trades.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    shown, ctx = agent._policy_view(False)
    assert [r["contract"] for r in shown] == ["B", "B", "B", "C"]
    assert ctx["entries_today"] == 2
    assert ctx["positions_closed_today"] == 1          # B sold in two pieces
    assert ctx["losing_positions_today"] == 1
    assert ctx["losses_in_a_row"] == 1
    assert ctx["est_realized_pnl_today_usd"] == -75.0
    assert ctx["mode"] == "live" and ctx["now_et"].startswith("2026-11-17 21:30")


def test_evaluate_without_context_keeps_the_old_prompt(home, monkeypatch):
    seen = _policy(home, monkeypatch)
    log = [{"action": "entry", "event_id": f"x{i}"} for i in range(30)]
    llm_policy.evaluate({}, make_event(), log)
    assert "RECENT TRADE LOG (newest last):" in seen[-1]
    assert "NOW:" not in seen[-1]
    assert len(_log_rows(seen[-1])) == 20


# ── 2. a position sold in pieces is one loss ────────────────────────────────

def _x(contract, pnl, partial=False, dry=False, **kw):
    r = {"action": "exit", "contract": contract, "dry": dry, "est_pnl_usd": pnl}
    if partial:
        r["partial"] = True
    r.update(kw)
    return r


def test_loss_streak_merges_a_partial_exit_with_its_remainder():
    trades = [_x("RIVN C", -200.0, partial=True, qty=2, remaining=3),
              _x("RIVN C", -300.0, qty=3, cause="exit_remainder")]
    assert rules.loss_streak(trades) == 1
    ok, _why = rules.check({"rule_loss_streak": 2}, make_event(), trades)
    assert ok


def test_loss_streak_merges_a_partial_close_in_the_app_and_a_re_sold_remainder():
    trades = [_x("A", -100.0, partial=True, manual=True, cause="sold"),
              # the remainder's row: already counted by the first, no estimate
              {"action": "exit", "contract": "A", "dry": False, "qty": 2,
               "counted_before": 2}]
    assert rules.loss_streak(trades) == 1


def test_loss_streak_still_counts_separate_losing_positions():
    # two contracts
    assert rules.loss_streak([_x("A", -1.0), _x("B", -1.0)]) == 2
    # the same contract, bought and sold twice
    trades = [{"action": "entry", "contract": "A"}, _x("A", -1.0),
              {"action": "entry", "contract": "A"}, _x("A", -2.0)]
    assert rules.loss_streak(trades) == 2
    # a new entry ends a position whose remainder was never logged as sold
    trades = [_x("A", -1.0, partial=True), {"action": "entry", "contract": "A"},
              _x("A", -2.0)]
    assert rules.loss_streak(trades) == 2
    # practice and real rows of one contract are different positions
    assert rules.loss_streak([_x("A", -1.0, partial=True),
                              _x("A", -1.0, dry=True)]) == 2


def test_loss_streak_win_and_order():
    # a partly sold loser counts at once…
    assert rules.loss_streak([_x("A", -1.0, partial=True)]) == 1
    # …and where it was last sold: A's remainder after B's win
    trades = [_x("A", -1.0, partial=True), _x("B", 5.0), _x("A", -1.0)]
    assert rules.loss_streak(trades) == 1
    # a position that came back to a gain overall is a win
    trades = [_x("C", -1.0), _x("A", -1.0, partial=True), _x("A", 3.0)]
    assert rules.loss_streak(trades) == 0
    assert rules.exit_results([_x("A", None)]) == [None]


def test_one_losing_trade_sold_in_two_fills_does_not_stop_the_day(
        home, monkeypatch, no_policy):
    monkeypatch.setattr(agent, "_gate_on", lambda cfg, b, c: False)
    cfg = {"dry_run": False, "rule_loss_streak": 2, "max_entries_per_day": 10}
    state = {"positions": {}}
    b = PriceBroker(buy=2.0, sell=1.0, fill=2)
    assert agent.handle_event(make_event(event_id="A-in", expiry=EXP), cfg,
                              state, b, object(), NOSAVE).startswith("ENTRY")
    agent.handle_event(make_event(event="EXITED", event_id="A-out", expiry=EXP),
                       cfg, state, b, object(), NOSAVE)
    b.fill = None
    agent.handle_event(make_event(event="EXITED", event_id="exit-rest|A",
                                  expiry=EXP, exit_cause="exit_remainder"),
                       cfg, state, b, object(), NOSAVE)
    exits = [t for t in agent._trades_today() if t.get("action") == "exit"]
    assert [(t.get("qty"), t.get("partial")) for t in exits] == [(2, True), (3, None)]
    out = agent.handle_event(make_event(event_id="B-in", strike=700.0, expiry=EXP),
                             cfg, state, b, object(), NOSAVE)
    assert out.startswith("ENTRY"), out
    # a second losing trade does stop it
    agent.handle_event(make_event(event="EXITED", event_id="B-out", strike=700.0,
                                  expiry=EXP), cfg, state, b, object(), NOSAVE)
    out = agent.handle_event(make_event(event_id="C-in", strike=690.0, expiry=EXP),
                             cfg, state, b, object(), NOSAVE)
    assert "2 losing exit(s) in a row" in out


def test_loss_streak_rule_reads_the_full_log_not_a_tail(home, no_policy):
    cfg = {"dry_run": False, "rule_loss_streak": 1, "max_entries_per_day": 10}
    agent._log_trade(_x("A", -100.0))
    _skips(600)  # more than the old 500-row tail
    b = StubBroker()
    out = agent.handle_event(make_event(event_id="n1"), cfg, {"positions": {}},
                             b, object(), NOSAVE)
    assert "losing exit(s) in a row" in out and b.calls == []


# ── 3. an unreadable trade log fails closed for entries only ────────────────

@pytest.fixture
def unreadable(home, monkeypatch):
    """trades.jsonl exists but reading it fails (an ACL problem, another
    program holding it locked). Writing still works."""
    real_open = builtins.open
    on = [True]

    def fake_open(path, mode="r", *a, **kw):
        if (on[0] and os.fspath(path) == agent.TRADES_PATH
                and mode in ("r", "rt")):
            raise PermissionError(13, "Permission denied", path)
        return real_open(path, mode, *a, **kw)
    monkeypatch.setattr(builtins, "open", fake_open)
    return on


def test_unreadable_log_skips_the_entry_with_a_row_and_one_alert(
        home, unreadable, quiet, no_policy):
    for i in range(2):          # the cap, the loss limit and the streak are
        _entry(i)               # all hit — and none of them can be read
    agent._log_trade(_x("SPY x", -500.0, event_id="x0"))
    cfg = {"dry_run": False, "max_entries_per_day": 2, "max_daily_loss_usd": 100,
           "rule_loss_streak": 1}
    state = {"positions": {}}
    b = PriceBroker()
    out = agent.handle_event(make_event(event_id="new", expiry=EXP), cfg, state,
                             b, object(), NOSAVE)
    assert b.calls == []
    assert out.startswith("SKIPPED") and "trade log can't be read" in out
    out2 = agent.handle_event(make_event(event_id="new2", strike=700.0, expiry=EXP),
                              cfg, state, b, object(), NOSAVE)
    assert out2.startswith("SKIPPED") and b.calls == []
    unreadable[0] = False
    rows = [json.loads(x) for x in
            (home / "trades.jsonl").read_bytes().decode("utf-8").splitlines()]
    skips = [r for r in rows if r["action"] == "skip_log_unreadable"]
    assert [r["event_id"] for r in skips] == ["new", "new2"]
    assert skips[0]["ui"]["check"] == "daily_cap"
    alerts = [(m, kw) for m, kw in quiet if "trade log" in m]
    assert len(alerts) == 1 and "can't be read" in alerts[0][0]
    assert alerts[0][1].get("priority") == "high"
    assert agent._problems_today()[0]["kind"] == "skip_log_unreadable"


def test_unreadable_log_skips_in_practice_mode_too(unreadable, no_policy):
    b = PriceBroker()
    out = agent.handle_event(make_event(event_id="p1", expiry=EXP),
                             {"dry_run": True}, {"positions": {}}, b, object(),
                             NOSAVE)
    assert out.startswith("SKIPPED") and b.calls == []


def test_unreadable_log_never_blocks_an_exit(unreadable, no_policy):
    key = f"SPY|{EXP}|752.0|C"
    state = {"positions": {key: {"option_id": "x", "qty": 1}}}
    b = StubBroker()
    out = agent.handle_event(make_event(event="EXITED", event_id="x1", expiry=EXP),
                             {"dry_run": False, "max_entries_per_day": 0},
                             state, b, object(), NOSAVE)
    assert out == f"EXIT: SPY {EXP} $752.0 C" and len(b.calls) == 1


def test_no_log_yet_is_an_empty_day_not_a_failure(home, no_policy):
    assert not (home / "trades.jsonl").exists()
    b = StubBroker()
    out = agent.handle_event(make_event(event_id="first"),
                             {"dry_run": False, "max_daily_loss_usd": 100,
                              "rule_loss_streak": 1},
                             {"positions": {}}, b, object(), NOSAVE)
    assert out.startswith("ENTRY") and len(b.calls) == 1


def test_strict_reads_raise_and_lenient_reads_stay_lenient(unreadable):
    with pytest.raises(agent.TradeLogUnreadable):
        agent._entries_today(False, strict=True)
    with pytest.raises(agent.TradeLogUnreadable):
        agent._est_pnl_today(False, strict=True)
    with pytest.raises(agent.TradeLogUnreadable):
        agent._trades_today(strict=True)
    # the dashboard's readers keep showing what they can
    assert agent._entries_today(False) == 0
    assert agent._est_pnl_today(False) == (0.0, 0)
