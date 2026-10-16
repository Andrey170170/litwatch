/**
 * Needs you (DESIGN.md, "Needs you"): only what waits on the owner, grouped by kind. A settled item turns cyan,
 * keeps its result in place and fades; nothing moves until the page is left. The mark is written into the cached
 * quality page, so the sidebar's count follows and the next visit fetches afresh.
 */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { ApiError } from "@/api/client";
import { digestGone, said, useClassifyByHand, useDismiss, useFixText, useGradeNow, useLabel, useRetryAnalysis, useRunPassNow, useUnlabel, useWriteAgain } from "@/api/mutations";
import { paperQuery, qualityQuery, taxonomyQuery } from "@/api/queries";
import { Badge } from "@/components/Badge";
import { rowDate } from "@/lib/format";
import { type FailedDigest as FailedDigestRow, type LabelValue, openSplits, type Quality, settle, type Settled, type Split, type Stuck, type TextPassStuck, type Waiting, waiting } from "@/lib/settings";
import { FitSeg, Load, PageHead, PaperLink, useTopicCards } from "./parts";

type Settle = (what: string | undefined) => void;

/** A 409 says the item no longer waits on the owner (it ran, the text changed, another action took it): it settles
 * with the API's reason, so the row stays where it is and the count follows. */
const gone = (settle: Settle) => (e: unknown) => e instanceof ApiError && e.status === 409 && settle(`No longer waits on the owner: ${said(e)}.`);

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
function SplitItem({ s, badge, settle }: { s: Split; badge: React.ReactNode; settle: Settle }) {
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
      <FitSeg value={value} auto={s.fit} label={`Fit on ${s.topic}`} disabled={label.isPending} clear onPick={(f) => (f ? label.mutate({ topic: s.topic, value: f }, { onSuccess: () => settle(f) }) : unlabel.mutate(s.topic, { onSuccess: () => settle(undefined) }))} />
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

function Awaiting({ s, settle }: { s: Stuck; settle: Settle }) {
  const dismiss = useDismiss(s.paper_id);
  const classify = useClassifyByHand(s.paper_id);
  const fix = useFixText(s.paper_id);
  const { data: tree } = useQuery(taxonomyQuery);
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
            {(tree?.classes ?? []).map((c) => (
              <button key={c.slug} className={`fchip${chosen.includes(c.slug) ? " on" : ""}`} aria-pressed={chosen.includes(c.slug)} onClick={() => setChosen((x) => (x.includes(c.slug) ? x.filter((v) => v !== c.slug) : [...x, c.slug]))}>
                {c.name}
              </button>
            ))}
          </div>
          <div className="nact">
            <button className="btn pri" disabled={!chosen.length || classify.isPending} onClick={() => classify.mutate({ revision: s.revision, classes: chosen }, { onSuccess: () => settle("Classified by hand; grading follows tonight."), onError: gone(settle) })}>
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
            <button className="btn pri" disabled={fix.isPending || !changed || !(title.trim() || abstract.trim())} onClick={() => fix.mutate({ revision: s.revision, title, abstract }, { onSuccess: () => settle("Text fixed; the next run classifies it again."), onError: gone(settle) })}>
              Save the text
            </button>
            <button className="btn" onClick={() => setForm(null)}>
              Cancel
            </button>
          </div>
        </div>
      ) : (
        <div className="nact">
          <button className="btn" disabled={dismiss.isPending} onClick={() => dismiss.mutate(s.revision, { onSuccess: () => settle("Dismissed; it stays searchable in the archive."), onError: gone(settle) })}>
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
function Ungraded({ s, badge, settle }: { s: Stuck; badge: React.ReactNode; settle: Settle }) {
  const grade = useGradeNow(s.paper_id);
  return (
    <Stuck s={s} kind="ungraded" badge={badge}>
      <div className="nact">
        <button className="btn" disabled={grade.isPending} onClick={() => grade.mutate({ topic: s.topic!, revision: s.revision }, { onSuccess: (r) => settle(r.graded ? `Graded again: ${r.fit}. The result shows on its topic.` : `Grading failed again: ${r.error ?? "no verdict"}. The nightly runs try twice more.`), onError: gone(settle) })}>
          {grade.isPending ? "Grading…" : "Grade it now"}
        </button>
      </div>
    </Stuck>
  );
}

/** A full-text pass that failed three runs or waits for its text over a week; Run it now puts it first in line. */
function TextPass({ s, settle }: { s: TextPassStuck; settle: Settle }) {
  const run = useRunPassNow(s.paper_id);
  const what = s.kind === "brief" ? "Brief" : "Weight re-check";
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
          <button className="btn" disabled={run.isPending} onClick={() => run.mutate(s.id, { onSuccess: () => settle(s.state === "failed" ? "Running now, first in line, with three more runs." : "Asked for the text again, at urgent priority; the pass runs once it arrives."), onError: gone(settle) })}>
            {run.isPending ? "Starting…" : "Run it now"}
          </button>
        </div>
      )}
    </div>
  );
}

