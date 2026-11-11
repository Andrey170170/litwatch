/**
 * The paper's text as the run reads it: its headings, which the session's `sections()` gives and the checks look
 * places up in (`places.ts`), and the split the writer's first message makes of it (docs/plans/deep-read.md, D4):
 * the main text, with the bibliography and the appendix cut out wherever they sit. Both stay in the session's
 * `paper`, where the writer reads them on demand (`section()`, `find()`, `refs()`).
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

/** One heading of the paper, as the session's `sections()` gives it. */
export interface Heading {
  level: number;
  title: string;
  /** The heading's number in the paper ("3.2", "A.1"), where a LaTeX source's own numbering can be told. A PDF's
   * text carries its numbers in the titles. */
  number?: string;
  start: number;
  /** Where the section ends: at the next heading of its level or above. */
  end: number;
  chars: number;
}

/** Whether the text is a LaTeX source: litwatch says so, or it reads like one. */
export const isLatex = (body: string, source?: string | null): boolean =>
  source === "latex" || (source !== "pdf" && /\\begin\s*\{document\}|\\section\*?\s*\{/.test(body));

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

/** LaTeX without its comments: an unescaped % to the end of its line. */
export const uncommented = (tex: string): string => tex.replace(/((?:^|[^\\])(?:\\\\)*)%.*$/gm, "$1");

type Bare = Omit<Heading, "end" | "chars">;

/** The headings in the order of the text, each with where its section ends. */
function sized(found: Bare[], length: number): Heading[] {
  const sorted = [...found].sort((a, b) => a.start - b.start);
  return sorted.map((h, i) => {
    const end = sorted.slice(i + 1).find((next) => next.level <= h.level)?.start ?? length;
    return { ...h, end, chars: end - h.start };
  });
}

// ── a LaTeX source ──────────────────────────────────────────────────────────────────────────────────

const TEX_LEVELS: Record<string, number> = { section: 1, subsection: 2, subsubsection: 3, paragraph: 4 };

/** What numbers a paper some other way than an article's defaults, whatever is counted: chapters, a class that
 * has them, or the package that ties one counter to another. */
const RENUMBERING = /\\chapter\*?\s*[[{]|\\documentclass\s*(?:\[[^\]]*\])?\s*\{(?:book|report|memoir|scrbook|scrreprt)\}|\\usepackage\s*(?:\[[^\]]*\])?\s*\{[^}]*\bchngcntr\b/;
/** The classes that number sections in Roman numerals and subsections by letter. */
const ROMAN_SECTIONS = /\\documentclass\s*(?:\[[^\]]*\])?\s*\{(?:IEEEtran|revtex[\w-]*)\}/;
/** A float that takes the number of the one before it. */
const CONTINUED = /\\ContinuedFloat(?![a-zA-Z])/;

/** Whether a LaTeX source (without its comments) numbers these counters itself, rather than leave them to the
 * defaults this code models: counted from 1 through the paper, sections lettered from \appendix on. It does where
 * it sets or steps a counter, ties it to another, redefines or prints its form (\thefigure), or loads a class or
 * package that numbers its own way. No number of that kind is then inferred from the source. */
export function ownNumbering(tex: string, counters: readonly string[], also?: RegExp): boolean {
  const names = counters.join("|");
  const set = new RegExp(`\\\\(?:setcounter|addtocounter|stepcounter|refstepcounter|numberwithin|counterwithin\\*?|counterwithout\\*?)\\s*\\{(?:${names})\\}|\\\\the(?:${names})(?![a-zA-Z])`);
  return set.test(tex) || RENUMBERING.test(tex) || (also?.test(tex) ?? false);
}
/** Whether it numbers a kind of float itself. */
export const ownFloatNumbering = (tex: string, kind: string): boolean => ownNumbering(tex, [kind], CONTINUED);

/** Where a LaTeX source starts its appendix by command: from here its sections are lettered. */
const APPENDIX_COMMAND = /\\appendi(?:x|ces)(?![a-zA-Z])|\\begin\s*\{appendi(?:x|ces)\}/g;

/** The text between the brace at `open` and its match. */
function braced(body: string, open: number): string {
  for (let depth = 0, i = open; i < body.length; i++) {
    const ch = body[i];
    if (ch === "\\") i++;
    else if (ch === "{") depth++;
    else if (ch === "}" && --depth === 0) return body.slice(open + 1, i);
  }
  return body.slice(open + 1);
}

