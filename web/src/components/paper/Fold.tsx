/**
 * The paper's topic rows in grade order, folded after two (DESIGN.md, "The rail"). The order is taken once, when the
 * page opens, so a label that changes a grade does not move its row; the fold starts closed on every paper.
 */
import { useState, type ReactNode } from "react";
import { Badge } from "@/components/Badge";
import { GRADE_NAME, type Grade } from "@/lib/rarity";
import { gradeColour, type PaperTopic } from "./TopicRow";

type Rank = Grade | "ungraded";
const RANKS: Rank[] = ["must_read", "relevant", "adjacent", "none", "ungraded"];
const RANK_NAME: Record<Rank, string> = { ...GRADE_NAME, ungraded: "ungraded" };

/** The grade a row shows: a topic whose grading failed or is running again shows "ungraded", whatever it had. */
export const rankOf = (t: PaperTopic): Rank => (t.state === "ungraded" || t.state === "grading_again" || !t.grade ? "ungraded" : t.grade);

/** Must read first, then relevant, adjacent, none, ungraded; ties keep the API's order. */
export const byGrade = (topics: PaperTopic[]): PaperTopic[] => topics.map((t, i) => [t, i] as const).sort(([a, i], [b, j]) => RANKS.indexOf(rankOf(a)) - RANKS.indexOf(rankOf(b)) || i - j).map(([t]) => t);

/** The topics in a settled order (slugs); a topic the order lacks goes after, in grade order. */
export function arrange(topics: PaperTopic[], order: string[]): PaperTopic[] {
  const at = (t: PaperTopic) => order.indexOf(t.topic.slug);
  return [...topics.filter((t) => at(t) >= 0).sort((a, b) => at(a) - at(b)), ...byGrade(topics.filter((t) => at(t) < 0))];
}

/** The page's topics in the grade order they had when it opened; a topic that arrives later keeps the place it
 * first took. Call it from a component keyed by the paper. */
export function useSettledTopics(topics: PaperTopic[] | undefined): PaperTopic[] {
  const [order, setOrder] = useState<string[]>([]);
  if (!topics) return [];
  const shown = arrange(topics, order);
  if (shown.some((t) => !order.includes(t.topic.slug))) setOrder([...order, ...shown.map((t) => t.topic.slug).filter((s) => !order.includes(s))]);
  return shown;
}

/** The rows a fold hides: all after the first two, and only when there are more than two. */
export const folded = (topics: PaperTopic[]): PaperTopic[] => (topics.length > 2 ? topics.slice(2) : []);

/** "+4" and the hidden rows by grade, each in the colour its rows' grade is written in. */
export function foldCount(hidden: PaperTopic[]): { total: number; grades: { rank: Rank; name: string; n: number; colour: string }[] } {
  const grades = RANKS.flatMap((rank) => {
    const rows = hidden.filter((t) => rankOf(t) === rank);
    const first = rows[0];
    return first ? [{ rank, name: RANK_NAME[rank], n: rows.length, colour: gradeColour(first) }] : [];
  });
  return { total: hidden.length, grades };
}

/** The page's topic rows, in the order they had when it opened, and whether their fold is open. */
export type Topics = { topics: PaperTopic[]; open: boolean; setOpen: (open: boolean) => void };

/** The first two rows, the fold line, and the rest once it is open. */
export function FoldedTopics({ topics, open, setOpen, row }: Topics & { row: (t: PaperTopic) => ReactNode }) {
  const hidden = folded(topics);
  const { total, grades } = foldCount(hidden);
  // the first rows keep their place in the tree as the fold comes and goes, so their open controls stay open
  return (
    <>
      {topics.slice(0, 2).map(row)}
      {total > 0 && (
        <button className="tpfold" onClick={() => setOpen(!open)} aria-expanded={open}>
          <span className={`car${open ? " open" : ""}`} aria-hidden />
          <span className="bs">
            {hidden.map((t) => (
              <Badge key={t.topic.slug} topic={t.topic} size="sm" />
            ))}
          </span>
          <span className="ct">
            +{total} ·{" "}
            {grades.map((g, i) => (
              <span key={g.rank}>
                {i > 0 && ", "}
                <b style={{ color: g.colour }}>
                  {g.n} {g.name}
                </b>
              </span>
            ))}
          </span>
        </button>
      )}
      {open && hidden.map(row)}
    </>
  );
}
