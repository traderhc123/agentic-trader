// The words and checks of the /v2 settings forms. Pure functions, no React,
// no imports that exist at run time except ./format, so the Node tests can
// run them as they are.
//
// The server is the judge: these checks only say what is wrong next to the
// field before anything is sent. What is saved, and what a change does to
// the owner's risk in the confirm dialog, always comes from the server's own
// preview (POST without `confirm`), never from here.
import type { Change, DeployStatus, Direction, Purchases, SettingsView, SignInEnd, SizeMode, SizeSpec, Unsold } from "../../lib/settingsApi";
import type { BudgetHint } from "../../lib/viewModel";
import { count, plural, usd } from "./format.ts";

/** The first-run wizard is the only page /v2 sends the owner to (to finish
 *  setup, or to bring a setup to this computer); it brings them back here
 *  (webui._RETURN_TO). */
export const SETUP_FROM_V2 = "/setup?from=v2";
/** The classic dashboard: Settings → This dashboard opens it (once). */
export const CLASSIC = "/dash";

// ── move to another computer ────────────────────────────────────────────────

/** What is wrong with the move file's passphrase ("" when nothing): the
 *  classic move tool's rules, 8 characters or more, typed twice alike. */
export function movePassProblem(pw: string, again: string): string {
  if (pw.length < 8) return "Pick a passphrase of at least 8 characters.";
  if (pw !== again) return "The two passphrases don't match.";
  return "";
}

/** The DigitalOcean regions a cloud move can use (provision.REGIONS; the
 *  server refuses any other). */
export const DEPLOY_REGIONS: [string, string][] = [
  ["nyc3", "New York"],
  ["sfo3", "San Francisco"],
  ["tor1", "Toronto"],
  ["lon1", "London"],
  ["fra1", "Frankfurt"],
  ["sgp1", "Singapore"],
];

/** The command that opens a new cloud server's dashboard from this
 *  computer: the server's own (deploy status "ssh"), or the classic page's
 *  fallback built from its address. null until there is an address. */
export function deploySsh(st: DeployStatus | null): string | null {
  if (!st) return null;
  if (typeof st.ssh === "string" && st.ssh.trim()) return st.ssh.trim();
  const ip = typeof st.ip === "string" ? st.ip.trim() : "";
  return /^[0-9A-Fa-f:.]{2,45}$/.test(ip) ? `ssh -L 8721:127.0.0.1:8721 trader@${ip}` : null;
}

/** One real position a stop or a move leaves unsold here, in words. */
export function unsoldWords(u: Unsold): string {
  const c = u.contract;
  const name = c ? `${c.symbol} ${c.strike}${c.side} ${c.expiry}` : u.label || "A position the agent can't name";
  const qty = typeof u.qty === "number" && Number.isFinite(u.qty) ? `, ${plural(u.qty, "contract")}` : "";
  return `${name}${qty}${u.expires_today ? ", expires today" : ""}`;
}

/** Why Start can't be offered yet ("" when it can): the first-run steps
 *  the wizard still has to do (the server checks again either way). */
export function setupUnfinished(v: SettingsView | null): string {
  if (!v) return "";
  if (v.consent?.accepted === false) return "The agreement isn't accepted yet.";
  if (!v.signals?.source) return "No signal source is chosen yet.";
  if (v.broker?.ready === false) return "No broker is connected yet.";
  return "";
}

/** "$1,150" for whole dollars, "$1,150.50" otherwise. */
export const money = (n: number) => usd(n, Number.isInteger(n) ? 0 : 2);

const isNum = (n: unknown): n is number => typeof n === "number" && Number.isFinite(n);

// ── reading what the owner typed ────────────────────────────────────────────

export type Parsed<T = number> = { ok: true; value: T } | { ok: false; error: string };

export interface Range {
  low: number;
  high: number;
  /** a whole number only */
  whole?: boolean;
  /** `low` itself is not allowed (more than low) */
  lowOpen?: boolean;
  /** dollars: say the range in dollars */
  money?: boolean;
  /** what 0 means, said in the error ("0 turns it off") */
  zero?: string;
}

