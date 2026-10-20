import assert from "node:assert/strict";
import { test } from "node:test";
import dns from "node:dns";
import { createServer } from "node:http";
import type { AddressInfo } from "node:net";
import { createModels, fauxAssistantMessage, fauxProvider, fauxToolCall, type Models } from "@earendil-works/pi-ai";
import { detailedSummaries, processPaper, type RunOptions } from "../src/agent.ts";
import { PROVIDER } from "../src/auth.ts";

const OPTS: RunOptions = { model: "faux-1", effort: "low", maxCells: 4, cellTimeoutMs: 5000, wallClockMs: 60_000, maxOutputChars: 2000 };
const HOSTS = { litwatchUrl: "http://litwatch.invalid", maxFetchChars: 1000, maxFetches: 60, maxFetchBytes: 1 << 20 };
const PAPER = { id: "2610.00001", title: "T", abstract: "A.", authors: [], body: "# T\nBody." };

/** Models whose `openai` provider is pi-ai's faux one, signed in to the subscription. */
function models(): { models: Models; faux: ReturnType<typeof fauxProvider> } {
  const faux = fauxProvider({ provider: PROVIDER, models: [{ id: OPTS.model }] });
  const m = createModels();
  m.setProvider(faux.provider);
  m.getAuth = (async () => ({ auth: { apiKey: "t" }, source: "OAuth" })) as Models["getAuth"];
  return { models: m, faux };
}

const run = (m: Models) => processPaper(PAPER, { extensions: [], selections: [] }, m, HOSTS, OPTS);

test("a failed model call is the service's failure", async () => {
  const { models: m, faux } = models();
  faux.setResponses([fauxAssistantMessage("", { stopReason: "error", errorMessage: "usage limit reached" })]);
  const outcome = await run(m);
  assert.equal(outcome.error, "usage limit reached");
  assert.equal(outcome.failure, "service");
});

test("a response the content filter stopped is the paper's failure", async () => {
  const { models: m, faux } = models();
  const filtered = fauxAssistantMessage("", { stopReason: "error", errorMessage: "Response incomplete: content_filter" });
  filtered.rawStopReason = "incomplete.content_filter";
  faux.setResponses([filtered]);
  const outcome = await run(m);
  assert.equal(outcome.error, "Response incomplete: content_filter");
  assert.equal(outcome.failure, "paper");
});

test("no card submitted is the paper's failure", async () => {
  const { models: m, faux } = models();
  faux.setResponses([fauxAssistantMessage("I read it."), fauxAssistantMessage("Done."), fauxAssistantMessage("Done.")]);
  const outcome = await run(m);
  assert.equal(outcome.error, "the model stopped without submitting");
  assert.equal(outcome.failure, "paper");
});

test("the wall clock ends the paper without waiting on its host calls", async () => {
  const server = createServer((_req, res) => void setTimeout(() => res.end('{"rows":[]}'), 3000));
  await new Promise<void>((r) => server.listen(0, "127.0.0.1", r));
  try {
    const { models: m, faux } = models();
    faux.setResponses([fauxAssistantMessage(fauxToolCall("run_code", { code: 'await store.search("x")' }), { stopReason: "toolUse" })]);
    const litwatchUrl = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
    const started = Date.now();
    const outcome = await processPaper(PAPER, { extensions: [], selections: [] }, m, { ...HOSTS, litwatchUrl }, { ...OPTS, wallClockMs: 150 });
    assert.match(String(outcome.error), /wall clock/);
    assert.ok(Date.now() - started < 2000, "the search under way was aborted, not waited out");
  } finally {
    server.closeAllConnections();
    server.close();
  }
});

test("the trace keeps why a host call failed, what a fetch did, and which cell timed out", async () => {
  const { models: m, faux } = models();
  const cell = (code: string) => fauxAssistantMessage(fauxToolCall("run_code", { code }), { stopReason: "toolUse" });
  faux.setResponses([
    cell(`await fetch("${"x".repeat(500)}")`),
    cell('await fetch("http://10.0.0.1/")'),
    cell("while (true) {}"),
    cell("1"),
  ]);
  const outcome = await processPaper(PAPER, { extensions: [], selections: [] }, m, HOSTS, { ...OPTS, cellTimeoutMs: 200 });
  const hosts = outcome.trace.filter((e) => e.type === "host");
  assert.equal(hosts.length, 2);
  assert.equal(hosts[0]!.ok, false);
  assert.equal(hosts[0]!.error, `not a URL: ${"x".repeat(500)}`.slice(0, 300), "the reason, cut to 300 characters");
  assert.equal(hosts[1]!.error, "10.0.0.1 is not a public address");
  assert.deepEqual(hosts[1]!.fetch, { hops: [] }, "a fetch refused before any answer has no hops and no page");
  const cells = outcome.trace.filter((e) => e.type === "cell");
  assert.deepEqual(cells.map((c) => c.timedOut), [undefined, undefined, true, undefined], "only the cell that ran too long");
});

