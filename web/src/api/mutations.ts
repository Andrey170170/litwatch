/**
 * The owner's actions. Each shows its result in place: the paper's page is refetched and a changed Save is patched
 * into every list row that shows it. Lists are otherwise left as they are, so the way back finds them unchanged and
 * nothing reorders under the reader; a list reached going forward starts afresh (`freshVisit`). A toast carries only
 * what the API says the page cannot show.
 */
import { useMutation, useMutationState, useQueryClient, type QueryClient } from "@tanstack/react-query";
import { ApiError, api, ok, type Schemas } from "./client";
import { qualityQuery } from "./queries";
import { rememberCard } from "@/lib/remember";
import { type Quality, settleFromPaper } from "@/lib/settings";
import { plural } from "@/lib/format";
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

/** After a write on a paper: refetch its page and patch its star in lists; the lists change no further until the
 * reader goes to one afresh. The write counts as pending until the page is refetched, so the controls show the new
 * state before they re-enable. An open state the refetched page no longer carries is marked settled, in place, on
 * the cached Needs you page (DESIGN.md: settling one settles it there too). */
export async function settled(qc: QueryClient, paperId: string, ack?: Ack) {
  if (ack) patchSaved(qc, paperId, ack.saved);
  void qc.invalidateQueries({ queryKey: ["status"] });
  await qc.invalidateQueries({ queryKey: ["paper", paperId] });
  if (qc.getQueryState(["paper", paperId])?.status === "error") toast("Recorded, but the page could not be refreshed", "error");
  const paper = qc.getQueryData<Schemas["Paper"]>(["paper", paperId]);
  if (paper) qc.setQueryData<Quality>(qualityQuery.queryKey, (old) => old && settleFromPaper(old, paper));
}

export function said(e: unknown, fallback = "That did not go through"): string {
  return e instanceof ApiError && e.message ? e.message : e instanceof Error && e.message ? e.message : fallback;
}

const PAPER_WRITE = ["paper-write"];

/** Whether a write on this paper is in flight, wherever it was started: a control that closed and reopened
 * meanwhile still waits for it, so a Clear cannot land before a slower change and be undone by it. */
function usePaperBusy(paperId: string): boolean {
  const ids = useMutationState({
    filters: { mutationKey: PAPER_WRITE, status: "pending" },
    select: (m) => (m.state.variables as { id?: string } | undefined)?.id,
  });
  return ids.includes(paperId);
}

/**
 * A write on one paper, one at a time per paper. The paper's id is taken when `mutate` is called, as one of the
 * write's variables: the hook's options follow the latest render, so a page that moves to another paper before
 * the write runs or returns would otherwise write to, or settle, the wrong one.
 */
function usePaperMutation<V, R>(paperId: string, call: (id: string, v: V) => Promise<R>, done: (qc: QueryClient, id: string, r: R) => void | Promise<void>) {
  const qc = useQueryClient();
  const m = useMutation({
    mutationKey: PAPER_WRITE,
    mutationFn: ({ id, v }: { id: string; v: V }) => call(id, v),
    onSuccess: (r, { id }) => done(qc, id, r),
    onError: (e) => toast(said(e), "error"),
  });
  const busy = usePaperBusy(paperId);
  return { isPending: busy, mutate: (v: V, options?: { onSuccess?: (r: R) => void; onError?: (e: unknown) => void }) => m.mutate({ id: paperId, v }, options) };
}

const acked = (qc: QueryClient, id: string, ack: Ack) => {
  toast(ack.message);
  return settled(qc, id, ack);
};

export function useSave(paperId: string) {
  return usePaperMutation(
    paperId,
    async (id, saved: boolean) => ok(await api.PUT("/api/library/{paper_id}", { params: { path: { paper_id: id } }, body: { saved } })),
    acked,
  );
}

export function useReact(paperId: string) {
  return usePaperMutation(
    paperId,
    async (id, v: { topic: string | null; value: Schemas["ReactionValue"]; reason: Schemas["ReactionReason"] }) =>
      ok(await api.PUT("/api/reactions/{paper_id}", { params: { path: { paper_id: id } }, body: v })),
    acked,
  );
}

export function useUnreact(paperId: string) {
  return usePaperMutation(
    paperId,
    async (id, topic: string | null) =>
      ok(await api.DELETE("/api/reactions/{paper_id}", { params: { path: { paper_id: id }, query: { topic: topic ?? undefined } } })),
    acked,
  );
}

