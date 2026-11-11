// The confirm dialogs. Each one sends exactly one typed command
// (POST /api/v2/command, which runs the chat commands' own code with fixed
// words; never text from this page):
//   Pause new trades   -> {action: "pause"}
//   Resume             -> {action: "resume"}
//   Switch to practice -> {action: "set_mode", mode: "practice"}
//   Go LIVE            -> {action: "set_mode", mode: "live"}: two steps; the
//                         dialog shows the SERVER's preview (account, cash,
//                         size, limits, open real positions) and needs the
//                         tick box, checked again by the server
//   Stop the agent     -> the fixed command "stop" (POST /api/command; the
//                         typed route has no stop)
// Nothing is assumed to have worked: the agent's own reply is shown, and the
// band changes only when the next status says so.
// UpdateDialog is the one that does not send a command: it installs the
// version the update check reported (POST /api/update/pull, see lib/update.ts).
import { useEffect, useState } from "react";
import type { CommandDone, Preview } from "../lib/settingsApi";
import type { Position, Status } from "../lib/viewModel";
import { ReviewDialog, askPreview } from "./Forms";
import { API } from "./lib/http";
import { money, sizeWords, lossWords } from "./lib/settings";
import { UPDATE, sellsHow } from "./lib/copy";
import { brokerLabel, paidFor, practicePositions, realPositions } from "./lib/derive";
import { UNKNOWN, contractName, count, etTime, plural, usd } from "./lib/format";
import type { Offer, Phase } from "./lib/update";
import { Dialog, ExpiresFlag, Icon, WillWont } from "./ui";

export type DialogKind = "pause" | "resume" | "to-practice" | "to-live" | "stop";

/** What a dialog asks the agent to do. */
export type Cmd = { action: "pause" } | { action: "resume" } | { action: "set_mode"; mode: "practice" } | { action: "stop" };

const their = (n: number, one: string, many: string) => (n === 1 ? one : many);

function PosList({ list }: { list: Position[] }) {
  return (
    <ul className="poslist">
      {list.map((p) => (
        <li key={p.id}>
          <span>
            {contractName(p.contract, p.label)} {p.expires_today && <ExpiresFlag />}
          </span>
          <span>
            {plural(p.qty, "contract")}, {p.entry_cost_usd != null ? `${usd(p.entry_cost_usd, 0)} paid` : "amount paid not known"}
          </span>
        </li>
      ))}
    </ul>
  );
}

/** The facts going LIVE is reviewed with, as the server's preview has them
 *  (an unknown is said as unknown). */
