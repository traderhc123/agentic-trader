"""Cross-platform process plumbing — no /bin/sh, correct venv paths."""

import socket
import subprocess
import sys
import time

import platform_compat


def test_venv_paths_match_os(monkeypatch):
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    assert platform_compat.venv_python() == ".\\.venv\\Scripts\\python.exe"
    assert platform_compat.venv_pip().endswith("\\Scripts\\pip.exe")
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
    assert platform_compat.venv_python() == "./.venv/bin/python"
    assert platform_compat.venv_pip() == "./.venv/bin/pip"


def test_spawn_waits_for_port_then_runs_script(tmp_path):
    """The launcher must not start the script while the port is held, and
    must run it (with its args) in pure Python once the port frees."""
    script = tmp_path / "child.py"
    marker = tmp_path / "ran.txt"
    script.write_text(
        "import sys, time\n"
        f"open({str(marker)!r}, 'w').write(' '.join(sys.argv[1:]) + ' ' + str(time.time()))\n")
    holder = socket.socket()
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    port = holder.getsockname()[1]
    log = tmp_path / "run.log"
    proc = platform_compat.spawn_after_port_free(
        str(script), ["run", "x"], port, log_path=str(log), cwd=str(tmp_path))
    time.sleep(1.2)
    assert not marker.exists()  # still waiting on the held port
    released = time.time()
    holder.close()
    proc.wait(timeout=20)
    text = marker.read_text().split()
    assert text[:2] == ["run", "x"]
    assert float(text[2]) >= released - 0.1


def test_no_posix_shell_in_launchers():
    import inspect

    import self_edit
    import webui
    for mod in (webui, self_edit):
        src = inspect.getsource(mod)
        assert '"/bin/sh"' not in src
        assert "os.execv(" not in src  # only platform_compat may exec
