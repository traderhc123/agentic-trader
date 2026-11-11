// The words. Every code the view-model can send (an Activity reason code, an
// entry check, a capability reason, a position's next action) is turned
// into a plain sentence HERE and nowhere else, so one table can be read,
// reviewed and tested (webui_app/tests/copy.test.mjs covers every code).
//
// Rules for this file:
//   - a sentence may only state what the row or the status carries. A number
//     that is not in `reason_params` is not mentioned; the agent's own note
//     (`reason_text`) is quoted as the agent's note, never rephrased as fact;
//   - a code this version does not know gets the fallback, which says so;
//   - no imports that exist at run time except ./format (the Node tests load
//     these two files as they are).
import type { Activity, Answer, CheckId, ExitLoss, NextAction, Position, ReasonCode, Status } from "../../lib/viewModel";
import { clip, contractName, etTime, etWhen, plural, usd } from "./format.ts";

export type Cat = "trade" | "limit" | "problem" | "other";
export type TagKind = "real" | "prac" | "paper" | "none" | "nobuy" | "unk";
export type Go = "rules" | "settings" | "signals" | "holdings";
export type IconName =
  | "in" | "out" | "skip" | "clock" | "alert" | "plug" | "note" | "q" | "check" | "dash" | "pause" | "info";

export const FALLBACK_TITLE = "Recorded, but not described yet";

export const TAG_WORDS: Record<TagKind, string> = {
  real: "Real money",
  prac: "Practice",
  paper: "Paper account",
  none: "No order",
  nobuy: "Nothing bought",
  unk: "Unknown",
};

/** The entry checks, in words. The ORDER comes from the status
 *  (`entry_checks`), never from this object. */
export const CHECK_LABELS: Record<CheckId, string> = {
  signal_fresh: "The signal is fresh, not one that arrived late",
  not_paused: "New trades are not paused",
  before_expiry_guard: "It does not expire today after the expiry sell time",
  exits_reachable: "The agent has not sold today for lack of exit signals",
  pass_covers_close: "The signal pass runs until today's close",
  daily_cap: "Daily limit of new trades",
  position_cap: "Limit on open positions",
  daily_loss: "Daily loss limit",
  your_rules: "Your filters (stock lists, calls or puts, smallest strike, same-day expiry, losing streak)",
  written_rules: "Your written rules",
  trade_size: "The broker has a price, and one contract fits your trade size",
  broker_order: "Order accepted and filled by the broker",
};
export const CHECK_FALLBACK = "A check this version of the page can't describe";

/** table[key], but only for a key the table itself has (a code from the
 *  server is never looked up on Object.prototype). */
export function own<T>(table: Record<string, T>, key: unknown): T | undefined {
  return typeof key === "string" && Object.prototype.hasOwnProperty.call(table, key) ? table[key] : undefined;
}

export function checkLabel(id: string): string {
  return own(CHECK_LABELS, id) ?? CHECK_FALLBACK;
}

// ── activity ────────────────────────────────────────────────────────────────

interface Ctx {
  e: Activity;
  /** "SPY $700 call" */
  name: string;
  /** the trade's book: true real, false practice, null not recorded */
  real: boolean | null;
  broker: string;
  num: (k: string) => number | null;
  str: (k: string) => string | null;
  /** the agent's own sentence for the row, quoted */
  note: string;
}

interface Entry {
  cat: Cat;
  icon: IconName;
  title: (c: Ctx) => string;
  sub: (c: Ctx) => string;
  what: (c: Ctx) => string;
  why: (c: Ctx) => string;
  can: (c: Ctx) => string;
  go?: Go;
}

const skipped = (c: Ctx) => `A signal to buy ${c.name} arrived and the agent did not act on it. No order was sent.`;
const nothing = () => "Nothing needed.";
const ownLimit = "Nothing needed. This is your own limit doing its job.";
const bookWord = (e: Activity) => (e.book === "other" ? "other trades" : e.book === "main" ? "main pick" : "");
const withNote = (c: Ctx, lead: string) => (c.note ? `${lead} ${c.note}` : lead);
/** "paper-account" for an order the row records as sent to a broker's
 *  paper-trading account, else "real" */
const acctWord = (c: Ctx) => (c.e.paper_account === true ? "paper-account" : "real");
const qtyOf = (c: Ctx) => (c.e.qty != null ? plural(c.e.qty, "contract") : "an unrecorded number of contracts");
const result = (c: Ctx) =>
  c.e.est_pnl_usd != null
    ? ` Estimated result: ${c.e.est_pnl_usd > 0 ? "+" : c.e.est_pnl_usd < 0 ? "−" : ""}${usd(c.e.est_pnl_usd)}${c.real === false ? " (pretend money)" : ""}.`
    : " The result was not measured.";
const cost = (c: Ctx) =>
  c.e.qty != null && c.e.quote_price != null ? `, about ${usd(c.e.qty * c.e.quote_price * 100)} at the quoted price` : "";
const sold = (c: Ctx, lead: string) => `${lead} ${qtyOf(c)}.${result(c)}`;
/** a real sell the row does not record as filled (still working, or nothing
 *  sold yet): never "Sold" */
const sellSent = (c: Ctx) => c.real !== false && c.e.filled === false;
const sentSale = (c: Ctx, of: string) =>
  `The agent sent a real sell order for ${qtyOf(c)} of ${of}. It had not filled when this line was written.${result(c)}`;

function rule(sub: (c: Ctx) => string, can: string): Entry {
  return {
    cat: "limit",
    icon: "skip",
    title: (c) => `Blocked by your rule: ${c.name}`,
    sub,
    what: skipped,
    why: (c) => withNote(c, sub(c) + "."),
    can: () => can,
    go: "rules",
  };
}

