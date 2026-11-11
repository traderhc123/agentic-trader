"""Signal sources: contract normalization, dedup ids, L402 day-pass flow."""

import json
import time
from datetime import datetime

import pytest

from sources import SOURCES, agenthc_day_trade_ideas as agenthc
from sources import generic_json_url, manual_file


@pytest.fixture(autouse=True)
def market_weekday(monkeypatch):
    """Pin 'now' to a Wednesday so the weekend day-pass gate never makes
    these tests pass/fail depending on which day CI happens to run."""
    monkeypatch.setattr(agenthc, "_now_et",
                        lambda: datetime(2026, 7, 15, 10, 30,
                                         tzinfo=agenthc.MARKET_TZ))


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("AGENT_HOME", str(h))
    return h


def test_registry():
    assert set(SOURCES) == {"agenthc", "manual", "url"}
    for mod in SOURCES.values():
        assert mod.NAME and mod.DESCRIPTION and callable(mod.poll)


# ── manual commands file ─────────────────────────────────────────────────────

def test_manual_parses_and_normalizes(home):
    lines = [
        '{"action": "enter", "ticker": "spy", "expiry": "2026-07-10",'
        ' "strike": "752", "type": "call"}',
        '{"action": "exit", "ticker": "SPY", "expiry": "2026-07-10",'
        ' "strike": 752, "type": "C"}',
        'not json at all',
        '{"action": "reboot", "ticker": "SPY"}',      # unknown action
        '{"action": "enter", "ticker": "SPY"}',        # missing fields
    ]
    (home / "commands.jsonl").write_text("\n".join(lines) + "\n")
    events = manual_file.poll({}, {})
    assert len(events) == 2
    ent, ex = events
    assert ent["event"] == "ENTERED" and ex["event"] == "EXITED"
    assert ent["ticker"] == "SPY"            # uppercased
    assert ent["strike"] == 752.0            # coerced to float
    assert ent["type"] == "C"                # "call" -> C
    assert ent["event_id"].startswith("manual-")


def test_manual_ids_stable_for_replay_dedup(home):
    line = ('{"action": "enter", "ticker": "SPY", "expiry": "2026-07-10",'
            ' "strike": 752, "type": "C"}')
    (home / "commands.jsonl").write_text(line + "\n")
    id1 = manual_file.poll({}, {})[0]["event_id"]
    id2 = manual_file.poll({}, {})[0]["event_id"]
    assert id1 == id2  # same line -> same id -> executed exactly once


def test_manual_no_file_is_empty(home):
    assert manual_file.poll({}, {}) == []


# ── generic url feed ─────────────────────────────────────────────────────────

class FakeResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def test_url_normalizes_and_synthesizes_ids(monkeypatch):
    payload = {"events": [
        {"event": "ENTERED", "ticker": "spy", "expiry": "2026-07-10",
         "strike": "752", "type": "call"},
        {"event": "IGNORE_ME", "ticker": "SPY"},
        {"event": "ENTERED"},  # missing everything
    ]}
    monkeypatch.setattr(generic_json_url.requests, "get",
                        lambda url, headers, timeout: FakeResp(payload))
    events = generic_json_url.poll({"source_url": "https://x.example/feed"}, {})
    assert len(events) == 1
    ev = events[0]
    assert (ev["ticker"], ev["strike"], ev["type"]) == ("SPY", 752.0, "C")
    assert ev["event_id"]  # synthesized from the identity fields


def test_url_unset_returns_empty():
    assert generic_json_url.poll({}, {}) == []


# ── agenthc feed: headers, normalization, L402 day-pass ─────────────────────

def test_headers_prefer_api_key_then_daypass():
    assert agenthc._headers({"agenthc_api_key": "k"}, {}) == {"X-API-Key": "k"}
    live = {"l402": {"token": "L402 m:p", "expires_at": time.time() + 100}}
    assert agenthc._headers({}, live) == {"Authorization": "L402 m:p"}
    expired = {"l402": {"token": "L402 m:p", "expires_at": time.time() - 1}}
    assert agenthc._headers({}, expired) == {}
    assert agenthc._headers({}, {}) == {}


def _feed_event(**over):
    ev = {"event": "ENTERED", "ticker": "SPY", "expiry": "2026-07-10",
          "strike": 752.0, "type": "C",
          "occurred_at": "2026-07-09T14:00:00+00:00"}
    ev.update(over)
    return ev


def test_agenthc_normalizes_wire_types(monkeypatch):
    payload = {"events": [_feed_event(strike="752.5", ticker="nvda",
                                      type="call")]}
    monkeypatch.setattr(agenthc.requests, "get",
                        lambda *a, **kw: FakeResp(payload))
    events = agenthc.poll({"agenthc_api_key": "k"}, {})
    ev = events[0]
    assert (ev["ticker"], ev["strike"], ev["type"]) == ("NVDA", 752.5, "C")
    assert isinstance(ev["strike"], float)


def test_agenthc_event_id_is_identity_composed(monkeypatch):
    payload = {"events": [_feed_event()]}
    monkeypatch.setattr(agenthc.requests, "get",
                        lambda *a, **kw: FakeResp(payload))
    ev = agenthc.poll({"agenthc_api_key": "k"}, {})[0]
    assert ev["event_id"] == ("ENTERED|SPY|2026-07-10|752.0|C|"
                              "2026-07-09T14:00:00+00:00")


def test_agenthc_requests_main_track_by_default(monkeypatch):
    captured = {}

    def fake_get(url, params=None, **kw):
        captured.update(params or {})
        return FakeResp({"events": [_feed_event()]})

    monkeypatch.setattr(agenthc.requests, "get", fake_get)
    ev = agenthc.poll({"agenthc_api_key": "k"}, {})[0]
    assert captured["track"] == "main"
    assert ev["track"] == "main"  # pre-track wire events count as main
    # legacy event_id format preserved for main — upgrades must not reset
    # the seen-events dedupe
    assert "|main" not in ev["event_id"]


def test_agenthc_other_trades_opt_in(monkeypatch):
    captured = {}

    def fake_get(url, params=None, **kw):
        captured.update(params or {})
        return FakeResp({"events": [_feed_event(track="other"),
                                    _feed_event(track="main")]})

    monkeypatch.setattr(agenthc.requests, "get", fake_get)
    events = agenthc.poll({"agenthc_api_key": "k",
                           "include_other_trades": True}, {})
    assert captured["track"] == "all"
    other, main = events[0], events[1]
    assert other["track"] == "other"
    assert other["event_id"].endswith("|other")
    assert main["event_id"] == ("ENTERED|SPY|2026-07-10|752.0|C|"
                                "2026-07-09T14:00:00+00:00")


