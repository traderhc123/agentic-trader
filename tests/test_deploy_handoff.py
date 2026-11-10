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
    monkeypatch.setattr(provision, "LOCAL_STOP_DELAY_S", 0)

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
    assert "paused now" in r["note"] and "stops in about" in r["note"]
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
    assert webui.CONTROLS == {"paused": False, "stop": False}


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
