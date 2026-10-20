/**
 * In-memory job registry: one paper at a time, recent jobs kept for inspection. A submission may carry a key (litwatch
 * sends its attempt): a key already known is answered with its job, so a submission sent again after a lost reply
 * never starts a second job while that job is kept.
 */

import { randomUUID } from "node:crypto";
import type { Failure, Outcome, Progress, Stats, TraceEvent } from "./agent.ts";
import type { PaperInput, SelectionContext } from "./prompt.ts";

export type JobStatus = "queued" | "running" | "done" | "error";

export interface Job {
  id: string;
  paper_id: string;
  title: string;
  extensions: string[];
  status: JobStatus;
  created_at: string;
  started_at?: string;
  finished_at?: string;
  model?: string;
  stats: Stats;
  last_event?: string;
  last_activity: string;
  result?: Outcome["result"];
  trace?: TraceEvent[];
  error?: string;
  failure?: Failure; // whose failure an `error` job's was
  key?: string;
}

/** What a job carries into the run: the paper, the requested extensions, and the selections it is in. */
export interface JobInput {
  paper: PaperInput;
  extensions: string[];
  selections: SelectionContext[];
}

export type Runner = (input: JobInput, progress: Progress) => Promise<Outcome>;

const EMPTY: Stats = { cells: 0, turns: 0, input: 0, output: 0, reasoning: 0, cache_read: 0, list_cost: 0, seconds: 0 };

export class JobQueue {
  private readonly jobs = new Map<string, Job>();
  private readonly keys = new Map<string, string>(); // a submission's key to its job's id
  private readonly inputs = new Map<string, JobInput>();
  private readonly queue: string[] = [];
  private running: string | null = null;
  private readonly runner: Runner;
  private readonly keep: number;

  constructor(runner: Runner, keep = 50) {
    this.runner = runner;
    this.keep = keep;
  }

  submit(paper: PaperInput, extensions: string[] = [], selections: SelectionContext[] = [], key?: string): Job {
    const known = key === undefined ? undefined : this.byKey(key);
    if (known) return known;
    const job: Job = {
      id: randomUUID(),
      paper_id: paper.id,
      title: paper.title,
      extensions,
      status: "queued",
      created_at: new Date().toISOString(),
      stats: { ...EMPTY },
      last_activity: new Date().toISOString(),
      key,
    };
    this.jobs.set(job.id, job);
    if (key !== undefined) this.keys.set(key, job.id);
    this.inputs.set(job.id, { paper, extensions, selections });
    this.queue.push(job.id);
    this.prune();
    void this.pump();
    return job;
  }

  get(id: string): Job | undefined {
    return this.jobs.get(id);
  }

  /** The job a submission with this key started, while it is kept. */
  byKey(key: string): Job | undefined {
    const id = this.keys.get(key);
    return id === undefined ? undefined : this.jobs.get(id);
  }

  /** Newest first, without traces and results. */
  list(): Omit<Job, "trace" | "result">[] {
    return [...this.jobs.values()]
      .reverse()
      .map(({ trace: _t, result: _r, ...rest }) => rest);
  }

  get active(): { running: string | null; queued: number } {
    return { running: this.running, queued: this.queue.length };
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
      const outcome = await this.runner(input, (event, stats) => {
        job.stats = { ...stats };
        job.last_event = describe(event);
        job.last_activity = new Date().toISOString();
      });
      job.status = outcome.error ? "error" : "done";
      job.model = outcome.model;
      job.stats = outcome.stats;
      job.result = outcome.result;
      job.trace = outcome.trace;
      job.error = outcome.error;
      job.failure = outcome.failure;
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
    for (const j of finished.slice(0, Math.max(0, finished.length - this.keep))) {
      this.jobs.delete(j.id);
      if (j.key !== undefined) this.keys.delete(j.key);
    }
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
    case "assistant":
      return `turn (${event.stop})`;
    case "system":
      return "nudge";
  }
}
