// GENERATED from view_model.py (types()) by `python view_model.py --ts`.
// Do not edit: tests/test_view_model.py fails when this file and the
// Python schema differ. GET /api/v2/status returns a Status;
// GET /api/v2/activity?before=<n>&limit=<k> returns an ActivityPage.
// Every field is always present: "not known" is null, never a
// missing key.

export const SCHEMA_VERSION = 2;

/** ISO-8601 UTC time, e.g. "2026-11-09T19:41:07Z". */
export type Iso = string;

/** The checks an entry signal passes, in the order the agent runs
 *  them, each with the Activity `action` kinds it stops a signal as. */
export const ENTRY_CHECKS = [
  { id: "signal_fresh", kinds: ["skip_stale"] },
  { id: "not_paused", kinds: ["skip_paused"] },
  { id: "before_expiry_guard", kinds: ["skip_rule"] },
  { id: "daily_cap", kinds: ["skip_daily_cap"] },
  { id: "position_cap", kinds: ["skip_position_cap"] },
  { id: "daily_loss", kinds: ["skip_daily_loss"] },
  { id: "your_rules", kinds: ["skip_rule"] },
  { id: "written_rules", kinds: ["skip_ai_needed", "policy_veto"] },
  { id: "trade_size", kinds: ["skip_sizing", "skip_budget", "skip_no_quote"] },
  { id: "broker_order", kinds: ["skip_no_contract", "broker_lookup_failed", "broker_disconnected", "order_failed", "entry_unfilled", "broker_no_order"] },
] as const;
export type CheckId = (typeof ENTRY_CHECKS)[number]["id"];

/** Every Activity `reason_code` this version can produce. */
export const REASON_CODES = [
  "entered",
  "exit_signal",
  "records_corrected",
  "paused",
  "daily_cap_reached",
  "position_cap_reached",
  "daily_loss_reached",
  "rule",
  "written_rules_veto",
  "ai_needed",
  "signal_too_old",
  "size_unknown",
  "over_trade_size",
  "no_quote",
  "contract_not_found",
  "broker_lookup_failed",
  "broker_disconnected",
  "order_rejected",
  "order_not_filled",
  "broker_no_order",
  "exit_failed",
  "exit_partial",
  "past_expiry_guard",
  "exit_already_arrived",
  "rule_not_in_list",
  "rule_skip_list",
  "rule_side",
  "rule_min_strike",
  "rule_0dte",
  "rule_loss_streak",
  "expiry_guard",
  "exit_remainder",
  "closed_outside",
  "buy_unconfirmed",
  "sell_unconfirmed",
  "buy_partly_filled",
  "unknown",
] as const;
export type ReasonCode = (typeof REASON_CODES)[number];

/** Every owner-facing condition code (Status.conditions). */
export const CONDITIONS = [
  "agent_stopped",
  "agent_not_answering",
  "state_hold",
  "handoff_hold",
  "restarting",
  "paused",
  "settings_unreadable",
  "ai_needed",
  "consent_needed",
  "proposal_pending",
  "broker_down",
  "positions_unmanaged",
  "low_cash",
  "daily_cap_reached",
  "daily_loss_reached",
  "position_cap_reached",
  "budget_too_small",
  "signal_pass",
  "feed_unreachable",
  "records_unwritten",
  "backup_failing",
  "second_copy",
  "position_expired",
  "position_expiring_unsold",
  "position_expiring",
  "feed_off_hours",
] as const;
export type Condition = (typeof CONDITIONS)[number];

