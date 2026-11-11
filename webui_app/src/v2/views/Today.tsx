// Today: what needs you, the one-sentence state with the last decision, what
// is held and what the agent will do with it, what the agent is doing right
// now, today's limits, today so far, money so far.
import type { Activity, Position, Status } from "../../lib/viewModel";
import { answerLook, answerWhy, describe, sellsHow } from "../lib/copy";
import {
  brokerLabel,
  dayBooks,
  dayTally,
  nextFor,
  notes,
  paidFor,
  practicePositions,
  realPositions,
  showPaperBook,
  statement,
  type Note,
} from "../lib/derive";
import { UNKNOWN, contractName, count, dayLong, etDayKey, etTime, etWhen, plural, signed, tone, usd } from "../lib/format";
import { ExpiresFlag, Icon, Stamp, Tag } from "../ui";
import type { Page } from "../App";

// ── notes ───────────────────────────────────────────────────────────────────

const NOTE_ACTION: Record<Note["actions"][number], string> = {
  resume: "Resume",
  start: "Open setup to start it",
  retry: "Try again",
  settings: "Open Settings",
  signals: "Signals & pass",
  rules: "Rules & limits",
};

export function Notes({ page, crit }: { page: Page; crit: boolean }) {
  const list = notes(page.s, page.link).filter((n) => (n.level === "crit") === crit);
  if (!list.length) return null;
  return (
    <section className="notes" aria-label={crit ? "Needs you now" : "Good to know today"}>
      {list.map((n) => (
        <div key={n.key} className={`note note-${n.level}`} role={crit ? "alert" : undefined}>
          <Icon name={n.icon} />
          <h3>{n.title}</h3>
          <p>{n.text}</p>
          {n.actions.length > 0 && (
            <div className="note-act">
              {n.actions.map((a) =>
                a === "start" ? (
                  <a key={a} className="btn btn-primary" href="/setup">
                    {NOTE_ACTION[a]}
                  </a>
                ) : (
                  <button
                    key={a}
                    className={`btn ${a === "resume" ? "btn-primary" : ""}`}
                    disabled={page.busy && (a === "resume" || a === "retry")}
                    onClick={() =>
                      a === "resume" ? page.act("resume") : a === "retry" ? page.act("retry") : page.go(a === "settings" ? "settings" : a)
                    }
                  >
                    {NOTE_ACTION[a]}
                  </button>
                ),
              )}
            </div>
          )}
        </div>
      ))}
    </section>
  );
}

// ── statement + last decision ───────────────────────────────────────────────

function LastDecision({ page }: { page: Page }) {
  const e: Activity | undefined = page.rows[0];
  if (!e) return null;
  const d = describe(e, page.s.broker.name);
  const today = etDayKey(page.s.as_of);
  const when = e.at ? (etDayKey(e.at) === today ? etTime(e.at) : etWhen(e.at)) : "time not recorded";
  return (
    <button className="lastdec" data-ev={e.n} onClick={() => page.openEv(e.n)}>
      <span className="eico" data-cat={d.cat}>
        <Icon name={d.icon} />
      </span>
      <span className="ld">
        <span className="ld-k">
          Last decision, {when}
          {page.stale ? " (last report)" : ""}
        </span>
        <span className="ld-t">{d.title}</span>
        <span className="ld-s">{d.sub}.</span>
      </span>
      <span className="ld-go">
        Why
        <Icon name="chev" small />
      </span>
    </button>
  );
}

