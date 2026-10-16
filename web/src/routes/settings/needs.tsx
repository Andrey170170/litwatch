/**
 * Needs you (DESIGN.md, "Needs you"): only what waits on the owner, grouped by kind. A settled item turns cyan,
 * keeps its result in place and fades; nothing moves until the page is left. Each action's mutation writes the
 * mark into the cached quality page (`settleNeeds` in mutations.ts), so the row settles even when the page was left
 * before the write returned, the sidebar's count follows and the next fresh visit fetches afresh.
 */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useClassifyByHand, useDismiss, useFixText, useGradeNow, useLabel, useRetryAnalysis, useRunPassNow, useUnlabel, useWriteAgain } from "@/api/mutations";
import { paperQuery, qualityQuery, taxonomyQuery } from "@/api/queries";
import { Badge } from "@/components/Badge";
import { rowDate } from "@/lib/format";
import { type FailedDigest as FailedDigestRow, type LabelValue, openSplits, type Quality, type Split, type Stuck, type TextPassStuck, waiting } from "@/lib/settings";
import { Failed, FitSeg, Load, PageHead, PaperLink, useTopicCards } from "./parts";

/** The groups: their name, the line under it, and for a stuck step the name of what failed. */
const KIND = {
  split: { name: "Focus splits", line: "Judge and check disagree. The more inclusive Fit stands until it is labelled." },
  escalated: { name: "Area escalations", line: "The check disagreed with the judge. The less inclusive Fit stands until it is labelled." },
  awaiting: { name: "Awaiting classification", line: "Classification failed three runs; the paper is in no class.", step: "Classification" },
  ungraded: { name: "Ungraded", line: "Grading failed three runs; the paper shows ungraded on its topic.", step: "Grading" },
  analysis: { name: "Analysis", line: "Analysis failed three runs; the paper has no Gist or Weight and shows its title alone.", step: "Analysis" },
  fulltext: { name: "Full text", line: "A full-text pass failed three runs, or has waited for its text over a week; the abstract stands in for the Brief." },
  digests: { name: "Digests", line: "A digest could not be written; its papers stay due and the next run writes it again." },
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

/** A split or escalation: the opinions, the standing Fit underlined, and the Fit control until labelled. Tapping the
 * label's segment again takes the label back, and the item waits once more. */
function SplitItem({ s, badge }: { s: Split; badge: React.ReactNode }) {
  const label = useLabel(s.paper_id);
  const unlabel = useUnlabel(s.paper_id);
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
  const value = (s.settled as LabelValue | undefined) ?? s.label?.value ?? null;
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
      <FitSeg value={value} auto={s.fit} label={`Fit on ${s.topic}`} disabled={label.isPending} clear onPick={(f) => (f ? label.mutate({ topic: s.topic, value: f }) : unlabel.mutate(s.topic))} />
    </div>
  );
}

function Stuck({ s, kind, badge, children }: { s: Stuck; kind: "awaiting" | "ungraded" | "analysis"; badge?: React.ReactNode; children: React.ReactNode }) {
  return (
    <div className={`need fail${s.settled ? " settled" : ""}`}>
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
      {s.settled ? <div className="nres">{s.settled}</div> : children}
    </div>
  );
}

function Awaiting({ s }: { s: Stuck }) {
  const dismiss = useDismiss(s.paper_id);
  const classify = useClassifyByHand(s.paper_id);
  const fix = useFixText(s.paper_id);
  const tree = useQuery(taxonomyQuery);
  const [form, setForm] = useState<"classes" | "text" | null>(null);
  const [chosen, setChosen] = useState<string[]>([]);
  const qc = useQueryClient();
  const [title, setTitle] = useState(s.title);
  const [abstract, setAbstract] = useState("");
  const [was, setWas] = useState({ title: s.title, abstract: "" });
  /** The form starts from the paper's text as it stands, so a fix changes only what it touches. */
  const openText = async () => {
    const p = await qc.fetchQuery(paperQuery(s.paper_id)).catch(() => null);
    const text = { title: p?.title ?? s.title, abstract: p?.abstract ?? "" };
    setTitle(text.title);
    setAbstract(text.abstract);
    setWas(text);
    setForm("text");
  };
  const changed = title.trim() !== was.title.trim() || abstract.trim() !== was.abstract.trim();
  return (
    <Stuck s={s} kind="awaiting">
      {form === "classes" ? (
        <div className="nform">
          <div className="cap2">Its classes</div>
          <div className="row max-h-48 overflow-y-auto">
            {(tree.data?.classes ?? []).map((c) => (
              <button key={c.slug} className={`fchip${chosen.includes(c.slug) ? " on" : ""}`} aria-pressed={chosen.includes(c.slug)} onClick={() => setChosen((x) => (x.includes(c.slug) ? x.filter((v) => v !== c.slug) : [...x, c.slug]))}>
                {c.name}
              </button>
            ))}
          </div>
          <Failed q={tree} what="the classes" />
          <div className="nact">
            <button className="btn pri" disabled={!chosen.length || classify.isPending} onClick={() => classify.mutate({ revision: s.revision, classes: chosen })}>
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
            <button className="btn pri" disabled={fix.isPending || !changed || !(title.trim() || abstract.trim())} onClick={() => fix.mutate({ revision: s.revision, title, abstract })}>
              Save the text
            </button>
            <button className="btn" onClick={() => setForm(null)}>
              Cancel
            </button>
          </div>
        </div>
      ) : (
        <div className="nact">
          <button className="btn" disabled={dismiss.isPending} onClick={() => dismiss.mutate(s.revision)}>
            Dismiss
          </button>
          <button className="btn" onClick={() => setForm("classes")}>
            Classify by hand
          </button>
          <button className="btn" onClick={() => void openText()}>
            Fix the text
          </button>
        </div>
      )}
    </Stuck>
  );
}

