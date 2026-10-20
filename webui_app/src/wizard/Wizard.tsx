import { ArrowRight, Check, Lock } from "lucide-react";
import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { AppBar, Pill } from "../components/AppBar";
import { ChatLog, nextMsgId, type ChatMsg } from "../components/Chat";
import { UpdateBox } from "../components/UpdateBox";
import { Button, LinkButton } from "../components/ui/button";
import { Input } from "../components/ui/field";
import { Panel } from "../components/ui/panel";
import { api, type WizardState } from "../lib/api";
import { cn } from "../lib/cn";
import { pluralFix } from "../lib/format";
import { MoveImport } from "./MoveImport";
import { SourceStep } from "./SourceStep";
import { BrokerStep, ConsentStep, LaunchStep, LlmStep, SafetyStep, SizingStep } from "./steps";

// "Your AI" is optional and sits near the end: trading never needs it (the
// built-in rules in Safety are plain settings). It adds chat, Q&A and
// plain-English rules for those who want them.
export const STEPS = [
  ["consent", "Agreement"],
  ["source", "Signal source"],
  ["broker", "Broker"],
  ["sizing", "Sizing"],
  ["safety", "Safety & rules"],
  ["llm", "AI (optional)"],
  ["done", "Launch"],
] as const;
export type StepId = (typeof STEPS)[number][0];
const idx = (id: StepId) => STEPS.findIndex((s) => s[0] === id);

interface Ctx {
  st: WizardState | null;
  reloadState: () => Promise<WizardState | null>;
  done: (id: StepId) => void;
  isDone: (id: StepId) => boolean;
  goBack: () => void;
  goNext: () => void;
  canNext: boolean;
  next: string | null; // setup's "start from a terminal" text after Safety
  setNext: (s: string) => void;
}
const WizCtx = createContext<Ctx>(null as unknown as Ctx);
export const useWiz = () => useContext(WizCtx);

