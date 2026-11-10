"""The dashboard's view-model: everything the UI shows, as data.

`build_status()` turns what the agent knows — its settings, its memory
(state.json), its trade log and a few facts the running loop holds in
memory — into one typed, JSON-able dict (schema 2), served at
GET /api/v2/status. `activity_page()` does the same for older trade-log rows
(GET /api/v2/activity).

Rules this module keeps:

  - No I/O. Every input is handed in; nothing here reads a file, asks the
    broker, the wallet or the network, or looks at the clock when `now` is
    given. That is what makes every screen state a plain unit test.
  - No second copy of a trading rule. What the agent will do with a position,
    when the expiry guard runs, whether the market is open and which checks
    an entry passes are asked of the same functions the loop uses (agent.py,
    brokers/market_hours.py). The ordered list of entry checks is
    agent.ENTRY_CHECKS.
  - Never a guess. A fact that was not recorded (an older trade-log row, a
    broker that doesn't report its connection) is null, and the type says so.
  - Hostile input can't break the page: every string is cut to a sane length,
    every number checked, and an unreadable row becomes "unknown".

The schema is declared once, below (types()). The TypeScript the React app
compiles against (webui_app/src/lib/viewModel.ts) is generated from it —
`python view_model.py --ts` — and tests/test_view_model.py fails when that
file is stale or when the builder's output and the declaration disagree.
"""

import functools
import math
import re
from datetime import datetime, timedelta, timezone

SCHEMA_VERSION = 2
TS_PATH = "webui_app/src/lib/viewModel.ts"
ACTIVITY_LIMIT = 50      # trade-log rows in a status payload
ACTIVITY_LIMIT_MAX = 200
_TEXT = 300              # longest free text passed through (a reason, a note)
_NAME = 80               # longest name / code / contract text

# ── schema declaration ───────────────────────────────────────────────────────
# Primitive types: "str", "num" (a finite number), "int", "bool", "iso" (an
# ISO-8601 UTC time, "2026-11-09T19:41:07Z"). Every field is always present;
# "not known" is null, never a missing key.


def Opt(t):
    return ("opt", t)


def Enum(*values):
    return ("enum", values)


def List(t):
    return ("list", t)


def Ref(name):
    return ("ref", name)


def Lit(value):
    return ("lit", value)


MAP = ("map",)  # {string: string | number}

MODES = ("live", "practice")
PAUSE_KINDS = ("none", "entries", "state_hold", "handoff_hold", "restarting",
               "stopped")
PAUSE_SCOPES = ("none", "entries", "all")
# why a real position is not being sold for the owner right now
UNMANAGED_REASONS = ("agent_stopped", "agent_not_answering", "state_hold",
                     "handoff_hold", "restarting", "broker_down",
                     "practice_holds_real_sells", "other_account", "expired",
                     "no_exit_signals", "feed_unreachable", "no_bid")
ANSWER_STATES = ("yes", "practice_only", "not_today", "paused", "cant",
                 "unknown")
ANSWER_REASONS = (
    "ok", "practice_mode", "nothing_open", "only_practice_positions",
    "agent_stopped", "agent_not_answering", "state_hold", "handoff_hold",
    "restarting", "owner_pause", "pause_feed_not_read", "broker_down",
    "daily_cap_reached", "daily_loss_reached", "position_cap_reached",
    "market_closed", "no_signal_access", "feed_unreachable", "ai_needed",
    "practice_holds_real_sells")
NEXT_ACTIONS = ("sell_on_exit", "sell_at_expiry_guard", "selling_now",
                "paper_close", "unmanaged", "unknown")
NEXT_REASONS = UNMANAGED_REASONS + (
    "exit_signal", "expiry_guard", "sell_working", "exit_remainder",
    "pause_feed_not_read")
PASS_KINDS = ("prepaid", "day_pass", "subscription", "not_needed", "none")
PASS_STATES = ("active", "exits_only", "waiting", "none")

# trade-log kinds ("action") -> the reason code a row of that kind gets when
# the row itself doesn't carry a more exact one. Anything else is "unknown".
_KIND_CODES = {
    "entry": "entered", "exit": "exit_signal", "ledger_adopt": "records_corrected",
    "skip_paused": "paused", "skip_daily_cap": "daily_cap_reached",
    "skip_position_cap": "position_cap_reached",
    "skip_daily_loss": "daily_loss_reached", "skip_rule": "rule",
    "policy_veto": "written_rules_veto", "skip_ai_needed": "ai_needed",
    "skip_stale": "signal_too_old", "skip_sizing": "size_unknown",
    "skip_budget": "over_trade_size", "skip_no_quote": "no_quote",
    "skip_no_contract": "contract_not_found",
    "broker_lookup_failed": "broker_lookup_failed",
    "broker_disconnected": "broker_disconnected",
    "order_failed": "order_rejected", "entry_unfilled": "order_not_filled",
    "broker_no_order": "broker_no_order", "exit_failed": "exit_failed",
    "exit_partial": "exit_partial",
}
# more exact codes the agent records on the row (agent._row_facts "code"), or
# that follow from a recorded cause
_ROW_CODES = ("past_expiry_guard", "exit_already_arrived",
              "rule_not_in_list", "rule_skip_list", "rule_side",
              "rule_min_strike", "rule_0dte", "rule_loss_streak",
              "expiry_guard", "exit_remainder", "closed_outside",
              # an order that went in and may have filled (agent
              # _report_problem placed=True): never "rejected"/"failed"
              "buy_unconfirmed", "sell_unconfirmed", "buy_partly_filled")
REASON_CODES = tuple(dict.fromkeys(tuple(_KIND_CODES.values()) + _ROW_CODES
                                   + ("unknown",)))


def check_ids():
    """The entry checks, in the order the agent runs them."""
    import agent as A
    return tuple(c for c, _kinds in A.ENTRY_CHECKS)


def aliases():
    """Named unions (declared in the TypeScript as const arrays)."""
    return {"CheckId": Enum(*check_ids()), "ReasonCode": Enum(*REASON_CODES)}


