/** A read's length against its paper's: measured in code, since a reviewer judging it by eye is not consistent. The
 * read aims for about a third of the paper's main text; it fails its length check only past LENGTH_MAX of it. */
import { headings, splitPaper } from "./paper.ts";
import type { PaperInput } from "./prompt.ts";

/** What a read aims for, as a share of its paper's main text. */
export const LENGTH_AIM = 1 / 3;
/** Past this share a read fails its length check. Five real reads at 0.32 to 0.46 by this measure were the right
 * length to the owner's eye (evals/2026-11-10-deep-reads.md). */
export const LENGTH_MAX = 0.6;

/** Fields that name or point rather than say something a reader reads. */
const NOT_TEXT = new Set(["id", "type", "kind", "where", "name", "topic", "from", "to", "number", "source", "format", "style", "colour", "state", "anchor", "url", "code_url", "arxiv_id", "tags", "label_at", "verdict"]);

function chars(value: unknown, key?: string): number {
  if (key !== undefined && NOT_TEXT.has(key)) return 0;
  if (typeof value === "string") return value.length;
  if (Array.isArray(value)) return value.reduce((n: number, v) => n + chars(v), 0);
  if (value && typeof value === "object") return Object.entries(value).reduce((n, [k, v]) => n + chars(v, k), 0);
  return 0;
}

export interface Length {
  /** The read's characters of text. */
  read: number;
  /** The paper's main text, from its first heading, without the appendix or bibliography. */
  main: number;
  share: number;
}

export function measure(read: unknown, paper: Pick<PaperInput, "body" | "body_source">): Length {
  const { main } = splitPaper(paper.body, paper.body_source);
  // what comes before the first heading is a LaTeX source's preamble and the title, which no read retells
  const start = headings(paper.body, paper.body_source)[0]?.start ?? 0;
  const length = Math.max(1, main.length - start);
  const text = chars(read);
  return { read: text, main: length, share: text / length };
}

/** A share as a percentage. */
export const pct = (x: number): string => `${Math.round(x * 100)}%`;

/** The measure as the writer and the reviewer are told it. */
export function lengthLine(m: Length): string {
  return `Length: the read's text is ${m.read} characters, ${pct(m.share)} of the paper's main text (${m.main}); the aim is about ${pct(LENGTH_AIM)}, and past ${pct(LENGTH_MAX)} the length check fails.`;
}
