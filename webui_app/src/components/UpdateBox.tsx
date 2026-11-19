import { RefreshCw } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { api, apiTimed } from "../lib/api";
import { cn } from "../lib/cn";
import type { AutoUpdate } from "../lib/settingsApi";
import { Button } from "./ui/button";

const AUTO_TEXT =
  "Installs official updates automatically between 8 PM and 8 AM ET or on weekends, only when no order is being placed. Off by default.";

function autoWords(a: AutoUpdate | null | undefined): string {
  if (!a) return AUTO_TEXT;
  let t = a.text || AUTO_TEXT;
  if (a.last) t += ` Last automatic update (${a.last.at_et || a.last.at || ""}): ${a.last.ok ? "" : "FAILED: "}${a.last.text || ""}.`;
  if (a.on && a.note) t += ` ${a.note}`;
  return t;
}

/** Automatic updates (off by default): the same switch as /v2 Settings,
 *  saved through the same route. */
function AutoUpdateToggle({ auto }: { auto?: AutoUpdate | null }) {
  const [now, setNow] = useState<AutoUpdate | null>(auto ?? null);
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState("");
  const [lead, setLead] = useState("");
  useEffect(() => setNow(auto ?? null), [auto]);
  const flip = async (on: boolean) => {
    setSaving(true);
    setErr("");
    const r = await apiTimed("/api/v2/settings/updates", { auto_update: on }, 30000);
    setSaving(false);
    if (!r.ok) return setErr(r.error || "not saved");
    const a = (r.settings && r.settings.updates) as AutoUpdate | null | undefined;
    if (a) setNow(a);
    setLead(a ? (a.on ? "Saved: on. " : "Saved: off. ") : "Saved. ");
  };
  return (
    <div className="space-y-1">
      <label className="flex cursor-pointer items-center gap-2 text-fg2">
        <input type="checkbox" checked={!!now?.on} disabled={saving || auto == null} onChange={(e) => flip(e.target.checked)} />
        Install updates automatically
      </label>
      <span className="block [overflow-wrap:anywhere]">
        {lead}
        {autoWords(now)}
      </span>
      {err && <span className="block whitespace-pre-line text-down">{err}</span>}
    </div>
  );
}

/** Update button: never silent. A ticking "checking… 12s" while the server
 *  works, a hard timeout, and every failure shown in words. Installs only the
 *  exact reviewed commit (`expect`). */
export function UpdateBox({ className, compact, auto }: { className?: string; compact?: boolean; auto?: AutoUpdate | null }) {
  const [busy, setBusy] = useState<string | null>(null);
  const [secs, setSecs] = useState(0);
  const [res, setRes] = useState<{ kind: "ok" | "err"; text: string; summary?: string; target?: string } | null>(null);
  const [canPull, setCanPull] = useState(false);
  const target = useRef("");
  const tick = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => () => { if (tick.current) clearInterval(tick.current); }, []);

  const start = (label: string) => {
    setBusy(label);
    setSecs(0);
    const t0 = Date.now();
    if (tick.current) clearInterval(tick.current);
    tick.current = setInterval(() => setSecs(Math.round((Date.now() - t0) / 1000)), 1000);
  };
  const stop = () => {
    if (tick.current) clearInterval(tick.current);
    tick.current = null;
    setBusy(null);
  };

  const check = async () => {
    setCanPull(false);
    setRes(null);
    start("Checking");
    // the owner pressed Check: a fresh check, not the remembered answer
    const r = await apiTimed("/api/update/check?force=1", undefined, 150000);
    stop();
    if (!r.ok) return setRes({ kind: "err", text: r.error || "update check failed" });
    if (!r.behind) return setRes({ kind: "ok", text: "Up to date" });
    target.current = r.target || "";
    setRes({
      kind: "ok",
      text: `${r.behind} update${r.behind > 1 ? "s" : ""} available. Review before installing:`,
      summary: r.summary || "",
      target: target.current,
    });
    setCanPull(true);
  };

  const pull = async () => {
    start("Installing");
    const r = await apiTimed("/api/update/pull", { expect: target.current }, 400000);
    stop();
    setRes(r.ok ? { kind: "ok", text: r.note } : { kind: "err", text: r.error || "update failed" });
    if (r.ok) setCanPull(false);
    if (r.ok && r.restarting) {
      // Server is re-execing onto the new code — wait until it's back, then
      // reload so the NEW UI is what's on screen.
      const waitBack = async () => {
        try {
          await api("/api/state");
          location.reload();
        } catch {
          setTimeout(waitBack, 1500);
        }
      };
      setTimeout(waitBack, 2500);
    }
  };

  return (
    <div className={cn("flex flex-col gap-2 text-xs text-muted", className)}>
      <div className={cn("flex gap-2", compact ? "flex-row flex-wrap" : "flex-col")}>
        <Button size="sm" onClick={check} disabled={!!busy} className={compact ? "" : "w-full"}>
          <RefreshCw className={cn("h-3.5 w-3.5", busy === "Checking" && "animate-spin")} />
          Check for updates
        </Button>
        {canPull && (
          <Button size="sm" variant="primary" onClick={pull} disabled={!!busy} className={compact ? "" : "w-full"}>
            Install update
          </Button>
        )}
      </div>
      {busy && (
        <span role="status">
          {busy}… {secs}s{secs >= 20 ? " (downloading from GitHub, this can take up to a minute)" : ""}
        </span>
      )}
      {!busy && res && (
        <div role="status" className="space-y-2 [overflow-wrap:anywhere]">
          <span className={res.kind === "ok" ? "text-up" : "whitespace-pre-line text-down"}>{res.text}</span>
          {res.summary != null && (
            <>
              <pre className="scroll-thin max-h-48 overflow-auto whitespace-pre-wrap rounded border border-line bg-sink p-2 font-mono text-[11px] text-fg2">
                {res.summary}
              </pre>
              <span className="block">
                Installs exactly <span className="font-mono">{(res.target || "").slice(0, 12)}</span> from
                github.com/traderhc123/agentic-trader
              </span>
            </>
          )}
        </div>
      )}
      <AutoUpdateToggle auto={auto} />
    </div>
  );
}
