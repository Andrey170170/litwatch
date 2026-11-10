/**
 * The checks `submit` runs in code on a draft that fits the schema (docs/specs/deep-read.md, "Checks in code"):
 * what a model need not judge. The structure (anchors in their places, one lens per graded topic, unique ids, a
 * table row's cells against its columns, a diagram's edges against its nodes); every equation through KaTeX; every
 * figure and table cited against the paper; every location (`where`) naming a place in the paper; and every number
 * given as the paper's against the paper's text (`numbers.ts`).
 *
 * A number the paper's text lacks passes when its sentence cites a figure: it was read off a plot, and the reviewer
 * checks it against that figure instead (`Checked.plots`).
 */

import katex from "katex";
import { PaperNumbers, numbersIn, plainText } from "./numbers.ts";
import { isLatex } from "./paper.ts";
import type { Draft, DraftBlock, DraftDiagram, DraftSection } from "./read.ts";
import { amongErrors, isWebUrl, type Found } from "./schema.ts";

export interface CheckContext {
  /** The paper's complete text. */
  paper: string;
  /** `latex` or `pdf`; anything else is told from the text. */
  source?: string | null;
  found: Found;
  /** The topics the read is written for, as a lens names each (`Lens.topic`): one lens each. */
  topics: readonly string[];
  /** The names of the figures this session made (`figure()`, `crop()`). */
  figures: ReadonlySet<string>;
}

export interface Checked {
  errors: string[];
  /** The figures cited for values the paper's text lacks: `shown` by the names of the read's `fig` blocks,
   * `unshown` as cited ("Figure 9") where the read shows no such figure. */
  plots: { shown: string[]; unshown: string[] };
}

/** Diagram nodes stay tappable on the phone. */
export const MIN_NODE_HEIGHT = 40;
/** How many of a kind of error are listed before the rest are counted. */
const MAX_LISTED = 40;

// ── the paper's figures, tables and sections ────────────────────────────────────────────────────────

/** LaTeX without its comments: an unescaped % to the end of its line. */
const uncommented = (tex: string) => tex.replace(/((?:^|[^\\])(?:\\\\)*)%.*$/gm, "$1");

type Kind = "figure" | "table";
const ENVIRONMENTS: Record<Kind, string> = {
  figure: "figure\\*?|wrapfigure|SCfigure\\*?|sidewaysfigure\\*?|teaserfigure",
  table: "table\\*?|wraptable|sidewaystable\\*?|longtable\\*?|supertabular\\*?",
};

/** What the paper's text shows of its numbering: how many figures and tables a LaTeX source numbers (0: it cannot
 * be told), and how many top-level sections. A PDF's text is searched for each figure's and table's label instead. */
class Places {
  /** The paper's text, a LaTeX source's without its comments. */
  readonly text: string;
  private readonly latex: boolean;
  private readonly counts: Partial<Record<Kind, number>> = {};
  /** The labels the text itself cites, by kind ("Tables 2–3" names both). */
  private mentioned: Record<Kind, Set<string>> | undefined;

  constructor(paper: string, source?: string | null) {
    this.latex = isLatex(paper, source);
    this.text = this.latex ? uncommented(paper) : paper;
  }

