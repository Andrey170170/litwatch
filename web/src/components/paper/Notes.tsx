/**
 * The open states under the header (DESIGN.md, "Open states"): yellow for a split or escalation, red for ungraded,
 * a failed analysis, a full-text pass waiting on the owner or awaiting classification, each with its buttons.
 * Settling one settles it on Needs you too.
 */
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import type { Schemas } from "@/api/client";
import { useClassifyByHand, useDismiss, useFixText, useGradeNow, useRetryAnalysis, useRunPassNow } from "@/api/mutations";
import { taxonomyQuery } from "@/api/queries";
import type { Paper } from "./Actions";

type OpenState = Schemas["OpenState"];

function ClassifyByHand({ paper, state, onDone }: { paper: Paper; state: OpenState; onDone: () => void }) {
  const { data: tree } = useQuery(taxonomyQuery);
  const classify = useClassifyByHand(paper.id);
  const [chosen, setChosen] = useState<string[]>([]);
  const classes = tree?.classes ?? [];
  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap gap-1.5 max-h-48 overflow-y-auto">
        {classes.map((c) => (
          <button key={c.slug} className={`fchip${chosen.includes(c.slug) ? " on" : ""}`} aria-pressed={chosen.includes(c.slug)} onClick={() => setChosen((x) => (x.includes(c.slug) ? x.filter((s) => s !== c.slug) : [...x, c.slug]))}>
            {c.name}
          </button>
        ))}
      </div>
      <div className="acts">
        <button className="btn pri" disabled={!chosen.length || classify.isPending} onClick={() => classify.mutate({ revision: state.revision, classes: chosen }, { onSuccess: onDone })}>
          Classify{chosen.length ? ` · ${chosen[0]} primary` : ""}
        </button>
        <button className="btn" onClick={onDone}>
          Cancel
        </button>
      </div>
    </div>
  );
}

function FixText({ paper, state, onDone }: { paper: Paper; state: OpenState; onDone: () => void }) {
  const fix = useFixText(paper.id);
  const [title, setTitle] = useState(paper.title);
  const [abstract, setAbstract] = useState(paper.abstract);
  return (
    <div className="flex flex-col gap-2">
      <input className="field" value={title} onChange={(e) => setTitle(e.target.value)} aria-label="Title" />
      <textarea className="field" rows={6} value={abstract} onChange={(e) => setAbstract(e.target.value)} aria-label="Abstract" />
      <div className="acts">
        <button className="btn pri" disabled={fix.isPending} onClick={() => fix.mutate({ revision: state.revision, title, abstract }, { onSuccess: onDone })}>
          Save the text
        </button>
        <button className="btn" onClick={onDone}>
          Cancel
        </button>
      </div>
    </div>
  );
}

function Note({ paper, state, onLabel }: { paper: Paper; state: OpenState; onLabel: (topic: string) => void }) {
  const [form, setForm] = useState<"classes" | "text" | null>(null);
  const dismiss = useDismiss(paper.id);
  const retry = useRetryAnalysis(paper.id);
  const gradeNow = useGradeNow(paper.id);
  const runPass = useRunPassNow(paper.id);
  const topic = paper.topics.find((t) => t.topic.slug === state.topic);
  const name = topic?.topic.name ?? state.topic ?? "";
  switch (state.kind) {
    case "split":
    case "escalated": {
      const said = topic?.verdict ? Object.entries(topic.verdict.opinions ?? {}).filter(([, o]) => o.fit).map(([role, o]) => `${role} ${o.fit}`) : [];
      const opinions = said.join(", ").replace(/^\w/, (c) => c.toUpperCase());
      return (
        <div className="note">
          <div>
            <b>
              {state.kind === "split" ? "Split" : "Escalated"} on {name}.
            </b>{" "}
            {opinions}; {topic?.fit} stands until it is labelled.
          </div>
          <div className="acts">
            <button className="btn" onClick={() => onLabel(state.topic!)}>
              Label
            </button>
          </div>
        </div>
      );
    }
    case "ungraded":
      return (
        <div className="note fail">
          <div>
            <b>Ungraded on {name}:</b> grading failed three runs in a row.
            {state.error && <div className="err">{state.error}</div>}
          </div>
          <div className="acts">
            <button className="btn" disabled={gradeNow.isPending} onClick={() => gradeNow.mutate({ topic: state.topic!, revision: state.revision })}>
              {gradeNow.isPending ? "Grading…" : "Grade it now"}
            </button>
          </div>
        </div>
      );
    case "analysis_stopped":
      return (
        <div className="note fail">
          <div>
            <b>Analysis failed three runs in a row.</b> The paper has no Gist, Weight or Brief until it succeeds; the abstract stands in.
            {state.error && <div className="err">{state.error}</div>}
          </div>
          <div className="acts">
            <button className="btn" disabled={retry.isPending} onClick={() => retry.mutate(state.revision)}>
              Try again
            </button>
          </div>
        </div>
      );
    case "fulltext_failed":
    case "awaiting_text": {
      // An older revision's Brief is kept until the new one is written; without one the abstract stands in.
      const meanwhile = paper.brief ? `the Brief from revision ${paper.brief.revision} stands meanwhile` : "the abstract stands in for the Brief";
      return (
        <div className="note fail">
          <div>
            {state.kind === "fulltext_failed" ? (
              <>
                <b>Full text failed three runs in a row.</b> Until a pass succeeds, {meanwhile}.
              </>
            ) : (
              <>
                <b>Awaiting the full text for over a week.</b> The archive has not delivered it; {meanwhile}.
              </>
            )}
            {state.error && <div className="err">{state.error}</div>}
          </div>
          <div className="acts">
            <button className="btn" disabled={runPass.isPending} onClick={() => runPass.mutate(state.pass_id!)}>
              {runPass.isPending ? "Starting…" : "Run it now"}
            </button>
          </div>
        </div>
      );
    }
    case "awaiting_classification":
      return (
        <div className="note fail">
          <div>
            <b>Awaiting classification:</b> it failed three runs in a row, so nothing after it has run.
            {state.error && <div className="err">{state.error}</div>}
          </div>
          {form === "classes" ? (
            <ClassifyByHand paper={paper} state={state} onDone={() => setForm(null)} />
          ) : form === "text" ? (
            <FixText paper={paper} state={state} onDone={() => setForm(null)} />
          ) : (
            <div className="acts">
              <button className="btn" disabled={dismiss.isPending} onClick={() => dismiss.mutate(state.revision)}>
                Dismiss
              </button>
              <button className="btn" onClick={() => setForm("classes")}>
                Classify by hand
              </button>
              <button className="btn" onClick={() => setForm("text")}>
                Fix the text
              </button>
            </div>
          )}
        </div>
      );
  }
}

export function Notes({ paper, onLabel }: { paper: Paper; onLabel: (topic: string) => void }) {
  return (
    <>
      {paper.states?.map((s) => (
        // One state per kind and topic or pass: a refetch that drops one keeps the others' open forms.
        <Note key={`${s.kind}:${s.topic ?? ""}:${s.pass_id ?? ""}`} paper={paper} state={s} onLabel={onLabel} />
      ))}
    </>
  );
}
