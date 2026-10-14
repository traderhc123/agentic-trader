"""One door to whichever AI model the owner connected — any provider.

Claude talks through the official Anthropic SDK. Every other provider goes
through the OpenAI-compatible chat API that almost all of them expose
(OpenAI, Grok/xAI, Gemini, OpenRouter, DeepSeek, Mistral, Groq, and local
servers like Ollama / LM Studio), so "any model" needs no extra packages.

Config (config.json, written by the wizard's "Connect your AI" step):
    llm_provider   one of PROVIDERS (absent + anthropic_api_key = "anthropic",
                   the pre-multi-provider layout)
    llm_api_key    the key (or the provider's usual env var, e.g. XAI_API_KEY)
    llm_base_url   only for "custom" or to override a preset
    llm_model      the model id the owner picked

Callers never touch provider details: complete() returns text,
complete_json() returns a dict, and every failure is an LLMError — callers
keep their own fail-safe behavior (the policy brain skips the trade).
"""

import json
import os
import re

import requests

DEFAULT_ANTHROPIC_MODEL = "claude-opus-4-8"

# kind "anthropic" = Anthropic SDK; everything else = OpenAI-compatible.
PROVIDERS = {
    "anthropic": {"name": "Claude (Anthropic)", "kind": "anthropic",
                  "base_url": "https://api.anthropic.com/v1",
                  "env": "ANTHROPIC_API_KEY", "key_prefix": "sk-ant-",
                  "keys_url": "https://console.anthropic.com/settings/keys",
                  "prefer": [DEFAULT_ANTHROPIC_MODEL, "opus", "sonnet"]},
    "openai": {"name": "OpenAI (GPT models)", "kind": "openai",
               "base_url": "https://api.openai.com/v1",
               "env": "OPENAI_API_KEY", "key_prefix": "sk-",
               "keys_url": "https://platform.openai.com/api-keys",
               "prefer": ["gpt-5", "gpt-4.1", "gpt-4o"]},
    "xai": {"name": "Grok (xAI)", "kind": "openai",
            "base_url": "https://api.x.ai/v1",
            "env": "XAI_API_KEY", "key_prefix": "xai-",
            "keys_url": "https://console.x.ai",
            "prefer": ["grok-4"]},
    "gemini": {"name": "Gemini (Google)", "kind": "openai",
               "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
               "env": "GEMINI_API_KEY", "key_prefix": "AIza",
               "keys_url": "https://aistudio.google.com/apikey",
               "prefer": ["gemini-2.5-pro", "gemini"]},
    "openrouter": {"name": "OpenRouter (hundreds of models, one key)", "kind": "openai",
                   "base_url": "https://openrouter.ai/api/v1",
                   "env": "OPENROUTER_API_KEY", "key_prefix": "sk-or-",
                   "keys_url": "https://openrouter.ai/keys", "prefer": []},
    "deepseek": {"name": "DeepSeek", "kind": "openai",
                 "base_url": "https://api.deepseek.com/v1",
                 "env": "DEEPSEEK_API_KEY", "key_prefix": "",
                 "keys_url": "https://platform.deepseek.com/api_keys",
                 "prefer": ["deepseek-chat"]},
    "mistral": {"name": "Mistral", "kind": "openai",
                "base_url": "https://api.mistral.ai/v1",
                "env": "MISTRAL_API_KEY", "key_prefix": "",
                "keys_url": "https://console.mistral.ai/api-keys",
                "prefer": ["mistral-large"]},
    "groq": {"name": "Groq", "kind": "openai",
             "base_url": "https://api.groq.com/openai/v1",
             "env": "GROQ_API_KEY", "key_prefix": "gsk_",
             "keys_url": "https://console.groq.com/keys", "prefer": []},
    "ollama": {"name": "Local model (Ollama / LM Studio)", "kind": "openai",
               "base_url": "http://localhost:11434/v1",
               "env": "", "key_prefix": "", "no_key": True,
               "keys_url": "https://ollama.com", "prefer": []},
    "custom": {"name": "Any other OpenAI-compatible endpoint", "kind": "openai",
               "base_url": "", "env": "", "key_prefix": "",
               "keys_url": "", "prefer": []},
}


