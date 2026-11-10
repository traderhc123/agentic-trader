// Results: real money and practice are two books that never share a number.
import { Suspense, lazy, useMemo, useState } from "react";
import type { Book, Status } from "../../lib/viewModel";
import { PAPER_BOOK_CAPTION, bookToday, bookTodayNote, realBookCaption } from "../lib/derive";
import { dayLong, etDayKey, plural, signed, tone } from "../lib/format";
import { Stamp, Tag } from "../ui";
import type { Page } from "../App";
import { SignalCostLine } from "./Today";
import type { ChartModel, DayPoint } from "./ResultsChart";

const ResultsChart = lazy(() => import("./ResultsChart"));

/** Both books on one calendar: every day either book closed a trade, oldest
 *  first, each with that day's result and the running total. */
export function chartModel(s: Status): ChartModel {
  const B = { real: s.results.real.days, practice: s.results.practice.days };
  const days = [...new Set([...B.real, ...B.practice].map((d) => d.day))].sort();
  const run = (k: "real" | "practice"): DayPoint[] => {
    let cum: number | null = null;
    return days.map((day) => {
      const x = B[k].find((y) => y.day === day);
      if (x) cum = (cum ?? 0) + x.pnl;
      return { day, pnl: x ? x.pnl : null, closed: x ? x.closed : 0, cum: cum == null ? null : Math.round(cum * 100) / 100 };
    });
  };
  return { days, real: run("real"), practice: run("practice") };
}

function BookPanel({ k, B, cap, page }: { k: "real" | "practice" | "paper"; B: Book; cap: string; page: Page }) {
  const t = bookToday(B, etDayKey(page.s.as_of));
  // sales today, none of them measured: not a $0.00 day
  const unmeasuredOnly = !t.closed && t.unmeasured > 0;
  return (
    <section className={`panel book book-${k === "paper" ? "practice" : k} ${page.stale ? "stale" : ""}`}>
      <div className="ph">
        <h2>
          <Tag kind={k === "real" ? "real" : k === "paper" ? "paper" : "prac"} />
        </h2>
        <span className="meta">{cap}</span>
      </div>
      <div className="stale-c">
        <dl className="periods">
          <div>
            <dt>Today</dt>
            <dd className={unmeasuredOnly ? "" : tone(B.today)}>
              {unmeasuredOnly ? "not measured" : signed(B.today)}
              <small>{bookTodayNote(t)}</small>
            </dd>
          </div>
          <div>
            <dt>Last 7 days</dt>
            <dd className={tone(B.last_7_days)}>{signed(B.last_7_days)}</dd>
          </div>
          <div className="all">
            <dt>All time</dt>
            <dd className={tone(B.total)}>
              {signed(B.total)}
              <small>
                {plural(B.closed, "trade")} closed: {B.won} made money, {B.lost} lost
                {B.unmeasured > 0 ? `. ${plural(B.unmeasured, "sale")} could not be measured and ${B.unmeasured === 1 ? "is" : "are"} not counted` : ""}
              </small>
            </dd>
          </div>
        </dl>
      </div>
    </section>
  );
}

