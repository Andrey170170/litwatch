import assert from "node:assert/strict";
import { test } from "node:test";
import { fauxAssistantMessage, type Models } from "@earendil-works/pi-ai";
import { Value } from "typebox/value";
import { LinkJobSchema, LinksAnswerSchema, type LinksInput } from "../src/contracts.ts";
import { JobQueue } from "../src/jobs.ts";
import { confirms, type Fetched, findLinks, LinkQueue, shows } from "../src/links.ts";
import { buildServer } from "../src/server.ts";
import { cell, HOSTS, models, OPEN, OPTS, PAPER, withSite } from "./support.ts";

const INPUT: LinksInput = { paper: PAPER, comments: "Code: https://github.com/a/probes" };
const find = (m: Models, site: string) => findLinks(INPUT, m, { ...HOSTS, litwatchUrl: site, guard: OPEN }, OPTS);
const answers = (links: unknown[]) => cell(`console.log(JSON.stringify(await answer(${JSON.stringify({ links })})))`);

test("a link is confirmed by a fetch of that URL, or of its repository's API page", () => {
  assert.ok(confirms("https://github.com/a/probes", "HTTPS://GITHUB.COM/a/probes/"), "the scheme's and host's case, a trailing slash");
  assert.ok(confirms("https://github.com/a/probes", "https://api.github.com/repos/a/probes"));
  assert.ok(confirms("https://huggingface.co/a/m", "https://huggingface.co/api/models/a/m"));
  assert.ok(confirms("https://huggingface.co/datasets/a/d", "https://huggingface.co/api/datasets/a/d"));
  assert.ok(confirms("https://o.io/download?file=weights", "https://o.io/download?file=weights"));
  assert.ok(!confirms("https://o.io/download?file=weights", "https://o.io/download?file=readme"), "another query is another resource");
  assert.ok(!confirms("https://github.com/a/probes", "https://github.com/a/probes/tree/main"), "a page under it is not it");
  assert.ok(!confirms("https://github.com/a/probes", "https://api.github.com/repos/a/probes/contents/nope"), "a child's contents speak for that child");
  assert.ok(!confirms("https://github.com/a/probes", "https://github.com/a/probes-v2"), "a sibling is not the repository");
  assert.ok(!confirms("https://github.com/a/probes", "https://api.github.com/repos/a/other"));
  assert.ok(!confirms("https://huggingface.co/a/m", "https://huggingface.co/api/datasets/a/m"), "a dataset is not the model");
  assert.ok(!confirms("not a url", "https://github.com/a"));
});

test("the contract takes an absolute http(s) URL and a time in UTC, nothing else", () => {
  const link = { url: "https://github.com/a/probes", kind: "code", state: "public", checked_at: "2026-11-26T03:00:00.000Z" };
  assert.ok(Value.Check(LinksAnswerSchema, { links: [link] }));
  for (const url of ["not a URL", "github.com/a/probes", "ftp://o.io/x", "https:///x"]) {
    assert.ok(!Value.Check(LinksAnswerSchema, { links: [{ ...link, url }] }), url);
  }
  for (const checked_at of ["2026-11-26", "2026-11-26T03:00:00+01:00", "yesterday"]) {
    assert.ok(!Value.Check(LinksAnswerSchema, { links: [{ ...link, checked_at }] }), checked_at);
  }
});

test("a fetch shows a state only by what it answered", () => {
  const at = "2026-11-26T03:00:00.000Z";
  const page = (status?: number, extra: Partial<Fetched> = {}): Fetched => ({ asked: "https://o.io/x", answered: "https://o.io/x", status, at, ...extra });
  const api = (status: number): Fetched => ({ asked: "https://api.github.com/repos/a/p", answered: "https://api.github.com/repos/a/p", status, at });
  assert.ok(shows("public", page(200)) && shows("placeholder", page(204)));
  assert.ok(!shows("public", page(404)) && !shows("public", page(429)) && !shows("public", page(500)));
  assert.ok(shows("missing", page(404)) && shows("missing", page(410)) && shows("missing", page(undefined)), "a name that does not resolve leads nowhere");
  assert.ok(!shows("missing", page(429)) && !shows("missing", page(403)));
  assert.ok(shows("gated", page(200, { gated: true })) && !shows("public", page(200, { gated: true })));
  assert.ok(shows("gated", page(401)) && shows("gated", page(403)));
  assert.ok(!shows("gated", api(403)), "GitHub's API refusing is its rate limit, no gate");
  assert.ok(!shows("gated", page(200)));
});

test("an answer whose state its fetch contradicts is refused", async () => {
  await withSite(async ({ url }) => {
    const { models: m, faux } = models();
    const link = { url: `${url}/a/probes`, kind: "code", state: "public", license: null };
    faux.setResponses([cell(`await fetch(${JSON.stringify(link.url)})`), answers([link]), answers([{ ...link, state: "missing" }])]);
    const outcome = await find(m, url);
    assert.deepEqual(outcome.result?.links.map((l) => l.state), ["missing"]);
    const submits = outcome.trace.filter((e) => e.type === "submit");
    assert.deepEqual(submits.map((e) => e.ok), [false, true]);
    assert.match((submits[0] as { errors: string[] }).errors[0]!, /its fetch answered 404, which does not show it public/);
  });
});

