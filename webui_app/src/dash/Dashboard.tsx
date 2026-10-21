import { AlertTriangle, ArrowDownRight, ArrowUpRight, CircleHelp, Laptop, Minus, Pause, Play, Settings, ShieldCheck, Zap } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { AppBar, Pill } from "../components/AppBar";
import { ChatLog, nextMsgId, type ChatMsg } from "../components/Chat";
import { Button, LinkButton } from "../components/ui/button";
import { ConfirmDialog } from "../components/ui/dialog";
import { Input } from "../components/ui/field";
import { Count, Panel } from "../components/ui/panel";
import { ModeTag, RightTag } from "../components/ui/tag";
import { api, type BudgetHint, type Problem, type Proposal, type StatusResp, type Trade, type WouldHave } from "../lib/api";
import { cn } from "../lib/cn";
import { copyToClipboard, useInterval } from "../lib/hooks";
import { cap1, hhmm, isToday, money, parseContract, parsePositions, shortDate, whenShort, type Contract } from "../lib/format";
import { MovePanel } from "./MovePanel";
import { PnlChart, type PnlPoint } from "./PnlChart";

const SOURCES: Record<string, string> = { agenthc: "AgentHC feed", url: "Your JSON feed", manual: "Manual commands" };
const SKIPS: Record<string, string> = {
  skip_stale: "signal was too old",
  skip_daily_cap: "daily entry cap reached",
  skip_position_cap: "max open positions reached",
  skip_daily_loss: "daily loss limit reached",
  policy_veto: "your plain-English rules said no",
  skip_ai_needed: "entries skipped — your rules need an AI and none is working",
  skip_rule: "your rules",
  skip_sizing: "couldn't size the trade",
  order_failed: "the broker rejected the order",
  skip_budget: "one contract costs more than your budget",
  skip_no_quote: "no price quote for the contract",
  skip_no_contract: "contract not found at the broker",
  entry_unfilled: "order placed but never filled",
  exit_failed: "exit order didn't go through (retrying)",
  exit_partial: "exit only partly filled",
  broker_no_order: "the broker didn't place the order",
  broker_disconnected: "broker connection down (login expired or unreachable)",
};

/** "$1,250" — whole dollars, no sign (budgets and contract costs). */
const usd = (v: number) => "$" + Math.round(v).toLocaleString("en-US");

type Conn = "loading" | "ok" | "off" | "unreachable";

