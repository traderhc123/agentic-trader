import type { ReactNode } from "react";
import { Logo } from "./ui/logo";

/** Sticky top bar. `rail` is the 3px mode band across the very top:
 *  blue = dry-run, red = LIVE, amber = paused, red-dim = agent down. */
export function AppBar({
  page,
  rail,
  children,
}: {
  page: string;
  rail?: "dry" | "live" | "paused" | "off" | "none";
  children?: ReactNode;
}) {
  const railCls =
    rail === "live"
      ? "bg-live"
      : rail === "paused"
        ? "bg-warn"
        : rail === "off"
          ? "bg-down/60"
          : rail === "dry"
            ? "bg-accent"
            : "bg-line2";
  return (
    <header className="sticky top-0 z-30 border-b border-line bg-bg/90 backdrop-blur-md max-sm:static">
      <div className={`h-[3px] w-full ${railCls}`} aria-hidden="true" />
      <div className="mx-auto flex min-h-12 max-w-[1440px] flex-wrap items-center gap-x-3 gap-y-2 px-6 py-1.5 max-sm:px-4 max-sm:py-2.5">
        <a href="/dash" className="flex min-w-0 items-center gap-2.5 text-fg no-underline">
          <Logo />
          <span className="whitespace-nowrap text-[15px] font-semibold tracking-[-0.01em]">Agentic Trader</span>
          <span className="text-faint max-md:hidden">/</span>
          <span className="whitespace-nowrap text-fg2 max-md:hidden">{page}</span>
        </a>
        {children}
      </div>
    </header>
  );
}

export function Pill({
  tone = "neutral",
  dot,
  children,
  title,
  className = "",
}: {
  tone?: "neutral" | "dry" | "live" | "warn" | "bad" | "up";
  dot?: boolean;
  children: ReactNode;
  title?: string;
  className?: string;
}) {
  const t = {
    neutral: "border-line2 text-fg2",
    dry: "border-accent/45 bg-accent/10 text-accent",
    live: "border-live/50 bg-live/10 text-live",
    warn: "border-warn/45 bg-warn/10 text-warn",
    bad: "border-down/45 bg-down/10 text-down",
    up: "border-line2 text-fg2",
  }[tone];
  return (
    <span
      title={title}
      className={`inline-flex h-6 items-center gap-1.5 whitespace-nowrap rounded border px-2 text-xs font-medium ${t} ${className}`}
    >
      {dot && (
        <span
          className={`h-1.5 w-1.5 flex-none rounded-full ${tone === "up" ? "bg-up shadow-[0_0_0_3px_rgb(34_197_142/0.2)]" : "bg-current"}`}
        />
      )}
      {children}
    </span>
  );
}
