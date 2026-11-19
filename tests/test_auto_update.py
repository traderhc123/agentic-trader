"""Automatic updates (auto_update.py): off by default; when on, installs
only at night or on a weekend/holiday, only when no order is being placed,
only a commit seen on the official branch for two hours, at most once a
night, through the Update button's own pull and restart.

The rules run on a fake backend with injected clocks (no wall clock, no
timezone, no network). The git side (an older commit of the official branch,
a fork's commit refused) runs against a throwaway local bare repo, like
tests/test_webui_updates.py."""

import json
import subprocess
from datetime import datetime

import pytest

import agent
import auto_update as au
import view_model as vm
import webui

from test_snapshot_update import (  # noqa: F401
    SHA_NEW, SHA_OLD, _Resp, _zip, install)
from test_v2_settings_api import (  # noqa: F401
    BASE, app, read_cfg, write_cfg)

ET = au.MARKET_TZ
A = "a" * 40
B = "b" * 40
ON = {"auto_update": True}


def et(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=ET)


# Thursday 2026-11-19 is a trading day; Thanksgiving 2026-11-26 is not.
THU_2030 = et(2026, 11, 19, 20, 30)


class Fake:
    """A backend that records what it was asked."""

    def __init__(self, target=A, behind=1, busy=None, pull=(True, "updated"),
                 snapshot=False):
        self.res = {"ok": True, "behind": behind, "target": target,
                    "upstream": "origin/main", "summary": ""}
        self.busy_why = busy
        self.pull_answer = pull
        self.snapshot = snapshot
        self.local = None                      # local_changes()
        self.verify_answer = (None, "")        # verify()
        self.rollback_answer = (True, "put back")
        self.held = False
        self.calls = []
        self.rows, self.notes = [], []

    def busy(self):
        self.calls.append("busy")
        return self.busy_why

    def hold(self):
        why = self.busy()  # like the real one: busy, then the hold
        if why:
            return why
        self.calls.append("hold")
        self.held = True
        return None

    def release(self):
        self.calls.append("release")
        self.held = False

    def check(self):
        self.calls.append("check")
        return dict(self.res)

    def local_changes(self):
        return self.local

    def candidates(self, check, shas):
        if self.snapshot:
            return [s for s in shas if s == check["target"]]
        return list(shas)

    def changes(self, sha, check):
        return "1111111", sha[:7], ["fix one thing", "fix another"]

    def prepare(self):
        return {"head": "1" * 40}

    def pull(self, sha):
        assert self.held, "the heartbeat is put on hold before any file changes"
        self.calls.append(("pull", sha))
        return self.pull_answer

    def verify(self, prep):
        self.calls.append("verify")
        return self.verify_answer

    def rollback(self, prep):
        self.calls.append("rollback")
        return self.rollback_answer

    def finish(self, note):
        self.calls.append("finish")

    def log(self, row):
        self.rows.append(row)

    def notify(self, text):
        self.notes.append(text)


def run(cfg, fake, home, when, **kw):
    return au.tick(cfg, fake, str(home), now=when.timestamp(), now_et=when, **kw)


def pulls(fake):
    return [c for c in fake.calls if isinstance(c, tuple)]


# ── when ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("when,ok", [
    (et(2026, 11, 19, 19, 59), False),   # trading day, before 20:00
    (et(2026, 11, 19, 20, 0), True),
    (et(2026, 11, 20, 7, 59), True),     # next morning, before 08:00
    (et(2026, 11, 20, 8, 0), False),
    (et(2026, 11, 19, 12, 0), False),    # market hours
    (et(2026, 11, 21, 12, 0), True),     # Saturday
    (et(2026, 11, 22, 15, 0), True),     # Sunday
    (et(2026, 11, 26, 12, 0), True),     # Thanksgiving
])
def test_window(when, ok):
    assert au.in_window({}, when) is ok


@pytest.mark.parametrize("when,ok", [
    (et(2026, 11, 20, 7, 29), True),     # trading day: starts until 07:30
    (et(2026, 11, 20, 7, 30), False),    # ... not in the last 30 minutes
    (et(2026, 11, 20, 7, 59), False),
    (et(2026, 11, 21, 7, 45), True),     # Saturday: no margin needed
    (et(2026, 11, 19, 20, 0), True),
    (et(2026, 11, 19, 19, 59), False),
])
def test_may_start_stops_before_the_trading_day(when, ok):
    assert au.may_start({}, when) is ok


