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

// "expired" = AgentHC said the invoice expired unpaid. "lapsed" = it is past
// its validity locally but AgentHC never confirmed either way (e.g. tips were
// turned off mid-invoice), so we can't promise nothing was charged.
type View = "loading" | "tiers" | "invoice" | "paid" | "expired" | "lapsed" | "unavailable" | "error";

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
  const [unconfirmed, setUnconfirmed] = useState("");
  const poll = useRef<ReturnType<typeof setInterval> | null>(null);
  const alive = useRef(true);
  // Hash of the invoice on screen; a status reply for any other hash is stale.
  const curHash = useRef<string | null>(null);
  const inFlight = useRef(false);
  const intro = useRef<HTMLParagraphElement | null>(null);

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
    // Move keyboard / screen-reader focus into the panel when it opens.
    intro.current?.focus();
    loadTiers();
    return () => {
      alive.current = false;
      stopPoll();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const current = (cur: Inv) => alive.current && poll.current != null && curHash.current === cur.hash;

  const check = async (cur: Inv) => {
    if (!current(cur)) return;
    if (Date.now() > cur.shownAt + cur.validS * 1000 + 60000) {
      stopPoll();
      curHash.current = null;
      setView("lapsed");
      return;
    }
    if (Date.now() - cur.shownAt > HELP_AFTER_MS) setShowHelp(true);
    if (inFlight.current) return; // a slow reply is still out: don't stack polls
    inFlight.current = true;
    let r: TipStatus;
    try {
      r = await api<TipStatus>("/api/tip/status", { payment_hash: cur.hash });
    } catch {
      return; // agent briefly unreachable: keep polling
    } finally {
      inFlight.current = false;
    }
    // Cancelled, closed, or a newer invoice is on screen: this reply is stale.
    if (!current(cur)) return;
    if (r.ok && r.status === "paid") {
      stopPoll();
      curHash.current = null;
      setView("paid");
    } else if (r.ok && r.status === "expired") {
      stopPoll();
      curHash.current = null;
      setView("expired");
    } else if (r.ok) {
      setUnconfirmed("");
    } else if (r.unknown) {
      setUnconfirmed(r.error || "");
    }
    // pending, or {ok:false, retry}: keep polling
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
    setUnconfirmed("");
    setView("invoice");
    stopPoll();
    curHash.current = cur.hash;
    poll.current = setInterval(() => check(cur), POLL_MS);
  };

  const backToTiers = () => {
    stopPoll();
    curHash.current = null;
    setUnconfirmed("");
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
      <p ref={intro} tabIndex={-1} className="m-0 text-sm text-muted outline-none">
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
                  // An unavailable tier stays focusable (aria-disabled, click is a
                  // no-op in pick) so keyboard and screen-reader users hear why.
                  disabled={busy != null}
                  aria-disabled={!t.available || undefined}
                  aria-describedby={!t.available && t.reason ? `tip-reason-${t.amount_sats}` : undefined}
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
                {!t.available && t.reason && (
                  <span id={`tip-reason-${t.amount_sats}`} className="text-2xs leading-snug text-muted">
                    {t.reason}
                  </span>
                )}
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
                unconfirmed
                  ? unconfirmed
                  : showHelp
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

      {view === "lapsed" && (
        <div className="flex flex-wrap items-center gap-3">
          <Msg m={info("This invoice is past its 1 hour. If your wallet shows it paid, it reached AgentHC; if not, nothing was charged.")} />
          <Button onClick={backToTiers}>Get a new invoice</Button>
        </div>
      )}

      {view === "error" && (
        <div className="flex flex-wrap items-center gap-3">
          <Button onClick={backToTiers}>Try again</Button>
        </div>
      )}

      {msg && view !== "invoice" && view !== "paid" && view !== "expired" && view !== "lapsed" && (
        <p className="m-0 text-sm">
          <Msg m={msg} />
        </p>
      )}
    </Panel>
  );
}
