/**
 * The owner's actions. Each shows its result in place: the paper's page is refetched and a changed Save is patched
 * into every list row that shows it. Lists are otherwise left as they are, so the way back finds them unchanged and
 * nothing reorders under the reader; a list reached going forward starts afresh (`freshVisit`). A toast carries only
 * what the API says the page cannot show.
 */
import { useMutation, useMutationState, useQueryClient, type QueryClient } from "@tanstack/react-query";
import { ApiError, api, ok, type Schemas } from "./client";
import { digestQuery, patchRead, qualityQuery, refreshSummary, sessionQuery, sessionsQuery, settingsReads } from "./queries";
import { rememberCard } from "@/lib/remember";
import { acknowledged, labelled, noted, type Quality, type Seen, type Session, type SessionRow, sessionRow, settle, type Settled, settleFromPaper, type Waiting } from "@/lib/settings";
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

/** The one place the cached Needs you page is patched with what a write settled, or unsettled (DESIGN.md: a settled
 * item keeps its result in place, and the sidebar's count follows). It is called from the write's own mutation,
 * with the identity the write took when it was sent, so the row settles whether or not the page is still shown
 * when the write returns; `patchRead` keeps a read in flight from landing over it. */
export function settleNeeds(qc: QueryClient, patch: (q: Quality) => Quality) {
  return patchRead<Quality>(qc, qualityQuery("needs").queryKey, patch);
}

/** A write on a paper (a label, a reaction, an answer) may move its set-aside mark, the one live element of the
 * digests it is in (digests.md): the Digest's cached reads are reread when next shown, kept on screen meanwhile. A
 * read in flight is cancelled first, since begun before the write it would land as fresh. */
async function digestsStale(qc: QueryClient) {
  await qc.cancelQueries({ queryKey: ["digest"] });
  // all are marked stale at once; a read interrupted before it had anything (a view just gone to) is then started
  // again rather than left pending
  await qc.invalidateQueries({ queryKey: ["digest"], refetchType: "none" });
  await qc.refetchQueries({ queryKey: ["digest"], type: "active", predicate: (q) => q.state.data === undefined });
}

/** After a write on a paper: refetch its page and patch its star in lists; the lists change no further until the
 * reader goes to one afresh. The write counts as pending until the page is refetched, so the controls show the new
 * state before they re-enable. An open state the refetched page no longer carries is marked settled, in place, on
 * the cached Needs you page (DESIGN.md: settling one settles it there too). */
