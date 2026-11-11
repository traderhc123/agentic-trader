"""PR 4 (/v2 parity with the classic dashboard), the server side of groups
8-11 and the review findings (webui_app/PARITY.md):

  group 8   Rules & limits: the expiry-day sell time (two-step, on the limits
            route, through the chat's own check), and the budget hint's
            suggested size saved the way the classic hint button saves it
  group 9   Results: the result on paid signal days (view-model, read-only)
  group 10  Today: "Ask your agent", POST /api/v2/ask, answer-only: the same
            answer function the classic chat falls back to, never a command
  findings  "Today so far · Opened" counts the Eastern day; Activity knows
            how many lines each loaded day has in the whole log

Hermetic: no broker, wallet, AI, AgentHC or network."""

import json
from datetime import datetime, timezone

import pytest

import agent
import view_model as vm
import webui

from test_v2_settings_api import (  # noqa: F401
    BASE, SECRETS, app, read_cfg, same_effect, write_cfg)
from test_v2_parity import _running
import test_v2_parity_settings as T


# ── group 8: the expiry-day sell time ───────────────────────────────────────

@pytest.mark.parametrize("start, chat, v2", [
    ("15:30", "expiry guard off", "off"),
    ("15:30", "expiry guard 15:40", "15:40"),
    ("15:30", "expiry guard 9:45", "09:45"),
    ("off", "expiry guard on", "15:45"),  # "on" is the default time
    ("off", "expiry guard 15:59", "15:59"),
])
def test_the_expiry_sell_time_is_the_chat_command(home, app, start, chat, v2):
    c = _running(app)
    out = same_effect(home, {**BASE, "expiry_guard_et": start},
                      lambda: c.post("/api/command", {"text": chat}),
                      lambda: c.confirmed("/api/v2/settings/limits",
                                          {"expiry_sell_et": v2}))
    assert out["expiry_guard_et"] == v2
    got = c.get("/api/v2/settings")["expiry"]
    assert got["sell_et"] == (None if v2 == "off" else v2)


def test_the_expiry_sell_time_preview_says_what_changes(home, app):
    write_cfg(home, {**BASE, "expiry_guard_et": "15:30"})
    c = _running(app)
    for new, saved_now, now, direction in (
            ("off", "15:30", None, "raises_risk"),
            ("15:50", "15:30", "15:50", "raises_risk"),
            ("10:15", "15:30", "10:15", "lowers_risk")):
        p = c.post("/api/v2/settings/limits", {"expiry_sell_et": new})
        assert p["needs_confirm"] is True
        assert p["preview"]["changes"] == [{
            "key": "expiry_sell_et", "label": "Expiry-day sell time",
            "saved_now": saved_now, "new": now, "direction": direction}]
        assert "It does not switch between Practice and LIVE." in p["preview"]["does_not"]
    # off -> a time lowers the risk; the same time is nothing to change
    write_cfg(home, {**BASE, "expiry_guard_et": "off"})
    p = c.post("/api/v2/settings/limits", {"expiry_sell_et": "15:45"})
    assert p["preview"]["changes"][0]["direction"] == "lowers_risk"
    write_cfg(home, {**BASE, "expiry_guard_et": "15:45"})
    r = c.post("/api/v2/settings/limits", {"expiry_sell_et": "15:45"})
    assert r == {"ok": True, "saved": False,
                 "note": "Nothing to change: those are your settings already."}
    assert read_cfg(home) == {**BASE, "expiry_guard_et": "15:45"}


def test_the_expiry_sell_time_takes_only_the_chats_range(home, app):
    write_cfg(home, {**BASE, "expiry_guard_et": "15:30"})
    c = _running(app)
    for bad in ("16:00", "09:00", "9:29", "25:00", "on", "", "3pm", "OFF",
                5, None, True, ["15:40"]):
        r = c.post("/api/v2/settings/limits", {"expiry_sell_et": bad})
        assert r["ok"] is False and "09:30 to 15:59" in r["error"], bad
        assert "needs_confirm" not in r
    assert read_cfg(home) == {**BASE, "expiry_guard_et": "15:30"}


