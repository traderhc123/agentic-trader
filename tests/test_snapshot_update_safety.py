"""The zip updater (installs without git) may only install the commit the
agent ITSELF last saw at the head of the official branch — never a commit id
the browser sends — and a failed install must leave the old version whole.
No network: GitHub is stubbed."""

import io
import os
import zipfile

import pytest

import snapshot_update as su

SHA_OLD = "a" * 40
SHA_NEW = "c" * 40
SHA_OTHER = "e" * 40   # e.g. a commit that only exists in somebody's fork


class _Resp:
    def __init__(self, json_body=None, content=b""):
        self.status_code = 200
        self._json = json_body
        self.text = ""
        self._content = content

    def json(self):
        return self._json

    def raise_for_status(self):
        pass

    def iter_content(self, n):
        for i in range(0, len(self._content), n):
            yield self._content[i:i + n]


def _zip(files, sha=SHA_NEW):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(f"agentic-trader-{sha}/{name}", data)
    return buf.getvalue()


@pytest.fixture
def install(tmp_path):
    d = tmp_path / "install"
    d.mkdir()
    (d / "agent.py").write_bytes(b"old agent\n")
    (d / "b.py").write_bytes(b"old b\n")
    (d / su.VERSION_FILE).write_text(SHA_OLD, encoding="utf-8")
    return d


@pytest.fixture
def github(monkeypatch):
    """Official main is at SHA_NEW; records every URL asked for."""
    urls = []

    def get(url, **k):
        urls.append(url)
        if url.endswith("/commits"):
            return _Resp(json_body=[
                {"sha": s, "commit": {"message": "change"}}
                for s in (SHA_NEW, SHA_OLD)])
        return _Resp(content=_zip({"agent.py": "new agent\n", "b.py": "new b\n",
                                   "sub/c.py": "new c\n"},
                                  sha=url.rsplit("/", 1)[1]))
    monkeypatch.setattr(su, "_get", get)
    return urls


def _tree(install):
    return {str(p.relative_to(install)).replace(os.sep, "/"): p.read_bytes()
            for p in install.rglob("*") if p.is_file()}


# ── only the commit the agent's own check reported ──

def test_pull_refuses_a_commit_the_agent_never_checked(install, github):
    before = _tree(install)
    ok, note = su.pull(str(install), SHA_NEW)    # no check in this process
    assert not ok and "Check for code updates" in note
    assert _tree(install) == before and github == []   # nothing downloaded


def test_pull_refuses_a_different_commit_than_the_one_checked(install, github):
    assert su.check(str(install))["target"] == SHA_NEW
    before = _tree(install)
    ok, note = su.pull(str(install), SHA_OTHER)
    assert not ok and "Check for code updates" in note
    assert _tree(install) == before
    assert not any("/zip/" in u for u in github)


def test_pull_installs_the_commit_the_check_reported(install, github):
    target = su.check(str(install))["target"]
    ok, note = su.pull(str(install), target)
    assert ok, note
    assert (install / "agent.py").read_bytes() == b"new agent\n"
    assert (install / "sub" / "c.py").read_bytes() == b"new c\n"
    assert su.installed_commit(str(install)) == SHA_NEW
    assert not (install / su.STAGING_DIR).exists()


# ── all or nothing ──

def test_failed_install_is_undone(install, github, monkeypatch):
    su.check(str(install))
    before = _tree(install)
    real_replace, calls = os.replace, []

    def flaky(src, dst):
        calls.append(dst)
        if len(calls) == 2:     # first file is already in place
            raise PermissionError(13, "file is in use", str(dst))
        return real_replace(src, dst)
    monkeypatch.setattr(su.os, "replace", flaky)
    try:
        ok, note = su.pull(str(install), SHA_NEW)
    except OSError as exc:      # the pre-fix behaviour: blew up half-way
        ok, note = False, f"raised {exc}"
    monkeypatch.setattr(su.os, "replace", real_replace)
    now = {k: v for k, v in _tree(install).items()
           if not k.startswith(su.BACKUP_DIR)}
    assert now == before        # old code whole, old version still recorded
    assert not ok and "undone" in note


def test_unreadable_archive_entry_changes_nothing(install, github, monkeypatch):
    su.check(str(install))
    before = _tree(install)
    real_read = zipfile.ZipFile.read

    def read(self, member, *a, **k):
        name = member if isinstance(member, str) else member.filename
        if name.endswith("sub/c.py"):
            raise zipfile.BadZipFile("Bad CRC-32 for file 'sub/c.py'")
        return real_read(self, member, *a, **k)
    monkeypatch.setattr(zipfile.ZipFile, "read", read)
    try:
        ok, note = su.pull(str(install), SHA_NEW)
    except zipfile.BadZipFile as exc:
        ok, note = False, f"raised {exc}"
    assert _tree(install) == before
    assert not ok and "nothing was changed" in note