export async function settled(qc: QueryClient, paperId: string, ack?: Ack) {
  if (ack) patchSaved(qc, paperId, ack.saved);
  refreshSummary(qc);
  await digestsStale(qc);
  // A paper read before is refetched whether or not its page is open: the reconciliation reads fresh states or
  // none (not the retained ones when the refetch fails). A paper never read has nothing cached to reconcile from;
  // the write that settles a Needs you row settles it by name (`stepped`, `useLabel`).
  await qc.invalidateQueries({ queryKey: ["paper", paperId], refetchType: "all" });
  if (qc.getQueryState(["paper", paperId])?.status === "error") return toast("Recorded, but the page could not be refreshed", "error");
  const paper = qc.getQueryData<Schemas["Paper"]>(["paper", paperId]);
  if (paper) await settleNeeds(qc, (q) => settleFromPaper(q, paper));
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
 * the write runs or returns would otherwise write to, or settle, the wrong one. `done` and `failed` run on the
 * mutation itself, with the variables sent, whatever page is shown when the write returns.
 */
function usePaperMutation<V, R>(paperId: string, call: (id: string, v: V) => Promise<R>, done: (qc: QueryClient, id: string, r: R, v: V) => unknown, failed?: (qc: QueryClient, id: string, v: V, e: unknown) => unknown) {
  const qc = useQueryClient();
  const m = useMutation({
    mutationKey: PAPER_WRITE,
    mutationFn: ({ id, v }: { id: string; v: V }) => call(id, v),
    onSuccess: (r, { id, v }) => done(qc, id, r, v),
    onError: (e, { id, v }) => {
      toast(said(e), "error");
      return failed?.(qc, id, v, e);
    },
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

/** A label, from the paper's page or a split on Needs you, named with the paper revision the page showed: the
 * split's row for that revision settles with it (`labelled`), and the paper is settled as after any write. */
export function useLabel(paperId: string) {
  return usePaperMutation(
    paperId,
    async (id, v: { topic: string; value: Schemas["LabelValue"]; revision: number }) =>
      ok(await api.PUT("/api/labels/{topic}/{paper_id}", { params: { path: { topic: v.topic, paper_id: id } }, body: { value: v.value } })),
    (qc, id, ack, v) => Promise.all([settleNeeds(qc, (q) => labelled(q, id, v.topic, v.value, v.revision)), acked(qc, id, ack)]),
  );
}

/** A label taken back: the split waits again on Needs you, where it was. */
export function useUnlabel(paperId: string) {
  return usePaperMutation(
    paperId,
    async (id, v: { topic: string; revision: number }) => ok(await api.DELETE("/api/labels/{topic}/{paper_id}", { params: { path: { topic: v.topic, paper_id: id } } })),
    (qc, id, ack, v) => Promise.all([settleNeeds(qc, (q) => labelled(q, id, v.topic, null, v.revision)), acked(qc, id, ack)]),
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
      // the card asked over is the one on the page as the request goes, not one a poll reads back meanwhile
      const over = qc.getQueryData<Schemas["Paper"]>(["paper", id])?.card?.written_at ?? null;
      try {
        return { ...ok(await api.POST("/api/papers/{paper_id}/card", { params: { path: { paper_id: id } } })), over };
      } catch (e) {
        // a 409 means the page is behind (a card exists, or none can be asked): refetch the paper asked for
        if (e instanceof ApiError && e.status === 409) void qc.invalidateQueries({ queryKey: ["paper", id] });
        throw e;
      }
    },
    // remembered here, not by the page, which may have gone by the time the request returns
    (qc, id, r) => {
      rememberCard(id, r.over);
      toast("Deep card requested");
      return settled(qc, id); // a paper litwatch lacked is fetched first: the page shows it fetched
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

// The open states' actions (pipeline.md, section 9), each named by the paper revision the page showed. Each
// settles its row on the cached Needs you page from the mutation, by the paper, its revision and the topic or
// pass where a paper has several; a 409 (it ran, the text changed, another action took it) settles the row too,
// with the API's reason, as the item no longer waits on the owner.

/** Which row of `list` the write's variables name, beside its paper. */
type Which<V> = (v: V) => Omit<Settled, "paper_id">;

/** The row's settlement: with `what` once the action is done, with the API's reason on a 409. */
const settling = <V,>(list: Waiting, which: Which<V>) => ({
  done: (qc: QueryClient, id: string, v: V, what: string) => settleNeeds(qc, (q) => settle(q, list, { paper_id: id, ...which(v) }, what)),
  failed: (qc: QueryClient, id: string, v: V, e: unknown) => (e instanceof ApiError && e.status === 409 ? settleNeeds(qc, (q) => settle(q, list, { paper_id: id, ...which(v) }, `No longer waits on the owner: ${said(e)}.`)) : undefined),
});

/** A step's action done: the toast, the row settled with the same words, the paper settled as after any write. */
const stepped = <V,>(list: Waiting, which: Which<V>, done: string) => {
  const on = settling(list, which);
  return [
    (qc: QueryClient, id: string, _r: unknown, v: V) => {
      toast(done);
      return Promise.all([on.done(qc, id, v, `${done}.`), settled(qc, id)]);
    },
    on.failed,
  ] as const;
};

/** The row for the paper's revision the action named, sent as the variable or in it. */
const atRevision = (v: number | { revision: number }) => ({ revision: typeof v === "number" ? v : v.revision });

export function useDismiss(paperId: string) {
  return usePaperMutation(
    paperId,
    async (id, revision: number) =>
      ok(await api.POST("/api/settings/classification/{paper_id}/dismiss", { params: { path: { paper_id: id } }, body: { revision } })),
    ...stepped("awaiting_classification", atRevision, "Dismissed; it stays searchable in the archive"),
  );
}

export function useClassifyByHand(paperId: string) {
  return usePaperMutation(
    paperId,
    async (id, v: { revision: number; classes: string[]; primary?: string }) =>
      ok(await api.PUT("/api/settings/classification/{paper_id}", { params: { path: { paper_id: id } }, body: v })),
    ...stepped("awaiting_classification", atRevision, "Classified by hand; grading follows tonight"),
  );
}

export function useFixText(paperId: string) {
  return usePaperMutation(
    paperId,
    async (id, v: { revision: number; title?: string; abstract?: string }) =>
      ok(await api.POST("/api/settings/classification/{paper_id}/correction", { params: { path: { paper_id: id } }, body: v })),
    ...stepped("awaiting_classification", atRevision, "Text fixed; the next run classifies it again"),
  );
}

export function useRetryAnalysis(paperId: string) {
  return usePaperMutation(
    paperId,
    async (id, revision: number) =>
      ok(await api.POST("/api/settings/analysis/{paper_id}/retry", { params: { path: { paper_id: id } }, body: { revision } })),
    ...stepped("analysis_failed", atRevision, "Analysis gets three more runs, from the next one"),
  );
}

/** Grade it now counts as the first of three more runs: a grading that fails again settles the item too, since
 * the nightly runs take it from here and it no longer waits on the owner. */
export function useGradeNow(paperId: string) {
  const on = settling<{ topic: string; revision: number }>("ungraded", (v) => v);
  return usePaperMutation(
    paperId,
    async (id, v: { topic: string; revision: number }) =>
      ok(
        await api.POST("/api/settings/grading/{topic}/{paper_id}/now", {
          params: { path: { topic: v.topic, paper_id: id } },
          body: { revision: v.revision },
        }),
      ),
    (qc, id, r, v) => {
      toast(r.graded ? `Graded: ${r.fit}` : `Grading failed again: ${r.error ?? "no verdict"}`, r.graded ? "plain" : "error");
      const what = r.graded ? `Graded again: ${r.fit}. The result shows on its topic.` : `Grading failed again: ${r.error ?? "no verdict"}. The nightly runs try twice more.`;
      return Promise.all([on.done(qc, id, v, what), settled(qc, id)]);
    },
    on.failed,
  );
}

/** Run a full-text pass that waits on the owner now: failed three runs (`state` failed), or awaiting its text over
 * a week; with `pdf` ("Use the PDF", for LaTeX that reads badly), on the paper's PDF from then on. A 409 means the
 * page is behind (it ran, or the text changed): the pass's row settles with the reason, and the paper is refetched
 * and the cached Needs you page reconciled from it, as after a write. */
export function useRunPassNow(paperId: string) {
  type Run = { id: number; state: "failed" | "awaiting_text"; pdf?: boolean };
  const on = settling<Run>("fulltext", (v) => ({ id: v.id }));
  return usePaperMutation(
    paperId,
    async (id, v: Run) => {
      const params = { params: { path: { pass_id: v.id } } };
      return ok(await (v.pdf ? api.POST("/api/settings/fulltext/{pass_id}/pdf", params) : api.POST("/api/settings/fulltext/{pass_id}/now", params)));
    },
    (qc, id, _r, v) => {
      toast(`Running the pass now${v.pdf ? ", on the PDF" : ""}; its result shows here once written`);
      const what =
        v.state === "failed"
          ? `Running now${v.pdf ? ", on the PDF" : ""}, first in line, with three more runs.`
          : v.pdf
            ? "Asked for the PDF, at urgent priority; the pass runs on it once it arrives."
            : "Asked for the text again, at urgent priority; the pass runs once it arrives.";
      return Promise.all([on.done(qc, id, v, what), settled(qc, id)]);
    },
    async (qc, id, v, e) => {
      if (!(e instanceof ApiError && e.status === 409)) return;
      await on.failed(qc, id, v, e);
      await settled(qc, id);
    },
  );
}

/** Write a failed digest again now, as a run of its own; the digest comes back with its new state. Written, its
 * Needs you row settles; failed again, the row stays to be written, with the reason. The API answers 409 when it
 * no longer waits (written meanwhile, its topic paused: the row settles with that reason) or another job holds the
 * run lock (the row stays, with the reason). The row is patched from the mutation, so it is whether or not Needs
 * you is still shown when the write returns. */
export function useWriteAgain() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (digestId: number) => ok(await api.POST("/api/digests/{digest_id}/write", { params: { path: { digest_id: digestId } } })),
    onSuccess: async (r, digestId) => {
      // The answer is the digest as it now stands: shown at once where it is open (a read in flight is dropped, so it
      // cannot land over the answer), its squares in the topics' strips recoloured, and the Digest's views reread.
      const key = ["digest", String(digestId)];
      await qc.cancelQueries({ queryKey: key, exact: true });
      qc.setQueryData(key, r);
      await restripe(qc, r);
      digestViewsStale(qc);
      refreshSummary(qc);
      return settleNeeds(qc, (q) => (r.state === "failed" ? noted(q, digestId, `Failed again: ${r.error ?? "no text"}. The next run writes it again.`) : settle(q, "digests", { id: digestId }, "Written again; it shows in the Digest.")));
    },
    onError: async (e, digestId) => {
      toast(said(e), "error");
      if (!(e instanceof ApiError && e.status === 409)) return;
      // A 409 says the server's view moved (written meanwhile, its topic paused, a job running): the digest is
      // reread, its squares follow it, and its views and the counts are reread too.
      const key = ["digest", String(digestId)];
      await qc.cancelQueries({ queryKey: key, exact: true }); // a read begun before the write would be joined, and is old
      const d = await qc.fetchQuery({ ...digestQuery(digestId), staleTime: 0 }).catch(() => null);
      if (d) await restripe(qc, d);
      digestViewsStale(qc);
      refreshSummary(qc);
      return settleNeeds(qc, (q) => (digestGone(e) ? settle(q, "digests", { id: digestId }, `No longer to be written: ${said(e)}.`) : noted(q, digestId, `Not written now: ${said(e)}.`)));
    },
  });
}

/** Acknowledge on Needs you: the owner has seen the items named, and they stop counting in the run status. Once
 * the API has them, their rows fold into the page's Acknowledged section and the summary is reread, so the run
 * status and the sidebar's count follow. */
export function useAcknowledge() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (seen: Seen) => ok(await api.POST("/api/settings/acknowledge", { body: seen })),
    onSuccess: (_r, seen) => {
      refreshSummary(qc);
      return settleNeeds(qc, (q) => acknowledged(q, seen, new Date().toISOString()));
    },
    onError: (e) => toast(said(e), "error"),
  });
}

