// From a Status to what the screen says. Pure functions, no React, no
// imports that exist at run time except ./format and ./copy, so the Node
// tests can run every fixture state through them.
//
// The honesty rules, in code:
//   - the mode band comes from `mode` only, the run-state block from `pause`
//     only (and from the link to the agent, which is a separate fact);
//   - "your real positions are still sold" is only ever said when
//     `real_positions_managed` is true;
//   - when the agent is not answering, or this page can't fetch, nothing is
//     presented as current: the mode is "unknown" and every panel is stamped
//     "as of" the last report.
import type { Activity, Book, Condition, Iso, Notice, Position, Status } from "../../lib/viewModel";
import { GUARD_STILL, UNMANAGED_FALLBACK, UNMANAGED_SHORT, UNMANAGED_WHY, answerWhy, describe, leftToday, nextSay, own, sizeEach, type Go, type IconName, type NextSay } from "./copy.ts";
import { v2Words } from "./settings.ts";
import { contractName, etDayKey, etTime, etWhen, dayLong, plural, span, secondsBetween, usd } from "./format.ts";

/** How this page is connected to the agent. Independent of mode and pause.
 *    ok       a fresh snapshot, and the agent's loop is answering
 *    silent   the server answers, but the agent's loop has gone quiet
 *    lost     this page's last fetches failed (the case the API can't report)
 *    stopped  the server answers and says no agent is running */
export type Link = "ok" | "silent" | "lost" | "stopped";

export function linkOf(s: Status, fetchFailing: boolean): Link {
  if (fetchFailing) return "lost";
  if (!s.agent.running) return "stopped";
  if (!s.agent.answering) return "silent";
  return "ok";
}

/** Not current: greyscale, dashed, stamped "as of". */
export const isStale = (l: Link) => l === "lost" || l === "silent";

/** The time of the last report the page can stand behind. */
export function asOf(s: Status, l: Link): Iso {
  return l === "silent" && s.agent.last_heartbeat ? s.agent.last_heartbeat : s.as_of;
}

const modeWord = (s: Status) => (s.mode === "live" ? "LIVE" : "Practice");
const brokerName = (s: Status) => s.broker.name || "your broker";
export function brokerLabel(s: Status): string {
  return s.broker.account_mask ? `${brokerName(s)} ····${s.broker.account_mask}` : brokerName(s);
}
const their = (n: number, one: string, many: string) => (n === 1 ? one : many);

export const realPositions = (s: Status) => s.positions.filter((p) => p.real);
export const practicePositions = (s: Status) => s.positions.filter((p) => !p.real);

/** The real positions the agent is NOT selling right now. When the status
 *  says "not managed" but names none, every real position counts. */
export function unwatched(s: Status): Position[] {
  if (s.real_positions_managed) return [];
  const rp = realPositions(s);
  const list = rp.filter((p) => p.next.action === "unmanaged" || p.next.action === "unknown");
  return list.length ? list : rp;
}

/** "Your 5 real positions are" / "1 of your 5 real positions is" */
function notSold(s: Status): string {
  const n = s.real_positions;
  const k = unwatched(s).length;
  return k >= n ? `Your ${plural(n, "real position")} ${their(n, "is", "are")}` : `${k} of your ${plural(n, "real position")} ${their(k, "is", "are")}`;
}

/** Dollars paid for a set of positions; null when any of them has no
 *  recorded price (never a partial sum presented as the whole). */
export function paidFor(list: Position[]): number | null {
  let sum = 0;
  for (const p of list) {
    if (p.entry_cost_usd == null) return null;
    sum += p.entry_cost_usd;
  }
  return sum;
}

// ── the band ────────────────────────────────────────────────────────────────

export interface Run {
  kind: "ok" | "paused" | "off";
  icon: IconName | null;
  text: string;
  /** dropped on the narrowest phones */
  long: string;
}

export interface BandModel {
  /** what the window frame and the band wear */
  frame: "live" | "practice" | "unknown";
  word: string;
  /** [bold lead, rest] */
  say: [string, string];
  run: Run;
  title: string;
  announce: string;
  actions: { pause: boolean; resume: boolean; toPractice: boolean; toLive: boolean; retry: boolean; start: boolean };
}

