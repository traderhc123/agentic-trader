import { useEffect, useRef } from "react";
import { startPolling, type Interval, type PollOptions } from "./polling";

/** setInterval that always calls the latest callback; runs once immediately. */
export function useInterval(fn: () => void, ms: number | null, immediate = true) {
  const ref = useRef(fn);
  ref.current = fn;
  useEffect(() => {
    if (ms == null) return;
    if (immediate) ref.current();
    const id = setInterval(() => ref.current(), ms);
    return () => clearInterval(id);
  }, [ms, immediate]);
}

/** startPolling (lib/polling.ts) for a component: paused while the page is
 *  hidden, once at once when shown again, stopWhen ends it. `interval` null
 *  = not polling. A new `restart` value starts it over (e.g. a new invoice);
 *  the callbacks may change on every render. */
export function usePolling(
  fn: () => unknown,
  interval: Interval | null,
  opts: PollOptions = {},
  restart: unknown = null,
) {
  const ref = useRef({ fn, interval, opts });
  ref.current = { fn, interval, opts };
  const on = interval != null;
  const fixed = typeof interval === "number" ? interval : -1;
  useEffect(() => {
    if (!on) return;
    const o = ref.current.opts;
    return startPolling(
      () => ref.current.fn(),
      typeof ref.current.interval === "function"
        ? (el) => (typeof ref.current.interval === "function" ? ref.current.interval(el) : ref.current.interval)
        : fixed,
      {
        pauseWhenHidden: o.pauseWhenHidden,
        immediate: o.immediate,
        stopWhen: () => !!ref.current.opts.stopWhen?.(),
        onStop: () => ref.current.opts.onStop?.(),
      },
    );
  }, [on, fixed, restart]);
}

export async function copyToClipboard(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}
