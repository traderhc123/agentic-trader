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
            return FakeResp({"payment": {"payment_request": "lnbc1...",
                                         "macaroon": "MAC",
                                         "amount_sats": 9500}}, status=402)
        return FakeResp({"events": [_feed_event()]})

    monkeypatch.setattr(agenthc.requests, "get", fake_get)
    state = {}
    events = agenthc.poll({"day_pass_recurring": True}, state,
                          save_state=lambda s: None)
    assert wallet.paid == ["lnbc1..."]
    assert state["l402"]["token"] == "L402 MAC:preimage-abc"
    assert calls[1]["Authorization"] == "L402 MAC:preimage-abc"
    assert len(events) == 1


def test_agenthc_402_respects_autopay_cap(monkeypatch):
    monkeypatch.setattr(agenthc, "wallet_from_cfg", lambda cfg: FakeWallet())
    resp = FakeResp({"payment": {"payment_request": "lnbc1...",
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
    resp = FakeResp({"payment": {"payment_request": "lnbc1...",
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
            return FakeResp({"payment": {"payment_request": "lnbc1...",
                                         "macaroon": "MAC",
                                         "amount_sats": 9500}}, status=402)
        return FakeResp({"events": [_feed_event()]})

    monkeypatch.setattr(agenthc.requests, "get", fake_get)
    events = agenthc.poll({"day_pass_recurring": True}, {}, lambda s: None)
    assert wallet.paid == ["lnbc1..."]
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
    assert state.get("weekend_skip_notified")


def test_agenthc_live_token_still_polls(monkeypatch):
    """A cached unexpired day-pass token must keep polling normally even with
    recurring OFF — the guard only blocks access-impossible states."""
    monkeypatch.setattr(agenthc.requests, "get",
                        lambda *a, **kw: FakeResp({"events": [_feed_event()]}))
    state = {"l402": {"token": "L402 m:p", "expires_at": time.time() + 3600}}
    events = agenthc.poll({}, state, save_state=lambda s: None)
    assert len(events) == 1


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
    resp = FakeResp({"payment": {"payment_request": "lnbc1...",
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
    assert wallet.paid == ["lnbc1..."]
    assert state["autopay_spent"]["sats"] == 16_000


def test_agenthc_daily_autopay_cap_is_configurable(monkeypatch):
    wallet = FakeWallet()
    monkeypatch.setattr(agenthc, "wallet_from_cfg", lambda cfg: wallet)
    resp = FakeResp({"payment": {"payment_request": "lnbc1...",
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
    assert len(sent) == 1 and "EXIT signals" in sent[0]
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
