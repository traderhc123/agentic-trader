#!/usr/bin/env python3
"""agentic-trader — a consent-gated, open-source autonomous trading agent.

Watches a SIGNAL SOURCE you choose (your own command file, any JSON feed URL,
or the optional AgentHC Agentic Day Trade Ideas journal feed) and, entirely
under YOUR configuration and responsibility, executes long-options orders
through a BROKER adapter (currently: your own Robinhood Agentic account via
Robinhood's trading MCP).

    python agent.py          # do the right thing: open the setup wizard if not
                             # set up yet, otherwise start the agent in the
                             # background and open its dashboard
    python agent.py setup    # explicit setup (add --web for the browser wizard)
    python agent.py run      # heartbeat in the FOREGROUND (servers, Docker, tmux;
                             # closing the terminal stops it)
    python agent.py app      # start in the background + open the dashboard as a
                             # desktop app window (chromeless browser)
    python agent.py start    # run in the background — closing this window
                             # doesn't stop it; restarts itself if it crashes
    python agent.py stop     # stop the background agent
    python agent.py autostart on|off|status   # start at every login
    python agent.py status   # running?, config, wallet balance, open positions
    python agent.py export FILE   # pack this setup into one passphrase-locked
                             # file to move it to another computer
    python agent.py import FILE   # unpack a move file on the new computer
    python agent.py resume-here   # run a moved-away setup here again
    python agent.py doctor   # check EVERYTHING (config, wallet, feed, broker)
                             # and print the exact command to fix each problem
                             # (--json: machine-readable, for AI assistants)
    python agent.py setup --from plan.json   # apply an assistant-written plan
                             # (everything except the owner's own steps)
    python agent.py fund N   # print a Lightning invoice to add N sats
    python agent.py link     # print the dashboard login link
    python agent.py netcheck # can't reach AgentHC? names the cause (antivirus,
                             # VPN, DNS filter…) and the fix

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
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save(path, payload, private=False):
    import platform_compat
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    # keys/tokens live here. POSIX chmod is cheap, so re-apply every save;
    # Windows icacls is a subprocess, so lock once (marker file) — this also
    # locks homes created by older versions on their first save.
    if not platform_compat.IS_WINDOWS:
        platform_compat.make_private(d, is_dir=True)
    else:
        marker = os.path.join(d, ".acl-locked")
        if not os.path.exists(marker) and platform_compat.make_private(d, is_dir=True):
            try:
                open(marker, "w", encoding="utf-8").close()
            except OSError:
                pass
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    if private and not platform_compat.IS_WINDOWS:
        os.chmod(tmp, 0o600)  # Windows: inherits the locked directory's ACL
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
        with open(TRADES_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except OSError:
        pass


def _recent_trades(limit=50):
    try:
        with open(TRADES_PATH, encoding="utf-8") as f:
            lines = f.readlines()[-limit:]
        return [json.loads(x) for x in lines if x.strip()]
    except (OSError, ValueError):
        return []


def _trades_today():
    """Today's trade-log rows, oldest first (UTC day, like the entry cap)."""
    today = datetime.now(timezone.utc).date().isoformat()
    return [t for t in _recent_trades(500)
            if str(t.get("ts", "")).startswith(today)]


def _entries_today():
    """Count today's entries by scanning the FULL trade log.

    Deliberately not a tail read: every skip/veto also appends a log line, so
    a hostile or misbehaving feed could emit enough skippable events to push
    today's real entries out of a fixed-size tail and slip past the daily cap.
    """
    today = datetime.now(timezone.utc).date().isoformat()
    count = 0
    try:
        with open(TRADES_PATH, encoding="utf-8") as f:
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


def _est_pnl_today():
    """Today's estimated realized P&L (USD) from the trade log's exit records.

    An ESTIMATE: brokers don't report fill prices back to the agent, so each
    exit's P&L is (exit quote - entry quote) x qty x 100, or the feed's own
    paper P&L %% applied to the entry estimate when an exit quote is missing.
    Exits with no estimate at all count as 0 (see `unmeasured`). Full-log
    scan for the same reason as _entries_today."""
    today = datetime.now(timezone.utc).date().isoformat()
    total, unmeasured = 0.0, 0
    try:
        with open(TRADES_PATH, encoding="utf-8") as f:
            for line in f:
                if today not in line:
                    continue
                try:
                    t = json.loads(line)
                except ValueError:
                    continue
                if t.get("action") != "exit" or not str(t.get("ts", "")).startswith(today):
                    continue
                if isinstance(t.get("est_pnl_usd"), (int, float)):
                    total += float(t["est_pnl_usd"])
                else:
                    unmeasured += 1
    except OSError:
        pass
    return total, unmeasured


def _mark(broker, client, cfg, event, side):
    """Best-effort per-share quote for P&L estimates; 0.0 when unavailable."""
    fn = getattr(broker, "mark_price", None) if broker is not None else None
    if fn is None or client is None:
        return 0.0
    try:
        return float(fn(client, cfg, event, side) or 0.0)
    except Exception:
        return 0.0


def _est_exit_pnl(pos, exit_mark, event):
    qty = int((pos or {}).get("qty") or 0)
    entry = float((pos or {}).get("entry_mark") or 0)
    if qty < 1 or entry <= 0:
        return None
    if exit_mark > 0:
        return round((exit_mark - entry) * qty * 100, 2)
    pct = event.get("paper_pnl_pct")
    if isinstance(pct, (int, float)):
        return round(entry * qty * 100 * float(pct) / 100.0, 2)
    return None


def _market_open_now():
    """US equity market hours-ish (9:25–16:15 ET, Mon–Fri). Poll slow outside."""
    now = datetime.now(MARKET_TZ)
    if now.weekday() >= 5:
        return False
    minutes = now.hour * 60 + now.minute
    return (9 * 60 + 25) <= minutes <= (16 * 60 + 15)


def _prune_expired_positions(cfg, state, save_state):
    """Drop ledger positions whose contract expiry has passed (ET).

    A position leaves the ledger only via an EXITED event; miss that event
    (agent offline, feed gap) and the row lingers forever, silently eating a
    max_open_positions slot until entries are blocked. Past expiry the
    contract can't be exited anymore, so the row is pure staleness.
    Bookkeeping only — places no orders. The notify matters: expiry without
    an exit usually means the premium was lost, so check the account.
    """
    today = datetime.now(MARKET_TZ).date().isoformat()
    dropped = []
    for key in list(state.get("positions", {})):
        parts = key.split("|")
        if len(parts) == 4 and _EXPIRY_RE.match(parts[1]) and parts[1] < today:
            state["positions"].pop(key)
            dropped.append(key)
    if dropped:
        save_state(state)
        for key in dropped:
            msg = (f"pruned expired position {key} — expired without an exit "
                   f"event; check your broker account")
            print(msg)
            notify(cfg, f"agentic-trader: {msg}")
    return dropped


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
    with open(path, encoding="utf-8") as f:
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

SIZING_MODES = ("budget", "contracts", "percent")
_TRACK_SIZING_KEYS = ("sizing_mode", "budget_per_trade_usd",
                      "contracts_per_trade", "percent_per_trade",
                      "max_contracts_per_trade")


def _sizing_cfg(cfg, event):
    """Effective config for one event. Feed events carry track="main" (the
    daily pick) or track="other" (the wider journal); other-track events use
    the other_* sizing overrides when set, so the two can be allocated
    differently (e.g. $500 main / $100 other). No overrides = same sizing."""
    if str(event.get("track", "main")) == "main":
        return cfg
    overrides = {k: cfg[f"other_{k}"] for k in _TRACK_SIZING_KEYS
                 if cfg.get(f"other_{k}") not in (None, "")}
    return {**cfg, **overrides} if overrides else cfg


def _account_value(broker, client, cfg):
    """Total account value from the broker (0.0 when it can't be read)."""
    fn = getattr(broker, "account_value", None)
    if fn is None or client is None:
        return 0.0
    try:
        return max(0.0, float(fn(client, cfg) or 0))
    except Exception:
        return 0.0


