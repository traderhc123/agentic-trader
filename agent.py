#!/usr/bin/env python3
"""Agentic Day Trade Ideas — autonomous agent template.

Watches AgentHC's Agentic Day Trade Ideas feed (declarative journal events
from AgentHC's published hypothetical paper-trading journal) and, entirely
under YOUR configuration and responsibility, mirrors those events as
long-options orders in YOUR Robinhood Agentic account.

    python agent.py setup    # one-time: consent gate -> feed access (Lightning
                             # day-pass wallet OR API key) -> Robinhood OAuth ->
                             # contracts-per-trade config
    python agent.py run      # heartbeat loop (refuses to run without consent)
    python agent.py status   # config, wallet balance, day-pass, open positions
    python agent.py fund N   # print a Lightning invoice to add N sats

HARD CONSENT GATE: this program will not perform ANY setup or trading action
until you have accepted DISCLAIMER.md (all liability is yours; neither AgentHC
nor this template is a registered investment adviser). See Section 3 of
DISCLAIMER.md.
"""

import hashlib
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone

import requests

from lightning_wallet import LNbitsWallet, WalletError
from robinhood_mcp import RobinhoodMCP, content_json, tool_ok

TERMS_VERSION = "agent-terms-2026.07"
AGENTHC_API = os.getenv("AGENTHC_API", "https://api.traderhc.com")
FEED_PATH = "/api/v1/trading/day-trade-ideas"

HOME = os.path.expanduser(os.getenv("AGENT_HOME", "~/.agentic-day-trade-agent"))
ACCEPTANCE_PATH = os.path.join(HOME, "acceptance.json")
CONFIG_PATH = os.path.join(HOME, "config.json")
STATE_PATH = os.path.join(HOME, "state.json")
RH_TOKEN_PATH = os.path.join(HOME, "robinhood_oauth.json")

SIZING_NOTICE = (
    "AgentHC is not an investment advisor and cannot advise position sizing.\n"
    "How many contracts to trade per journal event is YOUR decision alone,\n"
    "made with money you can afford to lose entirely."
)


# ── small io helpers ─────────────────────────────────────────────────────────

def _load(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def _save(path, payload, private=False):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f, indent=2)
    if private:
        os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _disclaimer_text():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "DISCLAIMER.md")
    with open(path) as f:
        return f.read()


# ── the hard consent gate ────────────────────────────────────────────────────

def consent_ok():
    """True iff the CURRENT terms version + text have been accepted."""
    rec = _load(ACCEPTANCE_PATH)
    if not rec:
        return False
    digest = hashlib.sha256(_disclaimer_text().encode()).hexdigest()
    return (rec.get("terms_version") == TERMS_VERSION
            and rec.get("disclaimer_sha256") == digest
            and rec.get("accepted") is True)


