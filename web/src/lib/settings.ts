/**
 * Settings' pages and the pure rules behind them (DESIGN.md, "Settings"): the index in its groups, what each
 * page's hint says, how a share is coloured, a spend bar's geometry, and the shapes of the endpoints the API
 * leaves untyped (`/quality`, `/runs`, `/calibrate`).
 */
import type { Schemas } from "@/api/client";
import { admits } from "./rarity";

export type Page = "overview" | "needs" | "calibrate" | "misses" | "topics" | "taxonomy" | "runs" | "integrations" | "configuration";

export const GROUPS: { name: string; pages: { id: Page; name: string }[] }[] = [
  {
    name: "Reading",
    pages: [
      { id: "overview", name: "Overview" },
      { id: "needs", name: "Needs you" },
      { id: "calibrate", name: "Calibrate" },
      { id: "misses", name: "Misses" },
    ],
  },
  {
    name: "Following",
    pages: [
      { id: "topics", name: "Topics" },
      { id: "taxonomy", name: "Taxonomy" },
    ],
  },
  {
    name: "System",
    pages: [
      { id: "runs", name: "Runs and cost" },
      { id: "integrations", name: "Integrations" },
      { id: "configuration", name: "Configuration" },
    ],
  },
];

export function isPage(s: string): s is Page {
  return GROUPS.some((g) => g.pages.some((p) => p.id === s));
}

// -- the untyped endpoints -----------------------------------------------------------------------------------------

export type Fit = Schemas["Fit"];
export type LabelValue = Schemas["LabelValue"];
export type Opinion = Schemas["Opinion"];

/** A split or escalation from last night (`store.quality.splits_since`). */
export type Split = {
  paper_id: string;
  topic: string;
  depth: Schemas["Depth"];
  fit: Fit;
  reason: string;
  split: boolean;
  escalated: boolean;
  opinions: Record<string, Opinion>;
  recorded_at: string;
  title: string;
  label: { value: LabelValue } | null;
  /** What settled it on this visit (client-side; DESIGN.md: a settled item keeps its result in place). */
  settled?: string;
};

/** A paper stuck after three failed runs of a step (`store.failures.stuck`). */
export type Stuck = { paper_id: string; revision: number; title: string; failed_runs: number; last_error: string | null; failed_at: string; topic?: string | null; settled?: string };

/** A full-text pass waiting on the owner (`store.fulltext.stuck`): failed three runs, or awaiting its text over
 * a week. */
export type TextPassStuck = {
  id: number;
  paper_id: string;
  revision: number;
  title: string;
  kind: "brief" | "weight";
  state: "failed" | "awaiting_text";
  failed_runs: number;
  last_error: string | null;
  queued_at: string;
  settled?: string;
};

/** A digest whose writer call failed (`store.digests.failed`): the next run writes it again, or the owner now. A
 * weekly digest's `day` is its Monday. */
export type FailedDigest = { id: number; topic: string; period: "day" | "week"; day: string; error: string | null; written_at: string; settled?: string };

export type Spend = { provider: string; spent: number; projected: number; budget: number | null; over: boolean };

export type Recall = {
  cohort: string;
  topic: string;
  positives: number;
  classification: { hits: number; n: number };
  membership: { hits: number; n: number };
  grading: { hits: number; n: number };
  end_to_end: { hits: number; n: number };
  grading_failed: number;
  negatives: number;
};

export type Quality = {
  night: { run: number | null; error: string | null; splits: Split[]; awaiting_classification: Stuck[]; analysis_failed: Stuck[]; ungraded: Stuck[]; fulltext: TextPassStuck[]; digests: FailedDigest[] };
  cost: Spend[];
  recall: { eval_run: number; started_at: string; finished_at: string | null; cost: number; recall: Recall[] } | null;
  calibration: { topic: string; precision: number | null; precision_n: number; dropped: number | null; dropped_n: number }[];
  matters_no_topic: { paper_id: string; note: string | null; at: string; title: string }[];
  collected: { since: string; thumbs: number; papers: number } | Record<string, unknown>;
  reactions: { topic: string; reason: Schemas["ReactionReason"] | null; n: number }[];
};