def test_agenthc_drops_malformed_events(monkeypatch):
    payload = {"events": [_feed_event(strike="not-a-price"),
                          _feed_event(strike=None),
                          _feed_event()]}
    monkeypatch.setattr(agenthc.requests, "get",
                        lambda *a, **kw: FakeResp(payload))
    assert len(agenthc.poll({"agenthc_api_key": "k"}, {})) == 1


def test_agenthc_feed_not_live_returns_empty(monkeypatch):
    resp = FakeResp({"detail": {"error": "feed_not_live"}}, status=403)
    monkeypatch.setattr(agenthc.requests, "get", lambda *a, **kw: resp)
    assert agenthc.poll({"agenthc_api_key": "k"}, {}) == []


def _invoice(sats):
    """Invoice-shaped text asking for `sats`: the agent reads the amount (and
    the payment id) from the invoice itself, so a placeholder won't do."""
    return (f"lnbc{sats * 10}n1" + "q" * 7 + "pp5" + "q" * 51 + "s"
            + "q" * 110)


class FakeWallet:
    def __init__(self):
        self.paid = []

    def pay_invoice(self, invoice):
        self.paid.append(invoice)
        return "preimage-abc"


def test_agenthc_402_buys_day_pass_and_retries(monkeypatch):
    """402 -> pay invoice -> retry with fresh L402 token -> events."""
    wallet = FakeWallet()
    monkeypatch.setattr(agenthc, "wallet_from_cfg", lambda cfg: wallet)
    calls = []

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append(headers or {})
        if len(calls) == 1:
            return FakeResp({"payment": {"payment_request": _invoice(9500),
                                         "macaroon": "MAC",
                                         "amount_sats": 9500}}, status=402)
        return FakeResp({"events": [_feed_event()]})

    monkeypatch.setattr(agenthc.requests, "get", fake_get)
    state = {}
    events = agenthc.poll({"day_pass_recurring": True}, state,
                          save_state=lambda s: None)
    assert wallet.paid == [_invoice(9500)]
    assert state["l402"]["token"] == "L402 MAC:preimage-abc"
    assert calls[1]["Authorization"] == "L402 MAC:preimage-abc"
    assert len(events) == 1
    # the dashboard's pass-day P&L knows today was paid for
    assert state["paid_days"] == [agenthc._now_et().strftime("%Y-%m-%d")]


def test_agenthc_402_respects_autopay_cap(monkeypatch):
    monkeypatch.setattr(agenthc, "wallet_from_cfg", lambda cfg: FakeWallet())
    resp = FakeResp({"payment": {"payment_request": _invoice(999_999),
                                 "macaroon": "MAC",
                                 "amount_sats": 999_999}}, status=402)
    monkeypatch.setattr(agenthc.requests, "get", lambda *a, **kw: resp)
    with pytest.raises(RuntimeError, match="auto-pay cap"):
        agenthc.poll({"max_autopay_sats": 30_000,
                      "day_pass_recurring": True}, {}, lambda s: None)


def test_agenthc_402_without_wallet_is_actionable(monkeypatch):
    monkeypatch.setattr(agenthc, "wallet_from_cfg", lambda cfg: None)
    resp = FakeResp({"payment": {}}, status=402)
    monkeypatch.setattr(agenthc.requests, "get", lambda *a, **kw: resp)
    with pytest.raises(RuntimeError, match="wallet"):
        agenthc.poll({"day_pass_recurring": True}, {}, lambda s: None)


def test_agenthc_402_recurring_off_does_not_pay(monkeypatch):
    """Default (recurring OFF): the agent must never auto-spend — no pay,
    empty events, once-per-day notify flag persisted."""
    wallet = FakeWallet()
    monkeypatch.setattr(agenthc, "wallet_from_cfg", lambda cfg: wallet)
    resp = FakeResp({"payment": {"payment_request": _invoice(9500),
                                 "macaroon": "MAC",
                                 "amount_sats": 9500}}, status=402)
    monkeypatch.setattr(agenthc.requests, "get", lambda *a, **kw: resp)
    saved = []
    state = {}
    events = agenthc.poll({}, state, save_state=saved.append)
    assert events == []
    assert wallet.paid == []
    assert state.get("pass_needed_notified")
    assert saved  # notify flag persisted


def test_agenthc_402_recurring_on_pays(monkeypatch):
    wallet = FakeWallet()
    monkeypatch.setattr(agenthc, "wallet_from_cfg", lambda cfg: wallet)
    calls = []

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append(1)
        if len(calls) == 1:
            return FakeResp({"payment": {"payment_request": _invoice(9500),
                                         "macaroon": "MAC",
                                         "amount_sats": 9500}}, status=402)
        return FakeResp({"events": [_feed_event()]})

    monkeypatch.setattr(agenthc.requests, "get", fake_get)
    events = agenthc.poll({"day_pass_recurring": True}, {}, lambda s: None)
    assert wallet.paid == [_invoice(9500)]
    assert len(events) == 1


def test_agenthc_recurring_off_skips_network_entirely(monkeypatch):
    """When access is impossible (no key, no token, recurring OFF) the agent
    must not even make the request — each such GET makes the server mint a
    Lightning invoice that is guaranteed to expire unpaid."""
    def exploding_get(*a, **kw):
        raise AssertionError("poll() must not hit the network when it "
                             "cannot obtain access")
    monkeypatch.setattr(agenthc.requests, "get", exploding_get)
    saved = []
    state = {}
    events = agenthc.poll({}, state, save_state=saved.append)
    assert events == []
    assert state.get("pass_needed_notified")  # once-per-day notify still fires
    assert saved


def test_agenthc_weekend_skips_network_entirely(monkeypatch):
    """Weekend + no credentials: no request, no invoice churn."""
    monkeypatch.setattr(agenthc, "_now_et",
                        lambda: datetime(2026, 7, 18, 12, 0,
                                         tzinfo=agenthc.MARKET_TZ))  # Saturday
    monkeypatch.setattr(agenthc.requests, "get",
                        lambda *a, **kw: (_ for _ in ()).throw(
                            AssertionError("no network on weekend skip")))
    state = {}
    assert agenthc.poll({"day_pass_recurring": True}, state, lambda s: None) == []
    assert state.get("market_closed_skip_notified")