export function Dashboard() {
  const [conn, setConn] = useState<Conn>("loading");
  const [status, setStatus] = useState<StatusResp | null>(null);
  const [trades, setTrades] = useState<Trade[]>([]);
  const [proposal, setProposal] = useState<Proposal | null>(null);
  const [updateAvail, setUpdateAvail] = useState(false);
  const [helpOpen, setHelpOpen] = useState(false);
  const [moveOpen, setMoveOpen] = useState(false);
  const [confirmLive, setConfirmLive] = useState(false);
  const [msgs, setMsgs] = useState<ChatMsg[]>([]);

  const refresh = async () => {
    try {
      const s = await api<StatusResp>("/api/status");
      if (s.mode === "OFFLINE") {
        setConn("off");
        setStatus(null);
        return;
      }
      setStatus(s);
      setConn("ok");
      try {
        setProposal(await api<Proposal>("/api/proposal"));
      } catch {
        /* proposal panel is optional */
      }
      const t = await api<{ trades: Trade[] }>("/api/trades");
      setTrades(t.trades || []);
    } catch {
      setConn("unreachable");
    }
  };
  useInterval(refresh, 5000);

  // "update available" badge: one quiet check per page load; never installs.
  useEffect(() => {
    api("/api/update/check")
      .then((r) => r.ok && r.behind && setUpdateAvail(true))
      .catch(() => {});
  }, []);

  const send = async (text: string) => {
    const t = text.trim();
    if (!t) return;
    const id = nextMsgId();
    setMsgs((m) => [...m, { who: "me", text: t }, { who: "agent", text: "…", pending: true, id }]);
    let reply = "";
    try {
      reply = (await api<{ reply: string }>("/api/command", { text: t })).reply;
    } catch {
      reply = "Could not reach the agent on this computer. Is it still running?";
    }
    setMsgs((m) => m.map((x) => (x.id === id ? { who: "agent", text: reply } : x)));
    refresh();
  };

  const live = status?.mode === "LIVE";
  const paused = !!status?.paused;
  const f = status?.fields || {};
  const rail = conn === "ok" ? (paused ? "paused" : live ? "live" : "dry") : conn === "loading" ? "none" : "off";

  return (
    <>
      <AppBar page="Dashboard" rail={rail}>
        <span className="h-5 w-px bg-line2 max-sm:hidden" />
        {conn === "ok" ? (
          <>
            <Pill tone={live ? "live" : "dry"} dot>
              {live ? "LIVE · real orders" : "Dry-run · no orders"}
            </Pill>
            {(f["market"] || "").startsWith("open") ? (
              <Pill tone="up" dot className="max-sm:hidden">
                Market open
              </Pill>
            ) : (
              <Pill className="max-sm:hidden">Market closed</Pill>
            )}
            {paused && <Pill tone="warn">Paused</Pill>}
          </>
        ) : conn === "loading" ? null : (
          <Pill tone="bad">{conn === "off" ? "Agent not running" : "Agent unreachable"}</Pill>
        )}
        <span className="flex-1" />
        <div className="flex items-center gap-1 max-sm:order-2 max-sm:ml-auto">
          {updateAvail && (
            <LinkButton href="/setup" variant="ghost" size="sm" className="text-accent" title="Install it from Settings">
              Update available
            </LinkButton>
          )}
          <Button variant="ghost" size="sm" onClick={() => setMoveOpen((o) => !o)} aria-expanded={moveOpen} aria-controls="move" title="Move to another computer">
            <Laptop className="h-4 w-4" />
            <span className="max-lg:hidden">Move computer</span>
          </Button>
          <Button variant="ghost" size="sm" onClick={() => setHelpOpen((o) => !o)} aria-expanded={helpOpen} aria-controls="help">
            <CircleHelp className="h-4 w-4" />
            <span className="max-lg:hidden">Help</span>
          </Button>
          <LinkButton href="/setup" variant="ghost" size="sm" title="Settings">
            <Settings className="h-4 w-4" />
            <span className="max-lg:hidden">Settings</span>
          </LinkButton>
        </div>
        {conn === "ok" && (
          <div className="flex items-center gap-2 max-sm:order-5 max-sm:w-full max-sm:[&>*]:flex-1">
            <span className="h-5 w-px bg-line2 max-sm:hidden" />
            <Button
              variant={paused ? "primary" : "default"}
              onClick={() => send(paused ? "resume" : "pause")}
            >
              {paused ? <Play className="h-3.5 w-3.5" /> : <Pause className="h-3.5 w-3.5" />}
              {paused ? "Resume" : "Pause"}
            </Button>
            {live ? (
              <Button onClick={() => send("dry on")}>
                <ShieldCheck className="h-3.5 w-3.5" />
                Switch to dry-run
              </Button>
            ) : (
              <Button variant="warn" onClick={() => setConfirmLive(true)}>
                <Zap className="h-3.5 w-3.5" />
                Go live
              </Button>
            )}
          </div>
        )}
      </AppBar>

      <ConfirmDialog
        open={confirmLive}
        onOpenChange={setConfirmLive}
        title="Go live?"
        confirmLabel="Go live"
        onConfirm={() => send("dry off")}
      >
        From now on the agent sends <b className="text-fg">real orders with real money</b> to your broker. You can switch
        back to dry-run at any time. Your decision, not advice.
      </ConfirmDialog>

      <main className="mx-auto max-w-[1440px] px-6 pb-10 pt-5 max-sm:px-4 max-sm:pt-4">
        <ModeBanner conn={conn} live={live} paused={paused} />
        <StatusStrip conn={conn} f={f} />
        {conn === "ok" && <WhyNoTrades warning={status?.cash_warning || ""} problems={status?.problems || []} hints={status?.budget_hints || []} live={live} onCommand={send} />}

        <div className="mt-4 grid grid-cols-[minmax(0,1fr)_400px] items-start gap-4 max-xl:grid-cols-1">
          <div className="flex min-w-0 flex-col gap-4">
            {proposal?.pending && <ProposalPanel p={proposal} onDone={refresh} />}
            <PnlPanel trades={trades} />
            {conn === "ok" && !live && status?.would_have && <WouldHavePanel p={status.would_have} />}
            <PositionsPanel positions={parsePositions(f["open positions"])} />
            <Blotter trades={trades} />
          </div>
          <div className="flex min-w-0 flex-col gap-4 xl:sticky xl:top-[68px]">
            {moveOpen && <MovePanel onClose={() => setMoveOpen(false)} />}
            {helpOpen && <HelpPanel onClose={() => setHelpOpen(false)} />}
            <ConsolePanel msgs={msgs} send={send} />
          </div>
        </div>
      </main>
    </>
  );
}

