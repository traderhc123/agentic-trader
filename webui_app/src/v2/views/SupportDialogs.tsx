// The dialogs of Settings → Help and support. Their buttons live in
// Settings.tsx (one home each); these are what the buttons open.
//
//   Connection check  GET /api/help/netcheck (the classic help tool, as it is)
//   Support report    GET /api/help/report: keys, tokens, invoices and wallet
//                     ids are taken out by the agent before it answers
//   Tip AgentHC       GET /api/tip/options, POST /api/tip/create, POST
//                     /api/tip/status: an invoice the owner pays from their
//                     own wallet app. The agent's wallet never pays it, and
//                     nothing is saved.
//   Your agreement    GET /api/v2/agreement (the text only)
// None of them changes a setting, buys or sells anything.
import { useEffect, useRef, useState } from "react";
import type { Agreement, HelpText, TipCheck, TipInvoice, TipOptions, TipTier } from "../../lib/settingsApi";
import { API, getJson, send, type Sent } from "../lib/http";
import { tipLapsed } from "../lib/settings";
import { count, usd } from "../lib/format";
import { Dialog, Icon } from "../ui";
import { PayCode } from "./Pay";

function Failed({ text }: { text: string }) {
  return (
    <div className="callout callout-bad" role="alert">
      <p className="err">
        <Icon name="alert" small />
        {text}
      </p>
    </div>
  );
}

function Waiting({ text }: { text: string }) {
  return (
    <p className="wait" role="status" aria-live="polite">
      <span className="spin" aria-hidden="true" />
      {text}
    </p>
  );
}

/** The connection check or the support report: asked for when the dialog
 *  opens, shown as the agent wrote it. */
export function HelpTextDialog({ kind, onClose }: { kind: "netcheck" | "report"; onClose: () => void }) {
  const [got, setGot] = useState<{ text: string } | { error: string } | null>(null);
  const [copied, setCopied] = useState("");
  const ask = async () => {
    setGot(null);
    const r = await getJson<HelpText>(kind === "netcheck" ? API.netcheck : API.report);
    if (!r.ok) return setGot({ error: r.error });
    if (r.body.ok !== true || typeof r.body.text !== "string") return setGot({ error: r.body.error || "The agent could not make it. Try again." });
    setGot({ text: r.body.text });
  };
  useEffect(() => {
    ask();
  }, []); // eslint-disable-line react-hooks/exhaustive-deps
  const report = kind === "report";
  return (
    <Dialog
      title={report ? "Support report" : "Connection check"}
      lead={
        report
          ? "Paste this into a support request. The agent took out your keys, tokens, invoices and wallet ids before showing it; read it before you send it."
          : "Whether this computer can reach AgentHC (signals) and GitHub (updates) right now. Nothing is changed."
      }
      onClose={onClose}
      foot={
        <>
          <button className="btn" onClick={onClose} data-autofocus>
            Close
          </button>
          {got && "text" in got && report && (
            <button
              className="btn btn-primary"
              onClick={async () => {
                try {
                  await navigator.clipboard.writeText(got.text);
                  setCopied("Copied.");
                } catch {
                  setCopied("This browser blocked copying: select the text and copy it.");
                }
              }}
            >
              Copy the report
            </button>
          )}
          {got && !report && (
            <button className="btn btn-primary" onClick={ask}>
              Check again
            </button>
          )}
        </>
      }
    >
      {got == null ? (
        <Waiting text={report ? "Making the report…" : "Checking (this takes a few seconds)…"} />
      ) : "error" in got ? (
        <Failed text={got.error} />
      ) : (
        <pre className="helptext" tabIndex={0} aria-label={report ? "Support report" : "Connection check"}>
          {got.text}
        </pre>
      )}
      {copied && (
        <p className="small" role="status">
          {copied}
        </p>
      )}
    </Dialog>
  );
}

/** The agreement on file, to read again (accepting it is the setup
 *  wizard's job). */