function StatementBlock({ page }: { page: Page }) {
  const st = statement(page.s, page.link);
  const live = page.s.mode === "live";
  return (
    <>
      <section className="statement">
        <div>
          <h1>{st.h}</h1>
          <p className="lead">{st.lead}</p>
        </div>
        {!st.empty && <LastDecision page={page} />}
      </section>
      {st.empty && (
        <div className="empty">
          <h3>What happens next</h3>
          <ol className="steps3">
            <li>
              <span>
                <b>A signal arrives.</b> Signals come while the market is open. Some days bring none.
              </span>
            </li>
            <li>
              <span>
                <b>The agent checks it against your rules and limits.</b> If it passes, it{" "}
                {live ? "buys" : "records a practice trade"}. If not, Activity shows exactly which check stopped it.
              </span>
            </li>
            <li>
              <span>
                <b>You decide when to go LIVE.</b>{" "}
                {live
                  ? "You are LIVE now. Switch to practice at any time; nothing becomes pretend or real without your confirmation."
                  : "After a few practice days, compare Results with your rules. Nothing becomes real until you switch to LIVE and confirm."}
              </span>
            </li>
          </ol>
          <div>
            <button className="btn" onClick={() => page.go("rules")}>
              Review my rules and limits
            </button>
          </div>
        </div>
      )}
    </>
  );
}

// ── holdings ────────────────────────────────────────────────────────────────

function HoldRow({ p, page, wide }: { p: Position; page: Page; wide: boolean }) {
  const s = page.s;
  const nx = nextFor(p, s, page.link);
  const name = contractName(p.contract, p.label);
  const expires = p.contract ? `expires ${dayLong(p.contract.expiry)}` : "expiry not known";
  const paid = p.entry_cost_usd != null ? usd(p.entry_cost_usd) : UNKNOWN;
  const label = `${name}, ${p.real ? "real money" : "practice"}, ${plural(p.qty, "contract")}, ${p.entry_cost_usd != null ? `${paid} paid` : "amount paid not known"}, ${expires}${p.expires_today ? ", today" : ""}. ${nx.text}`;
  const today = etDayKey(s.as_of);
  return (
    <li className={`hrow ${p.real ? "real" : "prac"}`}>
      <button data-pos={p.id} onClick={() => page.openPos(p.id)} aria-label={label}>
        <span className="h-name">
          <b>{name}</b>
          {p.expires_today && <ExpiresFlag />}
          <small>
            {plural(p.qty, "contract")}, {expires}
          </small>
        </span>
        {wide && (
          <span className="h-bought">
            <small>Bought</small>
            {etWhen(p.opened_at, today)}
          </span>
        )}
        <span className="h-paid">
          {p.entry_cost_usd != null ? paid : <span className="faint">{UNKNOWN}</span>}
          <small>{wide && p.entry_price != null ? `${count(p.qty)} × ${usd(p.entry_price)}` : "paid"}</small>
        </span>
        <span className={`h-next ${nx.look}`}>
          <Icon name={nx.icon} small />
          <span>{nx.text}</span>
        </span>
        <span className="chev">
          <Icon name="chev" small />
        </span>
      </button>
    </li>
  );
}

export function HoldGroups({ page, wide }: { page: Page; wide: boolean }) {
  const s = page.s;
  const rp = realPositions(s);
  const pp = practicePositions(s);
  const paid = paidFor(rp);
  const note =
    !rp.length || page.stale || !s.real_positions_managed
      ? ""
      : s.pause.kind === "entries"
        ? " Still sold while new trades are paused."
        : s.mode === "practice"
          ? " Still sold for real while you practice."
          : "";
  return (
    <>
      <div className={`hhead ${wide ? "wide" : ""}`} aria-hidden="true">
        <span>Contract</span>
        {wide && <span>Bought</span>}
        <span className="r">Paid</span>
        <span>What the agent will do</span>
        <span />
      </div>
      {rp.length > 0 && (
        <div className="hgroup">
          <div className="hgroup-h">
            <Tag kind="real" />
            <span>
              {rp.length} at {brokerLabel(s)}
              {paid != null ? `, ${usd(paid)} paid.` : ", amount paid not known for all of them."}
              {note}
            </span>
          </div>
          <ul className={`hlist ${wide ? "wide" : ""}`}>
            {rp.map((p) => (
              <HoldRow key={p.id} p={p} page={page} wide={wide} />
            ))}
          </ul>
        </div>
      )}
      {pp.length > 0 && (
        <div className="hgroup">
          <div className="hgroup-h">
            <Tag kind="prac" />
            <span>{pp.length} pretend, only in the agent's records. No order is ever sent.</span>
          </div>
          <ul className={`hlist ${wide ? "wide" : ""}`}>
            {pp.map((p) => (
              <HoldRow key={p.id} p={p} page={page} wide={wide} />
            ))}
          </ul>
        </div>
      )}
    </>
  );
}

