/**
 * The checks `submit` runs in code on a draft that fits the schema (docs/specs/deep-read.md, "Checks in code"):
 * what a model need not judge. The structure (anchors in their places, one lens per graded topic, unique ids, a
 * table row's cells against its columns, a diagram's edges against its nodes); every equation through KaTeX; every
 * figure and table cited against the paper; every location (`where`) naming a place the paper has; and every
 * number given as the paper's against the paper's text (`numbers.ts`). What the paper has is the index's to say
 * (`places.ts`).
 *
 * A check refuses only on evidence: a place a LaTeX source's own numbering rules out, a number the paper's text
 * lacks. What code cannot settle either way (a PDF's sections, an equation or a page, a label under numbering the
 * source sets itself) is neither passed in silence nor refused: it goes to the reviewer by name
 * (`Checked.unverified`).
 *
 * A number the paper's text lacks passes when its sentence cites a figure this session has an image of: it was read
 * off a plot, and the reviewer checks it against that image (`Checked.plots`).
 *
 * Reading LaTeX and prose this way has no end of cases it gets wrong, so no refusal of a place or a number is final:
 * the writer may stand by an item it is sure of (`CheckContext.standing`), which then goes to the reviewer by name
 * instead (`Checked.stood`).
 */

import katex from "katex";
import { numbersIn, plainText } from "./numbers.ts";
import { cites, figureLabel, labelKey, sectionCites, type Answer, type PaperIndex } from "./places.ts";
import type { Draft, DraftBlock, DraftDiagram, DraftSection } from "./read.ts";
import { amongErrors, isWebUrl, type Found } from "./schema.ts";

export interface CheckContext {
  /** The places and numbers code refused that the writer stands by, each as the refusal quotes it ("Table 2",
   * "0.00005"): the reviewer's to check instead. */
  standing?: readonly string[];
  /** The paper's places and numbers. */
  index: PaperIndex;
  found: Found;
  /** The topics the read is written for, as a lens names each (`Lens.topic`): one lens each. */
  topics: readonly string[];
  /** The figures this session made (`figure()`, `crop()`), by name, each with the paper's label it was made as
   * when the writer gave one. */
  figures: ReadonlyMap<string, { readonly label?: string }>;
}

export interface Checked {
  errors: string[];
  /** The figures, by name, that the read cites for values the paper's text lacks: the reviewer is shown each. */
  plots: string[];
  /** The places cited that code can neither confirm nor rule out, as cited: the reviewer's to check. */
  unverified: string[];
  /** The places and numbers code refused that the writer stood by, when any: the reviewer's to check. */
  stood?: string[];
}

/** How an item is compared with one the writer stands by. */
const quoted = (item: string): string => item.replace(/\s+/g, " ").trim().toLowerCase();

/** Diagram nodes stay tappable on the phone. */
export const MIN_NODE_HEIGHT = 40;
/** How many of a kind of error are listed before the rest are counted. */
const MAX_LISTED = 40;

// ── the read's texts ────────────────────────────────────────────────────────────────────────────────

/** One string of the read. `inline`: it takes the inline markup, so its $tex$ is parsed. `numbers`: its numbers are
 * given as the paper's, so they are checked. A number the paper's text lacks may have been read off a plot: off
 * `figure` (by name), which the whole text is about, as a figure's own caption is, and any figure that text cites;
 * or off the paper's figures the text is said to be built from (`figures`, by label), as a table's cells are whose
 * caption cites one; or off a figure its own sentence cites. `cut`: a table's caption, whose counts of the rows
 * and columns kept are the read's.
 *
 * Not checked for numbers: the read's own presentation (a section's title and id, an `h3`, a fact's name, a
 * figure's number), the reading time, and Related work, whose numbers are other papers'. The reviewer's prompt
 * names every exemption (`prompt.ts`), so it checks those itself. */