export function AgreementDialog({ onClose }: { onClose: () => void }) {
  const [got, setGot] = useState<Agreement | { error: string } | null>(null);
  useEffect(() => {
    getJson<Agreement>(API.agreement).then((r) =>
      setGot(!r.ok ? { error: r.error } : typeof r.body.text === "string" ? r.body : { error: "The agent answered in a form this page can't read. Try again." }),
    );
  }, []);
  return (
    <Dialog
      title="Your agreement"
      lead={got && "text" in got ? `The agreement as it stands now (terms version ${got.terms_version}).` : undefined}
      onClose={onClose}
      foot={
        <button className="btn" onClick={onClose} data-autofocus>
          Close
        </button>
      }
    >
      {got == null ? <Waiting text="Reading the agreement…" /> : "error" in got ? <Failed text={got.error} /> : <pre className="helptext helptext-long">{got.text}</pre>}
    </Dialog>
  );
}

// ── tip ─────────────────────────────────────────────────────────────────────

type TipStep =
  | { kind: "loading" }
  | { kind: "tiers"; tiers: TipTier[]; btc: number | null; note: string }
  | { kind: "unavailable"; text: string }
  | { kind: "error"; text: string }
  | { kind: "invoice"; inv: TipInvoice; shownAt: number }
  | { kind: "paid"; inv: TipInvoice }
  | { kind: "expired" }
  | { kind: "lapsed" };

const POLL_MS = 3000;
const HELP_AFTER_MS = 120000;

/** Tip AgentHC: pick an amount, then pay the invoice from your own wallet
 *  app. "Thank you" only when AgentHC says the invoice was paid. */