def test_agenthc_live_token_still_polls(monkeypatch):
    """A cached unexpired day-pass token must keep polling normally even with
    recurring OFF — the guard only blocks access-impossible states."""
    monkeypatch.setattr(agenthc.requests, "get",
                        lambda *a, **kw: FakeResp({"events": [_feed_event()]}))
    state = {"l402": {"token": "L402 m:p", "expires_at": time.time() + 3600}}
    events = agenthc.poll({}, state, save_state=lambda s: None)
    assert len(events) == 1


def test_running_daypass_records_each_market_day_it_covers(monkeypatch):
    """A day-pass bought yesterday afternoon still covers this morning: a
    feed read with it during the session marks today as paid (pass-day
    P&L). Reads on weekends or outside market hours don't."""
    monkeypatch.setattr(agenthc.requests, "get",
                        lambda *a, **kw: FakeResp({"events": [_feed_event()]}))
    saved = []
    state = {"l402": {"token": "L402 m:p", "expires_at": time.time() + 3600},
             "paid_days": ["2026-07-14"]}
    agenthc.poll({}, state, save_state=saved.append)
    assert state["paid_days"] == ["2026-07-14", "2026-07-15"]
    assert saved
    saved.clear()
    agenthc.poll({}, state, save_state=saved.append)  # same day: no re-save
    assert state["paid_days"] == ["2026-07-14", "2026-07-15"] and not saved
    for when in (datetime(2026, 7, 18, 11, 0),   # Saturday
                 datetime(2026, 7, 16, 3, 0),    # Thursday, overnight
                 datetime(2026, 7, 16, 9, 29),   # Thursday, pre-market
                 datetime(2026, 7, 16, 16, 0)):  # Thursday, after the close
        monkeypatch.setattr(agenthc, "_now_et",
                            lambda w=when: w.replace(tzinfo=agenthc.MARKET_TZ))
        agenthc.poll({}, state, save_state=saved.append)
        assert state["paid_days"] == ["2026-07-14", "2026-07-15"], when
    monkeypatch.setattr(agenthc, "_now_et",
                        lambda: datetime(2026, 7, 16, 9, 30,
                                         tzinfo=agenthc.MARKET_TZ))  # the open
    agenthc.poll({}, state, save_state=saved.append)
    assert state["paid_days"][-1] == "2026-07-16"


def test_api_key_reads_record_no_paid_days(monkeypatch):
    monkeypatch.setattr(agenthc.requests, "get",
                        lambda *a, **kw: FakeResp({"events": [_feed_event()]}))
    state = {}
    agenthc.poll({"agenthc_api_key": "k"}, state, save_state=lambda s: None)
    assert "paid_days" not in state


def test_agenthc_api_key_still_polls(monkeypatch):
    """An API-key consumer is unaffected by the day-pass purchase gate."""
    monkeypatch.setattr(agenthc.requests, "get",
                        lambda *a, **kw: FakeResp({"events": [_feed_event()]}))
    events = agenthc.poll({"agenthc_api_key": "k"}, {}, save_state=lambda s: None)
    assert len(events) == 1


def test_agenthc_daily_autopay_cap_stops_rebuy_loop(monkeypatch):
    """A server that keeps rejecting a valid pass must not drain the wallet:
    one pass per day by default, then polls go quiet until tomorrow."""
    wallet = FakeWallet()
    monkeypatch.setattr(agenthc, "wallet_from_cfg", lambda cfg: wallet)
    resp = FakeResp({"payment": {"payment_request": _invoice(16_000),
                                 "macaroon": "MAC",
                                 "amount_sats": 16_000}}, status=402)
    monkeypatch.setattr(agenthc.requests, "get", lambda *a, **kw: resp)
    cfg = {"day_pass_recurring": True, "max_autopay_sats": 30_000}
    state = {}
    for _ in range(20):  # 20 polls against a server that never honours the pass
        try:
            agenthc.poll(cfg, state, save_state=lambda s: None)
        except Exception:
            pass
        state.pop("l402", None)  # as if the token were rejected/expired
    assert wallet.paid == [_invoice(16_000)]
    assert state["autopay_spent"]["sats"] == 16_000


def test_agenthc_daily_autopay_cap_is_configurable(monkeypatch):
    wallet = FakeWallet()
    monkeypatch.setattr(agenthc, "wallet_from_cfg", lambda cfg: wallet)
    resp = FakeResp({"payment": {"payment_request": _invoice(16_000),
                                 "macaroon": "MAC",
                                 "amount_sats": 16_000}}, status=402)
    monkeypatch.setattr(agenthc.requests, "get", lambda *a, **kw: resp)
    cfg = {"day_pass_recurring": True, "max_autopay_sats_per_day": 50_000}
    state = {}
    for _ in range(10):
        try:
            agenthc.poll(cfg, state, save_state=lambda s: None)
        except Exception:
            pass
        state.pop("l402", None)
    assert len(wallet.paid) == 3  # 48,000 <= 50,000 < 64,000


# ── prepaid multi-day pass ──────────────────────────────────────────────────

PASS_TOK = "L402 MACPASS:paid"


class _PassServer:
    """Scripted AgentHC: pass status + feed, recording every request."""

    def __init__(self, status="active", days_left=10, feed_status=200):
        self.status, self.days_left, self.feed_status = status, days_left, feed_status
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, dict(headers or {})))
        if url.endswith("/pass"):
            return FakeResp({"status": self.status,
                             "access_until": "2030-01-02T00:00:00+00:00",
                             "trading_days_left": self.days_left})
        return FakeResp({"events": [{"event": "ENTERED", "ticker": "SPY",
                                     "expiry": "2026-07-17", "strike": 600,
                                     "type": "C", "occurred_at": "x"}],
                         "access": "multiday_pass",
                         "pass_trading_days_left": self.days_left},
                        status=self.feed_status)


def _pass_cfg(**kw):
    return {"source": "agenthc", "feed_pass": {"token": PASS_TOK}, **kw}


def test_active_pass_reads_feed_with_pass_token(monkeypatch):
    srv = _PassServer()
    monkeypatch.setattr(agenthc.requests, "get", srv.get)
    state = {}
    events = agenthc.poll(_pass_cfg(), state, lambda s: None)
    assert len(events) == 1
    feed_calls = [h for u, h in srv.calls if u.endswith("day-trade-ideas")]
    assert feed_calls and feed_calls[0]["Authorization"] == PASS_TOK
    # status checked once, then cached while active
    agenthc.poll(_pass_cfg(), state, lambda s: None)
    assert sum(1 for u, _ in srv.calls if u.endswith("/pass")) == 1


