"""Self-edit guards: FOUNDATION immutability, path safety, rollback."""

import json
import os

import pytest

import self_edit


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Fake repo + fake home so apply() can never touch the real tree."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "agent.py").write_text("# the real consent gate lives here\n")
    (repo / "module.py").write_text("VALUE = 1\n")
    (repo / "brokers").mkdir()
    (repo / "brokers" / "alpaca.py").write_text("ORIGINAL = True\n")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("AGENT_HOME", str(home))
    monkeypatch.setattr(self_edit, "_repo", lambda: str(repo))
    return repo, home


def _stage_proposal(home, changes):
    with open(os.path.join(str(home), "proposal.json"), "w") as f:
        json.dump({"request": "test", "explanation": "", "warnings": [],
                   "changes": changes}, f)


# ── FOUNDATION lock ──────────────────────────────────────────────────────────

def test_foundation_covers_the_safety_critical_files():
    assert {"agent.py", "self_edit.py", "llm_policy.py", "lightning_wallet.py",
            "DISCLAIMER.md", "install.sh"} <= self_edit.FOUNDATION


def test_apply_refuses_foundation_file(sandbox):
    repo, home = sandbox
    _stage_proposal(home, [{"path": "agent.py", "content": "pwned = True\n"}])
    ok, msg = self_edit.apply_and_restart(restart=False)
    assert not ok and "FOUNDATION" in msg
    assert self_edit.current() is None  # proposal discarded, not retryable
    assert "pwned" not in (repo / "agent.py").read_text()


def test_apply_refuses_foundation_even_mixed_with_safe_changes(sandbox):
    """A proposal must be rejected atomically — no partial application."""
    repo, home = sandbox
    _stage_proposal(home, [
        {"path": "module.py", "content": "VALUE = 2\n"},
        {"path": "self_edit.py", "content": "FOUNDATION = frozenset()\n"},
    ])
    ok, msg = self_edit.apply_and_restart(restart=False)
    assert not ok
    assert (repo / "module.py").read_text() == "VALUE = 1\n"  # untouched


# ── path safety (apply re-validates; never trusts the stored proposal) ──────

@pytest.mark.parametrize("path", [
    "../outside.py",
    "/etc/cron.d/evil",
    ".git/hooks/post-checkout",
    ".hidden/x.py",
    "brokers/../../outside.py",
])
def test_apply_refuses_illegal_paths(sandbox, path):
    repo, home = sandbox
    _stage_proposal(home, [{"path": path, "content": "x = 1\n"}])
    ok, msg = self_edit.apply_and_restart(restart=False)
    assert not ok and "illegal path" in msg
    assert self_edit.current() is None


# ── rollback ─────────────────────────────────────────────────────────────────

def test_apply_rolls_back_on_syntax_error(sandbox):
    repo, home = sandbox
    _stage_proposal(home, [
        {"path": "module.py", "content": "VALUE = 2\n"},
        {"path": "brokers/alpaca.py", "content": "def broken(:\n"},
    ])
    ok, msg = self_edit.apply_and_restart(restart=False)
    assert not ok and "rolled back" in msg
    assert (repo / "module.py").read_text() == "VALUE = 1\n"
    assert (repo / "brokers" / "alpaca.py").read_text() == "ORIGINAL = True\n"


def test_apply_success_writes_backs_up_and_consumes(sandbox):
    repo, home = sandbox
    _stage_proposal(home, [{"path": "module.py", "content": "VALUE = 2\n"}])
    ok, msg = self_edit.apply_and_restart(restart=False)
    assert ok
    assert (repo / "module.py").read_text() == "VALUE = 2\n"
    assert self_edit.current() is None  # consumed
    backups = list((home / "backups").rglob("module.py"))
    assert backups and backups[0].read_text() == "VALUE = 1\n"


def test_apply_can_create_new_source_files(sandbox):
    repo, home = sandbox
    _stage_proposal(home, [{"path": "sources/rss.py", "content": "NAME = 'rss'\n"}])
    ok, _ = self_edit.apply_and_restart(restart=False)
    assert ok
    assert (repo / "sources" / "rss.py").read_text() == "NAME = 'rss'\n"


def test_apply_with_no_proposal(sandbox):
    ok, msg = self_edit.apply_and_restart(restart=False)
    assert not ok and "no pending proposal" in msg


# ── propose() gating (no LLM involved in these paths) ────────────────────────

def test_propose_requires_enable_flag(sandbox):
    ok, msg = self_edit.propose({}, "add an RSS source")
    assert not ok and "self-edit on" in msg.lower().replace("'", "")


def test_propose_requires_api_key(sandbox, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    ok, msg = self_edit.propose({"self_edit_enabled": True}, "do a thing")
    assert not ok and "key" in msg.lower()


# ── source-tree scan ─────────────────────────────────────────────────────────

def test_repo_files_skips_hidden_git_and_pycache(sandbox):
    repo, _ = sandbox
    (repo / ".git").mkdir()
    (repo / ".git" / "config").write_text("secret")
    (repo / "__pycache__").mkdir()
    (repo / "__pycache__" / "x.pyc").write_text("junk")
    (repo / ".env").write_text("KEY=1")
    files = self_edit._repo_files()
    assert "module.py" in files and "brokers/alpaca.py" in files
    assert not any(p.startswith(".") or "__pycache__" in p for p in files)