/** Grade it now counts as the first of three more runs: a run that fails again settles the item too, since the
 * nightly runs take it from here and it no longer waits on the owner. */
function Ungraded({ s, badge }: { s: Stuck; badge: React.ReactNode }) {
  const grade = useGradeNow(s.paper_id);
  return (
    <Stuck s={s} kind="ungraded" badge={badge}>
      <div className="nact">
        <button className="btn" disabled={grade.isPending} onClick={() => grade.mutate({ topic: s.topic!, revision: s.revision })}>
          {grade.isPending ? "Grading…" : "Grade it now"}
        </button>
      </div>
    </Stuck>
  );
}

/** A full-text pass that failed three runs or waits for its text over a week; Run it now puts it first in line. */
function TextPass({ s }: { s: TextPassStuck }) {
  const run = useRunPassNow(s.paper_id);
  const what = { brief: "Brief", weight: "Weight re-check", deep: "Deep card" }[s.kind];
  return (
    <div className={`need fail${s.settled ? " settled" : ""}`}>
      <div className="nh">
        <PaperLink id={s.paper_id} className="nt">
          {s.title}
        </PaperLink>
      </div>
      <div className="nb">
        {s.state === "failed" ? `${what} failed ${s.failed_runs} runs${s.last_error ? `: ${s.last_error}` : "."}` : `${what} has waited for the paper's text since ${rowDate(s.queued_at)}; the archive has not delivered it.`}
      </div>
      {s.settled ? (
        <div className="nres">{s.settled}</div>
      ) : (
        <div className="nact">
          <button className="btn" disabled={run.isPending} onClick={() => run.mutate({ id: s.id, state: s.state })}>
            {run.isPending ? "Starting…" : "Run it now"}
          </button>
        </div>
      )}
    </div>
  );
}

/** A failed digest: which one and why; Write it again runs the writer now. A digest still failed after it (the
 * writer failed again, or a job holds the run lock) stays on the page with the reason (`note`), still to be
 * written, as the server still counts it; one the API says is no longer one to write again (written meanwhile, its
 * topic paused) settles with that reason, as the server no longer counts it. */
function FailedDigest({ d, badge }: { d: FailedDigestRow; badge: React.ReactNode }) {
  const write = useWriteAgain();
  const when = d.period === "day" ? `Daily digest · ${rowDate(d.day)}` : `Weekly digest · the week of ${rowDate(d.day)}`;
  return (
    <div className={`need fail${d.settled ? " settled" : ""}`}>
      <div className="nh">
        {badge}
        <span className="nt">{when}</span>
      </div>
      <div className="nb">{d.error ? `Could not be written: ${d.error}` : "Could not be written."}</div>
      {d.note && !d.settled && <div className="nb">{d.note}</div>}
      {d.settled ? (
        <div className="nres">{d.settled}</div>
      ) : (
        <div className="nact">
          <button className="btn" disabled={write.isPending} onClick={() => write.mutate(d.id)}>
            {write.isPending ? "Writing…" : "Write it again"}
          </button>
        </div>
      )}
    </div>
  );
}

function Analysis({ s }: { s: Stuck }) {
  const retry = useRetryAnalysis(s.paper_id);
  return (
    <Stuck s={s} kind="analysis">
      <div className="nact">
        <button className="btn" disabled={retry.isPending} onClick={() => retry.mutate(s.revision)}>
          Try again
        </button>
      </div>
    </Stuck>
  );
}

export function NeedsPage() {
  const quality = useQuery(qualityQuery("needs"));
  const cards = useTopicCards();
  const q = quality.data;
  const badge = (slug: string | null | undefined) => (slug && cards[slug] ? <Badge topic={cards[slug]} /> : null);
  const open = q ? waiting(q) : 0;
  // A split labelled before this visit no longer waits; one labelled on it stays, settled.
  const { splits, escalations } = openSplits(q?.night.splits ?? []);
  const line = (q: Quality) => (q.night.error ? `Last night's run failed: ${q.night.error}` : open > 0 ? `${open} ${open === 1 ? "thing waits" : "things wait"} on the owner. Each also shows on its paper's page.` : "Nothing waits on the owner.");
  return (
    <>
      <PageHead title="Needs you" sub={q ? line(q) : undefined} />
      <Load q={quality} />
      {q && (
        <>
          {(["split", "escalated"] as const).map((kind) => (
            <Group key={kind} kind={kind} items={kind === "split" ? splits : escalations}>
              {(s) => <SplitItem key={`${s.paper_id}:${s.topic}`} s={s} badge={badge(s.topic)} />}
            </Group>
          ))}
          <Group kind="awaiting" items={q.night.awaiting_classification}>
            {(s) => <Awaiting key={s.paper_id} s={s} />}
          </Group>
          <Group kind="ungraded" items={q.night.ungraded}>
            {(s) => <Ungraded key={`${s.paper_id}:${s.topic}`} s={s} badge={badge(s.topic)} />}
          </Group>
          <Group kind="analysis" items={q.night.analysis_failed}>
            {(s) => <Analysis key={s.paper_id} s={s} />}
          </Group>
          <Group kind="fulltext" items={q.night.fulltext}>
            {(s) => <TextPass key={s.id} s={s} />}
          </Group>
          <Group kind="digests" items={q.night.digests}>
            {(d) => <FailedDigest key={d.id} d={d} badge={badge(d.topic)} />}
          </Group>
        </>
      )}
    </>
  );
}
