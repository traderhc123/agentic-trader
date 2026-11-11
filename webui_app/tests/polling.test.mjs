// startPolling (src/lib/polling.ts) and the Signal source step's cadences
// (src/wizard/polls.ts), on a fake clock and a fake document. No test
// framework and no dependencies: Node's own test runner, loading the
// TypeScript sources as they are (Node 22.6+ strips the types).
//
//   node --experimental-strip-types --test webui_app/tests/polling.test.mjs
//
// tests/test_polling_pages.py runs this under pytest (and skips when Node is
// missing or too old).
import assert from "node:assert/strict";
import test from "node:test";

import { startPolling } from "../src/lib/polling.ts";
import { BALANCE_WATCH_MS, PASS_SIZES_MS, balanceEvery, passStatusEvery } from "../src/wizard/polls.ts";

/** A fake page: a clock, timers that run when the clock passes them, and a
 *  document whose `hidden` the test flips. */
function fakePage() {
  let now = 0;
  let seq = 0;
  const timers = new Map();
  const listeners = new Set();
  const doc = {
    hidden: false,
    addEventListener: (t, fn) => t === "visibilitychange" && listeners.add(fn),
    removeEventListener: (t, fn) => listeners.delete(fn),
  };
  const env = {
    doc,
    now: () => now,
    setTimeout: (fn, ms) => {
      const id = ++seq;
      timers.set(id, { at: now + ms, fn });
      return id;
    },
    clearTimeout: (id) => timers.delete(id),
  };
  const flush = () => new Promise((r) => setImmediate(r));
  return {
    env,
    doc,
    timers,
    listeners,
    get now() {
      return now;
    },
    /** move the clock `ms` forward, running every timer due on the way */
    async advance(ms) {
      const end = now + ms;
      await flush();
      for (;;) {
        let next = null;
        for (const [id, t] of timers) if (t.at <= end && (!next || t.at < next[1].at)) next = [id, t];
        if (!next) break;
        timers.delete(next[0]);
        now = next[1].at;
        next[1].fn();
        await flush();
      }
      now = end;
    },
    async setHidden(h) {
      doc.hidden = h;
      for (const fn of [...listeners]) fn();
      await flush();
    },
  };
}

test("polls on the interval while visible, once at the start", async () => {
  const p = fakePage();
  let n = 0;
  startPolling(() => n++, 5000, {}, p.env);
  await p.advance(0);
  assert.equal(n, 1);
  await p.advance(60_000);
  assert.equal(n, 13);
});

test("no calls while hidden, one at once when shown again", async () => {
  const p = fakePage();
  let n = 0;
  startPolling(() => n++, 5000, {}, p.env);
  await p.advance(10_000);
  assert.equal(n, 3);
  await p.setHidden(true);
  await p.advance(3_600_000);
  assert.equal(n, 3, "an hour hidden: nothing asked");
  assert.equal(p.timers.size, 0, "no timer left running while hidden");
  await p.setHidden(false);
  assert.equal(n, 4, "shown again: asked at once");
  await p.advance(5000);
  assert.equal(n, 5);
});

test("starting hidden asks nothing until shown", async () => {
  const p = fakePage();
  p.doc.hidden = true;
  let n = 0;
  startPolling(() => n++, 1000, {}, p.env);
  await p.advance(10_000);
  assert.equal(n, 0);
  await p.setHidden(false);
  assert.equal(n, 1);
});

test("pauseWhenHidden: false keeps polling", async () => {
  const p = fakePage();
  let n = 0;
  startPolling(() => n++, 1000, { pauseWhenHidden: false }, p.env);
  await p.setHidden(true);
  await p.advance(10_000);
  assert.equal(n, 11);
});

