"""What the /v2 settings forms need from the server beyond the settings API:
the Robinhood sign-in started from /v2 (two-step, the classic /api/rh/start
underneath, and a sign-in that lands back on /v2), and the setup wizard's
way back to /v2. All over real HTTP with no broker, wallet or network."""

import http.client
import json

import pytest

import agent
import webui
from brokers import robinhood

from test_v2_settings_api import BASE, app, read_cfg, write_cfg  # noqa: F401


class FakeRobinhood:
    """Stands in for the Robinhood MCP client: no network."""
    started = []
    finished = []

    def __init__(self, _token_path):
        pass

    def auth_start(self):
        FakeRobinhood.started.append(True)
        return "https://robinhood.example/oauth?state=abc", {"verifier": "v"}

    def auth_finish(self, p, redirect):
        FakeRobinhood.finished.append(redirect)

    def call_tool(self, name, args):
        assert name == "get_accounts"
        return {"content": [{"type": "text", "text": json.dumps({"data": {"accounts": [
            {"account_number": "5QR74821", "agentic_allowed": True,
             "state": "active", "option_level": "option_level_2"}]}})}]}


@pytest.fixture
def rh(monkeypatch):
    FakeRobinhood.started, FakeRobinhood.finished = [], []
    monkeypatch.setattr(robinhood, "RobinhoodMCP", FakeRobinhood)
    return FakeRobinhood


def _callback(c):
    """The browser coming back from Robinhood: where it is sent next."""
    conn = http.client.HTTPConnection("127.0.0.1", c.port, timeout=10)
    conn.request("GET", "/callback?code=xyz&state=abc",
                 headers={"Host": f"127.0.0.1:{c.port}",
                          "Cookie": f"{webui._SESSION_COOKIE}={webui.ui_token()}"})
    r = conn.getresponse()
    r.read()
    conn.close()
    return r.status, r.getheader("Location")


def test_robinhood_sign_in_needs_the_confirmation(home, app, rh):  # noqa: F811
    start = {k: v for k, v in BASE.items()
             if k not in ("broker", "alpaca_key_id", "alpaca_secret", "alpaca_paper")}
    write_cfg(home, start)
    c = app()
    first = c.post("/api/v2/broker/robinhood", {})
    assert first["needs_confirm"] is True
    p = first["preview"]
    ch = p["changes"][0]
    # no broker before: a real-money account raises the risk
    assert ch["key"] == "broker" and ch["new"] == {"id": "robinhood", "paper": False}
    assert ch["direction"] == "raises_risk"
    assert any("Practice and LIVE" in d for d in p["does_not"])
    # the preview starts no sign-in and saves nothing
    assert rh.started == [] and read_cfg(home) == start
    # a wrong token: nothing
    r = c.post("/api/v2/broker/robinhood", {"confirm": "1.2.3"})
    assert r["ok"] is False and rh.started == []
    # unknown keys are refused
    r = c.post("/api/v2/broker/robinhood", {"broker": "robinhood"})
    assert r["ok"] is False and "Unknown setting" in r["error"]
    r = c.confirmed("/api/v2/broker/robinhood", {})
    assert r == {"ok": True, "url": "https://robinhood.example/oauth?state=abc"}
    assert rh.started == [True]
    # still nothing saved: only the sign-in at Robinhood changes the broker
    assert read_cfg(home) == start


def test_a_sign_in_from_v2_lands_on_v2_and_a_classic_one_where_it_did(home, app, rh):  # noqa: F811
    write_cfg(home, BASE)
    c = app()
    c.confirmed("/api/v2/broker/robinhood", {})
    status, where = _callback(c)
    assert status == 302 and where == "/v2"
    cfg = read_cfg(home)
    assert cfg["broker"] == "robinhood" and cfg["robinhood_account"] == "5QR74821"
    # the classic sign-in, after it: back to "/" exactly as before
    write_cfg(home, BASE)
    assert c.get("/api/rh/start") == {"url": "https://robinhood.example/oauth?state=abc"}
    status, where = _callback(c)
    assert status == 302 and where == "/"
    # a v2 sign-in that was started and then replaced by a classic one
    c.confirmed("/api/v2/broker/robinhood", {})
    c.get("/api/rh/start")
    assert _callback(c)[1] == "/"


def test_replacing_a_paper_broker_with_robinhood_says_so(home, app, rh):  # noqa: F811
    write_cfg(home, BASE)  # alpaca, paper
    c = app()
    p = c.post("/api/v2/broker/robinhood", {})["preview"]
    assert p["changes"][0]["saved_now"] == {"id": "alpaca", "paper": True}
    assert p["changes"][0]["direction"] == "raises_risk"
    assert any("not sold from the new one" in d for d in p["does_not"])


def test_robinhood_route_needs_the_login_and_the_right_host_and_origin(home, app, rh):  # noqa: F811
    write_cfg(home, BASE)
    c = app()
    own = f"http://127.0.0.1:{c.port}"
    assert c.raw("POST", "/api/v2/broker/robinhood", "{}", login=False, origin=own)[0] == 401
    assert c.raw("POST", "/api/v2/broker/robinhood", "{}", host="evil.example", origin=own)[0] == 403
    assert c.raw("POST", "/api/v2/broker/robinhood", "{}", origin="http://evil.example")[0] == 403
    assert rh.started == [] and read_cfg(home) == BASE


def test_robinhood_needs_the_agreement(home, app, rh, monkeypatch):  # noqa: F811
    write_cfg(home, BASE)
    c = app()
    monkeypatch.setattr(agent, "consent_ok", lambda: False)
    r = c.post("/api/v2/broker/robinhood", {})
    assert r == {"ok": False, "error": "accept the agreement first"}
    assert rh.started == []


def test_broker_direction():
    d = webui._broker_direction
    assert d({"id": None, "paper": None}, {"id": "alpaca", "paper": True}) == "neutral"
    assert d({"id": None, "paper": None}, {"id": "robinhood", "paper": False}) == "raises_risk"
    assert d({"id": "alpaca", "paper": False}, {"id": "alpaca", "paper": True}) == "lowers_risk"
    assert d({"id": "robinhood", "paper": False}, {"id": "robinhood", "paper": False}) == "neutral"
    assert d({"id": "alpaca", "paper": True}, {"id": "x", "paper": None}) == "neutral"
