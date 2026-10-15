/**
 * The owner's actions. Each shows its result in place: the paper's page is refetched, a changed Save is patched
 * into every list row that shows it, and lists are marked stale without refetching, so nothing reorders under
 * the reader. A toast carries only what the API says the page cannot show.
 */
import { useMutation, useQueryClient, type QueryClient } from "@tanstack/react-query";
import { ApiError, api, ok, type Schemas } from "./client";
import { toast } from "@/lib/toast";

type Ack = Schemas["Ack"];
type Row = Schemas["Row"];
type Listed = { rows: Row[] };
type Pages = { pages: Listed[]; pageParams: unknown[] };

function patchSaved(qc: QueryClient, paperId: string, saved: boolean) {
  for (const key of [["papers"], ["topic"]])
    qc.setQueriesData<Pages>({ queryKey: key }, (old) =>
      old?.pages
        ? { ...old, pages: old.pages.map((p) => ({ ...p, rows: p.rows.map((r) => (r.id === paperId ? { ...r, saved } : r)) })) }
        : old,
    );
}

/** After a write on a paper: refetch its page, patch its star in lists, leave the rest to the next visit. */
export function settled(qc: QueryClient, paperId: string, ack?: Ack) {
  if (ack) patchSaved(qc, paperId, ack.saved);
  void qc.invalidateQueries({ queryKey: ["paper", paperId] });
  void qc.invalidateQueries({ queryKey: ["papers"], refetchType: "none" });
  void qc.invalidateQueries({ queryKey: ["topic"], refetchType: "none" });
  void qc.invalidateQueries({ queryKey: ["topics"], refetchType: "none" });
  void qc.invalidateQueries({ queryKey: ["status"] });
}

export function said(e: unknown, fallback = "That did not go through"): string {
  return e instanceof ApiError && e.message ? e.message : e instanceof Error && e.message ? e.message : fallback;
}

function useAck(paperId: string) {
  const qc = useQueryClient();
  return {
    onSuccess: (ack: Ack) => {
      toast(ack.message);
      settled(qc, paperId, ack);
    },
    onError: (e: unknown) => toast(said(e), "error"),
  };
}

export function useSave(paperId: string) {
  return useMutation({
    mutationFn: async (saved: boolean) =>
      ok(await api.PUT("/api/library/{paper_id}", { params: { path: { paper_id: paperId } }, body: { saved } })),
    ...useAck(paperId),
  });
}

export function useReact(paperId: string) {
  return useMutation({
    mutationFn: async (v: { topic: string | null; value: Schemas["ReactionValue"]; reason: Schemas["ReactionReason"] }) =>
      ok(await api.PUT("/api/reactions/{paper_id}", { params: { path: { paper_id: paperId } }, body: v })),
    ...useAck(paperId),
  });
}

export function useUnreact(paperId: string) {
  return useMutation({
    mutationFn: async (topic: string | null) =>
      ok(await api.DELETE("/api/reactions/{paper_id}", { params: { path: { paper_id: paperId }, query: { topic: topic ?? undefined } } })),
    ...useAck(paperId),
  });
}

export function useLabel(paperId: string) {
  return useMutation({
    mutationFn: async (v: { topic: string; value: Schemas["LabelValue"] }) =>
      ok(await api.PUT("/api/labels/{topic}/{paper_id}", { params: { path: { topic: v.topic, paper_id: paperId } }, body: { value: v.value } })),
    ...useAck(paperId),
  });
}

export function useUnlabel(paperId: string) {
  return useMutation({
    mutationFn: async (topic: string) =>
      ok(await api.DELETE("/api/labels/{topic}/{paper_id}", { params: { path: { topic, paper_id: paperId } } })),
    ...useAck(paperId),
  });
}

export function useMiss(paperId: string) {
  return useMutation({
    mutationFn: async (v: { topic: string; fit: "central" | "related" }) =>
      ok(await api.POST("/api/misses", { body: { paper_id: paperId, ...v } })),
    ...useAck(paperId),
  });
}