export type Run = {
  id: number;
  kind: string;
  started_at: string;
  finished_at: string | null;
  cost: number | string;
  error: string | null;
  counts: { classified?: number; classification_failed?: number; analysed?: number; analysis_failed?: number; graded?: number; grading_failed?: number; digests_written?: number; digests_empty?: number; digests_failed?: number; calls?: Record<string, { cost: number; calls: number }> } | null;
  fetched: number;
  inserted: number;
  updated: number;
};

/** The digests a night wrote; null for a night that recorded none (before digests, or none due). */
export function digests(r: Run): number | null {
  const c = r.counts ?? {};
  return c.digests_written ?? (c.digests_empty != null || c.digests_failed != null ? 0 : null);
}

export type SessionRow = { id: number; seed: number; drawn_at: string; papers: number; answered: number; differ: number };

export type SessionItem = {
  position: number;
  paper_id: string;
  topic: string;
  why: "above" | "below" | "split" | "miss" | "reaction";
  /** The pipeline's Fit on the drawn topic; null while it has no verdict. */
  verdict: Fit | null;
  /** The Fit the owner answered on the drawn topic; null until answered. `fit` holds the current labels. */
  answer: LabelValue | null;
  answered_at: string | null;
  title: string;
  abstract: string;
  authors: string[];
  published: string | null;
  fit: Record<string, LabelValue>;
  appraisal: { value: "matters" | "fine" | "not_for_me" | null; note: string | null } | null;
};

export type Session = { id: number; seed: number; drawn_at: string; items: SessionItem[] };

// -- rules ---------------------------------------------------------------------------------------------------------

export type Waiting = "splits" | "awaiting_classification" | "analysis_failed" | "ungraded" | "fulltext" | "digests";

/** How many things wait on the owner: splits and escalations still unlabelled, the three stuck steps, the
 * full-text passes and the failed digests, less what this visit settled. */
export function waiting(q: Quality): number {
  const n = q.night;
  const open = (xs: { settled?: string }[]) => xs.filter((x) => !x.settled).length;
  return open(n.splits.filter((s) => !s.label)) + open(n.awaiting_classification) + open(n.analysis_failed) + open(n.ungraded) + open(n.fulltext) + open(n.digests);
}

/** Which item settled: its paper, and its topic or pass where a paper can have several; a digest by its id alone. */
export type Settled = { paper_id?: string; topic?: string | null; id?: number };

/** The quality page with one item marked settled, in place: the page and the sidebar read the same copy. `what`
 * undefined unsettles it (a label taken back). */
export function settle(q: Quality, list: Waiting, item: Settled, what: string | undefined): Quality {
  const same = (x: Settled) => (item.paper_id == null || x.paper_id === item.paper_id) && (item.topic == null || x.topic === item.topic) && (item.id == null || x.id === item.id);
  const mark = <T extends Settled>(xs: T[]): T[] => xs.map((x) => (same(x) ? { ...x, settled: what } : x));
  const night = { ...q.night };
  if (list === "splits") night.splits = mark(night.splits);
  else if (list === "fulltext") night.fulltext = mark(night.fulltext);
  else if (list === "digests") night.digests = mark(night.digests);
  else night[list] = mark(night[list]);
  return { ...q, night };
}

/** Last night's splits and escalations still waiting for a label, or settled on this visit, in their two groups.
 * An Area escalation whose graders also disagreed carries both flags; it is an escalation (the less inclusive Fit
 * stands), as the paper's page reads it. */
export function openSplits(splits: Split[]): { splits: Split[]; escalations: Split[] } {
  const open = splits.filter((s) => !s.label || s.settled);
  return { splits: open.filter((s) => s.split && !s.escalated), escalations: open.filter((s) => s.escalated) };
}

