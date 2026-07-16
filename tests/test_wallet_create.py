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