interface Unit {
  path: string;
  text: string;
  inline?: boolean;
  numbers?: boolean;
  where?: boolean;
  figure?: string;
  figures?: string[];
  cut?: boolean;
  /** Related work's: the places it cites are other papers', not looked up in this one. */
  theirs?: boolean;
}

/** The glance fact that is the read's own: the reading time. */
const READING_TIME = /^read(ing)?( time)?$/i;

type Fact = { name: string; text?: string; tags?: { text: string }[] };
/** A fact's text and tags, which give the paper's numbers like any prose. */
function* factUnits(facts: readonly Fact[], at: string): Generator<Unit> {
  for (const [i, fact] of facts.entries()) {
    const numbers = !READING_TIME.test(fact.name.trim());
    // a fact's name is the read's own label, but a place it cites is the paper's
    yield { path: `${at}/facts/${i}/name`, text: fact.name };
    if (fact.text !== undefined) yield { path: `${at}/facts/${i}/text`, text: fact.text, inline: true, numbers };
    for (const [j, tag] of (fact.tags ?? []).entries()) yield { path: `${at}/facts/${i}/tags/${j}/text`, text: tag.text, numbers };
  }
}

function* blockUnits(b: DraftBlock, at: string): Generator<Unit> {
  const prose = { inline: true, numbers: true };
  switch (b.type) {
    case "p":
      yield { path: `${at}/text`, text: b.text, ...prose };
      break;
    case "why":
      // the question is plain text, in view while the box is folded: its numbers and cites are checked as prose's
      if (b.question !== undefined) yield { path: `${at}/question`, text: b.question, numbers: true };
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
      // The equation's own TeX is notation, not values given as the paper's: indices, exponents and the constants
      // of a formula (the 2 of x^2, the 1 of 1 - p) are rarely written the same way in the paper's source, so
      // checking them would only reject correct formulas. KaTeX checks that it parses; the caption is prose.
      if (b.caption !== undefined) yield { path: `${at}/caption`, text: b.caption, ...prose };
      break;
    case "fig":
      // a figure's caption gives what the figure shows: its values are the plot's; its number is looked up in check()
      yield { path: `${at}/caption`, text: b.caption, ...prose, figure: b.name };
      break;
    case "xp":
      for (const key of ["question", "did", "found", "means", "not_shown"] as const) {
        const value = b[key];
        if (value !== undefined) yield { path: `${at}/${key}`, text: value, ...prose };
      }
      break;
    case "table": {
      if (b.caption !== undefined) yield { path: `${at}/caption`, text: b.caption, ...prose, cut: true };
      // a figure the caption cites is where the cells were read off
      const figures = b.caption === undefined ? [] : cites(plainText(b.caption)).filter((c) => c.kind === "figure").map((c) => c.label);
      const cells = { numbers: true, ...(figures.length ? { figures } : {}) };
      for (const [i, column] of b.columns.entries()) yield { path: `${at}/columns/${i}`, text: column, ...cells };
      for (const [r, row] of b.rows.entries()) {
        if ("group" in row) {
          yield { path: `${at}/rows/${r}/group`, text: row.group, ...cells };
          continue;
        }
        for (const [c, cell] of row.cells.entries()) {
          const value = typeof cell === "object" ? cell.value : cell;
          yield { path: `${at}/rows/${r}/cells/${c}`, text: String(value), ...cells };
          if (typeof cell === "object") yield { path: `${at}/rows/${r}/cells/${c}/note`, text: cell.note, ...cells };
        }
      }
      break;
    }
    case "diagram":
      if (b.caption !== undefined) yield { path: `${at}/caption`, text: b.caption, ...prose };
      for (const [i, n] of b.nodes.entries()) {
        yield { path: `${at}/nodes/${i}/label`, text: n.label, numbers: true };
        if (n.sub !== undefined) yield { path: `${at}/nodes/${i}/sub`, text: n.sub, numbers: true };
        yield { path: `${at}/nodes/${i}/tip`, text: n.tip, ...prose };
        if (n.where !== undefined) yield { path: `${at}/nodes/${i}/where`, text: n.where, where: true };
      }
      for (const [i, e] of b.edges.entries()) {
        if (e.label !== undefined) yield { path: `${at}/edges/${i}/label`, text: e.label, numbers: true };
        if (e.title !== undefined) yield { path: `${at}/edges/${i}/title`, text: e.title };
        yield { path: `${at}/edges/${i}/tip`, text: e.tip, ...prose };
        if (e.where !== undefined) yield { path: `${at}/edges/${i}/where`, text: e.where, where: true };
      }
      break;
  }
}

