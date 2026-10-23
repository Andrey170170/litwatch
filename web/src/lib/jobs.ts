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

export const jobKind = (j: Job) => (j.kind === "deep" ? "Deep card" : "Process");

export const ACTIONS: Record<JobAction, string> = { run: "Run now", tonight: "With tonight's run", discard: "Discard", again: "Try again" };

/** The line under a row: its papers (how far a running one got), what its runs spent, when it last changed; a card
 * that failed without waiting on the owner says the next run tries it again. */
export function jobMeta(j: Job, zone?: string): string {
  const papers = j.state === "running" && j.papers > 1 ? `${j.done} of ${plural(j.papers, "paper")}` : plural(j.papers, "paper");
  const retried = j.kind === "deep" && j.state === "failed" && !j.actions.length ? "tried again on the next run" : null;
  return [papers, j.cost != null ? usd(j.cost) : null, retried, dateTime(j.at, zone)].filter(Boolean).join(" · ");
}

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