def _resolve_percent(cfg, broker, client):
    """'% of account' sizing -> a dollar budget for this one entry.

    Reads the account value from the broker at entry time and hands the
    broker an ordinary budget-mode config, so every broker's existing
    budget logic (skip if one contract doesn't fit, max-contracts cap)
    applies unchanged. Returns (cfg, skip_reason); a non-empty reason means
    skip the entry — never guess a size without knowing the account value.
    """
    if str(cfg.get("sizing_mode")) != "percent":
        return cfg, ""
    try:
        pct = float(cfg.get("percent_per_trade") or 0)
    except (TypeError, ValueError):
        pct = 0.0
    if not 0 < pct <= 100:
        return cfg, "%-of-account mode but percent_per_trade unset — skipped"
    value = _account_value(broker, client, cfg)
    if value <= 0:
        return cfg, ("couldn't read your account value for %-of-account "
                     "sizing — skipped")
    budget = round(value * pct / 100.0, 2)
    print(f"  sizing: {pct:g}% of ${value:,.0f} account = ${budget:,.2f} budget")
    return {**cfg, "sizing_mode": "budget", "budget_per_trade_usd": budget}, ""


def _execute_dry(cfg, event, state, broker=None, client=None):
    """Dry-run bookkeeping: identical position tracking, zero orders."""
    contract = f"{event['ticker']} {event['expiry']} ${event['strike']:g} " \
               f"{'CALL' if event['type'] == 'C' else 'PUT'}"
    pos_key = f"{event['ticker']}|{event['expiry']}|{event['strike']}|{event['type']}"
    if event["event"] == "ENTERED" and pos_key not in state["positions"]:
        qty = min(max(1, int(cfg.get("contracts_per_trade", 1))),
                  int(cfg.get("max_contracts_per_trade", 100)))
        note = "fixed contract count"
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


def _notify_ai_problem_once(cfg, state, save_state, contract, reason):
    """An entry was skipped because the AI is missing or failing (not because
    the owner's rules said no). Tell the owner, at most once per day, since
    every entry will keep being skipped until they fix it."""
    today = datetime.now(timezone.utc).date().isoformat()
    if state.get("ai_problem_notified") == today:
        return
    state["ai_problem_notified"] = today
    try:
        save_state(state)
    except Exception:
        pass
    notify(cfg, f"agentic-trader: entry SKIPPED ({contract}) because your trading "
                f"policy couldn't be checked: {reason}. Every entry will be "
                "skipped until you fix your AI (Setup → Connect your AI) or turn "
                "the policy off. No more alerts about this today.")


class ExitNotDone(Exception):
    """An EXITED event couldn't close the position (order rejected, no
    quote…). Raised so the heartbeat leaves the event unseen and tries again
    next poll — an exit is never silently dropped."""


_problems_logged = set()  # (event_id, kind) already written this run


def _report_problem(cfg, state, save_state, kind, contract, message,
                    event_id=None, dry=False):
    """Write a 'why this trade didn't happen' row to the trade log (what the
    dashboard shows) and notify at most once per kind per day."""
    import broker_events
    key = (event_id, kind)
    if event_id and key in _problems_logged:
        return
    if event_id:
        _problems_logged.add(key)
    hint = broker_events.hint(message)
    rec = {"action": kind, "contract": contract, "reason": message}
    if event_id:
        rec["event_id"] = event_id
    if hint:
        rec["hint"] = hint
    if dry:
        rec["dry"] = True
    _log_trade(rec)
    if kind not in broker_events.NOTIFY or dry:
        return
    today = datetime.now(timezone.utc).date().isoformat()
    sent = state.setdefault("problem_notified", {})
    if sent.get(kind) == today:
        return
    sent[kind] = today
    try:
        save_state(state)
    except Exception:
        pass
    label = broker_events.KINDS.get(kind, kind.replace("_", " "))
    notify(cfg, f"agentic-trader: trade NOT placed ({contract}) — {label}: "
                f"{message}" + (f"\nFix: {hint}" if hint else "")
                + "\n(No more alerts about this today; every case is in the "
                "dashboard activity log.)")


def _buying_power(broker, client, cfg):
    """Broker cash available for orders, or None if unknown/unsupported."""
    fn = getattr(broker, "buying_power", None)
    if fn is None or client is None:
        return None
    try:
        bp = fn(client, cfg)
        return None if bp is None else float(bp)
    except Exception:
        return None


LOW_CASH_USD = 50.0


def _cash_warning(cfg, bp):
    """Plain-English warning when buying power can't cover trades, or ''."""
    if bp is None:
        return ""
    if bp < LOW_CASH_USD:
        return (f"Your broker account has ${bp:,.2f} buying power — orders will "
                "be rejected until you add cash.")
    if str(cfg.get("sizing_mode")) == "budget":
        budget = float(cfg.get("budget_per_trade_usd") or 0)
        if budget > bp:
            return (f"Your per-trade budget (${budget:,.0f}) is more than your "
                    f"buying power (${bp:,.2f}) — trades costing more than "
                    f"${bp:,.0f} will be rejected.")
    return ""


def _refresh_cash(cfg, state, save_state, broker, client, cash):
    """Re-read buying power (every ~10 min) for the dashboard, and warn once
    a day in LIVE mode when it can't cover trades."""
    cash["at"] = time.time()
    cash["dry"] = bool(cfg.get("dry_run"))
    cash["bp"] = _buying_power(broker, client, cfg)
    warn = _cash_warning(cfg, cash["bp"])
    cash["warning"] = warn
    if not warn or cfg.get("dry_run"):
        return
    today = datetime.now(timezone.utc).date().isoformat()
    if state.get("low_cash_notified") == today:
        return
    state["low_cash_notified"] = today
    try:
        save_state(state)
    except Exception:
        pass
    print("WARNING: " + warn)
    notify(cfg, "agentic-trader WARNING: " + warn)


