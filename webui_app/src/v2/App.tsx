// The /v2 dashboard shell: band, sidebar (rail at ~1100 px, bottom tabs on
// phones), the current view, the drawer, the dialog and the toast.
import { useCallback, useEffect, useRef, useState } from "react";
import type { Activity, Iso, Position, Status } from "../lib/viewModel";
import { Band, type BandAction } from "./Band";
import { ActionDialog, UpdateDialog, type DialogKind } from "./Dialogs";
import { UPDATE, describe, expiryWill } from "./lib/copy";
import { asOf, brokerLabel, isStale, linkOf, nextFor, type Link } from "./lib/derive";
import { badgeFor, updateLine } from "./lib/update";
import { useUpdate, type UpdateFeed } from "./useUpdate";
import { UNKNOWN, contractName, count, dayLong, etDayKey, etTime, etWhen, usd } from "./lib/format";
import { Drawer, ExpiresFlag, Icon, Tag, WillWont, type IconId } from "./ui";
import { useStatus, type StatusFeed } from "./useStatus";
import { ActivityView, EventDrawer } from "./views/Activity";
import { HoldingsView, MoreView, RulesView, SettingsView, SignalsView } from "./views/Pages";
import { ResultsView } from "./views/Results";
import { Today } from "./views/Today";

export type View = "today" | "holdings" | "activity" | "results" | "rules" | "signals" | "settings" | "more";
const VIEWS: View[] = ["today", "holdings", "activity", "results", "rules", "signals", "settings", "more"];

/** What every view is handed: the snapshot, how current it is, and the few
 *  things a view can ask the shell to do. */
export interface Page {
  s: Status;
  link: Link;
  stale: boolean;
  asOf: Iso;
  rows: Activity[];
  hasOlder: boolean;
  feed: StatusFeed;
  busy: boolean;
  dark: boolean;
  toggleTheme: () => void;
  go: (v: View) => void;
  openEv: (n: number) => void;
  openPos: (id: string) => void;
  act: (a: BandAction | "stop") => void;
  /** the page load's update check, and the dialog that installs it */
  update: UpdateFeed;
  openUpdate: () => void;
}

