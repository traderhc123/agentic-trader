import * as AD from "@radix-ui/react-alert-dialog";
import type { ReactNode } from "react";
import { Button } from "./button";

/** Confirmation for real-money actions — never a single (mis-)tap. */
export function ConfirmDialog({
  open,
  onOpenChange,
  title,
  children,
  confirmLabel,
  onConfirm,
  tone = "live",
}: {
  open: boolean;
  onOpenChange: (o: boolean) => void;
  title: string;
  children: ReactNode;
  confirmLabel: string;
  onConfirm: () => void;
  tone?: "live" | "primary";
}) {
  return (
    <AD.Root open={open} onOpenChange={onOpenChange}>
      <AD.Portal>
        <AD.Overlay className="fixed inset-0 z-40 bg-black/60 backdrop-blur-[2px]" />
        <AD.Content className="fixed left-1/2 top-1/2 z-50 w-[calc(100vw-32px)] max-w-md -translate-x-1/2 -translate-y-1/2 rounded-lg border border-line2 bg-panel shadow-2xl shadow-black/50">
          <div className="border-b border-line px-5 py-4">
            <AD.Title className="m-0 text-base font-semibold text-fg">{title}</AD.Title>
          </div>
          <AD.Description asChild>
            <div className="px-5 py-4 text-sm leading-relaxed text-fg2">{children}</div>
          </AD.Description>
          <div className="flex justify-end gap-2 rounded-b-lg border-t border-line bg-panel2 px-5 py-3">
            <AD.Cancel asChild>
              <Button>Cancel</Button>
            </AD.Cancel>
            <AD.Action asChild>
              <Button variant={tone} onClick={onConfirm}>
                {confirmLabel}
              </Button>
            </AD.Action>
          </div>
        </AD.Content>
      </AD.Portal>
    </AD.Root>
  );
}