/** The Digest's views reread when next shown (a view on screen rereads now), their rows kept meanwhile. */
const digestViewsStale = (qc: QueryClient) => {
  for (const view of ["day", "week", "history"]) void qc.invalidateQueries({ queryKey: ["digest", view] });
};

/** A digest written again recolours its square in the topics' strips (`/topics` and its topic's page), in place
 * (`patchRead`: a read in flight does not land over it): the lists around it are not reread, so nothing moves. */
export function restripe(qc: QueryClient, d: Pick<Schemas["Digest"], "id" | "state" | "best_rarity" | "topic">) {
  const square = (s: Schemas["StripDigest"]) => (s.id === d.id ? { ...s, state: d.state, best_rarity: d.best_rarity } : s);
  const card = (t: Schemas["TopicCard"]) => (t.slug === d.topic.slug && t.strip ? { ...t, strip: t.strip.map(square) } : t);
  type Page = { topic: { strip: Schemas["StripDigest"][] } };
  return Promise.all([
    patchRead<Schemas["TopicGroup"][]>(qc, ["topics"], (gs) => gs.map((g) => ({ ...g, topics: g.topics.map(card) }))),
    ...qc.getQueryCache().findAll({ queryKey: ["topic", d.topic.slug] }).map((q) => patchRead<{ pages: Page[]; pageParams: unknown[] }>(qc, q.queryKey, (r) => ({ ...r, pages: r.pages.map((p) => ({ ...p, topic: { ...p.topic, strip: p.topic.strip.map(square) } })) }))),
  ]);
}

