"""create_wallet must return a wallet-page URL so the wizard can show users
where to send bitcoin, across LNbits response-shape variants."""

from unittest.mock import MagicMock, patch

import pytest

import lightning_wallet as lw


def _resp(payload, status=200):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = payload
    r.text = str(payload)
    return r


def test_create_wallet_returns_page_with_user_and_wallet_id():
    payload = {"id": "wal123", "user": "usr456", "adminkey": "adm789",
               "name": "agentic-trader"}
    with patch.object(lw.requests, "post", return_value=_resp(payload)):
        url, key, page = lw.create_wallet("https://demo.lnbits.com")
    assert key == "adm789"
    assert page == "https://demo.lnbits.com/wallet?usr=usr456&wal=wal123"


def test_create_wallet_page_wallet_id_only():
    payload = {"id": "wal123", "adminkey": "adm789"}
    with patch.object(lw.requests, "post", return_value=_resp(payload)):
        _, _, page = lw.create_wallet("https://demo.lnbits.com")
    assert page == "https://demo.lnbits.com/wallet?wal=wal123"


def test_create_wallet_nested_wallets_shape():
    payload = {"user": "usr1",
               "wallets": [{"id": "w1", "adminkey": "a1"}]}
    with patch.object(lw.requests, "post", return_value=_resp(payload)):
        url, key, page = lw.create_wallet("https://demo.lnbits.com")
    assert key == "a1"
    assert page == "https://demo.lnbits.com/wallet?usr=usr1&wal=w1"


def test_create_wallet_no_ids_page_empty():
    payload = {"adminkey": "a1"}
    with patch.object(lw.requests, "post", return_value=_resp(payload)):
        _, _, page = lw.create_wallet("https://demo.lnbits.com")
    assert page == ""


def test_create_wallet_no_adminkey_raises():
    with patch.object(lw.requests, "post", return_value=_resp({"id": "x"})):
        with pytest.raises(lw.WalletError):
            lw.create_wallet("https://demo.lnbits.com")


def test_qr_matrix_shape_and_failsoft():
    import webui
    m = webui._qr_matrix("LNBC500N1TESTINVOICE")
    if m is not None:  # qrcode installed
        assert len(m) >= 21 and len(m) == len(m[0])
        assert all(cell in (0, 1) for row in m for cell in row)
    # fail-soft: qrcode import breaking must yield None, not raise
    import builtins
    real_import = builtins.__import__

    def _no_qrcode(name, *a, **k):
        if name == "qrcode":
            raise ImportError("nope")
        return real_import(name, *a, **k)
    builtins.__import__ = _no_qrcode
    try:
        assert webui._qr_matrix("X") is None
    finally:
        builtins.__import__ = real_import


def test_qr_matrix_bootstraps_missing_dep_once(monkeypatch):
    """qrcode missing -> install requirements once, retry; never twice."""
    import builtins
    import webui
    monkeypatch.setattr(webui, "_qr_bootstrap_attempted", False)
    installs = []
    monkeypatch.setattr(webui, "_install_requirements",
                        lambda: installs.append(1) or "")
    real_import = builtins.__import__

    def _no_qrcode(name, *a, **k):
        if name == "qrcode":
            raise ImportError("missing")
        return real_import(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", _no_qrcode)

    assert webui._qr_matrix("X") is None
    assert installs == [1]          # bootstrap attempted
    assert webui._qr_matrix("X") is None
    assert installs == [1]          # but only once per process


def test_qr_matrix_recovers_after_bootstrap(monkeypatch):
    """Import fails once, install 'fixes' it -> matrix returned same call."""
    import builtins
    import webui
    monkeypatch.setattr(webui, "_qr_bootstrap_attempted", False)
    state = {"fixed": False}
    monkeypatch.setattr(webui, "_install_requirements",
                        lambda: state.update(fixed=True) or "")
    real_import = builtins.__import__

    def _flaky(name, *a, **k):
        if name == "qrcode" and not state["fixed"]:
            raise ImportError("missing")
        return real_import(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", _flaky)

    m = webui._qr_matrix("LNBC1SELFHEAL")
    assert m is not None and len(m) == len(m[0])


def test_fund_quote_math(monkeypatch):
    import webui
    monkeypatch.setattr(webui, "_btc_usd", lambda: 64000.0)
    q = webui._fund_quote(50_000)
    assert q["ok"] and q["usd"] == 32.0 and q["day_passes"] == 3
    # month suggestion: 21 passes x $10 = $210 -> sats, rounded to 1k
    assert q["suggested_month_sats"] == 328_000
    assert webui._fund_quote(0)["usd"] == 0


def test_fund_quote_failsoft_without_price(monkeypatch):
    import webui
    monkeypatch.setattr(webui, "_btc_usd", lambda: 0.0)
    assert webui._fund_quote(50_000)["ok"] is False


def test_btc_usd_cache(monkeypatch):
    import webui
    webui._btc_usd_cache.update(t=0.0, usd=0.0)
    calls = []

    class _R:
        def json(self):
            calls.append(1)
            return {"data": {"amount": "64000"}}
    import requests
    monkeypatch.setattr(requests, "get", lambda *a, **k: _R())
    assert webui._btc_usd() == 64000.0
    assert webui._btc_usd() == 64000.0  # cached — no second fetch
    assert len(calls) == 1
