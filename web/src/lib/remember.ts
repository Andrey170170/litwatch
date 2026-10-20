/**
 * What the app remembers for the session so the way back lands where the reader was: the shelves opened or folded
 * on a list, Needs you's groups and a digest's updated papers the same way; and the papers a Deep card was asked for (the API has no queued
 * state to read back).
 */
import { useSyncExternalStore } from "react";

// shelf, Needs you group or updated list key → open, only for those the reader toggled; the rest keep the shelf's own default
const shelves = new Map<string, boolean>();
// paper id → when the card it already had was written (null for none), so a Write again is told from its old card
const askedCards = new Map<string, string | null>();
const watchers = new Set<() => void>();
const changed = () => watchers.forEach((w) => w());
const watch = (w: () => void) => {
  watchers.add(w);
  return () => void watchers.delete(w);
};

export const shelfOpen = (key: string, byDefault: boolean) => shelves.get(key) ?? byDefault;
export const setShelfOpen = (key: string, open: boolean) => void shelves.set(key, open);
export const forgetShelves = () => shelves.clear();

export function rememberCard(id: string, writtenAt: string | null) {
  askedCards.set(id, writtenAt);
  changed();
}
export function forgetCard(id: string) {
  askedCards.delete(id);
  changed();
}
export function forgetAllCards() {
  askedCards.clear();
  changed();
}
/** Whether a card was asked for this paper, live: the page sees a request made by a control that has since closed. */
export const useCardAsked = (id: string) => useSyncExternalStore(watch, () => askedCards.has(id));
/** Whether a paper read back answers the request: the owner's own pass is open (a request takes an automatic pass
 * over, so its reason is what tells), or its card is newer than the one asked over. */
export const cardAnswered = (id: string, card: { written_at?: string | null } | null | undefined, pass: { reason: string } | null | undefined) =>
  pass?.reason === "request" || (!!card && (card.written_at ?? null) !== askedCards.get(id));
