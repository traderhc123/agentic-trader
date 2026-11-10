// Running totals of closed trades, one line per book (TradingView
// Lightweight Charts, bundled; nothing is loaded at run time).
// Real money = solid orange with round marks, practice = dashed periwinkle
// with square marks: told apart by shape as well as colour. Left/right arrow
// keys read each day; the same numbers are in the table under the chart.
import { ColorType, CrosshairMode, LineStyle, createChart, type IChartApi, type ISeriesApi, type Time } from "lightweight-charts";
import { useEffect, useRef, useState } from "react";
import { dayLong, signed } from "../lib/format";

export interface DayPoint {
  day: string;
  /** that day's result, null when nothing closed in this book that day */
  pnl: number | null;
  closed: number;
  /** running total up to and including the day; null before the book's first trade */
  cum: number | null;
}
export interface ChartModel {
  days: string[];
  real: DayPoint[];
  practice: DayPoint[];
}

const css = (n: string) => getComputedStyle(document.documentElement).getPropertyValue(`--${n}`).trim();

function safeLocale(): string {
  for (const l of [navigator.language, "en-US"]) {
    try {
      new Intl.DateTimeFormat(l);
      return l;
    } catch {
      /* some systems report a tag Intl rejects: try the next one */
    }
  }
  return "en-US";
}

const axis = (v: number) => {
  const a = Math.abs(v);
  return (v < 0 ? "−" : "") + "$" + (a >= 10000 ? (a / 1000).toLocaleString("en-US", { maximumFractionDigits: 1 }) + "k" : a.toLocaleString("en-US", { maximumFractionDigits: 0 }));
};

function line(label: string, p: DayPoint) {
  if (p.cum == null) return `${label}: no trades yet`;
  if (p.pnl == null) return `${label}: nothing closed, total ${signed(p.cum)}`;
  return `${label}: ${signed(p.pnl)} that day, total ${signed(p.cum)}`;
}

