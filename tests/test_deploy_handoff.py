"""Moving to a DigitalOcean server must hand the WHOLE setup over: the server
gets the positions / seen signals / daily counters / paid day-pass, and this
computer is retired so the two can never both trade the same account.

Hermetic: DigitalOcean is stubbed, no port is bound, AGENT_HOME is a tmp dir.
"""

import base64
import gzip
import json
import os
import time
from datetime import datetime, timezone

import pytest

import agent as A
import keep_running
import provision
import transfer
import webui


def _doc(raw):
    assert raw.startswith("#cloud-config\n")
    return json.loads(raw.split("\n", 1)[1])


def _content(doc, name):
    f = next(f for f in doc["write_files"]
             if f["path"] == f"/home/trader/.agentic-trader/{name}")
    data = base64.b64decode(f["content"])
    if f["encoding"] == "gz+b64":
        data = gzip.decompress(data)
    else:
        assert f["encoding"] == "b64"
    assert f["permissions"] == "0600"
    return data


def _today():
    return datetime.now(timezone.utc).date().isoformat()


def _row(day, i, action="entry"):
    return json.dumps({"action": action, "contract": f"SPY-{i}", "qty": 1,
                       "ts": f"{day}T14:{i % 60:02d}:00+00:00",
                       "note": os.urandom(24).hex()}) + "\n"


# ── what travels to the server ──

def test_server_gets_positions_seen_events_counters_and_pass(home):
    state = {"seen": ["ev-1", "ev-2"],
             "positions": {"SPY|2026-11-10|600|call": {"qty": 2}},
             "l402_token": "paid-day-pass"}
    (home / "config.json").write_text(json.dumps({"source": "agenthc"}),
                                      encoding="utf-8")
    (home / "state.json").write_text(json.dumps(state), encoding="utf-8")
    ledger = _row("2026-01-02", 1) + _row(_today(), 2)
    (home / "trades.jsonl").write_bytes(ledger.encode("utf-8"))
    doc = _doc(provision.build_cloud_init())
    assert json.loads(_content(doc, "state.json")) == state
    assert _content(doc, "trades.jsonl") == ledger.encode("utf-8")


def test_machine_only_files_stay_behind(home):
    (home / "config.json").write_text("{}", encoding="utf-8")
    for name in ("MOVED_AWAY", "ui_token", "agent.lock", "do_ssh_key"):
        (home / name).write_text("x", encoding="utf-8")
    paths = [f["path"] for f in _doc(provision.build_cloud_init())["write_files"]]
    assert not any(p.endswith(("MOVED_AWAY", "ui_token", "agent.lock",
                               "do_ssh_key")) for p in paths)


def test_long_history_is_trimmed_to_fit_but_today_always_travels(home):
    """DigitalOcean refuses user-data over 64 KiB. Old history is only used
    for the P&L pages; today's rows are the daily entry cap and loss stop."""
    (home / "config.json").write_text("{}", encoding="utf-8")
    (home / "state.json").write_text(json.dumps({"positions": {}}),
                                     encoding="utf-8")
    old = [_row("2026-01-02", i) for i in range(3000)]
    today = [_row(_today(), i) for i in range(40)]
    (home / "trades.jsonl").write_bytes("".join(old + today).encode("utf-8"))
    notes = []
    raw = provision.build_cloud_init(notes=notes)
    assert len(raw.encode("utf-8")) <= provision.USER_DATA_LIMIT
    sent = _content(_doc(raw), "trades.jsonl").decode("utf-8").splitlines(True)
    assert sent[-40:] == today                  # every one of today's rows
    assert 40 < len(sent) < 3040                # plus the newest older ones
    assert sent == (old + today)[-len(sent):]   # whole rows, newest kept
    assert len(notes) == 1 and "history" in notes[0]
    assert str(home / "trades.jsonl") in notes[0]  # where the full copy is


def test_refuses_when_todays_log_alone_cannot_fit(home):
    (home / "config.json").write_text("{}", encoding="utf-8")
    today = [_row(_today(), i) for i in range(3000)]
    (home / "trades.jsonl").write_bytes("".join(today).encode("utf-8"))
    with pytest.raises(RuntimeError, match="too large"):
        provision.build_cloud_init()


# ── the dashboard button: retire this computer after the server is made ──

class _FakeServer:
    """Stands in for the HTTP server so no port is bound."""
    handler = None

    def __init__(self, addr, handler):
        _FakeServer.handler = handler

    def serve_forever(self):
        pass

    def shutdown(self):
        pass


