/**
 * The kitchen's endpoints under `/api/settings`. Most return untyped objects in the schema, so
 * their shapes are written down here from `litwatch/api/settings.py` and the stores it reads.
 */
import { queryOptions, useMutation, useQueryClient } from "@tanstack/react-query";
import { api, ok, type Grade, type LabelValue, type Schemas } from "./client";
import { toast } from "@/lib/toast";
import { invalidateAfterWrite } from "./invalidate";

export type Topic = Schemas["Topic"];
export type TopicStatus = Schemas["litwatch__core__types__Status"];
export type Opinion = { grade: Grade; reason: string; model?: string | null };

export type Split = {
  paper_id: string;
  topic: string;
  grade: Grade;
  split: boolean;
  judge: Opinion | null;
  check: Opinion | null;
  recorded_at: string;
  title: string;
  label: { value: LabelValue; grade?: Grade | null } | null;
};
export type Ungraded = { paper_id: string; topic: string; failed_runs: number; last_error: string | null; updated_at: string; title: string };
export type Awaiting = { paper_id: string; failed_runs: number; last_error: string | null; title: string };
export type FailedReview = { topic: string; topic_name: string; kind: string; period: "day" | "week"; day: string; error: string | null };
export type Recall = {
  n: number;
  triage: number | null;
  candidate: number | null;
  grading: number | null;
  end_to_end: number | null;
  sent_to_grading: number;
  grading_failed: number;
  triage_misses: string[];
  candidate_misses: string[];
  grading_misses: string[];
};
export type TopicMetrics = {
  recall_firm?: Recall;
  recall_provisional?: Recall;
  precision_by_origin?: Record<string, { surfaced: number; precision: number | null }>;
  [k: string]: unknown;
};
export type Evaluation = {
  eval_run: number;
  kind: string;
  note: string | null;
  models?: Record<string, string>;
  cost?: number | null;
  papers?: number;
  topics: Record<string, TopicMetrics>;
  finished?: boolean;
  /** Set when the run failed; `finished` is then true and there are no metrics. */
  error?: string | null;
};
export type Quality = {
  night: { run: number | null; error: string | null; splits: Split[]; ungraded: Ungraded[]; awaiting_triage: Awaiting[]; reviews_failed: FailedReview[] };
  evaluation: Evaluation | null;
  calibration: { topic: string; precision?: number | null; precision_n?: number; dropped?: number | null; dropped_n?: number }[];
  matters_no_topic: { paper_id: string; note: string | null; at: string; title: string }[];
  correction_rate: { topic: string; shown: number; corrected: number }[];
  collected: { paper_id: string; topic: string | null; value: "up" | "down"; reason: string | null; reacted_at: string; title: string }[];
};

export type SessionSummary = { id: number; seed: number; drawn_at: string; papers: number; answered: number };
export type Take = { id: number; topic: string; text: string; accurate: boolean | null };
export type SessionItem = {
  position: number;
  paper_id: string;
  topic: string;
  why: "above" | "below" | "split" | "reaction";
  answered_at: string | null;
  title: string;
  abstract: string;
  authors: string[];
  published: string | null;
  takes: Take[];
  fit: Record<string, { value: LabelValue; grade: Grade | null }>;
  appraisal: { value: "matters" | "fine" | "not_for_me"; note: string | null } | null;
};
export type Session = { id: number; seed: number; drawn_at: string; items: SessionItem[] };
export type Answer = Schemas["Answer"];

export type Miss = {
  id: number;
  paper_id: string;
  topic: string;
  reported_at: string;
  eval_run_id: number | null;
  trace: "pending" | "done" | "failed" | null;
  trace_error: string | null;
  title: string;
  triage_yes: boolean | null;
  triage_why: string | null;
  keyword: boolean | null;
  judge: Opinion | null;
  check: Opinion | null;
  grade: Grade | null;
  error: string | null;
  grade_error: string | null;
};
export type Run = {
  id: number;
  kind: string;
  started_at: string;
  finished_at: string | null;
  cost: string | number | null;
  error: string | null;
  counts: Record<string, unknown> | null;
  fetched: number | null;
  inserted: number | null;
  updated: number | null;
};
export type Schedule = Schemas["ScheduleOut"];
export type RerunScope = Schemas["RerunIn"]["scope"];
export type RerunEstimate = { scope: RerunScope; papers: number; cost: number | null; known: boolean };
export type EvalSummary = { id: number; kind: string; note: string | null; params: Record<string, unknown>; started_at: string; finished_at: string | null; cost: string | null };

const S = "/api/settings" as const;

/** The schema types these endpoints as plain objects; the shapes above are what they return. */
const typed = <T,>(v: unknown) => v as T;

