"""Keep the agent running: background start/stop, crash restarts, start at
login, and a guard so only ONE copy of the agent can ever trade at once.

    agent.py start        start in the background (the window can be closed)
    agent.py stop         ask the running agent to stop
    agent.py autostart on|off|status   start automatically when you log in

How the pieces fit:
- `agent.py run` takes an OS file lock (HOME/agent.lock) for its whole life.
  A second copy sees the lock and exits instead of trading twice. The OS drops
  the lock when the process dies, so a crash never leaves it stuck.
- `agent.py supervise` runs `agent.py run` as a child, writes its output to
  HOME/agent-run.log, and starts it again if it crashes. A clean exit (stopped
  from the dashboard or with `agent.py stop`) ends the supervisor too.
- `agent.py start` launches the supervisor detached from the terminal; the
  wizard's "Start my agent now" button does the same.
- Autostart writes ONE file per OS that runs the supervisor at login: a
  Startup-folder shortcut (Windows), a LaunchAgent plist (macOS), or an XDG
  autostart entry (Linux desktops). Servers should use the systemd unit in
  deploy/ instead. Nothing here needs admin rights.
"""
import os
import subprocess
import sys
import time

import platform_compat

IS_WINDOWS = platform_compat.IS_WINDOWS
IS_MAC = sys.platform == "darwin"

# `agent.py run` exits with this when it wants the supervisor to start it
# again right away (Windows has no exec, so an in-place restart is a re-run).
RESTART_EXIT = platform_compat.RESTART_EXIT
SUPERVISED_ENV = platform_compat.SUPERVISED_ENV

LOG_MAX_BYTES = 5 * 1024 * 1024
AUTOSTART_NAME = "agentic-trader"
MAC_LABEL = "com.traderhc.agentic-trader"


def home():
    return os.path.expanduser(os.getenv("AGENT_HOME", "~/.agentic-trader"))


def _path(name):
    return os.path.join(home(), name)


def log_path():
    return _path("agent-run.log")


def repo_dir():
    return os.path.dirname(os.path.abspath(__file__))


def agent_script():
    return os.path.join(repo_dir(), "agent.py")


# ── single-instance lock ─────────────────────────────────────────────────────

