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
};

/** A paper stuck after three failed runs of a step (`store.failures.stuck`). */
export type Stuck = { paper_id: string; revision: number; title: string; failed_runs: number; last_error: string | null; failed_at: string; topic?: string | null };

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
  night: { run: number | null; error: string | null; splits: Split[]; awaiting_classification: Stuck[]; analysis_failed: Stuck[]; ungraded: Stuck[] };
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
  counts: { classified?: number; analysed?: number; graded?: number; grading_failed?: number; analysis_failed?: number; calls?: Record<string, { cost: number; calls: number }> } | null;
  fetched: number;
  inserted: number;
  updated: number;
};

export type SessionRow = { id: number; seed: number; drawn_at: string; papers: number; answered: number };

export type SessionItem = {
  position: number;
  paper_id: string;
  topic: string;
  why: "above" | "below" | "split" | "miss" | "reaction";
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

/** How many things wait on the owner: splits and escalations still unlabelled, and the three stuck steps. */
export function waiting(q: Quality): number {
  const n = q.night;
  return n.splits.filter((s) => !s.label).length + n.awaiting_classification.length + n.analysis_failed.length + n.ungraded.length;
}

export type Level = "ok" | "warn" | "bad";

/** A share's colour: cyan at or above `good`, amber at or above `ok`, red under; `hi` false reads the other way
 * (a lower share is better). */
export function level(x: number, { hi = true, good = 0.9, ok = 0.8 } = {}): Level {
  if (hi) return x >= good ? "ok" : x >= ok ? "warn" : "bad";
  return x <= good ? "ok" : x <= ok ? "warn" : "bad";
}

/** This month's spend and budget over every provider. */
export function spendTotals(cost: Spend[]): { spent: number; budget: number } {
  return { spent: cost.reduce((s, c) => s + c.spent, 0), budget: cost.reduce((s, c) => s + (c.budget ?? 0), 0) };
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

/** Whether the owner's Fit on the drawn topic sits on the other side from the pipeline's draw. */
export function differs(item: SessionItem): boolean {
  const fit = item.fit[item.topic];
  if (!fit || fit === "unsure") return false;
  const side = pipelineSide(item);
  return side === "relevant or above" ? !admits(fit) : side === "below" ? admits(fit) : false;
}

/** The first line of the re-run offer after a save (DESIGN.md, "The re-run offer"). */
export function savedLine(before: Schemas["Topic"] | null, after: Schemas["Topic"], version: number): string {
  if (!before) return "Followed. Its papers show ungraded until a run grades them.";
  if (before.depth !== after.depth) return "Saved. Topic pages and rarity follow the new depth now; its papers are graded again on the next run.";
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
