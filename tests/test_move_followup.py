"""Follow-up to audit PR5 (dashboard "Move computer").

1. The 10-minute limit of an unfinished move lifted the hold even after the
   move file had been DOWNLOADED. If the owner had already started the file
   on the new computer, both computers then sold the same real positions.
   A downloaded file now retires this computer when the limit runs out (as
   "I saved the file" would); a file that never left it still lifts the
   hold, as before. (Later: a downloaded file stops ENTRIES for good, but
   exits keep running, see the exits-only tests below.)
2. With no agent in the page's process (the setup wizard), a move file was
   "current" no matter what: an agent started and stopped elsewhere after
   the file was made could have traded. Now the memory and the trade log
   must be unchanged since the file was made."""

import os
import time

import pytest

import agent
import keep_running
import transfer
import webui
from test_audit_pr5 import PW, mover  # noqa: F401  (fixture)


class _Timers:
    """threading.Timer that never runs by itself: the test fires it."""

    def __init__(self):
        self.made = []

    def __call__(self, s, fn, args=()):
        made = self.made

        class T:
            daemon = True

            def start(self):
                made.append((fn, args))
        return T()

    def fire(self):
        fn, args = self.made[-1]
        fn(*args)


@pytest.fixture(autouse=True)
def _no_exits_only_left_over(monkeypatch):
    monkeypatch.setitem(agent._EXITS_ONLY_RUN, "on", False)


def _told(monkeypatch):
    told = []
    monkeypatch.setattr(agent, "notify",
                        lambda cfg, msg, *a, **k: told.append(msg))
    return told


def test_a_downloaded_unfinished_move_stops_entries_but_keeps_exits(
        mover, monkeypatch):
    """The file may run on the other computer now, or it may never be
    imported: this one takes no new entries (for good, also after a
    restart) but keeps selling what it holds, so no real position is ever
    left with no computer to sell it."""
    timers = _Timers()
    monkeypatch.setattr(webui.threading, "Timer", timers)
    told = _told(monkeypatch)
    routes = mover()
    assert routes["/api/move/export"](None, {"passphrase": PW})["ok"]
    assert routes["/api/move/download"](None).body   # the file has left
    timers.fire()                                    # 10 minutes later
    # exits, the expiry guard and the holdings check run again
    assert transfer.HANDOFF["hold"] is False and not agent._full_hold_note()
    assert transfer.moved_away() is None
    # ... but no new entries, and a restart (a new run) keeps it so
    assert agent._paused({})
    monkeypatch.setitem(agent._EXITS_ONLY_RUN, "on", False)
    assert transfer.exits_only() is not None and agent._paused({})
    assert told and "NO new entries" in told[0] and "resume-here" in told[0]
    # the owner's Resume can't undo it; resume-here can
    handled, reply = agent._apply_command("resume")
    assert handled and "resume-here" in reply and agent._paused({})
    # the old file is gone: nothing to download or finish with any more
    r = routes["/api/move/download"](None)
    assert isinstance(r, dict) and r["ok"] is False
    assert transfer.resume_here() is True
    assert transfer.exits_only() is None and not agent._paused({})


def test_exits_only_never_travels_in_a_move_file(home):
    transfer.mark_exits_only()
    assert not transfer._travels("MOVE_EXITS_ONLY")


def test_a_file_never_downloaded_still_lifts_the_hold(mover, monkeypatch):
    timers = _Timers()
    monkeypatch.setattr(webui.threading, "Timer", timers)
    told = _told(monkeypatch)
    routes = mover()
    assert routes["/api/move/export"](None, {"passphrase": PW})["ok"]
    timers.fire()
    assert transfer.HANDOFF["hold"] is False     # nobody else can have it
    assert transfer.moved_away() is None
    assert told and "out of date" in told[0]


def test_a_new_export_forgets_the_earlier_download(mover, monkeypatch):
    """Downloaded, then "Create move file" again (not downloaded): the
    second file never left this computer, so its limit lifts the hold."""
    timers = _Timers()
    monkeypatch.setattr(webui.threading, "Timer", timers)
    _told(monkeypatch)
    routes = mover()
    assert routes["/api/move/export"](None, {"passphrase": PW})["ok"]
    assert routes["/api/move/download"](None).body
    assert routes["/api/move/export"](None, {"passphrase": PW})["ok"]
    timers.fire()
    assert transfer.HANDOFF["hold"] is False
    assert transfer.moved_away() is None


