"""Wizard "check for code updates" — git fetch/compare/ff-only pull helpers.

Exercised against a real throwaway git remote (local bare repo) so the
plumbing (fetch, rev-list, ff-only merge, diverged-history refusal) is tested
for real, not mocked.
"""

import subprocess

import pytest

import webui


def _run(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args],
                          capture_output=True, text=True, check=True)


@pytest.fixture
def repos(tmp_path, monkeypatch):
    """origin (bare) + install clone + author clone; webui points at install."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)],
                   capture_output=True, check=True)
    author = tmp_path / "author"
    subprocess.run(["git", "clone", str(origin), str(author)],
                   capture_output=True, check=True)
    _run(author, "config", "user.email", "t@t")
    _run(author, "config", "user.name", "t")
    (author / "f.txt").write_text("v1\n")
    _run(author, "add", "f.txt")
    _run(author, "commit", "-m", "v1")
    _run(author, "push", "origin", "main")
    install = tmp_path / "install"
    subprocess.run(["git", "clone", str(origin), str(install)],
                   capture_output=True, check=True)
    _run(install, "config", "user.email", "t@t")
    _run(install, "config", "user.name", "t")
    monkeypatch.setattr(webui, "_repo_dir", lambda: str(install))
    return author, install


def test_up_to_date(repos):
    r = webui.check_code_updates()
    assert r["ok"] and r["behind"] == 0


def test_behind_then_pull(repos):
    author, install = repos
    (author / "f.txt").write_text("v2\n")
    _run(author, "commit", "-am", "v2 fix")
    _run(author, "push", "origin", "main")

    r = webui.check_code_updates()
    assert r["ok"] and r["behind"] == 1
    assert "v2 fix" in r["summary"]

    ok, note = webui.pull_code_updates()
    assert ok, note
    assert (install / "f.txt").read_text() == "v2\n"
    assert webui.check_code_updates()["behind"] == 0


def test_diverged_history_refused(repos):
    author, install = repos
    (author / "f.txt").write_text("remote\n")
    _run(author, "commit", "-am", "remote change")
    _run(author, "push", "origin", "main")
    (install / "f.txt").write_text("local\n")
    _run(install, "commit", "-am", "local change")

    assert webui.check_code_updates()["ok"]
    ok, note = webui.pull_code_updates()
    assert not ok
    assert "fast-forward" in note


def test_not_a_git_checkout(tmp_path, monkeypatch):
    monkeypatch.setattr(webui, "_repo_dir", lambda: str(tmp_path))
    r = webui.check_code_updates()
    assert not r["ok"] and "not a git checkout" in r["error"]
