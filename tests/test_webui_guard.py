"""The localhost web UI must reject cross-site (CSRF) and DNS-rebinding requests.

`_Handler._guard` is pure header logic (reads self.headers, calls self._send on
reject), so we exercise it against a minimal stub rather than a live socket.
"""

import webui


class _Stub:
    """Just enough of a handler for _guard: a headers dict + a _send recorder."""

    def __init__(self, headers):
        self.headers = headers
        self.sent = None  # (obj, status)

    def _send(self, obj, status=200, html=None):
        self.sent = (obj, status)

    _guard = webui._Handler._guard


def _allowed_host():
    return f"127.0.0.1:{webui.APP_PORT}"


def _allowed_origin():
    return f"http://127.0.0.1:{webui.APP_PORT}"


# ── DNS-rebinding: Host must be our own loopback ────────────────────────────

def test_get_allows_own_host():
    s = _Stub({"Host": _allowed_host()})
    assert s._guard(check_origin=False) is True
    assert s.sent is None


def test_get_allows_localhost_alias():
    s = _Stub({"Host": f"localhost:{webui.APP_PORT}"})
    assert s._guard(check_origin=False) is True


def test_get_allows_missing_host():
    # Top-level navigations / some clients omit Host; don't hard-fail those.
    s = _Stub({})
    assert s._guard(check_origin=False) is True


def test_get_rejects_foreign_host():
    s = _Stub({"Host": "evil.example.com"})
    assert s._guard(check_origin=False) is False
    assert s.sent[1] == 403


# ── CSRF: state-changing POST must not carry a foreign Origin ───────────────

def test_post_allows_same_origin():
    s = _Stub({"Host": _allowed_host(), "Origin": _allowed_origin()})
    assert s._guard(check_origin=True) is True
    assert s.sent is None


def test_post_allows_absent_origin():
    # Non-browser clients (curl, the desktop app) send no Origin — permitted.
    s = _Stub({"Host": _allowed_host()})
    assert s._guard(check_origin=True) is True


def test_post_rejects_foreign_origin():
    s = _Stub({"Host": _allowed_host(), "Origin": "http://evil.example.com"})
    assert s._guard(check_origin=True) is False
    assert s.sent[1] == 403


def test_post_rejects_foreign_host_even_with_ok_origin():
    s = _Stub({"Host": "evil.example.com", "Origin": _allowed_origin()})
    assert s._guard(check_origin=True) is False
    assert s.sent[1] == 403


# ── wizard finish-line wiring (regression: the s-done step used to fire
#    /api/finish on arrival, shutting the server down BEFORE Option A/B) ─────

def _webui_source():
    import inspect
    return inspect.getsource(webui)


def test_safety_step_does_not_kill_the_wizard_server():
    src = _webui_source()
    safety_js = src.split("async function doSafety")[1].split("async function")[0]
    assert "/api/finish" not in safety_js, (
        "doSafety must NOT call /api/finish — it shuts the wizard server down "
        "before the user can click Start/Deploy on the final step")


def test_start_local_button_and_route_wired():
    src = _webui_source()
    assert "startLocal()" in src            # Option A button
    assert '"/api/start-local": start_local' in src  # route registered
    assert "connect_ex" in src              # child waits for the port to free


def test_deploy_success_finishes_wizard():
    src = _webui_source()
    deploy_js = src.split("async function deploy")[1].split("async function")[0]
    assert "/api/finish" in deploy_js
