// One home per action, one place per fact: what each /v2 screen shows.
//
// Every control that changes something has exactly one home (ACTION_HOME):
// the band (on every screen) or one page. Other places may only link to that
// page, at most once per screen, from a note about the condition (or a
// panel's own "see all" link), and never with a copy of the control. Each
// condition is stated by one note per screen (notes() in derive.ts).
//
// The components render from these same functions, and
// webui_app/tests/guardian.test.mjs runs every fixture state through
// screenOf() and scans the sources for `data-act` / `data-go`, so a second
// copy of a control or a second note about a condition fails a test.
// Pure, no React: the Node tests load it as it is.
import type { Status } from "../../lib/viewModel";
import type { Go } from "./copy.ts";
import { bandModel, notes, statement, type Link, type NoteCode } from "./derive.ts";
import { etDayKey } from "./format.ts";

export type View = "today" | "holdings" | "activity" | "results" | "rules" | "signals" | "settings" | "more";
export const VIEWS: View[] = ["today", "holdings", "activity", "results", "rules", "signals", "settings", "more"];

/** Every control /v2 has that changes something, or leaves /v2. */
export type ActionId =
  // the band: on every screen
  | "pause"
  | "resume"
  | "to-practice"
  | "to-live"
  | "retry"
  | "update"
  // Rules & limits
  | "limits"
  | "size"
  | "filters"
  | "policy"
  // Signals & pass
  | "buy-days"
  // Settings
  | "broker"
  | "ai"
  | "alerts"
  | "autostart"
  | "open-at-login"
  | "keep-awake"
  | "move"
  | "start"
  | "stop"
  | "theme"
  | "dashboard"
  | "classic";

export type Home = "band" | View;

export const ACTION_HOME: Record<ActionId, Home> = {
  pause: "band",
  resume: "band",
  "to-practice": "band",
  "to-live": "band",
  retry: "band",
  update: "band",
  limits: "rules",
  size: "rules",
  filters: "rules",
  policy: "rules",
  "buy-days": "signals",
  broker: "settings",
  ai: "settings",
  alerts: "settings",
  autostart: "settings",
  "open-at-login": "settings",
  "keep-awake": "settings",
  move: "settings",
  start: "settings",
  stop: "settings",
  theme: "settings",
  dashboard: "settings",
  classic: "settings",
};

/** The source file each home's controls are written in (the source scan in
 *  the tests holds every `data-act` to it). */
export const HOME_FILE: Record<Home, string> = {
  band: "Band.tsx",
  today: "views/Today.tsx",
  holdings: "views/Pages.tsx",
  activity: "views/Activity.tsx",
  results: "views/Results.tsx",
  rules: "views/Rules.tsx",
  signals: "views/Signals.tsx",
  settings: "views/Settings.tsx",
  more: "views/Pages.tsx",
};

/** The pages a link inside a page's content can go to. */
export type LinkTo = Go | "activity" | "results";

export interface Screen {
  /** one entry per note shown: the conditions it states */
  conditions: NoteCode[][];
  /** the controls on this screen (band included) */
  controls: ActionId[];
  /** the in-page links to other pages (not the sidebar / tab bar) */
  links: LinkTo[];
}

function bandControls(s: Status, l: Link): ActionId[] {
  const a = bandModel(s, l).actions;
  const out: ActionId[] = [];
  if (a.retry) out.push("retry");
  if (a.pause) out.push("pause");
  if (a.resume) out.push("resume");
  if (a.toPractice) out.push("to-practice");
  if (a.toLive) out.push("to-live");
  return out;
}

/** The notes a page shows: Today all of them, Holdings the urgent ones. */
export function notesOn(view: View, s: Status, l: Link) {
  if (view === "today") return notes(s, l, "today");
  if (view === "holdings") return notes(s, l, "holdings").filter((n) => n.level === "crit");
  return [];
}

/** The band's "Start it in Settings" link, shown while no agent runs: not
 *  on Settings itself (the Start button is there), and not where a note on
 *  the screen links to Settings already. */
export function bandStartLink(view: View, s: Status, l: Link): boolean {
  if (!bandModel(s, l).actions.start || view === "settings") return false;
  return !notesOn(view, s, l).some((n) => n.go === "settings");
}

/** Today's "Change limits" link on its limits panel: only when nothing else
 *  on the screen links to Rules & limits already. */
export function todayLimitsLink(s: Status, l: Link): boolean {
  if (statement(s, l).empty) return false; // "Review my rules and limits" is there
  return !notesOn("today", s, l).some((n) => n.go === "rules");
}

/** Today's "All activity" link: on its "Today so far" panel, shown when
 *  something happened today. */
export function todayHasDay(s: Status, l: Link): boolean {
  if (statement(s, l).empty) return false;
  const today = etDayKey(s.as_of);
  return s.activity.rows.some((e) => etDayKey(e.at) === today);
}

export function screenOf(view: View, s: Status, l: Link): Screen {
  const ns = notesOn(view, s, l);
  const links: LinkTo[] = ns.flatMap((n) => (n.go ? [n.go] : []));
  if (bandStartLink(view, s, l)) links.push("settings");
  const controls = bandControls(s, l);
  if (view === "today") {
    if (s.positions.length) links.push("holdings");
    if (todayHasDay(s, l)) links.push("activity");
    links.push("results");
    if (statement(s, l).empty || todayLimitsLink(s, l)) links.push("rules");
  } else if (view === "rules") controls.push("limits", "size", "filters", "policy");
  else if (view === "signals") controls.push("buy-days");
  else if (view === "settings") {
    links.push("signals");
    controls.push("broker", "ai", "alerts", "autostart", "open-at-login", "keep-awake", "move", "theme", "dashboard", "classic");
    controls.push(s.agent.running ? "stop" : "start");
  }
  return { conditions: ns.map((n) => n.codes), controls, links };
}
