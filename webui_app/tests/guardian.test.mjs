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
  EXPIRY_OFF,
  FALLBACK_TITLE,
  REASONS,
  UNMANAGED_SHORT,
  UNMANAGED_WHY,
  answerLook,
  answerWhy,
  checkLabel,
  describe,
  expiryWill,
  nextSay,
  sellsHow,
  tagOf,
} from "../src/v2/lib/copy.ts";
import {
  asOf,
  bandModel,
  bookToday,
  bookTodayNote,
  dayTally,
  isStale,
  linkOf,
  nextFor,
  notes,
  paidFor,
  realBookCaption,
  statement,
  unwatched,
  vitals,
} from "../src/v2/lib/derive.ts";
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

test("an order with no clear answer is never 'rejected', and 'filled' only when recorded", () => {
  for (const [code, action] of [["buy_unconfirmed", "order_failed"], ["sell_unconfirmed", "exit_failed"]]) {
    const d = describe(row({ action, reason_code: code, real: true, mode_at_time: "live", order_sent: true, filled: null, stopped_at_check: null }), "Robinhood");
    const said = [d.title, d.sub, d.what].join(" ");
    assert.ok(!/rejected|did not go through|Nothing was bought|failed/i.test(said), `${code}: ${said}`);
    assert.match(d.sub, /may have gone through/, code);
    assert.equal(d.tag, "real", code);
    assert.match(d.money, /not known/, code);
    assert.ok(!/was filled/.test(d.money), `${code}: ${d.money}`);
  }
  const partly = describe(row({ action: "entry_unfilled", reason_code: "buy_partly_filled", real: true, order_sent: true, filled: null }), "Robinhood");
  assert.ok(!/Nothing was bought/.test(partly.what + partly.sub), partly.what);
  // `filled` null: never "filled"; true: filled
  const unk = describe(row({ action: "entry", reason_code: "entered", real: true, order_sent: true, filled: null }), "Robinhood");
  assert.equal(unk.money, "Real money. An order was sent to Robinhood; whether it filled is not known.");
  const yes = describe(row({ action: "entry", reason_code: "entered", real: true, order_sent: true, filled: true }), "Robinhood");
  assert.equal(yes.money, "Real money. An order was filled at Robinhood.");
});

test("a sell still working is 'sent', never 'Sold'", () => {
  for (const code of ["exit_signal", "expiry_guard", "exit_remainder"]) {
    const d = describe(row({ action: "exit", reason_code: code, real: true, order_sent: true, filled: false, qty: 3 }), "Robinhood");
    assert.ok(d.title.startsWith("Sell order sent"), d.title);
    assert.ok(!/\bsold\b/i.test(d.title) && !/The agent sold/.test(d.what), `${code}: ${d.title} / ${d.what}`);
    assert.match(d.money, /had not filled/);
    const done = describe(row({ action: "exit", reason_code: code, real: true, order_sent: true, filled: true, qty: 3 }), "Robinhood");
    assert.ok(/^Sold/.test(done.title), done.title);
  }
});

// a minimal Status / Position for the sentence functions
const S = (over = {}) => ({
  broker: { name: "Robinhood", paper_account: false, account_mask: null },
  market: { open: true, closes_at: "2026-11-09T21:00:00Z" },
  limits: { open_positions: { real: 1, practice: 0 } },
  signals: { pass: { days_left: null }, last_signal: null },
  expiry_guard_on: true,
  ...over,
});
const P = (next, over = {}) => ({
  real: true, expires_today: false, qty: 1, contract: null, label: "SPY",
  next: { action: "unmanaged", at: null, reason_code: "no_exit_signals", expiry_guard_at: null, ...next }, ...over,
});

