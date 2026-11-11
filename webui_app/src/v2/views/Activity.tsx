// Activity: one list of full-row buttons at every width, grouped by market
// day, with the money tag that says whether an order existed. A day the page
// has only part of says how much, and no totals (ActivityPage.day_lines).
// The Problems filter groups them by kind. The drawer shows the checks in
// the order the agent runs them, with "stopped here".
import { useMemo, useState } from "react";
import type { Activity as Row, Status } from "../../lib/viewModel";
import { checkLabel, describe, type Cat, type Described } from "../lib/copy";
import { dayWhole, problemGroups } from "../lib/derive";
import { contractName, count, dayLong, etDayKey, etTime, etWhen, plural, signed, tone, usd } from "../lib/format";
import { Drawer, Icon, Stamp, Tag } from "../ui";
import type { Page } from "../App";

type Filter = "all" | Cat;
const FILTERS: [Filter, string][] = [
  ["all", "Everything"],
  ["trade", "Bought and sold"],
  ["limit", "Stopped by my limits"],
  ["problem", "Problems"],
];

function Amount({ e }: { e: Row }) {
  if (e.action === "entry" && e.qty != null && e.quote_price != null)
    return (
      <span className="amt">
        {usd(e.qty * e.quote_price * 100)}
        <small>
          {count(e.qty)} × {usd(e.quote_price)}
        </small>
      </span>
    );
  if (e.action === "exit" && e.est_pnl_usd != null)
    return (
      <span className={`amt ${tone(e.est_pnl_usd)}`}>
        {signed(e.est_pnl_usd)}
        <small>estimated result</small>
      </span>
    );
  return <span className="amt" />;
}

function EvRow({ e, d, onOpen }: { e: Row; d: Described; onOpen: (n: number) => void }) {
  return (
    <li>
      <button className="ev" data-ev={e.n} onClick={() => onOpen(e.n)}>
        <time dateTime={e.at ?? undefined}>{e.at ? etTime(e.at) : "time not recorded"}</time>
        <span className="eico" data-cat={d.cat}>
          <Icon name={d.icon} />
        </span>
        <span className="ebody">
          <span className="et">{d.title}</span>
          <span className="es">{d.sub}</span>
        </span>
        <span className="etag">
          <Tag kind={d.tag} />
        </span>
        <Amount e={e} />
      </button>
    </li>
  );
}

