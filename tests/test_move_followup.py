"""Follow-up to audit PR5 (dashboard "Move computer").

1. The 10-minute limit of an unfinished move lifted the hold even after the
   move file had been DOWNLOADED. If the owner had already started the file
   on the new computer, both computers then sold the same real positions.
   A downloaded file now retires this computer when the limit runs out (as
   "I saved the file" would); a file that never left it still lifts the
   hold, as before."""

import agent
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
