"""Signal source: AgentHC's Agentic Day Trade Ideas feed (optional, featured).

A machine-readable stream of AgentHC's own published, HYPOTHETICAL
paper-trading journal — declarative "ENTERED"/"EXITED" statements of fact for
its main day-trade pick. No prices, no sizing, no recommendations; exits
carry AgentHC's own paper P&L %% statistic. Educational commentary, NOT
investment advice (full terms in the feed's `disclosure` field and this
repo's DISCLAIMER.md).

Access is sats-based: the agent auto-buys a ~$10/day Lightning day-pass
(price floats with Bitcoin's USD price) from its built-in wallet, or uses a
Premium AgentHC API key. Free aggregate track record (hypothetical, labeled):
GET https://api.traderhc.com/api/v1/trading/day-trade-ideas/track-record
"""

import os
import time

import requests

from lightning_wallet import wallet_from_cfg, wallet_setup

NAME = "agenthc"
DESCRIPTION = ("AgentHC Agentic Day Trade Ideas — journal events from a "
               "published paper-trade system (~$10/day in sats, or Premium key)")

AGENTHC_API = os.getenv("AGENTHC_API", "https://api.traderhc.com")
FEED_PATH = "/api/v1/trading/day-trade-ideas"


def _headers(cfg, state):
    if cfg.get("agenthc_api_key"):
        return {"X-API-Key": cfg["agenthc_api_key"]}
    tok = (state or {}).get("l402") or {}
    if tok.get("token") and tok.get("expires_at", 0) > time.time():
        return {"Authorization": tok["token"]}
    return {}


def register_key():
    name = input("Name for your agent on AgentHC (e.g. my-trade-agent): ").strip() \
        or "agentic-trader"
    resp = requests.post(f"{AGENTHC_API}/api/v1/agents/register",
                         json={"name": name}, timeout=15)
    resp.raise_for_status()
    body = resp.json()
    key = body.get("api_key") or body.get("key")
    print(f"Registered. agent_id={body.get('agent_id')}")
    print("NOTE: the feed requires PREMIUM tier — see POST /api/v1/agents/upgrade")
    print(f"      and {AGENTHC_API}/docs for upgrade options.")
    return key


def accept_terms(cfg):
    resp = requests.post(f"{AGENTHC_API}{FEED_PATH}/accept-terms",
                         headers={"X-API-Key": cfg["agenthc_api_key"]}, timeout=15)
    resp.raise_for_status()
    body = resp.json()
    print(f"AgentHC feed terms accepted (version {body.get('terms_version')}).")
    return body.get("terms_version")


def _pass_purchase_allowed(cfg, state, save_state):
    """Recurring-day-pass gate: the agent only auto-pays the ~$10 pass when
    the operator turned 'Recurring day-pass' ON (wizard Source tab / config
    day_pass_recurring). OFF (the default) = the agent never spends without
    approval — it notifies once per day and returns no events instead."""
    if cfg.get("day_pass_recurring"):
        return True
    day = time.strftime("%Y-%m-%d")
    if state.get("pass_needed_notified") != day:
        state["pass_needed_notified"] = day
        save_state(state)
        print("Day-pass required (~$10 in sats) but recurring payments are "
              "OFF — turn ON 'Recurring day-pass' in the wizard Source tab "
              "to let the agent buy it each day.")
    return False


def _buy_day_pass(cfg, state, body, save_state):
    """Pay the 402 Lightning invoice; cache the 24h L402 token in state."""
    wallet = wallet_from_cfg(cfg)
    if wallet is None:
        raise RuntimeError(
            "Feed requires payment and no Lightning wallet is configured — "
            "run `python agent.py setup` (or use a Premium API key).")
    payment = body.get("payment", {})
    invoice = payment.get("payment_request")
    macaroon = payment.get("macaroon")
    amount = int(payment.get("amount_sats", 0) or 0)
    if not invoice or not macaroon:
        raise RuntimeError(f"402 without payable invoice: {str(body)[:200]}")
    cap = int(cfg.get("max_autopay_sats", 30_000))
    if amount > cap:
        raise RuntimeError(
            f"Day-pass costs {amount:,} sats which exceeds your auto-pay cap "
            f"({cap:,}). Raise max_autopay_sats in config.json if intended.")
    print(f"Buying 24h day-pass: paying {amount:,} sats "
          "(terms accepted by payment — see `disclosure` in the 402 body) …")
    preimage = wallet.pay_invoice(invoice)
    state["l402"] = {"token": f"L402 {macaroon}:{preimage}",
                     "expires_at": time.time() + 23 * 3600}
    save_state(state)
    print("Day-pass active for ~23h.")


