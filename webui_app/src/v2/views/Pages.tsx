// Holdings, Rules & limits, Signals & pass, Settings, More (phone).
// These show what the status says. Editing still happens in the existing
// settings pages (/setup): this dashboard links there instead of saving
// anything itself.
import type { ReactNode } from "react";
import type { Status, TradeSize } from "../../lib/viewModel";
import { brokerLabel } from "../lib/derive";
import { UNKNOWN, count, etDayKey, etTime, etWhen, plural, usd } from "../lib/format";
import { Icon, Stamp, type IconId } from "../ui";
import type { Page, View } from "../App";
import { HoldGroups, LimitsPanel, Notes } from "./Today";

const SETUP = "/setup";

function Row({ label, exp, val, act, valClass = "sm" }: { label: ReactNode; exp: ReactNode; val?: ReactNode; act?: ReactNode; valClass?: string }) {
  return (
    <div className="setrow">
      <span className="lbl">{label}</span>
      <span className="exp">{exp}</span>
      <span className={`val ${valClass}`}>{val}</span>
      <span className="act">{act}</span>
    </div>
  );
}
const plain = (t: string, strong = true) => (
  <span className="tag tag-plain" style={strong ? { color: "var(--ink)" } : undefined}>
    {t}
  </span>
);
const SetupLink = ({ children = "Change in Settings" }: { children?: ReactNode }) => (
  <a className="btn btn-sm" href={SETUP}>
    {children}
  </a>
);

// ── Holdings ────────────────────────────────────────────────────────────────

export function HoldingsView({ page }: { page: Page }) {
  const s = page.s;
  const head = (
    <div className="view-h">
      <div>
        <h1>Holdings</h1>
        <p className="lead">
          Everything the agent bought and has not sold yet, with what it will do next. Real money and practice are always kept apart.
        </p>
      </div>
      {page.stale && <Stamp at={page.asOf} />}
    </div>
  );
  if (!s.positions.length)
    return (
      <div className="view">
        <Notes page={page} crit />
        {head}
        <div className="empty">
          <h3>Nothing open</h3>
          <p>
            The agent is not holding anything, real or practice. When it buys, the position appears here with what was paid and what the agent
            will do with it.
          </p>
        </div>
      </div>
    );
  return (
    <div className="view">
      <Notes page={page} crit />
      {head}
      <section className={`panel ${page.stale ? "stale" : ""}`}>
        <div className="stale-c">
          <HoldGroups page={page} wide />
        </div>
      </section>
      <p className="small muted">
        Today's value is not tracked yet; {s.broker.name || "your broker"} shows live prices. Practice positions are never turned into real
        orders, including if you switch to LIVE. If you sell something yourself at your broker, the agent notices and stops tracking it.
      </p>
    </div>
  );
}

// ── Rules & limits ──────────────────────────────────────────────────────────

function sizeText(t: TradeSize): { val: string; note: string } {
  const cap = t.max_contracts != null ? ` At most ${plural(t.max_contracts, "contract")} a trade.` : "";
  if (t.mode === "budget") return { val: t.usd != null ? usd(t.usd, 0) : UNKNOWN, note: `Up to this many dollars on one trade.${cap}` };
  if (t.mode === "percent")
    return { val: t.percent != null ? `${t.percent}%` : UNKNOWN, note: `This share of your account value on one trade, worked out at each entry.${cap}` };
  return { val: t.contracts != null ? plural(t.contracts, "contract") : UNKNOWN, note: `A fixed number of contracts on every trade, whatever they cost.${cap}` };
}