@pytest.fixture
def deploy_route(home, tmp_path, monkeypatch):
    """The real /api/deploy handler, with a login entry to turn off."""
    (home / "config.json").write_text(json.dumps({"source": "agenthc"}),
                                      encoding="utf-8")
    entry = tmp_path / "autostart-entry"
    entry.write_text("x", encoding="utf-8")
    monkeypatch.setattr(keep_running, "autostart_paths", lambda: [str(entry)])
    monkeypatch.setattr(webui, "_AppServer", _FakeServer)
    monkeypatch.setitem(webui.CONTROLS, "paused", False)
    monkeypatch.setitem(webui.CONTROLS, "stop", False)
    monkeypatch.setitem(transfer.HANDOFF, "hold", False)
    transfer.CYCLE_IDLE.set()   # no heartbeat cycle in progress
    monkeypatch.setattr(keep_running, "is_running", lambda: False)
    monkeypatch.setattr(provision, "LOCAL_STOP_AFTER_SHOWN_S", 0)
    monkeypatch.setattr(provision, "LOCAL_STOP_CAP_S", 0)
    monkeypatch.setattr(A, "notify", lambda *a, **k: None)

    def make(running):
        webui.start_app(get_status=(lambda: {}) if running else None)
        return _FakeServer.handler.routes_post["/api/deploy"]
    return make


def test_deploy_retires_this_computer(deploy_route, monkeypatch):
    monkeypatch.setattr(provision, "create_droplet",
                        lambda token, region: (7, "droplet 7 creating in nyc3"))
    r = deploy_route(running=True)(None, {"token": "t", "region": "nyc3"})
    assert r["ok"], r
    # exactly what "move to another computer" does
    assert transfer.moved_away()
    assert not keep_running.autostart_enabled()
    # paused at once (no orders), stopped once the page had time to show the
    # server's address — the delay is 0 here
    assert webui.CONTROLS["paused"] is True
    deadline = time.time() + 10
    while not webui.CONTROLS["stop"] and time.time() < deadline:
        time.sleep(0.01)
    assert webui.CONTROLS["stop"] is True
    assert "paused now" in r["note"] and "stops by itself" in r["note"]
    # ...and the owner is told, with the way back
    assert "this computer" in r["note"].lower()
    assert "resume-here" in r["note"]


def test_deploy_from_the_wizard_retires_without_a_running_agent(
        deploy_route, monkeypatch):
    monkeypatch.setattr(provision, "create_droplet",
                        lambda token, region: (7, "droplet 7 creating in nyc3"))
    r = deploy_route(running=False)(None, {"token": "t"})
    assert r["ok"] and transfer.moved_away()
    assert not keep_running.autostart_enabled()
    assert webui.CONTROLS["stop"] is False  # nothing running here to stop


def test_failed_deploy_changes_nothing_here(deploy_route, monkeypatch):
    seen = {}

    def boom(token, region):
        # no new signals may be handled while the snapshot is being sent
        seen["paused"] = webui.CONTROLS["paused"]
        raise RuntimeError("DigitalOcean rejected the token (401)")
    monkeypatch.setattr(provision, "create_droplet", boom)
    r = deploy_route(running=True)(None, {"token": "bad"})
    assert not r["ok"] and "401" in r["error"]
    assert seen["paused"] is True
    assert transfer.moved_away() is None
    assert keep_running.autostart_enabled()
    assert not webui.CONTROLS["paused"] and not webui.CONTROLS["stop"]


def test_second_deploy_is_refused_while_the_first_server_has_the_setup(
        deploy_route, monkeypatch):
    """Two servers made from one setup would also place every order twice."""
    made = []
    monkeypatch.setattr(provision, "create_droplet", lambda token, region: (
        made.append(region) or (7, "droplet 7 creating in nyc3")))
    deploy = deploy_route(running=False)
    assert deploy(None, {"token": "t"})["ok"]
    r = deploy(None, {"token": "t"})
    assert not r["ok"] and made == ["nyc3"]
    assert "twice" in r["error"] and "resume-here" in r["error"]


# ── review: the copy sent to the server must not miss a trade in progress ──

def _wait_for(cond, timeout=10):
    deadline = time.time() + timeout
    while not cond() and time.time() < deadline:
        time.sleep(0.01)
    return cond()


