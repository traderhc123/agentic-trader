// The budget guide's sums (views/BudgetGuide.tsx shows them). Plain
// TypeScript, no React, so tests/budget_guide.test.mjs runs it under Node.
import { GUIDE_MAIN, GUIDE_OTHER, type GuideRow } from "./budgetGuide.data.ts";

export const RISK_LEVELS = [
  { key: "low", label: "Conservative", pct: 5 },
  { key: "mid", label: "Moderate", pct: 10 },
  { key: "high", label: "Aggressive", pct: 20 },
] as const;
export type RiskKey = (typeof RISK_LEVELS)[number]["key"];

export const TIERS = [5000, 10000, 15000, 20000, 25000, 30000, 40000, 50000, 75000, 100000];

/** Money in open positions at once stays under this share of the account. */
export const CAPITAL_SHARE = 0.75;

export type Pick = {
  main: GuideRow | null;
  other: GuideRow | null;
  worstDay: number;
  drawdown: number;
  capital: number;
};

/** The largest budget whose worst day stayed within `limit` — scanning up
 * from the smallest and stopping at the first that did not, so a budget is
 * never picked past one that broke the limit. */
/** A budget's worst day: the worst seen, and never less than one budget. */
export const worstOf = (r: GuideRow): number => Math.max(r[0], r[2]);

function largestWithin(rows: GuideRow[], limit: number, capLimit = Infinity): GuideRow | null {
  let ok: GuideRow | null = null;
  for (const r of rows) {
    // one trade can lose its whole budget, so a book's worst day is never
    // counted as less than one budget (a few small budgets had a gain as
    // their worst day only because they took so few trades)
    if (worstOf(r) > limit || r[4] > capLimit) break;
    ok = r;
  }
  return ok;
}

/** The two budgets for a most-you'd-lose-in-a-day figure, split half and
 * half between the main pick and the other trades. With an account size, the
 * money in open positions at once also stays under CAPITAL_SHARE of it. */
export function pickBudgets(dailyRisk: number, account?: number): Pick {
  const half = dailyRisk / 2;
  const main = largestWithin(GUIDE_MAIN, half);
  const capLeft = account ? account * CAPITAL_SHARE - (main ? main[4] : 0) : Infinity;
  const other = largestWithin(GUIDE_OTHER, half, capLeft);
  const sum = (i: 3 | 4) => Math.max(0, main ? main[i] : 0) + Math.max(0, other ? other[i] : 0);
  const worstDay = (main ? worstOf(main) : 0) + (other ? worstOf(other) : 0);
  return { main, other, worstDay, drawdown: sum(3), capital: sum(4) };
}
