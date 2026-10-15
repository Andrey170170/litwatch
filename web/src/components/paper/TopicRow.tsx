/**
 * One row per topic the paper is in (DESIGN.md, "The rail"): badge, name, grade, the verdict or the owner's word,
 * the two thumbs, and Label, which opens the Fit control. A split or escalation keeps the control open until
 * labelled; an ungraded topic offers to grade it now.
 */
import { useState } from "react";
import type { Schemas } from "@/api/client";
import { useGradeNow, useLabel, useReact, useUnlabel, useUnreact } from "@/api/mutations";
import { Badge } from "@/components/Badge";
import { gradeLine, rc, type Weight } from "@/lib/rarity";
import { REASONS, reasonLabel, type ReactionValue } from "@/lib/reactions";

export type PaperTopic = Schemas["PaperTopic"];
type LabelValue = Schemas["LabelValue"];
const FITS: LabelValue[] = ["central", "related", "adjacent", "none", "unsure"];

/** The Fit control: five equal segments; the owner's label filled, the verdict's underlined while unlabelled.
 * Tapping the filled segment removes the label. */
export function FitControl({ paperId, t }: { paperId: string; t: PaperTopic }) {
  const label = useLabel(paperId);
  const unlabel = useUnlabel(paperId);
  const on = t.label?.value;
  const auto = !on ? t.verdict?.fit : null;
  return (
    <div className="fitseg" role="group" aria-label={`Fit for ${t.topic.name}`}>
      {FITS.map((f) => (
        <button
          key={f}
          className={`seg${on === f ? " on" : ""}${auto === f ? " auto" : ""}`}
          aria-pressed={on === f}
          disabled={label.isPending || unlabel.isPending}
          onClick={() => (on === f ? unlabel.mutate(t.topic.slug) : label.mutate({ topic: t.topic.slug, value: f }))}
        >
          {f}
        </button>
      ))}
    </div>
  );
}

/** The reasons for a thumb as chips, and Clear when the thumb is lit; nothing is recorded until a reason is picked. */
export function Reasons({ paperId, topic, value, current, onDone, big }: { paperId: string; topic: string | null; value: ReactionValue; current: PaperTopic["reaction"] | null | undefined; onDone: () => void; big?: boolean }) {
  const react = useReact(paperId);
  const unreact = useUnreact(paperId);
  return (
    <div className={`why${big ? " big" : ""}`} role="group" aria-label="Reason">
      {REASONS[value].map((r) => (
        <button
          key={r.reason}
          className={current?.value === value && current.reason === r.reason ? "on" : ""}
          onClick={() => react.mutate({ topic, value, reason: r.reason }, { onSuccess: onDone })}
          disabled={react.isPending}
        >
          {r.label}
        </button>
      ))}
      {current && (
        <button className="clr" onClick={() => unreact.mutate(topic, { onSuccess: onDone })} disabled={unreact.isPending}>
          Clear
        </button>
      )}
    </div>
  );
}

/** "Fit central · Weight landmark", "your label: related", or the open state's line. */
export function verdictLine(t: PaperTopic, weight: Weight | null | undefined): React.ReactNode {
  if (t.state === "split" || t.state === "escalated") {
    const v = t.verdict;
    const said = v ? Object.entries(v.opinions ?? {}).filter(([, o]) => o.fit).map(([role, o]) => `${role} ${o.fit}`) : [];
    return (
      <>
        <b className="askt">{t.state === "split" ? "Split" : "Escalated"}</b> · {said.join(" · ")} · {t.fit} stands
      </>
    );
  }
  if (t.state === "ungraded") return <b className="badt">Grading failed 3 runs</b>;
  if (t.state === "grading_again") return "Grading again";
  if (t.label && t.label.value !== "unsure") return <b>your label: {t.label.value}</b>;
  if (t.missed && t.word) return <b>reported as missed · {t.word}</b>;
  if (t.word && t.reaction) return <b>your mark: {t.word}</b>;
  if (!t.fit) return "not graded here";
  return (
    <>
      Fit {t.fit}
      {weight ? ` · Weight ${weight}` : ""}
    </>
  );
}

export function TopicRow({ paperId, t, weight, revision }: { paperId: string; t: PaperTopic; weight: Weight | null | undefined; revision: number }) {
  const [pick, setPick] = useState<ReactionValue | null>(null);
  const [labelOpen, setLabelOpen] = useState(false);
  const gradeNow = useGradeNow(paperId);
  const asks = t.state === "split" || t.state === "escalated";
  const failed = t.state === "ungraded";
  const colour = failed || t.state === "grading_again" ? "var(--fg3)" : rc(t.rarity);
  const thumb = (value: ReactionValue) => {
    const on = t.reaction?.value === value;
    return (
      <button className={`th${on ? " on" : ""}`} onClick={() => setPick(pick === value ? null : value)} aria-label={value === "up" ? "Thumbs up" : "Thumbs down"} aria-pressed={on}>
        {value === "up" ? "👍" : "👎"}
        {on && t.reaction?.reason && <span>{reasonLabel(t.reaction.reason)}</span>}
      </button>
    );
  };
  return (
    <>
      <div className={`tprow${asks ? " ask" : ""}`} data-topic={t.topic.slug} style={{ "--rc": colour } as React.CSSProperties}>
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
        {thumb("up")}
        {thumb("down")}
        <button className="lb" onClick={() => setLabelOpen((o) => !o)} title="Label the Fit" aria-expanded={labelOpen || asks}>
          Label
        </button>
      </div>
      {pick && <Reasons paperId={paperId} topic={t.topic.slug} value={pick} current={t.reaction} onDone={() => setPick(null)} />}
      {(labelOpen || asks) && (
        <div className={`why${asks ? " ask" : ""}`}>
          <FitControl paperId={paperId} t={t} />
        </div>
      )}
    </>
  );
}