function HoldingsPanel({ page }: { page: Page }) {
  const s = page.s;
  if (!s.positions.length)
    return (
      <section className="panel a-hold">
        <div className="ph">
          <h2>Holding now</h2>
        </div>
        <div className="pb">
          <div className="empty">
            <h3>Nothing yet</h3>
            <p>When the agent buys something, real or practice, it appears here with what was paid and what the agent will do with it.</p>
          </div>
        </div>
      </section>
    );
  return (
    <section className={`panel a-hold ${page.stale ? "stale" : ""}`}>
      <div className="ph">
        <h2>Holding now</h2>
        <span className="ph-r">
          {page.stale && <Stamp at={page.asOf} />}
          <button className="btn btn-quiet btn-sm" onClick={() => page.go("holdings")}>
            All holdings
          </button>
        </span>
      </div>
      <div className="stale-c">
        <HoldGroups page={page} wide={false} />
      </div>
      <p className="pfoot">
        An exit signal is the signal provider saying a trade is over. The agent has no stop-loss and no profit target: it sells {sellsHow(s)}.
        Today's value is not tracked yet; {s.broker.name || "your broker"} shows live prices.
      </p>
    </section>
  );
}

// ── doing right now ─────────────────────────────────────────────────────────

function DoingPanel({ page }: { page: Page }) {
  const s = page.s;
  const c = s.capabilities;
  const expiring = realPositions(s).find((p) => p.next.action === "sell_at_expiry_guard");
  const row = (q: string, which: "opening" | "closing" | "listening") => {
    const a = c[which];
    const look = page.link === "lost" ? { cls: "unk", icon: "q" as const, word: "Unknown" } : answerLook(a);
    let why =
      page.link === "lost"
        ? `This page has had no answer since ${etTime(s.as_of)}. The agent's last report said: ${answerWhy(which, a, s)}`
        : answerWhy(which, a, s);
    if (which === "closing" && a.state === "yes" && a.reason_code === "ok" && expiring && page.link === "ok")
      why += ` ${contractName(expiring.contract, expiring.label)} expires today and is sold at ${expiring.next.at ? etTime(expiring.next.at) : "the expiry sell time"} if still open. Nothing for you to do.`;
    return (
      <li>
        <span className="d-q">{q}</span>
        <span className={`d-a ${look.cls}`}>
          <Icon name={look.icon} small />
          {look.word}
        </span>
        <span className="d-why">{why}</span>
      </li>
    );
  };
  return (
    <section className={`panel a-doing ${page.stale ? "stale" : ""}`}>
      <div className="ph">
        <h2>What the agent is doing right now</h2>
        {page.stale && <Stamp at={page.asOf} />}
      </div>
      <ul className="doing">
        {row("Opening new trades", "opening")}
        {row("Closing open trades", "closing")}
        {row("Listening for signals", "listening")}
      </ul>
    </section>
  );
}

// ── today's limits ──────────────────────────────────────────────────────────

function Segments({ on, total, cls = "" }: { on: number; total: number; cls?: string }) {
  const n = Math.max(total, on, 1);
  return (
    <>
      {Array.from({ length: Math.min(n, 60) }, (_, i) => (
        <i key={i} className={i < Math.round((on / n) * Math.min(n, 60)) ? `on ${cls}` : ""} />
      ))}
    </>
  );
}