// ── dry-run preview: what this setup would have taken ───────────────────────
function pct(n: number | null) {
  return n == null ? "—" : `${n > 0 ? "+" : ""}${n.toFixed(1)}%`;
}
function pnlTone(n: number | null) {
  // W/BE/L: 0% to +6% is breakeven
  return n == null ? "text-faint" : n > 6 ? "text-up" : n < 0 ? "text-down" : "text-fg2";
}
function entryWhen(r: { date: string; entry_time?: string }) {
  const d = r.entry_time ? new Date(r.entry_time) : null;
  return d && !isNaN(+d) ? `${shortDate(d)} ${hhmm(d)}` : r.date;
}
function WouldHavePanel({ p }: { p: WouldHave }) {
  const rows = p.rows || [];
  const sub =
    p.would_take > 0
      ? `${p.would_take} would take · ${p.wins}W ${p.breakeven}BE ${p.losses}L` +
        (p.avg_paper_pnl_pct != null ? ` · avg ${pct(p.avg_paper_pnl_pct)}` : "")
      : "";
  return (
    <Panel title={`Would have taken · past ${p.days} days`} right={sub ? <span className="text-xs text-muted">{sub}</span> : <Count n={rows.length} />}>
      <p className="m-0 border-b border-line px-4 py-2 text-xs leading-5 text-muted">
        Dry-run preview. AgentHC's closed paper trades, checked against your rules. Free, no pass needed. Trades show up
        after they close, not live. P&L is AgentHC's paper result, not your fills. Your AI policy, daily caps and budget
        aren't applied. Hypothetical, not advice. Turn off with <code className="font-mono">preview off</code>.
      </p>
      {rows.length === 0 ? (
        <p className="m-0 px-4 py-6 text-center text-xs text-muted">
          {p.unavailable
            ? "The signal server doesn't offer the free preview yet."
            : p.error
              ? `Couldn't load the preview yet (${p.error}). It retries every 5 minutes.`
              : "No closed trades in the past week yet."}
        </p>
      ) : (
        <>
          <div className="overflow-x-auto max-md:hidden">
            <table className="w-full border-collapse text-sm">
              <thead>
                <tr className="text-left text-xs text-muted">
                  <Th>Entered</Th>
                  <Th>Contract</Th>
                  <Th>Your setup</Th>
                  <Th>Held</Th>
                  <Th className="text-right">Paper P&L</Th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r, i) => (
                  <tr key={i} className="border-t border-line align-top hover:bg-panel2">
                    <Td className="font-mono text-xs leading-5 text-muted">{entryWhen(r)}</Td>
                    <Td className="font-mono text-xs leading-5 text-fg">{r.contract}</Td>
                    <td className="min-w-[180px] max-w-[360px] px-4 py-2 text-xs leading-5 [overflow-wrap:anywhere]">
                      {r.would_take ? <span className="text-fg">Would take</span> : <span className="text-muted">Skip: {r.skip_reason}</span>}
                    </td>
                    <Td className="font-mono text-xs leading-5 text-muted">{r.hold_minutes != null ? `${r.hold_minutes}m` : "—"}</Td>
                    <Td className={cn("text-right font-mono", r.would_take ? pnlTone(r.paper_pnl_pct) : "text-faint")}>{pct(r.paper_pnl_pct)}</Td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <ul className="m-0 list-none p-0 md:hidden">
            {rows.map((r, i) => (
              <li key={i} className="grid grid-cols-[minmax(0,1fr)_auto] gap-x-2.5 gap-y-0.5 border-t border-line px-3 py-2.5 first:border-t-0">
                <div className="min-w-0 font-mono text-xs text-fg [overflow-wrap:anywhere]">{r.contract}</div>
                <div className={cn("text-right font-mono text-sm", r.would_take ? pnlTone(r.paper_pnl_pct) : "text-faint")}>{pct(r.paper_pnl_pct)}</div>
                <div className="col-span-2 text-xs text-muted [overflow-wrap:anywhere]">
                  {entryWhen(r)} · {r.would_take ? "would take" : `skip: ${r.skip_reason}`}
                </div>
              </li>
            ))}
          </ul>
        </>
      )}
    </Panel>
  );
}

// ── why trades didn't happen today ──────────────────────────────────────────
function WhyNoTrades({
  warning,
  problems,
  hints,
  live,
  onCommand,
}: {
  warning: string;
  problems: Problem[];
  hints: BudgetHint[];
  live: boolean;
  onCommand: (text: string) => void;
}) {
  const [ask, setAsk] = useState<BudgetHint | null>(null);
  if (!warning && !problems.length && !hints.length) return null;
  const who = (h: BudgetHint) => (h.book === "shadow" ? "Other trades (shadow book)" : "Main pick");
  return (
    <div role="alert" className="mt-4 rounded-md border border-warn/45 bg-warn/[0.07] px-4 py-3 text-sm text-fg2">
      <div className="flex items-start gap-3">
        <AlertTriangle className="mt-0.5 h-4 w-4 flex-none text-warn" />
        <div className="min-w-0 flex-1 space-y-2">
          <b className="text-fg">Trades that didn't happen today, and why</b>
          {warning && (
            <p className="m-0">
              {warning}
              {!live && " (You're in dry-run, so nothing is blocked yet.)"}
            </p>
          )}
          {problems.length > 0 && (
            <ul className="m-0 list-none space-y-1.5 p-0">
              {problems.map((p) => (
                <li key={p.kind} className="[overflow-wrap:anywhere]">
                  <b className="text-fg">{cap1(SKIPS[p.kind] || p.kind.replace(/_/g, " "))}</b>
                  {p.count > 1 ? ` ×${p.count}` : ""}
                  {p.at ? <span className="text-muted"> · last {hhmm(new Date(p.at))}</span> : null}
                  {p.contract ? <span className="text-muted"> · {p.contract}</span> : null}
                  {p.last ? <div className="text-muted">{String(p.last).slice(0, 240)}</div> : null}
                  {p.hint ? <div className="text-fg">Fix: {p.hint}</div> : null}
                </li>
              ))}
            </ul>
          )}
          {hints.map((h) => (
            <div key={h.book} className="rounded-md border border-line2 bg-panel px-3 py-2">
              <p className="m-0">
                <b className="text-fg">{who(h)}: </b>
                {h.skipped} {h.skipped === 1 ? "signal" : "signals"} today cost more than your {usd(h.budget)} per-trade
                budget. One contract ran {usd(h.min_cost)}
                {h.max_cost > h.min_cost ? ` to ${usd(h.max_cost)}` : ""}. A {usd(h.suggest)} budget would have taken{" "}
                {h.would_take} of {h.skipped}.
              </p>
              <Button size="sm" className="mt-2" onClick={() => setAsk(h)}>
                Set budget to {usd(h.suggest)}
              </Button>
            </div>
          ))}
        </div>
      </div>
      <ConfirmDialog
        open={!!ask}
        onOpenChange={(o) => !o && setAsk(null)}
        title="Raise your per-trade budget?"
        confirmLabel={ask ? `Set ${usd(ask.suggest)}` : "Set"}
        onConfirm={() => ask && onCommand(ask.command)}
      >
        {ask && (
          <>
            {who(ask)} will spend up to <b className="text-fg">{usd(ask.suggest)}</b> per trade instead of{" "}
            {usd(ask.budget)}
            {live ? ", with real money" : ""}. You can change it back any time in Settings. Your decision, not advice.
          </>
        )}
      </ConfirmDialog>
    </div>
  );
}

