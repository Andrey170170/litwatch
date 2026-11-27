import assert from "node:assert/strict";
import { test } from "node:test";
import { Value } from "typebox/value";
import { JobQueue } from "../src/jobs.ts";
import { LinkQueue } from "../src/links.ts";
import { buildServer, refusal } from "../src/server.ts";
import type { Outcome } from "../src/agent.ts";
import { JobInputSchema, JobRefSchema, JobSchema } from "../src/contracts.ts";
import { BODY, latex, sentJob } from "./support.ts";

/** A finds queue no test here submits to. */
const noFinds = () => new LinkQueue(async () => assert.fail("no find was asked for"));

/** A paper as litwatch sends one, a PDF's text unless `over` says otherwise. */
const paper = (over: Record<string, unknown> = {}) => ({ id: "a", title: "T", abstract: "", authors: [], body: "# T", body_source: "pdf", ...over });

const outcome = (id: string): Outcome => ({
  result: null,
  figures: [],
  reviews: null,
  trace: [{ t: 1, type: "system", text: `ran ${id}` }],
  stats: { cells: 2, turns: 1, input: 10, output: 5, reasoning: 0, cache_read: 0, list_cost: 0.01, seconds: 1 },
  model: "fake",
  error: "the model stopped without submitting",
  failure: "paper",
});