/** The quality page reconciled with the paper's own page after a write there: an item whose open state the
 * refetched paper no longer carries is marked settled in place (a split with the label it got), and one whose
 * state is back (a label taken back) waits again. The sidebar's count follows, and the way back to Needs you finds
 * the row where it was. */
export function settleFromPaper(q: Quality, paper: Schemas["Paper"]): Quality {
  const states = paper.states ?? [];
  const has = (kind: Schemas["OpenState"]["kind"], topic?: string | null, id?: number) => states.some((s) => s.kind === kind && (topic == null || s.topic === topic) && (id == null || s.pass_id === id));
  const labelOf = (topic: string) => paper.topics.find((t) => t.topic.slug === topic)?.label?.value;
  const done = "Settled on the paper's page.";
  // The item as the paper's page leaves it: settled with `what` while its state is gone, open while it is back.
  const read = <T extends { paper_id: string; settled?: string }>(x: T, open: boolean, what: string | undefined): T => {
    if (x.paper_id !== paper.id) return x;
    if (open) return x.settled ? { ...x, settled: undefined } : x;
    return x.settled || !what ? x : { ...x, settled: what };
  };
  const night = { ...q.night };
  // A split waits again when its state is back (the label taken back, whenever it was given); labelled, it shows
  // the label the page now carries, a changed one included, where the visit lists it.
  night.splits = night.splits.map((s) => {
    if (s.paper_id !== paper.id) return s;
    if (has(s.escalated ? "escalated" : "split", s.topic)) return s.settled || s.label ? { ...s, settled: undefined, label: null } : s;
    const label = labelOf(s.topic);
    return label && (!s.label || s.settled) && s.settled !== label ? { ...s, settled: label } : s;
  });
  night.awaiting_classification = night.awaiting_classification.map((s) => read(s, has("awaiting_classification"), done));
  night.analysis_failed = night.analysis_failed.map((s) => read(s, has("analysis_stopped"), done));
  night.ungraded = night.ungraded.map((s) => read(s, has("ungraded", s.topic), done));
  night.fulltext = night.fulltext.map((s) => read(s, has(s.state === "failed" ? "fulltext_failed" : "awaiting_text", null, s.id), done));
  return { ...q, night };
}

/** The Fits an answer sends: the drawn topic's, and any Focus topic's the owner changed on this screen. A label
 * left as it was is not written again (writing renews its time, and the newest word wins). */
export function answerFits(draft: Record<string, LabelValue>, item: SessionItem, focus: Set<string>): Record<string, LabelValue> {
  return Object.fromEntries(Object.entries(draft).filter(([slug, fit]) => slug === item.topic || (focus.has(slug) && fit !== item.fit[slug])));
}

export type Level = "ok" | "warn" | "bad";

/** A share's colour: cyan at or above `good`, amber at or above `ok`, red under; `hi` false reads the other way
 * (a lower share is better). */
export function level(x: number, { hi = true, good = 0.9, ok = 0.8 } = {}): Level {
  if (hi) return x >= good ? "ok" : x >= ok ? "warn" : "bad";
  return x <= good ? "ok" : x <= ok ? "warn" : "bad";
}

/** This month's spend against its budget. Budgets are per provider, so once any provider has one the spend set
 * against it is the budgeted providers' only, and `unbudgeted` is the rest; with no budget, `spent` is all of it. */
export function spendTotals(cost: Spend[]): { spent: number; budget: number; unbudgeted: number } {
  const sum = (xs: Spend[]) => xs.reduce((s, c) => s + c.spent, 0);
  const budgeted = cost.filter((c) => c.budget != null);
  if (!budgeted.length) return { spent: sum(cost), budget: 0, unbudgeted: 0 };
  return { spent: sum(budgeted), budget: budgeted.reduce((s, c) => s + (c.budget ?? 0), 0), unbudgeted: sum(cost.filter((c) => c.budget == null)) };
}