test("a finished find is kept for its retention time, past the count kept", async () => {
  const done = async () => ({ result: { links: [] }, trace: [], stats: { cells: 0, turns: 0, input: 0, output: 0, reasoning: 0, cache_read: 0, list_cost: 0, seconds: 0 }, model: "fake" });
  const settle = () => new Promise((r) => setTimeout(r, 20));
  const kept = new LinkQueue(done, 1);
  const pruned = new LinkQueue(done, 1, 0);
  for (const q of [kept, pruned]) {
    q.submit(INPUT, "first");
    await settle();
    q.submit(INPUT, "second");
    await settle();
    q.submit(INPUT, "third");
    await settle();
  }
  assert.ok(kept.byKey("first"), "kept by default for hours");
  assert.equal(pruned.byKey("first"), undefined, "with no retention time, only the count is kept");
});

test("an answer naming a link not fetched is refused; fetched, it is kept with when it was checked", async () => {
  await withSite(async ({ url, asked }) => {
    const { models: m, faux } = models();
    const link = { url: `${url}/a/probes`, kind: "code", state: "missing", license: null };
    faux.setResponses([answers([link]), cell(`await fetch(${JSON.stringify(link.url)})`), answers([link, link])]);
    const before = new Date().toISOString();
    const outcome = await find(m, url);
    assert.equal(outcome.error, undefined);
    assert.deepEqual(asked, ["/a/probes"]);
    const [kept, ...more] = outcome.result!.links;
    assert.deepEqual(more, [], "a link given twice is kept once");
    assert.deepEqual({ ...kept, checked_at: undefined }, { url: link.url, kind: "code", state: "missing", checked_at: undefined }, "a null license is left out");
    assert.ok(kept!.checked_at >= before && !Number.isNaN(Date.parse(kept!.checked_at)));
    const submits = outcome.trace.filter((e) => e.type === "submit");
    assert.deepEqual(submits.map((e) => e.ok), [false, true]);
    assert.match((submits[0] as { errors: string[] }).errors[0]!, /was not fetched in this session/);
  });
});

test("no link found is an answer, and the comments and the text reach the finder", async () => {
  const { models: m, faux } = models();
  faux.setResponses([cell("console.log(meta.comments, paper.length)"), answers([])]);
  const outcome = await find(m, "http://litwatch.invalid");
  assert.deepEqual([outcome.result, outcome.error, outcome.failure], [{ links: [] }, undefined, undefined]);
  const first = outcome.trace.find((e) => e.type === "cell");
  assert.equal(first?.type === "cell" && first.output, `${INPUT.comments} ${PAPER.body.length}`);
});

test("a finder that never answers is the paper's failure, a failed model call the service's", async () => {
  const { models: m, faux } = models();
  faux.setResponses([fauxAssistantMessage("Done."), fauxAssistantMessage("Done."), fauxAssistantMessage("Done.")]);
  const silent = await find(m, "http://litwatch.invalid");
  assert.deepEqual([silent.result, silent.error, silent.failure], [null, "the finder stopped without answering", "paper"]);
  faux.setResponses([fauxAssistantMessage("", { stopReason: "error", errorMessage: "usage limit reached" })]);
  const failed = await find(m, "http://litwatch.invalid");
  assert.deepEqual([failed.error, failed.failure], ["usage limit reached", "service"]);
});

test("a find is submitted by its key once, found by it, and reported as the contract says", async () => {
  let runs = 0;
  const links = new LinkQueue(async () => {
    runs += 1;
    return { result: { links: [] }, trace: [], stats: { cells: 1, turns: 1, input: 1, output: 1, reasoning: 0, cache_read: 0, list_cost: 0.01, seconds: 1 }, model: "fake" };
  });
  const server = buildServer({ jobs: new JobQueue(async () => assert.fail("no read was asked for")), links, health: async () => ({ ok: true }) });
  await new Promise<void>((r) => server.listen(0, r));
  const base = `http://127.0.0.1:${(server.address() as { port: number }).port}`;
  const post = (body: unknown, key = "links:7:a") => fetch(`${base}/links`, { method: "POST", headers: { "idempotency-key": key }, body: JSON.stringify(body) });
  try {
    assert.equal((await post({ paper: { id: "x" } }, "other")).status, 422);
    const first = await post(INPUT);
    assert.equal(first.status, 202);
    const { id } = (await first.json()) as { id: string };
    const again = await post(INPUT);
    assert.equal(again.status, 200);
    assert.equal(((await again.json()) as { id: string }).id, id);
    assert.equal(((await (await fetch(`${base}/links/keys/${encodeURIComponent("links:7:a")}`)).json()) as { id: string }).id, id);
    assert.equal((await fetch(`${base}/links/keys/nope`)).status, 404);
    let job: Record<string, unknown> = {};
    for (let i = 0; i < 50 && job.status !== "done"; i++) {
      await new Promise((r) => setTimeout(r, 10));
      job = (await (await fetch(`${base}/links/${id}`)).json()) as Record<string, unknown>;
    }
    assert.deepEqual(job.result, { links: [] });
    assert.ok(Value.Check(LinkJobSchema, job), "a finished find is reported as the contract says");
    assert.equal(runs, 1);
    assert.equal((await fetch(`${base}/jobs/${id}`)).status, 404, "a find is not a read");
  } finally {
    server.close();
  }
});
