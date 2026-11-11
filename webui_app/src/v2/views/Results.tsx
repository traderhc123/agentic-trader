// Results: real money and practice are two books that never share a number.
// The chart comes first: each book's running total over 7 days, 30 days or
// all time (lib/chart.ts), then the books' panels, the by-day table and the
// cost ledger.
import { useMemo, useState } from "react";
import type { Book, Status } from "../../lib/viewModel";
import { type ChartModel, type Range, RANGES, chartModel, isRange } from "../lib/chart";
import { PAPER_BOOK_CAPTION, bookToday, bookTodayNote, paidMark, realBookCaption } from "../lib/derive";
import { dayLong, etDayKey, plural, signed, tone } from "../lib/format";
import { Stamp, Tag } from "../ui";
import type { Page } from "../App";
import { SignalCostLine } from "./Today";
import ResultsChart from "./ResultsChart";

interface DayRow {
  day: string;
  real: { pnl: number; closed: number } | null;
  practice: { pnl: number; closed: number } | null;
}

/** Every day either book closed a trade, newest first, each book's result
 *  in its own column. */
export function dayRows(s: Status): DayRow[] {
  const by = (B: Book) => new Map(B.days.map((d) => [d.day, d]));
  const R = by(s.results.real);
  const P = by(s.results.practice);
  return [...new Set([...R.keys(), ...P.keys()])]
    .sort()
    .reverse()
    .map((day) => ({ day, real: R.get(day) ?? null, practice: P.get(day) ?? null }));
}

const RANGE_KEY = "at-results-range";
function useRange(): [Range, (r: Range) => void] {
  const [range, set] = useState<Range>(() => {
    try {
      const v = localStorage.getItem(RANGE_KEY);
      return isRange(v) ? v : "all";
    } catch {
      return "all";
    }
  });
  const pick = (r: Range) => {
    set(r);
    try {
      localStorage.setItem(RANGE_KEY, r);
    } catch {
      /* private mode: the choice lasts for this visit */
    }
  };
  return [range, pick];
}