def test_pending_pass_waits_quietly_without_reading_feed(monkeypatch):
    srv = _PassServer(status="pending")
    monkeypatch.setattr(agenthc.requests, "get", srv.get)
    notified = []
    monkeypatch.setattr(agenthc, "_pass_notice", lambda *a: notified.append(a))
    assert agenthc.poll(_pass_cfg(), {}, lambda s: None) == []
    assert not any(u.endswith("day-trade-ideas") for u, _ in srv.calls)
    assert notified == []


def test_expired_pass_notifies_once_and_never_buys(monkeypatch):
    srv = _PassServer(status="expired")
    monkeypatch.setattr(agenthc.requests, "get", srv.get)
    wallet = FakeWallet()
    monkeypatch.setattr(agenthc, "wallet_from_cfg", lambda cfg: wallet)
    sent = []
    import notifications
    monkeypatch.setattr(notifications, "notify", lambda cfg, text: sent.append(text))
    state = {}
    for _ in range(3):
        assert agenthc.poll(_pass_cfg(), state, lambda s: None) == []
    assert len(sent) == 1 and "run out" in sent[0]
    assert wallet.paid == []


def test_low_days_left_notifies_once_per_day(monkeypatch):
    srv = _PassServer(days_left=2)
    monkeypatch.setattr(agenthc.requests, "get", srv.get)
    sent = []
    import notifications
    monkeypatch.setattr(notifications, "notify", lambda cfg, text: sent.append(text))
    state = {}
    agenthc.poll(_pass_cfg(), state, lambda s: None)
    agenthc.poll(_pass_cfg(), state, lambda s: None)
    assert len(sent) == 1 and "2 signal day" in sent[0]


def test_unpaid_invoice_stops_waiting_after_it_expires(monkeypatch):
    srv = _PassServer(status="pending")
    monkeypatch.setattr(agenthc.requests, "get", srv.get)
    state = {"pass_status": {agenthc._token_id(PASS_TOK): {
        "status": "pending", "first_seen": 0, "checked": 0}}}
    agenthc.poll(_pass_cfg(), state, lambda s: None)
    assert state["pass_status"][agenthc._token_id(PASS_TOK)]["status"] == "unpaid"
    assert state.get("pass_notice")  # told to renew instead of waiting forever


def test_renewal_uses_newest_active_token(monkeypatch):
    """Pending renewal that got paid wins over the old token (it carries the
    extended end date) even before the wizard promotes it in config."""
    srv = _PassServer()
    monkeypatch.setattr(agenthc.requests, "get", srv.get)
    cfg = {"source": "agenthc", "feed_pass": {"token": "L402 OLD:paid"},
           "feed_pass_pending": {"token": PASS_TOK}}
    state = {"pass_status": {
        agenthc._token_id("L402 OLD:paid"): {"status": "active", "until_ts": 1e10 - 5}}}
    srv_until = agenthc._iso_ts("2030-01-02T00:00:00+00:00")
    assert srv_until < 1e10 - 5  # scripted server date is earlier: OLD wins here
    agenthc.poll(cfg, state, lambda s: None)
    used = [h["Authorization"] for u, h in srv.calls if u.endswith("day-trade-ideas")]
    assert used == ["L402 OLD:paid"]


def test_buy_pass_unavailable_is_actionable(monkeypatch):
    monkeypatch.setattr(agenthc.requests, "post",
                        lambda *a, **kw: FakeResp({"detail": {}}, status=404))
    with pytest.raises(agenthc.PassUnavailable, match="Advanced"):
        agenthc.buy_pass(5)


def test_buy_pass_sends_extend_token(monkeypatch):
    seen = {}

    def post(url, json=None, timeout=None):
        seen.update(json)
        return FakeResp({"payment_request": "lnbc1", "pass_token": "T"})
    monkeypatch.setattr(agenthc.requests, "post", post)
    agenthc.buy_pass(20, extend_token=PASS_TOK)
    assert seen == {"days": 20, "extend": PASS_TOK}


def test_pass_summary_lines():
    tid = agenthc._token_id(PASS_TOK)
    st = {"pass_status": {tid: {"status": "active", "until_ts": 1e10, "days_left": 7}}}
    assert agenthc.pass_summary(_pass_cfg(), st) == "prepaid pass · 7 signal day(s) left"
    assert "waiting" in agenthc.pass_summary(_pass_cfg(), {})
    assert agenthc.pass_summary({"agenthc_api_key": "k"}, {}) == "Premium API key"


def test_exits_only_pass_keeps_reading_exits_and_warns(monkeypatch):
    """Days used up -> server serves exits only; the agent must KEEP polling
    (or an open position would never see its exit) and tell the owner."""
    srv = _PassServer(status="exits_only", days_left=0)
    orig = srv.get

    def get(url, params=None, headers=None, timeout=None):
        r = orig(url, params, headers, timeout)
        if not url.endswith("/pass"):
            r._payload = {"events": [{"event": "EXITED", "ticker": "SPY",
                                      "expiry": "2026-07-17", "strike": 600,
                                      "type": "C", "occurred_at": "x"}],
                          "pass_days_left": 0, "pass_exits_only": True}
        return r
    monkeypatch.setattr(agenthc.requests, "get", get)
    sent = []
    import notifications
    monkeypatch.setattr(notifications, "notify", lambda cfg, text: sent.append(text))
    state = {}
    events = agenthc.poll(_pass_cfg(), state, lambda s: None)
    assert [e["event"] for e in events] == ["EXITED"]
    assert sent == [agenthc.PASS_USED_UP_TEXT]
    assert "used up" in agenthc.pass_summary(_pass_cfg(), state)


def test_active_pass_preferred_over_exits_only_one(monkeypatch):
    srv = _PassServer()
    monkeypatch.setattr(agenthc.requests, "get", srv.get)
    cfg = {"source": "agenthc", "feed_pass": {"token": "L402 OLD:paid"},
           "feed_pass_pending": {"token": PASS_TOK}}
    state = {"pass_status": {
        agenthc._token_id("L402 OLD:paid"): {"status": "exits_only", "until_ts": 1e10}}}
    agenthc.poll(cfg, state, lambda s: None)
    used = [h["Authorization"] for u, h in srv.calls if u.endswith("day-trade-ideas")]
    assert used == [PASS_TOK]


