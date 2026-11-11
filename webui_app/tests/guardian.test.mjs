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
  dayBooks,
  dayTally,
  isStale,
  linkOf,
  nextFor,
  notes,
  paidFor,
  realBookCaption,
  showPaperBook,
  statement,
  unwatched,
  vitals,
} from "../src/v2/lib/derive.ts";
import { UNKNOWN, ago, contractName, dayLong, etDayKey, etTime, etWhen, plural, signed, span, tone, usd } from "../src/v2/lib/format.ts";
import { UPDATE } from "../src/v2/lib/copy.ts";
import { API, FAILED, confirmBody, getJson, onUnknownWrite, postJson, send } from "../src/v2/lib/http.ts";
import {
  MOVE_TOOL,
  ackGiven,
  SETUP_FROM_V2,
  confirmLabel,
  limitMeans,
  limitRange,
  limitsPayload,
  lossWords,
  purchaseFate,
  readNumber,
  readTickers,
  riskOf,
  savedTickers,
  settingsDoubt,
  signInOutcome,
  sizeMeans,
  sizeWords,
  v2Words,
  valueWords,
} from "../src/v2/lib/settings.ts";
import { ACTION_HOME, HOME_FILE, VIEWS, screenOf, todayLimitsLink } from "../src/v2/lib/screens.ts";
import { returnTo } from "../src/lib/returnTo.ts";
import { fileURLToPath } from "node:url";
import { RANGES, addDays, axisMoney, chartModel, niceTicks, rangeStart } from "../src/v2/lib/chart.ts";
import {
  CHECK_PATH,
  PULL_PATH,
  backOnNewVersion,
  badgeFor,
  checkForUpdate,
  failureText,
  installUpdate,
  pullBody,
  readCheck,
  readPull,
  updateLine,
} from "../src/v2/lib/update.ts";

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
  // problem rows from a paper account are not real money either
  for (const [action, code] of [["order_failed", "buy_unconfirmed"], ["exit_failed", "sell_unconfirmed"], ["entry_unfilled", "buy_partly_filled"]]) {
    const p = describe(row({ action, reason_code: code, real: true, mode_at_time: "live", paper_account: true, order_sent: true, filled: null, stopped_at_check: null }), "Alpaca");
    assert.equal(p.tag, "paper", code);
    const said = [p.money, p.title, p.sub, p.what].join(" ");
    assert.ok(!/Real money|real position|a real (buy|sell)/.test(said), `${code}: ${said}`);
  }
  // a paper-account line that did not record whether an order was sent
  const rej = describe(row({ action: "order_failed", reason_code: "order_rejected", real: true, paper_account: true, order_sent: null, filled: null }), "Alpaca");
  assert.match(rej.money, /not recorded/);
  assert.ok(!/An order was sent/.test(rej.money), rej.money);
});

test("an order the broker refused as a repeat is not 'no clear answer'", () => {
  for (const [action, code, note] of [
    ["order_failed", "buy_unconfirmed", "Alpaca already has an earlier order for this same signal, so it took no new buy order for SPY"],
    ["order_failed", "buy_unconfirmed", "an earlier buy order for SPY is still unconfirmed (the agent can't tell whether it went through), so it did not buy again"],
    ["exit_failed", "sell_unconfirmed", "Robinhood already has an earlier order for this same signal, so it took no new sell order for SPY"],
  ]) {
    const d = describe(row({ action, reason_code: code, real: true, order_sent: true, filled: null, reason_text: note, stopped_at_check: null }), "Alpaca");
    const said = [d.title, d.sub, d.what].join(" ");
    assert.ok(!/no clear answer/i.test(said), said);
    assert.match(d.what, /now or earlier/);
    assert.match(d.why, /earlier/);
  }
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
  // the agent's own pass notice is kept, folded into the pass note (one fact, one place)
  assert.ok(lapsed.some((n) => n.key === "pass" && n.agent.some((t) => t.includes("has run out"))), "the agent's own pass notice");
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
  assert.ok(said.some((d) => d.title.startsWith("Buy not confirmed")));
  assert.ok(said.some((d) => d.title.startsWith("Sell not confirmed")));
  assert.ok(said.some((d) => d.title.startsWith("Sell order sent")));
  assert.ok(bookToday(c.results.real, etDayKey(c.as_of)).unmeasured >= 1);
  // a paper account
  const pa = get("paper-account");
  assert.equal(pa.results.paper_account.total, 64);
  assert.ok(pa.activity.rows.filter((e) => e.action === "exit" && etDayKey(e.at) === etDayKey(pa.as_of)).every((e) => tagOf(e) === "paper"));
  assert.match(realBookCaption(pa), /paper-trading/);
});

test("the pass note and the agent's pass notice only say what is true now", { skip }, () => {
  const get = (n) => states.find(([x]) => x === n)[1];
  // the prepaid pass is used up, but the agent reads with today's wallet day-pass
  const w = get("wallet-over-lapsed-pass");
  assert.equal(w.signals.pass.status, "active");
  assert.equal(w.real_positions_managed, true);
  const wn = notes(w, "ok");
  assert.ok(!wn.some((n) => n.key === "pass"), JSON.stringify(wn));
  assert.ok(!wn.some((n) => /receives no signals|gets no signals|has run out/.test(n.text)), JSON.stringify(wn));
  // a waiting pass while the last read saw entry signals: never "no signals"
  const lapsed = get("pass-lapsed");
  const reading = { ...lapsed, signals: { ...lapsed.signals, entry_access: true } };
  assert.ok(!notes(reading, "ok").some((n) => n.key === "pass"));
  for (const status of ["waiting", "none", "unknown"]) {
    const s = { ...reading, signals: { ...reading.signals, pass: { ...reading.signals.pass, status } } };
    assert.ok(!notes(s, "ok").some((n) => n.key === "pass"), status);
  }
  // a pass that could not be checked is not called used up or expired
  const u = get("pass-unchecked");
  assert.equal(u.signals.pass.status, "unknown");
  assert.equal(u.unmanaged_reason, "pass_unchecked");
  const note = notes(u, "ok").find((n) => n.key === "pass");
  assert.ok(note, "pass note");
  assert.match(note.title, /could not be checked/);
  const words = [note.text, UNMANAGED_WHY.pass_unchecked, answerWhy("closing", u.capabilities.closing, u), ...u.positions.filter((p) => p.real).map((p) => nextSay(p, u).text)];
  for (const t of words) assert.ok(!/used up|expired|waiting for payment|no usable/.test(t), t);
});