def _case_expiry_sell(c, home, monkeypatch):
    start = {**BASE, "expiry_guard_et": "15:45"}
    return {"path": "/api/v2/settings/limits", "payload": {"expiry_sell_et": "15:30"},
            "done": lambda: read_cfg(home).get("expiry_guard_et") == "15:30",
            "reset": lambda: None, "start": start}


def test_the_expiry_sell_time_refuses_a_wrong_confirmation(home, app, monkeypatch):
    monkeypatch.setitem(T.TWO_STEP, "expiry-sell", _case_expiry_sell)
    T._check_two_step("expiry-sell", home, app, monkeypatch)
    # a token for one time never confirms another (valid) time
    write_cfg(home, {**BASE, "expiry_guard_et": "15:45"})
    c = _running(app)
    token = c.post("/api/v2/settings/limits", {"expiry_sell_et": "15:30"})["preview"]["token"]
    r = c.post("/api/v2/settings/limits", {"expiry_sell_et": "15:20", "confirm": token})
    assert r["ok"] is False
    assert read_cfg(home)["expiry_guard_et"] == "15:45"


def test_a_limits_save_with_the_sell_time_never_touches_the_mode(home, app):
    write_cfg(home, {**BASE, "dry_run": False, "expiry_guard_et": "15:45"})
    c = _running(app)
    r = c.confirmed("/api/v2/settings/limits", {"expiry_sell_et": "15:00",
                                                "max_entries_per_day": 4})
    assert r["ok"] is True and r["saved"] is True
    assert read_cfg(home) == {**BASE, "dry_run": False, "expiry_guard_et": "15:00",
                              "max_entries_per_day": 4}


# ── group 8: the budget hint's "Use $S" ─────────────────────────────────────

@pytest.mark.parametrize("book", ["main", "other"])
def test_the_budget_hints_size_saves_as_the_classic_hint_command(home, app, book):
    """The classic hint button sends its `command` ("set [other ]budget S");
    /v2 pre-fills the trade-size form with S: the same config."""
    start = {**BASE, "budget_per_trade_usd": 500.0,
             "other_sizing_mode": "budget", "other_budget_per_trade_usd": 150.0}
    today = datetime.now(timezone.utc).date().isoformat()
    rows = [{"action": "skip_budget", "ts": f"{today}T15:05:00+00:00",
             "book": "main" if book == "main" else "shadow",
             "reason": f"1 contract ≈ ${cost:,} exceeds per-trade budget $500"}
            for cost in (1120, 1240)]
    hint = agent._budget_hints(start, trades=rows, today=today)[0]
    c = _running(app)
    out = same_effect(home, start,
                      lambda: c.post("/api/command", {"text": hint["command"]}),
                      lambda: c.confirmed("/api/v2/settings/limits", {"trade_size": {
                          book: {"mode": "budget", "value": hint["suggest"]}}}))
    key = "budget_per_trade_usd" if book == "main" else "other_budget_per_trade_usd"
    assert out[key] == hint["suggest"] == 1150


# ── group 10: Ask your agent ────────────────────────────────────────────────

@pytest.fixture
def answers(monkeypatch):
    """webui._ask_llm stubbed (no AI): what it was asked, and with what."""
    asked = []

    def ask(cfg, question, status_fields, trades, system=None):
        asked.append({"cfg": dict(cfg), "question": question,
                      "fields": status_fields, "trades": trades, "system": system})
        return f"answer to {question}"
    monkeypatch.setattr(webui, "_ask_llm", ask)
    import llm_client
    monkeypatch.setattr(llm_client, "configured", lambda cfg: True)
    return asked


TRADES = [{"action": "skip_budget", "ts": "2026-11-09T15:05:00+00:00",
           "contract": "MSFT 2026-11-13 $525.0 C", "reason": "too dear"}]
FIELDS = {"mode": "DRY-RUN", "fields": {"entries today": "1 / 3"}}


def _asking(c_app, ran):
    return c_app(lambda: FIELDS, lambda: TRADES,
                 lambda text: (ran.append(text), agent._apply_command(text))[1],
                 lambda: agent._load(agent.CONFIG_PATH, {}))


