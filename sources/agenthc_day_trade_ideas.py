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
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

from lightning_wallet import wallet_from_cfg, wallet_setup

MARKET_TZ = ZoneInfo("America/New_York")


def _now_et():
    return datetime.now(MARKET_TZ)


NAME = "agenthc"
DESCRIPTION = ("AgentHC Agentic Day Trade Ideas — journal events from a "
               "published paper-trade system (~$10/day in sats, or Premium key)")

AGENTHC_API = os.getenv("AGENTHC_API", "https://api.traderhc.com")
FEED_PATH = "/api/v1/trading/day-trade-ideas"


PASS_PATH = FEED_PATH + "/pass"
PASS_LOW_DAYS = 2               # warn when this many signal days (or fewer) remain
# Server statuses that still read the feed: "exits_only" = days used up, but
# exit signals keep coming for a few market days so open positions can close.
_USABLE = ("active", "exits_only")
_PENDING_RECHECK_S = 60         # status poll cadence while an invoice is unpaid
_INVOICE_LIFETIME_S = 3600      # server invoices expire after 1h


class PassUnavailable(RuntimeError):
    """The feed server isn't selling prepaid passes (yet)."""


def buy_pass(days, extend_token=None):
    """Ask AgentHC for an invoice covering `days` SIGNAL days — a market day
    is only used when the agent is served an entry/exit that day. Returns the
    server body (payment_request, pass_token, amount_sats, signal_days…).
    `extend_token` (the current pass) carries its unused days over."""
    payload = {"days": int(days)}
    if extend_token:
        payload["extend"] = extend_token
    resp = requests.post(f"{AGENTHC_API}{PASS_PATH}", json=payload, timeout=20)
    if resp.status_code == 404:
        raise PassUnavailable("Prepaid passes aren't on sale yet — use a "
                              "Lightning wallet (Advanced) for now.")
    if resp.status_code != 200:
        try:
            detail = resp.json().get("detail")
        except ValueError:
            detail = resp.text
        if isinstance(detail, dict):
            detail = detail.get("message") or detail.get("error") or detail
        raise RuntimeError(f"pass purchase failed ({resp.status_code}): {str(detail)[:160]}")
    return resp.json()


def pass_status(token):
    """{'status': pending|active|exits_only|used_up|superseded|expired|invalid,
    'days_left', 'valid_until', …}."""
    resp = requests.get(f"{AGENTHC_API}{PASS_PATH}",
                        headers={"Authorization": token}, timeout=15)
    if resp.status_code == 401:
        return {"status": "invalid"}
    resp.raise_for_status()
    return resp.json()


def _pass_tokens(cfg):
    """Prepaid pass tokens from config, newest purchase first."""
    out = []
    for key in ("feed_pass_pending", "feed_pass"):
        tok = (cfg.get(key) or {}).get("token")
        if tok and tok not in out:
            out.append(tok)
    return out


def _token_id(token):
    import hashlib
    return hashlib.sha256(token.encode()).hexdigest()[:16]


def _refresh_pass(cfg, state, save_state, force=False):
    """Return the usable (active) pass token, checking the server only when
    needed: pending invoices at most once a minute until they expire, active
    passes only after their end time or when the feed refused them."""
    cache = state.setdefault("pass_status", {})
    now = time.time()
    best, best_until = None, 0.0
    for token in _pass_tokens(cfg):
        tid = _token_id(token)
        rec = cache.get(tid) or {}
        st = rec.get("status")
        if st == "pending" and now - rec.get("first_seen", now) > _INVOICE_LIFETIME_S + 300:
            rec["status"] = st = "unpaid"   # invoice expired without payment
            cache[tid] = rec
            save_state(state)
        stale = (force or st is None
                 or (st == "pending" and now - rec.get("checked", 0) > _PENDING_RECHECK_S
                     and now - rec.get("first_seen", now) < _INVOICE_LIFETIME_S + 300)
                 or (st in _USABLE and now > rec.get("until_ts", 0)))
        if stale:
            try:
                fresh = pass_status(token)
            except Exception:
                fresh = None
            if fresh is not None:
                until_ts = _iso_ts(fresh.get("access_until"))
                rec = {"status": fresh.get("status"), "until_ts": until_ts,
                       "days_left": fresh.get("days_left", fresh.get("trading_days_left")),
                       "checked": now, "first_seen": rec.get("first_seen", now)}
                cache[tid] = rec
                save_state(state)
        if rec.get("status") in _USABLE:
            # prefer a pass with days left over one in its exits-only grace
            rank = rec.get("until_ts", 0) + (1e12 if rec.get("status") == "active" else 0)
            if rank > best_until:
                best, best_until = token, rank
    return best


