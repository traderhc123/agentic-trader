"""Keep the computer awake while the agent runs — per-process OS requests,
never a power-settings change."""

import subprocess
import sys
import types

import pytest

import agent
import keep_awake


def test_default_is_on_and_can_be_turned_off():
    assert keep_awake.wanted({}) and keep_awake.wanted({"keep_awake": True})
    assert not keep_awake.wanted({"keep_awake": False})


def test_windows_uses_set_thread_execution_state(monkeypatch):
    calls = []
    fake = types.SimpleNamespace(windll=types.SimpleNamespace(kernel32=types.SimpleNamespace(
        SetThreadExecutionState=lambda flags: calls.append(flags) or 1)))
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "ctypes", fake)
    k = keep_awake.KeepAwake()
    assert k.apply({}) and k.describe({}) == "on"
    k.apply({})                                   # already on: no repeat call
    k.apply({"keep_awake": False})
    assert calls == [0x80000001, 0x80000000] and not k.active


def test_mac_runs_caffeinate_tied_to_the_agent_pid(monkeypatch):
    procs = []

    class P:
        def __init__(self, cmd, **kw):
            self.cmd, self.killed = cmd, False
            procs.append(self)

        def terminate(self):
            self.killed = True
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(keep_awake.shutil, "which", lambda n: "/usr/bin/" + n)
    monkeypatch.setattr(subprocess, "Popen", P)
    k = keep_awake.KeepAwake()
    k.apply({})
    import os
    assert procs[0].cmd == ["caffeinate", "-i", "-s", "-w", str(os.getpid())]
    k.apply({"keep_awake": False})
    assert procs[0].killed and not k.active


def test_linux_server_without_display_is_skipped(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("no inhibitor on servers"))
    k = keep_awake.KeepAwake()
    assert not k.apply({}) and k.describe({}) == "on (not supported here)"


def test_failure_never_raises(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(keep_awake.shutil, "which", lambda n: "/x")

    def boom(*a, **k):
        raise OSError("no caffeinate")
    monkeypatch.setattr(subprocess, "Popen", boom)
    assert keep_awake.KeepAwake().apply({}) is False


@pytest.mark.skipif(sys.platform != "win32", reason="real Windows API")
def test_real_windows_call():
    k = keep_awake.KeepAwake()
    assert k.apply({}) and k.how == "Windows sleep blocked"
    k.apply({"keep_awake": False})
    assert not k.active


def test_chat_toggle(home):
    agent._save(agent.CONFIG_PATH, {"source": "manual"})
    handled, reply = agent._apply_command("keep awake off")
    assert handled and "OFF" in reply
    assert agent._load(agent.CONFIG_PATH)["keep_awake"] is False
    agent._apply_command("keep awake on")
    assert agent._load(agent.CONFIG_PATH)["keep_awake"] is True
