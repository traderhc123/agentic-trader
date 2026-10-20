"""Updates for installs without git (Windows zip installs) + the Update
button's failure paths. No network: GitHub is stubbed."""

import io
import subprocess
import zipfile

import pytest

import net_check
import snapshot_update as su
import webui

SHA_OLD = "a" * 40
SHA_MID = "b" * 40
SHA_NEW = "c" * 40


class _Resp:
    def __init__(self, status=200, json_body=None, text="", content=b""):
        self.status_code = status
        self._json = json_body
        self.text = text
        self._content = content

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, n):
        for i in range(0, len(self._content), n):
            yield self._content[i:i + n]


def _commits(*shas):
    return [{"sha": s, "commit": {"message": f"change {s[:2]}\n\nbody"}} for s in shas]


def _zip(files, top=f"agentic-trader-{SHA_NEW}"):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name if name.startswith("/") or name.startswith("..")
                       else f"{top}/{name}", data)
    return buf.getvalue()


@pytest.fixture
def install(tmp_path):
    d = tmp_path / "install"
    d.mkdir()
    # bytes, like a real zip install — write_text would add \r on Windows
    (d / "agent.py").write_bytes(b"old agent\n")
    (d / "keep.py").write_bytes(b"same\n")
    return d


# ── check ──

def test_check_up_to_date(install, monkeypatch):
    (install / su.VERSION_FILE).write_text(SHA_NEW)
    monkeypatch.setattr(su, "_get", lambda url, **k: _Resp(json_body=_commits(SHA_NEW, SHA_OLD)))
    r = su.check(str(install))
    assert r["ok"] and r["behind"] == 0 and r["target"] == SHA_NEW


def test_check_lists_only_new_commits(install, monkeypatch):
    (install / su.VERSION_FILE).write_text(SHA_OLD)
    monkeypatch.setattr(su, "_get", lambda url, **k: _Resp(
        json_body=_commits(SHA_NEW, SHA_MID, SHA_OLD)))
    r = su.check(str(install))
    assert r["behind"] == 2 and r["target"] == SHA_NEW
    assert "change cc" in r["summary"] and "change aa" not in r["summary"]
    assert not r["version_unknown"]


def test_check_unrecorded_version_still_offers_update(install, monkeypatch):
    monkeypatch.setattr(su, "_get", lambda url, **k: _Resp(json_body=_commits(SHA_NEW)))
    r = su.check(str(install))
    assert r["ok"] and r["behind"] == 1 and r["version_unknown"]
    assert "isn't recorded" in r["summary"]


def test_check_rate_limited(install, monkeypatch):
    monkeypatch.setattr(su, "_get", lambda url, **k: _Resp(
        status=403, text="API rate limit exceeded"))
    r = su.check(str(install))
    assert not r["ok"] and "hourly limit" in r["error"]


# ── pull ──

def test_pull_installs_reviewed_commit_and_backs_up(install, monkeypatch):
    seen = {}

    def get(url, **k):
        seen["url"] = url
        return _Resp(content=_zip({"agent.py": "new agent\n", "keep.py": "same\n",
                                   "sub/new.py": "x\n", ".venv/junk": "no",
                                   "__pycache__/a.pyc": "no"}))
    monkeypatch.setattr(su, "_get", get)
    ok, note = su.pull(str(install), SHA_NEW)
    assert ok, note
    assert seen["url"].endswith(f"/zip/{SHA_NEW}")  # pinned to what was reviewed
    assert (install / "agent.py").read_text() == "new agent\n"
    assert (install / "sub" / "new.py").read_text() == "x\n"
    assert not (install / ".venv").exists() and not (install / "__pycache__").exists()
    assert (install / su.VERSION_FILE).read_text().strip() == SHA_NEW
    backups = list((install / su.BACKUP_DIR).rglob("agent.py"))
    assert len(backups) == 1 and backups[0].read_text() == "old agent\n"
    assert not list((install / su.BACKUP_DIR).rglob("keep.py"))  # unchanged
    assert "2 file(s) changed" in note


