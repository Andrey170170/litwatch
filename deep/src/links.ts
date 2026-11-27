/**
 * The release-link finder (docs/specs/architecture/a2-work-gate.md, "Release links"): a narrow agent that finds
 * where a paper's code and model weights are released and how each can be had. It works in the Deep read's session
 * (`repl.ts`) over the paper's text, with the same public fetch and Semantic Scholar (`hosts.ts`), on its own model
 * and effort, and gives a small structured answer through `answer()`: each link's kind, its access state and its
 * license when cheap to see; none found is an answer too. A GitHub or Hugging Face link takes its state from the
 * host's API (`hostcheck.ts`), whatever the model drafted. Any other link must have been fetched in the session, by its
 * own URL, with an answer that shows the state it is given, a quote of the page among it (`shows`); each link's
 * `checked_at` is when that fetch, or the host's check, was made.
 *
 * The finds have a queue of their own (`LinkQueue`), so a find never waits behind a Deep read.
 */

import { Agent, type AgentEvent, type AgentTool } from "@earendil-works/pi-agent-core";
import type { AssistantMessage, Models } from "@earendil-works/pi-ai";
import { Type } from "typebox";
import type { RunOptions, TraceEvent, TurnUsage } from "./agent.ts";
import { detailedSummaries } from "./agent.ts";
import { PROVIDER, requireSubscription } from "./auth.ts";
import { LINK_FIELDS, type Failure, type LinkJobSchema, type LinksAnswer, type LinksInput, type Stats } from "./contracts.ts";
import { buildHosts, pacer, type HostConfig } from "./hosts.ts";
import { Queue } from "./jobs.ts";
import { headings } from "./paper.ts";
import { Repl, type HostFn } from "./repl.ts";
import { schemaErrors } from "./schema.ts";
import { hostState, type Get, type HostState } from "./hostcheck.ts";
import type { Static } from "typebox";

/** The finder's budget: it looks a few things up, it writes nothing long. */
export const LINK_CELLS = 30;
export const LINK_WALL_CLOCK_MS = 15 * 60_000;
const NUDGE = "No answer has been given. Call answer({links: [...]}) now, an empty list when you found none; fix any errors it reports.";
const MAX_NUDGES = 2;
const MAX_LINKS = 12;

const DraftSchema = Type.Object(
  {
    links: Type.Array(
      Type.Object(
        { ...LINK_FIELDS, evidence: Type.Optional(Type.String({ maxLength: 300, description: "A passage of its fetched page that shows its state." })) },
        { additionalProperties: false },
      ),
      { maxItems: MAX_LINKS },
    ),
    note: Type.Optional(Type.String({ maxLength: 500 })),
  },
  { additionalProperties: false },
);
type Draft = Static<typeof DraftSchema>;
type Link = LinksAnswer["links"][number];

export interface LinksOutcome {
  result: LinksAnswer | null;
  trace: TraceEvent[];
  stats: Stats;
  model: string;
  error?: string;
  failure?: Failure;
}

type Progress = (event: TraceEvent, stats: Stats) => void;

/** One fetch the session made, as a link on another host is confirmed by it: the URL asked, the one that answered,
 * its status (none when the host's name did not resolve), the text it read, and when. */
export interface Fetched {
  asked: string;
  answered?: string;
  status?: number;
  text?: string;
  at: string;
}

/** A URL as it is compared: an absolute http(s) URL with a host, its scheme and host lower-cased (as `URL` writes
 * them), the path without a trailing slash, the query kept, no fragment; null for anything else. */
export function normal(url: string): string | null {
  try {
    const u = new URL(url);
    if ((u.protocol !== "https:" && u.protocol !== "http:") || !u.hostname) return null;
    return `${u.protocol}//${u.host}${u.pathname.replace(/\/+$/, "")}${u.search}`;
  } catch {
    return null;
  }
}

/** Whether a fetch of `fetched` speaks for the link `link`: the same URL (`normal`). */
export function confirms(link: string, fetched: string): boolean {
  const l = normal(link);
  return l !== null && l === normal(fetched);
}

