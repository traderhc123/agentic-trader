// Formatting for the /v2 dashboard: money, market (ET) times, counts.
// Pure functions with no imports that exist at run time, so the Node tests
// (webui_app/tests/) can load this file as it is.
//
// One rule: a value that is null (the agent did not record it) is never
// turned into a number. Every function here returns words for it instead.
import type { Contract, Iso } from "../../lib/viewModel";

export const UNKNOWN = "not known yet";
const ET = "America/New_York";

const isNum = (n: unknown): n is number => typeof n === "number" && Number.isFinite(n);

/** "$1,983.00" (no sign; use signed() for results). */
export function usd(n: number | null | undefined, cents = 2): string {
  if (!isNum(n)) return UNKNOWN;
  return (
    "$" +
    Math.abs(n).toLocaleString("en-US", { minimumFractionDigits: cents, maximumFractionDigits: cents })
  );
}

/** "+$104.00", "−$84.00", "$0.00": a result always carries its sign. */
export function signed(n: number | null | undefined, cents = 2): string {
  if (!isNum(n)) return UNKNOWN;
  return (n > 0 ? "+" : n < 0 ? "−" : "") + usd(n, cents);
}

/** CSS class for a result: green only for a gain, red only for a loss. */
export function tone(n: number | null | undefined): "gain" | "loss" | "" {
  return !isNum(n) || n === 0 ? "" : n > 0 ? "gain" : "loss";
}

export function count(n: number | null | undefined): string {
  return isNum(n) ? n.toLocaleString("en-US") : UNKNOWN;
}

/** "3 real positions", "1 trade". */
export function plural(n: number | null | undefined, word: string, many?: string): string {
  if (!isNum(n)) return `an unknown number of ${many ?? word + "s"}`;
  return `${n.toLocaleString("en-US")} ${n === 1 ? word : many ?? word + "s"}`;
}

export function strike(n: number): string {
  return (
    "$" +
    n.toLocaleString("en-US", { minimumFractionDigits: Number.isInteger(n) ? 0 : 2, maximumFractionDigits: 2 })
  );
}

/** "SPY $700 call". `fallback` is the agent's own text for a row whose
 *  contract could not be read. */
export function contractName(c: Contract | null, fallback?: string | null): string {
  if (!c) return fallback ? clip(fallback, 60) : "a contract the agent could not name";
  return `${c.symbol} ${strike(c.strike)} ${c.side === "C" ? "call" : "put"}`;
}

export function clip(s: string, n: number): string {
  return s.length > n ? s.slice(0, n - 1) + "…" : s;
}

function parse(iso: Iso | null | undefined): Date | null {
  if (typeof iso !== "string" || !iso) return null;
  const d = new Date(iso);
  return isNaN(d.getTime()) ? null : d;
}

function parts(d: Date, opts: Intl.DateTimeFormatOptions): Record<string, string> {
  const out: Record<string, string> = {};
  for (const p of new Intl.DateTimeFormat("en-US", { timeZone: ET, ...opts }).formatToParts(d)) out[p.type] = p.value;
  return out;
}

/** "2:41 PM ET": market time, always with the suffix. */
export function etTime(iso: Iso | null | undefined): string {
  const d = parse(iso);
  if (!d) return UNKNOWN;
  const p = parts(d, { hour: "numeric", minute: "2-digit", hour12: true });
  return `${p.hour}:${p.minute} ${p.dayPeriod} ET`;
}

/** "2026-11-09": the ET calendar day of an instant ("" when unreadable). */
export function etDayKey(iso: Iso | null | undefined): string {
  const d = parse(iso);
  if (!d) return "";
  const p = parts(d, { year: "numeric", month: "2-digit", day: "2-digit" });
  return `${p.year}-${p.month}-${p.day}`;
}

/** "Mon, Nov 9" for a "2026-11-09" day key. */
export function dayLong(day: string | null | undefined): string {
  if (typeof day !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(day)) return "date not recorded";
  const d = new Date(day + "T12:00:00Z");
  if (isNaN(d.getTime())) return "date not recorded";
  return d.toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric", timeZone: "UTC" });
}

export function dayShort(day: string): string {
  const d = new Date(day + "T12:00:00Z");
  if (isNaN(d.getTime())) return "";
  return d.toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: "UTC" });
}

/** "Fri, Nov 6 at 9:49 AM ET"; "Today at 9:41 AM ET" when `today` matches. */
export function etWhen(iso: Iso | null | undefined, today?: string): string {
  const key = etDayKey(iso);
  if (!key) return "time not recorded";
  return `${key === today ? "Today" : dayLong(key)} at ${etTime(iso)}`;
}

/** Seconds between two instants (b - a), or null. */
export function secondsBetween(a: Iso | null | undefined, b: Iso | null | undefined): number | null {
  const x = parse(a);
  const y = parse(b);
  return x && y ? (y.getTime() - x.getTime()) / 1000 : null;
}

/** "4 s", "3 min", "2 h 5 min": how long ago, for a small counter. */
export function ago(seconds: number | null): string {
  if (!isNum(seconds)) return UNKNOWN;
  const s = Math.max(0, Math.round(seconds));
  if (s < 90) return `${s} s`;
  if (s < 5400) return `${Math.round(s / 60)} min`;
  return span(s);
}

/** "1 h 19 min" */
export function span(seconds: number): string {
  const m = Math.max(0, Math.floor(seconds / 60));
  const h = Math.floor(m / 60);
  if (h >= 48) return `${Math.floor(h / 24)} days`;
  return h ? `${h} h ${m % 60} min` : `${m} min`;
}

/** The instant `seconds` after an ISO time, as ISO. */
export function addSeconds(iso: Iso, seconds: number): Iso {
  const d = parse(iso);
  return d ? new Date(d.getTime() + seconds * 1000).toISOString() : iso;
}
