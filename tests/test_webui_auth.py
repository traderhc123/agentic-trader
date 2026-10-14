"""Dashboard login: every page and API call needs the per-install token."""

import http.client
import threading

import pytest

import webui


@pytest.fixture
def server(home):
    class H(webui._Handler):
        pages = {"/dash": "<p>dash</p>"}
        routes_get = {"/api/status": lambda h: {"ok": True}}
        routes_post = {"/api/command": lambda h, d: {"reply": "done"}}
        landing = "/dash"

    srv = webui._AppServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    # the Host/Origin guard only allows the app's own port
    hosts = (f"127.0.0.1:{port}", f"localhost:{port}")
    old = (webui._ALLOWED_HOSTS, webui._ALLOWED_ORIGINS)
    webui._ALLOWED_HOSTS = hosts
    webui._ALLOWED_ORIGINS = tuple(f"http://{h}" for h in hosts)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield port
    srv.shutdown()
    webui._ALLOWED_HOSTS, webui._ALLOWED_ORIGINS = old


def _req(port, method, path, cookie=None, body=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {"Host": f"127.0.0.1:{port}"}
    if cookie:
        headers["Cookie"] = cookie
    if body is not None:
        headers["Content-Type"] = "application/json"
    c.request(method, path, body=body, headers=headers)
    r = c.getresponse()
    return r.status, dict(r.getheaders()), r.read().decode()


def test_unauthenticated_requests_are_locked(server):
    assert _req(server, "GET", "/dash")[0] == 401
    assert _req(server, "GET", "/api/status")[0] == 401
    assert _req(server, "POST", "/api/command", body="{}")[0] == 401


def test_wrong_token_is_refused(server):
    assert _req(server, "GET", "/dash?k=nope")[0] == 403
    assert _req(server, "GET", "/api/status",
                cookie=f"{webui._SESSION_COOKIE}=nope")[0] == 401


def test_login_link_sets_cookie_and_unlocks(server):
    status, headers, _ = _req(server, "GET", f"/dash?k={webui.ui_token()}")
    assert status == 302 and headers["Location"] == "/dash"
    cookie = headers["Set-Cookie"].split(";")[0]
    assert "HttpOnly" in headers["Set-Cookie"]
    assert _req(server, "GET", "/dash", cookie=cookie)[0] == 200
    assert _req(server, "GET", "/api/status", cookie=cookie)[0] == 200
    status, _, body = _req(server, "POST", "/api/command", cookie=cookie, body="{}")
    assert status == 200 and "done" in body


def test_token_is_stable_and_private(home):
    import os
    import stat
    t = webui.ui_token()
    assert len(t) >= 32 and webui.ui_token() == t
    if os.name != "nt":
        mode = stat.S_IMODE(os.stat(webui._token_path()).st_mode)
        assert mode == 0o600
