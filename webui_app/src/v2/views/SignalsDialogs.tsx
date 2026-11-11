// The dialogs of Signals & pass → Signal source and → Payment wallet. Their
// buttons live in Signals.tsx (one home each); these are what they open.
//
//   Source         POST /api/v2/settings/source {source, url}: two steps
//   Which signals  POST /api/v2/settings/signals {scope}: two steps
//   Key            POST /api/v2/settings/source {agenthc_key}: saved straight
//                  away, never shown again (only "set, ending 1234")
//   Make / replace the agent's wallet   POST /api/v2/wallet/create: two steps
//   Add sats       POST /api/v2/wallet/fund {sats}: two steps, then a
//                  Lightning invoice the owner pays from their own wallet app
//                  (GET /api/wallet/quote, reused, says what it is worth)
//   Own wallet     POST /api/v2/wallet/connect {url, admin_key}: two steps
//   Buys a day-pass by itself   POST /api/v2/wallet/recurring {on}: two steps
// After each, the page reads the settings again and shows those.
import { useEffect, useId, useState } from "react";
import type { FundInvoice, Preview, SettingsView, WalletBalance, WalletQuote } from "../../lib/settingsApi";
import { FieldError, ReviewDialog, askPreview } from "../Forms";
import { API, getJson, send } from "../lib/http";
import { readAddress, readSats, scopeWords, sourceWords } from "../lib/settings";
import { count, plural, usd } from "../lib/format";
import { Dialog, Icon } from "../ui";
import type { Page } from "../App";
import { Asking, usePreview } from "./AgentDialogs";
import { PayCode } from "./Pay";

const saved = async (page: Page, text: (v: SettingsView) => string) => {
  const nv = await page.settings.reload();
  page.refresh();
  page.say(true, nv ? text(nv) : "Saved. This page could not read your settings back yet.");
};

function TestServer({ text }: { text: string | null | undefined }) {
  if (!text) return null;
  return (
    <div className="callout callout-warn" role="note">
      <p>
        <Icon name="alert" small />
        {text}
      </p>
    </div>
  );
}

// ── the signal source ───────────────────────────────────────────────────────

const SOURCES: [string, string, string][] = [
  ["agenthc", "AgentHC Day Trade Ideas", "AgentHC's signals, read with signal days, a subscription key or a payment wallet."],
  ["manual", "Your own commands file", "Signals you write yourself into commands.jsonl in the agent's folder."],
  ["url", "A JSON feed", "Another feed's address (https://)."],
];