test("today's sales in a broker paper account are shown in LIVE mode", { skip }, () => {
  const pa = states.find(([x]) => x === "paper-account")[1];
  const d = dayBooks(pa);
  assert.equal(d.opened, "live trade");
  assert.equal(d.closed[0].word, "paper-account trade");
  assert.equal(d.closed[0].day.closed, 1);
  assert.equal(d.closed[0].day.pnl, 64);
  assert.ok(showPaperBook(pa));
  // a real-money account whose paper book has sales today: both lines
  const live = states.find(([x]) => x === "live")[1];
  assert.ok(!showPaperBook(live));
  assert.equal(dayBooks(live).closed.length, 1);
  const both = { ...live, results: { ...live.results, paper_account: pa.results.paper_account } };
  const lines = dayBooks(both).closed;
  assert.deepEqual(lines.map((x) => x.label), ["Closed", "Closed in a paper account"]);
  assert.equal(lines[0].word, "real-money trade");
  assert.ok(showPaperBook(both));
  // practice mode reads the practice book only
  const prac = states.find(([x]) => x === "practice")[1];
  assert.deepEqual(dayBooks(prac).closed.map((x) => x.word), ["practice trade"]);
});

test("amounts paid are never a partial sum", { skip }, () => {
  const s = states.find(([n]) => n === "stress")[1];
  assert.equal(paidFor(s.positions.filter((p) => p.real)), null);
  const live = states.find(([n]) => n === "live")[1];
  assert.equal(paidFor(live.positions.filter((p) => p.real)), 1983);
  assert.ok(vitals(s, "ok")[0].text.includes("not known"));
});

// ── the update badge and its dialog (lib/update.ts) ─────────────────────────
// Every answer GET /api/update/check and POST /api/update/pull can give
// (webui.check_code_updates / pull_code_updates, snapshot_update.check),
// driven through a fake fetch that records what the page sends.

const SHA = "0123456789abcdef0123456789abcdef01234567";
const fakeFetch = (answers) => {
  const calls = [];
  const f = async (url, init = {}) => {
    calls.push({ url, init });
    const a = answers.length > 1 ? answers.shift() : answers[0];
    if (a instanceof Error) throw a;
    return {
      status: a.status ?? 200,
      json: async () => {
        if (a.body === undefined) throw new SyntaxError("not JSON");
        return a.body;
      },
    };
  };
  return { f, calls };
};
const AVAILABLE = { ok: true, behind: 3, upstream: "origin/main", summary: "abc1234 fix one\nbcd2345 fix two\ncde3456 fix three", target: SHA };

test("update: copy has words for every case", () => {
  for (const [k, v] of Object.entries(UPDATE)) {
    if (typeof v === "string") clean(v, `UPDATE.${k}`);
    else if (Array.isArray(v)) v.forEach((x, i) => clean(x, `UPDATE.${k}[${i}]`));
    else for (const arg of [0, 1, 3, "a note", 25]) clean(String(v(arg)), `UPDATE.${k}(${arg})`);
  }
  assert.equal(UPDATE.badgeShort + UPDATE.badgeRest, UPDATE.badge);
  // the dialog says what happens in plain words
  assert.match(UPDATE.lead("0123456789ab"), /exactly version 0123456789ab/);
  assert.ok(UPDATE.will.some((w) => /no orders/.test(w)));
  assert.ok(UPDATE.wont.some((w) => /Pause setting/.test(w)));
  assert.match(UPDATE.live(2), /LIVE.*2 real positions/);
  assert.ok(!/position/.test(UPDATE.live(0)));
});

test("update: up to date or a failed check shows no badge, and the failure quietly", () => {
  const quiet = [
    [200, { ok: true, behind: 0, upstream: "origin/main", summary: "", target: SHA }, "current"],
    [200, { ok: true, behind: 0, upstream: "github/main", summary: "", target: SHA, mode: "snapshot" }, "current"],
    // install folder not writable (old systemd unit), with its fix on a second line
    [200, { ok: false, error: "Can't update: the background service on this server is not allowed to change the agent's own files.\n  sudo systemctl edit agentic-trader" }, "unavailable"],
    [200, { ok: false, error: "refusing to update from an unrecognised remote (https://evil.example/x) — updates only come from github.com/traderhc123/agentic-trader" }, "unavailable"],
    [200, { ok: false, error: "fetch failed: GitHub is not reachable from this computer" }, "unavailable"],
    [200, { ok: false, error: "GitHub's hourly limit for update checks was reached from this network — try again in an hour" }, "unavailable"],
    [200, { ok: false }, "unavailable"],
    [500, { ok: false, error: "boom" }, "unavailable"],
    [404, { error: "not found" }, "unavailable"],
    [401, { error: "locked — open the dashboard login link (python agent.py link)" }, "unavailable"],
    [403, null, "unavailable"],
    [200, undefined, "unavailable"], // not JSON
    [200, [], "unavailable"],
    [200, { ok: true, behind: "3", target: SHA }, "unavailable"],
    [200, { ok: true, behind: -1, target: SHA }, "unavailable"],
    [200, { ok: true, behind: 1.5, target: SHA }, "unavailable"],
    [200, { ok: true }, "unavailable"],
  ];
  for (const [http, body, kind] of quiet) {
    const where = `${http} ${JSON.stringify(body)}`;
    const c = readCheck(http, body);
    assert.equal(c.kind, kind, where);
    assert.equal(badgeFor(c, "idle", true), null, where);
    const line = updateLine(c, "idle");
    assert.equal(line.review, false, where);
    clean(line.text, where);
    if (kind === "unavailable") {
      assert.match(line.text, /^Couldn't check for updates/, where);
      if (typeof body?.error === "string" && http !== 401) assert.ok(line.text.includes(body.error), `${where}: the agent's words`);
    } else assert.equal(line.text, UPDATE.current);
  }
  assert.match(readCheck(401, {}).text, /login expired/);
});

test("update: behind with an exact version shows the badge with the count", () => {
  const c = readCheck(200, AVAILABLE);
  assert.equal(c.kind, "available");
  assert.equal(c.offer.target, SHA);
  assert.equal(c.offer.short, SHA.slice(0, 12));
  assert.equal(c.offer.count, 3);
  assert.deepEqual(c.offer.changes, ["abc1234 fix one", "bcd2345 fix two", "cde3456 fix three"]);
  assert.deepEqual(badgeFor(c, "idle", true), { count: 3 });
  assert.match(UPDATE.badgeLabel(3), /^Update available: 3 changes/);
  assert.equal(UPDATE.badgeCount(1), "1 change");
  assert.deepEqual(updateLine(c, "idle"), { text: UPDATE.available(3), review: true });
  // a zip install: same badge
  const snap = readCheck(200, { ...AVAILABLE, upstream: "github/main", mode: "snapshot" });
  assert.equal(snap.offer.snapshot, true);
  assert.deepEqual(badgeFor(snap, "idle", true), { count: 3 });
  // a zip install whose version isn't recorded: the number is not a count
  const unk = readCheck(200, {
    ...AVAILABLE,
    mode: "snapshot",
    behind: 20,
    version_unknown: true,
    summary: "(this install's version isn't recorded — showing the latest changes; updating installs the newest code)\nx",
  });
  assert.deepEqual(badgeFor(unk, "idle", true), { count: null });
  assert.equal(UPDATE.badgeCount(null), "");
  assert.ok(!/\d/.test(UPDATE.badgeLabel(null)));
  // no exact version to install: nothing is offered
  for (const target of [undefined, "", "main", SHA.slice(0, 12), SHA + "x", "origin/main"]) {
    const c2 = readCheck(200, { ...AVAILABLE, target });
    assert.equal(c2.kind, "unavailable", String(target));
    assert.equal(badgeFor(c2, "idle", true), null, String(target));
  }
});

test("update: never offered while the check is in flight, an install is under way, or the agent can't be reached", () => {
  const c = readCheck(200, AVAILABLE);
  assert.equal(badgeFor({ kind: "checking" }, "idle", true), null);
  assert.equal(updateLine({ kind: "checking" }, "idle").review, false);
  for (const phase of ["installing", "failed", "restarting", "installed", "slow"]) {
    assert.equal(badgeFor(c, phase, true), null, phase);
    assert.equal(updateLine(c, phase, "x").review, false, phase);
  }
  assert.equal(badgeFor(c, "idle", false), null);
});

test("update: the check is one GET that never installs", async () => {
  const { f, calls } = fakeFetch([{ body: AVAILABLE }]);
  const c = await checkForUpdate(f, 1000);
  assert.equal(c.kind, "available");
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, CHECK_PATH);
  assert.equal(calls[0].init.method, undefined);
  assert.equal(calls[0].init.body, undefined);
  // no answer: quiet, no badge
  const down = await checkForUpdate(fakeFetch([new TypeError("Failed to fetch")]).f, 1000);
  assert.equal(down.kind, "unavailable");
  assert.match(down.text, /could not reach the agent/);
  const hang = (_url, init) =>
    new Promise((_, no) => init.signal.addEventListener("abort", () => no(Object.assign(new Error("aborted"), { name: "AbortError" }))));
  const slow = await checkForUpdate(hang, 20);
  assert.equal(slow.kind, "unavailable");
  assert.match(slow.text, /no answer after/);
});