def test_copy_waits_for_the_trade_being_placed(deploy_route, home, monkeypatch):
    """The heartbeat is in the middle of a buy when the owner clicks: the
    server's copy is taken only after that cycle has saved its position."""
    import threading
    (home / "state.json").write_text(json.dumps({"positions": {}}),
                                     encoding="utf-8")
    transfer.CYCLE_IDLE.clear()   # a cycle is running (order being placed)

    def finish_the_buy():
        _wait_for(lambda: transfer.HANDOFF["hold"])   # the click arrived
        time.sleep(0.1)
        (home / "state.json").write_text(json.dumps(
            {"positions": {"SPY|2026-11-10|600|call": {"qty": 1}}}),
            encoding="utf-8")
        transfer.CYCLE_IDLE.set()   # end of that cycle
    sent = {}

    def create(token, region):
        sent["hold"] = transfer.HANDOFF["hold"]
        sent["state"] = json.loads(_content(_doc(provision.build_cloud_init()),
                                            "state.json"))
        return 7, "droplet 7 creating in nyc3"
    monkeypatch.setattr(provision, "create_droplet", create)
    t = threading.Thread(target=finish_the_buy)
    t.start()
    r = deploy_route(running=True)(None, {"token": "t"})
    t.join()
    assert r["ok"], r
    assert sent["hold"] is True
    assert "SPY|2026-11-10|600|call" in sent["state"]["positions"]


def test_deploy_is_refused_while_a_trade_will_not_finish(deploy_route, monkeypatch):
    made = []
    monkeypatch.setattr(provision, "create_droplet",
                        lambda token, region: made.append(1) or (7, "x"))
    monkeypatch.setattr(provision, "CYCLE_WAIT_S", 0.05)
    transfer.CYCLE_IDLE.clear()
    try:
        r = deploy_route(running=True)(None, {"token": "t"})
    finally:
        transfer.CYCLE_IDLE.set()
    assert not r["ok"] and not made
    assert "no server was created" in r["error"].lower()
    assert transfer.moved_away() is None and keep_running.autostart_enabled()
    assert transfer.HANDOFF["hold"] is False
    assert not webui.CONTROLS["paused"] and not webui.CONTROLS["stop"]


def test_setup_page_refuses_while_the_agent_runs_in_another_window(
        deploy_route, monkeypatch):
    """The setup page cannot pause an agent running in another process, so it
    cannot take a safe copy: the owner stops that agent first."""
    made = []
    monkeypatch.setattr(provision, "create_droplet",
                        lambda token, region: made.append(1) or (7, "x"))
    monkeypatch.setattr(keep_running, "is_running", lambda: True)
    r = deploy_route(running=False)(None, {"token": "t"})
    assert not r["ok"] and not made
    assert "python agent.py stop" in r["error"]
    assert "no server was created" in r["error"].lower()
    assert transfer.moved_away() is None


def test_resume_cannot_undo_the_handoff(deploy_route, monkeypatch):
    monkeypatch.setattr(provision, "create_droplet",
                        lambda token, region: (7, "droplet 7 creating in nyc3"))
    monkeypatch.setattr(provision, "LOCAL_STOP_CAP_S", 3600)
    assert deploy_route(running=True)(None, {"token": "t"})["ok"]
    handled, reply = A._apply_command("resume")
    assert handled and "resume-here" in reply and "twice" in reply
    assert webui.CONTROLS["paused"] is True and transfer.HANDOFF["hold"] is True
    webui.CONTROLS["stop"] = True   # let the waiting thread go


def test_resume_still_works_when_nothing_was_moved(deploy_route):
    deploy_route(running=True)
    webui.CONTROLS["paused"] = True
    assert A._apply_command("resume") == (True, "Resumed.")
    assert webui.CONTROLS["paused"] is False


# ── review: the page must get the server's address before this one stops ──

def test_agent_here_stops_only_after_the_page_has_the_address(
        deploy_route, monkeypatch):
    monkeypatch.setattr(provision, "create_droplet",
                        lambda token, region: (7, "droplet 7 creating in nyc3"))
    monkeypatch.setattr(provision, "LOCAL_STOP_CAP_S", 3600)
    ip = {"v": ""}
    monkeypatch.setattr(provision, "droplet_status", lambda token, did: {
        "status": "new" if not ip["v"] else "active", "ip": ip["v"]})
    deploy = deploy_route(running=True)
    status = _FakeServer.handler.routes_get["/api/deploy/status"]
    assert deploy(None, {"token": "t"})["ok"]
    assert status(None)["ip"] == ""
    time.sleep(0.3)
    assert webui.CONTROLS["stop"] is False     # still serving the page
    assert webui.CONTROLS["paused"] is True    # ...but placing no orders
    ip["v"] = "203.0.113.9"
    assert "203.0.113.9" in status(None)["ssh"]
    assert _wait_for(lambda: webui.CONTROLS["stop"])


