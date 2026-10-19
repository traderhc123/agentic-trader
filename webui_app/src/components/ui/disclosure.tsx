import { ChevronRight } from "lucide-react";
import type { ReactNode } from "react";

/** Native <details> — keyboard + screen-reader behaviour for free. */
export function Disclosure({
  summary,
  children,
  open,
  onToggle,
  id,
}: {
  summary: ReactNode;
  children: ReactNode;
  open?: boolean;
  onToggle?: (open: boolean) => void;
  id?: string;
}) {
  return (
    <details
      id={id}
      open={open}
      onToggle={(e) => onToggle?.((e.currentTarget as HTMLDetailsElement).open)}
      className="group rounded-md border border-line bg-panel"
    >
      <summary className="flex cursor-pointer list-none items-start gap-2 px-4 py-2.5 text-sm text-fg2 hover:text-fg max-sm:min-h-11 max-sm:items-center [&::-webkit-details-marker]:hidden group-open:border-b group-open:border-line">
        <ChevronRight className="mt-0.5 h-3.5 w-3.5 flex-none text-muted transition-transform group-open:rotate-90 max-sm:mt-0" />
        <span>{summary}</span>
      </summary>
      <div className="space-y-3 p-4">{children}</div>
    </details>
  );
}