test("update: the pull sends exactly the checked version, the way /v2 sends commands", async () => {
  const c = readCheck(200, AVAILABLE);
  const { f, calls } = fakeFetch([{ body: { ok: true, restarting: true, note: "updated to origin/main — restarting with the new code" } }]);
  const r = await installUpdate(c.offer, f, 1000);
  assert.deepEqual(r, { ok: true, restarting: true, note: "updated to origin/main — restarting with the new code" });
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, PULL_PATH);
  assert.deepEqual(JSON.parse(calls[0].init.body), { expect: SHA });
  assert.deepEqual(pullBody(c.offer), { expect: SHA });
  // the same request shape as POST /api/command: same method, same headers,
  // the login cookie only (no credentials override, no token of its own)
  const cmd = fakeFetch([{ body: { reply: "ok" } }]);
  await postJson("/api/command", { text: "pause" }, undefined, cmd.f);
  const a = calls[0].init;
  const b = cmd.calls[0].init;
  assert.equal(a.method, "POST");
  assert.equal(a.method, b.method);
  assert.deepEqual(a.headers, b.headers);
  assert.deepEqual(a.headers, { "Content-Type": "application/json" });
  assert.equal(a.credentials, undefined);
  assert.deepEqual(Object.keys(a).sort(), ["body", "headers", "method", "signal"]);
  // a pull that does not restart (AGENT_UPDATE_NO_RESTART)
  const plain = readPull(200, { ok: true, note: "updated to origin/main — restart skipped (AGENT_UPDATE_NO_RESTART)" });
  assert.equal(plain.restarting, false);
  assert.match(updateLine(c, "installed", plain.note).text, /restart skipped/);
});

test("update: a failed pull shows the agent's words, once, and offers nothing more", async () => {
  const c = readCheck(200, AVAILABLE);
  const cases = [
    // diverged checkout / local changes: the check said behind, the pull refuses
    [200, { ok: false, error: "cannot fast-forward (local changes or diverged history): fatal: Not possible to fast-forward, aborting." }, /cannot fast-forward/],
    [200, { ok: false, error: "the remote changed since you checked — press 'Check for code updates' again and review the new commit list" }, /remote changed/],
    [200, { ok: false, error: "Can't update: the background service on this server is not allowed to change the agent's own files." }, /not allowed to change/],
    [500, { ok: false, error: "disk full" }, /disk full/],
    [200, { ok: false }, /gave no reason/],
    [401, { error: "locked" }, /login expired, so nothing was installed/],
    [502, undefined, /HTTP 502.*does not know whether/],
  ];
  for (const [status, body, want] of cases) {
    const { f, calls } = fakeFetch([{ status, body }]);
    const r = await installUpdate(c.offer, f, 1000);
    const where = `${status} ${JSON.stringify(body)}`;
    assert.equal(r.ok, false, where);
    assert.equal(calls.length, 1, `${where}: tried once`);
    const text = failureText(r);
    assert.match(text, want, where);
    if (typeof body?.error === "string" && status !== 401) {
      assert.equal(r.fromAgent, true, where);
      assert.ok(text.startsWith("The agent said: ") && text.includes(body.error), where);
    }
    const line = updateLine(c, "failed", text);
    assert.match(line.text, want, where);
    assert.match(line.text, /reload this page/, where);
    assert.equal(line.review, false, where);
    assert.equal(badgeFor(c, "failed", true), null, where);
  }
  // no answer at all: the page can't say nothing was installed
  const { f, calls } = fakeFetch([new TypeError("Failed to fetch")]);
  const r = await installUpdate(c.offer, f, 1000);
  assert.equal(calls.length, 1);
  assert.equal(r.ok, false);
  assert.match(r.text, /does not know whether the update was installed/);
});