def _problems_today():
    """Today's 'trade didn't happen' rows, grouped for the dashboard."""
    import broker_events
    today = datetime.now(timezone.utc).date().isoformat()
    kinds = set(broker_events.KINDS) | {"skip_stale", "skip_sizing"}
    out = {}
    for t in _recent_trades(300):
        if t.get("action") not in kinds or not str(t.get("ts", "")).startswith(today):
            continue
        p = out.setdefault(t["action"], {"kind": t["action"], "count": 0})
        p["count"] += 1
        p.update(last=t.get("reason", ""), hint=t.get("hint", ""),
                 contract=t.get("contract", ""), at=t.get("ts", ""))
    return sorted(out.values(), key=lambda p: p["at"], reverse=True)


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
        _report_problem(cfg, state, save_state, "skip_stale", contract,
                        f"signal was {int(cfg.get('max_event_age_s', 300))}s+ old "
                        "when the agent saw it (computer asleep or offline?)",
                        event_id=ev["event_id"])
        return msg

    if ev["event"] == "ENTERED":
        # Hard mechanical caps — independent of (and checked before) the LLM.
        cap = int(cfg.get("max_entries_per_day", 5))
        if _entries_today() >= cap:
            msg = f"SKIPPED {contract}: daily entry cap reached ({cap})"
            print(msg)
            _log_trade({"action": "skip_daily_cap", "event_id": ev["event_id"],
                        "contract": contract})
            return msg
        # Concurrent-exposure cap: the daily cap resets every day, so without
        # this a feed could accumulate unbounded open positions across days.
        pos_cap = int(cfg.get("max_open_positions", 10))
        if len(state.get("positions", {})) >= pos_cap:
            msg = (f"SKIPPED {contract}: max open positions reached "
                   f"({pos_cap}) — close positions or raise max_open_positions")
            print(msg)
            _log_trade({"action": "skip_position_cap", "event_id": ev["event_id"],
                        "contract": contract})
            return msg
        # Max daily loss (opt-in): once today's estimated realized loss reaches
        # the limit, stop opening positions. Exits are never blocked.
        loss_cap = float(cfg.get("max_daily_loss_usd") or 0)
        if loss_cap > 0:
            pnl, _unmeasured = _est_pnl_today()
            if -pnl >= loss_cap:
                msg = (f"SKIPPED {contract}: max daily loss reached "
                       f"(est. ${-pnl:,.0f} of ${loss_cap:,.0f}) — no new "
                       "entries until tomorrow")
                print(msg)
                _log_trade({"action": "skip_daily_loss", "event_id": ev["event_id"],
                            "contract": contract})
                return msg
        # Built-in rules (plain settings, no AI): tickers, calls/puts, min
        # strike, no 0DTE, stop after a losing streak.
        import rules
        ok, why = rules.check(cfg, ev, _trades_today())
        if not ok:
            msg = f"SKIPPED {contract}: {why} (your rules)"
            print(msg)
            _log_trade({"action": "skip_rule", "event_id": ev["event_id"],
                        "contract": contract, "reason": why})
            return msg
        # The user's own policy, applied by the LLM policy brain (veto-only).
        verdict = llm_policy.evaluate(cfg, ev, _recent_trades(20))
        if not verdict["act"]:
            msg = f"POLICY VETO {contract}: {verdict['reason']}"
            print(msg)
            _log_trade({"action": "policy_veto", "event_id": ev["event_id"],
                        "contract": contract, "reason": verdict["reason"]})
            if verdict.get("ai_problem"):
                _notify_ai_problem_once(cfg, state, save_state, contract,
                                        verdict["reason"])
            return msg
        policy_reason = verdict["reason"]
    else:
        policy_reason = "exits always close what was opened"

    # Never place a real order against a dry-run position.
    pos_key = f"{ev['ticker']}|{ev['expiry']}|{ev['strike']}|{ev['type']}"
    existing = state["positions"].get(pos_key)
    dry = bool(cfg.get("dry_run")) or bool(existing and existing.get("dry"))

    ecfg = _sizing_cfg(cfg, ev)  # per-track sizing (main vs "other trades")
    if ev["event"] == "ENTERED" and not existing:
        ecfg, skip = _resolve_percent(ecfg, broker, client)
        if skip:
            msg = f"{'[DRY-RUN] ' if dry else ''}SKIPPED {contract}: {skip}"
            print(msg)
            _report_problem(cfg, state, save_state, "skip_sizing", contract, skip,
                            event_id=ev["event_id"], dry=dry)
            return msg
    prior_pos = dict(existing) if existing else None  # qty/entry mark for P&L
    if dry:
        action = _execute_dry(ecfg, ev, state, broker, client)
        prefix = "[DRY-RUN] "
    else:
        import broker_events
        broker_events.begin()
        changed = broker.execute(client, ecfg, ev, state)
        reported = broker_events.drain()
        action = ("entry" if ev["event"] == "ENTERED" else "exit") if changed else None
        prefix = ""
        for r in reported:
            msg = r["message"]
            if r["kind"] == "order_failed" and broker_events.is_cash_problem(msg):
                bp = _buying_power(broker, client, cfg)
                if bp is not None:
                    msg += f" (buying power now ${bp:,.2f})"
            _report_problem(cfg, state, save_state, r["kind"], contract, msg,
                            event_id=ev["event_id"])
        if not action and ev["event"] == "ENTERED" and not existing and not reported:
            _report_problem(cfg, state, save_state, "broker_no_order", contract,
                            "the broker adapter didn't place the order and gave "
                            "no reason — see agent-run.log",
                            event_id=ev["event_id"])
        if not action and ev["event"] == "EXITED":
            held = state["positions"].get(pos_key)
            if held and not held.get("pending"):
                if not reported:
                    _report_problem(cfg, state, save_state, "exit_failed",
                                    contract, "the exit order wasn't placed",
                                    event_id=ev["event_id"])
                raise ExitNotDone(f"exit for {contract} not done — retrying "
                                  "next poll")

    if action:
        record = {"action": action, "event_id": ev["event_id"],
                  "contract": contract, "dry": dry, "reason": policy_reason}
        if action == "entry" and pos_key in state["positions"]:
            mark = _mark(broker, client, ecfg, ev, "buy")
            if mark > 0:
                state["positions"][pos_key]["entry_mark"] = mark
        elif action == "exit":
            est = _est_exit_pnl(prior_pos, _mark(broker, client, ecfg, ev, "sell"), ev)
            if est is not None:
                record["est_pnl_usd"] = est
        _log_trade(record)
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
    print("  3) moomoo — paper or live (needs their OpenD gateway running)")
    bc = input("Choose [1-3, default 1]: ").strip() or "1"
    cfg["broker"] = {"2": "alpaca", "3": "moomoo"}.get(bc, "robinhood")
    cfg = BROKERS[cfg["broker"]].setup(cfg)

    print("\n== Step 3/4: Position sizing ==")
    print(SIZING_NOTICE)
    print("\n  1) Dollar budget per trade — buys as many contracts as fit;")
    print("     skips the trade if even one contract exceeds the budget")
    print("  2) Fixed number of contracts per trade")
    print("  3) Percent of account value per trade (re-read at each entry)")
    mode = input("Choose [1-3, default 1]: ").strip() or "1"
    if mode == "3":
        cfg["sizing_mode"] = "percent"
        while True:
            raw = input("Percent of account per trade (e.g. 2): ").strip().rstrip("%")
            try:
                p = float(raw)
                if 0 < p <= 100:
                    break
            except ValueError:
                pass
            print("Enter a percent between 0 and 100.")
        cfg["percent_per_trade"] = p
        cfg.setdefault("contracts_per_trade", 1)
    elif mode == "2":
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
    # 100 (was 25 through v2026-07): still a guard against a penny option
    # ballooning one budget-mode trade into a lot too large to exit into thin
    # quotes, without capping cheap contracts to a fraction of the budget.
    cfg.setdefault("max_contracts_per_trade", 100)

    if cfg.get("include_other_trades"):
        print("\nYour source includes the \"other trades\" (shadow) journal. Size")
        print("those separately from the main pick?")
        print("  0) Same as the main pick (default)  1) Dollar budget")
        print("  2) Fixed contracts  3) Percent of account")
        omode = {"1": "budget", "2": "contracts", "3": "percent"}.get(
            input("Choose [0-3, default 0]: ").strip(), "")
        if omode:
            raw = input({"budget": "Other-trades budget in USD: ",
                         "contracts": "Other-trades contracts per trade: ",
                         "percent": "Other-trades percent of account: "}[omode])
            ok, err = set_book_sizing(cfg, "other", omode, raw)
            if not ok:
                print(f"{err} — other trades will use the main sizing.")

    print("\n== Step 4/4: Safety rails & extras ==")
    dry = input("Start in DRY-RUN mode (log actions, place NO orders — "
                "recommended for the first days)? [Y/n]: ").strip().lower()
    cfg["dry_run"] = dry != "n"
    raw = input("Max new entries per day (hard cap) [5]: ").strip()
    cfg["max_entries_per_day"] = int(raw) if raw.isdigit() else 5
    cfg.setdefault("max_open_positions", 10)
    cfg.setdefault("max_event_age_s", 300)
    cfg = notifications_setup(cfg)
    cfg = llm_policy.setup(cfg)

    cfg["poll_seconds"] = int(cfg.get("poll_seconds", 30))
    _save(CONFIG_PATH, cfg, private=True)
    print(f"\nSetup complete. Config at {CONFIG_PATH}.")
    if cfg["dry_run"]:
        print("DRY-RUN is ON — set \"dry_run\": false in config.json to go live.")
    print("Start the heartbeat with: python agent.py run")


def _feed_access(cfg, state):
    try:
        from sources import agenthc_day_trade_ideas as hc
        line = hc.pass_summary(cfg, state)
        notice = state.get("pass_notice")
        return f"{line} — {notice}" if notice and "left" not in line else line
    except Exception:
        return "unknown"


