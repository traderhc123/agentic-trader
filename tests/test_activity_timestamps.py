"""Activity rows show the date and time each event happened, in ET.

The dashboard's Activity list (/api/trades -> agent.trade_page) carries, on
every row, `event_at` (UTC ISO) and `event_at_et` ("Nov 6, 3:55:17 PM ET").
Both are "" for a row with no readable time, which the UI shows as "—".
"""

import json
import re
from datetime import datetime, timezone

import pytest

import agent
import webui

NOW_2026 = datetime(2026, 11, 9, 15, 0, tzinfo=timezone.utc)


def _write(rows):
    with open(agent.TRADES_PATH, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


# ── formatting ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("ts,want", [
    # EST (after the 2026-11-01 fall-back): UTC-5
    ("2026-11-06T20:55:17+00:00", "Nov 6, 3:55:17 PM ET"),
    # EDT (before it): UTC-4, so the same UTC clock reads an hour later
    ("2026-10-30T19:55:17+00:00", "Oct 30, 3:55:17 PM ET"),
    ("2026-10-30T20:55:17+00:00", "Oct 30, 4:55:17 PM ET"),
    # morning / noon / midnight edges of the 12-hour clock
    ("2026-11-06T14:30:05+00:00", "Nov 6, 9:30:05 AM ET"),
    ("2026-11-06T17:00:00+00:00", "Nov 6, 12:00:00 PM ET"),
    ("2026-11-06T05:00:00+00:00", "Nov 6, 12:00:00 AM ET"),
    # an ET day that differs from the UTC day
    ("2026-11-07T02:15:00+00:00", "Nov 6, 9:15:00 PM ET"),
    # broker-style stamps: Z suffix, 9-digit fractions, another offset
    ("2026-11-06T20:55:17.123456789Z", "Nov 6, 3:55:17 PM ET"),
    ("2026-11-06T14:55:17-06:00", "Nov 6, 3:55:17 PM ET"),
    # no offset = UTC (the log writes UTC)
    ("2026-11-06T20:55:17", "Nov 6, 3:55:17 PM ET"),
])
def test_et_stamp(ts, want):
    assert agent._et_stamp(ts, now=NOW_2026) == want


def test_et_stamp_across_the_2026_11_01_dst_change():
    def s(ts):
        return agent._et_stamp(ts, now=NOW_2026)
    assert s("2026-11-01T04:59:59Z") == "Nov 1, 12:59:59 AM ET"
    # 1:00-1:59 AM happens twice that night: name the offset so two
    # instants an hour apart don't print the same
    assert s("2026-11-01T05:30:00Z") == "Nov 1, 1:30:00 AM EDT"
    assert s("2026-11-01T06:30:00Z") == "Nov 1, 1:30:00 AM EST"
    assert s("2026-11-01T07:00:00Z") == "Nov 1, 2:00:00 AM ET"
    # the same 3:55 PM wall time on both sides of the change
    assert s("2026-10-30T19:55:00Z") == "Oct 30, 3:55:00 PM ET"
    assert s("2026-11-02T20:55:00Z") == "Nov 2, 3:55:00 PM ET"


def test_et_stamp_spring_forward():
    def s(ts):
        return agent._et_stamp(ts, now=NOW_2026)
    assert s("2026-03-08T06:59:59Z") == "Mar 8, 1:59:59 AM ET"
    assert s("2026-03-08T07:00:00Z") == "Mar 8, 3:00:00 AM ET"


def test_et_stamp_adds_the_year_outside_the_current_one():
    assert agent._et_stamp("2025-12-30T20:55:17Z", now=NOW_2026) == "Dec 30, 2025, 3:55:17 PM ET"
    # the current year is the ET year: 00:30 UTC Jan 1 is still Dec 31 in New York
    jan1 = datetime(2027, 1, 1, 0, 30, tzinfo=timezone.utc)
    assert agent._et_stamp("2026-12-31T20:00:00Z", now=jan1) == "Dec 31, 3:00:00 PM ET"


@pytest.mark.parametrize("ts", [None, "", "   ", "not a time", 1730900000, {"x": 1}])
def test_et_stamp_unreadable_is_blank(ts):
    assert agent._et_stamp(ts, now=NOW_2026) == ""


def test_et_stamp_ignores_server_locale():
    import locale
    try:
        locale.setlocale(locale.LC_TIME, "de_DE.UTF-8")
    except locale.Error:
        pytest.skip("de_DE locale not installed")
    try:
        assert agent._et_stamp("2026-10-30T19:55:17Z", now=NOW_2026) == "Oct 30, 3:55:17 PM ET"
    finally:
        locale.setlocale(locale.LC_TIME, "C")


# ── /api/trades rows ────────────────────────────────────────────────────────

