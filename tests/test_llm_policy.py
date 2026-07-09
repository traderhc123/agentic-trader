"""Policy brain: veto-only, fail-SAFE on every error path."""

import sys

import pytest

import llm_policy
from conftest import make_event


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("AGENT_HOME", str(h))
    return h


@pytest.fixture
def policy(home):
    (home / "policy.md").write_text("- Skip puts.\n")
    return home


@pytest.fixture
def no_anthropic(monkeypatch):
    """Simulate the SDK being uninstalled."""
    monkeypatch.setitem(sys.modules, "anthropic", None)


def test_no_policy_means_mechanical_mode(home):
    v = llm_policy.evaluate({}, make_event(), [])
    assert v["act"] is True
    assert "no policy" in v["reason"]


def test_enabled_iff_policy_file_nonempty(home):
    assert not llm_policy.enabled({})
    (home / "policy.md").write_text("   \n")
    assert not llm_policy.enabled({})  # whitespace-only = off
    (home / "policy.md").write_text("- Skip puts.\n")
    assert llm_policy.enabled({})


def test_sdk_missing_default_skips(policy, no_anthropic):
    """Error + policy configured -> the safe direction is NOT trading."""
    v = llm_policy.evaluate({}, make_event(), [])
    assert v["act"] is False


def test_sdk_missing_explicit_fallback_act(policy, no_anthropic):
    v = llm_policy.evaluate({"llm_fallback": "act"}, make_event(), [])
    assert v["act"] is True


def _fake_anthropic(raises):
    """Minimal stand-in for the SDK whose client constructor raises."""
    import types

    mod = types.ModuleType("anthropic")
    for name in ("AuthenticationError", "RateLimitError",
                 "APIConnectionError"):
        setattr(mod, name, type(name, (Exception,), {}))
    mod.APIStatusError = type("APIStatusError", (Exception,),
                              {"status_code": 500})

    class Anthropic:
        def __init__(self, **kw):
            raise raises

    mod.Anthropic = Anthropic
    return mod


def test_evaluate_never_raises(policy, monkeypatch):
    """Whatever explodes inside, the heartbeat must survive."""
    monkeypatch.setitem(sys.modules, "anthropic",
                        _fake_anthropic(RuntimeError("kaboom")))
    v = llm_policy.evaluate({}, make_event(), [])
    assert v["act"] is False  # skip fail-safe
    assert "kaboom" in v["reason"]


def test_evaluate_auth_error_reports_key_problem(policy, monkeypatch):
    fake = _fake_anthropic(RuntimeError)
    fake.Anthropic = lambda **kw: (_ for _ in ()).throw(
        fake.AuthenticationError("bad key"))
    monkeypatch.setitem(sys.modules, "anthropic", fake)
    v = llm_policy.evaluate({}, make_event(), [])
    assert v["act"] is False
    assert "key" in v["reason"].lower()


def test_policy_template_is_valid_starting_point():
    assert "Skip puts" in llm_policy.POLICY_TEMPLATE