export function SourceDialog({ page, onClose }: { page: Page; onClose: () => void }) {
  const v = page.settings.view as SettingsView;
  const [pick, setPick] = useState(v.signals.source ?? "agenthc");
  const [url, setUrl] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const [review, setReview] = useState<{ payload: Record<string, unknown>; preview: Preview } | null>(null);
  const id = useId();
  const ask = async () => {
    const payload: Record<string, unknown> = { source: pick };
    if (pick === "url") {
      const a = readAddress(url);
      if (!a.ok) return setErr(a.error);
      payload.url = a.value;
    }
    setErr("");
    setBusy(true);
    const a = await askPreview(API.source, payload);
    setBusy(false);
    if (a.kind === "review") setReview({ payload, preview: a.preview });
    else setErr(a.kind === "error" ? a.error : a.note);
  };
  if (review)
    return (
      <ReviewDialog
        path={API.source}
        payload={review.payload}
        first={review.preview}
        title="Change the signal source?"
        lead="The agent acts on the new source's signals from its next read."
        cancel="Keep the current source"
        confirm={`Switch to ${sourceWords({ source: String(review.payload.source), site: null })}`}
        live={page.s.mode === "live"}
        onClose={() => setReview(null)}
        onDone={async () => {
          await saved(page, (nv) => `Saved. Signal source: ${sourceWords({ source: nv.signals.source, site: nv.signals.source_site })}.`);
          onClose();
        }}
      >
        {(p) =>
          p.real_positions ? (
            <p className="small">
              You hold {plural(p.real_positions, "real position")}. Check that the new source sends exit signals for{" "}
              {p.real_positions === 1 ? "it" : "them"}, or sell {p.real_positions === 1 ? "it" : "them"} yourself.
            </p>
          ) : null
        }
      </ReviewDialog>
    );
  return (
    <Dialog
      title="Signal source"
      lead="Where the agent reads its trade signals. Changing it never switches Practice or LIVE."
      onClose={() => !busy && onClose()}
      foot={
        <>
          <button className="btn" onClick={onClose} disabled={busy} data-autofocus>
            Cancel
          </button>
          <button className="btn btn-primary" id="dlg-ok" onClick={ask} disabled={busy}>
            {busy ? "Asking the agent…" : "Review"}
          </button>
        </>
      }
    >
      <div className="choices" role="radiogroup" aria-label="Signal source">
        {SOURCES.map(([k, name, help]) => (
          <label key={k} className="check">
            <input type="radio" name={`${id}-src`} checked={pick === k} onChange={() => (setPick(k), setErr(""))} />
            <span>
              <b>{name}</b>
              <small className="help">{help}</small>
            </span>
          </label>
        ))}
      </div>
      {pick === "url" && (
        <div className="field">
          <label htmlFor={`${id}-u`}>The feed's address</label>
          <input id={`${id}-u`} className="input" autoComplete="off" spellCheck={false} placeholder="https://" value={url} onChange={(e) => (setUrl(e.target.value), setErr(""))} />
          {v.signals.source === "url" && <span className="help">Saved now: a feed at {v.signals.source_site ?? "an address not shown"}. Type the address again to change it.</span>}
        </div>
      )}
      <FieldError id={`${id}-e`} text={err} />
    </Dialog>
  );
}

/** Which AgentHC signals: the server's preview of the switch, then confirm. */
export function ScopeDialog({ page, onClose }: { page: Page; onClose: () => void }) {
  const v = page.settings.view as SettingsView;
  const next = v.signals.include_other_trades ? "main" : "all";
  const payload = { scope: next };
  const ask = usePreview(API.signals, payload);
  if (ask.kind !== "review") return <Asking title="Which signals" ask={ask} onClose={onClose} />;
  return (
    <ReviewDialog
      path={API.signals}
      payload={payload}
      first={ask.preview}
      title={next === "all" ? "Take other trades too?" : "Take the main pick only?"}
      lead={
        next === "all"
          ? "AgentHC's other trades are more signals each day, sized by the other-trades size on Rules & limits."
          : "The agent stops opening other trades. Other trades it already holds are still sold on their exit signals."
      }
      cancel="Keep it as it is"
      confirm={next === "all" ? "Take other trades too" : "Main pick only"}
      live={page.s.mode === "live"}
      onClose={onClose}
      onDone={async () => {
        await saved(page, (nv) => `Saved. Signals: ${scopeWords(nv.signals.include_other_trades ? "all" : "main").toLowerCase()}.`);
        onClose();
      }}
    />
  );
}

/** The AgentHC subscription key: write-only, saved straight away. */
export function KeyDialog({ page, onClose }: { page: Page; onClose: () => void }) {
  const v = page.settings.view as SettingsView;
  const [key, setKey] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const id = useId();
  const save = async () => {
    const k = key.trim();
    if (!k) return setErr("Paste the key first.");
    setErr("");
    setBusy(true);
    const r = await send(API.source, { agenthc_key: k });
    setBusy(false);
    if (!r.ok) return setErr(r.error);
    await saved(page, (nv) => `Saved. Subscription key: ${nv.signals.api_key.set ? `set${nv.signals.api_key.last4 ? `, ending ${nv.signals.api_key.last4}` : ""}` : "not set"}.`);
    onClose();
  };
  return (
    <Dialog
      title="Subscription key"
      lead="An AgentHC Premium key reads the signals instead of signal days. The agent keeps it; this page never shows it again."
      onClose={() => !busy && onClose()}
      foot={
        <>
          <button className="btn" onClick={onClose} disabled={busy} data-autofocus>
            Cancel
          </button>
          <button className="btn btn-primary" id="dlg-ok" onClick={save} disabled={busy}>
            {busy ? "Saving…" : "Save the key"}
          </button>
        </>
      }
    >
      <div className="field">
        <label htmlFor={`${id}-k`}>Key</label>
        <input
          id={`${id}-k`}
          className="input"
          type="password"
          autoComplete="off"
          spellCheck={false}
          value={key}
          onChange={(e) => (setKey(e.target.value), setErr(""))}
          aria-invalid={!!err || undefined}
          aria-describedby={err ? `${id}-e` : undefined}
        />
        {v.signals.api_key.set && <span className="help">Saved now: set{v.signals.api_key.last4 ? `, ending ${v.signals.api_key.last4}` : ""}. A new key replaces it.</span>}
        <FieldError id={`${id}-e`} text={err} />
      </div>
    </Dialog>
  );
}

