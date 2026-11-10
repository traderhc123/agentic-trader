// Updating the agent's own code from /v2, with the classic dashboard's rules:
//   - one quiet GET /api/update/check per page load; it never installs;
//   - the update is offered only for the exact commit that check reported
//     (`target`), and POST /api/update/pull sends that commit and nothing
//     else ({expect: target}); the agent refuses if its remote moved since;
//   - nothing is ever tried again by itself.
// Plain functions (the fetch is handed in), so the Node tests can run them.
//
// What GET /api/update/check can answer (webui.check_code_updates and
// snapshot_update.check), and what this page does with it:
//   HTTP 401/403 (login expired)                     -> no badge, quiet line
//   no answer / timeout / not JSON                   -> no badge, quiet line
//   {ok:false, error}  install folder not writable,
//     unrecognised remote, GitHub unreachable or
//     rate-limited, no commits, any other failure    -> no badge, the agent's words, quietly
//   {ok:true, behind:0}                              -> no badge ("newest version")
//   {ok:true, behind:n>0, target, summary}           -> badge "Update available · n changes"
//   ... mode:"snapshot" (zip install), same shape    -> the same badge
//   ... version_unknown:true (snapshot: this install's
//     commit isn't recorded, so n is not a count)    -> badge without a count
//   behind>0 without a full commit id in `target`    -> no badge (nothing exact to install)
//   a diverged checkout (local commits) reports
//     behind>0 like any other; the agent refuses the
//     pull ("cannot fast-forward") and that is shown -> no retry, no button after
import { UPDATE } from "./copy.ts";
import { browserFetch, postJson, type Fetch } from "./http.ts";

/** the classic page's limits: a check can wait on GitHub, a pull on pip too */
export const CHECK_MS = 150_000;
export const PULL_MS = 400_000;
/** after a pull: how often to look for the restarted agent, and for how long
 *  (it may first wait up to two minutes for an order that is being placed) */
export const RESTART_POLL_MS = 1500;
export const RESTART_GIVE_UP_MS = 5 * 60_000;

export const CHECK_PATH = "/api/update/check";
export const PULL_PATH = "/api/update/pull";

/** What the owner is shown, and the only thing a pull may install. */
export interface Offer {
  /** the commit id exactly as the agent reported it */
  target: string;
  /** the first 12 characters, as the classic page shows it */
  short: string;
  /** how many changes; null when the agent can't count them */
  count: number | null;
  /** the agent's list of changes, one line each */
  changes: string[];
  /** a zip install (no git): the agent downloads GitHub's zip of `target` */
  snapshot: boolean;
}

export type Check =
  | { kind: "checking" }
  | { kind: "current" }
  /** `text` is a whole sentence, said quietly (never as an alarm) */
  | { kind: "unavailable"; text: string }
  | { kind: "available"; offer: Offer };

export type Pulled =
  | { ok: true; restarting: boolean; note: string }
  /** fromAgent: `text` is the agent's own message, quoted as it is */
  | { ok: false; text: string; fromAgent: boolean };

export type Phase = "idle" | "installing" | "failed" | "restarting" | "installed" | "slow";

// git: 40 hex (sha-1) or 64 (sha-256); the zip install uses GitHub's sha-1
const COMMIT = /^[0-9a-f]{40}(?:[0-9a-f]{24})?$/i;

const isObj = (x: unknown): x is Record<string, unknown> => !!x && typeof x === "object" && !Array.isArray(x);
const words = (x: unknown) => (typeof x === "string" && x.trim() ? x.trim() : "");
const here = (why: string): Check => ({ kind: "unavailable", text: UPDATE.unavailableHere(why) });

/** Turn the check's answer into what the page shows. */
export function readCheck(http: number, body: unknown): Check {
  if (http === 401 || http === 403) return here(UPDATE.lockedCheck);
  if (!isObj(body)) return here(UPDATE.unreadable);
  if (body.ok !== true || http !== 200) {
    const err = words(body.error);
    if (err) return { kind: "unavailable", text: UPDATE.unavailable(err) };
    return here(http === 200 ? UPDATE.noReason : UPDATE.unreadable);
  }
  const behind = body.behind;
  if (typeof behind !== "number" || !Number.isInteger(behind) || behind < 0) return here(UPDATE.unreadable);
  if (behind === 0) return { kind: "current" };
  const target = typeof body.target === "string" ? body.target : "";
  if (!COMMIT.test(target)) return here(UPDATE.noVersion);
  const changes = typeof body.summary === "string" ? body.summary.split("\n").map((l) => l.trim()).filter(Boolean).slice(0, 60) : [];
  return {
    kind: "available",
    offer: {
      target,
      short: target.slice(0, 12),
      count: body.version_unknown === true ? null : behind,
      changes,
      snapshot: body.mode === "snapshot",
    },
  };
}

