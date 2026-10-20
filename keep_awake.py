"""Keep the computer from idle-sleeping while the agent runs.

Uses each OS's own "I'm busy" request, scoped to the agent process — no
power settings are changed, and it ends by itself when the agent stops
(even after a crash):
- Windows: SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED) from
  the heartbeat thread. The screen may still turn off; the PC stays awake.
- macOS: `caffeinate -i -s -w <agent pid>` (idle sleep blocked; on power,
  system sleep too). Exits on its own when the agent's pid goes away.
- Linux desktops: `systemd-inhibit --what=idle:sleep` around a watcher that
  exits with the agent. Servers don't idle-sleep, so it's skipped there.

It can't stop a laptop lid being closed or someone choosing Sleep — that
needs admin power settings, which this deliberately doesn't touch.

Config: keep_awake (default true). The heartbeat calls apply(cfg) every
cycle, so a dashboard toggle takes effect right away.
"""
import os
import shutil
import subprocess
import sys

_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001


def wanted(cfg):
    return cfg.get("keep_awake", True) is not False


class KeepAwake:
    def __init__(self):
        self.active = False
        self.how = ""
        self._proc = None

    def apply(self, cfg):
        """Turn the request on/off to match config. Never raises."""
        try:
            if wanted(cfg) and not self.active:
                self._on()
            elif not wanted(cfg) and self.active:
                self._off()
        except Exception:
            pass
        return self.active

    def _on(self):
        if sys.platform == "win32":
            import ctypes
            ok = ctypes.windll.kernel32.SetThreadExecutionState(
                _ES_CONTINUOUS | _ES_SYSTEM_REQUIRED)
            self.active, self.how = bool(ok), "Windows sleep blocked"
        elif sys.platform == "darwin":
            if shutil.which("caffeinate"):
                self._proc = subprocess.Popen(
                    ["caffeinate", "-i", "-s", "-w", str(os.getpid())],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL)
                self.active, self.how = True, "macOS caffeinate"
        elif (shutil.which("systemd-inhibit")
              and (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))):
            watcher = (f"while kill -0 {os.getpid()} 2>/dev/null; "
                       "do sleep 30; done")
            self._proc = subprocess.Popen(
                ["systemd-inhibit", "--what=idle:sleep", "--who=agentic-trader",
                 "--why=Trading agent running", "--mode=block",
                 "sh", "-c", watcher],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL)
            self.active, self.how = True, "systemd-inhibit"

    def _off(self):
        if sys.platform == "win32":
            import ctypes
            ctypes.windll.kernel32.SetThreadExecutionState(_ES_CONTINUOUS)
        if self._proc is not None:
            try:
                self._proc.terminate()
            except Exception:
                pass
            self._proc = None
        self.active, self.how = False, ""

    def describe(self, cfg):
        if not wanted(cfg):
            return "off"
        if self.active:
            return "on"
        return "on (not supported here)"