STAMP = re.compile(r"^[A-Z][a-z]{2} \d{1,2}(, \d{4})?, \d{1,2}:\d{2}:\d{2} [AP]M E[DS]?T$")
NOV6_355 = re.compile(r"Nov 6(, 2026)?, 3:55:17 PM ET")


def test_every_activity_kind_pages_with_its_time(home):
    kinds = ["entry", "exit", "skip_stale", "skip_daily_cap", "skip_position_cap",
             "skip_daily_loss", "skip_rule", "policy_veto", "skip_budget",
             "skip_no_quote", "skip_no_contract", "skip_ai_needed", "skip_sizing",
             "order_failed", "entry_unfilled", "exit_failed", "exit_partial",
             "broker_no_order", "broker_disconnected", "some_future_kind"]
    _write([{"action": k, "contract": "SPY 2026-11-06 700 CALL",
             "ts": "2026-11-06T20:55:17+00:00"} for k in kinds])
    rows = agent.trade_page(limit=100)["trades"]
    assert len(rows) == len(kinds)
    for r in rows:
        assert r["event_at"] == "2026-11-06T20:55:17+00:00", r["action"]
        assert NOV6_355.fullmatch(r["event_at_et"]), r
        assert STAMP.match(r["event_at_et"])


def test_rows_logged_by_the_agent_carry_a_time(home):
    # _log_trade stamps every row it writes, whatever the kind
    agent._log_trade({"action": "broker_disconnected", "reason": "login expired"})
    agent._log_trade({"action": "entry", "contract": "SPY 2026-11-06 700 CALL", "dry": True})
    rows = agent.trade_page()["trades"]
    assert [r["action"] for r in rows] == ["entry", "broker_disconnected"]
    for r in rows:
        assert r["ts"] and r["event_at"]
        assert STAMP.match(r["event_at_et"]), r
        assert r["event_at_et"] == agent._et_stamp(r["ts"])


def test_rows_without_a_time_show_blank(home):
    _write([{"action": "entry", "contract": "SPY 2026-11-06 700 CALL"},  # no ts
            {"action": "skip_stale", "ts": "garbage"},
            {"action": "skip_rule", "ts": None},
            {"action": "exit", "ts": "2026-11-06T20:55:17+00:00"}])
    rows = agent.trade_page()["trades"]
    assert [(r["action"], r["event_at"]) for r in rows] == [
        ("exit", "2026-11-06T20:55:17+00:00"),
        ("skip_rule", ""), ("skip_stale", ""), ("entry", "")]
    assert NOV6_355.fullmatch(rows[0]["event_at_et"])
    assert all(r["event_at_et"] == "" for r in rows[1:])


def test_a_close_found_in_the_account_shows_when_the_sale_filled(home):
    # the owner sold in the broker app at 3:55 PM EST; the agent noticed later
    _write([{"action": "exit", "manual": True, "contract": "SPY 2026-11-06 700 CALL",
             "closed_at": "2026-11-06T20:55:17+00:00",
             "ts": "2026-11-06T21:10:00+00:00"},
            {"action": "exit", "manual": True, "closed_at": "bad",
             "ts": "2026-11-06T21:10:00+00:00"}])
    sale_bad, sale = agent.trade_page()["trades"]
    assert sale["event_at"] == "2026-11-06T20:55:17+00:00"
    assert NOV6_355.fullmatch(sale["event_at_et"])
    # an unreadable fill time falls back to when the agent logged it
    assert sale_bad["event_at"] == "2026-11-06T21:10:00+00:00"
    assert re.fullmatch(r"Nov 6(, 2026)?, 4:10:00 PM ET", sale_bad["event_at_et"])


def test_older_pages_carry_times_too(home):
    _write([{"action": "entry", "ts": f"2026-11-0{1 + i % 5}T15:00:0{i % 10}+00:00"}
            for i in range(30)])
    p1 = agent.trade_page(limit=10)
    p2 = agent.trade_page(before=p1["next"], limit=10)
    assert len(p2["trades"]) == 10
    for r in p1["trades"] + p2["trades"]:
        assert STAMP.match(r["event_at_et"]), r


def test_paging_leaves_the_log_file_untouched(home):
    _write([{"action": "entry", "ts": "2026-11-06T20:55:17+00:00"}])
    before = open(agent.TRADES_PATH, encoding="utf-8").read()
    agent.trade_page()
    assert open(agent.TRADES_PATH, encoding="utf-8").read() == before


# ── the built-in (?ui=classic) dashboard renders the field ──────────────────

def test_classic_dashboard_renders_the_et_time():
    html = webui._DASH_HTML
    assert "when(x)" in html and "x.event_at_et" in html
    assert "when(x.ts)" not in html