export function LimitsPanel({ page, linkLabel = "Change limits" }: { page: Page; linkLabel?: string | null }) {
  const s = page.s;
  const L = s.limits;
  const live = s.mode === "live";
  const used = live ? L.used_today.real : L.used_today.practice;
  const other = live ? L.used_today.practice : L.used_today.real;
  const lost = live ? L.realised_loss_today.real : L.realised_loss_today.practice;
  const unmeasured = live ? L.unmeasured_exits_today.real : L.unmeasured_exits_today.practice;
  const rp = L.open_positions.real;
  const pp = L.open_positions.practice;
  const n = rp + pp;
  const slots = Math.max(L.max_positions, n, 1);
  const many = (x: number) => (x > 24 ? "many" : "");
  // at most 60 segments: beyond that the bar is drawn to scale
  const shown = Math.min(slots, 60);
  const rpShown = slots > 60 ? Math.ceil((rp / slots) * 60) : rp;
  const nShown = slots > 60 ? Math.ceil((n / slots) * 60) : n;
  const b = s.broker;
  const bname = b.name || "your broker";
  const m = L.trade_size.main;
  const afford = L.cash.trades_affordable;
  return (
    <section className={`panel a-lim ${page.stale ? "stale" : ""}`}>
      <div className="ph">
        <h2>Today's limits</h2>
        <span className="ph-r">
          {page.stale && <Stamp at={page.asOf} />}
          {linkLabel && (
            <button className="btn btn-quiet btn-sm" onClick={() => page.go("rules")}>
              {linkLabel}
            </button>
          )}
        </span>
      </div>
      <div className="pb limits stale-c">
        <div className="limit">
          <div className="limit-top">
            <span>New {live ? "" : "practice "}trades today</span>
            <b>
              {count(used)} <small>of {count(L.cap)}</small>
            </b>
          </div>
          <div className={`meter ${many(L.cap)}`} role="img" aria-label={`${used} of ${L.cap} used`}>
            <Segments on={used} total={L.cap} />
          </div>
          <p>
            {L.cap_reached ? (
              <>
                <b>Limit reached.</b> No more new trades today. The count starts again at {etTime(L.resets_at)}.
              </>
            ) : (
              `${count(Math.max(0, L.cap - used))} more allowed today. The count starts again at ${etTime(L.resets_at)}.`
            )}
            {other > 0 && ` ${plural(other, live ? "practice trade" : "real trade")} today ${other === 1 ? "is" : "are"} counted separately.`}
          </p>
        </div>
        <div className="limit">
          <div className="limit-top">
            <span>Lost today, of your daily loss limit</span>
            <b>
              <span className={lost > 0 ? "loss" : ""}>{usd(lost, 0)}</span> <small>{L.max_daily_loss != null ? `of ${usd(L.max_daily_loss, 0)}` : "no limit set"}</small>
            </b>
          </div>
          {L.max_daily_loss != null && (
            <div className="meter bar" role="img" aria-label={`${usd(lost, 0)} of ${usd(L.max_daily_loss, 0)}`}>
              <i style={{ flex: `0 0 ${Math.min(100, (lost / L.max_daily_loss) * 100)}%` }} className="on" />
            </div>
          )}
          <p>
            {L.max_daily_loss != null
              ? L.loss_limit_reached
                ? "Limit reached: the agent opens nothing new until tomorrow. This limit never sells anything."
                : `If today's closed trades lose ${usd(L.max_daily_loss, 0)}, the agent opens nothing new until tomorrow. This limit never sells anything.`
              : "You have not set a daily loss limit, so losses never stop new trades."}
            {unmeasured > 0 && ` ${plural(unmeasured, "sale")} today could not be measured and ${unmeasured === 1 ? "is" : "are"} not in this figure.`}
          </p>
        </div>
        <div className="limit">
          <div className="limit-top">
            <span>Open positions</span>
            <b>
              {count(n)} <small>of {count(L.max_positions)}</small>
            </b>
          </div>
          <div className={`meter ${many(slots)}`} role="img" aria-label={`${rp} real money, ${pp} practice, of ${L.max_positions} allowed`}>
            {Array.from({ length: shown }, (_, i) => (
              <i key={i} className={i < rpShown ? "on real" : i < nShown ? "on prac" : ""} />
            ))}
          </div>
          <p>
            {n ? (
              <>
                <span className="sq sq-real" /> {rp} real money <span className="sq sq-prac" style={{ marginLeft: 10 }} /> {pp} practice
              </>
            ) : (
              "Nothing open."
            )}
          </p>
        </div>
        <div className="limit">
          <div className="limit-top">
            <span>Cash at {bname}</span>
            <b>
              {b.buying_power != null ? (
                <>
                  {(page.stale || b.connected === false) && <small>last read </small>}
                  {usd(b.buying_power)}
                </>
              ) : (
                <small>{UNKNOWN}</small>
              )}
            </b>
          </div>
          <p>
            {b.buying_power == null ? (
              `${bname} has not reported a cash figure to the agent.`
            ) : (
              <>
                {L.cash.low ? (
                  <b>{L.cash.low_reason === "under_trade_size" && m.usd != null ? `Not enough for one trade at your ${usd(m.usd, 0)} size.` : "Too little to buy with."}</b>
                ) : afford != null && m.usd != null ? (
                  `Enough for ${plural(afford, "trade")} at your ${usd(m.usd, 0)} size.`
                ) : (
                  ""
                )}{" "}
                {b.buying_power_at ? `Read from ${bname} at ${etTime(b.buying_power_at)}.` : ""}
              </>
            )}
          </p>
        </div>
      </div>
    </section>
  );
}

