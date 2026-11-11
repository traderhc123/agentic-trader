// Where the /v2 dashboard gets its data: GET /api/v2/status every 5 s (the
// classic dashboard's cadence) while the page is visible — paused while it
// is hidden, asked at once when it is shown again — and older Activity rows
// from /api/v2/activity on demand. `command` is only the stop button's fixed
// "stop" (POST /api/command); every other command goes through the typed
// POST /api/v2/command (App.tsx).
import { useCallback, useEffect, useRef, useState } from "react";
import { startPolling } from "../lib/polling";
import { SCHEMA_VERSION, type Activity, type ActivityPage, type Status } from "../lib/viewModel";
import { postJson } from "./lib/http";

export const POLL_MS = 5000;
const TIMEOUT_MS = 4000;
/** A fetch that fails twice in a row, or no good answer for two polls plus a
 *  margin, and the page stops presenting anything as current. */
const MISSES_ALLOWED = 1;
const STALE_MS = 2 * POLL_MS + 1500;

export type Fetching =
  | { kind: "loading" }
  | { kind: "ok" }
  /** the last fetches failed (network, a server error, an answer that is not a status) */
  | { kind: "failing"; why: "network" | "server" | "schema" }
  /** the dashboard login expired: nothing more can be read */
  | { kind: "locked" };

async function getJson(path: string, signal?: AbortSignal): Promise<{ status: number; body: unknown }> {
  const r = await fetch(path, { signal, cache: "no-store" });
  let body: unknown = null;
  try {
    body = await r.json();
  } catch {
    /* not JSON: the caller treats it as a failed read */
  }
  return { status: r.status, body };
}

function isStatus(x: unknown): x is Status {
  const s = x as Status | null;
  return !!s && typeof s === "object" && s.schema === SCHEMA_VERSION && typeof s.as_of === "string" && !!s.agent && !!s.pause && Array.isArray(s.positions) && !!s.activity && Array.isArray(s.activity.rows);
}

const edge = (rows: Activity[], f: (...n: number[]) => number) => (rows.length ? f(...rows.map((r) => r.n)) : null);

function merge(a: Activity[], b: Activity[]): Activity[] {
  const m = new Map<number, Activity>();
  for (const r of a) m.set(r.n, r);
  for (const r of b) m.set(r.n, r);
  return [...m.values()].sort((x, y) => y.n - x.n);
}

export interface StatusFeed {
  status: Status | null;
  fetching: Fetching;
  /** true once the page must stop presenting the snapshot as current */
  failing: boolean;
  /** every Activity row loaded this visit, newest first */
  rows: Activity[];
  total: number;
  canLoadOlder: boolean;
  loadingOlder: boolean;
  olderError: string;
  loadOlder: () => void;
  refresh: () => Promise<void>;
  /** send one command; resolves to the agent's own reply */
  command: (text: string) => Promise<{ ok: boolean; reply: string }>;
}

