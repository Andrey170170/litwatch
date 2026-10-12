/** Search-param parsing shared by the table routes. Unknown or malformed values are dropped. */

export type Sort = "added" | "published" | "title";
const SORTS: Sort[] = ["added", "published", "title"];

export type TableSearch = {
  q?: string;
  saved?: boolean;
  year?: number;
  sort?: Sort;
  adjacent?: boolean;
  page?: number;
};

export function str(v: unknown): string | undefined {
  return typeof v === "string" && v !== "" ? v : undefined;
}
export function bool(v: unknown): boolean | undefined {
  return v === true || v === "true" ? true : v === false || v === "false" ? false : undefined;
}
export function int(v: unknown): number | undefined {
  const n = typeof v === "number" ? v : typeof v === "string" ? Number(v) : NaN;
  return Number.isInteger(n) && n >= 0 ? n : undefined;
}

export function tableSearch(s: Record<string, unknown>): TableSearch {
  // Every key optional, so links need not spell out a search.
  const sort = str(s.sort);
  return {
    q: str(s.q),
    saved: bool(s.saved),
    year: int(s.year),
    sort: sort && (SORTS as string[]).includes(sort) ? (sort as Sort) : undefined,
    adjacent: bool(s.adjacent),
    page: int(s.page),
  };
}