test("update: the page reloads only once the agent is back after the restart", () => {
  const T = "2026-11-10T14:00:00Z";
  assert.equal(backOnNewVersion(T, false, { answered: true, startedAt: T }), false); // the old agent, still up
  assert.equal(backOnNewVersion(T, false, { answered: false, startedAt: null }), false);
  assert.equal(backOnNewVersion(T, true, { answered: false, startedAt: null }), false);
  assert.equal(backOnNewVersion(T, false, { answered: true, startedAt: "2026-11-10T14:01:00Z" }), true); // restarted
  assert.equal(backOnNewVersion(T, true, { answered: true, startedAt: T }), true); // gone, then back
  assert.equal(backOnNewVersion(null, false, { answered: true, startedAt: null }), false); // setup mode: needs the gap
  assert.equal(backOnNewVersion(null, true, { answered: true, startedAt: null }), true);
});

test("update: no badge in any state where this page can't reach the agent", { skip }, () => {
  const c = readCheck(200, AVAILABLE);
  for (const [name, s] of states)
    for (const failing of [false, true]) {
      const l = linkOf(s, failing);
      const b = badgeFor(c, "idle", !isStale(l));
      if (isStale(l)) assert.equal(b, null, `${name} ${failing}`);
      else assert.deepEqual(b, { count: 3 }, `${name} ${failing}`);
      clean(UPDATE.live(s.positions.filter((p) => p.real).length), name);
    }
});

// ── Results chart (lib/chart.ts) ─────────────────────────────────────────────

const cents = (n) => Math.round(n * 100);
const book = (days, over = {}) => {
  const total = days.reduce((a, d) => a + d.pnl, 0);
  return { today: 0, last_7_days: 0, total, closed: days.length, won: 0, lost: 0, unmeasured: 0, unmeasured_today: 0, days, ...over };
};
const withBooks = (real, practice) => ({ results: { real, practice, paper_account: book([]), signal_cost: {} } });
const weekday = (d) => ![0, 6].includes(new Date(d + "T00:00:00Z").getUTCDay());

test("chart: every period's last point is the figure the panels show", { skip }, () => {
  let checked = 0;
  for (const [name, s] of states) {
    const today = etDayKey(s.as_of);
    for (const [range] of RANGES) {
      const C = chartModel(s, range, today);
      assert.equal(C.real.length, C.days.length, name);
      assert.equal(C.practice.length, C.days.length, name);
      assert.ok(C.days.length > 0, `${name} ${range}: never a blank chart`);
      assert.deepEqual([...C.days].sort(), C.days, name);
      for (const k of ["real", "practice"]) {
        const B = s.results[k];
        const want = range === "all" ? B.total : range === "7d" ? B.last_7_days : null;
        if (!B.days.length) assert.equal(C.end[k], null, `${name} ${range} ${k}: no line without a closed trade`);
        else if (want != null) {
          assert.equal(cents(C.end[k]), cents(want), `${name} ${range} ${k}`);
          checked++;
        }
        if (range === "all") assert.equal(C.undated[k], 0, `${name} ${k}: everything dated is plotted`);
        // a day with nothing closed carries the total forward
        C[k].forEach((p, i) => {
          if (i && p.pnl == null && C[k][i - 1].cum != null) assert.equal(p.cum, C[k][i - 1].cum, `${name} ${range} ${k} ${p.day}`);
          if (p.pnl != null && p.cum != null) assert.equal(cents(p.cum), cents((i ? C[k][i - 1].cum ?? 0 : 0) + p.pnl));
        });
      }
    }
  }
  assert.ok(checked >= 20, `only ${checked} totals checked`);
});

test("chart: weekdays only, the 7 days the panels count, ET day keys in any time zone", () => {
  assert.equal(rangeStart("7d", "2026-11-09", null), "2026-11-03");
  assert.equal(rangeStart("30d", "2026-11-09", null), "2026-10-11");
  assert.equal(rangeStart("all", "2026-11-09", "2026-01-02"), "2026-01-02");
  assert.equal(addDays("2026-03-08", 1), "2026-03-09"); // across a clock change
  const s = withBooks(book([{ day: "2026-11-07", pnl: 5, closed: 1 }, { day: "2026-11-02", pnl: 10, closed: 1 }]), book([]));
  const C = chartModel(s, "7d", "2026-11-09");
  // Saturday Nov 7 is on the axis because a trade closed then; Sunday Nov 8 is not
  assert.deepEqual(C.days, ["2026-11-03", "2026-11-04", "2026-11-05", "2026-11-06", "2026-11-07", "2026-11-09"]);
  assert.ok(C.days.every((d) => weekday(d) || d === "2026-11-07"));
  // the Nov 2 trade is before the period: the line starts at $0, not at +$10
  assert.deepEqual(C.real.map((p) => p.cum), [0, 0, 0, 0, 5, 5]);
  assert.equal(C.end.real, 5);
  assert.equal(C.end.practice, null);
  assert.equal(C.quiet, false);
  const A = chartModel(s, "all", "2026-11-09");
  assert.equal(A.days[0], "2026-11-02");
  assert.equal(A.end.real, 15);
});

test("chart: always draws something: one day, no days, no trades in the period", () => {
  const one = chartModel(withBooks(book([{ day: "2026-11-09", pnl: -12.5, closed: 1 }]), book([])), "all", "2026-11-09");
  assert.deepEqual(one.days, ["2026-11-09"]);
  assert.deepEqual(one.real, [{ day: "2026-11-09", pnl: -12.5, closed: 1, cum: -12.5 }]);
  assert.equal(one.empty, false);
  const none = chartModel(withBooks(book([]), book([])), "all", "2026-11-09");
  assert.equal(none.empty, true);
  assert.ok(none.days.length >= 5, "empty axes still span a week");
  assert.equal(none.end.real, null);
  // a weekend-only period still has its day
  assert.deepEqual(chartModel(withBooks(book([]), book([])), "7d", "2026-11-08").days.length, 5);
  const quiet = chartModel(withBooks(book([{ day: "2026-09-01", pnl: 40, closed: 1 }]), book([])), "7d", "2026-11-09");
  assert.equal(quiet.quiet, true);
  assert.equal(quiet.end.real, 0);
  assert.ok(quiet.real.every((p) => p.cum === 0));
});