function viewFromHash(): View {
  const h = location.hash.replace(/^#\/?/, "");
  return (VIEWS as string[]).includes(h) ? (h as View) : "today";
}

// The navy palette of the classic dashboard unless the owner chose the light
// theme here (guardian.css): the computer's own light/dark setting does not
// switch it, the same as the classic page.
function useTheme() {
  const root = document.documentElement;
  const read = () => root.dataset.theme !== "light";
  const [dark, setDark] = useState(read);
  const toggle = useCallback(() => {
    const next = !read();
    root.dataset.theme = next ? "dark" : "light";
    try {
      localStorage.setItem("at-theme", root.dataset.theme);
    } catch {
      /* private mode: the choice lasts for this visit */
    }
    setDark(next);
  }, []); // eslint-disable-line react-hooks/exhaustive-deps
  return { dark, toggle };
}

const NAV: [View, IconId, string, string][] = [
  ["today", "today", "Today", ""],
  ["holdings", "hold", "Holdings", ""],
  ["activity", "list", "Activity", ""],
  ["results", "chart", "Results", ""],
  ["rules", "rules", "Rules", " & limits"],
  ["signals", "signal", "Signals", ""],
  ["settings", "gear", "Settings", ""],
];
const TABS: [View, IconId, string][] = [
  ["today", "today", "Today"],
  ["holdings", "hold", "Holdings"],
  ["activity", "list", "Activity"],
  ["results", "chart", "Results"],
  ["more", "more", "More"],
];

function PositionDrawer({ p, page, onClose }: { p: Position; page: Page; onClose: () => void }) {
  const s = page.s;
  const nx = nextFor(p, s, page.link);
  const name = contractName(p.contract, p.label);
  const b = s.broker.name || "your broker";
  const today = etDayKey(s.as_of);
  const looked = p.next.action !== "unmanaged" && p.next.action !== "unknown";
  const hist = p.contract
    ? page.rows
        .filter((e) => e.contract_text != null && e.contract && e.contract.symbol === p.contract?.symbol && e.contract.expiry === p.contract?.expiry && e.contract.strike === p.contract?.strike && e.contract.side === p.contract?.side)
        .slice()
        .reverse()
    : [];
  const exp = expiryWill(p, s);
  const will = p.real
    ? looked
      ? [`Sell ${p.qty === 1 ? "the contract" : `all ${count(p.qty)} contracts`} when the exit signal arrives.`, ...(exp.will ? [exp.will] : [])]
      : ["Nothing with this position right now. See above for why."]
    : ["Record a pretend sale when the exit signal arrives.", "Show the pretend result under Results, in the practice column."];
  const wont = p.real
    ? [
        "Buy more of it.",
        `Sell it early on its own judgement. It has no stop-loss and no profit target; it follows the exit signal${s.expiry_guard_on ? " and the expiry rule" : ""}.`,
        `Sell it while the agent is stopped or on hold, or while ${b} is not connected.`,
        ...(exp.wont ? [exp.wont] : []),
      ]
    : [`Send anything to ${b}.`, "Turn into a real position if you switch to LIVE."];
  return (
    <Drawer
      title={name}
      sub={
        <>
          <Tag kind={p.real ? "real" : "prac"} />
          {p.expires_today && <ExpiresFlag />}
        </>
      }
      onClose={onClose}
    >
      <section className={`nextbox ${nx.look === "bad" ? "bad" : ""}`}>
        <Icon name={nx.icon} />
        <div>
          <h3>What the agent will do{page.stale ? ` (as of ${etTime(page.asOf)})` : ""}</h3>
          <p>{nx.text}</p>
        </div>
      </section>
      <section>
        <dl className="facts">
          <dt>Contracts</dt>
          <dd>{count(p.qty)}</dd>
          <dt>Paid</dt>
          <dd>
            {p.entry_cost_usd != null ? usd(p.entry_cost_usd) : UNKNOWN}{" "}
            <small>{p.entry_price != null ? `${usd(p.entry_price)} per share at the quoted price, 100 shares per contract` : "The price when it was bought was not recorded."}</small>
          </dd>
          <dt>Bought</dt>
          <dd>{etWhen(p.opened_at, today)}</dd>
          <dt>Expires</dt>
          <dd>
            {p.contract ? dayLong(p.contract.expiry) : UNKNOWN}
            {p.expires_today ? " (today)" : ""}
          </dd>
          <dt>Where</dt>
          <dd>{p.real ? brokerLabel(s) : "Agent's practice records only"}</dd>
          <dt>Signal type</dt>
          <dd>{p.book === "other" ? "Other trades" : p.book === "main" ? "Main pick" : "not recorded"}</dd>
          <dt>Value now</dt>
          <dd>
            <small>{p.real ? `Not tracked yet. ${b} shows the live price.` : "Not tracked for practice positions."}</small>
          </dd>
        </dl>
      </section>
      <section>
        <h3>What happens next</h3>
        <WillWont will={will} wont={wont} />
      </section>
      <section>
        <h3>History</h3>
        {hist.length ? (
          <ol className="tl">
            {hist.map((e) => (
              <li key={e.n}>
                {describe(e, s.broker.name).title}
                <span>{etWhen(e.at)}</span>
              </li>
            ))}
            <li className="next">
              {p.real ? "Sale on exit signal" : "Pretend sale on exit signal"}
              <span>Not yet</span>
            </li>
          </ol>
        ) : (
          <p className="muted small">No lines for this contract in the activity loaded on this page. Earlier lines are under Activity.</p>
        )}
      </section>
      {p.real && (
        <section>
          <p className="small muted">To close it now, sell it at {b}. The agent notices and stops tracking it.</p>
        </section>
      )}
    </Drawer>
  );
}

function Shell({ feed, status, upd }: { feed: StatusFeed; status: Status; upd: UpdateFeed }) {
  const [view, setView] = useState<View>(viewFromHash);
  const [dialog, setDialog] = useState<DialogKind | null>(null);
  const [updOpen, setUpdOpen] = useState(false);
  const [evN, setEvN] = useState<number | null>(null);
  const [posId, setPosId] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [toast, setToast] = useState<{ ok: boolean; text: string } | null>(null);
  const toastTimer = useRef<ReturnType<typeof setTimeout>>();
  const main = useRef<HTMLElement>(null);
  const theme = useTheme();

  useEffect(() => {
    const on = () => setView(viewFromHash());
    addEventListener("hashchange", on);
    return () => removeEventListener("hashchange", on);
  }, []);
  const go = useCallback((v: View) => {
    if (viewFromHash() !== v) location.hash = `/${v}`;
    setView(v);
    requestAnimationFrame(() => {
      main.current?.focus({ preventScroll: true });
      scrollTo(0, 0);
    });
  }, []);

  const link = linkOf(status, feed.failing);
  const say = useCallback((ok: boolean, text: string) => {
    setToast({ ok, text });
    clearTimeout(toastTimer.current);
    toastTimer.current = setTimeout(() => setToast(null), 15000);
  }, []);
  const send = useCallback(
    async (command: string) => {
      setBusy(true);
      const r = await feed.command(command);
      setBusy(false);
      setDialog(null);
      say(r.ok, r.ok ? `The agent answered: ${r.reply}` : r.reply);
    },
    [feed, say],
  );
  const act = useCallback(
    (a: BandAction | "stop") => {
      if (a === "retry") {
        setBusy(true);
        feed.refresh().finally(() => setBusy(false));
        return;
      }
      setDialog(a);
    },
    [feed],
  );

  const stale = isStale(link);
  const badge = badgeFor(upd.check, upd.phase, !stale);
  const openUpdate = useCallback(() => {
    if (upd.check.kind === "available" && upd.phase === "idle") setUpdOpen(true);
  }, [upd.check, upd.phase]);
  // the install answered: the dialog closes and the toast says what happens
  // next; a failure stays in the dialog until the owner closes it
  useEffect(() => {
    if (upd.phase === "restarting") {
      setUpdOpen(false);
      say(true, UPDATE.restartingSaid(upd.detail));
    } else if (upd.phase === "installed" || upd.phase === "slow") {
      setUpdOpen(false);
      say(true, updateLine(upd.check, upd.phase, upd.detail).text);
    }
  }, [upd.phase]); // eslint-disable-line react-hooks/exhaustive-deps
  const page: Page = {
    s: status,
    link,
    stale,
    asOf: asOf(status, link),
    rows: feed.rows,
    hasOlder: feed.canLoadOlder,
    feed,
    busy,
    dark: theme.dark,
    toggleTheme: theme.toggle,
    go,
    openEv: setEvN,
    openPos: setPosId,
    act,
    update: upd,
    openUpdate,
  };
  const ev = evN != null ? feed.rows.find((e) => e.n === evN) : undefined;
  const pos = posId != null ? status.positions.find((p) => p.id === posId) : undefined;
  const np = status.positions.length;
  const days = status.signals.pass.days_left;
  const problems = !stale && status.broker.connected === false ? 1 : 0;
  const tab = (["today", "holdings", "activity", "results"] as View[]).includes(view) ? view : "more";

  return (
    <>
      <div id="shell" className={stale ? "stale-all" : ""}>
        <a className="skip" href="#main">
          Skip to content
        </a>
        <div className="frame" aria-hidden="true" />
        <Band status={status} link={link} busy={busy} onAction={act} update={badge} onUpdate={openUpdate} />
        <div className="app">
          <aside className="side" aria-label="Sections">
            <div className="brand">
              <span className="brand-mark">
                <Icon name="shield" />
              </span>
              <span className="nl">Agentic Trader</span>
            </div>
            <nav className="nav" aria-label="Main">
              {NAV.map(([k, i, l, l2]) => (
                <button key={k} data-view={k} aria-current={view === k ? "page" : undefined} onClick={() => go(k)}>
                  <Icon name={i} />
                  <span className="nl">
                    {l}
                    {l2 && <span className="l2">{l2}</span>}
                  </span>
                  {k === "holdings" && np > 0 && <span className="count">{np}</span>}
                  {k === "settings" && problems > 0 && (
                    <span className="count alert">
                      <Icon name="alert" small />
                      <span className="sr"> 1 problem</span>
                    </span>
                  )}
                  {k === "signals" && days != null && (
                    <span className="count">
                      {days}
                      <span className="cl"> {days === 1 ? "day" : "days"}</span>
                    </span>
                  )}
                </button>
              ))}
            </nav>
            <div className="side-foot">
              <a className="side-btn" href="/dash">
                <Icon name="back" small />
                <span className="nl">
                  Classic<span className="l2"> dashboard</span>
                </span>
              </a>
              <button className="side-btn" onClick={theme.toggle}>
                <Icon name={theme.dark ? "sun" : "moon"} small />
                <span className="nl">
                  {theme.dark ? "Light" : "Dark"}
                  <span className="l2"> theme</span>
                </span>
              </button>
            </div>
          </aside>
          <main id="main" tabIndex={-1} ref={main}>
            {feed.fetching.kind === "locked" && (
              <div className="view" style={{ marginBottom: 20 }}>
                <div className="note note-crit" role="alert">
                  <Icon name="alert" />
                  <h3>Your dashboard login expired</h3>
                  <p>
                    This page can no longer read from the agent, so everything below is its last report. Run `python agent.py link` in the
                    agent folder (or restart the agent) and open the link it prints.
                  </p>
                </div>
              </div>
            )}
            {view === "today" && <Today page={page} />}
            {view === "holdings" && <HoldingsView page={page} />}
            {view === "activity" && <ActivityView page={page} />}
            {view === "results" && <ResultsView page={page} />}
            {view === "rules" && <RulesView page={page} />}
            {view === "signals" && <SignalsView page={page} />}
            {view === "settings" && <SettingsView page={page} />}
            {view === "more" && <MoreView page={page} />}
          </main>
        </div>
        <nav className="tabs" aria-label="Sections, bottom bar">
          {TABS.map(([k, i, l]) => (
            <button key={k} data-view={k} aria-current={tab === k ? "page" : undefined} onClick={() => go(k)}>
              <Icon name={i} />
              <span>{l}</span>
              {k === "more" && problems > 0 && (
                <>
                  <span className="pip">
                    <Icon name="alert" small />
                  </span>
                  <span className="sr">1 problem</span>
                </>
              )}
            </button>
          ))}
        </nav>
      </div>
      {ev && !dialog && !updOpen && <EventDrawer e={ev} page={page} onClose={() => setEvN(null)} />}
      {pos && !dialog && !updOpen && !ev && <PositionDrawer p={pos} page={page} onClose={() => setPosId(null)} />}
      {dialog && <ActionDialog kind={dialog} s={status} busy={busy} onConfirm={send} onClose={() => !busy && setDialog(null)} />}
      {updOpen && !dialog && upd.check.kind === "available" && (
        <UpdateDialog
          offer={upd.check.offer}
          s={status}
          phase={upd.phase}
          detail={upd.detail}
          since={upd.since}
          onInstall={() => upd.install(status.agent.started_at)}
          onClose={() => upd.phase !== "installing" && setUpdOpen(false)}
        />
      )}
      <div className={`toast ${toast ? "open" : ""} ${toast && !toast.ok ? "toast-bad" : ""}`} role="status" aria-live="polite">
        {toast && (
          <>
            {!toast.ok && <Icon name="alert" small />}
            <span>{toast.text}</span>
            <button onClick={() => setToast(null)}>Dismiss</button>
          </>
        )}
      </div>
    </>
  );
}

/** Before the first answer, and when there has never been one. */
function Waiting({ feed }: { feed: StatusFeed }) {
  const locked = feed.fetching.kind === "locked";
  const failed = feed.failing || locked;
  // the agent answered, but not with a status this page can read
  const mismatch = feed.fetching.kind === "failing" && feed.fetching.why === "schema";
  useEffect(() => {
    document.documentElement.dataset.mode = failed ? "unknown" : "setup";
    document.title = failed ? "No connection · Agentic Trader" : "Agentic Trader";
  }, [failed]);
  return (
    <div id="shell">
      <div className="frame" aria-hidden="true" />
      <header className="band" aria-label="Trading mode and agent status">
        <div className="band-mode">
          <div className="mode-id">
            <span className="mode-glyph" />
            <span className="mode-word">{failed ? "Mode unknown" : "Agentic Trader"}</span>
          </div>
          <p className="mode-say" role="status">
            {locked
              ? "Your dashboard login expired."
              : failed && mismatch
                ? "Your agent answered in a form this page can't read."
                : failed
                  ? "This window can't reach your agent, and it has no earlier report to show."
                : "Asking your agent how things stand…"}
          </p>
        </div>
      </header>
      <main id="main" tabIndex={-1} className="waiting">
        {failed && (
          <div className="view">
            <div className="note note-crit" role="alert">
              <Icon name="alert" />
              <h3>{locked ? "Open your login link again" : mismatch ? "The agent and this page are different versions" : "Nothing here is known yet"}</h3>
              <p>
                {locked
                  ? "Run `python agent.py link` in the agent folder (or restart the agent) and open the link it prints. Nothing was changed."
                  : mismatch
                    ? "Nothing is shown rather than something misread. The classic dashboard still works; updating the agent brings this page back."
                    : "The agent may be stopped, or this computer may have just woken up. This page keeps trying every few seconds. If the agent is stopped, it is not buying or selling anything: check your positions at your broker."}
              </p>
              {!locked && (
                <div className="note-act">
                  <button className="btn" onClick={() => feed.refresh()}>
                    Try again
                  </button>
                  <a className="btn" href="/dash">
                    Back to the classic dashboard
                  </a>
                </div>
              )}
            </div>
          </div>
        )}
      </main>
    </div>
  );
}

export function App() {
  const feed = useStatus();
  // here, not in the shell: one check per page load, whatever is on screen
  const upd = useUpdate();
  if (!feed.status) return <Waiting feed={feed} />;
  return <Shell feed={feed} status={feed.status} upd={upd} />;
}
