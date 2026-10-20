"""Apply a setup plan written by an AI assistant — everything EXCEPT the owner's own steps.

An assistant (Claude Cowork, Claude Code, …) that is setting the agent up for
its human writes the choices the human made into a JSON plan and runs

    python agent.py setup --from plan.json [--json]

instead of clicking through every wizard page. The plan is validated in full
first — including a real test call to the AI model and the broker's own key
check — and NOTHING is written unless every part passes.

Deliberately impossible from a plan (each needs the human, in the wizard or
dashboard):
  - accepting DISCLAIMER.md (the plan refuses to run until it is accepted)
  - going live: dry_run can only be turned OFF by the owner (`dry off`)
  - turning on self-edit
  - logging in to Robinhood (OAuth in the owner's browser)
  - paying for the feed (the owner pays the invoice from their own wallet)

Sizing is accepted only because the owner chose it; assistants must not
suggest amounts (BOOT.md).
"""

import json
import os

ALLOWED_TOP = {"source", "feed_access", "ai", "broker", "sizing", "safety",
               "notifications", "policy", "rules"}

EXAMPLE_PLAN = {
    "source": {"type": "agenthc", "include_other_trades": False},
    "ai": {"provider": "anthropic", "api_key": "sk-ant-…", "model": "claude-opus-4-8"},
    "broker": {"type": "alpaca", "alpaca_key_id": "…", "alpaca_secret": "…",
               "alpaca_paper": True},
    "sizing": {"mode": "contracts", "contracts_per_trade": 1},
    "safety": {"max_entries_per_day": 3, "max_open_positions": 5,
               "max_daily_loss_usd": 0, "max_contracts_per_trade": 10},
    "notifications": {"ntfy_topic": "my-agent-alerts"},
    # built-in rules: no AI needed (only what the owner actually asked for)
    "rules": {"tickers": ["SPY", "QQQ"], "sides": "calls", "skip_0dte": True,
              "loss_streak": 2},
}

_FORBIDDEN = {
    "dry_run": "going live is the owner's own decision — they say `dry off` "
               "in the dashboard when ready (plans always keep dry-run ON)",
    "self_edit_enabled": "self-edit can only be turned on by the owner "
                         "(`self-edit on` in the dashboard)",
    "consent": "the owner accepts DISCLAIMER.md themselves in the wizard",
    "accepted": "the owner accepts DISCLAIMER.md themselves in the wizard",
}


def _https_or_local(url):
    u = str(url or "")
    return (u.startswith("https://") or u.startswith("http://127.0.0.1")
            or u.startswith("http://localhost"))


def _pos_number(v, name, errors, integer=False, allow_zero=False):
    try:
        n = int(v) if integer else float(v)
    except (TypeError, ValueError):
        errors.append(f"{name} must be a number")
        return None
    if n < 0 or (n == 0 and not allow_zero):
        errors.append(f"{name} must be {'≥ 0' if allow_zero else '> 0'}")
        return None
    return n


def _find_forbidden(obj, path=""):
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in _FORBIDDEN:
                out.append(f"{path + k}: {_FORBIDDEN[k]}")
            out.extend(_find_forbidden(v, f"{path}{k}."))
    return out


