/**
 * The writes, each showing the server's acknowledgement and refreshing what it may have
 * changed. Every write returns `{message, saved, grades}`.
 */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ApiError, api, ok, type Ack, type LabelValue, type ReactionReason, type ReactionValue } from "./client";
import { toast } from "@/lib/toast";
import { invalidateAfterWrite } from "./invalidate";

function useAck() {
  const qc = useQueryClient();
  return {
    onSuccess: (ack: Ack) => {
      toast(ack.message);
      invalidateAfterWrite(qc);
    },
    onError: (e: Error) => toast(e.message || "That did not save", "error"),
  };
}

export function useSave(paperId: string) {
  return useMutation({
    mutationFn: async (saved: boolean) =>
      ok(await api.PUT("/api/library/{paper_id}", { params: { path: { paper_id: paperId } }, body: { saved } })),
    ...useAck(),
  });
}

export function useReact(paperId: string) {
  return useMutation({
    mutationFn: async (v: { topic: string | null; value: ReactionValue; reason: ReactionReason | null }) =>
      ok(
        await api.PUT("/api/reactions/{paper_id}", {
          params: { path: { paper_id: paperId } },
          body: { topic: v.topic, value: v.value, reason: v.reason },
        }),
      ),
    ...useAck(),
  });
}

export function useUnreact(paperId: string) {
  return useMutation({
    mutationFn: async (topic: string | null) =>
      ok(
        await api.DELETE("/api/reactions/{paper_id}", {
          params: { path: { paper_id: paperId }, query: { topic: topic ?? undefined } },
        }),
      ),
    ...useAck(),
  });
}

export function useLabel(paperId: string) {
  return useMutation({
    mutationFn: async (v: { topic: string; value: LabelValue }) =>
      ok(
        await api.PUT("/api/word/{topic}/{paper_id}", {
          params: { path: { topic: v.topic, paper_id: paperId } },
          body: { value: v.value },
        }),
      ),
    ...useAck(),
  });
}

export function useUnlabel(paperId: string) {
  return useMutation({
    mutationFn: async (topic: string) =>
      ok(await api.DELETE("/api/word/{topic}/{paper_id}", { params: { path: { topic, paper_id: paperId } } })),
    ...useAck(),
  });
}

export function useMiss() {
  return useMutation({
    mutationFn: async (v: { paper_id: string; topic: string }) => ok(await api.POST("/api/misses", { body: v })),
    ...useAck(),
  });
}

export function useVisit() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (slug: string) => {
      await api.POST("/api/topics/{slug}/visit", { params: { path: { slug } } });
    },
    // A visit moves only the "new since" counts.
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["topics"] }),
  });
}

/** What the server said about a correction that failed, in plain words (SPEC §6). */
export function publicationError(e: unknown): string {
  if (e instanceof ApiError) {
    if (e.status === 404) return "DBLP has no record with that key.";
    if (e.status === 502) return "DBLP did not answer; try again in a while.";
    if (e.status === 422) return e.message || "That is not a DOI or a DBLP key.";
  }
  return e instanceof Error && e.message ? e.message : "That did not save";
}

/** The owner names the published version: a DBLP key, a DOI, or that there is none. */
export function usePublication(paperId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (v: { dblp_key?: string; doi?: string; none?: boolean }) =>
      ok(
        await api.PUT("/api/papers/{paper_id}/publication", {
          params: { path: { paper_id: paperId } },
          body: { dblp_key: v.dblp_key ?? null, doi: v.doi ?? null, none: v.none ?? false },
        }),
      ),
    onSuccess: (record) => {
      toast(record ? `Published version set: ${[record.venue, record.year].filter(Boolean).join(" ") || "a record"}` : "Noted: no published version");
      void qc.invalidateQueries({ queryKey: ["paper", paperId] });
    },
    onError: (e: Error) => toast(publicationError(e), "error"),
  });
}

/** Back to automatic lookup: the stored version, the owner's choice included, is dropped. */
export function useForgetPublication(paperId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async () => ok(await api.DELETE("/api/papers/{paper_id}/publication", { params: { path: { paper_id: paperId } } })),
    onSuccess: () => {
      toast("Back to automatic lookup");
      void qc.invalidateQueries({ queryKey: ["paper", paperId] });
    },
    onError: (e: Error) => toast(e.message || "That did not save", "error"),
  });
}

/** Ask for a deep card; the card shows on the paper once an integration has written it. */
export function useRequestCard(paperId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async () => ok(await api.POST("/api/papers/{paper_id}/card", { params: { path: { paper_id: paperId } } })),
    onSuccess: () => toast("Deep card requested"),
    onError: (e: Error & { status?: number }) => {
      // 409: a card arrived meanwhile, or nothing makes cards any more; the page tells.
      if (e.status === 409) void qc.invalidateQueries({ queryKey: ["paper", paperId] });
      toast(e.status === 409 ? e.message : e.message || "The request did not go through", "error");
    },
  });
}

/** Fetch a paper a provider found into the collection (the lookup, by arXiv id). */
export function useFetchPaper() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (id: string) => ok(await api.POST("/api/lookup", { body: { q: id } })),
    onSuccess: (r) => {
      toast(r.fetched ? "Fetched into the collection" : "Already in the collection");
      void qc.invalidateQueries({ queryKey: ["papers"] });
    },
    onError: (e: Error) => toast(e.message || "Could not fetch it", "error"),
  });
}

/** BibTeX for chosen papers, as text. */
export async function bibtexFor(ids: string[]): Promise<string> {
  const r = await api.POST("/api/bibtex", { body: { ids }, parseAs: "text" });
  return ok(r);
}

/** BibTeX for everything a table filter matches, as text. */
export async function bibtexForFilter(f: {
  q?: string;
  topic?: string;
  adjacent?: boolean;
  saved?: boolean;
  year?: number;
}): Promise<string> {
  const r = await api.GET("/api/bibtex", {
    params: { query: { q: f.q || undefined, topic: f.topic || undefined, adjacent: f.adjacent || undefined, saved: f.saved, year: f.year } },
    parseAs: "text",
  });
  return ok(r);
}

export async function copyText(text: string, said: string) {
  try {
    await navigator.clipboard.writeText(text);
    toast(said);
  } catch {
    toast("Could not reach the clipboard", "error");
  }
}

export function download(text: string, name: string) {
  const url = URL.createObjectURL(new Blob([text], { type: "application/x-bibtex" }));
  const a = Object.assign(document.createElement("a"), { href: url, download: name });
  document.body.appendChild(a);
  a.click();
  // Safari saves an empty file when the URL is revoked in the same task as the click.
  window.setTimeout(() => {
    a.remove();
    URL.revokeObjectURL(url);
  }, 1000);
}