test("stopWhen ends the timer for good, onStop is told", async () => {
  const p = fakePage();
  let n = 0;
  let stopped = 0;
  startPolling(() => n++, 1000, { stopWhen: () => n >= 3, onStop: () => stopped++ }, p.env);
  await p.advance(60_000);
  assert.equal(n, 3);
  assert.equal(stopped, 1);
  assert.equal(p.timers.size, 0);
  assert.equal(p.listeners.size, 0);
  await p.setHidden(true);
  await p.setHidden(false);
  assert.equal(n, 3, "shown again after it stopped: still nothing");
});

test("the caller's stop ends it without onStop", async () => {
  const p = fakePage();
  let n = 0;
  let stopped = 0;
  const stop = startPolling(() => n++, 1000, { onStop: () => stopped++ }, p.env);
  await p.advance(2000);
  stop();
  await p.advance(10_000);
  assert.equal(n, 3);
  assert.equal(stopped, 0);
});

test("never two calls at once: a slow call delays the next", async () => {
  const p = fakePage();
  let inFlight = 0;
  let most = 0;
  let calls = 0;
  let release = null;
  startPolling(
    () => {
      calls++;
      inFlight++;
      most = Math.max(most, inFlight);
      return new Promise((r) => {
        release = () => {
          inFlight--;
          r();
        };
      });
    },
    1000,
    {},
    p.env,
  );
  await p.advance(5000);
  await p.setHidden(true);
  await p.setHidden(false); // shown again while the first call still runs
  assert.equal(calls, 1);
  release();
  await p.advance(1000);
  assert.equal(calls, 2);
  assert.equal(most, 1);
});

test("a call that throws doesn't end the polling", async () => {
  const p = fakePage();
  let n = 0;
  startPolling(
    () => {
      n++;
      throw new Error("agent unreachable");
    },
    1000,
    {},
    p.env,
  );
  await p.advance(3000);
  assert.equal(n, 4);
});

// ── the Signal source step ──────────────────────────────────────────────────

test("pass sizes: every 15 minutes while visible (was every 3 minutes, always)", async () => {
  const p = fakePage();
  let n = 0;
  startPolling(() => n++, PASS_SIZES_MS, {}, p.env);
  await p.advance(24 * 3_600_000);
  assert.equal(n, 1 + 96);
  // hidden overnight: nothing at all
  const q = fakePage();
  let m = 0;
  startPolling(() => m++, PASS_SIZES_MS, {}, q.env);
  await q.setHidden(true);
  await q.advance(24 * 3_600_000);
  assert.equal(m, 1);
});

test("unpaid pass status: every 3 s for 2 minutes, then every 15 s", async () => {
  const p = fakePage();
  const at = [];
  startPolling(() => at.push(p.now), passStatusEvery, { immediate: false }, p.env);
  await p.advance(120_000);
  assert.equal(at.length, 40);
  assert.equal(at[0], 3000);
  await p.advance(60_000);
  assert.deepEqual(at.slice(40), [135_000, 150_000, 165_000, 180_000]);
  // paused while hidden
  await p.setHidden(true);
  await p.advance(600_000);
  assert.equal(at.length, 44);
});

test("wallet balance: stops when the balance changes", async () => {
  const p = fakePage();
  let bal = 1000;
  let n = 0;
  const start = bal;
  let seen = bal;
  startPolling(
    () => {
      n++;
      if (n === 5) bal = 17_000; // paid
      seen = bal;
    },
    balanceEvery,
    { immediate: false, stopWhen: () => seen !== start },
    p.env,
  );
  await p.advance(BALANCE_WATCH_MS);
  assert.equal(n, 5);
  assert.equal(p.timers.size, 0);
});

test("wallet balance: stops after 10 minutes unpaid, and says so", async () => {
  const p = fakePage();
  let n = 0;
  let stopped = false;
  startPolling(() => n++, balanceEvery, { immediate: false, onStop: () => (stopped = true) }, p.env);
  await p.advance(3_600_000);
  assert.equal(n, BALANCE_WATCH_MS / 4000);
  assert.ok(stopped);
});
