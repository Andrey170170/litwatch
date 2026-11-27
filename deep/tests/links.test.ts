import assert from "node:assert/strict";
import { test } from "node:test";
import { fauxAssistantMessage, type Models } from "@earendil-works/pi-ai";
import { Value } from "typebox/value";
import { LinkJobSchema, type LinksInput } from "../src/contracts.ts";
import { JobQueue } from "../src/jobs.ts";
import { confirms, findLinks, LinkQueue } from "../src/links.ts";
import { buildServer } from "../src/server.ts";
import { cell, HOSTS, models, OPEN, OPTS, PAPER, withSite } from "./support.ts";

const INPUT: LinksInput = { paper: PAPER, comments: "Code: https://github.com/a/probes" };
const find = (m: Models, site: string) => findLinks(INPUT, m, { ...HOSTS, litwatchUrl: site, guard: OPEN }, OPTS);
const answers = (links: unknown[]) => cell(`console.log(JSON.stringify(await answer(${JSON.stringify({ links })})))`);

test("a link is confirmed by a fetch of its page, of a page under it, or of its host's API", () => {
  assert.ok(confirms("https://github.com/A/Probes", "https://www.github.com/a/probes/"));
  assert.ok(confirms("https://github.com/a/probes", "https://github.com/a/probes/tree/main"));
  assert.ok(confirms("https://github.com/a/probes", "https://api.github.com/repos/a/probes/contents"));
  assert.ok(confirms("https://huggingface.co/a/m", "https://huggingface.co/api/models/a/m"));
  assert.ok(confirms("https://huggingface.co/datasets/a/d", "https://huggingface.co/api/datasets/a/d"));
  assert.ok(!confirms("https://github.com/a/probes", "https://github.com/a/probes-v2"), "a sibling is not the repository");
  assert.ok(!confirms("https://github.com/a/probes", "https://api.github.com/repos/a/other"));
  assert.ok(!confirms("https://huggingface.co/a/m", "https://huggingface.co/api/datasets/a/m"), "a dataset is not the model");
  assert.ok(!confirms("not a url", "https://github.com/a"));
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