def _iso_ts(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _pass_notice(cfg, state, save_state, key, text):
    """Once-per-day notification (dashboard + Discord/ntfy/Telegram)."""
    day = time.strftime("%Y-%m-%d")
    if state.get(key) == day:
        return
    state[key] = day
    state["pass_notice"] = text
    save_state(state)
    print(text)
    try:
        from notifications import notify
        notify(cfg, text)
    except Exception:
        pass


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


def _pass_purchase_allowed(cfg, state, save_state, now=None):
    """Recurring-day-pass gate: the agent only auto-pays the ~$10 pass when
    the operator turned 'Recurring day-pass' ON (wizard Source tab / config
    day_pass_recurring). OFF (the default) = the agent never spends without
    approval — it notifies once per day and returns no events instead.

    Never buys on weekends regardless of the recurring flag: the feed only
    publishes on market days, so a Saturday/Sunday pass is pure wasted sats
    (the 24h token expires before Monday's first event). The API also stops
    selling passes on non-trading days server-side; this check keeps every
    consumer safe even against an older server."""
    now = now or _now_et()
    if now.weekday() >= 5:
        day = now.strftime("%Y-%m-%d")
        if state.get("weekend_skip_notified") != day:
            state["weekend_skip_notified"] = day
            save_state(state)
            print("Day-pass purchase skipped: market closed (weekend), no "
                  "events publish today — auto-buy resumes next market day.")
        return False
    if cfg.get("day_pass_recurring"):
        spent = _autopay_spent_today(state, now)
        cap = _daily_autopay_cap(cfg)
        if spent["sats"] >= cap:
            day = now.strftime("%Y-%m-%d")
            if state.get("autopay_cap_notified") != day:
                state["autopay_cap_notified"] = day
                save_state(state)
                print(f"Day-pass auto-pay STOPPED for today: already paid "
                      f"{spent['sats']:,} sats ({spent['count']} pass(es)), "
                      f"daily cap {cap:,} (max_autopay_sats_per_day). If a "
                      "pass you paid for was rejected, contact the feed "
                      "operator before raising the cap. Resumes tomorrow.")
            return False
        return True
    day = time.strftime("%Y-%m-%d")
    if state.get("pass_needed_notified") != day:
        state["pass_needed_notified"] = day
        save_state(state)
        print("Day-pass required (~$10 in sats) but recurring payments are "
              "OFF — turn ON 'Recurring day-pass' in the wizard Source tab "
              "to let the agent buy it each day.")
    return False


def _daily_autopay_cap(cfg):
    """Total sats the agent may auto-pay per market day. Defaults to ONE
    invoice's worth (max_autopay_sats): a healthy feed never needs a second
    pass the same day, so a second purchase means something is wrong (e.g.
    the server keeps rejecting a valid pass) — without this cap the agent
    would re-buy on every poll and drain the wallet."""
    per_invoice = int(cfg.get("max_autopay_sats", 30_000))
    return int(cfg.get("max_autopay_sats_per_day", per_invoice))


def _autopay_spent_today(state, now=None):
    day = (now or _now_et()).strftime("%Y-%m-%d")
    spent = state.get("autopay_spent") or {}
    if spent.get("day") != day:
        spent = {"day": day, "sats": 0, "count": 0}
    return spent


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
    spent = _autopay_spent_today(state)
    daily_cap = _daily_autopay_cap(cfg)
    if spent["sats"] + amount > daily_cap:
        raise RuntimeError(
            f"Day-pass ({amount:,} sats) would exceed today's auto-pay cap: "
            f"{spent['sats']:,} of {daily_cap:,} sats already spent "
            "(max_autopay_sats_per_day in config.json).")
    # Count BEFORE paying: a payment that errors after the wallet already
    # sent the sats must still consume the daily budget.
    spent["sats"] += amount
    spent["count"] += 1
    state["autopay_spent"] = spent
    save_state(state)
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
    # Prepaid multi-day pass (bought in the dashboard, paid from any wallet):
    # takes precedence over the per-day wallet auto-pay.
    if not cfg.get("agenthc_api_key") and _pass_tokens(cfg):
        token = _refresh_pass(cfg, state, save_state)
        if token:
            return _poll_with_pass(cfg, state, save_state, token, _retried)
        if not (cfg.get("lnbits_url") and cfg.get("day_pass_recurring")):
            statuses = {r.get("status") for r in state.get("pass_status", {}).values()}
            if "pending" in statuses:
                return []  # invoice shown, not paid yet — nothing to read
            _pass_notice(cfg, state, save_state, "pass_expired_notified",
                         "Your AgentHC feed pass has run out — the agent gets no "
                         "signals until you buy more signal days (dashboard → "
                         "Settings → Signal source).")
            return []
        # else: fall through to the wallet's daily auto-pay
    # Skip the request entirely when access is impossible: no API key, no
    # live day-pass token, and buying one isn't allowed right now (recurring
    # OFF, or weekend). Every such GET makes the server mint a Lightning
    # invoice that is guaranteed to expire unpaid — at market-hours poll
    # cadence that's ~1,000 doomed invoices a day against the publisher's
    # node. The once-per-day notify inside _pass_purchase_allowed still fires.
    if not _headers(cfg, state) and not _pass_purchase_allowed(cfg, state, save_state):
        return []
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
    return _parse_events(resp.json())


def _poll_with_pass(cfg, state, save_state, token, _retried):
    track = "all" if cfg.get("include_other_trades") else "main"
    resp = requests.get(f"{AGENTHC_API}{FEED_PATH}",
                        params={"limit": 20, "track": track},
                        headers={"Authorization": token}, timeout=15)
    if resp.status_code in (401, 402) and not _retried:
        _refresh_pass(cfg, state, save_state, force=True)  # ended/refused: recheck
        return poll(cfg, state, save_state, _retried=True)
    if resp.status_code == 403:
        detail = resp.json().get("detail", {})
        if isinstance(detail, dict) and detail.get("error") == "feed_not_live":
            return []
        raise RuntimeError(f"403 from feed: {str(detail)[:200]}")
    resp.raise_for_status()
    body = resp.json()
    left = body.get("pass_days_left", body.get("pass_trading_days_left"))
    exits_only = bool(body.get("pass_exits_only"))
    rec = state.setdefault("pass_status", {}).get(_token_id(token))
    if rec is not None:
        new_status = "exits_only" if exits_only else "active"
        if rec.get("days_left") != left or rec.get("status") != new_status:
            rec["days_left"] = left
            rec["status"] = new_status
            save_state(state)
    if exits_only:
        _pass_notice(cfg, state, save_state, "pass_used_up_notified",
                     "Your AgentHC feed pass is used up — for a few more days "
                     "the agent still gets EXIT signals (so open positions can "
                     "close), but no new entries. Buy more signal days in the "
                     "dashboard (Settings → Signal source).")
    elif isinstance(left, int) and left <= PASS_LOW_DAYS:
        _pass_notice(cfg, state, save_state, "pass_low_notified",
                     f"Your AgentHC feed pass has {left} signal day(s) left — "
                     "buy more in the dashboard (Settings → Signal source); "
                     "unused days carry over.")
    return _parse_events(body)


def pass_summary(cfg, state):
    """One line for the dashboard: what feed access the agent has right now."""
    if cfg.get("agenthc_api_key"):
        return "Premium API key"
    best = None
    for token in _pass_tokens(cfg):
        rec = (state.get("pass_status") or {}).get(_token_id(token)) or {}
        if rec.get("status") in _USABLE and (
                best is None or (rec.get("status") == "active"
                                 and best.get("status") != "active")):
            best = rec
    if best and best.get("status") == "exits_only":
        return "prepaid pass used up — exit signals only for a few days; buy more"
    if best:
        left = best.get("days_left")
        return (f"prepaid pass · {left} signal day(s) left" if left is not None
                else "prepaid pass · active")
    if _pass_tokens(cfg):
        return "prepaid pass · waiting for payment or expired"
    if cfg.get("lnbits_url"):
        return ("wallet auto-pay (recurring ON)" if cfg.get("day_pass_recurring")
                else "wallet — recurring day-pass OFF")
    return "no access configured"


def _parse_events(body):
    events = []
    for ev in body.get("events", []):
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
