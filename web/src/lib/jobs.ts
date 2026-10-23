/** The Jobs page's rows (DESIGN.md, "Runs and cost", Jobs): the groups, what a row says and in which colour, the
 * latest three Runs and cost shows, and what a paper's Process button reads while its job runs. */
import type { Schemas } from "@/api/client";
import { dateTime, plural } from "./format";
import { usd } from "./settings";

export type Job = Schemas["Job"];
export type Jobs = Schemas["Jobs"];
export type JobAction = Job["actions"][number];
type JobState = Job["state"];

export const GROUPS: [keyof Jobs, string][] = [
  ["not_run", "Not run"],
  ["running", "Running and queued"],
  ["failed", "Failed"],
  ["done", "Done"],
];

/** The row's colour: yellow while it waits on the owner, cyan while it runs or waits its turn, red failed, faded
 * done. */
export const jobTone = (s: JobState) => ({ offered: "ask", stopped: "ask", tonight: "run", queued: "run", running: "run", failed: "fail", done: "settled" })[s];

export const jobStatus = (s: JobState) =>
  ({ offered: "not run", stopped: "stopped", tonight: "with tonight's run", queued: "queued", running: "running", failed: "failed", done: "done" })[s];

export const jobKind = (j: Job) => ({ deep: "Deep card", process: "Process", rerun: "Re-run" })[j.kind];

/** What a row is about: a Process job's or a card's paper (linked by the page), a re-run's topic, its version and
 * scopes. */
export function rerunWhat(j: Job): string | null {
  if (!j.topic) return null;
  const version = j.topic.revision == null ? "" : ` · version ${j.topic.revision}`;
  const scopes = j.scopes.map((s) => SCOPE_SHORT[s]).join(", ");
  return `${j.topic.name}${version}${scopes ? ` · ${scopes}` : ""}`;
}

const SCOPE_SHORT: Record<Schemas["RerunScopeRow"]["scope"], string> = { labels: "labelled and missed", members: "members", keywords: "keyword matches", range: "date range" };

export const ACTIONS: Record<JobAction, string> = { run: "Run now", tonight: "With tonight's run", discard: "Discard", again: "Try again" };

/** The line under a row: its papers (how far a running one got; a re-run's graded), what its runs spent so far (of
 * a re-run's ceiling, or its estimate before it ran), when it last changed; a card that failed without waiting on
 * the owner says the next run tries it again. */
export function jobMeta(j: Job, zone?: string): string {
  const far = j.graded ?? j.done;
  const papers = (j.state === "running" || j.state === "stopped") && j.papers > 1 ? `${far} of ${plural(j.papers, "paper")}` : plural(j.papers, "paper");
  const retried = j.kind === "deep" && j.state === "failed" && !j.actions.length ? "tried again on the next run" : null;
  return [papers, jobSpend(j), retried, dateTime(j.at, zone)].filter(Boolean).join(" · ");
}

/** What a job spent, of its ceiling when it has one; before it ran, a re-run's estimate. */
function jobSpend(j: Job): string | null {
  if (j.cost == null) return j.estimate != null && j.state === "offered" ? `≈ ${usd(j.estimate)}` : j.ceiling != null ? `stops at ${usd(j.ceiling)}` : null;
  return j.ceiling != null ? `${usd(j.cost)} of ${usd(j.ceiling)}` : usd(j.cost);
}

/** A stopped re-run's Run now continues it under a new ceiling: what it spent plus its first ceiling again, as the
 * box's first value. */
export const nextCeiling = (j: Job) => (j.cost ?? 0) + (j.ceiling ?? 5);

/** The latest jobs of any group, by when they last changed: Runs and cost shows three. */
export function latestJobs(jobs: Jobs, n = 3): Job[] {
  return GROUPS.flatMap(([g]) => jobs[g])
    .sort((a, b) => b.at.localeCompare(a.at))
    .slice(0, n);
}

/** Whether a job is running or waits its turn: the page polls while one does. */
export const anyActive = (jobs: Jobs | undefined) => !!jobs?.running.length;

/** A paper's Process job is under way: queued, running or put on tonight's run. */
export const processing = (job: Schemas["ProcessJob"] | null | undefined) => !!job && ["queued", "running", "tonight"].includes(job.state);

// -- the re-run offer (DESIGN.md, "The re-run offer") ----------------------------------------------------------------

export type RerunScope = Schemas["RerunScopeRow"]["scope"];
export type RerunOffer = Schemas["RerunOffer"];

/** The offer's rows, in the spec's order: what each re-runs, and its aside. */
export const SCOPES: [RerunScope, string, string][] = [
  ["labels", "Labelled papers and reported misses", "does the edit help"],
  ["members", "The topic's existing members", ""],
  ["keywords", "Papers matching its keywords", "last 90 days"],
  ["range", "A date range", ""],
];

/** The scopes ticked when the offer opens, as the sketch has them. */
export const FIRST_SCOPES: RerunScope[] = ["labels", "members"];

/** A ceiling typed as "$5.00", "5" or "5.5": its dollars, or null when it is not a positive amount. */
export function ceilingOf(typed: string): number | null {
  const m = /^\s*\$?\s*(\d+(?:\.\d*)?|\.\d+)\s*$/.exec(typed);
  const x = m ? Number(m[1]) : NaN;
  return Number.isFinite(x) && x > 0 ? x : null;
}

/** The Run button's label: the papers of the ticked scopes, and their estimate when there is one. */
export function runLabel(offer: Pick<RerunOffer, "papers" | "estimate"> | undefined): string {
  if (!offer?.papers) return "Run";
  const cost = offer.estimate == null ? "" : ` ≈ ${usd(offer.estimate)}`;
  return `Run · ${offer.papers.toLocaleString("en")} ${offer.papers === 1 ? "paper" : "papers"}${cost}`;
}

/** A day `days` before today in `zone` (the schedule's), as an ISO date: the date range's defaults. */
export function dayIn(zone: string | undefined, days = 0, now = new Date()): string {
  const at = new Date(now.getTime() - days * 86_400_000);
  return new Intl.DateTimeFormat("en-CA", { timeZone: zone, year: "numeric", month: "2-digit", day: "2-digit" }).format(at);
}
