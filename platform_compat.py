"""Cross-platform process plumbing (Linux, macOS, Windows).

Everything that used to lean on POSIX-only tools lives here: `/bin/sh`
launchers, `os.execv` restarts (on Windows execv mangles paths with spaces
and leaves the console in a half-dead state), and `.venv/bin` paths (Windows
venvs use `.venv\\Scripts`).
"""
import os
import subprocess
import sys

IS_WINDOWS = os.name == "nt"

# Child-side bootstrap: wait until nobody is listening on PORT (the parent's
# dashboard/wizard socket has been released), then run SCRIPT ARGS... in this
# same interpreter. Pure Python, so it works without a POSIX shell.
_WAIT_THEN_RUN = r"""
import os, runpy, socket, sys, time
port = int(sys.argv[1])
for _ in range(40):
    s = socket.socket()
    try:
        busy = s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()
    if not busy:
        break
    time.sleep(0.5)
script = sys.argv[2]
sys.argv = sys.argv[2:]
sys.path.insert(0, os.path.dirname(os.path.abspath(script)))
runpy.run_path(script, run_name="__main__")
"""


def make_private(path, is_dir=False):
    """Restrict `path` to the current user. POSIX: chmod 600/700. Windows:
    chmod is a no-op there, so strip inherited ACLs with icacls and grant only
    this user (directories pass the grant on to files created inside them).
    Never raises — a failure leaves the OS default (user-profile) ACL."""
    try:
        if not IS_WINDOWS:
            os.chmod(path, 0o700 if is_dir else 0o600)
            return True
        user = os.environ.get("USERNAME")
        if not user:
            return False
        domain = os.environ.get("USERDOMAIN")
        who = f"{domain}\\{user}" if domain else user
        grant = f"{who}:(OI)(CI)F" if is_dir else f"{who}:F"
        r = subprocess.run(["icacls", path, "/inheritance:r", "/grant:r", grant],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=15)
        return r.returncode == 0
    except Exception:
        return False


def venv_python(prefix="./.venv"):
    """The venv interpreter path a user should type on this OS."""
    if IS_WINDOWS:
        return prefix.replace("/", "\\") + "\\Scripts\\python.exe"
    return prefix + "/bin/python"


def venv_pip(prefix="./.venv"):
    if IS_WINDOWS:
        return prefix.replace("/", "\\") + "\\Scripts\\pip.exe"
    return prefix + "/bin/pip"


def _detach_kwargs(new_console=False):
    if IS_WINDOWS:
        if new_console:
            return {"creationflags": subprocess.CREATE_NEW_CONSOLE}
        return {"creationflags": (subprocess.DETACHED_PROCESS
                                  | subprocess.CREATE_NEW_PROCESS_GROUP)}
    return {"start_new_session": True}


def spawn_after_port_free(script, args, port, log_path=None, cwd=None,
                          new_console=False):
    """Start `python script *args` detached from this process, once `port`
    is free. Output goes to `log_path` (appended) when given, else is
    inherited. Returns the Popen handle; raises on spawn failure."""
    cmd = [sys.executable, "-c", _WAIT_THEN_RUN, str(port), script, *args]
    log = open(log_path, "a", encoding="utf-8") if log_path else None
    try:
        out = log if log else None
        return subprocess.Popen(
            cmd, cwd=cwd, stdin=subprocess.DEVNULL, stdout=out,
            stderr=subprocess.STDOUT if log else None,
            **_detach_kwargs(new_console))
    finally:
        if log:
            log.close()  # the child holds its own handle


def restart_self(argv, port):
    """Replace this process with `python *argv` (argv[0] = the script).

    POSIX: os.execv — same PID, same terminal, sockets closed on exec.
    Windows: there is no real exec, so spawn a successor that waits for our
    port to free, then hard-exit. An interactive console gets a fresh window
    (our console returns to its prompt when we exit); a detached/logging
    process hands its inherited stdout/stderr to the successor."""
    if not IS_WINDOWS:
        os.execv(sys.executable, [sys.executable, *argv])
        return  # pragma: no cover — execv does not return
    interactive = False
    try:
        interactive = sys.stdout is not None and sys.stdout.isatty()
    except Exception:
        pass
    spawn_after_port_free(argv[0], argv[1:], port, new_console=interactive)
    os._exit(0)