def test_owner_holiday_counts():
    day = et(2026, 11, 18, 13, 0)  # a Wednesday
    assert not au.in_window({}, day)
    assert au.in_window({"market_holidays": ["2026-11-18"]}, day)


def test_one_night_runs_noon_to_noon():
    assert au.night_of(et(2026, 11, 19, 20, 0)) == "2026-11-19"
    assert au.night_of(et(2026, 11, 20, 7, 59)) == "2026-11-19"
    assert au.night_of(et(2026, 11, 20, 20, 0)) == "2026-11-20"


# ── off, and outside the window: nothing is asked ────────────────────────────

def test_off_by_default_does_nothing(tmp_path):
    f = Fake()
    for cfg in ({}, {"auto_update": False}, {"auto_update": "true"},
                {"auto_update": 1}):
        assert run(cfg, f, tmp_path, THU_2030) == "off"
    assert f.calls == [] and f.rows == [] and f.notes == []


def test_never_during_the_trading_day(tmp_path):
    f = Fake()
    for h in range(8, 20):
        assert run(ON, f, tmp_path, et(2026, 11, 19, h, 30)) == "outside"
    assert f.calls == []


# ── the two-hour wait, then one install ──────────────────────────────────────

def test_waits_two_hours_from_first_seen_then_installs_once(tmp_path):
    f = Fake()
    assert run(ON, f, tmp_path, THU_2030) == "waiting"
    assert pulls(f) == []
    st = au.load(str(tmp_path))
    assert st["seen"] == {A: THU_2030.timestamp()}
    assert "can install from" in st["note"]
    # checks at most every 30 minutes
    assert run(ON, f, tmp_path, et(2026, 11, 19, 20, 45)) == "not_due"
    # 1 h 59 min after it was first seen: still waiting
    assert run(ON, f, tmp_path, et(2026, 11, 19, 22, 29)) == "waiting"
    assert pulls(f) == []
    assert run(ON, f, tmp_path, et(2026, 11, 19, 23, 0)) == "installed"
    assert pulls(f) == [("pull", A)]
    assert f.calls[-1] == "finish"  # the Update button's restart, last
    # the activity row and the owner's alert say what was installed
    assert f.rows == [{"action": "code_update", "auto": True,
                       "from": "1111111", "to": A[:7],
                       "changes": ["fix one thing", "fix another"],
                       "reason": f"Updated itself from 1111111 to {A[:7]}: "
                                 "fix one thing; fix another"}]
    assert len(f.notes) == 1
    assert f"1111111 → {A[:7]}" in f.notes[0] and "• fix one thing" in f.notes[0]
    last = au.load(str(tmp_path))["last"]
    assert last["ok"] is True and last["to"] == A[:7]
    # one install a night: nothing more tonight, whatever is new
    f.res["target"] = B
    assert run(ON, f, tmp_path, et(2026, 11, 20, 3, 0)) == "done_tonight"
    assert len(pulls(f)) == 1


def test_the_wait_counts_from_what_this_agent_saw_not_the_commit_date(tmp_path):
    """A commit dated days ago but first seen now still waits two hours."""
    f = Fake()
    assert run(ON, f, tmp_path, et(2026, 11, 21, 10, 0)) == "waiting"  # Saturday
    assert run(ON, f, tmp_path, et(2026, 11, 21, 11, 59)) == "waiting"
    assert run(ON, f, tmp_path, et(2026, 11, 21, 12, 30)) == "installed"


def test_a_newer_push_does_not_reset_the_wait_for_git(tmp_path):
    """git: the older commit, seen two hours ago, installs even though a
    newer one arrived since (that one waits for its own two hours)."""
    f = Fake(target=A)
    run(ON, f, tmp_path, THU_2030)
    f.res["target"] = B
    assert run(ON, f, tmp_path, et(2026, 11, 19, 22, 0)) == "waiting"
    assert run(ON, f, tmp_path, et(2026, 11, 19, 22, 31)) == "installed"
    assert pulls(f) == [("pull", A)]