// ── mode banner ─────────────────────────────────────────────────────────────
function ModeBanner({ conn, live, paused }: { conn: Conn; live: boolean; paused: boolean }) {
  if (conn === "loading") return <div className="h-10" />;
  const off = conn !== "ok";
  const tone = off ? "border-down/45 bg-down/[0.07]" : live ? "border-live/50 bg-live/[0.07]" : paused ? "border-warn/45 bg-warn/[0.07]" : "border-accent/40 bg-accent/[0.06]";
  const ic = off ? "text-down" : live ? "text-live" : paused ? "text-warn" : "text-accent";
  return (
    <div role="status" className={cn("mb-4 flex items-start gap-3 rounded-md border px-4 py-2.5 text-sm text-fg2", tone)}>
      <ShieldCheck className={cn("mt-0.5 h-4 w-4 flex-none", ic)} />
      <div className="flex min-w-0 flex-1 flex-wrap justify-between gap-x-6 gap-y-1">
        {conn === "off" ? (
          <span>
            <b className="text-down">Agent not running.</b> Start it with{" "}
            <code className="rounded-sm bg-raise px-1 font-mono text-xs">python agent.py start</code>
          </span>
        ) : conn === "unreachable" ? (
          <span>
            <b className="text-down">Can't reach the agent on this computer.</b> Is it still running?
          </span>
        ) : (
          <>
            <span>
              {live ? (
                <>
                  <b className="text-live">LIVE.</b> Real orders with real money are sent to your broker.
                </>
              ) : (
                <>
                  <b className="text-accent">Dry-run.</b> The agent logs every decision but places no orders.
                </>
              )}
              {paused && (
                <>
                  {" "}
                  <b className="text-warn">Paused:</b> no new entries until you press Resume.
                </>
              )}
            </span>
            <span className="text-muted">
              {live
                ? "Switch to dry-run any time to stop sending orders. Your decision, not advice."
                : "Going live sends real orders with real money. Your decision, not advice."}
            </span>
          </>
        )}
      </div>
    </div>
  );
}

// ── status strip ────────────────────────────────────────────────────────────
function Cell({ k, children, sub, bad, className, wrap }: { k: string; children: React.ReactNode; sub?: React.ReactNode; bad?: boolean; className?: string; wrap?: boolean }) {
  return (
    <div className={cn("relative min-w-0 px-4 py-3", bad && "shadow-[inset_0_2px_0_rgb(var(--down))]", className)}>
      <div className="text-xs text-muted">{k}</div>
      <div className={cn("mt-1.5 font-mono text-xl", wrap ? "break-words" : "truncate", " font-medium tracking-[-0.02em]", bad ? "text-down" : "text-fg")}>{children}</div>
      {sub != null && <div className="mt-1 text-xs [overflow-wrap:anywhere] text-muted">{sub}</div>}
    </div>
  );
}