export function RulesView({ page }: { page: Page }) {
  const s = page.s;
  const L = s.limits;
  const live = s.mode === "live";
  const used = live ? L.used_today.real : L.used_today.practice;
  const lost = live ? L.realised_loss_today.real : L.realised_loss_today.practice;
  const n = L.open_positions.real + L.open_positions.practice;
  const main = sizeText(L.trade_size.main);
  const other = sizeText(L.trade_size.other);
  const mu = L.trade_size.main.usd;
  const ou = L.trade_size.other.usd;
  const perTrade = mu != null && ou != null ? Math.max(mu, ou) : null;
  const guard = s.positions.find((p) => p.next.expiry_guard_at)?.next.expiry_guard_at ?? null;
  return (
    <div className="view">
      <div className="view-h">
        <div>
          <h1>Rules & limits</h1>
          <p className="lead">
            These are the values your agent is running on right now. They are changed in Settings; changing one never switches Practice or
            LIVE, and never buys or sells anything.
          </p>
        </div>
        {page.stale && <Stamp at={page.asOf} />}
      </div>
      {live && page.link === "ok" && (
        <div className="note note-info">
          <Icon name="info" />
          <h3>You are LIVE</h3>
          <p>A change to these values applies to real orders from the next signal.</p>
        </div>
      )}
      <section className={`panel ${page.stale ? "stale" : ""}`}>
        <div className="ph">
          <h2>Daily limits</h2>
          <SetupLink />
        </div>
        <Row
          label="New trades per day"
          exp="The most new positions the agent may open in one day. Selling is never limited."
          valClass=""
          val={
            <>
              {count(L.cap)}
              <small>{count(used)} used today</small>
            </>
          }
        />
        <Row
          label="Daily loss limit"
          exp="When the day's closed trades have lost this much, the agent opens nothing new until the next day. It is not a stop-loss: it never sells anything."
          valClass=""
          val={
            L.max_daily_loss != null ? (
              <>
                {usd(L.max_daily_loss, 0)}
                <small>{usd(lost, 0)} lost today</small>
              </>
            ) : (
              <>
                Off<small>no limit set</small>
              </>
            )
          }
        />
        <Row
          label="Open positions at once"
          exp="The most positions held at the same time, real and practice counted together."
          valClass=""
          val={
            <>
              {count(L.max_positions)}
              <small>{count(n)} open now</small>
            </>
          }
        />
      </section>
      <section className={`panel ${page.stale ? "stale" : ""}`}>
        <div className="ph">
          <h2>Trade size</h2>
          <SetupLink />
        </div>
        <Row label="Trade size, main pick" exp={main.note} valClass="" val={main.val} />
        <Row
          label="Trade size, other trades"
          exp={L.trade_size.other.same_as_main ? "No size of its own: the main-pick size applies." : other.note}
          valClass=""
          val={
            <>
              {other.val}
              {L.trade_size.other.same_as_main && <small>same as main pick</small>}
            </>
          }
        />
      </section>
      <div className="two">
        <section className={`panel ${page.stale ? "stale" : ""}`}>
          <div className="ph">
            <h2>With these limits</h2>
            <span className="meta">Worked out from the values above</span>
          </div>
          <div className="pb">
            <ul className="tally">
              <li>
                <span>Most put into new trades in one day</span>
                <b>{perTrade != null ? usd(L.cap * perTrade, 0) : "depends on prices"}</b>
              </li>
              <li>
                <span>Most on one trade</span>
                <b>{perTrade != null ? usd(perTrade, 0) : "depends on prices"}</b>
              </li>
              <li>
                <span>Most held at once</span>
                <b>{plural(L.max_positions, "position")}</b>
              </li>
              <li>
                <span>Cash at {s.broker.name || "your broker"}</span>
                <b>{s.broker.buying_power != null ? usd(s.broker.buying_power) : UNKNOWN}</b>
              </li>
            </ul>
            <p className="small muted" style={{ marginTop: 8 }}>
              {perTrade == null ? "Your trade size is not a dollar amount, so there is no fixed dollar ceiling to show. " : ""}
              The most put in is also the most those trades can lose: an option can lose everything paid for it. The agent has no stop-loss
              and no profit target. It sells on the exit signal or on expiry day.
            </p>
          </div>
        </section>
        <section className="panel">
          <div className="ph">
            <h2>Always on</h2>
            <span className="meta">Part of how the agent works</span>
          </div>
          <div className="pb">
            <ul className="always">
              <li>
                <Icon name="shield" />
                <span>
                  <b>Always sells what it bought when the exit signal arrives,</b> whatever your entry rules say, and while new trades are
                  paused.
                </span>
              </li>
              <li>
                <Icon name="shield" />
                <span>
                  <b>Runs every entry signal through the same checks, in the same order.</b> Activity shows which one stopped a signal.
                </span>
              </li>
              <li>
                <Icon name="shield" />
                <span>
                  <b>Skips rather than guesses.</b> No price, or a signal that arrived late, means no order.
                </span>
              </li>
              <li>
                <Icon name="shield" />
                <span>
                  <b>Never goes LIVE by itself.</b> Going LIVE needs your confirmation in this window.
                </span>
              </li>
              {guard && (
                <li>
                  <Icon name="shield" />
                  <span>
                    <b>Expiry sell time.</b> For what you hold now, the next one is {etWhen(guard, etDayKey(s.as_of))}.
                  </span>
                </li>
              )}
            </ul>
          </div>
        </section>
      </div>
      <section className="panel">
        <div className="ph">
          <h2>Which signals to take</h2>
          <SetupLink>Open Settings</SetupLink>
        </div>
        <Row
          label="Your filters and written rules"
          exp="Stock lists, calls or puts, smallest strike, same-day contracts, losing-streak stop and your written rules are shown and changed in Settings. This page does not list them yet. When one of them stops a signal, Activity names it."
        />
      </section>
      <LimitsPanel page={page} linkLabel={null} />
    </div>
  );
}

