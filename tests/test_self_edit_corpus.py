"""Self-edit proposals send the source tree FIRST (sorted, byte-identical
for every request on an unchanged tree) and the owner's request LAST, so a
provider's automatic prefix cache can reuse the tree. self_edit_corpus =
"lean" leaves tests/ out (and says so). No network: the LLM is a stub."""

import pytest

import llm_client
import self_edit


@pytest.fixture
def repo(tmp_path, monkeypatch):
    r = tmp_path / "repo"
    (r / "tests").mkdir(parents=True)
    (r / "zeta.py").write_text("Z = 1\n")
    (r / "alpha.py").write_text("A = 1\n")
    (r / "tests" / "test_alpha.py").write_text("def test_a():\n    pass\n")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("AGENT_HOME", str(home))
    monkeypatch.setattr(self_edit, "_repo", lambda: str(r))
    return r


@pytest.fixture
def asked(monkeypatch):
    """The user messages sent to the model."""
    out = []
    monkeypatch.setattr(llm_client, "configured", lambda cfg: True)

    def complete_json(cfg, system, user, schema, **kw):
        out.append(user)
        return {"explanation": "nothing to do", "files": []}
    monkeypatch.setattr(llm_client, "complete_json", complete_json)
    return out


def test_tree_precedes_request_and_is_sorted(repo, asked):
    cfg = {"self_edit_enabled": True}
    self_edit.propose(cfg, "add an RSS source")
    self_edit.propose(cfg, "make the log quieter")
    one, two = asked
    req1, req2 = one.index("add an RSS source"), two.index("make the log quieter")
    # everything before the request is the same bytes for both requests
    assert one[:req1] == two[:req2]
    assert one.startswith("CURRENT SOURCE TREE:\n")
    assert one.rstrip().endswith("OWNER'S REQUEST:\nadd an RSS source")
    order = [one.index(f"===== {p} =====")
             for p in ("alpha.py", "tests/test_alpha.py", "zeta.py")]
    assert order == sorted(order)


def test_full_is_default(repo, asked):
    self_edit.propose({"self_edit_enabled": True}, "x")
    assert "===== tests/test_alpha.py =====" in asked[0]
    assert self_edit.LEAN_NOTE not in asked[0]
    assert self_edit.corpus_mode({}) == "full"
    assert self_edit.corpus_mode({"self_edit_corpus": "bogus"}) == "full"


def test_lean_excludes_tests_and_says_so(repo, asked):
    self_edit.propose({"self_edit_enabled": True, "self_edit_corpus": "lean"}, "x")
    (msg,) = asked
    assert "tests/test_alpha.py" not in msg and "===== alpha.py =====" in msg
    assert self_edit.LEAN_NOTE in msg
    # the note sits after the tree: the tree is still the shared prefix
    assert msg.index(self_edit.LEAN_NOTE) > msg.index("===== zeta.py =====")
