#!/usr/bin/env python3
"""agentic-trader — a consent-gated, open-source autonomous trading agent.

Watches a SIGNAL SOURCE you choose (your own command file, any JSON feed URL,
or the optional AgentHC Agentic Day Trade Ideas journal feed) and, entirely
under YOUR configuration and responsibility, executes long-options orders
through a BROKER adapter (currently: your own Robinhood Agentic account via
Robinhood's trading MCP).

    python agent.py setup    # one-time: consent gate -> pick signal source ->
                             # broker auth (Robinhood OAuth) -> sizing
    python agent.py run      # heartbeat loop (refuses to run without consent)
    python agent.py status   # config, wallet balance, day-pass, open positions
    python agent.py fund N   # print a Lightning invoice to add N sats

HARD CONSENT GATE: this program will not perform ANY setup or trading action
until you have accepted DISCLAIMER.md (all liability is yours; no party here
is a registered investment adviser). See Section 3 of DISCLAIMER.md.
"""

import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone

from brokers import BROKERS
from lightning_wallet import WalletError, print_funding_invoice, wallet_from_cfg
from sources import SOURCES

TERMS_VERSION = "agent-terms-2026.07.1"

HOME = os.path.expanduser(os.getenv("AGENT_HOME", "~/.agentic-trader"))
ACCEPTANCE_PATH = os.path.join(HOME, "acceptance.json")
CONFIG_PATH = os.path.join(HOME, "config.json")
STATE_PATH = os.path.join(HOME, "state.json")

SIZING_NOTICE = (
    "Nobody involved in this software — not AgentHC, not any signal source,\n"
    "not the template authors — is an investment advisor, and none of them\n"
    "can advise position sizing. How many contracts to trade per event is\n"
    "YOUR decision alone, made with money you can afford to lose entirely."
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
    print("are yours, and that neither AgentHC (Sky Manor Trading LLC), nor any")
    print("signal source, nor this template is a registered investment adviser")
    print("or broker-dealer.")
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


# ── commands ─────────────────────────────────────────────────────────────────

def cmd_setup():
    require_consent_or_exit()
    cfg = _load(CONFIG_PATH, {}) or {}

    print("\n== Step 1/3: Signal source ==")
    names = list(SOURCES)
    for i, name in enumerate(names, 1):
        print(f"  {i}) {name} — {SOURCES[name].DESCRIPTION}")
    default = cfg.get("source", "agenthc")
    raw = input(f"Choose source [1-{len(names)}, default {default}]: ").strip()
    if raw.isdigit() and 1 <= int(raw) <= len(names):
        cfg["source"] = names[int(raw) - 1]
    else:
        cfg["source"] = default
    cfg = SOURCES[cfg["source"]].setup(cfg)

    print("\n== Step 2/3: Broker (Robinhood Agentic account) ==")
    cfg["broker"] = "robinhood"
    cfg = BROKERS[cfg["broker"]].setup(cfg)

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
    if not cfg or not cfg.get("source") or not cfg.get("robinhood_account"):
        print("Not configured — run: python agent.py setup")
        sys.exit(1)
    source = SOURCES.get(cfg["source"])
    broker = BROKERS.get(cfg.get("broker", "robinhood"))
    if source is None or broker is None:
        print(f"Unknown source/broker in config: {cfg.get('source')}/{cfg.get('broker')}")
        sys.exit(1)
    client = broker.client(cfg)
    if client is None:
        print("Broker auth missing/expired — run: python agent.py setup")
        sys.exit(1)
    state = _load(STATE_PATH, {"seen": [], "positions": {}})
    seen = set(state["seen"])
    poll = max(10, int(cfg.get("poll_seconds", 30)))

    def save_state(st):
        _save(STATE_PATH, st)

    print(f"Heartbeat started: source={cfg['source']}, polling every {poll}s, "
          f"{cfg['contracts_per_trade']} contract(s) per trade. Ctrl-C to stop.")
    print("Reminder: you are responsible for every order this agent places.")
    while True:
        try:
            events = source.poll(cfg, state, save_state)
            for ev in events:
                if ev["event_id"] in seen:
                    continue
                broker.execute(client, cfg, ev, state)
                seen.add(ev["event_id"])
                state["seen"] = sorted(seen)[-500:]
                seen = set(state["seen"])
                save_state(state)
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
    print(f"signal source    : {cfg.get('source', 'unset')}")
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
    print_funding_invoice(wallet, sats)


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
