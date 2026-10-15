/**
 * Needs you (DESIGN.md, "Needs you"): only what waits on the owner, grouped by kind. A settled item turns cyan,
 * keeps its result in place and fades; nothing moves until the page is left.
 */
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { useClassifyByHand, useDismiss, useFixText, useGradeNow, useLabel, useRetryAnalysis } from "@/api/mutations";
import { qualityQuery, taxonomyQuery } from "@/api/queries";
import { Badge } from "@/components/Badge";
import { type LabelValue, type Quality, type Split, type Stuck, waiting } from "@/lib/settings";
import { FitSeg, Later, Load, PageHead, PaperLink, useTopicCards } from "./parts";

type Settled = Record<string, string>;

/** The groups: their name, the line under it, and for a stuck step the name of what failed. */
const KIND = {
  split: { name: "Focus splits", line: "Judge and check disagree. The more inclusive Fit stands until it is labelled." },
  escalated: { name: "Area escalations", line: "The check disagreed with the judge. The less inclusive Fit stands until it is labelled." },
  awaiting: { name: "Awaiting classification", line: "Classification failed three runs; the paper is in no class.", step: "Classification" },
  ungraded: { name: "Ungraded", line: "Grading failed three runs; the paper shows ungraded on its topic.", step: "Grading" },
  analysis: { name: "Analysis", line: "Analysis failed three runs; the paper has no Gist or Weight and shows its title alone.", step: "Analysis" },
} as const;

function Group<T>({ kind, items, children }: { kind: keyof typeof KIND; items: T[]; children: (item: T) => React.ReactNode }) {
  if (!items.length) return null;
  return (
    <div className="ngrp">
      <div className="cap">
        {KIND[kind].name} · {items.length}
      </div>
      <div className="nsub">{KIND[kind].line}</div>
      {items.map(children)}
    </div>
  );
}

/** A split or escalation: the opinions, the standing Fit underlined, and the Fit control until labelled. */
function SplitItem({ s, badge, done, settle }: { s: Split; badge: React.ReactNode; done: string | undefined; settle: (what: string) => void }) {
  const label = useLabel(s.paper_id);
  // The graders' opinions, judge first; the standing Fit after them.
  const order = ["judge", "check", "area_check"];
  const said = Object.entries(s.opinions)
    .filter(([, o]) => o.fit)
    .sort(([a], [b]) => order.indexOf(a) - order.indexOf(b))
    .map(([role, o]) => (
      <span key={role}>
        {role} <b>{o.fit}</b> ·{" "}
      </span>
    ));
  const value = (done as LabelValue | undefined) ?? s.label?.value ?? null;
  return (
    <div className={`need ask${value ? " settled" : ""}`}>
      <div className="nh">
        {badge}
        <PaperLink id={s.paper_id} topic={s.topic} className="nt">
          {s.title}
        </PaperLink>
      </div>
      <div className="nb">
        {said}
        <span className="faint">{value ? <>labelled <b>{value}</b></> : <>{s.fit} stands</>}</span>
      </div>
      <FitSeg value={value} auto={s.fit} label={`Fit on ${s.topic}`} disabled={label.isPending} onPick={(f) => label.mutate({ topic: s.topic, value: f }, { onSuccess: () => settle(f) })} />
    </div>
  );
}

function Stuck({ s, kind, badge, done, children }: { s: Stuck; kind: "awaiting" | "ungraded" | "analysis"; badge?: React.ReactNode; done: string | undefined; children: React.ReactNode }) {
  return (
    <div className={`need fail${done ? " settled" : ""}`}>
      <div className="nh">
        {badge}
        <PaperLink id={s.paper_id} topic={s.topic} className="nt">
          {s.title}
        </PaperLink>
        {kind === "awaiting" && <span className="mono faint">{s.paper_id}</span>}
      </div>
      <div className="nb">
        {KIND[kind].step} failed {s.failed_runs} runs{s.last_error ? `: ${s.last_error}` : "."}
      </div>
      {done ? <div className="nres">{done}</div> : children}
    </div>
  );
}

