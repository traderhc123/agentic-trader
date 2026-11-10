// The confirm dialogs. Each one sends exactly one of the commands the agent
// already understands (the same words the classic dashboard's buttons send):
//   Pause new trades   -> "pause"      Resume            -> "resume"
//   Switch to practice -> "dry on"     Go LIVE           -> "dry off"
//   Stop the agent     -> "stop"
// Nothing is assumed to have worked: the agent's own reply is shown, and the
// band changes only when the next status says so.
import { useState } from "react";
import type { Position, Status } from "../lib/viewModel";
import { brokerLabel, paidFor, practicePositions, realPositions } from "./lib/derive";
import { UNKNOWN, contractName, count, etTime, plural, usd } from "./lib/format";
import { Dialog, ExpiresFlag, Icon, WillWont } from "./ui";

export type DialogKind = "pause" | "resume" | "to-practice" | "to-live" | "stop";

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

function Summary({ s }: { s: Status }) {
  const L = s.limits;
  const m = L.trade_size.main;
  const o = L.trade_size.other;
  const size = (t: typeof m) =>
    t.mode === "budget" && t.usd != null
      ? `up to ${usd(t.usd, 0)}`
      : t.mode === "percent" && t.percent != null
        ? `${t.percent}% of your account`
        : t.mode === "contracts" && t.contracts != null
          ? plural(t.contracts, "contract")
          : UNKNOWN;
  const perTrade = m.usd != null && o.usd != null ? Math.max(m.usd, o.usd) : null;
  return (
    <dl className="facts">
      <dt>Broker account</dt>
      <dd>
        {brokerLabel(s)}
        {s.broker.paper_account && <small>The broker's paper-trading account.</small>}
        {s.broker.connected === false && <small>Not connected right now, so real orders can't be sent until it is.</small>}
      </dd>
      <dt>Cash available</dt>
      <dd>
        {s.broker.buying_power != null ? usd(s.broker.buying_power) : UNKNOWN}
        {s.broker.buying_power_at && <small>Read at {etTime(s.broker.buying_power_at)}.</small>}
        {L.cash.low && <small>Too little for one trade at your size, so buy orders would be rejected.</small>}
      </dd>
      <dt>Each trade</dt>
      <dd>
        {size(m)} <small>{o.same_as_main ? "The same for the provider's other trades." : `${size(o)} for the provider's other trades.`}</small>
      </dd>
      <dt>New trades per day</dt>
      <dd>
        {count(L.cap)}
        <small>
          {count(L.used_today.real)} real {their(L.used_today.real, "trade", "trades")} opened today. Practice trades are counted separately.
        </small>
      </dd>
      <dt>Daily loss limit</dt>
      <dd>
        {L.max_daily_loss != null ? usd(L.max_daily_loss, 0) : "Off"}{" "}
        <small>{L.max_daily_loss != null ? "Stops new trades for the day. It does not sell anything." : "Losses never stop new trades."}</small>
      </dd>
      {perTrade != null && (
        <>
          <dt>Most in new trades in a day</dt>
          <dd>{usd(L.cap * perTrade, 0)}</dd>
        </>
      )}
    </dl>
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
  /** the command to send, in the agent's own words */
  onConfirm: (command: string) => void;
  onClose: () => void;
}) {
  const [ack, setAck] = useState(false);
  const [choice, setChoice] = useState<"entries" | "all">("entries");
  const rp = realPositions(s);
  const pp = practicePositions(s);
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

  if (kind === "to-live")
    return (
      <Dialog
        tone="live"
        title="Switch to LIVE: real money"
        lead="From the next signal that passes your limits, the agent places real orders in your broker account. Check these are the numbers you expect."
        onClose={onClose}
        foot={
          <>
            <label className="check ack">
              <input type="checkbox" checked={ack} onChange={(e) => setAck(e.target.checked)} />
              <span id="dlg-why">I understand the agent will place real orders with my money, and I can lose it.</span>
            </label>
            {cancel("Stay in practice")}
            <button className="btn btn-golive" id="dlg-ok" disabled={!ack || busy} aria-describedby="dlg-why" onClick={() => onConfirm("dry off")}>
              Go LIVE with real money
            </button>
          </>
        }
      >
        <Summary s={s} />
        <WillWont
          will={[
            "Buy real option contracts when a signal passes your rules and limits.",
            "Sell them when the exit signal arrives, or at its expiry sell time on the day they expire.",
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
        <p className="small muted">The agent may still refuse (for example when your written rules need an AI helper that is not connected). Its answer is shown here.</p>
      </Dialog>
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
            <button className="btn btn-primary" id="dlg-ok" disabled={busy} onClick={() => onConfirm("dry on")}>
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
            <button className="btn btn-primary" id="dlg-ok" disabled={busy || choice !== "entries"} onClick={() => onConfirm("pause")}>
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
              ? "The agent keeps selling what you hold: on exit signals, and at its expiry sell time on expiry day. Choose this to take a break without leaving positions unwatched."
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
            <button className="btn btn-primary" id="dlg-ok" disabled={busy} onClick={() => onConfirm("resume")}>
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
          <button className="btn btn-primary" id="dlg-ok" disabled={busy} onClick={() => onConfirm("stop")}>
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
