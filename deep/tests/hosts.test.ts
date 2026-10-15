import assert from "node:assert/strict";
import { test } from "node:test";
import { buildHosts, storeHits } from "../src/hosts.ts";

test("store.search asks v1's papers table with the read token and keeps the old hit shape", async () => {
  const asked: { url: string; auth: string | null }[] = [];
  const realFetch = globalThis.fetch;
  globalThis.fetch = (async (url: string, init?: RequestInit) => {
    asked.push({ url: String(url), auth: new Headers(init?.headers).get("authorization") });
    return Response.json({
      rows: [
        { id: "2610.00001", title: "Hidden steps", authors: ["Ann Lee"], published: "2026-10-01T00:00:00Z", badges: [{ grade: "must_read" }, { grade: "adjacent" }], gist: "It finds them." },
        { id: "2610.00002", title: "Near it", authors: [], published: null, badges: [], gist: null },
      ],
      total: 2,
      outside: [{ id: "2401.00009", title: "Only in the archive", authors: ["Bo Chen"], published: "2024-01-05T00:00:00Z", source: "archive" }],
      left_out: [],
    });
  }) as typeof fetch;
  try {
    const hosts = buildHosts({ litwatchUrl: "http://litwatch.test", litwatchToken: "r3ad", maxFetchChars: 100 });
    const hits = (await hosts["store.search"](["hidden steps", 3])) as ReturnType<typeof storeHits>;
    assert.deepEqual(asked, [{ url: "http://litwatch.test/api/papers?q=hidden+steps&limit=3", auth: "Bearer r3ad" }]);
    assert.deepEqual(
      hits.map((h) => [h.id, h.tier, h.reason, h.in_collection]),
      [
        ["2610.00001", 1, "It finds them.", true],
        ["2610.00002", null, null, true],
        ["2401.00009", null, null, false],
      ],
    );
    assert.equal(hits[2].authors[0], "Bo Chen");
  } finally {
    globalThis.fetch = realFetch;
  }
});

test("the highest grade across the badges maps to the tier the agent knows", () => {
  const tiers = ["must_read", "relevant", "adjacent", "none"].map(
    (grade) => storeHits({ rows: [{ id: "1", title: "t", badges: [{ grade }] }] })[0].tier,
  );
  assert.deepEqual(tiers, [1, 2, 3, 4]);
  // A Focus relevant outranks a Field must read lowered by its depth and comes first; ungraded is no tier.
  const tied = [{ grade: "relevant" }, { grade: null }, { grade: "must_read" }];
  assert.equal(storeHits({ rows: [{ id: "1", title: "t", badges: tied }] })[0].tier, 1);
  assert.equal(storeHits({ rows: [{ id: "1", title: "t", badges: [{ grade: null }] }] })[0].tier, null);
  assert.deepEqual(storeHits({}), []);
});