test("the provider is asked for detailed reasoning summaries", async () => {
  const { models: m, faux } = models();
  let asked: unknown;
  faux.setResponses([
    async (_context, options) => {
      asked = await options?.onPayload?.({ reasoning: { effort: "medium", summary: "auto" } }, m.getModel(PROVIDER, OPTS.model)!);
      return fauxAssistantMessage("Done.");
    },
  ]);
  await run(m);
  assert.deepEqual(asked, { reasoning: { effort: "medium", summary: "detailed" } });
  assert.deepEqual(detailedSummaries({ reasoning: { effort: "none" } }), { reasoning: { effort: "none" } }, "no summary where none is asked for");
});

const CARD = {
  summary: "s".repeat(320),
  key_claim: "Probes on residual streams predict CoT unfaithfulness.",
  paper_kind: "method",
  evaluation_setup: { models: ["Qwen3-8B"], tasks: ["GSM8K"], generalization: "Not tested across models." },
  prior_work: [{ title: "Turpin et al.", relation: "Reuses the biased-context setup for hints." }],
  among_collection: [],
  code_available: false,
  weights_available: false,
  limitations: ["Only two models were evaluated in the study."],
};
const MODEL_NOTE = { kind: "input", source: "model", note: "The appendix tables came through the PDF unreadable." };
const cell = (code: string) => fauxAssistantMessage(fauxToolCall("run_code", { code }), { stopReason: "toolUse" });
/** A cell that fetches and carries on whatever the fetch did. */
const fetches = (...urls: string[]) => urls.map((u) => `try { await fetch(${JSON.stringify(u)}) } catch {}`).join("\n");

/** DNS as the production guard asks it: `lan.example` is on the LAN, every other name is unknown. */
async function withDns<T>(work: () => Promise<T>): Promise<T> {
  const real = dns.lookup;
  (dns as { lookup: unknown }).lookup = (host: string, _opts: unknown, cb: (e: Error | null, a: unknown) => void) =>
    host === "lan.example" ? cb(null, [{ address: "192.168.0.1", family: 4 }]) : cb(Object.assign(new Error(`getaddrinfo ENOTFOUND ${host}`), { code: "ENOTFOUND" }), []);
  try {
    return await work();
  } finally {
    (dns as { lookup: unknown }).lookup = real;
  }
}

test("the service's notes follow the model's, one per kind with a count, the events after the submit included", async () => {
  const { models: m, faux } = models();
  faux.setResponses([
    cell(fetches("http://10.0.0.1/", "http://lan.example/a", "http://gone.example/x")),
    // After the submit: one more refused, two past the budget of four, then a cell timeout.
    cell(`await submit(${JSON.stringify({ ...CARD, process_notes: [MODEL_NOTE] })});\n${fetches("http://10.0.0.2/", "https://example.org/", "https://example.org/b")}\nwhile (true) {}`),
  ]);
  const outcome = await withDns(() => processPaper(PAPER, { extensions: [], selections: [] }, m, { ...HOSTS, maxFetches: 4 }, { ...OPTS, cellTimeoutMs: 300 }));
  assert.equal(outcome.error, undefined);
  assert.deepEqual(outcome.result?.process_notes, [
    MODEL_NOTE,
    { kind: "access", source: "service", note: "3 fetches were refused: the address is not a public one.", url: "http://10.0.0.1/" },
    { kind: "access", source: "service", note: "A fetch failed on the network: DNS, the connection, a timeout or a body cut off.", url: "http://gone.example/x" },
    { kind: "budget", source: "service", note: "The fetch budget of 4 per paper was used up; 2 more fetches were refused." },
    { kind: "tool", source: "service", note: "A cell ran past its 0.3 s limit, which ended the session and lost its state." },
  ]);
});

test("the model can neither forge a service note nor drop one, and a quiet run adds none", async () => {
  const { models: m, faux } = models();
  const forged = { kind: "access", source: "service", note: "3 fetches were refused: the address is not a public one." };
  faux.setResponses([
    cell(`${fetches("http://10.0.0.1/")}\nawait submit(${JSON.stringify({ ...CARD, process_notes: [forged] })})`),
    cell(`await submit(${JSON.stringify({ ...CARD, process_notes: [] })})`),
  ]);
  const outcome = await processPaper(PAPER, { extensions: [], selections: [] }, m, HOSTS, OPTS);
  const submits = outcome.trace.filter((e) => e.type === "submit");
  assert.equal(submits[0]?.ok, false);
  assert.ok(submits[0]?.errors?.some((e) => e.startsWith("/process_notes/0/source")), "the service's source is refused from the model");
  assert.deepEqual(outcome.result?.process_notes, [{ kind: "access", source: "service", note: "A fetch was refused: the address is not a public one.", url: "http://10.0.0.1/" }], "the model's empty list does not drop it");

  const quiet = models();
  quiet.faux.setResponses([cell(`await submit(${JSON.stringify(CARD)})`)]);
  assert.deepEqual((await run(quiet.models)).result, CARD, "the card as the model gave it");
});
