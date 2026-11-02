import { Copy, Zap } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { Button, LinkButton } from "../components/ui/button";
import { Panel } from "../components/ui/panel";
import { QR } from "../components/ui/qr";
import { Msg, err, info, ok, type MsgT } from "../components/ui/tag";
import { api, type TipInvoice, type TipOptions, type TipStatus, type TipTier } from "../lib/api";
import { cn } from "../lib/cn";
import { copyToClipboard } from "../lib/hooks";

// "Tip AgentHC": a fixed 100 / 500 / 1,000-sat thank-you, paid by the owner
// from their own Lightning wallet (QR / copy / lightning: link), like a
// prepaid pass. The agent's wallet never pays it; nothing is saved.

type View = "loading" | "tiers" | "invoice" | "paid" | "expired" | "unavailable" | "error";

interface Inv {
  bolt11: string;
  qr?: number[][] | null;
  hash: string;
  sats: number;
  usd: number | null;
  validS: number;
  shownAt: number;
}

const POLL_MS = 3000;
const HELP_AFTER_MS = 120000;

function amountText(sats: number, usd: number | null) {
  return `${sats.toLocaleString("en-US")} sats` + (usd != null ? ` (~$${usd.toFixed(2)})` : "");
}

export function TipPanel({ onClose }: { onClose: () => void }) {
  const [view, setView] = useState<View>("loading");
  const [tiers, setTiers] = useState<TipTier[]>([]);
  const [noQuote, setNoQuote] = useState(false);
  const [msg, setMsg] = useState<MsgT>(null);
  const [busy, setBusy] = useState<number | null>(null);
  const [inv, setInv] = useState<Inv | null>(null);
  const [copied, setCopied] = useState("");
  const [showHelp, setShowHelp] = useState(false);
  const poll = useRef<ReturnType<typeof setInterval> | null>(null);
  const alive = useRef(true);

  const stopPoll = () => {
    if (poll.current) clearInterval(poll.current);
    poll.current = null;
  };

  const loadTiers = async (keepMsg = false) => {
    if (!keepMsg) setMsg(null);
    let r: TipOptions;
    try {
      r = await api<TipOptions>("/api/tip/options");
    } catch {
      r = { ok: false, error: "Could not reach the agent on this computer. Is it still running?" };
    }
    if (!alive.current) return;
    if (r.ok && Array.isArray(r.tiers)) {
      setTiers(r.tiers);
      setNoQuote(r.tiers.some((t) => t.amount_usd_approx == null));
      setView("tiers");
    } else if (r.unavailable) {
      setMsg(info(r.error || ""));
      setView("unavailable");
    } else {
      setMsg(err(r.error || "Couldn't get the tip amounts."));
      setView("error");
    }
  };

  useEffect(() => {
    alive.current = true;
    loadTiers();
    return () => {
      alive.current = false;
      stopPoll();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const check = async (cur: Inv) => {
    if (Date.now() > cur.shownAt + cur.validS * 1000 + 60000) {
      stopPoll();
      setView("expired");
      return;
    }
    if (Date.now() - cur.shownAt > HELP_AFTER_MS) setShowHelp(true);
    let r: TipStatus;
    try {
      r = await api<TipStatus>("/api/tip/status", { payment_hash: cur.hash });
    } catch {
      return; // agent briefly unreachable: keep polling
    }
    if (!alive.current || !poll.current) return;
    if (r.ok && r.status === "paid") {
      stopPoll();
      setView("paid");
    } else if (r.ok && r.status === "expired") {
      stopPoll();
      setView("expired");
    }
    // pending, or {ok:false, retry}: nothing changes, keep polling
  };

  const pick = async (t: TipTier) => {
    if (busy != null || !t.available) return;
    setBusy(t.amount_sats);
    setMsg(null);
    let r: TipInvoice;
    try {
      r = await api<TipInvoice>("/api/tip/create", { amount_sats: t.amount_sats });
    } catch {
      r = { ok: false, error: "Could not reach the agent on this computer. Is it still running?" };
    }
    if (!alive.current) return;
    setBusy(null);
    if (!r.ok || !r.bolt11 || !r.payment_hash) {
      if (r.unavailable) {
        setMsg(info(r.error || ""));
        setView("unavailable");
        return;
      }
      setMsg(err(r.error || "Couldn't make a Lightning invoice right now. Try again in a minute."));
      if (r.refresh_tiers) loadTiers(true);
      return;
    }
    const cur: Inv = {
      bolt11: r.bolt11,
      qr: r.qr,
      hash: r.payment_hash,
      sats: r.amount_sats || t.amount_sats,
      usd: r.amount_usd_approx ?? null,
      validS: r.expires_in || 3600,
      shownAt: Date.now(),
    };
    setInv(cur);
    setCopied("");
    setShowHelp(false);
    setView("invoice");
    stopPoll();
    poll.current = setInterval(() => check(cur), POLL_MS);
  };

  const backToTiers = () => {
    stopPoll();
    setInv(null);
    setView("loading");
    loadTiers();
  };

  return (
    <Panel
      id="tip"
      title="Tip AgentHC"
      right={
        <Button variant="ghost" size="sm" onClick={onClose}>
          Close
        </Button>
      }
      bodyClass="p-4 space-y-3"
    >
      <p className="m-0 text-sm text-muted">
        Like what your agent does? Send a thank-you in sats from your own Lightning wallet. A tip buys nothing — no signals, no
        credit, no refunds. Your agent's wallet never pays it.
      </p>

      {view === "loading" && (
        <p role="status" className="m-0 text-sm text-muted">
          Getting tip amounts…
        </p>
      )}

      {view === "tiers" && (
        <>
          <div role="group" aria-label="Tip amounts" className="grid grid-cols-[repeat(auto-fit,minmax(110px,1fr))] gap-2">
            {tiers.map((t) => (
              <div key={t.amount_sats} className="flex min-w-0 flex-col gap-1">
                <button
                  type="button"
                  data-sats={t.amount_sats}
                  onClick={() => pick(t)}
                  disabled={!t.available || busy != null}
                  aria-label={t.label}
                  aria-busy={busy === t.amount_sats}
                  className={cn(
                    "flex min-w-0 flex-col items-start rounded-md border px-3 pb-2.5 pt-3 text-left transition-colors",
                    !t.available
                      ? "cursor-not-allowed border-line bg-panel2 opacity-50"
                      : busy === t.amount_sats
                        ? "border-accent bg-accent/[0.08] shadow-[inset_0_0_0_1px_rgb(var(--accent))]"
                        : "border-line2 bg-panel2 hover:border-accent/50 hover:bg-raise disabled:opacity-60",
                  )}
                >
                  <span className="font-mono text-xl font-medium leading-none tracking-[-0.02em] text-fg">
                    {t.amount_sats.toLocaleString("en-US")}
                  </span>
                  <span className="mt-1 text-xs text-muted">sats</span>
                  <span className="mt-3 w-full border-t border-line pt-2 font-mono text-xs text-fg2">
                    {busy === t.amount_sats ? "Making invoice…" : t.amount_usd_approx != null ? `≈ $${t.amount_usd_approx.toFixed(2)}` : " "}
                  </span>
                </button>
                {!t.available && t.reason && <span className="text-2xs leading-snug text-muted">{t.reason}</span>}
              </div>
            ))}
          </div>
          {noQuote && <p className="m-0 text-xs text-muted">Dollar amounts are approximate and appear when AgentHC has a current Bitcoin price.</p>}
        </>
      )}

      {view === "invoice" && inv && (
        <>
          <div className="flex gap-4 rounded-md border border-line2 bg-sink p-4 max-sm:flex-col max-sm:items-center">
            <QR matrix={inv.qr} />
            <div className="w-full min-w-0 flex-1">
              <p className="m-0 font-mono text-sm font-medium text-fg">Scan or copy to pay {amountText(inv.sats, inv.usd)}</p>
              <p className="m-0 mt-1 text-xs text-muted">Pay from any Lightning wallet on your phone or computer. Good for 1 hour.</p>
              <pre
                tabIndex={0}
                aria-label="Lightning invoice"
                className="scroll-thin mt-3 max-h-16 overflow-auto whitespace-pre-wrap break-all rounded border border-line bg-panel px-2.5 py-2 font-mono text-[11px] leading-normal text-fg2"
              >
                {inv.bolt11}
              </pre>
              <div className="mt-3 flex flex-wrap items-center gap-2">
                <Button
                  onClick={async () => setCopied((await copyToClipboard(inv.bolt11)) ? "Copied" : "Clipboard blocked — select the text and copy it.")}
                >
                  <Copy className="h-3.5 w-3.5" />
                  Copy invoice
                </Button>
                <LinkButton href={"lightning:" + inv.bolt11}>
                  <Zap className="h-3.5 w-3.5" />
                  Open in wallet
                </LinkButton>
                <Button variant="ghost" onClick={backToTiers}>
                  Cancel
                </Button>
                {copied && <span className="text-xs text-up">{copied}</span>}
              </div>
            </div>
          </div>
          <p className="m-0 text-sm">
            <Msg
              m={info(
                showHelp
                  ? `Still waiting. Check your wallet sent exactly ${inv.sats.toLocaleString("en-US")} sats and has enough balance plus a small network fee. If it failed, nothing was charged.`
                  : "Waiting for payment…",
              )}
            />
          </p>
        </>
      )}

      {view === "paid" && inv && (
        <div className="flex flex-wrap items-center gap-3">
          <Msg m={ok(`Got it — thank you! Your ${inv.sats.toLocaleString("en-US")}-sat tip reached AgentHC.`)} />
          <Button onClick={onClose}>Close</Button>
        </div>
      )}

      {view === "expired" && (
        <div className="flex flex-wrap items-center gap-3">
          <Msg m={info("This invoice expired before it was paid. Nothing was charged.")} />
          <Button onClick={backToTiers}>Get a new invoice</Button>
        </div>
      )}

      {view === "error" && (
        <div className="flex flex-wrap items-center gap-3">
          <Button onClick={backToTiers}>Try again</Button>
        </div>
      )}

      {msg && view !== "invoice" && view !== "paid" && view !== "expired" && (
        <p className="m-0 text-sm">
          <Msg m={msg} />
        </p>
      )}
    </Panel>
  );
}
