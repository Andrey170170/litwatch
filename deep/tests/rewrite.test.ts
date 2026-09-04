import assert from "node:assert/strict";
import { test } from "node:test";
import { rewriteCell } from "../src/rewrite.ts";

test("declarations become globals and the last expression is captured", () => {
  const out = rewriteCell("const a = 1, b = a + 1;\nlet c;\na + b");
  assert.equal(out, "globalThis.a = 1; globalThis.b = a + 1;\nglobalThis.c = undefined;\nglobalThis.__last = (a + b);");
});

test("destructuring keeps defaults, rest, and computed keys", () => {
  const out = rewriteCell("const { x, y: [p, q = 2], ...rest } = obj;");
  assert.equal(out, "({ x: globalThis.x, y: [globalThis.p, globalThis.q = 2], ...globalThis.rest } = obj);");
});

test("functions are hoisted and classes assigned", () => {
  const out = rewriteCell("f();\nasync function f() { return 1; }\nclass K {}");
  assert.equal(
    out,
    "globalThis.f = async function f() { return 1; };\nf();\n\nglobalThis.K = class K {};",
  );
});

test("top-level await parses and imports are rejected", () => {
  assert.equal(rewriteCell("const v = await go();"), "globalThis.v = await go();");
  assert.throws(() => rewriteCell("import x from 'y'"), /not available/);
});

test("nested declarations are untouched", () => {
  const src = "for (const i of [1]) { const j = i; }";
  assert.equal(rewriteCell(src), src);
});
