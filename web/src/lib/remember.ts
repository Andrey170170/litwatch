/**
 * What the app remembers for the session so the way back lands where the reader was: the lower shelves opened on a
 * list, and the papers a Deep card was asked for (the API has no queued state to read back).
 */
import { useSyncExternalStore } from "react";

const openShelves = new Set<string>();
const askedCards = new Set<string>();
const watchers = new Set<() => void>();
const changed = () => watchers.forEach((w) => w());
const watch = (w: () => void) => {
  watchers.add(w);
  return () => void watchers.delete(w);
};

export const shelfOpen = (key: string) => openShelves.has(key);
export function setShelfOpen(key: string, open: boolean) {
  if (open) openShelves.add(key);
  else openShelves.delete(key);
}

export function rememberCard(id: string) {
  askedCards.add(id);
  changed();
}
export function forgetCard(id: string) {
  askedCards.delete(id);
  changed();
}
/** Whether a card was asked for this paper, live: the page sees a request made by a control that has since closed. */
export const useCardAsked = (id: string) => useSyncExternalStore(watch, () => askedCards.has(id));