function rangeWords(r: Range): string {
  const f = (n: number) => (r.money ? money(n) : n.toLocaleString("en-US"));
  const what = r.whole ? "a whole number" : r.money ? "an amount" : "a number";
  const span = r.lowOpen ? `more than ${f(r.low)} and at most ${f(r.high)}` : `from ${f(r.low)} to ${f(r.high)}`;
  return `Enter ${what} ${span}${r.zero ? ` (${r.zero})` : ""}.`;
}

/** A number from a text box: "$1,150", "1150", "2.5%". */
export function readNumber(text: string, r: Range): Parsed {
  const t = String(text ?? "").trim().replace(/^\$/, "").replace(/%$/, "").replace(/,/g, "").trim();
  if (!t || !/^-?\d*\.?\d+$/.test(t)) return { ok: false, error: rangeWords(r) };
  const v = Number(t);
  if (!Number.isFinite(v) || (r.whole && !Number.isInteger(v))) return { ok: false, error: rangeWords(r) };
  if ((r.lowOpen ? v <= r.low : v < r.low) || v > r.high) return { ok: false, error: rangeWords(r) };
  return { ok: true, value: v };
}

/** What each limit accepts: the server's own ranges (GET allowed), else the
 *  same values webui._V2_LIMIT_RANGES holds. */
export function limitRange(s: SettingsView | null, key: LimitKey): Range {
  const fallback: Record<LimitKey, [number, number]> = {
    max_entries_per_day: [1, 1000],
    max_daily_loss_usd: [0, 1_000_000_000],
    max_open_positions: [1, 1000],
  };
  const [low, high] = s?.limits.allowed?.[key] ?? fallback[key];
  return key === "max_daily_loss_usd" ? { low, high, money: true, zero: "0 turns the limit off" } : { low, high, whole: true };
}

/** webui._V2_SIZE_RANGES */
export const SIZE_RANGE: Record<SizeMode, Range> = {
  budget: { low: 0, high: 10_000_000, lowOpen: true, money: true },
  contracts: { low: 1, high: 100_000, whole: true },
  percent: { low: 0, high: 100, lowOpen: true },
};

const TICKER = /^[A-Z][A-Z0-9.]{0,9}$/; // rules._TICKER

/** A list of stock symbols: "spy, QQQ nvda" -> ["SPY", "QQQ", "NVDA"]. Any
 *  word that is not a symbol is an error here (the server refuses it too),
 *  never silently dropped. */
export function readTickers(text: string): Parsed<string[]> {
  const out: string[] = [];
  const bad: string[] = [];
  for (const p of String(text ?? "").split(/[\s,;]+/)) {
    const t = p.trim().toUpperCase().replace(/^\$/, "");
    if (!t) continue;
    if (!TICKER.test(t)) bad.push(p.trim());
    else if (!out.includes(t)) out.push(t);
  }
  if (bad.length) return { ok: false, error: `Not a stock symbol: ${bad.slice(0, 5).join(", ")}. Use symbols like SPY or NVDA, separated by commas.` };
  if (out.length > 200) return { ok: false, error: "At most 200 symbols in one list." };
  return { ok: true, value: out };
}

/** A saved list (a list, or text from older versions) as symbols. */
export function savedTickers(v: string[] | string | null | undefined): string[] {
  const parts = Array.isArray(v) ? v.map(String) : typeof v === "string" ? v.split(/[\s,;]+/) : [];
  const out: string[] = [];
  for (const p of parts) {
    const t = p.trim().toUpperCase().replace(/^\$/, "");
    if (t && TICKER.test(t) && !out.includes(t)) out.push(t);
  }
  return out;
}

// ── saying a value ──────────────────────────────────────────────────────────

export type LimitKey = "max_entries_per_day" | "max_daily_loss_usd" | "max_open_positions";
export type Book = "main" | "other";

export const SIZE_MODE_WORDS: Record<SizeMode, string> = {
  budget: "Dollars per trade",
  contracts: "Contracts per trade",
  percent: "Share of the account",
};