def _book_desc(cfg, prefix=""):
    """One book's sizing in words; prefix "" = main pick, "other_" = shadow."""
    mode = str(cfg.get(f"{prefix}sizing_mode") or "contracts")
    if mode == "budget":
        return (f"${float(cfg.get(f'{prefix}budget_per_trade_usd', 0) or 0):,.0f}"
                "/trade budget")
    if mode == "percent":
        return (f"{float(cfg.get(f'{prefix}percent_per_trade', 0) or 0):g}%"
                " of account/trade")
    return f"{int(cfg.get(f'{prefix}contracts_per_trade', 1) or 1)} contract(s)/trade"


def _sizing_desc(cfg):
    desc = _book_desc(cfg)
    if str(cfg.get("other_sizing_mode") or "") in SIZING_MODES:
        desc += f" (other trades: {_book_desc(cfg, 'other_')})"
    return desc


_BOOK_VALUE_KEY = {"budget": "budget_per_trade_usd",
                   "contracts": "contracts_per_trade",
                   "percent": "percent_per_trade"}


def set_book_sizing(cfg, book, mode, value):
    """Set one book's sizing in cfg (not saved). book: "main" or "other".
    mode: budget | contracts | percent, or "same" (other only) to clear the
    override. Returns (ok, error). Shared by the CLI wizard, the dashboard
    wizard and chat commands so all three validate the same way."""
    prefix = "" if book == "main" else "other_"
    if book == "other" and mode in ("same", "", None):
        for k in ("sizing_mode", *_BOOK_VALUE_KEY.values(), "max_contracts_per_trade"):
            cfg.pop(f"other_{k}", None)
        return True, ""
    if mode not in SIZING_MODES:
        return False, "choose dollar budget, contracts, or % of account"
    try:
        val = float(str(value).strip().lstrip("$").rstrip("%").replace(",", ""))
    except (TypeError, ValueError):
        val = 0.0
    if mode == "contracts":
        if val < 1 or val != int(val):
            return False, "contracts must be a whole number of 1 or more"
        val = int(val)
    elif mode == "percent":
        if not 0 < val <= 100:
            return False, "percent must be more than 0 and at most 100"
    elif val <= 0:
        return False, "budget must be a positive dollar amount"
    cfg[f"{prefix}sizing_mode"] = mode
    for m, key in _BOOK_VALUE_KEY.items():
        if m != mode and book == "other":
            cfg.pop(f"other_{key}", None)  # stale override values confuse the overlay
    cfg[f"{prefix}{_BOOK_VALUE_KEY[mode]}"] = val
    if book == "main":
        cfg.setdefault("contracts_per_trade", 1)
    cfg.setdefault("max_contracts_per_trade", 100)
    return True, ""


def _rule_command(t, cfg):
    """Dashboard chat commands for the built-in rules. (handled, reply)."""
    import rules

    def saved(reply):
        _save(CONFIG_PATH, cfg, private=True)
        return True, reply + " Rules now: " + (rules.describe(cfg) or "none") + "."
    if t in ("rules", "my rules", "show rules"):
        return True, ("Your rules: " + (rules.describe(cfg) or "none") + ". Change "
                      "with: tickers SPY QQQ / tickers any, skip tickers TSLA / "
                      "skip tickers none, calls only / puts only / calls and puts, "
                      "min strike 10, no 0dte on|off, loss stop 2 (0 = off).")
    m = re.match(r"^(?:set |only )?tickers?\s+(.+)$", t)
    if m:
        val = m.group(1).strip()
        cfg["rule_tickers"] = [] if val in ("any", "all", "none", "off") \
            else rules.parse_tickers(val)
        return saved("Ticker list updated.")
    m = re.match(r"^skip tickers?\s+(.+)$", t)
    if m:
        val = m.group(1).strip()
        cfg["rule_skip_tickers"] = [] if val in ("none", "off") \
            else rules.parse_tickers(val)
        return saved("Skip list updated.")
    if t in ("calls only", "puts only", "calls and puts", "both sides"):
        cfg["rule_sides"] = t.split()[0] if t.endswith("only") else "both"
        return saved("Updated.")
    m = re.match(r"^(?:set )?min(?:imum)? strike\s+\$?([\d.]+)$", t)
    if m:
        try:
            cfg["rule_min_strike"] = float(m.group(1))
        except ValueError:
            return True, "Usage: min strike 10  (0 = off)"
        return saved("Minimum strike updated.")
    m = re.match(r"^(?:set )?loss stop\s+(\d+)$", t)
    if m:
        cfg["rule_loss_streak"] = int(m.group(1))
        return saved("Losing-streak stop updated.")
    if t in ("no 0dte on", "no 0dte off", "skip 0dte on", "skip 0dte off"):
        cfg["rule_skip_0dte"] = t.endswith("on")
        return saved("Updated.")
    return False, ""


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
    handled, reply = _rule_command(t, cfg)
    if handled:
        return True, reply
    if t in ("autostart on", "autostart off", "start at login on",
             "start at login off"):
        import keep_running
        ok, note = keep_running.set_autostart(t.endswith("on"))
        return True, (note[0].upper() + note[1:] + ".") if ok else note
    if t in ("keep awake on", "keep awake off", "stay awake on", "stay awake off"):
        cfg["keep_awake"] = t.endswith("on")
        _save(CONFIG_PATH, cfg, private=True)
        return True, ("Keep awake ON — this computer won't idle-sleep while the "
                      "agent runs (closing a laptop lid still sleeps it)."
                      if cfg["keep_awake"] else
                      "Keep awake OFF — the computer may sleep; the agent pauses "
                      "while it does.")
    if t in ("autostart", "start at login"):
        import keep_running
        return True, ("Starts at login: "
                      + ("ON" if keep_running.autostart_enabled() else "off")
                      + " — say 'autostart on' or 'autostart off'.")
    if t in ("policy off", "disable policy"):
        moved = llm_policy.disable_policy()
        return True, (f"Policy turned off (kept as {llm_policy.policy_path()}.disabled)."
                      if moved else "No policy was set.")
    if t in ("dry on", "dry-run on"):
        cfg["dry_run"] = True
        _save(CONFIG_PATH, cfg, private=True)
        return True, "Dry-run ON — actions logged, no real orders."
    if t in ("dry off", "dry-run off", "go live"):
        if llm_policy.entries_blocked(cfg):
            return True, ("Not going live: " + llm_policy.BLOCKED_MSG
                          + " (Say 'policy off' to remove it.)")
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
    if t.startswith("set other budget"):
        try:
            val = float(t.split()[-1].lstrip("$").replace(",", ""))
            assert val > 0
        except (ValueError, AssertionError, IndexError):
            return True, "Usage: set other budget 100"
        cfg["other_sizing_mode"] = "budget"
        cfg["other_budget_per_trade_usd"] = val
        _save(CONFIG_PATH, cfg, private=True)
        return True, (f"'Other trades' budget set to ${val:,.0f}/trade — main "
                      "pick keeps its own sizing (your decision — see DISCLAIMER.md).")
    if t.startswith("set other contracts"):
        try:
            val = int(t.split()[-1])
            assert val >= 1
        except (ValueError, AssertionError, IndexError):
            return True, "Usage: set other contracts 1"
        cfg["other_sizing_mode"] = "contracts"
        cfg["other_contracts_per_trade"] = val
        _save(CONFIG_PATH, cfg, private=True)
        return True, (f"'Other trades' sizing set to {val} contract(s)/trade — "
                      "main pick keeps its own sizing.")
    if t.startswith("set percent") or t.startswith("set other percent"):
        book = "other" if t.startswith("set other") else "main"
        ok, err = set_book_sizing(cfg, book, "percent", t.split()[-1])
        if not ok:
            return True, (f"{err}. Usage: set {'other ' if book == 'other' else ''}"
                          "percent 2")
        _save(CONFIG_PATH, cfg, private=True)
        who = "'Other trades'" if book == "other" else "Main pick"
        return True, (f"{who} sizing set to {_book_desc(cfg, 'other_' if book == 'other' else '')}"
                      " — sized from your account value at each entry "
                      "(your decision — see DISCLAIMER.md).")
    if t.startswith("set contracts"):
        ok, err = set_book_sizing(cfg, "main", "contracts", t.split()[-1])
        if not ok:
            return True, f"{err}. Usage: set contracts 1"
        _save(CONFIG_PATH, cfg, private=True)
        return True, f"Main pick sizing set to {_book_desc(cfg)}."
    if t in ("other sizing same", "clear other sizing"):
        set_book_sizing(cfg, "other", "same", None)
        _save(CONFIG_PATH, cfg, private=True)
        return True, "'Other trades' now size the same as the main pick."
    if t.startswith("set cap"):
        try:
            val = int(t.split()[-1])
            assert val >= 1
        except (ValueError, AssertionError, IndexError):
            return True, "Usage: set cap 3"
        cfg["max_entries_per_day"] = val
        _save(CONFIG_PATH, cfg, private=True)
        return True, f"Daily entry cap set to {val}."
    if t.startswith("set positions"):
        try:
            val = int(t.split()[-1])
            assert val >= 1
        except (ValueError, AssertionError, IndexError):
            return True, "Usage: set positions 5"
        cfg["max_open_positions"] = val
        _save(CONFIG_PATH, cfg, private=True)
        return True, f"Max concurrent open positions set to {val}."
    if t.startswith("set max loss"):
        try:
            val = float(t.split()[-1].lstrip("$").replace(",", ""))
            assert val >= 0
        except (ValueError, AssertionError, IndexError):
            return True, "Usage: set max loss 300   (0 = off)"
        cfg["max_daily_loss_usd"] = val
        _save(CONFIG_PATH, cfg, private=True)
        if val == 0:
            return True, "Max daily loss OFF."
        return True, (f"Max daily loss set to ${val:,.0f}: once today's estimated "
                      "realized loss reaches it, no new entries until tomorrow "
                      "(exits still run).")
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
                      f"{cfg.get('max_entries_per_day', 5)} · "
                      f"est. P&L today ${_est_pnl_today()[0]:,.0f}"
                      + (f" (max loss ${float(cfg['max_daily_loss_usd']):,.0f})"
                         if float(cfg.get('max_daily_loss_usd') or 0) > 0 else "")
                      + " · open: "
                      f"{', '.join(st.get('positions', {})) or 'none'}")
    return False, ""


