/** A list's filters as search params (plain strings, so links stay readable), and back. */
import type { Rarity } from "./rarity";
import { RARITIES } from "./rarity";

export type Sort = "newest" | "rarest" | "best";
const SORTS: Sort[] = ["newest", "rarest", "best"];

export type ListSearch = {
  q?: string;
  rarity?: Rarity[];
  saved?: boolean;
  code?: boolean;
  sort?: Sort;
  /** Papers only: the topics filtered to. */
  topic?: string[];
};

export function str(v: unknown): string | undefined {
  return typeof v === "string" && v !== "" ? v : undefined;
}
export function flag(v: unknown): boolean | undefined {
  return v === true || v === "true" || v === "1" ? true : undefined;
}
function list(v: unknown): string[] | undefined {
  const s = str(v);
  return s ? s.split(",").filter(Boolean) : undefined;
}

/** Unknown or malformed values are dropped; every key is optional so links need not spell out a search. */
export function listSearch(s: Record<string, unknown>): ListSearch {
  const sort = str(s.sort);
  const rarity = list(s.rarity)?.filter((r): r is Rarity => (RARITIES as string[]).includes(r));
  const out: ListSearch = {};
  const q = str(s.q);
  if (q) out.q = q;
  if (rarity?.length) out.rarity = rarity;
  if (flag(s.saved)) out.saved = true;
  if (flag(s.code)) out.code = true;
  if (sort && (SORTS as string[]).includes(sort)) out.sort = sort as Sort;
  const topic = list(s.topic);
  if (topic?.length) out.topic = topic;
  return out;
}

/** The sort in force: Best match while a search is typed and no other sort chosen; Rarest by default. */
export function sortOf(s: ListSearch): Sort {
  if (s.sort && (s.sort !== "best" || s.q)) return s.sort;
  return s.q ? "best" : "rarest";
}

/** How many filters are on, for the phone's filter button. */
export function activeFilters(s: ListSearch, withTopics: boolean): number {
  return (s.rarity?.length ? 1 : 0) + (s.saved ? 1 : 0) + (s.code ? 1 : 0) + (withTopics && s.topic?.length ? 1 : 0);
}

/** Toggle one value in a list filter; an empty list clears the key. */
export function toggled<T>(current: T[] | undefined, value: T): T[] | undefined {
  const next = current?.includes(value) ? current.filter((x) => x !== value) : [...(current ?? []), value];
  return next.length ? next : undefined;
}

/** Query strings stay plain: lists joined by commas, flags as "1". */
export function stringifySearch(search: Record<string, unknown>): string {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(search)) {
    if (v === undefined || v === null || v === "" || v === false) continue;
    if (Array.isArray(v)) {
      if (v.length) p.set(k, v.join(","));
    } else p.set(k, v === true ? "1" : String(v));
  }
  const s = p.toString();
  return s ? `?${s}` : "";
}
export function parseSearch(search: string): Record<string, string> {
  return Object.fromEntries(new URLSearchParams(search));
}
