// Small building blocks of the /v2 dashboard: icons, money tags, the "as of"
// stamp, and the one modal behaviour dialogs and the drawer share.
import { useEffect, useRef, type ReactNode, type RefObject } from "react";
import { TAG_WORDS, type TagKind } from "./lib/copy";
import { etTime } from "./lib/format";
import type { Iso } from "../lib/viewModel";

// Drawn icons (24x24, stroked). Kept inline: nothing is loaded at run time.
const PATHS = {
  shield: ["M12 3l7 3v5c0 4.6-2.9 8.3-7 10-4.1-1.7-7-5.4-7-10V6l7-3z", "M9 12l2.2 2.2L15.2 10"],
  pause: ["M9 5v14M15 5v14"],
  play: ["M7 5l12 7-12 7V5z"],
  alert: ["M12 4l9 16H3l9-16z", "M12 10v4.5M12 17.2v.1"],
  info: ["M12 3a9 9 0 100 18 9 9 0 000-18z", "M12 11v5.5M12 7.6v.1"],
  check: ["M5 12.5l4.5 4.5L19 7.5"],
  dash: ["M6 12h12"],
  q: ["M9.6 9.6a2.5 2.5 0 114 2c-.9.7-1.6 1.2-1.6 2.4M12 17.2v.1"],
  x: ["M6 6l12 12M18 6L6 18"],
  chev: ["M9 6l6 6-6 6"],
  clock: ["M12 3a9 9 0 100 18 9 9 0 000-18z", "M12 7v5l3.5 2"],
  sun: [
    "M12 8a4 4 0 100 8 4 4 0 000-8z",
    "M12 2.5v2M12 19.5v2M2.5 12h2M19.5 12h2M5.3 5.3l1.4 1.4M17.3 17.3l1.4 1.4M5.3 18.7l1.4-1.4M17.3 6.7l1.4-1.4",
  ],
  moon: ["M20 14.5A8 8 0 019.5 4a8 8 0 1010.5 10.5z"],
  in: ["M4 13v6h16v-6", "M12 3v11M8 10l4 4 4-4"],
  out: ["M4 13v6h16v-6", "M12 15V4M8 8l4-4 4 4"],
  skip: ["M12 3.5a8.5 8.5 0 100 17 8.5 8.5 0 000-17z", "M6.2 17.8L17.8 6.2"],
  note: ["M6 4h9l4 4v12H6V4z", "M9 12h7M9 16h5"],
  today: ["M4 11l8-7 8 7v8a1 1 0 01-1 1h-4v-6h-6v6H5a1 1 0 01-1-1v-8z"],
  hold: ["M6 8h12a2 2 0 012 2v7a2 2 0 01-2 2H6a2 2 0 01-2-2v-7a2 2 0 012-2z", "M9 8V6a2 2 0 012-2h2a2 2 0 012 2v2"],
  list: ["M8 7h12M8 12h12M8 17h12M4 7h.1M4 12h.1M4 17h.1"],
  chart: ["M4 20h16M4 15l5-5 4 3 7-8"],
  rules: ["M4 7h10M18 7h2M4 17h2M10 17h10", "M16 5a2 2 0 100 4 2 2 0 000-4z", "M8 15a2 2 0 100 4 2 2 0 000-4z"],
  signal: ["M5 18a10 10 0 010-12M19 6a10 10 0 010 12M8.5 15a5 5 0 010-6M15.5 9a5 5 0 010 6", "M12 11a1 1 0 100 2 1 1 0 000-2z"],
  gear: [
    "M12 9a3 3 0 100 6 3 3 0 000-6z",
    "M12 3v3M12 18v3M3 12h3M18 12h3M5.6 5.6l2.1 2.1M16.3 16.3l2.1 2.1M5.6 18.4l2.1-2.1M16.3 7.7l2.1-2.1",
  ],
  more: ["M5 12h.1M12 12h.1M19 12h.1"],
  plug: ["M9 3v5M15 3v5M6 8h12v3a6 6 0 01-12 0V8zM12 17v4"],
  back: ["M15 6l-6 6 6 6"],
} as const;
export type IconId = keyof typeof PATHS;

export function Icon({ name, small, className = "" }: { name: IconId; small?: boolean; className?: string }) {
  return (
    <svg
      className={`ic ${small ? "ic-s" : ""} ${className}`}
      viewBox="0 0 24 24"
      aria-hidden="true"
      focusable="false"
      style={name === "more" ? { strokeWidth: 3 } : undefined}
    >
      {name === "q" && <circle cx="12" cy="12" r="9" strokeDasharray="3 3" />}
      {PATHS[name].map((d, i) => (
        <path key={i} d={d} />
      ))}
    </svg>
  );
}

const TAG_CLASS: Record<TagKind, string> = {
  real: "tag-real",
  prac: "tag-prac",
  none: "tag-none",
  nobuy: "tag-none",
  unk: "tag-unk",
};

/** Solid = real money, dashed = practice, hollow = no order, dotted = unknown. */
export function Tag({ kind }: { kind: TagKind }) {
  return <span className={`tag ${TAG_CLASS[kind]}`}>{TAG_WORDS[kind]}</span>;
}

