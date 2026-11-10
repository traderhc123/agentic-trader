// The top of every screen: the mode band (slot 1 = mode, from `mode` only;
// slot 2 = run-state, from `pause` only; then the actions) and the vitals
// strip. Pausing never recolours the mode.
import { useEffect, useRef, useState } from "react";
import type { Status } from "../lib/viewModel";
import { bandModel, marketVital, vitals, type Link, type Vital } from "./lib/derive";
import { addSeconds, ago } from "./lib/format";
import { Icon } from "./ui";

export type BandAction = "pause" | "resume" | "to-practice" | "to-live" | "retry";

/** Seconds since the snapshot arrived, ticking once a second. Only the
 *  small counters use it, so the rest of the page re-renders on polls. */
function useElapsed(stamp: string): number {
  const [t, setT] = useState(0);
  useEffect(() => {
    const t0 = Date.now();
    setT(0);
    const id = setInterval(() => setT(Math.floor((Date.now() - t0) / 1000)), 1000);
    return () => clearInterval(id);
  }, [stamp]);
  return t;
}

function VitalItem({ v, status, frozen }: { v: Vital; status: Status; frozen: boolean }) {
  const elapsed = useElapsed(status.as_of);
  const live = frozen ? 0 : elapsed;
  const m = v.key === "market" && !frozen ? marketVital(status, addSeconds(status.as_of, live)) : v;
  return (
    <span className={`vital ${m.look ? `vital-${m.look}` : ""} ${m.desk ? "vital-desk" : ""} ${m.key === "market" ? "vital-right" : ""}`}>
      {m.icon === "sq-real" ? (
        <span className="sq sq-real" />
      ) : m.icon === "sq-none" ? (
        <span className="sq sq-none" />
      ) : m.icon === "beat" ? (
        <span className="beat" />
      ) : (
        <Icon name={m.icon} />
      )}
      <span>
        {m.key === "broker" && m.look === "warn" ? (
          <>
            {m.text}
            <b>{m.bold}</b>
          </>
        ) : (
          <>
            {m.bold && <b>{m.bold}</b>}
            {m.text}
          </>
        )}
        {m.tail && (
          <>
            {" "}
            <b>{m.tail}</b>
          </>
        )}
        {m.sinceCheck !== undefined && (
          <>
            {/* the ticking counter is not announced: the band's status line is */}
            <span aria-hidden="true">{m.sinceCheck == null ? "a moment" : ago(m.sinceCheck + live)}</span>
            <span className="sr">a moment</span> ago
          </>
        )}
        {m.wide && <span className="wide">{m.wide}</span>}
      </span>
    </span>
  );
}

export function Band({
  status,
  link,
  busy,
  onAction,
}: {
  status: Status;
  link: Link;
  busy: boolean;
  onAction: (a: BandAction) => void;
}) {
  const m = bandModel(status, link);
  const ref = useRef<HTMLElement>(null);
  const [said, setSaid] = useState("");

  // the window wears the mode: frame, title, and one announcement per change
  useEffect(() => {
    document.documentElement.dataset.mode = m.frame;
    document.title = m.title;
  }, [m.frame, m.title]);
  useEffect(() => setSaid(m.announce), [m.announce]);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const set = () => document.documentElement.style.setProperty("--band-h", el.offsetHeight + "px");
    set();
    if (typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(set);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const a = m.actions;
  return (
    <header className="band" ref={ref} aria-label="Trading mode and agent status">
      <div className="sr" role="status">
        {said}
      </div>
      <div className="band-mode">
        <div className="mode-id">
          <span className="mode-glyph" />
          <span className="mode-word">{m.word}</span>
        </div>
        <p className="mode-say">
          {m.say[0] && <b>{m.say[0]}</b>}
          {m.say[1]}
        </p>
        <span className={`run run-${m.run.kind}`}>
          {m.run.kind === "ok" ? <span className="pulse" /> : m.run.icon && <Icon name={m.run.icon} />}
          <span>
            {m.run.text}
            {m.run.long && <span className="run-long">{m.run.long}</span>}
          </span>
        </span>
        <div className="band-actions">
          {a.retry && (
            <button className="btn" data-act="retry" onClick={() => onAction("retry")} disabled={busy}>
              Try again
            </button>
          )}
          {a.start && (
            <a className="btn btn-primary" href="/setup">
              Open setup to start it
            </a>
          )}
          {a.pause && (
            <button className="btn" data-act="pause" onClick={() => onAction("pause")} disabled={busy}>
              <Icon name="pause" small />
              Pause…
            </button>
          )}
          {a.resume && (
            <button className="btn btn-primary" data-act="resume" onClick={() => onAction("resume")} disabled={busy}>
              <Icon name="play" small />
              Resume
            </button>
          )}
          {a.toPractice && (
            <button className="btn btn-ghost" data-act="to-practice" onClick={() => onAction("to-practice")} disabled={busy}>
              <span>
                Switch<span className="desk"> to practice</span>
                <span className="phone-only"> mode</span>…
              </span>
            </button>
          )}
          {a.toLive && (
            <button className="btn btn-ghost" data-act="to-live" onClick={() => onAction("to-live")} disabled={busy}>
              <span>
                Switch<span className="desk"> to LIVE</span>
                <span className="phone-only"> mode</span>…
              </span>
            </button>
          )}
        </div>
      </div>
      <div className="vitals">
        {vitals(status, link).map((v) => (
          <VitalItem key={v.key} v={v} status={status} frozen={link === "lost" || link === "silent"} />
        ))}
      </div>
    </header>
  );
}