// ── Signals & pass ──────────────────────────────────────────────────────────

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
};

export function SignalsView({ page }: { page: Page }) {
  const s = page.s;
  const sig = s.signals;
  const p = sig.pass;
  const c = s.results.signal_cost;
  const statusTag = p.status === "active" ? plain(PASS_STATUS[p.status]) : <span className="tag tag-warn">{PASS_STATUS[p.status] ?? "Unknown"}</span>;
  return (
    <div className="view">
      <div className="view-h">
        <div>
          <h1>Signals & pass</h1>
          <p>
            Your agent acts on trade signals from its signal source{sig.source ? ` (${sig.source})` : ""}. This page shows what access it has
            and when it last heard from the source.
          </p>
        </div>
        {page.stale && <Stamp at={page.asOf} />}
      </div>
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
              <li>
                <span>Automatic payment</span>
                <b>{p.auto_pay ? "On" : "Off"}</b>
              </li>
            </ul>
            <p className="small muted" style={{ marginTop: 10 }}>
              {p.status === "exits_only"
                ? "No days are left for new trades. The agent still receives exit signals for anything it holds, so open positions are still sold."
                : p.status === "waiting"
                  ? "The agent is waiting for a pass to become usable (a payment that has not arrived, or a day pass it will buy on the next market day)."
                  : p.status === "none"
                    ? "Without a pass the agent receives no signals."
                    : "If access runs out, the agent stops taking new trades."}
            </p>
            <div style={{ marginTop: 12 }}>
              <a className="btn" href={SETUP}>
                {p.kind === "prepaid" || p.kind === "none" ? "Add signal days in Settings" : "Open signal settings"}
              </a>
            </div>
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
              {sig.last_signal_from === "activity_log"
                ? "The newest-signal time is when the agent last logged handling a signal. "
                : ""}
              {!sig.reading && page.link === "ok"
                ? "The agent is deliberately not reading the feed right now (a hold, or a pause with no real position open)."
                : ""}
            </p>
          </div>
        </section>
      </div>
    </div>
  );
}

// ── Settings ────────────────────────────────────────────────────────────────