def test_zip_install_needs_the_newest_for_two_hours(tmp_path):
    f = Fake(target=A, snapshot=True)
    run(ON, f, tmp_path, THU_2030)
    f.res["target"] = B
    run(ON, f, tmp_path, et(2026, 11, 19, 22, 0))
    assert run(ON, f, tmp_path, et(2026, 11, 19, 22, 31)) == "waiting"
    assert run(ON, f, tmp_path, et(2026, 11, 20, 0, 1)) == "installed"
    assert pulls(f) == [("pull", B)]


def test_up_to_date_installs_nothing(tmp_path):
    f = Fake(behind=0)
    assert run(ON, f, tmp_path, THU_2030) == "current"
    assert pulls(f) == []


# ── orders and restarts come first ───────────────────────────────────────────

def test_waits_while_an_order_is_being_placed(tmp_path):
    f = Fake(busy="an order is being placed")
    run(ON, f, tmp_path, THU_2030)
    assert run(ON, f, tmp_path, et(2026, 11, 19, 23, 0)) == "busy"
    assert "check" not in f.calls and pulls(f) == []
    assert au.load(str(tmp_path))["note"] == "Waiting: an order is being placed."
    # the night is not used up: once the order is done it installs
    f.busy_why = None
    assert run(ON, f, tmp_path, et(2026, 11, 19, 23, 1)) == "waiting"
    assert run(ON, f, tmp_path, et(2026, 11, 20, 1, 2)) == "installed"


def test_an_order_that_starts_during_the_check_holds_the_install(tmp_path):
    f = Fake()
    run(ON, f, tmp_path, THU_2030)
    answers = iter([None, "an order is being placed"])
    f.busy = lambda: next(answers)
    assert run(ON, f, tmp_path, et(2026, 11, 19, 23, 0)) == "busy"
    assert pulls(f) == []
    f.busy = lambda: None
    # asked again on the next tick (not 30 minutes later)
    assert run(ON, f, tmp_path, et(2026, 11, 19, 23, 1)) == "installed"


# ── failures: shown, told once, tried again the next night ───────────────────

def test_failure_backs_off_to_the_next_night_and_is_told_once(tmp_path):
    msg = "cannot fast-forward (local changes or diverged history): x"
    f = Fake(pull=(False, msg))
    run(ON, f, tmp_path, THU_2030)
    assert run(ON, f, tmp_path, et(2026, 11, 19, 23, 0)) == "failed"
    assert f.rows == [] and "finish" not in f.calls
    assert len(f.notes) == 1 and msg in f.notes[0]
    s = au.status(ON, str(tmp_path), "git")
    assert s["last"]["ok"] is False and s["last"]["text"] == msg
    assert run(ON, f, tmp_path, et(2026, 11, 20, 5, 0)) == "done_tonight"
    # the next night: tried again; the same failure is not told again
    assert run(ON, f, tmp_path, et(2026, 11, 20, 21, 0)) == "failed"
    assert len(pulls(f)) == 2 and len(f.notes) == 1
    # it works the night after: told, and the error is gone
    f.pull_answer = (True, "updated")
    assert run(ON, f, tmp_path, et(2026, 11, 21, 21, 0)) == "installed"
    assert au.status(ON, str(tmp_path))["last"]["ok"] is True


def test_check_failure_is_shown_and_does_not_use_up_the_night(tmp_path):
    f = Fake()
    f.res = {"ok": False, "error": "GitHub unreachable"}
    assert run(ON, f, tmp_path, THU_2030) == "check_failed"
    assert "GitHub unreachable" in au.status(ON, str(tmp_path))["note"]
    assert au.load(str(tmp_path)).get("night") is None


def test_damaged_notes_file_starts_over(tmp_path):
    (tmp_path / au.FILE).write_text("{not json", encoding="utf-8")
    assert run(ON, Fake(), tmp_path, THU_2030) == "waiting"
    (tmp_path / au.FILE).write_text(json.dumps(
        {"seen": {"not-a-sha": 1, A: "x"}, "checked_at": "soon"}), encoding="utf-8")
    assert au.load(str(tmp_path))["seen"] == {}
    assert run(ON, Fake(), tmp_path, THU_2030) == "waiting"


