/** A paper's Deep read on its page: the read's components (`@/deepread`) given the app's figures, links and topic
 * colours, and closed by the card's foot (what it was written from, Write again, a new one's status). */
import { Link } from "@tanstack/react-router";
import { useMemo } from "react";
import { DeepRead } from "@/deepread/DeepRead";
import type { ReadEnv } from "@/deepread/env";
import { isRead, type Read } from "@/deepread/types";
import type { Paper } from "./Actions";
import { CardFoot } from "./DeepCard";

/** The paper's Deep read, when its card is one (`format: "read"`); a Deep card from before is not. */
export const readOf = (paper: Paper): Read | null => (paper.card && isRead(paper.card.card) ? paper.card.card : null);

/** A topic's colour by its slug, from the paper's own topic rows: a lens takes its topic's colour. */
export const topicColourOf = (paper: Paper) => (slug: string) => paper.topics.find((t) => t.topic.slug === slug)?.topic.colour;

const PaperLink: ReadEnv["PaperLink"] = ({ id, className, style, children }) => (
  <Link to="/paper/$" params={{ _splat: id }} className={className} style={style}>
    {children}
  </Link>
);

export function PaperRead({ paper, read, asked }: { paper: Paper; read: Read; asked: boolean }) {
  // a read written again has other figures under the same names: its time keeps the browser from showing the old ones
  const written = paper.card?.written_at ?? "";
  const env = useMemo<ReadEnv>(
    () => ({
      figureSrc: (name) => `/api/papers/${paper.id}/deep/figures/${encodeURIComponent(name)}${written ? `?v=${encodeURIComponent(written)}` : ""}`,
      topicColour: topicColourOf(paper),
      PaperLink,
    }),
    [paper, written],
  );
  return (
    // keyed by the read, so one written again starts with its claims and notes folded
    <DeepRead key={`${paper.id}:${written}`} read={read} among={paper.card?.among ?? []} env={env}>
      <CardFoot paper={paper} asked={asked} />
    </DeepRead>
  );
}