def test_ask_answers_with_what_the_classic_chat_gives_its_fallback(home, app, answers):
    """The classic chat, for a question it has no command for, calls
    _ask_llm(settings, text, status fields, trades). /v2's Ask calls the
    same function with the same settings, status fields and trades."""
    write_cfg(home, BASE)
    ran = []
    c = _asking(app, ran)
    q = "why did the agent skip MSFT?"
    classic = c.post("/api/command", {"text": q})
    assert classic == {"reply": f"answer to {q}"}
    v2 = c.post("/api/v2/ask", {"question": "  " + q + " "})
    assert v2 == {"ok": True, "answer": f"answer to {q}"}
    a, b = answers
    for k in ("cfg", "question", "fields", "trades"):
        assert a[k] == b[k], k
    assert b["fields"] == FIELDS["fields"] and b["trades"] == TRADES
    # /v2's instructions point at /v2's pages, not at chat commands
    assert a["system"] is None and "Rules & limits" in b["system"]
    assert ran == [q]  # only the classic chat tried its commands first


def test_ask_never_runs_a_command(home, app, monkeypatch, answers):
    """What would be a chat command is a question here: nothing runs, no
    setting, control or proposal changes."""
    import self_edit
    cfg = {**BASE, "self_edit_enabled": True}
    write_cfg(home, cfg)
    monkeypatch.setattr(self_edit, "propose", lambda *a: pytest.fail("proposed"))
    monkeypatch.setitem(webui.CONTROLS, "stop", False)
    controls = dict(webui.CONTROLS)
    ran = []
    c = _asking(app, ran)
    for text in ("pause", "dry off", "stop", "set budget 1", "code: x", "go live",
                 "resume", "expiry guard off", "self-edit off"):
        r = c.post("/api/v2/ask", {"question": text})
        assert r == {"ok": True, "answer": f"answer to {text}"}, text
    assert ran == []
    assert read_cfg(home) == cfg and dict(webui.CONTROLS) == controls
    assert [a["question"] for a in answers][-1] == "self-edit off"
    assert self_edit.current() is None


def test_ask_refuses_what_it_cannot_answer(home, app, monkeypatch, answers):
    write_cfg(home, BASE)
    ran = []
    c = _asking(app, ran)
    for bad in ({}, {"question": ""}, {"question": "   "}, {"question": 5},
                {"question": "x" * 2001}, {"question": "a\x00b"},
                {"question": "x", "confirm": "t"}, {"question": "x", "text": "pause"}):
        r = c.post("/api/v2/ask", bad)
        assert r["ok"] is False and r["error"], bad
    assert answers == [] and ran == []
    # no AI helper: said in /v2's words, and nothing is asked
    import llm_client
    monkeypatch.setattr(llm_client, "configured", lambda cfg: False)
    r = c.post("/api/v2/ask", {"question": "how am I doing?"})
    assert r["ok"] is False and "Settings" in r["error"] and "Setup" not in r["error"]
    assert answers == []
    # no agent behind this server: nothing to ask about yet
    monkeypatch.setattr(llm_client, "configured", lambda cfg: True)
    r = app().post("/api/v2/ask", {"question": "how am I doing?"})
    assert r["ok"] is False and "running" in r["error"]
    assert answers == []


def test_ask_needs_the_login_and_the_right_host_and_origin(home, app, answers):
    write_cfg(home, BASE)
    ran = []
    c = _asking(app, ran)
    own = f"http://127.0.0.1:{c.port}"
    body = json.dumps({"question": "hello"})
    assert c.raw("POST", "/api/v2/ask", body, login=False, origin=own)[0] == 401
    assert c.raw("POST", "/api/v2/ask", body, host="evil.example", origin=own)[0] == 403
    assert c.raw("POST", "/api/v2/ask", body, origin="http://evil.example")[0] == 403
    assert answers == [] and ran == []


# ── finding: "Today so far · Opened" is the Eastern day ─────────────────────

