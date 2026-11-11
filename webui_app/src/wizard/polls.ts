// How often the Signal source step asks the agent again (see usePolling in
// lib/hooks.ts: every one of these pauses while the page is hidden and asks
// at once when it is shown again). Kept out of SourceStep.tsx so Node's test
// runner can load them (tests/polling.test.mjs).

/** The pass sizes on sale (each ask goes on to AgentHC): every 15 minutes. */
export const PASS_SIZES_MS = 15 * 60_000;

/** An unpaid invoice's status: every 3 s for 2 minutes, then every 15 s
 *  (the agent itself re-checks a pending pass every minute either way). */
export const passStatusEvery = (elapsedMs: number): number => (elapsedMs < 120_000 ? 3000 : 15_000);

/** The wallet balance after "Fund": every 4 s for at most 10 minutes (and
 *  until it changes), then the page offers a Refresh button. */
export const BALANCE_WATCH_MS = 10 * 60_000;
export const balanceEvery = (elapsedMs: number): number | null => (elapsedMs < BALANCE_WATCH_MS ? 4000 : null);
