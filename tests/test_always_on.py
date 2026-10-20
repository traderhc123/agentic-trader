"""Keeping the dashboard up 24/7: frozen-agent watchdog, open-at-login,
dashboard self-check, refreshing old start-at-login entries, and the macOS
app-window fallback. The same code paths run on Windows, macOS and Linux;
OS-specific bits are faked so every case runs on every CI runner."""

import http.server
import os
import plistlib
import subprocess
import sys
import threading
import time

import agent
import keep_running
import webui


# ── frozen-agent watchdog ────────────────────────────────────────────────────

def test_child_hung_only_after_a_signal_then_silence(home):
    started = time.time()
    assert not keep_running.child_hung(started)            # no signal yet
    keep_running.touch_alive(force=True)
    assert not keep_running.child_hung(started)            # fresh
    later = time.time() + keep_running.HANG_SECONDS + 5
    assert keep_running.child_hung(started, now=later)     # went quiet
    # a file left by an earlier copy never counts against a new one
    assert not keep_running.child_hung(time.time() + 60,
                                       now=later + 60)


def test_touch_alive_is_throttled(home):
    keep_running.touch_alive(force=True)
    first = os.path.getmtime(keep_running._alive_path())
    os.utime(keep_running._alive_path(), (first - 100, first - 100))
    keep_running.touch_alive()                             # within 30 s: skipped
    assert os.path.getmtime(keep_running._alive_path()) == first - 100


def test_supervisor_ends_a_frozen_agent(home, monkeypatch):
    monkeypatch.setattr(keep_running, "HANG_SECONDS", 1)
    code = ("import sys, time; sys.path.insert(0, %r); import keep_running;"
            "keep_running.touch_alive(force=True); time.sleep(120)") % (
                os.path.dirname(os.path.abspath(keep_running.__file__)))
    t0 = time.time()
    rc = keep_running._run_child([sys.executable, "-c", code])
    assert rc == keep_running.HUNG_EXIT
    assert time.time() - t0 < 60
    with open(keep_running.log_path(), encoding="utf-8") as f:
        assert "stopped responding" in f.read()


def test_on_first_start_runs_once_across_restarts(home, tmp_path):
    seq = tmp_path / "seq"
    script = ("import pathlib, sys; p = pathlib.Path(%r); n = int(p.read_text() "
              "if p.exists() else 0); p.write_text(str(n + 1));"
              "sys.exit([1, 0][min(n, 1)])") % str(seq)
    calls = []
    rc = keep_running.supervise(cmd=[sys.executable, "-c", script],
                                sleep=lambda s: None,
                                on_first_start=lambda: calls.append(1))
    assert rc == 0 and seq.read_text() == "2"
    time.sleep(0.2)
    assert calls == [1]


# ── start at login opens the dashboard ───────────────────────────────────────