// ── the payment wallet ──────────────────────────────────────────────────────

/** Make the agent's own wallet, or replace it (the server's preview first). */
export function CreateWalletDialog({ page, onClose }: { page: Page; onClose: () => void }) {
  const v = page.settings.view as SettingsView;
  const had = v.signals.wallet_connected;
  const payload = had ? { replace: true } : {};
  const ask = usePreview(API.walletCreate, payload);
  const title = had ? "Replace your agent's wallet?" : "Make your agent's wallet?";
  if (ask.kind !== "review") return <Asking title={title} ask={ask} onClose={onClose} />;
  return (
    <ReviewDialog<{ note?: string; warning?: string | null; old_wallet_file?: string | null }>
      path={API.walletCreate}
      payload={payload}
      first={ask.preview}
      title={title}
      lead={
        had
          ? "A new, empty wallet takes the place of the one your agent has. Do this only if that wallet no longer works; to keep using it, add sats to it instead."
          : "A Lightning wallet of the agent's own, to pay for signal days by itself. It starts empty."
      }
      cancel={had ? "Keep my wallet" : "Not now"}
      confirm={had ? "Replace it with a new wallet" : "Make the wallet"}
      onClose={onClose}
      onDone={async (r) => {
        const note = (r as { note?: string }).note;
        await saved(page, (nv) => `${note || "Wallet made."} Wallet: ${nv.signals.wallet.site ?? "connected"}.`);
        onClose();
      }}
    >
      {(p) => <TestServer text={(p as Preview & { warning?: string | null }).warning} />}
    </ReviewDialog>
  );
}