export const REASONS: Record<ReasonCode, Entry> = {
  entered: {
    cat: "trade",
    icon: "in",
    title: (c) =>
      c.real === false
        ? `Practice buy: ${c.name}`
        : c.e.filled === false
          ? `Buy order sent: ${c.name}`
          : `Bought ${c.name}`,
    sub: (c) => `Passed every check${bookWord(c.e) ? ` (${bookWord(c.e)})` : ""}`,
    what: (c) =>
      c.real === false
        ? `The agent recorded a pretend purchase of ${qtyOf(c)} of ${c.name}${cost(c)}. Nothing was sent to ${c.broker}.`
        : c.e.filled === false
          ? `The agent sent a real buy order for ${qtyOf(c)} of ${c.name}${cost(c)}. It had not filled yet when this line was written.`
          : `The agent bought ${qtyOf(c)} of ${c.name} at ${c.broker}${cost(c)}.`,
    why: () => "The signal passed every check below, in order.",
    can: (c) =>
      c.real === false
        ? "Nothing needed. This is practice."
        : `Nothing needed. The agent sells it when the exit signal arrives. You can also sell it yourself at ${c.broker}; the agent notices and stops tracking it.`,
  },
  exit_signal: {
    cat: "trade",
    icon: "out",
    title: (c) => (c.real === false ? `Practice sale: ${c.name}` : sellSent(c) ? `Sell order sent: ${c.name}` : `Sold ${c.name}`),
    sub: () => "Exit signal arrived",
    what: (c) =>
      sellSent(c) ? sentSale(c, c.name) : sold(c, c.real === false ? "The agent recorded a pretend sale of" : "The agent sold"),
    why: () =>
      "The signal source reported this trade closed. The agent always sells what it bought when the exit signal arrives, whatever your entry rules say.",
    can: nothing,
  },
  expiry_guard: {
    cat: "trade",
    icon: "out",
    title: (c) =>
      c.real === false
        ? `Practice sale before expiry: ${c.name}`
        : sellSent(c)
          ? `Sell order sent before expiry: ${c.name}`
          : `Sold ${c.name} before it expired`,
    sub: () => "Expiry protection",
    what: (c) =>
      sellSent(c) ? sentSale(c, c.name) : sold(c, c.real === false ? "The agent recorded a pretend sale of" : "The agent sold"),
    why: () =>
      "It expired that day and was still open at the expiry sell time. The agent sells expiring positions then, so they cannot be exercised or expire worthless after the close.",
    can: nothing,
  },
  signal_loss: {
    cat: "trade",
    icon: "out",
    title: (c) =>
      c.real === false
        ? `Practice sale, no exit signals: ${c.name}`
        : sellSent(c)
          ? `Sell order sent, no exit signals: ${c.name}`
          : `Sold ${c.name}: no exit signals`,
    sub: () => "No exit signals reached the agent",
    what: (c) =>
      sellSent(c) ? sentSale(c, c.name) : sold(c, c.real === false ? "The agent recorded a pretend sale of" : "The agent sold"),
    why: (c) =>
      withNote(
        c,
        "No exit signal could reach the agent for most of the session, so it sold what it held before the close rather than carry it overnight with nobody watching. It is not a price stop.",
      ),
    can: () => "Nothing needed. It opens nothing new for the rest of that day. The no-signal sell time is in Rules & limits.",
    go: "rules",
  },
  exit_remainder: {
    cat: "trade",
    icon: "out",
    title: (c) => (sellSent(c) ? `Sell order sent for the rest of ${c.name}` : `Sold the rest of ${c.name}`),
    sub: () => "The first sell order only sold part",
    what: (c) => (sellSent(c) ? sentSale(c, `the rest of ${c.name}`) : sold(c, "The agent sold the remaining")),
    why: () => "An earlier sell order for this position only partly filled. This is the agent selling what was left.",
    can: nothing,
  },
  closed_outside: {
    cat: "trade",
    icon: "out",
    title: (c) => `Closed outside the agent: ${c.name}`,
    sub: (c) => `Sold at ${c.broker}, not by the agent`,
    what: (c) =>
      `${c.name} was sold in your ${c.broker} account by an order the agent did not send${c.e.fill_price != null ? `, at ${usd(c.e.fill_price)} per share` : ""}.${result(c)}`,
    why: () => "The agent found the sale in your account and stopped tracking the position.",
    can: nothing,
  },
  records_corrected: {
    cat: "trade",
    icon: "note",
    title: (c) => `Records corrected: ${c.name}`,
    sub: () => "The agent's records now match your broker. No order was placed",
    what: (c) => `The agent corrected its own records for ${c.name}. No order was placed.`,
    why: (c) => withNote(c, "Your broker account and the agent's records disagreed, and the agent's own filled orders settled it."),
    can: nothing,
  },
  paused: {
    cat: "limit",
    icon: "pause",
    title: (c) => `Skipped ${c.name}`,
    sub: () => "New trades were paused",
    what: skipped,
    why: () => "You had paused new trades when this signal arrived.",
    can: () => "Nothing needed. Press Resume when you want new trades again.",
  },
  daily_cap_reached: {
    cat: "limit",
    icon: "skip",
    title: (c) => `Skipped ${c.name}`,
    sub: () => "Your daily trade limit was already reached",
    what: skipped,
    why: (c) =>
      c.num("cap") != null
        ? `You allowed ${plural(c.num("cap"), "new trade")} a day, and that many had already been opened when this signal arrived.`
        : "The day's limit of new trades had already been used when this signal arrived.",
    can: () => `${ownLimit} To allow more, raise "New trades per day" in Rules & limits.`,
    go: "rules",
  },
  position_cap_reached: {
    cat: "limit",
    icon: "skip",
    title: (c) => `Skipped ${c.name}`,
    sub: () => "Your open-positions limit was already reached",
    what: skipped,
    why: (c) =>
      c.num("max_open") != null
        ? `You were already holding ${plural(c.num("max_open"), "position")}, the most you allowed at one time.`
        : "You were already holding as many positions as you allowed at one time.",
    can: () => `${ownLimit} To hold more at once, raise "Open positions at once" in Rules & limits.`,
    go: "rules",
  },
  daily_loss_reached: {
    cat: "limit",
    icon: "skip",
    title: (c) => `Skipped ${c.name}`,
    sub: () => "Your daily loss limit had been reached",
    what: skipped,
    why: (c) =>
      c.num("loss_usd") != null && c.num("max_loss_usd") != null
        ? `Closed trades that day had already lost about ${usd(c.num("loss_usd"))}, at or over your ${usd(c.num("max_loss_usd"), 0)} daily loss limit, so the agent stopped opening new trades for the rest of the day.`
        : "That day's closed trades had already lost as much as your daily loss limit allows.",
    can: () => "Nothing needed. New trades start again the next day. This limit never sells anything.",
    go: "rules",
  },
  rule: rule(() => "One of your rules said no", "Nothing needed. Your rules are listed in Settings."),
  rule_not_in_list: rule(
    (c) => `${c.str("ticker") ?? "This stock"} is not on your list of stocks to trade`,
    "Nothing needed. To trade it, add it to your list in Settings.",
  ),
  rule_skip_list: rule(
    (c) => `${c.str("ticker") ?? "This stock"} is on your never-trade list`,
    "Nothing needed. To trade it, remove it from the list in Settings.",
  ),
  rule_side: rule(
    (c) => (c.str("sides") ? `You trade ${c.str("sides")} only` : "You trade only calls, or only puts"),
    "Nothing needed. To take both, change it in Settings.",
  ),
  rule_min_strike: rule(
    (c) =>
      c.num("min_strike") != null
        ? `The strike is under your ${usd(c.num("min_strike"), 2)} minimum`
        : "The strike is under your minimum",
    "Nothing needed. To allow it, lower the smallest strike in Settings.",
  ),
  rule_0dte: rule(
    () => "It expired the same day, and you skip same-day contracts",
    "Nothing needed. To allow same-day contracts, change it in Settings.",
  ),
  rule_loss_streak: rule(
    (c) =>
      c.num("losses") != null
        ? `${plural(c.num("losses"), "losing trade")} in a row that day, your limit`
        : "Too many losing trades in a row that day",
    "Nothing needed. New trades start again the next day.",
  ),
  past_expiry_guard: {
    cat: "limit",
    icon: "skip",
    title: (c) => `Skipped ${c.name}`,
    sub: (c) =>
      `It expired that day, and the signal came after the expiry sell time${c.str("guard_et") ? ` (${c.str("guard_et")} ET)` : ""}`,
    what: skipped,
    why: () =>
      "Buying a contract that expires today, after the time the agent sells expiring positions, would be sold again straight away.",
    can: nothing,
  },
  after_signal_loss_sell: {
    cat: "limit",
    icon: "skip",
    title: (c) => `Skipped ${c.name}`,
    sub: () => "The agent had sold what it held for lack of exit signals that day",
    what: skipped,
    why: () =>
      "No exit signals had reached the agent that day, so it sold what it held before the close and opened nothing new until the next day: a new position would have had no exit signal either.",
    can: () => "Nothing needed. New trades start again the next day.",
  },
  pass_wont_cover: {
    cat: "limit",
    icon: "skip",
    title: (c) => `Skipped ${c.name}`,
    sub: (c) =>
      c.str("pass_ends_et") ? `The day-pass ended at ${c.str("pass_ends_et")} ET, before the close` : "The day-pass ended before the close",
    what: skipped,
    why: () =>
      "The wallet's day-pass ran out before that day's close and a new one was not paid, so a position opened then would have lost its exit signals before the close.",
    can: () => "Add sats to the agent's wallet on Signals & pass, or buy signal days there.",
    go: "signals",
  },
  written_rules_veto: {
    cat: "limit",
    icon: "skip",
    title: (c) => `Blocked by your rule: ${c.name}`,
    sub: () => "Your written rules said no",
    what: skipped,
    why: (c) => c.note || "Your written rules said no. The reason was not recorded.",
    can: () => "Nothing needed. You can read or change your written rules in Settings.",
    go: "rules",
  },
  ai_needed: {
    cat: "problem",
    icon: "alert",
    title: (c) => `Skipped ${c.name}`,
    sub: () => "The AI helper that checks your written rules was not available",
    what: (c) => `A signal to buy ${c.name} was not acted on. While this lasts, every new trade is skipped.`,
    why: (c) =>
      withNote(
        c,
        "Your written rules need the AI helper to read each signal. It could not, so the agent skipped the trade rather than ignore your rules.",
      ),
    can: (c) => c.e.hint ?? "Reconnect the AI helper in Settings, or turn off written rules.",
    go: "settings",
  },
  signal_too_old: {
    cat: "problem",
    icon: "clock",
    title: (c) => `Missed, arrived late: ${c.name}`,
    sub: (c) =>
      c.num("max_age_s") != null
        ? `The signal reached this computer more than ${Math.round((c.num("max_age_s") as number) / 60)} minutes late`
        : "The signal reached this computer too late",
    what: (c) => `A signal to buy ${c.name} was not acted on. No order was sent.`,
    why: () =>
      "The signal was too old when your agent saw it, so the price had probably moved. This usually means the computer was asleep or offline.",
    can: () => "Keep this computer awake and online while the market is open (Settings).",
    go: "settings",
  },
  exit_already_arrived: {
    cat: "problem",
    icon: "clock",
    title: (c) => `Missed, already over: ${c.name}`,
    sub: () => "The exit for this trade had arrived before the agent saw the entry",
    what: (c) => `A signal to buy ${c.name} was not acted on. No order was sent.`,
    why: () => "By the time your agent first saw this signal, the signal source had already closed the trade.",
    can: nothing,
  },
  size_unknown: {
    cat: "problem",
    icon: "alert",
    title: (c) => `Skipped ${c.name}`,
    sub: () => "The trade size could not be worked out",
    what: (c) => `A signal to buy ${c.name} was not acted on. No order was sent.`,
    why: (c) => withNote(c, "The agent could not work out how much to buy, so it skipped rather than guess."),
    can: (c) => c.e.hint ?? "Check your trade size in Settings.",
    go: "rules",
  },
  over_trade_size: {
    cat: "limit",
    icon: "skip",
    title: (c) => `Skipped ${c.name}`,
    sub: (c) =>
      c.num("cost_usd") != null
        ? `One contract cost about ${usd(c.num("cost_usd"), 0)}, above your trade size`
        : "One contract cost more than your trade size",
    what: skipped,
    why: (c) =>
      `${c.num("cost_usd") != null ? `One contract cost about ${usd(c.num("cost_usd"), 0)}. ` : ""}That was more than your trade size${bookWord(c.e) ? ` for ${bookWord(c.e)} signals` : ""}, and the agent never spends more than that on one trade.`,
    can: () =>
      "Nothing needed. If you want trades this large, raise the trade size. A larger size puts more money at risk on every trade.",
    go: "rules",
  },
  no_quote: {
    cat: "problem",
    icon: "alert",
    title: (c) => `Skipped ${c.name}`,
    sub: (c) => `${c.broker} had no price for it at that moment`,
    what: (c) => `A signal to buy ${c.name} was not acted on. No order was sent.`,
    why: () =>
      "There was no price for this contract at that moment, so the agent could not work out how many contracts fit your trade size. It skips rather than guesses.",
    can: () => "Nothing needed. This is rare and passes on its own.",
  },
  contract_not_found: {
    cat: "problem",
    icon: "alert",
    title: (c) => `Skipped ${c.name}`,
    sub: (c) => `${c.broker} could not find this contract`,
    what: (c) => `A signal to buy ${c.name} was not acted on. No order was sent.`,
    why: (c) => withNote(c, "The broker did not list this exact contract, so there was nothing to order."),
    can: (c) => c.e.hint ?? nothing(),
  },
  broker_lookup_failed: {
    cat: "problem",
    icon: "alert",
    title: (c) => `Skipped ${c.name}`,
    sub: (c) => `${c.broker} returned an error while looking it up`,
    what: (c) => `A signal to buy ${c.name} was not acted on.`,
    why: (c) => withNote(c, "The broker returned an error when the agent looked the contract up."),
    can: (c) => c.e.hint ?? "Nothing needed if it happens once. If it keeps happening, check the broker connection in Settings.",
    go: "settings",
  },
  broker_disconnected: {
    cat: "problem",
    icon: "plug",
    title: (c) => `${c.broker} connection lost`,
    sub: (c) => `A signal for ${c.name} could not be acted on`,
    what: (c) => `The agent could not reach your ${c.broker} account when a signal for ${c.name} arrived.`,
    why: (c) => withNote(c, "Until the broker is connected again the agent cannot buy or sell."),
    can: (c) => c.e.hint ?? "Connect the broker again in Settings.",
    go: "settings",
  },
  order_rejected: {
    cat: "problem",
    icon: "alert",
    title: (c) => `Buy order rejected: ${c.name}`,
    sub: (c) => `${c.broker} rejected the order. Nothing was bought`,
    what: (c) => `The agent sent a real buy order for ${c.name} and the broker rejected it. Nothing was bought.`,
    why: (c) => c.note || "The broker did not say why.",
    can: (c) => c.e.hint ?? "Check your broker account, then your trade size.",
    go: "rules",
  },
  order_not_filled: {
    cat: "problem",
    icon: "alert",
    title: (c) => `Buy order not filled: ${c.name}`,
    sub: () => "The order was sent and never filled. Nothing was bought",
    what: (c) => `The agent sent a real buy order for ${c.name}. It was never filled. Nothing was bought.`,
    why: (c) => withNote(c, "Nobody sold at the price the order asked for."),
    can: (c) => c.e.hint ?? nothing(),
  },
  broker_no_order: {
    cat: "problem",
    icon: "alert",
    title: (c) => `No order placed: ${c.name}`,
    sub: (c) => `${c.broker} did not place the order`,
    what: (c) => `The agent tried to buy ${c.name} and the broker did not place the order.`,
    why: (c) => c.note || "The broker did not say why.",
    can: (c) => c.e.hint ?? "Check the broker connection in Settings.",
    go: "settings",
  },
  exit_failed: {
    cat: "problem",
    icon: "alert",
    title: (c) => `Sell order failed: ${c.name}`,
    sub: () => "The sell did not go through at that moment",
    what: (c) => `An attempt to sell ${c.name} did not go through.`,
    why: (c) => c.note || "The reason was not recorded.",
    can: (c) =>
      c.e.hint ??
      `Look for a later "Sold" line for this contract. If there is none, check the position at ${c.broker} and sell it there if you want it closed.`,
  },
  exit_partial: {
    cat: "problem",
    icon: "alert",
    title: (c) => `Only part sold at first: ${c.name}`,
    sub: () => "The sell order only partly filled",
    what: (c) => `The sell order for ${c.name} only sold part of the position.`,
    why: (c) => withNote(c, "There were not enough buyers at that price for all of it at once."),
    can: (c) =>
      c.e.hint ??
      `The agent sells the rest by itself. Look for a later "Sold the rest" line; if there is none, check the position at ${c.broker}.`,
  },
  // The agent records these for every order that went in and may have
  // filled: no clear answer from the broker, an earlier order for the same
  // signal that the broker already has, or an earlier buy still unconfirmed.
  // The agent's note (`why`) says which; the words here fit all three.
  buy_unconfirmed: {
    cat: "problem",
    icon: "alert",
    title: (c) => `Buy not confirmed: ${c.name}`,
    sub: () => "Not confirmed by the broker. A buy may have gone through",
    what: (c) =>
      `The agent could not confirm a ${acctWord(c)} buy of ${c.name} at ${c.broker}. A buy order for it went in, now or earlier, and may have filled: whether anything was bought is not known.`,
    why: (c) => c.note || "The broker did not confirm the order either way.",
    can: (c) => c.e.hint ?? `Check your ${c.broker} account for ${c.name}. A buy may have filled even though the agent could not confirm it.`,
  },
  sell_unconfirmed: {
    cat: "problem",
    icon: "alert",
    title: (c) => `Sell not confirmed: ${c.name}`,
    sub: () => "Not confirmed by the broker. A sell may have gone through",
    what: (c) =>
      `The agent could not confirm a ${acctWord(c)} sell of ${c.name} at ${c.broker}. A sell order for it went in, now or earlier, and may have filled: whether anything was sold is not known.`,
    why: (c) => c.note || "The broker did not confirm the order either way.",
    can: (c) =>
      c.e.hint ?? `Check ${c.name} in your ${c.broker} account before you sell anything yourself: it may already be sold.`,
  },
  buy_partly_filled: {
    cat: "problem",
    icon: "alert",
    title: (c) => `Buy order partly filled: ${c.name}`,
    sub: (c) => `The order did not fill in full. Part of it may be a ${c.e.paper_account === true ? "position in the paper-trading account" : "real position"}`,
    what: (c) =>
      `The agent's ${acctWord(c)} buy order for ${c.name} did not fill in full. The part that filled is ${c.e.paper_account === true ? `a position in your ${c.broker} paper-trading account (not real money)` : "a real position in your account"}.`,
    why: (c) => withNote(c, "There were not enough sellers at the order's price for all of it."),
    can: (c) => c.e.hint ?? `Check what your ${c.broker} account holds of ${c.name}. The agent looks after the part it finds there.`,
  },
  unknown: {
    cat: "other",
    icon: "q",
    title: (c) => `${FALLBACK_TITLE}: ${c.name}`,
    sub: () => "This version of the page has no description for this event",
    what: (c) =>
      `The agent recorded an event${c.e.action ? ` called "${clip(c.e.action, 40)}"` : ""} for ${c.name}. This version of the page has no plain-language description for it.`,
    why: (c) => withNote(c, "The agent is newer than this page, or the event is rare. Nothing here says an order was placed."),
    can: () => "Nothing needed. If it keeps appearing, install the update or send a report to support.",
    go: "settings",
  },
};

