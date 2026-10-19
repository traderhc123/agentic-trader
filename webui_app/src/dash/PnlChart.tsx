import {
  ColorType,
  CrosshairMode,
  LineStyle,
  createChart,
  type IChartApi,
  type ISeriesApi,
  type UTCTimestamp,
} from "lightweight-charts";
import { useEffect, useRef } from "react";

export interface PnlPoint {
  ts: string; // ISO time of the exit
  cum: number; // cumulative est. P&L after it
}

const css = (n: string, a = 1) => {
  const v = getComputedStyle(document.documentElement).getPropertyValue(`--${n}`).trim();
  return `rgba(${v.split(/\s+/).join(",")},${a})`;
};

/** The library formats with navigator.language; some systems report tags
 *  Intl rejects (e.g. "en-US@posix"), which would crash the chart. */
function safeLocale(): string {
  for (const l of [navigator.language, "en-US"]) {
    try {
      new Intl.DateTimeFormat(l);
      return l;
    } catch {
      /* try the next one */
    }
  }
  return "en-US";
}

/** Cumulative est. P&L of exits as a baseline chart around $0. Times are
 *  shifted to the viewer's local clock (the library renders UTC). */
export function PnlChart({ points, height = 220 }: { points: PnlPoint[]; height?: number }) {
  const el = useRef<HTMLDivElement>(null);
  const chart = useRef<IChartApi | null>(null);
  const series = useRef<ISeriesApi<"Baseline"> | null>(null);

  useEffect(() => {
    if (!el.current) return;
    const c = createChart(el.current, {
      autoSize: true,
      layout: {
        background: { type: ColorType.Solid, color: "transparent" },
        textColor: css("muted"),
        fontFamily: '"IBM Plex Mono", ui-monospace, monospace',
        fontSize: 11,
      },
      grid: { vertLines: { color: css("line", 0.6) }, horzLines: { color: css("line", 0.6) } },
      rightPriceScale: { borderColor: css("line"), scaleMargins: { top: 0.12, bottom: 0.08 } },
      timeScale: { borderColor: css("line"), timeVisible: true, secondsVisible: false, fixLeftEdge: true, fixRightEdge: true },
      crosshair: {
        mode: CrosshairMode.Magnet,
        vertLine: { color: css("line-3"), labelBackgroundColor: css("raise") },
        horzLine: { color: css("line-3"), labelBackgroundColor: css("raise") },
      },
      handleScroll: false,
      handleScale: false,
      localization: { locale: safeLocale() },
    });
    const s = c.addBaselineSeries({
      baseValue: { type: "price", price: 0 },
      topLineColor: css("up"),
      topFillColor1: css("up", 0.22),
      topFillColor2: css("up", 0.02),
      bottomLineColor: css("down"),
      bottomFillColor1: css("down", 0.02),
      bottomFillColor2: css("down", 0.22),
      lineWidth: 2,
      priceFormat: { type: "custom", formatter: (p: number) => (p < 0 ? "−$" : "$") + Math.abs(p).toFixed(2), minMove: 0.01 },
      lastValueVisible: true,
      priceLineVisible: false,
    });
    s.createPriceLine({ price: 0, color: css("line-3"), lineStyle: LineStyle.Dashed, lineWidth: 1, axisLabelVisible: false, title: "" });
    chart.current = c;
    series.current = s;
    return () => {
      c.remove();
      chart.current = null;
      series.current = null;
    };
  }, []);

  useEffect(() => {
    if (!series.current || !chart.current) return;
    const off = new Date().getTimezoneOffset() * 60;
    let last = -Infinity;
    const data = points.map((p) => {
      let t = Math.floor(new Date(p.ts).getTime() / 1000) - off;
      if (!isFinite(t)) t = last + 60;
      if (t <= last) t = last + 1; // the library needs strictly increasing times
      last = t;
      return { time: t as UTCTimestamp, value: Math.round(p.cum * 100) / 100 };
    });
    // anchor the curve at $0 just before the first exit
    if (data.length) data.unshift({ time: (data[0].time - 60) as UTCTimestamp, value: 0 });
    series.current.setData(data);
    chart.current.timeScale().fitContent();
  }, [points]);

  return <div ref={el} style={{ height }} className="w-full" aria-hidden="true" />;
}
