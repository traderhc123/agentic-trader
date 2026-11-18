"""Follow-up to audit PR5 (dashboard "Move computer").

1. The 10-minute limit of an unfinished move lifted the hold even after the
   move file had been DOWNLOADED. If the owner had already started the file
   on the new computer, both computers then sold the same real positions.
   A downloaded file now retires this computer when the limit runs out (as
   "I saved the file" would); a file that never left it still lifts the
   hold, as before.
2. With no agent in the page's process (the setup wizard), a move file was
   "current" no matter what: an agent started and stopped elsewhere after
   the file was made could have traded. Now the memory and the trade log
   must be unchanged since the file was made."""

import os
import time

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


def _told(monkeypatch):
    told = []
    monkeypatch.setattr(agent, "notify",
                        lambda cfg, msg, *a, **k: told.append(msg))
    return told


def test_a_downloaded_unfinished_move_retires_this_computer(mover,
                                                            monkeypatch):
    timers = _Timers()
    monkeypatch.setattr(webui.threading, "Timer", timers)
    told = _told(monkeypatch)
    routes = mover()
    assert routes["/api/move/export"](None, {"passphrase": PW})["ok"]
    assert routes["/api/move/download"](None).body   # the file has left
    timers.fire()                                    # 10 minutes later
    # the new computer may run the file and sell its positions: this one
    # never trades again by itself, also not after a restart
    assert transfer.HANDOFF["hold"] is True
    assert transfer.moved_away()
    assert agent._full_hold_note()
    assert told and "retired" in told[0] and "resume-here" in told[0]
    # what the owner's chat "resume" says: not here
    handled, reply = agent._apply_command("resume")
    assert handled and "resume-here" in reply
    assert transfer.HANDOFF["hold"] is True
    # the old file is gone: nothing to download or finish with any more
    r = routes["/api/move/download"](None)
    assert isinstance(r, dict) and r["ok"] is False


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