export function ResultsView({ page }: { page: Page }) {
  const s = page.s;
  const R = s.results.real;
  const P = s.results.practice;
  const A = s.results.paper_account;
  const hasPaper = A.closed > 0 || A.unmeasured > 0;
  const [allDays, setAllDays] = useState(false);
  // rebuild only when the numbers change, not on every poll
  const key = JSON.stringify([R.days, P.days]);
  const C = useMemo(() => chartModel(s), [key]); // eslint-disable-line react-hooks/exhaustive-deps
  const today = etDayKey(s.as_of);
  const head = (
    <div className="view-h">
      <div>
        <h1>Results</h1>
        <p className="lead">
          What closed trades made or lost. Figures are estimated from quoted prices at the time of each sale; your broker statement is the
          final word.
        </p>
      </div>
      {page.stale && <Stamp at={page.asOf} />}
    </div>
  );
  if (!C.days.length && !R.unmeasured && !P.unmeasured && !hasPaper)
    return (
      <div className="view">
        {head}
        <div className="empty">
          <h3>No closed trades yet</h3>
          <p>
            Results appear after the first trade is bought and then sold. Practice results and real-money results are always shown separately
            and never added together.
          </p>
        </div>
      </div>
    );
  const days = [...C.days].reverse();
  const showN = allDays ? days.length : Math.min(days.length, 10);
  const cell = (p: DayPoint) =>
    p.pnl == null ? (
      <td className="r muted">none</td>
    ) : (
      <td className={`r ${tone(p.pnl)}`}>
        <b>{signed(p.pnl)}</b>
        <small>{plural(p.closed, "trade")}</small>
      </td>
    );
  const c = s.results.signal_cost;
  const net = c.applies && c.usd_paid != null ? R.total - c.usd_paid : null;
  const label = `Line chart of the running total of closed trades by day. Real money, solid line, ends at ${signed(R.total)}. Practice, dashed line, ends at ${signed(P.total)}. Use the left and right arrow keys to read each day; the same numbers are in the table below.`;
  return (
    <div className="view">
      {head}
      <div className="books">
        <BookPanel k="real" B={R} cap={realBookCaption(s)} page={page} />
        <BookPanel k="practice" B={P} cap="Pretend trades. Never counted as money." page={page} />
        {hasPaper && <BookPanel k="paper" B={A} cap={PAPER_BOOK_CAPTION} page={page} />}
      </div>
      <section className={`panel ${page.stale ? "stale" : ""}`}>
        <div className="ph">
          <h2>Running total of closed trades</h2>
          <span className="legend">
            <span>
              <i className="key key-real" />
              Real money {signed(R.total, 0)}
            </span>
            <span>
              <i className="key key-practice" />
              Practice {signed(P.total, 0)}
            </span>
          </span>
        </div>
        {C.days.length > 0 && (
          <div className="pb stale-c">
            <Suspense fallback={<div className="chart-wrap chart-loading muted small">Drawing the chart…</div>}>
              <ResultsChart model={C} label={label} />
            </Suspense>
          </div>
        )}
        <div className="tbl-wrap stale-c">
          <table className="tbl tbl-days">
            <caption className="sr">Result by day, real money and practice in separate columns</caption>
            <thead>
              <tr>
                <th scope="col">Day</th>
                <th scope="col" className="r">
                  Real money
                </th>
                <th scope="col" className="r">
                  Practice
                </th>
              </tr>
            </thead>
            <tbody>
              {days.slice(0, showN).map((d) => {
                const i = C.days.indexOf(d);
                return (
                  <tr key={d}>
                    <th scope="row">
                      {d === today ? "Today, " : ""}
                      {dayLong(d)}
                    </th>
                    {cell(C.real[i])}
                    {cell(C.practice[i])}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
        {days.length > showN && (
          <div className="pfoot">
            <button className="btn btn-sm" onClick={() => setAllDays(true)}>
              Show all {days.length} days
            </button>
          </div>
        )}
      </section>
        <section className={`panel ${page.stale ? "stale" : ""}`}>
          <div className="ph">
            <h2>What it has cost you</h2>
            <Tag kind="real" />
          </div>
          <div className="pb stale-c">
            <ul className="money">
              <li>
                <span>
                  Real-money trades
                  <small>{plural(R.closed, "closed trade")}, all time</small>
                </span>
                <b className={tone(R.total)}>{signed(R.total)}</b>
              </li>
              {c.applies ? (
                <SignalCostLine s={s} />
              ) : (
                <li>
                  <span>
                    Signals
                    <small>This signal source has no per-day charge.</small>
                  </span>
                  <b className="soft">none</b>
                </li>
              )}
              {net != null && (
                <li className="total">
                  <span>
                    <strong>After signal cost</strong>
                  </span>
                  <b className={tone(net)}>{signed(net)}</b>
                </li>
              )}
            </ul>
            <p className="small muted" style={{ marginTop: 8 }}>
              {c.applies && c.usd_paid == null
                ? "The dollars paid for signal days are not tracked yet, so there is no after-cost total here. "
                : ""}
              Broker fees are not included; {s.broker.name || "your broker"} reports those.
            </p>
          </div>
        </section>
    </div>
  );
}
