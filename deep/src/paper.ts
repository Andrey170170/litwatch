/**
 * The paper's text as the writer's first message carries it (docs/plans/deep-read.md, D4): the main text, with the
 * bibliography and the appendix cut out wherever they sit. Both stay in the session's `paper`, where the writer
 * reads them on demand (`section()`, `find()`, `refs()`).
 */

export const MAX_BODY_CHARS_IN_CONTEXT = 200_000;

export type Part = "references" | "appendix";

/** A stretch of the paper's text left out of the first message. */
export interface Cut {
  part: Part;
  start: number;
  end: number;
}

export interface Split {
  /** The main text: the paper's text without the cuts, in order. */
  main: string;
  /** What was left out, in the order it stands in the text; no two overlap. */
  cuts: Cut[];
}

/** Whether the text is a LaTeX source: litwatch says so, or it reads like one. */
export const isLatex = (body: string, source?: string | null): boolean =>
  source === "latex" || (source !== "pdf" && /\\begin\{document\}|\\section\*?\s*\{/.test(body));

/** Whether the match at `at` sits on a LaTeX comment line: an unescaped % before it. */
function commented(body: string, at: number): boolean {
  const line = body.slice(body.lastIndexOf("\n", at - 1) + 1, at);
  return /(^|[^\\])(\\\\)*%/.test(line);
}

/** The first match of `pattern` (a global one) that is not commented out. */
function first(body: string, pattern: RegExp): RegExpExecArray | null {
  pattern.lastIndex = 0;
  for (let m = pattern.exec(body); m; m = pattern.exec(body)) if (!commented(body, m.index)) return m;
  return null;
}

function latexCuts(body: string): Cut[] {
  const cuts: Cut[] = [];
  // the archive inlines the bibliography as a thebibliography environment, often before the appendix
  const open = first(body, /\\begin\{thebibliography\}/g);
  if (open) {
    const close = body.indexOf("\\end{thebibliography}", open.index);
    cuts.push({ part: "references", start: open.index, end: close === -1 ? body.length : close + "\\end{thebibliography}".length });
  }
  const appendix = first(body, /\\appendix(?![a-zA-Z])|\\begin\{appendi(?:x|ces)\}|\\section\*?\s*(?:\[[^\]]*\])?\s*\{\s*(?:Appendi(?:x|ces)|Supplementa(?:ry|l)\b)/g);
  if (appendix) cuts.push({ part: "appendix", start: appendix.index, end: body.length });
  return cuts;
}

function markdownCuts(body: string): Cut[] {
  const headings = [...body.matchAll(/^(#{1,6})[ \t]+(.*)$/gm)].map((m) => ({ level: m[1]!.length, title: m[2]!.trim(), start: m.index }));
  const cuts: Cut[] = [];
  const refs = headings.findIndex((h) => /^(?:\d+\.?\s+)?(?:references|bibliography)\s*$/i.test(h.title));
  if (refs !== -1) {
    const next = headings.slice(refs + 1).find((h) => h.level <= headings[refs]!.level);
    cuts.push({ part: "references", start: headings[refs]!.start, end: next?.start ?? body.length });
  }
  const appendix = headings.find((h) => /^(?:[A-Z]\.?\s+)?(?:appendi(?:x|ces)|supplementa(?:ry|l)\b)/i.test(h.title));
  if (appendix) cuts.push({ part: "appendix", start: appendix.start, end: body.length });
  return cuts;
}

/** The paper's main text and what was cut from it: the bibliography wherever it sits, and everything from the
 * appendix's start (LaTeX's \appendix, or an Appendix heading) to the end. A paper with neither is all main text. */
export function splitPaper(body: string, source?: string | null): Split {
  const found = (isLatex(body, source) ? latexCuts(body) : markdownCuts(body)).sort((a, b) => a.start - b.start);
  // a bibliography inside the appendix is part of it; one the appendix starts inside ends where the appendix starts
  const cuts: Cut[] = [];
  for (const cut of found) {
    const last = cuts.at(-1);
    if (last && cut.start < last.end) {
      if (cut.end > last.end) cuts.push({ ...cut, start: last.end });
    } else cuts.push({ ...cut });
  }
  let main = "";
  let at = 0;
  for (const cut of cuts) {
    main += body.slice(at, cut.start);
    at = cut.end;
  }
  return { main: main + body.slice(at), cuts: cuts.filter((c) => c.end > c.start) };
}

/** One heading of the paper, as the session's `sections()` gives it. */
export interface Heading {
  level: number;
  title: string;
  start: number;
  chars: number;
}

/** The paper's outline for the first message: each heading with its size, and which part it is in when that part
 * was left out of the message. */
export function outline(headings: Heading[], cuts: Cut[]): string {
  if (headings.length === 0) return "(no headings found; read the text with find())";
  return headings
    .map((h) => {
      const part = cuts.find((c) => h.start >= c.start && h.start < c.end)?.part;
      return `${"  ".repeat(Math.max(0, h.level - 1))}- ${h.title} (${h.chars} characters${part ? `, ${part === "appendix" ? "appendix" : "bibliography"}: in the session only` : ""})`;
    })
    .join("\n");
}
