// The update flow's state for the shell: one check per page load, then an
// install only when the owner presses Update now (the rules and the answers
// are in lib/update.ts). After a pull that restarts the agent, the band shows
// "Restarting" and then the lost link from the status feed as usual; this
// hook only reloads the page once the agent is back, so the new version's
// page is the one on screen.
import { useCallback, useEffect, useRef, useState } from "react";
import {
  RESTART_GIVE_UP_MS,
  RESTART_POLL_MS,
  backOnNewVersion,
  checkForUpdate,
  failureText,
  installUpdate,
  type Check,
  type Phase,
} from "./lib/update";

export interface UpdateFeed {
  check: Check;
  phase: Phase;
  /** the failure in words (failed), or the agent's note (installed, restarting) */
  detail: string;
  /** when the install was sent (ms), for the ticking counter */
  since: number;
  /** install the version the check reported; nothing happens otherwise */
  install: (startedAt: string | null) => Promise<void>;
}

const STATUS_TIMEOUT_MS = 3000;

async function sample(): Promise<{ answered: boolean; startedAt: string | null }> {
  const ctl = new AbortController();
  const to = setTimeout(() => ctl.abort(), STATUS_TIMEOUT_MS);
  try {
    const r = await fetch("/api/v2/status", { cache: "no-store", signal: ctl.signal });
    let startedAt: string | null = null;
    try {
      const j = (await r.json()) as { agent?: { started_at?: unknown } } | null;
      if (j && j.agent && typeof j.agent.started_at === "string") startedAt = j.agent.started_at;
    } catch {
      /* answered, but not with a status */
    }
    return { answered: true, startedAt };
  } finally {
    clearTimeout(to);
  }
}

export function useUpdate(): UpdateFeed {
  const [check, setCheck] = useState<Check>({ kind: "checking" });
  const [phase, setPhase] = useState<Phase>("idle");
  const [detail, setDetail] = useState("");
  const [since, setSince] = useState(0);
  const checked = useRef(false);
  const before = useRef<string | null>(null);
  const sent = useRef(false);

  // one quiet check per page load (the ref keeps a second effect run in
  // development from asking twice)
  useEffect(() => {
    if (checked.current) return;
    checked.current = true;
    checkForUpdate().then(setCheck);
  }, []);

  const install = useCallback(
    async (startedAt: string | null) => {
      // one install per page: a second press (or a double click) sends nothing
      if (check.kind !== "available" || phase !== "idle" || sent.current) return;
      sent.current = true;
      before.current = startedAt;
      setSince(Date.now());
      setPhase("installing");
      const r = await installUpdate(check.offer);
      if (!r.ok) {
        setDetail(failureText(r));
        setPhase("failed");
        return;
      }
      setDetail(r.note);
      setPhase(r.restarting ? "restarting" : "installed");
    },
    [check, phase],
  );

  // after a restarting pull: look for the agent until it is back on the new
  // code, then reload; give up (and say so) after a while
  useEffect(() => {
    if (phase !== "restarting") return;
    let sawDown = false;
    let done = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const t0 = Date.now();
    const look = async () => {
      if (done) return;
      if (Date.now() - t0 > RESTART_GIVE_UP_MS) {
        setPhase("slow");
        return;
      }
      try {
        const s = await sample();
        if (!done && backOnNewVersion(before.current, sawDown, s)) {
          done = true;
          location.reload();
          return;
        }
      } catch (e) {
        // refused = gone; a slow answer is not proof it went away
        if ((e as Error)?.name !== "AbortError") sawDown = true;
      }
      if (!done) timer = setTimeout(look, RESTART_POLL_MS);
    };
    timer = setTimeout(look, RESTART_POLL_MS);
    return () => {
      done = true;
      clearTimeout(timer);
    };
  }, [phase]);

  return { check, phase, detail, since, install };
}