/** "$500 a trade", "2 contracts a trade", "2.5% of the account". */
export function sizeWords(x: SizeSpec | null | undefined, book: Book = "main"): string {
  if (!x) return book === "other" ? "Same as the main pick" : "not set";
  if (!isNum(x.value)) return "not set";
  if (x.mode === "budget") return `${money(x.value)} a trade`;
  if (x.mode === "percent") return `${x.value}% of the account`;
  return `${plural(x.value, "contract")} a trade`;
}

/** The daily loss limit: 0 and null both mean there is none. */
export const lossWords = (v: unknown) => (isNum(v) && v > 0 ? money(v) : "Off");

const BROKER_NAMES: Record<string, string> = { robinhood: "Robinhood", alpaca: "Alpaca", moomoo: "moomoo" };
export const brokerName = (id: string | null | undefined) =>
  !id ? "No broker" : Object.prototype.hasOwnProperty.call(BROKER_NAMES, id) ? BROKER_NAMES[id] : id;

/** A value as a preview line shows it, by the change's key. */
export function valueWords(key: string, v: unknown): string {
  if (key === "trade_size.main" || key === "trade_size.other")
    return sizeWords(v as SizeSpec | null, key === "trade_size.other" ? "other" : "main");
  if (key === "max_daily_loss_usd") return lossWords(v);
  if (key === "max_entries_per_day") return isNum(v) ? `${count(v)} a day` : "not set";
  if (key === "max_open_positions") return isNum(v) ? `${count(v)} at once` : "not set";
  if (key === "broker") {
    const b = (v ?? {}) as { id?: string | null; paper?: boolean | null };
    if (!b.id) return "No broker";
    return `${brokerName(b.id)}, ${b.paper == null ? "account type not known" : b.paper ? "paper-trading account" : "real-money account"}`;
  }
  if (key === "mode") return v === "live" ? "LIVE" : v === "practice" ? "Practice" : "not known";
  if (key === "signal_days") return isNum(v) ? plural(v, "signal day") : "None";
  if (key === "robinhood") return v ? "Signed in" : "Not signed in";
  if (key === "agent") return v === "running" ? "Running" : v === "stopped" ? "Stopped" : "not known";
  if (key === "this_computer" && typeof v === "string" && v) return v.charAt(0).toUpperCase() + v.slice(1);
  if (key === "source") return sourceWords(v as SourceShown | null);
  if (key === "scope") return scopeWords(v);
  if (key === "wallet") {
    const w = v as { site?: string | null; new?: boolean } | null;
    if (!w) return "None";
    return `${w.new ? "A new wallet" : "Your own wallet"}${w.site ? ` at ${w.site}` : ""}`;
  }
  if (key === "wallet_fund") return isNum(v) ? `${count(v)} sats` : "not set";
  if (key === "code" && typeof v === "string" && v) return v.charAt(0).toUpperCase() + v.slice(1);
  if (key === "expiry_sell_et") return v == null || typeof v === "string" ? etClockWords(v) : "not known";
  if (typeof v === "boolean") return v ? "On" : "Off";
  if (isNum(v)) return count(v);
  if (v == null || v === "") return "not set";
  if (typeof v === "object") return "set";
  return String(v);
}

export const DIRECTION_WORDS: Record<Direction, string> = {
  raises_risk: "This raises your risk.",
  lowers_risk: "This lowers your risk.",
  neutral: "This neither raises nor lowers your risk in a way this page can compare.",
};

/** The confirm button, named after what it does: "Raise to $1,150". */
export function confirmLabel(changes: Change[]): string {
  if (changes.length !== 1) return `Save ${plural(changes.length, "change")}`;
  const c = changes[0];
  const to = valueWords(c.key, c.new);
  if (c.key === "max_daily_loss_usd") {
    if (lossWords(c.new) === "Off") return "Turn off the daily loss limit";
    if (lossWords(c.saved_now) === "Off") return `Set the limit to ${to}`;
  }
  if (c.key === "trade_size.other" && c.new == null) return "Use the main-pick size";
  if (c.key === "expiry_sell_et") return c.new == null ? "Turn off the expiry-day sell" : `Sell at ${to}`;
  const short = to.replace(/ a (trade|day)$| at once$/, "");
  return c.direction === "raises_risk" ? `Raise to ${short}` : c.direction === "lowers_risk" ? `Lower to ${short}` : `Change to ${short}`;
}