def _types():
    checks = Ref("CheckId")
    book = Enum("main", "other")
    per_book = lambda t: {"real": t, "practice": t}  # noqa: E731
    return {
        "Status": {
            "schema": Lit(SCHEMA_VERSION),
            "as_of": "iso",
            "agent": Ref("Agent"),
            "mode": Enum(*MODES),
            "mode_explicit": "bool",
            "pause": Ref("Pause"),
            "real_positions": "int",
            "real_positions_managed": "bool",
            "unmanaged_reason": Opt(Enum(*UNMANAGED_REASONS)),
            "expiry_guard_on": "bool",
            "broker": Ref("Broker"),
            "signals": Ref("Signals"),
            "market": Ref("Market"),
            "limits": Ref("Limits"),
            "capabilities": Ref("Capabilities"),
            "entry_checks": List(checks),
            "positions": List(Ref("Position")),
            "activity": Ref("ActivityPage"),
            "results": Ref("Results"),
            "notices": List(Ref("Notice")),
        },
        "Agent": {
            "running": "bool",
            "answering": "bool",
            "last_heartbeat": Opt("iso"),
            "heartbeat_every_s": Opt("num"),
            "started_at": Opt("iso"),
            "settings_readable": "bool",
        },
        "Pause": {
            "kind": Enum(*PAUSE_KINDS),
            "scope": Enum(*PAUSE_SCOPES),
            "entries_paused": "bool",
            "since": Opt("iso"),
            "survives_restart": "bool",
            "can_resume": "bool",
        },
        "Broker": {
            "id": "str",
            "name": "str",
            "account_mask": Opt("str"),
            "paper_account": "bool",
            "connected": Opt("bool"),
            "since": Opt("iso"),
            "last_answer": Opt("iso"),
            "last_error": Opt("str"),
            "buying_power": Opt("num"),
            "buying_power_at": Opt("iso"),
        },
        "Signals": {
            "source": "str",
            "reading": "bool",
            "entry_access": Opt("bool"),
            "last_poll": Opt("iso"),
            "last_poll_ok": Opt("bool"),
            "last_signal": Opt("iso"),
            "last_signal_from": Opt(Enum("feed", "activity_log")),
            "pass": Ref("Pass"),
        },
        "Pass": {
            "kind": Enum(*PASS_KINDS),
            "status": Enum(*PASS_STATES),
            "days_left": Opt("int"),
            "auto_pay": "bool",
        },
        "Market": {
            "open": "bool",
            "trading_day": "bool",
            "opens_at": "iso",
            "closes_at": "iso",
            "early_close": "bool",
            "timezone": Lit("America/New_York"),
        },
        "Limits": {
            "cap": "int",
            "used_today": per_book("int"),
            "cap_reached": "bool",
            "resets_at": "iso",
            "max_daily_loss": Opt("num"),
            "realised_pnl_today": per_book("num"),
            "realised_loss_today": per_book("num"),
            "unmeasured_exits_today": per_book("int"),
            "loss_limit_reached": "bool",
            "max_positions": "int",
            "open_positions": per_book("int"),
            "positions_reached": "bool",
            "trade_size": {"main": Ref("TradeSize"), "other": Ref("TradeSize")},
            "cash": Ref("Cash"),
        },
        "TradeSize": {
            "mode": Enum("budget", "contracts", "percent"),
            "usd": Opt("num"),
            "contracts": Opt("int"),
            "percent": Opt("num"),
            "max_contracts": Opt("int"),
            "same_as_main": "bool",
        },
        "Cash": {
            "low": "bool",
            "low_reason": Opt(Enum("under_minimum", "under_trade_size")),
            "trades_affordable": Opt("int"),
        },
        "Capabilities": {
            "opening": Ref("Answer"),
            "closing": Ref("Answer"),
            "listening": Ref("Answer"),
        },
        "Answer": {
            "state": Enum(*ANSWER_STATES),
            "reason_code": Enum(*ANSWER_REASONS),
        },
        "Contract": {
            "symbol": "str",
            "expiry": "str",
            "strike": "num",
            "side": Enum("C", "P"),
        },
        "Position": {
            "id": "str",
            "contract": Opt(Ref("Contract")),
            "label": "str",
            "qty": "int",
            "entry_price": Opt("num"),
            "entry_cost_usd": Opt("num"),
            "opened_at": Opt("iso"),
            "real": "bool",
            "account": Opt("str"),
            "book": Opt(book),
            "expires_today": "bool",
            "pending": Opt(Enum("buy", "sell")),
            "next": Ref("NextAction"),
        },
        "NextAction": {
            "action": Enum(*NEXT_ACTIONS),
            "at": Opt("iso"),
            "reason_code": Enum(*NEXT_REASONS),
            "expiry_guard_at": Opt("iso"),
        },
        "ActivityPage": {
            "rows": List(Ref("Activity")),
            "total": "int",
            "next": Opt("int"),
            "oldest": Opt("int"),
        },
        "Activity": {
            "n": "int",
            "at": Opt("iso"),
            "action": "str",
            "known": "bool",
            "contract": Opt(Ref("Contract")),
            "contract_text": Opt("str"),
            "book": Opt(book),
            "real": Opt("bool"),
            "paper_account": Opt("bool"),
            "mode_at_time": Opt(Enum(*MODES)),
            "order_sent": Opt("bool"),
            "filled": Opt("bool"),
            "qty": Opt("int"),
            "fill_price": Opt("num"),
            "quote_price": Opt("num"),
            "est_pnl_usd": Opt("num"),
            "cause": Opt("str"),
            "stopped_at_check": Opt(checks),
            "reason_code": Ref("ReasonCode"),
            "reason_params": MAP,
            "reason_text": Opt("str"),
            "hint": Opt("str"),
        },
        "Results": {
            "real": Ref("Book"),
            "practice": Ref("Book"),
            "paper_account": Ref("Book"),
            "signal_cost": Ref("SignalCost"),
        },
        "Book": {
            "today": "num",
            "last_7_days": "num",
            "total": "num",
            "closed": "int",
            "won": "int",
            "lost": "int",
            "unmeasured": "int",
            "unmeasured_today": "int",
            "days": List(Ref("BookDay")),
        },
        "BookDay": {"day": "str", "pnl": "num", "closed": "int"},
        "SignalCost": {
            "applies": "bool",
            "days_used": "int",
            "days_estimated": "int",
            "sats_paid_today": Opt("int"),
            "sats_paid": Opt("int"),
            "usd_paid": Opt("num"),
        },
        "Notice": {
            "level": Enum("info", "warn", "urgent"),
            "code": "str",
            "text": "str",
        },
    }


# What the fields mean, for the generated TypeScript (and the reader).
_DOC = {
    "Status.as_of": "server time of this snapshot",
    "Status.mode": "what NEW trades use; pausing never changes it",
    "Status.mode_explicit": "false: the settings never said (dry_run is "
                            "missing), and the agent treats that as live",
    "Status.real_positions_managed": "are the real positions being sold on "
                                     "their exit signal / at expiry right now",
    "Status.unmanaged_reason": "why not, for the first real position that isn't",
    "Status.expiry_guard_on": "false: the expiry sell is switched off in the "
                              "settings, so nothing sells a position on its "
                              "expiry day but an exit signal",
    "Status.entry_checks": "the checks an entry signal passes, in the order "
                           "the agent runs them (agent.ENTRY_CHECKS)",
    "Agent.running": "false: this server has no agent loop behind it (setup, "
                     "or the agent was stopped)",
    "Agent.answering": "the loop's last heartbeat is as recent as its own "
                       "schedule says it should be",
    "Agent.settings_readable": "false: config.json can't be read right now; "
                               "the values shown are the ones the agent runs on",
    "Pause.kind": "entries = the owner's Pause (exits, expiry guard and broker "
                  "checks keep running); state_hold / handoff_hold / "
                  "restarting / stopped = no orders at all",
    "Pause.entries_paused": "the owner's Pause is saved, whatever `kind` says",
    "Pause.since": "null when it was not recorded",
    "Broker.connected": "null: this broker adapter doesn't report it",
    "Broker.since": "when the connection went down (only while it is down)",
    "Signals.reading": "false: the agent is deliberately not reading the feed "
                       "(a full hold, or paused with no real position open on "
                       "a pay-per-day feed)",
    "Signals.entry_access": "could the last read of the feed see entry "
                            "signals (false: no pass or day-pass, or it "
                            "ran out); null = not read yet",
    "Signals.last_signal": "when the newest signal the agent has seen happened",
    "Signals.last_signal_from": "feed = the feed's own time; activity_log = "
                                "when the agent last logged handling a signal",
    "Market.opens_at": "this session's open while the market is open, else "
                       "the next session's",
    "Market.closes_at": "the close of that same session (early closes included)",
    "Limits.cap": "new trades allowed per day",
    "Limits.used_today": "entries today, real and practice counted apart",
    "Limits.cap_reached": "for the mode the agent is in",
    "Limits.resets_at": "the limits count by UTC day",
    "Limits.max_daily_loss": "null = no daily loss limit",
    "Limits.realised_loss_today": "today's estimated closed-trade loss as a "
                                  "positive number (0 when in profit)",
    "Limits.open_positions": "the limit counts both together",
    "Cash.trades_affordable": "whole main-size trades the buying power "
                              "covers; null unless the size is a dollar budget",
    "Position.id": "the agent's ledger key",
    "Position.contract": "null when the ledger key can't be read as a contract",
    "Position.entry_price": "per-share quote when it was bought (brokers don't "
                            "report fill prices back)",
    "Position.account": "the agent's own tag for the broker account; never an "
                        "account number",
    "Position.book": "null for a position opened before this was recorded",
    "NextAction.at": "when, if the agent acts at a set time",
    "NextAction.expiry_guard_at": "when the expiry guard would sell it on its "
                                  "expiry day, if it is still open then",
    "ActivityPage.next": "pass as ?before= for the page after this one",
    "Activity.n": "line number in the trade log: a stable row id",
    "Activity.known": "false: a kind this version can't describe",
    "Activity.real": "which book the row belongs to; null = not recorded",
    "Activity.paper_account": "true: the order went to a broker's "
                              "paper-trading account (not real money); "
                              "null = not recorded",
    "Activity.mode_at_time": "the mode NEW trades used then; null = not recorded",
    "Activity.fill_price": "per share, only when the broker reported the fill",
    "Activity.quote_price": "per-share quote the estimate used",
    "Activity.stopped_at_check": "the entry check that stopped it",
    "Book.today": "estimated result of trades closed today (ET)",
    "Book.last_7_days": "the last 7 calendar days (ET), today included",
    "Book.unmeasured_today": "sales today (ET) with no estimate: not in "
                             "`today`, and not in `days`",
    "Results.real": "orders the agent sent to a real-money account (or one "
                    "not recorded as a paper-trading account)",
    "Results.paper_account": "orders filled in a broker's paper-trading "
                             "account (recorded on each line since this "
                             "version): not real money",
    "BookDay.day": "ET day the trades were closed",
    "SignalCost.applies": "false: this signal source has no per-day charge",
    "SignalCost.days_used": "signal days recorded as paid for",
    "SignalCost.days_estimated": "days from before that was recorded",
    "SignalCost.sats_paid": "null: the total paid is not recorded yet",
    "SignalCost.usd_paid": "null: the total paid is not recorded yet",
}

