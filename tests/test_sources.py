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
