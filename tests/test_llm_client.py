"""Any-provider AI client: detection, config, OpenAI-compatible quirks, and
the policy brain's fail-safe on every non-Claude failure shape."""

import json

import pytest

import llm_client
import llm_policy
from conftest import make_event


class R:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code, self._p = status, payload
        self.text = text or json.dumps(payload or {})

    def json(self):
        if self._p is None:
            raise ValueError("no json")
        return self._p


def _chat(content, finish="stop"):
    return R(200, {"choices": [{"message": {"content": content},
                                "finish_reason": finish}]})


GROK = {"llm_provider": "xai", "llm_api_key": "xai-abc", "llm_model": "grok-4.6"}


@pytest.mark.parametrize("key,pid", [
    ("sk-ant-api03-x", "anthropic"), ("xai-123", "xai"), ("sk-or-v1-x", "openrouter"),
    ("AIzaSyX", "gemini"), ("gsk_abc", "groq"), ("sk-proj-abc", "openai"),
    ("sk-abc", "openai"), ("nonsense", None), ("", None)])
def test_detect_provider(key, pid):
    assert llm_client.detect_provider(key) == pid


def test_resolve_back_compat_and_defaults(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert llm_client.resolve({}) is None
    r = llm_client.resolve({"anthropic_api_key": "sk-ant-x"})
    assert r["provider"] == "anthropic" and r["model"] == "claude-opus-4-8"
    assert llm_client.resolve(GROK)["base_url"] == "https://api.x.ai/v1"
    # non-Claude providers need an explicit model
    assert llm_client.resolve({"llm_provider": "openai", "llm_api_key": "sk-x"}) is None
    # local model needs no key
    assert llm_client.resolve({"llm_provider": "ollama", "llm_model": "llama3"})


def test_env_key_used_when_config_has_none(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "xai-env")
    r = llm_client.resolve({"llm_provider": "xai", "llm_model": "grok-4.6"})
    assert r and r["key"] == "xai-env"


@pytest.mark.parametrize("url,ok", [
    ("https://api.example.com/v1", True), ("http://localhost:11434/v1", True),
    ("http://127.0.0.1:1234/v1", True), ("http://evil.example.com/v1", False),
    ("http://localhost.evil.com/v1", False), ("ftp://x", False)])
def test_keys_only_travel_over_https_or_localhost(url, ok):
    assert bool(llm_client.safe_base_url(url)) is ok
    cfg = {"llm_provider": "custom", "llm_api_key": "k", "llm_model": "m",
           "llm_base_url": url}
    assert llm_client.configured(cfg) is ok


def test_openai_compat_sends_bearer_and_parses(monkeypatch):
    seen = []

    def post(url, headers=None, json=None, timeout=None):
        seen.append((url, headers, json))
        return _chat("hello")
    monkeypatch.setattr(llm_client.requests, "post", post)
    assert llm_client.complete(GROK, "sys", "hi") == "hello"
    url, headers, body = seen[0]
    assert url == "https://api.x.ai/v1/chat/completions"
    assert headers["Authorization"] == "Bearer xai-abc"
    assert body["model"] == "grok-4.6"
    assert body["messages"][0] == {"role": "system", "content": "sys"}


def test_structured_output_degrades_json_schema_then_json_object(monkeypatch):
    formats = []

    def post(url, headers=None, json=None, timeout=None):
        fmt = (json.get("response_format") or {}).get("type")
        formats.append(fmt)
        if fmt == "json_schema":
            return R(400, {"error": "response_format json_schema unsupported"})
        return _chat('{"act": true, "reason": "ok"}')
    monkeypatch.setattr(llm_client.requests, "post", post)
    out = llm_client.complete_json(GROK, "s", "u", {"type": "object"})
    assert out == {"act": True, "reason": "ok"}
    assert formats == ["json_schema", "json_object"]


def test_max_completion_tokens_fallback(monkeypatch):
    params = []

    def post(url, headers=None, json=None, timeout=None):
        params.append([k for k in json if "tokens" in k][0])
        if "max_tokens" in json:
            return R(400, text="Unsupported parameter: 'max_tokens'. Use "
                               "'max_completion_tokens' instead.")
        return _chat("ok")
    monkeypatch.setattr(llm_client.requests, "post", post)
    assert llm_client.complete({**GROK, "llm_provider": "openai",
                                "llm_api_key": "sk-x"}, "s", "u") == "ok"
    assert params[:2] == ["max_tokens", "max_completion_tokens"]


@pytest.mark.parametrize("text", ['```json\n{"a": 1}\n```', 'Sure! {"a": 1} hope it helps',
                                  '{"a": 1}'])
def test_parse_json_is_lenient(text):
    assert llm_client.parse_json(text) == {"a": 1}


@pytest.mark.parametrize("text", ["yes", "[1,2]", "{broken", ""])
def test_parse_json_rejects_garbage(text):
    with pytest.raises(llm_client.LLMError):
        llm_client.parse_json(text)


@pytest.mark.parametrize("status,reason", [(401, "auth"), (429, "rate_limited"),
                                           (500, "api_error")])
def test_http_errors_map_to_reasons(monkeypatch, status, reason):
    monkeypatch.setattr(llm_client.requests, "post",
                        lambda *a, **k: R(status, {"error": "x"}))
    with pytest.raises(llm_client.LLMError) as e:
        llm_client.complete(GROK, "s", "u")
    assert e.value.reason == reason


def test_content_filter_is_a_refusal(monkeypatch):
    monkeypatch.setattr(llm_client.requests, "post",
                        lambda *a, **k: _chat("", finish="content_filter"))
    with pytest.raises(llm_client.LLMError) as e:
        llm_client.complete(GROK, "s", "u")
    assert e.value.reason == "refused"


def test_list_models_and_suggest(monkeypatch):
    monkeypatch.setattr(llm_client.requests, "get", lambda *a, **k: R(200, {
        "data": [{"id": "grok-3-mini"}, {"id": "grok-4.6"}, {"id": "grok-4.6-fast"}]}))
    models = llm_client.list_models("xai", "xai-abc")
    assert models == ["grok-3-mini", "grok-4.6", "grok-4.6-fast"]
    assert llm_client.suggest_model("xai", models) == "grok-4.6"
    monkeypatch.setattr(llm_client.requests, "get", lambda *a, **k: R(401, {}))
    with pytest.raises(llm_client.LLMError) as e:
        llm_client.list_models("xai", "bad")
    assert e.value.reason == "auth"


def test_gemini_style_model_names_are_cleaned(monkeypatch):
    monkeypatch.setattr(llm_client.requests, "get", lambda *a, **k: R(200, {
        "data": [{"id": "models/gemini-2.5-pro"}]}))
    assert llm_client.list_models("gemini", "AIzaX") == ["gemini-2.5-pro"]


# ── policy brain on a non-Claude model ───────────────────────────────────────

@pytest.fixture
def policy(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_HOME", str(tmp_path))
    (tmp_path / "policy.md").write_text("- Skip puts.\n")


@pytest.mark.parametrize("reply,act", [
    ('{"act": true, "reason": "complies"}', True),
    ('{"act": false, "reason": "policy: skip puts"}', False),
    ('{"act": "true", "reason": "string, not bool"}', False),   # fail-safe
    ('{"reason": "forgot act"}', False),                         # fail-safe
    ("I think this trade is fine!", False),                      # fail-safe
])
def test_policy_brain_on_grok_is_strict(policy, monkeypatch, reply, act):
    monkeypatch.setattr(llm_client.requests, "post", lambda *a, **k: _chat(reply))
    v = llm_policy.evaluate(GROK, make_event(), [])
    assert v["act"] is act


def test_policy_brain_without_any_model_skips(policy, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    v = llm_policy.evaluate({}, make_event(), [])
    assert v["act"] is False and "Connect your AI" in v["reason"]


def test_policy_brain_network_error_skips(policy, monkeypatch):
    import requests

    def boom(*a, **k):
        raise requests.ConnectionError("down")
    monkeypatch.setattr(llm_client.requests, "post", boom)
    v = llm_policy.evaluate(GROK, make_event(), [])
    assert v["act"] is False and "network" in v["reason"]


def test_http_400_bad_key_is_auth(monkeypatch):
    monkeypatch.setattr(llm_client.requests, "get", lambda *a, **k: R(
        400, text='{"error":"Incorrect API key provided: xa***00."}'))
    with pytest.raises(llm_client.LLMError) as e:
        llm_client.list_models("xai", "xai-bad")
    assert e.value.reason == "auth"