/** Use your own LNbits wallet: address + admin key, then the server's preview. */
export function ConnectWalletDialog({ page, onClose }: { page: Page; onClose: () => void }) {
  const v = page.settings.view as SettingsView;
  const [url, setUrl] = useState("");
  const [key, setKey] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const [review, setReview] = useState<{ payload: Record<string, unknown>; preview: Preview } | null>(null);
  const id = useId();
  const ask = async () => {
    const a = readAddress(url);
    if (!a.ok) return setErr(a.error);
    if (!key.trim()) return setErr("Paste the wallet's admin key.");
    setErr("");
    setBusy(true);
    const payload = { url: a.value, admin_key: key.trim() };
    const r = await askPreview(API.walletConnect, payload);
    setBusy(false);
    if (r.kind === "review") setReview({ payload, preview: r.preview });
    else setErr(r.kind === "error" ? r.error : r.note);
  };
  if (review)
    return (
      <ReviewDialog<{ note?: string }>
        path={API.walletConnect}
        payload={review.payload}
        first={review.preview}
        title={v.signals.wallet_connected ? "Use your own wallet instead?" : "Use your own wallet?"}
        lead="The agent pays for signal days from this wallet. It checks the wallet answers before it saves anything."
        cancel={v.signals.wallet_connected ? "Keep the current wallet" : "Not now"}
        confirm="Use this wallet"
        onClose={() => setReview(null)}
        onDone={async (r) => {
          setKey("");
          const note = (r as { note?: string }).note;
          await saved(page, (nv) => `Connected${note ? ` (the agent said: ${note})` : ""}. Wallet: ${nv.signals.wallet.site ?? "connected"}.`);
          onClose();
        }}
      />
    );
  return (
    <Dialog
      title="Use your own LNbits wallet"
      lead="The wallet's address and its admin key (in LNbits: the wallet's API info). The key lets the agent spend from it, only on day-passes and only up to its cap."
      onClose={() => !busy && onClose()}
      foot={
        <>
          <button className="btn" onClick={onClose} disabled={busy} data-autofocus>
            Cancel
          </button>
          <button className="btn btn-primary" id="dlg-ok" onClick={ask} disabled={busy}>
            {busy ? "Asking the agent…" : "Review"}
          </button>
        </>
      }
    >
      <div className="field">
        <label htmlFor={`${id}-u`}>Wallet address</label>
        <input id={`${id}-u`} className="input" autoComplete="off" spellCheck={false} placeholder="https://" value={url} onChange={(e) => (setUrl(e.target.value), setErr(""))} />
      </div>
      <div className="field">
        <label htmlFor={`${id}-k`}>Admin key</label>
        <input id={`${id}-k`} className="input" type="password" autoComplete="off" spellCheck={false} value={key} onChange={(e) => (setKey(e.target.value), setErr(""))} />
      </div>
      <FieldError id={`${id}-e`} text={err} />
    </Dialog>
  );
}

type FundStep = { kind: "amount" } | { kind: "review"; sats: number; preview: Preview } | { kind: "pay"; inv: FundInvoice };

/** Add sats to the agent's wallet: amount (with its worth now) → the
 *  server's preview → an invoice the owner pays from their own wallet app. */