test("jobs run one at a time and expose progress, outcome, and health", async () => {
  const order: string[] = [];
  const seen: Record<string, unknown>[] = [];
  // Job a runs until the test has seen it running, whatever the machine's load.
  let releaseA!: () => void;
  const gateA = new Promise<void>((r) => (releaseA = r));
  const jobs = new JobQueue(async ({ paper, extensions, selections }, progress) => {
    seen.push({ id: paper.id, extensions, selections });
    order.push(`start ${paper.id}`);
    progress({ t: 0, type: "cell", n: 1, code: "1", output: "1", ms: 1 }, { cells: 1, turns: 0, input: 0, output: 0, reasoning: 0, cache_read: 0, list_cost: 0, seconds: 0 });
    await (paper.id === "a" ? gateA : new Promise((r) => setTimeout(r, 20)));
    order.push(`end ${paper.id}`);
    return outcome(paper.id);
  });
  const server = buildServer({ jobs, links: noFinds(), health: async () => ({ ok: true }) });
  await new Promise<void>((r) => server.listen(0, r));
  const port = (server.address() as { port: number }).port;
  const base = `http://127.0.0.1:${port}`;
  try {
    const post = (body: unknown) => fetch(`${base}/process`, { method: "POST", body: JSON.stringify(body) });
    const bad = await post({ paper: { id: "x" } });
    assert.equal(bad.status, 422);
    assert.match(((await bad.json()) as { error: string }).error, /not a job's input: \/paper: must have required properties title/);
    // the contract is exact: a field it does not name is a submission gone wrong
    assert.equal((await post({ paper: paper({ id: "x", pages: 3 }) })).status, 422);
    // a LaTeX source comes with its outline, and a PDF's text without one
    assert.equal((await post({ paper: paper({ id: "x", body: BODY, body_source: "latex" }) })).status, 422);
    assert.equal((await post({ paper: paper({ id: "x", outline: { sections: [] } }) })).status, 422);
    // and every text says where it comes from: a file from before the outline names none, or null
    for (const body_source of [undefined, null]) assert.equal((await post({ paper: paper({ id: "x", body: BODY, body_source }) })).status, 422);
    const unknown = await post({ paper: paper({ id: "x" }), extensions: ["nope"] });
    assert.equal(unknown.status, 400);
    assert.match(((await unknown.json()) as { error: string }).error, /unknown extension "nope"/);
    const submit = async (id: string, extra: Record<string, unknown> = {}) =>
      (await (await post({ paper: paper({ id }), ...extra })).json()) as { id: string };
    const selections = [{ name: "cot-faithfulness", subject: "CoT faithfulness", rubric: "Measures unfaithfulness." }];
    const a = await submit("a", { extensions: ["faithfulness"], selections });
    const b = await post({ paper: { ...paper({ id: "b" }), ...latex(BODY) } }).then((r) => r.json() as Promise<{ id: string }>);
    assert.ok(Value.Check(JobRefSchema, a), "a submission is answered as the contract says");
    const health = (await (await fetch(`${base}/healthz`)).json()) as Record<string, unknown>;
    assert.equal(health.running, a.id);
    assert.equal(health.queued, 1);
    releaseA();
    let job: Record<string, unknown> = {};
    for (let i = 0; i < 50 && job.status !== "error"; i++) {
      await new Promise((r) => setTimeout(r, 10));
      job = (await (await fetch(`${base}/jobs/${b.id}`)).json()) as Record<string, unknown>;
    }
    assert.equal(job.status, "error");
    assert.ok(Value.Check(JobSchema, job), "a job is reported as the contract says");
    assert.equal(job.error, "the model stopped without submitting");
    assert.equal(job.failure, "paper");
    assert.deepEqual(job.trace, [{ t: 1, type: "system", text: "ran b" }]);
    assert.deepEqual(order, ["start a", "end a", "start b", "end b"]);
    assert.deepEqual(seen, [
      { id: "a", extensions: ["faithfulness"], selections },
      { id: "b", extensions: [], selections: [] },
    ]);
    assert.deepEqual(((await (await fetch(`${base}/jobs/${a.id}`)).json()) as { extensions: string[] }).extensions, ["faithfulness"]);
    const list = (await (await fetch(`${base}/jobs`)).json()) as Record<string, unknown>[];
    assert.equal(list.length, 2);
    assert.ok(!("trace" in list[0]));
    assert.equal((await fetch(`${base}/jobs/00000000-0000-0000-0000-000000000000`)).status, 404);
  } finally {
    releaseA();
    server.close();
  }
});

test("a submission's key answers with the job it started, and a runner that throws fails the service", async () => {
  let runs = 0;
  const jobs = new JobQueue(async () => {
    runs += 1;
    throw new Error("not running on the ChatGPT subscription: no credential stored; run `login`");
  });
  const server = buildServer({ jobs, links: noFinds(), health: async () => ({ ok: true }) });
  await new Promise<void>((r) => server.listen(0, r));
  const base = `http://127.0.0.1:${(server.address() as { port: number }).port}`;
  const submit = (key?: string) =>
    fetch(`${base}/process`, {
      method: "POST",
      headers: key ? { "idempotency-key": key } : {},
      body: JSON.stringify({ paper: paper() }),
    });
  try {
    const first = await submit("7:a");
    assert.equal(first.status, 202);
    const { id } = (await first.json()) as { id: string };
    const again = await submit("7:a");
    assert.equal(again.status, 200);
    assert.equal(((await again.json()) as { id: string }).id, id);
    assert.equal(((await (await fetch(`${base}/keys/${encodeURIComponent("7:a")}`)).json()) as { id: string }).id, id);
    assert.equal((await fetch(`${base}/keys/${encodeURIComponent("9:a")}`)).status, 404);
    const other = (await (await submit("8:a")).json()) as { id: string };
    assert.notEqual(other.id, id);
    let job: Record<string, unknown> = {};
    for (let i = 0; i < 50 && job.status !== "error"; i++) {
      await new Promise((r) => setTimeout(r, 10));
      job = (await (await fetch(`${base}/jobs/${id}`)).json()) as Record<string, unknown>;
    }
    assert.equal(job.failure, "service");
    assert.equal(job.key, "7:a");
    assert.equal(((await (await submit("7:a")).json()) as { id: string }).id, id); // ended, still kept
    for (let i = 0; i < 50 && runs < 2; i++) await new Promise((r) => setTimeout(r, 10));
    assert.equal(runs, 2);
  } finally {
    server.close();
  }
});

test("a key is forgotten with its job", async () => {
  const jobs = new JobQueue(async () => outcome("x"), 1);
  const paper = { id: "a", title: "T", abstract: "", authors: [], body: "# T", body_source: "pdf" as const };
  const first = jobs.submit(paper, [], [], "1:a");
  for (let i = 0; i < 50 && first.status !== "error"; i++) await new Promise((r) => setTimeout(r, 5));
  const second = jobs.submit(paper, [], [], "2:a");
  for (let i = 0; i < 50 && second.status !== "error"; i++) await new Promise((r) => setTimeout(r, 5));
  jobs.submit(paper, [], [], "3:a"); // prunes the first, keeping one finished
  assert.equal(jobs.byKey("1:a"), undefined);
  assert.notEqual(jobs.submit(paper, [], [], "1:a").id, first.id);
});

test("a finished job carries the read, its figures and its reviews, and the text's version reaches the run", async () => {
  const read = { format: "read", sections: [], among_collection: [] } as unknown as NonNullable<Outcome["result"]>;
  const figures: Outcome["figures"] = [{ name: "fig-1", mime: "image/webp", data: "UklGRg==" }];
  const reviews: Outcome["reviews"] = [
    { round: 1, passed: true, scores: { faithfulness: 5, clarity: 4, depth: 4, calibration: 5, followability: 4 }, checks: { anchors: true, experiments: true, why_boxes: true, voice: true, length: true }, findings: [], retelling: "A probe beats chance." },
  ];
  const versions: unknown[] = [];
  const jobs = new JobQueue(async ({ paper }) => {
    versions.push(paper.body_version);
    // the second paper's read shows no figure and was never reviewed
    return { ...outcome(paper.id), result: read, error: undefined, failure: undefined, ...(paper.id === "a" ? { figures, reviews } : {}) };
  });
  const server = buildServer({ jobs, links: noFinds(), health: async () => ({ ok: true }) });
  await new Promise<void>((r) => server.listen(0, r));
  const base = `http://127.0.0.1:${(server.address() as { port: number }).port}`;
  const submit = (over: Record<string, unknown>) => fetch(`${base}/process`, { method: "POST", body: JSON.stringify({ paper: paper(over) }) });
  const finished = async (id: string) => {
    let job: Record<string, unknown> = {};
    for (let i = 0; i < 50 && job.status !== "done"; i++) {
      await new Promise((r) => setTimeout(r, 10));
      job = (await (await fetch(`${base}/jobs/${id}`)).json()) as Record<string, unknown>;
    }
    return job;
  };
  try {
    for (const body_version of ["2", 0, 1.5, -1, true]) assert.equal((await submit({ id: "x", body_version })).status, 422, `body_version ${JSON.stringify(body_version)}`);
    const a = await finished(((await (await submit({ id: "a", body_version: 3 })).json()) as { id: string }).id);
    assert.deepEqual([a.result, a.figures, a.reviews], [read, figures, reviews]);
    assert.ok(Value.Check(JobSchema, a), "a finished job is reported as the contract says");
    const b = await finished(((await (await submit({ id: "b", body_version: null })).json()) as { id: string }).id);
    assert.ok(!("figures" in b), "no figures field for a read that shows none");
    assert.equal(b.reviews, null);
    await finished(((await (await submit({ id: "c" })).json()) as { id: string }).id);
    assert.deepEqual(versions, [3, null, undefined], "the version as litwatch sent it; null or absent when it cannot say");
    const list = (await (await fetch(`${base}/jobs`)).json()) as Record<string, unknown>[];
    assert.ok(list.every((j) => !("figures" in j) && !("reviews" in j) && !("result" in j)), "the list stays light");
  } finally {
    server.close();
  }
});

test("a paper arXiv gives no title is still a job's input", () => {
  assert.equal(refusal({ paper: paper({ title: "" }) }), undefined);
  assert.notEqual(refusal({ paper: paper({ title: undefined }) }), undefined, "but the title is never left out");
});

test("what litwatch's code sends of a LaTeX source and of a PDF's text is a job's input", () => {
  for (const name of ["job-latex.json", "job-pdf.json"] as const) {
    const sent = sentJob(name);
    assert.equal(refusal(sent), undefined, name);
    assert.ok(Value.Check(JobInputSchema, sent), name);
  }
});