export function useStatus(): StatusFeed {
  const [status, setStatus] = useState<Status | null>(null);
  const [fetching, setFetching] = useState<Fetching>({ kind: "loading" });
  const [failing, setFailing] = useState(false);
  const [rows, setRows] = useState<Activity[]>([]);
  const [total, setTotal] = useState(0);
  const [olderDone, setOlderDone] = useState(true);
  const [loadingOlder, setLoadingOlder] = useState(false);
  const [olderError, setOlderError] = useState("");
  const rowsRef = useRef<Activity[]>(rows);
  rowsRef.current = rows;
  const oldestRef = useRef<number | null>(null);
  const olderLoaded = useRef(false);
  const gen = useRef(0); // bumps when the list restarts; a stale "older" reply is dropped
  const misses = useRef(0);
  const lastOk = useRef(0);
  const busy = useRef(false);

  const takeActivity = useCallback((page: ActivityPage) => {
    const first = page.rows;
    const prev = rowsRef.current;
    const prevMax = edge(prev, Math.max);
    const pageMin = edge(first, Math.min);
    const pageMax = edge(first, Math.max);
    const more = page.next != null;
    const seen = new Map(prev.map((x) => [x.n, x] as const));
    // The log was swapped underneath the page (a move was imported): line
    // numbers now point at different rows.
    const replaced =
      (prevMax != null && pageMax != null && pageMax < prevMax) ||
      (prevMax != null && pageMax == null) ||
      first.some((x) => {
        const o = seen.get(x.n);
        return !!o && (o.at !== x.at || o.action !== x.action);
      });
    // More new rows arrived than one page holds: the rows between were never fetched.
    const gap = more && prevMax != null && pageMin != null && pageMin > prevMax + 1;
    if (replaced || gap) {
      gen.current++;
      olderLoaded.current = false;
      setRows(first);
      setOlderDone(!more);
      setOlderError("");
    } else {
      setRows((p) => merge(p, first));
      if (!olderLoaded.current) setOlderDone(!more);
    }
    setTotal(page.total);
    oldestRef.current = page.oldest;
  }, []);

  const refresh = useCallback(async () => {
    if (busy.current) return;
    busy.current = true;
    const ctl = new AbortController();
    const to = setTimeout(() => ctl.abort(), TIMEOUT_MS);
    let why: "network" | "server" | "schema" = "network";
    try {
      const r = await getJson("/api/v2/status", ctl.signal);
      if (r.status === 401 || r.status === 403) {
        setFetching({ kind: "locked" });
        setFailing(true);
        return;
      }
      if (r.status !== 200) why = "server";
      else if (!isStatus(r.body)) why = "schema";
      else {
        misses.current = 0;
        lastOk.current = Date.now();
        setStatus(r.body);
        takeActivity(r.body.activity);
        setFetching({ kind: "ok" });
        setFailing(false);
        return;
      }
    } catch {
      why = "network";
    } finally {
      clearTimeout(to);
      busy.current = false;
    }
    misses.current++;
    setFetching({ kind: "failing", why });
    if (misses.current > MISSES_ALLOWED) setFailing(true);
  }, [takeActivity]);

  useEffect(() => {
    // paused while hidden (local reads only, but nobody is looking); asks at
    // once when the page is shown again
    const stop = startPolling(refresh, POLL_MS);
    // shown again: the answer to the ask made then gets the usual margin
    // before the page calls itself out of date
    let shownAt = Date.now();
    const shown = () => {
      if (!document.hidden) shownAt = Date.now();
    };
    document.addEventListener("visibilitychange", shown);
    // A sleeping tab or computer misses polls without any fetch failing.
    const watch = setInterval(() => {
      if (document.hidden) return;
      if (lastOk.current && Date.now() - Math.max(lastOk.current, shownAt) > STALE_MS) setFailing(true);
    }, 1000);
    return () => {
      stop();
      clearInterval(watch);
      document.removeEventListener("visibilitychange", shown);
    };
  }, [refresh]);

  const cursor = edge(rows, Math.min);
  const canLoadOlder = !olderDone && cursor != null && cursor > (oldestRef.current ?? 0);
  const loadOlder = useCallback(async () => {
    const before = edge(rowsRef.current, Math.min);
    if (before == null || loadingOlder) return;
    const g = gen.current;
    setLoadingOlder(true);
    setOlderError("");
    try {
      const r = await getJson(`/api/v2/activity?before=${before}&limit=100`);
      if (g !== gen.current) return;
      const page = r.body as ActivityPage | null;
      if (r.status !== 200 || !page || !Array.isArray(page.rows)) {
        setOlderError("Couldn't load earlier activity. Try again.");
        return;
      }
      olderLoaded.current = true;
      setRows((p) => merge(p, page.rows));
      setOlderDone(page.next == null || page.rows.length === 0);
    } catch {
      setOlderError("Couldn't reach the agent. Try again.");
    } finally {
      setLoadingOlder(false);
    }
  }, [loadingOlder]);

  const command = useCallback(
    async (text: string) => {
      try {
        const r = await postJson("/api/command", { text });
        if (r.status === 401 || r.status === 403) {
          setFetching({ kind: "locked" });
          return { ok: false, reply: "Your dashboard login expired, so nothing was changed. Open the login link again (python agent.py link)." };
        }
        const j = (await r.json()) as { reply?: unknown; error?: unknown };
        if (r.status !== 200 || typeof j.reply !== "string")
          return { ok: false, reply: `The agent did not accept that${typeof j.error === "string" ? `: ${j.error}` : ""}. Nothing was changed as far as this page knows.` };
        return { ok: true, reply: j.reply };
      } catch {
        return { ok: false, reply: "Could not reach the agent, so this page does not know whether anything changed. Check the band at the top once it reconnects." };
      } finally {
        refresh();
      }
    },
    [refresh],
  );

  return { status, fetching, failing, rows, total, canLoadOlder, loadingOlder, olderError, loadOlder, refresh, command };
}
