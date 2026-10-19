import { RefreshCw } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { api, apiTimed } from "../lib/api";
import { cn } from "../lib/cn";
import { Button } from "./ui/button";

/** Update button: never silent. A ticking "checking… 12s" while the server
 *  works, a hard timeout, and every failure shown in words. Installs only the
 *  exact reviewed commit (`expect`). */
export function UpdateBox({ className, compact }: { className?: string; compact?: boolean }) {
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
    const r = await apiTimed("/api/update/check", undefined, 150000);
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
    </div>
  );
}