/** How "Today so far" groups the signals your limits stopped: one line per
 *  reason, not one per contract. A code not listed here groups by its `sub`. */
export const TALLY_LABELS: Partial<Record<ReasonCode, string>> = {
  paused: "New trades were paused",
  daily_cap_reached: "Daily trade limit already reached",
  position_cap_reached: "Open-positions limit already reached",
  daily_loss_reached: "Daily loss limit already reached",
  over_trade_size: "Cost more than your trade size",
  rule_skip_list: "On your never-trade list",
  rule_not_in_list: "Not on your list of stocks to trade",
  rule_min_strike: "Strike under your minimum",
  past_expiry_guard: "Expiring today, after the expiry sell time",
  after_signal_loss_sell: "Sold for lack of exit signals earlier",
  pass_wont_cover: "The day-pass ends before the close",
};

export interface Described {
  /** the line this row is counted under in "Today so far" */
  group: string;
  cat: Cat;
  icon: IconName;
  tag: TagKind;
  title: string;
  sub: string;
  what: string;
  why: string;
  can: string;
  go: Go | null;
  /** the money fact in one sentence, for the drawer */
  money: string;
  /** an entry signal that ran the checks (bought, or stopped at one) */
  entryPath: boolean;
  /** a sale: the entry checks do not apply */
  exitPath: boolean;
}

