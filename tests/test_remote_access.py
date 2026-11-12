"""Phone remote: one-time pairing, hashed device tokens, and the phone-only
API (bearer token, route allowlist, no browser origins)."""

import http.client
import json
import threading

import pytest

import notifications
import remote_access as RA
import webui


@pytest.fixture(autouse=True)
def _reset_fails():
    RA._fails.update(n=0, until=0.0)
    yield
    RA._fails.update(n=0, until=0.0)


# ── pairing + devices ────────────────────────────────────────────────────────

def test_pair_with_code_gives_token_once(home):
    code = RA.new_pairing_code()
    r = RA.pair(code, "Cooper's iPhone")
    assert r["token"] and r["device_id"]
    assert RA.device_for_token(r["token"])["name"] == "Cooper's iPhone"
    assert "error" in RA.pair(code, "again")           # one use only


def test_expired_code_is_refused(home):
    code = RA.new_pairing_code(now=1000.0)
    assert "error" in RA.pair(code, "p", now=1000.0 + RA.PAIR_TTL_S + 1)


def test_new_code_replaces_old(home):
    old = RA.new_pairing_code()
    new = RA.new_pairing_code()
    assert "error" in RA.pair(old, "p")
    assert "token" in RA.pair(new, "p")


def test_wrong_codes_lock_pairing(home):
    code = RA.new_pairing_code(now=0.0)
    for _ in range(RA.MAX_PAIR_FAILS):
        assert "error" in RA.pair("nope", now=1.0)
    # locked: even the right code fails, and the code itself is dead
    assert "error" in RA.pair(code, now=2.0)
    assert "error" in RA.pair(code, now=2.0 + RA.PAIR_LOCK_S + 1)


def test_tokens_are_stored_hashed_only(home):
    r = RA.pair(RA.new_pairing_code(), "p")
    raw = (home / "devices.json").read_text()
    assert r["token"] not in raw
    assert RA._sha(r["token"]) in raw


def test_unknown_or_empty_token(home):
    assert RA.device_for_token("") is None
    assert RA.device_for_token(None) is None
    assert RA.device_for_token("x" * 43) is None


def test_revoke(home):
    r = RA.pair(RA.new_pairing_code(), "p")
    assert RA.revoke(r["device_id"])
    assert RA.device_for_token(r["token"]) is None
    assert not RA.revoke(r["device_id"])


def test_device_cap(home):
    for _ in range(RA.MAX_DEVICES):
        assert "token" in RA.pair(RA.new_pairing_code(), "p")
    assert "error" in RA.pair(RA.new_pairing_code(), "one too many")


def test_push_token_validation(home):
    r = RA.pair(RA.new_pairing_code(), "p", push_token="not-a-token")
    assert RA.push_tokens() == []
    assert not RA.set_push_token(r["device_id"], "https://evil")
    assert RA.set_push_token(r["device_id"], "ExponentPushToken[abc]")
    assert RA.push_tokens() == ["ExponentPushToken[abc]"]
    assert RA.set_push_token(r["device_id"], None)
    assert RA.push_tokens() == []


def test_name_is_cleaned(home):
    r = RA.pair(RA.new_pairing_code(), "a\x00b\n" + "x" * 200)
    name = RA.device_for_token(r["token"])["name"]
    assert "\x00" not in name and "\n" not in name and len(name) <= 60


def test_pairing_link_encodes(home):
    link = RA.pairing_link("https://box.tail1.ts.net", "a+b/c")
    assert link.startswith("agentictrader://pair?u=https%3A%2F%2Fbox")
    assert "c=a%2Bb%2Fc" in link


# ── phone API over HTTP ──────────────────────────────────────────────────────

@pytest.fixture
def remote(home, monkeypatch):
    monkeypatch.setenv("AGENT_REMOTE", "1")
    calls = []

    class Base(webui._Handler):
        routes_get = {"/api/v2/status": lambda h: {"ok": True, "status": "running"},
                      "/api/state": lambda h: {"secret": "dashboard-only"}}
        routes_post = {"/api/v2/command": lambda h, d: calls.append(d) or {"reply": "paused"},
                       "/api/v2/settings/code": lambda h, d: {"edited": True}}

    srv = webui.start_remote(Base, port=0)
    port = srv.server_address[1]
    yield port, calls
    srv.shutdown()