def require_consent_or_exit():
    if consent_ok():
        return
    print("=" * 72)
    print("CONSENT REQUIRED — this agent will not run until you accept the")
    print("agreement below (DISCLAIMER.md). Read it in full.")
    print("=" * 72)
    print(_disclaimer_text())
    print("=" * 72)
    print("By accepting you agree that ALL trading decisions and ALL liability")
    print("are yours, and that neither AgentHC (Sky Manor Trading LLC) nor this")
    print("template is a registered investment adviser or broker-dealer.")
    print()
    phrase = "I AGREE AND ACCEPT ALL LIABILITY"
    try:
        typed = input(f'Type exactly "{phrase}" to accept (anything else exits): ')
    except EOFError:
        typed = ""
    if typed.strip() != phrase:
        print("\nNot accepted. Exiting — the agent cannot be used without acceptance.")
        sys.exit(2)
    _save(ACCEPTANCE_PATH, {
        "accepted": True,
        "terms_version": TERMS_VERSION,
        "disclaimer_sha256": hashlib.sha256(_disclaimer_text().encode()).hexdigest(),
        "accepted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })
    print("\nAcceptance recorded at", ACCEPTANCE_PATH)


# ── Lightning wallet (pays the sats day-pass) ───────────────────────────────

def wallet_from_cfg(cfg):
    if cfg.get("lnbits_url") and cfg.get("lnbits_admin_key"):
        return LNbitsWallet(cfg["lnbits_url"], cfg["lnbits_admin_key"])
    return None


def wallet_setup(cfg):
    """Attach an LNbits wallet so the agent can pay the Lightning day-pass."""
    print("\nThe feed is sats-priced (~$10/day, floats with Bitcoin's price).")
    print("Give this agent a Lightning wallet it can pay from (see README")
    print("'Give your agent sats' — an LNbits wallet takes ~2 minutes to make).")
    url = input("LNbits instance URL (e.g. https://demo.lnbits.com): ").strip()
    key = input("Wallet ADMIN key (Wallet -> API info -> Admin key): ").strip()
    if not url or not key:
        print("Skipped — without a wallet the agent needs a Premium API key instead.")
        return cfg
    wallet = LNbitsWallet(url, key)
    try:
        bal = wallet.balance_sats()
    except WalletError as exc:
        print(f"Wallet check FAILED: {exc}")
        print("Fix the URL/key and re-run setup.")
        return cfg
    cfg["lnbits_url"] = url
    cfg["lnbits_admin_key"] = key
    print(f"Wallet connected ✓  balance: {bal:,} sats")
    if bal < 15_000:
        print("Balance is low for a ~$10/day pass. Fund it now?")
        raw = input("Amount in sats to request (blank to skip): ").strip()
        if raw.isdigit() and int(raw) > 0:
            _print_funding_invoice(wallet, int(raw))
    # Safety cap: the agent will never auto-pay an invoice above this.
    cfg.setdefault("max_autopay_sats", 30_000)
    print(f"Auto-pay safety cap: {cfg['max_autopay_sats']:,} sats per invoice "
          "(edit max_autopay_sats in config.json to change).")
    return cfg


def _print_funding_invoice(wallet, sats):
    bolt11 = wallet.create_invoice(sats, memo="fund agentic day-trade agent")
    print(f"\nPay this invoice from ANY Lightning wallet to add {sats:,} sats:\n")
    print(bolt11)
    print("\n(Strike, Cash App, Phoenix, Alby, Wallet of Satoshi, etc. — scan or paste.)")


# ── AgentHC feed client ──────────────────────────────────────────────────────

def agenthc_headers(cfg, state=None):
    if cfg.get("agenthc_api_key"):
        return {"X-API-Key": cfg["agenthc_api_key"]}
    tok = (state or {}).get("l402") or {}
    if tok.get("token") and tok.get("expires_at", 0) > time.time():
        return {"Authorization": tok["token"]}
    return {}


def agenthc_register():
    name = input("Name for your agent on AgentHC (e.g. my-trade-agent): ").strip() \
        or "agentic-day-trade-agent"
    resp = requests.post(f"{AGENTHC_API}/api/v1/agents/register",
                         json={"name": name}, timeout=15)
    resp.raise_for_status()
    body = resp.json()
    key = body.get("api_key") or body.get("key")
    print(f"Registered. agent_id={body.get('agent_id')}")
    print("NOTE: the feed requires PREMIUM tier — see POST /api/v1/agents/upgrade")
    print(f"      and {AGENTHC_API}/docs for upgrade options.")
    return key


def agenthc_accept_terms(cfg):
    resp = requests.post(f"{AGENTHC_API}{FEED_PATH}/accept-terms",
                         headers=agenthc_headers(cfg), timeout=15)
    resp.raise_for_status()
    body = resp.json()
    print(f"AgentHC feed terms accepted (version {body.get('terms_version')}).")
    return body.get("terms_version")


def _buy_day_pass(cfg, state, body):
    """Pay the 402 Lightning invoice and cache the 24h L402 token."""
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
    _save(STATE_PATH, state)
    print("Day-pass active for ~23h.")


def fetch_feed(cfg, limit=20, state=None, _retried=False):
    state = state if state is not None else _load(STATE_PATH, {"seen": [], "positions": {}})
    resp = requests.get(f"{AGENTHC_API}{FEED_PATH}",
                        params={"limit": limit},
                        headers=agenthc_headers(cfg, state), timeout=15)
    if resp.status_code == 402 and not _retried:
        _buy_day_pass(cfg, state, resp.json())
        return fetch_feed(cfg, limit, state=state, _retried=True)
    if resp.status_code == 401 and not _retried and not cfg.get("agenthc_api_key"):
        state.pop("l402", None)  # stale day-pass token — re-buy on next 402
        _save(STATE_PATH, state)
        return fetch_feed(cfg, limit, state=state, _retried=True)
    if resp.status_code == 403:
        detail = resp.json().get("detail", {})
        if isinstance(detail, dict) and detail.get("error") == "terms_acceptance_required":
            agenthc_accept_terms(cfg)
            return fetch_feed(cfg, limit, state=state, _retried=True)
        if isinstance(detail, dict) and detail.get("error") == "feed_not_live":
            return []  # feed exists but isn't publishing yet — keep polling
        raise RuntimeError(f"403 from feed: {str(detail)[:200]}")
    resp.raise_for_status()
    return resp.json().get("events", [])


# ── Robinhood execution ──────────────────────────────────────────────────────

def rh_client():
    return RobinhoodMCP(RH_TOKEN_PATH)


def rh_setup(cfg):
    """Interactive OAuth + Agentic-account discovery + options-level check."""
    rh = rh_client()
    if not rh.is_authenticated():
        url, pending = rh.auth_start()
        print("\n1. Open this URL in your browser (logged into Robinhood) and approve:")
        print(f"\n   {url}\n")
        print("2. You'll land on a dead http://127.0.0.1:8721/... page — expected.")
        print("   Copy the FULL URL from the address bar. Codes expire in minutes.")
        redirect = input("\nPaste the full redirect URL: ").strip()
        rh.auth_finish(pending, redirect)
        print("Robinhood tokens stored (0600).")

    payload = content_json(rh.call_tool("get_accounts", {})) or {}
    agentic = None
    for acct in ((payload.get("data") or {}).get("accounts") or []):
        num = str(acct.get("account_number", ""))
        print(f"  account ••••{num[-4:]} type={acct.get('type')} "
              f"agentic_allowed={acct.get('agentic_allowed')} "
              f"option_level={acct.get('option_level') or 'NONE'}")
        if acct.get("agentic_allowed") and acct.get("state") == "active":
            agentic = acct
    if not agentic:
        print("\nNo agentic-allowed account found. Finish Agentic-account onboarding")
        print("in the Robinhood app, then rerun setup.")
        sys.exit(1)
    cfg["robinhood_account"] = str(agentic["account_number"])
    print(f"\nAgentic account ••••{cfg['robinhood_account'][-4:]} selected.")
    if not agentic.get("option_level") or agentic.get("option_level") == "option_level_0":
        print("⚠️  Options are NOT enabled on this account — orders will be rejected")
        print("   until approved. Apply: https://applink.robinhood.com/upgrade_options"
              f"?account_number={cfg['robinhood_account']}")
    return cfg


def resolve_instrument(rh, event):
    result = rh.call_tool("get_option_instruments", {
        "chain_symbol": event["ticker"],
        "expiration_dates": event["expiry"],
        "strike_price": f"{float(event['strike']):.4f}",
        "type": "call" if event["type"] == "C" else "put",
        "state": "active",
        "tradability": "tradable",
    })
    payload = content_json(result)

    def find_id(obj):
        if isinstance(obj, dict):
            if obj.get("id") and (obj.get("chain_symbol") or obj.get("strike_price")):
                return str(obj["id"])
            for v in obj.values():
                f = find_id(v)
                if f:
                    return f
        elif isinstance(obj, list):
            for v in obj:
                f = find_id(v)
                if f:
                    return f
        return ""

    return find_id(payload) if payload else ""


def place(rh, cfg, option_id, side, effect, qty):
    args = {
        "account_number": cfg["robinhood_account"],
        "legs": [{"option_id": option_id, "side": side, "position_effect": effect}],
        "quantity": str(int(qty)),
        "type": "market",
        "time_in_force": "gfd",
        "ref_id": str(uuid.uuid4()),
    }
    result = rh.call_tool("place_option_order", args)
    if not tool_ok(result):
        print(f"  ORDER FAILED: {str(result)[:300]}")
        return None
    body = content_json(result) or {}
    oid = str((body.get("data") or body).get("id", "")) or "submitted"
    print(f"  order {side}/{effect} x{qty} -> {oid}")
    return oid


# ── event processing ─────────────────────────────────────────────────────────

def event_key(ev):
    return f"{ev.get('event')}|{ev.get('ticker')}|{ev.get('expiry')}|" \
           f"{ev.get('strike')}|{ev.get('type')}|{ev.get('occurred_at')}"


def process_event(rh, cfg, state, ev):
    contract = f"{ev['ticker']} {ev['expiry']} ${ev['strike']:g} " \
               f"{'CALL' if ev['type'] == 'C' else 'PUT'}"
    pos_key = f"{ev['ticker']}|{ev['expiry']}|{ev['strike']}|{ev['type']}"
    if ev["event"] == "ENTERED":
        if pos_key in state["positions"]:
            return
        option_id = resolve_instrument(rh, ev)
        if not option_id:
            print(f"  could not resolve instrument for {contract} — skipped")
            return
        qty = int(cfg.get("contracts_per_trade", 1))
        print(f"ENTERED event -> buying {qty}x {contract} (your configuration)")
        if place(rh, cfg, option_id, "buy", "open", qty):
            state["positions"][pos_key] = {"option_id": option_id, "qty": qty,
                                           "opened_at": ev.get("occurred_at")}
    elif ev["event"] == "EXITED":
        pos = state["positions"].get(pos_key)
        if not pos:
            return  # we never opened this one
        print(f"EXITED event -> selling {pos['qty']}x {contract}")
        if place(rh, cfg, pos["option_id"], "sell", "close", pos["qty"]):
            del state["positions"][pos_key]


# ── commands ─────────────────────────────────────────────────────────────────

def cmd_setup():
    require_consent_or_exit()
    cfg = _load(CONFIG_PATH, {}) or {}

    print("\n== Step 1/3: AgentHC feed access (sats-based) ==")
    print("  1) Lightning day-pass — the agent pays ~$10/day in sats from its")
    print("     own wallet, no account needed (recommended for agents)")
    print("  2) AgentHC Premium API key (sats-purchased subscription)")
    choice = input("Choose [1/2, default 1]: ").strip() or "1"
    if choice == "2":
        if not cfg.get("agenthc_api_key"):
            have = input("Do you already have an AgentHC API key? [y/N]: ").strip().lower()
            cfg["agenthc_api_key"] = (input("Paste your API key: ").strip()
                                      if have == "y" else agenthc_register())
        try:
            agenthc_accept_terms(cfg)
        except Exception as exc:
            print(f"(terms acceptance deferred: {exc} — will retry on first run)")
    else:
        cfg = wallet_setup(cfg)
        if not wallet_from_cfg(cfg):
            print("No wallet configured and no API key — the agent cannot access")
            print("the feed until you re-run setup and complete one of the two.")

    print("\n== Step 2/3: Robinhood Agentic account ==")
    cfg = rh_setup(cfg)

    print("\n== Step 3/3: Position sizing ==")
    print(SIZING_NOTICE)
    while True:
        raw = input("\nContracts per trade [1]: ").strip() or "1"
        try:
            n = int(raw)
            if n >= 1:
                break
        except ValueError:
            pass
        print("Enter a positive integer.")
    cfg["contracts_per_trade"] = n
    cfg["poll_seconds"] = int(cfg.get("poll_seconds", 30))
    _save(CONFIG_PATH, cfg, private=True)
    print(f"\nSetup complete. Config at {CONFIG_PATH}.")
    print("Start the heartbeat with: python agent.py run")


def cmd_run():
    require_consent_or_exit()
    cfg = _load(CONFIG_PATH)
    has_access = cfg and (cfg.get("agenthc_api_key")
                          or (cfg.get("lnbits_url") and cfg.get("lnbits_admin_key")))
    if not cfg or not has_access or not cfg.get("robinhood_account"):
        print("Not configured — run: python agent.py setup")
        sys.exit(1)
    rh = rh_client()
    if not rh.is_authenticated():
        print("Robinhood auth missing/expired — run: python agent.py setup")
        sys.exit(1)
    state = _load(STATE_PATH, {"seen": [], "positions": {}})
    seen = set(state["seen"])
    poll = max(10, int(cfg.get("poll_seconds", 30)))
    print(f"Heartbeat started: polling every {poll}s, "
          f"{cfg['contracts_per_trade']} contract(s) per trade. Ctrl-C to stop.")
    print("Reminder: you are responsible for every order this agent places.")
    while True:
        try:
            events = fetch_feed(cfg)
            for ev in sorted(events, key=lambda e: e.get("occurred_at", "")):
                k = event_key(ev)
                if k in seen or ev.get("event") not in ("ENTERED", "EXITED"):
                    continue
                process_event(rh, cfg, state, ev)
                seen.add(k)
                state["seen"] = sorted(seen)[-500:]
                seen = set(state["seen"])
                _save(STATE_PATH, state)
        except KeyboardInterrupt:
            print("\nStopped. Open positions remain in your account:",
                  list(state["positions"]) or "none")
            return
        except Exception as exc:
            print(f"heartbeat error (will retry): {str(exc)[:200]}")
        time.sleep(poll)


def cmd_status():
    print(f"consent accepted : {consent_ok()} ({ACCEPTANCE_PATH})")
    cfg = _load(CONFIG_PATH, {}) or {}
    print(f"agenthc key      : {'set' if cfg.get('agenthc_api_key') else 'not set'}")
    wallet = wallet_from_cfg(cfg)
    if wallet:
        try:
            print(f"lightning wallet : connected, {wallet.balance_sats():,} sats")
        except WalletError as exc:
            print(f"lightning wallet : ERROR — {exc}")
    else:
        print("lightning wallet : not configured")
    state = _load(STATE_PATH, {"positions": {}})
    tok = state.get("l402") or {}
    if tok.get("expires_at", 0) > time.time():
        mins = int((tok["expires_at"] - time.time()) / 60)
        print(f"day-pass         : active, ~{mins} min remaining")
    else:
        print("day-pass         : none (bought automatically on next 402)")
    acct = cfg.get("robinhood_account", "")
    print(f"robinhood account: {'••••' + acct[-4:] if acct else 'MISSING'}")
    print(f"contracts/trade  : {cfg.get('contracts_per_trade', 'unset')}")
    print(f"open positions   : {list(state.get('positions', {})) or 'none'}")


def cmd_fund(sats):
    cfg = _load(CONFIG_PATH, {}) or {}
    wallet = wallet_from_cfg(cfg)
    if wallet is None:
        print("No Lightning wallet configured — run: python agent.py setup")
        sys.exit(1)
    _print_funding_invoice(wallet, sats)


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "setup":
        cmd_setup()
    elif cmd == "run":
        cmd_run()
    elif cmd == "status":
        cmd_status()
    elif cmd == "fund":
        amount = sys.argv[2] if len(sys.argv) > 2 else "20000"
        if not amount.isdigit():
            print("Usage: python agent.py fund <sats>")
            sys.exit(1)
        cmd_fund(int(amount))
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
