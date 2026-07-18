"""Per-track position sizing + weekend day-pass gate."""

from datetime import datetime

from agent import _sizing_cfg
from sources import agenthc_day_trade_ideas as agenthc


def _ev(track):
    return {"event": "ENTERED", "ticker": "SPY", "expiry": "2026-07-24",
            "strike": 630.0, "type": "C", "track": track}


BASE = {"sizing_mode": "budget", "budget_per_trade_usd": 500,
        "contracts_per_trade": 1, "max_contracts_per_trade": 25}


# ── _sizing_cfg: main vs other-track overlay ─────────────────────────────────

def test_main_track_uses_base_config():
    assert _sizing_cfg(BASE, _ev("main")) is BASE


def test_missing_track_treated_as_main():
    ev = _ev("main")
    del ev["track"]
    assert _sizing_cfg(BASE, ev) is BASE


def test_other_track_without_overrides_uses_base_config():
    assert _sizing_cfg(BASE, _ev("other")) is BASE


def test_other_track_budget_override():
    cfg = {**BASE, "other_sizing_mode": "budget",
           "other_budget_per_trade_usd": 100}
    eff = _sizing_cfg(cfg, _ev("other"))
    assert eff["budget_per_trade_usd"] == 100
    assert eff["sizing_mode"] == "budget"
    # main events still see the original budget
    assert _sizing_cfg(cfg, _ev("main"))["budget_per_trade_usd"] == 500


def test_other_track_contracts_override_switches_mode():
    cfg = {**BASE, "other_sizing_mode": "contracts",
           "other_contracts_per_trade": 2}
    eff = _sizing_cfg(cfg, _ev("other"))
    assert eff["sizing_mode"] == "contracts"
    assert eff["contracts_per_trade"] == 2


def test_override_does_not_mutate_base_config():
    cfg = {**BASE, "other_sizing_mode": "budget",
           "other_budget_per_trade_usd": 100}
    _sizing_cfg(cfg, _ev("other"))
    assert cfg["budget_per_trade_usd"] == 500


# ── weekend day-pass gate ────────────────────────────────────────────────────

SAT = datetime(2026, 7, 18, 9, 0, tzinfo=agenthc.MARKET_TZ)
SUN = datetime(2026, 7, 19, 9, 0, tzinfo=agenthc.MARKET_TZ)
MON = datetime(2026, 7, 20, 9, 0, tzinfo=agenthc.MARKET_TZ)


def test_no_buy_on_weekend_even_with_recurring_on():
    for day in (SAT, SUN):
        assert agenthc._pass_purchase_allowed(
            {"day_pass_recurring": True}, {}, lambda s: None, now=day) is False


def test_weekend_skip_notifies_once_per_day(capsys):
    state, saved = {}, []
    for _ in range(3):
        agenthc._pass_purchase_allowed({"day_pass_recurring": True}, state,
                                       saved.append, now=SAT)
    assert len(saved) == 1  # notice recorded once, not per poll
    assert capsys.readouterr().out.count("weekend") == 1


def test_weekday_recurring_still_buys():
    assert agenthc._pass_purchase_allowed(
        {"day_pass_recurring": True}, {}, lambda s: None, now=MON) is True


def test_weekday_recurring_off_still_blocked():
    assert agenthc._pass_purchase_allowed(
        {}, {}, lambda s: None, now=MON) is False