def test_tick_never_raises(tmp_path):
    f = Fake()

    def boom():
        raise RuntimeError("x")
    f.check = boom
    assert run(ON, f, tmp_path, THU_2030) == "error"


def test_status_words():
    s = au.status({}, "/nonexistent-home", "git")
    assert s["on"] is False and s["last"] is None
    assert s["text"] == ("Installs official updates automatically between 8 PM "
                         "and 8 AM ET or on weekends, only when no order is "
                         "being placed. Off by default.")
    assert "newest version" in au.status(ON, "/nonexistent-home", "snapshot")["text"]


# ── the chat, the CLI, the settings route ────────────────────────────────────

def test_chat_toggle(home):
    agent._save(agent.CONFIG_PATH, {"source": "manual"})
    handled, reply = agent._apply_command("auto update on")
    assert handled and reply.startswith("Auto update: ON.")
    assert agent._load(agent.CONFIG_PATH)["auto_update"] is True
    handled, reply = agent._apply_command("auto update")
    assert handled and "ON" in reply
    agent._apply_command("auto update off")
    assert agent._load(agent.CONFIG_PATH)["auto_update"] is False


def test_cli(home, capsys):
    agent._save(agent.CONFIG_PATH, {"source": "manual"})
    agent.cmd_auto_update("on")
    assert agent._load(agent.CONFIG_PATH)["auto_update"] is True
    assert "Auto update: ON." in capsys.readouterr().out
    agent.cmd_auto_update("off")
    assert agent._load(agent.CONFIG_PATH)["auto_update"] is False
    agent.cmd_auto_update("status")
    assert "Auto update: off." in capsys.readouterr().out


def test_settings_route(home, app):
    write_cfg(home, BASE)
    c = app()
    s = c.get("/api/v2/settings")
    assert s["updates"]["on"] is False and "8 PM and 8 AM ET" in s["updates"]["text"]
    r = c.post("/api/v2/settings/updates", {"auto_update": True})
    assert r["ok"] and r["settings"]["updates"]["on"] is True
    assert read_cfg(home) == {**BASE, "auto_update": True}
    for bad in ({}, {"auto_update": "on"}, {"auto_update": 1},
                {"auto_update": True, "x": 1}):
        assert c.post("/api/v2/settings/updates", bad)["ok"] is False, bad
    assert read_cfg(home) == {**BASE, "auto_update": True}
    # the classic setup page reads it from the wizard state
    assert c.get("/api/state")["auto_update"]["on"] is True
    own = f"http://127.0.0.1:{c.port}"
    body = json.dumps({"auto_update": False})
    assert c.raw("POST", "/api/v2/settings/updates", body, login=False, origin=own)[0] == 401
    assert c.raw("POST", "/api/v2/settings/updates", body, host="evil.example", origin=own)[0] == 403
    assert c.raw("POST", "/api/v2/settings/updates", body, origin="http://evil.example")[0] == 403
    assert read_cfg(home)["auto_update"] is True


def test_activity_row_is_described_and_moves_no_money():
    a = vm.activity_row({"n": 7, "action": "code_update", "auto": True,
                         "from": "1111111", "to": "2222222",
                         "reason": "Updated itself from 1111111 to 2222222",
                         "ts": "2026-11-20T02:00:00+00:00"})
    assert a["known"] is True and a["reason_code"] == "code_updated"
    assert a["order_sent"] is False and a["real"] is None
    assert a["reason_text"] == "Updated itself from 1111111 to 2222222"


# ── the real backend: order lock, git ────────────────────────────────────────

def test_backend_busy_while_an_order_holds_the_lock(monkeypatch):
    import platform_compat
    import threading
    b = webui.auto_update_backend()
    monkeypatch.setitem(webui.CONTROLS, "hold_until", 0.0)
    monkeypatch.setitem(webui.CONTROLS, "stop", False)
    assert b.busy() is None
    got, done = threading.Event(), threading.Event()

    def order():
        with platform_compat.ORDER_LOCK:
            got.set()
            done.wait(5)
    t = threading.Thread(target=order)
    t.start()
    got.wait(5)
    try:
        assert b.busy() == "an order is being placed"
    finally:
        done.set()
        t.join()
    webui.hold_orders(60)
    try:
        assert b.busy() == "a restart is already under way"
    finally:
        webui.release_orders()