def cmd_run(app_window=False):
    import keep_running
    # One copy only: two heartbeats on the same config would place every
    # order twice. After an update restart the old process may still be
    # exiting, so give it a moment.
    lock = keep_running.InstanceLock()
    if not lock.acquire(wait=20 if os.environ.get("AGENT_UPDATE_RESTARTED") else 2):
        print("The agent is already running (another window, the background, "
              "or start-at-login) — not starting a second copy.")
        print(f"Dashboard: {_dashboard_link()}")
        print("Stop it with: python agent.py stop")
        sys.exit(0)
    keep_running.clear_stop()
    import transfer
    moved = transfer.moved_away()
    if moved:
        print(f"This setup was moved to another computer ({moved.get('moved_at', '?')}). "
              "Not starting here, so the two computers can't both trade.")
        print("Running it on THIS computer again? First stop the other one, then: "
              "python agent.py resume-here")
        sys.exit(0)
    require_consent_or_exit()
    cfg = _load(CONFIG_PATH)
    from brokers import broker_ready as _broker_ready
    if not cfg or not cfg.get("source") or not _broker_ready(cfg):
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
    if llm_policy.entries_blocked(cfg):
        if state.get("positions"):
            print("WARNING: " + llm_policy.BLOCKED_MSG + " Starting anyway so "
                  "your open positions still get their exits.")
        else:
            print("Not starting: " + llm_policy.BLOCKED_MSG
                  + "\n  Connect your AI:  python agent.py setup --web  (AI step)"
                  + f"\n  or remove:        {llm_policy.policy_path()}")
            today = datetime.now(timezone.utc).date().isoformat()
            if state.get("policy_block_notified") != today:
                state["policy_block_notified"] = today
                _save(STATE_PATH, state, private=True)
                notify(cfg, "agentic-trader did NOT start: " + llm_policy.BLOCKED_MSG)
            sys.exit(1)

    def save_state(st):
        _save(STATE_PATH, st, private=True)  # holds the day-pass L402 token

    cash = {"at": 0.0, "bp": None, "warning": ""}
    import keep_awake
    awake = keep_awake.KeepAwake()
    awake.apply(cfg)

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
                **({"feed access": _feed_access(c, st)}
                   if c.get("source") == "agenthc" else {}),
                "sizing": _sizing_desc(c),
                "entries today": f"{_entries_today()} / cap {c.get('max_entries_per_day', 5)}",
                "policy brain": ("blocked" if llm_policy.entries_blocked(c)
                                 else "on" if llm_policy.enabled(c) else "off"),
                "rules": __import__("rules").describe(c) or "none",
                "market": "open" if _market_open_now() else "closed (slow polling)",
                "open positions": ", ".join(st.get("positions", {})) or "none",
                "start at login": ("on" if keep_running.autostart_enabled()
                                   else "off"),
                "keep awake": awake.describe(c),
                **({"buying power": f"${cash['bp']:,.2f}"}
                   if cash["bp"] is not None else {}),
            },
            "cash_warning": cash.get("warning", ""),
            "problems": _problems_today(),
        }

    dash_url = None
    try:
        dash_url = webui.start_dashboard(
            get_status, lambda: list(reversed(_recent_trades(25))),
            _apply_command, lambda: _load(CONFIG_PATH, {}) or {})
    except OSError as exc:
        print(f"dashboard not started ({exc}) — continuing headless")

    _refresh_cash(cfg, state, save_state, broker, client, cash)
    mode = "DRY-RUN (no orders)" if cfg.get("dry_run") else "LIVE"
    print(f"Heartbeat started [{mode}]: source={cfg['source']}, "
          f"{_sizing_desc(cfg)}, policy brain "
          f"{'ON' if llm_policy.enabled(cfg) else 'off'}. "
          + ("Stop: dashboard or `agent.py stop`."
             if os.environ.get(keep_running.SUPERVISED_ENV) else "Ctrl-C to stop."))
    if dash_url:
        print(f"Dashboard: {dash_url}  (remote? tunnel: ssh -L 8721:127.0.0.1:8721 user@host)")
        if app_window:
            import webui
            webui.open_app_window(dash_url)
    print("Reminder: you are responsible for every order this agent places.")
    try:
        interactive = sys.stdout.isatty()
    except Exception:
        interactive = False
    if interactive and not os.environ.get(keep_running.SUPERVISED_ENV):
        print("NOTE: running in THIS window — closing it stops the agent. To keep "
              "it running after you close the window: Ctrl-C, then "
              "`python agent.py start`.")
    if llm_policy.entries_blocked(cfg):
        print("\n" + "!" * 72 + "\nWARNING: " + llm_policy.BLOCKED_MSG
              + "\n" + "!" * 72 + "\n")
        notify(cfg, "agentic-trader WARNING: " + llm_policy.BLOCKED_MSG)
    notify(cfg, f"agentic-trader heartbeat started [{mode}], source={cfg['source']}"
                # never send the login token to a third-party notifier
                + (f" — dashboard {dash_url.split('?')[0]}" if dash_url else ""))
    while True:
        cfg = _load(CONFIG_PATH, cfg) or cfg  # dashboard edits apply live
        awake.apply(cfg)  # same thread every time (Windows needs that)
        poll = max(10, int(cfg.get("poll_seconds", 30)))
        if keep_running.stop_requested():
            keep_running.clear_stop()
            print("Stopped (agent.py stop). Open positions remain in your account:",
                  list(state["positions"]) or "none")
            notify(cfg, "agentic-trader stopped")
            return
        if webui.CONTROLS["stop"]:
            print("Stopped via dashboard. Open positions remain in your account:",
                  list(state["positions"]) or "none")
            notify(cfg, "agentic-trader stopped via dashboard")
            return
        try:
            if not webui.CONTROLS["paused"]:
                try:
                    _prune_expired_positions(cfg, state, save_state)
                except Exception as exc:
                    print(f"expiry prune error (non-fatal): {str(exc)[:150]}")
                if client is not None and hasattr(broker, "reconcile"):
                    import broker_events
                    broker_events.begin()
                    try:
                        # progress any unverified orders (fill verification,
                        # blackout-limit conversion) before new events
                        broker.reconcile(client, cfg, state, save_state)
                    except Exception as exc:
                        print(f"order reconcile error (non-fatal): {str(exc)[:150]}")
                    for r in broker_events.drain():
                        _report_problem(cfg, state, save_state, r["kind"],
                                        "", r["message"])
                # every 10 min, and right away when the owner goes live
                if (time.time() - cash["at"] > 600
                        or bool(cfg.get("dry_run")) != cash.get("dry")):
                    _refresh_cash(cfg, state, save_state, broker, client, cash)
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
            import support
            support.note_error(f"heartbeat error: {exc}")
            if cfg.get("source") == "agenthc":
                _explain_connection_error(exc, cfg, state, save_state)
        try:
            _maybe_daily_digest(cfg, state, save_state)
        except Exception:
            pass
        # Market-hours-aware cadence: fast while the market can move, slow
        # overnight/weekends; snappy while paused so 'resume' feels instant.
        if webui.CONTROLS["paused"]:
            wait = 2
        else:
            wait = poll if _market_open_now() else max(poll, 300)
        try:
            _sleep_unless_stopped(wait, webui.CONTROLS)
        except KeyboardInterrupt:
            print("\nStopped. Open positions remain in your account:",
                  list(state["positions"]) or "none")
            return


