// The Results chart: each book's running total of closed trades over the
// chosen period, one point per market day (ET). Plain SVG drawn from the
// numbers in lib/chart.ts; nothing is loaded at run time.
//
// Real money = solid blue line with round marks, practice = dashed violet
// line with diamonds: told apart by line style and shape as well as colour.
// Each day's mark is filled green for a winning day and red for a losing one
// (the edge keeps the book colour). The end of each line carries its book's
// name and signed total, green or red. Left/right arrow keys (and Home/End)
// read each day; the by-day table under the chart has the same numbers.
import { useLayoutEffect, useRef, useState } from "react";
import { type BookKey, type ChartModel, type DayPoint, axisMoney, niceTicks } from "../lib/chart";
import { dayLong, dayShort, signed, tone } from "../lib/format";

const NAME: Record<BookKey, [string, string]> = { real: ["Real money", "Real"], practice: ["Practice", "Practice"] };
// marks are drawn while the days are far enough apart to read them
const MAX_MARKED_DAYS = 66;

/** Rough text width in px (the axis and end labels are short and tabular). */
const textW = (s: string, px: number) => Math.ceil(s.length * px * 0.6);

function Mark({ k, x, y, cls }: { k: BookKey; x: number; y: number; cls: string }) {
  return k === "real" ? (
    <circle className={`mk mk-real ${cls}`} cx={x} cy={y} r={4.5} />
  ) : (
    <rect className={`mk mk-practice ${cls}`} x={x - 4} y={y - 4} width={8} height={8} transform={`rotate(45 ${x} ${y})`} />
  );
}

function TipLine({ k, p }: { k: BookKey; p: DayPoint }) {
  const name = NAME[k][0];
  if (p.cum == null) return <span>{name}: no closed trades yet</span>;
  return (
    <span>
      {name}:{" "}
      {p.pnl == null ? (
        "nothing closed"
      ) : (
        <>
          <b className={tone(p.pnl)}>{signed(p.pnl)}</b> that day
        </>
      )}
      , running total <b className={tone(p.cum)}>{signed(p.cum)}</b>
    </span>
  );
}

