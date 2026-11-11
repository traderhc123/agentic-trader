// Holdings and More (phone), and the settings row the pages share. Rules &
// limits, Signals & pass and Settings each have their own file: each is the
// one home of the controls it owns (lib/screens.ts).
import type { ReactNode } from "react";
import { plural } from "../lib/format";
import { Icon, Stamp, type IconId } from "../ui";
import type { Page, View } from "../App";
import { HoldGroups, Notes } from "./Today";
import { notesOn } from "../lib/screens";

export function Row({ label, exp, val, act, valClass = "sm" }: { label: ReactNode; exp: ReactNode; val?: ReactNode; act?: ReactNode; valClass?: string }) {
  return (
    <div className="setrow">
      <span className="lbl">{label}</span>
      <span className="exp">{exp}</span>
      <span className={`val ${valClass}`}>{val}</span>
      <span className="act">{act}</span>
    </div>
  );
}

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
        <Notes page={page} list={notesOn("holdings", page.s, page.link)} />
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
      <Notes page={page} list={notesOn("holdings", page.s, page.link)} />
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

// ── More (phone) ────────────────────────────────────────────────────────────

export function MoreView({ page }: { page: Page }) {
  const s = page.s;
  const days = s.signals.pass.days_left;
  const items: [View, IconId, string, string][] = [
    ["rules", "rules", "Rules & limits", "Daily limits, trade size, which signals to take"],
    ["signals", "signal", "Signals & pass", days != null ? `${plural(days, "signal day")} left` : "Signal access and when it last listened"],
    ["settings", "gear", "Settings", s.broker.connected === false ? `1 problem: ${s.broker.name || "the broker"} is not connected` : "Connections, this computer, this dashboard"],
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
    </div>
  );
}