// ── today so far / money so far ─────────────────────────────────────────────

function DayPanel({ page }: { page: Page }) {
  const s = page.s;
  const today = etDayKey(s.as_of);
  const live = s.mode === "live";
  const t = dayTally(page.rows, today, page.hasOlder, s.broker.name);
  const anyToday = page.rows.some((e) => etDayKey(e.at) === today);
  if (!anyToday) return null;
  const books = dayBooks(s);
  const opened = live ? s.limits.used_today.real : s.limits.used_today.practice;
  const least = t.complete ? "" : "at least ";
  return (
    <section className={`panel a-day ${page.stale ? "stale" : ""}`}>
      <div className="ph">
        <h2>Today so far</h2>
        <span className="ph-r">
          {page.stale && <Stamp at={page.asOf} />}
          <button className="btn btn-quiet btn-sm" onClick={() => page.go("activity")}>
            All activity
          </button>
        </span>
      </div>
      <div className="pb stale-c">
        <ul className="tally">
          <li>
            <span>Opened</span>
            <b>{plural(opened, books.opened)}</b>
          </li>
          {books.closed.map(({ label, word, day }) => (
            <li key={label}>
              <span>{label}</span>
              <b>
                {day.closed ? (
                  <>
                    {plural(day.closed, word)}, <span className={tone(day.pnl)}>{signed(day.pnl)}</span>
                    {day.unmeasured ? `, plus ${plural(day.unmeasured, "sale")} not measured` : ""}
                  </>
                ) : day.unmeasured ? (
                  `${plural(day.unmeasured, "sale")}, not measured`
                ) : (
                  "none"
                )}
              </b>
            </li>
          ))}
        </ul>
        <p className="tally-h">
          {t.stoppedTotal
            ? `Your limits and rules stopped ${least}${plural(t.stoppedTotal, "signal")}`
            : t.complete
              ? "Your limits and rules stopped nothing today"
              : "Your limits and rules: see Activity for today's earlier signals"}
        </p>
        <ul className="tally">
          {t.stopped.map((x) => (
            <li key={x.label}>
              <span>{x.label}</span>
              <b>{x.n}</b>
            </li>
          ))}
        </ul>
        <p className={`tally-h ${t.problems ? "bad" : ""}`}>
          {t.problems ? (
            <>
              <Icon name="alert" small />
              {least}
              {plural(t.problems, "problem")} today
            </>
          ) : t.complete ? (
            "No problems today"
          ) : (
            "No problems in the latest activity"
          )}
        </p>
      </div>
    </section>
  );
}

