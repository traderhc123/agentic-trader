// Signals & pass: what signal access the agent has, when it last listened,
// and the home of "buy signal days". Buying goes: pick a size (the sizes
// AgentHC sells right now) → the server's preview → a Lightning invoice the
// owner pays from their own wallet app. Nothing is paid before that, and the
// page says "paid" only when the agent's own pass check says so. The wallet
// is shown here, read-only.
import { useEffect, useRef, useState } from "react";
import type { Invoice, PassCheck, PassOptions, Preview, WalletBalance, WalletPage } from "../../lib/settingsApi";
import type { Status } from "../../lib/viewModel";
import { FieldError, ReviewDialog, SettingsDoubt, Switch, askPreview } from "../Forms";
import { API, getJson, send } from "../lib/http";
import { purchaseFate, scopeWords, sourceWords } from "../lib/settings";
import { UNKNOWN, count, etDayKey, etWhen, plural, tone, usd } from "../lib/format";
import { Dialog, Icon, Stamp } from "../ui";
import { PayCode } from "./Pay";
import { Row } from "./Pages";
import { ConnectWalletDialog, CreateWalletDialog, FundDialog, KeyDialog, RecurringDialog, ScopeDialog, SourceDialog } from "./SignalsDialogs";
import type { Page } from "../App";


const PASS_KIND: Record<Status["signals"]["pass"]["kind"], string> = {
  prepaid: "Prepaid signal days",
  day_pass: "Day pass, paid from your payment wallet",
  subscription: "Subscription key",
  not_needed: "This signal source needs no pass",
  none: "No pass",
};
const PASS_STATUS: Record<Status["signals"]["pass"]["status"], string> = {
  active: "Active",
  exits_only: "Exit signals only",
  waiting: "Waiting",
  none: "None",
  unknown: "Not checked",
};
const plain = (t: string, strong = true) => (
  <span className="tag tag-plain" style={strong ? { color: "var(--ink)" } : undefined}>
    {t}
  </span>
);

/** Every size the wizard offers; the server says which are on sale now. */
const SIZES = [1, 5, 20, 60];

type Step =
  | { kind: "pick" }
  | { kind: "review"; days: number; preview: Preview }
  | { kind: "pay"; days: number; inv: Invoice }
  | { kind: "paid"; days: number; left: number | null };

