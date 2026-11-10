/**
 * The places a paper has, as the checks look them up (`checks.ts`): its figures and tables, its numbered sections
 * and the titles of its headings, and its numbers. One index is built per run from the paper's text, and one
 * reading of a label ("3", "A.1", "S2") serves every lookup: a cite in the read, a `fig` block's number, the number
 * a figure is made under.
 *
 * An answer is "there" or a refusal only on evidence. A LaTeX source numbered by LaTeX's defaults shows its floats
 * and its section commands, so a number past them is refused and one within them is there. Everything else is
 * "unknown" and left to the reviewer by name: any place of a PDF's text that the text does not itself name, every
 * number of a kind the source numbers itself (`ownNumbering`), an appendix's own numbering, a float the count
 * cannot place.
 */

import { PaperNumbers } from "./numbers.ts";
import { headings, isLatex, ownFloatNumbering, splitPaper, uncommented } from "./paper.ts";

export type Kind = "figure" | "table";
/** Whether the paper has a place: it does, it does not (and why), or the text cannot tell. */
export type Answer = "there" | "unknown" | { no: string };

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

// the word in any case ("fig. 1", "TABLE 2"); the label as the paper writes it
const CITE = new RegExp(`\\b((?i:figures?|figs?\\.?|tables?|tabs?\\.?))(?:\\s|~)*(${LABEL}(?:${JOIN}${LABEL})*)`, "g");
/** The longest range that is written out label by label. */
const MAX_RANGE = 50;

/** The labels a range names, both ends included, where the two count the same way ("1" to "3", "A.1" to "A.3",
 * "S2" to "S4"); the two ends alone where they do not. */
function between(from: string, to: string): string[] {
  const parts = (label: string) => /^(.*?)(\d+)[a-z]?$/.exec(label);
  const [a, b] = [parts(from), parts(to)];
  const span = a && b && a[1] === b[1] ? Number(b[2]) - Number(a[2]) : 0;
  return span > 1 && span <= MAX_RANGE ? Array.from({ length: span + 1 }, (_, i) => `${a![1]}${Number(a![2]) + i}`) : [from, to];
}

/** The figures and tables a text cites. A plural cites every label of its list, a range in it label by label
 * ("Figures 1–3 and 5" cites 1, 2, 3 and 5); a singular cites its first label only. */
export function cites(text: string): Cite[] {
  const out: Cite[] = [];
  for (const m of text.matchAll(CITE)) {
    const kind: Kind = /^f/i.test(m[1]!) ? "figure" : "table";
    const plural = /^(Figures|Figs\.?|Tables|Tabs\.?)$/i.test(m[1]!);
    // labels and what joins them, in turn
    const tokens = m[2]!.split(new RegExp(`(${JOIN})`));
    const labels = [tokens[0]!];
    if (plural)
      for (let i = 1; i < tokens.length; i += 2) {
        if (/[–—-]|to/.test(tokens[i]!)) labels.push(...between(labels.pop()!, tokens[i + 1]!));
        else labels.push(tokens[i + 1]!);
      }
    const end = plural ? m.index + m[0].length : m.index + m[0].length - (m[2]!.length - tokens[0]!.length);
    for (const label of labels) out.push({ kind, label, index: m.index, end });
  }
  return out;
}

// A Roman numeral first ("Sec. IV", as IEEEtran and REVTeX number), then a letter or a number.
const SECTION_LABEL = "(?:[IVX]{2,6}(?:\\.\\d+)*(?![A-Za-z])|[A-Z](?:\\.?\\d+(?:\\.\\d+)*)?(?![A-Za-z])|\\d+(?:\\.\\d+)*)";
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

interface Floats {
  sure: number;
  most: number;
  plain: boolean;
}

export class PaperIndex {
  /** The paper's text, a LaTeX source's without its comments. */
  readonly text: string;
  /** Whether the paper's headings are all known, as a LaTeX source's are: a location that names none of them names
   * no place. A PDF's are unevenly found. */
  readonly outlined: boolean;
  /** How many figures and tables a LaTeX source holds: at least `sure` in its main text and at most `most` in
   * all. `plain`: they are numbered by LaTeX's defaults, from 1, so the counts say which numbers there are. */
  private readonly floats: Record<Kind, Floats> = { figure: { sure: 0, most: 0, plain: false }, table: { sure: 0, most: 0, plain: false } };
  /** The labels the text itself cites, by kind ("Tables 2–3" names both). */
  private readonly mentioned: Record<Kind, Set<string>> = { figure: new Set(), table: new Set() };
  /** The section numbers the text shows, as keys: a LaTeX source's own, or those a PDF's lines open with. */
  private readonly sections: Set<string>;
  /** Whether `sections` is the paper's whole numbered outline: a LaTeX source numbered by LaTeX's defaults. */
  private readonly numbered: boolean;
  /** The titles of its headings, as `words` gives them. */
  private readonly titles: string[];
  private readonly hasAppendix: boolean;
  private found: PaperNumbers | undefined;

