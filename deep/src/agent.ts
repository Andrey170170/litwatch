/**
 * One paper through the run that writes its Deep read (docs/specs/deep-read.md, "How a read is made"): the writer,
 * on the configured model and effort, with the single `run_code` tool whose cells also return the images its
 * session functions make; a `submit` that checks the draft against the schema and in code and lays its diagrams
 * out; then the reviewer, whose findings go back into the writer's own conversation for at most two revisions. The
 * run keeps a trace of everything that happened, the review rounds, the figures the read shows, and the service's
 * own process notes on the read.
 */

import { Agent, type AgentEvent, type AgentMessage, type AgentTool } from "@earendil-works/pi-agent-core";
import type { AssistantMessage, ImageContent, Models, TextContent, ThinkingLevel } from "@earendil-works/pi-ai";
import { Type } from "typebox";
import { PROVIDER, requireSubscription } from "./auth.ts";
import { check, drawErrors, type Checked } from "./checks.ts";
import { Figures, type Made } from "./figures.ts";
import { BudgetError, buildHosts, NetworkError, pacer, RefusedError, type FetchRecord, type HostConfig, type StoreHit } from "./hosts.ts";
import { RenderUnavailable, type Imaging, type RenderTarget, type RenderWidth, type Shot } from "./imaging.ts";
import { layout } from "./layout.ts";
import { headings } from "./paper.ts";
import { PaperIndex } from "./places.ts";
import { findingsMessage, systemPrompt, topicKey, userMessage, type PaperInput, type SelectionContext } from "./prompt.ts";
import { draftErrors, readErrors, type Draft, type Read } from "./read.ts";
import { Repl, type HostDetail, type HostFn } from "./repl.ts";
import { failings, passes, review, type Review } from "./review.ts";
import { isWebUrl } from "./schema.ts";

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
  | { t: number; type: "cell"; n: number; code: string; output: string; error?: string; ms: number; timedOut?: true; images?: number }
  | { t: number; type: "host"; name: string; args: string; ms: number; ok: boolean; chars: number; error?: string; fetch?: FetchRecord }
  | { t: number; type: "submit"; ok: boolean; errors?: string[]; stood?: string[] }
  | { t: number; type: "review"; round: number; passed?: boolean; error?: string; usage: TurnUsage }
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
 * paper's when no valid read was submitted within the budget or the provider's content filter stopped the model.
 * litwatch counts only the paper's against it.
 */
export type Failure = "service" | "paper";

/** A process note on the read: the model's, validated with it, or the service's, added after. */
export type ProcessNote = NonNullable<Read["process_notes"]>[number];

/** One of the read's figures as the job returns it (docs/integrations.md). */
export interface ReturnedFigure {
  name: string;
  mime: "image/webp";
  data: string; // base64
}

export interface Outcome {
  result: Read | null;
  /** The figures the read shows; empty when it shows none. */
  figures: ReturnedFigure[];
  /** The review rounds in order; null when no review gave a verdict. */
  reviews: Review[] | null;
  trace: TraceEvent[];
  stats: Stats;
  model: string;
  error?: string;
  failure?: Failure; // set with `error`
}

export type Progress = (event: TraceEvent, stats: Stats) => void;

/** Per-job shape of the read: the topics it is written for, and the lens questions (`extensions`) they ask. */
export interface CardSpec {
  extensions: string[];
  selections: SelectionContext[];
}

const NUDGE = "No read has been submitted. Call submit(read) with the complete read now; fix any errors it reports.";
const MAX_NUDGES = 2;
/** The first write does not count: at most this many revisions follow a failed review. */
export const MAX_REVISIONS = 2;
/** A failed host call's reason, as the trace keeps it. */
const MAX_ERROR_CHARS = 300;
/** A rejected submit's errors, as the trace keeps them. */
const MAX_TRACED_ERRORS = 60;
/** The images one cell returns; the rest are dropped with a line saying so. */
const MAX_CELL_IMAGES = 12;
/** The model keeps seeing the images of this many of its latest cells that made any, and at most twice as many:
 * once more have come, the oldest lose theirs, a batch at a time so the conversation's start stays the same
 * between batches. */