/** A failed digest: which one and why; Write it again runs the writer now. A digest still failed after it (the
 * writer failed again, or a job holds the run lock) stays on the page with the reason, still to be written, as the
 * server still counts it; one the API says is no longer one to write again (written meanwhile, its topic paused)
 * settles with that reason, as the server no longer counts it. */
function FailedDigest({ d, badge, settle }: { d: FailedDigestRow; badge: React.ReactNode; settle: Settle }) {
  const write = useWriteAgain();
  const [note, setNote] = useState<string | null>(null);
  const when = d.period === "day" ? `Daily digest · ${rowDate(d.day)}` : `Weekly digest · the week of ${rowDate(d.day)}`;
  const onDone = (r: { state: string; error?: string | null }) => (r.state === "failed" ? setNote(`Failed again: ${r.error ?? "no text"}. The next run writes it again.`) : settle("Written again; it shows in the Digest."));
  return (
    <div className={`need fail${d.settled ? " settled" : ""}`}>
      <div className="nh">
        {badge}
        <span className="nt">{when}</span>
      </div>
      <div className="nb">{d.error ? `Could not be written: ${d.error}` : "Could not be written."}</div>
      {note && <div className="nb">{note}</div>}
      {d.settled ? (
        <div className="nres">{d.settled}</div>
      ) : (
        <div className="nact">
          <button className="btn" disabled={write.isPending} onClick={() => write.mutate(d.id, { onSuccess: onDone, onError: (e) => (digestGone(e) ? settle(`No longer to be written: ${said(e)}.`) : setNote(`Not written now: ${said(e)}.`)) })}>
            {write.isPending ? "Writing…" : "Write it again"}
          </button>
        </div>
      )}
    </div>
  );
}

function Analysis({ s, settle }: { s: Stuck; settle: Settle }) {
  const retry = useRetryAnalysis(s.paper_id);
  return (
    <Stuck s={s} kind="analysis">
      <div className="nact">
        <button className="btn" disabled={retry.isPending} onClick={() => retry.mutate(s.revision, { onSuccess: () => settle("Analysis gets three more runs, from the next one."), onError: gone(settle) })}>
          Try again
        </button>
      </div>
    </Stuck>
  );
}

export function NeedsPage() {
  const quality = useQuery(qualityQuery("needs"));
  const qc = useQueryClient();
  const cards = useTopicCards();
  const q = quality.data;
  const badge = (slug: string | null | undefined) => (slug && cards[slug] ? <Badge topic={cards[slug]} /> : null);
  // Marks the item settled in the cached page: it stays where it is, and the counts follow.
  const settler = (list: Waiting, s: Settled) => (what: string | undefined) => qc.setQueryData(qualityQuery("needs").queryKey, (old: Quality | undefined) => old && settle(old, list, s, what));
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
              {(s) => <SplitItem key={`${s.paper_id}:${s.topic}`} s={s} badge={badge(s.topic)} settle={settler("splits", { paper_id: s.paper_id, topic: s.topic })} />}
            </Group>
          ))}
          <Group kind="awaiting" items={q.night.awaiting_classification}>
            {(s) => <Awaiting key={s.paper_id} s={s} settle={settler("awaiting_classification", { paper_id: s.paper_id })} />}
          </Group>
          <Group kind="ungraded" items={q.night.ungraded}>
            {(s) => <Ungraded key={`${s.paper_id}:${s.topic}`} s={s} badge={badge(s.topic)} settle={settler("ungraded", { paper_id: s.paper_id, topic: s.topic })} />}
          </Group>
          <Group kind="analysis" items={q.night.analysis_failed}>
            {(s) => <Analysis key={s.paper_id} s={s} settle={settler("analysis_failed", { paper_id: s.paper_id })} />}
          </Group>
          <Group kind="fulltext" items={q.night.fulltext}>
            {(s) => <TextPass key={s.id} s={s} settle={settler("fulltext", { paper_id: s.paper_id, id: s.id })} />}
          </Group>
          <Group kind="digests" items={q.night.digests}>
            {(d) => <FailedDigest key={d.id} d={d} badge={badge(d.topic)} settle={settler("digests", { id: d.id })} />}
          </Group>
        </>
      )}
    </>
  );
}
