/** TanStack Query options over the typed client: one per endpoint, and one per shelf of a list. */
import { infiniteQueryOptions, queryOptions } from "@tanstack/react-query";
import { api, ok } from "./client";
import type { Shelf } from "@/lib/rarity";
import { sortOf, type ListSearch } from "@/lib/search";

export const statusQuery = queryOptions({
  queryKey: ["status"],
  queryFn: async () => ok(await api.GET("/api/status")),
  staleTime: 30_000,
  refetchInterval: 60_000,
});

export const topicsQuery = queryOptions({
  queryKey: ["topics"],
  queryFn: async () => ok(await api.GET("/api/topics")),
  staleTime: 60_000,
});

export const paperQuery = (id: string) =>
  queryOptions({
    queryKey: ["paper", id],
    queryFn: async () => ok(await api.GET("/api/papers/{paper_id}", { params: { path: { paper_id: id } } })),
  });

/** The class tree, for classifying a paper by hand. */
export const taxonomyQuery = queryOptions({
  queryKey: ["taxonomy"],
  queryFn: async () => ok(await api.GET("/api/settings/taxonomy")),
  staleTime: 5 * 60_000,
});

/** A shelf's filter: the list's search plus the shelf's own levels or grades. A level filter narrows a Papers
 * shelf to the levels it holds. */
function shelfFilter(search: ListSearch, shelf: Shelf) {
  const rarity = shelf.rarity ? shelf.rarity.filter((r) => !search.rarity?.length || search.rarity.includes(r)) : search.rarity;
  return {
    q: search.q,
    rarity: rarity?.length ? rarity : undefined,
    grade: shelf.grade,
    saved: search.saved,
    code: search.code,
    sort: sortOf(search),
  };
}

const loaded = (pages: { rows: unknown[] }[]) => pages.reduce((n, p) => n + p.rows.length, 0);

/** A list keeps its order while it is read (DESIGN.md, "Positions stay put"): no refetch on focus, on reconnect or on
 * the way back from a paper. Actions patch rows in place or invalidate on purpose. */
const listOptions = { staleTime: 5 * 60_000, refetchOnWindowFocus: false, refetchOnReconnect: false } as const;

/** One shelf of Papers, loading more as it scrolls. `limit` 1 fetches only the count for a folded shelf. */
export const papersQuery = (search: ListSearch, shelf: Shelf, limit = 50) =>
  infiniteQueryOptions({
    queryKey: ["papers", search, shelf.key, limit],
    queryFn: async ({ pageParam }) =>
      ok(
        await api.GET("/api/papers", {
          params: { query: { ...shelfFilter(search, shelf), topic: search.topic, source: "collection", limit, offset: pageParam } },
        }),
      ),
    initialPageParam: 0,
    getNextPageParam: (last, pages) => (loaded(pages) < last.total ? loaded(pages) : undefined),
    placeholderData: (previous) => previous,
    ...listOptions,
  });

/** The papers a search finds outside the collection, on the first page only. */
export const outsideQuery = (q: string | undefined, search: ListSearch) =>
  queryOptions({
    queryKey: ["papers", "outside", q ?? "", sortOf(search)],
    queryFn: async () => ok(await api.GET("/api/papers", { params: { query: { q, source: "outside", sort: sortOf(search), limit: 50 } } })),
    enabled: !!q,
    staleTime: 60_000,
  });

/** One shelf of a topic's page. */
export const topicQuery = (slug: string, search: ListSearch, shelf: Shelf, limit = 50) =>
  infiniteQueryOptions({
    queryKey: ["topic", slug, search, shelf.key, limit],
    queryFn: async ({ pageParam }) =>
      ok(
        await api.GET("/api/topics/{slug}", {
          params: { path: { slug }, query: { ...shelfFilter(search, shelf), limit, offset: pageParam } },
        }),
      ),
    initialPageParam: 0,
    getNextPageParam: (last, pages) => (loaded(pages) < last.total ? loaded(pages) : undefined),
    // Rows stay while the filter changes, but never another topic's rows under this topic's name.
    placeholderData: (previous, previousQuery) => (previousQuery?.queryKey[1] === slug ? previous : undefined),
    ...listOptions,
  });
