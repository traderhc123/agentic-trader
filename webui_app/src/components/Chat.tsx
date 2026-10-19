import { useEffect, useRef, type ReactNode } from "react";
import { cn } from "../lib/cn";

export interface ChatMsg {
  who: "me" | "agent";
  text: string;
  pending?: boolean;
  id?: number; // set on a pending reply so it can be filled in later
}

let seq = 0;
export const nextMsgId = () => ++seq;

/** Console-style log: your lines prefixed ›, the agent's replies indented. */
export function ChatLog({
  msgs,
  empty,
  className,
  label,
}: {
  msgs: ChatMsg[];
  empty?: ReactNode;
  className?: string;
  label: string;
}) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (ref.current) ref.current.scrollTop = ref.current.scrollHeight;
  }, [msgs]);
  return (
    <div
      ref={ref}
      role="log"
      aria-live="polite"
      aria-label={label}
      tabIndex={0}
      className={cn("scroll-thin overflow-y-auto rounded border border-line bg-sink p-3 font-mono text-xs leading-5", className)}
    >
      {msgs.length === 0 ? (
        <div className="font-sans text-xs leading-relaxed text-muted">{empty}</div>
      ) : (
        <div className="space-y-2.5">
          {msgs.map((m, i) =>
            m.who === "me" ? (
              <div key={i} className="flex gap-2 text-fg">
                <span className="select-none text-accent" aria-hidden="true">
                  ›
                </span>
                <span className="sr-only">You:</span>
                <span className="min-w-0 whitespace-pre-wrap [overflow-wrap:anywhere]">{m.text}</span>
              </div>
            ) : (
              <div key={i} className={cn("ml-4 border-l border-line2 pl-3 font-sans text-[13px] leading-relaxed", m.pending ? "text-muted" : "text-fg2")}>
                <span className="sr-only">Agent:</span>
                <span className="whitespace-pre-wrap [overflow-wrap:anywhere]">{m.text}</span>
              </div>
            ),
          )}
        </div>
      )}
    </div>
  );
}
