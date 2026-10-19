import { forwardRef, type ButtonHTMLAttributes } from "react";
import { cn } from "../../lib/cn";

type Variant = "default" | "primary" | "warn" | "live" | "ghost";
type Size = "sm" | "md" | "lg";

const V: Record<Variant, string> = {
  default: "bg-panel2 border-line2 text-fg hover:bg-raise hover:border-line3",
  primary: "bg-accent border-accent text-accent-ink font-semibold hover:bg-accent-hi hover:border-accent-hi",
  warn: "bg-warn/10 border-warn/45 text-warn hover:bg-warn/20 hover:border-warn",
  live: "bg-live/10 border-live/50 text-live hover:bg-live/20 hover:border-live",
  ghost: "bg-transparent border-transparent text-fg2 hover:bg-panel2 hover:text-fg hover:border-line",
};
const S: Record<Size, string> = {
  sm: "h-7 px-2.5 text-xs gap-1.5 max-sm:min-h-10",
  md: "h-8 px-3 text-sm gap-2 max-sm:min-h-11",
  lg: "h-10 px-4 text-sm gap-2 max-sm:min-h-11",
};

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant;
  size?: Size;
}

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(
  ({ variant = "default", size = "md", className, type = "button", ...p }, ref) => (
    <button
      ref={ref}
      type={type}
      className={cn(
        "inline-flex items-center justify-center whitespace-nowrap rounded border font-medium transition-colors",
        "disabled:cursor-not-allowed disabled:opacity-45",
        V[variant],
        S[size],
        className,
      )}
      {...p}
    />
  ),
);
Button.displayName = "Button";

export function LinkButton({
  variant = "default",
  size = "md",
  className,
  ...p
}: React.AnchorHTMLAttributes<HTMLAnchorElement> & { variant?: Variant; size?: Size }) {
  return (
    <a
      className={cn(
        "inline-flex items-center justify-center whitespace-nowrap rounded border font-medium no-underline transition-colors",
        V[variant],
        S[size],
        className,
      )}
      {...p}
    />
  );
}
