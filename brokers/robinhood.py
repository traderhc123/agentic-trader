"""Broker adapter: Robinhood agentic-trading MCP.

Places long single-leg options orders (buy-to-open / sell-to-close, market,
good-for-day) in the user's dedicated Robinhood Agentic account. The account
must be agentic-enabled, options-approved, and funded — the setup wizard
checks and explains each.
"""

import os
import sys
import uuid

from .robinhood_mcp import RobinhoodMCP, content_json, tool_ok


def _token_path():
    home = os.path.expanduser(os.getenv("AGENT_HOME", "~/.agentic-trader"))
    return os.path.join(home, "robinhood_oauth.json")


def client(cfg):
    rh = RobinhoodMCP(_token_path())
    return rh if rh.is_authenticated() else None


def setup(cfg):
    """Interactive OAuth + Agentic-account discovery + options-level check."""
    rh = RobinhoodMCP(_token_path())
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


def execute(rh, cfg, event, state):
    """Act on one normalized event. Returns True if state changed."""
    contract = f"{event['ticker']} {event['expiry']} ${event['strike']:g} " \
               f"{'CALL' if event['type'] == 'C' else 'PUT'}"
    pos_key = f"{event['ticker']}|{event['expiry']}|{event['strike']}|{event['type']}"
    if event["event"] == "ENTERED":
        if pos_key in state["positions"]:
            return False
        option_id = resolve_instrument(rh, event)
        if not option_id:
            print(f"  could not resolve instrument for {contract} — skipped")
            return False
        qty = int(cfg.get("contracts_per_trade", 1))
        print(f"ENTERED event -> buying {qty}x {contract} (your configuration)")
        if place(rh, cfg, option_id, "buy", "open", qty):
            state["positions"][pos_key] = {"option_id": option_id, "qty": qty,
                                           "opened_event": event.get("event_id")}
            return True
    elif event["event"] == "EXITED":
        pos = state["positions"].get(pos_key)
        if not pos:
            return False  # we never opened this one
        print(f"EXITED event -> selling {pos['qty']}x {contract}")
        if place(rh, cfg, pos["option_id"], "sell", "close", pos["qty"]):
            del state["positions"][pos_key]
            return True
    return False