export default function ResultsChart({ model, label, emptyText }: { model: ChartModel; label: string; emptyText?: string }) {
  const wrap = useRef<HTMLDivElement>(null);
  const tip = useRef<HTMLDivElement>(null);
  const [W, setW] = useState(0);
  const [cur, setCur] = useState(-1);

  useLayoutEffect(() => {
    const el = wrap.current;
    if (!el) return;
    const read = () => setW(Math.max(260, Math.floor(el.clientWidth)));
    read();
    const ro = new ResizeObserver(read);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const N = model.days.length;
  const i = cur >= 0 && cur < N ? cur : -1;
  const small = W > 0 && W < 560;
  const H = small ? 220 : 260;

  // the money axis: every value on the chart, and always $0
  const vals = [...model.real, ...model.practice].map((p) => p.cum).filter((v): v is number => v != null);
  const T = niceTicks(Math.min(0, ...vals), Math.max(0, ...vals));
  const lo = T[0];
  const hi = T[T.length - 1];

  // end labels sit in a gutter right of the plot
  const lines = (["real", "practice"] as BookKey[]).filter((k) => model.end[k] != null);
  const name = (k: BookKey) => NAME[k][small ? 1 : 0];
  const gutter = lines.length
    ? Math.min(150, Math.max(56, ...lines.map((k) => Math.max(textW(name(k), 13) + 16, textW(signed(model.end[k]), 14)))) + 14)
    : 12;
  const m = { l: Math.max(...T.map((t) => textW(axisMoney(t), 13))) + 14, t: 16, r: gutter, b: 30 };
  const plotW = Math.max(40, W - m.l - m.r);
  const bw = plotW / Math.max(1, N);
  const x = (j: number) => m.l + bw * (j + 0.5);
  const y = (v: number) => m.t + ((hi - v) / (hi - lo)) * (H - m.t - m.b);

  const step = Math.max(1, Math.ceil(N / Math.max(1, Math.floor(plotW / 64))));
  const labelled: number[] = [];
  for (let j = N - 1; j >= 0; j -= step) labelled.push(j);

  const marked = N <= MAX_MARKED_DAYS;
  const path = (k: BookKey) => {
    const pts = model[k].map((p, j) => (p.cum == null ? null : `${x(j).toFixed(1)},${y(p.cum).toFixed(1)}`)).filter(Boolean);
    return pts.length > 1 ? <path className={`ln ln-${k}`} d={`M${pts.join("L")}`} /> : null;
  };
  const marks = (k: BookKey) =>
    model[k].map((p, j) => {
      if (p.cum == null) return null;
      const last = j === N - 1 || model[k][j + 1]?.cum == null;
      if (marked && p.pnl != null) return <Mark key={j} k={k} x={x(j)} y={y(p.cum)} cls={tone(p.pnl) || "flat"} />;
      // without daily marks, the line still ends on its book's mark
      if (!marked && last) return <Mark key={j} k={k} x={x(j)} y={y(p.cum)} cls="book" />;
      return null;
    });

  // direct labels at the end of each line, pushed apart when they would touch
  const ends = lines
    .map((k) => ({ k, v: model.end[k] as number, y: y(model.end[k] as number) }))
    .sort((a, b) => a.y - b.y);
  if (ends.length === 2 && ends[1].y - ends[0].y < 36) {
    const mid = (ends[0].y + ends[1].y) / 2;
    ends[0].y = mid - 18;
    ends[1].y = mid + 18;
  }
  for (const e of ends) e.y = Math.min(H - m.b - 8, Math.max(m.t + 10, e.y));

  useLayoutEffect(() => {
    const t = tip.current;
    if (!t || i < 0) return;
    const left = Math.max(0, Math.min(x(i) - t.offsetWidth / 2, W - t.offsetWidth));
    t.style.left = `${left}px`;
  });

  const move = (j: number) => setCur(Math.max(0, Math.min(N - 1, j)));
  return (
    <div
      className="chart-wrap"
      ref={wrap}
      tabIndex={0}
      role="group"
      aria-roledescription="chart"
      aria-label={label}
      onBlur={() => setCur(-1)}
      onMouseLeave={() => setCur(-1)}
      onKeyDown={(e) => {
        const go: Record<string, number> = { ArrowRight: i < 0 ? N - 1 : i + 1, ArrowLeft: i < 0 ? N - 1 : i - 1, Home: 0, End: N - 1 };
        if (e.key in go) {
          e.preventDefault();
          move(go[e.key]);
        } else if (e.key === "Escape") setCur(-1);
      }}
    >
      {W > 0 ? (
        <svg className="chart" viewBox={`0 0 ${W} ${H}`} width={W} height={H} aria-hidden="true">
          {T.map((t) => (
            <g key={t}>
              <line className={t === 0 ? "zero" : "grid"} x1={m.l} x2={W - m.r} y1={y(t)} y2={y(t)} />
              <text x={m.l - 8} y={y(t) + 4} textAnchor="end">
                {axisMoney(t)}
              </text>
            </g>
          ))}
          {labelled.map((j) => (
            <text key={j} x={x(j)} y={H - 8} textAnchor={j === N - 1 && N > 1 ? "end" : "middle"} dx={j === N - 1 && N > 1 ? bw / 2 : 0}>
              {dayShort(model.days[j])}
            </text>
          ))}
          {i >= 0 && <line className="cross" x1={x(i)} x2={x(i)} y1={m.t} y2={H - m.b} />}
          {path("practice")}
          {path("real")}
          {marks("practice")}
          {marks("real")}
          {i >= 0 &&
            (["practice", "real"] as BookKey[]).map((k) => {
              const p = model[k][i];
              return p.cum == null ? null : <Mark key={k} k={k} x={x(i)} y={y(p.cum)} cls="hot" />;
            })}
          {ends.map((e) => (
            <g key={e.k} className="endlab">
              <Mark k={e.k} x={W - m.r + 12} y={e.y - 6} cls="book" />
              <text className="end" x={W - m.r + 22} y={e.y - 2}>
                {name(e.k)}
              </text>
              <text className={`endv ${tone(e.v)}`} x={W - m.r + 8} y={e.y + 15}>
                {signed(e.v)}
              </text>
            </g>
          ))}
          {model.days.map((d, j) => (
            <rect key={d} className="hit" x={m.l + bw * j} y={m.t} width={bw} height={H - m.t - m.b} onMouseEnter={() => setCur(j)} onClick={() => setCur(j)} />
          ))}
        </svg>
      ) : (
        <div style={{ height: H }} />
      )}
      {emptyText && (
        <p className="chart-empty" style={{ top: m.t, bottom: m.b, left: m.l, right: m.r }}>
          <span>{emptyText}</span>
        </p>
      )}
      <div className="tip" ref={tip} role="status" aria-live="polite" style={{ display: i >= 0 ? "block" : "none" }}>
        {i >= 0 && (
          <>
            <b>{dayLong(model.days[i])}</b>
            <TipLine k="real" p={model.real[i]} />
            <TipLine k="practice" p={model.practice[i]} />
          </>
        )}
      </div>
    </div>
  );
}