/** The page's own reading of a change's direction, shown while the owner
 *  types ("This raises your risk"). Same rules as webui._risk_direction:
 *  bigger is riskier, and no daily loss limit is the riskiest. */
export function riskOf(key: string, old: unknown, next: unknown): Direction {
  if (key === "expiry_sell_et") {
    // "HH:MM" or null (off): off raises the risk, a later time holds the
    // contract longer into its last day (webui._expiry_direction)
    const a = old as string | null;
    const b = next as string | null;
    if (a === b) return "neutral";
    if (b == null) return "raises_risk";
    if (a == null) return "lowers_risk";
    return b > a ? "raises_risk" : "lowers_risk";
  }
  if (key === "trade_size.main" || key === "trade_size.other") {
    const a = old as SizeSpec | null;
    const b = next as SizeSpec | null;
    if (!a || !b || a.mode !== b.mode || !isNum(a.value) || !isNum(b.value) || a.value === b.value) return "neutral";
    return b.value > a.value ? "raises_risk" : "lowers_risk";
  }
  const eff = (v: unknown) => (key === "max_daily_loss_usd" ? (isNum(v) && v > 0 ? v : Infinity) : isNum(v) ? v : null);
  const a = eff(old);
  const b = eff(next);
  if (a == null || b == null || a === b) return "neutral";
  return b > a ? "raises_risk" : "lowers_risk";
}

/** What a limit's new value means, in one or two sentences. */
export function limitMeans(key: LimitKey, v: number, ctx: { mainUsd: number | null; open: number }): string {
  if (key === "max_entries_per_day")
    return `At most ${plural(v, "new trade")} a day.${ctx.mainUsd != null ? ` With your ${money(ctx.mainUsd)} trade size, that is up to ${money(v * ctx.mainUsd)} put into new trades in one day.` : ""}`;
  if (key === "max_daily_loss_usd")
    return v > 0
      ? `Once today's closed trades have lost ${money(v)}, no new trades until tomorrow. It never sells anything, and a trade already open can lose more than this before it is sold.`
      : "No daily loss limit: losses never stop new trades.";
  return `The agent holds at most ${plural(v, "position")} at a time, real and practice together. ${ctx.open} ${ctx.open === 1 ? "is" : "are"} open now${v < ctx.open ? ", so no new trades until some are sold. Nothing is sold because of this change" : ""}.`;
}

export function sizeMeans(x: SizeSpec, book: Book): string {
  const which = book === "main" ? "main-pick" : "other";
  if (!isNum(x.value)) return "";
  if (x.mode === "budget") return `Up to ${money(x.value)} on each ${which} trade, and an option can lose all of it. A signal whose single contract costs more is skipped.`;
  if (x.mode === "percent") return `${x.value}% of your account value on each ${which} trade, worked out at each entry. An option can lose all of it.`;
  return `${plural(x.value, "contract")} on every ${which} trade, whatever they cost. An option can lose all of it.`;
}

/** The limits payload: only what differs from the saved settings (the
 *  server changes only the keys sent). */
export function limitsPayload(saved: SettingsView, draft: Partial<Record<LimitKey, number>> & { main?: SizeSpec; other?: SizeSpec | null }) {
  const out: Record<string, unknown> = {};
  for (const k of ["max_entries_per_day", "max_daily_loss_usd", "max_open_positions"] as LimitKey[])
    if (draft[k] !== undefined && draft[k] !== saved.limits[k]) out[k] = draft[k];
  const size: Record<string, unknown> = {};
  if (draft.main) size.main = { mode: draft.main.mode, value: draft.main.value };
  if (draft.other !== undefined) size.other = draft.other == null ? { mode: "same" } : { mode: draft.other.mode, value: draft.other.value };
  if (Object.keys(size).length) out.trade_size = size;
  return out;
}