const RUN: Record<Status["pause"]["kind"], Run> = {
  none: { kind: "ok", icon: null, text: "Running", long: "" },
  entries: { kind: "paused", icon: "pause", text: "Paused", long: ": no new trades" },
  state_hold: { kind: "paused", icon: "pause", text: "On hold", long: ": everything stopped" },
  handoff_hold: { kind: "paused", icon: "pause", text: "Being moved", long: ": everything stopped" },
  restarting: { kind: "paused", icon: "pause", text: "Restarting", long: ": no orders for a moment" },
  stopped: { kind: "off", icon: "alert", text: "Stopped", long: "" },
};

export function bandModel(s: Status, l: Link): BandModel {
  const live = s.mode === "live";
  const n = s.real_positions;
  const none = { pause: false, resume: false, toPractice: false, toLive: false, retry: false, start: false };
  if (l === "lost" || l === "silent") {
    const run: Run = { kind: "off", icon: "alert", text: l === "lost" ? "No connection" : "Not answering", long: "" };
    return {
      frame: "unknown",
      word: "Mode unknown",
      // the run-state block says why ("No connection", "Not answering");
      // the Today statement says what that means: the band only the mode
      say: ["", `Its last report, at ${etTime(asOf(s, l))}, said ${modeWord(s)}.`],
      run,
      title: `${run.text} · Agentic Trader`,
      announce: `Mode unknown. ${l === "lost" ? "This window can't reach the agent." : "The agent is not answering."}`,
      actions: { ...none, retry: true },
    };
  }
  const run: Run = own(RUN, s.pause.kind) ?? { kind: "paused", icon: "pause", text: "Paused", long: "" };
  let say: [string, string];
  if (l === "stopped") {
    say = [live ? "Real money is the saved mode." : "Pretend money is the saved mode.", " It applies when the agent is started again."];
  } else if (live) {
    say = [
      "Real money.",
      ` New trades are real orders at ${brokerLabel(s)}${s.broker.paper_account ? " (the broker's paper-trading account)" : ""}.` +
        (s.mode_explicit ? "" : " The settings never chose a mode; the agent treats that as LIVE."),
    ];
  } else {
    let rest = " New trades are only recorded.";
    if (n && s.real_positions_managed)
      rest += ` Your ${plural(n, "real position")} ${their(n, "is", "are")} still sold for real on ${their(n, "its exit signal", "their exit signals")}.`;
    else if (n) rest += ` ${notSold(s)} NOT being sold by the agent right now.`;
    else rest += ` Nothing is sent to ${brokerName(s)}.`;
    say = ["Pretend money.", rest];
  }
  const answering = l === "ok";
  return {
    frame: s.mode,
    word: modeWord(s),
    say,
    run,
    title: `${modeWord(s)}${run.kind === "ok" ? "" : `, ${run.text.toLowerCase()}`} · Agentic Trader`,
    announce: `${modeWord(s)}. ${run.text}${run.long}.${s.broker.connected === false ? ` ${brokerName(s)} is not connected.` : ""}`,
    actions: {
      pause: answering && s.pause.kind === "none",
      resume: answering && s.pause.can_resume,
      toPractice: answering && live,
      toLive: answering && !live,
      retry: false,
      start: l === "stopped",
    },
  };
}

// ── vitals ──────────────────────────────────────────────────────────────────

export interface Vital {
  key: "exposure" | "broker" | "signals" | "market";
  look: "" | "bad" | "warn";
  icon: IconName | "sq-real" | "sq-none" | "beat";
  bold: string;
  text: string;
  /** the risk, in bold after the text ("They can't be sold right now.") */
  tail?: string;
  /** shown only on wide windows */
  wide?: string;
  /** the per-second counter: seconds since the last signal check at as_of */
  sinceCheck?: number | null;
  desk?: boolean;
}