export function useLabel(paperId: string) {
  return usePaperMutation(
    paperId,
    async (id, v: { topic: string; value: Schemas["LabelValue"] }) =>
      ok(await api.PUT("/api/labels/{topic}/{paper_id}", { params: { path: { topic: v.topic, paper_id: id } }, body: { value: v.value } })),
    acked,
  );
}

export function useUnlabel(paperId: string) {
  return usePaperMutation(
    paperId,
    async (id, topic: string) => ok(await api.DELETE("/api/labels/{topic}/{paper_id}", { params: { path: { topic, paper_id: id } } })),
    acked,
  );
}

export function useMiss(paperId: string) {
  return usePaperMutation(
    paperId,
    async (id, v: { topic: string; fit: "central" | "related" }) => ok(await api.POST("/api/misses", { body: { paper_id: id, ...v } })),
    acked,
  );
}

/** Process: fetch a paper the collection lacks, by arXiv id. */
export function useProcess() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (id: string) => ok(await api.POST("/api/lookup", { body: { q: id } })),
    onSuccess: (r, id) => {
      toast(r.fetched ? "Fetched into the collection; it is processed on the next run" : "Already in the collection");
      return qc.invalidateQueries({ queryKey: ["paper", id] });
    },
    onError: (e) => toast(said(e, "Could not fetch it"), "error"),
  });
}

/** Deep read: ask for a card; the page shows it once an integration has written it. */
export function useDeepRead(paperId: string) {
  const qc = useQueryClient();
  return usePaperMutation(
    paperId,
    async (id, _: undefined) => {
      try {
        return ok(await api.POST("/api/papers/{paper_id}/card", { params: { path: { paper_id: id } } }));
      } catch (e) {
        // a 409 means the page is behind (a card exists, or none can be asked): refetch the paper asked for
        if (e instanceof ApiError && e.status === 409) void qc.invalidateQueries({ queryKey: ["paper", id] });
        throw e;
      }
    },
    // remembered here, not by the page, which may have gone by the time the request returns
    (_, id) => {
      rememberCard(id);
      toast("Deep card requested");
    },
  );
}

/** The owner names the published version by DOI, or that there is none. */
export function usePublication(paperId: string) {
  return usePaperMutation(
    paperId,
    async (id, v: { doi?: string; none?: boolean }) =>
      ok(
        await api.PUT("/api/papers/{paper_id}/publication", {
          params: { path: { paper_id: id } },
          body: { doi: v.doi ?? null, none: v.none ?? false, dblp_key: null },
        }),
      ),
    (qc, id, record) => {
      toast(record ? `Published version set: ${[record.venue, record.year].filter(Boolean).join(" ") || "a record"}` : "Noted: no published version");
      return settled(qc, id);
    },
  );
}

// The open states' actions (pipeline.md, section 9), each named by the paper revision the page showed.

const stepped = (done: string) => (qc: QueryClient, id: string) => {
  toast(done);
  return settled(qc, id);
};

export function useDismiss(paperId: string) {
  return usePaperMutation(
    paperId,
    async (id, revision: number) =>
      ok(await api.POST("/api/settings/classification/{paper_id}/dismiss", { params: { path: { paper_id: id } }, body: { revision } })),
    stepped("Dismissed; it stays searchable in the archive"),
  );
}

export function useClassifyByHand(paperId: string) {
  return usePaperMutation(
    paperId,
    async (id, v: { revision: number; classes: string[]; primary?: string }) =>
      ok(await api.PUT("/api/settings/classification/{paper_id}", { params: { path: { paper_id: id } }, body: v })),
    stepped("Classified; it goes on as usual"),
  );
}

export function useFixText(paperId: string) {
  return usePaperMutation(
    paperId,
    async (id, v: { revision: number; title?: string; abstract?: string }) =>
      ok(await api.POST("/api/settings/classification/{paper_id}/correction", { params: { path: { paper_id: id } }, body: v })),
    stepped("Text fixed; the next run classifies it again"),
  );
}

export function useRetryAnalysis(paperId: string) {
  return usePaperMutation(
    paperId,
    async (id, revision: number) =>
      ok(await api.POST("/api/settings/analysis/{paper_id}/retry", { params: { path: { paper_id: id } }, body: { revision } })),
    stepped("Analysis gets three more runs, from the next one"),
  );
}

export function useGradeNow(paperId: string) {
  return usePaperMutation(
    paperId,
    async (id, v: { topic: string; revision: number }) =>
      ok(
        await api.POST("/api/settings/grading/{topic}/{paper_id}/now", {
          params: { path: { topic: v.topic, paper_id: id } },
          body: { revision: v.revision },
        }),
      ),
    (qc, id, r) => {
      toast(r.graded ? `Graded: ${r.fit}` : `Grading failed again: ${r.error ?? "no verdict"}`, r.graded ? "plain" : "error");
      return settled(qc, id);
    },
  );
}