export function TipDialog({ onClose }: { onClose: () => void }) {
  const [step, setStep] = useState<TipStep>({ kind: "loading" });
  const [busy, setBusy] = useState<number | null>(null);
  const [wait, setWait] = useState("Waiting for your payment…");
  const timer = useRef<ReturnType<typeof setInterval>>();
  const shown = useRef<string | null>(null); // the invoice on screen
  const out = useRef(false); // a status check is still out

  const stop = () => {
    clearInterval(timer.current);
    timer.current = undefined;
    shown.current = null;
  };
  const tiers = async (note = "") => {
    stop();
    setStep({ kind: "loading" });
    const r = await getJson<TipOptions>(API.tipOptions);
    if (!r.ok) return setStep({ kind: "error", text: r.error });
    const b = r.body;
    if (b.ok && Array.isArray(b.tiers)) return setStep({ kind: "tiers", tiers: b.tiers, btc: b.quote?.btc_usd ?? null, note });
    if (!b.ok && b.unavailable) return setStep({ kind: "unavailable", text: b.error || "Tips are not open yet." });
    setStep({ kind: "error", text: (!b.ok && b.error) || "Couldn't get the tip amounts. Try again." });
  };
  useEffect(() => {
    tiers();
    return stop;
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const watch = (inv: TipInvoice, shownAt: number) => {
    stop();
    shown.current = inv.payment_hash;
    setWait("Waiting for your payment…");
    timer.current = setInterval(async () => {
      if (shown.current !== inv.payment_hash) return;
      const now = Date.now();
      if (tipLapsed(shownAt, inv.expires_in, now)) {
        stop();
        return setStep({ kind: "lapsed" });
      }
      if (out.current) return;
      out.current = true;
      let r: Sent;
      try {
        r = await send(API.tipStatus, { payment_hash: inv.payment_hash });
      } finally {
        out.current = false;
      }
      if (shown.current !== inv.payment_hash) return; // closed, or another invoice
      const c = r as TipCheck;
      if (c.ok && c.status === "paid") {
        stop();
        return setStep({ kind: "paid", inv });
      }
      if (c.ok && c.status === "expired") {
        stop();
        return setStep({ kind: "expired" });
      }
      if (!c.ok) return setWait(`Waiting for your payment… (the last check said: ${c.error})`);
      setWait(
        now - shownAt > HELP_AFTER_MS
          ? `Still waiting. Check that your wallet sent exactly ${count(inv.amount_sats)} sats and has enough for a small network fee too. If the payment failed, nothing was charged.`
          : "Waiting for your payment…",
      );
    }, POLL_MS);
  };

  const pick = async (t: TipTier) => {
    if (!t.available || busy != null) return;
    setBusy(t.amount_usd);
    const r = await send<TipInvoice>(API.tipCreate, { amount_usd: t.amount_usd });
    setBusy(null);
    if (!r.ok) {
      const x = r as { error: string; refresh_tiers?: boolean; unavailable?: boolean };
      if (x.unavailable) return setStep({ kind: "unavailable", text: x.error });
      if (x.refresh_tiers) return tiers(x.error);
      return setStep((s) => (s.kind === "tiers" ? { ...s, note: x.error } : s));
    }
    const inv = r as unknown as TipInvoice;
    if (typeof inv.bolt11 !== "string" || typeof inv.payment_hash !== "string") return tiers("The agent did not hand back a payment code. Nothing was paid. Try again.");
    const at = Date.now();
    setStep({ kind: "invoice", inv, shownAt: at });
    watch(inv, at);
  };

  const close = () => {
    stop();
    onClose();
  };
  const foot =
    step.kind === "invoice" ? (
      <>
        <button className="btn" onClick={close} data-autofocus>
          Close
        </button>
        <button className="btn" onClick={() => tiers()}>
          Pick another amount
        </button>
      </>
    ) : (
      <button className={`btn ${step.kind === "paid" ? "btn-primary" : ""}`} onClick={close} data-autofocus>
        {step.kind === "paid" ? "Done" : "Close"}
      </button>
    );

  return (
    <Dialog
      title={step.kind === "paid" ? "Thank you" : "Tip AgentHC"}
      lead={
        step.kind === "tiers" || step.kind === "loading"
          ? "A thank-you to AgentHC of $100, $500 or $1,000, paid in sats at the current Bitcoin price from your own Lightning wallet app. A tip buys nothing: no signals, no credit, no refunds. Your agent's wallet never pays it."
          : undefined
      }
      onClose={close}
      foot={foot}
    >
      {step.kind === "loading" && <Waiting text="Asking AgentHC for the tip amounts…" />}
      {step.kind === "unavailable" && <p role="status">{step.text}</p>}
      {step.kind === "error" && (
        <>
          <Failed text={step.text} />
          <div>
            <button className="btn btn-sm" onClick={() => tiers()}>
              Try again
            </button>
          </div>
        </>
      )}
      {step.kind === "tiers" && (
        <>
          <div className="pass-opts" role="group" aria-label="Tip amounts">
            {step.tiers.map((t) => (
              <button
                key={t.amount_usd}
                type="button"
                className="pass-opt tip-opt"
                onClick={() => pick(t)}
                disabled={busy != null}
                aria-disabled={!t.available || undefined}
                aria-busy={busy === t.amount_usd || undefined}
              >
                <b>{usd(t.amount_usd, 0)}</b>
                <span>{busy === t.amount_usd ? "Making the code…" : t.amount_sats != null ? `about ${count(Math.round(t.amount_sats / 100) * 100)} sats` : "in sats"}</span>
                {!t.available && t.reason && <em>{t.reason}</em>}
              </button>
            ))}
          </div>
          <p className="small muted">
            {step.btc != null ? `Sats at AgentHC's Bitcoin price (${usd(step.btc, 0)} a bitcoin). ` : ""}The payment code holds the exact sats for 1 hour.
          </p>
          {step.note && <Failed text={step.note} />}
        </>
      )}
      {step.kind === "invoice" && (
        <>
          <PayCode bolt11={step.inv.bolt11} qr={step.inv.qr}>
            <dl className="facts">
              <dt>Tip</dt>
              <dd>
                {count(step.inv.amount_sats)} sats
                <small>
                  {usd(step.inv.amount_usd, 0)} at {usd(step.inv.btc_usd, 0)} a bitcoin. The amount stays fixed while the code is good (1 hour).
                </small>
              </dd>
            </dl>
          </PayCode>
          <Waiting text={wait} />
          <p className="small muted">A payment this big can fail if your wallet can't send that much at once. If it fails, nothing is charged.</p>
        </>
      )}
      {step.kind === "paid" && (
        <p role="status">
          Got it. Your {usd(step.inv.amount_usd, 0)} tip ({count(step.inv.amount_sats)} sats) reached AgentHC.
        </p>
      )}
      {step.kind === "expired" && <p role="status">This payment code expired before it was paid. Nothing was charged.</p>}
      {step.kind === "lapsed" && (
        <p role="status">This payment code is past its hour. If your wallet shows it paid, it reached AgentHC; if not, nothing was charged.</p>
      )}
    </Dialog>
  );
}
