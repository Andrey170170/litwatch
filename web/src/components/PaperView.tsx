import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import type { Chosen, Paper, PaperTopic } from "@/api/client";
import { paperQuery } from "@/api/queries";
import { LabelRow } from "@/components/LabelRow";
import { Thumbs } from "@/components/Thumbs";
import { BibtexCopy, GRADE_WORD, SaveToggle, TopicChip } from "@/components/paper-bits";
import { fmt, plural } from "@/lib/format";
import { topicColour } from "@/lib/topics";
import { cn } from "@/lib/utils";

type Props = {
  id: string;
  /**
   * The topic of the surface the paper was opened from, which the thumbs speak for: a slug,
   * "none" for a plain search result, or absent to take the first Watch topic shown.
   */
  surfaceTopic?: string;
  /** In the reading pane: a smaller title. */
  compact?: boolean;
};

/** Everything about one paper, in reading order (DESIGN.md "Paper"). */
export function PaperView({ id, surfaceTopic, compact }: Props) {
  const q = useQuery(paperQuery(id));
  if (q.isPending) return <p className="meta">Opening…</p>;
  if (q.isError || !q.data) return <p className="text-destructive">This paper could not be loaded.</p>;
  const p = q.data;
  const watch = p.topics.filter((t) => t.kind === "watch" && t.grade && t.grade !== "none");
  const shown = p.topics.filter((t) => (t.kind === "watch" ? t.grade === "must_read" || t.grade === "relevant" : t.member));
  const thumbsTopic = surfaceTopic === "none" ? null : (surfaceTopic ?? shown.find((t) => t.kind === "watch")?.slug ?? null);
  const reactionOn = thumbsTopic ? p.topics.find((t) => t.slug === thumbsTopic) : null;

  return (
    <article>
      <h1 className={cn("font-serif leading-[1.22] font-semibold tracking-[-.01em]", compact ? "text-[22px]" : "text-[22px] desk:text-[26px]")}>
        {p.title}
      </h1>
      {shown.length > 0 && (
        <div className="mt-2 flex flex-wrap gap-1.5">
          {shown.map((t) => (
            <TopicChip key={t.slug} slug={t.slug} name={t.name} colour={t.colour} />
          ))}
        </div>
      )}
      <p className="meta mt-2 text-[13.5px]">
        {p.authors.join(", ")} · arXiv {p.id} · {fmt.full(p.published)}
        {p.version ? ` · v${p.version}` : ""}
      </p>

      <div className="mt-3.5 flex flex-wrap gap-2">
        <SaveToggle paperId={p.id} saved={!!p.saved} as="button" />
        <a href={p.arxiv_url} target="_blank" rel="noreferrer" className="inline-flex min-h-[40px] items-center rounded-full border border-border px-4 text-[13.5px] hover:bg-muted">
          arXiv
        </a>
        <a href={p.pdf_url} target="_blank" rel="noreferrer" className="inline-flex min-h-[40px] items-center rounded-full border border-border px-4 text-[13.5px] hover:bg-muted">
          PDF
        </a>
        <BibtexCopy ids={[p.id]} as="button" />
      </div>

      {watch.filter((t) => t.take).map((t) => (
        <div key={t.slug} className="mt-5">
          <div className="mb-1.5">
            <TopicChip slug={t.slug} name={t.name} colour={t.colour} />
          </div>
          <p className="font-serif text-[15.5px] leading-[1.5] text-body desk:text-[16.5px]">{t.take}</p>
        </div>
      ))}

      <H3>Abstract</H3>
      <p className="text-[15px] text-body">{p.abstract}</p>

      <H3>Published version</H3>
      {p.doi || p.journal_ref ? (
        <p className="text-[15px] text-body">
          {p.journal_ref}
          {p.doi && (
            <>
              {p.journal_ref ? " · " : ""}
              <a href={`https://doi.org/${p.doi}`} target="_blank" rel="noreferrer" className="underline decoration-border">
                doi:{p.doi}
              </a>
            </>
          )}
        </p>
      ) : (
        <p className="text-[15px] text-muted-foreground">Not yet found.</p>
      )}

      <H3>History</H3>
      <p className="text-[13.5px] text-muted-foreground">{history(p)}</p>

      <HowChosen paper={p} />

      <div className="mt-4 flex items-center gap-2 border-t border-border pt-3">
        <Thumbs key={`${p.id}:${thumbsTopic ?? ""}`} paperId={p.id} topic={thumbsTopic} value={reactionOn ? reactionOn.reaction : p.reaction} reason={reactionOn ? reactionOn.reaction_reason : p.reaction_reason} />
        <span className="meta">{thumbsTopic ? `for ${p.topics.find((t) => t.slug === thumbsTopic)?.name}` : ""}</span>
      </div>
    </article>
  );
}

function H3({ children }: { children: string }) {
  return <h3 className="mt-[22px] mb-1.5 text-[12.5px] font-semibold tracking-[.02em] text-muted-foreground">{children}</h3>;
}

