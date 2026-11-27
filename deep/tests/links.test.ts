import assert from "node:assert/strict";
import { test } from "node:test";
import { fauxAssistantMessage, type Models } from "@earendil-works/pi-ai";
import { Value } from "typebox/value";
import { LinkJobSchema, LinksAnswerSchema, type LinksInput } from "../src/contracts.ts";
import { JobQueue } from "../src/jobs.ts";
import { readFileSync } from "node:fs";
import { apiGet, apiTarget, hostState, MAX_BYTES, type Get } from "../src/hostcheck.ts";
import { answered, confirms, type Fetched, findLinks, LinkQueue, normal, shows } from "../src/links.ts";
import { buildServer } from "../src/server.ts";
import { cell, HOSTS, models, OPEN, OPTS, PAPER, withSite } from "./support.ts";

const INPUT: LinksInput = { paper: PAPER, comments: "Code: https://github.com/a/probes" };
const find = (m: Models, site: string) => findLinks(INPUT, m, { ...HOSTS, litwatchUrl: site, guard: OPEN }, OPTS);
const answers = (links: unknown[]) => cell(`console.log(JSON.stringify(await answer(${JSON.stringify({ links })})))`);

test("a link elsewhere is confirmed by a fetch of that URL, its scheme's and host's case and a trailing slash aside", () => {
  assert.ok(confirms("https://o.io/probes", "HTTPS://O.IO/probes/"));
  assert.ok(confirms("https://o.io/download?file=weights", "https://o.io/download?file=weights"));
  assert.ok(!confirms("https://o.io/download?file=weights", "https://o.io/download?file=readme"), "another query is another resource");
  assert.ok(!confirms("https://o.io/probes", "https://o.io/probes/tree/main"), "a page under it is not it");
  assert.ok(!confirms("https://o.io/probes", "https://o.io/probes-v2"), "a sibling is not it");
  assert.ok(!confirms("not a url", "https://github.com/a"));
  assert.equal(normal("https://["), null);
  assert.equal(normal("ftp://o.io/x"), null);
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

test("a fetch of a link elsewhere shows a state by its status and the page the model quotes", () => {
  const at = "2026-11-26T03:00:00.000Z";
  const text = "Weights: download the 7B checkpoint below. Request access by filling in this form.";
  const page = (status?: number): Fetched => ({ asked: "https://o.io/x", answered: "https://o.io/x", status, text, at });
  const quote = "download the 7B checkpoint below";
  assert.ok(shows("public", page(200), quote) && shows("placeholder", page(204), quote));
  assert.ok(!shows("public", page(200)), "a page that answered, unquoted, shows nothing");
  assert.ok(!shows("public", page(200), "a passage the page does not have"));
  assert.ok(!shows("public", page(404), quote) && !shows("public", page(429), quote) && !shows("public", page(500), quote));
  assert.ok(shows("missing", page(404)) && shows("missing", page(410)) && shows("missing", page(undefined)), "a name that does not resolve leads nowhere");
  assert.ok(!shows("missing", page(429)) && !shows("missing", page(403)));
  assert.ok(shows("gated", page(401)) && shows("gated", page(403)) && shows("gated", page(200), "request access by filling in this form"));
  assert.ok(!shows("gated", page(200)));
});

type Answers = Record<string, { status: number; json?: unknown; location?: string }>;

/** The network as the table gives it, under the checks' own GET (`apiGet`): each URL's answer, absent a 404; a
 * redirect answered as such, for `apiGet` to follow or not. */
const network =
  (answers: Answers, asked: string[] = []) =>
  async (url: string): Promise<Response> => {
    asked.push(url);
    const got = answers[url];
    if (!got) return new Response("", { status: 404 });
    if (got.location) return new Response(null, { status: got.status, headers: { location: got.location } });
    return new Response("json" in got ? JSON.stringify(got.json) : "", { status: got.status });
  };
const tableGet = (answers: Answers, asked?: string[]): Get => apiGet(network(answers, asked));

test("a host's API says what a link holds, as the table litwatch's re-check shares says", async () => {
  const table = JSON.parse(readFileSync(new URL("./fixtures/host-checks.json", import.meta.url), "utf8")) as {
    cases: { name: string; url: string; page?: string; kind: "code" | "weights"; answers: Answers; expect: null | "unsupported" | { state: string; license?: string } }[];
  };
  for (const c of table.cases) {
    const asked: string[] = [];
    const got = await hostState(c.url, c.kind, tableGet(c.answers, asked));
    const said = got === undefined ? "unsupported" : got === null ? null : { state: got.state, ...(got.license ? { license: got.license } : {}) };
    assert.deepEqual(said, c.expect, c.name);
    const api = apiTarget(c.url)?.api;
    if (api) assert.equal(apiTarget(c.page ?? c.url)?.page, c.page ?? c.url, `${c.name}: the page it answers`);
    const off = asked.filter((u) => !api || new URL(u).protocol !== "https:" || new URL(u).hostname !== new URL(api).hostname);
    assert.deepEqual(off, [], `${c.name}: nothing asked off the API's host`);
  }
});

test("the checks' own GET reads a large answer whole, up to the bound litwatch's re-check takes too", async () => {
  const tree = { tree: [...Array.from({ length: 10000 }, (_, i) => ({ path: `src/module_${i}/file_${i}.py`, type: "blob" })), { path: "ckpt/model.safetensors", type: "blob" }] };
  const answers: Answers = {
    "https://api.github.com/repos/o/big": { status: 200, json: { full_name: "o/big", default_branch: "main", license: null } },
    "https://api.github.com/repos/o/big/git/trees/main?recursive=1": { status: 200, json: tree },
  };
  assert.ok(JSON.stringify(tree).length > 300_000);
  assert.deepEqual(await hostState("https://github.com/o/big", "weights", tableGet(answers)), { state: "public", status: 200 });
  const huge = apiGet(async () => new Response("x".repeat(MAX_BYTES + 1), { status: 200 }));
  assert.equal(await huge("https://api.github.com/repos/o/huge"), null);
});

test("the checks' own GET stops with the find: none starts once its session is aborted", async () => {
  const session = new AbortController();
  let asked = 0;
  const get = apiGet(async (_url, init) => {
    asked++;
    assert.ok(init.signal, "each request carries the find's signal with its timeout");
    session.abort(); // the find ends while its first request is answered
    return new Response(null, { status: 301, headers: { location: "https://api.github.com/repos/o/new" } });
  }, session.signal);
  assert.equal(await get("https://api.github.com/repos/o/old"), null);
  assert.equal(asked, 1, "the redirect is not followed once the find has ended");
  assert.equal(await get("https://api.github.com/repos/o/r"), null);
  assert.equal(asked, 1);
});

test("a find's host checks, by its own transport, stop when the find ends: the one under way cancelled, no next one", async () => {
  const { models: m, faux } = models();
  const repo = (name: string) => ({ url: `https://github.com/o/${name}`, kind: "code", state: "public", license: null });
  faux.setResponses([answers([repo("first"), repo("second")])]);
  const asked: { url: string; cancelled: boolean }[] = [];
  const real = globalThis.fetch;
  globalThis.fetch = (async (url: string | URL | Request, init?: RequestInit) => {
    const call = { url: String(url), cancelled: false };
    asked.push(call);
    return new Promise<Response>((resolve, reject) => {
      const answered = setTimeout(() => resolve(new Response("", { status: 404 })), 3000); // were it never cancelled
      init?.signal?.addEventListener(
        "abort",
        () => {
          clearTimeout(answered);
          call.cancelled = true;
          reject(init.signal?.reason);
        },
        { once: true },
      );
    });
  }) as typeof fetch;
  try {
    // No `hostGet`: the find's default transport. Its wall clock ends it while the first check is under way.
    const outcome = await findLinks(INPUT, m, { ...HOSTS, guard: OPEN }, { ...OPTS, wallClockMs: 500 });
    await new Promise((r) => setTimeout(r, 50));
    assert.match(outcome.error ?? "", /wall clock/);
  } finally {
    globalThis.fetch = real;
  }
  assert.deepEqual(asked, [{ url: "https://api.github.com/repos/o/first", cancelled: true }], "the second check never started");
});

test("a GitHub or Hugging Face link takes its state from the host's API, whatever the draft says", async () => {
  const { models: m, faux } = models();
  const hf = { url: "https://huggingface.co/o/m", kind: "weights", state: "public", license: null };
  const gh = { url: "https://github.com/o/r", kind: "code", state: "public", license: null };
  faux.setResponses([answers([hf, gh, { ...gh, url: "HTTPS://GITHUB.COM/o/r/" }])]);
  const get = tableGet({
    "https://huggingface.co/api/models/o/m": { status: 200, json: { gated: "manual", siblings: [{ rfilename: "model.safetensors" }] } },
    "https://api.github.com/repos/o/r": { status: 200, json: { full_name: "o/r", default_branch: "main", license: { spdx_id: "MIT" } } },
    "https://api.github.com/repos/o/r/contents": { status: 200, json: [{ path: "README.md" }] },
  });
  const outcome = await findLinks(INPUT, m, { ...HOSTS, guard: OPEN }, OPTS, () => undefined, get);
  assert.deepEqual(
    outcome.result?.links.map((l) => [l.url, l.state, l.license]),
    [
      [hf.url, "gated", undefined],
      [gh.url, "placeholder", "MIT"],
    ],
    "the same repository given twice, by another spelling, is kept once",
  );
  const submit = outcome.trace.find((e) => e.type === "submit") as { notes?: string[] };
  assert.match(submit.notes?.join("\n") ?? "", /gated by its host's API \(answered 200\), not public/);
});

test("a link given twice is kept the first time it is accepted: one dropped before it does not stand in for it", async () => {
  const at = "2026-11-26T03:00:00.000Z";
  const fetched: Fetched[] = [{ asked: "https://o.io/x", answered: "https://o.io/x", status: 200, text: "Download the 7B checkpoint below.", at }];
  const link = { url: "https://o.io/x", kind: "weights", state: "public", license: null };
  const got = await answered({ links: [link, { ...link, url: "https://o.io/x/", evidence: "download the 7B checkpoint" }] }, fetched, async () => undefined);
  assert.ok("answer" in got);
  assert.deepEqual(got.answer.links.map((l) => l.url), ["https://o.io/x/"]);
  assert.match(got.notes.join("\n"), /dropped, its page answered with no quote/);
});

test("a GitHub or Hugging Face link is answered by its repository's own page, as litwatch's re-check knows it", async () => {
  const draft = (url: string) => ({ url, kind: "code", state: "public", license: null });
  const urls = ["https://github.com/o/archive/../r", "https://www.github.com/o/s.git", "https://huggingface.co/datasets/o/d/"];
  const got = await answered({ links: urls.map(draft) }, [], async () => ({ state: "public", status: 200 }));
  assert.ok("answer" in got);
  assert.deepEqual(got.answer.links.map((l) => l.url), ["https://github.com/o/r", "https://github.com/o/s", "https://huggingface.co/datasets/o/d"]);
});

test("a link elsewhere whose page answered, unquoted, is dropped", async () => {
  await withSite(async ({ url }) => {
    const { models: m, faux } = models();
    const link = { url: `${url}/e-print/x`, kind: "code", state: "public", license: null };
    faux.setResponses([cell(`await fetch(${JSON.stringify(link.url)})`), answers([link])]);
    const outcome = await find(m, url);
    assert.deepEqual(outcome.result?.links, []);
    const submit = outcome.trace.find((e) => e.type === "submit") as { notes?: string[] };
    assert.match(submit.notes?.[0] ?? "", /dropped, its page answered with no quote/);
  });
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
