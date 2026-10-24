/**
 * Needs you (DESIGN.md, "Needs you"): only what waits on the owner, grouped by kind. A settled item turns cyan,
 * keeps its result in place and fades; nothing moves until the page is left. Each action's mutation writes the
 * mark into the cached quality page (`settleNeeds` in mutations.ts), so the row settles even when the page was left
 * before the write returned, the sidebar's count follows and the next fresh visit fetches afresh. Each red group has
 * Acknowledge, and the page Acknowledge all: what the owner has seen folds into the Acknowledged section at the foot,
 * grouped the same way and keeping its buttons, and stops counting in the run status. Deep notes, neutral, wait on
 * nothing and count nowhere, but are acknowledged and fold away like the red groups, for the card shown. Each group
 * folds under its head (`Group`).
 */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useReducer, useState } from "react";
import { useAcknowledge, useClassifyByHand, useDismiss, useFixText, useGradeNow, useLabel, useRetryAnalysis, useRunPassNow, useUnlabel, useWriteAgain } from "@/api/mutations";
import type { Schemas } from "@/api/client";
import { paperQuery, qualityQuery, taxonomyQuery } from "@/api/queries";
import { Badge } from "@/components/Badge";
import { rowDate } from "@/lib/format";
import { setShelfOpen, shelfOpen } from "@/lib/remember";
import { ACKNOWLEDGEABLE, type Acknowledgeable, acknowledgedCount, any, type DeepNotes, type FailedDigest as FailedDigestRow, type LabelValue, openSplits, type Quality, type Split, type Stuck, type TextPassStuck, toAcknowledge, waiting } from "@/lib/settings";
import { Failed, FitSeg, Load, PageHead, PaperLink, useTopicCards } from "./parts";

/** The groups: their name, the line under it, and for a stuck step the name of what failed. */
const KIND = {
  split: { name: "Focus splits", line: "Judge and check disagree. The more inclusive Fit stands until it is labelled." },
  one_lab: { name: "Graded by one lab", line: "One grader's lab refused the paper throughout and only the other answered. Its Fit stands until it is labelled." },
  escalated: { name: "Area escalations", line: "The decision model doubted the judge, and the check disagreed with it. The less inclusive Fit stands until it is labelled." },
  awaiting: { name: "Awaiting classification", line: "Classification failed three runs or the classifier refused it; the paper is in no class.", step: "Classification" },
  ungraded: { name: "Ungraded", line: "Grading failed three runs or every model refused it; the paper shows ungraded on its topic.", step: "Grading" },
  analysis: { name: "Analysis", line: "Analysis failed three runs or every model refused it; the paper has no Gist or Weight and shows its title alone.", step: "Analysis" },
  fulltext: { name: "Full text", line: "A full-text pass failed three runs, every model refused it, or it has waited for its text over a week; the abstract stands in for the Brief." },
  digests: { name: "Digests", line: "A digest could not be written; its papers stay due and the next run writes it again." },
  deep_notes: { name: "Deep notes", line: "Deep cards whose own process held them back: a page out of reach, a tool that failed, the budget used up. Nothing waits." },
} as const;

/** A group longer than this starts folded to its head and line. */
const FOLD_OVER = 10;

/** A group of items with its name, count and line; `acknowledge`, for a red group or Deep notes with something still
 * unseen. Its head folds or opens it, Acknowledge staying beside the head; a group of more than ten starts folded, and
 * the owner's choice is remembered for the session, the Acknowledged section's groups apart from the page's. The head
 * keeps the group's edge, yellow for the owner's word, red for a failure and grey for Deep notes, so a folded group
 * still shows which. */
function Group<T>({ kind, items, acknowledge, acknowledged = false, children }: { kind: keyof typeof KIND; items: T[]; acknowledge?: React.ReactNode; acknowledged?: boolean; children: (item: T) => React.ReactNode }) {
  const [, rerender] = useReducer((n: number) => n + 1, 0);
  if (!items.length) return null;
  const key = `needs:${acknowledged ? "acknowledged:" : ""}${kind}`;
  const open = shelfOpen(key, items.length <= FOLD_OVER);
  const toggle = () => {
    setShelfOpen(key, !open);
    rerender();
  };
  const ask = kind === "split" || kind === "one_lab" || kind === "escalated";
  return (
    <div className={`ngrp${ask ? " ask" : kind === "deep_notes" ? "" : " fail"}`}>
      <div className="caprow">
        <button className="tpfold" onClick={toggle} aria-expanded={open}>
          <span className={`car${open ? " open" : ""}`} aria-hidden />
          <span className="ct">
            {KIND[kind].name} · <b>{items.length}</b>
          </span>
        </button>
        {acknowledge}
      </div>
      <div className="nsub">{KIND[kind].line}</div>
      {/* Folded rows stay mounted, hidden, so a draft or a pending action in one survives the fold. */}
      <div hidden={!open}>{items.map(children)}</div>
    </div>
  );
}

