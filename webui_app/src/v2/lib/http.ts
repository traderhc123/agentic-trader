// The one way /v2 talks to the agent: same-origin JSON, carried by the
// dashboard's login cookie, with no Origin trick or token of its own (the
// server's host/origin guard and login check apply to every request alike).
// Every read and write of /v2 goes through here: the typed settings routes
// (/api/v2/...), the update pull, the few classic read-only routes /v2
// reuses, and the one fixed command the stop button still sends.
// No imports that exist at run time, so the Node tests load it as it is.
import type { Reply } from "../../lib/settingsApi";

export type Fetch = (input: string, init?: RequestInit) => Promise<Response>;

export const browserFetch: Fetch = (input, init) => fetch(input, init);

export function postJson(path: string, body: unknown, signal?: AbortSignal, f: Fetch = browserFetch): Promise<Response> {
  return f(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
}

/** Why a request has no answer to show. The words never claim that nothing
 *  changed when this page can't know. */
export type Failure = "locked" | "network" | "server";

export const FAILED: Record<Failure, { read: string; write: string }> = {
  locked: {
    read: "Your dashboard login expired. Open the login link again (python agent.py link).",
    write: "Your dashboard login expired, so nothing was changed. Open the login link again (python agent.py link).",
  },
  network: {
    read: "Could not reach the agent. Try again.",
    write: "Could not reach the agent, so this page does not know whether the change was saved. It shows your settings as the agent reports them once it answers.",
  },
  server: {
    read: "The agent answered in a form this page can't read. Try again.",
    write: "The agent answered in a form this page can't read, so this page does not know whether the change was saved. Check the setting again below.",
  },
};

export type Got<T> = { ok: true; body: T } | { ok: false; why: Failure; error: string };

/** GET a JSON route. */
export async function getJson<T>(path: string, f: Fetch = browserFetch): Promise<Got<T>> {
  let r: Response;
  try {
    r = await f(path, { cache: "no-store" } as RequestInit);
  } catch {
    return { ok: false, why: "network", error: FAILED.network.read };
  }
  if (r.status === 401 || r.status === 403) return { ok: false, why: "locked", error: FAILED.locked.read };
  let body: unknown = null;
  try {
    body = await r.json();
  } catch {
    /* not JSON */
  }
  if (r.status !== 200 || body == null || typeof body !== "object") return { ok: false, why: "server", error: FAILED.server.read };
  return { ok: true, body: body as T };
}

/** A /v2 write's answer, or why there is none (`why`, with the words to show). */
export type Sent<Done = object> = Reply<Done> | { ok: false; error: string; why: Failure };

// Who to tell when a write's outcome is not known (no answer, or one this
// page can't read): the change may have been saved, so the page must read
// the settings again rather than keep showing what it read before.
const unknownWrites = new Set<() => void>();

/** Called after every write whose outcome is not known; returns the unsubscribe. */
export function onUnknownWrite(fn: () => void): () => void {
  unknownWrites.add(fn);
  return () => {
    unknownWrites.delete(fn);
  };
}

function unknown<T>(r: T): T {
  for (const fn of Array.from(unknownWrites)) {
    try {
      fn();
    } catch {
      /* a listener's failure never changes the answer */
    }
  }
  return r;
}

/** POST to a /v2 route. The server answers every outcome with HTTP 200 and
 *  {ok, ...}; anything else is a failure this page reports as unknown. */
export async function send<Done = object>(path: string, body: unknown, f: Fetch = browserFetch): Promise<Sent<Done>> {
  let r: Response;
  try {
    r = await postJson(path, body, undefined, f);
  } catch {
    return unknown({ ok: false, why: "network", error: FAILED.network.write });
  }
  if (r.status === 401 || r.status === 403) return { ok: false, why: "locked", error: FAILED.locked.write };
  let j: unknown = null;
  try {
    j = await r.json();
  } catch {
    /* not JSON */
  }
  const o = j as { ok?: unknown; error?: unknown } | null;
  if (r.status !== 200 || !o || typeof o !== "object" || typeof o.ok !== "boolean")
    return unknown({ ok: false, why: "server", error: FAILED.server.write });
  if (o.ok === false && typeof o.error !== "string") return { ...(o as object), ok: false, error: "The agent refused the change without saying why. Nothing was changed." } as Sent<Done>;
  return o as Sent<Done>;
}

/** The second POST of a two-step write: the same payload with the preview's
 *  token (and, for going LIVE, the tick box). */
export function confirmBody(payload: Record<string, unknown>, token: string, acknowledge?: boolean): Record<string, unknown> {
  return acknowledge === undefined ? { ...payload, confirm: token } : { ...payload, confirm: token, acknowledge };
}

// The routes /v2 uses. One list, so a test can see every one of them.
export const API = {
  settings: "/api/v2/settings",
  limits: "/api/v2/settings/limits",
  rules: "/api/v2/settings/rules",
  alerts: "/api/v2/settings/alerts",
  computer: "/api/v2/settings/computer",
  ai: "/api/v2/settings/ai",
  brokerConnect: "/api/v2/broker/connect",
  robinhood: "/api/v2/broker/robinhood",
  passBuy: "/api/v2/pass/buy",
  command: "/api/v2/command",
  // classic, read-only (or saving nothing), reused as they are
  brokers: "/api/brokers",
  aiProviders: "/api/llm/providers",
  aiModels: "/api/llm/models",
  passOptions: "/api/pass/options",
  passStatus: "/api/pass/status",
  walletBalance: "/api/wallet/balance",
  // the one fixed command not in /api/v2/command: "stop" (see Dialogs.tsx)
  classicCommand: "/api/command",
} as const;