// ── words from the classic pages ────────────────────────────────────────────

/** The agent's own messages were written for the classic dashboard
 *  ("dashboard → Settings → Signal source"). Here they say where that is in
 *  /v2 instead. */
export function v2Words(text: string): string {
  const to = String.raw`\s*(?:→|->)\s*`;
  return String(text ?? "")
    .replace(new RegExp(String.raw`\s*\(dashboard${to}Settings${to}Signal\s+source\)`, "gi"), " (Signals & pass)")
    .replace(new RegExp(String.raw`\bin the dashboard\s*\(Settings${to}Signal\s+source\)`, "gi"), "on Signals & pass")
    .replace(new RegExp(String.raw`\bthe dashboard\s*\(Settings${to}Signal\s+source\)`, "gi"), "Signals & pass")
    .replace(new RegExp(String.raw`\bSettings${to}Signal\s+source`, "gi"), "Signals & pass")
    .replace(new RegExp(String.raw`\bin Settings${to}Broker`, "gi"), "in Settings, under Connections")
    .replace(new RegExp(String.raw`\s*\(dashboard${to}[^)]*\)`, "gi"), " (Settings)");
}

// ── settings this page can't vouch for ───────────────────────────────────────

/** What to say beside settings values this page read earlier but can't
 *  vouch for now: a write's outcome is not known (it may have been saved),
 *  or the last read failed. "" when the values are as last read. Pages with
 *  no view at all already say why. */
export function settingsDoubt(hasView: boolean, error: string, unsure: boolean): string {
  if (!hasView) return "";
  if (unsure)
    return `Not known: this page could not confirm your last change, so the values below may not be what is saved now. ${error || "Reading them again…"}`;
  if (error) return `The values below may be out of date: this page could not read them again. ${error}`;
  return "";
}

// ── the going-LIVE tick box ──────────────────────────────────────────────────

/** The tick box beside "Go LIVE" confirms ONE preview: it is remembered as
 *  the token of the preview it was ticked for, so any new preview (Review
 *  again, or the server answering with a fresh one) starts unticked. */
export const ackGiven = (tickedFor: string | null, token: string): boolean => tickedFor !== null && tickedFor === token;

// ── a signal-day purchase ───────────────────────────────────────────────────

/** Where one purchase stands, from GET /api/v2/settings' signals.purchases
 *  (read after the pass check, which files a paid invoice's pass). Never
 *  from the pass check's own "active": that is true of any pass on file,
 *  so an older pass would make an unpaid invoice look paid. */
export function purchaseFate(id: string, p: Purchases | null | undefined): "paid" | "waiting" | "gone" | "unknown" {
  if (!p || !Array.isArray(p.paid) || !Array.isArray(p.waiting)) return "unknown";
  if (p.paid.includes(id)) return "paid";
  if (p.waiting.includes(id)) return "waiting";
  return "gone"; // the pass check dropped it: the invoice expired unpaid
}

// ── the Robinhood sign-in, as the agent saw it end ──────────────────────────

/** Where Robinhood takes applications for options trading (the classic
 *  wizard's warning links here with the full account number; /v2 never
 *  has that number, only its last 4). */
export const RH_OPTIONS_URL = "https://applink.robinhood.com/upgrade_options";

export type SignInSay = { kind: "wait" } | { kind: "done" | "warn" | "failed"; text: string };

/** What to tell the owner about the sign-in started at `since` (the
 *  confirmed POST's signin_seq), from GET /api/v2/settings' broker.signin.
 *  Watching the seq, not the settings: signing in again to the same
 *  account, or with no account allowed, changes no setting at all. */