def _sleep_unless_stopped(seconds, controls):
    """Sleep in 1 s slices so a stop (dashboard button or `agent.py stop`)
    takes effect within a second, not after a 5-minute overnight poll."""
    import keep_running
    end = time.time() + seconds
    while True:
        left = end - time.time()
        if left <= 0 or controls.get("stop") or keep_running.stop_requested():
            return
        time.sleep(min(1.0, left))


def _agenthc_host():
    from urllib.parse import urlparse
    from sources.agenthc_day_trade_ideas import AGENTHC_API
    return urlparse(AGENTHC_API).hostname or "api.traderhc.com"


_NET_DIAG_EVERY_S = 900  # re-diagnose a persisting outage at most every 15 min


def _explain_connection_error(exc, cfg, state, save_state, now=None):
    """The feed can't be reached: work out WHY (antivirus, VPN, DNS filter…)
    and tell the owner once per day per cause, with the fix."""
    import net_check
    if not net_check.is_connection_error(exc):
        return None
    now = time.time() if now is None else now
    if now - float(state.get("net_diag_at") or 0) < _NET_DIAG_EVERY_S:
        return None
    state["net_diag_at"] = now
    diag = net_check.diagnose(_agenthc_host())
    if diag["ok"]:
        save_state(state)
        return diag
    print(net_check.summary(diag))
    import support
    support.note_error(net_check.summary(diag))
    key = f"{datetime.now(MARKET_TZ).date()}:{diag['problem']}"
    if state.get("net_diag_notified") != key:
        state["net_diag_notified"] = key
        notify(cfg, "agentic-trader: " + net_check.summary(diag))
    save_state(state)
    return diag


def cmd_netcheck():
    import net_check
    host = _agenthc_host()
    print(f"Checking the connection to {host} …")
    diag = net_check.diagnose(host)
    print(net_check.summary(diag))
    sys.exit(0 if diag["ok"] else 1)


def cmd_status():
    import keep_running
    pid = keep_running.running_pid()
    print(f"agent running    : " + (f"yes (pid {pid})" if pid else
          "yes" if keep_running.is_running() else "no"))
    auto = keep_running.autostart_status()
    print(f"start at login   : {'on' if auto['enabled'] else 'off'}"
          + (f" ({auto['path']})" if auto["enabled"] else ""))
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
    broker_name = cfg.get("broker", "robinhood")
    acct = cfg.get("robinhood_account", "")
    detail = f" (account ••••{acct[-4:]})" if broker_name == "robinhood" and acct else ""
    from brokers import broker_ready
    print(f"broker           : {broker_name}{detail}"
          f"{'' if broker_ready(cfg) else ' — NOT CONFIGURED'}")
    print(f"contracts/trade  : {cfg.get('contracts_per_trade', 'unset')}")
    print(f"mode             : {'DRY-RUN' if cfg.get('dry_run') else 'LIVE'}")
    print(f"policy brain     : {'ON (' + llm_policy.policy_path() + ')' if llm_policy.enabled(cfg) else 'off'}")
    print(f"daily entry cap  : {cfg.get('max_entries_per_day', 5)}")
    print(f"entries today    : {_entries_today()}")
    print(f"open positions   : {list(state.get('positions', {})) or 'none'}")


def doctor_checks():
    """Every readiness check as data: [{id, label, ok, detail, fix, who}].
    who = "owner" for steps only the human can do (agreement, Robinhood
    login, paying, choosing size) — an assistant hands those to them."""
    checks = []

    def check(cid, ok, label, detail="", fix="", who="agent"):
        checks.append({"id": cid, "label": label, "ok": bool(ok), "detail": detail,
                       "fix": "" if ok else fix, "who": who})

    check("python", sys.version_info >= (3, 10), "Python 3.10+",
          f"running {sys.version.split()[0]}",
          "re-run the installer (it installs a private Python 3.12 via uv)")
    check("consent", consent_ok(), "Consent accepted (DISCLAIMER.md)",
          "" if consent_ok() else "nothing can run or trade without it",
          "the OWNER reads and accepts it in the wizard: python agent.py setup --web",
          who="owner")

    cfg = _load(CONFIG_PATH, {}) or {}
    state = _load(STATE_PATH, {}) or {}
    src = cfg.get("source", "")
    check("source", bool(src and src in SOURCES), "Signal source configured",
          src or "unset", "python agent.py setup --from plan.json  (or the wizard)")

    if src == "agenthc":
        try:
            import requests as _rq
            api = os.getenv("AGENTHC_API", "https://api.traderhc.com")
            r = _rq.get(f"{api}/api/v1/trading/day-trade-ideas/track-record",
                        timeout=10)
            # Any HTTP answer proves reachability — 402 just means the free
            # daily teaser hit was already used (e.g. doctor ran twice today).
            detail = f"HTTP {r.status_code}"
            if r.status_code == 402:
                detail += " (free daily teaser already used — API is up)"
            check("agenthc_api", r.status_code in (200, 402), "AgentHC API reachable",
                  detail, "check your internet connection / firewall")
        except Exception as exc:
            import net_check
            diag = (net_check.diagnose(_agenthc_host())
                    if net_check.is_connection_error(exc) else None)
            if diag and not diag["ok"]:
                check("agenthc_api", False, "AgentHC API reachable",
                      diag["detail"], diag["fix"], who="owner")
            else:
                check("agenthc_api", False, "AgentHC API reachable", str(exc)[:80],
                      "check your internet connection / firewall")
        _feed_access_check(cfg, state, check)

    broker_name = cfg.get("broker", "")
    mod = BROKERS.get(broker_name)
    if mod is None:
        check("broker", False, "Broker configured", broker_name or "unset",
              "python agent.py setup --from plan.json  (or the wizard)")
    elif hasattr(mod, "verify"):
        ok, msg = mod.verify(cfg)
        check("broker", ok, f"Broker: {broker_name}", msg,
              "fix the keys in the plan / wizard broker step")
    else:
        ok = mod.client(cfg) is not None
        check("broker", ok, f"Broker: {broker_name}",
              "authenticated" if ok else "not authenticated",
              "the OWNER logs in: wizard → Broker → Connect Robinhood",
              who="owner" if broker_name == "robinhood" else "agent")

    # Informational, never a failure: unset sizing means 1 contract per
    # trade — the conservative default. Only the owner changes it.
    sized = bool(cfg.get("sizing_mode") or cfg.get("contracts_per_trade"))
    check("sizing", True, "Position sizing",
          _sizing_desc(cfg) + ("" if sized else " (default — only the owner changes it)"),
          who="owner")

    import keep_awake
    check("keep_awake", True, "Keep this computer awake while running",
          "on" if keep_awake.wanted(cfg) else
          "off — if the computer sleeps, the agent pauses and can miss trades",
          who="owner")

    import rules
    check("rules", True, "Your rules (built in, no AI needed)",
          rules.describe(cfg) or "none — every signal is eligible (caps still apply)",
          who="owner")

    blocked = llm_policy.entries_blocked(cfg)
    check("policy_ai", not blocked, "Trading policy can be checked",
          "no policy" if not llm_policy.enabled(cfg)
          else "policy + AI connected" if not blocked
          else "policy set but NO AI connected: every entry will be skipped",
          "the OWNER connects an AI in the wizard (AI step), or removes "
          + llm_policy.policy_path(), who="owner")

    if mod is not None and hasattr(mod, "buying_power"):
        try:
            cl = mod.client(cfg)
        except Exception:
            cl = None
        bp = _buying_power(mod, cl, cfg)
        warn = _cash_warning(cfg, bp)
        dry = bool(cfg.get("dry_run"))
        check("buying_power", dry or not warn, "Broker buying power",
              ("couldn't read it" if bp is None else f"${bp:,.2f}")
              + (f" — {warn}" if warn else "")
              + (" (dry-run: no orders, so this can't block trades yet)"
                 if dry and warn else ""),
              "the OWNER adds cash to the broker account or lowers the "
              "per-trade budget", who="owner")

    notif = bool(cfg.get("discord_webhook_url") or cfg.get("ntfy_topic")
                 or cfg.get("telegram_bot_token"))
    check("notifications", notif, "Notifications",
          "configured" if notif
          else "none set — you won't hear about trades or errors",
          "add one in the plan (notifications) or the wizard")
    return checks, cfg