/** Process: fetch a paper the collection lacks, by arXiv id. */
export function useProcess() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (id: string) => ok(await api.POST("/api/lookup", { body: { q: id } })),
    onSuccess: (r) => {
      toast(r.fetched ? "Fetched into the collection; it is processed on the next run" : "Already in the collection");
      void qc.invalidateQueries({ queryKey: ["papers"], refetchType: "none" });
    },
    onError: (e) => toast(said(e, "Could not fetch it"), "error"),
  });
}

/** Deep read: ask for a card; the page shows it once an integration has written it. */
export function useDeepRead(paperId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async () => ok(await api.POST("/api/papers/{paper_id}/card", { params: { path: { paper_id: paperId } } })),
    onSuccess: () => toast("Deep card requested"),
    onError: (e) => {
      if (e instanceof ApiError && e.status === 409) void qc.invalidateQueries({ queryKey: ["paper", paperId] });
      toast(said(e), "error");
    },
  });
}

/** The owner names the published version by DOI, or that there is none. */
export function usePublication(paperId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (v: { doi?: string; none?: boolean }) =>
      ok(
        await api.PUT("/api/papers/{paper_id}/publication", {
          params: { path: { paper_id: paperId } },
          body: { doi: v.doi ?? null, none: v.none ?? false, dblp_key: null },
        }),
      ),
    onSuccess: (record) => {
      toast(record ? `Published version set: ${[record.venue, record.year].filter(Boolean).join(" ") || "a record"}` : "Noted: no published version");
      void qc.invalidateQueries({ queryKey: ["paper", paperId] });
    },
    onError: (e) => toast(said(e), "error"),
  });
}

// The open states' actions (pipeline.md, section 9), each named by the paper revision the page showed.

function useSettle(paperId: string, done: string) {
  const qc = useQueryClient();
  return {
    onSuccess: () => {
      toast(done);
      settled(qc, paperId);
    },
    onError: (e: unknown) => toast(said(e), "error"),
  };
}

export function useDismiss(paperId: string) {
  return useMutation({
    mutationFn: async (revision: number) =>
      ok(await api.POST("/api/settings/classification/{paper_id}/dismiss", { params: { path: { paper_id: paperId } }, body: { revision } })),
    ...useSettle(paperId, "Dismissed; it stays searchable in the archive"),
  });
}

export function useClassifyByHand(paperId: string) {
  return useMutation({
    mutationFn: async (v: { revision: number; classes: string[]; primary?: string }) =>
      ok(await api.PUT("/api/settings/classification/{paper_id}", { params: { path: { paper_id: paperId } }, body: v })),
    ...useSettle(paperId, "Classified; it goes on as usual"),
  });
}

export function useFixText(paperId: string) {
  return useMutation({
    mutationFn: async (v: { revision: number; title?: string; abstract?: string }) =>
      ok(await api.POST("/api/settings/classification/{paper_id}/correction", { params: { path: { paper_id: paperId } }, body: v })),
    ...useSettle(paperId, "Text fixed; the next run classifies it again"),
  });
}

export function useRetryAnalysis(paperId: string) {
  return useMutation({
    mutationFn: async (revision: number) =>
      ok(await api.POST("/api/settings/analysis/{paper_id}/retry", { params: { path: { paper_id: paperId } }, body: { revision } })),
    ...useSettle(paperId, "Analysis gets three more runs, from the next one"),
  });
}

export function useGradeNow(paperId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (v: { topic: string; revision: number }) =>
      ok(
        await api.POST("/api/settings/grading/{topic}/{paper_id}/now", {
          params: { path: { topic: v.topic, paper_id: paperId } },
          body: { revision: v.revision },
        }),
      ),
    onSuccess: (r) => {
      toast(r.graded ? `Graded: ${r.fit}` : `Grading failed again: ${r.error ?? "no verdict"}`, r.graded ? "plain" : "error");
      settled(qc, paperId);
    },
    onError: (e) => toast(said(e), "error"),
  });
}

/** BibTeX for one paper, as text; the API looks the published version up first. */
export async function bibtexFor(id: string): Promise<string> {
  return ok(await api.POST("/api/bibtex", { body: { ids: [id] }, parseAs: "text" }));
}

export async function copyText(text: string, done: string) {
  try {
    await navigator.clipboard.writeText(text);
    toast(done);
  } catch {
    toast("Could not reach the clipboard", "error");
  }
}