const TRADE_CODES = ["entered", "exit_signal", "expiry_guard", "signal_loss", "exit_remainder", "closed_outside"];
const EXIT_CODES = [
  "exit_signal", "expiry_guard", "signal_loss", "exit_remainder", "closed_outside", "exit_failed", "exit_partial", "sell_unconfirmed",
];
/** an order that went in and may have filled: "filled" is not known */
const UNCONFIRMED = ["buy_unconfirmed", "sell_unconfirmed"];

/** Which money tag a row gets, from what the row recorded and nothing else. */
export function tagOf(e: Activity): TagKind {
  // a broker's paper-trading account is never real money
  if (e.paper_account === true && e.real !== false && e.order_sent !== false) return "paper";
  if (e.action === "entry" || e.action === "exit")
    return e.real === true ? "real" : e.real === false ? "prac" : "unk";
  if (e.reason_code === "exit_failed" || e.reason_code === "exit_partial" || e.reason_code === "sell_unconfirmed")
    return e.order_sent === true ? "real" : "unk";
  if (e.order_sent === true) return e.filled === false ? "nobuy" : e.real === true ? "real" : "unk";
  if (e.order_sent === false) return "none";
  return "unk";
}

/** What became of the order, from `filled` alone: "filled" only when the
 *  row says so, "not known" when it does not. */