export function Wizard() {
  const [st, setSt] = useState<WizardState | null>(null);
  const [doneSet, setDoneSet] = useState<Set<StepId>>(new Set());
  const [cur, setCur] = useState(0);
  const [maxUnlocked, setMaxUnlocked] = useState(0);
  const [next, setNext] = useState<string | null>(null);
  const booted = useRef(false);
  const focusPending = useRef(false);

  const reloadState = useCallback(async () => {
    try {
      const s = await api<WizardState>("/api/state");
      setSt(s);
      return s;
    } catch {
      return null;
    }
  }, []);

  // boot: mark what's already configured, open on the first unfinished step
  useEffect(() => {
    reloadState().then((s) => {
      if (!s) return;
      const d = new Set<StepId>();
      if (s.consent) d.add("consent");
      // optional step: once Safety is saved, skipping it counts as done
      if (s.llm || s.safety) d.add("llm");
      if (s.source) d.add("source");
      if (s.broker) d.add("broker");
      if (s.sizing) d.add("sizing");
      if (s.safety) d.add("safety");
      let mu = 0;
      d.forEach((id) => (mu = Math.max(mu, Math.min(idx(id) + 1, STEPS.length - 1))));
      setDoneSet(d);
      setMaxUnlocked(mu);
      setCur(Math.min(mu, STEPS.length - 1));
      booted.current = true;
    });
  }, [reloadState]);

  // The button that had focus just disappeared with its step — move focus to
  // the new step's heading so keyboard + screen-reader users follow.
  useEffect(() => {
    if (!focusPending.current) return;
    focusPending.current = false;
    const h = document.getElementById("step-heading-" + STEPS[cur][0]);
    if (h) {
      try {
        h.focus({ preventScroll: true });
      } catch {
        h.focus();
      }
    }
    const m = document.getElementById("wz-main");
    if (m && m.getBoundingClientRect().top < 0) m.scrollIntoView({ block: "start" });
  }, [cur]);

  const goStep = (i: number) => {
    if (i >= 0 && i <= maxUnlocked && i < STEPS.length) {
      focusPending.current = true;
      setCur(i);
    }
  };

  // auto-advance only the FIRST time a step completes — re-saving an
  // already-done step must not yank the user off the step they chose
  const done = (id: StepId) => {
    const first = !doneSet.has(id);
    setDoneSet((s) => new Set(s).add(id));
    setMaxUnlocked((m) => Math.max(m, Math.min(idx(id) + 1, STEPS.length - 1)));
    if (first) {
      focusPending.current = booted.current;
      setCur(Math.min(idx(id) + 1, STEPS.length - 1));
    }
    reloadState();
  };

  const ctx: Ctx = {
    st,
    reloadState,
    done,
    isDone: (id) => doneSet.has(id),
    goBack: () => goStep(cur - 1),
    goNext: () => goStep(cur + 1),
    canNext: cur + 1 <= maxUnlocked,
    next,
    setNext,
  };

  const id = STEPS[cur][0];

  return (
    <WizCtx.Provider value={ctx}>
      <AppBar page="Setup" rail="none">
        <span className="flex-1" />
        <Pill dot tone="up" className="max-sm:hidden" title="This page is served by your agent on this computer only">
          127.0.0.1 · local only
        </Pill>
        <LinkButton href="/dash" size="sm">
          Dashboard
          <ArrowRight className="h-3.5 w-3.5" />
        </LinkButton>
      </AppBar>

      <main className="mx-auto max-w-[1200px] px-6 pb-12 pt-7 max-sm:px-4 max-sm:pt-5">
        <div className="mb-6 max-w-[70ch]">
          <h1 className="m-0 text-2xl font-semibold tracking-[-0.02em]">Set up your trading agent</h1>
          <p className="mb-0 mt-2 text-base text-fg2">
            Everything runs on your own machine. This page is served from 127.0.0.1 by your agent itself. It moves real money, so
            read each step carefully.
          </p>
        </div>

        <div className="grid grid-cols-[232px_minmax(0,1fr)] items-start gap-6 max-lg:grid-cols-1 max-lg:gap-4">
          <aside className="sticky top-[72px] max-lg:static max-lg:contents">
            <Stepper cur={cur} maxUnlocked={maxUnlocked} doneSet={doneSet} goStep={goStep} />
            <div className="mt-4 rounded-md border border-line p-3 max-lg:order-3 max-lg:mt-0">
              <UpdateBox />
            </div>
          </aside>

          <div id="wz-main" className="min-w-0 space-y-4 max-lg:order-2">
            {!st ? (
              <div className="h-64 animate-pulse rounded-md border border-line bg-panel" />
            ) : (
              <>
                {/* New computer: bring a whole setup over instead of redoing it.
                    Works with the agent running too (it pauses and restarts). */}
                <MoveImport running={!!st.running} />
                {/* Every step stays mounted (hidden when not current), like the
                    old page: a Robinhood approval wait, a cloud deploy, an invoice
                    or half-filled form keeps going while you look at another step. */}
                <div hidden={id !== "consent"}><ConsentStep /></div>
                <div hidden={id !== "llm"}><LlmStep /></div>
                <div hidden={id !== "source"}><SourceStep /></div>
                <div hidden={id !== "broker"}><BrokerStep /></div>
                <div hidden={id !== "sizing"}><SizingStep /></div>
                <div hidden={id !== "safety"}><SafetyStep /></div>
                <div hidden={id !== "done"}><LaunchStep /></div>
              </>
            )}
            <AssistPanel />
          </div>
        </div>
      </main>
    </WizCtx.Provider>
  );
}