def _req(port, method, path, token=None, body=None, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    h = dict(headers or {})
    if token:
        h["Authorization"] = f"Bearer {token}"
    if body is not None:
        h["Content-Type"] = "application/json"
        body = json.dumps(body) if not isinstance(body, (str, bytes)) else body
    c.request(method, path, body=body, headers=h)
    r = c.getresponse()
    raw = r.read().decode()
    try:
        return r.status, json.loads(raw)
    except ValueError:
        return r.status, raw


def _paired(port):
    code = RA.new_pairing_code()
    s, r = _req(port, "POST", "/api/remote/pair", body={"code": code, "name": "Test phone"})
    assert s == 200
    return r["token"]


def test_unpaired_requests_are_refused(remote):
    port, _ = remote
    assert _req(port, "GET", "/api/v2/status")[0] == 401
    assert _req(port, "GET", "/api/v2/status", token="nope")[0] == 401
    assert _req(port, "POST", "/api/v2/command", body={"command": "pause"})[0] == 401


def test_dashboard_cookie_does_not_unlock_phone_api(remote):
    port, _ = remote
    cookie = f"{webui._SESSION_COOKIE}={webui.ui_token()}"
    assert _req(port, "GET", "/api/v2/status", headers={"Cookie": cookie})[0] == 401


def test_paired_phone_reaches_allowlisted_routes(remote):
    port, calls = remote
    tok = _paired(port)
    assert _req(port, "GET", "/api/v2/status", token=tok) == (200, {"ok": True, "status": "running"})
    s, r = _req(port, "POST", "/api/v2/command", token=tok, body={"command": "pause"})
    assert s == 200 and r == {"reply": "paused"} and calls == [{"command": "pause"}]
    s, r = _req(port, "GET", "/api/remote/whoami", token=tok)
    assert s == 200 and r["device"]["name"] == "Test phone"


def test_routes_outside_allowlist_are_hidden(remote):
    port, _ = remote
    tok = _paired(port)
    assert _req(port, "GET", "/api/state", token=tok)[0] == 404
    assert _req(port, "POST", "/api/v2/settings/code", token=tok, body={})[0] == 404


def test_browser_origin_is_refused(remote):
    port, _ = remote
    tok = _paired(port)
    o = {"Origin": "https://evil.example"}
    assert _req(port, "GET", "/api/v2/status", token=tok, headers=o)[0] == 403
    code = RA.new_pairing_code()
    assert _req(port, "POST", "/api/remote/pair", body={"code": code}, headers=o)[0] == 403


def test_bad_pair_and_bad_bodies(remote):
    port, _ = remote
    assert _req(port, "POST", "/api/remote/pair", body={"code": "nope"})[0] == 403
    tok = _paired(port)
    assert _req(port, "POST", "/api/v2/command", token=tok, body="[1,2]")[0] == 400
    big = {"x": "a" * (webui.REMOTE_MAX_BODY + 10)}
    assert _req(port, "POST", "/api/v2/command", token=tok, body=big)[0] == 400


def test_push_register_and_unpair(remote):
    port, _ = remote
    tok = _paired(port)
    assert _req(port, "POST", "/api/remote/push", token=tok,
                body={"push_token": "nope"})[0] == 400
    assert _req(port, "POST", "/api/remote/push", token=tok,
                body={"push_token": "ExpoPushToken[xyz]"})[0] == 200
    assert RA.push_tokens() == ["ExpoPushToken[xyz]"]
    assert _req(port, "POST", "/api/remote/unpair", token=tok, body={})[0] == 200
    assert _req(port, "GET", "/api/v2/status", token=tok)[0] == 401


def test_remote_can_be_turned_off(home, monkeypatch):
    monkeypatch.setenv("AGENT_REMOTE", "0")
    assert webui.start_remote(webui._Handler, port=0) is None


# ── push notifications ───────────────────────────────────────────────────────

def test_notify_pushes_to_paired_phones(home, monkeypatch):
    r = RA.pair(RA.new_pairing_code(), "p")
    RA.set_push_token(r["device_id"], "ExponentPushToken[abc]")
    sent = []
    monkeypatch.setattr(notifications.requests, "post",
                        lambda url, **kw: sent.append((url, kw)))
    assert notifications.notify({}, "BOUGHT 1 SPY 600C\nfilled at 1.20", priority="high")
    url, kw = sent[0]
    assert url == notifications._EXPO_PUSH_URL
    msg = kw["json"][0]
    assert msg["to"] == "ExponentPushToken[abc]" and msg["title"] == "BOUGHT 1 SPY 600C"
    assert msg["body"] == "filled at 1.20" and msg["priority"] == "high"


def test_notify_without_phones_sends_no_push(home, monkeypatch):
    sent = []
    monkeypatch.setattr(notifications.requests, "post", lambda url, **kw: sent.append(url))
    assert notifications.notify({}, "hello") is False
    assert sent == []


# ── CLI ──────────────────────────────────────────────────────────────────────

def test_cmd_pair_without_address_explains_tailscale(home, monkeypatch, capsys):
    import agent
    monkeypatch.setattr(RA, "tailscale_url", lambda: None)
    agent.cmd_pair()
    out = capsys.readouterr().out
    assert "tailscale serve --bg --https=443 http://127.0.0.1:8722" in out
    assert not (home / "devices.json").exists()


def test_cmd_pair_prints_code_that_pairs(home, capsys):
    import agent
    agent.cmd_pair("https://box.tail1.ts.net")
    out = capsys.readouterr().out
    code = [ln.split()[-1] for ln in out.splitlines() if ln.startswith("Code:")][0]
    assert "https://box.tail1.ts.net" in out
    assert "token" in RA.pair(code, "p")


# ── wiring into the running agent ───────────────────────────────────────────

def test_running_agent_starts_phone_api_on_the_real_routes(home, monkeypatch):
    """start_app (the running agent's server) also starts the phone API, on
    its own handler, and every allowlisted route exists in the real app."""
    import agent
    monkeypatch.setattr(agent, "consent_ok", lambda: True)
    started = []
    monkeypatch.setattr(webui, "start_remote", lambda base, port=None: started.append(base))

    class Capture:
        def __init__(self, addr, handler):
            pass

        def serve_forever(self):
            pass

        def shutdown(self):
            pass

    monkeypatch.setattr(webui, "_AppServer", Capture)
    webui.start_app()
    assert len(started) == 1
    base = started[0]
    assert webui.REMOTE_GET <= set(base.routes_get)
    assert webui.REMOTE_POST <= set(base.routes_post)
