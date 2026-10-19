export function money(v: number): string {
  return (v >= 0 ? "+$" : "−$") + Math.abs(v).toFixed(2);
}

/** "$290" for whole strikes, "$717.50" otherwise. */
export function fmtStrike(v: number | string): string {
  const n = Number(v);
  if (!isFinite(n)) return String(v);
  return "$" + (Number.isInteger(n) ? String(n) : n.toFixed(2));
}

export function shortDate(d: Date): string {
  return d.toLocaleDateString([], { month: "short", day: "numeric" });
}
export function hhmm(d: Date): string {
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
}
export function isToday(d: Date): boolean {
  return new Date().toDateString() === d.toDateString();
}
/** 24h HH:MM for today, "Oct 14" for older. */
export function whenShort(ts?: string): string {
  const d = new Date(ts || "");
  if (isNaN(d.getTime())) return ts || "";
  return isToday(d) ? hhmm(d) : shortDate(d);
}

export interface Contract {
  sym: string;
  expiry: Date | null;
  expiryRaw: string;
  strike: string;
  right: "CALL" | "PUT" | null;
  raw: string;
}

/** "NVDA 2026-10-16 $215 PUT" → parts; anything else → raw only. */
export function parseContract(c?: string): Contract {
  const raw = String(c || "");
  const m = raw.match(/^(\S+)\s+(\d{4})-(\d{2})-(\d{2})\s+\$?([\d.]+)\s+(CALL|PUT)$/i);
  if (!m) return { sym: raw, expiry: null, expiryRaw: "", strike: "", right: null, raw };
  return {
    sym: m[1],
    expiry: new Date(+m[2], +m[3] - 1, +m[4]),
    expiryRaw: `${m[2]}-${m[3]}-${m[4]}`,
    strike: fmtStrike(m[5]),
    right: m[6].toUpperCase() as "CALL" | "PUT",
    raw,
  };
}

/** Open positions arrive as "SYM|YYYY-MM-DD|strike|C" joined by ", ". */
export function parsePositions(s?: string): Contract[] {
  const pos = s || "none";
  if (pos === "none") return [];
  return pos.split(", ").map((p) => {
    const a = p.split("|");
    if (a.length < 4) return { sym: p, expiry: null, expiryRaw: "", strike: "", right: null, raw: p };
    const d = new Date(a[1] + "T12:00:00");
    return {
      sym: a[0],
      expiry: isNaN(d.getTime()) ? null : d,
      expiryRaw: a[1],
      strike: fmtStrike(a[2]),
      right: a[3] === "C" ? "CALL" : "PUT",
      raw: p,
    };
  });
}

/** Server copy says "4 signal day(s)" / "1 contract(s)": pluralise from n. */
export function pluralFix(s: string): string {
  return String(s).replace(/(\d[\d,]*)(\s+(?:[a-z-]+\s+)?)(day|contract)\(s\)/gi, (_m, n, mid, w) =>
    n + mid + w + (n === "1" ? "" : "s"),
  );
}

export function cap1(s: string): string {
  return s.charAt(0).toUpperCase() + s.slice(1);
}