/** Whether `evidence`, a passage the model quotes, is in a fetched page's text: whitespace and case aside, a dozen
 * characters at least. */
function quoted(evidence: string | undefined, text: string | undefined): boolean {
  const squash = (s: string) => s.replace(/\s+/g, " ").trim().toLowerCase();
  const said = squash(evidence ?? "");
  return said.length >= 12 && text !== undefined && squash(text).includes(said);
}

/** Whether a fetch of a link on another host than those checked by their API shows the state the link is given:
 * missing by a 404 or 410, or a name that does not resolve; gated by a 401 or 403, or a page whose text the model
 * quotes as its request form; public or a placeholder by a 2xx with the model's quote of what the page holds. A rate
 * limit or a server's error shows nothing. */
export function shows(state: Link["state"], f: Fetched, evidence?: string): boolean {
  if (f.status === undefined) return state === "missing";
  const there = f.status >= 200 && f.status < 300;
  switch (state) {
    case "missing":
      return f.status === 404 || f.status === 410;
    case "gated":
      return f.status === 401 || f.status === 403 || (there && quoted(evidence, f.text));
    default:
      return there && quoted(evidence, f.text);
  }
}

/** The host's API's word on a supported link (`hostState`): its state, or null when it could not say, or undefined
 * for a link on another host. */
export type Check = (url: string, kind: Link["kind"]) => Promise<HostState | null | undefined>;

/** The answer a draft makes, or what is wrong with it. A link on GitHub or Hugging Face takes its state (and license,
 * where the host gives one) from the host's API, whatever the draft says; one the host could not say of, and a link
 * elsewhere, must be fetched in the session, by its own URL, with an answer that shows its state (`shows`). A link
 * given only a page that answered, with no quote of what it holds, is dropped; a link given twice (by `normal` URL
 * and kind) is kept once. `notes` say what was replaced or dropped. */
export async function answered(
  draft: unknown,
  fetched: readonly Fetched[],
  check: Check,
): Promise<{ errors: string[] } | { answer: LinksAnswer; notes: string[] }> {
  const errors = schemaErrors(DraftSchema, draft);
  if (errors.length) return { errors };
  const { links, note } = draft as Draft;
  const out: LinksAnswer["links"] = [];
  const notes: string[] = [];
  const seen = new Set<string>();
  for (const [i, link] of links.entries()) {
    const url = normal(link.url);
    if (url === null) {
      errors.push(`/links/${i}/url: must be an absolute http(s) URL with a host`);
      continue;
    }
    if (seen.has(`${link.kind} ${url}`)) continue; // given twice, kept once
    seen.add(`${link.kind} ${url}`);
    const { license, evidence, ...rest } = link;
    const host = await check(link.url, link.kind);
    if (host) {
      if (host.state !== link.state) notes.push(`${link.url}: ${host.state} by its host's API (answered ${host.status}), not ${link.state}`);
      const kept = host.license ?? license ?? undefined;
      out.push({ ...rest, state: host.state, ...(kept ? { license: kept } : {}), checked_at: new Date().toISOString() });
      continue;
    }
    const by = fetched.filter((f) => confirms(link.url, f.asked) || (f.answered !== undefined && confirms(link.url, f.answered)));
    if (!by.length) {
      errors.push(`/links/${i}: ${link.url} was not fetched in this session; fetch it to confirm it before answering`);
      continue;
    }
    const shown = by.filter((f) => shows(link.state, f, evidence));
    const last = by.at(-1)!;
    if (!shown.length && link.state !== "missing" && by.some((f) => f.status !== undefined && f.status >= 200 && f.status < 300)) {
      notes.push(`${link.url}: dropped, its page answered with no quote of what it holds`);
      continue;
    }
    if (!shown.length) {
      const said = last.status === undefined ? "its host did not resolve" : `its fetch answered ${last.status}`;
      errors.push(`/links/${i}: ${link.url}: ${said}, which does not show it ${link.state}; give the state its fetch shows, or fetch it again`);
      continue;
    }
    out.push({ ...rest, ...(license ? { license } : {}), checked_at: shown.at(-1)!.at });
  }
  return errors.length ? { errors } : { answer: { links: out, ...(note ? { note } : {}) }, notes };
}

