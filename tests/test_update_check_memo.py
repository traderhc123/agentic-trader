"""The update check (a `git fetch`, or a GitHub API call for a zip install)
is remembered for 15 minutes — every page load asks — and says when it
really checked. "Check now" (force) asks again; errors are kept 60 s only;
an update attempt forgets the answer. No network: the check is a stub."""

import pytest

import webui


@pytest.fixture
def fetches(monkeypatch):
    """Count the real checks; the memo key is a fixed install."""
    n = {"calls": 0, "result": {"ok": True, "behind": 0, "target": "abc"}}

    def check():
        n["calls"] += 1
        return dict(n["result"])
    monkeypatch.setattr(webui, "_check_code_updates", check)
    monkeypatch.setattr(webui, "_update_memo_key", lambda: ("git", "origin", "head1"))
    clock = {"t": 5000.0}
    import time
    monkeypatch.setattr(time, "monotonic", lambda: clock["t"])
    n["clock"] = clock
    return n


def test_two_checks_one_fetch(fetches):
    a = webui.check_code_updates()
    b = webui.check_code_updates()
    assert fetches["calls"] == 1
    assert a == b and a["ok"] and a["target"] == "abc"


def test_memo_runs_out_after_15_minutes(fetches):
    webui.check_code_updates()
    fetches["clock"]["t"] += webui.UPDATE_CHECK_MEMO_S - 1
    webui.check_code_updates()
    assert fetches["calls"] == 1
    fetches["clock"]["t"] += 1
    webui.check_code_updates()
    assert fetches["calls"] == 2


def test_force_bypasses(fetches):
    webui.check_code_updates()
    webui.check_code_updates(force=True)
    assert fetches["calls"] == 2


def test_error_memo_short(fetches):
    fetches["result"] = {"ok": False, "error": "GitHub unreachable"}
    webui.check_code_updates()
    webui.check_code_updates()
    assert fetches["calls"] == 1
    fetches["clock"]["t"] += webui.UPDATE_ERROR_MEMO_S
    webui.check_code_updates()
    assert fetches["calls"] == 2


def test_checked_at_present(fetches):
    r = webui.check_code_updates()
    assert r["checked_at"].endswith("+00:00")
    assert webui.check_code_updates()["checked_at"] == r["checked_at"]


def test_another_install_or_an_update_asks_again(fetches, monkeypatch):
    webui.check_code_updates()
    monkeypatch.setattr(webui, "_update_memo_key", lambda: ("git", "origin", "head2"))
    webui.check_code_updates()
    assert fetches["calls"] == 2
    webui._forget_update_check()      # what pull_code_updates does first
    webui.check_code_updates()
    assert fetches["calls"] == 3


def test_route_passes_force(home, monkeypatch):
    """GET /api/update/check?force=1 is the owner's "check now"; a plain
    GET (every page load) may get the remembered answer."""
    seen = []
    monkeypatch.setattr(webui, "check_code_updates",
                        lambda force=False: seen.append(force) or {"ok": True})
    made = {}

    class NoPort:
        def __init__(self, addr, handler):
            made["handler"] = handler

        def serve_forever(self):
            pass

        def shutdown(self):
            pass
    monkeypatch.setattr(webui, "_AppServer", NoPort)
    webui.start_app()
    route = made["handler"].routes_get["/api/update/check"]

    class H:
        path = "/api/update/check"
    route(H)
    H.path = "/api/update/check?force=1"
    route(H)
    assert seen == [False, True]