export function vitals(s: Status, l: Link): Vital[] {
  const stale = isStale(l);
  const rp = realPositions(s);
  const n = s.real_positions;
  const paid = paidFor(rp);
  const paidText = paid != null ? `, about ${usd(paid, 0)} paid` : ", amount paid not known for all of them";
  let exposure: Vital;
  if (!n) exposure = { key: "exposure", look: "", icon: "sq-none", bold: "No real positions open", text: stale ? " (last report)" : "" };
  else {
    const risk = stale
      ? " They may be unwatched."
      : !s.real_positions_managed
        ? unwatched(s).length < n
          ? ` ${unwatched(s).length} of them ${their(unwatched(s).length, "is", "are")} not being watched.`
          : " " + (own(UNMANAGED_SHORT, s.unmanaged_reason) ?? UNMANAGED_FALLBACK)
        : "";
    exposure = {
      key: "exposure",
      look: risk ? "bad" : "",
      icon: risk ? "alert" : "sq-real",
      bold: `${plural(n, "real position")} open`,
      text: `${paidText}${stale ? " (last report)" : ""}.`,
      tail: risk.trim(),
    };
  }
  const b = s.broker;
  let broker: Vital;
  if (stale) broker = { key: "broker", look: "", icon: "q", bold: "", text: `${brokerName(s)}: unknown`, desk: true };
  else if (b.connected === false)
    broker = {
      key: "broker",
      look: "bad",
      icon: "alert",
      bold: `${brokerName(s)} not connected`,
      text: b.since ? ` since ${etTime(b.since)}` : "",
    };
  else if (b.connected == null)
    broker = { key: "broker", look: "", icon: "q", bold: "", text: `${brokerLabel(s)}: connection not reported`, desk: true };
  else if (s.limits.cash.low && b.buying_power != null)
    broker = { key: "broker", look: "warn", icon: "info", bold: `only ${usd(b.buying_power)} cash`, text: `${brokerName(s)} connected, ` };
  else broker = { key: "broker", look: "", icon: "check", bold: "", text: `${brokerLabel(s)} connected`, desk: true };

  const sig = s.signals;
  let signals: Vital;
  if (stale)
    signals = { key: "signals", look: "bad", icon: "alert", bold: l === "lost" ? "No connection" : "No answer", text: ` since ${etTime(asOf(s, l))}` };
  else if (!sig.reading) signals = { key: "signals", look: "", icon: "pause", bold: "", text: "Not checking for signals" };
  else if (!sig.last_poll) signals = { key: "signals", look: "", icon: "q", bold: "", text: "No signal check yet" };
  else
    signals = {
      key: "signals",
      look: sig.last_poll_ok === false ? "warn" : "",
      icon: sig.last_poll_ok === false ? "info" : "beat",
      bold: "",
      text: sig.last_poll_ok === false ? "Last signal check failed, " : "Checked for signals ",
      sinceCheck: secondsBetween(sig.last_poll, s.as_of),
    };
  const market = marketVital(s, s.as_of);
  // a frozen snapshot can't count down: say when it was true instead
  return [exposure, broker, signals, stale ? { ...market, text: `${market.text} (last report)`, wide: "" } : market];
}

/** What the agent will do with a position, as this page can stand behind
 *  it: when the page itself has lost the agent, the plan is not known. */
export function nextFor(p: Position, s: Status, l: Link): NextSay {
  if (l !== "lost") return nextSay(p, s);
  if (!p.real) return { text: "Unknown. This page can't reach the agent.", look: "unk", icon: "q" };
  return {
    text:
      "Unknown. Treat it as unwatched." +
      (p.expires_today ? ` It expires today: sell it yourself at ${brokerName(s)} before the close if you want it closed.` : ""),
    look: "bad",
    icon: "alert",
  };
}

export function marketVital(s: Status, now: Iso): Vital {
  const m = s.market;
  if (m.open) {
    const leftS = secondsBetween(now, m.closes_at);
    return {
      key: "market",
      look: "",
      icon: "clock",
      bold: "",
      text: `Market open until ${etTime(m.closes_at)}${m.early_close ? " (early close)" : ""}`,
      wide: leftS != null && leftS > 0 ? `, ${span(leftS)} left` : "",
      desk: true,
    };
  }
  return { key: "market", look: "", icon: "clock", bold: "", text: `Market closed. Opens ${etWhen(m.opens_at, etDayKey(now))}`, desk: true };
}