# ── one charge per market day: prepaid pass + wallet auto-pay ───────────────

def _wallet_cfg(**kw):
    return _pass_cfg(lnbits_url="https://lnbits.example", lnbits_admin_key="x",
                     day_pass_recurring=True, **kw)


class _PassAndWalletServer(_PassServer):
    """Pass status + a feed that 402s requests without a valid token."""

    def __init__(self, status="active", pass_status_error=False, **kw):
        super().__init__(status=status, **kw)
        self.pass_status_error = pass_status_error

    def get(self, url, params=None, headers=None, timeout=None):
        if url.endswith("/pass") and self.pass_status_error:
            self.calls.append((url, dict(headers or {})))
            raise ConnectionError("pass status unavailable")
        if not url.endswith("/pass") and not (headers or {}).get("Authorization"):
            self.calls.append((url, dict(headers or {})))
            return FakeResp({"payment": {"payment_request": _invoice(12_000),
                                         "macaroon": "MAC",
                                         "amount_sats": 12_000}}, status=402)
        return super().get(url, params, headers, timeout)


def test_pass_status_check_failure_never_pays_the_wallet(monkeypatch):
    """A failed pass-status check must not fall through to the wallet: the
    pass is probably fine, and buying would charge the owner twice today."""
    srv = _PassAndWalletServer(pass_status_error=True)
    monkeypatch.setattr(agenthc.requests, "get", srv.get)
    wallet = FakeWallet()
    monkeypatch.setattr(agenthc, "wallet_from_cfg", lambda cfg: wallet)
    state = {}
    for _ in range(3):
        assert agenthc.poll(_wallet_cfg(), state, lambda s: None) == []
    assert wallet.paid == []
    assert not any(u.endswith("day-trade-ideas") for u, _ in srv.calls)


def test_wallet_never_buys_on_a_day_the_pass_was_charged(monkeypatch):
    """Pass charged today, then ended (e.g. superseded/expired): the wallet
    must wait for tomorrow instead of paying for today a second time."""
    srv = _PassAndWalletServer(status="expired")
    monkeypatch.setattr(agenthc.requests, "get", srv.get)
    wallet = FakeWallet()
    monkeypatch.setattr(agenthc, "wallet_from_cfg", lambda cfg: wallet)
    state = {"pass_charged_day": "2026-07-15"}  # the pinned 'today'
    assert agenthc.poll(_wallet_cfg(), state, lambda s: None) == []
    assert wallet.paid == []
    assert state.get("pass_day_skip_notified") == "2026-07-15"
    # next market day the wallet may buy again
    state["pass_charged_day"] = "2026-07-14"
    agenthc.poll(_wallet_cfg(), state, lambda s: None)
    assert wallet.paid == [_invoice(12_000)]


def test_ended_pass_still_falls_back_to_wallet(monkeypatch):
    """Regression: a pass that really ended (and paid nothing today) still
    hands over to the recurring wallet day-pass."""
    srv = _PassAndWalletServer(status="expired")
    monkeypatch.setattr(agenthc.requests, "get", srv.get)
    wallet = FakeWallet()
    monkeypatch.setattr(agenthc, "wallet_from_cfg", lambda cfg: wallet)
    state = {}
    events = agenthc.poll(_wallet_cfg(), state, lambda s: None)
    assert wallet.paid == [_invoice(12_000)]
    assert len(events) == 1


def test_running_wallet_daypass_is_used_before_the_prepaid_pass(monkeypatch):
    """A day-pass the wallet already paid for is read first; switching to
    the prepaid pass now would charge a pass day on top of it."""
    srv = _PassServer()
    monkeypatch.setattr(agenthc.requests, "get", srv.get)
    state = {"l402": {"token": "L402 MAC:pre", "expires_at": time.time() + 3600}}
    events = agenthc.poll(_wallet_cfg(), state, lambda s: None)
    assert len(events) == 1
    used = [h.get("Authorization") for u, h in srv.calls if u.endswith("day-trade-ideas")]
    assert used == ["L402 MAC:pre"]
    assert not any(u.endswith("/pass") for u, _ in srv.calls)
    # once the day-pass runs out, the prepaid pass takes over
    state["l402"]["expires_at"] = time.time() - 1
    agenthc.poll(_wallet_cfg(), state, lambda s: None)
    used = [h.get("Authorization") for u, h in srv.calls if u.endswith("day-trade-ideas")]
    assert used[-1] == PASS_TOK


def test_pass_charge_today_is_remembered(monkeypatch):
    srv = _PassServer()
    orig = srv.get

    def get(url, params=None, headers=None, timeout=None):
        r = orig(url, params, headers, timeout)
        if not url.endswith("/pass"):
            r._payload = {**r._payload, "pass_charged_today": True}
        return r
    monkeypatch.setattr(agenthc.requests, "get", get)
    state = {"paid_days": ["2026-07-14"]}
    agenthc.poll(_wallet_cfg(), state, lambda s: None)
    assert state["pass_charged_day"] == "2026-07-15"
    assert state["paid_days"] == ["2026-07-14", "2026-07-15"]
    agenthc.poll(_wallet_cfg(), state, lambda s: None)  # same day: no duplicate
    assert state["paid_days"] == ["2026-07-14", "2026-07-15"]


def test_refused_wallet_daypass_falls_back_to_prepaid_pass(monkeypatch):
    """Server refuses the running day-pass (402): read with the prepaid pass
    — don't sit idle until the token's local expiry, don't buy again."""
    srv = _PassServer()
    orig = srv.get

    def get(url, params=None, headers=None, timeout=None):
        if (headers or {}).get("Authorization") == "L402 MAC:pre":
            srv.calls.append((url, dict(headers)))
            return FakeResp({"payment": {"payment_request": _invoice(12_000),
                                         "macaroon": "MAC",
                                         "amount_sats": 12_000}}, status=402)
        return orig(url, params, headers, timeout)
    monkeypatch.setattr(agenthc.requests, "get", get)
    wallet = FakeWallet()
    monkeypatch.setattr(agenthc, "wallet_from_cfg", lambda cfg: wallet)
    state = {"l402": {"token": "L402 MAC:pre", "expires_at": time.time() + 3600}}
    events = agenthc.poll(_wallet_cfg(), state, lambda s: None)
    assert len(events) == 1
    assert wallet.paid == []
    assert "l402" not in state
    used = [h.get("Authorization") for u, h in srv.calls if u.endswith("day-trade-ideas")]
    assert used == ["L402 MAC:pre", PASS_TOK]