function StatusStrip({ conn, f }: { conn: Conn; f: Record<string, string> }) {
  const ok = conn === "ok";
  const ent = (f["entries today"] || "").split("/");
  const n = parseInt(ent[0], 10);
  const cap = parseInt((ent[1] || "").replace(/\D+/g, ""), 10) || 0;
  const positions = parsePositions(f["open positions"]);

  // sizing: "$500/trade budget (other trades: $100/trade)"
  const szRaw = String(f["sizing"] || "");
  const plur = (s: string) => s.replace(/(\d+) contract\(s\)(\/trade)?/g, (_m, k) => k + " contract" + (k === "1" ? "" : "s"));
  const oi = szRaw.indexOf(" (other trades:");
  const sz = plur(oi >= 0 ? szRaw.slice(0, oi) : szRaw);
  const szOther = oi >= 0 ? plur(szRaw.slice(oi + 2).replace(/\)\s*$/, "")) : "";
  const sm = sz.match(/^(\S+)\s+(.+)$/);

  // feed: "prepaid pass · 4 signal days left" / "wallet 41,000 sats" / …
  let feedV: React.ReactNode = "–",
    feedS = "",
    feedBad = false,
    feedTitle = "";
  if (ok) {
    if (f["source"] === "agenthc") {
      const t = String(f["feed access"] || "");
      feedTitle = t;
      const m = t.match(/(\d+)\s+signal day/);
      if (m) {
        const d = parseInt(m[1], 10);
        feedV = (
          <>
            {d}
            <small className="ml-1 font-sans text-xs font-normal tracking-normal text-muted">day{d === 1 ? "" : "s"} left</small>
          </>
        );
        feedS = "prepaid signal days";
      } else {
        feedV = <span className="font-sans text-base">{t || "–"}</span>;
      }
      feedBad = /expired|used up|not paid|none|no access|exits only/i.test(t);
    } else {
      feedV = <span className="font-sans text-base">{SOURCES[f["source"]] || f["source"] || "–"}</span>;
      feedS = "signal source";
    }
  }
  const polOn = f["policy brain"] === "on";
  const polBlocked = f["policy brain"] === "blocked";
  const rulesOn = !!f["rules"] && f["rules"] !== "none";

  return (
    <div className="grid grid-cols-5 divide-x divide-line overflow-hidden rounded-md border border-line bg-panel max-lg:grid-cols-3 max-lg:divide-x-0 max-sm:grid-cols-2 [&>*]:border-line max-lg:[&>*]:border-b max-lg:[&>*]:border-r">
      <Cell
        k="Entries today"
        sub={ok && cap ? `of ${cap} allowed today` : ""}
      >
        {ok && !isNaN(n) ? (
          <>
            {n}
            {cap ? <small className="ml-1 text-sm text-muted">/ {cap}</small> : null}
          </>
        ) : (
          "–"
        )}
        <div className="mt-2 h-[3px] overflow-hidden rounded-full bg-line2" aria-hidden="true">
          <i
            className={cn("block h-full transition-[width]", cap && n >= cap ? "bg-warn" : "bg-accent")}
            style={{ width: `${ok && cap ? Math.min(100, (n * 100) / cap) : 0}%` }}
          />
        </div>
      </Cell>
      <Cell k="Open positions" sub="opened by this agent">
        {ok ? positions.length : "–"}
      </Cell>
      <Cell k="Signal feed" sub={feedS} bad={feedBad}>
        <span title={feedTitle}>{feedV}</span>
      </Cell>
      <Cell k="Size per trade" wrap sub={<span title={szOther ? szOther + " (your choice, not advice)" : ""}>{szOther || "your choice, not advice"}</span>}>
        <span title={szRaw ? szRaw + " (your choice, not advice)" : ""}>
          {ok && sz ? (
            sm ? (
              <>
                {sm[1]}
                <small className="ml-1 font-sans text-xs font-normal tracking-normal text-muted">{sm[2]}</small>
              </>
            ) : (
              sz
            )
          ) : (
            "–"
          )}
        </span>
      </Cell>
      <Cell
        k="Your rules"
        bad={ok && polBlocked}
        wrap
        sub={
          ok
            ? polBlocked
              ? "plain-English rules but no AI: every entry is skipped. Connect an AI in Settings, or say 'policy off'"
              : (rulesOn ? f["rules"] : "set them in Settings → Safety, or say 'rules'") +
                (polOn ? " · plus plain-English rules (AI)" : "")
            : ""
        }
        className="max-sm:col-span-2"
      >
        {ok ? (
          <span className="inline-flex items-center gap-2 font-sans text-lg">
            <span className={cn("h-2 w-2 rounded-full", polBlocked ? "bg-down" : rulesOn || polOn ? "bg-up" : "border border-faint")} />
            {polBlocked ? "Blocking entries" : rulesOn || polOn ? "On" : "None"}
          </span>
        ) : (
          "–"
        )}
      </Cell>
    </div>
  );
}

