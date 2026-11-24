// The budget guide's sums (src/v2/lib/budgetGuide.ts) over the generated
// backtest grid. Node's own test runner; types stripped.
//
//   node --experimental-strip-types --test webui_app/tests/budget_guide.test.mjs
import assert from "node:assert/strict";
import test from "node:test";

import { CAPITAL_SHARE, RISK_LEVELS, TIERS, pickBudgets, worstOf } from "../src/v2/lib/budgetGuide.ts";
import { GUIDE_MAIN, GUIDE_OTHER } from "../src/v2/lib/budgetGuide.data.ts";

test("grid rows are sorted, whole-dollar budgets with sane shares", () => {
  for (const rows of [GUIDE_MAIN, GUIDE_OTHER]) {
    assert.ok(rows.length > 10);
    rows.forEach((r, i) => {
      assert.equal(r.length, 5);
      assert.ok(Number.isInteger(r[0]) && r[0] > 0);
      assert.ok(r[1] >= 0 && r[1] <= 1);
      if (i) assert.ok(r[0] > rows[i - 1][0]);
    });
  }
});

test("each tier's worst day stays within its risk level, capital within the cap", () => {
  for (const lvl of RISK_LEVELS) {
    for (const a of TIERS) {
      const p = pickBudgets((a * lvl.pct) / 100, a);
      assert.ok(p.worstDay <= (a * lvl.pct) / 100 + 1e-9, `${lvl.key} ${a} worst day ${p.worstDay}`);
      assert.ok(p.capital <= a * CAPITAL_SHARE + 1e-9, `${lvl.key} ${a} capital ${p.capital}`);
    }
  }
});

test("budgets never shrink as the account or the risk level grows", () => {
  for (const lvl of RISK_LEVELS) {
    let prev = { m: 0, o: 0 };
    for (const a of TIERS) {
      const p = pickBudgets((a * lvl.pct) / 100, a);
      const m = p.main ? p.main[0] : 0;
      const o = p.other ? p.other[0] : 0;
      assert.ok(m >= prev.m && o >= prev.o, `${lvl.key} ${a}`);
      prev = { m, o };
    }
  }
  const a = 20000;
  const [lo, mid, hi] = RISK_LEVELS.map((l) => pickBudgets((a * l.pct) / 100, a));
  assert.ok(lo.main[0] <= mid.main[0] && mid.main[0] <= hi.main[0]);
});

test("a tiny daily loss fits no budget: nothing is suggested", () => {
  const p = pickBudgets(10);
  assert.equal(p.main, null);
  assert.equal(p.other, null);
  assert.equal(p.worstDay, 0);
});

test("a stated daily loss with no account size is split half and half", () => {
  const p = pickBudgets(2000);
  assert.ok(worstOf(p.main) <= 1000 && p.main[0] <= 1000);
  assert.ok(worstOf(p.other) <= 1000);
});
