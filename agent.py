#!/usr/bin/env python3
"""agentic-trader — a consent-gated, open-source autonomous trading agent.

Watches a SIGNAL SOURCE you choose (your own command file, any JSON feed URL,
or the optional AgentHC Agentic Day Trade Ideas journal feed) and, entirely
under YOUR configuration and responsibility, executes long-options orders
through a BROKER adapter (currently: your own Robinhood Agentic account via
Robinhood's trading MCP).

    python agent.py          # do the right thing: open the setup wizard if not
                             # set up yet, otherwise run the agent
    python agent.py setup    # explicit setup (add --web for the browser wizard)
    python agent.py run      # heartbeat loop (refuses to run without consent)
    python agent.py app      # same as run, but opens the dashboard as a desktop
                             # app window (chromeless browser; --app on run too)
    python agent.py status   # config, wallet balance, open positions
    python agent.py fund N   # print a Lightning invoice to add N sats

HARD CONSENT GATE: this program will not perform ANY setup or trading action
until you have accepted DISCLAIMER.md (all liability is yours; no party here
is a registered investment adviser). See Section 3 of DISCLAIMER.md.
"""

import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

MARKET_TZ = ZoneInfo("America/New_York")

import llm_policy
from brokers import BROKERS
from lightning_wallet import WalletError, print_funding_invoice, wallet_from_cfg
from notifications import notify
from notifications import setup as notifications_setup
from sources import SOURCES

TERMS_VERSION = "agent-terms-2026.07.1"

HOME = os.path.expanduser(os.getenv("AGENT_HOME", "~/.agentic-trader"))
ACCEPTANCE_PATH = os.path.join(HOME, "acceptance.json")
CONFIG_PATH = os.path.join(HOME, "config.json")
STATE_PATH = os.path.join(HOME, "state.json")
TRADES_PATH = os.path.join(HOME, "trades.jsonl")

_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.]{0,9}$")
_EXPIRY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

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
    try:
        os.chmod(os.path.dirname(path), 0o700)  # keys/tokens live here
    except OSError:
        pass
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f, indent=2)
    if private:
        os.chmod(tmp, 0o600)
    os.replace(tmp, path)


# ── event safety rails (see SECURITY.md) ─────────────────────────────────────

def _valid_event(ev):
    """Sanity-check a normalized event before it can trigger any order."""
    try:
        return bool(
            ev.get("event") in ("ENTERED", "EXITED")
            and _TICKER_RE.match(str(ev.get("ticker", "")))
            and _EXPIRY_RE.match(str(ev.get("expiry", "")))
            and 0 < float(ev.get("strike", 0)) < 100_000
            and ev.get("type") in ("C", "P")
            and ev.get("event_id")
        )
    except (TypeError, ValueError):
        return False


def _stale(ev, cfg):
    """True for ENTERED events older than max_event_age_s (default 300s).

    A laptop waking from sleep must not buy into an hours-old entry. Events
    without a timestamp (manual/url sources) are treated as fresh; EXITED
    events are never stale-blocked (closing an open position is always right).
    """
    if ev.get("event") != "ENTERED":
        return False
    ts = ev.get("occurred_at")
    if not ts:
        return False
    try:
        occurred = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - occurred).total_seconds()
    except ValueError:
        return False
    return age > float(cfg.get("max_event_age_s", 300))