/** Whether a write-again's 409 says the digest is no longer one to write (written meanwhile, or its topic paused),
 * rather than that a job holds the run lock. */
export const digestGone = (e: unknown) => e instanceof ApiError && e.status === 409 && said(e).includes("not one to write again");

/** One paper's BibTeX, as text: the API looks the published version up first; a paper litwatch has not fetched is
 * cited from its page, as a search hit is. */
export async function bibtexFor(paper: Schemas["Paper"]): Promise<string> {
  const year = paper.published ? Number(paper.published.slice(0, 4)) : null;
  const body = paper.stored === false ? { cited: [{ id: paper.id, title: paper.title, authors: paper.authors, year }] } : { ids: [paper.id] };
  return ok(await api.POST("/api/bibtex", { body, parseAs: "text" }));
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
        qc.invalidateQueries({ queryKey: ["summary"] }),
        digestsStale(qc),
        qc.invalidateQueries(settingsReads("topics")),
        qc.invalidateQueries(settingsReads("taxonomy")),
      ]),
    onError: (e) => toast(said(e, "Could not save it"), "error"),
  });
}

const REORDER = ["topics-order"];

/** The order the latest reorder still being saved put the topics in, if any. The Topics page lays its rows in it
 * over whatever its read holds, so no read landing meanwhile (one begun before the move, a fresh visit's during
 * it) puts a moved row back; it lasts until the last save in line has refreshed the reads, which is when the
 * mutation leaves its pending state. */