function history(p: Paper): string {
  const names = Object.fromEntries(p.topics.map((t) => [t.slug, t.name]));
  const parts = [`v${p.version ?? 1} seen ${fmt.published(p.added)}`];
  for (const h of p.history) {
    const topic = names[h.topic] ?? h.topic;
    if (h.what === "candidate") parts.push(`candidate for ${topic} ${fmt.published(h.at)}`);
    else if (h.what === "graded") parts.push(`graded ${fmt.published(h.at)} for ${topic}`);
    else parts.push(`reported missed for ${topic} ${fmt.published(h.at)}`);
  }
  const never = p.topics.filter((t) => t.kind === "watch" && !p.history.some((h) => h.topic === t.slug));
  if (never.length) parts.push(`not a candidate for ${never.map((t) => t.name).join(", ")}`);
  return parts.join("; ") + ".";
}

/** The one quiet link that unfolds the kitchen: triage, both opinions by model, the grade, the label row. */
function HowChosen({ paper }: { paper: Paper }) {
  const [open, setOpen] = useState(false);
  const triage = (paper.triage ?? {}) as Record<string, { yes?: boolean; why?: string | null }>;
  const names = Object.fromEntries(paper.topics.map((t) => [t.slug, t]));
  const triaged = Object.entries(triage).filter(([, a]) => a?.yes || a?.why);
  // A label with no verdict (a miss, a Calibrate answer) still needs its row to be changed.
  const chosenTopics = new Set(paper.chosen.map((c) => c.topic));
  const labelOnly = paper.topics.filter((t) => t.kind === "watch" && t.label && !chosenTopics.has(t.slug));
  if (paper.chosen.length === 0 && triaged.length === 0 && labelOnly.length === 0) return null;
  return (
    <div className="mt-[26px] border-t border-border pt-3 text-[13px] text-muted-foreground">
      <button type="button" className="min-h-[40px] underline decoration-border hover:text-foreground" onClick={() => setOpen((o) => !o)} aria-expanded={open}>
        How this was chosen
      </button>
      {open && (
        <div className="mt-2 space-y-4 text-[13.5px] text-body">
          {triaged.length > 0 && (
            <div>
              <small className="block text-muted-foreground">Triage</small>
              {triaged.map(([slug, a]) => (
                <p key={slug}>
                  {a.yes ? "candidate" : "not a candidate"} for {names[slug]?.name ?? slug}
                  {a.why ? `: “${a.why}”` : ""}
                </p>
              ))}
            </div>
          )}
          {paper.chosen.map((c) => (
            <Verdict key={c.topic} chosen={c} topic={names[c.topic]} paperId={paper.id} />
          ))}
          {labelOnly.map((t) => (
            <div key={t.slug}>
              <div className="font-semibold" style={{ color: topicColour(t.colour) }}>
                {t.name}
              </div>
              <p className="text-muted-foreground">Not graded; your word stands.</p>
              <LabelRow paperId={paper.id} topic={t.slug} current={t.label?.value} />
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function Verdict({ chosen, topic, paperId }: { chosen: Chosen; topic: PaperTopic | undefined; paperId: string }) {
  const colour = topicColour(topic?.colour);
  return (
    <div className="grid gap-x-3.5 gap-y-2 desk:grid-cols-2">
      <div className="col-span-full font-semibold" style={{ color: colour }}>
        {topic?.name ?? chosen.topic}
      </div>
      <div>
        <small className="block text-muted-foreground">Reconciled</small>
        <b>{GRADE_WORD[chosen.grade]}</b>
        {chosen.split ? ", from a disagreement; shown at the more inclusive grade." : chosen.migrated ? ", carried over." : "."}
      </div>
      {chosen.judge && <OpinionView role="judge" grade={chosen.judge.grade} model={chosen.judge.model} reason={chosen.judge.reason} />}
      {chosen.check && <OpinionView role="check" grade={chosen.check.grade} model={chosen.check.model} reason={chosen.check.reason} />}
      <div className="col-span-full">
        <LabelRow paperId={paperId} topic={chosen.topic} current={topic?.label?.value} />
      </div>
    </div>
  );
}

function OpinionView({ role, grade, model, reason }: { role: string; grade: Chosen["grade"]; model: string | null | undefined; reason: string }) {
  return (
    <div>
      <small className="block text-muted-foreground">
        {modelName(model)}, {role}
      </small>
      <b>{GRADE_WORD[grade]}.</b> {reason}
    </div>
  );
}

/** "anthropic:claude-opus-5-5@medium" → "claude-opus-5-5 (medium)". */
export function modelName(model: string | null | undefined): string {
  if (!model) return "unknown model";
  const [, rest = model] = model.split(":", 2).length === 2 ? model.split(":", 2) : [null, model];
  const [name, effort] = rest.split("@");
  return effort ? `${name} (${effort})` : (name ?? model);
}

export { plural };