def _feed_access_check(cfg, state, check):
    if cfg.get("agenthc_api_key"):
        check("feed_access", True, "Feed access", "Premium API key set")
        return
    from sources import agenthc_day_trade_ideas as hc
    tokens = hc._pass_tokens(cfg)
    if tokens:
        best, pending = None, False
        for token in tokens:
            try:
                st = hc.pass_status(token)
            except Exception:
                continue
            if st.get("status") == "active" or (st.get("status") == "exits_only"
                                                and best is None):
                best = st
            elif st.get("status") == "pending":
                pending = True
        if best:
            if best.get("status") == "exits_only":
                check("feed_access", False, "Feed access",
                      "signal days used up — exit signals only for a few days",
                      "the OWNER buys more signal days in the wizard (Signal source)",
                      who="owner")
                return
            left = best.get("days_left", best.get("trading_days_left", "?"))
            check("feed_access", True, "Feed access",
                  f"prepaid pass · {left} signal day(s) left")
            return
        check("feed_access", False, "Feed access",
              "pass invoice not paid yet" if pending else "prepaid pass expired",
              "the OWNER pays the invoice shown in the wizard (Signal source), "
              "or buys more signal days there", who="owner")
        return
    wallet = wallet_from_cfg(cfg)
    if wallet:
        try:
            bal = wallet.balance_sats()
            check("feed_access", bal >= 10_000, "Wallet funded",
                  f"{bal:,} sats ({'~' + str(bal // 12_000) + ' more market day(s)' if bal >= 10_000 else 'below one ~$10 day-pass'})",
                  "python agent.py fund 50000   (the OWNER pays the invoice)",
                  who="owner")
        except WalletError as exc:
            check("feed_access", False, "Lightning wallet", str(exc)[:80],
                  "python agent.py setup   (source step → recreate wallet)")
        return
    check("feed_access", False, "Feed access",
          "nothing paid yet — the feed needs prepaid signal days",
          "the OWNER buys signal days in the wizard (Signal source) and pays "
          "from their own wallet", who="owner")


def cmd_doctor(as_json=False):
    """Check every dependency of a working agent; print the exact fix for
    each failure. Exit 0 = ready to run, 1 = something needs attention.
    --json: machine-readable, for an assistant deciding whether setup is done."""
    checks, cfg = doctor_checks()
    failed = [c for c in checks if not c["ok"]]
    mode = ("DRY-RUN" if cfg.get("dry_run", True) else "LIVE")
    if as_json:
        print(json.dumps({
            "ready": not failed,
            "mode": mode,
            "checks": checks,
            "owner_steps": [c["fix"] for c in failed if c["who"] == "owner"],
            "agent_steps": [c["fix"] for c in failed if c["who"] == "agent"],
        }, indent=2))
        sys.exit(1 if failed else 0)
    print("agentic-trader doctor\n")
    for c in checks:
        line = f" {'✓' if c['ok'] else '✗'} {c['label']}"
        if c["detail"]:
            line += f" — {c['detail']}"
        print(line)
        if not c["ok"] and c["fix"]:
            print(f"      fix: {c['fix']}")
    if cfg.get("source") == "agenthc" and cfg.get("lnbits_url"):
        from lightning_wallet import demo_warning
        warn = demo_warning(cfg.get("lnbits_url"))
        if warn:
            print(f"  ⚠️  Wallet host: {warn}")
    if "dry_run" not in cfg:
        desc = "not set — setup defaults to DRY-RUN"
    elif cfg["dry_run"]:
        desc = "DRY-RUN (safe — logs what it WOULD do)"
    else:
        desc = "LIVE ORDERS"
    print(f"\nmode: {desc} | daily entry cap: {cfg.get('max_entries_per_day', 5)}")
    if failed:
        print(f"\n{len(failed)} problem(s): {', '.join(c['label'] for c in failed)}")
        sys.exit(1)
    print("\nAll checks passed — python agent.py run")


def cmd_setup_from(path, as_json=False):
    """Apply an assistant-written plan (see setup_plan.py). All-or-nothing."""
    import setup_plan
    try:
        plan = setup_plan.load_plan(path)
    except (OSError, ValueError) as exc:
        report = {"ok": False, "applied": [], "errors": [f"can't read plan: {exc}"],
                  "owner_steps": []}
        new = None
    else:
        cfg = _load(CONFIG_PATH, {}) or {}
        new, report = setup_plan.apply(plan, cfg, consent_ok(), HOME)
    if new is not None:
        _save(CONFIG_PATH, new, private=True)
        checks, _ = doctor_checks()
        report["owner_steps"] = [c["fix"] for c in checks
                                 if not c["ok"] and c["who"] == "owner"]
        report["remaining_agent_steps"] = [c["fix"] for c in checks
                                           if not c["ok"] and c["who"] == "agent"]
    if as_json:
        print(json.dumps(report, indent=2))
    else:
        if report["ok"]:
            print("Plan applied (dry-run stays ON):")
            for a in report["applied"]:
                print(f"  ✓ {a}")
            if report["owner_steps"]:
                print("\nStill needs the OWNER:")
                for s_ in report["owner_steps"]:
                    print(f"  → {s_}")
            print("\nThe plan file holds keys — delete it now.")
        else:
            print("Plan NOT applied (nothing was changed):")
            for e in report["errors"]:
                print(f"  ✗ {e}")
    sys.exit(0 if report["ok"] else 1)


def cmd_fund(sats):
    cfg = _load(CONFIG_PATH, {}) or {}
    wallet = wallet_from_cfg(cfg)
    if wallet is None:
        print("No Lightning wallet configured — run: python agent.py setup")
        sys.exit(1)
    print_funding_invoice(wallet, sats)


def _dashboard_link():
    import webui
    return f"http://127.0.0.1:{webui.APP_PORT}/dash?k={webui.ui_token()}"


def _open_running_dashboard():
    """If the agent is already running, open its dashboard instead of
    starting anything (the wizard and a second agent can't share its port).
    Returns True if it was running."""
    import keep_running
    if not keep_running.is_running():
        return False
    link = _dashboard_link()
    print("Your agent is already running. Its dashboard (settings live there "
          f"too):\n  {link}")
    try:
        import webbrowser
        webbrowser.open(link)
    except Exception:
        pass
    return True


def _open_dash(app_window=False):
    import webui
    try:
        if app_window:
            webui.open_app_window(_dashboard_link())
        else:
            import webbrowser
            webbrowser.open(_dashboard_link())
    except Exception:
        pass