export function usePendingOrder(): string[] | undefined {
  const orders = useMutationState({ filters: { mutationKey: REORDER, status: "pending" }, select: (m) => m.state.variables as string[] });
  return orders.at(-1);
}

/** The owner's order of the topics, which the sidebar and the Digest follow. The saves go one at a time (`scope`),
 * in the order they were made, whatever page is shown meanwhile, each from the order the page showed when the row
 * was dropped (`usePendingOrder`). Only the last save in line refreshes the reads, or on failure lets the page's
 * read be refetched: an earlier one's refetch would land the server's order of that moment, and a failed one's
 * rollback would undo the moves still in line. */
export function useReorderTopics() {
  const qc = useQueryClient();
  // A save counts as pending until its callbacks are done: more than one pending means another waits its turn.
  const last = () => qc.isMutating({ mutationKey: REORDER }) <= 1;
  return useMutation({
    mutationKey: REORDER,
    scope: { id: "topics-order" },
    mutationFn: async (slugs: string[]) => ok(await api.PUT("/api/settings/topics-order", { body: { slugs } })),
    onError: (e) => toast(said(e, "Could not save the order"), "error"),
    // When the last save settles, whichever way, everything showing the order is reread (the page's own back to the
    // order saved): an earlier save in the queue may have gone through even when the last did not.
    onSettled: () =>
      last() &&
      Promise.all([
        qc.invalidateQueries({ queryKey: ["taxonomy"] }),
        qc.invalidateQueries({ queryKey: ["topics"] }),
        digestsStale(qc),
        qc.invalidateQueries({ queryKey: ["summary"] }),
        qc.invalidateQueries(settingsReads("topics")),
        qc.invalidateQueries(settingsReads("taxonomy")),
      ]),
  });
}

