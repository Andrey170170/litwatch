/**
 * The screens left behind, for the edge swipe back (DESIGN.md, "Phone"): as the phone goes deeper the shell keeps
 * a copy of the screen's DOM, and a swipe back shows the one it returns to under the page that follows the finger.
 * A copy is inert, shown only during the drag; the real page takes over on arrival. Keyed by the history index of
 * the screen copied, with the few most recent kept; past them the way back shows the page colour.
 */
const KEEP = 8;
const kept = new Map<number, { node: HTMLElement; scrollTop: number }>();

export function keepSnapshot(index: number, screen: HTMLElement) {
  const node = screen.cloneNode(true) as HTMLElement;
  node.setAttribute("inert", "");
  node.setAttribute("aria-hidden", "true");
  for (const el of node.querySelectorAll("[id]")) el.removeAttribute("id");
  // the router restores scrolling by this id, and must find the live screen, not a copy
  for (const el of node.querySelectorAll("[data-scroll-restoration-id]")) el.removeAttribute("data-scroll-restoration-id");
  // a screen copied as a step leaves it has followed the finger and faded: the copy is at rest
  for (const el of [node, ...node.querySelectorAll<HTMLElement>(".screen")]) {
    for (const prop of ["transform", "transition", "opacity"]) el.style.removeProperty(prop);
    el.removeAttribute("data-came"); // a copy plays no entry
  }
  const scrollTop = screen.querySelector(".scroll")?.scrollTop ?? 0;
  kept.delete(index);
  kept.set(index, { node, scrollTop });
  while (kept.size > KEEP) kept.delete(kept.keys().next().value!);
}

/** The copy of the screen at `index`, put into `into`, scrolled as it was; null when none was kept (a deep link, a
 * reload). */
export function showSnapshot(index: number, into: HTMLElement): HTMLElement | null {
  const s = kept.get(index);
  if (!s) return null;
  into.replaceChildren(s.node);
  const scroll = s.node.querySelector(".scroll");
  if (scroll) scroll.scrollTop = s.scrollTop;
  return s.node;
}

export function clearSnapshots() {
  kept.clear();
}
