// Unit checks for the /v2 dashboard's words and derived state. No test
// framework and no dependencies: Node's own test runner, loading the
// TypeScript sources as they are (Node 22.6+ strips the types).
//
//   node --experimental-strip-types --test webui_app/tests/guardian.test.mjs
//
// tests/test_webui_v2.py runs this under pytest (and skips when Node is
// missing or too old). It hands over, in the folder named by
// GUARDIAN_FIXTURES, one view-model JSON per dashboard state plus
// codes.json (the code lists view_model.py declares); without that folder
// only the table checks run.
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";

import { ENTRY_CHECKS, REASON_CODES } from "../src/lib/viewModel.ts";
import {
  ANSWER_LOOK,
  CHECK_FALLBACK,
  CHECK_LABELS,
  FALLBACK_TITLE,
  REASONS,
  UNMANAGED_SHORT,
  UNMANAGED_WHY,
  answerLook,
  answerWhy,
  checkLabel,
  describe,
  nextSay,
  tagOf,
} from "../src/v2/lib/copy.ts";
import { asOf, bandModel, dayTally, isStale, linkOf, nextFor, notes, paidFor, statement, unwatched, vitals } from "../src/v2/lib/derive.ts";
import { UNKNOWN, ago, contractName, dayLong, etDayKey, etTime, etWhen, plural, signed, span, tone, usd } from "../src/v2/lib/format.ts";

