"""Dashboard history: page through the whole trade log, and all-time /
pass-day P&L computed from the full log rather than the latest 25 rows."""

import json

import agent


def _write(home, rows):
    with open(agent.TRADES_PATH, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def _ts(day, hhmm="15:00"):
    # 15:00 UTC = 11:00 ET in October: same calendar day in both zones
    return f"{day}T{hhmm}:00+00:00"


def _entry(day, contract="SPY 2026-10-30 700 C", dry=False, hhmm="14:00"):
    return {"action": "entry", "contract": contract, "dry": dry, "ts": _ts(day, hhmm)}


def _exit(day, pnl, contract="SPY 2026-10-30 700 C", dry=False, hhmm="18:00"):
    r = {"action": "exit", "contract": contract, "dry": dry, "ts": _ts(day, hhmm)}
    if pnl is not None:
        r["est_pnl_usd"] = pnl
    return r


# ── paging ──────────────────────────────────────────────────────────────────

def test_trade_page_walks_the_whole_log(home):
    _write(home, [{"action": "skip_rule", "ts": _ts("2026-10-01"), "i": i}
                  for i in range(120)])
    p1 = agent.trade_page(limit=50)
    assert [t["i"] for t in p1["trades"]][:2] == [119, 118]  # newest first
    assert p1["total"] == 120 and len(p1["trades"]) == 50
    p2 = agent.trade_page(before=p1["next"], limit=50)
    p3 = agent.trade_page(before=p2["next"], limit=50)
    seen = [t["i"] for p in (p1, p2, p3) for t in p["trades"]]
    assert seen == list(range(119, -1, -1))
    assert p3["next"] is None


def test_trade_page_ids_stay_stable_as_rows_are_appended(home):
    _write(home, [{"action": "skip_rule", "i": i} for i in range(5)])
    first = agent.trade_page(limit=2)
    agent._log_trade({"action": "skip_rule", "i": 5})
    older = agent.trade_page(before=first["next"], limit=10)
    assert [t["i"] for t in older["trades"]] == [2, 1, 0]


def test_trade_page_skips_bad_lines_and_bad_args(home):
    with open(agent.TRADES_PATH, "w", encoding="utf-8") as f:
        f.write('{"action":"entry"}\nnot json\n\n[1,2]\n{"action":"exit"}\n')
    p = agent.trade_page(before="x", limit="nope")
    assert [t["action"] for t in p["trades"]] == ["exit", "entry"]
    assert agent.trade_page(limit=10_000)["total"] == 2


def test_trade_page_without_a_log(home):
    assert agent.trade_page() == {"trades": [], "total": 0, "next": None,
                                  "oldest": None}


def test_trade_page_reports_the_first_readable_line(home):
    """A log whose first line can't be read (e.g. a BOM from a Windows
    editor): the dashboard must know row 1 is the oldest, not offer to load
    an older row 0 that doesn't exist."""
    with open(agent.TRADES_PATH, "w", encoding="utf-8-sig") as f:
        for i in range(3):
            f.write(json.dumps({"action": "skip_rule", "i": i}) + "\n")
    p = agent.trade_page()
    assert p["oldest"] == 1 and p["total"] == 2


def test_one_bad_byte_does_not_break_history_or_pnl(home):
    with open(agent.TRADES_PATH, "wb") as f:
        f.write(json.dumps(_entry("2026-10-01")).encode() + b"\n")
        f.write(b'{"action":"skip_rule","reason":"\xff\xfe"}\n')
        f.write(json.dumps(_exit("2026-10-01", 7.0)).encode() + b"\n")
    assert agent.trade_page()["total"] == 3
    assert agent.pnl_summary({"source": "url"}, {})["live"]["total"] == 7.0


# ── all-time P&L ────────────────────────────────────────────────────────────

def test_pnl_counts_exits_beyond_the_old_25_row_window(home):
    rows = [_entry("2026-10-01"), _exit("2026-10-01", 100.0)]
    rows += [{"action": "skip_rule", "ts": _ts("2026-10-02")}] * 60
    rows += [_entry("2026-10-05"), _exit("2026-10-05", -40.0)]
    _write(home, rows)
    s = agent.pnl_summary({"source": "agenthc"}, {})
    assert s["live"]["total"] == 60.0
    assert (s["live"]["exits"], s["live"]["wins"], s["live"]["losses"]) == (2, 1, 1)
    assert [p["cum"] for p in s["live"]["points"]] == [100.0, 60.0]


def test_pnl_keeps_dry_run_separate_and_counts_unmeasured(home):
    _write(home, [
        _entry("2026-10-01"), _exit("2026-10-01", 25.0),
        _entry("2026-10-01", contract="QQQ", dry=True),
        _exit("2026-10-01", 999.0, contract="QQQ", dry=True),
        _entry("2026-10-02"), _exit("2026-10-02", None),
    ])
    s = agent.pnl_summary({"source": "agenthc"}, {})
    assert s["live"]["total"] == 25.0 and s["live"]["unmeasured"] == 1
    assert s["dry"]["total"] == 999.0


def test_overnight_exit_counts_on_the_day_it_was_opened(home):
    _write(home, [_entry("2026-10-01"), _exit("2026-10-02", 50.0, hhmm="14:00")])
    s = agent.pnl_summary({"source": "agenthc"}, {"paid_days": ["2026-10-01"]})
    assert s["live"]["days"] == [{"day": "2026-10-01", "pnl": 50.0, "exits": 1,
                                  "pass": "paid"}]
    assert s["live"]["pass"] == {"total": 50.0, "exits": 1}


def test_closes_outside_the_agent_count_and_keep_the_opened_day(home):
    """The owner sold 2 of 3 in the app (a partial row), the agent's exit
    sold the last one the next day: both credit the day it was opened. A
    close with no sale found has no price and counts as unmeasured."""
    c = "SPY 2099-12-18 $752.0 C"
    _write(home, [
        _entry("2026-10-01", contract=c),
        dict(_exit("2026-10-02", 60.0, contract=c), manual=True, partial=True,
             remaining=1, qty=2),
        _exit("2026-10-03", -20.0, contract=c),
        _entry("2026-10-05", contract="QQQ"),
        dict(_exit("2026-10-06", None, contract="QQQ"), manual=True, cause="gone"),
    ])
    s = agent.pnl_summary({"source": "url"}, {})
    assert s["live"]["days"] == [{"day": "2026-10-01", "pnl": 40.0, "exits": 2,
                                  "pass": None}]
    assert s["live"]["total"] == 40.0 and s["live"]["unmeasured"] == 1


def test_loss_cap_counts_a_close_on_the_day_its_sale_filled(home, frozen_now):
    from datetime import timedelta
    now = frozen_now  # one clock for the rows and for "today"
    yesterday = now - timedelta(days=1)
    agent._log_trade({"action": "exit", "manual": True, "est_pnl_usd": -40.0,
                      "closed_at": now.isoformat(timespec="seconds")})
    agent._log_trade({"action": "exit", "manual": True, "est_pnl_usd": -500.0,
                      "closed_at": yesterday.isoformat(timespec="seconds")})
    assert agent._est_pnl_today() == (-40.0, 0)


def test_trade_day_is_the_market_day_not_utc(home):
    # 01:00 UTC on the 2nd is 21:00 ET on the 1st
    _write(home, [_entry("2026-10-02", hhmm="01:00"), _exit("2026-10-02", 5.0)])
    s = agent.pnl_summary({"source": "agenthc"}, {})
    assert s["live"]["days"][0]["day"] == "2026-10-01"


# ── pass days ───────────────────────────────────────────────────────────────

def test_pass_days_recorded_and_estimated_before_recording_began(home):
    _write(home, [
        _entry("2026-10-01"), _exit("2026-10-01", 10.0),  # before recording: est
        _entry("2026-10-05"), _exit("2026-10-05", 20.0),  # recorded paid day
        _entry("2026-10-06"), _exit("2026-10-06", 40.0),  # after recording, unpaid
    ])
    s = agent.pnl_summary({"source": "agenthc"}, {"paid_days": ["2026-10-05"]})
    by_day = {d["day"]: d["pass"] for d in s["live"]["days"]}
    assert by_day == {"2026-10-01": "est", "2026-10-05": "paid", "2026-10-06": None}
    assert s["live"]["pass"] == {"total": 30.0, "exits": 2}
    assert s["live"]["total"] == 70.0
    assert s["pass_days"] == {"recorded": 1, "estimated": 1}
    assert s["tracked_from"] == "2026-10-05"


def test_older_single_day_markers_count_as_paid(home):
    """state.json from before this version: only pass_charged_day / the
    wallet's L402 token say which day was paid."""
    from datetime import datetime
    exp = datetime(2026, 10, 8, 10, 0, tzinfo=agent.MARKET_TZ).timestamp() + 23 * 3600
    st = {"pass_charged_day": "2026-10-07", "l402": {"expires_at": exp}}
    assert agent._paid_days(st) == {"2026-10-07", "2026-10-08"}
    assert agent._paid_days({}) == set()


def test_stale_legacy_marker_does_not_move_the_estimate_cutoff(home):
    """An old wallet day-pass token / pass_charged_day is never cleared. It
    must not push the 'before recording began' cutoff back and strip the
    label from real pass days in between (review finding)."""
    from datetime import datetime
    old_buy = datetime(2026, 9, 1, 10, 0, tzinfo=agent.MARKET_TZ).timestamp()
    _write(home, [
        _entry("2026-08-20"), _exit("2026-08-20", 100.0),
        _entry("2026-09-15"), _exit("2026-09-15", 200.0),
        _entry("2026-10-10"), _exit("2026-10-10", 300.0),
        _entry("2026-10-28"), _exit("2026-10-28", 400.0),
    ])
    st = {"l402": {"expires_at": old_buy + 23 * 3600},
          "pass_charged_day": "2026-10-28"}
    s = agent.pnl_summary({"source": "agenthc"}, st)  # just upgraded: nothing recorded
    assert {d["day"]: d["pass"] for d in s["live"]["days"]} == {
        "2026-08-20": "est", "2026-09-15": "est", "2026-10-10": "est",
        "2026-10-28": "paid"}
    assert s["live"]["pass"] == {"total": 1000.0, "exits": 4}
    st["paid_days"] = ["2026-10-29"]  # recording has begun
    s = agent.pnl_summary({"source": "agenthc"}, st)
    assert s["tracked_from"] == "2026-10-29"
    assert s["live"]["pass"]["total"] == 1000.0


def test_premium_api_key_has_no_pass_days(home):
    """A subscription has no per-day charges: no 'pass (est.)' on every day."""
    _write(home, [_entry("2026-11-10"), _exit("2026-11-10", 10.0)])
    s = agent.pnl_summary({"source": "agenthc", "agenthc_api_key": "k"},
                          {"pass_charged_day": "2026-09-01"})
    assert s["passes"] is False
    assert s["live"]["days"][0]["pass"] is None
    assert s["live"]["pass"] == {"total": 0.0, "exits": 0}
    assert s["live"]["total"] == 10.0


def test_non_agenthc_source_has_no_pass_days(home):
    _write(home, [_entry("2026-10-01"), _exit("2026-10-01", 10.0)])
    s = agent.pnl_summary({"source": "url"}, {"paid_days": ["2026-10-01"]})
    assert s["passes"] is False
    assert s["live"]["pass"] == {"total": 0.0, "exits": 0}
    assert s["live"]["days"][0]["pass"] is None
    assert s["pass_days"] == {"recorded": 0, "estimated": 0}


def test_pnl_reads_config_and_state_from_disk(home):
    _write(home, [_entry("2026-10-01"), _exit("2026-10-01", 12.5)])
    agent._save(agent.STATE_PATH, {"paid_days": ["2026-10-01"]})
    s = agent.pnl_summary()
    assert s["live"]["pass"]["total"] == 12.5


def test_dashboard_serves_the_pnl_route():
    import inspect

    import webui
    src = inspect.getsource(webui.start_app)
    assert '"/api/pnl": d_pnl' in src
    assert "A.trade_page(before=before, limit=limit)" in src