// ── needs-you notes ─────────────────────────────────────────────────────────
//
// One fact, one place: each condition is one note. When the agent's own
// message ("From your agent") is about a condition the dashboard has a note
// for, the two are one note in the dashboard's (tested) words, with the
// agent's message folded in under it; they are matched by the view-model's
// condition codes (Status.conditions), never by comparing text. A note links
// at most once to the page that owns what to do about it, and no two notes
// on a screen link to the same page.

/** A note's condition: the view-model's codes, plus "link_lost" (this page
 *  can't reach the agent: a fact only the page knows). */
export type NoteCode = Condition | "link_lost";

export interface Note {
  key: string;
  /** the conditions this note states; the first is its own */
  codes: NoteCode[];
  level: "crit" | "warn" | "info";
  icon: IconName;
  title: string;
  text: string;
  /** the agent's own messages about the same condition, in its words */
  agent: string[];
  /** the one page this note links to (the page that owns the action), or none */
  go: Go | null;
}

/** Where a condition is dealt with in /v2 (null: the band's own buttons, or
 *  nothing the owner can do here). */
export const CONDITION_GO: Record<NoteCode, Go | null> = {
  link_lost: null,
  agent_stopped: null,
  agent_not_answering: null,
  state_hold: null,
  handoff_hold: null,
  restarting: null,
  paused: null,
  settings_unreadable: "settings",
  ai_needed: "settings",
  broker_down: "settings",
  positions_unmanaged: "holdings",
  low_cash: "rules",
  daily_cap_reached: "rules",
  daily_loss_reached: "rules",
  position_cap_reached: "rules",
  signal_pass: "signals",
  feed_unreachable: "signals",
  records_unwritten: null,
  backup_failing: null,
  second_copy: null,
  position_expired: "holdings",
  position_expiring_unsold: "holdings",
  position_expiring: "holdings",
};

const LEVEL_OF: Record<Notice["level"], Note["level"]> = { urgent: "crit", warn: "warn", info: "info" };
const RANK: Record<Note["level"], number> = { info: 0, warn: 1, crit: 2 };

/** Every note for this state. `here`: the page they are shown on (a note
 *  never links to the page it is on). */