export const settingsTopicsQuery = queryOptions({
  queryKey: ["settings", "topics"],
  queryFn: async () => ok(await api.GET(`${S}/topics`)),
});
/** Match counts for the editor's draft patterns (-1 for a pattern that is not a valid regex). */
export const keywordsQuery = (slug: string, patterns: string[]) =>
  queryOptions({
    queryKey: ["settings", "keywords", slug, patterns],
    queryFn: async () =>
      ok(await api.GET(`${S}/topics/{slug}/keywords`, { params: { path: { slug }, query: { pattern: patterns } } })),
    placeholderData: (previous) => previous,
    staleTime: 60_000,
  });
export const rerunsQuery = (slug: string, since?: string, until?: string) =>
  queryOptions({
    queryKey: ["settings", "reruns", slug, since ?? null, until ?? null],
    queryFn: async () =>
      typed<RerunEstimate[]>(ok(await api.GET(`${S}/topics/{slug}/reruns`, { params: { path: { slug }, query: { since, until } } }))),
  });
export const evaluationsQuery = (kind: string) =>
  queryOptions({
    queryKey: ["settings", "evaluations", kind],
    queryFn: async () => typed<EvalSummary[]>(ok(await api.GET(`${S}/evaluations`, { params: { query: { kind } } }))),
  });
export const evaluationQuery = (id: number) =>
  queryOptions({
    queryKey: ["settings", "evaluation", id],
    queryFn: async () =>
      typed<Evaluation>(ok(await api.GET(`${S}/evaluations/{eval_run_id}`, { params: { path: { eval_run_id: id } } }))),
  });
export const qualityQuery = queryOptions({
  queryKey: ["settings", "quality"],
  queryFn: async () => typed<Quality>(ok(await api.GET(`${S}/quality`))),
});
export const sessionsQuery = queryOptions({
  queryKey: ["settings", "calibrate"],
  queryFn: async () => typed<SessionSummary[]>(ok(await api.GET(`${S}/calibrate`))),
});
export const sessionQuery = (id: number) =>
  queryOptions({
    queryKey: ["settings", "calibrate", id],
    queryFn: async () => typed<Session>(ok(await api.GET(`${S}/calibrate/{session_id}`, { params: { path: { session_id: id } } }))),
  });
export const missesQuery = queryOptions({
  queryKey: ["settings", "misses"],
  queryFn: async () => typed<Miss[]>(ok(await api.GET(`${S}/misses`))),
  // A trace runs in the background for a minute; keep looking while one is pending.
  refetchInterval: (q) => (q.state.data?.some((m) => m.trace !== "done" && m.trace !== "failed") ? 3000 : false),
});
export const runsQuery = queryOptions({
  queryKey: ["settings", "runs"],
  queryFn: async () => typed<Run[]>(ok(await api.GET(`${S}/runs`))),
});
/** One card per integration (DESIGN.md "Integration card"); polled while one is failing. */
export const integrationsQuery = queryOptions({
  queryKey: ["settings", "integrations"],
  queryFn: async () => ok(await api.GET(`${S}/integrations`)),
  refetchInterval: (q) => (q.state.data?.some((c) => c.failing) ? 5000 : 30_000),
});

export const configQuery = queryOptions({
  queryKey: ["settings", "config"],
  queryFn: async () => ok(await api.GET(`${S}/config`)),
});
/** Whether the schedule is on, its zone, and when each job next fires (computed by the server). */
export const scheduleQuery = queryOptions({
  queryKey: ["settings", "schedule"],
  queryFn: async () => ok(await api.GET(`${S}/schedule`)),
  staleTime: 60_000,
});

function useRefresh() {
  const qc = useQueryClient();
  return () => invalidateAfterWrite(qc);
}

const onError = (e: Error) => toast(e.message || "That did not work", "error");

export function useSaveTopic() {
  const refresh = useRefresh();
  return useMutation({
    mutationFn: async (t: Topic) => ok(await api.PUT(`${S}/topics/{slug}`, { params: { path: { slug: t.slug } }, body: t })),
    onSuccess: (saved) => {
      toast(`Saved ${saved.name}, revision ${saved.revision ?? "?"}`);
      refresh();
    },
    onError,
  });
}

export function useOrderTopics() {
  const refresh = useRefresh();
  return useMutation({
    mutationFn: async (slugs: string[]) => ok(await api.PUT(`${S}/topics-order`, { body: { slugs } })),
    onSuccess: refresh,
    onError,
  });
}

export function usePreview() {
  return useMutation({
    mutationFn: async (t: Topic) =>
      typed<RerunEstimate>(ok(await api.POST(`${S}/topics/{slug}/preview`, { params: { path: { slug: t.slug } }, body: t }))),
    onError,
  });
}

export function useRerun(slug: string) {
  const refresh = useRefresh();
  return useMutation({
    mutationFn: async (v: Schemas["RerunIn"]) =>
      typed<RerunEstimate>(ok(await api.POST(`${S}/topics/{slug}/reruns`, { params: { path: { slug } }, body: v }))),
    onSuccess: (e) => {
      toast(`Re-run started over ${e.papers} ${e.papers === 1 ? "paper" : "papers"}`);
      refresh();
    },
    onError: (e: Error & { status?: number }) =>
      toast(e.status === 409 ? "Another litwatch job is running; try again when it is done" : e.message, "error"),
  });
}

