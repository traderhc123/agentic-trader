import { cn } from "../../lib/cn";

export function RightTag({ right }: { right: "CALL" | "PUT" | null }) {
  if (!right) return null;
  return (
    <span
      className={cn(
        "inline-block rounded-sm px-1.5 font-mono text-[10px] font-semibold leading-4",
        right === "CALL" ? "bg-up/15 text-up" : "bg-down/15 text-down",
      )}
    >
      {right}
    </span>
  );
}

export function ModeTag({ dry }: { dry?: boolean }) {
  if (dry === undefined || dry === null) return null;
  return (
    <span
      className={cn(
        "inline-block rounded-sm px-1.5 font-mono text-[10px] font-semibold leading-4",
        dry ? "bg-accent/15 text-accent" : "bg-live/15 text-live",
      )}
    >
      {dry ? "DRY-RUN" : "LIVE"}
    </span>
  );
}

/** Inline status line: green for ok, red for errors, muted otherwise. */
export function Msg({ m }: { m?: { kind: "ok" | "err" | "info"; text: string } | null }) {
  if (!m || !m.text) return null;
  return (
    <span
      role="status"
      aria-live="polite"
      className={cn(
        "min-w-0 whitespace-pre-line text-xs [overflow-wrap:anywhere]",
        m.kind === "ok" ? "text-up" : m.kind === "err" ? "text-down" : "text-muted",
      )}
    >
      {m.text}
    </span>
  );
}
export type MsgT = { kind: "ok" | "err" | "info"; text: string } | null;
export const ok = (text: string): MsgT => ({ kind: "ok", text });
export const err = (text: string): MsgT => ({ kind: "err", text });
export const info = (text: string): MsgT => ({ kind: "info", text });