export function notes(s: Status, l: Link, here?: Go | "today" | "activity" | "results" | "more"): Note[] {
  const out: Note[] = [];
  const rp = realPositions(s);
  const n = s.real_positions;
  const b = brokerName(s);
  const expiring = rp.find((p) => p.expires_today);
  const expText = expiring ? ` ${contractName(expiring.contract, expiring.label)} expires today.` : "";
  const note = (x: Omit<Note, "agent" | "go"> & { go?: Go | null }): Note => ({ agent: [], ...x, go: x.go === undefined ? CONDITION_GO[x.codes[0]] : x.go });
  if (l === "lost" || l === "silent")
    out.push(
      note({
        key: "link",
        codes: [l === "lost" ? "link_lost" : "agent_not_answering"],
        level: "crit",
        icon: "alert",
        title: n ? `Nobody may be watching your ${plural(n, "real position")}` : "The agent may have stopped",
        text:
          (l === "lost"
            ? "This page has stopped getting answers. If the agent has stopped, it is not buying or selling anything."
            : "The server answers, but the agent's loop has gone quiet. While it is quiet it is not buying or selling anything.") +
          (n ? ` Look after the positions yourself at ${b} until it answers.` : "") +
          expText,
      }),
    );
  else if (l === "stopped" && n)
    out.push(
      note({
        key: "stopped",
        codes: ["agent_stopped"],
        level: "crit",
        icon: "alert",
        title: `Nobody is selling your ${plural(n, "real position")}`,
        text: `Exit signals and the expiry sell are not acted on while the agent is stopped. Start it again, or look after ${their(n, "the position", "the positions")} yourself at ${b}.${expText}`,
      }),
    );
  else if (l === "ok" && s.broker.connected === false)
    out.push(
      note({
        key: "broker",
        codes: ["broker_down", ...(n && !s.real_positions_managed ? (["positions_unmanaged"] as NoteCode[]) : [])],
        level: "crit",
        icon: "plug",
        title: `Connect ${b} again`,
        text:
          `Until it is connected, the agent can't place or sell any order there.` +
          (n ? ` Your ${plural(n, "real position")} ${their(n, "is", "are")} not being sold on exit signals.${expText}` : "") +
          (s.broker.last_error ? ` The broker's last answer: “${s.broker.last_error}”.` : ""),
      }),
    );
  else if (l === "ok" && n && !s.real_positions_managed) {
    const lost = unwatched(s);
    // with no exit signals the expiry sell still acts: not "nobody is selling it"
    const exp = lost.find(
      (p) => p.expires_today && !(p.next.expiry_guard_at && GUARD_STILL.includes(p.next.reason_code)),
    );
    const k = lost.length;
    out.push(
      note({
        key: "unmanaged",
        codes: ["positions_unmanaged", ...(exp ? (["position_expiring_unsold"] as NoteCode[]) : [])],
        level: "crit",
        icon: "alert",
        title: exp
          ? `${contractName(exp.contract, exp.label)} expires today and nobody is selling it`
          : `${notSold(s)} not being sold by the agent`,
        text:
          (own(UNMANAGED_WHY, s.unmanaged_reason) ?? 'Holdings says which, under "What the agent will do".') +
          (s.pause.can_resume ? " Press Resume to put the agent back on watch, or" : " Until that changes,") +
          ` look after ${their(k, "it", "them")} yourself at ${b}.` +
          (exp ? " Left alone, an expiring contract can be exercised or expire worthless." : ""),
      }),
    );
  }
  if (!isStale(l)) {
    if (!s.agent.settings_readable)
      out.push(
        note({
          key: "settings",
          codes: ["settings_unreadable"],
          level: "warn",
          icon: "info",
          title: "The settings file can't be read right now",
          text: "The values shown are the ones the agent is running on. Saving a setting is refused until the file is readable again.",
        }),
      );
    const c = s.limits.cash;
    if (c.low && s.broker.buying_power != null && s.broker.connected !== false) {
      const m = s.limits.trade_size.main;
      out.push(
        note({
          key: "cash",
          codes: ["low_cash"],
          level: "warn",
          icon: "info",
          title: `Add cash at ${b}, or lower your trade size`,
          text:
            (c.low_reason === "under_trade_size" && m.usd != null
              ? `${usd(s.broker.buying_power)} is less than one trade at your ${usd(m.usd, 0)} size, so real buy orders would be rejected.`
              : `${usd(s.broker.buying_power)} is too little for the agent to buy with, so real buy orders would be rejected.`) +
            " Selling is not affected." +
            (s.mode === "practice" ? " You are in practice mode, so nothing is blocked now." : ""),
        }),
      );
    }
    const p = s.signals.pass;
    // a last read of the feed that saw entry signals saw the exits too: the
    // agent is reading with some pass (a running wallet day-pass, say), so
    // "no signals" would be false whatever the prepaid pass says
    const reading = s.signals.entry_access === true;
    const lacking = p.status === "exits_only" || p.status === "none" || p.status === "waiting" || p.status === "unknown";
    if (l === "ok" && lacking && !reading && p.kind !== "not_needed") {
      const held = n ? ` Until then, watch your ${plural(n, "real position")} and sell ${their(n, "it", "them")} yourself at ${b}.` : "";
      out.push(
        note({
          key: "pass",
          codes: ["signal_pass"],
          level: "warn",
          icon: "info",
          title:
            p.status === "exits_only"
              ? "No signal days left for new trades"
              : p.status === "unknown"
                ? "Your signal pass could not be checked"
                : p.status === "waiting"
                  ? p.auto_pay
                    ? "No signal pass for today yet"
                    : "No usable signal pass"
                  : "No signal pass",
          text:
            p.status === "exits_only"
              ? "The agent still receives exit signals for what it holds, so open positions are still sold. It takes no new trades until you add signal days."
              : p.status === "unknown"
                ? `The agent could not check your signal pass with AgentHC (the check failed, or has not run yet). Until it can, it reads no signals: no new trades, and no exit signals for what it holds.${held}`
                : p.status === "waiting" && p.auto_pay
                  ? "Your wallet's auto-pay buys a day-pass on the agent's next read of the feed on a market day. Until it has one, the agent receives no signals: no new trades, and no exit signals for what it holds."
                  : p.status === "waiting"
                    ? `Your signal pass is used up, expired or waiting for payment, so the agent receives no signals: no new trades, and no exit signals for what it holds.${held}`
                    : `Without a pass the agent receives no signals: no new trades, and no exit signals for what it holds.${held}`,
        }),
      );
    }
  }

  // the agent's own messages: folded into the note about the same condition,
  // else one note per condition, else (about nothing the view-model knows)
  // as they are, each text once
  const used = new Set<number>();
  for (const c of s.conditions ?? []) {
    const idx = c.notices.filter((i) => i >= 0 && i < s.notices.length && !used.has(i));
    if (!idx.length) continue;
    idx.forEach((i) => used.add(i));
    const texts = [...new Set(idx.map((i) => v2Words(s.notices[i].text)))];
    const lvl = LEVEL_OF[c.level] ?? "info";
    const home = out.find((x) => x.codes.includes(c.code));
    if (home) {
      for (const t of texts) if (!home.agent.includes(t)) home.agent.push(t);
      if (RANK[lvl] > RANK[home.level]) home.level = lvl;
      continue;
    }
    out.push(
      note({
        key: `cond-${c.code}`,
        codes: [c.code],
        level: lvl,
        icon: lvl === "crit" ? "alert" : "info",
        title: lvl === "crit" ? "From your agent: needs you" : "From your agent",
        text: texts[0],
      }),
    );
    out[out.length - 1].agent.push(...texts.slice(1));
  }
  const seen = new Set<string>();
  for (const [i, x] of s.notices.entries()) {
    if (used.has(i)) continue;
    const text = v2Words(x.text);
    if (seen.has(text)) continue;
    seen.add(text);
    out.push({
      key: `notice-${i}`,
      codes: [],
      level: LEVEL_OF[x.level] ?? "info",
      icon: x.level === "urgent" ? "alert" : "info",
      title: x.level === "urgent" ? "From your agent: needs you" : "From your agent",
      text,
      agent: [],
      go: null,
    });
  }
  // one link per page on a screen, never to the page the notes are on (nor
  // to Holdings from Today, whose "Holding now" panel is right there)
  const linked = new Set<Go>();
  for (const x of out) {
    if (x.go && (x.go === here || linked.has(x.go) || (here === "today" && x.go === "holdings"))) x.go = null;
    if (x.go) linked.add(x.go);
  }
  return out;
}