function orderFate(e: Activity, at: string, to: string): string {
  if (e.reason_code === "buy_partly_filled") return `A buy order ${at} only partly filled.`;
  if (UNCONFIRMED.includes(e.reason_code)) return `An order was sent ${to}; whether it went through is not known.`;
  if (e.filled === true) return `An order was filled ${at}.`;
  if (e.filled === false) return `An order was sent ${to}; it had not filled when this was recorded.`;
  return `An order was sent ${to}; whether it filled is not known.`;
}

function moneyLine(e: Activity, tag: TagKind, broker: string): string {
  switch (tag) {
    case "real":
      if (e.reason_code === "closed_outside") return `Real money. Sold at ${broker} by an order the agent did not send.`;
      if (e.reason_code === "exit_failed") return "Real money. A sell order was sent and did not go through.";
      return `Real money. ${orderFate(e, `at ${broker}`, `to ${broker}`)}`;
    case "paper":
      if (e.reason_code === "closed_outside") return `Not real money: sold in ${broker}'s paper-trading account by an order the agent did not send.`;
      if (e.order_sent !== true) return `Not real money: ${broker}'s paper-trading account. Whether an order was sent was not recorded for this line.`;
      if (e.reason_code === "exit_failed") return `Not real money: ${broker}'s paper-trading account. A sell order was sent and did not go through.`;
      return `Not real money: ${broker}'s paper-trading account. ${orderFate(e, "in that account", "to that account")}`;
    case "prac":
      return `Practice. Recorded only; nothing was sent to ${broker}.`;
    case "nobuy":
      return "A real order was sent and nothing was bought. No money moved.";
    case "none":
      return "No order was sent, so no money was involved.";
    default:
      return "Whether an order was sent was not recorded for this line.";
  }
}

/** One Activity row in words. Never throws: a row this version can't
 *  describe (an unknown code, a damaged row) gets the fallback. */
export function describe(e: Activity, brokerName?: string | null): Described {
  const broker = brokerName || "your broker";
  const params = e && typeof e.reason_params === "object" && e.reason_params ? e.reason_params : {};
  const ctx: Ctx = {
    e,
    name: contractName(e.contract, e.contract_text),
    real: e.action === "entry" || e.action === "exit" ? e.real : null,
    broker,
    num: (k) => (typeof params[k] === "number" && Number.isFinite(params[k] as number) ? (params[k] as number) : null),
    str: (k) => (typeof params[k] === "string" && params[k] ? clip(String(params[k]), 40) : null),
    note: e.reason_text ? `The agent's note: “${clip(e.reason_text, 300)}”` : "",
  };
  const known = own(REASONS, e.reason_code) !== undefined && e.known !== false;
  const tag = tagOf(e);
  const build = (r: Entry): Described => ({
    group: (known && TALLY_LABELS[e.reason_code]) || r.sub(ctx),
    cat: r.cat,
    icon: r.icon,
    tag,
    title: r.title(ctx),
    sub: r.sub(ctx),
    what: r.what(ctx),
    why: r.why(ctx),
    can: r.can(ctx),
    go: r.go ?? null,
    money: moneyLine(e, tag, broker),
    entryPath: e.reason_code === "entered" || e.stopped_at_check != null,
    exitPath: EXIT_CODES.includes(e.reason_code),
  });
  try {
    return build(known ? REASONS[e.reason_code] : REASONS.unknown);
  } catch {
    return build(REASONS.unknown);
  }
}

export const isTradeCode = (code: string) => TRADE_CODES.includes(code);

// ── "What the agent is doing right now" ─────────────────────────────────────

export type AnswerLook = { cls: "ok" | "no" | "paused" | "cant" | "unk"; icon: IconName; word: string };

export const ANSWER_LOOK: Record<Answer["state"], AnswerLook> = {
  yes: { cls: "ok", icon: "check", word: "Yes" },
  practice_only: { cls: "ok", icon: "check", word: "Practice only" },
  not_today: { cls: "no", icon: "dash", word: "Not today" },
  paused: { cls: "paused", icon: "pause", word: "Paused" },
  cant: { cls: "cant", icon: "alert", word: "Can't" },
  unknown: { cls: "unk", icon: "q", word: "Unknown" },
};
export const ANSWER_FALLBACK: AnswerLook = { cls: "unk", icon: "q", word: "Unknown" };

type Which = "opening" | "closing" | "listening";

/** Reasons that read the same whichever question they answer. */
const SHARED: Partial<Record<Answer["reason_code"], (s: Status) => string>> = {
  agent_stopped: () => "The agent is stopped. Nothing is bought, sold or watched until it is started again.",
  agent_not_answering: (s) =>
    `No answer from the agent since ${s.agent.last_heartbeat ? etTime(s.agent.last_heartbeat) : "an unknown time"}.`,
  state_hold: () =>
    "The agent is on hold because its memory file could not be read at startup. It places no orders at all until you press Resume.",
  handoff_hold: () =>
    "This setup is being moved to another computer or a server, so the agent here places no orders.",
  restarting: () => "The agent is about to restart (an update or a move). It places no orders until it is back.",
  pause_feed_not_read: () =>
    "New trades are paused and no real position is open, so the agent is not reading the signal feed. The pause uses no signal day; practice positions are not updated until you resume.",
};

export const leftToday = (s: Status) => {
  const used = s.mode === "live" ? s.limits.used_today.real : s.limits.used_today.practice;
  return Math.max(0, s.limits.cap - used);
};
export const sizeEach = (s: Status) => {
  const m = s.limits.trade_size.main;
  if (m.mode === "budget" && m.usd != null) return `, up to ${usd(m.usd, 0)} each`;
  if (m.mode === "contracts" && m.contracts != null) return `, ${plural(m.contracts, "contract")} each`;
  if (m.mode === "percent" && m.percent != null) return `, up to ${m.percent}% of your account each`;
  return "";
};