export function signInOutcome(since: number, end: SignInEnd | null | undefined): SignInSay {
  if (!end || typeof end.seq !== "number" || end.seq <= since) return { kind: "wait" };
  const acct = end.account_mask ? ` ····${end.account_mask}` : "";
  if (end.connected !== true)
    return {
      kind: "failed",
      text: end.error
        ? `The Robinhood sign-in did not finish (${end.error}). Nothing was changed. Try again.`
        : "You signed in, but Robinhood has no active account that allows agentic trading, so nothing was changed. Turn on agentic trading for an account at Robinhood, then connect again.",
    };
  if (end.options_off)
    return {
      kind: "warn",
      text: `Connected. Broker: Robinhood${acct}. Options trading is not turned on in this account, so Robinhood rejects the agent's orders until you apply for it at Robinhood.`,
    };
  return { kind: "done", text: `Connected. Broker: Robinhood${acct}.` };
}

// ── a tip ───────────────────────────────────────────────────────────────────

/** A tip's payment code is past its validity (plus a minute's grace) and
 *  AgentHC never said it was paid or expired: this page can no longer say
 *  which, so it says both possibilities. */
export const tipLapsed = (shownAt: number, validS: number, now: number): boolean =>
  now > shownAt + (Number.isFinite(validS) && validS > 0 ? validS : 3600) * 1000 + 60000;

// ── the agreement ───────────────────────────────────────────────────────────

/** The agreement on file, in words (GET /api/v2/settings' consent). */
export function agreementWords(c: { accepted: boolean; on_file: boolean; accepted_at: string | null; terms_version: string | null; current_terms_version: string } | null | undefined): string {
  if (!c) return "Not known.";
  const when = c.accepted_at && /^\d{4}-\d{2}-\d{2}/.test(c.accepted_at) ? ` on ${c.accepted_at.slice(0, 10)}` : "";
  if (c.accepted) return `You accepted it${when}${c.terms_version ? ` (terms version ${c.terms_version})` : ""}. It is on file on this computer.`;
  if (c.on_file)
    return `The agreement changed since you accepted it${when}${c.terms_version ? ` (version ${c.terms_version})` : ""}: the current one is version ${c.current_terms_version}, and the agent needs it accepted in setup.`;
  return "No accepted agreement is on file on this computer: the agent needs it accepted in setup.";
}

// ── a code change ───────────────────────────────────────────────────────────

/** The sha256 (hex) of the diff exactly as this page shows it: what an
 *  Apply is bound to (the server applies only the proposal whose diff has
 *  it). null when this browser can't work it out (then nothing is applied). */
export async function diffSha256(text: string): Promise<string | null> {
  const subtle = (globalThis as { crypto?: Crypto }).crypto?.subtle;
  if (!subtle) return null;
  try {
    const d = new Uint8Array(await subtle.digest("SHA-256", new TextEncoder().encode(text)));
    return Array.from(d, (b) => b.toString(16).padStart(2, "0")).join("");
  } catch {
    return null;
  }
}

// ── the expiry-day times ────────────────────────────────────────────────────

/** An Eastern time of day the expiry sell or its alert accepts: "15:40",
 *  "9:45" → "09:45"; from 09:30 to 15:59 (agent._expiry_setting). */
export function readEtTime(text: string): Parsed<string> {
  const m = /^\s*([01]?\d|2[0-3]):([0-5]\d)\s*$/.exec(String(text ?? ""));
  const mins = m ? Number(m[1]) * 60 + Number(m[2]) : -1;
  if (mins < 9 * 60 + 30 || mins >= 16 * 60) return { ok: false, error: "Enter a time from 09:30 to 15:59 (Eastern), like 15:40." };
  return { ok: true, value: `${String(Math.floor(mins / 60)).padStart(2, "0")}:${String(mins % 60).padStart(2, "0")}` };
}

/** "3:50 PM ET" for a saved "15:50"; "Off" for none. */
export function etClockWords(hhmm: string | null | undefined): string {
  if (!hhmm) return "Off";
  const m = /^(\d{2}):(\d{2})$/.exec(hhmm);
  if (!m) return "not known";
  const h = Number(m[1]);
  return `${h % 12 || 12}:${m[2]} ${h < 12 ? "AM" : "PM"} ET`;
}