function LiveFacts({ p, s }: { p: Preview; s: Status }) {
  const acct = p.account;
  const name = acct?.name || brokerLabel(s);
  const lim = p.limits;
  return (
    <dl className="facts">
      <dt>Broker account</dt>
      <dd>
        {name}
        {acct?.account_mask ? ` ····${acct.account_mask}` : ""}
        {acct?.paper_account && <small>The broker's paper-trading account.</small>}
        {s.broker.connected === false && <small>Not connected right now, so real orders can't be sent until it is.</small>}
      </dd>
      <dt>Cash available</dt>
      <dd>
        {p.buying_power != null ? usd(p.buying_power) : UNKNOWN}
        {p.buying_power_at && <small>Read at {etTime(p.buying_power_at)}.</small>}
        {p.cash_low && <small>Too little for one trade at your size, so buy orders would be rejected.</small>}
      </dd>
      <dt>Each trade</dt>
      <dd>
        {sizeWords(p.trade_size?.main ?? null, "main")}
        <small>{p.trade_size?.other ? `${sizeWords(p.trade_size.other, "other")} for the provider's other trades.` : "The same for the provider's other trades."}</small>
      </dd>
      <dt>New trades per day</dt>
      <dd>{lim?.max_entries_per_day != null ? count(lim.max_entries_per_day) : UNKNOWN}</dd>
      <dt>Daily loss limit</dt>
      <dd>
        {lossWords(lim?.max_daily_loss_usd)}{" "}
        <small>{lossWords(lim?.max_daily_loss_usd) !== "Off" ? "Stops new trades for the day. It does not sell anything." : "Losses never stop new trades."}</small>
      </dd>
      <dt>Open positions at once</dt>
      <dd>{lim?.max_open_positions != null ? count(lim.max_open_positions) : UNKNOWN}</dd>
      <dt>Real positions open now</dt>
      <dd>{p.real_positions != null ? count(p.real_positions) : UNKNOWN}</dd>
      {p.trade_size?.main?.mode === "budget" && p.trade_size.main.value != null && lim?.max_entries_per_day != null && (
        <>
          <dt>Most in new trades in a day</dt>
          <dd>{money(lim.max_entries_per_day * Math.max(p.trade_size.main.value, p.trade_size.other?.mode === "budget" && p.trade_size.other.value != null ? p.trade_size.other.value : 0))}</dd>
        </>
      )}
    </dl>
  );
}

/** Switch to LIVE: the first POST asks the server for its preview; the
 *  dialog shows that, and only the same request with the preview's token
 *  and the ticked box goes LIVE. */
export function LiveDialog({ s, onClose, onDone }: { s: Status; onClose: () => void; onDone: (reply: string) => void }) {
  const [ask, setAsk] = useState<{ kind: "asking" } | Awaited<ReturnType<typeof askPreview>>>({ kind: "asking" });
  const payload = { action: "set_mode", mode: "live" };
  useEffect(() => {
    askPreview(API.command, payload).then(setAsk);
  }, []); // eslint-disable-line react-hooks/exhaustive-deps
  const pp = practicePositions(s);
  if (ask.kind === "review")
    return (
      <ReviewDialog<CommandDone>
        path={API.command}
        payload={payload}
        first={ask.preview}
        tone="live"
        title="Switch to LIVE: real money"
        lead="From the next signal that passes your limits, the agent places real orders in your broker account. Check these are the numbers you expect."
        cancel="Stay in practice"
        confirm="Go LIVE with real money"
        ack="I understand the agent will place real orders with my money, and I can lose it."
        onClose={onClose}
        onDone={(r) => onDone(typeof (r as unknown as CommandDone).reply === "string" ? (r as unknown as CommandDone).reply : "LIVE.")}
      >
        {(p) => (
          <>
            <LiveFacts p={p} s={s} />
            <WillWont
              will={[
                "Buy real option contracts when a signal passes your rules and limits.",
                s.expiry_guard_on
                  ? "Sell them when the exit signal arrives, or at its expiry sell time on the day they expire."
                  : "Sell them when the exit signal arrives. The expiry sell is off: sell expiring positions yourself before the close.",
                pp.length
                  ? `Leave your ${plural(pp.length, "practice position")} as practice. ${their(pp.length, "It closes", "They close")} as pretend ${their(pp.length, "trade", "trades")}.`
                  : "Keep every rule and limit exactly as saved.",
              ]}
              wont={[
                "Go past your trade size or your daily limits.",
                "Protect you from losses. It has no stop-loss, and an option can lose everything paid for it, quickly.",
                "Switch back by itself. It stays LIVE, including after a restart, until you switch to practice.",
              ]}
            />
          </>
        )}
      </ReviewDialog>
    );
  return (
    <Dialog
      tone="live"
      title="Switch to LIVE: real money"
      lead={ask.kind === "asking" ? "Asking the agent for your account, cash and limits…" : undefined}
      onClose={onClose}
      foot={
        <button className="btn" onClick={onClose} data-autofocus>
          {ask.kind === "asking" ? "Cancel" : "Close"}
        </button>
      }
    >
      {ask.kind === "asking" ? (
        <p className="wait" role="status">
          <span className="spin" aria-hidden="true" />
          Reading what going LIVE would use…
        </p>
      ) : ask.kind === "nothing" ? (
        <p role="status">{ask.note}</p>
      ) : (
        <div className="callout callout-bad" role="alert">
          <p className="err">
            <Icon name="alert" small />
            {ask.error}
          </p>
          <p className="small">Nothing was changed. You are still in practice.</p>
        </div>
      )}
    </Dialog>
  );
}

export function ActionDialog({
  kind,
  s,
  busy,
  onConfirm,
  onClose,
}: {
  kind: DialogKind;
  s: Status;
  busy: boolean;
  /** the command to send */
  onConfirm: (cmd: Cmd) => void;
  onClose: () => void;
}) {
  const [choice, setChoice] = useState<"entries" | "all">("entries");
  const rp = realPositions(s);
  const n = rp.length;
  const b = s.broker.name || "your broker";
  const expiring = rp.find((p) => p.expires_today);
  const managed = s.real_positions_managed;
  const paid = paidFor(rp);
  const cancel = (label: string) => (
    <button className="btn" onClick={onClose} data-autofocus>
      {label}
    </button>
  );

  if (kind === "to-practice")
    return (
      <Dialog
        tone="prac"
        title="Switch to practice"
        lead={
          n
            ? `New trades become pretend straight away. Your ${plural(n, "real position")} ${their(n, "stays", "stay")} open at ${b}.`
            : "New trades become pretend straight away. You have no real positions open."
        }
        onClose={onClose}
        foot={
          <>
            {cancel("Stay LIVE")}
            <button className="btn btn-primary" id="dlg-ok" disabled={busy} onClick={() => onConfirm({ action: "set_mode", mode: "practice" })}>
              Switch to practice
            </button>
          </>
        }
      >
        {n > 0 && (
          <div className="callout callout-real">
            <h4>
              {their(n, "This is", "These are")} still real{paid != null ? `: ${usd(paid, 0)} paid` : ""}
            </h4>
            <PosList list={rp} />
            <p className="small">
              After you switch, the agent's reply says whether it keeps selling {their(n, "it", "them")} on {their(n, "its exit signal", "their exit signals")} in
              practice mode. That depends on your own setting, and this page shows it as soon as the agent reports it.
            </p>
          </div>
        )}
        <WillWont
          will={["Stop placing new real buy orders, from now.", "Record each new signal that passes your rules as a pretend trade.", "Keep every rule and limit exactly as saved."]}
          wont={[
            n ? `Sell your real positions now. To close them now, sell them yourself at ${b}.` : "Change anything in your broker account.",
            "Cancel an order your broker has already accepted.",
            "Go back to LIVE by itself. It stays in practice until you switch and confirm.",
          ]}
        />
      </Dialog>
    );

  if (kind === "pause")
    return (
      <Dialog
        title="Pause the agent"
        lead="Choose what stops. Nothing is sold because you paused, and Practice or LIVE does not change."
        onClose={onClose}
        foot={
          <>
            {cancel("Keep running")}
            <button className="btn btn-primary" id="dlg-ok" disabled={busy || choice !== "entries"} onClick={() => onConfirm({ action: "pause" })}>
              Pause new trades
            </button>
          </>
        }
      >
        <label className="choice">
          <input type="radio" name="pz" value="entries" checked={choice === "entries"} onChange={() => setChoice("entries")} />
          <strong>Stop new trades only</strong>
          <span>
            {managed || !n
              ? `The agent keeps selling what you hold: ${sellsHow(s)}. Choose this to take a break without leaving positions unwatched.`
              : "No new trades. Whether the agent sells what you hold does not change with a pause: see What the agent will do for each position."}
          </span>
        </label>
        <label className="choice choice-off" aria-disabled="true">
          <input type="radio" name="pz" value="all" disabled />
          <strong>Stop everything</strong>
          <span>The agent would do nothing at all: no buying, no selling, no checking for signals.</span>
          <span className="conseq">
            <Icon name="info" small />
            <span>
              Not available yet. The agent has no pause that also stops selling, so this page will not pretend it does. To turn the agent off
              completely, use "Stop the agent" in Settings
              {n ? `; your ${plural(n, "real position")} would then be unwatched${expiring ? `, and ${contractName(expiring.contract, expiring.label)} expires today` : ""}` : ""}.
            </span>
          </span>
        </label>
        <p className="small muted">
          This pause is saved: it lasts, also after a restart, until you press Resume. The agent stays in {s.mode === "live" ? "LIVE" : "practice"} mode.
        </p>
      </Dialog>
    );

  if (kind === "resume")
    return (
      <Dialog
        title={s.pause.kind === "state_hold" ? "Resume from a damaged memory file?" : "Resume new trades?"}
        lead={
          s.pause.kind === "state_hold"
            ? "The agent put itself on hold because its memory file could not be read. Resuming trades from the memory it has now."
            : `The agent takes new ${s.mode === "live" ? "real-money" : "practice"} trades again from the next signal that passes your limits.`
        }
        onClose={onClose}
        foot={
          <>
            {cancel(s.pause.kind === "state_hold" ? "Stay on hold" : "Stay paused")}
            <button className="btn btn-primary" id="dlg-ok" disabled={busy} onClick={() => onConfirm({ action: "resume" })}>
              Resume
            </button>
          </>
        }
      >
        {s.pause.kind === "state_hold" ? (
          <div className="callout">
            <h4>Check the list of positions first</h4>
            <p className="small">
              After resuming, the agent only sells positions that are in its list (Holdings). A position that is missing from that list is not
              sold by the agent: close it at {b} yourself.
            </p>
          </div>
        ) : (
          <p className="small muted">This does not change Practice or LIVE, and does not buy or sell anything now.</p>
        )}
      </Dialog>
    );

  // stop
  return (
    <Dialog
      title="Stop the agent completely?"
      lead="Use Pause for a break from new trades. Stopping is for when you want it off."
      onClose={onClose}
      foot={
        <>
          {cancel("Keep it running")}
          <button className="btn btn-primary" id="dlg-ok" disabled={busy} onClick={() => onConfirm({ action: "stop" })}>
            Stop the agent
          </button>
        </>
      }
    >
      {n > 0 && (
        <div className="callout callout-real">
          <h4>Nobody will be selling {their(n, "this", "these")} while it is stopped</h4>
          <PosList list={rp} />
        </div>
      )}
      <WillWont
        will={["Finish the cycle it is in, then shut down.", "Leave every open position exactly as it is at your broker."]}
        wont={[
          "Sell anything on the way out.",
          "Sell real positions on exit signals or before expiry while it is stopped.",
          "Answer this dashboard once it has stopped: this page will then say it can't reach the agent.",
        ]}
      />
    </Dialog>
  );
}

/** "Update the agent?": what will be installed (exactly the version the check
 *  reported), what happens while it restarts, then Update now / Not now. On a
 *  failure the agent's message is shown and only Close is left. */
export function UpdateDialog({
  offer,
  s,
  phase,
  detail,
  since,
  onInstall,
  onClose,
}: {
  offer: Offer;
  s: Status;
  phase: Phase;
  detail: string;
  since: number;
  onInstall: () => void;
  onClose: () => void;
}) {
  const installing = phase === "installing";
  const [secs, setSecs] = useState(0);
  useEffect(() => {
    if (!installing) return;
    const tick = () => setSecs(Math.round((Date.now() - since) / 1000));
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, [installing, since]);

  if (phase === "failed")
    return (
      <Dialog
        title={UPDATE.failedTitle}
        lead={UPDATE.againHint}
        onClose={onClose}
        foot={
          <button className="btn btn-primary" onClick={onClose} data-autofocus>
            {UPDATE.close}
          </button>
        }
      >
        <div className="callout" role="alert">
          <p className="upd-said">{detail || UPDATE.pullNoReason}</p>
        </div>
      </Dialog>
    );

  const real = realPositions(s).length;
  return (
    <Dialog
      title={UPDATE.title}
      lead={UPDATE.lead(offer.short)}
      onClose={onClose}
      foot={
        <>
          <button className="btn" onClick={onClose} disabled={installing} data-autofocus>
            {UPDATE.notNow}
          </button>
          <button className="btn btn-primary" id="dlg-ok" disabled={installing || phase !== "idle"} onClick={onInstall}>
            {UPDATE.install}
          </button>
        </>
      }
    >
      <dl className="facts">
        <dt>{UPDATE.versionLabel}</dt>
        <dd>
          <code>{offer.short}</code>
        </dd>
        <dt>{UPDATE.changesLabel}</dt>
        <dd>{offer.count != null ? count(offer.count) : <small>{UPDATE.changesUncounted}</small>}</dd>
      </dl>
      <div className="upd-list">
        <h4>{UPDATE.listLabel}</h4>
        {offer.changes.length ? <pre tabIndex={0}>{offer.changes.join("\n")}</pre> : <p className="small muted">{UPDATE.noList}</p>}
      </div>
      {s.mode === "live" && (
        <div className="callout callout-real">
          <h4>{UPDATE.liveTitle}</h4>
          <p className="small">{UPDATE.live(real)}</p>
        </div>
      )}
      <WillWont will={UPDATE.will} wont={UPDATE.wont} />
      <p className="small muted" role="status" aria-live="polite">
        {installing ? UPDATE.installing(secs) : ""}
      </p>
    </Dialog>
  );
}
