import assert from "node:assert/strict";
import { test } from "node:test";
import { createModels, fauxAssistantMessage, fauxProvider, type Models } from "@earendil-works/pi-ai";
import { processPaper, type RunOptions } from "../src/agent.ts";
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