_TYPES = {}


def types():
    """The schema: {type name: {field: type}} (built once)."""
    if not _TYPES:
        _TYPES.update(_types())
    return _TYPES


# ── schema: validation ───────────────────────────────────────────────────────

_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def _is_num(v):
    return (isinstance(v, (int, float)) and not isinstance(v, bool)
            and math.isfinite(v))


def validate(value, type_="Status", path="$"):
    """Every way `value` differs from the declared type, as a list of
    'path: problem' strings ([] = it matches exactly: no missing key, no
    extra key, no wrong type)."""
    t = (Ref(type_) if isinstance(type_, str)
         and (type_ in types() or type_ in aliases()) else type_)
    out = []
    if isinstance(t, dict):
        if not isinstance(value, dict):
            return [f"{path}: expected an object"]
        for k in t:
            if k not in value:
                out.append(f"{path}.{k}: missing")
        for k in value:
            if k not in t:
                out.append(f"{path}.{k}: not in the schema")
        for k, sub in t.items():
            if k in value:
                out.extend(validate(value[k], sub, f"{path}.{k}"))
        return out
    if t == "str":
        return [] if isinstance(value, str) else [f"{path}: expected a string"]
    if t == "bool":
        return [] if isinstance(value, bool) else [f"{path}: expected a boolean"]
    if t == "int":
        ok = isinstance(value, int) and not isinstance(value, bool)
        return [] if ok else [f"{path}: expected a whole number"]
    if t == "num":
        return [] if _is_num(value) else [f"{path}: expected a finite number"]
    if t == "iso":
        ok = isinstance(value, str) and bool(_ISO_RE.match(value))
        return [] if ok else [f"{path}: expected a UTC time"]
    if t == MAP:
        if not isinstance(value, dict):
            return [f"{path}: expected an object"]
        return [f"{path}.{k}: expected a string or number"
                for k, v in value.items()
                if not isinstance(k, str)
                or not (isinstance(v, str) or _is_num(v))]
    tag = t[0]
    if tag == "opt":
        return [] if value is None else validate(value, t[1], path)
    if tag == "enum":
        return [] if value in t[1] and isinstance(value, str) else [
            f"{path}: {value!r} is not one of {list(t[1])}"]
    if tag == "lit":
        return [] if value == t[1] and type(value) is type(t[1]) else [
            f"{path}: expected {t[1]!r}"]
    if tag == "list":
        if not isinstance(value, list):
            return [f"{path}: expected a list"]
        for i, v in enumerate(value):
            out.extend(validate(v, t[1], f"{path}[{i}]"))
        return out
    if tag == "ref":
        named = types().get(t[1]) or aliases()[t[1]]
        return validate(value, named, path)
    return [f"{path}: unknown type {t!r}"]


# ── schema: TypeScript ───────────────────────────────────────────────────────

def _ts(t, indent="  "):
    if isinstance(t, dict):
        inner = "".join(f"{indent}  {k}: {_ts(v, indent + '  ')};\n"
                        for k, v in t.items())
        return "{\n" + inner + indent + "}"
    if t in ("str", "iso"):
        return "string" if t == "str" else "Iso"
    if t in ("num", "int"):
        return "number"
    if t == "bool":
        return "boolean"
    if t == MAP:
        return "Record<string, string | number>"
    tag = t[0]
    if tag == "opt":
        return _ts(t[1], indent) + " | null"
    if tag == "enum":
        return " | ".join(f'"{v}"' for v in t[1])
    if tag == "lit":
        return f'"{t[1]}"' if isinstance(t[1], str) else str(t[1])
    if tag == "list":
        inner = _ts(t[1], indent)
        return f"({inner})[]" if " | " in inner else f"{inner}[]"
    if tag == "ref":
        return t[1]
    raise ValueError(f"unknown type {t!r}")


def typescript():
    """The schema as the TypeScript file the React app compiles against."""
    import agent as A
    out = [
        "// GENERATED from view_model.py (types()) by `python view_model.py --ts`.",
        "// Do not edit: tests/test_view_model.py fails when this file and the",
        "// Python schema differ. GET /api/v2/status returns a Status;",
        "// GET /api/v2/activity?before=<n>&limit=<k> returns an ActivityPage.",
        "// Every field is always present: \"not known\" is null, never a",
        "// missing key.",
        "",
        f"export const SCHEMA_VERSION = {SCHEMA_VERSION};",
        "",
        "/** ISO-8601 UTC time, e.g. \"2026-11-09T19:41:07Z\". */",
        "export type Iso = string;",
        "",
        "/** The checks an entry signal passes, in the order the agent runs",
        " *  them, each with the Activity `action` kinds it stops a signal as. */",
        "export const ENTRY_CHECKS = [",
    ]
    for check, kinds in A.ENTRY_CHECKS:
        listed = ", ".join(f'"{k}"' for k in kinds)
        out.append(f'  {{ id: "{check}", kinds: [{listed}] }},')
    out += ["] as const;",
            "export type CheckId = (typeof ENTRY_CHECKS)[number][\"id\"];",
            "",
            "/** Every Activity `reason_code` this version can produce. */",
            "export const REASON_CODES = ["]
    out += [f'  "{c}",' for c in REASON_CODES]
    out += ["] as const;",
            "export type ReasonCode = (typeof REASON_CODES)[number];",
            ""]
    for name, fields in types().items():
        out.append(f"export interface {name} {{")
        for k, t in fields.items():
            doc = _DOC.get(f"{name}.{k}")
            if doc:
                out.append(f"  /** {doc} */")
            out.append(f"  {k}: {_ts(t)};")
        out += ["}", ""]
    return "\n".join(out)


# ── small, tolerant readers ──────────────────────────────────────────────────

def _text(v, limit=_TEXT):
    """A string cut to `limit`, or None."""
    if v is None or isinstance(v, (dict, list, tuple, set)):
        return None
    s = str(v)
    return s[:limit] if s else None


def _num(v):
    """A finite float, or None (bools and anything unreadable are None)."""
    if isinstance(v, bool) or v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError, OverflowError):  # an int of 309+ digits
        return None
    return f if math.isfinite(f) else None


# An estimate past this (a trillion dollars) is damage, not a trade: such a
# row counts as not measured, so a sum of estimates is always finite.
_PNL_MAX = 1e12


def _pnl(v):
    """A trade's estimated result as a finite float, or None."""
    f = _num(v)
    return f if f is not None and abs(f) < _PNL_MAX else None


def _int(v, default=None):
    f = _num(v)
    if f is None or abs(f) > 1e12:
        return default
    return int(f)


