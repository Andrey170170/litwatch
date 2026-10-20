/**
 * One row per topic the paper is in (DESIGN.md, "The rail"): badge, name, grade, the verdict or the owner's word,
 * the two thumbs, and Label, which opens the Fit control. A split or escalation keeps the control open until
 * labelled; an ungraded topic offers to grade it now.
 */
import { useState } from "react";
import type { Schemas } from "@/api/client";
import { useGradeNow, useLabel, useReact, useUnlabel, useUnreact } from "@/api/mutations";
import { Badge } from "@/components/Badge";
import { FITS, Seg } from "@/components/Seg";
import { gradeLine, rc, type Weight } from "@/lib/rarity";
import { REASONS, type ReactionValue, reasonLabel, speaks } from "@/lib/reactions";

export type PaperTopic = Schemas["PaperTopic"];
/** The Fit control: five equal segments; the owner's label filled, the verdict's underlined while unlabelled.
 * Tapping the filled segment removes the label. */
export function FitControl({ paperId, t, revision }: { paperId: string; t: PaperTopic; revision: number }) {
  const label = useLabel(paperId);
  const unlabel = useUnlabel(paperId);
  const on = t.label?.value ?? null;
  return <Seg values={FITS} value={on} auto={t.verdict?.fit} clear disabled={label.isPending || unlabel.isPending} label={`Fit for ${t.topic.name}`} onPick={(f) => (f ? label.mutate({ topic: t.topic.slug, value: f, revision }) : unlabel.mutate({ topic: t.topic.slug, revision }))} />;
}

/** The reasons for a thumb as chips, and Clear when the thumb is lit; nothing is recorded until a reason is picked. */
export function Reasons({ paperId, topic, value, current, onDone, big }: { paperId: string; topic: string | null; value: ReactionValue; current: PaperTopic["reaction"] | null | undefined; onDone: () => void; big?: boolean }) {
  const react = useReact(paperId);
  const unreact = useUnreact(paperId);
  // One write at a time, so a Clear cannot land before a slower change and be undone by it.
  const busy = react.isPending || unreact.isPending;
  return (
    <div className={`why${big ? " big" : ""}`} role="group" aria-label="Reason">
      {REASONS[value].map((r) => (
        <button
          key={r.reason}
          className={current?.value === value && current.reason === r.reason ? "on" : ""}
          onClick={() => react.mutate({ topic, value, reason: r.reason }, { onSuccess: onDone })}
          disabled={busy}
        >
          {r.label}
        </button>
      ))}
      {current && (
        <button className="clr" onClick={() => unreact.mutate(topic, { onSuccess: onDone })} disabled={busy}>
          Clear
        </button>
      )}
    </div>
  );
}

/** "Fit central · Weight landmark", "your label: related", or the open state's line. */
/** Whose Fit the row shows: the owner's word and where it came from, else the pipeline's (feedback.md, "The owner's
 * word"). The API names the word but not which of a label, a reaction or a miss set it, so a label that matches the
 * word is named first. */
export function whoseFit(t: PaperTopic): string | null {
  if (!t.word) return t.fit ? `pipeline: ${t.fit}` : null;
  if (t.label && t.label.value !== "unsure" && t.label.value === t.word) return `your label: ${t.word}`;
  if (t.reaction && speaks(t.topic.depth, t.reaction.reason)) return `your mark: ${t.word}`;
  if (t.missed) return `reported as missed: ${t.word}`;
  return `your word: ${t.word}`;
}

/** The topic states that ask for the owner's word, by the name they show under: a Focus split, an Area escalation,
 * a central or related grade from one lab. */
export const ASKING = { split: "Split", escalated: "Escalated", one_lab: "One lab" } as const;
export const asks = (t: PaperTopic): t is PaperTopic & { state: keyof typeof ASKING } => t.state != null && t.state in ASKING;

/** What the graders said, by role; for a grade from one lab, first the side that refused it throughout. */
export function graders(v: PaperTopic["verdict"]): string[] {
  if (!v) return [];
  const said = Object.entries(v.opinions ?? {})
    .filter(([, o]) => o.fit)
    .map(([role, o]) => `${role} ${o.fit}`);
  return v.one_lab ? [`${v.opinions?.judge ? "check" : "judge"} refused`, ...said] : said;
}