export function useGradeNow() {
  const refresh = useRefresh();
  return useMutation({
    mutationFn: async (v: Schemas["Pair"]) =>
      typed<{ graded: boolean; error: string | null }>(ok(await api.POST(`${S}/quality/grade-now`, { body: v }))),
    onSuccess: (r) => {
      toast(r.graded ? "Graded" : `Grading failed: ${r.error ?? "unknown error"}`, r.graded ? "plain" : "error");
      refresh();
    },
    onError: (e: Error & { status?: number }) =>
      toast(e.status === 409 ? "Another litwatch job is running; try again when it is done" : e.message, "error"),
  });
}

export function useDraw() {
  const refresh = useRefresh();
  return useMutation({
    mutationFn: async () => typed<{ id: number }>(ok(await api.POST(`${S}/calibrate`, { body: {} }))),
    onSuccess: refresh,
    onError,
  });
}

export function useMore(id: number) {
  const refresh = useRefresh();
  return useMutation({
    mutationFn: async () =>
      typed<{ added: number }>(ok(await api.POST(`${S}/calibrate/{session_id}/more`, { params: { path: { session_id: id } } }))),
    onSuccess: (r) => {
      toast(r.added ? `${r.added} more ${r.added === 1 ? "paper" : "papers"} waiting for a word` : "Nothing is waiting for a word");
      refresh();
    },
    onError,
  });
}

export function useAnswer(id: number) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (v: { position: number; answer: Answer }) =>
      ok(
        await api.PUT(`${S}/calibrate/{session_id}/items/{position}`, {
          params: { path: { session_id: id, position: v.position } },
          body: v.answer,
        }),
      ),
    // An answer is a label and an appraisal: it can move a grade and the library.
    onSuccess: () => invalidateAfterWrite(qc),
    onError,
  });
}

export function useRetrace() {
  const refresh = useRefresh();
  return useMutation({
    mutationFn: async (id: number) => ok(await api.POST(`${S}/misses/{miss_id}/trace`, { params: { path: { miss_id: id } } })),
    onSuccess: () => {
      toast("Tracing again; this takes a minute");
      refresh();
    },
    onError,
  });
}

// -- papers triage kept failing on (spec decision 24) ------------------------------------------

export function useDismiss() {
  const refresh = useRefresh();
  return useMutation({
    mutationFn: async (paperId: string) =>
      ok(await api.POST(`${S}/triage/{paper_id}/dismiss`, { params: { path: { paper_id: paperId } } })),
    onSuccess: () => {
      toast("Dismissed; it stays in the collection, never triaged");
      refresh();
    },
    onError,
  });
}

/** The owner's own triage answers: for each active topic, could it belong (Watch) or is it in the field. */
export function useTriageByHand(names: Record<string, string>) {
  const refresh = useRefresh();
  return useMutation({
    mutationFn: async (v: { paperId: string; answers: Record<string, boolean> }) =>
      typed<{ candidates: string[] }>(
        ok(await api.PUT(`${S}/triage/{paper_id}`, { params: { path: { paper_id: v.paperId } }, body: { answers: v.answers } })),
      ),
    onSuccess: (r) => {
      const named = r.candidates.map((slug) => names[slug] ?? slug);
      toast(named.length ? `Sent on: a candidate for ${named.join(" and ")}, graded on the next run` : "Sent on with no Watch candidate");
      refresh();
    },
    onError,
  });
}

export function useCorrect() {
  const refresh = useRefresh();
  return useMutation({
    mutationFn: async (v: { paperId: string; title: string | null; abstract: string | null }) =>
      ok(await api.POST(`${S}/triage/{paper_id}/correction`, { params: { path: { paper_id: v.paperId } }, body: { title: v.title, abstract: v.abstract } })),
    onSuccess: () => {
      toast("Text fixed; it is triaged again on the next run");
      refresh();
    },
    onError,
  });
}

// -- integrations ------------------------------------------------------------------------------

export function useRetryIntegration() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (name: string) => ok(await api.POST(`${S}/integrations/{name}/retry`, { params: { path: { name } } })),
    onSuccess: () => {
      toast("Trying the event again now");
      void qc.invalidateQueries({ queryKey: ["settings", "integrations"] });
    },
    onError: (e: Error & { status?: number }) => toast(e.status === 409 ? "Nothing is failing any more" : e.message, "error"),
  });
}

export function useSkipIntegration() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (name: string) => ok(await api.POST(`${S}/integrations/{name}/skip`, { params: { path: { name } } })),
    onSuccess: (skipped) => {
      toast(`Skipped ${skipped.type ?? "the event"}${skipped.paper_id ? ` on ${skipped.paper_id}` : ""}`);
      void qc.invalidateQueries({ queryKey: ["settings", "integrations"] });
    },
    onError: (e: Error & { status?: number }) => toast(e.status === 409 ? "Nothing is failing any more" : e.message, "error"),
  });
}