def test_every_os_login_entry_carries_the_at_login_flag(home, tmp_path, monkeypatch):
    # Linux desktop
    monkeypatch.setattr(keep_running, "IS_WINDOWS", False)
    monkeypatch.setattr(keep_running, "IS_MAC", False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    p = keep_running.enable_autostart()
    assert keep_running.AT_LOGIN_FLAG in open(p).read()
    keep_running.disable_autostart()
    # macOS
    monkeypatch.setattr(keep_running, "IS_MAC", True)
    real = os.path.expanduser
    monkeypatch.setattr(os.path, "expanduser",
                        lambda q: str(tmp_path) + q[1:] if q.startswith("~") else real(q))
    p = keep_running.enable_autostart()
    with open(p, "rb") as f:
        assert plistlib.load(f)["ProgramArguments"][-1] == keep_running.AT_LOGIN_FLAG
    keep_running.disable_autostart()
    # Windows (shortcut via PowerShell, and the batch-file fallback)
    monkeypatch.setattr(keep_running, "IS_MAC", False)
    monkeypatch.setattr(keep_running, "IS_WINDOWS", True)
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    seen = []

    def fake_ps(cmd, **kw):
        seen.append(cmd[-1])
        open(keep_running.autostart_paths()[0], "w").close()
        return subprocess.CompletedProcess(cmd, 0)
    monkeypatch.setattr(subprocess, "run", fake_ps)
    keep_running.enable_autostart()
    assert f"supervise {keep_running.AT_LOGIN_FLAG}" in seen[0]
    keep_running.disable_autostart()

    def no_ps(cmd, **kw):
        raise FileNotFoundError
    monkeypatch.setattr(subprocess, "run", no_ps)
    p = keep_running.enable_autostart()
    assert f"supervise {keep_running.AT_LOGIN_FLAG}" in open(p).read()


def test_old_login_entries_are_refreshed(home, tmp_path, monkeypatch):
    monkeypatch.setattr(keep_running, "IS_WINDOWS", False)
    monkeypatch.setattr(keep_running, "IS_MAC", False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    assert keep_running.refresh_autostart() is None        # off: untouched
    (path,) = keep_running.autostart_paths()
    os.makedirs(os.path.dirname(path))
    with open(path, "w") as f:                             # pre-flag entry
        f.write("[Desktop Entry]\nExec=python agent.py supervise\n")
    assert keep_running.autostart_outdated()
    assert keep_running.refresh_autostart() == path
    assert not keep_running.autostart_outdated()
    assert keep_running.refresh_autostart() is None        # current: no-op


def test_windows_shortcut_strings_are_read_as_utf16(home, tmp_path, monkeypatch):
    monkeypatch.setattr(keep_running, "IS_WINDOWS", True)
    monkeypatch.setenv("APPDATA", str(tmp_path))
    lnk = keep_running.autostart_paths()[0]
    os.makedirs(os.path.dirname(lnk))
    args = f'"{keep_running.agent_script()}" supervise {keep_running.AT_LOGIN_FLAG}'
    with open(lnk, "wb") as f:
        f.write(b"L\x00\x00\x00" + args.encode("utf-16-le"))
    assert not keep_running.autostart_outdated()


def test_open_when_ready_opens_the_signed_in_link(home, monkeypatch):
    opened = []
    monkeypatch.setattr(keep_running, "is_running", lambda: True)
    monkeypatch.setattr(agent, "_dashboard_answers", lambda port, timeout=10: True)
    monkeypatch.setattr(webui, "open_app_window", opened.append)
    assert agent._open_dashboard_when_ready(timeout=5, sleep=lambda s: None)
    assert opened == [agent._dashboard_link()] and "?k=" in opened[0]


def test_open_when_ready_gives_up_quietly(home, monkeypatch):
    opened = []
    monkeypatch.setattr(keep_running, "is_running", lambda: False)
    monkeypatch.setattr(webui, "open_app_window", opened.append)
    assert not agent._open_dashboard_when_ready(timeout=0, sleep=lambda s: None)
    assert opened == []


def test_supervise_cli_wires_open_at_login(home, monkeypatch):
    got = {}

    def fake_supervise(port=None, on_first_start=None, **kw):
        got["cb"] = on_first_start
        return 0
    monkeypatch.setattr(keep_running, "supervise", fake_supervise)
    for argv, cfg, want in (
            (["agent.py", "supervise", keep_running.AT_LOGIN_FLAG], {}, True),
            (["agent.py", "supervise"], {}, False),             # crash-safe start
            (["agent.py", "supervise", keep_running.AT_LOGIN_FLAG],
             {"open_dashboard_at_login": False}, False)):
        monkeypatch.setattr(sys, "argv", argv)
        monkeypatch.setattr(agent, "_load", lambda p, d=None, c=cfg: c)
        try:
            agent.cmd_supervise()
        except SystemExit:
            pass
        assert (got["cb"] is agent._open_dashboard_when_ready) is want


def test_chat_toggles_open_at_login(home):
    handled, reply = agent._apply_command("open at login off")
    assert handled and "OFF" in reply
    assert agent._load(agent.CONFIG_PATH, {})["open_dashboard_at_login"] is False
    handled, reply = agent._apply_command("open at login on")
    assert handled and "ON" in reply
    assert agent._open_at_login_wanted()


# ── dashboard self-check ─────────────────────────────────────────────────────

def test_dashboard_answers_needs_a_real_http_reply():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0),
                                          http.server.BaseHTTPRequestHandler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        assert agent._dashboard_answers(port, timeout=5)  # 501 still = alive
    finally:
        srv.shutdown()
        srv.server_close()
    assert not agent._dashboard_answers(port, timeout=2)


# ── macOS app window falls back like Windows does ────────────────────────────

def test_mac_without_chromium_browsers_opens_default_browser(monkeypatch):
    import webbrowser
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(os.path, "isdir", lambda p: False)
    launched, opened = [], []
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kw: launched.append(cmd))
    monkeypatch.setattr(webbrowser, "open", opened.append)
    assert webui.open_app_window("http://127.0.0.1:8721/dash?k=x") is False
    assert launched == [] and opened == ["http://127.0.0.1:8721/dash?k=x"]


def test_mac_with_chrome_uses_an_app_window(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(os.path, "isdir",
                        lambda p: p.endswith("Google Chrome.app"))
    launched = []
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kw: launched.append(cmd))
    assert webui.open_app_window("http://x/") is True
    assert launched[0][:3] == ["open", "-na", "Google Chrome"]