test("chart: whole cents, and a total the days can't account for is reported, not hidden", () => {
  const days = [0.1, 0.2, 0.3, 0.7, 1.1].map((pnl, i) => ({ day: `2026-11-0${i + 2}`, pnl, closed: 1 }));
  const C = chartModel(withBooks(book(days), book([])), "all", "2026-11-06");
  assert.equal(C.end.real, 2.4);
  const odd = chartModel(withBooks(book(days, { total: 52.4 }), book([])), "all", "2026-11-06");
  assert.equal(odd.undated.real, 50);
  assert.equal(chartModel(withBooks(book(days, { total: 52.4 }), book([])), "7d", "2026-11-06").undated.real, 0);
});

test("chart: the money axis always has $0 and short labels", () => {
  for (const [lo, hi] of [[0, 0], [5, 80], [-316, 212], [-0.4, 0], [-1234567.89, 98765432.1]]) {
    const t = niceTicks(lo, hi);
    assert.ok(t.includes(0), `${lo}..${hi}: ${t}`);
    assert.ok(t[0] <= Math.min(0, lo) && t[t.length - 1] >= Math.max(0, hi), `${lo}..${hi}`);
    assert.ok(t.length >= 2 && t.length <= 8, `${lo}..${hi}: ${t.length} ticks`);
    for (const v of t) clean(axisMoney(v), String(v));
  }
  assert.equal(axisMoney(-200), "−$200");
  assert.equal(axisMoney(12500), "$12.5k");
  assert.equal(axisMoney(100000000), "$100M");
  assert.equal(axisMoney(-2500000), "−$2.5M");
});

// ── settings forms (PR 3): words, checks, the typed client ──────────────────

test("settings: what the owner types is read, or said wrong next to the field", () => {
  const cap = limitRange(null, "max_entries_per_day");
  assert.deepEqual(readNumber("3", cap), { ok: true, value: 3 });
  for (const bad of ["", "abc", "2.5", "0", "1001", "-1", "NaN", "Infinity", "1e3x"]) {
    const r = readNumber(bad, cap);
    assert.equal(r.ok, false, bad);
    clean(r.error, bad);
    assert.match(r.error, /from 1 to 1,000/);
  }
  const loss = limitRange(null, "max_daily_loss_usd");
  assert.deepEqual(readNumber("$1,150.50", loss), { ok: true, value: 1150.5 });
  assert.deepEqual(readNumber("0", loss), { ok: true, value: 0 });
  assert.match(readNumber("-5", loss).error, /0 turns the limit off/);
  // the server's own ranges win when it sends them
  const s = { limits: { allowed: { max_entries_per_day: [1, 20] } } };
  assert.equal(readNumber("21", limitRange(s, "max_entries_per_day")).ok, false);
  assert.deepEqual(readTickers("spy, QQQ $nvda spy"), { ok: true, value: ["SPY", "QQQ", "NVDA"] });
  assert.deepEqual(readTickers(""), { ok: true, value: [] });
  const t = readTickers("spy, 12x, b@d");
  assert.equal(t.ok, false);
  assert.match(t.error, /12x, b@d/);
  assert.deepEqual(savedTickers("spy qqq"), ["SPY", "QQQ"]);
  assert.deepEqual(savedTickers(null), []);
});

test("settings: a value and a change are always said in words, never null or NaN", () => {
  const vals = [null, undefined, 0, 3, 1150, 2.5, true, false, "x", { mode: "budget", value: 500 }, { mode: "percent", value: 2.5 },
    { mode: "contracts", value: 2 }, { mode: "budget", value: null }, { id: "alpaca", paper: true }, { id: null, paper: null }, "live", "practice"];
  for (const key of ["trade_size.main", "trade_size.other", "max_daily_loss_usd", "max_entries_per_day", "max_open_positions", "broker", "mode", "signal_days", "day_pass_recurring", "robinhood", "anything"])
    for (const v of vals) clean(valueWords(key, v), `${key}=${JSON.stringify(v)}`);
  assert.equal(lossWords(0), "Off");
  assert.equal(lossWords(null), "Off");
  assert.equal(lossWords(300), "$300");
  assert.equal(sizeWords(null, "other"), "Same as the main pick");
  assert.equal(sizeWords({ mode: "budget", value: 1150 }), "$1,150 a trade");
  clean(sizeMeans({ mode: "percent", value: 2.5 }, "main"), "percent");
  for (const k of ["max_entries_per_day", "max_daily_loss_usd", "max_open_positions"])
    for (const v of [0, 1, 3, 500]) clean(limitMeans(k, v, { mainUsd: v ? 500 : null, open: 2 }), `${k} ${v}`);
  // the loss limit never says "stop-loss" and always says it never sells
  assert.match(limitMeans("max_daily_loss_usd", 300, { mainUsd: null, open: 0 }), /never sells anything/);
});

test("settings: the confirm button is named after the change", () => {
  const ch = (key, a, b, direction) => [{ key, label: key, saved_now: a, new: b, direction }];
  assert.equal(confirmLabel(ch("trade_size.main", { mode: "budget", value: 500 }, { mode: "budget", value: 1150 }, "raises_risk")), "Raise to $1,150");
  assert.equal(confirmLabel(ch("max_entries_per_day", 4, 2, "lowers_risk")), "Lower to 2");
  assert.equal(confirmLabel(ch("max_daily_loss_usd", 300, 0, "raises_risk")), "Turn off the daily loss limit");
  assert.equal(confirmLabel(ch("max_daily_loss_usd", null, 200, "lowers_risk")), "Set the limit to $200");
  assert.equal(confirmLabel(ch("trade_size.other", { mode: "budget", value: 150 }, null, "neutral")), "Use the main-pick size");
  assert.equal(confirmLabel(ch("trade_size.main", { mode: "budget", value: 500 }, { mode: "contracts", value: 2 }, "neutral")), "Change to 2 contracts");
  assert.equal(confirmLabel([...ch("max_entries_per_day", 4, 2, "lowers_risk"), ...ch("max_open_positions", 8, 9, "raises_risk")]), "Save 2 changes");
});