  /** The numbered captions of a kind: each environment's captions outside its sub-floats (one for an environment
   * with none, so the count errs high), and each \captionof. */
  private count(kind: Kind): number {
    const known = this.counts[kind];
    if (known !== undefined) return known;
    let n = 0;
    const env = new RegExp(`\\\\begin\\{(${ENVIRONMENTS[kind]})\\}([\\s\\S]*?)\\\\end\\{\\1\\}`, "g");
    for (const m of this.text.matchAll(env)) {
      const body = m[2]!.replace(/\\begin\{(sub(?:figure|table)|minipage)\}[\s\S]*?\\end\{\1\}/g, (sub, name: string) => (name === "minipage" ? sub : ""));
      n += Math.max(1, (body.match(/\\caption\s*[[{]/g) ?? []).length);
    }
    n += (this.text.match(new RegExp(`\\\\captionof\\s*\\{${kind}\\}`, "g")) ?? []).length;
    return (this.counts[kind] = n);
  }

  /** Why a cited figure or table is not the paper's, or undefined when it is or cannot be told. */
  missing(kind: Kind, label: string): string | undefined {
    const n = /^(\d+)[a-z]?$/.exec(label);
    if (!n) return undefined; // A1, S3, B.2: an appendix's own numbering, which the text does not show
    if (!this.mentioned) {
      this.mentioned = { figure: new Set(), table: new Set() };
      for (const c of cites(this.text)) this.mentioned[c.kind].add(c.label.replace(/[a-z]$/, ""));
    }
    if (this.mentioned[kind].has(n[1]!)) return undefined;
    if (!this.latex) return `the paper's text never mentions ${kind === "figure" ? "Figure" : "Table"} ${n[1]}`;
    const count = this.count(kind);
    return count === 0 || Number(n[1]) <= count ? undefined : `the paper's text has ${count} ${kind}${count === 1 ? "" : "s"}`;
  }

  /** How many numbered top-level sections a LaTeX source's main text has; 0 when it cannot be told, as for a PDF's
   * text, whose headings are too unevenly found to count. */
  sections(): number {
    if (!this.latex) return 0;
    const appendix = this.text.search(/\\appendix\b|\\begin\{appendi(?:x|ces)\}/);
    const main = appendix === -1 ? this.text : this.text.slice(0, appendix);
    return (main.match(/\\section\s*(?:\[[^\]]*\])?\s*\{/g) ?? []).length;
  }
}

// ── the read's texts ────────────────────────────────────────────────────────────────────────────────

/** One string of the read. `inline`: it takes the inline markup, so its $tex$ is parsed. `numbers`: its numbers are
 * given as the paper's, so they are checked; `plot`: unless the paper's text lacks them and this figure is cited
 * for the whole of it (a figure's own caption, a table whose caption cites one). */
interface Unit {
  path: string;
  text: string;
  inline?: boolean;
  numbers?: boolean;
  where?: boolean;
  plot?: string[];
}

/** A glance fact that is the read's own: the reading time, and what the read found of the code. */
const OWN_FACT = /^(read(ing)?( time)?|code|artifacts?|code and artifacts)$/i;

const CITE = /\b(Figures?|Figs?\.?|Tables?|Tabs?\.?)(?:\s|~)*((?:[A-Z]\.?)?\d+[a-z]?(?:\s*(?:,|and|&|–|—|-|to)\s*(?:[A-Z]\.?)?\d+[a-z]?)*)/g;
/** The figures and tables a text cites: a plural cites each label of its list, a singular its first only. */
function cites(text: string): { kind: Kind; label: string; index: number; end: number }[] {
  const out: { kind: Kind; label: string; index: number; end: number }[] = [];
  for (const m of text.matchAll(CITE)) {
    const kind: Kind = /^f/i.test(m[1]!) ? "figure" : "table";
    const plural = /^(Figures|Figs\.?|Tables|Tabs\.?)$/i.test(m[1]!);
    const labels = m[2]!.split(/\s*(?:,|and|&|–|—|-|to)\s*/);
    const taken = plural ? labels : labels.slice(0, 1);
    const end = plural ? m.index + m[0].length : m.index + m[0].length - (m[2]!.length - labels[0]!.length);
    for (const label of taken) out.push({ kind, label, index: m.index, end });
  }
  return out;
}

/** The text of a `fig` block's `number` as a label: "Figure 6" is 6. */
const figureLabel = (number: string): string | undefined => /(\d+)[a-z]?\s*$/.exec(number.trim())?.[1];

function* blockUnits(b: DraftBlock, at: string): Generator<Unit> {
  const prose = { inline: true, numbers: true };
  switch (b.type) {
    case "p":
    case "why":
      yield { path: `${at}/text`, text: b.text, ...prose };
      break;
    case "h3":
      yield { path: `${at}/text`, text: b.text };
      break;
    case "list":
      for (const [i, item] of b.items.entries()) yield { path: `${at}/items/${i}`, text: item, ...prose };
      break;
    case "note":
      yield { path: `${at}/text`, text: b.text, ...prose };
      yield { path: `${at}/where`, text: b.where, where: true };
      break;
    case "eq":
      if (b.caption !== undefined) yield { path: `${at}/caption`, text: b.caption, ...prose };
      break;
    case "fig":
      yield { path: `${at}/number`, text: b.number };
      // a figure's caption gives what the figure shows: its values are the plot's
      yield { path: `${at}/caption`, text: b.caption, ...prose, plot: [b.name] };
      break;
    case "xp":
      for (const key of ["question", "did", "found", "means", "not_shown"] as const) {
        const value = b[key];
        if (value !== undefined) yield { path: `${at}/${key}`, text: value, ...prose };
      }
      break;
    case "table": {
      // how the read built the table is its own; a figure the caption cites is where the cells were read off
      if (b.caption !== undefined) yield { path: `${at}/caption`, text: b.caption, inline: true };
      const plot = b.caption === undefined ? [] : cites(b.caption).filter((c) => c.kind === "figure").map((c) => `#${c.label}`);
      const cells = { numbers: true, ...(plot.length ? { plot } : {}) };
      for (const [i, column] of b.columns.entries()) yield { path: `${at}/columns/${i}`, text: column, ...cells };
      for (const [r, row] of b.rows.entries()) {
        if ("group" in row) continue;
        for (const [c, cell] of row.cells.entries()) {
          const value = typeof cell === "object" ? cell.value : cell;
          yield { path: `${at}/rows/${r}/cells/${c}`, text: String(value), ...cells };
          if (typeof cell === "object") yield { path: `${at}/rows/${r}/cells/${c}/note`, text: cell.note, ...cells };
        }
      }
      break;
    }
    case "diagram":
      if (b.caption !== undefined) yield { path: `${at}/caption`, text: b.caption, inline: true };
      for (const [i, n] of b.nodes.entries()) {
        yield { path: `${at}/nodes/${i}/label`, text: n.label, numbers: true };
        if (n.sub !== undefined) yield { path: `${at}/nodes/${i}/sub`, text: n.sub, numbers: true };
        yield { path: `${at}/nodes/${i}/tip`, text: n.tip, ...prose };
        if (n.where !== undefined) yield { path: `${at}/nodes/${i}/where`, text: n.where, where: true };
      }
      for (const [i, e] of b.edges.entries()) {
        if (e.label !== undefined) yield { path: `${at}/edges/${i}/label`, text: e.label, numbers: true };
        yield { path: `${at}/edges/${i}/tip`, text: e.tip, ...prose };
        if (e.where !== undefined) yield { path: `${at}/edges/${i}/where`, text: e.where, where: true };
      }
      break;
  }
}

function* sectionUnits(s: DraftSection, at: string): Generator<Unit> {
  const prose = { inline: true, numbers: true };
  switch (s.kind) {
    case "glance":
      yield { path: `${at}/claim`, text: s.claim, ...prose };
      for (const [i, point] of s.points.entries()) yield { path: `${at}/points/${i}`, text: point, ...prose };
      for (const [i, fact] of s.facts.entries())
        if (fact.text !== undefined) yield { path: `${at}/facts/${i}/text`, text: fact.text, inline: true, numbers: !OWN_FACT.test(fact.name.trim()) };
      break;
    case "claims":
      yield { path: `${at}/takeaway`, text: s.takeaway, ...prose };
      for (const [i, claim] of s.claims.entries()) {
        yield { path: `${at}/claims/${i}/text`, text: claim.text, ...prose };
        yield { path: `${at}/claims/${i}/evidence`, text: claim.evidence, ...prose };
        yield { path: `${at}/claims/${i}/where`, text: claim.where, where: true };
      }
      break;
    case "limits":
      yield { path: `${at}/takeaway`, text: s.takeaway, ...prose };
      for (const [i, l] of s.limitations.entries()) yield { path: `${at}/limitations/${i}/text`, text: l.text, ...prose };
      break;
    case "related":
      // other papers' numbers are not this paper's
      yield { path: `${at}/takeaway`, text: s.takeaway, inline: true };
      break;
    case "repro":
      // what the read found when it checked: the repository's own numbers, not the paper's
      yield { path: `${at}/takeaway`, text: s.takeaway, inline: true };
      for (const [i, fact] of s.facts.entries()) if (fact.text !== undefined) yield { path: `${at}/facts/${i}/text`, text: fact.text, inline: true };
      break;
    default:
      yield { path: `${at}/takeaway`, text: s.takeaway, ...prose };
      for (const [i, b] of s.blocks.entries()) yield* blockUnits(b, `${at}/blocks/${i}`);
  }
}

// ── the checks ──────────────────────────────────────────────────────────────────────────────────────

const BODY = new Set(["problem", "method", "finding"]);

/** The anchors in their places, a lens per graded topic and no other, and no id twice. */
function structureErrors(read: Draft, topics: readonly string[]): string[] {
  const errors: string[] = [];
  const kinds = read.sections.map((s) => s.kind);
  // one letter per section: g, then the paper's own sections (b), c, lenses (l), then m r p
  const letters = kinds.map((k) => (BODY.has(k) ? "b" : k === "limits" ? "m" : k === "repro" ? "p" : k[0])).join("");
  if (!/^gb+cl*mrp$/.test(letters))
    errors.push(
      `/sections: the order must be At a glance (glance), the paper's own sections (problem, method, finding; at least one), ` +
        `Claims, one lens per graded topic, Limitations (limits), Related work (related), Reproducibility (repro); got ${kinds.join(", ")}`,
    );
  const ids = new Set<string>();
  for (const [i, s] of read.sections.entries()) {
    if (ids.has(s.id)) errors.push(`/sections/${i}/id: "${s.id}" is used by an earlier section`);
    ids.add(s.id);
  }
  const lenses = read.sections.flatMap((s, i) => (s.kind === "lens" ? [{ topic: s.topic, i }] : []));
  const seen = new Set<string>();
  for (const { topic, i } of lenses) {
    if (!topics.includes(topic)) errors.push(`/sections/${i}/topic: "${topic}" is not a topic this read is written for${topics.length ? ` (${topics.join(", ")})` : "; the paper is graded in none, so the read has no lens"}`);
    else if (seen.has(topic)) errors.push(`/sections/${i}/topic: a second lens for "${topic}"`);
    seen.add(topic);
  }
  for (const topic of topics) if (!seen.has(topic)) errors.push(`/sections: no lens for the topic "${topic}"`);
  return errors;
}

function tableErrors(b: Extract<DraftBlock, { type: "table" }>, at: string): string[] {
  return b.rows.flatMap((row, r) =>
    "cells" in row && row.cells.length !== b.columns.length ? [`${at}/rows/${r}/cells: ${row.cells.length} cells for ${b.columns.length} columns`] : [],
  );
}

/** Whether every node of a diagram carries its place, so the diagram is drawn as given; none does when it is to be
 * laid out. */
export const placed = (d: DraftDiagram): boolean => d.nodes.every((n) => n.x !== undefined && n.y !== undefined);

function diagramErrors(d: DraftDiagram, at: string): string[] {
  const errors: string[] = [];
  const ids = new Set<string>();
  for (const [i, n] of d.nodes.entries()) {
    if (ids.has(n.id)) errors.push(`${at}/nodes/${i}/id: "${n.id}" is used by an earlier node`);
    ids.add(n.id);
    if (n.h !== undefined && n.h < MIN_NODE_HEIGHT) errors.push(`${at}/nodes/${i}/h: at least ${MIN_NODE_HEIGHT}, so the box can be tapped on a phone`);
  }
  for (const [i, e] of d.edges.entries())
    for (const end of ["from", "to"] as const) if (!ids.has(e[end])) errors.push(`${at}/edges/${i}/${end}: no node "${e[end]}" in this diagram`);
  const some = d.nodes.some((n) => n.x !== undefined || n.y !== undefined);
  if (placed(d)) {
    for (const [i, n] of d.nodes.entries()) if (n.w === undefined || n.h === undefined) errors.push(`${at}/nodes/${i}: a node with x and y needs its w and h`);
  } else {
    if (some) errors.push(`${at}/nodes: give x and y on every node, or on none to have the diagram laid out`);
    else {
      // a laid-out diagram's routes and places are the layout's
      if (d.lanes?.length) errors.push(`${at}/lanes: lanes need every node placed (x, y, w, h); leave them out of a diagram that is laid out for you`);
      for (const [i, e] of d.edges.entries())
        for (const key of ["points", "label_at"] as const) if (e[key] !== undefined) errors.push(`${at}/edges/${i}/${key}: only in a diagram whose nodes are all placed`);
    }
  }
  return errors;
}

/** What stops a draft that fits the schema from being drawn: a table's or a diagram's own faults. */
export function drawErrors(read: Draft): string[] {
  return read.sections.flatMap((s, i) =>
    "blocks" in s
      ? s.blocks.flatMap((b, j) => (b.type === "table" ? tableErrors(b, `/sections/${i}/blocks/${j}`) : b.type === "diagram" ? diagramErrors(b, `/sections/${i}/blocks/${j}`) : []))
      : [],
  );
}

const tex = (source: string, displayMode: boolean): string | undefined => {
  try {
    katex.renderToString(source, { displayMode, throwOnError: true, strict: "ignore" });
    return undefined;
  } catch (e) {
    return (e instanceof Error ? e.message : String(e)).replace(/^KaTeX parse error: /, "");
  }
};

// The web app's rule for inline text (`web/src/deepread/inline.tsx`), mark for mark; group 3 is $tex$.
const MARK = /\\([\\*`$[\]])|`([^`\n]+)`|\$(?!\s)((?:\\.|[^$\\])+?)(?<!\s)\$(?!\d)|\*\*(?!\s)(.+?)(?<!\s)\*\*|\*(?![\s*])(.+?)(?<![\s*])\*|\[([^[\]\n]+)\]\((https?:\/\/[^\s()]+)\)/g;

/** The inline maths of an inline text, nested marks included. */
function inlineTex(text: string): string[] {
  const out: string[] = [];
  for (const m of text.matchAll(MARK)) {
    if (m[3] !== undefined) out.push(m[3]);
    const inner = m[4] ?? m[5] ?? m[6];
    if (inner !== undefined) out.push(...inlineTex(inner));
  }
  return out;
}

/** A place in the paper, as a location names one. */
const PLACE = /§|\b(?:Sec(?:tion|s)?\.?|App(?:endix|endices|\.)|Fig(?:ure|s)?\.?|Tab(?:le|s)?\.?|Eqs?\.?|Equations?|Alg(?:orithm|\.)|Abstract|Introduction|Conclusions?|Discussion|Related work|Footnotes?|Listings?|Theorem|Lemma|Proposition|Definition|pp?\.|pages?)(?![a-z])/i;

// What a number is no value of: a place in the paper, an arXiv id, a link.
const LOCATION = /(?:§+\s*|\b(?:Sections?|Secs?\.?|Appendi(?:x|ces)|Apps?\.?|Eqs?\.?|Equations?|Algorithms?|Algs?\.?|Theorems?|Lemmas?|Propositions?|Corollar(?:y|ies)|Definitions?|Footnotes?|Listings?|Pages?|pp?\.|Lines?)(?:\s|~)*)\(?(?:[A-Z]\.?)?\d+(?:\.\d+)*[a-z]?\)?(?:\s*(?:,|and|&|–|—|-|to)\s*§*\s*\(?(?:[A-Z]\.?)?\d+(?:\.\d+)*[a-z]?\)?)*/gi;
const NOT_VALUES = [LOCATION, /\b\d{4}\.\d{4,5}(?:v\d+)?\b/g, /\]\(https?:\/\/[^\s()]+\)/g, /https?:\/\/\S+/g];
/** A whole number that is a year, which a read may give of its own knowledge. */
const isYear = (n: { value: number; bare: boolean; text: string }) => n.bare && /^\d{4}$/.test(n.text) && n.value >= 1900 && n.value <= 2100;

const ABBREVIATION = /(?:\b(?:Figs?|Secs?|Apps?|Eqs?|Tabs?|Algs?|vs|cf|al|approx|resp|No|e\.g|i\.e|pp?)|\b[A-Z])\.$/;
/** The sentence around `index`: from the last sentence end before it to the next one after it. */
function sentence(text: string, index: number): string {
  let start = 0;
  let end = text.length;
  for (const m of text.matchAll(/[.!?](?=\s+["'“(\[]?[A-Z])/g)) {
    if (m[0] === "." && ABBREVIATION.test(text.slice(0, m.index + 1))) continue;
    if (m.index < index) start = m.index + 1;
    else {
      end = m.index + 1;
      break;
    }
  }
  return text.slice(start, end);
}

/** The check `submit` runs, and `check()` before it. Every error names the place in the read it is about. */
export function check(read: Draft, ctx: CheckContext): Checked {
  const errors = structureErrors(read, ctx.topics);
  const places = new Places(ctx.paper, ctx.source);
  const numbers = new PaperNumbers(places.text);
  const sectionCount = places.sections();
  // the figures the read shows, by the paper's number of each
  const shown = new Map<string, string>();
  const plots = new Set<string>();
  const absent = new Map<string, string[]>();

  for (const [i, s] of read.sections.entries()) {
    const at = `/sections/${i}`;
    if ("blocks" in s) {
      for (const [j, b] of s.blocks.entries()) {
        const here = `${at}/blocks/${j}`;
        if (b.type === "table") errors.push(...tableErrors(b, here));
        else if (b.type === "diagram") errors.push(...diagramErrors(b, here));
        else if (b.type === "eq") {
          const problem = tex(b.tex, true);
          if (problem) errors.push(`${here}/tex: KaTeX cannot parse it: ${problem}`);
        } else if (b.type === "fig") {
          if (!ctx.figures.has(b.name)) errors.push(`${here}/name: this session made no figure "${b.name}"; figure() or crop() makes one under the name you give it`);
          const label = figureLabel(b.number);
          if (label !== undefined) shown.set(label, b.name);
        }
      }
    }
  }

  for (const [i, s] of read.sections.entries()) {
    for (const unit of sectionUnits(s, `/sections/${i}`)) {
      if (unit.inline)
        for (const source of inlineTex(unit.text)) {
          const problem = tex(source, false);
          if (problem) errors.push(`${unit.path}: KaTeX cannot parse $${source}$: ${problem}`);
        }
      const text = plainText(unit.text);
      const cited = cites(text);
      for (const c of cited) {
        const problem = places.missing(c.kind, c.label);
        if (problem) errors.push(`${unit.path}: cites ${c.kind === "figure" ? "Figure" : "Table"} ${c.label}, but ${problem}`);
      }
      if (unit.where) {
        if (!PLACE.test(unit.text)) errors.push(`${unit.path}: "${unit.text}" names no place in the paper: give a section, figure, table, appendix, equation or page`);
        if (sectionCount > 0)
          for (const m of unit.text.matchAll(/(?:§+|\bSec(?:tion|s)?\.?)\s*(\d+)/g))
            if (Number(m[1]) > sectionCount) errors.push(`${unit.path}: cites §${m[1]}, but the paper's main text has ${sectionCount} section${sectionCount === 1 ? "" : "s"}`);
      }
      if (!unit.numbers) continue;
      // blank out what is no value, keeping every index
      let values = text;
      for (const c of cited) values = values.slice(0, c.index) + " ".repeat(c.end - c.index) + values.slice(c.end);
      for (const pattern of NOT_VALUES) values = values.replace(pattern, (m) => " ".repeat(m.length));
      const missing: string[] = [];
      for (const n of numbersIn(values)) {
        if ((n.bare && n.value < 10) || isYear(n) || numbers.has(n)) continue;
        // read off a plot: the figure the whole text is about, or one its sentence cites
        const from = unit.plot ?? cites(sentence(text, n.index)).filter((c) => c.kind === "figure").map((c) => `#${c.label}`);
        if (from.length) for (const f of from) plots.add(f);
        else missing.push(n.text);
      }
      if (missing.length) absent.set(unit.path, missing);
    }
  }

  const listed = [...absent].slice(0, MAX_LISTED).map(([path, texts]) => `${path}: ${[...new Set(texts)].join(", ")} ${texts.length === 1 ? "is" : "are"} not in the paper's text`);
  if (listed.length) {
    if (absent.size > MAX_LISTED) listed.push(`… and ${absent.size - MAX_LISTED} more places with numbers the paper's text lacks`);
    listed.push(
      "Numbers: give each number as the paper's text states it (0.34 and 34% are the same; rounding is fine). A value read off a plot " +
        "cites its figure in the same sentence. Your own arithmetic (a difference, a ratio the paper does not state) is given through " +
        "the paper's numbers or in words.",
    );
  }
  errors.push(...listed, ...amongErrors(read.among_collection, ctx.found));
  for (const [i, n] of (read.process_notes ?? []).entries()) if (n.url !== undefined && !isWebUrl(n.url)) errors.push(`/process_notes/${i}/url: not an absolute http(s) URL`);

  const names = new Set<string>();
  const unshown = new Set<string>();
  for (const plot of plots) {
    // "#6" is the paper's Figure 6, cited; anything else is the name of a figure the read shows
    const name = plot.startsWith("#") ? shown.get(/^#(\d+)/.exec(plot)?.[1] ?? "") : plot;
    if (name !== undefined && ctx.figures.has(name)) names.add(name);
    else if (plot.startsWith("#")) unshown.add(`Figure ${plot.slice(1)}`);
  }
  return { errors, plots: { shown: [...names], unshown: [...unshown] } };
}
