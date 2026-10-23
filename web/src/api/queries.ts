/** TanStack Query options over the typed client: one per endpoint, and one per shelf of a list. */
import { infiniteQueryOptions, queryOptions, type QueryClient, type QueryKey } from "@tanstack/react-query";
import { api, ok } from "./client";
import type { RerunScope } from "@/lib/jobs";
import type { Shelf } from "@/lib/rarity";
import type { Quality, Run, Session, SessionRow } from "@/lib/settings";
import { sortOf, type ListSearch } from "@/lib/search";
import { addDays, nextSpan } from "@/lib/digests";

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

/** The search box's reach beyond the collection: the archive's size, which changes slowly. */
export const searchScopeQuery = queryOptions({
  queryKey: ["search-scope"],
  queryFn: async () => ok(await api.GET("/api/search/scope")),
  staleTime: 60 * 60_000,
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
  // The Digest's views, each under its own key (below), and a digest's page; the slide-over on the desktop is a
  // search param on the view, no visit.
  const digest = path === "/" ? "day" : path === "/week" ? "week" : path === "/history" ? "history" : /^\/digests\/\d+$/.test(path) ? path.slice("/digests/".length) : null;
  const key =
    path === "/papers" ? ["papers"] : path === "/topics" ? ["topics"] : topic?.[1] ? ["topic", decodeURIComponent(topic[1])] : settings ? ["settings", settings[1] ? decodeURIComponent(settings[1]) : "overview"] : digest ? ["digest", digest] : null;
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

// -- The Digest (DESIGN.md, "Digest") ----------------------------------------------------------------------------

/** A Digest view's read, under `["digest", <view>, ...]`, kept as a list is (`listOptions`): a Day or Week stepped
 * to stays as read for the visit and the way back, and a fresh visit of the view refetches it behind what is shown
 * (`freshVisit`). A digest's own read is under its id, the same on the desktop's slide-over and the phone's page. */
const digestRead = <T,>(key: (string | number)[], queryFn: () => Promise<T>) => queryOptions({ queryKey: ["digest", ...key], queryFn, ...listOptions });

export const dayQuery = (day: string) => digestRead(["day", day], async () => ok(await api.GET("/api/digests/days/{day}", { params: { path: { day } } })));

/** The week holding `day`. */
export const weekQuery = (day: string) => digestRead(["week", day], async () => ok(await api.GET("/api/digests/weeks/{day}", { params: { path: { day } } })));

export const digestQuery = (id: number) => digestRead([String(id)], async () => ok(await api.GET("/api/digests/{digest_id}", { params: { path: { digest_id: id } } })));

/** History back from `until`, a span of days at a time (`SPAN`), older pages as the reader scrolls until the
 * archive's start. */
export const historyQuery = (until: string, span: number) =>
  infiniteQueryOptions({
    queryKey: ["digest", "history", until],
    queryFn: async ({ pageParam }) => ok(await api.GET("/api/digests", { params: { query: { until: pageParam, since: addDays(pageParam, -span) } } })),
    initialPageParam: until,
    getNextPageParam: (_last, pages) => nextSpan(pages),
    ...listOptions,
  });

// -- Settings (DESIGN.md, "Settings") ---------------------------------------------------------------------------

/** A Settings page's read, under the page's own key, `["settings", <its path under /settings>, name]`: refetched
 * in the background on each visit by a link (`freshVisit`), and still for the visit, kept as a list is (`listOptions`:
 * half an hour after the reader leaves it, so Back after a long read finds it): nothing moves under the reader while a
 * page is open or on the way back to it, and a page left keeps its rows whatever its siblings or subpages read
 * meanwhile (two pages reading one endpoint each keep their own copy). What a write settles is patched into the
 * cached page (`settle`, `settleFromPaper`); a write that changes a page invalidates its reads (`settingsReads`). */
const settingsRead = <T,>(page: string, name: string, queryFn: () => Promise<T>) => queryOptions({ queryKey: ["settings", page, name], queryFn, ...listOptions });

/** The reads of a Settings page and its subpages (`page` and `page/...`), for a write that changes what they show. */
export const settingsReads = (page: string) => ({
  predicate: (q: { queryKey: readonly unknown[] }) => q.queryKey[0] === "settings" && typeof q.queryKey[1] === "string" && (q.queryKey[1] === page || q.queryKey[1].startsWith(`${page}/`)),
});

/** The one way a cached read is patched with what a write settled, whatever read of it is in flight. Three cases:
 * a snapshot exists, and is patched in place (the patch matches rows by their identity) after the read in flight
 * is cancelled, since begun before the write it would land over the patch; the read is the initial one, which the
 * cancel interrupted, and is started again rather than left pending with nothing shown; the read was never asked
 * for (the page not visited), and nothing is fetched for a page nobody shows. */
export async function patchRead<T>(qc: QueryClient, key: QueryKey, patch: (old: T) => T) {
  if (!qc.getQueryState(key)) return;
  await qc.cancelQueries({ queryKey: key, exact: true });
  const old = qc.getQueryData<T>(key);
  if (old !== undefined) qc.setQueryData<T>(key, patch(old));
  else void qc.invalidateQueries({ queryKey: key, exact: true });
}

export const qualityQuery = (page: "needs" | "overview" | "runs") => settingsRead(page, "quality", async () => ok(await api.GET("/api/settings/quality")) as unknown as Quality);

export const missesQuery = settingsRead("misses", "list", async () => ok(await api.GET("/api/settings/misses")));

export const sessionsQuery = (page: "calibrate" | "overview") => settingsRead(page, "sessions", async () => ok(await api.GET("/api/settings/calibrate")) as unknown as SessionRow[]);

export const sessionQuery = (id: number) =>
  settingsRead(`calibrate/${id}`, "session", async () => ok(await api.GET("/api/settings/calibrate/{session_id}", { params: { path: { session_id: id } } })) as unknown as Session);

export const runsQuery = (page: "overview" | "runs") => settingsRead(page, "runs", async () => ok(await api.GET("/api/settings/runs")) as unknown as Run[]);

export const scheduleQuery = settingsRead("runs", "schedule", async () => ok(await api.GET("/api/settings/schedule")));

/** The jobs in their groups, for Runs and cost (`page` "runs") and the Jobs page ("runs/jobs"). */
export const jobsQuery = (page: "runs" | "runs/jobs") => settingsRead(page, "jobs", async () => ok(await api.GET("/api/jobs")));

/** The re-run offer for a topic just saved, for the scopes ticked (and the date range when it is one of them). */
export const rerunOfferQuery = (slug: string, scopes: RerunScope[], since: string | null, until: string | null) =>
  queryOptions({
    queryKey: ["settings", `topics/${slug}`, "rerun-offer", scopes, since, until],
    queryFn: async () => ok(await api.GET("/api/jobs/reruns/offer", { params: { query: { topic: slug, scopes, since, until } } })),
    // Read afresh each time the offer opens: the save it follows changes the counts (keywords) and versions.
    staleTime: 0,
    refetchOnWindowFocus: false,
  });

/** Both pages' reads of the jobs, for a write that changes them. */
export const JOBS_PAGES = ["runs", "runs/jobs"] as const;

export const configQuery = settingsRead("configuration", "config", async () => ok(await api.GET("/api/settings/config")) as Record<string, unknown>);

/** `page` is "integrations" or an integration's events page, `integrations/<name>`. */
export const integrationsQuery = (page: string) => settingsRead(page, "cards", async () => ok(await api.GET("/api/settings/integrations")));

/** The tree as a Settings page reads it (`page` is "topics", "taxonomy" or a topic's editor, `topics/<slug>`); the
 * paper's page and the labels elsewhere read `taxonomyQuery`. */
export const settingsTaxonomyQuery = (page: string) => settingsRead(page, "taxonomy", async () => ok(await api.GET("/api/settings/taxonomy")));

export const taxonomyStatsQuery = (page: string) => settingsRead(page, "stats", async () => ok(await api.GET("/api/settings/taxonomy/stats")));

/** How many papers of the last 90 days each of a topic's keywords matches, for the editor. */
export const keywordsQuery = (slug: string, enabled = true) => ({
  ...settingsRead(`topics/${slug}`, "keywords", async () => ok(await api.GET("/api/settings/topics/{slug}/keywords", { params: { path: { slug } } }))),
  enabled,
});