// ── stepper ─────────────────────────────────────────────────────────────────
function Stepper({
  cur,
  maxUnlocked,
  doneSet,
  goStep,
}: {
  cur: number;
  maxUnlocked: number;
  doneSet: Set<StepId>;
  goStep: (i: number) => void;
}) {
  return (
    <>
      {/* desktop: vertical list with a progress spine */}
      <nav aria-label="Setup steps" className="max-lg:hidden">
        <ol className="relative m-0 list-none p-0">
          {STEPS.map(([sid, label], i) => {
            const d = doneSet.has(sid);
            const locked = i > maxUnlocked;
            const isCur = i === cur;
            return (
              <li key={sid} className="relative">
                {i < STEPS.length - 1 && (
                  <span
                    aria-hidden="true"
                    className={cn("absolute left-[21px] top-[30px] h-[calc(100%-18px)] w-px", d ? "bg-up/40" : "bg-line2")}
                  />
                )}
                <button
                  type="button"
                  disabled={locked}
                  aria-current={isCur ? "step" : undefined}
                  title={locked ? "Locked: finish the earlier steps first" : undefined}
                  onClick={() => goStep(i)}
                  className={cn(
                    "relative flex h-11 w-full items-center gap-3 rounded px-2.5 text-left text-sm transition-colors",
                    isCur ? "bg-panel2 font-medium text-fg" : locked ? "cursor-default text-faint" : "text-fg2 hover:bg-panel2",
                  )}
                >
                  <span
                    className={cn(
                      "relative z-10 grid h-[22px] w-[22px] flex-none place-items-center rounded-full border font-mono text-[11px] font-semibold",
                      isCur
                        ? "border-accent bg-accent text-accent-ink"
                        : d
                          ? "border-up/40 bg-[rgb(14_40_38)] text-up"
                          : locked
                            ? "border-line2 bg-bg text-faint"
                            : "border-line3 bg-bg text-muted",
                    )}
                  >
                    {d && !isCur ? <Check className="h-3 w-3" strokeWidth={3} /> : locked ? <Lock className="h-2.5 w-2.5" /> : i + 1}
                  </span>
                  <span className="truncate">{label}</span>
                  {d && <span className="sr-only">(done)</span>}
                </button>
              </li>
            );
          })}
        </ol>
      </nav>

      {/* tablet/phone: "Step 3 of 7 · Signal source" + segmented bar */}
      <div className="lg:hidden max-lg:order-1">
        <div className="flex items-center gap-3 text-base font-semibold">
          <span>
            Step {cur + 1} of {STEPS.length} · {STEPS[cur][1]}
          </span>
          {cur + 1 < STEPS.length &&
            (cur + 1 <= maxUnlocked ? (
              <Button variant="ghost" size="sm" className="ml-auto text-accent" onClick={() => goStep(cur + 1)}>
                Next: {STEPS[cur + 1][1]}
                <ArrowRight className="h-3.5 w-3.5" />
              </Button>
            ) : (
              <span className="ml-auto text-xs font-normal text-muted">Next: {STEPS[cur + 1][1]}</span>
            ))}
        </div>
        <div role="group" aria-label="Jump to step" className="mt-2.5 grid grid-cols-7 gap-1">
          {STEPS.map(([sid, label], i) => {
            const d = doneSet.has(sid);
            return (
              <button
                key={sid}
                type="button"
                disabled={i > maxUnlocked}
                aria-current={i === cur ? "step" : undefined}
                aria-label={`Step ${i + 1}: ${label}${d ? " (done)" : ""}${i > maxUnlocked ? " (locked)" : ""}`}
                onClick={() => goStep(i)}
                className="group h-10 disabled:cursor-default"
              >
                <span
                  className={cn(
                    "block h-1 rounded-full transition-all group-enabled:group-hover:h-1.5",
                    i === cur ? "bg-accent" : d ? "bg-up" : "bg-line2",
                  )}
                />
              </button>
            );
          })}
        </div>
      </div>
    </>
  );
}