def apply(plan, cfg, consent_ok, home):
    """Validate + apply. Returns (new_cfg or None, report). `report` =
    {"ok", "applied": [...], "errors": [...], "owner_steps": [...]}."""
    report = {"ok": False, "applied": [], "errors": [], "owner_steps": []}
    errors = report["errors"]
    if not consent_ok:
        errors.append("the owner must accept the agreement first — open the "
                      "wizard (python agent.py setup --web) and let THEM read "
                      "and accept it; never accept it for them")
        return None, report
    if not isinstance(plan, dict):
        errors.append("plan must be a JSON object")
        return None, report
    errors.extend(_find_forbidden(plan))
    unknown = sorted(set(plan) - ALLOWED_TOP)
    if unknown:
        errors.append(f"unknown plan sections: {', '.join(unknown)} "
                      f"(allowed: {', '.join(sorted(ALLOWED_TOP))})")
    if errors:
        return None, report

    new = dict(cfg)
    applied = report["applied"]

    # ── source ──
    src = plan.get("source")
    if src is not None:
        kind = (src or {}).get("type")
        if kind not in ("agenthc", "url", "manual"):
            errors.append("source.type must be agenthc, url or manual")
        else:
            new["source"] = kind
            if kind == "url":
                if not _https_or_local(src.get("url")):
                    errors.append("source.url must be https:// (or localhost)")
                new["source_url"] = str(src.get("url", "")).strip()
            if kind == "agenthc":
                new["include_other_trades"] = bool(src.get("include_other_trades", False))
            applied.append(f"signal source: {kind}")

    fa = plan.get("feed_access") or {}
    if fa.get("agenthc_api_key"):
        new["agenthc_api_key"] = str(fa["agenthc_api_key"]).strip()
        applied.append("feed access: Premium API key")
    extra = set(fa) - {"agenthc_api_key"}
    if extra:
        errors.append("feed_access accepts only agenthc_api_key — prepaid days "
                      "are bought and paid by the owner in the wizard")

    # ── AI model ──
    ai = plan.get("ai")
    if ai is not None:
        import llm_client
        pid = ai.get("provider") or llm_client.detect_provider(ai.get("api_key", ""))
        if pid not in llm_client.PROVIDERS:
            errors.append("ai.provider unknown (one of: "
                          + ", ".join(llm_client.PROVIDERS) + ")")
        elif not ai.get("model"):
            errors.append("ai.model is required (list them with the wizard, or "
                          "the provider's /models endpoint)")
        else:
            trial = {"llm_provider": pid, "llm_api_key": str(ai.get("api_key", "")).strip(),
                     "llm_model": str(ai["model"]).strip()}
            if ai.get("base_url"):
                trial["llm_base_url"] = str(ai["base_url"]).strip()
            if not llm_client.configured(trial):
                errors.append("ai: incomplete (key missing, or base_url not https/localhost)")
            else:
                try:
                    llm_client.complete(trial, "Reply with the word OK.", "ping",
                                        max_tokens=256, timeout=45)
                except llm_client.LLMError as exc:
                    errors.append(f"ai: the model didn't answer a test call ({str(exc)[:120]})")
                else:
                    for k in ("llm_provider", "llm_api_key", "llm_model",
                              "llm_base_url", "anthropic_api_key"):
                        new.pop(k, None)
                    new.update(trial)
                    new.setdefault("llm_fallback", "skip")
                    applied.append(f"AI: {llm_client.describe(new)}")

    # ── broker ──
    br = plan.get("broker")
    if br is not None:
        from brokers import BROKERS
        kind = (br or {}).get("type")
        if kind == "robinhood":
            new["broker"] = "robinhood"
            applied.append("broker: robinhood (owner logs in)")
        elif kind in BROKERS and callable(getattr(BROKERS[kind], "connect", None)):
            values = {k: v for k, v in br.items() if k != "type"}
            trial = dict(new)
            ok, msg = BROKERS[kind].connect(trial, values)
            if not ok:
                errors.append(f"broker {kind}: {msg}")
            else:
                new = trial
                applied.append(f"broker: {kind} ({msg})")
        else:
            errors.append("broker.type must be robinhood, " + ", ".join(
                k for k in BROKERS if k != "robinhood"))

    # ── sizing (the owner's own choice) ──
    sz = plan.get("sizing")
    if sz is not None:
        mode = sz.get("mode")
        if mode == "contracts":
            n = _pos_number(sz.get("contracts_per_trade", 1), "sizing.contracts_per_trade",
                            errors, integer=True)
            if n:
                new.update({"sizing_mode": "contracts", "contracts_per_trade": n})
                applied.append(f"sizing: {n} contract(s) per trade")
        elif mode == "budget":
            b = _pos_number(sz.get("budget_per_trade_usd"), "sizing.budget_per_trade_usd", errors)
            if b:
                new.update({"sizing_mode": "budget", "budget_per_trade_usd": b})
                applied.append(f"sizing: ${b:,.0f} per trade")
        elif mode == "percent":
            p = _pos_number(sz.get("percent_per_trade"), "sizing.percent_per_trade", errors)
            if p and p > 100:
                errors.append("sizing.percent_per_trade must be at most 100")
            elif p:
                new.update({"sizing_mode": "percent", "percent_per_trade": p})
                applied.append(f"sizing: {p:g}% of account per trade")
        else:
            errors.append("sizing.mode must be contracts, budget or percent")

    # ── safety rails ──
    sf = plan.get("safety") or {}
    for key, integer, zero in (("max_entries_per_day", True, False),
                               ("max_open_positions", True, False),
                               ("max_contracts_per_trade", True, False),
                               ("max_daily_loss_usd", False, True)):
        if key in sf:
            v = _pos_number(sf[key], f"safety.{key}", errors, integer=integer,
                            allow_zero=zero)
            if v is not None:
                new[key] = v
                applied.append(f"{key}: {v}")
    extra = set(sf) - {"max_entries_per_day", "max_open_positions",
                       "max_contracts_per_trade", "max_daily_loss_usd"}
    if extra:
        errors.append(f"unknown safety settings: {', '.join(sorted(extra))}")

    # ── notifications ──
    nt = plan.get("notifications") or {}
    for key in ("discord_webhook_url", "ntfy_topic", "telegram_bot_token", "telegram_chat_id"):
        if nt.get(key):
            val = str(nt[key]).strip()
            if key == "discord_webhook_url" and not val.startswith("https://"):
                errors.append("notifications.discord_webhook_url must be https://")
                continue
            new[key] = val
            applied.append(f"notifications: {key}")
    extra = set(nt) - {"discord_webhook_url", "ntfy_topic", "telegram_bot_token",
                       "telegram_chat_id"}
    if extra:
        errors.append(f"unknown notification settings: {', '.join(sorted(extra))}")

    # ── built-in rules (no AI) ──
    rl = plan.get("rules") or {}
    if not isinstance(rl, dict):
        errors.append("rules must be an object")
        rl = {}
    names = {"tickers": "rule_tickers", "skip_tickers": "rule_skip_tickers",
             "sides": "rule_sides", "min_strike": "rule_min_strike",
             "skip_0dte": "rule_skip_0dte", "loss_streak": "rule_loss_streak"}
    extra = set(rl) - set(names)
    if extra:
        errors.append(f"unknown rules: {', '.join(sorted(extra))} "
                      f"(allowed: {', '.join(sorted(names))})")
    if "sides" in rl and str(rl["sides"]).lower() not in ("both", "calls", "puts"):
        errors.append("rules.sides must be both, calls or puts")
    if rl and not extra:
        import rules as _rules
        _rules.apply_settings(new, {names[k]: v for k, v in rl.items()})
        desc = _rules.describe(new)
        if desc:
            applied.append(f"rules: {desc}")

    policy = plan.get("policy")
    if policy is not None and not isinstance(policy, str):
        errors.append("policy must be plain text")
    elif isinstance(policy, str) and policy.strip():
        import llm_client
        if (not llm_client.configured(new)
                and str(new.get("llm_fallback", "skip")).lower() != "act"):
            errors.append("policy needs a connected AI to check it (add an "
                          "\"ai\" section) — without one every entry is skipped")

    if errors:
        report["applied"] = []
        return None, report

    # Plans never go live: dry-run stays ON unless the owner already chose LIVE.
    if "dry_run" not in new:
        new["dry_run"] = True
    new.setdefault("max_event_age_s", 300)
    new.setdefault("max_entries_per_day", 5)
    new.setdefault("poll_seconds", 30)
    if isinstance(policy, str) and policy.strip():
        path = os.path.join(home, "policy.md")
        os.makedirs(home, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(policy.strip() + "\n")
        new.setdefault("llm_fallback", "skip")
        applied.append("trading policy written to policy.md")

    report["ok"] = True
    return new, report


def load_plan(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)