/** A split, escalation or grade from one lab: the opinions (for one lab, first the side that refused), the standing
 * Fit underlined, and the Fit control until labelled. Tapping the label's segment again takes the label back, and
 * the item waits once more. */
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
        {s.one_lab && <span>{s.opinions.judge ? "check" : "judge"} refused · </span>}
        {said}
        <span className="faint">{value ? <>labelled <b>{value}</b></> : <>{s.fit} stands</>}</span>
      </div>
      <FitSeg value={value} auto={s.fit} label={`Fit on ${s.topic}`} disabled={label.isPending} clear onPick={(f) => (f ? label.mutate({ topic: s.topic, value: f, revision: s.revision }) : unlabel.mutate({ topic: s.topic, revision: s.revision }))} />
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
      <div className="nb">{s.stopped_by_refusal ? `Refused by every ${s.refused?.join(" and ")} model.` : `${s.refused?.length ? `${s.refused.join(" and ")} refused · ` : ""}${KIND[kind].step} failed ${s.failed_runs} runs${s.last_error ? `: ${s.last_error}` : "."}`}</div>
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
  // The text the form started from, and the paper revision the row showed then: the fix is for that text.
  const [was, setWas] = useState({ title: s.title, abstract: "", revision: s.revision });
  const [textFailed, setTextFailed] = useState(false);
  /** The form starts from the paper's text as it stands, so a fix changes only what it touches. A read that fails
   * is said, with the way to try again; no form opens on a text not read. */
  const openText = async () => {
    setTextFailed(false);
    let p: Schemas["Paper"];
    try {
      p = await qc.fetchQuery(paperQuery(s.paper_id));
    } catch {
      return setTextFailed(true);
    }
    const text = { title: p.title, abstract: p.abstract ?? "", revision: s.revision };
    setTitle(text.title);
    setAbstract(text.abstract);
    setWas(text);
    setForm("text");
  };
  const changed = title.trim() !== was.title.trim() || abstract.trim() !== was.abstract.trim();
  // The row's revision moved under the open form (a refresh brought a rewrite): the draft is the old text, and is
  // not saved under the new revision; the owner reloads the text and fixes that.
  const moved = form === "text" && was.revision !== s.revision;
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
          {moved && (
            <div className="nb warnt" role="alert">
              The paper's text changed meanwhile (revision {s.revision}; this draft is for revision {was.revision}).{" "}
              <button className="inl" onClick={() => void openText()}>
                Reload the text
              </button>
            </div>
          )}
          <input className="field" value={title} onChange={(e) => setTitle(e.target.value)} aria-label="Title" />
          <textarea className="field" rows={5} value={abstract} onChange={(e) => setAbstract(e.target.value)} aria-label="Abstract" placeholder="The abstract, as it should read" />
          <div className="nact">
            <button className="btn pri" disabled={moved || fix.isPending || !changed || !(title.trim() || abstract.trim())} onClick={() => fix.mutate({ revision: was.revision, title, abstract })}>
              Save the text
            </button>
            <button className="btn" onClick={() => setForm(null)}>
              Cancel
            </button>
          </div>
        </div>
      ) : (
        <>
          {textFailed && (
            <div className="loadmore">
              Could not load the paper's text ·{" "}
              <button className="inl" onClick={() => void openText()}>
                Try again
              </button>
            </div>
          )}
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
        </>
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

/** A full-text pass that failed three runs or waits for its text over a week; Run it now puts it first in line, Use
 * the PDF does so reading the paper's PDF from then on, for LaTeX that reads badly. */
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
        {s.state !== "failed" ? `${what} has waited for the paper's text since ${rowDate(s.queued_at)}; the archive has not delivered it.` : s.refused ? `${what} refused by every model.` : `${what} failed ${s.failed_runs} runs${s.last_error ? `: ${s.last_error}` : "."}`}
      </div>
      {s.settled ? (
        <div className="nres">{s.settled}</div>
      ) : (
        <div className="nact">
          <button className="btn" disabled={run.isPending} onClick={() => run.mutate({ id: s.id, state: s.state })}>
            {run.isPending ? "Starting…" : "Run it now"}
          </button>
          <button className="btn" disabled={run.isPending} onClick={() => run.mutate({ id: s.id, state: s.state, pdf: true })}>
            Use the PDF
          </button>
        </div>
      )}
    </div>
  );
}

/** A failed digest: which one and why; Write it again queues a job that writes it as soon as the run lock is free,
 * and the row notes where the job is (`note`) until it ends. A digest still failed after it (the writer failed
 * again, or no worker runs jobs) stays on the page with the reason, still to be written, as the server still counts
 * it; one written settles, and one the API says is no longer one to write again (written meanwhile, its topic
 * paused) settles with that reason, as the server no longer counts it. */
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

