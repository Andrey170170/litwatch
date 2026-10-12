import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "@tanstack/react-router";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowDown, ArrowUp } from "lucide-react";
import type { TopicKind } from "@/api/client";
import {
  evaluationQuery,
  evaluationsQuery,
  keywordsQuery,
  rerunsQuery,
  settingsTopicsQuery,
  usePreview,
  useOrderTopics,
  useRerun,
  useSaveTopic,
  type Evaluation,
  type RerunEstimate,
  type RerunScope,
  type Topic,
  type TopicMetrics,
} from "@/api/settings";
import { BTN, BTN_PRIMARY, Empty, FIELD, H2, Page, TABLE, money, pct } from "@/routes/settings/index";
import { KIND_LABEL } from "@/lib/topics";
import { cn } from "@/lib/utils";

/** The eight colours of DESIGN.md, in the order they are first assigned. */
const PALETTE = ["#2f5d50", "#8a3b2e", "#3b4f8a", "#a2661e", "#5a6b3a", "#7a4f8a", "#2f6b7a", "#8a6a3b"];

/** The list: order, kind, status, revision; a link per topic to its editor. */
export function TopicsSettings() {
  const topics = useQuery(settingsTopicsQuery);
  const order = useOrderTopics();
  const list = useMemo(() => [...(topics.data ?? [])].sort((a, b) => (a.position ?? 99) - (b.position ?? 99)), [topics.data]);

  function move(i: number, by: number) {
    const slugs = list.map((t) => t.slug);
    const j = i + by;
    if (j < 0 || j >= slugs.length) return;
    [slugs[i], slugs[j]] = [slugs[j]!, slugs[i]!];
    order.mutate(slugs);
  }

  return (
    <Page
      title="Topics"
      sub="What litwatch follows for you. The order here is the order of the Digest's sections and the Topics tab."
      actions={
        <Link to="/settings/topics/$slug" params={{ slug: "new" }} className={BTN_PRIMARY}>
          New topic
        </Link>
      }
    >
      {list.length === 0 && !topics.isPending && <Empty>No topics yet.</Empty>}
      {list.length > 0 && (
        <table className={TABLE}>
          <thead>
            <tr>
              <th className="w-16" />
              <th>Topic</th>
              <th>Kind</th>
              <th>Status</th>
              <th className="text-right">Revision</th>
            </tr>
          </thead>
          <tbody>
            {list.map((t, i) => (
              <tr key={t.slug} className={cn(t.status !== "active" && "text-muted-foreground")}>
                <td className="whitespace-nowrap">
                  <button type="button" className="hit text-muted-foreground disabled:opacity-30" aria-label="Move up" disabled={i === 0 || order.isPending} onClick={() => move(i, -1)}>
                    <ArrowUp className="size-4" />
                  </button>
                  <button type="button" className="hit text-muted-foreground disabled:opacity-30" aria-label="Move down" disabled={i === list.length - 1 || order.isPending} onClick={() => move(i, 1)}>
                    <ArrowDown className="size-4" />
                  </button>
                </td>
                <td>
                  <Link to="/settings/topics/$slug" params={{ slug: t.slug }} className="inline-flex items-center gap-2 font-medium hover:underline">
                    <span className="inline-block size-3 rounded-full" style={{ background: t.status === "active" ? (t.colour ?? "var(--foreground)") : "#9a9a94" }} aria-hidden />
                    {t.name}
                  </Link>
                  <div className="text-[12px] text-muted-foreground">{t.slug}</div>
                </td>
                <td>{KIND_LABEL[t.kind]}</td>
                <td>{t.status}</td>
                <td className="tabular text-right">{t.revision ?? "–"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Page>
  );
}

const BLANK: Topic = { slug: "", name: "", kind: "watch", subject: "", rubric: null, interests: null, keywords: [], status: "active", colour: null };

function slugify(name: string) {
  return name.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 60);
}

/** The editor: name, kind, subject, rubric or interests, keywords with counts, status, colour; preview, save, re-runs. */
export function TopicEditorPage() {
  const { slug } = useParams({ from: "/settings/topics/$slug" });
  const topics = useQuery(settingsTopicsQuery);
  const isNew = slug === "new";
  const stored = topics.data?.find((t) => t.slug === slug);
  if (!isNew && topics.isPending) return <Page title="…">{null}</Page>;
  if (!isNew && !stored) return <Page title="No such topic">{null}</Page>;
  const taken = new Set((topics.data ?? []).map((t) => t.colour).filter(Boolean));
  const base = stored ?? { ...BLANK, colour: PALETTE.find((c) => !taken.has(c)) ?? PALETTE[0]! };
  return <TopicEditor key={slug} stored={stored} base={base} isNew={isNew} />;
}

function TopicEditor({ stored, base, isNew }: { stored: Topic | undefined; base: Topic; isNew: boolean }) {
  const navigate = useNavigate();
  const [draft, setDraft] = useState<Topic>({ ...base, keywords: base.keywords ?? [] });
  const [slugTouched, setSlugTouched] = useState(!isNew);
  const [savedWatch, setSavedWatch] = useState<Topic | null>(null);
  const save = useSaveTopic();
  const counts = useQuery({ ...keywordsQuery(stored?.slug ?? ""), enabled: !!stored });
  const set = <K extends keyof Topic>(k: K, v: Topic[K]) => setDraft((d) => ({ ...d, [k]: v }));
  const dirty = JSON.stringify(draft) !== JSON.stringify({ ...base, keywords: base.keywords ?? [] });
  const valid = draft.slug.trim() !== "" && draft.name.trim() !== "" && draft.subject.trim() !== "";

  function submit() {
    const body: Topic = {
      ...draft,
      slug: draft.slug.trim(),
      keywords: (draft.keywords ?? []).map((k) => k.trim()).filter(Boolean),
      rubric: draft.kind === "watch" ? draft.rubric || null : null,
      interests: draft.kind === "radar" ? draft.interests || null : null,
    };
    save.mutate(body, {
      onSuccess: (saved) => {
        if (saved.kind === "watch") setSavedWatch(saved);
        if (isNew) void navigate({ to: "/settings/topics/$slug", params: { slug: saved.slug } });
      },
    });
  }

  return (
    <Page
      title={isNew ? "New topic" : stored?.name ?? draft.name}
      sub={isNew ? "A topic is what litwatch follows for you. Edits apply forward." : `${KIND_LABEL[base.kind]} · revision ${base.revision ?? "–"}. Edits apply forward; past papers keep the answer they got.`}
      back={{ to: "/settings/topics", label: "Topics" }}
    >
      <div className="grid max-w-[720px] gap-4">
        <div className="grid gap-4 desk:grid-cols-[1fr_1fr]">
          <Field label="Name">
            <input
              className={FIELD}
              value={draft.name}
              onChange={(e) => {
                set("name", e.target.value);
                if (!slugTouched) set("slug", slugify(e.target.value));
              }}
            />
          </Field>
          <Field label="Slug" hint={isNew ? "In addresses; letters, digits and dashes." : "Fixed once created."}>
            <input className={cn(FIELD, "font-mono text-[13px]")} value={draft.slug} disabled={!isNew} onChange={(e) => { setSlugTouched(true); set("slug", e.target.value); }} />
          </Field>
        </div>
        <div className="grid gap-4 desk:grid-cols-3">
          <Field label="Kind" hint={KIND_HINT[draft.kind]}>
            <select className={FIELD} value={draft.kind} onChange={(e) => set("kind", e.target.value as TopicKind)}>
              <option value="watch">Watch</option>
              <option value="radar">Radar</option>
              <option value="pulse">Pulse</option>
            </select>
          </Field>
          <Field label="Status">
            <select className={FIELD} value={draft.status ?? "active"} onChange={(e) => set("status", e.target.value as Topic["status"])}>
              <option value="active">Active</option>
              <option value="paused">Paused</option>
            </select>
          </Field>
          <Field label="Colour">
            <div className="flex flex-wrap items-center gap-1.5">
              {PALETTE.map((c) => (
                <button
                  key={c}
                  type="button"
                  aria-label={c}
                  aria-pressed={draft.colour === c}
                  onClick={() => set("colour", c)}
                  className={cn("size-7 rounded-full border-2 border-transparent", draft.colour === c && "border-foreground")}
                  style={{ background: c }}
                />
              ))}
              <input type="color" value={draft.colour ?? "#2f5d50"} onChange={(e) => set("colour", e.target.value)} aria-label="Any colour" className="h-7 w-9 cursor-pointer rounded border border-border bg-transparent" />
            </div>
          </Field>
        </div>
        <Field label="Subject" hint="What the topic is about, in your words. Triage reads this for every topic.">
          <textarea className={cn(FIELD, "min-h-[120px]")} value={draft.subject} onChange={(e) => set("subject", e.target.value)} />
        </Field>
        {draft.kind === "watch" && (
          <Field label="Rubric" hint="What makes a paper must read, relevant or only adjacent. The graders read this.">
            <textarea className={cn(FIELD, "min-h-[120px]")} value={draft.rubric ?? ""} onChange={(e) => set("rubric", e.target.value)} />
          </Field>
        )}
        {draft.kind === "radar" && (
          <Field label="Interests" hint="What you would care about in this field. The reviewer reads this.">
            <textarea className={cn(FIELD, "min-h-[100px]")} value={draft.interests ?? ""} onChange={(e) => set("interests", e.target.value)} />
          </Field>
        )}
        <Field label="Keywords" hint="Patterns matched against titles and abstracts; a match makes a paper a candidate without a model. Counts are over the stored collection for the saved patterns.">
          <Keywords
            value={draft.keywords ?? []}
            counts={counts.data ?? {}}
            saved={new Set(stored?.keywords ?? [])}
            onChange={(k) => set("keywords", k)}
          />
        </Field>

        <div className="flex flex-wrap items-center gap-2 border-t border-border pt-4">
          <button type="button" className={BTN_PRIMARY} disabled={!valid || !dirty || save.isPending} onClick={submit}>
            {save.isPending ? "Saving…" : isNew ? "Create" : "Save"}
          </button>
          {!isNew && dirty && <span className="text-[12.5px] text-muted-foreground">Unsaved changes.</span>}
        </div>

        {draft.kind === "watch" && !isNew && <Preview draft={{ ...draft, slug: draft.slug.trim() }} />}
        {savedWatch && <Reruns topic={savedWatch} />}
      </div>
    </Page>
  );
}

const KIND_HINT: Record<TopicKind, string> = {
  watch: "Graded per paper, nightly.",
  radar: "Reviewed weekly; daily too if asked.",
  pulse: "A paragraph a week.",
};

function Field({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <span className="mb-1 block text-[12.5px] font-medium">{label}</span>
      {children}
      {hint && <span className="mt-1 block text-[12px] text-muted-foreground">{hint}</span>}
    </label>
  );
}

function Keywords({ value, counts, saved, onChange }: { value: string[]; counts: Record<string, number>; saved: Set<string>; onChange: (k: string[]) => void }) {
  const [adding, setAdding] = useState("");
  return (
    <div className="rounded-lg border border-border">
      {value.map((k, i) => {
        const n = counts[k];
        return (
          <div key={i} className="flex items-center gap-2 border-b border-border px-2 py-1">
            <input
              className="min-w-0 flex-1 bg-transparent px-1 py-1 font-mono text-[13px] outline-none"
              value={k}
              onChange={(e) => onChange(value.map((x, j) => (j === i ? e.target.value : x)))}
              aria-label={`Keyword ${i + 1}`}
            />
            <span className="tabular w-24 text-right text-[12px] text-muted-foreground">
              {!saved.has(k) ? "unsaved" : n === undefined ? "…" : n < 0 ? "bad pattern" : `${n} ${n === 1 ? "paper" : "papers"}`}
            </span>
            <button type="button" className="hit text-muted-foreground hover:text-foreground" aria-label="Remove keyword" onClick={() => onChange(value.filter((_, j) => j !== i))}>
              ×
            </button>
          </div>
        );
      })}
      <form
        className="flex items-center gap-2 px-2 py-1"
        onSubmit={(e) => {
          e.preventDefault();
          if (adding.trim()) {
            onChange([...value, adding.trim()]);
            setAdding("");
          }
        }}
      >
        <input className="min-w-0 flex-1 bg-transparent px-1 py-1 font-mono text-[13px] outline-none placeholder:font-sans placeholder:text-muted-foreground" value={adding} onChange={(e) => setAdding(e.target.value)} placeholder="Add a pattern and press Enter" aria-label="New keyword" />
        <button type="submit" className={BTN} disabled={!adding.trim()}>
          Add
        </button>
      </form>
    </div>
  );
}

/** Preview a draft against the labelled papers and misses: a dry run, polled until its metrics arrive. */
function Preview({ draft }: { draft: Topic }) {
  const preview = usePreview();
  const qc = useQueryClient();
  const [started, setStarted] = useState<{ at: string; estimate: RerunEstimate } | null>(null);
  const [evalId, setEvalId] = useState<number | null>(null);
  const list = useQuery({ ...evaluationsQuery("preview"), enabled: !!started && evalId === null, refetchInterval: 3000 });
  const result = useQuery({ ...evaluationQuery(evalId ?? 0), enabled: evalId !== null, refetchInterval: (q) => (q.state.data?.finished ? false : 3000) });

  useEffect(() => {
    if (!started || evalId !== null || !list.data) return;
    const mine = list.data.find((e) => e.params?.topic === draft.slug && e.started_at >= started.at);
    if (mine) setEvalId(mine.id);
  }, [list.data, started, evalId, draft.slug]);

  function run() {
    setEvalId(null);
    preview.mutate(draft, {
      onSuccess: (estimate) => {
        setStarted({ at: new Date(Date.now() - 5000).toISOString(), estimate });
        void qc.invalidateQueries({ queryKey: ["settings", "evaluations", "preview"] });
      },
    });
  }

  const metrics = result.data?.finished ? result.data.topics?.[draft.slug] : undefined;
  return (
    <div className="border-t border-border pt-4">
      <H2 note="a dry run of this draft over the papers you have labelled and the misses you reported; nothing is stored">Preview</H2>
      <div className="flex flex-wrap items-center gap-2">
        <button type="button" className={BTN} disabled={preview.isPending} onClick={run}>
          {preview.isPending ? "Starting…" : "Preview this draft"}
        </button>
        {started && (
          <span className="text-[12.5px] text-muted-foreground">
            {started.estimate.papers} {started.estimate.papers === 1 ? "paper" : "papers"}, about {money(started.estimate.cost)}
            {!metrics && " · running…"}
          </span>
        )}
      </div>
      {metrics && <MetricsView m={metrics} evaluation={result.data!} />}
    </div>
  );
}

/** Recall and precision for one topic, in plain words. */
export function MetricsView({ m, evaluation }: { m: TopicMetrics; evaluation: Evaluation }) {
  const firm = m.recall_firm;
  const prov = m.recall_provisional;
  const precision = Object.entries(m.precision_by_origin ?? {}).filter(([, v]) => v.surfaced > 0);
  return (
    <div className="mt-3 grid gap-3 text-[13.5px] desk:grid-cols-2">
      <div className="rounded-lg border border-border px-3.5 py-3">
        <div className="text-[12px] text-muted-foreground">Recall, on papers you called relevant</div>
        {firm && firm.n > 0 ? (
          <>
            <div className="text-[22px] font-semibold tabular">{pct(firm.end_to_end)}</div>
            <div className="text-muted-foreground">
              {Math.round((firm.end_to_end ?? 0) * firm.n)} of {firm.n} found end to end; triage {pct(firm.triage)}, candidates {pct(firm.candidate)}, grading {pct(firm.grading)}.
            </div>
            {firm.grading_misses.length + firm.triage_misses.length + firm.candidate_misses.length > 0 && (
              <div className="mt-1 text-[12.5px] text-muted-foreground">Missed: {Array.from(new Set([...firm.triage_misses, ...firm.candidate_misses, ...firm.grading_misses])).join(", ")}</div>
            )}
          </>
        ) : (
          <div className="text-muted-foreground">No firm labels to measure against.</div>
        )}
        {prov && prov.n > 0 && (
          <div className="mt-1 text-[12.5px] text-muted-foreground">
            With provisional labels too: {pct(prov.end_to_end)} of {prov.n}.
          </div>
        )}
      </div>
      <div className="rounded-lg border border-border px-3.5 py-3">
        <div className="text-[12px] text-muted-foreground">Precision, of what it would have shown</div>
        {precision.length ? (
          precision.map(([origin, v]) => (
            <div key={origin} className="flex items-baseline justify-between gap-3">
              <span>{ORIGIN_WORD[origin] ?? origin}</span>
              <span className="tabular">
                <b>{pct(v.precision)}</b> <span className="text-muted-foreground">of {v.surfaced}</span>
              </span>
            </div>
          ))
        ) : (
          <div className="text-muted-foreground">Nothing labelled was shown.</div>
        )}
        <div className="mt-1 text-[12.5px] text-muted-foreground">
          {evaluation.papers ?? "?"} papers, {money(evaluation.cost)}.
        </div>
      </div>
    </div>
  );
}

const ORIGIN_WORD: Record<string, string> = {
  gold: "hand-labelled before v1",
  legacy_positive: "0.x positives",
  reject_sample: "sampled rejects",
  sample: "calibration draws",
  candidate_sample: "sampled candidates",
  reader: "your labels",
  miss: "reported misses",
};

const SCOPE_WORD: Record<RerunScope, { name: string; blurb: string }> = {
  evidence: { name: "Your labels and misses", blurb: "Shows whether the edit helped; stores nothing new." },
  candidates: { name: "Existing candidates", blurb: "Re-graded under the new rubric." },
  keywords: { name: "Keyword matches", blurb: "Papers anywhere in the collection matching the patterns, then triaged." },
  range: { name: "A date range", blurb: "Everything published in the range, triaged and graded." },
};

/** After a Watch topic is saved: the scoped re-runs, each with its paper count and estimated cost. */
function Reruns({ topic }: { topic: Topic }) {
  const [since, setSince] = useState("");
  const [until, setUntil] = useState("");
  const ranged = since && until;
  const estimates = useQuery(rerunsQuery(topic.slug, ranged ? since : undefined, ranged ? until : undefined));
  const rerun = useRerun(topic.slug);
  return (
    <div className="border-t border-border pt-4">
      <H2 note="nothing re-runs by itself; choose a scope">Re-run for this topic</H2>
      <div className="mb-3 flex flex-wrap items-center gap-2 text-[12.5px] text-muted-foreground">
        For a date range:
        <input type="date" value={since} onChange={(e) => setSince(e.target.value)} className={cn(FIELD, "w-auto py-1")} aria-label="Since" />
        to
        <input type="date" value={until} onChange={(e) => setUntil(e.target.value)} className={cn(FIELD, "w-auto py-1")} aria-label="Until" />
      </div>
      {estimates.isPending && <p className="text-muted-foreground">Counting…</p>}
      {estimates.data && (
        <table className={TABLE}>
          <thead>
            <tr>
              <th>Scope</th>
              <th className="text-right">Papers</th>
              <th className="text-right">Cost</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {estimates.data.map((e) => (
              <tr key={e.scope}>
                <td>
                  <div className="font-medium">{SCOPE_WORD[e.scope].name}</div>
                  <div className="text-[12px] text-muted-foreground">{SCOPE_WORD[e.scope].blurb}</div>
                </td>
                <td className="tabular text-right">{e.papers}</td>
                <td className="tabular text-right">
                  {money(e.cost)}
                  {!e.known && <span className="text-muted-foreground" title="No cost history for a role yet"> ?</span>}
                </td>
                <td className="text-right">
                  <button
                    type="button"
                    className={BTN}
                    disabled={rerun.isPending || e.papers === 0}
                    onClick={() => rerun.mutate({ scope: e.scope, since: e.scope === "range" ? since : null, until: e.scope === "range" ? until : null })}
                  >
                    Run
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
