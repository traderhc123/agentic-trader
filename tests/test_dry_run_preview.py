"""Dry-run preview: AgentHC's free closed-trades list checked against the
owner's built-in rules. No network: the fetcher is faked."""

import agent
import dry_run_preview as P


def _t(ticker, direction, strike, pnl, date="2026-10-19", hhmm="10:10", expiry="2026-10-20"):
    return {"date": date, "ticker": ticker, "direction": direction, "strike": strike,
            "expiry": expiry, "entry_time": f"{date}T{hhmm}:00-04:00",
            "exit_time": f"{date}T{hhmm}:30-04:00", "hold_minutes": 4,
            "paper_pnl_pct": pnl, "exit_kind": "target_hit"}


TRADES = [_t("QQQ", "calls", 720.0, 33.9), _t("SPY", "puts", 600.0, -12.0, date="2026-10-16"),
          _t("IWM", "calls", 250.0, 3.0, date="2026-10-15")]


def test_only_in_dry_run_for_agenthc():
    assert P.wanted({"dry_run": True, "source": "agenthc"})
    assert not P.wanted({"dry_run": False, "source": "agenthc"})
    assert not P.wanted({"dry_run": True, "source": "webhook"})
    assert not P.wanted({"dry_run": True, "dry_run_preview": False})


def test_rules_decide_each_row():
    rows = P.evaluate({"rule_sides": "calls", "rule_min_strike": 300}, TRADES)
    assert [r["contract"].split()[0] for r in rows] == ["QQQ", "SPY", "IWM"]  # newest first
    q, s, i = rows
    assert q["would_take"] and q["contract"] == "QQQ 720C exp 2026-10-20"
    assert not s["would_take"] and "calls only" in s["skip_reason"]
    assert not i["would_take"] and "minimum" in i["skip_reason"]


def test_no_rules_takes_everything_and_scores_w_be_l(home):
    cfg = {"dry_run": True, "source": "agenthc"}
    P.refresh(cfg, market_open=True, fetcher=lambda: TRADES)
    s = P.summary(cfg)
    assert s["would_take"] == 3
    assert (s["wins"], s["breakeven"], s["losses"]) == (1, 1, 1)   # +3% = breakeven
    assert s["avg_paper_pnl_pct"] == round((33.9 - 12.0 + 3.0) / 3, 1)


def test_loss_stop_uses_earlier_paper_exits_same_day():
    day = [_t("SPY", "puts", 600.0, -20.0, hhmm="10:00"),
           _t("QQQ", "calls", 700.0, 10.0, hhmm="11:00")]
    rows = P.evaluate({"rule_loss_streak": 1}, day)
    assert rows[1]["would_take"]                                  # the first loss
    assert not rows[0]["would_take"] and "losing exit" in rows[0]["skip_reason"]


def test_0dte_rule_judged_on_the_entry_day():
    rows = P.evaluate({"rule_skip_0dte": True},
                      [_t("SPY", "calls", 600.0, 5.0, expiry="2026-10-19")])
    assert not rows[0]["would_take"]


def test_refresh_cadence_and_retry(home):
    calls = []

    def f():
        calls.append(1)
        return TRADES
    cfg = {"dry_run": True}
    P.refresh(cfg, True, now=1000, fetcher=f)
    P.refresh(cfg, True, now=1000 + 60, fetcher=f)                 # not due yet
    assert len(calls) == 1
    P.refresh(cfg, True, now=1000 + P.REFRESH_MARKET_S + 1, fetcher=f)
    assert len(calls) == 2

    def boom():
        raise OSError("offline")
    d = P.refresh(cfg, True, now=10**6, fetcher=boom)              # Wi-Fi down
    assert d["error"] == "offline" and d["trades"] == TRADES       # keeps last list
    assert P.summary({"dry_run": True})["error"] == ""             # rows still shown


def test_server_without_the_endpoint(home):
    d = P.refresh({"dry_run": True}, False, fetcher=lambda: None)
    assert d["unavailable"] and P.summary({"dry_run": True})["unavailable"]


def test_fetch_reads_the_free_endpoint(monkeypatch):
    seen = {}

    class R:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"trades": TRADES + ["junk"]}
    monkeypatch.setattr(P._http, "get",
                        lambda url, params=None, timeout=None: seen.update(url=url, params=params) or R())
    assert P.fetch() == TRADES
    assert seen["url"].endswith("/api/v1/trading/day-trade-ideas/closed")
    assert seen["params"] == {"days": 7}


def test_chat_toggle(home):
    assert agent._apply_command("preview off")[0]
    assert agent._load(agent.CONFIG_PATH, {})["dry_run_preview"] is False
    assert "ON" in agent._apply_command("preview on")[1]
