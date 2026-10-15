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
        "detail": {"error": "invalid_days", "options": [5, 20, 60]}}))
    with pytest.raises(hc.PassSizeUnavailable) as exc:
        hc.buy_pass(1)
    assert "1-day passes aren't on sale right now — pick 5, 20, 60" in str(exc.value)


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
