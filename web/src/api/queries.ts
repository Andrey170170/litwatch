/** TanStack Query options over the typed client: one per endpoint, and one per shelf of a list. */
import { infiniteQueryOptions, queryOptions, type QueryClient } from "@tanstack/react-query";
import { api, ok } from "./client";
import type { Shelf } from "@/lib/rarity";
import type { Quality, Run, Session, SessionRow } from "@/lib/settings";
import { sortOf, type ListSearch } from "@/lib/search";

/** The shell's summary (the status line, the sidebar's topics, the Settings hints): one read on every page,
 * polled, refreshed on focus and after the owner's own writes that change a count (`refreshSummary`), and never by
 * navigation. Counts only; the pages keep their own reads. */
export const summaryQuery = queryOptions({
  queryKey: ["summary"],
  queryFn: async () => ok(await api.GET("/api/summary")),
  staleTime: 30_000,
  refetchInterval: 60_000,
  // A sidebar mounting (the reading one for the Settings one, the phone's drawer) is navigation, not a reason.
  refetchOnMount: false,
  // Only the data: a refetch that changes nothing re-renders nothing (structural sharing).
  notifyOnChangeProps: ["data"],
});

export const refreshSummary = (qc: QueryClient) => void qc.invalidateQueries({ queryKey: summaryQuery.queryKey });

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

/** A list keeps its order for the visit (DESIGN.md, "Positions stay put"): never refetched on its own, not on focus,
 * reconnect or the way back from a paper, and kept half an hour after the reader leaves it. Actions patch rows in
 * place or invalidate on purpose; a page load starts afresh. */
const listOptions = { staleTime: Infinity, gcTime: 30 * 60_000, refetchOnWindowFocus: false, refetchOnReconnect: false } as const;

/** A page reached going forward (a link, not Back) is a fresh visit: its cached reads are marked stale, so the page
 * refetches them in the background when shown, its cached rows on screen meanwhile. Only the page gone to: the one
 * left keeps its reads for the way back. The shell's summary is never touched here. */
export function freshVisit(qc: QueryClient, pathname: string) {
  const path = pathname.replace(/\/$/, "") || "/";
  const topic = /^\/topics\/([^/]+)$/.exec(path);
  // A Settings page's reads are keyed by its path under /settings (below): the page gone to is all that is marked,
  // a subpage (a session, a topic's editor) leaves the list it came from. /settings itself opens Overview.
  const settings = /^\/settings(?:\/(.+))?$/.exec(path);
  const key = path === "/papers" ? ["papers"] : path === "/topics" ? ["topics"] : topic?.[1] ? ["topic", decodeURIComponent(topic[1])] : settings ? ["settings", settings[1] ? decodeURIComponent(settings[1]) : "overview"] : null;
  if (key) void qc.invalidateQueries({ queryKey: key, refetchType: "none" });
}

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
    ...listOptions,
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

// -- Settings (DESIGN.md, "Settings") ---------------------------------------------------------------------------

// A Settings page's reads live under its own key, `["settings", <its path under /settings>, ...]`, so a fresh visit
// to one page (`freshVisit`) refetches that page's reads only: a page left keeps its rows for the way back, whatever
// its siblings or subpages read meanwhile. Two pages reading one endpoint each keep their own copy.

/** Refetched in the background on each visit by a link (`freshVisit`), and still for the visit: nothing moves under
 * the reader while a page is open or on the way back to it. What a write settles is patched into the cached page
 * (`settle`, `settleFromPaper`). */
const settingsOptions = { staleTime: Infinity, refetchOnWindowFocus: false, refetchOnReconnect: false } as const;

export const qualityQuery = (page: "needs" | "overview" | "runs") =>
  queryOptions({
    queryKey: ["settings", page, "quality"],
    queryFn: async () => ok(await api.GET("/api/settings/quality")) as unknown as Quality,
    ...settingsOptions,
  });

export const missesQuery = queryOptions({
  queryKey: ["settings", "misses", "list"],
  queryFn: async () => ok(await api.GET("/api/settings/misses")),
  ...settingsOptions,
});

export const sessionsQuery = (page: "calibrate" | "overview") =>
  queryOptions({
    queryKey: ["settings", page, "sessions"],
    queryFn: async () => ok(await api.GET("/api/settings/calibrate")) as unknown as SessionRow[],
    ...settingsOptions,
  });

export const sessionQuery = (id: number) =>
  queryOptions({
    queryKey: ["settings", `calibrate/${id}`],
    queryFn: async () => ok(await api.GET("/api/settings/calibrate/{session_id}", { params: { path: { session_id: id } } })) as unknown as Session,
    ...settingsOptions,
  });

export const runsQuery = (page: "overview" | "runs") =>
  queryOptions({
    queryKey: ["settings", page, "runs"],
    queryFn: async () => ok(await api.GET("/api/settings/runs")) as unknown as Run[],
    ...settingsOptions,
  });

export const scheduleQuery = queryOptions({
  queryKey: ["settings", "runs", "schedule"],
  queryFn: async () => ok(await api.GET("/api/settings/schedule")),
  staleTime: 5 * 60_000,
});

export const configQuery = queryOptions({
  queryKey: ["settings", "configuration", "config"],
  queryFn: async () => ok(await api.GET("/api/settings/config")) as Record<string, unknown>,
  staleTime: 5 * 60_000,
});

/** The reads of a Settings page and its subpages (`page` and `page/...`), for a write that changes what they show. */
export const settingsReads = (page: string) => ({
  predicate: (q: { queryKey: readonly unknown[] }) => q.queryKey[0] === "settings" && typeof q.queryKey[1] === "string" && (q.queryKey[1] === page || q.queryKey[1].startsWith(`${page}/`)),
});

/** `page` is "integrations" or an integration's events page, `integrations/<name>`. */
export const integrationsQuery = (page: string) =>
  queryOptions({
    queryKey: ["settings", page, "cards"],
    queryFn: async () => ok(await api.GET("/api/settings/integrations")),
    ...settingsOptions,
  });

/** `page` is "topics", "taxonomy" or a topic's editor, `topics/<slug>`. */
export const taxonomyStatsQuery = (page: string) =>
  queryOptions({
    queryKey: ["settings", page, "stats"],
    queryFn: async () => ok(await api.GET("/api/settings/taxonomy/stats")),
    ...settingsOptions,
  });

/** How many papers of the last 90 days each of a topic's keywords matches, for the editor. */
export const keywordsQuery = (slug: string, enabled = true) =>
  queryOptions({
    queryKey: ["settings", `topics/${slug}`, "keywords"],
    queryFn: async () => ok(await api.GET("/api/settings/topics/{slug}/keywords", { params: { path: { slug } } })),
    staleTime: 5 * 60_000,
    enabled,
  });
