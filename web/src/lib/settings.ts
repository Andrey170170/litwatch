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

/** A split, escalation or central or related grade from one lab from last night (`store.quality.splits_since`). */
export type Split = {
  paper_id: string;
  topic: string;
  /** The paper revision the verdict is on (the current one: `splits_since` lists no older text's). */
  revision: number;
  depth: Schemas["Depth"];
  fit: Fit;
  reason: string;
  split: boolean;
  escalated: boolean;
  /** One Focus grader refused throughout; the other's Fit stands (`opinions` holds its answer alone). */
  one_lab: boolean;
  opinions: Record<string, Opinion>;
  recorded_at: string;
  title: string;
  label: { value: LabelValue } | null;
  /** What settled it on this visit (client-side; DESIGN.md: a settled item keeps its result in place). */
  settled?: string;
};

/** A paper stuck after three failed runs of a step, or one every model refused (`store.failures.stuck`; `refused`
 * names the roles whose whole list refused, `stopped_by_refusal` whether that stopped it: a Focus side can refuse
 * throughout while the other fails three runs). `acknowledged_at`: when the owner acknowledged it, as for passes and
 * digests (it no longer counts in the run status, and folds away). */
export type Stuck = { paper_id: string; revision: number; title: string; failed_runs: number; last_error: string | null; failed_at: string; topic?: string | null; refused?: string[]; stopped_by_refusal?: boolean; acknowledged_at?: string | null; settled?: string };

/** A full-text pass waiting on the owner (`store.fulltext.stuck`): failed three runs or refused by every model
 * (`refused`), or awaiting its text over a week. `since`: since when it waits, which names this failure for
 * Acknowledge. */
export type TextPassStuck = {
  id: number;
  paper_id: string;
  revision: number;
  title: string;
  kind: "brief" | "weight" | "deep";
  state: "failed" | "awaiting_text";
  failed_runs: number;
  last_error: string | null;
  refused?: boolean;
  queued_at: string;
  since: string;
  acknowledged_at?: string | null;
  settled?: string;
};

/** A digest whose writer call failed (`store.digests.failed`): the next run writes it again, or the owner now. A
 * weekly digest's `day` is its Monday. */
export type FailedDigest = {
  id: number;
  topic: string;
  period: "day" | "week";
  day: string;
  error: string | null;
  written_at: string;
  acknowledged_at?: string | null;
  settled?: string;
  /** What the last write-again from this visit said while the digest is still to be written (client-side). */
  note?: string;
};

/** A process note on a Deep card (GLOSSARY.md): what the Deep agent could not do, as the model or the service wrote
 * it. `url`, when given, is linked only if it is http(s). */
export type ProcessNote = { kind: "access" | "tool" | "budget" | "input"; source: "model" | "service"; note: string; url?: string };

/** A Deep card with process notes (`store.integrations.deep_notes`): Needs you's Deep notes, which wait on nothing.
 * `written_at` names the card, for Acknowledge: a card written again since is another. */
export type DeepNotes = { paper_id: string; title: string; written_at: string; notes: ProcessNote[]; acknowledged_at?: string | null };

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
  night: { run: number | null; error: string | null; splits: Split[]; awaiting_classification: Stuck[]; analysis_failed: Stuck[]; ungraded: Stuck[]; fulltext: TextPassStuck[]; digests: FailedDigest[]; deep_notes: DeepNotes[] };
  cost: Spend[];
  /** Deep cards' month to date at list prices: the attempts ended, their cost where known, and how many have their
   * usage unknown (the cost is "at least" while any is). */
  deep?: { attempts: number; list_cost: number; unknown: number };
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

/** What a run left undone, for its red-edged cell on Runs and cost and Overview's tile: its error, or the steps
 * that failed on some papers ("20 grading, 1 digest failed"); "" for a clean run. */
