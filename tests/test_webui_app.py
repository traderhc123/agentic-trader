"""The React app UI: served from the committed build in webui_dist/, with a
strict CSP, auth on every asset, no path tricks, and the built-in pages as a
fallback (?ui=classic, or when no build is present)."""

import os
import re

import pytest

import webui
from test_webui_auth import _req, server  # noqa: F401  (fixture reuse)


def _login(port):
    _, headers, _ = _req(port, "GET", f"/dash?k={webui.ui_token()}")
    return headers["Set-Cookie"].split(";")[0]


def test_committed_build_is_complete():
    """index.html must point at assets that exist — a half-committed build
    would show users a blank page."""
    html = webui._app_index()
    assert html, "webui_dist/index.html missing: run `npm run build` in webui_app/"
    refs = re.findall(r'(?:src|href)="/assets/([^"]+)"', html)
    assert any(r.endswith(".js") for r in refs)
    assert any(r.endswith(".css") for r in refs)
    for r in refs:
        assert webui._asset_path("/assets/" + r), r
    # no network loads at runtime: everything comes from this agent
    assert not re.search(r'(?:src|href)="(?:https?:)?//', html)


def test_pages_serve_the_app_with_csp(server):  # noqa: F811
    cookie = _login(server)
    for page in ("/dash",):  # the fixture app only registers /dash
        status, headers, body = _req(server, "GET", page, cookie=cookie)
        assert status == 200
        assert '<div id="root">' in body
        csp = headers["Content-Security-Policy"]
        assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
        assert headers["X-Frame-Options"] == "DENY"


def test_classic_pages_still_available(server):  # noqa: F811
    cookie = _login(server)
    status, _, body = _req(server, "GET", "/dash?ui=classic", cookie=cookie)
    assert status == 200 and body == "<p>dash</p>"


def test_falls_back_to_builtin_pages_without_a_build(server, monkeypatch):  # noqa: F811
    monkeypatch.setattr(webui, "_app_index", lambda: None)
    cookie = _login(server)
    assert _req(server, "GET", "/dash", cookie=cookie)[2] == "<p>dash</p>"


def test_assets_need_login_and_have_types(server):  # noqa: F811
    js = re.search(r'/assets/[^"]+\.js', webui._app_index()).group(0)
    assert _req(server, "GET", js)[0] == 401
    status, headers, _ = _req(server, "GET", js, cookie=_login(server))
    assert status == 200
    assert headers["Content-Type"].startswith("text/javascript")
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert "immutable" in headers["Cache-Control"]


@pytest.mark.parametrize("path", [
    "/assets/../index.html",
    "/assets/..%2findex.html",
    "/assets/%2e%2e/webui.py",
    "/assets/sub/x.js",
    "/assets/.hidden.js",
    "/assets/index.html",
    "/assets/x.py",
    "/assets/",
])
def test_asset_paths_cannot_escape(server, path):  # noqa: F811
    assert webui._asset_path(path) is None
    assert _req(server, "GET", path, cookie=_login(server))[0] == 404


def test_asset_path_rejects_symlink_out(tmp_path, monkeypatch):
    dist = tmp_path / "webui_dist"
    (dist / "assets").mkdir(parents=True)
    secret = tmp_path / "secret.js"
    secret.write_text("x")
    try:
        os.symlink(secret, dist / "assets" / "evil.js")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    (dist / "assets" / "ok.js").write_text("y")
    monkeypatch.setattr(webui, "_DIST_DIR", str(dist))
    assert webui._asset_path("/assets/evil.js") is None
    assert webui._asset_path("/assets/ok.js")
