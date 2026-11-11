"""The /v2 Results chart draws each book's running total from the view-model's
`results.<book>.days`. These checks hold the server side of that promise: the
days cover the whole trade log (not a recent window), and they add up, in
whole cents, to exactly the totals the Results panels print — for all time
and for the panels' "Last 7 days". (webui_app/tests/guardian.test.mjs checks
the browser side: lib/chart.ts ends each period on those same figures.)"""

from datetime import datetime, timedelta, timezone

import pytest

import view_model as vm

import guardian_states as gs


def _cents(v):
    return round(v * 100)


def _week(now):
    import agent as A
    today = now.astimezone(A.MARKET_TZ).date()
    return (today - timedelta(days=6)).isoformat(), today.isoformat()


@pytest.mark.parametrize("name", sorted(gs.STATES))
def test_days_add_up_to_the_panel_totals(home, name):
    s = gs.status(name)
    lo, hi = _week(gs.NOW)
    for k in ("real", "practice", "paper_account"):
        b = s["results"][k]
        assert sum(_cents(d["pnl"]) for d in b["days"]) == _cents(b["total"]), (name, k)
        assert sum(_cents(d["pnl"]) for d in b["days"]
                   if lo <= d["day"] <= hi) == _cents(b["last_7_days"]), (name, k)
        assert sum(d["closed"] for d in b["days"]) == b["closed"], (name, k)


def test_days_reach_back_over_the_whole_log(home):
    """A sale from more than a year ago is a day of its own: the chart's
    "All time" needs every day, not the last few weeks."""
    old = (gs.NOW - timedelta(days=400)).astimezone(timezone.utc)
    old_day = (old - timedelta(hours=5)).date().isoformat()
    c = ("SPY", "2025-10-10", 600, "C")
    rows = [gs.entry(c, gs.at(10, 0, old_day), 1, 1.0, True),
            gs.exit_(c, gs.at(11, 0, old_day), 1, -42.5, True)] + gs._history(True)
    rows = [{**r, "n": i} for i, r in enumerate(rows)]
    s = vm.build_status(gs.LIVE, gs._state(True), rows, gs.runtime(), now=gs.NOW)
    real = s["results"]["real"]
    assert real["days"][-1] == {"day": old_day, "pnl": -42.5, "closed": 1}
    assert sum(_cents(d["pnl"]) for d in real["days"]) == _cents(real["total"])
    vm.validate(s)


def test_days_are_sums_of_cents_however_many_sales(home):
    """Many small results on one day still add up to the cent the panel
    shows (no float drift between the series and the total)."""
    c = ("SPY", "2026-11-13", 700, "C")
    rows = []
    for i in range(30):
        rows += [gs.entry(c, gs.at(10, i), 1, 1.0, True),
                 gs.exit_(c, gs.at(11, i), 1, 0.1 if i % 2 else 0.2, True)]
    rows = [{**r, "n": i} for i, r in enumerate(rows)]
    s = vm.build_status(gs.LIVE, gs._state(True), rows, gs.runtime(), now=gs.NOW)
    real = s["results"]["real"]
    assert real["days"] == [{"day": gs.TODAY, "pnl": 4.5, "closed": 30}]
    assert real["total"] == 4.5 == real["last_7_days"] == real["today"]


def test_fixture_moment_is_a_weekday_in_et():
    # the chart's axis is market days; the fixtures sit on a Monday
    assert datetime.strptime(gs.TODAY, "%Y-%m-%d").weekday() == 0