export interface Status {
  schema: 2;
  /** server time of this snapshot */
  as_of: Iso;
  agent: Agent;
  /** what NEW trades use; pausing never changes it */
  mode: "live" | "practice";
  /** false: the settings never said (dry_run is missing), and the agent treats that as live */
  mode_explicit: boolean;
  pause: Pause;
  real_positions: number;
  /** are the real positions being sold on their exit signal / at expiry right now */
  real_positions_managed: boolean;
  /** why not, for the first real position that isn't */
  unmanaged_reason: "agent_stopped" | "agent_not_answering" | "state_hold" | "handoff_hold" | "restarting" | "broker_down" | "practice_holds_real_sells" | "other_account" | "expired" | "no_exit_signals" | "feed_unreachable" | "no_bid" | "pass_unchecked" | null;
  /** false: the expiry sell is switched off in the settings, so nothing sells a position on its expiry day but an exit signal */
  expiry_guard_on: boolean;
  broker: Broker;
  signals: Signals;
  market: Market;
  limits: Limits;
  capabilities: Capabilities;
  /** the checks an entry signal passes, in the order the agent runs them (agent.ENTRY_CHECKS) */
  entry_checks: CheckId[];
  positions: Position[];
  activity: ActivityPage;
  results: Results;
  notices: Notice[];
  /** every owner-facing condition true now, each once, most urgent first: one note per code on a screen */
  conditions: ConditionNote[];
  proposal: Proposal;
}

export interface Proposal {
  /** an AI-proposed code change is waiting for review */
  pending: boolean;
  /** sha256 of the whole diff (the text GET /api/proposal shows, unless it says truncated) */
  diff_sha256: string | null;
}

export interface Agent {
  /** false: this server has no agent loop behind it (setup, or the agent was stopped) */
  running: boolean;
  /** the loop's last heartbeat is as recent as its own schedule says it should be */
  answering: boolean;
  last_heartbeat: Iso | null;
  heartbeat_every_s: number | null;
  started_at: Iso | null;
  /** false: config.json can't be read right now; the values shown are the ones the agent runs on */
  settings_readable: boolean;
}

export interface Pause {
  /** entries = the owner's Pause (exits, expiry guard and broker checks keep running); state_hold / handoff_hold / restarting / stopped = no orders at all */
  kind: "none" | "entries" | "state_hold" | "handoff_hold" | "restarting" | "stopped";
  scope: "none" | "entries" | "all";
  /** the owner's Pause is saved, whatever `kind` says */
  entries_paused: boolean;
  /** null when it was not recorded */
  since: Iso | null;
  survives_restart: boolean;
  can_resume: boolean;
}

export interface Broker {
  id: string;
  name: string;
  account_mask: string | null;
  paper_account: boolean;
  /** null: this broker adapter doesn't report it */
  connected: boolean | null;
  /** when the connection went down (only while it is down) */
  since: Iso | null;
  last_answer: Iso | null;
  last_error: string | null;
  buying_power: number | null;
  buying_power_at: Iso | null;
}

export interface Signals {
  source: string;
  /** false: the agent is deliberately not reading the feed (a full hold, or paused with no real position open on a pay-per-day feed) */
  reading: boolean;
  /** could the last read of the feed see entry signals (false: no pass or day-pass, or it ran out); null = not read yet */
  entry_access: boolean | null;
  last_poll: Iso | null;
  last_poll_ok: boolean | null;
  /** when the newest signal the agent has seen happened */
  last_signal: Iso | null;
  /** feed = the feed's own time; activity_log = when the agent last logged handling a signal */
  last_signal_from: "feed" | "activity_log" | null;
  pass: Pass;
  /** practice mode with the AgentHC feed: what the owner's rules would have taken from the feed's closed trades of the past days (dry_run_preview); null when it is off or does not apply */
  preview: WouldHave | null;
}

export interface WouldHave {
  days: number;
  would_take: number;
  wins: number;
  breakeven: number;
  losses: number;
  avg_paper_pnl_pct: number | null;
  updated_at: Iso | null;
  /** why the last refresh failed; null when it did not */
  error: string | null;
  unavailable: boolean;
  rows: WouldHaveRow[];
}

export interface WouldHaveRow {
  date: string;
  contract: string;
  entry_time: string | null;
  exit_time: string | null;
  hold_minutes: number | null;
  paper_pnl_pct: number | null;
  exit_kind: string | null;
  would_take: boolean;
  skip_reason: string | null;
}

export interface Pass {
  kind: "prepaid" | "day_pass" | "subscription" | "not_needed" | "none";
  /** unknown: a prepaid pass the agent could not check with AgentHC yet (it reads no signals until it can); a wallet day-pass that is running counts as active */
  status: "active" | "exits_only" | "waiting" | "none" | "unknown";
  days_left: number | null;
  auto_pay: boolean;
}

