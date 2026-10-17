/**
 * The sequence a page stands in, for the phone's horizontal swipe (DESIGN.md, "Phone"): a Day or Week registers
 * the older and newer one, a paper its neighbours in the list it was opened from, a Calibrate session its papers.
 * The shell reads the sequence as a drag starts and, before it moves, says how the next screen arrives, so it
 * enters from the side it was stepped to (or stays still after a swipe back) instead of as a deeper page.
 */
import { useEffect } from "react";

/** A step says whether the screen leaves: `false` when it stays (Calibrate refuses while a save is in flight, or
 * while an edit lacks its Fit), anything else when it goes. */
export type Steps = { prev: (() => boolean | void) | null; next: (() => boolean | void) | null };
/** How the screen now rendering got here, when a gesture brought it: stepped to the next or the previous, or
 * already in view after a swipe back. */
export type Arrival = "next" | "prev" | "still";

let current: Steps | null = null;
let last: { n: number; how: Arrival | null } = { n: 0, how: null };

/** Register the page's neighbours; null for a page that does not step. Re-registers every render, so the
 * callbacks see the page's latest state. */
export function useSteps(steps: Steps | null) {
  useEffect(() => {
    current = steps;
    return () => {
      current = null;
    };
  });
}

export const stepsNow = (): Steps | null => current;

/** Say how the screen about to render arrives. Called right before the navigation that brings it; returns the
 * way to take it back when the navigation did not happen. */
export function arrive(how: Arrival): () => void {
  const before = last;
  last = { n: last.n + 1, how };
  return () => (last = before);
}
/** The latest arrival by gesture: how, and its number, which the shell keys the screen by, so a step within one
 * route (a Day to the next) mounts a fresh screen that plays its entry, and a render can tell whether its screen
 * is the one the gesture brought. */
export const arrival = () => last;

/** Where a location sits in the session's history: the router's own index, 0 for the first entry. The shell tells
 * forward from back by it, and the snapshots of screens left behind are kept by it (`lib/snapshots`). */
export const historyIndex = (location: { state: unknown }) => (location.state as { __TSR_index?: number }).__TSR_index ?? 0;
