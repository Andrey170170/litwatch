/**
 * The list a paper was opened from, so its page can say where it sits ("3/22") and, later, step to its
 * neighbours. Rows register the list they are in as they open a paper, and name it in the history entry; the
 * newest registration wins, and a page asks for its place by the list its own entry names, so a paper reached
 * another way, or an older entry from another list, shows none.
 */
let current: { key: string; ids: string[]; total: number } | null = null;

/** `ids` are the rows loaded so far, in order; `total` is the whole list's count. */
export function openedFrom(key: string, ids: string[], total: number) {
  current = { key, ids, total };
}

/** The paper's place in the list `key`, 1-based, or null when that is not the list registered last. */
export function placeOf(id: string, key: string | undefined): { index: number; total: number; prev: string | null; next: string | null } | null {
  if (!current || !key || current.key !== key) return null;
  const i = current.ids.indexOf(id);
  if (i < 0) return null;
  return { index: i + 1, total: current.total, prev: current.ids[i - 1] ?? null, next: current.ids[i + 1] ?? null };
}

export function forgetList() {
  current = null;
}
