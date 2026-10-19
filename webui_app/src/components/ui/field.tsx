import { forwardRef, type InputHTMLAttributes, type ReactNode, type SelectHTMLAttributes, type TextareaHTMLAttributes } from "react";
import { cn } from "../../lib/cn";

const base =
  "w-full rounded border border-line3 bg-sink px-2.5 text-base text-fg transition-colors " +
  "focus:border-accent focus:outline-none focus:ring-1 focus:ring-accent aria-[invalid=true]:border-down";

export const Input = forwardRef<HTMLInputElement, InputHTMLAttributes<HTMLInputElement>>(({ className, ...p }, ref) => (
  <input ref={ref} className={cn(base, "h-9 max-sm:h-11", className)} {...p} />
));
Input.displayName = "Input";

export const Textarea = forwardRef<HTMLTextAreaElement, TextareaHTMLAttributes<HTMLTextAreaElement>>(
  ({ className, ...p }, ref) => <textarea ref={ref} className={cn(base, "py-2 leading-relaxed", className)} {...p} />,
);
Textarea.displayName = "Textarea";

export const Select = forwardRef<HTMLSelectElement, SelectHTMLAttributes<HTMLSelectElement>>(
  ({ className, ...p }, ref) => <select ref={ref} className={cn(base, "at-select h-9 max-sm:h-11", className)} {...p} />,
);
Select.displayName = "Select";

export function Field({
  label,
  htmlFor,
  help,
  children,
  className,
}: {
  label: ReactNode;
  htmlFor?: string;
  help?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("min-w-0", className)}>
      <label htmlFor={htmlFor} className="mb-1.5 block text-xs font-medium text-fg2">
        {label}
      </label>
      {children}
      {help && <p className="mt-1.5 text-xs leading-relaxed text-muted">{help}</p>}
    </div>
  );
}

/** A labelled radio/checkbox row with a bordered hit area. */
export function ChoiceRow({
  type,
  checked,
  onChange,
  name,
  value,
  id,
  children,
  tone = "accent",
}: {
  type: "radio" | "checkbox";
  checked: boolean;
  onChange: (checked: boolean) => void;
  name?: string;
  value?: string;
  id?: string;
  children: ReactNode;
  tone?: "accent" | "live";
}) {
  return (
    <label
      className={cn(
        "flex cursor-pointer items-start gap-3 rounded border bg-sink px-3 py-2.5 text-sm leading-relaxed text-fg2 transition-colors hover:border-line3",
        checked ? (tone === "live" ? "border-live/50 bg-live/[0.06]" : "border-accent/50 bg-accent/[0.05]") : "border-line2",
      )}
    >
      <input
        type={type}
        id={id}
        name={name}
        value={value}
        checked={checked}
        onChange={(e) => onChange(e.target.checked)}
        className="mt-[3px] h-4 w-4 flex-none"
      />
      <span className="min-w-0">{children}</span>
    </label>
  );
}