def test_wallet_buys_at_most_one_daypass_a_day_with_a_pass_set_up(monkeypatch):
    """Pass ended, wallet already paid once today (e.g. its token was lost
    after paying): no second wallet purchase the same day."""
    srv = _PassAndWalletServer(status="expired")
    monkeypatch.setattr(agenthc.requests, "get", srv.get)
    wallet = FakeWallet()
    monkeypatch.setattr(agenthc, "wallet_from_cfg", lambda cfg: wallet)
    state = {"autopay_spent": {"day": "2026-07-15", "sats": 12_000, "count": 1}}
    assert agenthc.poll(_wallet_cfg(), state, lambda s: None) == []
    assert wallet.paid == []


# ── exit access: can the agent still get EXIT signals? (no-signal sell) ─────

def _exit_flag():
    return agenthc.had_exit_access(), agenthc.exit_loss_reason()


def test_exits_only_read_has_exit_access_but_no_entry_access(monkeypatch):
    srv = _PassServer(status="exits_only", days_left=0)
    orig = srv.get

    def get(url, params=None, headers=None, timeout=None):
        r = orig(url, params, headers, timeout)
        if not url.endswith("/pass"):
            r._payload = {"events": [], "pass_days_left": 0, "pass_exits_only": True}
        return r
    monkeypatch.setattr(agenthc.requests, "get", get)
    agenthc.poll(_pass_cfg(), {}, lambda s: None)
    assert agenthc.had_access() is False
    assert _exit_flag() == (True, "")


def test_api_key_and_active_pass_reads_have_exit_access(monkeypatch):
    monkeypatch.setattr(agenthc.requests, "get",
                        lambda *a, **kw: FakeResp({"events": []}))
    agenthc.poll({"agenthc_api_key": "k"}, {}, lambda s: None)
    assert _exit_flag() == (True, "")
    srv = _PassServer()
    monkeypatch.setattr(agenthc.requests, "get", srv.get)
    agenthc.poll(_pass_cfg(), {}, lambda s: None)
    assert _exit_flag() == (True, "")


def test_pass_unchecked_has_no_exit_access(monkeypatch):
    srv = _PassAndWalletServer(pass_status_error=True)
    monkeypatch.setattr(agenthc.requests, "get", srv.get)
    assert agenthc.poll(_wallet_cfg(), {}, lambda s: None) == []
    assert _exit_flag() == (False, "pass_unchecked")


def test_unpaid_pass_has_no_exit_access(monkeypatch):
    monkeypatch.setattr(agenthc.requests, "get", _PassServer(status="pending").get)
    assert agenthc.poll(_pass_cfg(), {}, lambda s: None) == []
    assert _exit_flag() == (False, "pass_unpaid")


def test_ended_pass_has_no_exit_access(monkeypatch):
    monkeypatch.setattr(agenthc.requests, "get", _PassServer(status="expired").get)
    import notifications
    monkeypatch.setattr(notifications, "notify", lambda cfg, text: None)
    assert agenthc.poll(_pass_cfg(), {}, lambda s: None) == []
    assert _exit_flag() == (False, "pass_ended")


def test_no_day_pass_has_no_exit_access(monkeypatch):
    monkeypatch.setattr(agenthc.requests, "get", lambda *a, **kw: (_ for _ in ()).throw(
        AssertionError("no request without a pass")))
    assert agenthc.poll({}, {}, lambda s: None) == []
    assert _exit_flag() == (False, "no_day_pass")
    # a 402 the wallet may not pay (recurring off): the same
    resp = FakeResp({"payment": {"payment_request": _invoice(9500),
                                 "macaroon": "MAC", "amount_sats": 9500}}, status=402)
    monkeypatch.setattr(agenthc.requests, "get", lambda *a, **kw: resp)
    state = {"l402": {"token": "L402 m:p", "expires_at": time.time() + 3600,
                      "paid_day": "2026-07-14"}}
    assert agenthc.poll({}, state, lambda s: None) == []
    assert _exit_flag() == (False, "no_day_pass")


def test_refused_day_pass_has_no_exit_access(monkeypatch):
    resp = FakeResp({"detail": "payment required"}, status=402)
    monkeypatch.setattr(agenthc.requests, "get", lambda *a, **kw: resp)
    import notifications
    monkeypatch.setattr(notifications, "notify", lambda cfg, text: None)
    state = {"l402": {"token": "L402 m:p", "expires_at": time.time() + 3600,
                      "paid_day": "2026-07-15"}}
    assert agenthc.poll({}, state, lambda s: None) == []
    assert _exit_flag() == (False, "day_pass_refused")
    # offered again only after a pause: that skipped read is no access too
    assert agenthc.poll({}, state, lambda s: None) == []
    assert _exit_flag() == (False, "day_pass_refused")


def test_feed_not_live_has_no_exit_access(monkeypatch):
    resp = FakeResp({"detail": {"error": "feed_not_live"}}, status=403)
    monkeypatch.setattr(agenthc.requests, "get", lambda *a, **kw: resp)
    assert agenthc.poll({"agenthc_api_key": "k"}, {}) == []
    assert _exit_flag() == (False, "feed_not_live")
    monkeypatch.setattr(agenthc.requests, "get", _PassServer().get)
    agenthc.poll(_pass_cfg(), {}, lambda s: None)
    monkeypatch.setattr(agenthc.requests, "get", lambda *a, **kw: resp)
    tid = agenthc._token_id(PASS_TOK)
    state = {"pass_status": {tid: {"status": "active", "until_ts": 9e12}}}
    assert agenthc.poll(_pass_cfg(), state, lambda s: None) == []
    assert _exit_flag() == (False, "feed_not_live")


# ── pass notices: new trades only, one note per condition ───────────────────

def test_pass_notices_speak_only_about_new_trades():
    texts = (agenthc.PASS_LOW_TEXT.format(n=2), agenthc.PASS_LAST_DAY_TEXT,
             agenthc.PASS_USED_UP_TEXT, agenthc.PASS_RUN_OUT_TEXT)
    assert texts == (
        "Your signal pass has 2 signal day(s) left. Buy more in the dashboard "
        "(Settings → Signal source); unused days carry over.",
        "Today is the last day your signal pass covers new trades. Buy more "
        "signal days (dashboard → Settings → Signal source) to keep getting new "
        "trades. Exit signals for trades already open keep arriving.",
        "Your signal pass is used up: no new trades until you buy more signal "
        "days (dashboard → Settings → Signal source). Exit signals for the "
        "trades your agent already opened still arrive.",
        "Your signal pass has run out: no new trades until you buy more signal "
        "days (dashboard → Settings → Signal source).")
    for t in texts:
        assert "position" not in t and "watch" not in t