def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args],
                          capture_output=True, text=True, check=True).stdout.strip()


def _commit(repo, text, msg):
    (repo / "f.txt").write_text(text)
    _git(repo, "commit", "-qam", msg)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repos(tmp_path, monkeypatch):
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)],
                   capture_output=True, check=True)
    author = tmp_path / "author"
    subprocess.run(["git", "clone", "-q", str(origin), str(author)],
                   capture_output=True, check=True)
    _git(author, "config", "user.email", "t@t")
    _git(author, "config", "user.name", "t")
    (author / "f.txt").write_text("v1\n")
    _git(author, "add", "f.txt")
    _git(author, "commit", "-qm", "v1")
    _git(author, "push", "-q", "origin", "main")
    install = tmp_path / "install"
    subprocess.run(["git", "clone", "-q", str(origin), str(install)],
                   capture_output=True, check=True)
    monkeypatch.setattr(webui, "_repo_dir", lambda: str(install))
    monkeypatch.setenv("AGENTIC_TRADER_UPDATE_REMOTE", str(origin))
    webui._forget_update_check()
    return author, install


def test_git_installs_an_older_commit_of_the_official_branch(repos):
    author, install = repos
    v2 = _commit(author, "v2\n", "v2: first fix")
    v3 = _commit(author, "v3\n", "v3: second fix")
    _git(author, "push", "-q", "origin", "main")
    b = webui.auto_update_backend()
    res = b.check()
    assert res["ok"] and res["target"] == v3
    assert b.candidates(res, [v3, v2]) == [v3, v2]
    old, new, subjects = b.changes(v2, res)
    assert new == v2[:7] and subjects == ["v2: first fix"]
    assert old == _git(install, "rev-parse", "--short=7", "HEAD")
    ok, msg = b.pull(v2)
    assert ok, msg
    assert (install / "f.txt").read_text() == "v2\n"
    # the Update button still installs only the head it showed
    webui._forget_update_check()
    ok, msg = webui.pull_code_updates(expect=v2)
    assert not ok and "remote changed" in msg


def test_git_refuses_a_commit_not_on_the_official_branch(repos):
    author, install = repos
    _commit(author, "v2\n", "v2")
    _git(author, "push", "-q", "origin", "main")
    # a commit that exists locally but was never on origin/main
    _git(author, "checkout", "-qb", "fork")
    fork = _commit(author, "evil\n", "evil")
    _git(author, "push", "-q", "origin", "fork")
    b = webui.auto_update_backend()
    res = b.check()
    assert b.candidates(res, [fork]) == []
    ok, msg = b.pull(fork)
    assert not ok and "remote changed" in msg
    assert (install / "f.txt").read_text() == "v1\n"


def test_git_refuses_local_changes_like_the_update_button(repos):
    author, install = repos
    v2 = _commit(author, "v2\n", "v2")
    _git(author, "push", "-q", "origin", "main")
    _git(install, "config", "user.email", "t@t")
    _git(install, "config", "user.name", "t")
    _commit(install, "local\n", "my own change")
    b = webui.auto_update_backend()
    res = b.check()
    # kept as a candidate, so the refusal is shown and told (not "waiting")
    assert b.candidates(res, [v2]) == [v2]
    assert "commits of its own" in b.local_changes()
    ok, msg = b.pull(v2)
    assert not ok and "fast-forward" in msg
    assert (install / "f.txt").read_text() == "local\n"


# ── after the review: checks before the restart, put back, local changes ─────

LATER = et(2026, 11, 19, 23, 0)  # two and a half hours after THU_2030


def _cooled(f, home):
    """A first tick that sees A, so it may install two hours later."""
    assert run(ON, f, home, THU_2030) == "waiting"


def test_heartbeat_is_held_before_the_pull_and_handed_to_the_restart(tmp_path):
    f = Fake()
    _cooled(f, tmp_path)
    assert run(ON, f, tmp_path, LATER) == "installed"
    # Fake.pull asserts the hold; the restart (finish) takes it over
    assert f.held and "release" not in f.calls
    assert f.calls.index("hold") < f.calls.index(("pull", A))
    assert f.calls.index("verify") < f.calls.index("finish")