def test_agent_here_stops_anyway_if_the_address_never_comes(
        deploy_route, monkeypatch):
    monkeypatch.setattr(provision, "create_droplet",
                        lambda token, region: (7, "droplet 7 creating in nyc3"))
    monkeypatch.setattr(provision, "LOCAL_STOP_CAP_S", 0.2)
    assert deploy_route(running=True)(None, {"token": "t"})["ok"]
    assert _wait_for(lambda: webui.CONTROLS["stop"])


def test_reply_tells_the_page_what_happened_to_this_computer(
        deploy_route, monkeypatch):
    monkeypatch.setattr(provision, "create_droplet",
                        lambda token, region: (7, "droplet 7 creating in nyc3"))
    r = deploy_route(running=True)(None, {"token": "t"})
    here = r["here"]
    assert "paused now" in here and "places no more orders" in here
    assert "this page" in here                      # when it stops
    assert "nothing is watching your open positions" in here
    assert "resume-here" in here
    r2 = None
    transfer.resume_here()
    monkeypatch.setitem(transfer.HANDOFF, "hold", False)
    monkeypatch.setitem(webui.CONTROLS, "stop", False)
    r2 = deploy_route(running=False)(None, {"token": "t"})
    assert "won't run the agent again" in r2["here"]
    assert "paused now" not in r2["here"]   # nothing was running here


# ── review: nothing after "the server exists" may leave both copies live ──

def test_server_created_but_login_entry_cannot_be_removed(
        deploy_route, monkeypatch):
    made = []
    monkeypatch.setattr(provision, "create_droplet", lambda token, region: (
        made.append(1) or (7, "droplet 7 creating in nyc3")))

    def locked():
        raise PermissionError(13, "Access is denied")
    monkeypatch.setattr(keep_running, "disable_autostart", locked)
    deploy = deploy_route(running=True)
    r = deploy(None, {"token": "t"})
    assert r["ok"], r
    assert transfer.moved_away()                 # the agent refuses to start
    assert "could not be removed" in r["note"]   # and the owner is told
    assert webui.CONTROLS["paused"] is True
    assert _wait_for(lambda: webui.CONTROLS["stop"])
    assert not deploy(None, {"token": "t"})["ok"] and made == [1]


def test_server_created_but_the_marker_cannot_be_written(
        deploy_route, monkeypatch):
    monkeypatch.setattr(provision, "create_droplet",
                        lambda token, region: (7, "droplet 7 creating in nyc3"))

    def disk_full():
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(transfer, "mark_moved_away", disk_full)
    r = deploy_route(running=True)(None, {"token": "t"})
    assert r["ok"], r
    assert "do not start the agent on this computer" in r["note"].lower()
    assert webui.CONTROLS["paused"] is True and transfer.HANDOFF["hold"] is True
    assert _wait_for(lambda: webui.CONTROLS["stop"])


# ── review: DigitalOcean did not answer — was the server created? ──

class _R:
    def __init__(self, code, body=None):
        self.status_code, self._b = code, body or {}
        self.text = json.dumps(self._b)

    def json(self):
        return self._b


def _silent_create(monkeypatch, tmp_path, listing):
    """POST /droplets times out; GET /droplets answers with `listing`
    (an Exception instance = that call fails too)."""
    import requests
    monkeypatch.setenv("AGENT_HOME", str(tmp_path))
    monkeypatch.setattr(provision.shutil, "which", lambda name: None)

    def req(method, url, **kw):
        if url.endswith("/droplets") and method == "POST":
            raise requests.exceptions.ReadTimeout("read timed out")
        if "/droplets?" in url and method == "GET":
            if isinstance(listing, Exception):
                raise listing
            return _R(200, {"droplets": listing})
        return _R(404)
    monkeypatch.setattr(provision.requests, "request", req)


def test_no_answer_but_the_server_was_created(monkeypatch, tmp_path):
    made = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _silent_create(monkeypatch, tmp_path, [
        {"id": 3, "name": "something-else", "status": "new", "created_at": made},
        {"id": 5, "name": "agentic-trader", "status": "active",
         "created_at": "2026-01-02T03:04:05Z"},            # an old one
        {"id": 9, "name": "agentic-trader", "status": "new", "created_at": made,
         "region": {"slug": "nyc3"}}])
    droplet_id, msg = provision.create_droplet("t", "nyc3")
    assert droplet_id == 9 and "9" in msg


