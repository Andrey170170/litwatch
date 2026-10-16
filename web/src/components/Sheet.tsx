import { Dialog } from "radix-ui";
import { useEffect, useRef, type ReactNode, type RefObject } from "react";
import { axisOf, motionMs, sheetCloses, Tracker } from "@/lib/gesture";
import { clearDrag, EASE, finishEntry, sample, settle, swallowNextClick, trackPointer, type DrawerDrag } from "./gestures";

const CLASS = { bottom: "sheet", left: "drawer", right: "over" };

type Props = {
  open: boolean;
  onClose: () => void;
  side?: keyof typeof CLASS;
  label: string;
  children: ReactNode;
  /** The drawer under the shell's edge swipe (`usePhoneGestures`): it mounts shut, without its entry, and keeps
   * that entry off once the swipe has left it open. */
  drag?: DrawerDrag;
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
  // the drag listens only while open: a sheet kept mounted and closed (Filters) lets go of a pointer it followed
  useSheetDrag(open && side === "bottom" ? panel : null, shade, onClose);
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

/** Whether the sheet, or a list of its own inside it (Calibrate's picker), is scrolled down under the finger. */
function scrolled(from: Element, sheet: Element): boolean {
  for (let el: Element | null = from; el; el = el.parentElement) {
    if (el.scrollTop > 0) return true;
    if (el === sheet) return false;
  }
  return false;
}

/** Drag a sheet down to close it: from its handle, or from its body while that is scrolled to the top. It follows
 * the finger and its shade fades; past a third of its height, or flicked, it goes, else it springs back. A release
 * still settling when the sheet closes by other means is called off. The body keeps `touch-action: pan-y` for its
 * scrolling, so the browser would take a finger moving down from the top for a pan and end the pointer: the touch
 * events under the drag are told no from the first move down, and the pointer stream goes on. */
function useSheetDrag(panel: RefObject<HTMLElement | null> | null, shade: RefObject<HTMLElement | null>, onClose: () => void) {
  const close = useRef(onClose);
  close.current = onClose;
  useEffect(() => {
    if (!panel) return;
    // one pointer at a time, let go of when the sheet goes; nothing new while a release settles
    let stop: (() => void) | null = null;
    let settling = false;
    let pending: (() => void) | null = null;
    const down = (e: PointerEvent) => {
      const el = panel.current;
      if (stop || settling || !el || e.button !== 0 || !e.isPrimary || e.pointerType === "mouse" || !el.contains(e.target as Node)) return;
      const fromHandle = (e.target as Element).closest(".hdl");
      if (!fromHandle && scrolled(e.target as Element, el)) return;
      const tracker = new Tracker();
      tracker.push(sample(e));
      let axis: ReturnType<typeof axisOf> = null;
      const height = () => el.offsetHeight || 300;
      // down from the top, or once the drag is the sheet's: not the browser's pan; up, sideways, or once it is the
      // page's: the body scrolls
      const claim = (t: TouchEvent) => {
        const touch = t.touches[0];
        // a second finger is a pinch: the browser's
        if (!touch || t.touches.length > 1 || axis === "x") return;
        const tx = touch.clientX - e.clientX;
        const ty = touch.clientY - e.clientY;
        if (axis === "y" || (ty > 0 && ty >= Math.abs(tx))) t.preventDefault();
      };
      el.addEventListener("touchmove", claim, { passive: false });
      const letGo = () => el.removeEventListener("touchmove", claim);
      const track = trackPointer(e, {
        move: (m) => {
          tracker.push(sample(m));
          const dy = m.clientY - e.clientY;
          if (!axis) {
            axis = axisOf(m.clientX - e.clientX, dy);
            if (!axis) return;
            if (axis === "x" || dy < 0) return (axis = "x");
            finishEntry(el, shade.current);
            el.style.transition = "none";
          }
          if (axis !== "y") return;
          el.style.transform = `translateY(${Math.max(0, dy)}px)`;
          if (shade.current) shade.current.style.opacity = String(1 - Math.max(0, dy) / height());
        },
        end: (u, cancelled) => {
          stop = null;
          letGo();
          if (axis !== "y") return;
          settling = true;
          swallowNextClick();
          tracker.push(sample(u));
          const dy = Math.max(0, u.clientY - e.clientY);
          const goes = !cancelled && sheetCloses(dy, tracker.velocity().vy, height());
          const ms = motionMs(goes ? 160 : 200);
          if (shade.current) {
            shade.current.style.transition = `opacity ${ms}ms ${EASE}`;
            shade.current.style.opacity = goes ? "0" : "1";
          }
          pending = settle(el, { transform: goes ? "translateY(100%)" : "translateY(0)" }, ms, () => {
            pending = null;
            settling = false;
            if (goes) close.current();
            else clearDrag(el, shade.current);
          });
        },
      });
      stop = () => {
        track();
        letGo();
      };
    };
    document.addEventListener("pointerdown", down);
    return () => {
      document.removeEventListener("pointerdown", down);
      stop?.();
      pending?.();
    };
  }, [panel, shade]);
}
