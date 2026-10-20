// Same-origin JSON calls to the agent's own server (127.0.0.1 only).
// The session cookie rides along automatically.

export type Json = Record<string, any>;

export class LoginExpired extends Error {}

export async function api<T = Json>(path: string, body?: unknown): Promise<T> {
  const r = await fetch(
    path,
    body !== undefined
      ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }
      : {},
  );
  if (r.status === 401) throw new LoginExpired("login expired");
  return (await r.json()) as T;
}

/** Like api(), but never throws: a hard timeout and every failure in words.
 *  Used by the update buttons, which can take a minute. */
export async function apiTimed(path: string, body: unknown, ms: number): Promise<Json> {
  const ctl = new AbortController();
  const to = setTimeout(() => ctl.abort(), ms);
  try {
    const r = await fetch(
      path,
      body !== undefined
        ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), signal: ctl.signal }
        : { signal: ctl.signal },
    );
    if (r.status === 401 || r.status === 403)
      return {
        ok: false,
        error:
          "Your dashboard login expired. Open the link from `python agent.py link` (or restart the agent) and try again.",
      };
    try {
      return await r.json();
    } catch {
      return { ok: false, error: `The agent answered unexpectedly (HTTP ${r.status}). Restart it and try again.` };
    }
  } catch (e: any) {
    return {
      ok: false,
      error:
        e && e.name === "AbortError"
          ? `No answer after ${Math.round(ms / 1000)}s. Your network may be blocking GitHub. Run \`python agent.py netcheck\` in the agent folder, or re-run the installer.`
          : "Could not reach the agent on this computer. Is it still running? Restart it, then reload this page.",
    };
  } finally {
    clearTimeout(to);
  }
}

// ── shapes the server returns ────────────────────────────────────────────
export interface StatusResp {
  mode: "LIVE" | "DRY-RUN" | "OFFLINE" | string;
  paused: boolean;
  fields: Record<string, string>;
  cash_warning?: string;
  problems?: Problem[];
  budget_hints?: BudgetHint[];
  would_have?: WouldHave | null;
}
export interface WouldHaveRow {
  date: string;
  entry_time?: string;
  exit_time?: string;
  contract: string;
  hold_minutes?: number | null;
  paper_pnl_pct: number | null;
  exit_kind?: string;
  would_take: boolean;
  skip_reason?: string;
}
export interface WouldHave {
  days: number;
  rows: WouldHaveRow[];
  would_take: number;
  wins: number;
  breakeven: number;
  losses: number;
  avg_paper_pnl_pct: number | null;
  updated_at?: number | null;
  error?: string;
  unavailable?: boolean;
}
export interface BudgetHint {
  book: "main" | "shadow";
  budget: number;
  skipped: number;
  min_cost: number;
  max_cost: number;
  suggest: number;
  would_take: number;
  command: string;
}
export interface Problem {
  kind: string;
  count: number;
  last?: string;
  hint?: string;
  contract?: string;
  at?: string;
}
export interface Trade {
  ts?: string;
  action?: string;
  contract?: string;
  dry?: boolean;
  est_pnl_usd?: number | null;
  reason?: string;
  [k: string]: unknown;
}
export interface Proposal {
  pending?: boolean;
  explanation?: string;
  warnings?: string[];
  diff?: string;
}
export interface WizardState {
  disclaimer: string;
  consent: boolean;
  source: string | null;
  broker: string;
  rh_warning: string;
  llm: boolean;
  policy_blocks_entries?: boolean; // policy saved but no AI: every entry would be skipped
  sizing: boolean;
  include_other_trades?: boolean;
  sizing_detail: { main: { mode: string; value: unknown } | null; other: { mode: string; value: unknown } | null };
  safety: boolean;
  notes: Record<string, string>;
  wallet: { connected: boolean; page: string };
  recurring: boolean;
  pass: boolean;
  autostart?: boolean;
  rules?: {
    rule_tickers?: string[] | null;
    rule_skip_tickers?: string[] | null;
    rule_sides?: string | null;
    rule_min_strike?: number | null;
    rule_skip_0dte?: boolean | null;
    rule_loss_streak?: number | null;
  };
  rules_summary?: string;
  policy_on?: boolean;
  running?: boolean;
  keep_awake?: boolean;
}