@pytest.mark.parametrize("evil", ["../escape.py", "/abs.py"])
def test_pull_refuses_paths_outside_install(install, monkeypatch, evil):
    monkeypatch.setattr(su, "_get", lambda url, **k: _Resp(
        content=_zip({"agent.py": "x", evil: "pwn"})))
    ok, note = su.pull(str(install), SHA_NEW)
    assert not ok and "rejected" in note
    assert (install / "agent.py").read_text() == "old agent\n"


def test_pull_refuses_dotdot_inside_top_folder(install, monkeypatch):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(f"agentic-trader-{SHA_NEW}/agent.py", "x")
        z.writestr(f"agentic-trader-{SHA_NEW}/../../escape.py", "pwn")
    monkeypatch.setattr(su, "_get", lambda url, **k: _Resp(content=buf.getvalue()))
    ok, note = su.pull(str(install), SHA_NEW)
    assert not ok and "unsafe path" in note


def test_pull_refuses_foreign_archive(install, monkeypatch):
    monkeypatch.setattr(su, "_get", lambda url, **k: _Resp(content=_zip({"x.py": "x"})))
    ok, note = su.pull(str(install), SHA_NEW)
    assert not ok and "doesn't look like agentic-trader" in note


def test_pull_needs_a_reviewed_sha(install):
    ok, note = su.pull(str(install), "main")
    assert not ok and "check for updates first" in note


# ── webui wiring ──

def test_webui_uses_snapshot_mode_without_git(install, monkeypatch):
    monkeypatch.setattr(webui, "_repo_dir", lambda: str(install))
    monkeypatch.setattr(su, "check", lambda d: {"ok": True, "behind": 3,
                                                "mode": "snapshot", "target": SHA_NEW})
    monkeypatch.setattr(su, "pull", lambda d, e: (True, f"updated to {e[:12]}"))
    assert webui.check_code_updates()["mode"] == "snapshot"
    assert webui.pull_code_updates(expect=SHA_NEW) == (True, f"updated to {SHA_NEW[:12]}")


def test_snapshot_network_error_is_diagnosed(install, monkeypatch):
    monkeypatch.setattr(webui, "_repo_dir", lambda: str(install))

    def boom(d):
        raise ConnectionError("HTTPSConnectionPool: SSL handshake failed")
    monkeypatch.setattr(su, "check", boom)
    hosts = []
    monkeypatch.setattr(net_check, "diagnose", lambda h: hosts.append(h) or net_check._result(
        False, "intercepted", "Avast is intercepting HTTPS", "In Avast, …"))
    r = webui.check_code_updates()
    assert not r["ok"] and "Can't reach GitHub" in r["error"] and "Avast" in r["error"]
    assert hosts == ["github.com"]


def test_git_fetch_timeout_is_diagnosed_not_silent(tmp_path, monkeypatch):
    repo = tmp_path / "r"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    monkeypatch.setattr(webui, "_repo_dir", lambda: str(repo))
    monkeypatch.setattr(webui, "_remote_problem", lambda: None)

    def slow(args, timeout=60):
        if args and args[0] == "fetch":
            raise subprocess.TimeoutExpired("git fetch", timeout)
        return subprocess.CompletedProcess(args, 0, "", "")
    monkeypatch.setattr(webui, "_git", slow)
    monkeypatch.setattr(net_check, "diagnose", lambda h: net_check._result(
        False, "tls_cut", "github.com is reachable, but the secure connection is cut",
        "turn off HTTPS scanning"))
    r = webui.check_code_updates()
    assert not r["ok"] and "secure connection is cut" in r["error"]


def test_git_never_prompts_for_credentials(tmp_path, monkeypatch):
    seen = {}

    def run(cmd, **kw):
        seen.update(kw.get("env") or {})
        return subprocess.CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr(subprocess, "run", run)
    webui._git(["status"])
    assert seen.get("GIT_TERMINAL_PROMPT") == "0"