def poll(cfg, state, save_state=lambda s: None, _retried=False):
    """Fetch recent journal events (normalized contract shape).

    Signal scope (config `include_other_trades`, default False): the main
    daily pick only, or main + AgentHC's "other trades" journal (its wider
    shadow book — several entries a day; your daily entry cap and budget
    still apply per trade).
    """
    track = "all" if cfg.get("include_other_trades") else "main"
    resp = requests.get(f"{AGENTHC_API}{FEED_PATH}",
                        params={"limit": 20, "track": track},
                        headers=_headers(cfg, state), timeout=15)
    if resp.status_code == 402 and not _retried:
        if not _pass_purchase_allowed(cfg, state, save_state):
            return []
        _buy_day_pass(cfg, state, resp.json(), save_state)
        return poll(cfg, state, save_state, _retried=True)
    if resp.status_code == 401 and not _retried and not cfg.get("agenthc_api_key"):
        state.pop("l402", None)  # stale day-pass token — re-buy on next 402
        save_state(state)
        return poll(cfg, state, save_state, _retried=True)
    if resp.status_code == 403:
        detail = resp.json().get("detail", {})
        if isinstance(detail, dict) and detail.get("error") == "terms_acceptance_required":
            accept_terms(cfg)
            return poll(cfg, state, save_state, _retried=True)
        if isinstance(detail, dict) and detail.get("error") == "feed_not_live":
            return []  # feed exists but isn't publishing yet — keep polling
        raise RuntimeError(f"403 from feed: {str(detail)[:200]}")
    resp.raise_for_status()
    events = []
    for ev in resp.json().get("events", []):
        if ev.get("event") not in ("ENTERED", "EXITED"):
            continue
        try:
            # Normalize to the contract types (see sources/__init__.py) — a
            # string strike from the wire must not reach the order path.
            norm = {
                "event": ev["event"],
                "ticker": str(ev["ticker"]).upper(),
                "expiry": str(ev["expiry"]),
                "strike": float(ev["strike"]),
                "type": "C" if str(ev["type"]).upper().startswith("C") else "P",
                "occurred_at": ev.get("occurred_at"),
                "message": ev.get("message", ""),
                # pre-track feeds send no track field — those are main-pick events
                "track": str(ev.get("track", "main")),
            }
            if "paper_pnl_pct" in ev:
                norm["paper_pnl_pct"] = ev["paper_pnl_pct"]
        except (KeyError, TypeError, ValueError):
            continue
        norm["event_id"] = (f"{norm['event']}|{norm['ticker']}|{norm['expiry']}|"
                            f"{norm['strike']}|{norm['type']}|{norm.get('occurred_at')}")
        # Suffix only for the non-default track: main-pick IDs must stay
        # byte-identical across upgrades or the seen-events dedupe resets.
        if norm["track"] != "main":
            norm["event_id"] += f"|{norm['track']}"
        events.append(norm)
    return events


def setup(cfg):
    print("\n-- AgentHC Agentic Day Trade Ideas (sats-based access) --")
    print("Which trade signals?")
    print("  1) Main pick only — one high-conviction day trade (default)")
    print("  2) Main pick + \"other trades\" — AgentHC's wider journal;")
    print("     several entries a day (your daily cap + budget still apply)")
    scope = input("Choose [1/2, default 1]: ").strip() or "1"
    cfg["include_other_trades"] = scope == "2"
    print("  1) Lightning day-pass — the agent pays ~$10/day in sats from its")
    print("     own wallet, no account needed (recommended for agents)")
    print("  2) AgentHC Premium API key (sats-purchased subscription)")
    choice = input("Choose [1/2, default 1]: ").strip() or "1"
    if choice == "2":
        if not cfg.get("agenthc_api_key"):
            have = input("Do you already have an AgentHC API key? [y/N]: ").strip().lower()
            cfg["agenthc_api_key"] = (input("Paste your API key: ").strip()
                                      if have == "y" else register_key())
        try:
            accept_terms(cfg)
        except Exception as exc:
            print(f"(terms acceptance deferred: {exc} — will retry on first run)")
    else:
        cfg = wallet_setup(cfg)
        if not wallet_from_cfg(cfg):
            print("No wallet configured and no API key — this source cannot be")
            print("used until you re-run setup and complete one of the two.")
    return cfg
