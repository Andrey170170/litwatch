/**
 * The writes, each showing the server's acknowledgement and refreshing what it may have
 * changed. Every write returns `{message, saved, grades}`.
 */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api, ok, type Ack, type LabelValue, type ReactionReason, type ReactionValue } from "./client";
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