/** Whether a failed fetch failed because its host's name does not resolve: a link that leads nowhere. */
const unresolved = (e: unknown): boolean => /ENOTFOUND/.test(e instanceof Error ? e.message : String(e));

export function linksPrompt(maxCells: number): string {
  return `You find where one arXiv paper's code and model weights are released, and check each link. You work in a JavaScript session through the tool run_code; state persists between cells and top-level await works.

In the session:
- paper: the paper's full text (LaTeX source, or a PDF's text as markdown); empty when litwatch had none.
- meta: {id, title, authors, comments}; comments are the paper's arXiv comments.
- sections(), section(title), find(pattern, context?, limit?): the paper's headings, one section's text, and matches with their context.
- fetch(url): any public page or API, as {status, url, contentType, text, truncated}; a 404 is an answer, not an error.
- s2.lookup(arxivIdOrQuery): Semantic Scholar's record of a paper.
- answer({links, note?}): your answer; it says what is wrong, if anything.

Where to look, in this order:
1. The abstract and the arXiv comments, which you are given.
2. The paper's text: find(/github\\.com|gitlab\\.com|huggingface\\.co|code (is|will be)|weights|checkpoint/i), footnotes, the end of the introduction, the conclusion, the appendix.
3. Only when those give no link, the open web: the Hugging Face models and datasets that cite the paper (https://huggingface.co/api/models?filter=arxiv:<id>, https://huggingface.co/api/datasets?filter=arxiv:<id>), its Hugging Face paper page (https://huggingface.co/api/papers/<id>), and GitHub's search (https://api.github.com/search/repositories?q=<id>). Take a link from the web only when it is plainly this paper's: its title, its arXiv id or its authors say so.

Confirm each link before you answer. A GitHub repository or a Hugging Face model, dataset or space: litwatch's service reads its state from the host's API itself when you answer, so give the state you believe; to decide, its API is https://api.github.com/repos/<owner>/<repo> (and /contents for what it holds) or https://huggingface.co/api/models/<owner>/<name> (datasets: /api/datasets/…), whose "siblings" are its files and "gated" says whether it is gated. Any other link: fetch that very URL, and give evidence: a short passage, quoted exactly from its fetched text, that shows its state (what it holds, or its request form). Then give each link:
- url: the repository's or the model's page (https://github.com/<owner>/<repo>, https://huggingface.co/<owner>/<name>), not a file in it; one link per repository.
- kind: "code" for a code repository, "weights" for model weights or a checkpoint.
- state: "public", anyone can have it now; "gated", behind a request form or a licence to accept; "placeholder", the repository is there but holds nothing but a README (a licence or .gitignore beside it), "code coming soon", or a weights link with no weight file yet; "missing", a link the paper gives that leads nowhere (a 404, a deleted repository).
- license: when the API shows it (GitHub's license.spdx_id, Hugging Face's cardData.license or a license:<id> tag); leave it out otherwise.
- evidence: for a link on any other host, the quoted passage; a link there without one is dropped.

No link found is an answer: answer({links: []}). Never give a link on another host that you have not fetched in this session, or a state its fetch does not show (a 404 is missing, not public; a rate limit shows nothing, so fetch again): the answer is refused. You have ${maxCells} cells; answer as soon as you know.`;
}

export function linksMessage(input: LinksInput): string {
  const { paper, comments } = input;
  const top = headings(paper).filter((h) => h.level === 0).slice(0, 40).map((h) => `- ${h.number ? `${h.number} ` : ""}${h.title}`);
  const text = paper.body
    ? `Its text is in \`paper\`: ${paper.body.length} characters of ${paper.body_source === "latex" ? "LaTeX source" : "a PDF's text"}${top.length ? `, with these sections:\n${top.join("\n")}` : "."}`
    : "litwatch has no text of it: `paper` is empty.";
  return `arXiv:${paper.id}: ${paper.title}\n\nAbstract:\n${paper.abstract}\n\narXiv comments: ${comments || "none"}\n\n${text}`;
}

