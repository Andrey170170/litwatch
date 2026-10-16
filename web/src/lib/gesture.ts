/**
 * The phone's gestures decided without the DOM (DESIGN.md, "Phone"): which axis a drag took, how fast the finger
 * moved as it let go, where a drawer, a step, a page dragged back or a sheet settles, and the resistance where
 * nothing lies. The hooks in `components/gestures.ts` and `Sheet.tsx` feed it pointer samples and apply its answers.
 */

export type Sample = { t: number; x: number; y: number };

/** A finger has moved far enough to mean a drag, in px from where it landed. */
export const SLOP = 8;
/** The window, in ms, the release velocity is read over: the finger's speed as it let go, not its mean. */
export const VELOCITY_WINDOW_MS = 90;
/** The speed, in px per ms, that commits a drag whatever its distance: a flick. */
export const FLICK = 0.5;
/** How far from the left edge, in px, a swipe counts as an edge swipe. Android's gesture navigation owns the first
 * ~24 dp and hands the app a finger that landed just inside it, so the zone reaches 48 px: half of it is where
 * such a finger lands, half is edge the page can reach. */
export const EDGE = 48;
/** How far a step slides after the finger, and from where the next content enters (DESIGN.md, "Motion"). */
export const STEP_SLIDE = 40;

export type Axis = "x" | "y" | null;

/** The axis a drag took: null within the slop, "y" when at least as vertical as horizontal (the page scrolls),
 * else "x". Decided once, when the slop is passed. */
export function axisOf(dx: number, dy: number): Axis {
  if (Math.hypot(dx, dy) < SLOP) return null;
  return Math.abs(dy) >= Math.abs(dx) ? "y" : "x";
}

/** The velocity at release: px per ms along each axis over the last VELOCITY_WINDOW_MS of samples. */
export class Tracker {
  private samples: Sample[] = [];
  push(s: Sample) {
    this.samples.push(s);
    while (this.samples.length > 2 && s.t - this.samples[0]!.t > VELOCITY_WINDOW_MS) this.samples.shift();
  }
  velocity(): { vx: number; vy: number } {
    const a = this.samples[0];
    const b = this.samples.at(-1);
    if (!a || !b || b.t <= a.t) return { vx: 0, vy: 0 };
    const dt = b.t - a.t;
    return { vx: (b.x - a.x) / dt, vy: (b.y - a.y) / dt };
  }
}

/** Where nothing lies the content follows a quarter of the finger: the end is felt, not hit. */
export function resist(d: number): number {
  return d * 0.25;
}

/** Where the drawer settles: by the finger's direction when it let go fast, else open past half the drawer's width.
 * `x` is the drawer's offset from open, 0 open and -width shut. */
export function drawerSettles(x: number, vx: number, width: number): "open" | "shut" {
  if (Math.abs(vx) >= FLICK) return vx > 0 ? "open" : "shut";
  return x > -width / 2 ? "open" : "shut";
}

/** Whether a step commits, and which way: past a quarter of the width, or flicked the way it was dragged; a flick
 * back towards the start is a change of mind. `dx` is the finger's travel, negative to the left, which brings the
 * next one (the newer day, the next paper) in. */
export function stepCommits(dx: number, vx: number, width: number): -1 | 0 | 1 {
  if (dx === 0) return 0;
  const flicked = Math.abs(vx) >= FLICK;
  if (flicked && Math.sign(vx) !== Math.sign(dx)) return 0;
  if (!flicked && Math.abs(dx) < width / 4) return 0;
  return dx < 0 ? 1 : -1;
}

/** Whether a page dragged from the left edge goes back: past a third of the width, or flicked to the right. */
export function backCommits(dx: number, vx: number, width: number): boolean {
  return dx >= width / 3 || (vx >= FLICK && dx > 0);
}

/** Whether a sheet dragged down closes: past a third of its height, or flicked down (DESIGN.md, "Sheets"). */
export function sheetCloses(dy: number, vy: number, height: number): boolean {
  return dy >= height / 3 || (vy >= FLICK && dy > 0);
}

/** A duration under the system's reduced-motion setting: nothing moves on its own (DESIGN.md, "Motion"). */
export function motionMs(ms: number): number {
  return typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches ? 0 : ms;
}