export interface Market {
  open: boolean;
  trading_day: boolean;
  /** this session's open while the market is open, else the next session's */
  opens_at: Iso;
  /** the close of that same session (early closes included) */
  closes_at: Iso;
  early_close: boolean;
  timezone: "America/New_York";
}

export interface Limits {
  /** new trades allowed per day */
  cap: number;
  /** entries today, real and practice counted apart */
  used_today: {
    real: number;
    practice: number;
  };
  /** for the mode the agent is in */
  cap_reached: boolean;
  /** the limits count by UTC day */
  resets_at: Iso;
  /** null = no daily loss limit */
  max_daily_loss: number | null;
  realised_pnl_today: {
    real: number;
    practice: number;
  };
  /** today's estimated closed-trade loss as a positive number (0 when in profit) */
  realised_loss_today: {
    real: number;
    practice: number;
  };
  unmeasured_exits_today: {
    real: number;
    practice: number;
  };
  loss_limit_reached: boolean;
  max_positions: number;
  /** the limit counts both together */
  open_positions: {
    real: number;
    practice: number;
  };
  positions_reached: boolean;
  trade_size: {
    main: TradeSize;
    other: TradeSize;
  };
  cash: Cash;
  /** today's (UTC) signals skipped because one contract cost more than the dollar trade size, and a size that would have taken about half of them (agent._budget_hints) */
  budget_hints: BudgetHint[];
}

export interface BudgetHint {
  book: "main" | "other";
  budget: number;
  skipped: number;
  min_cost: number;
  max_cost: number;
  /** a dollar trade size (rounded up to $50) */
  suggest: number;
  would_take: number;
}

export interface TradeSize {
  mode: "budget" | "contracts" | "percent";
  usd: number | null;
  contracts: number | null;
  percent: number | null;
  max_contracts: number | null;
  same_as_main: boolean;
}

export interface Cash {
  low: boolean;
  low_reason: "under_minimum" | "under_trade_size" | null;
  /** whole main-size trades the buying power covers; null unless the size is a dollar budget */
  trades_affordable: number | null;
}

export interface Capabilities {
  opening: Answer;
  closing: Answer;
  listening: Answer;
}

export interface Answer {
  state: "yes" | "practice_only" | "not_today" | "paused" | "cant" | "unknown";
  reason_code: "ok" | "practice_mode" | "nothing_open" | "only_practice_positions" | "agent_stopped" | "agent_not_answering" | "state_hold" | "handoff_hold" | "restarting" | "owner_pause" | "pause_feed_not_read" | "broker_down" | "daily_cap_reached" | "daily_loss_reached" | "position_cap_reached" | "market_closed" | "no_signal_access" | "feed_unreachable" | "ai_needed" | "practice_holds_real_sells" | "pass_unchecked";
}

export interface Contract {
  symbol: string;
  expiry: string;
  strike: number;
  side: "C" | "P";
}

export interface Position {
  /** the agent's ledger key */
  id: string;
  /** null when the ledger key can't be read as a contract */
  contract: Contract | null;
  label: string;
  qty: number;
  /** per-share quote when it was bought (brokers don't report fill prices back) */
  entry_price: number | null;
  entry_cost_usd: number | null;
  opened_at: Iso | null;
  real: boolean;
  /** the agent's own tag for the broker account; never an account number */
  account: string | null;
  /** null for a position opened before this was recorded */
  book: "main" | "other" | null;
  expires_today: boolean;
  pending: "buy" | "sell" | null;
  next: NextAction;
}

export interface NextAction {
  action: "sell_on_exit" | "sell_at_expiry_guard" | "selling_now" | "paper_close" | "unmanaged" | "unknown";
  /** when, if the agent acts at a set time */
  at: Iso | null;
  reason_code: "agent_stopped" | "agent_not_answering" | "state_hold" | "handoff_hold" | "restarting" | "broker_down" | "practice_holds_real_sells" | "other_account" | "expired" | "no_exit_signals" | "feed_unreachable" | "no_bid" | "pass_unchecked" | "exit_signal" | "expiry_guard" | "sell_working" | "exit_remainder" | "pause_feed_not_read";
  /** when the expiry guard would sell it on its expiry day, if it is still open then */
  expiry_guard_at: Iso | null;
}