/** Run a full-text pass that waits on the owner now: failed three runs, or awaiting its text over a week. A 409
 * means the page is behind (it ran, or the text changed): the paper is refetched. */
export function useRunPassNow(paperId: string) {
  const qc = useQueryClient();
  return usePaperMutation(
    paperId,
    async (id, passId: number) => {
      try {
        return ok(await api.POST("/api/settings/fulltext/{pass_id}/now", { params: { path: { pass_id: passId } } }));
      } catch (e) {
        if (e instanceof ApiError && e.status === 409) void qc.invalidateQueries({ queryKey: ["paper", id] });
        throw e;
      }
    },
    stepped("Running the pass now; its Brief shows here once written"),
  );
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

// -- Settings (DESIGN.md, "Settings") ---------------------------------------------------------------------------

/** Follow a class, or save its topic's edit. The tree and the topic lists are refetched; the pages show the offer. */
export function useSaveTopic() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async ({ slug, topic }: { slug: string; topic: Schemas["Topic"] }) =>
      ok(await api.PUT("/api/settings/topics/{slug}", { params: { path: { slug } }, body: topic })),
    onSuccess: (_, { slug }) =>
      Promise.all([
        qc.invalidateQueries({ queryKey: ["taxonomy"] }),
        qc.invalidateQueries({ queryKey: ["topics"] }),
        qc.invalidateQueries({ queryKey: ["settings", "taxonomy-stats"] }),
        qc.invalidateQueries({ queryKey: ["settings", "keywords", slug] }),
      ]),
    onError: (e) => toast(said(e, "Could not save it"), "error"),
  });
}

/** The owner's order of the topics, which the sidebar and the Digest follow. */
export function useReorderTopics() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (slugs: string[]) => ok(await api.PUT("/api/settings/topics-order", { body: { slugs } })),
    onSuccess: () => Promise.all([qc.invalidateQueries({ queryKey: ["taxonomy"] }), qc.invalidateQueries({ queryKey: ["topics"] })]),
    onError: (e) => toast(said(e, "Could not save the order"), "error"),
  });
}

export function useDrawSession() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async () => ok(await api.POST("/api/settings/calibrate", { body: {} })),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["settings", "calibrate"] }),
    onError: (e) => toast(said(e, "Could not draw a session"), "error"),
  });
}

/** One paper's answers in a session; the session is refetched so the strip and the review follow, and the paper
 * is settled as after any write on it: its page and the status line refetched, a split the labels settled marked
 * so on Needs you. */
export function useAnswer(sessionId: number) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async ({ position, answer }: { position: number; paperId: string; answer: Schemas["Answer"] }) =>
      ok(await api.PUT("/api/settings/calibrate/{session_id}/items/{position}", { params: { path: { session_id: sessionId, position } }, body: answer })),
    onSuccess: (_, { paperId }) => Promise.all([qc.invalidateQueries({ queryKey: ["settings", "calibrate", sessionId], exact: true }), settled(qc, paperId)]),
    onError: (e) => toast(said(e, "The answer did not go through"), "error"),
  });
}

/** Add the papers waiting for a word to a session. */
export function useMore(sessionId: number) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async () => ok(await api.POST("/api/settings/calibrate/{session_id}/more", { params: { path: { session_id: sessionId } } })),
    onSuccess: (r) => {
      toast(r.added ? `${plural(r.added, "more paper")} added` : "Nothing is waiting for a word");
      return qc.invalidateQueries({ queryKey: ["settings", "calibrate", sessionId] });
    },
    onError: (e) => toast(said(e), "error"),
  });
}

export function useRetryIntegration() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (name: string) => ok(await api.POST("/api/settings/integrations/{name}/retry", { params: { path: { name } } })),
    onSuccess: () => {
      toast("Trying the event again");
      return qc.invalidateQueries({ queryKey: ["settings", "integrations"] });
    },
    onError: (e) => toast(said(e), "error"),
  });
}

/** Skip the failing event the card showed; a 409 means another is failing by now, and the cards are refetched. */
export function useSkipEvent() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async ({ name, eventId }: { name: string; eventId: number }) =>
      ok(await api.POST("/api/settings/integrations/{name}/skip", { params: { path: { name }, query: { event_id: eventId } } })),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["settings", "integrations"] }),
    onError: (e) => {
      toast(said(e), "error");
      if (e instanceof ApiError && e.status === 409) void qc.invalidateQueries({ queryKey: ["settings", "integrations"] });
    },
  });
}