def _money(v):
    f = _num(v)
    return None if f is None else round(f, 2)


def _utc(v):
    """An aware UTC datetime from epoch seconds, an ISO string or a
    datetime; None when unreadable or absent."""
    try:
        if isinstance(v, datetime):
            d = v
        elif isinstance(v, bool) or v is None or v == "":
            return None
        elif isinstance(v, (int, float)):
            if not math.isfinite(v) or v <= 0:
                return None
            d = datetime.fromtimestamp(v, timezone.utc)
        else:
            # the agent's own reading of a timestamp: a "Z", or a broker's
            # 9-digit fraction, is fine on every supported Python
            import agent as A
            iso = A._utc_iso(str(v))
            if iso is None:
                return None
            d = datetime.fromisoformat(iso)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        d = d.astimezone(timezone.utc)
        # a stamp at the edge of the calendar can't be moved to market time
        return d if 1970 <= d.year <= 9000 else None
    except (ValueError, OverflowError, OSError, TypeError):
        return None


def _iso(v):
    """'2026-11-09T19:41:07Z' for anything _utc can read, else None."""
    d = _utc(v)
    if d is None:
        return None
    # by hand: strftime's %Y is not zero-padded on every platform
    return (f"{d.year:04d}-{d.month:02d}-{d.day:02d}"
            f"T{d.hour:02d}:{d.minute:02d}:{d.second:02d}Z")


_CONTRACT_RE = re.compile(
    r"^([A-Z][A-Z0-9.]{0,9}) (\d{4}-\d{2}-\d{2}) \$?(\d+(?:\.\d+)?) ([CP])$")


def _contract(symbol, expiry, strike, side):
    k = _num(strike)
    text = f"{symbol} {expiry} {'' if k is None else format(k, 'g')} {side}"
    if k is None or not 0 < k < 100_000 or not _CONTRACT_RE.match(text):
        return None
    return {"symbol": str(symbol), "expiry": str(expiry), "strike": k,
            "side": str(side)}


def contract_of_text(text):
    """A trade-log "contract" ("SPY 2026-11-13 $700.0 C") as a Contract, or
    None when it isn't exactly that."""
    m = _CONTRACT_RE.match(text) if isinstance(text, str) else None
    return _contract(*m.groups()) if m else None


def contract_of_key(key):
    """A ledger key ("SPY|2026-11-13|700.0|C") as a Contract, or None."""
    parts = key.split("|") if isinstance(key, str) else []
    return _contract(*parts) if len(parts) == 4 else None


def _facts(row):
    """What the agent recorded for the dashboard on this row ({} = an older
    row, or one damaged by hand)."""
    import agent as A
    ui = row.get(A.ROW_FACTS_KEY) if isinstance(row, dict) else None
    return ui if isinstance(ui, dict) else {}


def _row_time(row):
    """When a row's event happened: the broker's fill time for a close found
    in the account, else when the agent logged it (as agent._event_time)."""
    for k in ("closed_at", "ts"):
        d = _utc(row.get(k)) if isinstance(row.get(k), str) else None
        if d is not None:
            return d
    return None


# ── activity ─────────────────────────────────────────────────────────────────

def _known_kinds():
    import agent as A
    import broker_events
    kinds = set(_KIND_CODES) | set(broker_events.KINDS)
    for _check, logged in A.ENTRY_CHECKS:
        kinds.update(logged)
    return kinds


def _book_of(row, ui):
    b = ui.get("book") or row.get("book")
    if b == "main":
        return "main"
    return "other" if b in ("other", "shadow") else None


def _mode_at_time(row, ui, action):
    if ui.get("mode") in MODES:
        return ui["mode"]
    # Older rows. An entry was always made in the mode of its own book. A
    # row that isn't an exit and says "dry" was written in practice mode.
    # Everything else was not recorded: a practice-book exit can happen in
    # live mode (and a real one in practice), and most skip rows said nothing.
    if action == "entry":
        return "practice" if row.get("dry") else "live"
    if action != "exit" and row.get("dry") is True:
        return "practice"
    return None


def _reason_code(row, ui, action, known):
    code = ui.get("code")
    if isinstance(code, str) and code in REASON_CODES:
        return code
    if not known:
        return "unknown"
    if action == "exit":
        if row.get("manual"):
            return "closed_outside"
        if row.get("cause") in ("expiry_guard", "exit_remainder"):
            return row["cause"]
    return _KIND_CODES.get(action, "unknown")


def _reason_params(row, ui, action):
    import agent as A
    out = {}
    params = ui.get("params")
    for k, v in (params.items() if isinstance(params, dict) else ()):
        if len(out) >= 12:
            break
        if isinstance(v, str):
            out[str(k)[:40]] = v[:_NAME]
        elif _num(v) is not None:
            out[str(k)[:40]] = v
    if action == "skip_budget" and "cost_usd" not in out:
        # the one number the agent's own skip sentence carries (the same
        # reading agent._budget_hints makes of it)
        m = A._COST_RE.search(str(row.get("reason", ""))[:_TEXT * 4])
        cost = _num(m.group(1).replace(",", "")) if m else None
        if cost is not None:
            out["cost_usd"] = cost
    return out


# kinds agent.handle_event logs for an entry signal only (never for an exit)
_ENTRY_ONLY_KINDS = ("skip_stale", "skip_paused", "skip_daily_cap",
                     "skip_position_cap", "skip_daily_loss", "policy_veto",
                     "skip_ai_needed", "skip_sizing", "skip_budget",
                     "entry_unfilled", "broker_no_order")


def activity_row(row):
    """One trade-log row (with its line number `n`) as an Activity."""
    import agent as A
    ui = _facts(row)
    action = _text(row.get("action"), _NAME) or ""
    known = action in _known_kinds()
    is_trade = action in ("entry", "exit")
    mode = _mode_at_time(row, ui, action)
    text = row.get("contract")

    real = None
    if is_trade or action == "ledger_adopt":
        real = not row.get("dry")
    elif mode is not None:
        real = mode == "live"

    sent = ui.get("sent") if isinstance(ui.get("sent"), bool) else None
    filled = ui.get("filled") if isinstance(ui.get("filled"), bool) else None
    paper = ui.get("paper") if isinstance(ui.get("paper"), bool) else None
    if sent is None and known:
        if is_trade:
            # a real entry or exit row is only ever logged after its order
            # went to the broker; a close found in the account was the
            # owner's (or the broker's) order, not one the agent sent
            sent = not row.get("dry") and not row.get("manual")
        elif action.startswith("skip_") or action in ("policy_veto",
                                                      "ledger_adopt"):
            sent = False
    if filled is None and known:
        if sent is False and not row.get("manual"):
            filled = False
        elif action == "exit" and (_int(row.get("qty"), 0) or 0) >= 1:
            filled = True

    check = ui.get("check")
    if check not in check_ids():
        # a row from before the check was recorded: told from its kind only
        # when the agent logs that kind for entries alone, at one check
        check = (A._check_of_kind(action)
                 if not ui and action in _ENTRY_ONLY_KINDS else None)
    qty = _int(row.get("qty"))
    if qty is None:
        qty = _int(ui.get("qty"))
    pnl = row.get("est_pnl_usd")
    return {
        "n": _int(row.get("n"), 0) or 0,
        "at": _iso(_row_time(row)),
        "action": action,
        "known": known,
        "contract": contract_of_text(text),
        "contract_text": _text(text, _NAME),
        "book": _book_of(row, ui),
        "real": real,
        "paper_account": paper if real is not False else None,
        "mode_at_time": mode,
        "order_sent": sent,
        "filled": filled,
        "qty": qty if qty is not None and qty >= 0 else None,
        "fill_price": _num(row.get("exit_price")),
        "quote_price": _num(ui.get("mark")),
        "est_pnl_usd": _money(pnl),
        "cause": _text(row.get("cause"), _NAME),
        "stopped_at_check": check,
        "reason_code": _reason_code(row, ui, action, known),
        "reason_params": _reason_params(row, ui, action),
        "reason_text": _text(row.get("reason")),
        "hint": _text(row.get("hint")),
    }


