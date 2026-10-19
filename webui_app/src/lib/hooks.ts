import { useEffect, useRef } from "react";

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

export async function copyToClipboard(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}