/** The spend bar's three marks as shares of its width: month to date, the projection, the budget tick. The widest
 * of the budget and the projection fills the bar. */
export function spendBar(s: Spend): { spent: number; projected: number; budget: number | null } {
  const top = Math.max(s.budget ?? 0, s.projected, s.spent) || 1;
  return { spent: s.spent / top, projected: s.projected / top, budget: s.budget == null ? null : s.budget / top };
}

/** Money as the API gives it: a number, or a decimal as a string. */
export const usd = (x: number | string) => `$${Number(x).toFixed(2)}`;

/** "41 min" or "2 h 05" between two instants; "" while still running. */
export function took(start: string, end: string | null): string {
  if (!end) return "";
  const m = Math.max(0, Math.round((new Date(end).getTime() - new Date(start).getTime()) / 60_000));
  if (m < 60) return `${m} min`;
  return `${Math.floor(m / 60)} h ${String(m % 60).padStart(2, "0")}`;
}

/** The pipeline's side of a Calibrate item: a paper drawn from above relevant or from below. Other draws (splits,
 * misses, thumbs) carry no side. */
export function pipelineSide(item: SessionItem): "relevant or above" | "below" | null {
  return item.why === "above" ? "relevant or above" : item.why === "below" ? "below" : null;
}

/** Whether the owner's answer differs from the pipeline's: another Fit than its verdict's, or, while the verdict
 * is gone, the other side from the draw's (the API's `DIFFERS`, which the sessions list counts). */
export function differs(item: SessionItem): boolean {
  const fit = item.answer;
  if (!fit || fit === "unsure") return false;
  if (item.verdict) return fit !== item.verdict;
  const side = pipelineSide(item);
  return side === "relevant or above" ? !admits(fit) : side === "below" ? admits(fit) : false;
}

/** The first line of the re-run offer after a save (DESIGN.md, "The re-run offer"). */
export function savedLine(before: Schemas["Topic"] | null, after: Schemas["Topic"], version: number): string {
  if (!before) return "Followed. Its papers show ungraded until a run grades them.";
  if (before.depth !== after.depth) return "Saved. Topic pages and rarity follow the new depth now; the Fits already given stand until a re-run is asked for.";
  const read = (t: Schemas["Topic"]) => JSON.stringify([t.subject, t.rubric ?? "", t.interests ?? "", t.keywords ?? []]);
  if (read(before) !== read(after)) return `Saved as version ${version}. It applies to papers classified or graded from now on.`;
  return "Saved.";
}

/** `litwatch.toml` as text from the masked configuration the API returns: one table per section, nested tables
 * dotted, an array of tables as repeated headers, values in TOML's forms. */
export function toToml(value: Record<string, unknown>): string {
  const blocks: string[] = [];
  const walk = (table: Record<string, unknown>, path: string[], header: string) => {
    const lines: string[] = [];
    const tables: [string, Record<string, unknown>][] = [];
    const arrays: [string, Record<string, unknown>[]][] = [];
    for (const [k, v] of Object.entries(table)) {
      if (isRecord(v)) tables.push([k, v]);
      else if (Array.isArray(v) && v.length && v.every(isRecord)) arrays.push([k, v as Record<string, unknown>[]]);
      else lines.push(`${k} = ${tomlValue(v)}`);
    }
    if (lines.length || (header && !tables.length && !arrays.length)) blocks.push([header, ...lines].filter(Boolean).join("\n"));
    for (const [k, v] of tables) walk(v, [...path, k], `[${[...path, k].join(".")}]`);
    for (const [k, rows] of arrays) for (const row of rows) walk(row, [...path, k], `[[${[...path, k].join(".")}]]`);
  };
  walk(value, [], "");
  return blocks.join("\n\n") + "\n";
}

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

function tomlValue(v: unknown): string {
  if (v === null || v === undefined) return '""';
  if (typeof v === "string") return JSON.stringify(v);
  if (Array.isArray(v)) return `[${v.map(tomlValue).join(", ")}]`;
  return String(v);
}