function Awaiting({ s, done, settle }: { s: Stuck; done: string | undefined; settle: (what: string) => void }) {
  const dismiss = useDismiss(s.paper_id);
  const classify = useClassifyByHand(s.paper_id);
  const fix = useFixText(s.paper_id);
  const { data: tree } = useQuery(taxonomyQuery);
  const [form, setForm] = useState<"classes" | "text" | null>(null);
  const [chosen, setChosen] = useState<string[]>([]);
  const [title, setTitle] = useState(s.title);
  const [abstract, setAbstract] = useState("");
  return (
    <Stuck s={s} kind="awaiting" done={done}>
      {form === "classes" ? (
        <div className="nform">
          <div className="cap2">Its classes</div>
          <div className="row max-h-48 overflow-y-auto">
            {(tree?.classes ?? []).map((c) => (
              <button key={c.slug} className={`fchip${chosen.includes(c.slug) ? " on" : ""}`} aria-pressed={chosen.includes(c.slug)} onClick={() => setChosen((x) => (x.includes(c.slug) ? x.filter((v) => v !== c.slug) : [...x, c.slug]))}>
                {c.name}
              </button>
            ))}
          </div>
          <div className="nact">
            <button className="btn pri" disabled={!chosen.length || classify.isPending} onClick={() => classify.mutate({ revision: s.revision, classes: chosen }, { onSuccess: () => settle("Classified by hand; grading follows tonight.") })}>
              Save
            </button>
            <button className="btn" onClick={() => setForm(null)}>
              Cancel
            </button>
          </div>
        </div>
      ) : form === "text" ? (
        <div className="nform flex flex-col gap-2">
          <input className="field" value={title} onChange={(e) => setTitle(e.target.value)} aria-label="Title" />
          <textarea className="field" rows={5} value={abstract} onChange={(e) => setAbstract(e.target.value)} aria-label="Abstract" placeholder="The abstract, as it should read" />
          <div className="nact">
            <button className="btn pri" disabled={fix.isPending || !abstract.trim()} onClick={() => fix.mutate({ revision: s.revision, title, abstract }, { onSuccess: () => settle("Text fixed; the next run classifies it again.") })}>
              Save the text
            </button>
            <button className="btn" onClick={() => setForm(null)}>
              Cancel
            </button>
          </div>
        </div>
      ) : (
        <div className="nact">
          <button className="btn" disabled={dismiss.isPending} onClick={() => dismiss.mutate(s.revision, { onSuccess: () => settle("Dismissed; it stays searchable in the archive.") })}>
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
    </Stuck>
  );
}

function Ungraded({ s, badge, done, settle }: { s: Stuck; badge: React.ReactNode; done: string | undefined; settle: (what: string) => void }) {
  const grade = useGradeNow(s.paper_id);
  return (
    <Stuck s={s} kind="ungraded" badge={badge} done={done}>
      <div className="nact">
        <button className="btn" disabled={grade.isPending} onClick={() => grade.mutate({ topic: s.topic!, revision: s.revision }, { onSuccess: () => settle("Graded again; the result shows on its topic.") })}>
          {grade.isPending ? "Grading…" : "Grade it now"}
        </button>
      </div>
    </Stuck>
  );
}

function Analysis({ s, done, settle }: { s: Stuck; done: string | undefined; settle: (what: string) => void }) {
  const retry = useRetryAnalysis(s.paper_id);
  return (
    <Stuck s={s} kind="analysis" done={done}>
      <div className="nact">
        <button className="btn" disabled={retry.isPending} onClick={() => retry.mutate(s.revision, { onSuccess: () => settle("Analysis gets three more runs, from the next one.") })}>
          Try again
        </button>
      </div>
    </Stuck>
  );
}

export function NeedsPage() {
  const quality = useQuery(qualityQuery);
  const cards = useTopicCards();
  // What was settled on this visit, by item: shown in place until the page is left.
  const [settled, setSettled] = useState<Settled>({});
  const q = quality.data;
  const badge = (slug: string | null | undefined) => (slug && cards[slug] ? <Badge topic={cards[slug]} /> : null);
  const settle = (key: string) => (what: string) => setSettled((x) => ({ ...x, [key]: what }));
  const open = q ? waiting(q) - Object.keys(settled).length : 0;
  const splits = q?.night.splits.filter((s) => s.split) ?? [];
  const escalations = q?.night.splits.filter((s) => !s.split && s.escalated) ?? [];
  const line = (q: Quality) => (q.night.error ? `Last night's run failed: ${q.night.error}` : open > 0 ? `${open} ${open === 1 ? "thing waits" : "things wait"} on the owner. Each also shows on its paper's page.` : "Nothing waits on the owner.");
  return (
    <>
      <PageHead title="Needs you" sub={q ? line(q) : undefined} />
      <Load q={quality} />
      {q && (
        <>
          {(["split", "escalated"] as const).map((kind) => (
            <Group key={kind} kind={kind} items={kind === "split" ? splits : escalations}>
              {(s) => {
                const key = `${s.paper_id}:${s.topic}`;
                return <SplitItem key={key} s={s} badge={badge(s.topic)} done={settled[key]} settle={settle(key)} />;
              }}
            </Group>
          ))}
          <Group kind="awaiting" items={q.night.awaiting_classification}>
            {(s) => <Awaiting key={s.paper_id} s={s} done={settled[`c:${s.paper_id}`]} settle={settle(`c:${s.paper_id}`)} />}
          </Group>
          <Group kind="ungraded" items={q.night.ungraded}>
            {(s) => {
              const key = `g:${s.paper_id}:${s.topic}`;
              return <Ungraded key={key} s={s} badge={badge(s.topic)} done={settled[key]} settle={settle(key)} />;
            }}
          </Group>
          <Group kind="analysis" items={q.night.analysis_failed}>
            {(s) => <Analysis key={s.paper_id} s={s} done={settled[`a:${s.paper_id}`]} settle={settle(`a:${s.paper_id}`)} />}
          </Group>
          <Later>Full text and digests join this page when their passes land (N3 stage 2, N4).</Later>
        </>
      )}
    </>
  );
}
