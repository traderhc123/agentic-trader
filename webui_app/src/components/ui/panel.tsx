import type { ReactNode } from "react";
import { cn } from "../../lib/cn";

export function Panel({
  title,
  right,
  children,
  className,
  bodyClass,
  tone,
  id,
}: {
  title?: ReactNode;
  right?: ReactNode;
  children: ReactNode;
  className?: string;
  bodyClass?: string;
  tone?: "warn";
  id?: string;
}) {
  return (
    <section
      id={id}
      className={cn(
        "min-w-0 rounded-md border bg-panel",
        tone === "warn" ? "border-warn/45" : "border-line",
        className,
      )}
    >
      {title != null && (
        <header
          className={cn(
            "flex min-h-10 items-center gap-2 border-b px-4 py-2",
            tone === "warn" ? "border-warn/45 bg-warn/10" : "border-line",
          )}
        >
          <h2 className={cn("m-0 text-sm font-semibold", tone === "warn" ? "text-warn" : "text-fg")}>{title}</h2>
          <span className="flex-1" />
          {right}
        </header>
      )}
      <div className={bodyClass}>{children}</div>
    </section>
  );
}

export function Count({ n }: { n: number }) {
  if (!n) return null;
  return <span className="rounded-sm bg-raise px-1.5 py-0.5 font-mono text-2xs text-muted">{n}</span>;
}
