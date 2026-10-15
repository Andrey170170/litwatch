/**
 * What the app remembers for the session so the way back lands where the reader was: the lower shelves opened on a
 * list, and the papers a Deep card was asked for (the API has no queued state to read back).
 */
const openShelves = new Set<string>();
const askedCards = new Set<string>();

export const shelfOpen = (key: string) => openShelves.has(key);
export function setShelfOpen(key: string, open: boolean) {
  if (open) openShelves.add(key);
  else openShelves.delete(key);
}

export const cardAsked = (id: string) => askedCards.has(id);
export const rememberCard = (id: string) => void askedCards.add(id);
export const forgetCard = (id: string) => void askedCards.delete(id);
