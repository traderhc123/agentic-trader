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
        self.calls = []
        self.rows, self.notes = [], []

    def busy(self):
        self.calls.append("busy")
        return self.busy_why

    def check(self):
        self.calls.append("check")
        return dict(self.res)

    def candidates(self, check, shas):
        if self.snapshot:
            return [s for s in shas if s == check["target"]]
        return list(shas)

    def changes(self, sha, check):
        return "1111111", sha[:7], ["fix one thing", "fix another"]

    def pull(self, sha):
        self.calls.append(("pull", sha))
        return self.pull_answer

    def finish(self, note):
        self.calls.append("finish")

    def log(self, row):
        self.rows.append(row)

    def notify(self, text):
        self.notes.append(text)


def run(cfg, fake, home, when):
    return au.tick(cfg, fake, str(home), now=when.timestamp(), now_et=when)


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
    assert b.candidates(res, [v2]) == []  # not ahead of what is installed
    ok, msg = b.pull(v2)
    assert not ok and "fast-forward" in msg
    assert (install / "f.txt").read_text() == "local\n"