def test_no_answer_and_no_server_on_the_account(monkeypatch, tmp_path):
    _silent_create(monkeypatch, tmp_path, [])
    with pytest.raises(RuntimeError) as exc:
        provision.create_droplet("t", "nyc3")
    assert "no new server" in str(exc.value)
    assert "Nothing was changed on this computer" in str(exc.value)


def test_no_answer_and_the_account_cannot_be_checked(monkeypatch, tmp_path):
    import requests
    _silent_create(monkeypatch, tmp_path,
                   requests.exceptions.ConnectionError("network is down"))
    with pytest.raises(provision.OutcomeUnknown) as exc:
        provision.create_droplet("t", "nyc3")
    text = str(exc.value)
    assert "not known whether the server was created" in text
    assert "cloud.digitalocean.com" in text and "destroy it" in text
    assert "twice" in text or "second time" in text


def test_unknown_outcome_reaches_the_owner_in_full(deploy_route, monkeypatch,
                                                   tmp_path):
    import requests
    _silent_create(monkeypatch, tmp_path / "agent-home",
                   requests.exceptions.ConnectionError("network is down"))
    r = deploy_route(running=True)(None, {"token": "t"})
    assert not r["ok"]
    assert r["error"].rstrip().endswith("second time.")   # not cut short
    assert not webui.CONTROLS["paused"] and not webui.CONTROLS["stop"]
    assert transfer.HANDOFF["hold"] is False


# ── review: owners who deployed BEFORE this fix may have two copies live ──

def test_earlier_deploy_is_flagged_once(home):
    assert provision.earlier_deploy_warning() is None   # never deployed
    (home / "do_ssh_key").write_text("PRIVATE", encoding="utf-8")
    text = provision.earlier_deploy_warning()
    assert "DigitalOcean" in text and "twice" in text
    assert "python agent.py stop" in text
    assert provision.earlier_deploy_warning() is None   # shown once


def test_no_flag_after_a_deploy_that_retired_this_computer(
        deploy_route, home, monkeypatch):
    def create(token, region):
        (home / "do_ssh_key").write_text("PRIVATE", encoding="utf-8")
        return 7, "droplet 7 creating in nyc3"
    monkeypatch.setattr(provision, "create_droplet", create)
    assert deploy_route(running=False)(None, {"token": "t"})["ok"]
    transfer.resume_here()   # later: server destroyed, back on this computer
    assert provision.earlier_deploy_warning() is None


def test_no_flag_after_a_first_deploy_that_failed(deploy_route, home, monkeypatch):
    def create(token, region):
        (home / "do_ssh_key").write_text("PRIVATE", encoding="utf-8")
        raise RuntimeError("DigitalOcean rejected the token (401)")
    monkeypatch.setattr(provision, "create_droplet", create)
    assert not deploy_route(running=False)(None, {"token": "bad"})["ok"]
    assert provision.earlier_deploy_warning() is None   # no server was made


def test_flag_stays_when_it_is_not_known_whether_a_server_was_made(
        deploy_route, home, monkeypatch):
    """DigitalOcean never answered and the account could not be read: a
    server may exist. This computer carries on, so the "check for a second
    copy" warning must still come at the next start."""
    def create(token, region):
        (home / "do_ssh_key").write_text("PRIVATE", encoding="utf-8")
        raise provision.OutcomeUnknown("not known whether the server was created")
    monkeypatch.setattr(provision, "create_droplet", create)
    assert not deploy_route(running=False)(None, {"token": "t"})["ok"]
    assert "DigitalOcean" in (provision.earlier_deploy_warning() or "")


def test_double_click_creates_one_server(deploy_route, monkeypatch):
    """The second click arrives while the first is still talking to
    DigitalOcean (nothing is marked as moved yet): it must not make a
    second server."""
    import threading
    made, started, go = [], threading.Event(), threading.Event()

    def create(token, region):
        made.append(region)
        started.set()
        go.wait(10)
        return 7, "droplet 7 creating in nyc3"
    monkeypatch.setattr(provision, "create_droplet", create)
    deploy = deploy_route(running=False)
    first = {}
    t = threading.Thread(target=lambda: first.update(deploy(None, {"token": "t"})))
    t.start()
    try:
        assert started.wait(10)
        second = deploy(None, {"token": "t"})
    finally:
        go.set()
        t.join()
    assert first["ok"] and made == ["nyc3"]
    assert not second["ok"] and "already being created" in second["error"]
    assert "no second server" in second["error"].lower()