// ── the one-sentence state ──────────────────────────────────────────────────

export interface Statement {
  h: string;
  lead: string;
  /** first run: nothing has happened yet */
  empty: boolean;
}

export function statement(s: Status, l: Link): Statement {
  const n = s.real_positions;
  const b = brokerName(s);
  const live = s.mode === "live";
  const o = s.capabilities.opening;
  const empty = s.activity.total === 0 && s.positions.length === 0;
  const sells = "It sells what it holds when exit signals arrive.";
  if (l === "lost")
    return {
      h: "This window can't reach your agent.",
      lead: `It may still be running, or it may have stopped. Until it answers, ${n ? `treat your ${plural(n, "real position")} as unwatched` : "nothing here is current"}. Everything below is its last report, from ${etTime(s.as_of)}.`,
      empty: false,
    };
  if (l === "silent")
    return {
      h: "Your agent is not answering.",
      lead: `Its loop has not reported since ${etTime(asOf(s, l))}. Until it does, ${n ? `treat your ${plural(n, "real position")} as unwatched` : "nothing here is current"}. Everything below is its last report.`,
      empty: false,
    };
  if (l === "stopped")
    return {
      h: "Your agent is stopped. Nothing is being bought, sold or watched.",
      lead: n
        ? `Your ${plural(n, "real position")} ${their(n, "stays", "stay")} open at ${b}, and nobody is selling ${their(n, "it", "them")} on exit signals. When started, the agent comes back in ${modeWord(s)} mode.`
        : `When started, the agent comes back in ${modeWord(s)} mode.`,
      empty: false,
    };
  switch (s.pause.kind) {
    case "state_hold":
      return {
        h: "The agent is on hold. Nothing is being bought, sold or watched.",
        lead: "Its memory file could not be read when it started, so it put itself on hold rather than trade from a damaged record. Press Resume to carry on from the memory it has; it will not sell positions that are missing from the list below, so close those at your broker yourself.",
        empty: false,
      };
    case "handoff_hold":
      return {
        h: "This setup is being moved. The agent here places no orders.",
        lead: "Two copies would place every order twice, so this one waits. If the move fails, the agent carries on here by itself.",
        empty: false,
      };
    case "restarting":
      return {
        h: "The agent is restarting. No orders for a moment.",
        lead: "An update or a move is about to restart it. It reads no signals and places no orders until it is back.",
        empty: false,
      };
  }
  if (s.broker.connected === false && (live || n))
    return {
      h: `Your agent can't reach ${b}, so it can't buy or sell${live ? "" : " for real"}.`,
      lead: "It is still running and still receiving signals. Connect the broker again and it goes straight back on watch.",
      empty: false,
    };
  if (s.pause.kind === "entries")
    return {
      h: n && !s.real_positions_managed ? "New trades are paused." : "New trades are paused. It still sells what you hold.",
      lead:
        n && s.real_positions_managed
          ? `Your ${plural(n, "real position")} will be sold when ${their(n, "its exit signal arrives", "their exit signals arrive")}. Press Resume to allow new trades again.`
          : n
            ? // in practice the band says it already (it states what happens to real positions)
              `${live ? `${notSold(s)} not being sold by the agent right now. ` : ""}Press Resume to allow new trades again.`
            : "No real position is open. Press Resume to allow new trades again.",
      empty: false,
    };
  if (empty)
    return {
      h: "All set. Your agent is waiting for its first signal.",
      lead: live
        ? "It is LIVE, so the first signal that passes your limits becomes a real order."
        : "It is in practice mode, so nothing it does can spend your money.",
      empty: true,
    };
  if (live) {
    const left = leftToday(s);
    const held = s.real_positions_managed ? sells : `${notSold(s)} not being sold by the agent right now.`;
    const lead =
      o.state !== "yes"
        ? `${answerWhy("opening", o, s)}${n ? " " + held : ""}`
        : s.limits.cash.low && s.broker.buying_power != null
          ? `There is only ${usd(s.broker.buying_power)} cash at ${b}, so buy orders would be rejected until you add more. ${held}`
          : `It can open ${left} more ${left === 1 ? "trade" : "trades"} today${sizeEach(s)}. ${held}`;
    return { h: "Your agent is trading with real money, inside the limits you set.", lead, empty: false };
  }
  return {
    h: "Your agent is practicing. New trades are pretend and can't spend your money.",
    lead:
      n && s.real_positions_managed
        ? `It is still looking after ${plural(n, "real position")} bought while LIVE, and sells ${their(n, "it", "each one")} for real when its exit signal arrives.`
        : n
          ? // the band says which are not being sold; this says what to do
            `Holdings says which real ${their(unwatched(s).length, "position is", "positions are")} not being sold; look after ${their(unwatched(s).length, "it", "them")} at ${b}.`
          : o.state === "practice_only"
            ? "It records what it would have bought and sold, so you can see how your rules behave before using real money."
            : answerWhy("opening", o, s),
    empty: false,
  };
}