const KEEP_IMAGE_CELLS = 8;

/**
 * Asks for the provider's detailed reasoning summaries. pi-ai's simple stream options carry no
 * summary setting and ask for `auto`; the request it builds is changed here, wherever it asks for one.
 */
export function detailedSummaries(payload: unknown): unknown {
  const reasoning = (payload as { reasoning?: { summary?: unknown } } | null)?.reasoning;
  if (reasoning && reasoning.summary !== undefined) reasoning.summary = "detailed";
  return payload;
}

/** The conversation as the model is sent it: the images of all but the latest cells that made any replaced by a
 * line, so a long run's renders do not fill the context (docs/plans/deep-read.md, R4). */
export function withoutOldImages(messages: AgentMessage[], keep = KEEP_IMAGE_CELLS): AgentMessage[] {
  const pictured = messages.flatMap((m, i) => ("role" in m && m.role === "toolResult" && m.content.some((c) => c.type === "image") ? [i] : []));
  if (pictured.length <= keep * 2) return messages;
  const dropped = new Set(pictured.slice(0, Math.floor((pictured.length - keep) / keep) * keep));
  return messages.map((m, i) => {
    if (!dropped.has(i) || !("role" in m) || m.role !== "toolResult") return m;
    const images = m.content.filter((c) => c.type === "image").length;
    const text: TextContent = { type: "text", text: `[${images} image${images === 1 ? "" : "s"} shown here earlier; call the function again to see ${images === 1 ? "it" : "them"}]` };
    return { ...m, content: [...m.content.filter((c) => c.type !== "image"), text] };
  });
}

/** What the service itself saw hold a run back, by kind of event: a fetch the network guard refused, one that
 * failed on the network, the fetch budget used up, a cell timeout, an image the browser or litwatch's render page
 * was not there to make, and figures taken from arXiv's latest version for want of the text's. */
export type Held = "refused" | "network" | "budget" | "timeout" | "imaging" | "latest";

/** The service's process notes: one per kind of event the run saw, with how many times and the first URL
 * concerned, in the order they first happened. */
export function serviceNotes(seen: ReadonlyMap<Held, { count: number; url?: string }>, hostCfg: HostConfig, opts: RunOptions): ProcessNote[] {
  const times = (n: number, one: string, many: string) => (n === 1 ? one : `${n} ${many}`);
  const text: Record<Held, (n: number) => Pick<ProcessNote, "kind" | "note">> = {
    refused: (n) => ({ kind: "access", note: `${times(n, "A fetch was", "fetches were")} refused: the address is not a public one.` }),
    network: (n) => ({ kind: "access", note: `${times(n, "A fetch", "fetches")} failed on the network: DNS, the connection, a timeout or a body cut off.` }),
    budget: (n) => ({ kind: "budget", note: `The fetch budget of ${hostCfg.maxFetches} per paper was used up; ${times(n, "one more fetch was", "more fetches were")} refused.` }),
    timeout: () => ({ kind: "tool", note: `A cell ran past its ${opts.cellTimeoutMs / 1000} s limit, which ended the session and lost its state.` }),
    imaging: (n) => ({ kind: "tool", note: `${times(n, "An image", "images")} could not be made: the browser or litwatch's render page was not available, so the writer could not look at a figure or at what it drew.` }),
    latest: () => ({ kind: "input", note: "litwatch could not say which arXiv version its text is of, so the figures were taken from the latest version arXiv or litwatch had, which may not be the text's." }),
  };
  return [...seen].map(([held, { count, url }]) => ({ ...text[held](count), source: "service", ...(url ? { url } : {}) }));
}

