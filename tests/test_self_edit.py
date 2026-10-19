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
        {"path": "brokers/new_adapter.py", "content": "def broken(:\n"},
    ])
    ok, msg = self_edit.apply_and_restart(restart=False)
    assert not ok and "rolled back" in msg
    assert (repo / "module.py").read_text() == "VALUE = 1\n"
    assert not (repo / "brokers" / "new_adapter.py").exists()


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
    assert not ok and "connect an ai model" in msg.lower()


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


# ── case-insensitive / Windows name aliasing (Windows + stock macOS) ────────

@pytest.mark.parametrize("alias", ["Agent.py", "AGENT.PY", "agent.py.",
                                   "agent.py ", "agent.py::$DATA",
                                   "agent.py:evil", "Install.PS1"])
def test_apply_refuses_foundation_aliases(sandbox, alias):
    repo, home = sandbox
    _stage_proposal(home, [{"path": alias, "content": "pwned = True\n"}])
    ok, msg = self_edit.apply_and_restart(restart=False)
    assert not ok
    assert "pwned" not in (repo / "agent.py").read_text()


def test_is_foundation_and_illegal_name_helpers():
    assert self_edit._is_foundation("Agent.py")
    assert self_edit._is_foundation("install.ps1")
    assert not self_edit._is_foundation("module.py")
    assert self_edit._illegal_name("brokers/x.py:stream")
    assert self_edit._illegal_name("C:evil.py")
    assert self_edit._illegal_name("brokers./x.py")
    assert not self_edit._illegal_name("brokers/new_broker.py")


# ── order/signal code lock ──────────────────────────────────────────────────

@pytest.mark.parametrize("path", ["brokers/alpaca.py", "Brokers/Alpaca.py",
                                  "webui.py", "requirements.txt"])
def test_apply_refuses_existing_order_path_files(sandbox, path):
    repo, home = sandbox
    (repo / "webui.py").write_text("UI = 1\n")
    (repo / "requirements.txt").write_text("requests\n")
    _stage_proposal(home, [{"path": path, "content": "pwned = True\n"}])
    ok, _msg = self_edit.apply_and_restart(restart=False)
    assert not ok
    assert (repo / "brokers" / "alpaca.py").read_text() == "ORIGINAL = True\n"
    assert (repo / "webui.py").read_text() == "UI = 1\n"


def test_apply_allows_new_broker_adapter_and_registry(sandbox):
    repo, home = sandbox
    (repo / "brokers" / "__init__.py").write_text("BROKERS = {}\n")
    _stage_proposal(home, [
        {"path": "brokers/tasty.py", "content": "NAME = 'tasty'\n"},
        {"path": "brokers/__init__.py", "content": "BROKERS = {'tasty': 1}\n"},
    ])
    ok, _ = self_edit.apply_and_restart(restart=False)
    assert ok
    assert (repo / "brokers" / "tasty.py").exists()


# ── the dashboard app folders are FOUNDATION too ────────────────────────────

@pytest.mark.parametrize("path", [
    "webui_dist/index.html",
    "webui_dist/assets/index-abc.js",
    "WebUI_Dist/assets/x.js",
    "webui_app/src/dash/Dashboard.tsx",
    "webui_app./src/main.tsx",
])
def test_ui_folders_are_foundation(path):
    assert self_edit._is_foundation(path)


def test_apply_refuses_ui_build_files(sandbox):
    repo, home = sandbox
    (repo / "webui_dist" / "assets").mkdir(parents=True)
    (repo / "webui_dist" / "assets" / "app.js").write_text("SAFE\n")
    _stage_proposal(home, [{"path": "webui_dist/assets/app.js",
                            "content": "goLive()\n"}])
    ok, msg = self_edit.apply_and_restart(restart=False)
    assert not ok and "FOUNDATION" in msg
    assert (repo / "webui_dist" / "assets" / "app.js").read_text() == "SAFE\n"


def test_apply_refuses_ui_folder_reached_through_symlink(sandbox):
    repo, home = sandbox
    (repo / "webui_dist").mkdir()
    try:
        os.symlink(repo / "webui_dist", repo / "alias")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    _stage_proposal(home, [{"path": "alias/app.js", "content": "x\n"}])
    ok, msg = self_edit.apply_and_restart(restart=False)
    assert not ok
    assert not (repo / "webui_dist" / "app.js").exists()


def test_repo_files_skip_node_modules(sandbox):
    """`npm ci` in webui_app/ drops thousands of package READMEs; none of
    them belong in what the model is shown."""
    repo, _ = sandbox
    nm = repo / "webui_app" / "node_modules" / "react"
    nm.mkdir(parents=True)
    (nm / "README.md").write_text("package docs\n")
    (repo / "webui_app" / "README.md").write_text("ui docs\n")
    files = self_edit._repo_files()
    assert not any("node_modules" in k for k in files)
    assert os.path.join("webui_app", "README.md") in files
