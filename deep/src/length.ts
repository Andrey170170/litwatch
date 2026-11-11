/** A read's length against its paper's: measured in code, since a reviewer judging it by eye is not consistent. The
 * read aims for about a third of the paper's main text; it fails its length check only past LENGTH_MAX of it. */
import { headings, isLatex, splitPaper } from "./paper.ts";
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
  /** The paper's main text, without the appendix or bibliography, and in LaTeX from its first heading. */
  main: number;
  share: number;
}

export function measure(read: unknown, paper: Pick<PaperInput, "body" | "body_source">): Length {
  const { main, cuts } = splitPaper(paper.body, paper.body_source);
  // what comes before a LaTeX source's first heading is its preamble and the title, which no read retells, unless
  // that heading is where the cuts begin (a note with no sections); a PDF's text has no preamble, and its headings are
  // found loosely enough (a table's "# Failed" column) to trim the paper
  const first = isLatex(paper.body, paper.body_source) ? headings(paper.body, paper.body_source)[0]?.start : undefined;
  const start = first !== undefined && first < (cuts[0]?.start ?? Infinity) ? first : 0;
  const length = Math.max(1, main.length - start);
  const text = chars(read);
  return { read: text, main: length, share: text / length };
}

/** A share as a percentage. */
export const pct = (x: number): string => `${Math.round(x * 100)}%`;

/** The measure as the writer and the reviewer are told it. Only the reviewer is told where the check fails: a writer
 * told the limit wrote up to it, three of five reads at 58% against 32–46% without it. */
export function lengthLine(m: Length, limit = false): string {
  const line = `Length: the read's text is ${m.read} characters, ${pct(m.share)} of the paper's main text (${m.main}); the aim is about ${pct(LENGTH_AIM)}`;
  return limit ? `${line}, and past ${pct(LENGTH_MAX)} the length check fails.` : `${line}.`;
}