export function verdictLine(t: PaperTopic, weight: Weight | null | undefined): React.ReactNode {
  if (asks(t)) {
    return (
      <>
        <b className="askt">{ASKING[t.state]}</b> · {graders(t.verdict).join(" · ")} · {t.fit} stands
      </>
    );
  }
  if (t.state === "ungraded") return <b className="badt">Grading stopped</b>;
  if (t.state === "grading_again") return "Grading again";
  if (t.word) return <b>{whoseFit(t)}</b>;
  if (!t.fit) return "not graded here";
  return (
    <>
      Fit {t.fit}
      {weight ? ` · Weight ${weight}` : ""}
    </>
  );
}

/** The two thumbs; the lit one shows its reason. */
function Thumbs({ reaction, pick, setPick }: { reaction: PaperTopic["reaction"] | null | undefined; pick: ReactionValue | null; setPick: (v: ReactionValue | null) => void }) {
  const thumb = (value: ReactionValue) => {
    const on = reaction?.value === value;
    return (
      <button className={`th${on ? " on" : ""}`} onClick={() => setPick(pick === value ? null : value)} aria-label={value === "up" ? "Thumbs up" : "Thumbs down"} aria-pressed={on}>
        {value === "up" ? "👍" : "👎"}
        {on && reaction?.reason && <span>{reasonLabel(reaction.reason)}</span>}
      </button>
    );
  };
  return (
    <>
      {thumb("up")}
      {thumb("down")}
    </>
  );
}

/** The rail's row for a collection paper under no followed topic: the thumbs for the paper itself (feedback.md,
 * "Reactions": a thumb for the topic of the surface, or none). */
export function PaperRow({ paperId, reaction }: { paperId: string; reaction: PaperTopic["reaction"] | null | undefined }) {
  const [pick, setPick] = useState<ReactionValue | null>(null);
  return (
    <>
      <div className="tprow" style={{ "--rc": "var(--fg3)" } as React.CSSProperties}>
        <span className="nm">This paper</span>
        <span className="vd">Under no followed topic</span>
        <Thumbs reaction={reaction} pick={pick} setPick={setPick} />
      </div>
      {pick && <Reasons paperId={paperId} topic={null} value={pick} current={reaction} onDone={() => setPick(null)} />}
    </>
  );
}

/** A topic row's colour: its rarity's, grey while its grading failed or runs again. */
export const rowColour = (t: PaperTopic): string => (t.state === "ungraded" || t.state === "grading_again" ? "var(--fg3)" : rc(t.rarity));
/** The colour its grade is written in: the row's, red once grading failed. */
export const gradeColour = (t: PaperTopic): string => (t.state === "ungraded" ? "var(--sig)" : rowColour(t));

export function TopicRow({ paperId, t, weight, revision }: { paperId: string; t: PaperTopic; weight: Weight | null | undefined; revision: number }) {
  const [pick, setPick] = useState<ReactionValue | null>(null);
  const [labelOpen, setLabelOpen] = useState(false);
  const gradeNow = useGradeNow(paperId);
  const asking = asks(t);
  const failed = t.state === "ungraded";
  return (
    <>
      <div className={`tprow${asking ? " ask" : ""}`} data-topic={t.topic.slug} style={{ "--rc": rowColour(t) } as React.CSSProperties}>
        <Badge topic={t.topic} />
        <span className="nm">{t.topic.name}</span>
        <span className={`gr${failed ? " fail" : ""}`}>{failed || t.state === "grading_again" ? "ungraded" : gradeLine(t.grade, weight)}</span>
        <span className="vd">
          {verdictLine(t, weight)}
          {failed && (
            <>
              {" · "}
              <button className="inl" onClick={() => gradeNow.mutate({ topic: t.topic.slug, revision })} disabled={gradeNow.isPending}>
                {gradeNow.isPending ? "Grading…" : "Grade it now"}
              </button>
            </>
          )}
        </span>
        <Thumbs reaction={t.reaction} pick={pick} setPick={setPick} />
        <button className="lb" onClick={() => setLabelOpen((o) => !o)} title="Label the Fit" aria-expanded={labelOpen || asking}>
          Label
        </button>
      </div>
      {pick && <Reasons paperId={paperId} topic={t.topic.slug} value={pick} current={t.reaction} onDone={() => setPick(null)} />}
      {(labelOpen || asking) && (
        <div className={`why${asking ? " ask" : ""}`}>
          <FitControl paperId={paperId} t={t} revision={revision} />
        </div>
      )}
    </>
  );
}
