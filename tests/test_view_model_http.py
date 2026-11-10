"""GET /api/v2/status and /api/v2/activity: served behind the same login as
the rest of the API, next to an unchanged /api/status — over real HTTP, from
the routes start_app builds, and from a running `agent.py run` loop."""

import http.client
import json
import threading
from datetime import datetime, timezone

import pytest

import agent
import view_model as vm
import webui

CFG = {"source": "stub", "broker": "stub", "dry_run": True,
       "max_entries_per_day": 3}


def write(home, cfg=None, state=None, rows=()):
    (home / "config.json").write_text(json.dumps(CFG if cfg is None else cfg),
                                      encoding="utf-8")
    (home / "state.json").write_text(
        json.dumps(state or {"positions": {}, "seen": []}), encoding="utf-8")
    if rows:
        (home / "trades.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


@pytest.fixture
def serve(home, monkeypatch):
    """start(**start_app arguments) -> get(path, login=True): the real
    routes and the real auth/host guard, on a port of the test's own."""
    real_server = webui._AppServer
    made = {}

    class Capture:
        def __init__(self, addr, handler):
            made["handler"] = handler

        def serve_forever(self):
            pass

        def shutdown(self):
            pass

    servers = []
    old = (webui._ALLOWED_HOSTS, webui._ALLOWED_ORIGINS)

    def start(*args, **kwargs):
        monkeypatch.setattr(webui, "_AppServer", Capture)
        webui.start_app(*args, **kwargs)
        monkeypatch.setattr(webui, "_AppServer", real_server)
        srv = real_server(("127.0.0.1", 0), made["handler"])
        servers.append(srv)
        port = srv.server_address[1]
        hosts = (f"127.0.0.1:{port}", f"localhost:{port}")
        webui._ALLOWED_HOSTS = hosts
        webui._ALLOWED_ORIGINS = tuple(f"http://{h}" for h in hosts)
        threading.Thread(target=srv.serve_forever, daemon=True).start()

        def get(path, login=True, host=None):
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            headers = {"Host": host or f"127.0.0.1:{port}"}
            if login:
                headers["Cookie"] = f"{webui._SESSION_COOKIE}={webui.ui_token()}"
            c.request("GET", path, headers=headers)
            r = c.getresponse()
            body = r.read().decode()
            c.close()
            return r.status, r.getheader("Content-Type"), body
        return get

    yield start
    for srv in servers:
        srv.shutdown()
        srv.server_close()
    webui._ALLOWED_HOSTS, webui._ALLOWED_ORIGINS = old


def ts(minutes_ago):
    return datetime.fromtimestamp(
        datetime.now(timezone.utc).timestamp() - 60 * minutes_ago,
        timezone.utc).isoformat(timespec="seconds")


def test_v2_status_needs_the_same_login_as_the_old_status(home, serve):
    write(home)
    get = serve(lambda: {"mode": "DRY-RUN", "fields": {}}, lambda: [],
                agent._apply_command, lambda: agent._load(agent.CONFIG_PATH, {}))
    for path in ("/api/status", "/api/v2/status", "/api/v2/activity"):
        status, _ctype, body = get(path, login=False)
        assert status == 401 and "locked" in json.loads(body)["error"], path
    # ...and the same guard against another site's hostname
    for path in ("/api/status", "/api/v2/status", "/api/v2/activity"):
        assert get(path, host="evil.example")[0] == 403, path
    status, _ctype, body = get("/api/v2/status")
    assert status == 200 and vm.validate(json.loads(body)) == []
    # an agent that handed over no view of itself: unknown, never "all fine"
    assert json.loads(body)["agent"] == {
        **json.loads(body)["agent"], "running": True, "answering": False}
    assert get("/api/v2/nothing")[0] == 404


def test_v2_status_over_http_is_a_schema_2_view_model(home, serve):
    key = "SPY|2026-07-10|752.0|C"
    rows = [{"action": "entry", "event_id": "e1", "dry": True,
             "contract": "SPY 2026-07-10 $752.0 C", "ts": ts(30),
             "ui": {"mode": "practice", "book": "main", "sent": False,
                    "filled": False, "qty": 2}},
            {"action": "skip_daily_cap", "book": "main", "event_id": "e2",
             "contract": "QQQ 2026-07-10 $640.0 C", "ts": ts(5),
             "ui": {"mode": "practice", "check": "daily_cap",
                    "params": {"cap": 3}}}]
    write(home, state={"positions": {key: {"option_id": "dry", "qty": 2,
                                           "dry": True}}}, rows=rows)
    old_status = {"mode": "DRY-RUN", "paused": False, "fields": {"source": "stub"}}
    get = serve(lambda: dict(old_status), lambda: [], agent._apply_command,
                lambda: agent._load(agent.CONFIG_PATH, {}),
                get_view=lambda: agent.view_snapshot({
                    "running": True, "heartbeat_at": agent._now(),
                    "wait_s": 30}))
    status, ctype, body = get("/api/v2/status")
    assert status == 200 and ctype == "application/json"
    s = json.loads(body)
    assert vm.validate(s) == [], vm.validate(s)
    assert s["schema"] == 2 and s["mode"] == "practice"
    assert s["agent"] == {**s["agent"], "running": True, "answering": True}
    assert [p["id"] for p in s["positions"]] == [key]
    assert s["positions"][0]["qty"] == 2 and s["positions"][0]["real"] is False
    assert s["limits"]["cap"] == 3
    assert [r["action"] for r in s["activity"]["rows"]] == [
        "skip_daily_cap", "entry"]
    assert s["activity"]["rows"][0]["stopped_at_check"] == "daily_cap"
    # the old route still answers exactly what the agent hands it
    status, _ctype, body = get("/api/status")
    assert status == 200 and json.loads(body) == old_status
    status, _ctype, body = get("/api/trades")
    assert status == 200 and [t["action"] for t in json.loads(body)["trades"]] \
        == ["skip_daily_cap", "entry"]


def test_v2_activity_pages_back_through_the_log(home, serve):
    write(home, rows=[{"action": "skip_paused", "event_id": f"e{i}",
                       "contract": "SPY 2026-07-10 $752.0 C", "ts": ts(60 - i)}
                      for i in range(7)])
    get = serve(lambda: {}, lambda: [], agent._apply_command,
                lambda: agent._load(agent.CONFIG_PATH, {}))
    status, _ctype, body = get("/api/v2/activity?limit=3")
    page = json.loads(body)
    assert status == 200 and vm.validate(page, "ActivityPage") == []
    assert [r["n"] for r in page["rows"]] == [6, 5, 4]
    assert (page["total"], page["next"], page["oldest"]) == (7, 4, 0)
    page = json.loads(get(f"/api/v2/activity?before={page['next']}&limit=3")[2])
    assert [r["n"] for r in page["rows"]] == [3, 2, 1] and page["next"] == 1
    page = json.loads(get("/api/v2/activity?before=1&limit=3")[2])
    assert [r["n"] for r in page["rows"]] == [0] and page["next"] is None
    # junk in the query never errors
    status, _ctype, body = get("/api/v2/activity?before=abc&limit=-1e99&x=1")
    assert status == 200 and vm.validate(json.loads(body), "ActivityPage") == []
    assert len(json.loads(get("/api/v2/activity")[2])["rows"]) == 7


def test_with_no_agent_running_the_view_model_says_stopped(home, serve):
    """The setup server (no agent behind it) still answers: from the files,
    with nothing running to manage the positions in them."""
    key = "SPY|2026-07-10|752.0|C"
    write(home, cfg={**CFG, "dry_run": False},
          state={"positions": {key: {"option_id": "x", "qty": 1, "live": True}}})
    get = serve()
    status, _ctype, body = get("/api/v2/status")
    s = json.loads(body)
    assert status == 200 and vm.validate(s) == []
    assert s["agent"]["running"] is False and s["pause"]["kind"] == "stopped"
    assert s["mode"] == "live" and s["real_positions"] == 1
    assert s["real_positions_managed"] is False
    assert s["unmanaged_reason"] == "agent_stopped"
    # the old route's answer for this case is unchanged
    assert json.loads(get("/api/status")[2]) == {
        "mode": "OFFLINE", "paused": False, "fields": {}}


def test_first_run_with_no_files_at_all(home, serve):
    get = serve()
    s = json.loads(get("/api/v2/status")[2])
    assert vm.validate(s) == [] and s["positions"] == []
    assert s["agent"]["settings_readable"] is True
    assert s["activity"]["total"] == 0


def test_damaged_files_do_not_take_the_route_down(home, serve):
    (home / "config.json").write_text("{not json", encoding="utf-8")
    (home / "state.json").write_text("[1, 2", encoding="utf-8")
    (home / "trades.jsonl").write_bytes(b"\xff\xfe\n{\"action\": 5}\nnull\n")
    get = serve()
    status, _ctype, body = get("/api/v2/status")
    s = json.loads(body)
    assert status == 200 and vm.validate(s) == []
    assert s["agent"]["settings_readable"] is False
    assert s["activity"]["rows"][0]["known"] is False


def test_start_dashboard_passes_the_view_through(monkeypatch):
    got = {}
    monkeypatch.setattr(webui, "start_app", lambda *a, **k: got.update(a=a, k=k))
    webui.start_dashboard(1, 2, 3, 4)
    assert got == {"a": (1, 2, 3, 4), "k": {"get_view": None,
                                            "get_activity": None}}
    webui.start_dashboard(1, 2, 3, 4, get_view=5, get_activity=6)
    assert got["k"] == {"get_view": 5, "get_activity": 6}


# ── reading the trade log for the view-model ────────────────────────────────

def test_a_log_that_cannot_be_written_still_counts_and_shows(home, monkeypatch):
    """Rows kept in memory (full disk) are in the view-model too, numbered
    after the last line of the file."""
    agent._log_trade({"action": "entry", "contract": "A", "dry": False})

    def full(path, rows):
        raise OSError("disk full")
    monkeypatch.setattr(agent, "_append_trade_rows", full)
    monkeypatch.setattr(agent, "_UNWRITTEN", [])
    monkeypatch.setattr(agent, "_LOG_FAIL", {"day": datetime.now(
        timezone.utc).date().isoformat()})
    agent._log_trade(agent._row_facts(
        {"action": "entry", "contract": "B", "dry": False}, {}))
    got = agent._view_rows()
    assert [(r["n"], r["contract"]) for r in got] == [(0, "A"), (1, "B")]
    assert agent.view_snapshot({"running": True})["limits"]["used_today"][
        "real"] == agent._entries_today(False) == 2


def test_view_rows_are_read_again_only_when_the_log_changes(home, monkeypatch):
    agent._log_trade({"action": "entry", "contract": "A", "dry": False})
    reads = []
    real = agent._scan_trade_log
    monkeypatch.setattr(agent, "_scan_trade_log",
                        lambda strict=False: reads.append(1) or real(strict))
    monkeypatch.setattr(agent, "_VIEW_LOG", {"snap": (None, [], 0)})
    first = agent._view_rows()
    assert agent._view_rows() == first and len(reads) == 1
    first.append("mine")                      # a caller's list is its own
    assert len(agent._view_rows()) == 1 and len(reads) == 1
    agent._log_trade({"action": "exit", "contract": "A", "dry": False})
    assert [r["action"] for r in agent._view_rows()] == ["entry", "exit"]
    assert len(reads) == 2
    (home / "trades.jsonl").unlink()          # no file: nothing kept
    assert agent._view_rows() == [] and agent._view_rows() == []
    assert len(reads) == 4
    agent._log_trade({"action": "entry", "contract": "B", "dry": False})
    assert agent.view_activity(limit=1)["rows"][0]["n"] == 0
    assert agent.view_activity()["total"] == 1 and len(reads) == 5


def test_a_failed_read_of_the_log_is_never_kept(home, monkeypatch):
    """A read that fails (a lock held by a backup tool, a disk error) shows
    the last good read and is tried again on the next ask — it is never
    kept as "the log is empty" until the next append."""
    monkeypatch.setattr(agent, "_VIEW_LOG", {"snap": (None, [], 0)})
    agent._log_trade({"action": "entry", "contract": "A", "dry": False})
    assert [r["contract"] for r in agent._view_rows()] == ["A"]
    agent._log_trade({"action": "entry", "contract": "B", "dry": False})
    real_open = open
    lock = {"on": True}

    def locked(path, *a, **k):
        if lock["on"] and str(path) == agent.TRADES_PATH:
            raise PermissionError("locked by another program")
        return real_open(path, *a, **k)
    monkeypatch.setattr(agent, "open", locked, raising=False)
    assert [r["contract"] for r in agent._view_rows()] == ["A"]  # last good
    assert agent.view_snapshot({"running": True})["limits"]["used_today"][
        "real"] == 1
    lock["on"] = False
    # the file did not change since: read again all the same
    assert [r["contract"] for r in agent._view_rows()] == ["A", "B"]


def test_a_failed_first_read_shows_nothing_and_keeps_nothing(home, monkeypatch):
    monkeypatch.setattr(agent, "_VIEW_LOG", {"snap": (None, [], 0)})
    agent._log_trade({"action": "entry", "contract": "A", "dry": False})

    def broken(strict=False):
        raise OSError("I/O error")
    real = agent._scan_trade_log
    monkeypatch.setattr(agent, "_scan_trade_log", broken)
    assert agent._view_rows() == []
    monkeypatch.setattr(agent, "_scan_trade_log", real)
    assert [r["contract"] for r in agent._view_rows()] == ["A"]


def test_rows_kept_in_memory_are_numbered_by_the_line_they_get(home, monkeypatch):
    """After a torn last line or blank lines, a row the agent could not
    write yet is numbered with the line it is later written to — the page
    keys rows by that number, so it must not show the row twice."""
    (home / "trades.jsonl").write_text(
        json.dumps({"action": "entry", "contract": "A", "dry": False,
                    "ts": "2026-11-09T15:00:00+00:00"})
        + "\n\n\n{\"action\": \"exi", encoding="utf-8")  # torn by a crash
    monkeypatch.setattr(agent, "_VIEW_LOG", {"snap": (None, [], 0)})
    monkeypatch.setattr(agent, "_UNWRITTEN", [])
    monkeypatch.setattr(agent, "_LOG_FAIL", {"day": datetime.now(
        timezone.utc).date().isoformat()})
    real = agent._append_trade_rows

    def full(path, rows):
        raise OSError("disk full")
    monkeypatch.setattr(agent, "_append_trade_rows", full)
    agent._log_trade({"action": "entry", "contract": "B", "dry": False})
    shown = {r["contract"]: r["n"] for r in agent._view_rows()}
    monkeypatch.setattr(agent, "_append_trade_rows", real)
    agent._log_trade({"action": "entry", "contract": "C", "dry": False})
    written = {r["contract"]: r["n"] for r in agent._read_trade_log()}
    assert written["B"] == shown["B"] == 4
    assert [r["n"] for r in agent._view_rows()] == [0, 4, 5]


# ── what the loop notes for the dashboard ───────────────────────────────────

def test_loop_notes_never_raise(monkeypatch):
    monkeypatch.setattr(agent, "LOOP", {})

    class Source:
        had_access = staticmethod(lambda: False)
    events = [{"occurred_at": "2026-11-09T19:10:07Z"}, {"occurred_at": "junk"},
              {"occurred_at": "2026-11-09T14:00:00"}, {}, "x", None]
    agent._loop_note_poll(Source, events, None)
    assert agent.LOOP["poll_ok"] is True and agent.LOOP["feed_access"] is False
    first = agent.LOOP["signal_at"]
    assert first == datetime(2026, 11, 9, 19, 10, 7,
                             tzinfo=timezone.utc).timestamp()
    # a later poll that lists nothing newer keeps it; a failed one says so
    agent._loop_note_poll(Source, [], OSError("down"))
    assert agent.LOOP["poll_ok"] is False and agent.LOOP["signal_at"] == first
    assert agent.LOOP["feed_access"] is None
    for bad in (None, 5, "events", [object()]):
        agent._loop_note_poll(object(), bad, None)
    monkeypatch.setattr(agent, "LOOP", None)   # even with nowhere to write
    agent._loop_note(beat_at=1.0)
    agent._loop_note_poll(Source, events, None)
    assert agent._newest_signal_time(None) is None


# ── from a running loop ─────────────────────────────────────────────────────

class Broker:
    """Buys and sells whatever it is told; no connection tracking."""

    def client(self, cfg):
        return object()

    def execute(self, client, cfg, ev, state):
        key = f"{ev['ticker']}|{ev['expiry']}|{ev['strike']}|{ev['type']}"
        if ev["event"] == "ENTERED":
            state["positions"][key] = {"option_id": "stub", "qty": 1}
        else:
            state["positions"].pop(key, None)
        return True

    def buying_power(self, client, cfg):
        return 1234.5


HANDED = {}  # what the last cmd_run handed its dashboard


def run_agent(home, monkeypatch, polls, config, during):
    """`agent.py run` for len(polls) loops; during(i, get_view, get_status)
    is called in the sleep after loop i."""
    import brokers
    import keep_running
    i = {"n": 0}
    got = {}

    class Source:
        @staticmethod
        def poll(cfg, st, save_state):
            out = polls[i["n"]]
            if isinstance(out, Exception):
                raise out
            return [dict(ev) for ev in out]

    def sleep(wait, controls):
        during(i["n"], got["view"], got["status"])
        if i["n"] + 1 >= len(polls):
            raise KeyboardInterrupt
        i["n"] += 1

    def dashboard(get_status, get_trades, apply_command, cfg_getter,
                  get_view=None, get_activity=None):
        got.update(status=get_status, view=get_view, activity=get_activity)
        HANDED.update(got)
        return "http://127.0.0.1:0/dash?k=test"

    class NoLock:
        def acquire(self, wait=0.0):
            return True
    monkeypatch.setattr(keep_running, "InstanceLock", NoLock)
    monkeypatch.setattr(agent, "require_consent_or_exit", lambda: None)
    monkeypatch.setattr(brokers, "broker_ready", lambda c: True)
    monkeypatch.setitem(agent.BROKERS, "stub", Broker())
    monkeypatch.setitem(agent.SOURCES, "stub", Source)
    monkeypatch.setattr(webui, "start_dashboard", dashboard)
    monkeypatch.setattr(keep_running, "refresh_autostart", lambda: False)
    monkeypatch.setattr(agent, "notify", lambda *a, **k: None)
    monkeypatch.setattr(agent, "_position_upkeep", lambda *a, **k: None)
    monkeypatch.setattr(agent, "_maybe_daily_digest", lambda *a, **k: None)
    monkeypatch.setattr(agent, "_dashboard_answers", lambda port: True)
    monkeypatch.setattr(agent, "_market_open_now", lambda: True)
    monkeypatch.setattr(agent, "_WATCH_SINCE", {})
    monkeypatch.setattr(agent, "LOOP", {})
    monkeypatch.setitem(webui.CONTROLS, "stop", False)
    monkeypatch.setitem(webui.CONTROLS, "hold_until", 0.0)
    monkeypatch.setattr(agent, "_sleep_unless_stopped", sleep)
    write(home, cfg={"source": "stub", "broker": "stub", **config})
    agent.cmd_run()


def event(kind="ENTERED", eid="ENTERED|1", **over):
    ev = {"event": kind, "ticker": "SPY", "expiry": "2031-07-10",
          "strike": 752.0, "type": "C", "event_id": eid, "track": "main",
          "occurred_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    ev.update(over)
    return ev


def test_running_loop_serves_a_live_view_model(home, monkeypatch, no_policy):
    seen, pages = [], []

    def got_activity():
        return HANDED["activity"](limit=1)

    def during(i, get_view, get_status):
        s = get_view()
        assert vm.validate(s) == [], vm.validate(s)
        seen.append((s, get_status()))
        pages.append(got_activity())

    run_agent(home, monkeypatch,
              [[event()], OSError("feed down"),
               [event(), event("EXITED", "EXITED|1")]],
              {"dry_run": False, "max_entries_per_day": 1}, during)
    (first, old), (second, _), (third, _) = seen
    # loop 1: a real buy
    assert first["agent"] == {**first["agent"], "running": True,
                              "answering": True}
    assert first["agent"]["last_heartbeat"] and first["agent"]["started_at"]
    assert first["agent"]["heartbeat_every_s"] is not None
    assert first["mode"] == "live" and first["pause"]["kind"] == "none"
    assert first["signals"]["last_poll_ok"] is True
    assert first["signals"]["last_signal_from"] == "feed"
    assert first["signals"]["reading"] is True
    assert first["broker"]["id"] == "stub" and first["broker"]["connected"] is None
    assert first["broker"]["buying_power"] == 1234.5
    assert first["real_positions"] == 1 and first["real_positions_managed"]
    pos = first["positions"][0]
    assert pos["real"] and pos["qty"] == 1 and pos["book"] == "main"
    assert pos["next"]["action"] == "sell_on_exit" and pos["opened_at"]
    row = first["activity"]["rows"][0]
    assert (row["action"], row["mode_at_time"], row["order_sent"],
            row["filled"], row["qty"]) == ("entry", "live", True, True, 1)
    assert first["limits"]["used_today"]["real"] == 1
    assert first["limits"]["cap_reached"] is True
    assert first["capabilities"]["opening"] == {
        "state": "not_today", "reason_code": "daily_cap_reached"}
    # the old status, from the same run, is what it was
    assert old["mode"] == "LIVE" and old["paused"] is False
    assert old["fields"]["entries today"] == "1 / cap 1"
    assert set(old) == {"mode", "paused", "hold", "pause_note", "restarting",
                        "real_positions", "dry_run_note", "fields",
                        "cash_warning", "expiry_warnings", "problems",
                        "budget_hints", "would_have"}
    # loop 2: the feed could not be read
    assert second["signals"]["last_poll_ok"] is False
    assert second["capabilities"]["listening"] == {
        "state": "cant", "reason_code": "feed_unreachable"}
    assert second["real_positions"] == 1
    # loop 3: the exit sold it
    assert third["positions"] == [] and third["signals"]["last_poll_ok"] is True
    assert [r["action"] for r in third["activity"]["rows"]] == ["exit", "entry"]
    assert third["results"]["real"]["unmeasured"] == 1  # the stub has no quotes
    assert [p["rows"][0]["action"] for p in pages] == ["entry", "entry", "exit"]
    assert all(vm.validate(p, "ActivityPage") == [] for p in pages)


def test_a_cycle_is_measured_without_the_last_cycles_sleep(home, monkeypatch,
                                                          no_policy):
    """While a cycle works, `answering` must not be stretched by the sleep
    before it: a cycle that hangs right after a 5-minute sleep would read as
    answering for 5 more minutes."""
    during_work = []

    class Peek(list):
        def __iter__(self):
            during_work.append(agent.LOOP.get("wait_s"))
            return super().__iter__()

    seen = []
    run_agent(home, monkeypatch, [Peek(), Peek()], {"dry_run": True},
              lambda i, view, status: seen.append(view()["agent"]))
    assert during_work == [None, None]          # cleared at each cycle's start
    assert all(a["heartbeat_every_s"] is not None for a in seen)  # the sleep
    # a status taken while a cycle works: measured against the work alone
    t = datetime(2026, 11, 9, 19, 41, 7, tzinfo=timezone.utc)
    hung = vm.build_status({}, {}, [], {
        "running": True, "heartbeat_at": t.timestamp() - 200, "wait_s": None},
        now=t)
    assert hung["agent"]["answering"] is False


def test_running_loop_reports_each_kind_of_pause(home, monkeypatch, no_policy):
    import transfer
    seen = {}

    def during(i, get_view, get_status):
        if i == 0:
            seen["entries"] = (get_view(), get_status())
            monkeypatch.setitem(agent._STATE_HELD, "on", True)
            seen["state"] = (get_view(), get_status())
            monkeypatch.setitem(transfer.HANDOFF, "hold", True)
            seen["handoff"] = (get_view(), get_status())
            monkeypatch.setitem(transfer.HANDOFF, "hold", False)
            monkeypatch.setitem(agent._STATE_HELD, "on", False)
            webui.hold_orders(60)
            seen["restart"] = (get_view(), get_status())
            webui.release_orders()
            monkeypatch.setitem(webui.CONTROLS, "stop", True)
            seen["stop"] = (get_view(), get_status())
            monkeypatch.setitem(webui.CONTROLS, "stop", False)

    run_agent(home, monkeypatch, [[event()]],
              {"dry_run": True, "paused": True}, during)
    for name, (view, _old) in seen.items():
        assert vm.validate(view) == [], name
        assert view["mode"] == "practice" and view["pause"]["entries_paused"]
    kinds = {name: view["pause"]["kind"] for name, (view, _o) in seen.items()}
    assert kinds == {"entries": "entries", "state": "state_hold",
                     "handoff": "handoff_hold", "restart": "restarting",
                     "stop": "stopped"}
    # the two routes agree on what is a full hold and what is the owner's pause
    for name, (view, old) in seen.items():
        if name == "stop":
            continue
        assert old["paused"] is True
        assert old["hold"] is (view["pause"]["kind"] in ("state_hold",
                                                         "handoff_hold")), name
        assert old["restarting"] is (view["pause"]["kind"] == "restarting"), name
    # the paused entry was logged as stopped at the pause check, in practice
    row = seen["entries"][0]["activity"]["rows"][0]
    assert (row["action"], row["stopped_at_check"], row["mode_at_time"]) == (
        "skip_paused", "not_paused", "practice")
    assert seen["entries"][0]["capabilities"]["opening"] == {
        "state": "paused", "reason_code": "owner_pause"}