test("with no exit signals a real position is never presented as looked after", () => {
  const s = S();
  for (const [action, reason] of [["unmanaged", "no_exit_signals"], ["unknown", "feed_unreachable"], ["unmanaged", "no_bid"]]) {
    const say = nextSay(P({ action, reason_code: reason }), s);
    assert.equal(say.look, "bad", reason);
    assert.match(say.text, /yourself|watch it/, reason);
  }
  // expiring today, expiry sell on: it still runs, and only that is promised
  const guarded = nextSay(P({ expiry_guard_at: "2026-11-09T20:45:00Z" }, { expires_today: true }), s).text;
  assert.match(guarded, /expiry sell still sells it/);
  assert.ok(!/before the close/.test(guarded), guarded);
  // ...with no expiry sell, the owner is told to sell it before the close
  assert.match(nextSay(P({}, { expires_today: true }), s).text, /before the close/);
  // the no-bid sentence says it once
  const nb = nextSay(P({ action: "unmanaged", reason_code: "no_bid" }, { expires_today: true }), s).text;
  assert.equal(nb.match(/before the close/g).length, 1, nb);
  const closing = answerWhy("closing", { state: "cant", reason_code: "no_signal_access" }, s);
  assert.match(closing, /No exit signals reach the agent/);
  assert.match(answerWhy("closing", { state: "cant", reason_code: "feed_unreachable" }, s), /exit signal can't reach/);
});

test("the expiry sell is promised only when it is on", () => {
  const p = P({ action: "sell_on_exit", reason_code: "exit_signal" });
  assert.equal(expiryWill(p, S({ expiry_guard_on: false })).will, null);
  assert.equal(expiryWill(p, S({ expiry_guard_on: false })).wont, EXPIRY_OFF);
  assert.ok(!/expiry sell time/.test(sellsHow(S({ expiry_guard_on: false }))));
  assert.match(expiryWill(p, S()).will, /expiry sell time/);
  assert.match(expiryWill(P({ expiry_guard_at: "2026-11-13T20:45:00Z" }), S()).will, /3:45 PM ET/);
  assert.match(sellsHow(S()), /expiry sell time/);
});

test("a sale today with no estimate is never 'nothing closed'", () => {
  const B = (days, unmeasured_today) => ({ days, unmeasured_today });
  assert.equal(bookTodayNote(bookToday(B([], 0), "2026-11-09")), "nothing closed today");
  const only = bookToday(B([], 2), "2026-11-09");
  assert.deepEqual(only, { closed: 0, pnl: null, unmeasured: 2 });
  assert.ok(!/nothing closed/.test(bookTodayNote(only)), bookTodayNote(only));
  assert.match(bookTodayNote(only), /2 sales not measured/);
  const both = bookToday(B([{ day: "2026-11-09", pnl: 12, closed: 1 }], 1), "2026-11-09");
  assert.equal(bookTodayNote(both), "1 trade closed, plus 1 sale not measured");
});

test("a broker paper account is never real money", () => {
  const e = row({ action: "exit", reason_code: "exit_signal", real: true, paper_account: true, order_sent: true, filled: true, qty: 1 });
  assert.equal(tagOf(e), "paper");
  const d = describe(e, "Alpaca");
  assert.ok(!/Real money/.test(d.money), d.money);
  assert.match(d.money, /Not real money/);
  assert.equal(tagOf({ ...e, paper_account: false }), "real");
  assert.equal(tagOf({ ...e, paper_account: null }), "real");
  assert.match(realBookCaption(S({ broker: { name: "Alpaca", paper_account: true, account_mask: null } })), /paper-trading/);
  assert.equal(realBookCaption(S()), "Orders filled at Robinhood");
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
      for (const v of vitals(s, l)) clean(v.bold + v.text + (v.tail ?? ""), `${where} vital ${v.key}`);
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

test("the review states say what the agent can and can't do", { skip }, () => {
  const get = (n) => {
    const hit = states.find(([x]) => x === n);
    assert.ok(hit, `state ${n}`);
    return hit[1];
  };
  // no exit signal can arrive: nothing is "sold on its exit signal"
  for (const name of ["pass-lapsed", "day-pass-refused", "feed-down"]) {
    const s = get(name);
    assert.equal(s.real_positions_managed, false, name);
    assert.notEqual(s.capabilities.closing.state, "yes", name);
    for (const p of s.positions.filter((x) => x.real)) assert.equal(nextSay(p, s).look, "bad", `${name} ${p.id}`);
    assert.ok(notes(s, "ok").some((n) => n.level === "crit"), name);
    const said = [statement(s, "ok").lead, answerWhy("closing", s.capabilities.closing, s)].join(" ");
    assert.ok(!/It sells what it holds|On each exit signal/.test(said), `${name}: ${said}`);
  }
  const lapsed = notes(get("pass-lapsed"), "ok");
  assert.ok(lapsed.some((n) => n.key === "pass"), "pass note on a waiting pass");
  assert.ok(lapsed.some((n) => n.text.includes("has run out")), "the agent's own pass notice");
  assert.ok(notes(get("day-pass-refused"), "ok").some((n) => n.text.includes("refused")));
  // the expiry sell is off: never promised
  const off = get("expiry-guard-off");
  assert.equal(off.expiry_guard_on, false);
  for (const p of off.positions.filter((x) => x.real)) assert.equal(expiryWill(p, off).will, null, p.id);
  // no bid: not "being sold"
  const nb = get("expiry-no-bid");
  const iwm = nb.positions.find((p) => p.id.startsWith("IWM|2026-11-09"));
  assert.equal(iwm.next.reason_code, "no_bid");
  assert.match(nextSay(iwm, nb).text, /in the money/);
  // unclear orders, a working sell, a sale with no estimate
  const c = get("check-these-orders");
  const said = c.activity.rows.map((e) => describe(e, c.broker.name));
  assert.ok(said.some((d) => d.title.startsWith("No clear answer on a buy")));
  assert.ok(said.some((d) => d.title.startsWith("No clear answer on a sell")));
  assert.ok(said.some((d) => d.title.startsWith("Sell order sent")));
  assert.ok(bookToday(c.results.real, etDayKey(c.as_of)).unmeasured >= 1);
  // a paper account
  const pa = get("paper-account");
  assert.equal(pa.results.paper_account.total, 64);
  assert.ok(pa.activity.rows.filter((e) => e.action === "exit" && etDayKey(e.at) === etDayKey(pa.as_of)).every((e) => tagOf(e) === "paper"));
  assert.match(realBookCaption(pa), /paper-trading/);
});

test("amounts paid are never a partial sum", { skip }, () => {
  const s = states.find(([n]) => n === "stress")[1];
  assert.equal(paidFor(s.positions.filter((p) => p.real)), null);
  const live = states.find(([n]) => n === "live")[1];
  assert.equal(paidFor(live.positions.filter((p) => p.real)), 1983);
  assert.ok(vitals(s, "ok")[0].text.includes("not known"));
});