const BAD = /undefined|NaN|\[object|\bnull\b|Infinity/;
const clean = (s, where) => {
  assert.equal(typeof s, "string", where);
  assert.ok(s.trim().length > 0, `${where}: empty`);
  assert.ok(!BAD.test(s), `${where}: "${s}"`);
};

const row = (over = {}) => ({
  n: 1, at: "2026-11-09T18:18:00Z", action: "skip_budget", known: true,
  contract: { symbol: "AMZN", expiry: "2026-11-13", strike: 265, side: "P" },
  contract_text: "AMZN 2026-11-13 $265.0 P", book: "other", real: false, mode_at_time: "practice",
  order_sent: false, filled: false, qty: null, fill_price: null, quote_price: null, est_pnl_usd: null,
  cause: null, stopped_at_check: "trade_size", reason_code: "over_trade_size", reason_params: {},
  reason_text: null, hint: null, ...over,
});
const PARAMS = {
  cap: 4, max_open: 8, max_loss_usd: 300, loss_usd: 316, ticker: "TSLA", sides: "calls",
  strike: 9, min_strike: 10, losses: 3, guard_et: "3:45 PM", max_age_s: 300, cost_usd: 1240,
};

test("every reason code has words, with and without its numbers", () => {
  assert.deepEqual(Object.keys(REASONS).sort(), [...REASON_CODES].sort());
  for (const code of REASON_CODES) {
    for (const real of [true, false, null])
      for (const params of [{}, PARAMS, { cost_usd: "x", cap: null }])
        for (const extra of [{}, { reason_text: "the agent's own words", hint: "do this", qty: 2, quote_price: 1.5, est_pnl_usd: -12.5, fill_price: 2 }]) {
          const action = code === "entered" ? "entry" : /^exit_|expiry_guard|closed_outside/.test(code) && !/failed|partial/.test(code) ? "exit" : "skip_x";
          const d = describe(row({ reason_code: code, action, real, reason_params: params, ...extra }), "Robinhood");
          for (const k of ["title", "sub", "what", "why", "can", "money", "group"]) clean(d[k], `${code}.${k}`);
          assert.ok(["trade", "limit", "problem", "other"].includes(d.cat), code);
          if (code !== "unknown") assert.ok(!d.title.startsWith(FALLBACK_TITLE), `${code} fell back`);
        }
  }
});

test("a code this version does not know gets the safe fallback", () => {
  for (const e of [
    row({ reason_code: "brand_new_code", action: "something_new" }),
    row({ reason_code: "entered", action: "entry", known: false }),
    row({ reason_code: "toString" }),
    row({ reason_code: "unknown", contract: null, contract_text: null, at: null, action: "", reason_params: null }),
  ]) {
    const d = describe(e, null);
    assert.ok(d.title.startsWith(FALLBACK_TITLE), d.title);
    assert.equal(d.cat, "other");
    for (const k of ["title", "sub", "what", "why", "can", "money"]) clean(d[k], k);
  }
});

test("every entry check has a label, and an unknown one a fallback", () => {
  assert.deepEqual(Object.keys(CHECK_LABELS).sort(), ENTRY_CHECKS.map((c) => c.id).sort());
  for (const c of ENTRY_CHECKS) {
    clean(checkLabel(c.id), c.id);
    assert.notEqual(checkLabel(c.id), CHECK_FALLBACK);
  }
  assert.equal(checkLabel("a_new_check"), CHECK_FALLBACK);
  assert.equal(checkLabel("constructor"), CHECK_FALLBACK);
});

test("the money tag follows what the row recorded, never a guess", () => {
  assert.equal(tagOf(row({ action: "entry", real: true })), "real");
  assert.equal(tagOf(row({ action: "entry", real: false })), "prac");
  assert.equal(tagOf(row({ action: "exit", real: null })), "unk");
  // a skipped signal is "No order": never real money, never practice
  assert.equal(tagOf(row({ action: "skip_budget", real: true, order_sent: false })), "none");
  assert.equal(tagOf(row({ action: "order_failed", reason_code: "order_rejected", order_sent: true, filled: false })), "nobuy");
  assert.equal(tagOf(row({ action: "skip_x", order_sent: null })), "unk");
  // a failed sale written in practice mode is not a practice event
  assert.equal(tagOf(row({ action: "exit_failed", reason_code: "exit_failed", real: false, order_sent: null })), "unk");
});

test("money and times", () => {
  assert.equal(usd(1983), "$1,983.00");
  assert.equal(usd(1983, 0), "$1,983");
  assert.equal(usd(null), UNKNOWN);
  assert.equal(usd(NaN), UNKNOWN);
  assert.equal(signed(104), "+$104.00");
  assert.equal(signed(-84), "−$84.00");
  assert.equal(signed(0), "$0.00");
  assert.equal(signed(null), UNKNOWN);
  assert.equal(tone(5), "gain");
  assert.equal(tone(-5), "loss");
  assert.equal(tone(0), "");
  assert.equal(tone(null), "");
  // market time, whatever this computer's own time zone is: EST and EDT
  assert.equal(etTime("2026-11-09T19:41:07Z"), "2:41 PM ET");
  assert.equal(etTime("2026-07-09T19:41:07Z"), "3:41 PM ET");
  assert.equal(etTime(null), UNKNOWN);
  assert.equal(etTime("not a time"), UNKNOWN);
  assert.equal(etDayKey("2026-11-10T02:30:00Z"), "2026-11-09");
  assert.equal(etDayKey(null), "");
  assert.equal(dayLong("2026-11-09"), "Mon, Nov 9");
  assert.equal(dayLong("junk"), "date not recorded");
  assert.equal(etWhen("2026-11-09T14:41:00Z", "2026-11-09"), "Today at 9:41 AM ET");
  assert.equal(etWhen(null), "time not recorded");
  assert.equal(plural(1, "trade"), "1 trade");
  assert.equal(plural(3, "trade"), "3 trades");
  assert.equal(ago(4), "4 s");
  assert.equal(ago(null), UNKNOWN);
  assert.equal(span(4740), "1 h 19 min");
  assert.equal(contractName({ symbol: "NVDA", expiry: "2026-11-13", strike: 232.5, side: "P" }), "NVDA $232.50 put");
  clean(contractName(null, null), "unnamed contract");
});

const dir = process.env.GUARDIAN_FIXTURES;
const load = (name) => JSON.parse(fs.readFileSync(path.join(dir, name), "utf8"));
const states = dir ? fs.readdirSync(dir).filter((f) => f.endsWith(".json") && f !== "codes.json").map((f) => [f.replace(/\.json$/, ""), load(f)]) : [];
const skip = dir ? false : "no GUARDIAN_FIXTURES folder (pytest provides one)";

test("every code the view-model declares has words", { skip }, () => {
  const codes = load("codes.json");
  assert.deepEqual([...codes.reason_codes].sort(), [...REASON_CODES].sort());
  const base = states.find(([n]) => n === "live")[1];
  for (const st of codes.answer_states) assert.ok(ANSWER_LOOK[st], st);
  for (const reason of codes.answer_reasons)
    for (const which of ["opening", "closing", "listening"]) {
      const why = answerWhy(which, { state: "cant", reason_code: reason }, base);
      clean(why, `${which}/${reason}`);
      assert.ok(!why.includes("no description"), `${which}/${reason} fell back`);
      clean(answerLook({ state: "cant", reason_code: reason }).word, reason);
    }
  for (const r of codes.unmanaged_reasons) {
    clean(UNMANAGED_SHORT[r], r);
    clean(UNMANAGED_WHY[r], r);
  }
  const pos = base.positions[0];
  for (const action of codes.next_actions)
    for (const reason of codes.next_reasons)
      for (const real of [true, false]) {
        const say = nextSay({ ...pos, real, next: { ...pos.next, action, reason_code: reason } }, base);
        clean(say.text, `${action}/${reason}`);
        // every pairing the agent can send has its own sentence (the rest get the plain fallback)
        const idle = action === "unmanaged" || action === "unknown";
        if (!idle || [...codes.unmanaged_reasons, "pause_feed_not_read", "exit_signal"].includes(reason))
          assert.ok(!say.text.includes("can't describe") && !say.text.includes("not described"), `${action}/${reason} fell back`);
        // a real position nobody is selling is never drawn as "ok"
        if (real && (action === "unmanaged" || action === "unknown")) assert.equal(say.look, "bad", `${action}/${reason}`);
      }
});

test("every state: nothing undefined, and the band keeps to its rules", { skip }, () => {
  assert.ok(states.length >= 12, "fixtures");
  for (const [name, s] of states)
    for (const failing of [false, true]) {
      const l = linkOf(s, failing);
      const where = `${name}${failing ? " (fetch failing)" : ""}`;
      const b = bandModel(s, l);
      for (const t of [b.word, b.say[1], b.run.text, b.title, b.announce, statement(s, l).h, statement(s, l).lead]) clean(t, where);
      for (const v of vitals(s, l)) clean(v.bold + v.text, `${where} vital ${v.key}`);
      for (const n of notes(s, l)) {
        clean(n.title, `${where} note`);
        clean(n.text, `${where} note`);
      }
      for (const p of s.positions) clean(nextFor(p, s, l).text, `${where} ${p.id}`);
      for (const e of s.activity.rows) for (const k of ["title", "sub", "what", "why", "can", "money"]) clean(describe(e, s.broker.name)[k], `${where} row ${e.n}`);
      dayTally(s.activity.rows, etDayKey(s.as_of), s.activity.next != null, s.broker.name);
      clean(etTime(asOf(s, l)), where);

      // the mode band comes from `mode` alone...
      if (isStale(l)) {
        assert.equal(b.frame, "unknown", where);
        assert.equal(b.word, "Mode unknown", where);
        assert.ok(b.say[1].includes(s.mode === "live" ? "LIVE" : "Practice"), `${where}: last report names the mode`);
        assert.ok(!b.actions.pause && !b.actions.toLive && !b.actions.toPractice && !b.actions.resume, where);
      } else {
        assert.equal(b.frame, s.mode, where);
        assert.equal(b.word, s.mode === "live" ? "LIVE" : "Practice", where);
      }
      // ...and pausing never recolours it
      const paused = bandModel({ ...s, pause: { ...s.pause, kind: "entries", scope: "entries" } }, l);
      assert.equal(paused.frame, b.frame, where);
      // the run-state block comes from `pause` alone (or from the lost link)
      if (!isStale(l)) assert.equal(b.run.kind, s.pause.kind === "none" ? "ok" : s.pause.kind === "stopped" ? "off" : "paused", where);
      else assert.equal(b.run.kind, "off", where);
      // Go LIVE is only ever offered from practice, to an agent that answers
      if (b.actions.toLive) assert.ok(s.mode === "practice" && s.agent.answering && !failing, where);

      // never "still sold" when the agent is not selling them
      const all = [b.say[1], statement(s, l).lead, statement(s, l).h].join(" ");
      if (!s.real_positions_managed || l !== "ok") assert.ok(!/still sold|still sells|It sells what it holds|sells (it|each one) for real/.test(all), `${where}: ${all}`);
      if (!s.real_positions_managed && s.real_positions > 0 && l === "ok") {
        assert.ok(unwatched(s).length > 0, where);
        assert.ok(vitals(s, l)[0].look === "bad", `${where}: exposure vital not flagged`);
        assert.ok(notes(s, l).some((n) => n.level === "crit"), `${where}: no needs-you note`);
      }
      // a failed fetch: nothing is presented as being looked after
      if (l === "lost") {
        for (const p of s.positions) assert.notEqual(nextFor(p, s, l).look, "ok", `${where} ${p.id}`);
        if (s.real_positions) assert.equal(vitals(s, l)[0].look, "bad", where);
        assert.ok(notes(s, l).some((n) => n.level === "crit"), where);
      }
    }
});

test("amounts paid are never a partial sum", { skip }, () => {
  const s = states.find(([n]) => n === "stress")[1];
  assert.equal(paidFor(s.positions.filter((p) => p.real)), null);
  const live = states.find(([n]) => n === "live")[1];
  assert.equal(paidFor(live.positions.filter((p) => p.real)), 1983);
  assert.ok(vitals(s, "ok")[0].text.includes("not known"));
});