test("settings: the page's risk reading follows the server's rules", () => {
  // webui._risk_direction / _size_direction: bigger is riskier; no loss limit is the riskiest
  assert.equal(riskOf("max_entries_per_day", 4, 5), "raises_risk");
  assert.equal(riskOf("max_open_positions", 8, 3), "lowers_risk");
  assert.equal(riskOf("max_daily_loss_usd", 300, 0), "raises_risk");
  assert.equal(riskOf("max_daily_loss_usd", 0, 300), "lowers_risk");
  assert.equal(riskOf("max_daily_loss_usd", null, 0), "neutral");
  assert.equal(riskOf("max_daily_loss_usd", 300, 500), "raises_risk");
  assert.equal(riskOf("trade_size.main", { mode: "budget", value: 500 }, { mode: "budget", value: 400 }), "lowers_risk");
  assert.equal(riskOf("trade_size.main", { mode: "budget", value: 500 }, { mode: "percent", value: 1 }), "neutral");
  assert.equal(riskOf("trade_size.other", null, { mode: "budget", value: 1 }), "neutral");
});

test("settings: a limits save sends only what changed, and never the mode", () => {
  const saved = { limits: { max_entries_per_day: 4, max_daily_loss_usd: 300, max_open_positions: 8 } };
  assert.deepEqual(limitsPayload(saved, { max_entries_per_day: 4, max_daily_loss_usd: 0 }), { max_daily_loss_usd: 0 });
  assert.deepEqual(limitsPayload(saved, { main: { mode: "budget", value: 1150 } }), { trade_size: { main: { mode: "budget", value: 1150 } } });
  assert.deepEqual(limitsPayload(saved, { other: null }), { trade_size: { other: { mode: "same" } } });
  for (const p of [limitsPayload(saved, { max_open_positions: 9 }), limitsPayload(saved, {})])
    for (const k of Object.keys(p)) assert.ok(!/dry|mode|paused/.test(k), k);
});

test("http: a write with no answer, or one it can't read, makes the page read the settings again", async () => {
  let told = 0;
  const off = onUnknownWrite(() => told++);
  const answer = (status, body) => async () => new Response(body, { status });
  try {
    // no answer, and an answer this page can't read: the change may have been saved
    assert.equal((await send("/api/v2/settings/limits", {}, async () => { throw new TypeError("offline"); })).why, "network");
    assert.equal(told, 1);
    assert.equal((await send("/api/v2/settings/limits", {}, answer(502, "<html>bad gateway</html>"))).why, "server");
    assert.equal(told, 2);
    // a clear answer, either way, or a login that expired (nothing changed): no re-read needed
    await send("/api/v2/settings/limits", {}, answer(200, JSON.stringify({ ok: true, saved: true })));
    await send("/api/v2/settings/limits", {}, answer(200, JSON.stringify({ ok: false, error: "no" })));
    await send("/api/v2/settings/limits", {}, answer(401, "{}"));
    assert.equal(told, 2);
  } finally {
    off();
  }
  await send("/api/v2/settings/limits", {}, async () => { throw new TypeError("offline"); });
  assert.equal(told, 2, "unsubscribed");
  // what the page says beside values it can't vouch for, until a read succeeds
  assert.equal(settingsDoubt(true, "", false), "");
  assert.equal(settingsDoubt(false, "Could not reach the agent. Try again.", true), "", "no values shown: the page already says why");
  assert.match(settingsDoubt(true, "", true), /^Not known: .*may not be what is saved now/);
  assert.match(settingsDoubt(true, "Could not reach the agent. Try again.", true), /Could not reach the agent/);
  assert.match(settingsDoubt(true, "Could not reach the agent. Try again.", false), /may be out of date.*Could not reach/);
  for (const t of [settingsDoubt(true, "", true), settingsDoubt(true, "x.", false)]) clean(t, "doubt");
});

