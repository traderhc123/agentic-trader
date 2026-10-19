"""Connect your AI: switch to any model the saved key can use without
pasting the key again — and never send that key anywhere else."""

import webui

XAI = {"llm_provider": "xai", "llm_api_key": "xai-secret", "llm_model": "grok-4"}


def test_blank_key_reuses_saved_key():
    assert webui._reuse_saved_llm(XAI, "", "", "") == ("xai", "xai-secret", "")
    assert webui._reuse_saved_llm(XAI, "xai", "", "") == ("xai", "xai-secret", "")


def test_saved_key_reused_with_its_own_preset_url():
    import llm_client
    preset = llm_client.PROVIDERS["xai"]["base_url"]
    assert webui._reuse_saved_llm(XAI, "", "", preset)[1] == "xai-secret"


def test_typed_key_wins():
    assert webui._reuse_saved_llm(XAI, "", "sk-new", "") == ("", "sk-new", "")


def test_legacy_anthropic_key_is_reused(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cfg = {"anthropic_api_key": "sk-ant-old", "llm_model": "claude-opus-4-8"}
    assert webui._reuse_saved_llm(cfg, "", "", "") == ("anthropic", "sk-ant-old", "")


def test_saved_key_never_sent_to_another_provider_or_url():
    assert webui._reuse_saved_llm(XAI, "openai", "", "") == ("openai", "", "")
    assert webui._reuse_saved_llm(XAI, "", "", "https://evil.example/v1") == \
        ("", "", "https://evil.example/v1")


def test_nothing_saved(monkeypatch):
    for v in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "XAI_API_KEY"):
        monkeypatch.delenv(v, raising=False)
    assert webui._reuse_saved_llm({}, "", "", "") == ("", "", "")


def test_wizard_tells_owner_the_key_is_on_file():
    assert "saved key on file" in webui._WIZARD_HTML