def test_new_code_that_does_not_load_is_put_back_and_not_announced(tmp_path):
    f = Fake()
    _cooled(f, tmp_path)
    f.verify_answer = ("the new code does not load — agent: ImportError: x", "")
    assert run(ON, f, tmp_path, LATER) == "failed"
    assert "rollback" in f.calls and "finish" not in f.calls
    assert f.rows == []  # no "Updated itself" row
    assert len(f.notes) == 1 and "updated itself" not in f.notes[0]
    assert "previous version (1111111) was put back" in f.notes[0]
    assert not f.held and f.calls[-1] == "release"
    last = au.status(ON, str(tmp_path))["last"]
    assert last["ok"] is False and "ImportError" in last["text"]


def test_a_failed_put_back_is_told_plainly(tmp_path):
    f = Fake()
    _cooled(f, tmp_path)
    f.verify_answer = ("the new code does not load — x", "")
    f.rollback_answer = (False, "disk full")
    assert run(ON, f, tmp_path, LATER) == "failed"
    assert "could NOT be put back (disk full)" in f.notes[0]
    assert "Nothing was changed" not in f.notes[0]


def test_an_error_after_the_pull_puts_the_previous_version_back(tmp_path):
    f = Fake()
    _cooled(f, tmp_path)

    def boom(prep):
        raise OSError("pip went away")
    f.verify = boom
    assert run(ON, f, tmp_path, LATER) == "failed"
    assert "rollback" in f.calls and f.rows == [] and not f.held
    assert "pip went away" in au.status(ON, str(tmp_path))["last"]["text"]


def test_an_error_before_the_pull_is_shown_and_told(tmp_path):
    f = Fake()
    _cooled(f, tmp_path)

    def boom(sha, check):
        raise subprocess.TimeoutExpired("git", 60)
    f.changes = boom
    assert run(ON, f, tmp_path, LATER) == "failed"
    assert pulls(f) == [] and "rollback" not in f.calls and not f.held
    st = au.load(str(tmp_path))
    assert st["night"] == "2026-11-19" and st["note"] is None
    assert st["last"]["ok"] is False and "stopped part-way" in st["last"]["text"]
    assert len(f.notes) == 1


def test_local_changes_are_refused_shown_and_told_once(tmp_path):
    f = Fake()
    _cooled(f, tmp_path)
    f.local = ("this install has local changes (rules.py); automatic "
               "updates never install over them")
    assert run(ON, f, tmp_path, LATER) == "failed"
    assert pulls(f) == [] and not f.held and len(f.notes) == 1
    assert "local changes" in au.status(ON, str(tmp_path))["last"]["text"]
    assert run(ON, f, tmp_path, et(2026, 11, 20, 21, 0)) == "failed"
    assert pulls(f) == [] and len(f.notes) == 1  # the same refusal: told once


def test_the_window_is_asked_again_right_before_the_install(tmp_path):
    """A tick that starts in time but whose check runs past 08:00 on a
    trading day installs nothing."""
    f = Fake()
    assert run(ON, f, tmp_path, et(2026, 11, 20, 5, 0)) == "waiting"  # Friday
    late = et(2026, 11, 20, 8, 1).timestamp()
    assert run(ON, f, tmp_path, et(2026, 11, 20, 7, 20),
               clock=lambda: late) == "outside"
    assert pulls(f) == [] and "release" in f.calls and not f.held
    assert au.load(str(tmp_path)).get("night") is None  # not used up


def test_no_install_when_the_night_cannot_be_written(tmp_path, monkeypatch):
    f = Fake()
    _cooled(f, tmp_path)
    real = au.save
    monkeypatch.setattr(au, "save", lambda home, st: False if st.get("night")
                        else real(home, st))
    mem = {}
    assert run(ON, f, tmp_path, LATER, mem=mem) == "save_failed"
    assert pulls(f) == [] and not f.held
    # remembered in memory: not tried again tonight, nothing told
    assert run(ON, f, tmp_path, et(2026, 11, 19, 23, 1), mem=mem) == "done_tonight"
    assert f.notes == []


