// The one way /v2 sends a POST to the agent: same-origin JSON, carried by
// the dashboard's login cookie, with no Origin trick or token of its own (the
// server's host/origin guard and login check apply to every POST alike).
// The commands (POST /api/command) and the update (POST /api/update/pull)
// both go through here. No imports, so the Node tests load it as it is.

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
