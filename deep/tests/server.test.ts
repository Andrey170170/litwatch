import assert from "node:assert/strict";
import { test } from "node:test";
import { JobQueue } from "../src/jobs.ts";
import { buildServer } from "../src/server.ts";
import type { Outcome } from "../src/agent.ts";

const outcome = (id: string): Outcome => ({
  result: null,
  trace: [{ t: 1, type: "system", text: `ran ${id}` }],
  stats: { cells: 2, turns: 1, input: 10, output: 5, reasoning: 0, cache_read: 0, list_cost: 0.01, seconds: 1 },
  model: "fake",
  error: "the model stopped without submitting",
});

test("jobs run one at a time and expose progress, outcome, and health", async () => {
  const order: string[] = [];
  const seen: Record<string, unknown>[] = [];
  const jobs = new JobQueue(async ({ paper, extensions, selections }, progress) => {
    seen.push({ id: paper.id, extensions, selections });
    order.push(`start ${paper.id}`);
    progress({ t: 0, type: "cell", n: 1, code: "1", output: "1", ms: 1 }, { cells: 1, turns: 0, input: 0, output: 0, reasoning: 0, cache_read: 0, list_cost: 0, seconds: 0 });
    await new Promise((r) => setTimeout(r, 20));
    order.push(`end ${paper.id}`);
    return outcome(paper.id);
  });
  const server = buildServer({ jobs, health: async () => ({ ok: true }) });
  await new Promise<void>((r) => server.listen(0, r));
  const port = (server.address() as { port: number }).port;
  const base = `http://127.0.0.1:${port}`;
  try {
    const bad = await fetch(`${base}/process`, { method: "POST", body: JSON.stringify({ paper: { id: "x" } }) });
    assert.equal(bad.status, 422);
    const unknown = await fetch(`${base}/process`, {
      method: "POST",
      body: JSON.stringify({ paper: { id: "x", title: "T", body: "# T" }, extensions: ["nope"] }),
    });
    assert.equal(unknown.status, 400);
    assert.match(((await unknown.json()) as { error: string }).error, /unknown extension "nope"/);
    const submit = async (id: string, extra: Record<string, unknown> = {}) =>
      (await (await fetch(`${base}/process`, { method: "POST", body: JSON.stringify({ paper: { id, title: "T", body: "# T" }, ...extra }) })).json()) as { id: string };
    const selections = [{ name: "cot-faithfulness", subject: "CoT faithfulness", rubric: "Measures unfaithfulness." }];
    const a = await submit("a", { extensions: ["faithfulness"], selections });
    const b = await submit("b");
    const health = (await (await fetch(`${base}/healthz`)).json()) as Record<string, unknown>;
    assert.equal(health.running, a.id);
    assert.equal(health.queued, 1);
    let job: Record<string, unknown> = {};
    for (let i = 0; i < 50 && job.status !== "error"; i++) {
      await new Promise((r) => setTimeout(r, 10));
      job = (await (await fetch(`${base}/jobs/${b.id}`)).json()) as Record<string, unknown>;
    }
    assert.equal(job.status, "error");
    assert.equal(job.error, "the model stopped without submitting");
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
    server.close();
  }
});