def _feed_body(srv, **body):
    orig = srv.get

    def get(url, params=None, headers=None, timeout=None):
        r = orig(url, params, headers, timeout)
        if not url.endswith("/pass"):
            r._payload = {"events": [], **body}
        return r
    return get


def test_run_out_notice_is_the_new_text(monkeypatch):
    monkeypatch.setattr(agenthc.requests, "get", _PassServer(status="expired").get)
    sent = []
    import notifications
    monkeypatch.setattr(notifications, "notify", lambda cfg, text: sent.append(text))
    state = {}
    agenthc.poll(_pass_cfg(), state, lambda s: None)
    assert sent == [agenthc.PASS_RUN_OUT_TEXT]
    assert state["pass_notice"] == agenthc.PASS_RUN_OUT_TEXT


def test_last_day_notice_once_per_day(monkeypatch):
    srv = _PassServer(days_left=0)
    monkeypatch.setattr(agenthc.requests, "get", _feed_body(
        srv, pass_days_left=0, pass_charged_today=True))
    sent = []
    import notifications
    monkeypatch.setattr(notifications, "notify", lambda cfg, text: sent.append(text))
    state = {}
    for _ in range(3):
        agenthc.poll(_pass_cfg(), state, lambda s: None)
    assert sent == [agenthc.PASS_LAST_DAY_TEXT]
    assert state["pass_last_day_notified"] == time.strftime("%Y-%m-%d")
    # not charged today with 0 left: the "days left" notice, as before
    sent.clear()
    monkeypatch.setattr(agenthc.requests, "get", _feed_body(srv, pass_days_left=0))
    agenthc.poll(_pass_cfg(), {}, lambda s: None)
    assert sent == [agenthc.PASS_LOW_TEXT.format(n=0)]


def test_pass_notice_is_cleared_after_a_read_with_entry_access(monkeypatch):
    srv = _PassServer()
    monkeypatch.setattr(agenthc.requests, "get", srv.get)
    state = {"pass_notice": agenthc.PASS_RUN_OUT_TEXT}
    agenthc.poll(_pass_cfg(), state, lambda s: None)
    assert "pass_notice" not in state
    # with an API key or a wallet day-pass too
    monkeypatch.setattr(agenthc.requests, "get",
                        lambda *a, **kw: FakeResp({"events": []}))
    state = {"pass_notice": agenthc.PASS_RUN_OUT_TEXT}
    agenthc.poll({"agenthc_api_key": "k"}, state, lambda s: None)
    assert "pass_notice" not in state
    # a wallet day-pass alert stays until that problem ends
    state = {"pass_notice": "AgentHC refused the day-pass…",
             "daypass_alert": "daypass_refused_notified"}
    agenthc.poll({"agenthc_api_key": "k"}, state, lambda s: None)
    assert state["pass_notice"] == "AgentHC refused the day-pass…"


def test_exits_only_read_keeps_the_notice_and_the_exits_end(monkeypatch):
    srv = _PassServer(status="exits_only", days_left=0)
    monkeypatch.setattr(agenthc.requests, "get", _feed_body(
        srv, pass_days_left=0, pass_exits_only=True,
        pass_exits_until="2026-07-17T00:00:00+00:00"))
    import notifications
    monkeypatch.setattr(notifications, "notify", lambda cfg, text: None)
    state = {}
    agenthc.poll(_pass_cfg(), state, lambda s: None)
    assert state["pass_notice"] == agenthc.PASS_USED_UP_TEXT
    rec = state["pass_status"][agenthc._token_id(PASS_TOK)]
    assert rec["exits_until"] == agenthc._iso_ts("2026-07-17T00:00:00+00:00")


def test_day_pass_alerts_end_at_no_new_trades(monkeypatch):
    resp = FakeResp({"detail": "payment required"}, status=402)
    monkeypatch.setattr(agenthc.requests, "get", lambda *a, **kw: resp)
    sent = []
    import notifications
    monkeypatch.setattr(notifications, "notify", lambda cfg, text: sent.append(text))
    state = {"l402": {"token": "L402 m:p", "expires_at": time.time() + 3600,
                      "paid_day": "2026-07-15"}}
    agenthc.poll({}, state, lambda s: None)
    (text,) = sent
    assert "the agent gets no new trades" in text
    assert "position" not in text and "watch" not in text
    assert not hasattr(agenthc, "_NO_SIGNALS")


# ── an ended wallet day-pass is offered for exits while a position is open ──

ENDED = "L402 MACOLD:pre"


def _ended_state(real=True):
    row = ({"option_id": "opt-1", "qty": 1, "live": True} if real
           else {"option_id": "dry", "qty": 1, "dry": True})
    return {"l402": {"token": ENDED, "expires_at": time.time() - 60,
                     "paid_day": "2026-07-14"},
            "positions": {"SPY|2026-07-17|600.0|C": row}}


class _EndedTokenServer:
    def __init__(self, status=200):
        self.status, self.calls = status, []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append(dict(headers or {}))
        if self.status != 200:
            return FakeResp({"payment": {"payment_request": _invoice(9500),
                                         "macaroon": "M"}}, status=self.status)
        return FakeResp({"events": [
            _feed_event(event="ENTERED", occurred_at="a"),
            _feed_event(event="EXITED", occurred_at="b")],
            "pass_exits_only": True})


def test_ended_day_pass_still_reads_exits_for_a_real_position(monkeypatch):
    srv = _EndedTokenServer()
    monkeypatch.setattr(agenthc.requests, "get", srv.get)
    state = _ended_state()
    events = agenthc.poll({}, state, lambda s: None)
    assert [e["event"] for e in events] == ["EXITED"]   # never an entry
    assert srv.calls == [{"Authorization": ENDED}]
    assert agenthc.had_access() is False
    assert _exit_flag() == (True, "")
    assert "exits_done" not in state["l402"]