/** Turn the pull's answer into what the page shows. */
export function readPull(http: number, body: unknown): Pulled {
  if (http === 401 || http === 403) return { ok: false, text: UPDATE.pullLocked, fromAgent: false };
  if (!isObj(body)) return { ok: false, text: UPDATE.pullUnreadable(http), fromAgent: false };
  if (body.ok === true && http === 200) return { ok: true, restarting: body.restarting === true, note: words(body.note) };
  const err = words(body.error);
  if (err) return { ok: false, text: err, fromAgent: true };
  return http === 200 ? { ok: false, text: UPDATE.pullNoReason, fromAgent: false } : { ok: false, text: UPDATE.pullUnreadable(http), fromAgent: false };
}

/** The one request a pull sends: the commit the owner was shown. */
export function pullBody(offer: Offer): { expect: string } {
  return { expect: offer.target };
}

async function jsonOf(r: Response): Promise<unknown> {
  try {
    return await r.json();
  } catch {
    return null;
  }
}

/** The page load's one check. Never throws. */
export async function checkForUpdate(f: Fetch = browserFetch, ms = CHECK_MS): Promise<Check> {
  const ctl = new AbortController();
  const to = setTimeout(() => ctl.abort(), ms);
  try {
    const r = await f(CHECK_PATH, { cache: "no-store", signal: ctl.signal });
    return readCheck(r.status, await jsonOf(r));
  } catch (e) {
    return here((e as Error)?.name === "AbortError" ? UPDATE.checkTimeout(Math.round(ms / 1000)) : UPDATE.unreachable);
  } finally {
    clearTimeout(to);
  }
}

/** Install exactly `offer.target`, through the same POST the commands use.
 *  Never throws; one attempt only. */
export async function installUpdate(offer: Offer, f: Fetch = browserFetch, ms = PULL_MS): Promise<Pulled> {
  const ctl = new AbortController();
  const to = setTimeout(() => ctl.abort(), ms);
  try {
    const r = await postJson(PULL_PATH, pullBody(offer), ctl.signal, f);
    return readPull(r.status, await jsonOf(r));
  } catch {
    // the agent may still be installing: this page can't say it did not
    return { ok: false, text: UPDATE.pullNoAnswer, fromAgent: false };
  } finally {
    clearTimeout(to);
  }
}

/** The header badge: shown only once the check says there is an exact
 *  version to install, nothing is under way, and the agent can be reached. */
export function badgeFor(check: Check, phase: Phase, reachable: boolean): { count: number | null } | null {
  if (check.kind !== "available" || phase !== "idle" || !reachable) return null;
  return { count: check.offer.count };
}

/** A failed pull in words: the agent's own message is quoted as its own. */
export function failureText(p: Pulled): string {
  if (p.ok) return "";
  return p.fromAgent ? UPDATE.agentSaid(p.text) : p.text;
}

/** The quiet line under Settings. `review`: the button that opens the dialog.
 *  `detail`: the failure in words (failed), or the agent's note (installed). */
export function updateLine(check: Check, phase: Phase, detail = ""): { text: string; review: boolean } {
  if (phase === "installing") return { text: UPDATE.busy, review: false };
  if (phase === "restarting") return { text: UPDATE.restarting, review: false };
  if (phase === "slow") return { text: UPDATE.slow, review: false };
  if (phase === "installed") return { text: detail ? UPDATE.installed(detail) : UPDATE.installedPlain, review: false };
  if (phase === "failed") return { text: `${UPDATE.failedTitle}. ${detail || UPDATE.pullNoReason} ${UPDATE.againHint}`, review: false };
  switch (check.kind) {
    case "checking":
      return { text: UPDATE.checking, review: false };
    case "current":
      return { text: UPDATE.current, review: false };
    case "unavailable":
      return { text: check.text, review: false };
    case "available":
      return { text: UPDATE.available(check.offer.count), review: true };
  }
}

/** After a pull that restarts the agent: is the agent back, on the new code?
 *  `before`: its start time when the pull answered. Back = it answers again
 *  after this page saw it gone, or it reports a different start time. */
export function backOnNewVersion(before: string | null, sawDown: boolean, sample: { answered: boolean; startedAt: string | null }): boolean {
  if (!sample.answered) return false;
  return sawDown || (sample.startedAt !== null && sample.startedAt !== before);
}
