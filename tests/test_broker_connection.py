"""Robinhood connection drops are logged once, explained, and not mislabeled
as a missing contract (user report 2026-10-21: "Robinhood has no tradable
contract NFLX 2026-10-23 $65 PUT" while the contract existed)."""
import json
import time

import pytest
import requests

import broker_events
from brokers import robinhood
from brokers import robinhood_mcp
from brokers.robinhood_mcp import RobinhoodMCP


class _Resp:
    def __init__(self, status, body=None, headers=None):
        self.status_code = status
        self._body = body
        self.text = json.dumps(body) if body is not None else ""
        self.headers = headers or {}

    def json(self):
        return self._body


def _client(tmp_path, expires_in=-60):
    path = tmp_path / "robinhood_oauth.json"
    path.write_text(json.dumps({
        "access_token": "old", "refresh_token": "r1", "client_id": "c",
        "expires_at": time.time() + expires_in}))
    return RobinhoodMCP(str(path))


def _ok_mcp(url, json=None, headers=None, data=None, timeout=None):
    if url == robinhood_mcp.TOKEN_URL:
        return _Resp(200, {"access_token": "new", "expires_in": 3600})
    req_id = (json or {}).get("id")
    return _Resp(200, {"jsonrpc": "2.0", "id": req_id,
                       "result": {"content": [{"type": "text", "text": "{}"}]}},
                 {"Mcp-Session-Id": "s1"})


@pytest.fixture(autouse=True)
def _clean_events():
    broker_events.begin()
    yield
    broker_events.begin()


def test_expired_login_is_logged_once_and_explained(tmp_path, monkeypatch, capsys):
    rh = _client(tmp_path)
    monkeypatch.setattr(robinhood_mcp.requests, "post",
                        lambda url, **kw: _Resp(400, {"error": "invalid_grant"}))
    assert rh.call_tool("get_option_instruments", {}) is None
    assert rh.call_tool("get_option_instruments", {}) is None   # still down

    assert "expired" in rh.last_error and "HTTP 400" in rh.last_error
    out = capsys.readouterr().out
    assert out.count("Robinhood connection lost") == 1          # logged on change only
    reported = broker_events.drain()
    assert [r["kind"] for r in reported] == ["broker_disconnected"]
    assert "reconnect" in broker_events.hint(reported[0]["message"]).lower()


def test_connection_restored_is_logged(tmp_path, monkeypatch, capsys):
    rh = _client(tmp_path)
    monkeypatch.setattr(robinhood_mcp.requests, "post",
                        lambda url, **kw: _Resp(400, {"error": "invalid_grant"}))
    rh.call_tool("get_accounts", {})
    monkeypatch.setattr(robinhood_mcp.requests, "post", _ok_mcp)
    assert rh.call_tool("get_accounts", {}) is not None
    assert rh.last_error is None
    out = capsys.readouterr().out
    assert "Robinhood connection restored" in out


def test_rejected_access_token_is_reported(tmp_path, monkeypatch):
    rh = _client(tmp_path, expires_in=3600)   # token looks fresh, server says no

    def post(url, **kw):
        if url == robinhood_mcp.TOKEN_URL:
            return _Resp(200, {"access_token": "new", "expires_in": 3600})
        return _Resp(401)
    monkeypatch.setattr(robinhood_mcp.requests, "post", post)
    assert rh.call_tool("get_accounts", {}) is None
    assert "HTTP 401" in rh.last_error and "expired" in rh.last_error


def test_network_error_marks_down_and_still_raises(tmp_path, monkeypatch):
    rh = _client(tmp_path, expires_in=3600)

    def boom(url, **kw):
        raise requests.ConnectionError("no route")
    monkeypatch.setattr(robinhood_mcp.requests, "post", boom)
    with pytest.raises(requests.ConnectionError):
        rh.call_tool("get_accounts", {})
    assert "can't reach Robinhood" in rh.last_error
    assert [r["kind"] for r in broker_events.drain()] == ["broker_disconnected"]


def test_healthy_connection_reports_nothing(tmp_path, monkeypatch, capsys):
    rh = _client(tmp_path)
    monkeypatch.setattr(robinhood_mcp.requests, "post", _ok_mcp)
    assert rh.call_tool("get_accounts", {}) is not None
    assert rh.last_error is None
    assert broker_events.drain() == []
    assert "Robinhood connection" not in capsys.readouterr().out


class _DownRH:
    last_error = "Robinhood refused to renew the login (HTTP 400) — the login has expired"

    def call_tool(self, name, args):
        return None


def _entry():
    return {"event": "ENTERED", "ticker": "NFLX", "expiry": "2026-10-23",
            "strike": 65.0, "type": "P", "event_id": "e1"}


def test_entry_with_dead_connection_is_not_called_missing_contract():
    state = {"positions": {}}
    assert robinhood.execute(_DownRH(), {}, _entry(), state) is False
    kinds = [r["kind"] for r in broker_events.drain()]
    assert kinds == ["broker_disconnected"]
    assert "skip_no_contract" not in kinds


def test_entry_with_live_connection_and_no_row_is_still_missing_contract():
    class _LiveRH:
        last_error = None

        def call_tool(self, name, args):
            return {"content": [{"type": "text", "text": "{\"data\": {\"results\": []}}"}]}
    state = {"positions": {}}
    assert robinhood.execute(_LiveRH(), {}, _entry(), state) is False
    assert [r["kind"] for r in broker_events.drain()] == ["skip_no_contract"]


def test_new_kind_notifies():
    assert "broker_disconnected" in broker_events.KINDS
    assert "broker_disconnected" in broker_events.NOTIFY
