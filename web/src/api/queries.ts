/** TanStack Query keys and fetchers over the typed client, one per endpoint. */
import { queryOptions } from "@tanstack/react-query";
import { api, ok } from "./client";

export const statusQuery = queryOptions({
  queryKey: ["status"],
  queryFn: async () => ok(await api.GET("/api/status")),
  staleTime: 60_000,
});

export const digestQuery = (before?: string) =>
  queryOptions({
    queryKey: ["digest", before ?? null],
    queryFn: async () => ok(await api.GET("/api/digest", { params: { query: { before, limit: 14 } } })),
  });

export const issueQuery = (id: number) =>
  queryOptions({
    queryKey: ["issue", id],
    queryFn: async () => ok(await api.GET("/api/issues/{issue_id}", { params: { path: { issue_id: id } } })),
  });

export const paperQuery = (id: string) =>
  queryOptions({
    queryKey: ["paper", id],
    queryFn: async () => ok(await api.GET("/api/papers/{paper_id}", { params: { path: { paper_id: id } } })),
  });

export const topicsQuery = queryOptions({
  queryKey: ["topics"],
  queryFn: async () => ok(await api.GET("/api/topics")),
  staleTime: 30_000,
});

/** A Radar or Pulse topic's reviews, newest first. */
export const topicReviewsQuery = (slug: string) =>
  queryOptions({
    queryKey: ["topics", "reviews", slug],
    queryFn: async () => ok(await api.GET("/api/topics/{slug}/reviews", { params: { path: { slug } } })),
  });

export type PapersFilter = {
  q?: string;
  topic?: string;
  adjacent?: boolean;
  saved?: boolean;
  year?: number;
  sort?: string;
  limit?: number;
  offset?: number;
};

export const papersQuery = (f: PapersFilter) =>
  queryOptions({
    queryKey: ["papers", f],
    queryFn: async () =>
      ok(
        await api.GET("/api/papers", {
          params: {
            query: {
              q: f.q || undefined,
              topic: f.topic || undefined,
              adjacent: f.adjacent || undefined,
              saved: f.saved,
              year: f.year,
              sort: f.sort,
              limit: f.limit ?? 50,
              offset: f.offset ?? 0,
            },
          },
        }),
      ),
    placeholderData: (previous) => previous,
  });