export function FundDialog({ page, onClose }: { page: Page; onClose: () => void }) {
  const v = page.settings.view as SettingsView;
  const cap = v.signals.wallet.invoice_cap_sats;
  const [text, setText] = useState("");
  const [quote, setQuote] = useState<WalletQuote | null>(null);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const [bal, setBal] = useState("");
  const [step, setStep] = useState<FundStep>({ kind: "amount" });
  const id = useId();
  const worth = async (sats: number) => {
    const r = await getJson<WalletQuote>(`${API.walletQuote}?sats=${sats}`);
    setQuote(r.ok ? r.body : null);
    return r.ok && r.body.ok ? r.body : null;
  };
  useEffect(() => {
    // a day-pass's worth, suggested (the classic form's default)
    worth(0).then((q) => q && setText(String(q.suggested_sats)));
  }, []); // eslint-disable-line react-hooks/exhaustive-deps
  const review = async () => {
    const n = readSats(text, cap);
    if (!n.ok) return setErr(n.error);
    setErr("");
    setBusy(true);
    worth(n.value);
    const a = await askPreview(API.walletFund, { sats: n.value });
    setBusy(false);
    if (a.kind === "review") setStep({ kind: "review", sats: n.value, preview: a.preview });
    else setErr(a.kind === "error" ? a.error : a.note);
  };
  const balance = async () => {
    setBal("Reading…");
    const r = await getJson<WalletBalance>(API.walletBalance);
    setBal(!r.ok ? `Not read: ${r.error}` : r.body.ok ? `In the wallet now: ${count(r.body.sats)} sats.` : `Not read: ${r.body.error}`);
  };

  if (step.kind === "review")
    return (
      <ReviewDialog<FundInvoice>
        path={API.walletFund}
        payload={{ sats: step.sats }}
        first={step.preview}
        title={`Add ${count(step.sats)} sats to your agent's wallet?`}
        lead="Next you get a payment code to pay from your own Lightning wallet app. Nothing is paid until you approve it there."
        cancel="Not now"
        confirm="Show the payment code"
        onClose={() => setStep({ kind: "amount" })}
        onDone={(r) => {
          const inv = r as unknown as FundInvoice;
          if (typeof inv.bolt11 !== "string") {
            setStep({ kind: "amount" });
            return setErr("The agent did not hand back a payment code. Nothing was paid. Try again.");
          }
          setStep({ kind: "pay", inv });
        }}
      >
        {() =>
          quote && quote.ok ? (
            <p className="small">
              About {usd(quote.usd)} at {usd(quote.btc_usd, 0)} a bitcoin: {plural(quote.day_passes, "day-pass", "day-passes")} at about $10 each.
            </p>
          ) : null
        }
      </ReviewDialog>
    );

  if (step.kind === "pay")
    return (
      <Dialog
        title={`Pay ${count(step.inv.sats)} sats into your agent's wallet`}
        lead="Scan this with a Bitcoin Lightning wallet app, or copy the code into it, and approve the payment there."
        onClose={onClose}
        foot={
          <>
            <button className="btn" onClick={onClose} data-autofocus>
              Close
            </button>
            <button className="btn" onClick={balance}>
              Check the balance
            </button>
          </>
        }
      >
        <PayCode bolt11={step.inv.bolt11} qr={step.inv.qr}>
          <dl className="facts">
            <dt>Amount</dt>
            <dd>{count(step.inv.sats)} sats</dd>
          </dl>
        </PayCode>
        {step.inv.note && <p className="small">{step.inv.note}</p>}
        {bal && (
          <p className="small" role="status">
            {bal}
          </p>
        )}
      </Dialog>
    );

  return (
    <Dialog
      title="Add sats to your agent's wallet"
      lead="The agent pays for its day-passes from this wallet (about $10 a market day, in sats)."
      onClose={() => !busy && onClose()}
      foot={
        <>
          <button className="btn" onClick={onClose} disabled={busy} data-autofocus>
            Cancel
          </button>
          <button className="btn btn-primary" id="dlg-ok" onClick={review} disabled={busy}>
            {busy ? "Asking the agent…" : "Review"}
          </button>
        </>
      }
    >
      <div className="field">
        <label htmlFor={`${id}-s`}>Sats</label>
        <input
          id={`${id}-s`}
          className="input"
          inputMode="numeric"
          autoComplete="off"
          value={text}
          onChange={(e) => (setText(e.target.value), setErr(""))}
          onBlur={() => {
            const n = readSats(text, cap);
            if (n.ok) worth(n.value);
          }}
        />
        <span className="help">
          {quote && quote.ok
            ? `${quote.usd > 0 ? `About ${usd(quote.usd)} now. ` : ""}One day-pass is about ${count(quote.day_pass_sats)} sats.`
            : "From 1,000 sats."}
          {cap ? ` This wallet takes at most ${count(cap)} sats in one payment.` : ""}
        </span>
      </div>
      <TestServer text={v.signals.wallet.warning} />
      <FieldError id={`${id}-e`} text={err} />
    </Dialog>
  );
}

/** Buy a day-pass by itself, on or off (the server's preview first). */
export function RecurringDialog({ page, onClose }: { page: Page; onClose: () => void }) {
  const v = page.settings.view as SettingsView;
  const next = !v.signals.recurring;
  const payload = { on: next };
  const ask = usePreview(API.recurring, payload);
  const title = next ? "Let the agent buy its day-pass by itself?" : "Stop buying day-passes automatically?";
  if (ask.kind !== "review") return <Asking title={title} ask={ask} onClose={onClose} />;
  return (
    <ReviewDialog
      path={API.recurring}
      payload={payload}
      first={ask.preview}
      title={title}
      lead={
        next
          ? "On each market day it reads signals, the agent pays for that day's pass from its wallet (about $10 in sats)."
          : "The agent never spends from its wallet by itself. It reads signals only with days you buy, or a key."
      }
      cancel="Keep it as it is"
      confirm={next ? "Turn it on" : "Turn it off"}
      onClose={onClose}
      onDone={async () => {
        await saved(page, (nv) => `Saved. Buys a day-pass by itself: ${nv.signals.recurring ? "on" : "off"}.`);
        onClose();
      }}
    />
  );
}