/** An expiry-day time for a save (the sell time on the limits route, the
 *  alert on the alerts route): only when it differs from what is saved
 *  (null = off). Undefined: leave it as it is. */
export function expiryTimePayload(saved: string | null | undefined, off: boolean, text: string): Parsed<string | undefined> {
  if (off) return { ok: true, value: saved == null ? undefined : "off" };
  const t = readEtTime(text);
  if (!t.ok) return t;
  return { ok: true, value: t.value === saved ? undefined : t.value };
}

export const expiryAlertPayload = expiryTimePayload;

// ── trade size: the wizard's words, and the budget hint ─────────────────────

/** The classic sizing step's notice (wizard Sizing), said once in /v2: in
 *  the trade-size confirm dialog. */
export const SIZING_DISCLAIMER =
  "Nobody here is an investment adviser and none of this software can advise position sizing. This number is your decision, made with money you can afford to lose entirely.";

/** The size a budget hint suggests, as the trade-size form takes it. */
export const hintSize = (h: BudgetHint): SizeSpec => ({ mode: "budget", value: h.suggest });

/** The limits save "Use $S" leads to: exactly what typing $S in that book's
 *  trade-size form sends (the classic hint button's "set [other ]budget S"). */
export const hintPayload = (h: BudgetHint) => ({ trade_size: { [h.book]: { mode: "budget", value: h.suggest } } });

/** One budget hint in words (agent._budget_hints, as the classic "why no
 *  trades" panel said it). */
export function hintWords(h: BudgetHint): string {
  const which = h.book === "other" ? "other-trades" : "main-pick";
  const cost = h.max_cost > h.min_cost ? `${money(h.min_cost)} to ${money(h.max_cost)}` : money(h.min_cost);
  return `${plural(h.skipped, "signal")} today cost more than your ${money(h.budget)} ${which} size: one contract was ${cost}. ${money(h.suggest)} would have taken ${h.would_take} of them.`;
}

// ── the broker's own warning ────────────────────────────────────────────────

/** What the broker row says about the agent's account beyond up/down: the
 *  classic wizard's Robinhood warning (options not turned on), as a code. */
export function brokerWarning(v: SettingsView | null | undefined): string {
  if (v?.broker?.warning === "options_off")
    return "Options trading is not turned on in this Robinhood account, so Robinhood rejects the agent's orders until you apply for it at Robinhood.";
  return "";
}

// ── the signal source ───────────────────────────────────────────────────────

export interface SourceShown {
  source: string | null;
  site?: string | null;
}

/** "AgentHC Day Trade Ideas", "Your own commands file", "A JSON feed at x". */
export function sourceWords(v: SourceShown | null | undefined): string {
  if (!v?.source) return "None chosen";
  if (v.source === "agenthc") return "AgentHC Day Trade Ideas";
  if (v.source === "manual") return "Your own commands file (commands.jsonl)";
  if (v.source === "url") return v.site ? `A JSON feed at ${v.site}` : "A JSON feed";
  return v.source;
}

export const scopeWords = (v: unknown): string => (v === "all" ? "The main pick and other trades" : v === "main" ? "The main pick only" : "not known");

/** A feed or wallet address the agent accepts (the classic form's rule). */
export function readAddress(text: string): Parsed<string> {
  const t = String(text ?? "").trim();
  if (/^https:\/\/[^\s/]+/.test(t) || /^http:\/\/(127\.0\.0\.1|localhost)([:/]|$)/.test(t)) return { ok: true, value: t };
  return { ok: false, error: "Enter an address starting with https:// (or one on this computer)." };
}

/** Sats to add to the wallet: a whole number from 1,000 up to the wallet's
 *  invoice cap when it has said one (the server refuses anything else). */
export function readSats(text: string, cap: number | null | undefined): Parsed {
  return readNumber(text, { low: 1000, high: cap && cap >= 1000 ? cap : 100_000_000, whole: true });
}
