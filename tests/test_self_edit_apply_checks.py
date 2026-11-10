"""Self-edit: the diff the owner approves must be the real diff on Windows
too, and an approved change that compiles but cannot start must be rolled
back — not left as a crash-looping install with no dashboard."""

import json
import ntpath
import os

import pytest

import llm_client
import self_edit


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Fake repo + fake home so nothing touches the real tree."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "agent.py").write_text("from module import VALUE\n", encoding="utf-8")
    (repo / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo / "sources").mkdir()
    (repo / "sources" / "__init__.py").write_text(
        "SOURCES = {}\n# registry\n", encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("AGENT_HOME", str(home))
    monkeypatch.setattr(self_edit, "_repo", lambda: str(repo))
    return repo, home


def _stage(home, changes):
    with open(os.path.join(str(home), "proposal.json"), "w",
              encoding="utf-8") as f:
        json.dump({"request": "test", "explanation": "", "warnings": [],
                   "changes": changes}, f)


# ── I4a: Windows paths ──

class _WindowsPath:
    """os.path as self_edit sees it on Windows: backslash separators."""
    normpath = staticmethod(ntpath.normpath)
    isabs = staticmethod(ntpath.isabs)

    def __getattr__(self, name):
        return getattr(os.path, name)


class _WindowsOS:
    sep = "\\"
    path = _WindowsPath()

    def __getattr__(self, name):
        return getattr(os, name)


def test_windows_diff_of_a_file_in_a_folder_is_the_real_diff(sandbox, monkeypatch):
    repo, home = sandbox
    new = "SOURCES = {}\n# registry\nimport sources.rss\n"
    monkeypatch.setattr(llm_client, "configured", lambda cfg: True)
    monkeypatch.setattr(llm_client, "complete_json", lambda *a, **k: {
        "explanation": "register rss",
        "files": [{"path": "sources/__init__.py", "content": new},
                  # unchanged files the model echoes back are not "changes"
                  {"path": "sources/../module.py", "content": "VALUE = 1\n"}]})
    monkeypatch.setattr(self_edit, "os", _WindowsOS())
    ok, msg = self_edit.propose({"self_edit_enabled": True}, "add rss")
    assert ok, msg
    changes = self_edit.current()["changes"]
    assert [c["path"] for c in changes] == ["sources/__init__.py"]
    body = [ln for ln in changes[0]["diff"].splitlines()
            if not ln.startswith(("---", "+++", "@@"))]
    # one added line; the two existing lines are context, not "new"
    assert [ln for ln in body if ln.startswith("+")] == ["+import sources.rss"]
    assert " SOURCES = {}" in body and "\\" not in changes[0]["diff"]


# ── I4b: the new code must actually start ──

def test_apply_rolls_back_code_that_compiles_but_cannot_import(sandbox):
    repo, home = sandbox
    _stage(home, [
        # the agent loads module.py, which now loads the new source
        {"path": "module.py", "content": "from sources import rss\nVALUE = 2\n"},
        {"path": "sources/rss.py",
         "content": "import a_module_nobody_has_installed\nNAME = 'rss'\n"},
    ])
    ok, msg = self_edit.apply_and_restart(restart=False)
    assert not ok and "rolled back" in msg
    assert "a_module_nobody_has_installed" in msg   # the owner sees why
    assert (repo / "module.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert not (repo / "sources" / "rss.py").exists()


def test_apply_rolls_back_a_change_that_stops_the_agent_starting(sandbox):
    """module.py is fine on its own, but agent.py needs the name it lost."""
    repo, home = sandbox
    _stage(home, [{"path": "module.py", "content": "OTHER = 1\n"}])
    ok, msg = self_edit.apply_and_restart(restart=False)
    assert not ok and "rolled back" in msg and "agent" in msg
    assert (repo / "module.py").read_text(encoding="utf-8") == "VALUE = 1\n"


def test_apply_keeps_working_changes_and_does_not_run_test_files(sandbox):
    repo, home = sandbox
    _stage(home, [
        {"path": "module.py", "content": "VALUE = 2\n"},
        {"path": "sources/rss.py", "content": "NAME = 'rss'\n"},
        # needs a dev-only package the owner's install doesn't have
        {"path": "tests/test_rss.py",
         "content": "import a_module_nobody_has_installed\n"},
        {"path": "notes.md", "content": "not code\n"},
    ])
    ok, msg = self_edit.apply_and_restart(restart=False)
    assert ok, msg
    assert (repo / "module.py").read_text(encoding="utf-8") == "VALUE = 2\n"
    assert (repo / "sources" / "rss.py").exists()
    assert (repo / "tests" / "test_rss.py").exists()


def test_apply_rolls_back_when_the_start_check_cannot_run(sandbox, monkeypatch):
    repo, home = sandbox

    def no_python(*a, **k):
        raise OSError("cannot start python")
    monkeypatch.setattr(self_edit.subprocess, "run", no_python)
    _stage(home, [{"path": "module.py", "content": "VALUE = 2\n"}])
    ok, msg = self_edit.apply_and_restart(restart=False)
    assert not ok and "rolled back" in msg
    assert (repo / "module.py").read_text(encoding="utf-8") == "VALUE = 1\n"


# ── review: Apply must not RUN a file that the agent itself never loads ──

def test_apply_does_not_run_a_new_standalone_script(sandbox):
    """"Write me a script I can run by hand to sell everything": approving it
    saves the file. It must not be executed by the start check."""
    repo, home = sandbox
    ran = home / "script-ran.txt"
    _stage(home, [{"path": "panic_sell.py", "content": (
        "import agent\n"
        f"open({str(ran)!r}, 'w', encoding='utf-8').write('sold everything')\n")}])
    ok, msg = self_edit.apply_and_restart(restart=False)
    assert ok, msg
    assert (repo / "panic_sell.py").exists()
    assert not ran.exists()


def test_apply_still_checks_code_the_agent_loads_later(sandbox):
    """An import inside a function (the agent has many) is still followed: a
    new module reached only that way must load, or the change is undone."""
    repo, home = sandbox
    _stage(home, [
        {"path": "module.py", "content": (
            "VALUE = 2\n\n\ndef later():\n"
            "    from sources import rss\n    return rss\n")},
        {"path": "sources/rss.py",
         "content": "import a_module_nobody_has_installed\nNAME = 'rss'\n"},
    ])
    ok, msg = self_edit.apply_and_restart(restart=False)
    assert not ok and "rolled back" in msg
    assert "a_module_nobody_has_installed" in msg
    assert not (repo / "sources" / "rss.py").exists()