  constructor(paper: string, source?: string | null) {
    this.outlined = isLatex(paper, source);
    this.text = this.outlined ? uncommented(paper) : paper;
    for (const c of cites(this.text)) this.mentioned[c.kind].add(labelKey(c.label));
    const heads = headings(paper, source);
    const appendix = splitPaper(paper, source).cuts.find((c) => c.part === "appendix");
    this.hasAppendix = appendix !== undefined;
    let titles: string[];
    if (this.outlined) {
      for (const kind of ["figure", "table"] as const) this.floats[kind] = this.count(kind, appendix?.start ?? Infinity);
      // `headings` numbers a source only where its numbering is LaTeX's own
      this.sections = new Set(heads.flatMap((h) => (h.number ? [labelKey(h.number)] : [])));
      this.numbered = this.sections.size > 0;
      titles = heads.map((h) => h.title);
    } else {
      // a PDF's headings are unevenly found: a number a line opens with is there, and any other may be too
      this.sections = new Set([...this.text.matchAll(NUMBERED_LINE)].map((m) => labelKey(m[1]!)));
      this.numbered = false;
      // the Markdown headings, and the lines set bold alone, as a PDF's smaller headings come
      const bold = [...this.text.matchAll(/^[ \t]*\*\*([^*\n]{2,100})\*\*[ \t]*$/gm)].map((m) => m[1]!);
      titles = [...heads.map((h) => h.title), ...bold].map((t) => t.replace(/^(?:\d{1,2}|[A-Z])(?:\.\d{1,2})*\.?\s+/, ""));
    }
    this.titles = titles.map(words).filter((t) => t.trim() !== "");
  }

  /** The paper's numbers (`numbers.ts`), read once. */
  get numbers(): PaperNumbers {
    return (this.found ??= new PaperNumbers(this.text));
  }

  /** The numbered floats of a kind, between two bounds. Each environment's captions outside its sub-floats and
   * each \captionof count; an environment with no caption may be captioned by a macro, so it counts toward `most`
   * only. `sure` holds the main text's, before `appendix`, where default numbering runs on from 1. */
  private count(kind: Kind, appendix: number): Floats {
    let sure = 0;
    let most = 0;
    const add = (at: number, captions: number, floor: number) => {
      most += Math.max(floor, captions);
      if (at < appendix) sure += captions;
    };
    const env = new RegExp(`\\\\begin\\{(${ENVIRONMENTS[kind]})\\}([\\s\\S]*?)\\\\end\\{\\1\\}`, "g");
    for (const m of this.text.matchAll(env)) {
      const body = m[2]!.replace(/\\begin\{(sub(?:figure|table)|minipage)\}[\s\S]*?\\end\{\1\}/g, (sub, name: string) => (name === "minipage" ? sub : ""));
      add(m.index, (body.match(/\\caption\s*[[{]/g) ?? []).length, 1);
    }
    for (const m of this.text.matchAll(new RegExp(`\\\\captionof\\s*\\{${kind}\\}`, "g"))) add(m.index, 1, 1);
    return { sure, most, plain: !ownFloatNumbering(this.text, kind) };
  }

  /** Whether the paper has the figure or table of this label. */
  float(kind: Kind, label: string): Answer {
    const key = labelKey(label);
    if (this.mentioned[kind].has(key)) return "there";
    // a PDF's text may hold a figure it never names as the read does
    if (!this.outlined) return "unknown";
    const { sure, most, plain } = this.floats[kind];
    // a source with no such environment has none, however it numbers: every cite is wrong, an appendix's too
    if (most === 0) return { no: `the paper's source has no ${kind}` };
    // numbers the source sets itself, and A.1, S2, 3.4: an appendix's or a section's own, which it does not carry
    if (!plain || !/^\d+$/.test(key)) return "unknown";
    if (Number(key) > most) return { no: `the paper's source has ${sure === most ? "" : "at most "}${most} ${kind}${most === 1 ? "" : "s"}` };
    return Number(key) <= sure ? "there" : "unknown";
  }

  /** Whether the paper has an appendix: it does where one was found, and may where none was. */
  appendix(): Answer {
    return this.hasAppendix ? "there" : "unknown";
  }

  /** Whether the paper has the section or appendix of this label ("3.2", "A.1", "B"). */
  section(label: string): Answer {
    const key = labelKey(label);
    if (this.sections.has(key)) return "there";
    if (!this.numbered) return "unknown";
    // a lettered part of a source that letters none: an appendix under headings of its own
    if (/^[A-Z]/.test(key) && ![...this.sections].some((n) => /^[A-Z]/.test(n))) return "unknown";
    return { no: `the paper has no ${/^[A-Z]/.test(key) ? "appendix" : "section"} ${label}` };
  }

  /** Whether a location names a section by its title: it is the title, whole or as one of its parts, however
   * short ("PPO"); or the title is in it, or one of its parts is in a title ("Discussion" for "Discussion and
   * limitations"), where the match is long enough not to be chance. The abstract is every paper's. */
  named(where: string): boolean {
    if (/\babstract\b/i.test(where)) return true;
    const said = words(where);
    const parts = where.split(/[,;:()–—]|\band\b/).map((part) => words(part.replace(/\b(?:the|section|appendix|paper|of|in)\b/gi, " ")));
    const partly = (title: string) => title.length >= 6 && (said.includes(title) || parts.some((part) => part.length >= 6 && !/\d/.test(part) && title.includes(part)));
    return this.titles.some((title) => said === title || parts.includes(title) || partly(title));
  }
}