export function ActivityView({ page }: { page: Page }) {
  const [filter, setFilter] = useState<Filter>("all");
  const [realOnly, setRealOnly] = useState(false);
  const s = page.s;
  const feed = page.feed;
  const all = page.rows;
  const meta = useMemo(() => new Map(all.map((e) => [e.n, describe(e, s.broker.name)] as const)), [all, s.broker.name]);
  const mm = (e: Row) => meta.get(e.n) as Described;
  const inMoney = (e: Row) => !realOnly || mm(e).tag === "real";
  const cnt = (k: Filter) => all.filter((e) => (k === "all" || mm(e).cat === k) && inMoney(e)).length;
  const today = etDayKey(s.as_of);

  const head = (
    <div className="view-h">
      <div>
        <h1>Activity</h1>
        <p className="lead">
          Every signal the agent logged and what it did about it. Open any line to see which check decided it. Times are US Eastern (ET), the
          market's clock.
        </p>
      </div>
      {page.stale && <Stamp at={page.asOf} />}
    </div>
  );
  if (!all.length)
    return (
      <div className="view">
        {head}
        <div className="empty">
          <h3>No signals yet</h3>
          <p>The first signal appears here when it arrives, with what the agent did and why.</p>
        </div>
      </div>
    );

  const list = all.filter((e) => (filter === "all" || mm(e).cat === filter) && inMoney(e));
  const byDay = new Map<string, Row[]>();
  for (const e of list) {
    const d = etDayKey(e.at);
    if (!byDay.has(d)) byDay.set(d, []);
    (byDay.get(d) as Row[]).push(e);
  }
  const loaded = all.length;
  const more = feed.canLoadOlder;
  const groups = filter === "problem" ? problemGroups(list, s.broker.name) : [];
  return (
    <div className="view">
      {head}
      <div className="filters">
        <div className="chips" role="group" aria-label="Show">
          {FILTERS.map(([k, l]) => (
            <button key={k} className="chip" data-filter={k} aria-pressed={filter === k} onClick={() => setFilter(k)}>
              {l}
              <span className="n">{cnt(k)}</span>
            </button>
          ))}
        </div>
        <label className="check only">
          <input type="checkbox" checked={realOnly} onChange={(e) => setRealOnly(e.target.checked)} />
          <span>Real money only</span>
        </label>
      </div>
      {more && (
        <p className="small muted" data-counts="loaded">
          The counts are of the {plural(loaded, "line")} loaded so far, not of the whole log ({count(feed.total)} lines).
        </p>
      )}
      {groups.length > 0 && (
        <section className={`panel ${page.stale ? "stale" : ""}`} data-problems="by-kind">
          <div className="ph">
            <h2>Problems by kind</h2>
            <span className="meta">{more ? `In the ${plural(loaded, "line")} loaded so far` : "In the whole log"}</span>
          </div>
          <ul className="pgroups">
            {groups.map((g) => (
              <li key={g.key}>
                <button className="pgroup" onClick={() => page.openEv(g.last.n)}>
                  <span className="pg-t">
                    <b>{g.label}</b>
                    <span className="pg-n">×{count(g.n)}</span>
                  </span>
                  <span className="pg-s">
                    Last {g.last.at ? etWhen(g.last.at, today) : "at a time not recorded"}, {contractName(g.last.contract, g.last.contract_text)}
                  </span>
                  {g.hint && <span className="pg-h">Fix: {g.hint}</span>}
                </button>
              </li>
            ))}
          </ul>
        </section>
      )}
      <section className={`panel evpanel ${page.stale ? "stale" : ""}`}>
        {list.length === 0 && (
          <div className="pb">
            <p className="muted">
              Nothing of this kind{realOnly ? " with real money" : ""} in the {count(loaded)} most recent lines.
            </p>
          </div>
        )}
        {[...byDay.entries()].map(([d, rows]) => {
          // a day the page has only part of: how much, and no sums at all
          // (a partial count or P&L is never shown as the day's)
          const whole = dayWhole(d, all, feed.dayLines[d], more);
          if (!whole) {
            const have = all.filter((e) => etDayKey(e.at) === d).length;
            const of = feed.dayLines[d];
            return (
              <div key={d || "undated"}>
                <div className="day-h">
                  <h2>{d ? `${d === today ? "Today, " : ""}${dayLong(d)}` : "Time not recorded"}</h2>
                  <span className="day-t" data-partial>
                    {of != null ? `${count(have)} of ${plural(of, "line")} loaded` : `${plural(have, "line")} loaded so far`}. Show earlier activity for the rest of
                    the day and its totals.
                  </span>
                </div>
                <ul className="evlist">
                  {rows.map((e) => (
                    <EvRow key={e.n} e={e} d={mm(e)} onOpen={page.openEv} />
                  ))}
                </ul>
              </div>
            );
          }
          const c = (f: (e: Row) => boolean) => rows.filter(f).length;
          const b = c((e) => e.action === "entry");
          const so = c((e) => e.action === "exit");
          const st = c((e) => mm(e).cat === "limit");
          const pr = c((e) => mm(e).cat === "problem");
          const tot = (k: "real" | "prac" | "paper") => {
            const x = rows.filter((e) => e.action === "exit" && mm(e).tag === k && e.est_pnl_usd != null);
            if (!x.length) return null;
            const sum = x.reduce((a, e) => a + (e.est_pnl_usd as number), 0);
            return (
              <span className="dr">
                <Tag kind={k} />
                <b className={tone(sum)}>{signed(sum)}</b>
              </span>
            );
          };
          const bits =
            [b && `${b} bought`, so && `${so} sold`, st && `${st} stopped by your limits`, pr && plural(pr, "problem")]
              .filter(Boolean)
              .join(", ") || plural(rows.length, "event");
          return (
            <div key={d || "undated"}>
              <div className="day-h">
                <h2>{d ? `${d === today ? "Today, " : ""}${dayLong(d)}` : "Time not recorded"}</h2>
                <span className="day-t">{bits}</span>
                <span className="day-r">
                  {tot("real")}
                  {tot("paper")}
                  {tot("prac")}
                </span>
              </div>
              <ul className="evlist">
                {rows.map((e) => (
                  <EvRow key={e.n} e={e} d={mm(e)} onOpen={page.openEv} />
                ))}
              </ul>
            </div>
          );
        })}
        {feed.canLoadOlder ? (
          <div className="pfoot">
            <button className="btn" onClick={feed.loadOlder} disabled={feed.loadingOlder || page.link === "lost"}>
              {feed.loadingOlder ? "Loading…" : `Show earlier activity (${count(Math.max(0, feed.total - loaded))} more)`}
            </button>
            {feed.olderError && (
              <span className="err" role="alert" style={{ marginTop: 8 }}>
                <Icon name="alert" small />
                {feed.olderError}
              </span>
            )}
          </div>
        ) : (
          <p className="pfoot">That is everything in the agent's log ({plural(loaded, "line")}).</p>
        )}
      </section>
    </div>
  );
}

// ── the drawer ──────────────────────────────────────────────────────────────