/** A find. A supported link's host is asked through `hostGet` (tests give their own), else the session's own fetch. */
export async function findLinks(
  input: LinksInput,
  models: Models,
  hostCfg: HostConfig,
  opts: RunOptions,
  progress: Progress = () => undefined,
  hostGet?: Get,
): Promise<LinksOutcome> {
  const model = models.getModel(PROVIDER, opts.model);
  if (!model) throw new Error(`model ${opts.model} is not available from ${PROVIDER}`);
  await requireSubscription(models);
  const { paper } = input;

  const started = performance.now();
  const trace: TraceEvent[] = [];
  const stats: Stats = { cells: 0, turns: 0, input: 0, output: 0, reasoning: 0, cache_read: 0, list_cost: 0, seconds: 0 };
  const now = () => Math.round(performance.now() - started);
  const record = (event: TraceEvent) => {
    trace.push(event);
    stats.seconds = Math.round((performance.now() - started) / 1000);
    progress(event, stats);
  };
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

  let answer: LinksAnswer | null = null;
  let failure: string | undefined;
  let failedBy: Failure = "paper";
  const fetched: Fetched[] = [];
  const session = new AbortController();
  const base = buildHosts(hostCfg, session.signal, pacer(session.signal));
  const traced = (name: string, fn: HostFn): HostFn => async (args) => {
    const t0 = Date.now();
    const detail = {};
    try {
      const value = await fn(args, detail);
      record({ t: now(), type: "host", name, args: JSON.stringify(args).slice(0, 200), ms: Date.now() - t0, ok: true, chars: JSON.stringify(value ?? null).length, ...detail });
      return value;
    } catch (e) {
      const error = (e instanceof Error ? e.message : String(e)).slice(0, 300);
      record({ t: now(), type: "host", name, args: JSON.stringify(args).slice(0, 200), ms: Date.now() - t0, ok: false, chars: 0, error, ...detail });
      throw e;
    }
  };
  const hosts: Record<string, HostFn> = {
    "store.search": traced("store.search", base["store.search"]!),
    "s2.lookup": traced("s2.lookup", base["s2.lookup"]!),
    fetch: traced("fetch", async (args, detail) => {
      const at = new Date().toISOString();
      try {
        const value = (await base.fetch!(args, detail)) as { url?: string; status?: number; text?: unknown };
        fetched.push({ asked: String(args[0]), answered: value?.url, status: value?.status, text: typeof value?.text === "string" ? value.text : undefined, at });
        return value;
      } catch (e) {
        if (unresolved(e)) fetched.push({ asked: String(args[0]), at });
        throw e;
      }
    }),
    answer: async ([draft]) => {
      const found = await answered(draft, fetched, (url, kind) => hostState(url, kind, get));
      if ("errors" in found) {
        record({ t: now(), type: "submit", ok: false, errors: found.errors.slice(0, 30) });
        return { ok: false, errors: found.errors };
      }
      answer = found.answer;
      record({ t: now(), type: "submit", ok: true, ...(found.notes.length ? { notes: found.notes } : {}) });
      return { ok: true, links: answer.links.length };
    },
  };

  const get: Get =
    hostGet ??
    (async (url) => {
      try {
        const v = (await hosts.fetch!([url])) as { url?: string; status?: number; text?: unknown };
        return { status: v.status ?? 0, url: v.url ?? url, text: typeof v.text === "string" ? v.text : "" };
      } catch {
        return null;
      }
    });
  const repl = await Repl.create(
    {
      paper: paper.body,
      __meta: JSON.stringify({ id: paper.id, title: paper.title, authors: paper.authors, comments: input.comments ?? null }),
      __headings: JSON.stringify(headings(paper)),
      __references: JSON.stringify((paper.body_source === "latex" && paper.outline.references) || null),
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
    execute: async (_id, { code }) => {
      if (stats.cells >= opts.maxCells) throw new Error(`the budget of ${opts.maxCells} cells is exhausted`);
      const n = ++stats.cells;
      const cell = await repl.run(code);
      record({ t: now(), type: "cell", n, code, output: cell.output, error: cell.error, ms: cell.ms, ...(cell.timedOut ? { timedOut: true } : {}) });
      let text = [cell.output, cell.error ? `Error: ${cell.error}` : ""].filter(Boolean).join("\n") || "(no output)";
      const left = opts.maxCells - n;
      if (left <= 3 && !answer) text += `\n[${left} cell${left === 1 ? "" : "s"} left; answer now]`;
      return { content: [{ type: "text", text }], details: { n, ms: cell.ms }, terminate: answer !== null };
    },
  };
  const agent = new Agent({
    initialState: { systemPrompt: linksPrompt(opts.maxCells), model, thinkingLevel: opts.effort, tools: [runCode] },
    streamFn: models.streamSimple.bind(models),
    onPayload: detailedSummaries,
    sessionId: `litwatch-links-${paper.id}-${started}`,
    toolExecution: "sequential",
    finishTurn: () => (answer !== null || stats.cells >= opts.maxCells || failure !== undefined ? { action: "end" } : undefined),
  });
  agent.subscribe((event: AgentEvent) => {
    if (event.type !== "message_end" || event.message.role !== "assistant") return;
    const m = event.message;
    const usage = spent(m);
    const text = m.content.filter((c) => c.type === "text").map((c) => c.text).join("\n");
    const thinking = m.content.filter((c) => c.type === "thinking").map((c) => c.thinking).join("\n");
    record({ t: now(), type: "assistant", text, thinking, stop: m.stopReason, usage });
    if (m.stopReason === "error" && failure === undefined) {
      failure = m.errorMessage ?? m.stopReason;
      failedBy = m.rawStopReason?.endsWith("content_filter") ? "paper" : "service";
    } else if (m.stopReason === "aborted") failure ??= m.errorMessage ?? m.stopReason;
  });
  const clock = setTimeout(() => {
    failure = `wall clock of ${Math.round(opts.wallClockMs / 60000)} minutes exceeded`;
    session.abort();
    agent.abort();
  }, opts.wallClockMs);
  try {
    await agent.prompt(linksMessage(input));
    for (let nudges = 0; !answer && !failure && stats.cells < opts.maxCells && nudges < MAX_NUDGES; nudges++) {
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
  stats.seconds = Math.round((performance.now() - started) / 1000);
  const result = answer as LinksAnswer | null;
  const error = result ? undefined : (failure ?? (stats.cells >= opts.maxCells ? "cell budget exhausted without an answer" : "the finder stopped without answering"));
  return { result, trace, stats, model: `${PROVIDER}/${opts.model}`, error, failure: error === undefined ? undefined : failedBy };
}

/** A find as `GET /links/<id>` reports it. */
export type LinkJob = Omit<Static<typeof LinkJobSchema>, "trace"> & { trace?: TraceEvent[] };
export type LinksRunner = (input: LinksInput, progress: Progress) => Promise<LinksOutcome>;

/** How long a finished find, its key and its answer are kept at least: past litwatch's longest wait before it tries
 * a find again (15 minutes), so a try after a lost answer finds the answer instead of paying for another find. */
export const LINK_KEEP_MS = 6 * 60 * 60_000;

/** The finds, one at a time. */
export class LinkQueue extends Queue<LinksInput, LinkJob> {
  protected readonly heavy = ["trace", "result"] as const;
  private readonly runner: LinksRunner;

  constructor(runner: LinksRunner, keep = 50, keepMs = LINK_KEEP_MS) {
    super(keep, keepMs);
    this.runner = runner;
  }

  submit(input: LinksInput, key?: string): LinkJob {
    return this.enqueue({ ...this.fresh(key), paper_id: input.paper.id }, input);
  }

  protected async run(job: LinkJob, input: LinksInput): Promise<void> {
    const outcome = await this.runner(input, (event, stats) => this.progressed(job, event, stats));
    job.status = outcome.error ? "error" : "done";
    job.model = outcome.model;
    job.stats = outcome.stats;
    job.result = outcome.result;
    job.trace = outcome.trace;
    job.error = outcome.error;
    job.failure = outcome.failure;
  }
}
