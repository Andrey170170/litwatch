/**
 * The places a paper has, as the checks look them up (`checks.ts`): its figures and tables, its numbered sections
 * and the titles of its headings, and its numbers. One index is built per run from the paper's text, and one
 * reading of a label ("3", "A.1", "S2") serves every lookup: a cite in the read, a `fig` block's number, the number
 * a figure is made under.
 *
 * What an index knows depends on the text. A LaTeX source shows its floats and its section commands, so their
 * numbers are counted; a PDF's text shows only what it says, so a place is there when the text names it. Where
 * neither can tell (an appendix figure of a LaTeX source, whose number the source does not carry) the answer is
 * "unknown", and the cite is left to the reviewer.
 */

import { PaperNumbers } from "./numbers.ts";
import { headings, isLatex, splitPaper } from "./paper.ts";

export type Kind = "figure" | "table";
/** Whether the paper has a place: it does, it does not (and why), or the text cannot tell. */
export type Answer = "there" | "unknown" | { no: string };

/** LaTeX without its comments: an unescaped % to the end of its line. */
const uncommented = (tex: string) => tex.replace(/((?:^|[^\\])(?:\\\\)*)%.*$/gm, "$1");

// ── labels ──────────────────────────────────────────────────────────────────────────────────────────

/** A figure's, table's or section's label: a number, with an appendix's letter or a section's parts before it. */
const LABEL = "(?:[A-Z]\\.?)?\\d+(?:\\.\\d+)*[a-z]?";
const JOIN = "\\s*(?:,|and|&|–|—|-|to)\\s*";

/** The label as it is compared: without a sub-figure's letter ("3a" is 3) and written one way ("A.1" is A1). */
export const labelKey = (label: string): string => label.replace(/[a-z]$/, "").replace(/^([A-Z])\./, "$1");

/** The label a figure's number carries: "Figure A.1", "Fig. 3 (left)" and "3" carry A.1, 3 and 3. */
export function figureLabel(number: string): string | undefined {
  return new RegExp(`(?<![A-Za-z0-9.])${LABEL}(?![A-Za-z0-9])`).exec(number)?.[0].replace(/[a-z]$/, "");
}

export interface Cite {
  kind: Kind;
  label: string;
  index: number;
  end: number;
}

const CITE = new RegExp(`\\b(Figures?|Figs?\\.?|Tables?|Tabs?\\.?)(?:\\s|~)*(${LABEL}(?:${JOIN}${LABEL})*)`, "g");
/** The figures and tables a text cites: a plural cites each label of its list, a singular its first only. */
export function cites(text: string): Cite[] {
  const out: Cite[] = [];
  for (const m of text.matchAll(CITE)) {
    const kind: Kind = /^f/i.test(m[1]!) ? "figure" : "table";
    const plural = /^(Figures|Figs\.?|Tables|Tabs\.?)$/i.test(m[1]!);
    const labels = m[2]!.split(new RegExp(JOIN));
    const taken = plural ? labels : labels.slice(0, 1);
    const end = plural ? m.index + m[0].length : m.index + m[0].length - (m[2]!.length - labels[0]!.length);
    for (const label of taken) out.push({ kind, label, index: m.index, end });
  }
  return out;
}

const SECTION_LABEL = "(?:[A-Z](?:\\.?\\d+(?:\\.\\d+)*)?(?![A-Za-z])|\\d+(?:\\.\\d+)*)";
const SECTION = new RegExp(`(§+|\\b(?:[Ss]ections?|[Ss]ecs?\\.?|[Aa]ppendi(?:x|ces)|[Aa]pps?\\.))\\s*(${SECTION_LABEL}(?:${JOIN}§*\\s*${SECTION_LABEL})*)?`, "g");

/** The sections and appendices a location names, each by its label; a bare "Appendix" has none. */
export function sectionCites(text: string): { appendix: boolean; label?: string }[] {
  const out: { appendix: boolean; label?: string }[] = [];
  for (const m of text.matchAll(SECTION)) {
    const appendix = /^a/i.test(m[1]!);
    if (m[2] === undefined) {
      // "Section" with no number names nothing; "Appendix" names the appendix
      if (appendix) out.push({ appendix });
    } else for (const label of m[2].split(new RegExp(`${JOIN}§*\\s*`))) out.push({ appendix, label });
  }
  return out;
}

// ── the index ───────────────────────────────────────────────────────────────────────────────────────

const ENVIRONMENTS: Record<Kind, string> = {
  figure: "figure\\*?|wrapfigure|SCfigure\\*?|sidewaysfigure\\*?|teaserfigure",
  table: "table\\*?|wraptable|sidewaystable\\*?|longtable\\*?|supertabular\\*?",
};

/** A title's or a location's words as they are compared: lower case, a plural's s dropped, one space between. */
const words = (text: string): string =>
  ` ${text
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, " ")
    .trim()
    .split(" ")
    .map((w) => (w.length > 3 ? w.replace(/s$/, "") : w))
    .join(" ")} `;
