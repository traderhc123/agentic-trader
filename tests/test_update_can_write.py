"""The Update button on a server (systemd): the service must be allowed to
write its own install folder, and when it is not — every server made before
this fix — the owner gets the reason and the one command that fixes it,
instead of a raw git/pip error or nothing at all."""

import errno
import os
import tempfile

import snapshot_update as su
import webui

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHA = "c" * 40


def _unit():
    with open(os.path.join(REPO, "deploy", "agentic-trader.service"),
              encoding="utf-8") as f:
        lines = [ln.strip() for ln in f if "=" in ln and not ln.startswith("#")]
    return dict(ln.split("=", 1) for ln in lines)


def test_service_may_write_its_install_folder_and_state():
    unit = _unit()
    assert unit["ProtectSystem"] == "strict"   # everything else stays read-only
    writable = unit["ReadWritePaths"].split()
    # the code + its .venv (git fetch, pip install, self-edit) and the state
    assert unit["WorkingDirectory"] in writable
    assert unit["ExecStart"].startswith(unit["WorkingDirectory"] + "/.venv/")
    assert "/home/trader/.agentic-trader" in writable


def test_guide_prints_the_fix_for_servers_that_cannot_show_it():
    """A server made before the fix runs deploy-day code: its Update button
    shows only git's raw "Read-only file system" error, never the new message.
    So the guide itself must name that symptom and print the whole command."""
    with open(os.path.join(REPO, "GETTING_STARTED.md"), encoding="utf-8") as f:
        guide = f.read()
    assert "Read-only file system" in guide
    cmd = next(ln.strip() for ln in guide.splitlines()
               if ln.strip().startswith("sudo sed "))
    # sets exactly what the service file shipped today allows
    assert ("s|^ReadWritePaths=.*|ReadWritePaths="
            + _unit()["ReadWritePaths"] + "|") in cmd
    assert "/etc/systemd/system/agentic-trader.service" in cmd
    assert "sudo systemctl daemon-reload" in cmd
    assert "sudo systemctl restart agentic-trader" in cmd


def _read_only_install(tmp_path, monkeypatch, under_systemd=True):
    install = tmp_path / "install"
    install.mkdir()
    monkeypatch.setattr(webui, "_repo_dir", lambda: str(install))
    monkeypatch.setenv("AGENT_HOME", str(tmp_path / "state"))
    if under_systemd:
        monkeypatch.setenv("INVOCATION_ID", "abc123")  # set by systemd
    else:
        monkeypatch.delenv("INVOCATION_ID", raising=False)

    def refuse(*a, **k):
        raise OSError(errno.EROFS, "Read-only file system")
    monkeypatch.setattr(tempfile, "mkstemp", refuse)
    called = []
    monkeypatch.setattr(su, "check", lambda d: called.append("check") or {
        "ok": True, "behind": 1, "target": SHA, "mode": "snapshot"})
    monkeypatch.setattr(su, "pull", lambda d, e: called.append("pull") or (
        True, "updated"))
    monkeypatch.setattr(webui, "_git", lambda *a, **k: called.append("git"))
    return install, called


def test_old_service_file_gets_the_one_command_fix(tmp_path, monkeypatch):
    install, called = _read_only_install(tmp_path, monkeypatch)
    r = webui.check_code_updates()
    assert not r["ok"]
    err = r["error"]
    assert "Nothing was changed" in err
    # one copy-paste line: allow both folders, reload, restart
    cmd = next(ln.strip() for ln in err.splitlines()
               if ln.strip().startswith("sudo "))
    assert f"ReadWritePaths={tmp_path / 'state'} {install}" in cmd
    assert "/etc/systemd/system/agentic-trader.service" in cmd
    assert "systemctl daemon-reload" in cmd
    assert "systemctl restart agentic-trader" in cmd
    ok, note = webui.pull_code_updates(expect=SHA)
    assert not ok and cmd in note
    assert called == []   # no download, no git, no half-applied update


def test_unwritable_install_elsewhere_says_so_plainly(tmp_path, monkeypatch):
    install, called = _read_only_install(tmp_path, monkeypatch,
                                         under_systemd=False)
    ok, note = webui.pull_code_updates(expect=SHA)
    assert not ok and str(install) in note
    assert "can't write" in note and "Nothing was changed" in note
    assert "systemctl" not in note and called == []


def test_writable_install_is_checked_and_left_clean(tmp_path, monkeypatch):
    install = tmp_path / "install"
    install.mkdir()
    monkeypatch.setattr(webui, "_repo_dir", lambda: str(install))
    monkeypatch.setattr(su, "check", lambda d: {"ok": True, "behind": 0,
                                                "target": SHA})
    assert webui.check_code_updates()["ok"]
    assert os.listdir(install) == []   # the write test cleans up after itself