/** Buy signal days: one dialog at a time, each the one a step needs. */
function BuyDays({ page, onClose }: { page: Page; onClose: () => void }) {
  const [step, setStep] = useState<Step>({ kind: "pick" });
  const [sizes, setSizes] = useState<number[] | null | undefined>(undefined); // undefined: asking
  const [days, setDays] = useState<number | null>(null);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const [payWords, setPayWords] = useState("Waiting for your payment…");
  const timer = useRef<ReturnType<typeof setInterval>>();

  const loadSizes = async () => {
    const r = await getJson<PassOptions>(API.passOptions);
    const o = r.ok && Array.isArray(r.body.options) ? r.body.options.filter((x) => Number.isInteger(x) && x > 0) : null;
    setSizes(o);
    return o;
  };
  useEffect(() => {
    loadSizes();
    return () => clearInterval(timer.current);
  }, []);
  const shown = sizes == null ? SIZES : SIZES.filter((x) => sizes.includes(x)).concat(sizes.filter((x) => !SIZES.includes(x)));

  const review = async () => {
    if (days == null) return setErr("Pick how many signal days to buy.");
    setBusy(true);
    setErr("");
    const a = await askPreview(API.passBuy, { days });
    setBusy(false);
    if (a.kind === "review") setStep({ kind: "review", days, preview: a.preview });
    else setErr(a.kind === "error" ? a.error : a.note);
  };

  // every 3 s while the invoice is open: the agent's own pass check (it
  // files a paid invoice's pass), then where THIS purchase stands
  const polling = useRef(false);
  const watch = (d: number, id: string) => {
    clearInterval(timer.current);
    timer.current = setInterval(async () => {
      if (polling.current) return; // the last round is still out
      polling.current = true;
      try {
        const r = await getJson<PassCheck>(API.passStatus);
        const nv = await page.settings.reload();
        const fate = purchaseFate(id, nv?.signals.purchases);
        if (fate === "paid") {
          clearInterval(timer.current);
          const left = r.ok && r.body.status === "active" && typeof r.body.days_left === "number" ? r.body.days_left : null;
          setStep({ kind: "paid", days: d, left });
          page.refresh();
        } else if (fate === "gone") {
          clearInterval(timer.current);
          setPayWords("That payment code expired unpaid. Nothing was paid. Close this and buy again to get a new one.");
        } else if (!r.ok) setPayWords(`Waiting for your payment… (${r.error})`);
        else if (r.body.status === "error" && r.body.error) setPayWords(`Waiting for your payment… The last check said: ${r.body.error}`);
        else setPayWords("Waiting for your payment…");
      } finally {
        polling.current = false;
      }
    }, 3000);
  };

  if (step.kind === "review")
    return (
      <ReviewDialog<Invoice>
        path={API.passBuy}
        payload={{ days: step.days }}
        first={step.preview}
        title={`Buy ${plural(step.days, "signal day")}?`}
        lead="Next you get a payment code to pay from your own Lightning wallet app. The price is on it; nothing is paid until you approve it there."
        cancel="Not now"
        confirm="Show the payment code"
        onClose={onClose}
        onDone={(r) => {
          const inv = r as unknown as Invoice;
          if (!inv.bolt11) {
            setStep({ kind: "pick" });
            setErr("The agent did not hand back a payment code. Nothing was paid. Try again.");
            return;
          }
          setStep({ kind: "pay", days: step.days, inv });
          if (typeof inv.purchase === "string") watch(step.days, inv.purchase);
          else setPayWords("This page can't follow this payment. After you pay, the pass shows on this page within a minute.");
        }}
      >
        {() => (
          <dl className="facts">
            <dt>You have now</dt>
            <dd>
              {page.s.signals.pass.days_left != null ? plural(page.s.signals.pass.days_left, "signal day") : UNKNOWN}
            </dd>
            <dt>Price</dt>
            <dd>
              Shown on the payment code <small>AgentHC sets it when the code is made.</small>
            </dd>
          </dl>
        )}
      </ReviewDialog>
    );

  if (step.kind === "pay") {
    const i = step.inv;
    return (
      <Dialog
        title={`Pay for ${plural(step.days, "signal day")}`}
        lead="Scan this with a Bitcoin Lightning wallet app, or copy the code into it, and approve the payment there."
        onClose={onClose}
        foot={
          <button className="btn" onClick={onClose} data-autofocus>
            Close
          </button>
        }
      >
        <PayCode bolt11={i.bolt11} qr={i.qr}>
          <dl className="facts">
            <dt>Price</dt>
            <dd>
              {i.amount_sats != null ? `${count(i.amount_sats)} sats` : UNKNOWN}
              {i.amount_usd != null && <small>about {usd(i.amount_usd)}</small>}
            </dd>
            {i.carried > 0 && (
              <>
                <dt>Carried over</dt>
                <dd>{plural(i.carried, "unused day")}</dd>
              </>
            )}
          </dl>
        </PayCode>
        <p className="wait" role="status" aria-live="polite">
          <span className="spin" aria-hidden="true" />
          {payWords}
        </p>
        <p className="small muted">Closing this does not cancel the code: if you pay it later, the agent still finds the pass.</p>
      </Dialog>
    );
  }

  if (step.kind === "paid")
    return (
      <Dialog
        title="Paid"
        lead={`Your agent checked the pass with AgentHC: ${step.left != null ? `${plural(step.left, "signal day")} left` : "it is active"}.`}
        onClose={onClose}
        foot={
          <button className="btn btn-primary" onClick={onClose} data-autofocus>
            Done
          </button>
        }
      />
    );

  return (
    <Dialog
      title="Buy signal days"
      lead="Each signal day lets your agent read AgentHC's signals for one market day. Unused days carry over."
      onClose={() => !busy && onClose()}
      foot={
        <>
          <button className="btn" onClick={onClose} disabled={busy} data-autofocus>
            Not now
          </button>
          <button className="btn btn-primary" id="dlg-ok" onClick={review} disabled={busy || days == null || (sizes != null && sizes.length === 0)}>
            {busy ? "Asking the agent…" : "Review"}
          </button>
        </>
      }
    >
      {sizes === undefined ? (
        <p className="small muted" role="status">
          Asking AgentHC which sizes are on sale…
        </p>
      ) : sizes != null && sizes.length === 0 ? (
        <p className="callout" role="status">
          No size is on sale right now (AgentHC's payment node is refilling). Try again in a few minutes.
        </p>
      ) : (
        <>
          <div className="pass-opts" role="radiogroup" aria-label="How many signal days">
            {shown.map((n) => (
              <label key={n} className="pass-opt">
                <input type="radio" name="days" value={n} checked={days === n} onChange={() => (setDays(n), setErr(""))} />
                <b>{n}</b>
                <span>signal {n === 1 ? "day, to try it" : "days"}</span>
              </label>
            ))}
          </div>
          {sizes == null && <p className="small muted">This page could not ask AgentHC which sizes are on sale, so all are shown.</p>}
        </>
      )}
      <FieldError id="buy-e" text={err} />
      <p className="small muted">This does not change Practice or LIVE, and does not buy or sell anything.</p>
    </Dialog>
  );
}

type Open = "source" | "scope" | "key" | "wallet-create" | "wallet-connect" | "wallet-fund" | "recurring" | null;

/** Where the signals come from: the source, which AgentHC signals, and the
 *  subscription key. One control each. */
function SourcePanel({ page, open }: { page: Page; open: (o: Open) => void }) {
  const v = page.settings.view;
  const why = page.link === "lost" ? "This page can't reach the agent, so nothing can be changed right now." : !v ? page.settings.error || "Reading your settings…" : "";
  const hc = v?.signals.source === "agenthc";
  return (
    <section className="panel">
      <div className="ph">
        <h2>Signal source</h2>
      </div>
      <Row
        label="Source"
        exp={!v ? UNKNOWN : sourceWords({ source: v.signals.source, site: v.signals.source_site })}
        act={
          <button className="btn btn-sm" data-act="source" aria-haspopup="dialog" disabled={!!why} title={why || undefined} onClick={() => open("source")}>
            Change…
          </button>
        }
      />
      {hc && v && (
        <Row
          label="Which signals"
          exp={`${scopeWords(v.signals.include_other_trades ? "all" : "main")}.${v.signals.include_other_trades ? " Other trades are sized as Rules & limits says." : ""}`}
          act={
            <button className="btn btn-sm" data-act="scope" aria-haspopup="dialog" disabled={!!why} onClick={() => open("scope")}>
              {v.signals.include_other_trades ? "Main pick only…" : "Take other trades too…"}
            </button>
          }
        />
      )}
      {hc && v && (
        <Row
          label="Subscription key"
          exp={
            v.signals.api_key.set
              ? `Set${v.signals.api_key.last4 ? `, ending ${v.signals.api_key.last4}` : ""}. With a key, the agent needs no signal days.`
              : "Not set. An AgentHC Premium key reads the signals instead of signal days."
          }
          act={
            <button className="btn btn-sm" data-act="api-key" aria-haspopup="dialog" disabled={!!why} onClick={() => open("key")}>
              {v.signals.api_key.set ? "Replace…" : "Add a key…"}
            </button>
          }
        />
      )}
    </section>
  );
}

/** Practice mode: what the owner's rules would have taken from the feed's
 *  closed trades of the past days, and its on/off switch (one home). */
function PreviewPanel({ page }: { page: Page }) {
  const v = page.settings.view;
  const w = page.s.signals.preview;
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const on = v ? v.signals.dry_run_preview : null;
  const save = async (next: boolean) => {
    setBusy(true);
    setErr("");
    const r = await send(API.signals, { dry_run_preview: next });
    if (!r.ok) {
      setBusy(false);
      return setErr(r.error);
    }
    const nv = await page.settings.reload();
    setBusy(false);
    page.refresh();
    page.say(true, `Saved. Practice preview: ${!nv ? "not read back yet" : nv.signals.dry_run_preview ? "on" : "off"}.`);
  };
  return (
    <section className="panel">
      <div className="ph">
        <h2>What it would have taken</h2>
        <Switch act="preview" label="Practice preview" on={on} busy={busy} disabled={page.link === "lost"} onChange={save} />
      </div>
      <div className="pb">
        <FieldError id="preview-e" text={err} />
        {on === false ? (
          <p className="small muted">Off. On: in practice mode, this lists the feed's closed trades of the past days and whether your rules would have taken each one.</p>
        ) : !w ? (
          <p className="small muted">Nothing to show yet: the agent fills this in from the feed's closed trades on its next read.</p>
        ) : w.unavailable || w.error ? (
          <p className="small muted">{w.error ? `The last refresh failed: ${w.error}` : "The feed does not offer closed trades to preview right now."}</p>
        ) : (
          <>
            <p>
              <b>{plural(w.would_take, "trade")}</b> in the past {plural(w.days, "day")} would have been taken: {count(w.wins)} won, {count(w.breakeven)} broke even,{" "}
              {count(w.losses)} lost
              {w.avg_paper_pnl_pct != null && (
                <>
                  , <span className={tone(w.avg_paper_pnl_pct)}>{w.avg_paper_pnl_pct > 0 ? "+" : w.avg_paper_pnl_pct < 0 ? "−" : ""}{Math.abs(w.avg_paper_pnl_pct).toFixed(1)}%</span> on average
                </>
              )}
              .
            </p>
            <p className="small muted">Paper results of the feed's own trades, not your orders: no money was involved.{w.updated_at ? ` Updated ${etWhen(w.updated_at, etDayKey(page.s.as_of))}.` : ""}</p>
            {w.rows.length > 0 && (
              <div className="tbl-wrap">
                <table className="tbl">
                  <thead>
                    <tr>
                      <th>Day</th>
                      <th>Contract</th>
                      <th>Your rules</th>
                      <th>Paper result</th>
                    </tr>
                  </thead>
                  <tbody>
                    {w.rows.map((r, i) => (
                      <tr key={`${r.date}-${r.contract}-${i}`}>
                        <td>{r.date}</td>
                        <td>{r.contract}</td>
                        <td>{r.would_take ? "Would take it" : `Would skip it${r.skip_reason ? `: ${r.skip_reason}` : ""}`}</td>
                        <td className={tone(r.paper_pnl_pct)}>
                          {r.paper_pnl_pct == null ? UNKNOWN : `${r.paper_pnl_pct > 0 ? "+" : r.paper_pnl_pct < 0 ? "−" : ""}${Math.abs(r.paper_pnl_pct).toFixed(1)}%`}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </>
        )}
      </div>
    </section>
  );
}

/** The agent's payment wallet: what it is and holds, and every control for
 *  it (make / replace, add sats, use your own, the page link, recurring). */
function WalletPanel({ page, open }: { page: Page; open: (o: Open) => void }) {
  const v = page.settings.view;
  const on = v?.signals.wallet_connected;
  const w = v?.signals.wallet;
  const [bal, setBal] = useState<string>("");
  const [link, setLink] = useState<{ err?: string; url?: string } | null>(null);
  const lost = page.link === "lost" || !v;
  useEffect(() => {
    if (!on) return;
    let live = true;
    getJson<WalletBalance>(API.walletBalance).then((r) => {
      if (!live) return;
      if (!r.ok) setBal(`not read: ${r.error}`);
      else if (r.body.ok) setBal(`${count(r.body.sats)} sats`);
      else setBal(`not read: ${r.body.error}`);
    });
    return () => {
      live = false;
    };
  }, [on, w?.site]);
  // the page link opens the wallet: fetched by POST only on this click,
  // into a window opened by the click itself
  const openPage = async () => {
    setLink(null);
    const win = window.open("", "_blank");
    const r = await send<WalletPage>(API.walletPage, {});
    const url = r.ok ? (r as unknown as WalletPage).url : "";
    if (!r.ok || typeof url !== "string" || !/^https?:\/\//.test(url)) {
      win?.close();
      return setLink({ err: r.ok ? "The agent did not hand back a wallet page." : r.error });
    }
    if (win) {
      win.opener = null;
      win.location.href = url;
    } else setLink({ url });
  };
  return (
    <section className="panel">
      <div className="ph">
        <h2>Payment wallet</h2>
        <span className="meta">The agent's own Lightning wallet, for paying day-passes by itself</span>
      </div>
      <Row
        label="Wallet"
        exp={!v ? UNKNOWN : on ? `At ${w?.site ?? "an LNbits server"}.${on ? ` In it: ${bal || "reading…"}.` : ""}` : "None. Signal days bought on this page are paid from any wallet app you choose; this wallet is not needed for that."}
        act={
          <span className="row-acts">
            {on && (
              <button className="btn btn-sm btn-primary" data-act="wallet-fund" aria-haspopup="dialog" disabled={lost} onClick={() => open("wallet-fund")}>
                Add sats…
              </button>
            )}
            <button className="btn btn-sm" data-act="wallet-create" aria-haspopup="dialog" disabled={lost} onClick={() => open("wallet-create")}>
              {on ? "Replace with a new wallet…" : "Make the agent's wallet…"}
            </button>
          </span>
        }
      />
      {on && w?.warning && (
        <div className="pb">
          <div className="callout callout-warn" role="note">
            <p>
              <Icon name="alert" small />
              {w.warning}
            </p>
          </div>
        </div>
      )}
      {on && w?.page_on_file && (
        <Row
          label="Wallet page"
          exp={
            link?.err ? (
              <span className="err">{link.err}</span>
            ) : link?.url ? (
              <>
                Your browser blocked the new window:{" "}
                <a href={link.url} target="_blank" rel="noopener noreferrer">
                  open the wallet page
                </a>
                .
              </>
            ) : (
              "Opens the wallet in LNbits. Whoever has this link can spend from the wallet, so this page never shows it."
            )
          }
          act={
            <button className="btn btn-sm" data-act="wallet-page" disabled={lost} onClick={openPage}>
              <Icon name="ext" small />
              Open the wallet page
            </button>
          }
        />
      )}
      <Row
        label="Your own LNbits wallet"
        exp="Point the agent at a wallet you run instead. The current wallet's details are saved to a file first."
        act={
          <button className="btn btn-sm" data-act="wallet-connect" aria-haspopup="dialog" disabled={lost} onClick={() => open("wallet-connect")}>
            Use my own wallet…
          </button>
        }
      />
      <Row
        label="Buys a day-pass by itself"
        exp={
          !v
            ? UNKNOWN
            : v.signals.recurring
              ? `On: each market day it reads signals, the agent pays that day's pass from this wallet${w?.autopay_cap_sats ? `, at most ${count(w.autopay_cap_sats)} sats each` : ""}.`
              : "Off: the agent never spends from its wallet by itself."
        }
        val={v ? <span className="tag tag-plain">{v.signals.recurring ? "On" : "Off"}</span> : undefined}
        act={
          <button className="btn btn-sm" data-act="recurring" aria-haspopup="dialog" disabled={lost || (!on && !v?.signals.recurring)} onClick={() => open("recurring")}>
            {v?.signals.recurring ? "Turn off…" : "Turn on…"}
          </button>
        }
      />
    </section>
  );
}

export function SignalsView({ page }: { page: Page }) {
  const s = page.s;
  const sig = s.signals;
  const p = sig.pass;
  const c = s.results.signal_cost;
  const [buying, setBuying] = useState(false);
  const [open, setOpen] = useState<Open>(null);
  const reload = page.settings.reload;
  useEffect(() => {
    reload();
  }, [reload]);
  const statusTag = p.status === "active" ? plain(PASS_STATUS[p.status]) : <span className="tag tag-warn">{PASS_STATUS[p.status] ?? "Unknown"}</span>;
  // signal days are AgentHC's: offered for its feed, and while no other kind of access is set up
  const sells = (sig.source === "agenthc" || sig.source == null) && (p.kind === "prepaid" || p.kind === "none" || p.kind === "day_pass");
  return (
    <div className="view">
      <div className="view-h">
        <div>
          <h1>Signals & pass</h1>
          <p>
            Your agent acts on trade signals from its signal source. This page shows what access it has and when it last heard from the source, and is
            where you choose the source, buy signal days and look after the agent's payment wallet.
          </p>
        </div>
        {page.stale && <Stamp at={page.asOf} />}
      </div>
      <SettingsDoubt settings={page.settings} />
      <div className="two">
        <section className={`panel ${page.stale ? "stale" : ""}`}>
          <div className="ph">
            <h2>Signal access</h2>
            {statusTag}
          </div>
          <div className="pb stale-c">
            {p.kind === "prepaid" ? (
              <div className="bignum">
                {p.days_left != null ? count(p.days_left) : "?"} <small>{p.days_left != null ? "signal days left" : "signal days left: not known yet"}</small>
              </div>
            ) : (
              <div className="bignum" style={{ fontSize: 24 }}>
                {PASS_KIND[p.kind] ?? "Unknown pass"}
              </div>
            )}
            <ul className="tally" style={{ marginTop: 14 }}>
              <li>
                <span>Kind</span>
                <b>{PASS_KIND[p.kind] ?? UNKNOWN}</b>
              </li>
              {c.applies && (
                <li>
                  <span>Signal days used so far</span>
                  <b>
                    {plural(c.days_used, "day")}
                    {c.days_estimated > 0 ? `, plus about ${c.days_estimated} earlier` : ""}
                  </b>
                </li>
              )}
              {c.applies && (
                <li>
                  <span>Paid for signals so far</span>
                  <b>{c.usd_paid != null ? usd(c.usd_paid) : "not tracked yet"}</b>
                </li>
              )}
            </ul>
            <p className="small muted" style={{ marginTop: 10 }}>
              {p.status === "exits_only"
                ? "No days are left for new trades. The agent still receives exit signals for anything it holds, so open positions are still sold."
                : p.status === "waiting"
                  ? "The agent is waiting for a pass to become usable (a payment that has not arrived, or a day pass it will buy on the next market day)."
                  : p.status === "none"
                    ? "Without a pass the agent receives no signals."
                    : p.status === "unknown"
                      ? "The agent could not check this pass with AgentHC yet, so it reads no signals until it can. It checks again on each read."
                      : "If access runs out, the agent stops taking new trades."}
            </p>
            {sells && (
              <div style={{ marginTop: 12 }}>
                <button className="btn btn-primary" data-act="buy-days" aria-haspopup="dialog" disabled={page.link === "lost"} onClick={() => setBuying(true)}>
                  Buy signal days…
                </button>
              </div>
            )}
          </div>
        </section>
        <section className={`panel ${page.stale ? "stale" : ""}`}>
          <div className="ph">
            <h2>Listening</h2>
            {plain(sig.reading ? "Reading the feed" : "Not reading the feed", sig.reading)}
          </div>
          <div className="pb stale-c">
            <ul className="tally">
              <li>
                <span>Last check of the feed</span>
                <b>{sig.last_poll ? `${etWhen(sig.last_poll, etDayKey(s.as_of))}${sig.last_poll_ok === false ? ", failed" : ""}` : UNKNOWN}</b>
              </li>
              <li>
                <span>Newest signal seen</span>
                <b>{sig.last_signal ? etWhen(sig.last_signal, etDayKey(s.as_of)) : "none yet"}</b>
              </li>
              <li>
                <span>New-trade signals readable</span>
                <b>{sig.entry_access == null ? UNKNOWN : sig.entry_access ? "Yes" : "No"}</b>
              </li>
            </ul>
            <p className="small muted" style={{ marginTop: 10 }}>
              {sig.last_signal_from === "activity_log" ? "The newest-signal time is when the agent last logged handling a signal. " : ""}
              {!sig.reading && page.link === "ok" ? "The agent is deliberately not reading the feed right now (a hold, or a pause with no real position open)." : ""}
            </p>
          </div>
        </section>
      </div>
      <SourcePanel page={page} open={setOpen} />
      {s.mode === "practice" && <PreviewPanel page={page} />}
      <WalletPanel page={page} open={setOpen} />
      {buying && <BuyDays page={page} onClose={() => setBuying(false)} />}
      {open === "source" && page.settings.view && <SourceDialog page={page} onClose={() => setOpen(null)} />}
      {open === "scope" && page.settings.view && <ScopeDialog page={page} onClose={() => setOpen(null)} />}
      {open === "key" && page.settings.view && <KeyDialog page={page} onClose={() => setOpen(null)} />}
      {open === "wallet-create" && page.settings.view && <CreateWalletDialog page={page} onClose={() => setOpen(null)} />}
      {open === "wallet-connect" && page.settings.view && <ConnectWalletDialog page={page} onClose={() => setOpen(null)} />}
      {open === "wallet-fund" && page.settings.view && <FundDialog page={page} onClose={() => setOpen(null)} />}
      {open === "recurring" && page.settings.view && <RecurringDialog page={page} onClose={() => setOpen(null)} />}
    </div>
  );
}