class InstanceLock:
    """An exclusive OS lock on HOME/agent.lock, held for the process's life.
    POSIX flock / Windows msvcrt — both are released by the OS on exit or
    crash. The pid goes in a separate file because a Windows byte-range lock
    also blocks other processes from READING the locked bytes."""

    def __init__(self, path=None):
        self.path = path or _path("agent.lock")
        self._f = None

    def _try(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        f = open(self.path, "a+")
        try:
            if IS_WINDOWS:
                import msvcrt
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            f.close()
            return False
        self._f = f
        return True

    def acquire(self, wait=0.0):
        """True once the lock is ours; False if another process holds it
        for longer than `wait` seconds."""
        deadline = time.time() + max(0.0, wait)
        while True:
            if self._try():
                try:
                    with open(_pid_path(self.path), "w", encoding="utf-8") as p:
                        p.write(str(os.getpid()))
                except OSError:
                    pass
                return True
            if time.time() >= deadline:
                return False
            time.sleep(0.5)

    def release(self):
        if not self._f:
            return
        try:
            if IS_WINDOWS:
                import msvcrt
                self._f.seek(0)
                msvcrt.locking(self._f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._f.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        self._f.close()
        self._f = None


def _pid_path(lock_path):
    return os.path.join(os.path.dirname(lock_path), "agent.pid")


def is_running():
    """True if some process holds the agent lock right now."""
    probe = InstanceLock()
    if probe.acquire():
        probe.release()
        return False
    return True


def running_pid():
    """The running agent's pid, or None if it isn't running."""
    if not is_running():
        return None
    try:
        with open(_pid_path(_path("agent.lock")), encoding="utf-8") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


# ── stop requests (agent.py stop) ────────────────────────────────────────────

def _stop_path():
    return _path("stop.request")


def request_stop():
    os.makedirs(home(), exist_ok=True)
    with open(_stop_path(), "w", encoding="utf-8") as f:
        f.write(str(time.time()))


def stop_requested():
    return os.path.exists(_stop_path())


def clear_stop():
    try:
        os.remove(_stop_path())
    except OSError:
        pass


def stop(timeout=45.0, out=print):
    """Ask the running agent to stop and wait for it. The heartbeat checks
    the request every second, so this normally takes a second or two; if it
    hangs (stuck network call) it is killed after `timeout`. Returns True if
    nothing is running afterwards."""
    if not is_running():
        clear_stop()
        out("The agent isn't running.")
        return True
    request_stop()
    out("Asking the agent to stop…")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not is_running():
            out("Stopped. Open positions stay in your broker account.")
            return True
        time.sleep(0.5)
    pid = running_pid()
    if pid:
        out(f"It didn't stop within {int(timeout)}s — ending process {pid}.")
        _kill(pid)
        time.sleep(1.0)
    ok = not is_running()
    out("Stopped." if ok else "Could not stop it — end the python process "
        "in Task Manager / Activity Monitor.")
    return ok


def _kill(pid):
    try:
        if IS_WINDOWS:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=15)
        else:
            import signal
            os.kill(pid, signal.SIGTERM)
    except Exception:
        pass


# ── supervisor ───────────────────────────────────────────────────────────────

def _python_for(windowless):
    """Windows: pythonw.exe runs without a console window, python.exe with
    one. Elsewhere it's the same interpreter."""
    exe = sys.executable
    if not IS_WINDOWS:
        return exe
    d, name = os.path.split(exe)
    want = "pythonw.exe" if windowless else "python.exe"
    if name.lower() != want:
        alt = os.path.join(d, want)
        if os.path.exists(alt):
            return alt
    return exe


def _rotate_log():
    p = log_path()
    try:
        if os.path.getsize(p) > LOG_MAX_BYTES:
            os.replace(p, p + ".1")
    except OSError:
        pass


def _log(msg):
    os.makedirs(home(), exist_ok=True)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(log_path(), "a", encoding="utf-8") as f:
        f.write(f"[{stamp}] supervisor: {msg}\n")


def _run_child(cmd):
    """Run one copy of the agent with output appended to the log; returns
    its exit code."""
    _rotate_log()
    env = {**os.environ, SUPERVISED_ENV: "1", "PYTHONUNBUFFERED": "1",
           "PYTHONIOENCODING": "utf-8"}
    kwargs = {}
    if IS_WINDOWS:
        # the supervisor has no console; without this every child would pop
        # up a new console window
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    with open(log_path(), "a", encoding="utf-8") as log:
        proc = subprocess.Popen(cmd, cwd=repo_dir(), stdin=subprocess.DEVNULL,
                                stdout=log, stderr=subprocess.STDOUT, env=env,
                                **kwargs)
        try:
            return proc.wait()
        except KeyboardInterrupt:
            proc.terminate()
            return 0


def supervise(port=None, cmd=None, sleep=time.sleep, max_runs=None):
    """Keep `agent.py run` alive. Exit 0 from the agent (stopped on purpose,
    or another copy is already running) ends supervision; RESTART_EXIT
    restarts at once; anything else is a crash and restarts with backoff
    (15 s doubling to 10 min, reset after 10 healthy minutes)."""
    cmd = cmd or [_python_for(windowless=False), agent_script(), "run"]
    clear_stop()
    backoff, runs = 15, 0
    _log(f"started (pid {os.getpid()})")
    while True:
        if port:
            platform_compat.wait_port_free(port)
        started = time.time()
        code = _run_child(cmd)
        runs += 1
        if code == 0:
            _log("agent exited cleanly — supervisor done")
            return 0
        if stop_requested():
            _log("stop requested — supervisor done")
            return 0
        if max_runs is not None and runs >= max_runs:
            return code
        if code == RESTART_EXIT:
            _log("agent asked to restart")
            backoff = 15
            continue
        if time.time() - started > 600:
            backoff = 15
        _log(f"agent exited with code {code} — restarting in {backoff}s")
        waited = 0
        while waited < backoff:
            if stop_requested():
                _log("stop requested — supervisor done")
                return 0
            sleep(1)
            waited += 1
        backoff = min(backoff * 2, 600)


def start_background(port=None):
    """Launch the supervisor detached from this terminal/window, so closing
    it doesn't stop the agent. Returns the Popen handle."""
    cmd = [_python_for(windowless=True), agent_script(), "supervise"]
    if port:
        cmd += ["--port", str(port)]
    if IS_WINDOWS:
        kwargs = {"creationflags": (subprocess.DETACHED_PROCESS
                                    | subprocess.CREATE_NEW_PROCESS_GROUP)}
    else:
        kwargs = {"start_new_session": True}
    return subprocess.Popen(cmd, cwd=repo_dir(), stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, **kwargs)


def wait_until_running(timeout=30.0, proc=None):
    """Poll until the agent holds its lock. Stops early if the supervisor
    already exited (e.g. setup isn't finished)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if is_running():
            return True
        if proc is not None and proc.poll() is not None and not is_running():
            return False
        time.sleep(0.5)
    return is_running()


def last_log_lines(n=15):
    try:
        with open(log_path(), encoding="utf-8", errors="replace") as f:
            return f.read().splitlines()[-n:]
    except OSError:
        return []


# ── start at login ───────────────────────────────────────────────────────────

def _windows_startup_dir():
    appdata = os.environ.get("APPDATA") or os.path.expanduser(
        "~\\AppData\\Roaming")
    return os.path.join(appdata, "Microsoft", "Windows", "Start Menu",
                        "Programs", "Startup")


def autostart_paths():
    """Every file autostart may have written on this OS."""
    if IS_WINDOWS:
        d = _windows_startup_dir()
        return [os.path.join(d, AUTOSTART_NAME + ".lnk"),
                os.path.join(d, AUTOSTART_NAME + ".cmd")]
    if IS_MAC:
        return [os.path.expanduser(f"~/Library/LaunchAgents/{MAC_LABEL}.plist")]
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return [os.path.join(base, "autostart", AUTOSTART_NAME + ".desktop")]


def autostart_enabled():
    return any(os.path.exists(p) for p in autostart_paths())


def autostart_status():
    where = next((p for p in autostart_paths() if os.path.exists(p)), None)
    return {"supported": True, "enabled": where is not None, "path": where,
            "how": ("Windows Startup folder" if IS_WINDOWS
                    else "macOS LaunchAgent" if IS_MAC
                    else "desktop autostart (graphical login)")}


def _ps_quote(s):
    return "'" + str(s).replace("'", "''") + "'"


def _enable_windows():
    d = _windows_startup_dir()
    os.makedirs(d, exist_ok=True)
    lnk, cmd_file = autostart_paths()
    pyw = _python_for(windowless=True)
    script = agent_script()
    ps = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut("
          + _ps_quote(lnk) + ");"
          "$s.TargetPath=" + _ps_quote(pyw) + ";"
          "$s.Arguments=" + _ps_quote(f'"{script}" supervise') + ";"
          "$s.WorkingDirectory=" + _ps_quote(repo_dir()) + ";"
          "$s.Description='Starts agentic-trader in the background';"
          "$s.Save()")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive",
                            "-ExecutionPolicy", "Bypass", "-Command", ps],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=30)
        if r.returncode == 0 and os.path.exists(lnk):
            if os.path.exists(cmd_file):
                os.remove(cmd_file)
            return lnk
    except Exception:
        pass
    # Fallback without PowerShell: a batch file. It flashes a console window
    # for a moment at login, then the agent runs windowless.
    with open(cmd_file, "w", encoding="utf-8") as f:
        f.write("@echo off\r\n"
                f'cd /d "{repo_dir()}"\r\n'
                f'start "" "{pyw}" "{script}" supervise\r\n')
    return cmd_file


def _enable_mac():
    import plistlib
    (path,) = autostart_paths()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    os.makedirs(home(), exist_ok=True)
    with open(path, "wb") as f:
        # Loaded by launchd at the next login. Not loaded now on purpose:
        # loading starts a second supervisor, and unloading later would kill
        # whatever launchd started.
        plistlib.dump({
            "Label": MAC_LABEL,
            "ProgramArguments": [sys.executable, agent_script(), "supervise"],
            "WorkingDirectory": repo_dir(),
            "RunAtLoad": True,
            "StandardOutPath": os.path.join(home(), "launchd.log"),
            "StandardErrorPath": os.path.join(home(), "launchd.log"),
        }, f)
    return path


def _desktop_quote(s):
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _enable_linux():
    (path,) = autostart_paths()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("[Desktop Entry]\n"
                "Type=Application\n"
                "Name=agentic-trader\n"
                "Comment=Starts agentic-trader in the background\n"
                f"Exec={_desktop_quote(sys.executable)} "
                f"{_desktop_quote(agent_script())} supervise\n"
                f"Path={repo_dir()}\n"
                "Terminal=false\n"
                "X-GNOME-Autostart-enabled=true\n")
    return path


def enable_autostart():
    """Start the agent at every login. Returns the file written."""
    if IS_WINDOWS:
        return _enable_windows()
    if IS_MAC:
        return _enable_mac()
    return _enable_linux()


def disable_autostart():
    """Stop starting at login. Does not stop an agent that's running now.
    Returns the files removed."""
    removed = []
    for p in autostart_paths():
        if os.path.exists(p):
            os.remove(p)
            removed.append(p)
    return removed


def set_autostart(on):
    """Best-effort toggle for the UI. Returns (ok, message)."""
    try:
        if on:
            where = enable_autostart()
            return True, f"starts automatically when you log in ({where})"
        disable_autostart()
        return True, "won't start automatically at login"
    except Exception as exc:
        return False, f"could not change start-at-login: {str(exc)[:200]}"