/** One capability answer as a sentence, with the numbers the status has. */
export function answerWhy(which: Which, a: Answer, s: Status): string {
  const shared = own(SHARED as Record<string, (s: Status) => string>, a.reason_code);
  if (shared) return shared(s);
  const b = s.broker.name || "your broker";
  const real = s.limits.open_positions.real;
  const prac = s.limits.open_positions.practice;
  const days = s.signals.pass.days_left;
  // no exit signals, and the agent sells what it holds itself before the close
  const sells = sellsFor(s);
  if (which === "closing" && sells && NO_EXITS.includes(a.reason_code))
    return `No exit signals reach the agent. It sells your real positions itself at ${sells} today unless they are back.`;
  switch (a.reason_code) {
    case "ok":
      if (which === "opening")
        return `With real money: ${leftToday(s)} more today${sizeEach(s)}.${s.market.open ? "" : " The market is not open yet."}`;
      if (which === "closing") {
        const both = [real ? `${plural(real, "real position")} for real` : "", prac ? `${prac} practice on paper` : ""]
          .filter(Boolean)
          .join(", ");
        return `On each exit signal: ${both}.`;
      }
      return `${s.signals.last_signal ? `Last signal at ${etTime(s.signals.last_signal)}.` : "No signal seen yet."}${days != null ? ` ${plural(days, "signal day")} left.` : ""}`;
    case "practice_mode":
      return `${leftToday(s)} more pretend ${leftToday(s) === 1 ? "trade" : "trades"} today. No order is sent to ${b}.`;
    case "nothing_open":
      return "Nothing is open, so there is nothing to sell.";
    case "only_practice_positions":
      return `Only practice positions are open (${prac}). They close on paper at their exit signals.`;
    case "owner_pause":
      return "You paused new trades. Press Resume to allow them again.";
    case "broker_down":
      return which === "opening"
        ? `${b} is not connected, so real buy orders can't be sent.`
        : `Real positions can't be sold until ${b} is connected again.${prac ? " Practice ones still close on paper." : ""}`;
    case "daily_cap_reached":
      return `Today's limit of ${plural(s.limits.cap, "new trade")} is used. The count starts again at ${etTime(s.limits.resets_at)}.`;
    case "daily_loss_reached": {
      const lost = s.mode === "live" ? s.limits.realised_loss_today.real : s.limits.realised_loss_today.practice;
      return `Today's closed trades have lost about ${usd(lost)}${s.limits.max_daily_loss != null ? `, at or over your ${usd(s.limits.max_daily_loss, 0)} daily loss limit` : ""}. Nothing new until tomorrow; this never sells anything.`;
    }
    case "position_cap_reached":
      return `You hold ${plural(real + prac, "position")}, the most you allow at once (${s.limits.max_positions}). New trades wait until one is sold.`;
    case "market_closed":
      return "The market is closed for today.";
    case "no_signal_access":
      if (which === "opening")
        return "Your signal pass does not cover new trades right now. Exit signals for what you hold still arrive if the pass allows it.";
      if (which === "closing")
        return `No exit signals reach the agent: there is no usable signal pass. It will not sell your real positions on an exit signal, so watch them and sell them yourself at ${b}.${s.expiry_guard_on ? " The expiry sell on expiry day still runs." : ""}`;
      return "The agent has no access to the signal feed: there is no usable pass.";
    case "feed_unreachable":
      return which === "closing"
        ? `The last check of the signal feed failed, so an exit signal can't reach the agent right now. It keeps trying; if this lasts, watch your real positions at ${b}.`
        : "The last check of the signal feed failed. The agent keeps trying.";
    case "pass_unchecked":
      if (which === "closing")
        return `The agent could not check your signal pass with AgentHC, so it reads no signals for now and an exit signal can't reach it. It checks again on each read; if this lasts, watch your real positions at ${b}.${s.expiry_guard_on ? " The expiry sell on expiry day still runs." : ""}`;
      return "The agent could not check your signal pass with AgentHC, so it reads no signals until it can. It checks again on each read.";
    case "ai_needed":
      return "Your written rules need the AI helper, and it is not connected, so every new trade is skipped.";
    case "practice_holds_real_sells":
      return `In practice mode your agent is set not to sell real positions. Sell them yourself at ${b}.`;
    default:
      return "This version of the page has no description for the agent's reason yet.";
  }
}

/** "Nothing open" reads better than a bare "Yes" for closing. */
export function answerLook(a: Answer): AnswerLook {
  if (a.reason_code === "nothing_open") return { cls: "no", icon: "dash", word: "Nothing open" };
  return own(ANSWER_LOOK, a.state) ?? ANSWER_FALLBACK;
}

// ── "What the agent will do" with one position ──────────────────────────────

export interface NextSay {
  text: string;
  /** ok = being looked after, bad = a real position nobody is selling,
   *  idle/unk = practice (nothing at risk) or not known */
  look: "ok" | "bad" | "idle" | "unk";
  icon: IconName;
}

const NEXT_UNMANAGED: Record<string, (p: Position, b: string) => string> = {
  agent_stopped: (p) => (p.real ? "Not watched: the agent is stopped." : "Not updated while the agent is stopped. Pretend, so nothing is at risk."),
  agent_not_answering: (p) => (p.real ? "Unknown. Treat it as unwatched." : "Unknown. The agent is not answering."),
  state_hold: (p) => (p.real ? "Not watched while the agent is on hold." : "Not updated while the agent is on hold. Pretend, so nothing is at risk."),
  handoff_hold: (p) => (p.real ? "Not watched here: this setup is being moved." : "Not updated here: this setup is being moved."),
  restarting: (p) => (p.real ? "Not watched for a moment: the agent is restarting." : "Not updated for a moment: the agent is restarting."),
  broker_down: (_p, b) => `Can't be sold until ${b} is connected again.`,
  practice_holds_real_sells: (_p, b) => `Not sold by the agent in practice mode. Sell it yourself at ${b}.`,
  other_account: () => "Opened in a different broker account. The agent will not sell it; close it there yourself.",
  expired: (_p, b) => `Past its expiry. The agent no longer acts on it; check it at ${b}.`,
  pause_feed_not_read: () => "Not updated while paused. Pretend, so nothing is at risk.",
  exit_signal: (_p, b) =>
    `Unknown. The agent can't read this entry as a contract, so it will not sell it at expiry. Check it at ${b}.`,
  no_exit_signals: (p, b) =>
    p.real
      ? `Not sold on an exit signal: no exit signals reach the agent right now (there is no usable signal pass). Watch it and sell it yourself at ${b}.${guardStill(p)}`
      : "Not updated: no signals reach the agent right now. Pretend, so nothing is at risk.",
  feed_unreachable: (p, b) =>
    p.real
      ? `Unknown for now: the last check of the signal feed failed, so an exit signal can't reach the agent. It keeps trying; if this lasts, watch it at ${b}.${guardStill(p)}`
      : "Not updated while the signal feed can't be reached. Pretend, so nothing is at risk.",
  pass_unchecked: (p, b) =>
    p.real
      ? `Unknown for now: the agent could not check your signal pass with AgentHC, and it reads no signals until it can, so an exit signal can't reach it. It checks again on each read; if this lasts, watch it at ${b}.${guardStill(p)}`
      : "Not updated until the agent can check your signal pass. Pretend, so nothing is at risk.",
  no_bid: (p, b) =>
    p.real
      ? `Not sold so far: when the expiry sell looked today, nobody was bidding for it, so it left it (a contract with no bid is usually worthless). It looks again each loop and tries to sell it if a bid appears. If it is in the money, sell it yourself at ${b} before the close, or it may be exercised.`
      : "No bid on its expiry day. Pretend, so nothing is at risk.",
};