// ── a book's day ────────────────────────────────────────────────────────────

export interface BookToday {
  /** trades closed today with an estimate */
  closed: number;
  /** their estimated result; null when none was measured */
  pnl: number | null;
  /** sales today with no estimate: a sale, never "nothing closed" */
  unmeasured: number;
}

export function bookToday(B: Book, today: string): BookToday {
  const t = B.days.find((d) => d.day === today);
  const unmeasured = B.unmeasured_today ?? 0;
  return { closed: t ? t.closed : 0, pnl: t ? t.pnl : null, unmeasured };
}

/** What the "Real money" book holds. Sales in a broker's paper-trading
 *  account are booked apart (results.paper_account) once each line records
 *  its account; older lines can't be told apart, and the caption says so
 *  whenever the agent trades a paper account. */
export function realBookCaption(s: Status): string {
  if (!s.broker.paper_account) return `Orders filled at ${brokerLabel(s)}`;
  return `Orders filled at ${brokerName(s)}. Sales in its paper-trading account, the one the agent trades now, are counted apart; older lines that did not record the account are counted here and may include paper ones.`;
}
export const PAPER_BOOK_CAPTION = "Orders filled in a broker's paper-trading account. Not real money.";

/** Whether the broker paper-account book is shown: when the agent trades a
 *  paper account now, or that book holds sales. */
