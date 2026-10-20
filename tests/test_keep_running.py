"""Keeping the agent running: one copy only, background start/stop, crash
restarts, start at login. No test starts a real agent or touches the real
home/autostart folders."""

import os
import plistlib
import subprocess
import sys
import threading
import time

import pytest

import agent
import keep_running
import platform_compat


# ── single-instance lock ─────────────────────────────────────────────────────

def test_second_lock_is_refused_until_released(home):
    a, b = keep_running.InstanceLock(), keep_running.InstanceLock()
    assert a.acquire()
    assert not b.acquire()
    assert keep_running.is_running()
    assert keep_running.running_pid() == os.getpid()
    a.release()
    assert not keep_running.is_running()
    assert keep_running.running_pid() is None
    assert b.acquire()
    b.release()


def test_lock_is_freed_when_the_holder_dies(home):
    """A crashed agent must never leave the lock stuck."""
    code = ("import sys, time; sys.path.insert(0, %r); import keep_running;"
            "lk = keep_running.InstanceLock(); assert lk.acquire();"
            "print('locked', flush=True); time.sleep(60)") % os.path.dirname(
                os.path.abspath(keep_running.__file__))
    proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE,
                            text=True, env={**os.environ, "AGENT_HOME": str(home)})
    try:
        assert proc.stdout.readline().strip() == "locked"
        assert keep_running.is_running()
    finally:
        proc.kill()
        proc.wait(timeout=10)
    assert not keep_running.is_running()


def test_acquire_waits_for_a_departing_holder(home):
    a = keep_running.InstanceLock()
    assert a.acquire()
    threading.Timer(0.8, a.release).start()
    b = keep_running.InstanceLock()
    assert b.acquire(wait=5)
    b.release()


def test_run_refuses_a_second_copy(home, capsys):
    held = keep_running.InstanceLock()
    assert held.acquire()
    try:
        with pytest.raises(SystemExit) as e:
            agent.cmd_run()
        assert e.value.code == 0  # clean: a supervisor must not retry this
        assert "already running" in capsys.readouterr().out
    finally:
        held.release()


# ── stop ─────────────────────────────────────────────────────────────────────

def test_stop_when_not_running_clears_stale_request(home, capsys):
    keep_running.request_stop()
    assert keep_running.stop()
    assert not keep_running.stop_requested()
    assert "isn't running" in capsys.readouterr().out


def test_stop_waits_for_the_agent_to_let_go(home):
    held = keep_running.InstanceLock()
    assert held.acquire()

    def agent_loop():  # what the heartbeat does: notice the request, exit
        while not keep_running.stop_requested():
            time.sleep(0.1)
        keep_running.clear_stop()
        held.release()
    threading.Thread(target=agent_loop, daemon=True).start()
    assert keep_running.stop(timeout=10, out=lambda *_: None)
    assert not keep_running.is_running()


def test_sleep_wakes_up_on_stop(home):
    """Overnight polls are 5 minutes; a stop must not wait that long."""
    threading.Timer(0.5, keep_running.request_stop).start()
    t0 = time.time()
    agent._sleep_unless_stopped(300, {"stop": False})
    assert time.time() - t0 < 3
    t0 = time.time()
    keep_running.clear_stop()
    agent._sleep_unless_stopped(300, {"stop": True})
    assert time.time() - t0 < 0.5


# ── supervisor ───────────────────────────────────────────────────────────────

def _child(code_seq_file, codes):
    """A fake agent that exits with the next code from `codes` each run."""
    return [sys.executable, "-c",
            "import os,sys\n"
            f"p={str(code_seq_file)!r}\n"
            "n=int(open(p).read()) if os.path.exists(p) else 0\n"
            "open(p,'w').write(str(n+1))\n"
            f"codes={codes!r}\n"
            "print('run', n, os.environ.get('AGENTIC_TRADER_SUPERVISED'))\n"
            "sys.exit(codes[min(n, len(codes)-1)])\n"]


def test_supervisor_restarts_crashes_and_stops_on_clean_exit(home, tmp_path):
    seq = tmp_path / "n"
    sleeps = []
    code = keep_running.supervise(cmd=_child(seq, [1, keep_running.RESTART_EXIT, 0]),
                                  sleep=sleeps.append)
    assert code == 0
    assert seq.read_text() == "3"           # crash → restart, restart → run, 0 → done
    assert len(sleeps) == 15                # one 15 s backoff, only after the crash
    log = open(keep_running.log_path()).read()
    assert "run 0 1" in log                 # child output + supervised flag
    assert "restarting in 15s" in log and "asked to restart" in log


def test_supervisor_backoff_doubles(home, tmp_path):
    sleeps = []
    keep_running.supervise(cmd=_child(tmp_path / "n", [1]), sleep=sleeps.append,
                           max_runs=3)
    assert len(sleeps) == 15 + 30


def test_supervisor_honours_stop_during_backoff(home, tmp_path):
    def sleep(_s):
        keep_running.request_stop()
    assert keep_running.supervise(cmd=_child(tmp_path / "n", [1]), sleep=sleep) == 0
    assert (tmp_path / "n").read_text() == "1"