const plain = (tex: string): string =>
  tex
    .replace(/\\label\{[^}]*\}/g, "")
    .replace(/\\([&%$#_])/g, "$1")
    .replace(/\\[a-zA-Z]+\*?(\[[^\]]*\])?/g, "")
    .replace(/[{}~]/g, " ")
    .replace(/\s+/g, " ")
    .trim();

function latexHeadings(body: string): Bare[] {
  const out: Bare[] = [];
  // Numbers are LaTeX's defaults: sections counted from 1, lettered from the appendix command on. A source that
  // numbers any level of its sections itself is numbered some other way, which is not guessed at: no heading of
  // it gets a number.
  const numbered = !ownNumbering(uncommented(body), ["section", "subsection", "subsubsection", "secnumdepth"], ROMAN_SECTIONS);
  const appendix = first(body, APPENDIX_COMMAND)?.index ?? Infinity;
  const counters = [0, 0, 0];
  let lettered = false;
  const command = /\\(section|subsection|subsubsection|paragraph)(\*?)\s*(?:\[[^\]]*\])?\s*\{/g;
  for (const m of body.matchAll(command)) {
    if (commented(body, m.index)) continue;
    const level = TEX_LEVELS[m[1]!]!;
    const heading: Bare = { level, title: plain(braced(body, m.index + m[0].length - 1)), start: m.index };
    if (numbered && level <= 3 && m[2] === "") {
      if (!lettered && m.index > appendix) {
        lettered = true;
        counters.fill(0);
      }
      counters[level - 1]! += 1;
      counters.fill(0, level);
      const top = lettered ? String.fromCharCode(64 + counters[0]!) : String(counters[0]);
      // a subsection before any section has no number worth giving
      if (counters[0]! > 0 && (!lettered || counters[0]! <= 26)) heading.number = [top, ...counters.slice(1, level)].join(".");
    }
    out.push(heading);
  }
  for (const m of body.matchAll(/\\begin\s*\{(abstract|thebibliography)\}/g))
    if (!commented(body, m.index)) out.push({ level: 1, title: m[1] === "abstract" ? "Abstract" : "References", start: m.index });
  return out;
}

function latexCuts(body: string): Cut[] {
  const cuts: Cut[] = [];
  // the archive inlines the bibliography as a thebibliography environment, often before the appendix
  const open = first(body, /\\begin\s*\{thebibliography\}/g);
  if (open) {
    const close = /\\end\s*\{thebibliography\}/g;
    close.lastIndex = open.index;
    const end = close.exec(body);
    cuts.push({ part: "references", start: open.index, end: end ? end.index + end[0].length : body.length });
  }
  const appendix = first(body, new RegExp(`${APPENDIX_COMMAND.source}|\\\\section\\*?\\s*(?:\\[[^\\]]*\\])?\\s*\\{\\s*(?:Appendi(?:x|ces)|Supplementa(?:ry|l)\\b)`, "g"));
  if (appendix) cuts.push({ part: "appendix", start: appendix.index, end: body.length });
  return cuts;
}

// ── a PDF's text ────────────────────────────────────────────────────────────────────────────────────
// litwatch's archive reads a PDF as Markdown (`## **3.1 Setup**`: its headings come bold) or, without that reader,
// as plain text, whose headings are lines like any other (`3.1 Setup`).

/** A heading's title without Markdown's emphasis. */
const unmarked = (title: string): string => title.replace(/\*+/g, "").replace(/^_+|_+$/g, "").replace(/\s+/g, " ").trim();

const REFERENCES = /^(?:(?:\d{1,2}|[IVX]+)\.?\s+)?(?:references|bibliography)$/i;
/** A Markdown heading that starts the appendix by its name. */
const APPENDIX_TITLE = /^(?:[A-Z]\.?\s+)?(?:appendi(?:x|ces)|supplementa(?:ry|l)\b)/i;
/** The same on a line that is no Markdown heading, which has to be the whole line and read like a title. */
const APPENDIX_LINE = /^(?:(?:[Aa]ppendi(?:x|ces)|APPENDI(?:X|CES))(?:\s+[A-Z])?(?:[:.]?\s+[A-Z][^.,;()]{0,80})?|[Ss]upplementa(?:ry|l)\s+[Mm]aterials?|SUPPLEMENTARY MATERIALS?)$/;
/** A lettered heading, as an appendix's sections are numbered: "A Additional samples", "C.1 BBH". */
const LETTERED = /^[A-Z](?:\.\d{1,2})*\.?\s+[A-Z]/;
const LETTERED_LINE = /^[A-Z](?:\.\d{1,2})*\.?\s+[A-Z][^.,;()]{1,80}$/;
const INTRODUCTION = /^(?:(?:1|I)\.?\s+)?introduction\.?$/i;

interface PdfParts {
  headings: Bare[];
  references?: Bare;
  appendix?: Bare;
}

/** A PDF text's headings, with the one that starts its bibliography and the one that starts its appendix. The
 * Markdown headings are all taken; a line that is none counts only as one of those two, since nothing else tells a
 * heading from a short line. A Markdown heading counts from the Introduction's heading on, before which it may be a
 * table of contents' entry. The bibliography's is the first such heading that reads "References", else the last
 * line that does. The appendix's is the first such heading or, failing that, line that names it ("Appendix",
 * "Appendix A: Proofs"), a line too only past the Introduction's heading; or the first lettered one ("A Additional
 * samples"), which only after the bibliography reads as a heading. (Plain text is not told by its having no Markdown
 * heading: a table's "# Failed" column is one.) */
function pdfParts(body: string): PdfParts {
  const marked: Bare[] = [...body.matchAll(/^(#{1,6})[ \t]+(.*)$/gm)].map((m) => ({ level: m[1]!.length, title: unmarked(m[2]!), start: m.index })).filter((h) => h.title);
  // below every Markdown heading, so the next one of any level ends it
  const level = Math.max(1, ...marked.map((h) => h.level));
  const lines: Bare[] = [...body.matchAll(/^[ \t]*(?!#)(\S.*)$/gm)].map((m) => ({ level, title: unmarked(m[1]!), start: m.index }));
  // the last Introduction at the shallowest level: a contents list's entries come before it, and an appendix's own
  // Introduction, under an appendix's Markdown heading at its level or above (named, or lettered after a
  // References), is none. Plain lines have no levels to tell a contents list from an appendix by.
  const cited = marked.filter((h) => REFERENCES.test(h.title)).map((h) => h.start);
  const appendices = marked.filter((h) => APPENDIX_TITLE.test(h.title) || (cited.some((r) => r < h.start) && LETTERED.test(h.title)));
  const intros = [...marked, ...lines].filter((h) => INTRODUCTION.test(h.title) && !appendices.some((a) => a.start < h.start && a.level <= h.level));
  const top = Math.min(...intros.map((h) => h.level));
  const introduction = Math.max(0, ...intros.filter((h) => h.level === top).map((h) => h.start));
  // a contents list in Markdown headings sits before the Introduction too
  const sections = marked.filter((h) => h.start >= introduction);
  const references = sections.find((h) => REFERENCES.test(h.title)) ?? lines.filter((h) => (!marked.length || h.start >= introduction) && REFERENCES.test(h.title)).at(-1);
  const after = (h: Bare) => references !== undefined && h.start > references.start;
  const appendix =
    sections.find((h) => APPENDIX_TITLE.test(h.title) || (after(h) && LETTERED.test(h.title))) ??
    lines.find((h) => h.start > introduction && (APPENDIX_LINE.test(h.title) || (after(h) && LETTERED_LINE.test(h.title))));
  const headings = [...marked];
  for (const h of [references, appendix]) if (h && !marked.includes(h)) headings.push(h);
  return { headings, references, appendix };
}

function pdfCuts(body: string): Cut[] {
  const { headings, references, appendix } = pdfParts(body);
  const cuts: Cut[] = [];
  if (references) cuts.push({ part: "references", start: references.start, end: sized(headings, body.length).find((h) => h.start === references.start)!.end });
  if (appendix) cuts.push({ part: "appendix", start: appendix.start, end: body.length });
  return cuts;
}

// ── both ────────────────────────────────────────────────────────────────────────────────────────────

/** The paper's headings in the order of its text. */
export function headings(body: string, source?: string | null): Heading[] {
  return sized(isLatex(body, source) ? latexHeadings(body) : pdfParts(body).headings, body.length);
}

/** The paper's main text and what was cut from it: the bibliography wherever it sits, and everything from the
 * appendix's start (LaTeX's \appendix, an Appendix heading, or the first lettered heading after a PDF's
 * bibliography) to the end. A paper with neither is all main text. */
export function splitPaper(body: string, source?: string | null): Split {
  const found = (isLatex(body, source) ? latexCuts(body) : pdfCuts(body)).sort((a, b) => a.start - b.start);
  // a bibliography inside the appendix is part of it; one the appendix starts inside ends where the appendix starts
  const cuts: Cut[] = [];
  for (const cut of found) {
    const last = cuts.at(-1);
    if (last && cut.start < last.end) {
      if (cut.part === "appendix") {
        last.end = cut.start;
        cuts.push({ ...cut });
      } else if (cut.end > last.end) cuts.push({ ...cut, start: last.end });
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

/** The paper's outline for the first message: each heading with its number where the source gives one and its
 * size, and which part it is in when that part was left out of the message. */
export function outline(headings: Pick<Heading, "level" | "title" | "number" | "start" | "chars">[], cuts: Cut[]): string {
  if (headings.length === 0) return "(no headings found; read the text with find())";
  return headings
    .map((h) => {
      const part = cuts.find((c) => h.start >= c.start && h.start < c.end)?.part;
      return `${"  ".repeat(Math.max(0, h.level - 1))}- ${h.number ? `${h.number} ` : ""}${h.title} (${h.chars} characters${part ? `, ${part === "appendix" ? "appendix" : "bibliography"}: in the session only` : ""})`;
    })
    .join("\n");
}
