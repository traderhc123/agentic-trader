// The Results chart's numbers: each book's running total of closed trades,
// one point per market day (ET), for a chosen period. Pure functions with no
// imports that exist at run time, so the Node tests can load this file.
//
// The money is the view-model's: each book's `days` (every ET day it closed a
// trade, from the whole trade log) are only added up here, in whole cents, so
// a period's last point is exactly the figure the panels show for it
// (`last_7_days` for 7 days, `total` for all time).
import type { Book, Status } from "../../lib/viewModel";

export type Range = "7d" | "30d" | "all";
export const RANGES: readonly (readonly [Range, string])[] = [
  ["7d", "7 days"],
  ["30d", "30 days"],
  ["all", "All time"],
];
export const isRange = (v: unknown): v is Range => v === "7d" || v === "30d" || v === "all";

export type BookKey = "real" | "practice";
export const BOOKS: readonly BookKey[] = ["real", "practice"];

export interface DayPoint {
  day: string;
  /** that day's result in this book; null when it closed nothing that day */
  pnl: number | null;
  closed: number;
  /** running total from the start of the period, that day included; null
   *  before this book's first closed trade */
  cum: number | null;
}

export interface ChartModel {
  range: Range;
  /** every weekday of the period, oldest first (plus any weekend day with a
   *  closed trade), as ET "YYYY-MM-DD" day keys */
  days: string[];
  real: DayPoint[];
  practice: DayPoint[];
  /** neither book has a measured closed trade, ever */
  empty: boolean;
  /** neither book closed a trade inside this period */
  quiet: boolean;
  /** each book's period total (the last point), null for a book with no line */
  end: Record<BookKey, number | null>;
  /** All time only: the part of a book's total that has no readable date and
   *  so is not on the chart (0 when everything is plotted) */
  undated: Record<BookKey, number>;
}

const DAY = 86_400_000;
const KEY = /^\d{4}-\d{2}-\d{2}$/;
const cents = (n: number) => Math.round(n * 100);

function ms(day: string): number {
  return Date.parse(day + "T00:00:00Z");
}
function key(t: number): string {
  return new Date(t).toISOString().slice(0, 10);
}
/** `day` moved by n calendar days. */
export function addDays(day: string, n: number): string {
  return key(ms(day) + n * DAY);
}
const weekend = (t: number) => {
  const w = new Date(t).getUTCDay();
  return w === 0 || w === 6;
};

/** The first day of a period ending `today`: the same seven calendar days as
 *  the panels' "Last 7 days", today included. */
export function rangeStart(range: Range, today: string, first: string | null): string {
  if (range === "7d") return addDays(today, -6);
  if (range === "30d") return addDays(today, -29);
  return first ?? addDays(today, -6);
}

function readDays(B: Book): Map<string, { c: number; closed: number }> {
  const m = new Map<string, { c: number; closed: number }>();
  for (const d of B.days ?? []) {
    if (typeof d?.day !== "string" || !KEY.test(d.day) || !Number.isFinite(d.pnl)) continue;
    const was = m.get(d.day);
    m.set(d.day, { c: (was?.c ?? 0) + cents(d.pnl), closed: (was?.closed ?? 0) + (d.closed || 0) });
  }
  return m;
}

export function chartModel(s: Status, range: Range, today: string): ChartModel {
  const data = { real: readDays(s.results.real), practice: readDays(s.results.practice) };
  const all = [...data.real.keys(), ...data.practice.keys()].sort();
  const first = all.length ? all[0] : null;
  const last = all.length ? all[all.length - 1] : null;
  const to = KEY.test(today) ? (last && last > today && range === "all" ? last : today) : last ?? "1970-01-01";
  const from = rangeStart(range, to, first);
  const days: string[] = [];
  for (let t = ms(from), end = ms(to); t <= end; t += DAY)
    if (!weekend(t) || data.real.has(key(t)) || data.practice.has(key(t))) days.push(key(t));
  // a period that is only a weekend still has its day
  if (!days.length) days.push(to);

  const run = (k: BookKey): DayPoint[] => {
    const m = data[k];
    const firstEver = [...m.keys()].sort()[0];
    let cum: number | null = null;
    return days.map((day) => {
      const x = m.get(day);
      // the line starts at the period's first day, or at the book's first
      // closed trade when that comes later
      if (cum == null && firstEver != null && day >= firstEver) cum = 0;
      if (x && cum != null) cum += x.c;
      return { day, pnl: x ? x.c / 100 : null, closed: x ? x.closed : 0, cum: cum == null ? null : cum / 100 };
    });
  };
  const real = run("real");
  const practice = run("practice");
  const endOf = (pts: DayPoint[]) => {
    const p = pts[pts.length - 1];
    return p && p.cum != null ? p.cum : null;
  };
  const undated = (k: BookKey) => {
    if (range !== "all") return 0;
    const sum = [...data[k].values()].reduce((a, x) => a + x.c, 0);
    const total = s.results[k].total;
    return Number.isFinite(total) ? (cents(total) - sum) / 100 : 0;
  };
  return {
    range,
    days,
    real,
    practice,
    empty: !all.length,
    quiet: ![...real, ...practice].some((p) => p.pnl != null),
    end: { real: endOf(real), practice: endOf(practice) },
    undated: { real: undated("real"), practice: undated("practice") },
  };
}

/** Round axis steps covering lo..hi and always $0. */
export function niceTicks(lo: number, hi: number): number[] {
  lo = Math.min(0, lo);
  hi = Math.max(0, hi);
  if (hi - lo < 1e-9) {
    lo = -100;
    hi = 100;
  }
  const raw = (hi - lo) / 4;
  const p = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1, 2, 2.5, 5, 10].map((x) => x * p).find((x) => x >= raw) ?? 10 * p;
  const a = Math.floor(lo / step) * step;
  const b = Math.ceil(hi / step) * step;
  const out: number[] = [];
  for (let v = a; v <= b + step / 1e6; v += step) out.push(Math.round(v * 100) / 100);
  return out;
}

/** "$1,200", "−$250", "$12.5k", "$1.5M": short labels for the money axis. */
export function axisMoney(v: number): string {
  const a = Math.abs(v);
  const short = (x: number, unit: string) => x.toLocaleString("en-US", { maximumFractionDigits: 1 }) + unit;
  const n =
    a >= 1e6
      ? short(a / 1e6, "M")
      : a >= 10000
        ? short(a / 1000, "k")
        : a.toLocaleString("en-US", { maximumFractionDigits: a < 10 ? 2 : 0 });
  return (v < 0 ? "−" : "") + "$" + n;
}