function ChartPanel({ page, C, range, setRange }: { page: Page; C: ChartModel; range: Range; setRange: (r: Range) => void }) {
  const s = page.s;
  const today = etDayKey(s.as_of);
  const unmeasured = s.results.real.unmeasured + s.results.practice.unmeasured;
  const first = C.days[0];
  const last = C.days[C.days.length - 1];
  const period = `${dayLong(first)} to ${last === today ? "today" : dayLong(last)}`;
  const end = (k: "real" | "practice", words: string) =>
    C.end[k] == null ? `${words}: no closed trades.` : `${words}, ${signed(C.end[k])}.`;
  const label = C.empty
    ? "Chart of the running total of closed trades: empty, there are no closed trades yet."
    : `Line chart of the running total of closed trades by market day, ${period}. ${end("real", "Real money, solid line")} ${end("practice", "Practice, dashed line")} Use the left and right arrow keys to read each day; the table of results by day has the same numbers.`;
  const emptyText = !C.empty
    ? undefined
    : unmeasured > 0
      ? "Nothing to plot yet: no sale so far could be measured."
      : "No closed trades yet. Results appear here after the first sale.";
  const undated = (["real", "practice"] as const).filter((k) => C.undated[k] !== 0);
  return (
    <section className={`panel ${page.stale ? "stale" : ""}`}>
      <div className="ph">
        <h2>Running total of closed trades</h2>
        {!C.empty && (
          <div className="chips" role="group" aria-label="Period">
            {RANGES.map(([r, words]) => (
              <button key={r} className="chip" aria-pressed={r === range} onClick={() => setRange(r)}>
                {words}
              </button>
            ))}
          </div>
        )}
      </div>
      <div className="pb stale-c">
        {!C.empty && (
          <p className="chart-cap">
            {period}, market days (ET). Each point is the total from the first day shown to that day.
            {C.quiet ? ` Nothing closed in ${range === "7d" ? "these 7 days" : "these 30 days"}.` : ""}
          </p>
        )}
        <ResultsChart model={C} label={label} emptyText={emptyText} />
        {undated.map((k) => (
          <p key={k} className="chart-cap">
            Not on the chart: <b className={tone(C.undated[k])}>{signed(C.undated[k])}</b> of {k === "real" ? "real-money" : "practice"} results
            from sales whose date could not be read. The All time total includes them.
          </p>
        ))}
      </div>
    </section>
  );
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
  const [range, setRange] = useRange();
  const today = etDayKey(s.as_of);
  // rebuild only when the numbers change, not on every poll
  const key = JSON.stringify([R.days, P.days, R.total, P.total, range, today]);
  const C = useMemo(() => chartModel(s, range, today), [key]); // eslint-disable-line react-hooks/exhaustive-deps
  const days = useMemo(() => dayRows(s), [key]); // eslint-disable-line react-hooks/exhaustive-deps
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
  const chart = <ChartPanel page={page} C={C} range={range} setRange={setRange} />;
  // nothing closed in any book yet: the chart's own sentence says so
  if (!days.length && !R.unmeasured && !P.unmeasured && !hasPaper)
    return (
      <div className="view">
        {head}
        {chart}
      </div>
    );
  const showN = allDays ? days.length : Math.min(days.length, 10);
  const cell = (x: DayRow["real"]) =>
    x == null ? (
      <td className="r muted">none</td>
    ) : (
      <td className={`r ${tone(x.pnl)}`}>
        <b>{signed(x.pnl)}</b>
        <small>{plural(x.closed, "trade")}</small>
      </td>
    );
  const c = s.results.signal_cost;
  const net = c.applies && c.usd_paid != null ? R.total - c.usd_paid : null;
  const pd = s.results.paid_days;
  return (
    <div className="view">
      {head}
      {chart}
      <div className="books">
        <BookPanel k="real" B={R} cap={realBookCaption(s)} page={page} />
        <BookPanel k="practice" B={P} cap="Pretend trades. Never counted as money." page={page} />
        {hasPaper && <BookPanel k="paper" B={A} cap={PAPER_BOOK_CAPTION} page={page} />}
      </div>
      {days.length > 0 && (
        <section className={`panel ${page.stale ? "stale" : ""}`}>
          <div className="ph">
            <h2>Result by day</h2>
            <span className="meta">Each trade counts on the day it closed (ET)</span>
          </div>
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
                {days.slice(0, showN).map((d) => (
                  <tr key={d.day}>
                    <th scope="row">
                      {d.day === today ? "Today, " : ""}
                      {dayLong(d.day)}
                      {paidMark(s, d.day) && <small className="paid-mark">{paidMark(s, d.day)}</small>}
                    </th>
                    {cell(d.real)}
                    {cell(d.practice)}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {days.slice(0, showN).some((d) => paidMark(s, d.day)) && (
            <p className="pb small muted" data-paid="days-note">
              “Paid signal day” marks a day the signals were paid for. Result on paid signal days, below, counts the trades opened on those days,
              so it need not add up from the rows marked here.
            </p>
          )}
          {days.length > showN && (
            <div className="pfoot">
              <button className="btn btn-sm" onClick={() => setAllDays(true)}>
                Show all {days.length} days
              </button>
            </div>
          )}
        </section>
      )}
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
            {pd.real && (
              <li className="sep" data-paid="real">
                <span>
                  Result on paid signal days
                  <small>
                    {plural(pd.real.closed, "real-money trade")} opened on {plural(pd.real.days, "paid signal day")}, counted on the day each was
                    opened.
                  </small>
                </span>
                <b className={tone(pd.real.pnl)}>{signed(pd.real.pnl)}</b>
              </li>
            )}
            {pd.practice && pd.practice.closed > 0 && (
              <li data-paid="practice">
                <span>
                  <Tag kind="prac" />
                  <small>
                    {plural(pd.practice.closed, "practice trade")} opened on {plural(pd.practice.days, "paid signal day")}. Never counted as money.
                  </small>
                </span>
                <b className={`${tone(pd.practice.pnl)} soft`}>{signed(pd.practice.pnl)}</b>
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