export function runFailed(r: Run): string {
  if (r.error) return r.error;
  const c = r.counts ?? {};
  const parts = [
    c.classification_failed ? `${c.classification_failed} classification` : "",
    c.analysis_failed ? `${c.analysis_failed} analysis` : "",
    c.grading_failed ? `${c.grading_failed} grading` : "",
    c.digests_failed ? `${c.digests_failed} digest${c.digests_failed > 1 ? "s" : ""}` : "",
  ].filter(Boolean);
  return parts.length ? `${parts.join(", ")} failed` : "";
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

export type Value = NonNullable<Schemas["Answer"]["value"]>;

// -- rules ---------------------------------------------------------------------------------------------------------

export type Waiting = "splits" | "awaiting_classification" | "analysis_failed" | "ungraded" | "fulltext" | "digests";

/** How many things wait on the owner: splits and escalations still unlabelled, the three stuck steps, the
 * full-text passes and the failed digests, less what this visit settled and what the owner acknowledged. Deep notes
 * wait on nothing and are not counted. */
export function waiting(q: Quality): number {
  const n = q.night;
  const open = (xs: { settled?: string; acknowledged_at?: string | null }[]) => xs.filter((x) => !x.settled && !x.acknowledged_at).length;
  return open(n.splits.filter((s) => !s.label)) + open(n.awaiting_classification) + open(n.analysis_failed) + open(n.ungraded) + open(n.fulltext) + open(n.digests);
}

/** The red groups of Needs you, which Acknowledge marks seen (DESIGN.md, Needs you); the yellow ones wait on a
 * Fit and are not acknowledged. */
export type Red = "awaiting_classification" | "ungraded" | "analysis_failed" | "fulltext" | "digests";
export const RED: Red[] = ["awaiting_classification", "ungraded", "analysis_failed", "fulltext", "digests"];

/** The groups Acknowledge marks seen and the Acknowledged section holds: the red ones and Deep notes. Apart from
 * `waiting()`, which Deep notes never join. */
export type Acknowledgeable = Red | "deep_notes";
export const ACKNOWLEDGEABLE: Acknowledgeable[] = [...RED, "deep_notes"];

/** What an Acknowledge names, and what the API answers it marked (`POST /api/settings/acknowledge`). */
export type Seen = Schemas["Acknowledgement"];

const STEP = { awaiting_classification: "classification", analysis_failed: "analysis", ungraded: "grading" } as const;
type StepList = keyof typeof STEP;

/** Whether a row is to acknowledge: not acknowledged yet, nor settled on this visit (it no longer waits). */
const unseen = (x: { settled?: string; acknowledged_at?: string | null }) => !x.settled && !x.acknowledged_at;

/** What Acknowledge on the `lists` names: their rows the owner sees there now, each as it is shown (the failure
 * shown, by when it failed, so a later failure meanwhile is not acknowledged unseen). */
export function toAcknowledge(q: Quality, lists: Acknowledgeable[]): Seen {
  const n = q.night;
  const steps = (Object.keys(STEP) as StepList[])
    .filter((l) => lists.includes(l))
    .flatMap((l) => n[l].filter(unseen).map((s) => ({ step: STEP[l], paper_id: s.paper_id, revision: s.revision, topic: s.topic ?? null, failed_at: s.failed_at })));
  const passes = lists.includes("fulltext") ? n.fulltext.filter(unseen).map((p) => ({ id: p.id, since: p.since })) : [];
  const digests = lists.includes("digests") ? n.digests.filter(unseen).map((d) => d.id) : [];
  // A card's notes by the card shown: one written again meanwhile is not acknowledged unseen.
  const cards = lists.includes("deep_notes") ? n.deep_notes.filter(unseen).map((c) => ({ paper_id: c.paper_id, written_at: c.written_at })) : [];
  return { steps, passes, digests, cards };
}

/** Whether `seen` names anything. */
export const any = (seen: Seen): boolean => seen.steps.length + seen.passes.length + seen.digests.length + seen.cards.length > 0;

/** How many items the owner acknowledged, which the Acknowledged section holds: the red groups' and Deep notes'. */
export const acknowledgedCount = (q: Quality): number => ACKNOWLEDGEABLE.reduce((k, list) => k + q.night[list].filter((x) => x.acknowledged_at).length, 0);

/** The same instant, however each side wrote it. */
const at = (a: string, b: string) => Date.parse(a) === Date.parse(b);

/** The quality page with what the API answered it marked (`seen`) acknowledged `when`: those rows, as shown, fold
 * away; the rest, and a row settled on this visit, stay where they were. */
export function acknowledged(q: Quality, seen: Seen, when: string): Quality {
  const night = { ...q.night };
  const mark = <T extends { acknowledged_at?: string | null; settled?: string }>(xs: T[], named: (x: T) => boolean): T[] =>
    xs.map((x) => (unseen(x) && named(x) ? { ...x, acknowledged_at: when } : x));
  for (const l of Object.keys(STEP) as StepList[])
    night[l] = mark(night[l], (s) =>
      seen.steps.some((x) => x.step === STEP[l] && x.paper_id === s.paper_id && x.revision === s.revision && (x.topic ?? null) === (s.topic ?? null) && at(x.failed_at, s.failed_at)),
    );
  night.fulltext = mark(night.fulltext, (p) => seen.passes.some((x) => x.id === p.id && at(x.since, p.since)));
  night.digests = mark(night.digests, (d) => seen.digests.includes(d.id));
  night.deep_notes = mark(night.deep_notes, (c) => seen.cards.some((x) => x.paper_id === c.paper_id && at(x.written_at, c.written_at)));
  return { ...q, night };
}

/** Which item settled: its paper, and its topic, revision or pass where a paper can have several; a digest by its
 * id alone. A revision names the text the action was for: a row for an older one is not the item settled. */
export type Settled = { paper_id?: string; topic?: string | null; revision?: number; id?: number };

/** The quality page with one item marked settled, in place: the page and the sidebar read the same copy. A row
 * settled already keeps its word (the paper's page may have settled it first, a 409 come after). */
export function settle(q: Quality, list: Waiting, item: Settled, what: string): Quality {
  const same = (x: Settled) =>
    (item.paper_id == null || x.paper_id === item.paper_id) && (item.topic == null || x.topic === item.topic) && (item.revision == null || x.revision === item.revision) && (item.id == null || x.id === item.id);
  const mark = <T extends Settled & { settled?: string }>(xs: T[]): T[] => xs.map((x) => (same(x) && !x.settled ? { ...x, settled: what } : x));
  const night = { ...q.night };
  if (list === "splits") night.splits = mark(night.splits);
  else if (list === "fulltext") night.fulltext = mark(night.fulltext);
  else if (list === "digests") night.digests = mark(night.digests);
  else night[list] = mark(night[list]);
  return { ...q, night };
}

/** The quality page after a label written on a split, or taken back, wherever it was sent from, for the paper
 * revision the sender showed (a row for another text is not this one): labelled, a row the visit lists (fetched
 * unlabelled, or settled on it) settles with the label, a changed one replacing it, and a row fetched labelled
 * stays off the list; taken back, the split waits again, its label gone whether the visit fetched it labelled or
 * settled it. */
export function labelled(q: Quality, paper_id: string, topic: string, value: LabelValue | null, revision: number): Quality {
  const splits = q.night.splits.map((s) => {
    if (s.paper_id !== paper_id || s.topic !== topic || s.revision !== revision) return s;
    if (!value) return { ...s, settled: undefined, label: null };
    return !s.label || s.settled ? { ...s, settled: value } : s;
  });
  return { ...q, night: { ...q.night, splits } };
}

/** The quality page with what a digest's write-again said while it is still to be written (it failed again, or a
 * job holds the run lock): kept with the row, as a settlement is, for the page shown now or returned to. */
export function noted(q: Quality, id: number, note: string): Quality {
  return { ...q, night: { ...q.night, digests: q.night.digests.map((d) => (d.id === id ? { ...d, note } : d)) } };
}

/** Last night's splits, escalations and grades from one lab still waiting for a label, or settled on this visit, in
 * their three groups. An Area escalation whose graders also disagreed carries both flags; it is an escalation (the
 * less inclusive Fit stands), as the paper's page reads it. */
export function openSplits(splits: Split[]): { splits: Split[]; escalations: Split[]; oneLab: Split[] } {
  const open = splits.filter((s) => !s.label || s.settled);
  return { splits: open.filter((s) => s.split && !s.escalated), escalations: open.filter((s) => s.escalated), oneLab: open.filter((s) => s.one_lab && !s.split && !s.escalated) };
}

/** The paper page's open state for a split, escalation or grade from one lab. */
const splitState = (s: Split) => (s.escalated ? "escalated" : s.split ? "split" : "one_lab");

/** The quality page reconciled with the paper's own page after a write there: an item whose open state the
 * refetched paper no longer carries is marked settled in place (a split with the label it got), and one whose
 * state is back (a label taken back) waits again. Only the rows for the paper's current revision: a row for an
 * older text was not what the page acted on, and its state's absence says nothing about it. The sidebar's count
 * follows, and the way back to Needs you finds the row where it was. */
export function settleFromPaper(q: Quality, paper: Schemas["Paper"]): Quality {
  const states = paper.states ?? [];
  const has = (kind: Schemas["OpenState"]["kind"], topic?: string | null, id?: number) => states.some((s) => s.kind === kind && (topic == null || s.topic === topic) && (id == null || s.pass_id === id));
  const labelOf = (topic: string) => paper.topics.find((t) => t.topic.slug === topic)?.label?.value;
  const done = "Settled on the paper's page.";
  const mine = (x: { paper_id: string; revision: number }) => x.paper_id === paper.id && x.revision === paper.revision;
  // The item as the paper's page leaves it: settled with `what` while its state is gone, open while it is back.
  const read = <T extends { paper_id: string; revision: number; settled?: string }>(x: T, open: boolean, what: string | undefined): T => {
    if (!mine(x)) return x;
    if (open) return x.settled ? { ...x, settled: undefined } : x;
    return x.settled || !what ? x : { ...x, settled: what };
  };
  const night = { ...q.night };
  // A split waits again when its state is back (the label taken back, whenever it was given); labelled, it shows
  // the label the page now carries, a changed one included, where the visit lists it.
  night.splits = night.splits.map((s) => {
    if (!mine(s)) return s;
    if (has(splitState(s), s.topic)) return s.settled || s.label ? { ...s, settled: undefined, label: null } : s;
    const label = labelOf(s.topic);
    return label && (!s.label || s.settled) && s.settled !== label ? { ...s, settled: label } : s;
  });
  night.awaiting_classification = night.awaiting_classification.map((s) => read(s, has("awaiting_classification"), done));
  night.analysis_failed = night.analysis_failed.map((s) => read(s, has("analysis_stopped"), done));
  night.ungraded = night.ungraded.map((s) => read(s, has("ungraded", s.topic), done));
  // A failed Deep pass is the page's `card_failed`, any other failed pass its `fulltext_failed`.
  const passState = (s: TextPassStuck) => (s.state !== "failed" ? "awaiting_text" : s.kind === "deep" ? "card_failed" : "fulltext_failed");
  night.fulltext = night.fulltext.map((s) => read(s, has(passState(s), null, s.id), done));
  return { ...q, night };
}

/** What the owner changed on a Calibrate screen, over the item as last read: a Fit per topic, the value, the note.
 * The item refreshes under the edits; a field edited stands over what the refresh brought, one untouched follows
 * it. */
export type Edits = { fit: Record<string, LabelValue>; value?: Value | null; note?: string };

/** The answer a Calibrate screen sends for the item as it stands and the edits on it. `fit` holds the Fits changed
 * here on Focus topics or the drawn one (a label left as it was is not written again: writing renews its time,
 * and the newest word wins) and, on a first answer, the drawn topic's Fit as shown; `value` and `note` are the
 * appraisal as shown. `edited`: something was changed. `complete`: a first answer has the drawn topic's Fit, which
 * it needs; after it the recorded answer stands, so an edit of the appraisal alone writes no label. The recorded
 * answer is history, not a label: a label taken back since is not written again from it. */
export function answerOf(item: SessionItem, edits: Edits, focus: Set<string>): { answer: Schemas["Answer"]; edited: boolean; complete: boolean } {
  const fit = Object.fromEntries(Object.entries(edits.fit).filter(([slug, f]) => (slug === item.topic || focus.has(slug)) && f !== item.fit[slug]));
  const value = edits.value === undefined ? (item.appraisal?.value ?? null) : edits.value;
  const note = (edits.note ?? item.appraisal?.note ?? "") || null;
  const edited = Object.keys(fit).length > 0 || value !== (item.appraisal?.value ?? null) || note !== (item.appraisal?.note || null);
  const first = !item.answered_at;
  const drawn = fit[item.topic] ?? item.fit[item.topic];
  if (first && drawn) fit[item.topic] = drawn;
  return { answer: { fit, value, note }, edited, complete: !first || !!drawn };
}

/** A session's row in the past-sessions lists as the refreshed session reads: its papers, how many are answered
 * and how many answers differ (`differs`, the API's rule). The row keeps its place. */
export function sessionRow(row: SessionRow, s: Session): SessionRow {
  return { ...row, papers: s.items.length, answered: s.items.filter((it) => it.answered_at).length, differ: s.items.filter(differs).length };
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
export function savedLine(before: Schemas["Topic"] | null, after: Schemas["Topic"], version: number | undefined): string {
  if (!before) return "Followed. Its papers show ungraded until a run grades them.";
  if (before.depth !== after.depth) return "Saved. Topic pages and rarity follow the new depth now; the Fits already given stand until a re-run is asked for.";
  const read = (t: Schemas["Topic"]) => JSON.stringify([t.subject, t.rubric ?? "", t.interests ?? "", t.keywords ?? []]);
  if (read(before) !== read(after)) return `Saved as ${version == null ? "a new version" : `version ${version}`}. It applies to papers classified or graded from now on.`;
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
      else if (v != null) lines.push(`${k} = ${tomlValue(v)}`); // an unset setting is left out, as TOML has no null
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