def test_an_unwritable_notes_file_does_not_mean_a_check_every_minute(tmp_path, monkeypatch):
    monkeypatch.setattr(au, "save", lambda home, st: False)
    f = Fake()
    mem = {}
    assert run(ON, f, tmp_path, THU_2030, mem=mem) == "waiting"
    for m in range(31, 60):
        assert run(ON, f, tmp_path, et(2026, 11, 19, 20, m), mem=mem) == "not_due"
    assert f.calls.count("check") == 1


def test_a_check_that_raises_still_waits_before_the_next(tmp_path):
    f = Fake()
    _cooled(f, tmp_path)

    def boom(check, shas):
        raise subprocess.TimeoutExpired("git", 60)
    f.candidates = boom
    assert run(ON, f, tmp_path, LATER) == "error"
    assert run(ON, f, tmp_path, et(2026, 11, 19, 23, 1)) == "not_due"
    assert f.calls.count("check") == 2


def test_describe_words_the_last_install_once_in_eastern_time(tmp_path):
    f = Fake()
    _cooled(f, tmp_path)
    assert run(ON, f, tmp_path, LATER) == "installed"
    assert au.status(ON, str(tmp_path))["last"]["at_et"] == "Thu 2026-11-19 23:00 ET"
    line = au.describe(ON, str(tmp_path), "git")
    assert "installed Installed" not in line
    assert ("Last automatic update (Thu 2026-11-19 23:00 ET): "
            f"Installed 1111111 → {A[:7]}.") in line


def test_chat_question_shows_the_status(home):
    agent._save(agent.CONFIG_PATH, {"source": "manual"})
    for q in ("auto update?", "automatic updates?", "auto-update ?"):
        handled, reply = agent._apply_command(q)
        assert handled and reply.startswith("Auto update: off."), q
    assert "auto_update" not in agent._load(agent.CONFIG_PATH)


def test_cli_status_prints_on_a_cp1252_console(home, monkeypatch):
    import io
    import sys
    agent._save(agent.CONFIG_PATH, {"source": "manual", "auto_update": True})
    au.save(str(home), {"last": {"at": "2026-11-20T04:00:00+00:00", "ok": True,
                                 "text": "Installed 1111111 → 2222222"}})
    buf = io.BytesIO()
    out = io.TextIOWrapper(buf, encoding="cp1252")
    monkeypatch.setattr(sys, "stdout", out)
    agent.cmd_auto_update("status")
    out.flush()
    text = buf.getvalue().decode("cp1252")
    assert "Installed 1111111 ? 2222222" in text and "Change:" in text


def test_backend_hold_puts_the_heartbeat_on_hold(monkeypatch):
    import platform_compat
    import threading
    b = webui.auto_update_backend()
    monkeypatch.setitem(webui.CONTROLS, "hold_until", 0.0)
    monkeypatch.setitem(webui.CONTROLS, "stop", False)
    assert b.hold() is None and webui.orders_held()
    b.release()
    assert not webui.orders_held()
    # an order that holds the lock: no hold is left behind
    got, done = threading.Event(), threading.Event()

    def order():
        with platform_compat.ORDER_LOCK:
            got.set()
            done.wait(5)
    t = threading.Thread(target=order)
    t.start()
    got.wait(5)
    try:
        assert b.hold() == "an order is being placed"
        assert not webui.orders_held()
    finally:
        done.set()
        t.join()


def test_git_refuses_uncommitted_edits_a_fast_forward_would_carry(repos):
    """--ff-only keeps an edited file the update doesn't touch: the Update
    button allows that, an automatic update does not."""
    author, install = repos
    (author / "g.txt").write_text("g\n")
    _git(author, "add", "g.txt")
    _git(author, "commit", "-qm", "g")
    _git(author, "push", "-q", "origin", "main")
    b = webui.auto_update_backend()
    assert b.check()["ok"]
    assert b.local_changes() is None
    (install / "f.txt").write_text("my edit\n")
    assert "local changes (f.txt)" in b.local_changes()
    (install / "f.txt").write_text("v1\n")
    # an untracked venv or log is fine; an untracked Python file is not
    (install / ".venv").mkdir()
    (install / ".venv" / "x.py").write_text("")
    (install / "agent.log").write_text("x")
    assert b.local_changes() is None
    (install / "mine.py").write_text("x = 1\n")
    assert "mine.py" in b.local_changes()