/** " The expiry sell at …" when the expiry sell still acts on it without
 *  any signal. */
function guardStill(p: Position): string {
  return p.next.expiry_guard_at ? ` The expiry sell still sells it on ${etWhen(p.next.expiry_guard_at)} if it is open then.` : "";
}
/** "3:30 PM ET" while the agent will sell what it holds itself for lack of
 *  exit signals (signals.exit_loss armed), else null. */
export function sellsFor(s: Status): string | null {
  const x = s.signals?.exit_loss;
  return x && x.armed && x.sell_at ? etTime(x.sell_at) : null;
}
/** The capability reasons that mean no exit signal reaches the agent. */
const NO_EXITS = ["no_signal_access", "feed_unreachable", "pass_unchecked"];

/** Why no exit signal reaches the agent, in the agent's own words
 *  (agent._SIGNAL_LOSS_WHY; a test keeps the two the same). */
export const EXIT_LOSS_WHY: Record<ExitLoss["cause"], string> = {
  pass_ended: "your signal pass has run out",
  pass_unpaid: "your signal pass is not paid yet",
  pass_unchecked: "the agent could not check your signal pass with AgentHC",
  no_day_pass: "the agent has no day-pass for today",
  day_pass_off: "Recurring day-pass is off, so the agent has no day-pass for today",
  day_pass_short: "the agent's wallet does not hold enough sats for today's day-pass",
  day_pass_failed: "the agent's wallet could not pay for today's day-pass",
  day_pass_undecided: "the agent's wallet has not said whether today's day-pass payment went through",
  day_pass_refused: "AgentHC refused today's day-pass",
  feed_error: "the signal feed can't be read",
  feed_not_live: "the signal feed can't be read",
  feed_refused: "the signal feed is refusing the agent",
};
export const exitLossWhy = (x: ExitLoss) => own(EXIT_LOSS_WHY, x.cause) ?? EXIT_LOSS_WHY.feed_error;

/** Reasons whose own sentence already says what happens on expiry day. */
const SAYS_EXPIRY = ["expired", "no_bid"];
export const GUARD_STILL = ["no_exit_signals", "feed_unreachable", "pass_unchecked"];

/** What the agent will do with a position, from its `next` alone. */
export function nextSay(p: Position, s: Status): NextSay {
  const n: NextAction = p.next;
  const b = s.broker.name || "your broker";
  const real = p.real;
  const how = real ? "Sells for real" : "Closes on paper";
  switch (n.action) {
    case "sell_on_exit":
      return { text: "Sells for real at its exit signal.", look: "ok", icon: "check" };
    case "sell_at_expiry_guard":
      return {
        text: `Sells for real at ${n.at ? etTime(n.at) : "the expiry sell time"} today, or sooner on its exit signal.`,
        look: "ok",
        icon: "clock",
      };
    case "selling_now":
      return {
        text:
          n.reason_code === "expiry_guard"
            ? "Being sold now: it expires today."
            : n.reason_code === "exit_remainder"
              ? "Selling the rest now: the first sell order only partly filled."
              : `A sell order is working at ${b} now.`,
        look: "ok",
        icon: "clock",
      };
    case "paper_close":
      return n.reason_code === "expiry_guard"
        ? { text: `${how} at ${n.at ? etTime(n.at) : "the expiry sell time"} today, or sooner on its exit signal.`, look: "ok", icon: "clock" }
        : { text: "Closes on paper at its exit signal.", look: "ok", icon: "check" };
    case "unmanaged":
    case "unknown": {
      const sells = sellsFor(s);
      if (real && sells && GUARD_STILL.includes(n.reason_code)) {
        const guardFirst = n.expiry_guard_at != null && s.signals.exit_loss?.sell_at != null && n.expiry_guard_at < s.signals.exit_loss.sell_at;
        return {
          text: `No exit signals reach the agent. It sells this at ${sells} today unless they are back.${guardFirst ? guardStill(p) : ""}`,
          look: "bad",
          icon: "clock",
        };
      }
      const say = own(NEXT_UNMANAGED, n.reason_code);
      let text = say ? say(p, b) : real ? "Not being looked after. The reason is not described in this version." : "Not updated right now.";
      const guarded = GUARD_STILL.includes(n.reason_code) && n.expiry_guard_at != null;
      if (real && p.expires_today && !SAYS_EXPIRY.includes(n.reason_code) && !guarded)
        text += ` It expires today: sell it yourself at ${b} before the close${s.market.open ? ` (${etTime(s.market.closes_at)})` : ""} if you want it closed.`;
      return real
        ? { text, look: "bad", icon: "alert" }
        : { text, look: n.action === "unknown" ? "unk" : "idle", icon: n.action === "unknown" ? "q" : "pause" };
    }
    default:
      return { text: "Unknown. This version of the page can't describe the agent's plan for it.", look: real ? "bad" : "unk", icon: "q" };
  }
}