export const showPaperBook = (s: Status) =>
  s.broker.paper_account || s.results.paper_account.closed > 0 || s.results.paper_account.unmeasured > 0;

export interface DayClosed {
  label: string;
  word: string;
  day: BookToday;
}

/** "Today so far": what the Opened count is called, and the Closed lines.
 *  Practice mode reads the practice book. LIVE reads the book its account
 *  books sales into (a broker's paper-trading account has its own), plus
 *  the other one when it has sales today (the account was switched). The
 *  Opened count is the agent's own (its limits count paper fills as live). */
export function dayBooks(s: Status): { opened: string; closed: DayClosed[] } {
  const today = etDayKey(s.as_of);
  if (s.mode !== "live")
    return { opened: "practice trade", closed: [{ label: "Closed", word: "practice trade", day: bookToday(s.results.practice, today) }] };
  const paper = s.broker.paper_account;
  const real = { word: "real-money trade", B: s.results.real, other: "Closed for real money" };
  const pap = { word: "paper-account trade", B: s.results.paper_account, other: "Closed in a paper account" };
  const [main, second] = paper ? [pap, real] : [real, pap];
  const closed: DayClosed[] = [{ label: "Closed", word: main.word, day: bookToday(main.B, today) }];
  const o = bookToday(second.B, today);
  if (o.closed || o.unmeasured) closed.push({ label: second.other, word: second.word, day: o });
  return { opened: paper ? "live trade" : "real-money trade", closed };
}

/** The line under a book's "Today" figure. */
export function bookTodayNote(x: BookToday): string {
  const um = x.unmeasured ? `${plural(x.unmeasured, "sale")} not measured` : "";
  if (!x.closed) return um ? `${um} (no estimate)` : "nothing closed today";
  return `${plural(x.closed, "trade")} closed${um ? `, plus ${um}` : ""}`;
}

// ── today, from the rows the page has ───────────────────────────────────────

export interface DayTally {
  /** the loaded rows reach back before today, so the counts are complete */
  complete: boolean;
  stopped: { label: string; n: number }[];
  stoppedTotal: number;
  problems: number;
}

/** What today's Activity rows add up to. `rows`: newest first, every row
 *  the page has loaded. */
export function dayTally(rows: Activity[], today: string, hasOlder: boolean, broker: string | null): DayTally {
  const by = new Map<string, number>();
  let problems = 0;
  let stoppedTotal = 0;
  let sawOlder = false;
  for (const e of rows) {
    const day = etDayKey(e.at);
    if (day !== today) {
      if (day && day < today) sawOlder = true;
      continue;
    }
    const d = describe(e, broker);
    if (d.cat === "limit") {
      stoppedTotal++;
      by.set(d.group, (by.get(d.group) ?? 0) + 1);
    } else if (d.cat === "problem") problems++;
  }
  return {
    complete: sawOlder || !hasOlder,
    stopped: [...by.entries()].map(([label, n]) => ({ label, n })),
    stoppedTotal,
    problems,
  };
}

export { dayLong };