/** "as of 2:41 PM ET": shown on every panel whose content is a last report. */
export function Stamp({ at }: { at: Iso | null }) {
  if (!at) return null;
  return (
    <span className="stamp">
      <Icon name="clock" />
      as of {etTime(at)}
    </span>
  );
}

export function ExpiresFlag() {
  return (
    <span className="flag">
      <Icon name="clock" />
      Expires today
    </span>
  );
}

const FOCUSABLE =
  'button:not([disabled]),a[href],input:not([disabled]),select:not([disabled]),textarea:not([disabled]),summary,[tabindex="0"]';

/** Modal behaviour for a dialog or the drawer: focus moves in (to the
 *  element marked data-autofocus, else the first control), Tab stays inside,
 *  Escape closes, and focus goes back to whatever opened it. */
export function useModal(open: boolean, box: RefObject<HTMLElement>, onClose: () => void) {
  const close = useRef(onClose);
  close.current = onClose;
  useEffect(() => {
    if (!open) return;
    const opener = document.activeElement as HTMLElement | null;
    const el = box.current;
    const first = el?.querySelector<HTMLElement>("[data-autofocus]") ?? el?.querySelector<HTMLElement>(FOCUSABLE);
    first?.focus({ preventScroll: true });
    const shell = document.getElementById("shell");
    shell?.setAttribute("inert", "");
    const overflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        close.current();
        return;
      }
      if (e.key !== "Tab" || !el) return;
      const f = [...el.querySelectorAll<HTMLElement>(FOCUSABLE)].filter((x) => x.offsetParent !== null || x === document.activeElement);
      if (!f.length) {
        e.preventDefault();
        return;
      }
      const a = f[0];
      const z = f[f.length - 1];
      const cur = document.activeElement;
      if (e.shiftKey && (cur === a || !el.contains(cur))) {
        z.focus();
        e.preventDefault();
      } else if (!e.shiftKey && (cur === z || !el.contains(cur))) {
        a.focus();
        e.preventDefault();
      }
    };
    document.addEventListener("keydown", onKey, true);
    return () => {
      document.removeEventListener("keydown", onKey, true);
      document.body.style.overflow = overflow;
      shell?.removeAttribute("inert");
      if (opener && document.contains(opener)) opener.focus({ preventScroll: true });
      else document.getElementById("main")?.focus({ preventScroll: true });
    };
  }, [open, box]);
}

/** A confirm dialog in the design's shape: strip (orange for LIVE, hatched
 *  for practice), title, lead, body, then the safe button first in focus. */
export function Dialog({
  tone,
  title,
  lead,
  children,
  foot,
  onClose,
}: {
  tone?: "live" | "prac";
  title: string;
  lead?: ReactNode;
  children?: ReactNode;
  foot: ReactNode;
  onClose: () => void;
}) {
  const box = useRef<HTMLDivElement>(null);
  useModal(true, box, onClose);
  return (
    <div
      className="scrim open"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div
        ref={box}
        className={`dialog ${tone ? `dialog-${tone}` : ""}`}
        role="alertdialog"
        aria-modal="true"
        aria-labelledby="dlg-t"
        aria-describedby={lead ? "dlg-lead" : undefined}
      >
        {tone && <div className="dialog-strip" />}
        <div className="dialog-top">
          <h2 id="dlg-t">{title}</h2>
          {lead && <p id="dlg-lead">{lead}</p>}
        </div>
        <div className="dialog-body">{children}</div>
        <div className="dialog-foot">{foot}</div>
      </div>
    </div>
  );
}

export function Drawer({
  title,
  sub,
  children,
  onClose,
}: {
  title: string;
  sub?: ReactNode;
  children: ReactNode;
  onClose: () => void;
}) {
  const box = useRef<HTMLElement>(null);
  useModal(true, box, onClose);
  return (
    <>
      <div className="drawer-scrim open" onMouseDown={onClose} />
      <aside ref={box} className="drawer open" role="dialog" aria-modal="true" aria-labelledby="drawer-t">
        <div className="drawer-h">
          <h2 id="drawer-t">{title}</h2>
          <button className="drawer-x" onClick={onClose} aria-label="Close" data-autofocus>
            <Icon name="x" />
          </button>
          {sub && <p>{sub}</p>}
        </div>
        <div className="drawer-b">{children}</div>
      </aside>
    </>
  );
}

/** "The agent will" / "The agent will not": will-not is a calm dash, never a red cross. */
export function WillWont({ will, wont }: { will: string[]; wont: string[] }) {
  return (
    <div className="wwn">
      <div>
        <h4>The agent will</h4>
        <ul>
          {will.map((x) => (
            <li key={x}>
              <Icon name="check" small />
              <span>{x}</span>
            </li>
          ))}
        </ul>
      </div>
      <div>
        <h4>The agent will not</h4>
        <ul>
          {wont.map((x) => (
            <li key={x}>
              <Icon name="dash" small />
              <span>{x}</span>
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}
