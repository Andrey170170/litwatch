/**
 * In-memory job registries: one job at a time per queue, recent jobs kept for inspection. A submission may carry a
 * key (litwatch sends its attempt): a key already known is answered with its job, so a submission sent again after a
 * lost reply never starts a second job while that job is kept. `Queue` is what every queue shares; `JobQueue` runs
 * Deep reads, and a finished one carries what litwatch stores of it (docs/integrations.md): the read in `result`,
 * its `figures` and its `reviews`. The release-link finds have their own queue (`links.ts`), so a find never waits
 * behind a read.
 */

import { randomUUID } from "node:crypto";
import type { Static } from "typebox";
import type { Outcome, Progress, TraceEvent } from "./agent.ts";
import type { Failure, JobSchema, PaperInput, SelectionContext, Stats } from "./contracts.ts";
import type { Read } from "./read.ts";
import type { Review } from "./review.ts";

/** A job as `GET /jobs/<id>` reports it (`contracts.ts`, `JobSchema`), with its read, reviews and trace typed as
 * the service makes them. */
export type Job = Omit<Static<typeof JobSchema>, "result" | "reviews" | "trace"> & { result?: Read; reviews?: Review[] | null; trace?: TraceEvent[] };

/** What a job carries into the run: the paper, the lens questions asked for (`extensions`), and the topics the read
 * is written for. */
export interface JobInput {
  paper: PaperInput;
  extensions: string[];
  selections: SelectionContext[];
}

export type Runner = (input: JobInput, progress: Progress) => Promise<Outcome>;

const EMPTY: Stats = { cells: 0, turns: 0, input: 0, output: 0, reasoning: 0, cache_read: 0, list_cost: 0, seconds: 0 };

/** What every queue's jobs keep, whatever they run. */
export interface Kept {
  id: string;
  status: "queued" | "running" | "done" | "error";
  created_at: string;
  started_at?: string;
  finished_at?: string;
  stats: Stats;
  last_event?: string;
  last_activity: string;
  key?: string;
  error?: string;
  failure?: Failure;
}

/** A queue's jobs, run one at a time: `run` does one and sets its outcome on it; `heavy` names what `list` leaves
 * out of each. A runner that throws ends its job as the service's failure. */
export abstract class Queue<I, J extends Kept> {
  private readonly jobs = new Map<string, J>();
  private readonly keys = new Map<string, string>(); // a submission's key to its job's id
  private readonly inputs = new Map<string, I>();
  private readonly queue: string[] = [];
  private running: string | null = null;
  private readonly keep: number;
  private readonly keepMs: number;

  protected abstract run(job: J, input: I): Promise<void>;
  protected abstract readonly heavy: readonly (keyof J)[];

  /** `keep` finished jobs at most, but none finished less than `keepMs` ago. */
  constructor(keep = 50, keepMs = 0) {
    this.keep = keep;
    this.keepMs = keepMs;
  }

  /** The fields every new job starts with, for a queue's `submit` to build its job on. */
  protected fresh(key?: string): Kept {
    const now = new Date().toISOString();
    return { id: randomUUID(), status: "queued", created_at: now, stats: { ...EMPTY }, last_activity: now, key };
  }

  /** Queue `job` with its input, unless its key's job is kept already: then that one. */
  protected enqueue(job: J, input: I): J {
    const known = job.key === undefined ? undefined : this.byKey(job.key);
    if (known) return known;
    this.jobs.set(job.id, job);
    if (job.key !== undefined) this.keys.set(job.key, job.id);
    this.inputs.set(job.id, input);
    this.queue.push(job.id);
    this.prune();
    void this.pump();
    return job;
  }

  get(id: string): J | undefined {
    return this.jobs.get(id);
  }

  /** The job a submission with this key started, while it is kept. */
  byKey(key: string): J | undefined {
    const id = this.keys.get(key);
    return id === undefined ? undefined : this.jobs.get(id);
  }

  /** Newest first, without what is heavy. */
  list(): Partial<J>[] {
    return [...this.jobs.values()].reverse().map((job) => Object.fromEntries(Object.entries(job).filter(([k]) => !this.heavy.includes(k as keyof J))) as Partial<J>);
  }

  get active(): { running: string | null; queued: number } {
    return { running: this.running, queued: this.queue.length };
  }

  /** Progress as the run reports it. */
  protected progressed(job: J, event: TraceEvent, stats: Stats): void {
    job.stats = { ...stats };
    job.last_event = describe(event);
    job.last_activity = new Date().toISOString();
  }

  private async pump(): Promise<void> {
    if (this.running !== null) return;
    const id = this.queue.shift();
    if (!id) return;
    const job = this.jobs.get(id);
    const input = this.inputs.get(id);
    this.inputs.delete(id);
    if (!job || !input) return void this.pump();
    this.running = id;
    job.status = "running";
    job.started_at = new Date().toISOString();
    try {
      await this.run(job, input);
    } catch (e) {
      // Before the model was asked: the sign-in, the model, the REPL.
      job.status = "error";
      job.error = e instanceof Error ? e.message : String(e);
      job.failure = "service";
    } finally {
      job.finished_at = new Date().toISOString();
      job.last_activity = job.finished_at;
      this.running = null;
      void this.pump();
    }
  }

  private prune(): void {
    const finished = [...this.jobs.values()].filter((j) => j.status === "done" || j.status === "error");
    const before = Date.now() - this.keepMs;
    for (const j of finished.slice(0, Math.max(0, finished.length - this.keep))) {
      if (j.finished_at !== undefined && Date.parse(j.finished_at) > before) break; // kept, as those after it
      this.jobs.delete(j.id);
      if (j.key !== undefined) this.keys.delete(j.key);
    }
  }
}

/** The Deep reads. */
export class JobQueue extends Queue<JobInput, Job> {
  protected readonly heavy = ["trace", "result", "figures", "reviews"] as const;
  private readonly runner: Runner;

  constructor(runner: Runner, keep = 50) {
    super(keep);
    this.runner = runner;
  }

  submit(paper: PaperInput, extensions: string[] = [], selections: SelectionContext[] = [], key?: string): Job {
    return this.enqueue({ ...this.fresh(key), paper_id: paper.id, title: paper.title, extensions }, { paper, extensions, selections });
  }

  protected async run(job: Job, input: JobInput): Promise<void> {
    const outcome = await this.runner(input, (event, stats) => this.progressed(job, event, stats));
    job.status = outcome.error ? "error" : "done";
    job.model = outcome.model;
    job.stats = outcome.stats;
    job.result = outcome.result ?? undefined;
    if (outcome.figures.length) job.figures = outcome.figures;
    job.reviews = outcome.reviews;
    job.trace = outcome.trace;
    job.error = outcome.error;
    job.failure = outcome.failure;
  }
}

function describe(event: TraceEvent): string {
  switch (event.type) {
    case "cell":
      return `cell ${event.n}${event.timedOut ? " (timed out)" : event.error ? " (error)" : ""} ${event.ms}ms`;
    case "host":
      return `${event.name}${event.ok ? "" : " failed"} ${event.ms}ms`;
    case "submit":
      return event.ok ? "submitted" : `submit rejected: ${event.errors?.length ?? 0} errors`;
    case "review":
      return `review ${event.round}${event.error ? " failed" : event.passed ? " passed" : " not passed"}`;
    case "assistant":
      return `turn (${event.stop})`;
    case "system":
      return "message to the writer";
  }
}
