import { Dialog } from "radix-ui";
import { useEffect, useRef, type ReactNode, type RefObject } from "react";
import { axisOf, motionMs, sheetCloses, Tracker } from "@/lib/gesture";
import { clearDrag, EASE, sample, settle, swallowNextClick, trackPointer } from "./gestures";

const CLASS = { bottom: "sheet", left: "drawer", right: "over" };

type Props = {
  open: boolean;
  onClose: () => void;
  side?: keyof typeof CLASS;
  label: string;
  children: ReactNode;
  /** The drawer while the shell's edge swipe holds it (`usePhoneGestures`): it mounts shut, without its entry. */
  drag?: "opening" | "shutting" | null;
  panelRef?: RefObject<HTMLElement | null>;
  shadeRef?: RefObject<HTMLElement | null>;
};

/** A phone sheet from the bottom, the drawer from the left, or the desktop's slide-over from the right: closed by
 * the shade, Esc or its own button; the sheet also by dragging its handle down (DESIGN.md, "Sheets"). Focus goes
 * back to the control that opened it (Radix would look for a trigger it has none of). */
export function Sheet({ open, onClose, side = "bottom", label, children, drag, panelRef, shadeRef }: Props) {
  const opener = useRef<HTMLElement | null>(null);
  const panel = useRef<HTMLDivElement | null>(null);
  const shade = useRef<HTMLDivElement | null>(null);
  useSheetDrag(side === "bottom" ? panel : null, shade, onClose);
  const set = <T extends HTMLElement>(own: RefObject<T | null>, out?: RefObject<HTMLElement | null>) => (el: T | null) => {
    own.current = el;
    if (out) out.current = el;
  };
  return (
    <Dialog.Root open={open} onOpenChange={(o) => !o && onClose()}>
      <Dialog.Portal>
        <Dialog.Overlay ref={set(shade, shadeRef)} className="shade" data-drag={drag ?? undefined} />
        <Dialog.Content
          ref={set(panel, panelRef)}
          className={CLASS[side]}
          data-drag={drag ?? undefined}
          aria-describedby={undefined}
          onOpenAutoFocus={() => (opener.current = document.activeElement as HTMLElement | null)}
          onCloseAutoFocus={(e) => {
            e.preventDefault();
            opener.current?.focus();
          }}
        >
          {side === "bottom" && <div className="hdl" aria-hidden />}
          <Dialog.Title className="sr-only">{label}</Dialog.Title>
          {children}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

/** Drag a sheet down to close it: from its handle, or from its body while that is scrolled to the top. It follows
 * the finger and its shade fades; past a third of its height, or flicked, it goes, else it springs back. */
function useSheetDrag(panel: RefObject<HTMLElement | null> | null, shade: RefObject<HTMLElement | null>, onClose: () => void) {
  const close = useRef(onClose);
  close.current = onClose;
  useEffect(() => {
    if (!panel) return;
    const down = (e: PointerEvent) => {
      const el = panel.current;
      if (!el || e.button !== 0 || !e.isPrimary || !el.contains(e.target as Node)) return;
      const fromHandle = (e.target as Element).closest(".hdl");
      if (!fromHandle && el.scrollTop > 0) return;
      const tracker = new Tracker();
      tracker.push(sample(e));
      let axis: ReturnType<typeof axisOf> = null;
      const height = () => el.offsetHeight || 300;
      trackPointer(e, {
        move: (m) => {
          tracker.push(sample(m));
          const dy = m.clientY - e.clientY;
          if (!axis) {
            axis = axisOf(m.clientX - e.clientX, dy);
            if (!axis) return;
            if (axis === "x" || dy < 0) return (axis = "x");
            el.style.transition = "none";
          }
          el.style.transform = `translateY(${Math.max(0, dy)}px)`;
          if (shade.current) shade.current.style.opacity = String(1 - Math.max(0, dy) / height());
        },
        end: (u, cancelled) => {
          if (axis !== "y") return;
          swallowNextClick();
          const dy = Math.max(0, u.clientY - e.clientY);
          const goes = !cancelled && sheetCloses(dy, tracker.velocity().vy, height());
          const ms = motionMs(goes ? 160 : 200);
          if (shade.current) {
            shade.current.style.transition = `opacity ${ms}ms ${EASE}`;
            shade.current.style.opacity = goes ? "0" : "1";
          }
          settle(el, { transform: goes ? "translateY(100%)" : "translateY(0)" }, ms, () => {
            if (goes) close.current();
            else clearDrag(el, shade.current);
          });
        },
      });
    };
    document.addEventListener("pointerdown", down);
    return () => document.removeEventListener("pointerdown", down);
  }, [panel, shade]);
}