@pytest.mark.parametrize("status", [401, 402])
def test_ended_day_pass_refused_is_asked_once_only(monkeypatch, status):
    srv = _EndedTokenServer(status=status)
    monkeypatch.setattr(agenthc.requests, "get", srv.get)
    state = _ended_state()
    saved = []
    for _ in range(3):
        assert agenthc.poll({}, state, saved.append) == []
        assert _exit_flag() == (False, "no_day_pass")
    assert len(srv.calls) == 1
    assert state["l402"]["exits_done"] is True and saved


def test_ended_day_pass_other_errors_mean_no_exits(monkeypatch):
    srv = _EndedTokenServer(status=503)
    monkeypatch.setattr(agenthc.requests, "get", srv.get)
    state = _ended_state()
    assert agenthc.poll({}, state, lambda s: None) == []
    assert _exit_flag() == (False, "no_day_pass")
    assert "exits_done" not in state["l402"]       # asked again next time
    agenthc.poll({}, state, lambda s: None)
    assert len(srv.calls) == 2


def test_ended_day_pass_without_a_real_position_sends_nothing(monkeypatch):
    srv = _EndedTokenServer()
    monkeypatch.setattr(agenthc.requests, "get", srv.get)
    for state in (_ended_state(real=False), {**_ended_state(), "positions": {}}):
        assert agenthc.poll({}, state, lambda s: None) == []
    assert srv.calls == []


# ── AT-5: wallet auto-pay takes over for entries during exits-only days ─────

class _ExitsOnlyAndWalletServer(_PassAndWalletServer):
    """The prepaid pass is used up (exits only); a request without a token
    gets a day-pass invoice; the wallet's day-pass reads everything."""

    def __init__(self):
        super().__init__(status="exits_only", days_left=0)

    def get(self, url, params=None, headers=None, timeout=None):
        r = super().get(url, params, headers, timeout)
        if (not url.endswith("/pass")
                and (headers or {}).get("Authorization") == PASS_TOK):
            r._payload = {"events": [{"event": "EXITED", "ticker": "SPY",
                                      "expiry": "2026-07-17", "strike": 600,
                                      "type": "C", "occurred_at": "x"}],
                          "pass_days_left": 0, "pass_exits_only": True}
        return r


def _feed_auths(srv):
    return [h.get("Authorization") for u, h in srv.calls
            if u.endswith("day-trade-ideas")]


def test_exits_only_pass_and_recurring_on_buys_a_day_pass_for_entries(monkeypatch):
    srv = _ExitsOnlyAndWalletServer()
    monkeypatch.setattr(agenthc.requests, "get", srv.get)
    wallet = FakeWallet()
    monkeypatch.setattr(agenthc, "wallet_from_cfg", lambda cfg: wallet)
    import notifications
    monkeypatch.setattr(notifications, "notify", lambda cfg, text: None)
    state = {"pass_status": {agenthc._token_id(PASS_TOK): {
        "status": "exits_only", "until_ts": 9e12, "days_left": 0}}}
    events = agenthc.poll(_wallet_cfg(), state, lambda s: None)
    assert wallet.paid == [_invoice(12_000)]
    assert _feed_auths(srv) == [None, "L402 MAC:preimage-abc"]
    assert [e["event"] for e in events] == ["ENTERED"]
    assert agenthc.had_access() is True
    # the next polls read with the day-pass; no second purchase
    agenthc.poll(_wallet_cfg(), state, lambda s: None)
    assert len(wallet.paid) == 1
    assert _feed_auths(srv)[-1] == "L402 MAC:preimage-abc"


def test_exits_only_pass_still_reads_exits_when_the_purchase_fails(monkeypatch):
    srv = _ExitsOnlyAndWalletServer()
    monkeypatch.setattr(agenthc.requests, "get", srv.get)

    class Broke(FakeWallet):
        def pay_invoice(self, invoice):
            import lightning_wallet
            raise lightning_wallet.PaymentRefused("Insufficient balance")

        def payment_status(self, h):
            return ("not_found", "")
    monkeypatch.setattr(agenthc, "wallet_from_cfg", lambda cfg: Broke())
    import notifications
    monkeypatch.setattr(notifications, "notify", lambda cfg, text: None)
    state = {"pass_status": {agenthc._token_id(PASS_TOK): {
        "status": "exits_only", "until_ts": 9e12, "days_left": 0}}}
    events = agenthc.poll(_wallet_cfg(), state, lambda s: None)
    assert [e["event"] for e in events] == ["EXITED"]
    assert _feed_auths(srv) == [None, PASS_TOK]
    assert _exit_flag() == (True, "") and agenthc.had_access() is False
    # the retry waits: the next poll only reads the exits
    agenthc.poll(_wallet_cfg(), state, lambda s: None)
    assert _feed_auths(srv)[-1] == PASS_TOK and _feed_auths(srv).count(None) == 1


def test_exits_only_pass_with_recurring_off_is_unchanged(monkeypatch):
    srv = _ExitsOnlyAndWalletServer()
    monkeypatch.setattr(agenthc.requests, "get", srv.get)
    wallet = FakeWallet()
    monkeypatch.setattr(agenthc, "wallet_from_cfg", lambda cfg: wallet)
    import notifications
    monkeypatch.setattr(notifications, "notify", lambda cfg, text: None)
    state = {"pass_status": {agenthc._token_id(PASS_TOK): {
        "status": "exits_only", "until_ts": 9e12, "days_left": 0}}}
    cfg = {**_wallet_cfg(), "day_pass_recurring": False}
    events = agenthc.poll(cfg, state, lambda s: None)
    assert [e["event"] for e in events] == ["EXITED"]
    assert _feed_auths(srv) == [PASS_TOK] and wallet.paid == []


def test_no_day_pass_on_a_day_the_prepaid_pass_was_charged(monkeypatch):
    srv = _ExitsOnlyAndWalletServer()
    monkeypatch.setattr(agenthc.requests, "get", srv.get)
    wallet = FakeWallet()
    monkeypatch.setattr(agenthc, "wallet_from_cfg", lambda cfg: wallet)
    import notifications
    monkeypatch.setattr(notifications, "notify", lambda cfg, text: None)
    state = {"pass_charged_day": "2026-07-15",
             "pass_status": {agenthc._token_id(PASS_TOK): {
                 "status": "exits_only", "until_ts": 9e12, "days_left": 0}}}
    events = agenthc.poll(_wallet_cfg(), state, lambda s: None)
    assert [e["event"] for e in events] == ["EXITED"]
    assert _feed_auths(srv) == [PASS_TOK] and wallet.paid == []