def _clean_rows(rows):
    """The rows that are rows: dicts with a line number."""
    rows = rows if isinstance(rows, (list, tuple)) else ()
    return [r for r in rows if isinstance(r, dict)
            and isinstance(r.get("n"), int) and not isinstance(r.get("n"), bool)]


def activity_page(rows, before=None, limit=ACTIVITY_LIMIT, _checked=False):
    """One page of the trade log, newest first. `rows`: every row, oldest
    first, each with its line number `n` (agent._read_trade_log). `before`:
    only rows with a smaller `n`. Same paging contract as agent.trade_page."""
    limit = max(1, min(_int(limit, ACTIVITY_LIMIT) or ACTIVITY_LIMIT,
                       ACTIVITY_LIMIT_MAX))
    rows = rows if _checked else _clean_rows(rows)
    oldest = rows[0]["n"] if rows else None
    cut = _int(before)
    if cut is not None:
        rows = [r for r in rows if r["n"] < cut]
    page = rows[-limit:]
    return {"rows": [activity_row(r) for r in reversed(page)],
            "total": len(rows),
            "next": page[0]["n"] if page and len(rows) > len(page) else None,
            "oldest": oldest}


# ── results ──────────────────────────────────────────────────────────────────

def _et_day(d):
    import agent as A
    return d.astimezone(A.MARKET_TZ).date().isoformat()


@functools.lru_cache(maxsize=65536)
def _stamp_day(stamp):
    """The market (ET) day of a timestamp string, '' when unreadable. Kept:
    the dashboard asks every few seconds and a log's stamps don't change."""
    d = _utc(stamp)
    return _et_day(d) if d is not None else ""


def _row_day(row):
    """The market day a row's event happened on (as _row_time), or ''."""
    for k in ("closed_at", "ts"):
        v = row.get(k)
        day = _stamp_day(v) if isinstance(v, str) else ""
        if day:
            return day
    return ""


def _book_key(t):
    """Which Results book an exit row belongs to: practice (its "dry"), a
    broker's paper-trading account (recorded on the row), or real."""
    if t.get("dry"):
        return "practice"
    return "paper_account" if _facts(t).get("paper") is True else "real"


def _results(rows, now):
    """Each book's estimated closed-trade results, in one pass over the log.
    Real and practice are never added together: a row belongs to the book
    its "dry" says, and a sale in a broker's paper-trading account (as the
    row recorded it) is never counted as real money."""
    import agent as A
    today = _et_day(now)
    week_from = (now.astimezone(A.MARKET_TZ).date()
                 - timedelta(days=6)).isoformat()
    books = {k: {"today": 0.0, "last_7_days": 0.0, "total": 0.0,
                 "closed": 0, "won": 0, "lost": 0, "unmeasured": 0,
                 "unmeasured_today": 0, "days": {}}
             for k in ("real", "practice", "paper_account")}
    for t in rows:
        if t.get("action") != "exit":
            continue
        b = books[_book_key(t)]
        v = _pnl(t.get("est_pnl_usd"))
        if v is None:
            if not A._counted_before(t):
                b["unmeasured"] += 1
                # a sale today that has no estimate is still a sale today:
                # never shown as "nothing closed"
                b["unmeasured_today"] += _row_day(t) == today
            continue
        day = _row_day(t)
        b["total"] += v
        b["closed"] += 1
        b["won"] += v > 0
        b["lost"] += v < 0
        if day == today:
            b["today"] += v
        if week_from <= day <= today:
            b["last_7_days"] += v
        if day:
            d = b["days"].setdefault(day, {"day": day, "pnl": 0.0, "closed": 0})
            d["pnl"] += v
            d["closed"] += 1
    for b in books.values():
        for k in ("today", "last_7_days", "total"):
            b[k] = round(b[k], 2)
        b["days"] = sorted(b["days"].values(), key=lambda d: d["day"],
                           reverse=True)
        for d in b["days"]:
            d["pnl"] = round(d["pnl"], 2)
    return books


def _signal_cost(cfg, state, rows, now):
    import agent as A
    applies = A._feed_paid_per_day(cfg)
    used = estimated = 0
    if applies:
        # the same count the classic P&L panel shows (agent.pnl_summary)
        paid = A._paid_days(state)
        recorded = A._recorded_paid_days(state)
        tracked_from = min(recorded) if recorded else None
        entry_days = {_stamp_day(t["ts"]) for t in rows
                      if t.get("action") == "entry"
                      and isinstance(t.get("ts"), str)} - {""}
        used = len(paid)
        estimated = len({d for d in entry_days
                         if tracked_from is None or d < tracked_from} - paid)
    spent = state.get("autopay_spent")
    sats_today = None
    if applies and isinstance(spent, dict):
        sats_today = (_int(spent.get("sats"), 0)
                      if spent.get("day") == _et_day(now) else 0)
    return {"applies": applies, "days_used": used, "days_estimated": estimated,
            "sats_paid_today": sats_today, "sats_paid": None, "usd_paid": None}


# ── limits ───────────────────────────────────────────────────────────────────

def _cfg_int(cfg, key, default):
    v = _int(cfg.get(key, default))
    return default if v is None else v


def _today_counts(rows, now):
    """Today's entries and estimated closed-trade P&L per book, counted the
    way the limits count them (agent._entries_today / _est_pnl_today): by
    UTC day; a close found in the account on the day its sale filled."""
    import agent as A
    today = now.astimezone(timezone.utc).date().isoformat()
    out = {"entries": {"real": 0, "practice": 0},
           "pnl": {"real": 0.0, "practice": 0.0},
           "unmeasured": {"real": 0, "practice": 0}}
    for t in rows:
        book = "practice" if t.get("dry") else "real"
        a = t.get("action")
        if a == "entry" and str(t.get("ts", "")).startswith(today):
            out["entries"][book] += 1
        elif a == "exit" and str(t.get("closed_at")
                                 or t.get("ts", "")).startswith(today):
            v = _pnl(t.get("est_pnl_usd"))
            if v is not None:
                out["pnl"][book] += v
            elif not A._counted_before(t):
                out["unmeasured"][book] += 1
    return out


def _trade_size(cfg, prefix=""):
    import agent as A
    own = str(cfg.get(f"{prefix}sizing_mode") or "")
    same = bool(prefix) and own not in A.SIZING_MODES
    if same:
        prefix = ""  # no "other trades" size of its own: the main one applies
    mode = str(cfg.get(f"{prefix}sizing_mode") or "contracts")
    mode = mode if mode in A.SIZING_MODES else "contracts"
    usd = _num(cfg.get(f"{prefix}budget_per_trade_usd"))
    pct = _num(cfg.get(f"{prefix}percent_per_trade"))
    n = _int(cfg.get(f"{prefix}contracts_per_trade"))
    return {"mode": mode,
            "usd": round(usd, 2) if mode == "budget" and usd is not None else None,
            "contracts": (max(1, n if n is not None else 1)
                          if mode == "contracts" else None),
            "percent": pct if mode == "percent" else None,
            "max_contracts": _int(cfg.get(f"{prefix}max_contracts_per_trade")
                                  if cfg.get(f"{prefix}max_contracts_per_trade")
                                  is not None
                                  else cfg.get("max_contracts_per_trade")),
            "same_as_main": same}


