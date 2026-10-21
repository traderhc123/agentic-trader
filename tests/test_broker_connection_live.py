"""Robinhood 'connected' must mean a live answer, not just a saved login file."""
import json
import time

import agent
from brokers import robinhood
from brokers import robinhood_mcp


class _Resp:
    def __init__(self, status, body=None, headers=None):
        self.status_code = status
        self._body = body
        self.text = json.dumps(body) if body is not None else ""
        self.headers = headers or {}

    def json(self):
        return self._body


def _tokens(tmp_path, monkeypatch, expires_in=-60):
    path = tmp_path / "robinhood_oauth.json"
    path.write_text(json.dumps({
        "access_token": "old", "refresh_token": "r1", "client_id": "c",
        "expires_at": time.time() + expires_in}))
    monkeypatch.setattr(robinhood, "_token_path", lambda: str(path))


def _accounts_server(accounts):
    def post(url, json=None, **kw):
        if url == robinhood_mcp.TOKEN_URL:
            return _Resp(200, {"access_token": "new", "expires_in": 3600})
        payload = {"data": {"accounts": accounts}}
        return _Resp(200, {"jsonrpc": "2.0", "id": (json or {}).get("id"),
                           "result": {"content": [{"type": "text",
                                                   "text": __import__("json").dumps(payload)}]}},
                     {"Mcp-Session-Id": "s1"})
    return post


def test_verify_expired_login_is_not_connected(tmp_path, monkeypatch):
    _tokens(tmp_path, monkeypatch)
    monkeypatch.setattr(robinhood_mcp.requests, "post",
                        lambda url, **kw: _Resp(400, {"error": "invalid_grant"}))
    ok, msg = robinhood.verify({"robinhood_account": "5551234"})
    assert ok is False and "expired" in msg


def test_verify_live_account_is_connected(tmp_path, monkeypatch):
    _tokens(tmp_path, monkeypatch)
    monkeypatch.setattr(robinhood_mcp.requests, "post", _accounts_server([
        {"account_number": "5551234", "agentic_allowed": True, "state": "active",
         "option_level": "option_level_2"}]))
    ok, msg = robinhood.verify({"robinhood_account": "5551234"})
    assert ok is True and "live check" in msg and "1234" in msg


def test_verify_no_saved_login(tmp_path, monkeypatch):
    monkeypatch.setattr(robinhood, "_token_path", lambda: str(tmp_path / "missing.json"))
    ok, msg = robinhood.verify({})
    assert ok is False and "no saved Robinhood login" in msg


def test_verify_inactive_agentic_account(tmp_path, monkeypatch):
    _tokens(tmp_path, monkeypatch)
    monkeypatch.setattr(robinhood_mcp.requests, "post", _accounts_server([
        {"account_number": "5551234", "agentic_allowed": False, "state": "active"}]))
    ok, msg = robinhood.verify({"robinhood_account": "5551234"})
    assert ok is False and "isn't active" in msg


class _Client:
    def __init__(self, err=None, ok_at=None, since=None):
        self.last_error = err
        self.last_ok_at = ok_at
        self.down_since = since


def test_dashboard_line_down_and_ok():
    assert agent._broker_conn_desc(None) is None
    assert agent._broker_conn_desc(object()) is None          # broker w/o tracking
    down = agent._broker_conn_desc(_Client("login expired", since=time.time()))
    assert down.startswith("⚠️ DOWN") and "login expired" in down
    assert agent._broker_conn_desc(_Client(ok_at=time.time())).startswith("ok")


def test_down_alert_once_per_day(monkeypatch):
    sent = []
    monkeypatch.setattr(agent, "notify", lambda cfg, msg: sent.append(msg))
    state, saves = {}, []
    c = _Client("Robinhood refused to renew the login (HTTP 400)")
    agent._alert_if_broker_down({}, state, saves.append, c)
    agent._alert_if_broker_down({}, state, saves.append, c)
    assert len(sent) == 1 and "DOWN" in sent[0] and "reconnect" in sent[0]
    agent._alert_if_broker_down({}, {}, saves.append, _Client(None))
    assert len(sent) == 1