def _opened_rows(day="2026-07-15", prev="2026-07-14"):
    """Entries on an Eastern trading day (EDT, UTC-4): two real ones and a
    practice one during the day, and a real one the evening before (after
    20:00 ET, so already on `day` by UTC)."""
    return [
        {"n": 0, "action": "entry", "ts": f"{day}T00:30:00+00:00", "dry": False,
         "contract": "SPY 2026-07-17 $700.0 C"},   # 20:30 ET on `prev`
        {"n": 1, "action": "entry", "ts": f"{day}T14:00:00+00:00", "dry": False,
         "contract": "QQQ 2026-07-17 $640.0 C"},
        {"n": 2, "action": "entry", "ts": f"{day}T15:00:00+00:00", "dry": False,
         "contract": "IWM 2026-07-17 $246.0 C"},
        {"n": 3, "action": "entry", "ts": f"{day}T16:00:00+00:00", "dry": True,
         "contract": "NVDA 2026-07-17 $232.5 P"},
    ]


def _at(rows, utc, cfg=None):
    s = vm.build_status(cfg or {"dry_run": False}, {"positions": {}}, rows,
                        {"running": True}, now=datetime.fromisoformat(utc))
    assert vm.validate(s) == []
    return s


@pytest.mark.parametrize("utc, used_utc", [
    ("2026-07-15T23:30:00+00:00", 3),   # 19:30 ET: still the 15th by UTC
    ("2026-07-16T00:30:00+00:00", 0),   # 20:30 ET: the 16th by UTC
])
def test_opened_today_counts_the_eastern_day_at_1930_and_2030_et(utc, used_utc):
    """"Today so far" describes the Eastern day: its Opened count is the
    same at 19:30 and 20:30 ET, while the agent's UTC-day count (the daily
    limit's, which says when it starts again) has dropped to 0 by 20:30."""
    s = _at(_opened_rows(), utc)
    assert s["results"]["real"]["opened_today"] == 2
    assert s["results"]["practice"]["opened_today"] == 1
    assert s["results"]["paper_account"]["opened_today"] == 0
    assert s["limits"]["used_today"]["real"] == used_utc


def test_opened_today_keeps_paper_account_entries_apart():
    paper = [{**r, "ui": {"paper": True}} for r in _opened_rows()]
    s = _at(paper, "2026-07-16T00:30:00+00:00")
    assert s["results"]["paper_account"]["opened_today"] == 2
    assert s["results"]["real"]["opened_today"] == 0
    assert s["results"]["practice"]["opened_today"] == 1


# ── finding: Activity knows each loaded day's size ──────────────────────────

def _day_rows():
    rows = []
    for i, ts in enumerate(["2026-11-05T15:00:00+00:00"] * 3
                           + ["2026-11-06T15:00:00+00:00"] * 4
                           + ["2026-11-09T15:00:00+00:00"] * 2):
        rows.append({"n": i, "action": "exit", "ts": ts, "dry": False,
                     "contract": "SPY 2026-11-13 $700.0 C", "est_pnl_usd": 10.0})
    rows.append({"n": 9, "action": "skip_rule", "ts": "not a time", "contract": None})
    return rows


def test_an_activity_page_says_how_many_lines_each_of_its_days_has():
    rows = _day_rows()
    page = vm.activity_page(rows, limit=4)
    assert vm.validate(page, "ActivityPage") == []
    # newest 4: the undated line, both of the 9th, one of the 6th (of 4)
    assert [r["n"] for r in page["rows"]] == [9, 8, 7, 6]
    assert page["day_lines"] == [{"day": "", "lines": 1},
                                 {"day": "2026-11-09", "lines": 2},
                                 {"day": "2026-11-06", "lines": 4}]
    older = vm.activity_page(rows, before=page["next"], limit=4)
    assert [r["n"] for r in older["rows"]] == [5, 4, 3, 2]
    assert older["day_lines"] == [{"day": "2026-11-06", "lines": 4},
                                  {"day": "2026-11-05", "lines": 3}]
    # the status carries the same for its own page
    s = vm.build_status({"dry_run": False}, {"positions": {}}, rows,
                        {"running": True},
                        now=datetime(2026, 11, 9, 20, 0, tzinfo=timezone.utc),
                        activity_limit=4)
    assert s["activity"]["day_lines"] == page["day_lines"]