def test_a_diverged_install_is_a_told_failure_not_endless_waiting(repos, tmp_path, monkeypatch):
    author, install = repos
    v2 = _commit(author, "v2\n", "v2")
    _git(author, "push", "-q", "origin", "main")
    _git(install, "config", "user.email", "t@t")
    _git(install, "config", "user.name", "t")
    _commit(install, "local\n", "my own change")
    h = tmp_path / "h"
    au.save(str(h), {"seen": {v2: THU_2030.timestamp() - au.COOL_OFF_S}})
    monkeypatch.setitem(webui.CONTROLS, "hold_until", 0.0)
    monkeypatch.setitem(webui.CONTROLS, "stop", False)
    b = webui.auto_update_backend()
    told = []
    monkeypatch.setattr(b, "notify", told.append)
    assert au.tick(ON, b, str(h), now=THU_2030.timestamp(), now_et=THU_2030) == "failed"
    last = au.status(ON, str(h))["last"]
    assert last["ok"] is False and "commits of its own" in last["text"]
    assert len(told) == 1 and not webui.orders_held()
    assert (install / "f.txt").read_text() == "local\n"


def test_git_rollback_puts_the_previous_commit_back(repos, monkeypatch):
    author, install = repos
    v1 = _git(install, "rev-parse", "HEAD")
    v2 = _commit(author, "v2\n", "v2: breaks the import")
    _git(author, "push", "-q", "origin", "main")
    b = webui.auto_update_backend()
    assert b.check()["ok"]
    prep = b.prepare()
    assert prep["head"] == v1 and prep["snapshot"] is False
    assert b.pull(v2)[0]
    asked = []
    monkeypatch.setattr(b, "_import_problem", lambda paths: asked.append(paths)
                        or "the new code does not load — agent: ImportError: x")
    problem, note = b.verify(prep)
    assert "does not load" in problem and asked == [["f.txt"]] and note == ""
    ok, msg = b.rollback(prep)
    assert ok, msg
    assert _git(install, "rev-parse", "HEAD") == v1
    assert (install / "f.txt").read_text() == "v1\n"


def test_snapshot_undo_puts_every_file_back(install, monkeypatch):
    import os
    import snapshot_update as su
    (install / su.VERSION_FILE).write_text(SHA_OLD)
    monkeypatch.setattr(su, "_get", lambda url, **k: _Resp(content=_zip(
        {"agent.py": "new agent\n", "keep.py": "same\n", "sub/new.py": "x\n"})))
    ok, note = su.pull(str(install), SHA_NEW)
    assert ok, note
    assert sorted(su.last_changed(str(install))) == sorted(
        ["agent.py", os.path.join("sub", "new.py")])
    ok, msg = su.undo_last(str(install))
    assert ok, msg
    assert (install / "agent.py").read_bytes() == b"old agent\n"
    assert not (install / "sub" / "new.py").exists()
    assert (install / su.VERSION_FILE).read_text().strip() == SHA_OLD
    assert su.undo_last(str(install))[0] is False  # once


def test_snapshot_self_edits_since_the_install_are_local_changes(tmp_path, monkeypatch):
    import os
    import snapshot_update as su
    repo, h = tmp_path / "r", tmp_path / "h"
    repo.mkdir()
    assert not su.self_edited_since_install(str(repo), str(h))
    edit = h / "backups" / "20261119-010000"
    edit.mkdir(parents=True)
    assert su.self_edited_since_install(str(repo), str(h))  # version unknown
    version = repo / su.VERSION_FILE
    version.write_text(SHA_NEW)
    os.utime(edit, (1000, 1000))
    os.utime(version, (2000, 2000))
    assert not su.self_edited_since_install(str(repo), str(h))
    os.utime(edit, (3000, 3000))
    assert su.self_edited_since_install(str(repo), str(h))
    monkeypatch.setattr(webui, "_snapshot_mode", lambda: True)
    monkeypatch.setattr(webui, "_repo_dir", lambda: str(repo))
    monkeypatch.setattr(webui, "_agent_home", lambda: str(h))
    assert "self-edits" in webui.auto_update_backend().local_changes()
