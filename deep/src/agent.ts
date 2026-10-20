/**
 * One paper through the agent: the configured model and effort, the single `run_code` tool, a validating
 * `submit`, a cell budget, a trace of everything that happened, and the service's own process notes on the card.
 */

import { Agent, type AgentEvent, type AgentTool } from "@earendil-works/pi-agent-core";
import type { Models, ThinkingLevel } from "@earendil-works/pi-ai";
import { Type } from "typebox";
import { PROVIDER, requireSubscription } from "./auth.ts";
import { BudgetError, buildHosts, NetworkError, RefusedError, type FetchRecord, type HostConfig, type StoreHit } from "./hosts.ts";
import { systemPrompt, userMessage, type PaperInput, type SelectionContext } from "./prompt.ts";
import { Repl, type HostDetail, type HostFn } from "./repl.ts";
import { isWebUrl, validate, type ProcessNote, type Result } from "./schema.ts";

export interface RunOptions {
  model: string;
  effort: ThinkingLevel;
  maxCells: number;
  cellTimeoutMs: number;
  wallClockMs: number;
  maxOutputChars: number;
}

export type TraceEvent =
  | { t: number; type: "assistant"; text: string; thinking: string; stop: string; usage: TurnUsage }
  | { t: number; type: "cell"; n: number; code: string; output: string; error?: string; ms: number; timedOut?: true }
  | { t: number; type: "host"; name: string; args: string; ms: number; ok: boolean; chars: number; error?: string; fetch?: FetchRecord }
  | { t: number; type: "submit"; ok: boolean; errors?: string[] }
  | { t: number; type: "system"; text: string };

export interface TurnUsage {
  input: number;
  output: number;
  reasoning: number;
  cache_read: number;
  list_cost: number;
}

export interface Stats extends TurnUsage {
  cells: number;
  turns: number;
  seconds: number;
}

/**
 * Whose failure a job's was: the service's when the model call failed (the sign-in, a usage limit, the provider), the
 * paper's when no valid card was submitted within the budget or the provider's content filter stopped the model.
 * litwatch counts only the paper's against it.
 */
export type Failure = "service" | "paper";

export interface Outcome {
  result: Result | null;
  trace: TraceEvent[];
  stats: Stats;
  model: string;
  error?: string;
  failure?: Failure; // set with `error`
}

export type Progress = (event: TraceEvent, stats: Stats) => void;

/** Per-job shape of the card: which extensions the result must carry, and why the paper was selected. */
export interface CardSpec {
  extensions: string[];
  selections: SelectionContext[];
}

const NUDGE =
  "No result has been submitted. Call submit(result) with the complete result now; fix any errors it reports.";
const MAX_NUDGES = 2;
/** A failed host call's reason, as the trace keeps it. */
const MAX_ERROR_CHARS = 300;

/**
 * Asks for the provider's detailed reasoning summaries. pi-ai's simple stream options carry no
 * summary setting and ask for `auto`; the request it builds is changed here, wherever it asks for one.
 */
export function detailedSummaries(payload: unknown): unknown {
  const reasoning = (payload as { reasoning?: { summary?: unknown } } | null)?.reasoning;
  if (reasoning && reasoning.summary !== undefined) reasoning.summary = "detailed";
  return payload;
}

/** What the service itself saw hold a run back, by kind of event: a fetch the network guard refused, one that
 * failed on the network, the fetch budget used up, a cell timeout. */
export type Held = "refused" | "network" | "budget" | "timeout";

/** The service's process notes: one per kind of event the run saw, with how many times and the first URL
 * concerned, in the order they first happened. */
export function serviceNotes(seen: ReadonlyMap<Held, { count: number; url?: string }>, hostCfg: HostConfig, opts: RunOptions): ProcessNote[] {
  const times = (n: number, one: string, many: string) => (n === 1 ? one : `${n} ${many}`);
  const text: Record<Held, (n: number) => Pick<ProcessNote, "kind" | "note">> = {
    refused: (n) => ({ kind: "access", note: `${times(n, "A fetch was", "fetches were")} refused: the address is not a public one.` }),
    network: (n) => ({ kind: "access", note: `${times(n, "A fetch", "fetches")} failed on the network: DNS, the connection, a timeout or a body cut off.` }),
    budget: (n) => ({ kind: "budget", note: `The fetch budget of ${hostCfg.maxFetches} per paper was used up; ${times(n, "one more fetch was", "more fetches were")} refused.` }),
    timeout: () => ({ kind: "tool", note: `A cell ran past its ${opts.cellTimeoutMs / 1000} s limit, which ended the session and lost its state.` }),
  };
  return [...seen].map(([held, { count, url }]) => ({ ...text[held](count), source: "service", ...(url ? { url } : {}) }));
}

