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
  from the dashboard or with `agent.py stop`) ends the supervisor too. It
  holds its own lock (HOME/supervisor.lock), so `stop` can find and end it
  even while the agent is down between two tries, and it records why the
  last try failed (HOME/supervisor.status) so `start` can say so.
- "Running" means the agent is past its startup checks: it removes
  HOME/agent.alive when it takes the lock and writes it at the top of its
  first loop (is_ready). Holding the lock alone is not enough — a copy whose
  broker login expired holds it for a moment and then exits.
- `agent.py start` launches the supervisor detached from the terminal; the
  wizard's "Start my agent now" button does the same.
- Autostart writes ONE file per OS that runs the supervisor at login: a
  Startup-folder shortcut (Windows), a LaunchAgent plist (macOS), or an XDG
  autostart entry (Linux desktops). Servers should use the systemd unit in
  deploy/ instead. Nothing here needs admin rights.
"""
import json
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
# passed by every start-at-login entry so the supervisor knows to open the
# dashboard once the agent is up (crash restarts never open it)
AT_LOGIN_FLAG = "--at-login"


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
    # agent.lock -> agent.pid, supervisor.lock -> supervisor.pid
    return os.path.splitext(lock_path)[0] + ".pid"


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


def is_ready():
    """True once the running agent is past its startup checks (setup, broker
    login, policy) and in its main loop. `agent.py run` calls clear_alive()
    when it takes the lock and touch_alive() at the top of every loop, so
    "lock held + alive file there" means it really started. A copy that dies
    during startup never looks like it is running."""
    return is_running() and os.path.exists(_alive_path())


def _supervisor_lock_path():
    return _path("supervisor.lock")


def supervisor_running():
    """True if a supervisor is alive — also while the agent itself is down
    and the supervisor is waiting to start it again."""
    probe = InstanceLock(_supervisor_lock_path())
    if probe.acquire():
        probe.release()
        return False
    return True


def supervisor_pid():
    if not supervisor_running():
        return None
    try:
        with open(_pid_path(_supervisor_lock_path()), encoding="utf-8") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def _status_path():
    return _path("supervisor.status")


def _set_status(**fields):
    """What the supervisor is doing, for `start`, `stop` and `status`."""
    fields.update(pid=os.getpid(), at=time.time())
    try:
        os.makedirs(home(), exist_ok=True)
        tmp = _status_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(fields, f)
        os.replace(tmp, _status_path())
    except OSError:
        pass


def _clear_status():
    try:
        os.remove(_status_path())
    except OSError:
        pass


def supervisor_status():
    """The supervisor's last note ({} if none). state "running": it started
    the agent; "waiting": the agent stopped with an error ("reason" = the
    last lines it printed) and will be started again. Only meaningful while
    supervisor_running() — a killed supervisor leaves its note behind."""
    try:
        with open(_status_path(), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def start_failure(before=None):
    """Why the agent's latest start did not work, in its own words — or None
    if the supervisor has recorded no failed run. Pass the supervisor_status()
    read before starting as `before` to ignore an older failure."""
    st = supervisor_status()
    if st.get("state") != "waiting" or st == (before or {}):
        return None
    return st.get("reason") or f"it stopped with error code {st.get('code')}"


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


# ── "try again now" requests (a second `start` while one is waiting) ──────────

def _retry_path():
    return _path("retry.request")


def request_retry():
    os.makedirs(home(), exist_ok=True)
    with open(_retry_path(), "w", encoding="utf-8") as f:
        f.write(str(time.time()))


def _take_retry():
    """True (once) if someone asked the waiting supervisor to try again now."""
    try:
        os.remove(_retry_path())
        return True
    except OSError:
        return False


# A supervisor started by a version before the supervisor lock can't be seen,
# but it does look for a stop request every second while it waits to restart
# a crashed agent. `stop` leaves the request up this long so it notices.
OLD_SUPERVISOR_WAIT = 2.5


def stop(timeout=45.0, out=print):
    """Stop the agent AND the supervisor that would start it again, and wait
    for both. The heartbeat checks the request every second, so this normally
    takes a second or two; a supervisor that is waiting to restart a crashed
    agent checks it every second too. Whatever hangs (stuck network call) is
    killed after `timeout`. Returns True if nothing is running afterwards."""
    agent_up, keeper_up = is_running(), supervisor_running()
    if not agent_up and not keeper_up:
        request_stop()
        time.sleep(OLD_SUPERVISOR_WAIT)
        agent_up = is_running()  # an older supervisor restarted it meanwhile
        if not agent_up:
            clear_stop()
            out("The agent isn't running.")
            return True
    request_stop()
    out("Asking the agent to stop…" if agent_up else
        "The agent isn't running right now: it stopped with an error and was "
        "going to be started again automatically. Cancelling that…")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not is_running() and not supervisor_running():
            clear_stop()
            out("Stopped. Open positions stay in your broker account."
                if agent_up else
                "Cancelled. The agent stays off until you start it again "
                "(python agent.py start"
                + (", or your next login — start at login is on" if
                   autostart_enabled() else "")
                + "). Open positions stay in your broker account.")
            return True
        if not stop_requested():
            # A copy that was only just starting removes the request (it is
            # left over from last time, as far as it knows), and the copy
            # that acted on it removes it on its way out. Keep it up until
            # the supervisor is gone too, or it would start the agent again.
            request_stop()
        time.sleep(0.5)
    pid = running_pid()
    if pid:
        out(f"It didn't stop within {int(timeout)}s — ending process {pid}.")
        _kill(pid)
        time.sleep(1.0)
    # with its agent gone the supervisor sees the request and ends by itself
    for _ in range(10):
        if not supervisor_running():
            break
        time.sleep(0.5)
    keeper = supervisor_pid()
    if keeper:
        out(f"The background process that restarts the agent ({keeper}) "
            "didn't stop either — ending it.")
        _kill(keeper)
        time.sleep(1.0)
    ok = not is_running() and not supervisor_running()
    if ok:
        clear_stop()
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
    try:
        _run_log_start[0] = os.path.getsize(log_path())
    except OSError:
        _run_log_start[0] = 0
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
        started = time.time()
        tick, awake_since = started, None
        try:
            while True:
                code = proc.poll()
                if code is not None:
                    return code
                now = time.time()
                if now - tick > WAKE_GAP_SECONDS:
                    # This loop runs every 2 s, so a gap means the computer
                    # was asleep (or its clock jumped) and the agent was
                    # asleep with it. Its last sign of life looks hours old
                    # right now; count its silence from this moment instead
                    # of ending a healthy agent before it has had a turn.
                    awake_since = now
                    _log(f"woke up after {int((now - tick) // 60)} min asleep "
                         f"— giving the agent {HANG_SECONDS // 60} min to "
                         "show it is still working")
                tick = now
                if child_hung(started, now, awake_since):
                    _log(f"agent stopped responding for {HANG_SECONDS // 60} min "
                         "(stuck network call?) — restarting it")
                    _end_child(proc)
                    return HUNG_EXIT
                time.sleep(2)
        except KeyboardInterrupt:
            proc.terminate()
            return 0


# where the current/latest run's output starts in the log (bytes)
_run_log_start = [0]


def _child_said(max_lines=6, max_bytes=4000):
    """The last few lines the agent printed in its latest run — its own
    words for why it stopped ("Broker auth missing/expired — run: …")."""
    try:
        with open(log_path(), "rb") as f:
            f.seek(0, os.SEEK_END)
            end = f.tell()
            start = _run_log_start[0] if _run_log_start[0] <= end else 0
            f.seek(max(start, end - max_bytes))
            text = f.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
    lines = [ln.strip() for ln in text.splitlines()
             if ln.strip() and "] supervisor: " not in ln]
    return "\n".join(lines[-max_lines:])[:600]


def _end_child(proc):
    proc.terminate()
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)


# ── "still alive" signal (frozen-agent watchdog) ─────────────────────────────
# The agent touches HOME/agent.alive every loop and every second while it
# sleeps. A crash is easy to see (the process exits); a FREEZE is not — e.g. a
# network call that never returns after Wi-Fi drops or the computer wakes from
# sleep. If the file goes stale for HANG_SECONDS the supervisor ends that copy
# and starts a fresh one. Same code on Windows, macOS and Linux.
HANG_SECONDS = 20 * 60   # longest normal gap is a 5-min overnight sleep
HUNG_EXIT = 70           # treated as a crash: restarts with backoff
# The watchdog loop runs every 2 s. A longer pause than this between two
# turns means the whole computer was asleep, not that the agent froze.
WAKE_GAP_SECONDS = 60
_last_touch = [0.0]


def _alive_path():
    return _path("agent.alive")


def touch_alive(force=False):
    now = time.time()
    if not force and now - _last_touch[0] < 30:
        return
    _last_touch[0] = now
    try:
        os.makedirs(home(), exist_ok=True)
        with open(_alive_path(), "w", encoding="utf-8") as f:
            f.write(str(now))
    except OSError:
        pass


def clear_alive():
    """Called by `agent.py run` the moment it holds the lock: this copy has
    not shown it is up yet (see is_ready)."""
    _last_touch[0] = 0.0
    try:
        os.remove(_alive_path())
    except OSError:
        pass


def child_hung(started, now=None, awake_since=None):
    """True if the agent started after `started` has signalled it's alive at
    least once and then went quiet for HANG_SECONDS. Before the first signal
    (still starting up, e.g. broker login) it is never judged hung.
    `awake_since`: when the computer last woke from sleep — time spent asleep
    is not silence, so the quiet period counts from there."""
    try:
        last = os.path.getmtime(_alive_path())
    except OSError:
        return False
    if last < started - 1:
        return False   # left over from a previous copy
    if awake_since is not None:
        last = max(last, awake_since)
    return (now or time.time()) - last > HANG_SECONDS


# Tell the owner once the agent has stopped with an error this many times in
# a row, then remind them this often for as long as it keeps happening.
LOOP_NOTIFY_AFTER = 3
LOOP_NOTIFY_EVERY = 6 * 3600


def _no_links(text):
    """Notifications go to third parties (Discord, ntfy, Telegram): cut every
    web address down to its host, so a log line never carries the dashboard's
    login token or a webhook address out with it."""
    import re
    text = re.sub(r"(https?://[^/\s]+)\S*", r"\1/…", text)
    return re.sub(r"(with url: )\S+", r"\1…", text)  # requests' error text


def _loop_message(failures, reason, backoff):
    wait = (f"{backoff} seconds" if backoff < 120
            else f"{backoff // 60} minutes")
    reason = _no_links(reason or "")
    return ("agentic-trader is NOT running: it has stopped with an error "
            f"{failures} times in a row."
            + (" What it said: " + reason.replace("\n", " / ") + "."
               if reason else "")
            + f" It will try again by itself in about {wait} and keep "
            "trying, waiting longer each time (up to 10 minutes between "
            "tries). Until it is running again it "
            "places no orders and does NOT act on exit signals for your open "
            "positions — watch those in your broker app. To stop the "
            "retries: python agent.py stop")


def _tell(notify, text):
    """Send in the background: a slow notification must not delay a stop."""
    import threading

    def send():
        try:
            notify(text)
        except Exception:
            pass
    threading.Thread(target=send, daemon=True).start()


def supervise(port=None, cmd=None, sleep=time.sleep, max_runs=None,
              on_first_start=None, notify=None):
    """Keep `agent.py run` alive. Exit 0 from the agent (stopped on purpose,
    or another copy is already running) ends supervision; RESTART_EXIT
    restarts at once; anything else is a crash and restarts with backoff
    (15 s doubling to 10 min, reset after 10 healthy minutes). One supervisor
    per HOME: a second one asks the first to try again now and leaves.
    `notify(text)` is called when the agent keeps failing (restart loop)."""
    cmd = cmd or [_python_for(windowless=False), agent_script(), "run"]
    guard = InstanceLock(_supervisor_lock_path())
    if not guard.acquire(wait=2):
        if not is_running():
            request_retry()
        _log("a supervisor is already keeping the agent running — asked it "
             "to try again now instead of starting a second one")
        return 0
    try:
        clear_stop()
        _take_retry()
        backoff, runs = 15, 0
        failures, told_at = 0, None
        _log(f"started (pid {os.getpid()})")
        if on_first_start is not None:
            # e.g. open the dashboard once at login; runs beside the agent
            import threading
            threading.Thread(target=on_first_start, daemon=True).start()
        while True:
            if port:
                platform_compat.wait_port_free(port)
            if stop_requested():
                _log("stop requested — supervisor done")
                return 0
            _set_status(state="running", runs=runs)
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
                backoff, failures = 15, 0
                continue
            if time.time() - started > 600:
                backoff, failures = 15, 0
            failures += 1
            reason = (f"it stopped responding for {HANG_SECONDS // 60} minutes"
                      if code == HUNG_EXIT else _child_said())
            _set_status(state="waiting", code=code, reason=reason, runs=runs,
                        failures=failures, retry_in=backoff)
            _log(f"agent exited with code {code} — restarting in {backoff}s")
            if (notify is not None and failures >= LOOP_NOTIFY_AFTER
                    and (told_at is None
                         or time.time() - told_at >= LOOP_NOTIFY_EVERY)):
                told_at = time.time()
                _log(f"the agent has failed {failures} times in a row — "
                     "telling the owner")
                _tell(notify, _loop_message(failures, reason, backoff))
            waited = 0
            while waited < backoff:
                if stop_requested():
                    _log("stop requested — supervisor done")
                    return 0
                if _take_retry():
                    _log("asked to try again now")
                    break
                sleep(1)
                waited += 1
            else:
                backoff = min(backoff * 2, 600)
    finally:
        _clear_status()
        guard.release()


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


def wait_until_running(timeout=30.0, proc=None, before=None):
    """Poll until the agent is past its startup checks and in its main loop
    (is_ready). Returns False early if its start failed (start_failure says
    why; `before` = the supervisor_status() read before starting) or if the
    supervisor already exited (e.g. setup isn't finished)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if is_ready():
            return True
        if start_failure(before) is not None:
            return False
        if (proc is not None and proc.poll() is not None
                and not is_running() and not supervisor_running()):
            return False
        time.sleep(0.5)
    return is_ready()


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
          "$s.Arguments=" + _ps_quote(f'"{script}" supervise {AT_LOGIN_FLAG}') + ";"
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
                f'start "" "{pyw}" "{script}" supervise {AT_LOGIN_FLAG}\r\n')
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
            "ProgramArguments": [sys.executable, agent_script(), "supervise",
                                 AT_LOGIN_FLAG],
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
                f"{_desktop_quote(agent_script())} supervise {AT_LOGIN_FLAG}\n"
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


def autostart_outdated():
    """True if start-at-login is on but its entry was written by an older
    version (no AT_LOGIN_FLAG, or pointing at a different Python/folder,
    e.g. after the repo moved)."""
    where = next((p for p in autostart_paths() if os.path.exists(p)), None)
    if where is None:
        return False
    try:
        with open(where, "rb") as f:
            raw = f.read()
    except OSError:
        return False
    # .lnk files store their strings as UTF-16; the others are UTF-8
    def has(text):
        return text.encode() in raw or text.encode("utf-16-le") in raw
    script = agent_script()
    # the XDG .desktop entry escapes backslashes (matters for Windows paths)
    return not (has(AT_LOGIN_FLAG)
                and (has(script) or has(_desktop_quote(script)[1:-1])))


def refresh_autostart():
    """Rewrite an outdated start-at-login entry. Never raises; returns the
    path rewritten, or None."""
    try:
        if autostart_outdated():
            return enable_autostart()
    except Exception:
        pass
    return None


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