def _cash(cfg, bp):
    """Whether the buying power covers trades — the conditions of
    agent._cash_warning, as data."""
    import agent as A
    budget = (_num(cfg.get("budget_per_trade_usd"))
              if str(cfg.get("sizing_mode")) == "budget" else None)
    reason = None
    if bp is not None:
        if bp < A.LOW_CASH_USD:
            reason = "under_minimum"
        elif budget is not None and budget > bp:
            reason = "under_trade_size"
    try:
        afford = (int(bp // budget) if bp is not None and budget and budget > 0
                  and bp >= 0 else None)
    except (OverflowError, ValueError):  # a hand-edited, absurd trade size
        afford = None
    return {"low": reason is not None, "low_reason": reason,
            "trades_affordable": afford}


def _limits(cfg, state, rows, mode, bp, now):
    import agent as A
    counts = _today_counts(rows, now)
    book = "practice" if mode == "practice" else "real"
    cap = _cfg_int(cfg, "max_entries_per_day", 5)
    pos_cap = _cfg_int(cfg, "max_open_positions", 10)
    loss_cap = _num(cfg.get("max_daily_loss_usd")) or 0.0
    positions = state.get("positions")
    positions = positions if isinstance(positions, dict) else {}
    n_real = sum(1 for r in positions.values() if A._opened_live(r))
    midnight = (now.astimezone(timezone.utc)
                .replace(hour=0, minute=0, second=0, microsecond=0)
                + timedelta(days=1))
    pnl = {k: round(v, 2) for k, v in counts["pnl"].items()}
    return {
        "cap": max(0, cap),
        "used_today": counts["entries"],
        "cap_reached": counts["entries"][book] >= cap,
        "resets_at": _iso(midnight),
        "max_daily_loss": round(loss_cap, 2) if loss_cap > 0 else None,
        "realised_pnl_today": pnl,
        "realised_loss_today": {k: max(0.0, -v) for k, v in pnl.items()},
        "unmeasured_exits_today": counts["unmeasured"],
        "loss_limit_reached": loss_cap > 0 and -counts["pnl"][book] >= loss_cap,
        "max_positions": max(0, pos_cap),
        "open_positions": {"real": n_real,
                           "practice": len(positions) - n_real},
        "positions_reached": len(positions) >= pos_cap,
        "trade_size": {"main": _trade_size(cfg),
                       "other": _trade_size(cfg, "other_")},
        "cash": _cash(cfg, bp),
    }


# ── market ───────────────────────────────────────────────────────────────────

def _et_at(day, minute):
    """`minute` after midnight ET on date `day`, as an aware datetime."""
    import agent as A
    return datetime(day.year, day.month, day.day, minute // 60, minute % 60,
                    tzinfo=A.MARKET_TZ)


def _market(cfg, now):
    """Today's session, or the next one — from the calendar the loop uses
    (brokers/market_hours.py through agent._trading_now / _close_min)."""
    import agent as A
    from brokers import market_hours as mh
    now_et = now.astimezone(A.MARKET_TZ)
    day = now_et.date()
    is_open = A._trading_now(cfg, now_et)
    trading_day = mh.is_trading_day(cfg, day)
    if not is_open:
        past_open = (trading_day
                     and now_et.hour * 60 + now_et.minute >= A._OPEN_MIN)
        if past_open or not trading_day:
            for _ in range(15):  # the longest closure is a few days
                day = day + timedelta(days=1)
                if mh.is_trading_day(cfg, day):
                    break
    close = A._close_min(cfg, day.isoformat())
    return {"open": is_open, "trading_day": trading_day,
            "opens_at": _iso(_et_at(day, A._OPEN_MIN)),
            "closes_at": _iso(_et_at(day, close)),
            "early_close": close < mh.REGULAR_CLOSE_MIN,
            "timezone": "America/New_York"}


# ── positions ────────────────────────────────────────────────────────────────

def _opened(rows):
    """{(dry, contract text): the latest entry row} — how the P&L pairs a
    position with the entry that opened it (agent.pnl_summary)."""
    out = {}
    for t in rows:
        # a damaged row's "contract" can be a list or an object: not a key
        if t.get("action") == "entry" and isinstance(t.get("contract"), str):
            out[(bool(t.get("dry")), t["contract"])] = t
    return out


def _next_action(key, row, c):
    """What the agent will do with one ledger row and when, from the rules
    the loop itself runs: the expiry guard's times (agent._expiry_times), the
    states that hold its sell back (agent._guard_hold_reason), what a full
    hold and a paused feed mean (agent.cmd_run, agent._pause_skips_feed)."""
    import agent as A
    from brokers import market_hours as mh
    cfg, now_et = c["cfg"], c["now_et"]
    real = A._opened_live(row)
    parts = key.split("|")
    expiry = parts[1] if len(parts) == 4 and A._EXPIRY_RE.match(parts[1]) else None
    guard_at = None
    guard = close = None
    if expiry is not None:
        try:
            exp_day = datetime.strptime(expiry, "%Y-%m-%d").date()
            guard, _alert, close = A._expiry_times(cfg, expiry)
            if guard is not None and mh.is_trading_day(cfg, exp_day):
                guard_at = _iso(_et_at(exp_day, guard))
        except ValueError:
            expiry = None

    def out(action, reason, at=None):
        return {"action": action, "at": at, "reason_code": reason,
                "expiry_guard_at": guard_at}

    if not c["running"]:
        return out("unmanaged", "agent_stopped")
    if not c["answering"]:
        return out("unknown", "agent_not_answering")
    if c["hold"]:
        return out("unmanaged", c["hold"])
    today = now_et.date().isoformat()
    now_m = now_et.hour * 60 + now_et.minute
    if expiry is None:
        # the agent can't turn this row back into a contract: the expiry
        # guard won't sell it (agent._guard_event)
        return out("unknown", "exit_signal")
    if expiry < today or (expiry == today and now_m >= close):
        return out("unmanaged", "expired")
    guard_today = (expiry == today and guard_at is not None)
    if not real:
        if A._pause_skips_feed(cfg, c["state"]):
            return out("unmanaged", "pause_feed_not_read")
        if guard_today:
            return out("paper_close", "expiry_guard", guard_at)
        return out("paper_close", "exit_signal")
    if row.get("acct") and c["acct"] and row["acct"] != c["acct"]:
        return out("unmanaged", "other_account")
    if A._dry_run_holds_real_sells(cfg):
        return out("unmanaged", "practice_holds_real_sells")
    if c["broker_down"]:
        return out("unmanaged", "broker_down")
    pending = row.get("pending")
    if isinstance(pending, dict) and pending.get("side") == "sell":
        return out("selling_now", "sell_working")
    if guard_today and now_m >= guard:
        if row.get("expiry_no_bid") == today:
            # the guard found no bid and deliberately did not sell it; it
            # looks again each loop (agent._expiry_guard_sell)
            return out("unmanaged", "no_bid")
        return out("selling_now", "expiry_guard")
    if row.get("exiting"):
        return out("selling_now", "exit_remainder")
    if c.get("exits"):
        # no exit signal can reach the agent right now: only the expiry
        # sell (expiry_guard_at) still acts on it
        return out("unknown" if c["exits"] == "feed_unreachable"
                   else "unmanaged", c["exits"])
    if guard_today:
        return out("sell_at_expiry_guard", "expiry_guard", guard_at)
    return out("sell_on_exit", "exit_signal")


def _position(key, row, opened, c):
    import agent as A
    real = A._opened_live(row)
    contract = contract_of_key(key)
    entry = opened.get((not real, A._contract_of(key))
                       if contract is not None else None) or {}
    ui = _facts(entry)
    qty = max(0, _int(row.get("qty"), 0) or 0)
    price = _num(row.get("entry_mark"))
    price = price if price is not None and 0 < price < _PNL_MAX else None
    cost = price * qty * 100 if price is not None and qty else None
    pending = row.get("pending")
    side = pending.get("side") if isinstance(pending, dict) else None
    return {
        "id": _text(key, _NAME) or "",
        "contract": contract,
        "label": (_text(A._contract_of(key), _NAME) if contract is not None
                  else _text(key, _NAME)) or "",
        "qty": qty,
        "entry_price": price,
        "entry_cost_usd": (round(cost, 2) if cost is not None
                           and math.isfinite(cost) else None),
        "opened_at": (_iso(row.get("opened_at")) if _num(row.get("opened_at"))
                      else _iso(entry.get("ts"))),
        "real": real,
        "account": _text(row.get("acct"), _NAME) if real else None,
        "book": ui.get("book") if ui.get("book") in ("main", "other") else None,
        "expires_today": (contract is not None and contract["expiry"]
                          == c["now_et"].date().isoformat()),
        "pending": side if side in ("buy", "sell") else None,
        "next": _next_action(key, row, c),
    }


# ── the status ───────────────────────────────────────────────────────────────

_BROKER_NAMES = {"robinhood": "Robinhood", "alpaca": "Alpaca",
                 "moomoo": "moomoo"}


def _broker(cfg, rt):
    b = rt.get("broker") if isinstance(rt.get("broker"), dict) else {}
    bid = _text(b.get("id") or cfg.get("broker") or "robinhood", 40) or ""
    tag = _text(b.get("account_tag"), _NAME)
    if tag is None:
        try:
            from brokers import account_tag
            tag = account_tag({**cfg, "broker": bid})
        except Exception:
            tag = ""
    acct = cfg.get("robinhood_account") if bid == "robinhood" else None
    digits = "".join(ch for ch in str(acct or "") if ch.isalnum())[-4:]
    connected = b.get("connected") if isinstance(b.get("connected"), bool) else None
    err = _text(b.get("last_error"))
    return {
        "id": bid,
        "name": _BROKER_NAMES.get(bid, bid),
        "account_mask": digits or None,
        "paper_account": tag.endswith(":paper"),
        "connected": connected,
        "since": _iso(b.get("down_since")) if connected is False else None,
        "last_answer": _iso(b.get("last_ok_at")),
        "last_error": err if connected is False else None,
        "buying_power": _money(b.get("buying_power")),
        "buying_power_at": (_iso(b.get("buying_power_at"))
                            if _num(b.get("buying_power")) is not None else None),
    }


def _pass(cfg, state, now):
    """What signal access the agent has, as data — the cases of
    sources.agenthc_day_trade_ideas.pass_summary."""
    none = {"kind": "none", "status": "none", "days_left": None,
            "auto_pay": False}
    if cfg.get("source", "agenthc") != "agenthc":
        return {**none, "kind": "not_needed", "status": "active"}
    if cfg.get("agenthc_api_key"):
        return {**none, "kind": "subscription", "status": "active"}
    try:
        from sources import agenthc_day_trade_ideas as hc
        tokens = hc._pass_tokens(cfg)
        cache = state.get("pass_status")
        cache = cache if isinstance(cache, dict) else {}
        best = None
        for token in tokens:
            rec = cache.get(hc._token_id(token))
            rec = rec if isinstance(rec, dict) else {}
            if rec.get("status") in hc._USABLE and (
                    best is None or (rec.get("status") == "active"
                                     and best.get("status") != "active")):
                best = rec
    except Exception:
        return none
    auto = bool(cfg.get("day_pass_recurring"))
    if best:
        left = _int(best.get("days_left"))
        return {"kind": "prepaid", "status": best["status"],
                "days_left": left if left is not None and left >= 0 else None,
                "auto_pay": auto}
    if tokens:
        return {"kind": "prepaid", "status": "waiting", "days_left": None,
                "auto_pay": auto}
    if cfg.get("lnbits_url"):
        l402 = state.get("l402")
        until = _num(l402.get("expires_at")) if isinstance(l402, dict) else None
        live = until is not None and until > now.timestamp()
        # no day-pass in hand: with auto-pay on, the agent buys one on its
        # next read of the feed on a market day
        return {"kind": "day_pass",
                "status": "active" if live else "waiting" if auto else "none",
                "days_left": None, "auto_pay": auto}
    return none


# event ids the agent makes up itself (the expiry guard, a close it found in
# the account, a re-sold remainder, the ledger check): not signals
_OWN_EVENT_IDS = ("expiry-guard|", "outside|", "ledger|", "exit-rest|")


def _last_signal(rows, rt):
    at = _iso(rt.get("signal_at"))
    if at:
        return at, "feed"
    for t in reversed(rows[-500:]):
        eid = t.get("event_id")
        if (isinstance(eid, str) and eid and not eid.startswith(_OWN_EVENT_IDS)
                and not t.get("manual") and not t.get("cause")):
            when = _iso(t.get("ts")) if isinstance(t.get("ts"), str) else None
            if when:
                return when, "activity_log"
    return None, None


def _exits_cut_off(state, signals, market_open, now):
    """Why no exit signal can reach the agent right now — "no_exit_signals"
    (no usable pass: used up past its exits-only days, expired, waiting for
    payment with no wallet auto-pay, or a paid day-pass AgentHC refused) or
    "feed_unreachable" (its last read of the feed failed) — else None. The
    cases where sources.agenthc_day_trade_ideas.poll returns no events
    without raising."""
    if not signals["reading"]:
        return None  # not reading at all: the hold / pause says why
    p = signals["pass"]
    # a last read that could see entry signals saw the exits too
    if p["kind"] in ("prepaid", "day_pass", "none") \
            and signals["entry_access"] is not True:
        l402 = state.get("l402")
        l402 = l402 if isinstance(l402, dict) else {}
        until = _num(l402.get("expires_at"))
        refused = (_num(l402.get("refused_at")) is not None
                   and until is not None and until > now.timestamp())
        if (refused or p["status"] == "none"
                or (p["status"] == "waiting"
                    and (not p["auto_pay"]
                         # auto-pay buys on a market day: open, and the last
                         # read still had no access
                         or (market_open and signals["entry_access"] is False)))):
            return "no_exit_signals"
    if signals["last_poll_ok"] is False:
        return "feed_unreachable"
    return None


def _answer(state, reason):
    return {"state": state, "reason_code": reason}


def _capabilities(c, limits, market, signals, positions):
    """Opening new trades / closing open ones / listening for signals: can
    the agent do each right now, and the one reason when it can't."""
    stopped = (not c["running"] and _answer("cant", "agent_stopped")) or (
        not c["answering"] and _answer("unknown", "agent_not_answering")) or (
        c["hold"] and _answer("paused", c["hold"])) or None
    practice = c["mode"] == "practice"
    real_open = [p for p in positions if p["real"]]
    p = signals["pass"]
    access = signals["entry_access"]
    if access is None:  # not read yet: what the settings say it will have
        access = (p["status"] == "active"
                  or (p["kind"] == "day_pass" and p["auto_pay"]))
    exits_readable = access or p["status"] == "exits_only"
    # no session today, or today's is over (before the open it is "yes":
    # nothing stops an entry, the signals just haven't started)
    session_over = not market["open"] and (
        not market["trading_day"] or c["past_close"])

    if stopped:
        opening = stopped
    elif c["entries_paused"]:
        opening = _answer("paused", "owner_pause")
    elif not access:
        opening = _answer("cant", "no_signal_access")
    elif c["ai_blocked"]:
        opening = _answer("cant", "ai_needed")
    elif c["broker_down"] and not practice:
        opening = _answer("cant", "broker_down")
    elif limits["cap_reached"]:
        opening = _answer("not_today", "daily_cap_reached")
    elif limits["loss_limit_reached"]:
        opening = _answer("not_today", "daily_loss_reached")
    elif limits["positions_reached"]:
        opening = _answer("cant", "position_cap_reached")
    elif session_over:
        opening = _answer("not_today", "market_closed")
    elif practice:
        opening = _answer("practice_only", "practice_mode")
    else:
        opening = _answer("yes", "ok")

    if stopped:
        closing = stopped
    elif not positions:
        closing = _answer("yes", "nothing_open")
    elif real_open and c["holds_real_sells"]:
        closing = _answer("cant", "practice_holds_real_sells")
    elif real_open and c["broker_down"]:
        closing = _answer("cant", "broker_down")
    elif real_open and c["exits"] == "no_exit_signals":
        closing = _answer("cant", "no_signal_access")
    elif real_open and c["exits"] == "feed_unreachable":
        closing = _answer("cant", "feed_unreachable")
    elif not real_open and not signals["reading"]:
        closing = _answer("paused", "pause_feed_not_read")
    elif not real_open:
        closing = _answer("practice_only", "only_practice_positions")
    else:
        closing = _answer("yes", "ok")

    if stopped:
        listening = stopped
    elif not signals["reading"]:
        listening = _answer("paused", "pause_feed_not_read")
    elif not exits_readable or c["exits"] == "no_exit_signals":
        listening = _answer("cant", "no_signal_access")
    elif signals["last_poll_ok"] is False:
        listening = _answer("cant", "feed_unreachable")
    else:
        listening = _answer("yes", "ok")
    return {"opening": opening, "closing": closing, "listening": listening}


def _pass_notice(cfg, state):
    """The signal-pass message the agent saved for the owner (a pass that
    ran out, a day-pass AgentHC refused…), shown when the classic page
    shows it (agent._feed_access: not while a pass has days left)."""
    text = _text(state.get("pass_notice"))
    if not text:
        return None
    try:
        from sources import agenthc_day_trade_ideas as hc
        if "left" in hc.pass_summary(cfg, state):
            return None
    except Exception:
        pass
    return {"level": "warn", "code": "pass", "text": text}


def _notices(rt, cfg=None, state=None):
    out = []
    raw = rt.get("notices")
    for n in (raw if isinstance(raw, (list, tuple)) else ())[:50]:
        if not isinstance(n, dict) or not _text(n.get("text")):
            continue
        level = n.get("level") if n.get("level") in ("info", "warn", "urgent") \
            else "info"
        out.append({"level": level, "code": _text(n.get("code"), 40) or "note",
                    "text": _text(n.get("text"))})
    note = _pass_notice(cfg or {}, state or {})
    if note and not any(n["text"] == note["text"] for n in out):
        out.append(note)
    return out


def _guard_on(cfg, now_et):
    """Whether the expiry sell is switched on (agent._expiry_times)."""
    import agent as A
    try:
        return A._expiry_times(cfg, now_et.date().isoformat())[0] is not None
    except Exception:
        return False  # can't tell: never promise a sale


def build_status(cfg, state, rows, runtime=None, now=None,
                 activity_limit=ACTIVITY_LIMIT):
    """The whole dashboard state as one schema-2 dict.

    cfg      the settings (config.json as a dict)
    state    the agent's memory (state.json as a dict)
    rows     every trade-log row, oldest first, each with its line number
             `n` (agent._read_trade_log)
    runtime  what only the running agent knows (all optional; a missing
             fact is reported as unknown):
               running          False: no agent loop behind this server
               stopping         the owner pressed Stop
               started_at, heartbeat_at, wait_s      epoch seconds
               hold             None | "state_hold" | "handoff_hold" |
                                "restarting" (agent.cmd_run's `held`)
               hold_since, hold_recorded
               poll_at, poll_ok, signal_at, feed_access
               settings_readable
               ai_blocked       llm_policy.entries_blocked(cfg)
               broker           {id, account_tag, connected, down_since,
                                 last_ok_at, last_error, buying_power,
                                 buying_power_at}
               notices          [{level, code, text}]
    now      an aware datetime (default: the clock)
    """
    import agent as A
    cfg = cfg if isinstance(cfg, dict) else {}
    state = state if isinstance(state, dict) else {}
    rt = runtime if isinstance(runtime, dict) else {}
    now = _utc(now) or datetime.now(timezone.utc)
    rows = _clean_rows(rows)
    positions = state.get("positions")
    positions = positions if isinstance(positions, dict) else {}
    state = {**state, "positions": positions}

    running = rt.get("running") is not False and bool(rt) and not rt.get("stopping")
    beat = _utc(rt.get("heartbeat_at"))
    wait = _num(rt.get("wait_s"))
    # late by more than the loop's own "that was a gap" threshold
    answering = bool(running and beat is not None and (
        (now - beat).total_seconds() - (wait or 0.0) <= A.EXPIRY_WAKE_GAP_S))

    mode = A._mode(cfg)
    hold = rt.get("hold") if rt.get("hold") in (
        "state_hold", "handoff_hold", "restarting") else None
    entries_paused = A._paused(cfg)
    if not running:
        kind = "stopped"
    elif hold:
        kind = hold
    else:
        kind = "entries" if entries_paused else "none"
    pause = {
        "kind": kind,
        "scope": {"none": "none", "entries": "entries"}.get(kind, "all"),
        "entries_paused": entries_paused,
        "since": _iso(rt.get("hold_since")) if kind == "state_hold" else None,
        "survives_restart": (kind in ("entries", "stopped")
                             or (kind == "state_hold"
                                 and rt.get("hold_recorded") is not False)),
        "can_resume": kind in ("entries", "state_hold"),
    }

    broker = _broker(cfg, rt)
    c = {"cfg": cfg, "state": state, "now_et": now.astimezone(A.MARKET_TZ),
         "running": running, "answering": answering, "hold": hold,
         "mode": mode, "entries_paused": entries_paused,
         "acct": _text((rt.get("broker") or {}).get("account_tag")
                       if isinstance(rt.get("broker"), dict) else None, _NAME),
         "broker_down": broker["connected"] is False,
         "holds_real_sells": A._dry_run_holds_real_sells(cfg),
         "past_close": False,
         "exits": None,
         "ai_blocked": bool(rt.get("ai_blocked"))}

    feed_skipped = A._pause_skips_feed(cfg, state)
    last_signal, signal_from = _last_signal(rows, rt)
    signals = {
        "source": _text(cfg.get("source") or "agenthc", 40) or "",
        "reading": bool(running and not hold and not feed_skipped),
        "entry_access": (rt.get("feed_access")
                         if isinstance(rt.get("feed_access"), bool) else None),
        "last_poll": _iso(rt.get("poll_at")),
        "last_poll_ok": (rt.get("poll_ok")
                         if isinstance(rt.get("poll_ok"), bool) else None),
        "last_signal": last_signal,
        "last_signal_from": signal_from,
        "pass": _pass(cfg, state, now),
    }
    market = _market(cfg, now)
    c["past_close"] = (c["now_et"].hour * 60 + c["now_et"].minute
                       >= A._close_min(cfg, c["now_et"].date().isoformat()))
    c["exits"] = _exits_cut_off(state, signals, market["open"], now)

    opened = _opened(rows)
    pos = [_position(str(k), r, opened, c)
           for k, r in sorted(positions.items(), key=lambda kv: str(kv[0]))
           if isinstance(r, dict)]
    unmanaged = [p for p in pos if p["real"]
                 and p["next"]["action"] in ("unmanaged", "unknown")]
    why = unmanaged[0]["next"]["reason_code"] if unmanaged else None
    limits = _limits(cfg, state, rows, mode, broker["buying_power"], now)
    return {
        "schema": SCHEMA_VERSION,
        "as_of": _iso(now),
        "agent": {
            "running": running,
            "answering": answering,
            "last_heartbeat": _iso(beat),
            "heartbeat_every_s": wait,
            "started_at": _iso(rt.get("started_at")),
            "settings_readable": rt.get("settings_readable") is not False,
        },
        "mode": mode,
        "mode_explicit": isinstance(cfg.get("dry_run"), bool),
        "pause": pause,
        "real_positions": sum(1 for p in pos if p["real"]),
        "real_positions_managed": not unmanaged,
        "unmanaged_reason": why if why in UNMANAGED_REASONS else None,
        "expiry_guard_on": _guard_on(cfg, c["now_et"]),
        "broker": broker,
        "signals": signals,
        "market": market,
        "limits": limits,
        "capabilities": _capabilities(c, limits, market, signals, pos),
        "entry_checks": list(check_ids()),
        "positions": pos,
        "activity": activity_page(rows, limit=activity_limit, _checked=True),
        "results": {**_results(rows, now),
                    "signal_cost": _signal_cost(cfg, state, rows, now)},
        "notices": _notices(rt, cfg, state),
    }


if __name__ == "__main__":
    import os
    import sys
    if "--ts" in sys.argv:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            *TS_PATH.split("/"))
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(typescript())
        print(f"wrote {TS_PATH}")
    else:
        print(__doc__)
