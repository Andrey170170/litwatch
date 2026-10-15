import { Dialog } from "radix-ui";
import type { ReactNode } from "react";

/** A phone sheet from the bottom (or the drawer from the left): closed by the shade, Esc or its own button. */
export function Sheet({ open, onClose, side = "bottom", label, children }: { open: boolean; onClose: () => void; side?: "bottom" | "left"; label: string; children: ReactNode }) {
  return (
    <Dialog.Root open={open} onOpenChange={(o) => !o && onClose()}>
      <Dialog.Portal>
        <Dialog.Overlay className="shade" />
        <Dialog.Content className={side === "left" ? "drawer" : "sheet"} aria-describedby={undefined}>
          <Dialog.Title className="sr-only">{label}</Dialog.Title>
          {children}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
