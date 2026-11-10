"""The installers on machines that are NOT set up for developers.

install.sh runs for real here, but inside a sandbox: its PATH holds only a
handful of file tools plus FAKE git / curl / python / uname, so nothing is
downloaded, no real environment is built and nothing outside tmp_path is
touched. (Skipped where there is no bash — Windows uses install.ps1.)
"""

import io
import json
import os
import shutil
import subprocess
import sys
import tarfile

import pytest

import provision

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASH = shutil.which("bash")
needs_bash = pytest.mark.skipif(sys.platform == "win32" or not BASH,
                                reason="needs bash (install.sh is the "
                                       "macOS/Linux installer)")

_FILE_TOOLS = ("bash", "sh", "env", "mktemp", "mkdir", "cp", "rm", "tar",
               "gzip", "cat", "dirname", "basename", "touch")

# "new enough" Python that makes fake venvs; pip exists only in a healthy one
_FAKE_PYTHON = r'''
if [ "$1" = "-c" ]; then
  case "$2" in *print*) echo "3.12" ;; esac
  exit 0
fi
here="$(dirname "$0")"
if [ "$(basename "$0")" = "pip" ]; then exit 0; fi
if [ "$1" = "-m" ] && [ "$2" = "venv" ]; then
  mkdir -p "$3/bin"
  cp "$0" "$3/bin/python"
  if [ -n "${FAKE_NO_ENSUREPIP:-}" ]; then
    echo "The virtual environment was not created successfully because ensurepip is not available." >&2
    exit 1
  fi
  cp "$0" "$3/bin/pip"
  exit 0
fi
if [ "$1" = "-m" ] && [ "$2" = "pip" ]; then
  [ -f "$here/pip" ] || { echo "No module named pip" >&2; exit 1; }
fi
exit 0
'''
# stock macOS: /usr/bin/git exists but only asks to install developer tools
_GIT_PLACEHOLDER = r'''
echo "xcode-select: note: No developer tools were found, requesting install." >&2
exit 1
'''
_GIT_CANNOT_CLONE = r'''
if [ "$1" = "--version" ]; then echo "git version 2.43.0"; exit 0; fi
echo "fatal: unable to access 'https://github.com/': Could not resolve host" >&2
exit 128
'''