// ── P&L ─────────────────────────────────────────────────────────────────────
function PnlPanel({ trades }: { trades: Trade[] }) {
  const { points, cum, wins, losses } = useMemo(() => {
    let c = 0,
      w = 0,
      l = 0;
    const pts: PnlPoint[] = [];
    trades
      .slice()
      .reverse()
      .forEach((x) => {
        if (x.action === "exit" && x.est_pnl_usd != null && isFinite(Number(x.est_pnl_usd))) {
          const v = Number(x.est_pnl_usd);
          c += v;
          if (v > 0) w++;
          else if (v < 0) l++;
          pts.push({ ts: String(x.ts || ""), cum: c });
        }
      });
    return { points: pts, cum: c, wins: w, losses: l };
  }, [trades]);

  const n = points.length;
  const rangeLabel = (ts: string, withTime: boolean) => {
    const d = new Date(ts);
    if (isNaN(d.getTime())) return "";
    return isToday(d) ? hhmm(d) : shortDate(d) + (withTime ? " " + hhmm(d) : "");
  };

  return (
    <Panel
      title="Estimated P&L"
      right={<span className="text-xs text-muted">exits in the recent log</span>}
    >
      <div className="flex flex-wrap items-end gap-x-8 gap-y-2 px-4 pb-1 pt-3">
        <div>
          <div className="text-xs text-muted">Realized, estimated</div>
          <div className={cn("mt-0.5 font-mono text-2xl font-medium tracking-[-0.02em]", n ? (cum >= 0 ? "text-up" : "text-down") : "text-faint")}>
            {n ? money(cum) : "$0.00"}
          </div>
        </div>
        <Stat k="Exits" v={String(n)} />
        <Stat k="Won / lost" v={n ? `${wins} / ${losses}` : "–"} />
        {n > 0 && <Stat k="Period" v={`${rangeLabel(points[0].ts, false)} → ${rangeLabel(points[n - 1].ts, true)}`} />}
      </div>
      {n >= 1 ? (
        <div className="px-2 pb-2">
          <PnlChart points={points} />
        </div>
      ) : (
        <div className="mx-4 mb-4 mt-3 grid h-[150px] place-items-center rounded border border-dashed border-line2 text-center text-xs text-muted">
          The curve starts after the agent's first exit.
        </div>
      )}
    </Panel>
  );
}

function Stat({ k, v }: { k: string; v: string }) {
  return (
    <div className="pb-1">
      <div className="text-xs text-muted">{k}</div>
      <div className="mt-0.5 font-mono text-sm text-fg2">{v}</div>
    </div>
  );
}

