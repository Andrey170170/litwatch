/**
 * The sequence a page stands in, for the phone's horizontal swipe (DESIGN.md, "Phone"): a Day or Week registers
 * the older and newer one, a paper its neighbours in the list it was opened from, a Calibrate session its papers.
 * The shell reads the sequence as a drag starts and, before it moves, says how the next screen arrives, so it
 * enters from the side it was stepped to (or stays still after a swipe back) instead of as a deeper page.
 */
import { useEffect } from "react";

export type Steps = { prev: (() => void) | null; next: (() => void) | null };
/** How the screen now rendering got here, when a gesture brought it: stepped to the next or the previous, or
 * already in view after a swipe back. */
export type Arrival = "next" | "prev" | "still";

let current: Steps | null = null;
let arrival: Arrival | null = null;
let arrivals = 0;

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

/** Say how the screen about to render arrives. Called right before the navigation that brings it. */
export function arrive(how: Arrival) {
  arrival = how;
  arrivals++;
}
/** The arrival the current render owes to a gesture, once. */
export function takeArrival(): Arrival | null {
  const a = arrival;
  arrival = null;
  return a;
}
/** How many screens arrived by gesture so far: part of the screen's key, so a step within one route (a Day to the
 * next) mounts a fresh screen that plays its entry. */
export const arrivalCount = () => arrivals;