/** A paper whose Deep card carries process notes: its title, then each note on a line of its own with its kind. The
 * notes' links are on the card. */
function DeepNote({ c }: { c: DeepNotes }) {
  return (
    <div className="need">
      <div className="nh">
        <PaperLink id={c.paper_id} className="nt">
          {c.title}
        </PaperLink>
      </div>
      {c.notes.map((n, i) => (
        <div key={i} className="nb">
          <i className="tag">{n.kind}</i>
          {n.note}
        </div>
      ))}
    </div>
  );
}

export function NeedsPage() {
  const quality = useQuery(qualityQuery("needs"));
  const cards = useTopicCards();
  const ack = useAcknowledge();
  const [folded, setFolded] = useState(true);
  const q = quality.data;
  const badge = (slug: string | null | undefined) => (slug && cards[slug] ? <Badge topic={cards[slug]} /> : null);
  const open = q ? waiting(q) : 0;
  // A split labelled before this visit no longer waits; one labelled on it stays, settled.
  const { splits, escalations, oneLab } = openSplits(q?.night.splits ?? []);
  const line = (q: Quality) => (q.night.error ? `Last night's run failed: ${q.night.error}` : open > 0 ? `${open} ${open === 1 ? "thing waits" : "things wait"} on the owner. Each also shows on its paper's page.` : "Nothing waits on the owner.");
  const button = (lists: Acknowledgeable[], label: string) => {
    const seen = q && toAcknowledge(q, lists);
    return seen && any(seen) ? (
      <button className="btn" disabled={ack.isPending} onClick={() => ack.mutate(seen)}>
        {label}
      </button>
    ) : null;
  };
  /** The red groups' and Deep notes' rows acknowledged or not: the page's groups, or the Acknowledged section's. */
  const seenable = (q: Quality, acknowledged: boolean) => {
    const pick = <T extends { acknowledged_at?: string | null }>(xs: T[]) => xs.filter((x) => Boolean(x.acknowledged_at) === acknowledged);
    const n = q.night;
    const acknowledge = (list: Acknowledgeable) => (acknowledged ? undefined : button([list], "Acknowledge"));
    return (
      <>
        <Group kind="awaiting" acknowledged={acknowledged} items={pick(n.awaiting_classification)} acknowledge={acknowledge("awaiting_classification")}>
          {(s) => <Awaiting key={s.paper_id} s={s} />}
        </Group>
        <Group kind="ungraded" acknowledged={acknowledged} items={pick(n.ungraded)} acknowledge={acknowledge("ungraded")}>
          {(s) => <Ungraded key={`${s.paper_id}:${s.topic}`} s={s} badge={badge(s.topic)} />}
        </Group>
        <Group kind="analysis" acknowledged={acknowledged} items={pick(n.analysis_failed)} acknowledge={acknowledge("analysis_failed")}>
          {(s) => <Analysis key={s.paper_id} s={s} />}
        </Group>
        <Group kind="fulltext" acknowledged={acknowledged} items={pick(n.fulltext)} acknowledge={acknowledge("fulltext")}>
          {(s) => <TextPass key={s.id} s={s} />}
        </Group>
        <Group kind="digests" acknowledged={acknowledged} items={pick(n.digests)} acknowledge={acknowledge("digests")}>
          {(d) => <FailedDigest key={d.id} d={d} badge={badge(d.topic)} />}
        </Group>
        <Group kind="deep_notes" acknowledged={acknowledged} items={pick(n.deep_notes)} acknowledge={acknowledge("deep_notes")}>
          {(c) => <DeepNote key={`${c.paper_id}:${c.written_at}`} c={c} />}
        </Group>
      </>
    );
  };
  const seen = q ? acknowledgedCount(q) : 0;
  return (
    <>
      <PageHead title="Needs you" sub={q ? line(q) : undefined}>
        {button(ACKNOWLEDGEABLE, "Acknowledge all")}
      </PageHead>
      <Load q={quality} />
      {q && (
        <>
          {(["split", "one_lab", "escalated"] as const).map((kind) => (
            <Group key={kind} kind={kind} items={{ split: splits, one_lab: oneLab, escalated: escalations }[kind]}>
              {(s) => <SplitItem key={`${s.paper_id}:${s.topic}`} s={s} badge={badge(s.topic)} />}
            </Group>
          ))}
          {seenable(q, false)}
          {seen > 0 && (
            <section className="nack" aria-label="Acknowledged">
              <button className="tpfold" onClick={() => setFolded(!folded)} aria-expanded={!folded}>
                <span className={`car${folded ? "" : " open"}`} aria-hidden />
                <span className="ct">
                  Acknowledged · <b>{seen}</b>
                </span>
              </button>
              {!folded && seenable(q, true)}
            </section>
          )}
        </>
      )}
    </>
  );
}