def _sandbox(tmp_path, stubs):
    """A PATH directory with a few real file tools and the given fakes."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in _FILE_TOOLS:
        real = shutil.which(tool)
        if real:
            os.symlink(real, bin_dir / tool)
    for name, body in stubs.items():
        f = bin_dir / name
        f.write_text("#!/bin/sh\n" + body, encoding="utf-8")
        f.chmod(0o755)
    return bin_dir


def _snapshot_tarball(tmp_path):
    path = tmp_path / "snapshot.tar.gz"
    with tarfile.open(path, "w:gz") as tar:
        for name, data in (("agent.py", b"# agent\n"),
                           ("requirements.txt", b"requests\n")):
            info = tarfile.TarInfo(f"agentic-trader-main/{name}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return path


def _install(tmp_path, stubs, **env):
    stubs = {"python3": _FAKE_PYTHON, "curl": 'cat "$FAKE_TARBALL"\n',
             "uname": 'echo "${FAKE_UNAME:-Linux}"\n', **stubs}
    bin_dir = _sandbox(tmp_path, stubs)
    target = tmp_path / "agentic-trader"
    (tmp_path / "tmp").mkdir()
    r = subprocess.run(
        [BASH, os.path.join(REPO, "install.sh")], cwd=str(tmp_path),
        env={**os.environ, "PATH": str(bin_dir), "HOME": str(tmp_path),
             "TMPDIR": str(tmp_path / "tmp"),
             "AGENTIC_TRADER_DIR": str(target), "AGENTIC_TRADER_NO_LAUNCH": "1",
             "FAKE_TARBALL": str(_snapshot_tarball(tmp_path)), **env},
        capture_output=True, text=True, encoding="utf-8", timeout=120)
    return r, target


# ── I3a: a Mac without developer tools ──

@needs_bash
def test_macos_placeholder_git_falls_back_to_the_snapshot(tmp_path):
    r, target = _install(tmp_path, {"git": _GIT_PLACEHOLDER,
                                    "xcode-select": "exit 2\n"},
                         FAKE_UNAME="Darwin")
    assert r.returncode == 0, r.stdout + r.stderr
    assert (target / "agent.py").exists()
    assert "Installed to" in r.stdout


# stock macOS: /usr/bin/python3 and /usr/bin/git are the same kind of
# placeholder — RUNNING either one pops up the developer-tools dialog
_APPLE_PLACEHOLDER = r'''
touch "$HOME/developer-tools-dialog-popped-up"
echo "xcode-select: note: No developer tools were found, requesting install." >&2
exit 1
'''
# astral.sh's installer, faked: puts a `uv` in ~/.local/bin that "installs" Python
_CURL_WITH_UV = r'''
case "$*" in
  *astral.sh*)
    echo 'mkdir -p "$HOME/.local/bin"'
    echo 'cp "$FAKE_UV" "$HOME/.local/bin/uv"'
    echo 'cp "$FAKE_PY" "$HOME/.local/bin/python3.12"' ;;
  *) cat "$FAKE_TARBALL" ;;
esac
'''
_FAKE_UV = r'''
if [ "$2" = "find" ]; then echo "$HOME/.local/bin/python3.12"; fi
exit 0
'''


@needs_bash
def test_macos_placeholder_python_and_git_are_never_run(tmp_path):
    """A fresh Mac: the installer must get Python (uv) and the code
    (snapshot) without ever running Apple's placeholders."""
    for name, body in (("fake-uv", _FAKE_UV), ("fake-py", _FAKE_PYTHON)):
        f = tmp_path / name
        f.write_text("#!/bin/sh\n" + body, encoding="utf-8")
        f.chmod(0o755)
    r, target = _install(
        tmp_path, {"python3": _APPLE_PLACEHOLDER, "git": _APPLE_PLACEHOLDER,
                   "xcode-select": "exit 2\n", "curl": _CURL_WITH_UV},
        FAKE_UNAME="Darwin", FAKE_UV=str(tmp_path / "fake-uv"),
        FAKE_PY=str(tmp_path / "fake-py"),
        # where Apple keeps them; the sandbox's stand-in for /usr/bin
        AGENTIC_TRADER_APPLE_STUB_DIR=str(tmp_path / "bin"))
    assert r.returncode == 0, r.stdout + r.stderr
    assert not (tmp_path / "developer-tools-dialog-popped-up").exists()
    assert (target / "agent.py").exists() and "Installed to" in r.stdout


@needs_bash
def test_macos_with_developer_tools_uses_its_own_python_and_git(tmp_path):
    """Developer tools installed: the same paths are the real tools."""
    r, target = _install(
        tmp_path, {"git": _GIT_CANNOT_CLONE, "xcode-select": "exit 0\n"},
        FAKE_UNAME="Darwin",
        AGENTIC_TRADER_APPLE_STUB_DIR=str(tmp_path / "bin"))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Python 3.10+ not found" not in r.stdout    # python3 was used
    assert "git could not download the code" in r.stdout   # git was tried


@needs_bash
def test_failed_git_clone_falls_back_to_the_snapshot(tmp_path):
    r, target = _install(tmp_path, {"git": _GIT_CANNOT_CLONE})
    assert r.returncode == 0, r.stdout + r.stderr
    assert (target / "agent.py").exists()
    assert "git could not download the code" in r.stdout


# ── I3b: Ubuntu without python3-venv ──