/** A read `submit` accepted: laid out, with what its checks left to the reviewer, and the figures it shows or
 * cites for plot values as they were when it was accepted. A figure made again under the same name afterwards, in
 * a revision that is then not accepted, is no part of it. */
interface Accepted extends Pick<Checked, "plots" | "unverified" | "stood"> {
  read: Read;
  figures: ReadonlyMap<string, Made>;
}

/** The names of the figures a read shows. */
const figuresOf = (read: Read): string[] => read.sections.flatMap((s) => ("blocks" in s ? s.blocks.flatMap((b) => (b.type === "fig" ? [b.name] : [])) : []));

export async function processPaper(
  paper: PaperInput,
  spec: CardSpec,
  models: Models,
  hostCfg: HostConfig,
  opts: RunOptions,
  imaging: Imaging,
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
  /** Every model call counts, the reviewer's too. */
  const spent = (m: AssistantMessage): TurnUsage => {
    const usage: TurnUsage = { input: m.usage.input, output: m.usage.output, reasoning: m.usage.reasoning ?? 0, cache_read: m.usage.cacheRead, list_cost: m.usage.cost.total };
    stats.turns += 1;
    stats.input += usage.input;
    stats.output += usage.output;
    stats.reasoning += usage.reasoning;
    stats.cache_read += usage.cache_read;
    stats.list_cost += usage.list_cost;
    return usage;
  };

  // The read `submit` last accepted, until a review takes it (`take`); the one last taken is `accepted`.
  let candidate: Accepted | null = null;
  let accepted: Accepted | null = null;
  const pending = (): boolean => candidate !== null;
  const take = (): Accepted | null => {
    const taken = candidate;
    candidate = null;
    return taken;
  };
  let failure: string | undefined;
  let failedBy: Failure = "paper";
  // What held the run back, as the service saw it; it becomes the read's service notes when the run is over.
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
      else if (e instanceof RenderUnavailable) saw("imaging");
      throw e;
    }
  };
  // Ends with the paper: what the agent left fetching or queued stops with it.
  const session = new AbortController();
  const pace = pacer(session.signal);
  // The images the running cell's functions made, which go back with its output.
  let shown: Shot[] = [];
  const figures = new Figures(paper, hostCfg, imaging, (shot) => shown.push(shot), pace, session.signal);

  // The paper's headings and places, read once: the session's sections() and the checks share them.
  const heads = headings(paper.body, paper.body_source);
  const index = new PaperIndex(paper.body, paper.body_source);
  // The collection's papers the agent was shown: among_collection may name only these.
  const collection = new Set<string>();
  const topics = spec.selections.map(topicKey);
  const checked = (draft: Draft, standing: readonly string[]): Checked => check(draft, { index, found: { collection, self: paper.id }, topics, figures: figures.made, standing });
  /** A draft through the schema, the code checks and the layout: the read to store, or what is wrong with it.
   * `standBy` names the refused places and numbers the writer stands by. */
  const examine = async (input: unknown, standBy: unknown): Promise<{ errors: string[] } | Accepted> => {
    if (standBy !== undefined && !(Array.isArray(standBy) && standBy.every((s) => typeof s === "string")))
      return { errors: ["the second argument names what you stand by: an array of strings, each a place or number as a refusal quoted it"] };
    const shape = draftErrors(input);
    if (shape.length) return { errors: shape };
    const { errors, plots, unverified, stood } = checked(input as Draft, (standBy as string[] | undefined) ?? []);
    if (errors.length) return { errors };
    // the figures as they are now: each record is replaced, never changed, when its name is made again
    const kept = new Map([...figuresOf(input as Read), ...plots].flatMap((name) => (figures.made.has(name) ? [[name, figures.made.get(name)!] as const] : [])));
    const read = await layout(input as Draft);
    const stored = readErrors(read);
    return stored.length ? { errors: stored.map((e) => `after the diagrams were laid out, ${e}`) } : { read, plots, unverified, stood, figures: kept };
  };

  const hosts: Record<string, HostFn> = {
    ...buildHosts(hostCfg, session.signal, pace),
    ...figures.hosts(),
    render: async ([draft, target, width]) => {
      const errors = draftErrors(draft);
      if (errors.length === 0) errors.push(...drawErrors(draft as Draft));
      if (errors.length) throw new Error(`the draft cannot be drawn until these are fixed:\n${errors.slice(0, 30).join("\n")}`);
      const read = await layout(draft as Draft);
      // only figures this session made: a name it did not make draws as the page's placeholder
      const cited = new Set(figuresOf(read));
      const urls = Object.fromEntries([...cited].flatMap((name) => (figures.made.has(name) ? [[name, `data:image/webp;base64,${figures.made.get(name)!.webp.toString("base64")}`]] : [])));
      const made = await imaging.render(read, urls, renderTarget(target), renderWidth(width));
      shown.push(...made.shots);
      return { target: target ?? "read", width: renderWidth(width), images: made.shots.length, height: made.height, ...(made.cut ? { cut: "only the top is shown: render a section or a block" } : {}) };
    },
    check: async ([draft, standBy]) => {
      const result = await examine(draft, standBy);
      return "errors" in result ? { ok: false, errors: result.errors } : { ok: true };
    },
    submit: async ([draft, standBy]) => {
      const result = await examine(draft, standBy);
      if ("errors" in result) {
        record({ t: now(), type: "submit", ok: false, errors: result.errors.slice(0, MAX_TRACED_ERRORS) });
        return { ok: false, errors: result.errors };
      }
      candidate = result;
      record({ t: now(), type: "submit", ok: true, ...(result.stood ? { stood: result.stood } : {}) });
      return { ok: true };
    },
  };
  for (const [name, fn] of Object.entries(hosts)) if (name !== "submit") hosts[name] = traced(name, fn);
  const search = hosts["store.search"]!;
  hosts["store.search"] = async (args) => {
    const hits = (await search(args)) as StoreHit[];
    for (const h of hits) if (h.in_collection) collection.add(h.id);
    return hits;
  };

  const repl = await Repl.create(
    {
      paper: paper.body,
      __meta: JSON.stringify({ id: paper.id, title: paper.title, authors: paper.authors, published: paper.published ?? null, url: paper.url ?? null }),
      __headings: JSON.stringify(heads),
    },
    hosts,
    { cellTimeoutMs: opts.cellTimeoutMs, maxOutputChars: opts.maxOutputChars },
  );

  const params = Type.Object({ code: Type.String({ description: "JavaScript source for one cell" }) });
  const runCode: AgentTool<typeof params> = {
    name: "run_code",
    label: "Run code",
    description: "Run a JavaScript cell in the paper's session. State persists between cells; top-level await works; the last expression is echoed. Images the cell's functions make come back with its output.",
    parameters: params,
    executionMode: "sequential",
    execute: async (_id, params) => {
      if (stats.cells >= opts.maxCells) throw new Error(`the budget of ${opts.maxCells} cells is exhausted`);
      const n = ++stats.cells;
      shown = [];
      const cell = await repl.run(params.code);
      const images = shown.slice(0, MAX_CELL_IMAGES);
      const dropped = shown.length - images.length;
      shown = [];
      record({ t: now(), type: "cell", n, code: params.code, output: cell.output, error: cell.error, ms: cell.ms, ...(cell.timedOut ? { timedOut: true } : {}), ...(images.length ? { images: images.length } : {}) });
      if (cell.timedOut) saw("timeout");
      let text = cell.output;
      if (cell.error) text += `${text ? "\n" : ""}Error: ${cell.error}`;
      if (!text) text = "(no output)";
      if (dropped > 0) text += `\n[${dropped} more image${dropped === 1 ? "" : "s"} not shown: a cell returns at most ${MAX_CELL_IMAGES}]`;
      const left = opts.maxCells - n;
      if (left <= 3 && !candidate) text += `\n[${left} cell${left === 1 ? "" : "s"} left; submit now]`;
      const content: (TextContent | ImageContent)[] = [{ type: "text", text }, ...images.map((s): ImageContent => ({ type: "image", data: s.data, mimeType: s.mimeType }))];
      return { content, details: { n, ms: cell.ms }, terminate: candidate !== null };
    },
  };

  const agent = new Agent({
    initialState: { systemPrompt: systemPrompt(opts.maxCells, opts.maxOutputChars, spec.extensions, spec.selections), model, thinkingLevel: opts.effort, tools: [runCode] },
    streamFn: models.streamSimple.bind(models),
    onPayload: detailedSummaries,
    transformContext: async (messages) => withoutOldImages(messages),
    sessionId: `litwatch-${paper.id}-${started}`,
    toolExecution: "sequential",
    // Error and aborted turns are hard exits; the loop ignores this decision for them.
    finishTurn: () => (candidate !== null || stats.cells >= opts.maxCells || failure !== undefined ? { action: "end" } : undefined),
  });

  agent.subscribe((event: AgentEvent) => {
    if (event.type !== "message_end" || event.message.role !== "assistant") return;
    const m = event.message;
    const usage = spent(m);
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

  /** The writer's turn: the message, then nudges until it has submitted a read or cannot. */
  const write = async (message: string): Promise<void> => {
    await agent.prompt(message);
    for (let nudges = 0; !pending() && !failure && stats.cells < opts.maxCells && nudges < MAX_NUDGES; nudges++) {
      record({ t: now(), type: "system", text: NUDGE });
      await agent.prompt(NUDGE);
    }
  };
  /** Why the writer has no read to show for its turn. */
  const stopped = (): string => failure ?? (stats.cells >= opts.maxCells ? "the cell budget ran out" : "the writer stopped without submitting");

  const reviews: Review[] = [];
  // What the run's end leaves to say on the read about its review, if anything.
  let reviewNote: ProcessNote | undefined;
  // What of the paper the reviewer of the kept read was not given, for want of room in the model's context.
  let unread: string[] = [];
  try {
    await write(userMessage(paper, heads));
    for (let round = 1; ; round++) {
      const next = take();
      if (!next) break;
      accepted = next;
      const judged = await review(
        models,
        model,
        {
          paper,
          read: next.read,
          selections: spec.selections,
          // each figure as it was when the read was accepted, with the paper's number for it where the session has one
          plots: next.plots.flatMap((name) => {
            const made = next.figures.get(name);
            return made ? [{ name, ...(made.label === undefined ? {} : { number: `Figure ${made.label}` }), shot: made.view }] : [];
          }),
          unverified: next.unverified,
          stood: next.stood ?? [],
        },
        { effort: opts.effort, signal: session.signal, onPayload: detailedSummaries },
      );
      const usage = judged.calls.map(spent).reduce<TurnUsage>((sum, u) => ({ input: sum.input + u.input, output: sum.output + u.output, reasoning: sum.reasoning + u.reasoning, cache_read: sum.cache_read + u.cache_read, list_cost: sum.list_cost + u.list_cost }), { input: 0, output: 0, reasoning: 0, cache_read: 0, list_cost: 0 });
      if (!judged.verdict) {
        const why = failure ?? judged.error ?? "it gave no verdict";
        record({ t: now(), type: "review", round, error: why, usage });
        // The read kept is unreviewed, and says so. A verdict on the draft before it is no verdict on this one:
        // the earlier rounds stay in `reviews` as what happened, and nothing of them is said of this read.
        const revision = reviews.length ? " It is a revision: the reviews on record are of the drafts before it." : "";
        reviewNote = { kind: "tool", source: "service", note: `The read was not reviewed: the review could not run (${cut(why)}).${revision}` };
        unread = [];
        break;
      }
      unread = judged.left;
      const passed = passes(judged.verdict);
      const entry: Review = { round, passed, ...judged.verdict };
      reviews.push(entry);
      record({ t: now(), type: "review", round, passed, usage });
      if (passed) break;
      const failed = (rest: string): ProcessNote => ({ kind: "review", source: "service", note: `The read did not pass its review: ${failings(entry)}. ${rest}` });
      if (round > MAX_REVISIONS) {
        reviewNote = failed(`It was kept as it stood after ${MAX_REVISIONS} revisions.`);
        break;
      }
      const message = findingsMessage(entry, MAX_REVISIONS - round + 1);
      record({ t: now(), type: "system", text: message });
      await write(message).catch((e: unknown) => {
        failure ??= e instanceof Error ? e.message : String(e);
      });
      if (!pending()) reviewNote = failed(`The revision that should have followed was not made: ${cut(stopped())}.`);
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
    await figures.dispose().catch(() => undefined);
  }
  stats.seconds = Math.round((Date.now() - started) / 1000);
  if (figures.usedLatest) saw("latest");
  const kept = accepted as Accepted | null;
  // a review of part of the paper is not a silent pass: the read says what its reviewer did not see
  const partial: ProcessNote[] = unread.length
    ? [{ kind: "tool", source: "service", note: `The reviewer was not given the whole paper: ${unread.join(" and ")} did not fit the model's context, so what the read says of that part was not reviewed.` }]
    : [];
  const notes = [...serviceNotes(seen, hostCfg, opts), ...(reviewNote ? [reviewNote] : []), ...partial];
  const result = kept ? withServiceNotes(kept.read, notes) : null;
  const error = kept ? undefined : (failure ?? (stats.cells >= opts.maxCells ? "cell budget exhausted without a submit" : "the model stopped without submitting"));
  return {
    result,
    figures: result && kept ? returned(result, kept.figures) : [],
    reviews: reviews.length ? reviews : null,
    trace,
    stats,
    model: `${PROVIDER}/${opts.model}`,
    error,
    failure: error === undefined ? undefined : failedBy,
  };
}

/** The validated read with the service's notes after the model's: added once the run is over, past `submit`'s
 * checks, so the model can neither forge them nor leave them out, and an event after its submit still counts. */
function withServiceNotes(read: Read, notes: ProcessNote[]): Read {
  return notes.length === 0 ? read : { ...read, process_notes: [...(read.process_notes ?? []), ...notes] };
}

/** The figures the read shows, each once, as the job returns them. */
function returned(read: Read, made: ReadonlyMap<string, Made>): ReturnedFigure[] {
  return [...new Set(figuresOf(read))].flatMap((name) => (made.has(name) ? [{ name, mime: "image/webp" as const, data: made.get(name)!.webp.toString("base64") }] : []));
}

function renderTarget(target: unknown): RenderTarget {
  if (target === undefined || target === null || target === "read") return {};
  const m = typeof target === "string" ? /^([a-z][a-z0-9-]*)(?:\.(\d+))?$/.exec(target) : null;
  if (!m) throw new Error(`render(read, target?, width?): target is a section's id or "<section id>.<n>"; got ${JSON.stringify(target)}`);
  return m[2] === undefined ? { section: m[1]! } : { section: m[1]!, block: Number(m[2]) };
}

function renderWidth(width: unknown): RenderWidth {
  if (width === undefined || width === null) return "desktop";
  if (width === "desktop" || width === "phone") return width;
  throw new Error(`render(read, target?, width?): width is "desktop" or "phone"; got ${JSON.stringify(width)}`);
}

const cut = (text: string): string => (text.length > 200 ? `${text.slice(0, 200)}…` : text).replace(/[.\s]+$/, "");

function short(args: unknown[]): string {
  const s = JSON.stringify(args);
  return s.length > 200 ? `${s.slice(0, 200)}…` : s;
}
