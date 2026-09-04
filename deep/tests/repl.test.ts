import assert from "node:assert/strict";
import { test } from "node:test";
import { Repl } from "../src/repl.ts";

const PAPER = `# A Title

Intro text with a needle here.

## Method

We probe things.

## References

- [1] First ref. arXiv:2601.00001
- [2] Second ref.
`;

async function repl(hosts = {}, opts = {}) {
  return Repl.create({ paper: PAPER, __meta: JSON.stringify({ id: "2601.00001" }) }, hosts, opts);
}

test("state persists, the last expression is echoed, and console output is captured", async () => {
  const r = await repl();
  try {
    const silent = await r.run("const a = 2; let b = a + 1;");
    assert.equal(silent.output, "");
    assert.equal(silent.error, undefined);
    const out = await r.run("console.log('x', {k: b}); a * b");
    assert.equal(out.output, 'x {\n "k": 3\n}\n6');
    assert.equal(out.error, undefined);
  } finally {
    r.dispose();
  }
});

test("top-level await, host calls, and exceptions as output", async () => {
  const calls: unknown[] = [];
  const r = await repl({
    "store.search": async (args: unknown[]) => {
      calls.push(args);
      return [{ id: "1", title: "Found" }];
    },
  });
  try {
    const out = await r.run("const hits = await store.search('probe'); hits[0].title");
    assert.equal(out.output, "Found");
    assert.deepEqual(calls, [["probe", 10]]);
    const bad = await r.run("nope.x");
    assert.match(bad.error ?? "", /ReferenceError: nope is not defined/);
    assert.equal((await r.run("hits.length")).output, "1", "state survives an exception");
    const hostErr = await r.run("await s2.lookup('x')");
    assert.match(hostErr.error ?? "", /unknown host function s2.lookup/);
  } finally {
    r.dispose();
  }
});

test("paper helpers see the injected markdown", async () => {
  const r = await repl();
  try {
    assert.equal((await r.run("sections().map(s => s.title).join('|')")).output, "A Title|Method|References");
    assert.match((await r.run("section('method')")).output, /We probe things/);
    assert.equal((await r.run("find('needle')[0].section")).output, "A Title");
    assert.equal((await r.run("refs().length")).output, "2");
    assert.equal((await r.run("meta.id")).output, "2601.00001");
  } finally {
    r.dispose();
  }
});

test("a runaway cell kills the session and later cells report it", async () => {
  const r = await repl({}, { cellTimeoutMs: 200 });
  const out = await r.run("while (true) {}");
  assert.match(out.error ?? "", /terminated/);
  assert.equal(r.dead, true);
  assert.match((await r.run("1")).error ?? "", /earlier timeout/);
});

test("long output is truncated with a hint", async () => {
  const r = await repl({}, { maxOutputChars: 50 });
  try {
    const out = await r.run("'x'.repeat(200)");
    assert.equal(out.truncated, true);
    assert.match(out.output, /truncated at 50 characters of 200/);
  } finally {
    r.dispose();
  }
});
