/**
 * The list a paper was opened from, so its page can say where it sits ("3/22") and, later, step to its
 * neighbours. Rows register the list they are in as they open a paper; the newest registration wins.
 */
let current: { key: string; ids: string[] } | null = null;

export function openedFrom(key: string, ids: string[]) {
  current = { key, ids };
}

/** The paper's place in that list, 1-based, or null when it was reached another way. */
export function placeOf(id: string): { index: number; total: number; prev: string | null; next: string | null } | null {
  if (!current) return null;
  const i = current.ids.indexOf(id);
  if (i < 0) return null;
  return { index: i + 1, total: current.ids.length, prev: current.ids[i - 1] ?? null, next: current.ids[i + 1] ?? null };
}

export function forgetList() {
  current = null;
}
