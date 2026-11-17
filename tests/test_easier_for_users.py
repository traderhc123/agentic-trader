"""Help panel, support report, broken-proxy bypass, 1-day pass handling.
No network: every request is stubbed."""

import pytest
import requests

import net_check as nc
import support
import webui
from sources import agenthc_day_trade_ideas as hc


class _R:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body or {}
        self.text = str(body)

    def json(self):
        return self._body


@pytest.fixture
def fresh_http(monkeypatch):
    h = nc._Http()
    monkeypatch.setattr(nc, "http", h)
    monkeypatch.setattr(hc, "_http", h)
    return h


# ── broken proxy bypass ──

def test_proxy_for_hides_credentials(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://user:secret@proxy.local:3128")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    p = nc.proxy_for("https://api.traderhc.com")
    assert p == "http://proxy.local:3128" and "secret" not in p


def test_broken_proxy_is_bypassed_and_remembered(fresh_http, monkeypatch, capsys):
    """REGRESSION (user, 2026-10-15): SSL WRONG_VERSION_NUMBER on every
    request — the classic wrong HTTPS_PROXY entry."""
    monkeypatch.setattr(nc, "proxy_for", lambda url: "https://proxy.local:3128")

    def via_proxy(url, **kw):
        raise requests.exceptions.SSLError("[SSL: WRONG_VERSION_NUMBER] wrong version number")
    monkeypatch.setattr(requests, "get", via_proxy)
    direct = []

    class _S:
        trust_env = True

        def get(self, url, **kw):
            direct.append(url)
            return _R(200, {"ok": True})
    monkeypatch.setattr(requests, "Session", _S)
    assert fresh_http.get("https://api.traderhc.com/x").status_code == 200
    assert fresh_http.bypassed == "https://proxy.local:3128"
    assert "connects directly" in capsys.readouterr().out
    fresh_http.get("https://api.traderhc.com/y")  # straight to direct now
    assert direct == ["https://api.traderhc.com/x", "https://api.traderhc.com/y"]


def test_no_proxy_means_errors_still_raise(fresh_http, monkeypatch):
    monkeypatch.setattr(nc, "proxy_for", lambda url: "")

    def boom(url, **kw):
        raise requests.exceptions.SSLError("handshake failed")
    monkeypatch.setattr(requests, "get", boom)
    with pytest.raises(requests.exceptions.SSLError):
        fresh_http.get("https://api.traderhc.com/x")
    assert not fresh_http.bypassed


def test_direct_retry_failing_keeps_the_original_error(fresh_http, monkeypatch):
    monkeypatch.setattr(nc, "proxy_for", lambda url: "http://p:1")

    def boom(url, **kw):
        raise requests.exceptions.ProxyError("proxy refused")
    monkeypatch.setattr(requests, "post", boom)

    class _S:
        trust_env = True

        def post(self, url, **kw):
            raise requests.exceptions.ConnectionError("also down")
    monkeypatch.setattr(requests, "Session", _S)
    with pytest.raises(requests.exceptions.ProxyError):
        fresh_http.post("https://api.traderhc.com/x")
    assert not fresh_http.bypassed


def test_failed_diagnosis_names_the_proxy(monkeypatch):
    monkeypatch.setattr(nc, "_resolve", lambda host: ["167.71.174.199"])
    monkeypatch.setattr(nc, "_public_dns", lambda host, timeout=5: None)
    monkeypatch.setattr(nc, "_connect", lambda host, ip, t: nc._result(
        False, "not_tls", "a web page answered", "allow-list it"))
    monkeypatch.setattr(nc, "proxy_for", lambda url: "https://proxy.local:3128")
    d = nc.diagnose("api.traderhc.com")
    assert d["proxy"] == "https://proxy.local:3128"
    assert d["fix"].startswith("This computer is set to use a proxy (https://proxy.local:3128)")


# ── 1-day pass ──

def test_unsold_pass_size_is_a_friendly_message(fresh_http, monkeypatch):
    monkeypatch.setattr(requests, "post", lambda url, **kw: _R(400, {
        "detail": {"error": "invalid_days", "options": [5]}}))
    with pytest.raises(hc.PassSizeUnavailable) as exc:
        hc.buy_pass(1)
    assert "1-day passes aren't on sale right now — pick 5 signal days." in str(exc.value)


def test_refilling_pass_size_is_a_friendly_message(fresh_http, monkeypatch):
    monkeypatch.setattr(requests, "post", lambda url, **kw: _R(503, {
        "detail": {"error": "pass_size_temporarily_unavailable"}}))
    with pytest.raises(hc.PassSizeRefilling) as exc:
        hc.buy_pass(5)
    assert "briefly unavailable while our node refills" in str(exc.value)
    assert isinstance(exc.value, hc.PassSizeUnavailable)


def test_other_503_stays_a_generic_failure(fresh_http, monkeypatch):
    monkeypatch.setattr(requests, "post", lambda url, **kw: _R(503, {
        "detail": "maintenance"}))
    with pytest.raises(RuntimeError) as exc:
        hc.buy_pass(5)
    assert not isinstance(exc.value, hc.PassSizeUnavailable)


def test_wizard_refreshes_sizes_when_one_is_off_sale():
    html = webui._WIZARD_HTML
    assert "r.refresh_sizes" in html and "passSizes();" in html
    assert "signal day (try it)" not in html


def test_wizard_offers_one_day_and_payment_help():
    html = webui._WIZARD_HTML
    assert 'buyPass(1)' in html and "1 signal day" in html
    assert 'id="pass-help"' in html and "Cash App" in html and 'id="pass-need"' in html


# ── support report ──

def test_scrub_removes_secrets():
    raw = ("token L402 AgV0aGlzaXNhbWFjYXJvb24:paid key sk-ant-abc123456789 "
           "inv lnbc612600n1p4vlm50pp5dp0rksxgqqfzyxvjyq7 hash "
           "8068408cd442c360dc36ce6e9783c860ef012a5324e58897937725c3869cc84f "
           "url http://127.0.0.1:8721/dash?k=SECRETUITOKEN proxy http://u:pw@h:1")
    out = support.scrub(raw)
    for secret in ("AgV0aGlz", "sk-ant-abc", "lnbc6126", "8068408cd4",
                   "SECRETUITOKEN", "u:pw@"):
        assert secret not in out, secret
    assert "L402 [token]" in out and "[invoice]" in out


def test_report_has_every_section_and_no_secrets(monkeypatch, tmp_path):
    monkeypatch.setattr(support, "connection_check",
                        lambda host: ["AgentHC: OK — connected", "GitHub (updates): OK"])
    monkeypatch.setattr(nc, "proxy_for", lambda url: "")
    support.RECENT_ERRORS.clear()
    support.note_error("heartbeat error: HTTPSConnectionPool SSL WRONG_VERSION_NUMBER "
                       "with L402 abcdefabcdefabcdefabcdefabcdefabcdef:paid")
    cfg = {"source": "agenthc", "broker": "alpaca", "dry_run": True,
           "feed_pass": {"token": "L402 SECRETMAC:paid"},
           "lnbits_admin_key": "f" * 32, "alpaca_secret": "s" * 40}

    def doctor():
        return [{"label": "Feed access", "ok": True,
                 "detail": "prepaid pass · 4 signal day(s) left", "who": "agent"}], cfg
    text = support.report(str(tmp_path), doctor, cfg, "api.traderhc.com")
    for section in ("version:", "setup:", "checks:", "connection:", "recent errors:"):
        assert section in text
    assert "prepaid pass" in text and "WRONG_VERSION_NUMBER" in text
    assert "SECRETMAC" not in text and "f" * 32 not in text and "s" * 40 not in text
    assert "abcdefabcdef" not in text


def test_dashboard_has_help_panel_and_update_badge():
    html = webui._DASH_HTML
    for needle in ('id="help"', "helpNet()", "helpReport()", 'id="upd-badge"',
                   "/api/help/report"):
        assert needle in html


def test_report_says_none_when_no_feed_access(monkeypatch, tmp_path):
    monkeypatch.setattr(support, "connection_check", lambda host: [])
    monkeypatch.setattr(nc, "proxy_for", lambda url: "")
    text = support.report(str(tmp_path), lambda: ([], {}), {}, "api.traderhc.com")
    assert "feed access: none" in text


def test_pass_options_reads_the_servers_sizes(fresh_http, monkeypatch):
    monkeypatch.setattr(requests, "post", lambda url, **kw: _R(400, {
        "detail": {"error": "invalid_days", "options": [5, 1]}}))
    assert hc.pass_options() == [1, 5]


def test_pass_options_empty_list_means_all_paused(fresh_http, monkeypatch):
    monkeypatch.setattr(requests, "post", lambda url, **kw: _R(400, {
        "detail": {"error": "invalid_days", "options": [], "paused": [1, 5]}}))
    assert hc.pass_options() == []


def test_pass_offer_reads_the_servers_prices(fresh_http, monkeypatch):
    monkeypatch.setattr(requests, "post", lambda url, **kw: _R(400, {
        "detail": {"error": "invalid_days", "options": [1, 5], "paused": [],
                   "prices_usd": {"1": 50.0, "5": 225.0},
                   "prices_sats": {"1": 55_600, "5": 250_000}}}))
    assert hc.pass_offer() == {"options": [1, 5],
                               "prices_usd": {1: 50.0, 5: 225.0},
                               "prices_sats": {1: 55_600, 5: 250_000}}


def test_pass_offer_without_prices_from_an_older_server(fresh_http, monkeypatch):
    monkeypatch.setattr(requests, "post", lambda url, **kw: _R(400, {
        "detail": {"error": "invalid_days", "options": [1, 5]}}))
    assert hc.pass_offer() == {"options": [1, 5], "prices_usd": None,
                               "prices_sats": None}
    assert hc.PASS_PRICES_USD == {1: 50.0, 5: 225.0}


def test_pass_offer_drops_unusable_prices(fresh_http, monkeypatch):
    monkeypatch.setattr(requests, "post", lambda url, **kw: _R(400, {
        "detail": {"error": "invalid_days", "options": [1, 5],
                   "prices_usd": {"1": "x", "5": 225, "0": 9, "7": -1},
                   "prices_sats": "nope"}}))
    out = hc.pass_offer()
    assert out["prices_usd"] == {5: 225.0} and out["prices_sats"] is None


def test_pass_options_unknown_when_server_unreachable(fresh_http, monkeypatch):
    monkeypatch.setattr(nc, "proxy_for", lambda url: "")

    def boom(url, **kw):
        raise requests.exceptions.ConnectionError("down")
    monkeypatch.setattr(requests, "post", boom)
    assert hc.pass_options() is None


def test_wizard_hides_pass_sizes_not_on_sale():
    html = webui._WIZARD_HTML
    assert "passSizes()" in html and "/api/pass/options" in html
    assert "Array.isArray(r.options)" in html          # [] hides every card
    assert "poll(passSizes" in html                    # sizes come back on their own
    assert 'id="pass-sizes-note"' in html


def test_wizard_sells_one_day_and_five_day_passes_only():
    html = webui._WIZARD_HTML
    assert 'buyPass(1)' in html and 'buyPass(5)' in html
    assert 'buyPass(20)' not in html and 'buyPass(60)' not in html
    # built-in prices until the server's arrive; every price in the copy is
    # a [data-pass-usd] the server's prices_usd rewrites
    assert '≈ <span data-pass-usd="1">$50</span>' in html
    assert '≈ <span data-pass-usd="5">$225</span>' in html and "Save $25" in html
    assert "passPrices(r.prices_usd)" in html
    assert "~$10" not in html and "~$50" not in html   # no hard-coded day price left


# ── pass prices: the server's, else the built-in fallback ──

SERVER_PRICES = {"detail": {"error": "invalid_days", "options": [1, 5],
                            "prices_usd": {"1": 40.0, "5": 180.0},
                            "prices_sats": {"1": 61_000, "5": 274_000}}}


@pytest.fixture
def price_cache(monkeypatch):
    """An empty, expired price cache (conftest's never expires)."""
    c = {"at": 0.0, "usd": None, "sats": None}
    monkeypatch.setattr(hc, "_price_cache", c)
    return c


def _posts(monkeypatch, answer):
    calls = []

    def post(url, **kw):
        calls.append(kw.get("json"))
        if isinstance(answer, Exception):
            raise answer
        return _R(400, answer)
    monkeypatch.setattr(requests, "post", post)
    return calls


def test_pass_prices_come_from_the_server_and_are_cached(fresh_http, monkeypatch,
                                                         price_cache):
    calls = _posts(monkeypatch, SERVER_PRICES)
    p = hc.pass_prices()
    assert p == {"usd": {1: 40.0, 5: 180.0}, "sats": {1: 61_000, 5: 274_000},
                 "from_server": True}
    assert hc.day_pass_price() == {"usd": 40.0, "sats": 61_000, "from_server": True}
    assert calls == [{"days": 0}]                    # asked once, then cached


def test_pass_prices_fall_back_when_the_server_cannot_be_reached(
        fresh_http, monkeypatch, price_cache):
    monkeypatch.setattr(nc, "proxy_for", lambda url: "")
    calls = _posts(monkeypatch, requests.exceptions.ConnectionError("down"))
    assert hc.pass_prices() == {"usd": {1: 50.0, 5: 225.0}, "sats": None,
                                "from_server": False}
    assert hc.day_pass_price() == {"usd": 50.0, "sats": None, "from_server": False}
    assert len(calls) == 1                           # retried in a minute, not every call


def test_pass_prices_fall_back_for_an_older_server(fresh_http, monkeypatch, price_cache):
    _posts(monkeypatch, {"detail": {"error": "invalid_days", "options": [1, 5]}})
    assert hc.pass_prices() == {"usd": {1: 50.0, 5: 225.0}, "sats": None,
                                "from_server": False}


def test_pass_prices_keep_the_last_server_prices_while_unreachable(
        fresh_http, monkeypatch, price_cache):
    _posts(monkeypatch, SERVER_PRICES)
    hc.pass_prices()
    price_cache["at"] = 0.0                          # expired
    monkeypatch.setattr(nc, "proxy_for", lambda url: "")
    _posts(monkeypatch, requests.exceptions.ConnectionError("down"))
    assert hc.day_pass_price()["usd"] == 40.0


def test_fund_quote_uses_the_servers_day_price(monkeypatch):
    monkeypatch.setattr(hc, "_price_cache", {"at": float("inf"),
                                             "usd": {1: 40.0, 5: 180.0},
                                             "sats": {1: 61_000, 5: 274_000}})
    monkeypatch.setattr(webui, "_btc_usd", lambda: 64000.0)
    q = webui._fund_quote(200_000)                   # $128
    assert q["day_pass_usd"] == 40.0 and q["day_passes"] == 3
    assert q["day_pass_sats"] == 61_000                  # the server's sats
    assert q["suggested_sats"] == 61_000 + 610           # + LNbits fee reserve


def test_fund_quote_reports_the_day_price_without_a_btc_price(monkeypatch):
    monkeypatch.setattr(webui, "_btc_usd", lambda: 0.0)
    q = webui._fund_quote(0)
    assert q["ok"] is False and q["day_pass_usd"] == 50.0      # fallback


def test_doctor_day_price_server_then_fallback(monkeypatch):
    import agent
    monkeypatch.setattr(hc, "_price_cache", {"at": float("inf"),
                                             "usd": {1: 40.0}, "sats": {1: 61_000}})
    assert agent._day_pass_price() == (40.0, 61_000)
    monkeypatch.setattr(hc, "_price_cache", {"at": float("inf"), "usd": None, "sats": None})
    monkeypatch.setattr(webui, "_btc_usd", lambda: 0.0)
    assert agent._day_pass_price() == (50.0, agent.DAY_PASS_SATS_EST)
    monkeypatch.setattr(webui, "_btc_usd", lambda: 100_000.0)
    assert agent._day_pass_price() == (50.0, 50_000)


def test_polled_status_errors_are_diagnosed_once_per_minute(monkeypatch):
    calls = []
    monkeypatch.setattr(webui, "_NET_DIAG_CACHE", {"at": 0.0, "text": ""})
    monkeypatch.setattr(nc, "diagnose", lambda host: calls.append(host) or nc._result(
        False, "not_tls", "a web page answered instead", "allow-list it"))
    exc = requests.exceptions.SSLError("[SSL: WRONG_VERSION_NUMBER] wrong version number")
    first = webui._net_error_text(exc, cache_s=60)
    second = webui._net_error_text(exc, cache_s=60)
    assert first == second and "a web page answered instead" in first
    assert len(calls) == 1


def test_wizard_shows_pass_status_errors():
    assert "r.status==='error'&&r.error" in webui._WIZARD_HTML