test("money shown red always carries its sign (lost today included)", () => {
  const dir = path.join(path.dirname(fileURLToPath(import.meta.url)), "../src/v2");
  const files = ["views/Rules.tsx", "views/Today.tsx", "views/Pages.tsx", "views/Results.tsx", "views/Activity.tsx", "views/Signals.tsx", "views/Settings.tsx"];
  for (const f of files) {
    const src = fs.readFileSync(path.join(dir, f), "utf8");
    // a span coloured as a loss whose amount is printed without a sign
    assert.doesNotMatch(src, /"loss"[^>]*>\{usd\(/, f);
  }
  assert.equal(signed(-316, 0), "−$316");
  assert.equal(tone(-316), "loss");
  assert.equal(signed(-0, 0), "$0");
  assert.equal(tone(-0), "");
});

test("signal days: 'Paid' only for the purchase this dialog made", () => {
  // an older pass is active: the pass check says "active", but this purchase is nowhere
  assert.equal(purchaseFate("new", { paid: ["old"], waiting: [] }), "gone");
  assert.equal(purchaseFate("new", { paid: ["old"], waiting: ["new"] }), "waiting");
  assert.equal(purchaseFate("new", { paid: ["new"], waiting: [] }), "paid");
  assert.equal(purchaseFate("new", { paid: ["old", "new"], waiting: [] }), "paid");
  // the settings could not be read: not known, keep waiting
  assert.equal(purchaseFate("new", null), "unknown");
  assert.equal(purchaseFate("new", {}), "unknown");
  // and the dialog never takes the pass check's "active" as this purchase paid
  const src = fs.readFileSync(path.join(path.dirname(fileURLToPath(import.meta.url)), "../src/v2/views/Signals.tsx"), "utf8");
  assert.doesNotMatch(src, /if \(st === "active"\)/);
  assert.match(src, /purchaseFate\(id, nv\?\.signals\.purchases\)/);
});

test("going LIVE: a tick given for one preview never confirms another", () => {
  assert.equal(ackGiven(null, "t1"), false);
  assert.equal(ackGiven("t1", "t1"), true);
  // the settings changed, Review again brought a new preview: unticked
  assert.equal(ackGiven("t1", "t2"), false);
  // and the dialog keeps the tick as that token, never as a bare true/false
  const src = fs.readFileSync(path.join(path.dirname(fileURLToPath(import.meta.url)), "../src/v2/Forms.tsx"), "utf8");
  assert.match(src, /const ticked = ackGiven\(tickedFor, preview\.token\)/);
  assert.doesNotMatch(src, /setTicked\(/);
});

test("settings: a Robinhood sign-in is over when the agent says so, however it ended", () => {
  const end = (over) => ({ seq: 4, connected: true, account_mask: "4821", options_off: false, error: null, ...over });
  // still waiting: no sign-in finished since this one started (or no answer)
  assert.equal(signInOutcome(4, end()).kind, "wait");
  assert.equal(signInOutcome(4, null).kind, "wait");
  assert.equal(signInOutcome(0, { seq: 0, connected: null, account_mask: null, options_off: false, error: null }).kind, "wait");
  // the same account again changes no setting, but the sign-in is over
  const done = signInOutcome(3, end());
  assert.equal(done.kind, "done");
  assert.match(done.text, /Robinhood ····4821/);
  // options off: the classic wizard's warning, never a plain "Connected."
  const warn = signInOutcome(3, end({ options_off: true }));
  assert.equal(warn.kind, "warn");
  assert.match(warn.text, /rejects the agent's orders/);
  // no account allowed for agentic trading, or the sign-in failed: said, never a wait
  const none = signInOutcome(3, end({ connected: false, account_mask: null }));
  assert.equal(none.kind, "failed");
  assert.match(none.text, /no active account that allows agentic trading/);
  const failed = signInOutcome(3, end({ connected: false, account_mask: null, error: "state mismatch" }));
  assert.equal(failed.kind, "failed");
  assert.match(failed.text, /state mismatch/);
  for (const o of [done, warn, none, failed]) clean(o.text, o.kind);
});

test("settings: the agent's words for the classic page say where it is in /v2", () => {
  // the messages the agent and its signal source write (sources/agenthc_day_trade_ideas.py, agent.py)
  const said = [
    "Your AgentHC feed pass has run out — the agent gets no signals until you buy more signal days (dashboard → Settings → Signal source).",
    "connect a different Lightning wallet, or buy signal days with a prepaid pass (dashboard → Settings → Signal source). If you know",
    "Check it, or set up another wallet, in the dashboard (Settings → Signal source)",
    "the wallet does not hold enough sats — add sats in the dashboard (Settings → Signal source)",
    "Buy more signal days in the dashboard (Settings → Signal source).",
    "buy more in the dashboard (Settings → Signal source); unused days carry over.",
    "Trades can't be placed until it's back. Fix: reconnect it in Settings → Broker.",
  ];
  for (const t of said) {
    const w = v2Words(t);
    assert.ok(!/dashboard|Signal source|Settings → Broker/.test(w), w);
    assert.match(w, /Signals & pass|Settings/);
  }
  assert.equal(v2Words("Signals resumed."), "Signals resumed.");
});

const fakeReply = (status, body, calls = []) => async (url, init) => {
  calls.push({ url, init });
  if (status === "down") throw new TypeError("Failed to fetch");
  return { status, json: async () => (body === "not json" ? JSON.parse("{") : body) };
};

test("http: every /v2 write is one same-origin JSON POST; failures never claim nothing changed", async () => {
  const calls = [];
  const ok = await send(API.limits, { max_entries_per_day: 3 }, fakeReply(200, { ok: true, needs_confirm: true, preview: { changes: [], does_not: [], token: "t", expires_in_s: 120 } }, calls));
  assert.equal(ok.needs_confirm, true);
  assert.equal(calls[0].url, "/api/v2/settings/limits");
  assert.equal(calls[0].init.method, "POST");
  assert.equal(calls[0].init.headers["Content-Type"], "application/json");
  assert.deepEqual(JSON.parse(calls[0].init.body), { max_entries_per_day: 3 });
  assert.deepEqual(confirmBody({ a: 1 }, "tok"), { a: 1, confirm: "tok" });
  assert.deepEqual(confirmBody({ a: 1 }, "tok", false), { a: 1, confirm: "tok", acknowledge: false });
  const refused = await send(API.limits, {}, fakeReply(200, { ok: false, error: "max_entries_per_day must be from 1 to 1,000. Nothing was changed." }));
  assert.equal(refused.ok, false);
  assert.match(refused.error, /from 1 to 1,000/);
  for (const [status, body, why] of [[401, {}, "locked"], [403, {}, "locked"], ["down", null, "network"], [500, {}, "server"], [200, "not json", "server"], [200, { nope: 1 }, "server"]]) {
    const r = await send(API.rules, {}, fakeReply(status, body));
    assert.equal(r.ok, false);
    assert.equal(r.why, why, String(status));
    clean(r.error, why);
    if (why !== "locked") assert.match(r.error, /does not know whether/);
  }
  const g = await getJson(API.settings, fakeReply(200, { ok: true, fingerprint: "f" }));
  assert.equal(g.ok, true);
  assert.equal((await getJson(API.settings, fakeReply(401, {}))).why, "locked");
  assert.equal((await getJson(API.settings, fakeReply("down", null))).why, "network");
  for (const f of Object.values(FAILED)) for (const t of Object.values(f)) clean(t, "failure words");
  // /v2 sends no free text: the classic command route is only the fixed "stop"
  for (const [k, p] of Object.entries(API)) if (k !== "classicCommand") assert.ok(p !== "/api/command", k);
});

test("the setup wizard goes back to /v2 only when /v2 sent the owner there", () => {
  assert.equal(SETUP_FROM_V2, "/setup?from=v2");
  assert.equal(returnTo("?from=v2"), "/v2");
  assert.equal(returnTo("?ui=classic&from=v2"), "/v2");
  // without it, or with anything else, exactly where it went before
  for (const q of ["", "?from=dash", "?from=https://evil.example", "?from=/v2", "?from=v2&from=v2", "?from=V2", "?from=", "?from=//evil"])
    assert.equal(returnTo(q), "/dash", q);
});

// ── no redundancy: one fact, one place; one action, one home ────────────────

const sentences = (t) =>
  String(t ?? "")
    .split(/(?<=[.!?])\s+/)
    .map((x) => x.toLowerCase().replace(/[^a-z0-9$ ]+/g, " ").replace(/\s+/g, " ").trim())
    .filter((x) => x.split(" ").length >= 4);

test("no redundancy: each condition once per screen, each link once, every control in its one home", { skip }, () => {
  for (const [name, s] of states)
    for (const failing of [false, true]) {
      const l = linkOf(s, failing);
      for (const view of VIEWS) {
        const where = `${name}${failing ? " (fetch failing)" : ""} / ${view}`;
        const sc = screenOf(view, s, l);
        // one note per condition
        const codes = sc.conditions.flat();
        assert.equal(new Set(codes).size, codes.length, `${where}: a condition twice: ${codes}`);
        // at most one link to a page, never to the page itself
        assert.equal(new Set(sc.links).size, sc.links.length, `${where}: two links to one page: ${sc.links}`);
        assert.ok(!sc.links.includes(view), `${where}: links to itself`);
        // every control is in its home (the band is on every screen)
        for (const a of sc.controls) assert.ok(ACTION_HOME[a] === "band" || ACTION_HOME[a] === view, `${where}: ${a} is not at home here`);
        assert.equal(new Set(sc.controls).size, sc.controls.length, `${where}: a control twice: ${sc.controls}`);
      }
      // every agent message shows once: folded into its condition's note, or alone
      const ns = notes(s, l, "today");
      const told = ns.flatMap((n) => [n.text, ...n.agent]);
      for (const x of s.notices) assert.equal(told.filter((t) => t === v2Words(x.text)).length <= 1, true, `${name}: "${x.text}" twice`);
      // nothing points into the classic pages
      for (const n of ns) for (const t of [n.title, n.text, ...n.agent]) assert.ok(!/dashboard\s*(→|->)|Settings\s*(→|->)\s*(Signal|Broker)/.test(t), `${name}: ${t}`);
      // band = mode and run-state, vitals = readings, notes = what needs the owner,
      // the statement = the state: no sentence said twice among them
      const b = bandModel(s, l);
      const st = statement(s, l);
      const parts = {
        band: [b.say[0], b.say[1], b.run.text + b.run.long].join(" "),
        vitals: vitals(s, l).map((v) => `${v.bold}${v.text}. ${v.tail ?? ""}`).join(" "),
        notes: ns.map((n) => `${n.title}. ${n.text}`).join(" "),
        statement: `${st.h} ${st.lead}`,
      };
      const seen = new Map();
      for (const [part, text] of Object.entries(parts))
        for (const x of new Set(sentences(text))) {
          assert.ok(!seen.has(x) || seen.get(x) === part, `${name}: "${x}" in both ${seen.get(x)} and ${part}`);
          seen.set(x, part);
        }
    }
});

test("no redundancy: the operator's example is one note, in the dashboard's words", { skip }, () => {
  const get = (n) => states.find(([x]) => x === n)[1];
  const s = get("pass-lapsed");
  const ns = notes(s, "ok", "today");
  const pass = ns.filter((n) => n.codes.includes("signal_pass"));
  assert.equal(pass.length, 1, JSON.stringify(ns));
  assert.equal(pass[0].key, "pass"); // the dashboard's note, not "From your agent"
  assert.match(pass[0].title, /signal pass/i);
  assert.equal(pass[0].agent.length, 1); // the agent's message is folded in, with /v2's words
  assert.match(pass[0].agent[0], /\(Signals & pass\)/);
  assert.ok(!ns.some((n) => n.title.startsWith("From your agent")), JSON.stringify(ns));
  assert.equal(pass[0].go, "signals");
  // the broker down, said by the agent too: one note, raised to the agent's level
  const b = get("broker-down-agent-says-so");
  const bn = notes(b, "ok", "today");
  const down = bn.filter((n) => n.codes.includes("broker_down"));
  assert.equal(down.length, 1);
  assert.equal(down[0].level, "crit");
  assert.match(down[0].agent[0], /in Settings, under Connections/);
  // a message about a condition with no dashboard note: shown once, by itself
  assert.equal(bn.filter((n) => n.codes.includes("records_unwritten")).length, 1);
  // Today's limits panel drops its link when a note links to Rules & limits already
  const cash = get("low-cash");
  assert.ok(notes(cash, "ok", "today").some((n) => n.go === "rules"));
  assert.equal(todayLimitsLink(cash, "ok"), false);
  assert.equal(todayLimitsLink(get("live"), "ok"), true);
  // the band's own buttons are never repeated in a note
  for (const [, x] of states) for (const n of notes(x, linkOf(x, false), "today")) assert.ok(n.go == null || ["settings", "signals", "rules", "holdings"].includes(n.go));
});

const SRC = path.join(path.dirname(fileURLToPath(import.meta.url)), "..", "src");
function sources(dir) {
  const out = [];
  for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
    const p = path.join(dir, e.name);
    if (e.isDirectory()) out.push(...sources(p));
    else if (/\.(tsx?|mjs)$/.test(e.name)) out.push(p);
  }
  return out;
}
const rel = (p) => path.relative(path.join(SRC, "v2"), p).split(path.sep).join("/");

test("no redundancy: every control is written in its home's file, once per kind of action", () => {
  const files = sources(path.join(SRC, "v2")).filter((p) => p.endsWith(".tsx"));
  const where = new Map();
  for (const f of files) {
    const text = fs.readFileSync(f, "utf8");
    for (const m of text.matchAll(/\b(?:data-act|act)="([a-z-]+)"/g)) {
      if (!where.has(m[1])) where.set(m[1], new Set());
      where.get(m[1]).add(rel(f));
    }
  }
  for (const [a, fs_] of where) {
    assert.ok(Object.prototype.hasOwnProperty.call(ACTION_HOME, a), `data-act="${a}" is not a known action (lib/screens.ts)`);
    assert.deepEqual([...fs_], [HOME_FILE[ACTION_HOME[a]]], `${a} has a control outside its home: ${[...fs_]}`);
  }
  for (const a of Object.keys(ACTION_HOME)) assert.ok(where.has(a), `${a} has no control at all`);
});

test("the settings never leave /v2: every remaining link to /setup or /dash is on the list", () => {
  const files = sources(path.join(SRC, "v2"));
  const found = [];
  for (const f of files) {
    const text = fs.readFileSync(f, "utf8");
    for (const m of text.matchAll(/["'`](\/(?:setup|dash)\b[^"'`]*)["'`]/g)) found.push(`${rel(f)} ${m[1]}`);
  }
  // each one, and why it stays:
  assert.deepEqual(found.sort(), [
    // the constants the links use (Band's start link; Settings' move link)
    "lib/settings.ts /dash#move",
    "lib/settings.ts /setup?from=v2",
    // the Waiting screen, shown when there is no status to draw Settings from
    "App.tsx /dash",
    // Settings → This dashboard: the one "Back to the classic dashboard"
    "views/Settings.tsx /dash",
  ].sort());
  assert.equal(MOVE_TOOL, "/dash#move");
  // the start link (shown only when no agent runs) returns to /v2
  assert.match(fs.readFileSync(path.join(SRC, "v2", "Band.tsx"), "utf8"), /href=\{SETUP_FROM_V2\}/);
  // the setup wizard's own links and its hop after starting honour from=v2
  for (const f of ["wizard/Wizard.tsx", "wizard/steps.tsx"]) {
    const t = fs.readFileSync(path.join(SRC, f), "utf8");
    assert.ok(!/["']\/dash["']/.test(t), `${f} still goes to /dash by itself`);
    assert.match(t, /returnTo\(\)/);
  }
});