def cmd_start(open_dashboard=False, app_window=False):
    """Start the agent in the background: closing the window doesn't stop
    it, and it restarts itself after a crash."""
    import keep_running
    import webui
    if keep_running.is_running():
        print("The agent is already running.")
        print(f"Dashboard: {_dashboard_link()}")
        if open_dashboard:
            _open_dash(app_window)
        return
    if not _is_ready():
        print("Not set up yet — run: python agent.py setup --web")
        sys.exit(1)
    import transfer
    if transfer.moved_away():
        print("This setup was moved to another computer, so it won't start here. "
              "To run it here again (stop the other one first): "
              "python agent.py resume-here")
        sys.exit(1)
    proc = keep_running.start_background(port=webui.APP_PORT)
    print("Starting the agent in the background…")
    if keep_running.wait_until_running(timeout=40, proc=proc):
        print("Running. You can close this window — the agent keeps going.")
        print(f"Dashboard: {_dashboard_link()}")
        if open_dashboard:
            _open_dash(app_window)
        print(f"Log:       {keep_running.log_path()}")
        print("Stop:      python agent.py stop")
        if not keep_running.autostart_enabled():
            print("Start it automatically at login: python agent.py autostart on")
        return
    print("It didn't start. Last lines of the log "
          f"({keep_running.log_path()}):")
    for line in keep_running.last_log_lines():
        print("  " + line)
    sys.exit(1)


def cmd_export(path):
    import getpass
    import keep_running
    import transfer
    if not path:
        path = transfer.export_filename()
    pw = getpass.getpass("Pick a passphrase for the move file (8+ characters): ")
    if getpass.getpass("Type it again: ") != pw:
        print("The passphrases didn't match — nothing exported.")
        sys.exit(1)
    try:
        blob = transfer.export_bundle(pw)
    except transfer.TransferError as exc:
        print(f"Export failed: {exc}")
        sys.exit(1)
    with open(path, "wb") as f:
        f.write(blob)
    print(f"Saved {path} ({len(blob) // 1024} KB). It holds your broker login and "
          "wallet keys — keep it like a password, and delete it after the move.")
    if input("Stop the agent on THIS computer now so only the new one trades? "
             "[Y/n] ").strip().lower() in ("", "y", "yes"):
        if keep_running.is_running():
            keep_running.stop()
        print(transfer.mark_moved_away())
    print("On the new computer: run the installer, then in the setup page choose "
          "\"Moving from another computer?\" (or: python agent.py import FILE).")


def cmd_import(path):
    import getpass
    import keep_running
    import transfer
    if not path or not os.path.exists(path):
        print("Usage: python agent.py import <move file>")
        sys.exit(1)
    if keep_running.is_running():
        print("Stop the agent on this computer first: python agent.py stop")
        sys.exit(1)
    with open(path, "rb") as f:
        blob = f.read()
    pw = getpass.getpass("Passphrase for the move file: ")
    replace = False
    while True:
        try:
            info = transfer.import_bundle(blob, pw, replace=replace)
            break
        except transfer.TransferError as exc:
            if str(exc) == "NEEDS_REPLACE" and not replace:
                if input("This computer already has a setup. Replace it? [y/N] "
                         ).strip().lower() in ("y", "yes"):
                    replace = True
                    continue
                print("Nothing changed.")
                sys.exit(1)
            print(f"Import failed: {exc}")
            sys.exit(1)
    print(f"Imported your setup ({info['files']} files, from {info['from_os']}, "
          f"{info['created_at']}).")
    print("It starts in DRY-RUN. Make sure the old computer is off, then say "
          "`dry off` in the dashboard" + (" to go live again." if info["was_live"]
                                         else " when you want to go live."))
    print("Start it: python agent.py start")


def cmd_stop():
    import keep_running
    sys.exit(0 if keep_running.stop() else 1)


def cmd_supervise():
    import keep_running
    port = None
    if "--port" in sys.argv:
        i = sys.argv.index("--port")
        if i + 1 < len(sys.argv) and sys.argv[i + 1].isdigit():
            port = int(sys.argv[i + 1])
    sys.exit(keep_running.supervise(port=port))


def cmd_autostart(action):
    import keep_running
    if action == "on":
        if not _is_ready():
            print("Finish setup first: python agent.py setup --web")
            sys.exit(1)
        where = keep_running.enable_autostart()
        print(f"On — the agent starts in the background every time you log in.\n"
              f"  ({where})")
        if not keep_running.is_running():
            print("It isn't running right now — start it with: python agent.py start")
    elif action == "off":
        removed = keep_running.disable_autostart()
        print("Off — it won't start at login." if removed
              else "Start at login was already off.")
        if keep_running.is_running():
            print("The agent is still running now; stop it with: python agent.py stop")
    else:
        st = keep_running.autostart_status()
        print(f"Start at login: {'on' if st['enabled'] else 'off'} ({st['how']})")
        if st["enabled"]:
            print(f"  {st['path']}")
        print("Change: python agent.py autostart on|off")


def _is_ready():
    """Consent accepted AND a source + broker are configured."""
    if not consent_ok():
        return False
    cfg = _load(CONFIG_PATH) or {}
    from brokers import broker_ready
    return bool(cfg.get("source") and broker_ready(cfg))


def main():
    # Verify HTTPS with the OS certificate store, like the browser does — so
    # antivirus/workplace HTTPS inspection with an installed certificate
    # doesn't break the agent while Chrome works.
    import net_check
    net_check.use_system_trust()
    # No subcommand = do the right thing. Safe because nothing can trade until
    # the legal agreement is accepted: not set up -> open the browser wizard;
    # set up -> run the agent. So `python agent.py` is the whole thing.
    if len(sys.argv) <= 1:
        if _open_running_dashboard():
            return
        if _is_ready():
            # Background, like the wizard's Start button: closing this
            # terminal must not stop the agent. (`run` = foreground.)
            cmd_start(open_dashboard=True)
        else:
            print("Not set up yet — opening the setup wizard in your browser.")
            print("(Nothing runs or trades until you accept DISCLAIMER.md there.)")
            import webui
            webui.run_wizard()
        return

    cmd = sys.argv[1]
    if cmd == "setup":
        if "--web" in sys.argv:
            if _open_running_dashboard():
                return
            import webui
            webui.run_wizard()
        elif "--from" in sys.argv:
            i = sys.argv.index("--from")
            if i + 1 >= len(sys.argv):
                print("Usage: python agent.py setup --from plan.json [--json]")
                sys.exit(1)
            cmd_setup_from(sys.argv[i + 1], as_json="--json" in sys.argv)
        elif "--example-plan" in sys.argv:
            import setup_plan
            print(json.dumps(setup_plan.EXAMPLE_PLAN, indent=2))
        else:
            cmd_setup()
    elif cmd == "app":
        cmd_start(open_dashboard=True, app_window=True)
    elif cmd == "run":
        cmd_run(app_window="--app" in sys.argv)
    elif cmd == "start":
        cmd_start()
    elif cmd == "stop":
        cmd_stop()
    elif cmd == "export":
        cmd_export(sys.argv[2] if len(sys.argv) > 2 else "")
    elif cmd == "import":
        cmd_import(sys.argv[2] if len(sys.argv) > 2 else "")
    elif cmd == "resume-here":
        import transfer
        print("OK — this computer may run the agent again. Make sure the other "
              "computer is stopped." if transfer.resume_here()
              else "This setup wasn't marked as moved away.")
    elif cmd == "supervise":
        cmd_supervise()
    elif cmd == "autostart":
        cmd_autostart(sys.argv[2] if len(sys.argv) > 2 else "status")
    elif cmd == "status":
        cmd_status()
    elif cmd == "doctor":
        cmd_doctor(as_json="--json" in sys.argv)
    elif cmd == "netcheck":
        cmd_netcheck()
    elif cmd == "link":
        import webui
        print("Dashboard login link (keep it private — it unlocks the dashboard):")
        print(f"  {_dashboard_link()}")
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