/** The past-session lists, Calibrate's and Overview's: a new session is read on their next visit. */
const SESSION_LISTS = ["calibrate", "overview"] as const;

export function useDrawSession() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async () => ok(await api.POST("/api/settings/calibrate", { body: {} })),
    onSuccess: () => {
      refreshSummary(qc);
      return Promise.all([qc.invalidateQueries({ queryKey: ["settings", "calibrate"] }), ...SESSION_LISTS.map((page) => qc.invalidateQueries({ queryKey: sessionsQuery(page).queryKey }))]);
    },
    onError: (e) => toast(said(e, "Could not draw a session"), "error"),
  });
}

/** After a write on a session: the session is refetched (shown or not) so the strip and the review follow, and
 * its row in the past-session lists is patched from it, in place (`patchRead`: a list read in flight does not
 * land over it), so Back finds the counts as they are now. */
async function sessionChanged(qc: QueryClient, sessionId: number) {
  const key = sessionQuery(sessionId).queryKey;
  await qc.invalidateQueries({ queryKey: key, refetchType: "all" });
  if (qc.getQueryState(key)?.status === "error") return;
  const s = qc.getQueryData<Session>(key);
  if (s) for (const page of SESSION_LISTS) await patchRead<SessionRow[]>(qc, sessionsQuery(page).queryKey, (rows) => rows.map((r) => (r.id === sessionId ? sessionRow(r, s) : r)));
}

/** One paper's answers in a session: a write on the paper like its others, one at a time with them (a Calibrate
 * screen or a label control opened while one is in flight waits for it), named by the session and position it was
 * sent for. The session is refetched so the strip and the review follow, its row in the past-session lists is
 * patched, and the paper is settled as after any write on it. */
export function useAnswer(paperId: string) {
  return usePaperMutation(
    paperId,
    async (_id, v: { sessionId: number; position: number; answer: Schemas["Answer"] }) =>
      ok(await api.PUT("/api/settings/calibrate/{session_id}/items/{position}", { params: { path: { session_id: v.sessionId, position: v.position } }, body: v.answer })),
    (qc, id, _r, v) => Promise.all([sessionChanged(qc, v.sessionId), settled(qc, id)]),
  );
}

/** Add the papers waiting for a word to a session. */
export function useMore(sessionId: number) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async () => ok(await api.POST("/api/settings/calibrate/{session_id}/more", { params: { path: { session_id: sessionId } } })),
    onSuccess: (r) => {
      toast(r.added ? `${plural(r.added, "more paper")} added` : "Nothing is waiting for a word");
      return sessionChanged(qc, sessionId);
    },
    onError: (e) => toast(said(e), "error"),
  });
}

/** Retry only clears the event's wait; the dispatcher sends it on its next pass, so the cards are read again over
 * the next half minute to show how it went. */
export function useRetryIntegration() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (name: string) => ok(await api.POST("/api/settings/integrations/{name}/retry", { params: { path: { name } } })),
    onSuccess: () => {
      toast("Trying the event again");
      const again = () => {
        refreshSummary(qc);
        return qc.invalidateQueries(settingsReads("integrations"));
      };
      for (const ms of [3_000, 10_000, 30_000]) setTimeout(() => void again(), ms);
      return again();
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
    onSuccess: () => {
      refreshSummary(qc);
      return qc.invalidateQueries(settingsReads("integrations"));
    },
    onError: (e) => {
      toast(said(e), "error");
      if (e instanceof ApiError && e.status === 409) void qc.invalidateQueries(settingsReads("integrations"));
    },
  });
}
