import { Dialog } from "radix-ui";
import { useRef, type ReactNode } from "react";

/** A phone sheet from the bottom (or the drawer from the left): closed by the shade, Esc or its own button. Focus
 * goes back to the control that opened it (Radix would look for a trigger it has none of). */
export function Sheet({ open, onClose, side = "bottom", label, children }: { open: boolean; onClose: () => void; side?: "bottom" | "left"; label: string; children: ReactNode }) {
  const opener = useRef<HTMLElement | null>(null);
  return (
    <Dialog.Root open={open} onOpenChange={(o) => !o && onClose()}>
      <Dialog.Portal>
        <Dialog.Overlay className="shade" />
        <Dialog.Content
          className={side === "left" ? "drawer" : "sheet"}
          aria-describedby={undefined}
          onOpenAutoFocus={() => (opener.current = document.activeElement as HTMLElement | null)}
          onCloseAutoFocus={(e) => {
            e.preventDefault();
            opener.current?.focus();
          }}
        >
          <Dialog.Title className="sr-only">{label}</Dialog.Title>
          {children}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
