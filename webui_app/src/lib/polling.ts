// Polling that costs nothing when nobody can see it.
//
// Every timer on these pages asks the agent (and some of those questions go
// on to AgentHC or the wallet): the pass sizes, an unpaid invoice, the
// wallet balance, the dashboard status. A tab left open in the background
// kept asking all day. startPolling:
//   - pauses while the page is hidden, and asks once at once when it is
//     shown again (so what the owner sees is fresh, never silently old);
//   - takes a fixed interval, or one that depends on how long it has been
//     polling (fast at first, then slower; null ends it);
//   - stops for good when stopWhen() says nothing can change any more;
//   - never runs two calls at once.
// No React here, so Node's own test runner can drive it with fake timers
// (tests/polling.test.mjs); the hook is usePolling in hooks.ts.

export type Interval = number | ((elapsedMs: number) => number | null);

export interface PollOptions {
  /** Pause while document.hidden (default true). */
  pauseWhenHidden?: boolean;
  /** Checked after every call: true stops the polling for good. */
  stopWhen?: () => boolean;
  /** Call once at the start (default true); else wait one interval. */
  immediate?: boolean;
  /** Called once when the polling ends by itself (stopWhen, or the
   *  interval said null) — not when the caller stops it. */
  onStop?: () => void;
}

export interface PollEnv {
  doc: Pick<Document, "hidden" | "addEventListener" | "removeEventListener"> | null;
  now: () => number;
  setTimeout: (fn: () => void, ms: number) => unknown;
  clearTimeout: (id: unknown) => void;
}

function browserEnv(): PollEnv {
  return {
    doc: typeof document === "undefined" ? null : document,
    now: () => Date.now(),
    setTimeout: (fn, ms) => setTimeout(fn, ms),
    clearTimeout: (id) => clearTimeout(id as ReturnType<typeof setTimeout>),
  };
}

/** Start polling `fn`; returns a function that stops it. */
export function startPolling(
  fn: () => unknown,
  interval: Interval,
  opts: PollOptions = {},
  env: PollEnv = browserEnv(),
): () => void {
  const pause = opts.pauseWhenHidden !== false;
  const t0 = env.now();
  let timer: unknown = null;
  let off = false;
  let running = false;

  const hidden = () => pause && !!env.doc && env.doc.hidden;
  const clear = () => {
    if (timer != null) env.clearTimeout(timer);
    timer = null;
  };
  const end = (byItself: boolean) => {
    if (off) return;
    off = true;
    clear();
    env.doc?.removeEventListener("visibilitychange", onVisible);
    if (byItself) opts.onStop?.();
  };
  const next = () => {
    clear();
    if (off || hidden()) return; // hidden: wait for visibilitychange
    const ms = typeof interval === "function" ? interval(env.now() - t0) : interval;
    if (ms == null) return end(true);
    timer = env.setTimeout(run, ms);
  };
  async function run() {
    timer = null;
    if (off || running || hidden()) return;
    running = true;
    try {
      await fn();
    } catch {
      /* the next call tries again */
    } finally {
      running = false;
    }
    if (off) return;
    if (opts.stopWhen?.()) return end(true);
    next();
  }
  function onVisible() {
    if (off) return;
    clear();
    if (!hidden()) void run(); // shown again: ask now (a call in flight reschedules)
  }

  env.doc?.addEventListener("visibilitychange", onVisible);
  if (opts.immediate !== false && !hidden()) void run();
  else next();
  return () => end(false);
}