def test_finish_before_the_limit_is_unchanged(mover, monkeypatch):
    timers = _Timers()
    monkeypatch.setattr(webui.threading, "Timer", timers)
    told = _told(monkeypatch)
    routes = mover()
    assert routes["/api/move/export"](None, {"passphrase": PW})["ok"]
    assert routes["/api/move/download"](None).body
    assert routes["/api/move/finish"](None, {})["ok"] is True
    timers.fire()                                  # nothing left to do
    assert transfer.HANDOFF["hold"] is True and transfer.moved_away()
    assert told == []


def _wizard(mover, monkeypatch):
    routes = mover(running=False)
    monkeypatch.setattr(keep_running, "supervisor_running", lambda: False)
    assert routes["/api/move/export"](None, {"passphrase": PW})["ok"]
    return routes


def test_wizard_file_is_out_of_date_after_an_agent_ran_elsewhere(
        mover, home, monkeypatch):
    """No agent in this process: one started and stopped again since the
    file was made (it traded, or just saved its memory) leaves the file out
    of date — this computer is not retired on it."""
    routes = _wizard(mover, monkeypatch)
    with open(home / "trades.jsonl", "a", encoding="utf-8") as f:
        f.write('{"action": "entry", "dry": false}\n')
    r = routes["/api/move/finish"](None, {})
    assert r["ok"] is False and "out of date" in r["error"]
    assert transfer.moved_away() is None
    d = routes["/api/move/download"](None)
    assert isinstance(d, dict) and d["ok"] is False