function Checks({ e, d, s }: { e: Row; d: Described; s: Status }) {
  if (d.exitPath)
    return (
      <section>
        <h3>Checks</h3>
        <p>
          Selling skips the entry checks. The agent always sells what it bought when the exit signal arrives, or at its expiry sell time on
          expiry day, whatever your limits say.
        </p>
      </section>
    );
  const order = s.entry_checks;
  const at = e.stopped_at_check ? order.indexOf(e.stopped_at_check) : -1;
  const passedAll = e.reason_code === "entered";
  if (!passedAll && at < 0) {
    if (d.cat === "trade" || d.cat === "other") return null;
    return (
      <section>
        <h3>The checks, in the order the agent runs them</h3>
        <p className="muted">Which check stopped this signal was not recorded for this line (it was written by an earlier version).</p>
      </section>
    );
  }
  return (
    <section>
      <h3>The checks, in the order the agent runs them</h3>
      <ol className="checks">
        {order.map((id, i) => {
          const st = passedAll || i < at ? "pass" : i === at ? "stop" : "later";
          return (
            <li key={id} className={st}>
              <Icon name={st === "pass" ? "check" : st === "stop" ? "x" : "dash"} small />
              <span>
                {checkLabel(id)}
                {st === "stop" && (
                  <>
                    {" "}
                    <span className="tag tag-plain stophere">stopped here</span>
                  </>
                )}
                {st === "later" && (
                  <>
                    {" "}
                    <small>not reached</small>
                  </>
                )}
              </span>
            </li>
          );
        })}
      </ol>
      <p className="small muted">
        {passedAll ? "Every check passed, so the trade was taken." : "The first check that fails ends it. Nothing after that point runs."}
      </p>
    </section>
  );
}

export function EventDrawer({ e, page, onClose }: { e: Row; page: Page; onClose: () => void }) {
  const s = page.s;
  const d = describe(e, s.broker.name);
  const facts: [string, React.ReactNode][] = [
    ["When", e.at ? `${dayLong(etDayKey(e.at))} at ${etTime(e.at)}` : "time not recorded"],
    [
      "Contract",
      `${contractName(e.contract, e.contract_text)}${e.contract ? `, expires ${dayLong(e.contract.expiry)}` : ""}`,
    ],
    ["Signal type", e.book === "other" ? "Other trades" : e.book === "main" ? "Main pick" : "not recorded"],
    ["Agent mode then", e.mode_at_time === "live" ? "LIVE" : e.mode_at_time === "practice" ? "Practice" : "not recorded"],
    ["Money", d.money],
  ];
  if (e.qty != null)
    facts.push([
      "Size",
      e.quote_price != null
        ? `${plural(e.qty, "contract")} at a quoted ${usd(e.quote_price)} per share, about ${usd(e.qty * e.quote_price * 100)}`
        : plural(e.qty, "contract"),
    ]);
  if (e.fill_price != null) facts.push(["Fill price", `${usd(e.fill_price)} per share, reported by the broker`]);
  if (e.est_pnl_usd != null)
    facts.push([
      "Estimated result",
      <>
        <span className={tone(e.est_pnl_usd)}>{signed(e.est_pnl_usd)}</span>
        {d.tag === "prac" ? " (pretend)" : d.tag === "paper" ? " (paper account, not real money)" : ""}
      </>,
    ]);
  const goLabel = d.go === "rules" ? "Rules & limits" : d.go === "signals" ? "Signals & pass" : "Settings";
  return (
    <Drawer title={d.title} sub={<Tag kind={d.tag} />} onClose={onClose}>
      <section>
        <h3>What happened</h3>
        <p>{d.what}</p>
      </section>
      <section>
        <h3>Why</h3>
        <p>{d.why}</p>
      </section>
      <Checks e={e} d={d} s={s} />
      <section>
        <h3>What you can do</h3>
        <p>{d.can}</p>
        {d.go && (
          <div style={{ marginTop: 8 }}>
            <button
              className="btn btn-sm"
              onClick={() => {
                onClose();
                page.go(d.go as "rules" | "settings" | "signals");
              }}
            >
              Open {goLabel}
            </button>
          </div>
        )}
      </section>
      <section>
        <dl className="facts">
          {facts.map(([k, v]) => (
            <div key={k} style={{ display: "contents" }}>
              <dt>{k}</dt>
              <dd>{v}</dd>
            </div>
          ))}
        </dl>
      </section>
      <details className="tech">
        <summary>Technical details, for support</summary>
        <pre>
          {`event #${e.n}  ${e.action || "(no kind)"}  code=${e.reason_code}${e.cause ? `  cause=${e.cause}` : ""}
${e.contract_text ?? "no contract"}  book=${e.book ?? "?"}  real=${e.real ?? "?"}  sent=${e.order_sent ?? "?"}  filled=${e.filled ?? "?"}${
            e.stopped_at_check ? `\nstopped at: ${e.stopped_at_check}` : ""
          }${Object.keys(e.reason_params).length ? `\n${JSON.stringify(e.reason_params)}` : ""}`}
        </pre>
      </details>
    </Drawer>
  );
}
