/**
 * The phone's pointer handling (DESIGN.md, "Phone"). The shell's one handler: a drag from the left edge opens the
 * drawer on a top-level screen and goes back on a deeper one, the page following the finger with the screen it
 * returns to under it; a horizontal drag away from the edge steps the page's sequence (`lib/steps`); a drag that
 * starts vertical is left to the page, which scrolls. The sheets drag down by their handle (`Sheet.tsx`). Decisions
 * come from `lib/gesture`; this file applies them to the elements, and mostly by writing styles, so a moving finger
 * re-renders nothing.
 */
import { useRouter } from "@tanstack/react-router";
import { useEffect, useRef, useState, type RefObject } from "react";
import { axisOf, backCommits, drawerSettles, EDGE, motionMs, resist, STEP_SLIDE, stepCommits, Tracker, type Axis } from "@/lib/gesture";
import { keepSnapshot, showSnapshot } from "@/lib/snapshots";
import { arrive, stepsNow, type Steps } from "@/lib/steps";

export const EASE = "cubic-bezier(.2, .85, .25, 1)";

/** A pointer event as the Tracker takes it; timed by the clock, which tests can hold. */
export const sample = (e: PointerEvent) => ({ t: Date.now(), x: e.clientX, y: e.clientY });

/** Follow one pointer from `down` to its release: `move` gets each move, `end` the release (`cancelled` when the
 * browser took the pointer for a scroll). Window listeners, so the finger may leave the element. After a drag,
 * the click the release would make lands on nothing. */
export function trackPointer(down: PointerEvent, on: { move: (e: PointerEvent) => void; end: (e: PointerEvent, cancelled: boolean) => void }) {
  const id = down.pointerId;
  const move = (e: PointerEvent) => e.pointerId === id && on.move(e);
  const end = (e: PointerEvent) => {
    if (e.pointerId !== id) return;
    window.removeEventListener("pointermove", move);
    window.removeEventListener("pointerup", end);
    window.removeEventListener("pointercancel", end);
    on.end(e, e.type === "pointercancel");
  };
  window.addEventListener("pointermove", move);
  window.addEventListener("pointerup", end);
  window.addEventListener("pointercancel", end);
}

let swallowUntil = 0;
const disarm = () => {
  document.removeEventListener("click", swallow, true);
  document.removeEventListener("pointerdown", disarm, true);
};
const swallow = (e: Event) => {
  disarm();
  if (Date.now() >= swallowUntil) return;
  e.stopPropagation();
  e.preventDefault();
};
/** The release of a drag is not a tap on what lies under the finger: the click it makes, if within 350 ms, lands
 * on nothing. The next touch is a new gesture, and its tap counts. */
export function swallowNextClick() {
  swallowUntil = Date.now() + 350;
  document.addEventListener("click", swallow, true);
  document.addEventListener("pointerdown", disarm, true);
}

/** Move an element to its rest over `ms` (at once under reduced motion), then call `then`. */
export function settle(el: HTMLElement | null, to: Partial<CSSStyleDeclaration>, ms: number, then?: () => void) {
  if (el) {
    el.style.transition = ms ? `transform ${ms}ms ${EASE}, opacity ${ms}ms ${EASE}` : "";
    Object.assign(el.style, to);
  }
  setTimeout(() => then?.(), ms);
}

/** Take back what a drag wrote on an element, and nothing else: Radix keeps `pointer-events` inline on a dialog. */
export function clearDrag(...els: (HTMLElement | null)[]) {
  for (const el of els) for (const prop of ["transform", "transition", "opacity"]) el?.style.removeProperty(prop);
}

type Els = { root: RefObject<HTMLElement | null>; page: RefObject<HTMLElement | null>; main: RefObject<HTMLElement | null>; under: RefObject<HTMLElement | null>; panel: RefObject<HTMLElement | null>; shade: RefObject<HTMLElement | null> };
type Opts = { deeper: boolean; menu: boolean; setMenu: (open: boolean) => void; deeperAt: (pathname: string) => boolean };
type Kind = "drawer" | "back" | "step";
type Drag = { kind: Kind; x0: number; y0: number; axis: Axis; tracker: Tracker; steps: Steps | null; wasOpen: boolean };

