"""The redesigned dashboard at /v2: served from the committed build behind
the same login and Host guard as every other page, next to an unchanged
classic dashboard; every state it is tested and screenshotted in is a valid
view-model; and its words cover every code the view-model can send."""

import http.client
import json
import os
import re
import shutil
import subprocess
import threading

import pytest

import agent
import view_model as vm
import webui

import guardian_states as gs
from test_view_model_http import serve, write  # noqa: F401  (fixture reuse)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def app(home):
    """A server with the classic page and /v2, as start_app sets it up."""
    class H(webui._Handler):
        pages = {"/dash": "<p>dash</p>"}
        routes_get = {"/api/v2/status": lambda h: {"ok": True}}
        landing = "/dash"
        v2 = True

    srv = webui._AppServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    hosts = (f"127.0.0.1:{port}", f"localhost:{port}")
    old = (webui._ALLOWED_HOSTS, webui._ALLOWED_ORIGINS)
    webui._ALLOWED_HOSTS = hosts
    webui._ALLOWED_ORIGINS = tuple(f"http://{h}" for h in hosts)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def get(path, login=True, host=None):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        headers = {"Host": host or f"127.0.0.1:{port}"}
        if login:
            headers["Cookie"] = f"{webui._SESSION_COOKIE}={webui.ui_token()}"
        c.request("GET", path, headers=headers)
        r = c.getresponse()
        body = r.read().decode()
        c.close()
        return r.status, dict(r.getheaders()), body

    yield get
    srv.shutdown()
    srv.server_close()
    webui._ALLOWED_HOSTS, webui._ALLOWED_ORIGINS = old


def _refs(html):
    return re.findall(r'(?:src|href)="/assets/([^"]+)"', html)


# ── the route ───────────────────────────────────────────────────────────────

def test_v2_needs_the_same_login_as_the_dashboard(app):
    for path in ("/v2", "/dash"):
        status, _h, body = app(path, login=False)
        assert status == 401 and "<div id=\"root\">" not in body, path
    status, headers, _b = app(f"/v2?k={webui.ui_token()}", login=False)
    assert status == 302 and headers["Location"] == "/v2"
    assert "HttpOnly" in headers["Set-Cookie"]
    assert app("/v2?k=wrong", login=False)[0] == 403


def test_v2_is_behind_the_host_guard(app):
    for path in ("/v2", "/dash", "/api/v2/status"):
        assert app(path, host="evil.example")[0] == 403, path


def test_v2_serves_its_own_entry_with_the_app_headers(app):
    status, headers, body = app("/v2")
    assert status == 200 and body == webui._app_v2()
    assert '<div id="root">' in body
    assert headers["Content-Type"].startswith("text/html")
    csp = headers["Content-Security-Policy"]
    assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert headers["X-Frame-Options"] == "DENY"
    assert headers["Cache-Control"] == "no-store"
    # its scripts and styles come from this agent, behind the same login
    js = [r for r in _refs(body) if r.endswith(".js")]
    assert js and all(r.startswith("v2-") for r in _refs(body))
    assert app("/assets/" + js[0], login=False)[0] == 401
    status, headers, _b = app("/assets/" + js[0])
    assert status == 200 and headers["Content-Type"].startswith("text/javascript")


def test_classic_dashboard_is_unchanged_and_still_the_default(app):
    status, _h, body = app("/dash")
    assert status == 200 and body == webui._app_index()
    assert not any(r.startswith("v2-") for r in _refs(body))
    assert app("/dash?ui=classic")[2] == "<p>dash</p>"
    status, headers, _b = app("/")
    assert status == 302 and headers["Location"] == "/dash"
    # only the exact path: nothing else under it is a page
    for path in ("/v2/", "/v2/x", "/v2.html", "/V2"):
        assert app(path)[0] == 404, path


def test_v2_falls_back_to_the_classic_dashboard_without_a_build(app, monkeypatch):
    monkeypatch.setattr(webui, "_app_v2", lambda: None)
    status, headers, _b = app("/v2")
    assert status == 302 and headers["Location"] == "/dash"