/** A line of a PDF's text that opens with a section number: "3.1 Setup", "## **2 Method**", "A Additional samples". */
const NUMBERED_LINE = /^[ \t#]*\**[ \t]*((?:\d{1,2}|[A-Z])(?:\.\d{1,2}){0,3})\.?\**[ \t]+\**[A-Z]/gm;

export class PaperIndex {
  /** The paper's text, a LaTeX source's without its comments. */
  readonly text: string;
  private readonly latex: boolean;
  /** How many figures and tables a LaTeX source numbers. */
  private readonly counts: Record<Kind, number> = { figure: 0, table: 0 };
  /** The labels the text itself cites, by kind ("Tables 2–3" names both). */
  private readonly mentioned: Record<Kind, Set<string>> = { figure: new Set(), table: new Set() };
  /** The paper's section numbers, as keys; undefined when the text does not show its numbering. */
  private readonly sections: Set<string> | undefined;
  /** The titles of its headings, as `words` gives them. */
  private readonly titles: string[];
  private readonly hasAppendix: boolean;
  private found: PaperNumbers | undefined;

  constructor(paper: string, source?: string | null) {
    this.latex = isLatex(paper, source);
    this.text = this.latex ? uncommented(paper) : paper;
    for (const c of cites(this.text)) this.mentioned[c.kind].add(labelKey(c.label));
    const heads = headings(paper, source);
    this.hasAppendix = splitPaper(paper, source).cuts.some((c) => c.part === "appendix");
    if (this.latex) {
      for (const kind of ["figure", "table"] as const) this.counts[kind] = this.count(kind);
      const numbers = heads.flatMap((h) => (h.number ? [labelKey(h.number)] : []));
      this.sections = numbers.length ? new Set(numbers) : undefined;
      this.titles = heads.map((h) => words(h.title));
    } else {
      // a PDF's headings are unevenly found, so any line that opens with a number counts; a text with no "1 …"
      // line is not visibly numbered
      const numbers = new Set([...this.text.matchAll(NUMBERED_LINE)].map((m) => labelKey(m[1]!)));
      this.sections = numbers.has("1") ? numbers : undefined;
      // the Markdown headings, and the lines set bold alone, as a PDF's smaller headings come
      const bold = [...this.text.matchAll(/^[ \t]*\*\*([^*\n]{3,100})\*\*[ \t]*$/gm)].map((m) => m[1]!);
      this.titles = [...heads.map((h) => h.title), ...bold].map((t) => words(t.replace(/^(?:\d{1,2}|[A-Z])(?:\.\d{1,2})*\.?\s+/, "")));
    }
  }

  /** The paper's numbers (`numbers.ts`), read once. */
  get numbers(): PaperNumbers {
    return (this.found ??= new PaperNumbers(this.text));
  }

  /** The numbered captions of a kind: each environment's captions outside its sub-floats (one for an environment
   * with none, so the count errs high), and each \captionof. */
  private count(kind: Kind): number {
    let n = 0;
    const env = new RegExp(`\\\\begin\\{(${ENVIRONMENTS[kind]})\\}([\\s\\S]*?)\\\\end\\{\\1\\}`, "g");
    for (const m of this.text.matchAll(env)) {
      const body = m[2]!.replace(/\\begin\{(sub(?:figure|table)|minipage)\}[\s\S]*?\\end\{\1\}/g, (sub, name: string) => (name === "minipage" ? sub : ""));
      n += Math.max(1, (body.match(/\\caption\s*[[{]/g) ?? []).length);
    }
    return n + (this.text.match(new RegExp(`\\\\captionof\\s*\\{${kind}\\}`, "g")) ?? []).length;
  }

  /** Whether the paper has the figure or table of this label. */
  float(kind: Kind, label: string): Answer {
    const key = labelKey(label);
    if (this.mentioned[kind].has(key)) return "there";
    const name = `${kind === "figure" ? "Figure" : "Table"} ${label}`;
    if (!this.latex) return { no: `the paper's text never mentions ${name}` };
    const count = this.counts[kind];
    // a source with no such environment has none: every cite is wrong, an appendix's too
    if (count === 0) return { no: `the paper's source has no ${kind}` };
    // A.1, S2, 3.4: an appendix's or a section's own numbering, which the source does not carry
    if (!/^\d+$/.test(key)) return "unknown";
    return Number(key) <= count ? "there" : { no: `the paper's source has ${count} ${kind}${count === 1 ? "" : "s"}` };
  }

  /** Whether the paper has an appendix. A LaTeX source starts one by a command or a heading, so one without
   * either has none; a PDF's text may hold one under a heading that was not found. */
  appendix(): Answer {
    return this.hasAppendix ? "there" : this.latex ? { no: "the paper's text has no appendix" } : "unknown";
  }

  /** Whether the paper has the section or appendix of this label ("3.2", "A.1", "B"). */
  section(label: string): Answer {
    const key = labelKey(label);
    const appendix = /^[A-Z]/.test(key);
    if (this.sections?.has(key)) return "there";
    if (appendix && !this.hasAppendix) return this.appendix();
    if (!this.sections) return "unknown";
    // an appendix that is not lettered in the text: its parts cannot be told apart
    if (appendix && ![...this.sections].some((n) => /^[A-Z]/.test(n))) return "unknown";
    return { no: `the paper has no ${appendix ? "appendix" : "section"} ${label}` };
  }

  /** Whether a location names a section by its title: the title is in it, or one of its parts is in a title
   * ("Discussion" for "Discussion and limitations"). The abstract is every paper's. */
  named(where: string): boolean {
    if (/\babstract\b/i.test(where)) return true;
    const said = words(where);
    const parts = where.split(/[,;:()–—]|\band\b/).map((part) => words(part.replace(/\b(?:the|section|appendix|paper|of|in)\b/gi, " ")));
    return this.titles.some((title) => title.length >= 6 && (said.includes(title) || parts.some((part) => part.length >= 6 && !/\d/.test(part) && title.includes(part))));
  }
}