@needs_bash
def test_half_made_venv_without_pip_is_rebuilt(tmp_path):
    """What the first, failed run leaves behind: .venv/bin/python, no pip."""
    target = tmp_path / "agentic-trader"
    (target / ".venv" / "bin").mkdir(parents=True)
    (target / "agent.py").write_text("# agent\n", encoding="utf-8")
    broken = target / ".venv" / "bin" / "python"
    broken.write_text("#!/bin/sh\n" + _FAKE_PYTHON, encoding="utf-8")
    broken.chmod(0o755)
    r, target = _install(tmp_path, {})
    assert r.returncode == 0, r.stdout + r.stderr
    assert (target / ".venv" / "bin" / "pip").exists()


@needs_bash
def test_missing_venv_package_names_the_apt_command(tmp_path):
    r, target = _install(tmp_path, {"apt-get": "exit 0\n"},
                         FAKE_NO_ENSUREPIP="1")
    assert r.returncode != 0
    assert "sudo apt install -y python3.12-venv" in r.stdout
    assert "run this installer again" in r.stdout
    assert not (target / ".venv").exists()  # nothing half-made to trip over


# ── install.ps1 can't run here; pin the same two guards in its text ──

def _ps1():
    with open(os.path.join(REPO, "install.ps1"), encoding="utf-8") as f:
        return f.read()


def test_windows_installer_falls_back_when_clone_fails():
    clone = _ps1().split("git clone --depth 1 $Repo $Dir", 1)[1]
    guard = clone.split("Get-Snapshot", 1)[0]
    assert "$LASTEXITCODE -ne 0" in guard


def test_windows_installer_rebuilds_a_venv_without_pip():
    src = _ps1()
    assert "-m pip --version" in src.split("-m venv .venv", 1)[0]
    # ...and a failed step stops the installer instead of "Installed to"
    after = src.split("-m venv .venv", 1)[1].split('Write-Host "Installed to', 1)[0]
    assert after.count("$LASTEXITCODE -ne 0") >= 2 and after.count("throw") >= 2


# ── I6: the server's first-boot install retries a failed download ──

_FAKE_CURL_FLAKY = r'''
n=$(cat "$CURL_COUNT" 2>/dev/null || echo 0)
n=$((n + 1))
echo "$n" > "$CURL_COUNT"
if [ "$n" -lt 3 ]; then
  echo "curl: (6) Could not resolve host: raw.githubusercontent.com" >&2
  exit 6
fi
out=""
while [ $# -gt 0 ]; do
  if [ "$1" = "-o" ]; then out="$2"; fi
  shift
done
script='echo installed >> "$INSTALL_RAN"'
if [ -n "$out" ]; then echo "$script" > "$out"; else echo "$script"; fi
'''


@needs_bash
def test_first_boot_retries_a_failed_installer_download(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_HOME", str(tmp_path / "agent-home"))
    doc = json.loads(provision.build_cloud_init().split("\n", 1)[1])
    line = next(c for c in doc["runcmd"] if "install.sh" in c)
    assert line.startswith("for i in 1 2 3; do ")
    # the real command line; only the server's home folder is swapped for tmp
    server_home = tmp_path / "trader"
    server_home.mkdir()
    line = line.replace("/home/trader", str(server_home))
    bin_dir = _sandbox(tmp_path, {
        "curl": _FAKE_CURL_FLAKY, "sleep": "exit 0\n",
        "runuser": 'shift 3\nexec "$@"\n'})   # drop "-u trader --"
    r = subprocess.run(
        [BASH, "-c", line], cwd=str(tmp_path),
        env={**os.environ, "PATH": str(bin_dir),
             "CURL_COUNT": str(tmp_path / "curl-count"),
             "INSTALL_RAN": str(tmp_path / "install-ran")},
        capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (tmp_path / "curl-count").read_text(encoding="utf-8").strip() == "3"
    assert (tmp_path / "install-ran").read_text(
        encoding="utf-8").splitlines() == ["installed"]