function* sectionUnits(s: DraftSection, at: string): Generator<Unit> {
  const prose = { inline: true, numbers: true };
  // the section's name is the read's own label, but a place it cites is the paper's
  yield { path: `${at}/title`, text: s.title, ...(s.kind === "related" ? { theirs: true } : {}) };
  switch (s.kind) {
    case "glance":
      yield { path: `${at}/claim`, text: s.claim, ...prose };
      for (const [i, point] of s.points.entries()) yield { path: `${at}/points/${i}`, text: point, ...prose };
      yield* factUnits(s.facts, at);
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
      // other papers' numbers and places are not this paper's
      yield { path: `${at}/takeaway`, text: s.takeaway, inline: true, theirs: true };
      break;
    case "repro":
      // what the paper says of its code, data and compute is the paper's, numbers included
      yield { path: `${at}/takeaway`, text: s.takeaway, ...prose };
      yield* factUnits(s.facts, at);
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

/** A figure or table numbered in Roman numerals (IEEEtran's tables), or a list of them, which code does not look up. */
const ROMAN_FLOATS = /\b(?i:fig(?:ure)?s?\.?|tables?|tabs?\.)\s*[IVX]{1,6}(?:\s*(?:,|and|&|–|—|-|to)\s*[IVX]{1,6})*\b(?!\/)/g;
/** How a table's caption says the read cut the paper's table, which the spec asks it to say: the rows and columns
 * kept, counted ("11 of its 12 rows", "the first 5 columns"). The counts are the read's own. */
const CUT_COUNTS = /(?<![\d.,])\d+(?:\s+of\s+(?:(?:its|the|all)\s+)?\d+)?\s+(?:[a-z-]+\s+)?(?:rows?|columns?)\b/gi;

// What a number is no value of: a place in the paper, an arXiv id, a link.
const LOCATION = /(?:§+\s*|\b(?:Sections?|Secs?\.?|Appendi(?:x|ces)|Apps?\.?|Eqs?\.?|Equations?|Algorithms?|Algs?\.?|Theorems?|Lemmas?|Propositions?|Corollar(?:y|ies)|Definitions?|Footnotes?|Listings?|Pages?|pp?\.|Lines?)(?:\s|~)*)\(?(?:[A-Z]\.?)?\d+(?:\.\d+)*[a-z]?\)?(?:\s*(?:,|and|&|–|—|-|to)\s*§*\s*\(?(?:[A-Z]\.?)?\d+(?:\.\d+)*[a-z]?\)?)*/gi;
// what is no value: places, arXiv ids, links, and commit hashes (9550e8c), whose leading digits read as a number;
// a number in scientific notation (1.234567e-3, 10000e3) or with a unit letter (100000B) is a value, not a hash,
// unless it is named one ("commit 1234567", "the hash is 12345e6", "digest: 7654321"; "hash 1000000 inputs" is a count)
const NOT_VALUES = [LOCATION, /\b\d{4}\.\d{4,5}(?:v\d+)?\b/g, /\]\(https?:\/\/[^\s()]+\)/g, /https?:\/\/\S+/g, /(?<![\d.])\b(?!\d+(?:e[-+−]?\d+|[a-f])\b)(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{7,64}\b/gi, /\b(?:commit(?::\s*|\s+(?:is\s+)?)|(?:hash|digest)(?::\s*|\s+is\s+))[`"'“]?[0-9a-f]{7,64}\b/gi];
/** A whole number that is a year, which a read may give of its own knowledge: one a paper or its prior work could
 * carry, up to next year's. 2048 is a number like any other. */
const FIRST_YEAR = 1950;
const isYear = (n: { value: number; bare: boolean; text: string }) => n.bare && /^\d{4}$/.test(n.text) && n.value >= FIRST_YEAR && n.value <= new Date().getFullYear() + 1;

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
  const { index } = ctx;
  // The figures this session has an image of, by the paper's label: as each was made, or as a fig block shows it.
  const imaged = new Map<string, Set<string>>();
  const image = (label: string, name: string) => imaged.set(labelKey(label), (imaged.get(labelKey(label)) ?? new Set()).add(name));
  for (const [name, made] of ctx.figures) if (made.label !== undefined) image(made.label, name);
  const plots = new Set<string>();
  const unverified = new Set<string>();
  const absent = new Map<string, string[]>();
  const unseen = new Map<string, { texts: string[]; figures: Set<string> }>();
  const standing = new Set((ctx.standing ?? []).map(quoted));
  const stood = new Set<string>();
  /** Whether the writer stands by an item code would refuse, which then goes to the reviewer. */
  const standsBy = (item: string): boolean => standing.has(quoted(item)) && Boolean(stood.add(item));
  /** What the index says of a place cited at `path`: refused where the paper has none, the reviewer's where it
   * cannot tell. */
  const settle = (path: string, name: string, answer: Answer): void => {
    if (answer === "unknown") unverified.add(name);
    else if (answer !== "there" && !standsBy(name)) errors.push(`${path}: cites ${name}, but ${answer.no}`);
  };

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
          const made = ctx.figures.get(b.name);
          if (!made) errors.push(`${here}/name: this session made no figure "${b.name}"; figure() or crop() makes one under the name you give it`);
          // the number is the paper's however it is written ("Figure 6", "6"); one with no number in it cannot be looked up
          const label = figureLabel(b.number);
          if (label === undefined) unverified.add(`the figure numbered "${b.number}"`);
          else {
            settle(`${here}/number`, `Figure ${label}`, index.float("figure", label));
            if (made?.label !== undefined && labelKey(made.label) !== labelKey(label)) errors.push(`${here}/number: "${b.number}", but figure "${b.name}" was made as the paper's Figure ${made.label}`);
            else if (made) image(label, b.name);
          }
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
      // Related work's figures and tables are other papers'
      if (!unit.theirs) for (const c of cited) settle(unit.path, `${c.kind === "figure" ? "Figure" : "Table"} ${c.label}`, index.float(c.kind, c.label));
      // the paper's places in a location, and in prose given as the paper's (Related work cites other papers'). Prose
      // may name another paper's section, or read as one ("5 sec. 12 probes"): what the paper may lack there is the
      // reviewer's, and only a location is refused.
      if (!unit.theirs) {
        const look = (name: string, answer: Answer) => (unit.where ? settle(unit.path, name, answer) : answer !== "there" && unverified.add(name));
        const sections = sectionCites(text);
        for (const c of sections) {
          if (c.label !== undefined) look(c.appendix ? `Appendix ${c.label}` : `§${c.label}`, index.section(c.label));
          else look("the appendix", index.appendix());
        }
        // a numbered place code does not look up (an equation, an algorithm, a theorem, a footnote, a page, a line):
        // the places whose numbers are no values, but for sections and appendices, and Roman floats. Whether the
        // paper has it is the reviewer's to check, by name.
        const others = [...[...text.matchAll(LOCATION)].map((m) => m[0]).filter((m) => !/^(?:§|sec|app)/i.test(m)), ...[...text.matchAll(ROMAN_FLOATS)].map((m) => m[0])].map((m) => m.replace(/\s+/g, " "));
        for (const other of others) unverified.add(other);
        // nothing in a location has the form of a place, and no heading is named: no place where the paper's
        // headings are all known, and the reviewer's to judge where they are not
        if (unit.where && cited.length + sections.length + others.length === 0 && !index.named(text)) {
          if (index.outlined && !standsBy(unit.text)) errors.push(`${unit.path}: "${unit.text}" names no place in the paper: give a numbered section, figure, table, appendix, equation or page of the paper, or a section by its title`);
          else unverified.add(`the location "${unit.text}"`);
        }
      }
      if (!unit.numbers) continue;
      // blank out what is no value, keeping every index
      let values = text;
      for (const c of cited) values = values.slice(0, c.index) + " ".repeat(c.end - c.index) + values.slice(c.end);
      for (const pattern of unit.cut ? [...NOT_VALUES, CUT_COUNTS] : NOT_VALUES) values = values.replace(pattern, (m) => " ".repeat(m.length));
      const missing: string[] = [];
      for (const n of numbersIn(values)) {
        if ((n.bare && n.value < 10) || isYear(n) || index.numbers.has(n)) continue;
        // a figure's own caption; a figure the session did not make has its error already
        if (unit.figure !== undefined && !ctx.figures.has(unit.figure)) continue;
        // read off a plot: the figure the text captions with every figure it cites, the figures the whole text is
        // built from, or those its sentence cites. Every one of them needs its image, since the value may be any
        // one's.
        const labels = unit.figures ?? cites(unit.figure === undefined ? sentence(text, n.index) : text).filter((c) => c.kind === "figure").map((c) => c.label);
        const lacking = labels.filter((label) => !imaged.has(labelKey(label)));
        if (unit.figure === undefined && labels.length === 0) {
          if (!standsBy(n.text)) missing.push(n.text);
        } else if (lacking.length === 0 || standsBy(n.text)) {
          // a value stood by keeps the images the session has of the figures it cites, for the reviewer
          if (unit.figure !== undefined) plots.add(unit.figure);
          for (const label of labels) for (const name of imaged.get(labelKey(label)) ?? []) plots.add(name);
        } else {
          const entry = unseen.get(unit.path) ?? { texts: [], figures: new Set<string>() };
          entry.texts.push(n.text);
          for (const label of lacking) entry.figures.add(`Figure ${label}`);
          unseen.set(unit.path, entry);
        }
      }
      if (missing.length) absent.set(unit.path, missing);
    }
  }

  const are = (texts: string[]) => `${[...new Set(texts)].join(", ")} ${new Set(texts).size === 1 ? "is" : "are"}`;
  const listed = [
    ...[...absent].map(([path, texts]) => `${path}: ${are(texts)} not in the paper's text`),
    ...[...unseen].map(([path, { texts, figures }]) => `${path}: ${are(texts)} not in the paper's text, and this session has no image of ${[...figures].join(", ")} to check ${new Set(texts).size === 1 ? "it" : "them"} against`),
  ];
  if (listed.length > MAX_LISTED) listed.splice(MAX_LISTED, Infinity, `… and ${listed.length - MAX_LISTED} more places with numbers the paper's text lacks`);
  if (listed.length)
    listed.push(
      "Numbers: give each number as the paper's text states it (0.34 and 34% are the same; rounding is fine). A value read off a plot " +
        'cites its figure in the same sentence, and this session has that figure\'s image: make it with figure(file, name, "Figure 6") or ' +
        'crop(page, box, name, "Figure 6"), or show it in a fig block. Your own arithmetic (a difference, a ratio the paper does not ' +
        "state) and what you found outside the paper are given through the paper's numbers or in words.",
    );
  errors.push(...listed, ...amongErrors(read.among_collection, ctx.found));
  for (const [i, n] of (read.process_notes ?? []).entries()) if (n.url !== undefined && !isWebUrl(n.url)) errors.push(`/process_notes/${i}/url: not an absolute http(s) URL`);
  return { errors, plots: [...plots], unverified: [...unverified], ...(stood.size ? { stood: [...stood] } : {}) };
}