export function SignalCostLine({ s }: { s: Status }) {
  const c = s.results.signal_cost;
  const days = s.signals.pass.days_left;
  if (!c.applies) return null;
  return (
    <li>
      <span>
        Signals paid for
        <small>
          {plural(c.days_used, "signal day")} recorded as used
          {c.days_estimated > 0 ? `, plus about ${c.days_estimated} from before that was recorded` : ""}.
          {days != null ? ` ${plural(days, "day")} left.` : ""}
        </small>
      </span>
      <b className="soft">{c.usd_paid != null ? `−${usd(c.usd_paid)}` : "not tracked yet"}</b>
    </li>
  );
}

function MoneyPanel({ page }: { page: Page }) {
  const s = page.s;
  const R = s.results.real;
  const P = s.results.practice;
  const A = s.results.paper_account;
  const c = s.results.signal_cost;
  const net = c.applies && c.usd_paid != null ? R.total - c.usd_paid : null;
  return (
    <section className={`panel a-money ${page.stale ? "stale" : ""}`}>
      <div className="ph">
        <h2>Money so far</h2>
        <span className="ph-r">
          {page.stale && <Stamp at={page.asOf} />}
          <button className="btn btn-quiet btn-sm" onClick={() => page.go("results")}>
            Full results
          </button>
        </span>
      </div>
      <div className="pb stale-c">
        <ul className="money">
          <li>
            <span>
              <Tag kind="real" />
              <small>
                {plural(R.closed, "closed trade")}, all time. Estimated from quoted prices.
                {R.unmeasured > 0 ? ` ${plural(R.unmeasured, "sale")} could not be measured.` : ""}
                {s.broker.paper_account ? " Older lines that did not record the account may include paper-account sales." : ""}
              </small>
            </span>
            <b className={tone(R.total)}>{signed(R.total)}</b>
          </li>
          <SignalCostLine s={s} />
          {net != null && (
            <li className="total">
              <span>
                <strong>Real money after signal cost</strong>
              </span>
              <b className={tone(net)}>{signed(net)}</b>
            </li>
          )}
          {showPaperBook(s) && (
            <li className="sep">
              <span>
                <Tag kind="paper" />
                <small>
                  {plural(A.closed, "closed trade")} in a broker's paper-trading account. Not real money.
                  {A.unmeasured > 0 ? ` ${plural(A.unmeasured, "sale")} could not be measured.` : ""}
                </small>
              </span>
              <b className={`${tone(A.total)} soft`}>{signed(A.total)}</b>
            </li>
          )}
          <li className="sep">
            <span>
              <Tag kind="prac" />
              <small>{plural(P.closed, "closed pretend trade")}. Never counted as money.</small>
            </span>
            <b className={`${tone(P.total)} soft`}>{signed(P.total)}</b>
          </li>
        </ul>
      </div>
    </section>
  );
}

export function Today({ page }: { page: Page }) {
  const empty = statement(page.s, page.link).empty;
  return (
    <div className="view">
      <Notes page={page} crit />
      <StatementBlock page={page} />
      <Notes page={page} crit={false} />
      <div className="today-grid">
        <div className="stack">
          <HoldingsPanel page={page} />
          {empty ? <MoneyPanel page={page} /> : <DayPanel page={page} />}
        </div>
        <div className="stack">
          <DoingPanel page={page} />
          <LimitsPanel page={page} />
          {!empty && <MoneyPanel page={page} />}
        </div>
      </div>
    </div>
  );
}