/** Why real positions are not being sold, as a short clause for the band. */
export const UNMANAGED_SHORT: Record<NonNullable<Status["unmanaged_reason"]>, string> = {
  agent_stopped: "Not being watched.",
  agent_not_answering: "They may be unwatched.",
  state_hold: "Not being watched.",
  handoff_hold: "Not being watched here.",
  restarting: "Not watched for a moment.",
  broker_down: "They can't be sold right now.",
  practice_holds_real_sells: "Not sold in practice mode.",
  other_account: "One is in another broker account.",
  expired: "One is past its expiry.",
  no_exit_signals: "No exit signals reach the agent.",
  feed_unreachable: "The signal feed can't be reached right now.",
  no_bid: "One had no bid on its expiry day.",
  pass_unchecked: "The signal pass could not be checked.",
};
/** The same reasons as a full sentence, for the needs-you note. */
export const UNMANAGED_WHY: Record<NonNullable<Status["unmanaged_reason"]>, string> = {
  agent_stopped: "The agent is stopped.",
  agent_not_answering: "The agent is not answering.",
  state_hold: "The agent is on hold, so it places no orders at all.",
  handoff_hold: "This setup is being moved, so the agent here places no orders.",
  restarting: "The agent is restarting and places no orders until it is back.",
  broker_down: "The broker is not connected, so nothing can be sold.",
  practice_holds_real_sells: "In practice mode your agent is set not to sell real positions.",
  other_account: "A position was opened in a different broker account than the agent uses now, and the agent will not sell it.",
  expired: "A position is past its expiry and the agent no longer acts on it.",
  no_exit_signals:
    "No exit signals reach the agent right now: the signal pass is used up, expired or waiting for payment, or AgentHC refused the day-pass. The agent will not sell on an exit signal.",
  feed_unreachable:
    "The last check of the signal feed failed, so an exit signal can't reach the agent right now. It keeps trying.",
  no_bid:
    "A position that expires today had no bid when the expiry sell looked, so it was not sold then. It looks again each loop. If it is in the money it can be exercised.",
  pass_unchecked:
    "The agent could not check your signal pass with AgentHC, and it reads no signals until it can, so an exit signal can't reach it right now.",
};
export const UNMANAGED_FALLBACK = "Not being watched.";

// ── the expiry sell, said only when it is switched on ───────────────────────

/** What the agent does with a real position on its expiry day, for the
 *  position drawer: `will` when the expiry sell is on, `wont` when it is off. */
export function expiryWill(p: Position, s: Status): { will: string | null; wont: string | null } {
  const at = p.next.expiry_guard_at;
  if (at) return { will: `Sell it at ${etWhen(at)} if it is still open then, so it can't be exercised.`, wont: null };
  if (s.expiry_guard_on) return { will: "Sell it at its expiry sell time if it is still open then.", wont: null };
  return { will: null, wont: EXPIRY_OFF };
}
export const EXPIRY_OFF =
  "Sell it on its expiry day: the expiry sell is off in your settings, so sell it yourself before the close that day.";

/** How the agent sells what you hold, for the dialogs. */
export function sellsHow(s: Status): string {
  return s.expiry_guard_on
    ? "on exit signals, and at its expiry sell time on expiry day"
    : "on exit signals only (the expiry sell is off: sell expiring positions yourself before the close)";
}

// ── updating the agent's own code (/api/update/check, /api/update/pull) ─────
// The same flow as the classic dashboard's Update button: the page checks
// once, shows exactly which version it would install, and installs only
// when the owner presses Update now. The agent's own messages are quoted.

export const UPDATE_SOURCE = "github.com/traderhc123/agentic-trader";

export const UPDATE = {
  badge: "Update available",
  /** on phones the badge says only "Update"; wider screens add the rest */
  badgeShort: "Update",
  badgeRest: " available",
  /** the badge's count; null when the agent can't count the changes */
  badgeCount: (n: number | null) => (n == null ? "" : plural(n, "change")),
  badgeLabel: (n: number | null) =>
    n == null ? "Update available. Review it before installing." : `Update available: ${plural(n, "change")}. Review it before installing.`,

  title: "Update the agent?",
  lead: (version: string) => `This downloads exactly version ${version} from ${UPDATE_SOURCE}, nothing newer, and restarts the agent onto it.`,
  versionLabel: "Version",
  changesLabel: "Changes",
  changesUncounted: "Not counted: this install's version isn't recorded",
  listLabel: "What changed",
  noList: "The agent sent no list of changes.",
  will: [
    "Download and install exactly the version shown here.",
    "Place no orders, buys or sells, while it restarts onto it. That usually takes under a minute. If an order is being placed right then, it finishes that order first.",
    "Then carry on as before.",
  ],
  wont: [
    "Change your Pause setting, Practice or LIVE, or any rule or limit.",
    "Buy or sell anything because of the update.",
    "Install any version other than the one shown here.",
  ],
  liveTitle: "Real money",
  live: (realPositions: number) =>
    realPositions > 0
      ? `You are in LIVE mode. While it restarts, no real orders are sent, including sells of your ${plural(realPositions, "real position")}.`
      : "You are in LIVE mode. While it restarts, no real orders are sent.",
  notNow: "Not now",
  install: "Update now",
  installing: (seconds: number) =>
    `Downloading and installing… ${seconds}s${seconds >= 20 ? ". Downloading from GitHub can take up to a minute." : ""}`,

  failedTitle: "The update did not go through",
  againHint: "To check for updates again, reload this page.",
  close: "Close",

  restarting: "Update installed. The agent is restarting onto it and places no orders until it is back. This page reloads when it is.",
  /** the toast after the pull: the agent's note can say a dependency failed */
  restartingSaid: (note: string) =>
    note
      ? `Update installed. The agent is restarting onto it and places no orders until it is back. This page reloads when it is. The agent said: ${note}`
      : "Update installed. The agent is restarting onto it and places no orders until it is back. This page reloads when it is.",
  installed: (note: string) => `Update installed. The agent said: ${note}`,
  installedPlain: "Update installed.",
  agentSaid: (text: string) => `The agent said: ${text}`,
  slow: "The agent has not come back on the new version yet. If it could not restart, it sends you a message saying what to do. Reload this page to see where it stands.",

  // the quiet line under Settings
  rowLabel: "Updates",
  checking: "Checking for a newer version…",
  current: "This is the newest version.",
  /** the agent's own reason, quoted */
  unavailable: (why: string) => `Couldn't check for updates. The agent said: ${why}`,
  /** a reason this page found itself */
  unavailableHere: (why: string) => `Couldn't check for updates: ${why}`,
  available: (n: number | null) =>
    n == null ? "A newer version is available." : `A newer version is available: ${plural(n, "change")}.`,
  review: "Review update",
  busy: "Updating…",

  // reasons this page finds itself (completing unavailableHere)
  lockedCheck: "your dashboard login expired. Open the login link again (python agent.py link).",
  unreadable: "the agent answered in a form this page can't read.",
  noReason: "the agent gave no reason.",
  noVersion: "the agent did not say exactly which version it would install, so nothing is offered.",
  unreachable: "this page could not reach the agent.",
  checkTimeout: (seconds: number) => `no answer after ${seconds}s. Your network may be blocking GitHub.`,
  pullLocked: "Your dashboard login expired, so nothing was installed. Open the login link again (python agent.py link).",
  pullNoAnswer: "No answer from the agent, so this page does not know whether the update was installed. Reload the page in a minute to see.",
  pullUnreadable: (http: number) =>
    `The agent answered in a form this page can't read (HTTP ${http}), so this page does not know whether the update was installed. Reload the page in a minute to see.`,
  pullNoReason: "The agent did not install the update and gave no reason.",
};