/** A horizontal drag on an element that scrolls sideways (a strip) is that element's. */
function scrollsSideways(from: Element, upTo: Element): boolean {
  for (let el: Element | null = from; el && el !== upTo; el = el.parentElement) {
    if (el.scrollWidth > el.clientWidth + 1 && /auto|scroll/.test(getComputedStyle(el).overflowX)) return true;
  }
  return false;
}

const clamp = (v: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, v));

/**
 * The shell's gestures on its elements. Returns the drawer's drag state for the `Sheet` (so it mounts shut and
 * without its entry animation while the finger holds it), and `onRendered`, which the shell calls once a screen a
 * gesture brought in has rendered, so the page that followed the finger can give way to it.
 */
export function usePhoneGestures(els: Els, opts: Opts): { drawerDrag: "opening" | "shutting" | null; onRendered: () => void } {
  const router = useRouter();
  const [drawerDrag, setDrawerDrag] = useState<"opening" | "shutting" | null>(null);
  const live = useRef(opts);
  live.current = opts;
  const pending = useRef<(() => void) | null>(null);

  // As the phone goes deeper, the screen left is kept for the way back.
  useEffect(
    () =>
      router.history.subscribe(({ location, action }) => {
        const index = (location.state as { __TSR_index?: number }).__TSR_index ?? 0;
        if (action.type === "PUSH" && live.current.deeperAt(location.pathname) && els.page.current) keepSnapshot(index - 1, els.page.current);
      }),
    [router, els.page],
  );

  useEffect(() => {
    let busy = false;
    const width = () => els.root.current?.clientWidth || window.innerWidth;
    const panelWidth = () => els.panel.current?.offsetWidth || Math.min(300, width() * 0.82);
    const index = () => (router.state.location.state as { __TSR_index?: number }).__TSR_index ?? 0;
    const back = () => (router.history.canGoBack() ? router.history.back() : void router.navigate({ to: "/" }));

    /** The drawer at `x` from open (0 open, -width shut), its shade fading with it. */
    const drawerAt = (x: number, w: number) => {
      if (els.panel.current) els.panel.current.style.transform = `translateX(${x}px)`;
      if (els.shade.current) els.shade.current.style.opacity = String(1 + x / w);
    };
    const drawerX = (d: Drag, dx: number, w: number) => clamp(d.wasOpen ? dx : dx - w, -w, 0);
    /** The page `x` to the right, the screen under it coming to rest as the page leaves. */
    const pageAt = (x: number) => {
      if (els.page.current) els.page.current.style.transform = `translateX(${x}px)`;
      els.under.current?.style.setProperty("--f", String(clamp(x / width(), 0, 1)));
    };

    const begin = (d: Drag) => {
      busy = true;
      if (d.kind === "drawer") {
        setDrawerDrag(d.wasOpen ? "shutting" : "opening");
        if (!d.wasOpen) live.current.setMenu(true);
      } else if (d.kind === "back") {
        const under = els.under.current;
        if (under) {
          under.hidden = false;
          if (!showSnapshot(index() - 1, under)) under.replaceChildren();
        }
        if (els.page.current) els.page.current.style.transition = "none";
      } else if (els.main.current) els.main.current.style.transition = "none";
    };

    const follow = (d: Drag, dx: number) => {
      if (d.kind === "drawer") drawerAt(drawerX(d, dx, panelWidth()), panelWidth());
      else if (d.kind === "back") pageAt(Math.max(0, dx));
      else if (els.main.current) {
        const lies = dx < 0 ? d.steps?.next : d.steps?.prev;
        els.main.current.style.transform = `translateX(${lies ? dx : resist(dx)}px)`;
      }
    };

    const finish = (d: Drag, e: PointerEvent, cancelled: boolean) => {
      const dx = e.clientX - d.x0;
      const { vx } = d.tracker.velocity();
      const W = width();
      const done = () => (busy = false);
      if (d.kind === "drawer") {
        const w = panelWidth();
        const where = cancelled ? (d.wasOpen ? "open" : "shut") : drawerSettles(drawerX(d, dx, w), vx, w);
        settle(els.panel.current, { transform: where === "open" ? "translateX(0)" : `translateX(${-w}px)` }, motionMs(200), () => {
          if (where === "shut") live.current.setMenu(false);
          setDrawerDrag(null);
          clearDrag(els.panel.current, els.shade.current);
          done();
        });
        if (els.shade.current) {
          els.shade.current.style.transition = `opacity ${motionMs(200)}ms ${EASE}`;
          els.shade.current.style.opacity = where === "open" ? "1" : "0";
        }
      } else if (d.kind === "back") {
        const go = !cancelled && dx > 0 && backCommits(dx, vx, W);
        if (go) {
          els.under.current?.style.setProperty("--f", "1");
          settle(els.page.current, { transform: `translateX(${W}px)` }, motionMs(200), () => {
            // the page waits off screen until the screen under it has rendered for real (`onRendered`)
            pending.current = () => {
              clearDrag(els.page.current);
              if (els.under.current) {
                els.under.current.hidden = true;
                els.under.current.replaceChildren();
              }
              done();
            };
            arrive("still");
            back();
          });
        } else
          settle(els.page.current, { transform: "translateX(0)" }, motionMs(200), () => {
            clearDrag(els.page.current);
            if (els.under.current) els.under.current.hidden = true;
            done();
          });
      } else {
        const dir = cancelled ? 0 : stepCommits(dx, vx, W);
        const to = dir === 1 ? d.steps?.next : dir === -1 ? d.steps?.prev : null;
        if (to && dir) {
          settle(els.main.current, { transform: `translateX(${dx - dir * STEP_SLIDE}px)`, opacity: "0" }, motionMs(200), () => {
            arrive(dir === 1 ? "next" : "prev");
            to();
            done();
          });
        } else
          settle(els.main.current, { transform: "translateX(0)" }, motionMs(200), () => {
            clearDrag(els.main.current);
            done();
          });
      }
    };

    const down = (e: PointerEvent) => {
      if (busy || e.button !== 0 || !e.isPrimary) return;
      const { deeper, menu } = live.current;
      const t = e.target as Element;
      const root = els.root.current;
      if (!root) return;
      let kind: Kind | null = null;
      if (menu) kind = els.panel.current?.contains(t) ? "drawer" : null;
      else if (!root.contains(t)) return;
      else if (e.clientX <= EDGE) kind = deeper ? "back" : "drawer";
      else if (stepsNow() && els.main.current?.contains(t) && !scrollsSideways(t, els.main.current)) kind = "step";
      if (!kind) return;
      const d: Drag = { kind, x0: e.clientX, y0: e.clientY, axis: null, tracker: new Tracker(), steps: kind === "step" ? stepsNow() : null, wasOpen: menu };
      d.tracker.push(sample(e));
      trackPointer(e, {
        move: (m) => {
          d.tracker.push(sample(m));
          const dx = m.clientX - d.x0;
          if (!d.axis) {
            d.axis = axisOf(dx, m.clientY - d.y0);
            if (d.axis !== "x") return;
            // an edge swipe goes one way: right opens or goes back; the open drawer shuts to the left
            if ((d.kind === "back" || (d.kind === "drawer" && !d.wasOpen)) && dx < 0) return (d.axis = "y");
            begin(d);
          }
          follow(d, dx);
        },
        end: (u, cancelled) => {
          if (d.axis !== "x") return;
          swallowNextClick();
          finish(d, u, cancelled);
        },
      });
    };
    document.addEventListener("pointerdown", down);
    return () => document.removeEventListener("pointerdown", down);
  }, [router, els.root, els.page, els.main, els.under, els.panel, els.shade]);

  return {
    drawerDrag,
    onRendered: () => {
      pending.current?.();
      pending.current = null;
    },
  };
}