// ── positions ───────────────────────────────────────────────────────────────
function PositionsPanel({ positions }: { positions: Contract[] }) {
  return (
    <Panel title="Open positions" right={<Count n={positions.length} />}>
      <div className="overflow-x-auto">
        <table className="w-full border-collapse text-sm">
          <thead>
            <tr className="text-left text-xs text-muted">
              <Th>Symbol</Th>
              <Th>Type</Th>
              <Th>Expiry</Th>
              <Th className="text-right">Strike</Th>
            </tr>
          </thead>
          <tbody>
            {positions.length === 0 ? (
              <tr>
                <td colSpan={4} className="px-4 py-5 text-center text-xs text-muted">
                  No open positions
                </td>
              </tr>
            ) : (
              positions.map((p, i) => (
                <tr key={i} className="border-t border-line hover:bg-panel2">
                  {p.right ? (
                    <>
                      <Td className="font-semibold">{p.sym}</Td>
                      <Td>
                        <RightTag right={p.right} />
                      </Td>
                      <Td className="font-mono">{p.expiry ? shortDate(p.expiry) : p.expiryRaw}</Td>
                      <Td className="text-right font-mono">{p.strike}</Td>
                    </>
                  ) : (
                    <Td colSpan={4} className="font-semibold">
                      {p.raw}
                    </Td>
                  )}
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </Panel>
  );
}
function Th({ children, className }: { children: React.ReactNode; className?: string }) {
  return <th className={cn("whitespace-nowrap border-b border-line px-4 py-2 font-medium", className)}>{children}</th>;
}
function Td({ children, className, colSpan }: { children: React.ReactNode; className?: string; colSpan?: number }) {
  return (
    <td colSpan={colSpan} className={cn("whitespace-nowrap px-4 py-2", className)}>
      {children}
    </td>
  );
}

// ── activity blotter ────────────────────────────────────────────────────────
function rowOf(x: Trade) {
  const a = x.action || "";
  const c = parseContract(x.contract);
  const note = x.reason ? String(x.reason).slice(0, 160) + (x.hint ? ` · Fix: ${String(x.hint)}` : "") : "";
  let kind: "in" | "out" | "skip" = "skip",
    verb = "Skipped",
    detail = note;
  if (a === "entry") {
    kind = "in";
    verb = "Entered";
  } else if (a === "exit") {
    kind = "out";
    verb = "Exited";
  } else {
    detail = cap1(SKIPS[a] || a.replace(/_/g, " ")) + (note ? ` — ${note}` : "");
  }
  // real-money rows get an explicit LIVE tag — absence of DRY-RUN is not a signal.
  // Skips only ever show DRY-RUN (no order was involved).
  const dry = a === "entry" || a === "exit" ? x.dry : x.dry ? true : undefined;
  const pnl = a === "exit" && x.est_pnl_usd != null ? Number(x.est_pnl_usd) : null;
  return { kind, verb, c, detail, dry, pnl };
}

function KindIcon({ kind }: { kind: "in" | "out" | "skip" }) {
  const cls = {
    in: "bg-up/15 text-up",
    out: "bg-accent/15 text-accent",
    skip: "bg-raise text-muted",
  }[kind];
  const I = kind === "in" ? ArrowUpRight : kind === "out" ? ArrowDownRight : Minus;
  return (
    <span className={cn("grid h-5 w-5 flex-none place-items-center rounded-sm", cls)}>
      <I className="h-3 w-3" strokeWidth={2.4} />
    </span>
  );
}

function ContractCell({ c }: { c: Contract }) {
  if (!c.right) return <span className="font-semibold">{c.raw || "–"}</span>;
  return (
    <span className="inline-flex items-center gap-2 whitespace-nowrap">
      <b className="font-semibold text-fg">{c.sym}</b>
      <span className="font-mono text-fg2">
        {c.expiry ? shortDate(c.expiry) : ""} {c.strike}
      </span>
      <RightTag right={c.right} />
    </span>
  );
}

const BLOTTER_ROWS = 10;
function Blotter({ trades: all }: { trades: Trade[] }) {
  const [showAll, setShowAll] = useState(false);
  const trades = showAll ? all : all.slice(0, BLOTTER_ROWS);
  const rows = trades.map(rowOf);
  const more = all.length - BLOTTER_ROWS;
  return (
    <Panel title="Activity" right={<Count n={all.length} />}>
      {rows.length === 0 ? (
        <p className="m-0 px-4 py-6 text-center text-xs text-muted">Nothing yet. Signals and the agent's decisions show up here.</p>
      ) : (
        <>
          {/* wide screens: a blotter table */}
          <div className="overflow-x-auto max-md:hidden">
            <table className="w-full border-collapse text-sm">
              <thead>
                <tr className="text-left text-xs text-muted">
                  <Th>Time</Th>
                  <Th>Event</Th>
                  <Th>Contract</Th>
                  <Th>Mode</Th>
                  <Th>Detail</Th>
                  <Th className="text-right">Est. P&L</Th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r, i) => (
                  <tr key={i} className="border-t border-line align-top hover:bg-panel2">
                    <Td className="font-mono text-xs leading-5 text-muted">
                      <span title={trades[i].ts ? new Date(trades[i].ts!).toLocaleString() : ""}>{whenShort(trades[i].ts)}</span>
                    </Td>
                    <Td>
                      <span className="inline-flex items-center gap-2">
                        <KindIcon kind={r.kind} />
                        <span className={r.kind === "skip" ? "text-muted" : "text-fg"}>{r.verb}</span>
                      </span>
                    </Td>
                    <Td>
                      <ContractCell c={r.c} />
                    </Td>
                    <Td>
                      <ModeTag dry={r.dry} />
                    </Td>
                    <td className="min-w-[180px] max-w-[360px] px-4 py-2 text-xs leading-5 text-muted [overflow-wrap:anywhere]">{r.detail}</td>
                    <Td className={cn("text-right font-mono", r.pnl == null ? "text-faint" : r.pnl >= 0 ? "text-up" : "text-down")}>
                      {r.pnl == null ? "—" : money(r.pnl)}
                    </Td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {/* phones: one card per event */}
          <ul className="m-0 list-none p-0 md:hidden">
            {rows.map((r, i) => (
              <li key={i} className="grid grid-cols-[20px_minmax(0,1fr)_auto] gap-x-2.5 gap-y-1 border-t border-line px-3 py-2.5 first:border-t-0">
                <span className="row-span-2 pt-0.5">
                  <KindIcon kind={r.kind} />
                </span>
                <div className="min-w-0 text-sm text-fg2">
                  {r.verb} <ContractCell c={r.c} /> <ModeTag dry={r.dry} />
                </div>
                <span className={cn("font-mono text-sm", r.pnl == null ? "text-faint" : r.pnl >= 0 ? "text-up" : "text-down")}>
                  {r.pnl == null ? "—" : money(r.pnl)}
                </span>
                <div className="col-span-2 text-xs text-muted [overflow-wrap:anywhere]">
                  <span className="font-mono">{whenShort(trades[i].ts)}</span>
                  {r.detail ? ` · ${r.detail}` : ""}
                </div>
              </li>
            ))}
          </ul>
          {more > 0 && (
            <div className="border-t border-line px-4 py-2">
              <Button variant="ghost" size="sm" onClick={() => setShowAll((v) => !v)} aria-expanded={showAll}>
                {showAll ? "Show latest " + BLOTTER_ROWS : `Show all ${all.length} events`}
              </Button>
            </div>
          )}
        </>
      )}
    </Panel>
  );
}

// ── code-change proposal ────────────────────────────────────────────────────
function ProposalPanel({ p, onDone }: { p: Proposal; onDone: () => void }) {
  const [msg, setMsg] = useState("");
  const lines = String(p.diff || "").split("\n");
  return (
    <Panel title="Proposed code change: review before it applies" tone="warn" bodyClass="space-y-3 p-4">
      <p className="m-0 text-sm text-fg2">{p.explanation}</p>
      {!!p.warnings?.length && <p className="m-0 text-xs text-warn">{p.warnings.join("  ")}</p>}
      <pre
        tabIndex={0}
        aria-label="Proposed code diff"
        className="scroll-thin m-0 max-h-72 overflow-auto rounded border border-line bg-sink py-2 font-mono text-xs leading-[1.65]"
      >
        {lines.map((l, i) => {
          const c = l.charAt(0);
          const k = l.startsWith("+++") || l.startsWith("---") || c === "@" ? "h" : c === "+" ? "a" : c === "-" ? "d" : "";
          return (
            <span
              key={i}
              className={cn(
                "block min-w-max px-3",
                k === "a" && "bg-up/[0.08] text-[#8fe0b5]",
                k === "d" && "bg-down/[0.08] text-[#f3a3a1]",
                k === "h" && "text-muted",
                !k && "text-fg2",
              )}
            >
              {l || " "}
            </span>
          );
        })}
      </pre>
      <div className="flex flex-wrap items-center gap-2">
        <Button
          variant="primary"
          onClick={async () => {
            setMsg("Applying…");
            const r = await api<{ reply: string }>("/api/proposal/apply", {});
            setMsg(r.reply);
          }}
        >
          Apply and restart
        </Button>
        <Button
          onClick={async () => {
            await api("/api/proposal/reject", {});
            onDone();
          }}
        >
          Reject
        </Button>
        <span className="text-xs text-muted">Nothing changes until you apply.</span>
        {msg && (
          <span role="status" className="text-xs text-fg2">
            {msg}
          </span>
        )}
      </div>
    </Panel>
  );
}

// ── console ─────────────────────────────────────────────────────────────────
function ConsolePanel({ msgs, send }: { msgs: ChatMsg[]; send: (t: string) => void }) {
  const [text, setText] = useState("");
  const go = () => {
    const t = text.trim();
    if (!t) return;
    setText("");
    send(t);
  };
  return (
    <Panel title="Agent console" bodyClass="p-4 space-y-3">
      <div className="flex flex-wrap gap-1.5">
        {[
          ["status", "status"],
          ["set cap 3", "set cap 3"],
          ["set positions 5", "set positions 5"],
          ["rules", "my rules"],
          ["autostart", "start at login?"],
          ["open at login on", "open dashboard at login"],
          ["keep awake on", "keep awake"],
          ["why did you skip the last signal?", "why the last skip?"],
        ].map(([cmd, label]) => (
          <Button key={label} size="sm" className="font-mono text-fg2" onClick={() => send(cmd)}>
            {label}
          </Button>
        ))}
      </div>
      <ChatLog
        msgs={msgs}
        label="Conversation with your agent"
        className="h-[340px] max-sm:h-[240px]"
        empty={
          <>
            Ask anything, or give a command:{" "}
            {["pause", "set budget 500", "set percent 2", "set cap 3", "dry off", "autostart on", "stop"].map((c, i) => (
              <span key={c}>
                {i ? ", " : ""}
                <code className="rounded-sm bg-raise px-1 font-mono text-fg2">{c}</code>
              </span>
            ))}
            .
          </>
        }
      />
      <form
        className="flex gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          go();
        }}
      >
        <Input value={text} onChange={(e) => setText(e.target.value)} aria-label="Ask your agent or give a command" placeholder="Ask or command…" />
        <Button type="submit" variant="primary" className="h-9">
          Send
        </Button>
      </form>
      <p className="m-0 text-xs text-muted">Code changes: run Claude Code in the agent's folder on this computer, then restart the agent.</p>
    </Panel>
  );
}

// ── help ────────────────────────────────────────────────────────────────────
function HelpPanel({ onClose }: { onClose: () => void }) {
  const [msg, setMsg] = useState("");
  const [out, setOut] = useState<string | null>(null);
  const get = async (path: string, label: string) => {
    setMsg(label + "… (up to a minute)");
    setOut(null);
    try {
      const j = await (await fetch(path)).json();
      setOut(j.text || j.error || "no answer");
      setMsg("");
      return (j.text as string) || "";
    } catch {
      setMsg("Could not reach the agent on this computer. Is it still running?");
      return "";
    }
  };
  return (
    <Panel
      id="help"
      title="Help"
      right={
        <Button variant="ghost" size="sm" onClick={onClose}>
          Close
        </Button>
      }
      bodyClass="p-4 space-y-3"
    >
      <p className="m-0 text-sm text-fg2">Something not working? These two buttons usually find the cause without any typing.</p>
      <div className="flex flex-wrap items-center gap-2">
        <Button onClick={() => get("/api/help/netcheck", "Checking the connection")}>Check my connection</Button>
        <Button
          onClick={async () => {
            const t = await get("/api/help/report", "Building the report");
            if (!t) return;
            setMsg((await copyToClipboard(t)) ? "Copied. Paste it to support." : "Select the text below and copy it.");
          }}
        >
          Copy support report
        </Button>
        {msg && (
          <span role="status" className="text-xs text-muted">
            {msg}
          </span>
        )}
      </div>
      {out != null && (
        <pre tabIndex={0} aria-label="Help output" className="scroll-thin m-0 max-h-[22rem] overflow-auto whitespace-pre-wrap rounded border border-line bg-sink p-3 font-mono text-xs text-fg2">
          {out}
        </pre>
      )}
      <p className="m-0 text-xs text-muted">
        The report contains no keys, tokens, invoices or wallet IDs. They are removed before it's shown. Paste it to whoever helps
        you with the agent.
      </p>
    </Panel>
  );
}