export function SettingsView({ page }: { page: Page }) {
  const s = page.s;
  const b = s.broker;
  const bad = b.connected === false;
  const today = etDayKey(s.as_of);
  const a = s.agent;
  return (
    <div className="view">
      <div className="view-h">
        <div>
          <h1>Settings</h1>
          <p>Connections and this computer. Everything here is changed in the settings pages; this page shows where things stand.</p>
        </div>
        {page.stale && <Stamp at={page.asOf} />}
      </div>
      <section className={`panel ${page.stale ? "stale" : ""}`}>
        <div className="ph">
          <h2>Connections</h2>
        </div>
        <Row
          label={`Broker: ${brokerLabel(s)}`}
          exp={
            bad ? (
              <>
                <b style={{ color: "var(--bad)" }}>Not connected{b.since ? ` since ${etTime(b.since)}` : ""}.</b> The agent cannot buy or sell
                until it is connected again.{b.last_error ? ` The broker's last answer: “${b.last_error}”.` : ""}
              </>
            ) : b.connected == null ? (
              "This broker connection does not report whether it is up."
            ) : (
              <>
                {b.last_answer ? `Answered at ${etTime(b.last_answer)}. ` : ""}
                {b.buying_power != null ? `Cash available ${usd(b.buying_power)}. ` : ""}
                {b.paper_account ? "This is the broker's paper-trading account." : ""}
              </>
            )
          }
          val={bad ? <span className="tag tag-bad">Not connected</span> : b.connected == null ? <span className="tag tag-unk">Unknown</span> : plain("Connected")}
          act={
            <a className={`btn btn-sm ${bad ? "btn-primary" : ""}`} href={SETUP}>
              {bad ? "Connect again" : "Broker settings"}
            </a>
          }
        />
        <Row
          label={`Signals${s.signals.source ? `: ${s.signals.source}` : ""}`}
          exp={s.signals.last_signal ? `Newest signal seen ${etWhen(s.signals.last_signal, today)}.` : "No signal seen yet."}
          val={plain(s.signals.reading ? "Reading" : "Not reading", s.signals.reading)}
          act={
            <button className="btn btn-sm" onClick={() => page.go("signals")}>
              Signals & pass
            </button>
          }
        />
        <Row
          label="AI helper, alerts, written rules"
          exp="Connected and changed in the settings pages."
          act={
            <a className="btn btn-sm" href={SETUP}>
              Open Settings
            </a>
          }
        />
      </section>
      <section className={`panel ${page.stale ? "stale" : ""}`}>
        <div className="ph">
          <h2>The agent on this computer</h2>
        </div>
        <Row
          label="Agent"
          exp={
            !a.running
              ? "No agent is running behind this page."
              : `${a.started_at ? `Started ${etWhen(a.started_at, today)}. ` : ""}${a.last_heartbeat ? `Last report at ${etTime(a.last_heartbeat)}.` : "No report yet."}`
          }
          val={!a.running ? <span className="tag tag-bad">Stopped</span> : a.answering ? plain("Running") : <span className="tag tag-unk">Not answering</span>}
          act={
            !a.running ? (
              <a className="btn btn-sm btn-primary" href={SETUP}>
                Open setup to start it
              </a>
            ) : undefined
          }
        />
        <Row
          label="Icon and window frame"
          exp="The frame around this window shows the mode without reading anything."
          val={
            <span className="legendrow">
              <span>
                <i className="glyph glyph-live" />
                LIVE
              </span>
              <span>
                <i className="glyph glyph-prac" />
                Practice
              </span>
              <span>
                <i className="glyph glyph-unk" />
                Not answering
              </span>
            </span>
          }
        />
        <Row
          label="Start at login, keep awake, updates, move to another computer"
          exp="These live in the settings pages and the classic dashboard."
          act={
            <a className="btn btn-sm" href={SETUP}>
              Open Settings
            </a>
          }
        />
        {a.running && (
          <Row
            label="Stop the agent completely"
            exp="Nothing is bought, sold or watched until you start it again. Open positions stay open at your broker."
            act={
              <button className="btn btn-sm" data-act="stop" onClick={() => page.act("stop")} disabled={page.busy || page.link !== "ok"}>
                Stop the agent…
              </button>
            }
          />
        )}
      </section>
      <section className="panel">
        <div className="ph">
          <h2>This dashboard</h2>
        </div>
        <Row
          label="Classic dashboard"
          exp="The dashboard you had before is still there, with the question box, help, tips and the move-computer tool."
          act={
            <a className="btn btn-sm" href="/dash">
              Back to the classic dashboard
            </a>
          }
        />
        <Row
          label="Theme"
          exp="Light or dark. Follows your computer until you choose."
          act={
            <button className="btn btn-sm" onClick={page.toggleTheme}>
              <Icon name={page.dark ? "sun" : "moon"} small />
              {page.dark ? "Light theme" : "Dark theme"}
            </button>
          }
        />
      </section>
    </div>
  );
}

// ── More (phone) ────────────────────────────────────────────────────────────

export function MoreView({ page }: { page: Page }) {
  const s = page.s;
  const days = s.signals.pass.days_left;
  const items: [View, IconId, string, string][] = [
    ["rules", "rules", "Rules & limits", "Daily limits and trade size"],
    ["signals", "signal", "Signals & pass", days != null ? `${plural(days, "signal day")} left` : "Signal access and when it last listened"],
    ["settings", "gear", "Settings", s.broker.connected === false ? `1 problem: ${s.broker.name || "the broker"} is not connected` : "Connections, this computer, classic dashboard"],
  ];
  return (
    <div className="view">
      <div className="view-h">
        <h1>More</h1>
      </div>
      <section className="panel">
        <ul className="morelist">
          {items.map((x) => (
            <li key={x[0]}>
              <button onClick={() => page.go(x[0])}>
                <Icon name={x[1]} />
                <span>
                  <b>{x[2]}</b>
                  <small>{x[3]}</small>
                </span>
                <Icon name="chev" small />
              </button>
            </li>
          ))}
        </ul>
      </section>
      <div className="more-foot">
        <button className="btn" onClick={page.toggleTheme}>
          <Icon name={page.dark ? "sun" : "moon"} small />
          {page.dark ? "Light theme" : "Dark theme"}
        </button>
        <a className="btn" href="/dash">
          Back to the classic dashboard
        </a>
      </div>
    </div>
  );
}