export default function ResultsChart({ model, label }: { model: ChartModel; label: string }) {
  const wrap = useRef<HTMLDivElement>(null);
  const el = useRef<HTMLDivElement>(null);
  const chart = useRef<IChartApi | null>(null);
  const series = useRef<{ real: ISeriesApi<"Line">; practice: ISeriesApi<"Line"> } | null>(null);
  const [cur, setCur] = useState(-1);
  const [left, setLeft] = useState(0);
  const tip = useRef<HTMLDivElement>(null);
  const modelRef = useRef(model);
  const repaint = useRef<() => void>(() => {});
  modelRef.current = model;

  useEffect(() => {
    if (!el.current) return;
    const c = createChart(el.current, {
      autoSize: true,
      handleScroll: false,
      handleScale: false,
      localization: { locale: safeLocale(), priceFormatter: axis },
      crosshair: { mode: CrosshairMode.Magnet, horzLine: { visible: false, labelVisible: false } },
      rightPriceScale: { scaleMargins: { top: 0.14, bottom: 0.12 }, borderVisible: false },
      timeScale: { fixLeftEdge: true, fixRightEdge: true, borderVisible: false },
    });
    const practice = c.addLineSeries({ lineStyle: LineStyle.Dashed, lineWidth: 2, title: "Practice", priceLineVisible: false, crosshairMarkerRadius: 4 });
    const real = c.addLineSeries({ lineStyle: LineStyle.Solid, lineWidth: 3, title: "Real money", priceLineVisible: false, crosshairMarkerRadius: 4 });
    real.createPriceLine({ price: 0, color: css("line2"), lineWidth: 1, lineStyle: LineStyle.Solid, axisLabelVisible: false, title: "" });
    chart.current = c;
    series.current = { real, practice };

    const paint = () => {
      const font = getComputedStyle(document.body).fontFamily;
      c.applyOptions({
        layout: { background: { type: ColorType.Solid, color: "transparent" }, textColor: css("ink2"), fontFamily: font, fontSize: 13 },
        grid: { vertLines: { visible: false }, horzLines: { color: css("line") } },
        crosshair: { vertLine: { color: css("line2"), labelBackgroundColor: css("ink") } },
      });
      // on a narrow chart the end labels would cover the lines: the legend
      // above the chart carries the same two totals
      const small = (el.current?.clientWidth ?? 0) < 560;
      real.applyOptions({ color: css("real"), title: small ? "" : "Real money" });
      practice.applyOptions({ color: css("prac"), title: small ? "" : "Practice" });
      setMarks();
    };
    const setMarks = () => {
      const m = modelRef.current;
      if (m.days.length > 31) {
        real.setMarkers([]);
        practice.setMarkers([]);
        return;
      }
      real.setMarkers(m.real.filter((p) => p.pnl != null).map((p) => ({ time: p.day as Time, position: "inBar" as const, shape: "circle" as const, color: css("real"), size: 0.8 })));
      practice.setMarkers(m.practice.filter((p) => p.pnl != null).map((p) => ({ time: p.day as Time, position: "inBar" as const, shape: "square" as const, color: css("prac"), size: 0.8 })));
    };
    repaint.current = paint;
    paint();
    // follow the theme: the OS setting and the page's own switch
    const mq = matchMedia("(prefers-color-scheme: dark)");
    mq.addEventListener("change", paint);
    const mo = new MutationObserver(paint);
    mo.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    addEventListener("resize", paint);
    c.subscribeCrosshairMove((p) => {
      const i = typeof p.time === "string" ? modelRef.current.days.indexOf(p.time) : -1;
      setCur(i);
      if (i >= 0 && p.point) setLeft(p.point.x);
    });
    return () => {
      mq.removeEventListener("change", paint);
      mo.disconnect();
      removeEventListener("resize", paint);
      c.remove();
      chart.current = null;
      series.current = null;
    };
  }, []);

  useEffect(() => {
    const s = series.current;
    const c = chart.current;
    if (!s || !c) return;
    const pts = (k: "real" | "practice") => model[k].filter((p) => p.cum != null).map((p) => ({ time: p.day as Time, value: p.cum as number }));
    s.real.setData(pts("real"));
    s.practice.setData(pts("practice"));
    repaint.current();
    c.timeScale().fitContent();
  }, [model]);

  const show = (i: number) => {
    const c = chart.current;
    const s = series.current;
    if (!c || !s || i < 0 || i >= model.days.length) return;
    const day = model.days[i];
    const r = model.real[i];
    const p = model.practice[i];
    const on = r.cum != null ? s.real : s.practice;
    const v = r.cum ?? p.cum ?? 0;
    c.setCrosshairPosition(v, day as Time, on);
    const x = c.timeScale().timeToCoordinate(day as Time);
    if (x != null) setLeft(x);
    setCur(i);
  };
  const hide = () => {
    chart.current?.clearCrosshairPosition();
    setCur(-1);
  };

  const N = model.days.length;
  const w = wrap.current?.clientWidth ?? 0;
  const tw = tip.current?.offsetWidth ?? 0;
  const tipLeft = Math.max(0, Math.min(left - tw / 2, Math.max(0, w - tw)));
  return (
    <div
      className="chart-wrap"
      ref={wrap}
      tabIndex={0}
      role="img"
      aria-label={label}
      onBlur={hide}
      onMouseLeave={hide}
      onKeyDown={(e) => {
        if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
          e.preventDefault();
          show(Math.max(0, Math.min(N - 1, cur < 0 ? N - 1 : cur + (e.key === "ArrowRight" ? 1 : -1))));
        }
      }}
    >
      <div ref={el} className="chart-canvas" />
      <div className="tip" ref={tip} role="status" style={{ display: cur >= 0 ? "block" : "none", left: tipLeft }}>
        {cur >= 0 && (
          <>
            <b>{dayLong(model.days[cur])}</b>
            <span>{line("Real money", model.real[cur])}</span>
            <span>{line("Practice", model.practice[cur])}</span>
          </>
        )}
      </div>
    </div>
  );
}