def _log_trade(record):
    record["ts"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        os.makedirs(HOME, exist_ok=True)
        with open(TRADES_PATH, "a") as f:
            f.write(json.dumps(record) + "\n")
    except OSError:
        pass


def _recent_trades(limit=50):
    try:
        with open(TRADES_PATH) as f:
            lines = f.readlines()[-limit:]
        return [json.loads(x) for x in lines if x.strip()]
    except (OSError, ValueError):
        return []


def _entries_today():
    """Count today's entries by scanning the FULL trade log.

    Deliberately not a tail read: every skip/veto also appends a log line, so
    a hostile or misbehaving feed could emit enough skippable events to push
    today's real entries out of a fixed-size tail and slip past the daily cap.
    """
    today = datetime.now(timezone.utc).date().isoformat()
    count = 0
    try:
        with open(TRADES_PATH) as f:
            for line in f:
                if today not in line:
                    continue
                try:
                    t = json.loads(line)
                except ValueError:
                    continue
                if t.get("action") == "entry" and str(t.get("ts", "")).startswith(today):
                    count += 1
    except OSError:
        pass
    return count


def _market_open_now():
    """US equity market hours-ish (9:25–16:15 ET, Mon–Fri). Poll slow outside."""
    now = datetime.now(MARKET_TZ)
    if now.weekday() >= 5:
        return False
    minutes = now.hour * 60 + now.minute
    return (9 * 60 + 25) <= minutes <= (16 * 60 + 15)


def _maybe_daily_digest(cfg, state, save_state):
    """After the close, send a one-message summary of today's activity."""
    now = datetime.now(MARKET_TZ)
    day = now.date().isoformat()
    if now.weekday() >= 5 or (now.hour, now.minute) < (16, 20):
        return
    if state.get("last_digest_day") == day:
        return
    today_utc = datetime.now(timezone.utc).date().isoformat()
    todays = [t for t in _recent_trades(300) if str(t.get("ts", "")).startswith(today_utc)]
    entries = sum(1 for t in todays if t.get("action") == "entry")
    exits = sum(1 for t in todays if t.get("action") == "exit")
    vetoes = sum(1 for t in todays if t.get("action") == "policy_veto")
    skipped = sum(1 for t in todays if str(t.get("action", "")).startswith("skip"))
    open_pos = list(state.get("positions", {})) or ["none"]
    notify(cfg, (f"agentic-trader daily digest {day}: {entries} entries, "
                 f"{exits} exits, {vetoes} policy vetoes, {skipped} skipped. "
                 f"Open positions: {', '.join(open_pos)}"))
    state["last_digest_day"] = day
    save_state(state)


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


# ── event pipeline: validate -> staleness -> caps -> policy -> execute ───────

def _execute_dry(cfg, event, state, broker=None, client=None):
    """Dry-run bookkeeping: identical position tracking, zero orders."""
    contract = f"{event['ticker']} {event['expiry']} ${event['strike']:g} " \
               f"{'CALL' if event['type'] == 'C' else 'PUT'}"
    pos_key = f"{event['ticker']}|{event['expiry']}|{event['strike']}|{event['type']}"
    if event["event"] == "ENTERED" and pos_key not in state["positions"]:
        qty, note = int(cfg.get("contracts_per_trade", 1)), "fixed contract count"
        if broker is not None and client is not None:
            try:  # read-only: resolve + quote so budget sizing previews accurately
                option_id = broker.resolve_instrument(client, event)
                if option_id:
                    qty, note = broker.size_contracts(client, cfg, option_id)
            except Exception:
                pass
        if qty < 1:
            print(f"[DRY-RUN] would SKIP {contract}: {note}")
            return None
        state["positions"][pos_key] = {"option_id": "dry", "qty": qty, "dry": True}
        print(f"[DRY-RUN] would buy {qty}x {contract} ({note})")
        return "entry"
    if event["event"] == "EXITED" and pos_key in state["positions"]:
        pos = state["positions"].pop(pos_key)
        print(f"[DRY-RUN] would sell {pos['qty']}x {contract}")
        return "exit"
    return None


def handle_event(ev, cfg, state, broker, client, save_state):
    """Run one new event through the safety pipeline. Returns a log line or None."""
    contract = f"{ev.get('ticker')} {ev.get('expiry')} ${ev.get('strike')} {ev.get('type')}"

    if not _valid_event(ev):
        print(f"skipped invalid event from source: {str(ev)[:120]}")
        return None
    if _stale(ev, cfg):
        msg = f"SKIPPED stale ENTERED ({contract}) — older than " \
              f"{int(cfg.get('max_event_age_s', 300))}s"
        print(msg)
        _log_trade({"action": "skip_stale", "event_id": ev["event_id"],
                    "contract": contract})
        return msg

    if ev["event"] == "ENTERED":
        # Hard mechanical cap — independent of (and checked before) the LLM.
        cap = int(cfg.get("max_entries_per_day", 5))
        if _entries_today() >= cap:
            msg = f"SKIPPED {contract}: daily entry cap reached ({cap})"
            print(msg)
            _log_trade({"action": "skip_daily_cap", "event_id": ev["event_id"],
                        "contract": contract})
            return msg
        # The user's own policy, applied by the LLM policy brain (veto-only).
        verdict = llm_policy.evaluate(cfg, ev, _recent_trades(20))
        if not verdict["act"]:
            msg = f"POLICY VETO {contract}: {verdict['reason']}"
            print(msg)
            _log_trade({"action": "policy_veto", "event_id": ev["event_id"],
                        "contract": contract, "reason": verdict["reason"]})
            return msg
        policy_reason = verdict["reason"]
    else:
        policy_reason = "exits always close what was opened"

    # Never place a real order against a dry-run position.
    pos_key = f"{ev['ticker']}|{ev['expiry']}|{ev['strike']}|{ev['type']}"
    existing = state["positions"].get(pos_key)
    dry = bool(cfg.get("dry_run")) or bool(existing and existing.get("dry"))

    if dry:
        action = _execute_dry(cfg, ev, state, broker, client)
        prefix = "[DRY-RUN] "
    else:
        changed = broker.execute(client, cfg, ev, state)
        action = ("entry" if ev["event"] == "ENTERED" else "exit") if changed else None
        prefix = ""

    if action:
        _log_trade({"action": action, "event_id": ev["event_id"],
                    "contract": contract, "dry": dry, "reason": policy_reason})
        save_state(state)
        return f"{prefix}{action.upper()}: {contract}"
    return None


# ── commands ─────────────────────────────────────────────────────────────────

def cmd_setup():
    require_consent_or_exit()
    cfg = _load(CONFIG_PATH, {}) or {}

    print("\n== Step 1/4: Signal source ==")
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

    print("\n== Step 2/4: Broker ==")
    print("  1) Robinhood Agentic account (OAuth)")
    print("  2) Alpaca — paper or live (API keys; paper = zero real dollars)")
    bc = input("Choose [1/2, default 1]: ").strip() or "1"
    cfg["broker"] = "alpaca" if bc == "2" else "robinhood"
    cfg = BROKERS[cfg["broker"]].setup(cfg)

    print("\n== Step 3/4: Position sizing ==")
    print(SIZING_NOTICE)
    print("\n  1) Dollar budget per trade — buys as many contracts as fit;")
    print("     skips the trade if even one contract exceeds the budget")
    print("  2) Fixed number of contracts per trade")
    mode = input("Choose [1/2, default 1]: ").strip() or "1"
    if mode == "2":
        cfg["sizing_mode"] = "contracts"
        while True:
            raw = input("Contracts per trade [1]: ").strip() or "1"
            try:
                n = int(raw)
                if n >= 1:
                    break
            except ValueError:
                pass
            print("Enter a positive integer.")
        cfg["contracts_per_trade"] = n
    else:
        cfg["sizing_mode"] = "budget"
        while True:
            raw = input("Budget per trade in USD (e.g. 500): ").strip()
            try:
                b = float(raw)
                if b > 0:
                    break
            except ValueError:
                pass
            print("Enter a positive dollar amount.")
        cfg["budget_per_trade_usd"] = b
        cfg.setdefault("contracts_per_trade", 1)  # dry-run fallback estimate
    cfg.setdefault("max_contracts_per_trade", 25)

    print("\n== Step 4/4: Safety rails & extras ==")
    dry = input("Start in DRY-RUN mode (log actions, place NO orders — "
                "recommended for the first days)? [Y/n]: ").strip().lower()
    cfg["dry_run"] = dry != "n"
    raw = input("Max new entries per day (hard cap) [5]: ").strip()
    cfg["max_entries_per_day"] = int(raw) if raw.isdigit() else 5
    cfg.setdefault("max_event_age_s", 300)
    cfg = notifications_setup(cfg)
    cfg = llm_policy.setup(cfg)

    cfg["poll_seconds"] = int(cfg.get("poll_seconds", 30))
    _save(CONFIG_PATH, cfg, private=True)
    print(f"\nSetup complete. Config at {CONFIG_PATH}.")
    if cfg["dry_run"]:
        print("DRY-RUN is ON — set \"dry_run\": false in config.json to go live.")
    print("Start the heartbeat with: python agent.py run")


def _sizing_desc(cfg):
    if str(cfg.get("sizing_mode", "contracts")) == "budget":
        return f"${float(cfg.get('budget_per_trade_usd', 0) or 0):,.0f}/trade budget"
    return f"{int(cfg.get('contracts_per_trade', 1))} contract(s)/trade"


def _apply_command(text):
    """Fixed command allowlist for the dashboard chat. Returns (handled, reply).

    The dashboard's free-text box first tries this allowlist; anything not
    matched here falls through to answer-only LLM Q&A. The LLM can never
    reach these controls — it can only ANSWER.
    """
    import webui
    t = text.lower().strip()
    cfg = _load(CONFIG_PATH, {}) or {}
    if t == "pause":
        webui.CONTROLS["paused"] = True
        return True, "Paused — no new events processed until you say 'resume'."
    if t == "resume":
        webui.CONTROLS["paused"] = False
        return True, "Resumed."
    if t == "stop":
        webui.CONTROLS["stop"] = True
        return True, ("Stopping after this cycle. Open positions remain in your "
                      "brokerage account — close them there if needed.")
    if t in ("dry on", "dry-run on"):
        cfg["dry_run"] = True
        _save(CONFIG_PATH, cfg, private=True)
        return True, "Dry-run ON — actions logged, no real orders."
    if t in ("dry off", "dry-run off", "go live"):
        cfg["dry_run"] = False
        _save(CONFIG_PATH, cfg, private=True)
        return True, "Dry-run OFF — the agent will place REAL orders from now on."
    if t.startswith("set budget"):
        try:
            val = float(t.split()[-1].lstrip("$").replace(",", ""))
            assert val > 0
        except (ValueError, AssertionError, IndexError):
            return True, "Usage: set budget 500"
        cfg["sizing_mode"] = "budget"
        cfg["budget_per_trade_usd"] = val
        _save(CONFIG_PATH, cfg, private=True)
        return True, f"Per-trade budget set to ${val:,.0f} (your decision — see DISCLAIMER.md)."
    if t.startswith("set cap"):
        try:
            val = int(t.split()[-1])
            assert val >= 1
        except (ValueError, AssertionError, IndexError):
            return True, "Usage: set cap 3"
        cfg["max_entries_per_day"] = val
        _save(CONFIG_PATH, cfg, private=True)
        return True, f"Daily entry cap set to {val}."
    if t in ("self-edit on", "self edit on"):
        cfg["self_edit_enabled"] = True
        _save(CONFIG_PATH, cfg, private=True)
        return True, ("Self-edit ON. Ask for changes with:  code: <what to change>"
                      " — you review every diff before it applies; applies are "
                      "backed up, compile-checked, and auto-rolled-back on failure.")
    if t in ("self-edit off", "self edit off"):
        cfg["self_edit_enabled"] = False
        _save(CONFIG_PATH, cfg, private=True)
        return True, "Self-edit OFF."
    if text.lower().startswith("code:"):
        import self_edit
        _ok, msg = self_edit.propose(cfg, text[5:].strip())
        return True, msg
    if t == "status":
        st = _load(STATE_PATH, {"positions": {}})
        return True, (f"{'DRY-RUN' if cfg.get('dry_run') else 'LIVE'} · "
                      f"{_sizing_desc(cfg)} · entries today {_entries_today()}/"
                      f"{cfg.get('max_entries_per_day', 5)} · open: "
                      f"{', '.join(st.get('positions', {})) or 'none'}")
    return False, ""


def cmd_run(app_window=False):
    require_consent_or_exit()
    cfg = _load(CONFIG_PATH)
    broker_ready = bool(cfg and (cfg.get("robinhood_account")
                                 or cfg.get("alpaca_key_id")))
    if not cfg or not cfg.get("source") or not broker_ready:
        print("Not configured — run: python agent.py setup   (or setup --web)")
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

    def save_state(st):
        _save(STATE_PATH, st)

    # The agent serves its OWN dashboard — status, trade log, and a command/
    # chat box — on localhost. No central server involved.
    import webui

    def get_status():
        c = _load(CONFIG_PATH, {}) or {}
        st = _load(STATE_PATH, {"positions": {}})
        return {
            "mode": "DRY-RUN" if c.get("dry_run") else "LIVE",
            "paused": webui.CONTROLS["paused"],
            "fields": {
                "source": c.get("source"),
                "sizing": _sizing_desc(c),
                "entries today": f"{_entries_today()} / cap {c.get('max_entries_per_day', 5)}",
                "policy brain": "on" if llm_policy.enabled(c) else "off",
                "market": "open" if _market_open_now() else "closed (slow polling)",
                "open positions": ", ".join(st.get("positions", {})) or "none",
            },
        }

    dash_url = None
    try:
        dash_url = webui.start_dashboard(
            get_status, lambda: list(reversed(_recent_trades(25))),
            _apply_command, lambda: _load(CONFIG_PATH, {}) or {})
    except OSError as exc:
        print(f"dashboard not started ({exc}) — continuing headless")

    mode = "DRY-RUN (no orders)" if cfg.get("dry_run") else "LIVE"
    print(f"Heartbeat started [{mode}]: source={cfg['source']}, "
          f"{_sizing_desc(cfg)}, policy brain "
          f"{'ON' if llm_policy.enabled(cfg) else 'off'}. Ctrl-C to stop.")
    if dash_url:
        print(f"Dashboard: {dash_url}  (remote? tunnel: ssh -L 8721:127.0.0.1:8721 user@host)")
        if app_window:
            import webui
            webui.open_app_window(dash_url)
    print("Reminder: you are responsible for every order this agent places.")
    notify(cfg, f"agentic-trader heartbeat started [{mode}], source={cfg['source']}"
                + (f" — dashboard {dash_url}" if dash_url else ""))
    while True:
        cfg = _load(CONFIG_PATH, cfg) or cfg  # dashboard edits apply live
        poll = max(10, int(cfg.get("poll_seconds", 30)))
        if webui.CONTROLS["stop"]:
            print("Stopped via dashboard. Open positions remain in your account:",
                  list(state["positions"]) or "none")
            notify(cfg, "agentic-trader stopped via dashboard")
            return
        try:
            if not webui.CONTROLS["paused"]:
                events = source.poll(cfg, state, save_state)
                for ev in events:
                    if ev.get("event_id") in seen:
                        continue
                    try:
                        outcome = handle_event(ev, cfg, state, broker, client,
                                               save_state)
                    except Exception as exc:
                        # One malformed/erroring event must not block the rest
                        # of the batch. Not marked seen — transient broker/net
                        # errors get retried on the next poll.
                        print(f"event error (will retry next poll): "
                              f"{str(exc)[:200]}")
                        continue
                    if outcome:
                        notify(cfg, f"agentic-trader: {outcome}")
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
        try:
            _maybe_daily_digest(cfg, state, save_state)
        except Exception:
            pass
        # Market-hours-aware cadence: fast while the market can move, slow
        # overnight/weekends; snappy while paused so 'resume' feels instant.
        if webui.CONTROLS["paused"]:
            time.sleep(2)
        else:
            time.sleep(poll if _market_open_now() else max(poll, 300))


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
    print(f"mode             : {'DRY-RUN' if cfg.get('dry_run') else 'LIVE'}")
    print(f"policy brain     : {'ON (' + llm_policy.policy_path() + ')' if llm_policy.enabled(cfg) else 'off'}")
    print(f"daily entry cap  : {cfg.get('max_entries_per_day', 5)}")
    print(f"entries today    : {_entries_today()}")
    print(f"open positions   : {list(state.get('positions', {})) or 'none'}")


def cmd_fund(sats):
    cfg = _load(CONFIG_PATH, {}) or {}
    wallet = wallet_from_cfg(cfg)
    if wallet is None:
        print("No Lightning wallet configured — run: python agent.py setup")
        sys.exit(1)
    print_funding_invoice(wallet, sats)


def _is_ready():
    """Consent accepted AND a source + broker are configured."""
    if not consent_ok():
        return False
    cfg = _load(CONFIG_PATH) or {}
    return bool(cfg.get("source") and (cfg.get("robinhood_account")
                                       or cfg.get("alpaca_key_id")))


def main():
    # No subcommand = do the right thing. Safe because nothing can trade until
    # the legal agreement is accepted: not set up -> open the browser wizard;
    # set up -> run the agent. So `python agent.py` is the whole thing.
    if len(sys.argv) <= 1:
        if _is_ready():
            cmd_run()
        else:
            print("Not set up yet — opening the setup wizard in your browser.")
            print("(Nothing runs or trades until you accept DISCLAIMER.md there.)")
            import webui
            webui.run_wizard()
        return

    cmd = sys.argv[1]
    if cmd == "setup":
        if "--web" in sys.argv:
            import webui
            webui.run_wizard()
        else:
            cmd_setup()
    elif cmd in ("run", "app"):
        cmd_run(app_window=(cmd == "app" or "--app" in sys.argv))
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