def test_a_server_that_does_not_turn_it_on_has_no_v2(home):
    from test_webui_auth import _req

    class H(webui._Handler):
        pages = {"/dash": "<p>dash</p>"}
        landing = "/dash"

    srv = webui._AppServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    old = (webui._ALLOWED_HOSTS, webui._ALLOWED_ORIGINS)
    webui._ALLOWED_HOSTS = (f"127.0.0.1:{port}",)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        cookie = f"{webui._SESSION_COOKIE}={webui.ui_token()}"
        assert _req(port, "GET", "/v2", cookie=cookie)[0] == 404
    finally:
        srv.shutdown()
        srv.server_close()
        webui._ALLOWED_HOSTS, webui._ALLOWED_ORIGINS = old


def test_the_real_app_serves_v2_next_to_the_status_it_reads(home, serve):  # noqa: F811
    """start_app's own routes: the page, and the API the page polls, behind
    one login — with an agent, and for the setup server with none."""
    write(home)
    get = serve(lambda: {"mode": "DRY-RUN", "fields": {}}, lambda: [],
                agent._apply_command, lambda: agent._load(agent.CONFIG_PATH, {}),
                get_view=lambda: gs.status("live"),
                get_activity=lambda before=None, limit=None: gs.activity(
                    "live", before=before, limit=limit))
    assert get("/v2", login=False)[0] == 401
    assert get("/v2", host="evil.example")[0] == 403
    status, ctype, body = get("/v2")
    assert status == 200 and ctype.startswith("text/html")
    assert body == webui._app_v2()
    status, _ctype, body = get("/api/v2/status")
    s = json.loads(body)
    assert status == 200 and vm.validate(s) == [] and s["mode"] == "live"
    page = json.loads(get(f"/api/v2/activity?before={s['activity']['next']}"
                          "&limit=100")[2])
    assert vm.validate(page, "ActivityPage") == []
    assert page["rows"] and page["rows"][0]["n"] < s["activity"]["rows"][-1]["n"]
    # the classic page and its API are still what they were
    assert get("/dash")[2] == webui._app_index()
    assert json.loads(get("/api/status")[2]) == {"mode": "DRY-RUN", "fields": {}}


def test_setup_server_serves_v2_too(home, serve):  # noqa: F811
    get = serve()
    assert get("/v2")[0] == 200
    assert json.loads(get("/api/v2/status")[2])["pause"]["kind"] == "stopped"


# ── the committed build ─────────────────────────────────────────────────────

def test_committed_v2_build_is_complete():
    html = webui._app_v2()
    assert html, "webui_dist/v2.html missing: run `npm run build` in webui_app/"
    refs = _refs(html)
    assert any(r.endswith(".js") for r in refs)
    assert any(r.endswith(".css") for r in refs)
    for r in refs:
        assert webui._asset_path("/assets/" + r), r
    # no network loads at run time, and no inline script (the CSP forbids it)
    assert not re.search(r'(?:src|href)="(?:https?:)?//', html)
    assert not re.search(r"<script(?![^>]*\bsrc=)", html)
    # the lazily loaded chart chunk the entry names is in the build as well
    with open(webui._asset_path("/assets/" + next(
            r for r in refs if r.endswith(".js"))), encoding="utf-8") as f:
        entry = f.read()
    for chunk in set(re.findall(r"v2-[A-Za-z0-9_-]+\.js", entry)):
        assert webui._asset_path("/assets/" + chunk), chunk


def test_the_two_dashboards_link_to_each_other():
    def bundle(html):
        with open(webui._asset_path("/assets/" + next(
                r for r in _refs(html) if r.endswith(".js"))),
                encoding="utf-8") as f:
            return f.read()
    assert "Try the new dashboard" in bundle(webui._app_index())
    assert "Back to the classic dashboard" in bundle(webui._app_v2())