class LLMError(RuntimeError):
    """Any model failure. `reason` is a short machine-ish tag:
    not_configured | auth | rate_limited | network | refused | bad_output | api_error"""

    def __init__(self, reason, detail=""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


# ── configuration ────────────────────────────────────────────────────────────

def detect_provider(key):
    """Best guess from the key's prefix (the wizard lets the owner override)."""
    key = (key or "").strip()
    for pid in ("anthropic", "openrouter", "xai", "gemini", "groq"):
        if key.startswith(PROVIDERS[pid]["key_prefix"]):
            return pid
    if key.startswith("sk-"):
        return "openai"
    return None


def safe_base_url(url):
    """Keys only travel over HTTPS — or to this machine."""
    u = (url or "").strip().rstrip("/")
    if u.startswith("https://"):
        return u
    if re.match(r"^http://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?(/|$)", u):
        return u
    return ""


def resolve(cfg):
    """Effective {provider, kind, base_url, key, model} or None if no AI is
    connected. Back-compatible with configs that only have anthropic_api_key."""
    cfg = cfg or {}
    pid = cfg.get("llm_provider")
    if not pid:
        if cfg.get("anthropic_api_key") or os.getenv("ANTHROPIC_API_KEY"):
            pid = "anthropic"
        else:
            return None
    preset = PROVIDERS.get(pid)
    if preset is None:
        return None
    key = (cfg.get("llm_api_key")
           or (cfg.get("anthropic_api_key") if pid == "anthropic" else "")
           or (os.getenv(preset["env"]) if preset["env"] else "") or "")
    if not key and not preset.get("no_key"):
        return None
    base = safe_base_url(cfg.get("llm_base_url") or preset["base_url"])
    if preset["kind"] != "anthropic" and not base:
        return None
    model = cfg.get("llm_model") or (DEFAULT_ANTHROPIC_MODEL if pid == "anthropic" else "")
    if not model:
        return None
    return {"provider": pid, "kind": preset["kind"], "base_url": base,
            "key": key, "model": model, "name": preset["name"]}


def configured(cfg):
    return resolve(cfg) is not None


def describe(cfg):
    """Non-secret one-liner, e.g. 'Grok (xAI) · grok-4.6'."""
    r = resolve(cfg)
    return f"{r['name']} · {r['model']}" if r else "not connected"


def _key_complaint(r):
    """Some providers (xAI, Gemini) answer a bad key with HTTP 400."""
    if r.status_code != 400:
        return False
    low = (r.text or "").lower()
    return "api key" in low or "api_key" in low or "incorrect key" in low


# ── model discovery (wizard) ─────────────────────────────────────────────────

def list_models(provider, key, base_url=None, timeout=15):
    """Model ids the key can use. Doubles as key validation: a rejected key
    raises LLMError('auth')."""
    preset = PROVIDERS.get(provider)
    if preset is None:
        raise LLMError("not_configured", f"unknown provider {provider!r}")
    base = safe_base_url(base_url or preset["base_url"])
    if not base:
        raise LLMError("not_configured", "the endpoint must be https:// (or localhost)")
    if preset["kind"] == "anthropic":
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
        url = f"{base}/models?limit=1000"
    else:
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        url = f"{base}/models"
    try:
        r = requests.get(url, headers=headers, timeout=timeout)
    except requests.RequestException as exc:
        raise LLMError("network", str(exc)[:120])
    if r.status_code in (401, 403) or _key_complaint(r):
        raise LLMError("auth", "the provider rejected that key")
    if r.status_code != 200:
        raise LLMError("api_error", f"HTTP {r.status_code} listing models")
    try:
        data = r.json()
    except ValueError:
        raise LLMError("bad_output", "model list was not JSON")
    items = data.get("data") if isinstance(data, dict) else data
    if not isinstance(items, list) and isinstance(data, dict):
        items = data.get("models") or []
    ids = []
    for m in items or []:
        mid = m.get("id") or m.get("name") if isinstance(m, dict) else m
        if isinstance(mid, str) and mid:
            ids.append(mid.removeprefix("models/"))
    return sorted(set(ids))


def suggest_model(provider, models):
    """Pre-select something sensible; the owner can pick anything."""
    for pref in PROVIDERS.get(provider, {}).get("prefer", []):
        exact = [m for m in models if m == pref]
        if exact:
            return exact[0]
        partial = sorted((m for m in models if pref in m), key=len)
        if partial:
            return partial[0]
    return models[0] if models else ""


# ── calls ────────────────────────────────────────────────────────────────────

def complete(cfg, system, user, max_tokens=1024, schema=None, effort=None,
             long=False, timeout=60):
    """Run one prompt; return the reply text. Raises LLMError."""
    conf = resolve(cfg)
    if conf is None:
        raise LLMError("not_configured", "connect an AI model in Setup first")
    if conf["kind"] == "anthropic":
        return _anthropic(conf, system, user, max_tokens, schema, effort, long, timeout)
    return _openai_compat(conf, system, user, max_tokens, schema, timeout)


def complete_json(cfg, system, user, schema, **kw):
    """Like complete(), but returns the parsed JSON object. Models without
    native structured output still work: the reply is parsed leniently
    (code fences / surrounding prose stripped); anything unparseable raises
    LLMError('bad_output') so the caller's fail-safe applies."""
    text = complete(cfg, system + _JSON_HINT.format(schema=json.dumps(schema)),
                    user, schema=schema, **kw)
    return parse_json(text)


_JSON_HINT = ("\n\nReply with ONLY a JSON object matching this JSON schema, "
              "no prose and no code fences:\n{schema}")


def parse_json(text):
    t = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", t, re.S)
    if fence:
        t = fence.group(1).strip()
    try:
        obj = json.loads(t)
    except ValueError:
        start, end = t.find("{"), t.rfind("}")
        if start < 0 or end <= start:
            raise LLMError("bad_output", "reply was not JSON")
        try:
            obj = json.loads(t[start:end + 1])
        except ValueError:
            raise LLMError("bad_output", "reply was not valid JSON")
    if not isinstance(obj, dict):
        raise LLMError("bad_output", "reply JSON was not an object")
    return obj


def _anthropic(conf, system, user, max_tokens, schema, effort, long, timeout):
    try:
        import anthropic
    except ImportError:
        raise LLMError("not_configured", "pip install anthropic to use Claude")
    output_config = {}
    if effort:
        output_config["effort"] = effort
    if schema:
        output_config["format"] = {"type": "json_schema", "schema": schema}
    kwargs = {"model": conf["model"], "max_tokens": max_tokens, "system": system,
              "messages": [{"role": "user", "content": user}]}
    if output_config:
        kwargs["output_config"] = output_config
    try:
        client = anthropic.Anthropic(api_key=conf["key"] or None, timeout=timeout)
        if long:
            with client.messages.stream(thinking={"type": "adaptive"}, **kwargs) as s:
                response = s.get_final_message()
        else:
            response = client.messages.create(**kwargs)
    except anthropic.AuthenticationError:
        raise LLMError("auth", "Claude API key invalid or missing")
    except anthropic.RateLimitError:
        raise LLMError("rate_limited")
    except anthropic.APIStatusError as exc:
        raise LLMError("api_error", f"HTTP {exc.status_code}")
    except anthropic.APIConnectionError:
        raise LLMError("network")
    if response.stop_reason == "refusal":
        raise LLMError("refused")
    text = next((b.text for b in response.content if b.type == "text"), None)
    if text is None:
        raise LLMError("bad_output", "no text in reply")
    return text


def _openai_compat(conf, system, user, max_tokens, schema, timeout):
    """POST /chat/completions, degrading gracefully across provider quirks:
    json_schema -> json_object -> plain prompt; max_tokens ->
    max_completion_tokens (newer OpenAI models) -> smaller cap."""
    headers = {"Content-Type": "application/json"}
    if conf["key"]:
        headers["Authorization"] = f"Bearer {conf['key']}"
    base = {"model": conf["model"],
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}]}
    formats = [None]
    if schema:
        formats = [{"type": "json_schema",
                    "json_schema": {"name": "reply", "schema": schema, "strict": True}},
                   {"type": "json_object"}, None]
    token_params = [("max_tokens", max_tokens), ("max_completion_tokens", max_tokens),
                    ("max_tokens", min(max_tokens, 8192))]
    last = ""
    for fmt in formats:
        for tp, tv in token_params:
            body = dict(base, **{tp: tv})
            if fmt:
                body["response_format"] = fmt
            try:
                r = requests.post(f"{conf['base_url']}/chat/completions",
                                  headers=headers, json=body, timeout=timeout)
            except requests.RequestException as exc:
                raise LLMError("network", str(exc)[:120])
            if r.status_code in (401, 403) or _key_complaint(r):
                raise LLMError("auth", "the provider rejected the API key")
            if r.status_code == 429:
                raise LLMError("rate_limited")
            if r.status_code == 400 or r.status_code == 422:
                last = r.text[:200]
                low = last.lower()
                if "token" in low and ("max_tokens" in low or "max_completion" in low):
                    continue          # try the next token parameter style
                break                 # likely response_format: try the next format
            if r.status_code != 200:
                raise LLMError("api_error", f"HTTP {r.status_code}: {r.text[:120]}")
            try:
                choice = r.json()["choices"][0]
            except (ValueError, KeyError, IndexError, TypeError):
                raise LLMError("bad_output", "unexpected reply shape")
            if choice.get("finish_reason") == "content_filter":
                raise LLMError("refused")
            content = (choice.get("message") or {}).get("content")
            if isinstance(content, list):
                content = "".join(p.get("text", "") for p in content
                                  if isinstance(p, dict))
            if not content:
                raise LLMError("bad_output", "empty reply")
            return content
    raise LLMError("api_error", f"provider rejected the request: {last}")