export interface ActivityPage {
  rows: Activity[];
  total: number;
  /** pass as ?before= for the page after this one */
  next: number | null;
  oldest: number | null;
  /** for each market (ET) day this page has rows of, newest first: how many lines that day has in the whole log, so a page can tell a day it has only part of ("" = rows whose time could not be read) */
  day_lines: DayLines[];
}

export interface DayLines {
  day: string;
  /** the day's lines in the whole log, of every kind */
  lines: number;
}

export interface Activity {
  /** line number in the trade log: a stable row id */
  n: number;
  at: Iso | null;
  action: string;
  /** false: a kind this version can't describe */
  known: boolean;
  contract: Contract | null;
  contract_text: string | null;
  book: "main" | "other" | null;
  /** which book the row belongs to; null = not recorded */
  real: boolean | null;
  /** true: the order went to a broker's paper-trading account (not real money); null = not recorded */
  paper_account: boolean | null;
  /** the mode NEW trades used then; null = not recorded */
  mode_at_time: "live" | "practice" | null;
  order_sent: boolean | null;
  filled: boolean | null;
  qty: number | null;
  /** per share, only when the broker reported the fill */
  fill_price: number | null;
  /** per-share quote the estimate used */
  quote_price: number | null;
  est_pnl_usd: number | null;
  cause: string | null;
  /** the entry check that stopped it */
  stopped_at_check: CheckId | null;
  reason_code: ReasonCode;
  reason_params: Record<string, string | number>;
  reason_text: string | null;
  hint: string | null;
}

export interface Results {
  /** orders the agent sent to a real-money account (or one not recorded as a paper-trading account) */
  real: Book;
  practice: Book;
  /** orders filled in a broker's paper-trading account (recorded on each line since this version): not real money */
  paper_account: Book;
  signal_cost: SignalCost;
  /** per book, trades OPENED on a paid signal day (agent.pnl_summary's pass figures, credited to the day the position was opened); null: the source has no per-day charge, or the book has no such figure */
  paid_days: {
    real: PaidDays | null;
    practice: PaidDays | null;
  };
}

export interface Book {
  /** estimated result of trades closed today (ET) */
  today: number;
  /** the last 7 calendar days (ET), today included */
  last_7_days: number;
  total: number;
  closed: number;
  won: number;
  lost: number;
  unmeasured: number;
  /** sales today (ET) with no estimate: not in `today`, and not in `days` */
  unmeasured_today: number;
  /** entries today (the ET day `today` describes) in this book; the daily limit counts by UTC day instead (Limits.used_today) */
  opened_today: number;
  days: BookDay[];
}

export interface BookDay {
  /** ET day the trades were closed */
  day: string;
  pnl: number;
  closed: number;
}

export interface PaidDays {
  pnl: number;
  closed: number;
  /** paid signal days those trades were opened on */
  days: number;
  /** the book's `days` that were paid signal days */
  paid_on: string[];
  /** the book's `days` from before paid days were recorded on which the agent took an entry (a feed entry needs a paid day) */
  estimated_on: string[];
}

export interface SignalCost {
  /** false: this signal source has no per-day charge */
  applies: boolean;
  /** signal days recorded as paid for */
  days_used: number;
  /** days from before that was recorded */
  days_estimated: number;
  sats_paid_today: number | null;
  /** null: the total paid is not recorded yet */
  sats_paid: number | null;
  /** null: the total paid is not recorded yet */
  usd_paid: number | null;
}

export interface Notice {
  level: "info" | "warn" | "urgent";
  code: string;
  text: string;
}

export interface ConditionNote {
  code: Condition;
  /** the most urgent of the condition's own level and its agent messages' */
  level: "info" | "warn" | "urgent";
  /** indexes into `notices` of the agent's own messages about this condition (say it once: with the dashboard's note, or alone when it has none); a notice no condition lists is about something else */
  notices: number[];
}