def test_wizard_file_with_a_changed_memory_is_out_of_date(mover, home,
                                                         monkeypatch):
    routes = _wizard(mover, monkeypatch)
    st = os.stat(home / "state.json")
    agent._save(agent.STATE_PATH, {"seen": ["e-9"], "positions": {}},
                private=True)
    os.utime(home / "state.json", ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
    r = routes["/api/move/finish"](None, {})
    assert r["ok"] is False and transfer.moved_away() is None


def test_wizard_file_nothing_ran_since_finishes(mover, monkeypatch):
    routes = _wizard(mover, monkeypatch)
    time.sleep(0.01)
    assert routes["/api/move/download"](None).body
    assert routes["/api/move/finish"](None, {})["ok"] is True
    assert transfer.moved_away()


def test_activity_marks_see_a_missing_log(home):
    marks = transfer.activity_marks()
    assert set(marks) == {"state.json", "trades.jsonl"}
    (home / "trades.jsonl").write_text("x\n", encoding="utf-8")
    assert transfer.activity_marks() != marks


def test_an_import_clears_the_staged_files_a_killed_import_left(tmp_path,
                                                               monkeypatch):
    """A killed import left `*.import.tmp` copies of the move file's files
    next to their places; the next import removes them. The copies of this
    computer's own files (`*.import-bak.tmp`) stay: after such a crash they
    can be the only copy."""
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    new.mkdir()
    (old / "config.json").write_text(
        '{"source": "agenthc", "broker": "alpaca", "dry_run": false}',
        encoding="utf-8")
    (old / "state.json").write_text('{"positions": {}}', encoding="utf-8")
    monkeypatch.setenv("AGENT_HOME", str(old))
    blob = transfer.export_bundle(PW)
    monkeypatch.setenv("AGENT_HOME", str(new))
    (new / "sub").mkdir()
    for n in ("state.json.import.tmp", "sub/x.json.import.tmp",
              "notes.json.import.tmp"):
        (new / n).write_text("half", encoding="utf-8")
    (new / "state.json.import-bak.tmp").write_text("mine", encoding="utf-8")
    transfer.import_bundle(blob, PW)
    left = sorted(str(p.relative_to(new)).replace(os.sep, "/")
                  for p in new.rglob("*.tmp"))
    assert left == ["state.json.import-bak.tmp"]
    assert (new / "state.json").read_text(encoding="utf-8") == \
        '{"positions": {}}'


# ── exits-only: the other computer may already have sold ────────────────────

class _OtherComputerSold:
    """Alpaca for an account whose contracts the OTHER computer has already
    sold: no position, and a filled sell_to_close this computer never
    placed. Records every order this computer sends."""

    def __init__(self, monkeypatch, occ, sold_at):
        from brokers import alpaca
        self.occ, self.posts = occ, []
        self.fill = {"id": "other-pc-1", "symbol": occ, "side": "sell",
                     "position_intent": "sell_to_close", "status": "filled",
                     "filled_qty": "2", "filled_avg_price": "1.80",
                     "filled_at": sold_at}
        monkeypatch.setattr(alpaca.requests, "post", self.post)
        monkeypatch.setattr(alpaca.requests, "get", self.get)
        monkeypatch.setattr(alpaca.requests, "delete",
                            lambda url, **kw: _Resp(204, {}))

    def post(self, url, **kw):
        self.posts.append(dict(kw.get("json") or {}))
        return _Resp(200, {"id": "this-pc-1", "status": "filled"})

    def get(self, url, **kw):
        if url.endswith("/v2/positions"):
            return _Resp(200, [])                      # nothing held
        if url.endswith("/v2/orders"):
            return _Resp(200, [dict(self.fill)])
        if "quotes/latest" in url:
            return _Resp(200, {"quotes": {self.occ: {"ap": 1.9, "bp": 1.8}}})
        return _Resp(404, {})


class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body
        self.text = str(body)

    def json(self):
        return self._body


def test_exits_only_never_sells_what_the_other_computer_already_sold(
        home, monkeypatch):
    """Downloaded, unfinished move: the other computer imported the file and
    sold the position; this one gets the exit signal too. It reads the
    holdings first, so it places no order (no sell to open), and its
    holdings check then books the row as sold outside it."""
    from datetime import datetime, timezone

    import broker_events
    from brokers import account_tag, alpaca
    from conftest import make_event
    clock = {"t": datetime(2026, 10, 30, 15, 0, tzinfo=timezone.utc).timestamp()}
    monkeypatch.setattr(agent, "_now", lambda: clock["t"])
    monkeypatch.setattr(agent, "_et_now", lambda: datetime(
        2026, 10, 30, 11, 0, tzinfo=agent.MARKET_TZ))
    monkeypatch.setattr(agent, "_market_open_now", lambda: True)
    monkeypatch.setattr(agent, "_in_open_rush", lambda: False)
    monkeypatch.setitem(agent._POS_SYNC, "next_at", 0.0)
    monkeypatch.setattr(agent, "notify", lambda *a, **k: None)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    broker_events.begin()
    cfg = {"dry_run": False, "broker": "alpaca", "alpaca_key_id": "k",
           "alpaca_secret": "s", "contracts_per_trade": 2, "source": "agenthc"}
    occ, key = "SPY991218C00752000", "SPY|2099-12-18|752.0|C"
    tag = account_tag(cfg)
    api = _OtherComputerSold(monkeypatch, occ, "2026-10-30T14:58:00Z")
    transfer.mark_exits_only()               # the move's 10 minutes ran out
    st = {"seen": [], "positions": {key: {
        "option_id": occ, "qty": 2, "opened_event": "open-1", "acct": tag,
        "entry_mark": 1.5, "opened_at": clock["t"] - 3600,
        "last_order_at": clock["t"] - 3600, "order_ids": ["this-pc-0"]}},
        "position_sync": {"proven": {tag: "2026-10-29T15:00:00+00:00"}}}
    cl = alpaca.client(cfg)
    # a new signal: no entry here any more
    agent.handle_event(make_event(ticker="QQQ", expiry="2099-12-18",
                                  event_id="new-1"), cfg, st, alpaca, cl,
                       lambda s: None)
    assert "unfinished move" in agent.handle_event(
        make_event(ticker="QQQ", expiry="2099-12-18", event_id="new-2"), cfg,
        st, alpaca, cl, lambda s: None)
    # the exit signal the other computer already acted on: the holdings are
    # read before the sell — none held, so no order; the signal is kept
    exit_ev = make_event(event="EXITED", expiry="2099-12-18", event_id="x-1")
    with pytest.raises(agent.ExitNotDone):
        agent.handle_event(exit_ev, cfg, st, alpaca, cl, lambda s: None)
    assert api.posts == []                   # no buy, and no second sell
    # the holdings check: two reads apart, then the row is settled as sold
    agent._sync_positions(cfg, st, lambda s: None, alpaca, cl)
    clock["t"] += 60
    agent._sync_positions(cfg, st, lambda s: None, alpaca, cl)
    assert key not in st["positions"]
    # the kept signal, tried again: nothing left to sell, still no order
    agent.handle_event(exit_ev, cfg, st, alpaca, cl, lambda s: None)
    assert api.posts == []
    rows = [r for r in agent._read_trade_log() if r.get("manual")]
    assert rows and rows[-1]["action"] == "exit" and rows[-1]["cause"] == "sold"
