/** In-memory job registry: one paper at a time, recent jobs kept for inspection. */

import { randomUUID } from "node:crypto";
import type { Outcome, Progress, Stats, TraceEvent } from "./agent.ts";
import type { PaperInput } from "./prompt.ts";

export type JobStatus = "queued" | "running" | "done" | "error";

export interface Job {
  id: string;
  paper_id: string;
  title: string;
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
}

export type Runner = (paper: PaperInput, progress: Progress) => Promise<Outcome>;

const EMPTY: Stats = { cells: 0, turns: 0, input: 0, output: 0, reasoning: 0, cache_read: 0, list_cost: 0, seconds: 0 };

export class JobQueue {
  private readonly jobs = new Map<string, Job>();
  private readonly papers = new Map<string, PaperInput>();
  private readonly queue: string[] = [];
  private running: string | null = null;
  private readonly runner: Runner;
  private readonly keep: number;

  constructor(runner: Runner, keep = 50) {
    this.runner = runner;
    this.keep = keep;
  }

  submit(paper: PaperInput): Job {
    const job: Job = {
      id: randomUUID(),
      paper_id: paper.id,
      title: paper.title,
      status: "queued",
      created_at: new Date().toISOString(),
      stats: { ...EMPTY },
      last_activity: new Date().toISOString(),
    };
    this.jobs.set(job.id, job);
    this.papers.set(job.id, paper);
    this.queue.push(job.id);
    this.prune();
    void this.pump();
    return job;
  }

  get(id: string): Job | undefined {
    return this.jobs.get(id);
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
    const paper = this.papers.get(id);
    this.papers.delete(id);
    if (!job || !paper) return void this.pump();
    this.running = id;
    job.status = "running";
    job.started_at = new Date().toISOString();
    try {
      const outcome = await this.runner(paper, (event, stats) => {
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
    } catch (e) {
      job.status = "error";
      job.error = e instanceof Error ? e.message : String(e);
    } finally {
      job.finished_at = new Date().toISOString();
      job.last_activity = job.finished_at;
      this.running = null;
      void this.pump();
    }
  }

  private prune(): void {
    const finished = [...this.jobs.values()].filter((j) => j.status === "done" || j.status === "error");
    for (const j of finished.slice(0, Math.max(0, finished.length - this.keep))) this.jobs.delete(j.id);
  }
}

function describe(event: TraceEvent): string {
  switch (event.type) {
    case "cell":
      return `cell ${event.n}${event.error ? " (error)" : ""} ${event.ms}ms`;
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