def test_v2_bundle_has_no_demo_switch():
    """States for review come from tests/guardian_dev_server.py, never from
    a switch in the product: the bundle knows no fixture and no demo mode."""
    html = webui._app_v2()
    for r in _refs(html):
        with open(webui._asset_path("/assets/" + r), encoding="utf-8") as f:
            text = f.read().lower()
        for word in ("demo states", "demo mode", "fixture", "guardian_states"):
            assert word not in text, (r, word)


# ── the states it is reviewed in ────────────────────────────────────────────

@pytest.mark.parametrize("name", sorted(gs.STATES))
def test_every_fixture_state_is_a_valid_view_model(name):
    s = gs.status(name)
    assert vm.validate(s) == [], vm.validate(s)
    json.loads(json.dumps(s, allow_nan=False))
    page = gs.activity(name, limit=5)
    assert vm.validate(page, "ActivityPage") == []


def test_fixture_states_cover_the_states_the_dashboard_shows():
    got = {n: gs.status(n) for n in gs.STATES}
    assert got["live"]["mode"] == "live" and got["practice"]["mode"] == "practice"
    assert got["paused-new-trades"]["pause"]["kind"] == "entries"
    assert got["state-hold"]["pause"]["kind"] == "state_hold"
    assert got["handoff-hold"]["pause"]["kind"] == "handoff_hold"
    assert got["restarting"]["pause"]["kind"] == "restarting"
    assert got["stopped"]["agent"]["running"] is False
    assert got["not-answering"]["agent"] == {
        **got["not-answering"]["agent"], "running": True, "answering": False}
    assert got["broker-disconnected"]["broker"]["connected"] is False
    assert got["broker-disconnected"]["real_positions_managed"] is False
    assert got["limit-hit"]["limits"]["cap_reached"] is True
    assert got["low-cash"]["limits"]["cash"]["low"] is True
    assert got["empty-first-run"]["activity"]["total"] == 0
    assert got["pass-exits-only"]["signals"]["pass"]["status"] == "exits_only"
    # the practice day keeps real money and practice apart
    assert got["practice"]["real_positions"] == 3
    assert got["practice"]["real_positions_managed"] is True


# ── the words (Node, no dependencies; skipped where Node can't run it) ──────

def _node():
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    try:
        r = subprocess.run([node, "--experimental-strip-types", "-e", "0"],
                           capture_output=True, timeout=30, env=dict(os.environ))
    except (OSError, subprocess.TimeoutExpired):
        pytest.skip("node could not be started")
    if r.returncode != 0:
        pytest.skip("this node cannot load TypeScript (needs 22.6 or newer)")
    return node


def test_frontend_words_cover_every_code_and_every_state(tmp_path):
    node = _node()
    for name in gs.STATES:
        (tmp_path / f"{name}.json").write_text(
            json.dumps(gs.status(name)), encoding="utf-8")
    (tmp_path / "codes.json").write_text(json.dumps({
        "reason_codes": vm.REASON_CODES, "answer_states": vm.ANSWER_STATES,
        "answer_reasons": vm.ANSWER_REASONS, "next_actions": vm.NEXT_ACTIONS,
        "next_reasons": vm.NEXT_REASONS,
        "unmanaged_reasons": vm.UNMANAGED_REASONS}), encoding="utf-8")
    # Keep the parent environment: node.exe cannot start on Windows without
    # SystemRoot. Decode as UTF-8 on every platform.
    r = subprocess.run(
        [node, "--experimental-strip-types", "--test",
         os.path.join(ROOT, "webui_app", "tests", "guardian.test.mjs")],
        capture_output=True, text=True, encoding="utf-8", timeout=180,
        env={**os.environ, "GUARDIAN_FIXTURES": str(tmp_path),
             "TZ": "Asia/Tokyo"})
    assert r.returncode == 0, r.stdout[-4000:] + r.stderr[-2000:]
    assert re.search(r"^# fail 0$", r.stdout, re.M), r.stdout[-2000:]
    assert re.search(r"^# skipped 0$", r.stdout, re.M), r.stdout[-2000:]