export async function processPaper(
  paper: PaperInput,
  spec: CardSpec,
  models: Models,
  hostCfg: HostConfig,
  opts: RunOptions,
  progress: Progress = () => undefined,
): Promise<Outcome> {
  const model = models.getModel(PROVIDER, opts.model);
  if (!model) throw new Error(`model ${opts.model} is not available from ${PROVIDER}`);
  // The provider would fall back to OPENAI_API_KEY without a stored subscription credential.
  await requireSubscription(models);

  const started = Date.now();
  const trace: TraceEvent[] = [];
  const stats: Stats = { cells: 0, turns: 0, input: 0, output: 0, reasoning: 0, cache_read: 0, list_cost: 0, seconds: 0 };
  const record = (event: TraceEvent) => {
    trace.push(event);
    stats.seconds = Math.round((Date.now() - started) / 1000);
    progress(event, stats);
  };
  const now = () => Date.now() - started;

  let submitted: Result | null = null;
  let failure: string | undefined;
  let failedBy: Failure = "paper";
  // What held the run back, as the service saw it; it becomes the card's service notes when the run is over.
  const seen = new Map<Held, { count: number; url?: string }>();
  const saw = (held: Held, url?: unknown) => {
    const known = seen.get(held);
    if (known) known.count += 1;
    else seen.set(held, { count: 1, ...(typeof url === "string" && isWebUrl(url) ? { url } : {}) });
  };

  // Each host call is traced with what it did (`detail`), and a failed one with its reason.
  const traced = (name: string, fn: HostFn): HostFn => async (args) => {
    const t0 = Date.now();
    const detail: HostDetail = {};
    try {
      const value = await fn(args, detail);
      record({ t: now(), type: "host", name, args: short(args), ms: Date.now() - t0, ok: true, chars: JSON.stringify(value ?? null).length, ...detail });
      return value;
    } catch (e) {
      const error = (e instanceof Error ? e.message : String(e)).slice(0, MAX_ERROR_CHARS);
      record({ t: now(), type: "host", name, args: short(args), ms: Date.now() - t0, ok: false, chars: 0, error, ...detail });
      if (e instanceof RefusedError) saw("refused", args[0]);
      else if (e instanceof NetworkError) saw("network", args[0]);
      else if (e instanceof BudgetError) saw("budget");
      throw e;
    }
  };
  // Ends with the paper: what the agent left fetching or queued stops with it.
  const session = new AbortController();
  const hosts: Record<string, HostFn> = Object.fromEntries(
    Object.entries(buildHosts(hostCfg, session.signal)).map(([name, fn]) => [name, traced(name, fn)]),
  );
  // The collection's papers the agent was shown: among_collection may name only these.
  const collection = new Set<string>();
  const search = hosts["store.search"]!;
  hosts["store.search"] = async (args) => {
    const hits = (await search(args)) as StoreHit[];
    for (const h of hits) if (h.in_collection) collection.add(h.id);
    return hits;
  };
  hosts.submit = async ([result]) => {
    const v = validate(result, spec.extensions, { collection, self: paper.id });
    if (v.ok) {
      submitted = v.value;
      record({ t: now(), type: "submit", ok: true });
      return { ok: true };
    }
    record({ t: now(), type: "submit", ok: false, errors: v.errors });
    return { ok: false, errors: v.errors };
  };

  const repl = await Repl.create(
    {
      paper: paper.body,
      __meta: JSON.stringify({ id: paper.id, title: paper.title, authors: paper.authors, published: paper.published ?? null, url: paper.url ?? null }),
    },
    hosts,
    { cellTimeoutMs: opts.cellTimeoutMs, maxOutputChars: opts.maxOutputChars },
  );

  const params = Type.Object({ code: Type.String({ description: "JavaScript source for one cell" }) });
  const runCode: AgentTool<typeof params> = {
    name: "run_code",
    label: "Run code",
    description: "Run a JavaScript cell in the paper's session. State persists between cells; top-level await works; the last expression is echoed.",
    parameters: params,
    executionMode: "sequential",
    execute: async (_id, params) => {
      if (stats.cells >= opts.maxCells) throw new Error(`the budget of ${opts.maxCells} cells is exhausted`);
      const n = ++stats.cells;
      const cell = await repl.run(params.code);
      record({ t: now(), type: "cell", n, code: params.code, output: cell.output, error: cell.error, ms: cell.ms, ...(cell.timedOut ? { timedOut: true } : {}) });
      if (cell.timedOut) saw("timeout");
      let text = cell.output;
      if (cell.error) text += `${text ? "\n" : ""}Error: ${cell.error}`;
      if (!text) text = "(no output)";
      const left = opts.maxCells - n;
      if (left <= 3 && !submitted) text += `\n[${left} cell${left === 1 ? "" : "s"} left; submit now]`;
      return { content: [{ type: "text", text }], details: { n, ms: cell.ms }, terminate: submitted !== null };
    },
  };

  const agent = new Agent({
    initialState: { systemPrompt: systemPrompt(opts.maxCells, opts.maxOutputChars, spec.extensions, spec.selections), model, thinkingLevel: opts.effort, tools: [runCode] },
    streamFn: models.streamSimple.bind(models),
    onPayload: detailedSummaries,
    sessionId: `litwatch-${paper.id}-${started}`,
    toolExecution: "sequential",
    // Error and aborted turns are hard exits; the loop ignores this decision for them.
    finishTurn: () => (submitted !== null || stats.cells >= opts.maxCells || failure !== undefined ? { action: "end" } : undefined),
  });

  agent.subscribe((event: AgentEvent) => {
    if (event.type !== "message_end" || event.message.role !== "assistant") return;
    const m = event.message;
    const usage: TurnUsage = {
      input: m.usage.input,
      output: m.usage.output,
      reasoning: m.usage.reasoning ?? 0,
      cache_read: m.usage.cacheRead,
      list_cost: m.usage.cost.total,
    };
    stats.turns += 1;
    stats.input += usage.input;
    stats.output += usage.output;
    stats.reasoning += usage.reasoning;
    stats.cache_read += usage.cache_read;
    stats.list_cost += usage.list_cost;
    const text = m.content.filter((c) => c.type === "text").map((c) => c.text).join("\n");
    const thinking = m.content.filter((c) => c.type === "thinking").map((c) => c.thinking).join("\n");
    record({ t: now(), type: "assistant", text, thinking, stop: m.stopReason, usage });
    // An aborted turn is the wall clock's, which has said why already.
    if (m.stopReason === "error" && failure === undefined) {
      failure = m.errorMessage ?? m.stopReason;
      // A filtered response is about what the paper says, and would be filtered again.
      failedBy = m.rawStopReason?.endsWith("content_filter") ? "paper" : "service";
    } else if (m.stopReason === "aborted") failure ??= m.errorMessage ?? m.stopReason;
  });

  const clock = setTimeout(() => {
    failure = `wall clock of ${Math.round(opts.wallClockMs / 60000)} minutes exceeded`;
    session.abort();
    agent.abort();
  }, opts.wallClockMs);

  try {
    await agent.prompt(userMessage(paper));
    for (let nudges = 0; !submitted && !failure && stats.cells < opts.maxCells && nudges < MAX_NUDGES; nudges++) {
      record({ t: now(), type: "system", text: NUDGE });
      await agent.prompt(NUDGE);
    }
  } catch (e) {
    if (failure === undefined) {
      failure = e instanceof Error ? e.message : String(e);
      failedBy = "service";
    }
  } finally {
    clearTimeout(clock);
    session.abort();
    repl.dispose();
  }
  stats.seconds = Math.round((Date.now() - started) / 1000);
  const error = submitted ? undefined : (failure ?? (stats.cells >= opts.maxCells ? "cell budget exhausted without a submit" : "the model stopped without submitting"));
  return { result: withServiceNotes(submitted, serviceNotes(seen, hostCfg, opts)), trace, stats, model: `${PROVIDER}/${opts.model}`, error, failure: error === undefined ? undefined : failedBy };
}

/** The validated card with the service's notes after the model's: added once the run is over, past `validate`,
 * so the model can neither forge them nor leave them out, and an event after its submit still counts. */
function withServiceNotes(card: Result | null, notes: ProcessNote[]): Result | null {
  if (!card || notes.length === 0) return card;
  return { ...card, process_notes: [...(card.process_notes ?? []), ...notes] };
}

function short(args: unknown[]): string {
  const s = JSON.stringify(args);
  return s.length > 200 ? `${s.slice(0, 200)}…` : s;
}