def test_supervised_windows_restart_exits_for_the_supervisor(monkeypatch):
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    monkeypatch.setenv(platform_compat.SUPERVISED_ENV, "1")
    codes = []
    monkeypatch.setattr(os, "_exit", lambda c: codes.append(c))
    monkeypatch.setattr(platform_compat, "spawn_after_port_free",
                        lambda *a, **k: pytest.fail("must not spawn a successor"))
    platform_compat.restart_self(["agent.py", "run"], 1)
    assert codes == [platform_compat.RESTART_EXIT]


def test_start_background_detaches(monkeypatch):
    seen = {}

    class P:
        pass
    monkeypatch.setattr(subprocess, "Popen",
                        lambda cmd, **kw: seen.update(cmd=cmd, kw=kw) or P())
    keep_running.start_background(port=8721)
    assert seen["cmd"][-3:] == ["supervise", "--port", "8721"]
    assert seen["kw"]["stdin"] == subprocess.DEVNULL
    if platform_compat.IS_WINDOWS:
        assert seen["kw"]["creationflags"] & subprocess.DETACHED_PROCESS
    else:
        assert seen["kw"]["start_new_session"] is True


def test_windowless_python_on_windows(monkeypatch, tmp_path):
    (tmp_path / "python.exe").write_text("")
    (tmp_path / "pythonw.exe").write_text("")
    monkeypatch.setattr(keep_running, "IS_WINDOWS", True)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "python.exe"))
    assert keep_running._python_for(windowless=True).endswith("pythonw.exe")
    monkeypatch.setattr(sys, "executable", str(tmp_path / "pythonw.exe"))
    assert keep_running._python_for(windowless=False).endswith("python.exe")


# ── start at login ───────────────────────────────────────────────────────────

def test_linux_autostart_entry(home, tmp_path, monkeypatch):
    monkeypatch.setattr(keep_running, "IS_WINDOWS", False)
    monkeypatch.setattr(keep_running, "IS_MAC", False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    assert not keep_running.autostart_enabled()
    path = keep_running.enable_autostart()
    text = open(path).read()
    assert path.endswith(os.path.join("autostart", "agentic-trader.desktop"))
    assert "supervise" in text
    assert keep_running._desktop_quote(keep_running.agent_script()) in text
    assert keep_running.autostart_status()["enabled"]
    assert keep_running.disable_autostart() == [path]
    assert not keep_running.autostart_enabled()


def test_mac_autostart_plist(home, tmp_path, monkeypatch):
    monkeypatch.setattr(keep_running, "IS_WINDOWS", False)
    monkeypatch.setattr(keep_running, "IS_MAC", True)
    real = os.path.expanduser
    monkeypatch.setattr(os.path, "expanduser",
                        lambda p: str(tmp_path) + p[1:] if p.startswith("~") else real(p))
    path = keep_running.enable_autostart()
    assert path.startswith(str(tmp_path))
    with open(path, "rb") as f:
        pl = plistlib.load(f)
    assert pl["Label"] == keep_running.MAC_LABEL and pl["RunAtLoad"] is True
    assert pl["ProgramArguments"][1:] == [keep_running.agent_script(), "supervise"]
    keep_running.disable_autostart()
    assert not os.path.exists(path)


def test_windows_autostart_shortcut_and_fallback(home, tmp_path, monkeypatch):
    monkeypatch.setattr(keep_running, "IS_WINDOWS", True)
    monkeypatch.setenv("APPDATA", str(tmp_path))
    calls = []

    def fake_run(cmd, **kw):  # PowerShell creates the .lnk
        calls.append(cmd)
        lnk = keep_running.autostart_paths()[0]
        open(lnk, "w").close()
        return subprocess.CompletedProcess(cmd, 0)
    monkeypatch.setattr(subprocess, "run", fake_run)
    path = keep_running.enable_autostart()
    assert path.endswith("agentic-trader.lnk") and "Startup" in path
    ps = calls[0][-1]
    assert "CreateShortcut" in ps and "supervise" in ps
    keep_running.disable_autostart()

    # no PowerShell → batch-file fallback
    def no_ps(cmd, **kw):
        raise FileNotFoundError("powershell")
    monkeypatch.setattr(subprocess, "run", no_ps)
    path = keep_running.enable_autostart()
    assert path.endswith("agentic-trader.cmd")
    assert "supervise" in open(path).read()
    assert keep_running.autostart_enabled()
    assert len(keep_running.disable_autostart()) == 1


def test_dashboard_chat_toggles_autostart(home, tmp_path, monkeypatch):
    monkeypatch.setattr(keep_running, "IS_WINDOWS", False)
    monkeypatch.setattr(keep_running, "IS_MAC", False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    handled, reply = agent._apply_command("autostart on")
    assert handled and keep_running.autostart_enabled()
    handled, reply = agent._apply_command("autostart")
    assert handled and "ON" in reply
    agent._apply_command("autostart off")
    assert not keep_running.autostart_enabled()