// ── shared step chrome ──────────────────────────────────────────────────────
export function StepCard({
  id,
  title,
  restored,
  children,
  footer,
}: {
  id: StepId;
  title: string;
  restored?: string;
  children: ReactNode;
  footer: ReactNode;
}) {
  const i = idx(id);
  return (
    <section className="min-w-0 rounded-md border border-line bg-panel">
      <header className="border-b border-line px-6 pb-4 pt-5 max-sm:px-4 max-sm:pt-4">
        <div className="mb-1 font-mono text-xs text-muted max-lg:hidden">
          Step {i + 1} of {STEPS.length}
        </div>
        <h2 id={"step-heading-" + id} tabIndex={-1} className="m-0 text-lg font-semibold tracking-[-0.01em] focus:outline-none">
          {title}
        </h2>
      </header>
      {restored && (
        <p className="m-0 flex items-center gap-2 border-b border-line bg-up/[0.08] px-6 py-2.5 text-sm text-up max-sm:px-4">
          <Check className="h-4 w-4 flex-none" />
          {pluralFix(restored)}
        </p>
      )}
      <div className="divide-y divide-line">{children}</div>
      <footer className="flex flex-wrap items-center gap-3 rounded-b-md border-t border-line bg-panel2 px-6 py-3.5 max-sm:px-4 max-sm:[&_.btn-main]:flex-1">
        {footer}
      </footer>
    </section>
  );
}

/** One block inside a step card. With `label`, a two-column form row. */
export function Block({ label, desc, children, className }: { label?: string; desc?: string; children: ReactNode; className?: string }) {
  if (!label) return <div className={cn("space-y-3 px-6 py-5 max-sm:px-4 max-sm:py-4", className)}>{children}</div>;
  return (
    <div className={cn("grid grid-cols-[200px_minmax(0,1fr)] gap-6 px-6 py-5 max-md:grid-cols-1 max-md:gap-3 max-sm:px-4 max-sm:py-4", className)}>
      <div>
        <div className="text-sm font-semibold text-fg">{label}</div>
        {desc && <p className="m-0 mt-1 text-xs leading-relaxed text-muted">{desc}</p>}
      </div>
      <div className="min-w-0 space-y-3">{children}</div>
    </div>
  );
}

export function Notice({ children, tone = "warn", role }: { children: ReactNode; tone?: "warn" | "live"; role?: string }) {
  return (
    <div
      role={role}
      className={cn(
        "rounded border border-line2 border-l-2 bg-panel px-4 py-3 text-sm leading-relaxed text-fg2 [&_b]:text-fg",
        tone === "live" ? "border-l-live" : "border-l-warn",
      )}
    >
      {children}
    </div>
  );
}

// ── setup assistant ─────────────────────────────────────────────────────────
function AssistPanel() {
  const [msgs, setMsgs] = useState<ChatMsg[]>([]);
  const [q, setQ] = useState("");
  const ask = async () => {
    const t = q.trim();
    if (!t) return;
    setQ("");
    const id = nextMsgId();
    setMsgs((m) => [...m, { who: "me", text: t }, { who: "agent", text: "…", pending: true, id }]);
    let reply = "";
    try {
      reply = (await api<{ reply: string }>("/api/ask", { question: t })).reply;
    } catch {
      reply = "Could not reach the setup server on this computer.";
    }
    setMsgs((m) => m.map((x) => (x.id === id ? { who: "agent", text: reply } : x)));
  };
  return (
    <Panel title="Ask your agent" right={<span className="text-xs text-muted">uses your connected AI</span>} bodyClass="space-y-3 p-4">
      <p className="m-0 text-xs text-muted">Need help with this step? Ask your agent. Works once you connect an AI in the optional AI step (any provider).</p>
      {msgs.length > 0 && <ChatLog msgs={msgs} label="Conversation with your agent" className="h-44 max-sm:h-56" />}
      <form
        className="flex gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          ask();
        }}
      >
        <Input value={q} onChange={(e) => setQ(e.target.value)} aria-label="Ask your agent a question" placeholder="e.g. what is dry-run?" />
        <Button type="submit" variant="primary" className="h-9">
          Ask
        </Button>
      </form>
    </Panel>
  );
}
