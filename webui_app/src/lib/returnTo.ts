// Where the setup wizard's "Dashboard" links and its hop after "Start my
// agent" go. /setup?from=v2 (the /v2 dashboard's links) brings the owner
// back to /v2; anything else, or nothing, is the classic dashboard, exactly
// as before. A fixed list of two values (webui._RETURN_TO on the server
// side): never an open redirect. No imports, so the Node tests load it.

const RETURN_TO: Record<string, string> = { v2: "/v2", dash: "/dash" };

/** The dashboard to go back to, from a location.search string. */
export function returnTo(search: string = typeof location === "undefined" ? "" : location.search): string {
  let vals: string[] = [];
  try {
    vals = new URLSearchParams(search).getAll("from");
  } catch {
    vals = [];
  }
  return vals.length === 1 && Object.prototype.hasOwnProperty.call(RETURN_TO, vals[0]) ? RETURN_TO[vals[0]] : "/dash";
}
