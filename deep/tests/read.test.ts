import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { test } from "node:test";
import { Value } from "typebox/value";
import { ReadSchema, SECTION_KINDS, type Read } from "../src/read.ts";

const WEB = path.resolve(import.meta.dirname, "../../web/src/deepread");
const fixture = (name: string): Read => JSON.parse(readFileSync(path.join(WEB, "fixtures", `${name}.json`), "utf8"));
const errors = (value: unknown) => [...Value.Errors(ReadSchema, value)].map((e) => `${e.instancePath}: ${e.message}`);

for (const name of ["read-icl", "read-scarce"]) {
  test(`the ${name} fixture is a Deep read`, () => {
    const read = fixture(name);
    assert.deepEqual(errors(read), []);
    // the fixtures stand for a whole read: every anchor, in its place, and every block kind between the two
    const kinds = read.sections.map((s) => s.kind);
    assert.equal(kinds[0], "glance");
    assert.deepEqual(kinds.slice(-3), ["limits", "related", "repro"]);
    assert.ok(kinds.indexOf("claims") < kinds.indexOf("lens"));
    assert.equal(new Set(read.sections.map((s) => s.id)).size, read.sections.length);
  });
}

test("the two fixtures cover every section kind and every block type", () => {
  const sections = ["read-icl", "read-scarce"].flatMap((n) => fixture(n).sections);
  assert.deepEqual(new Set(sections.map((s) => s.kind)), new Set(SECTION_KINDS));
  const types = new Set(sections.flatMap((s) => ("blocks" in s ? s.blocks.map((b) => b.type) : [])));
  assert.deepEqual([...types].sort(), ["diagram", "eq", "fig", "h3", "list", "note", "p", "table", "why", "xp"]);
});

test("the exported JSON Schema is the schema", () => {
  const exported = JSON.parse(readFileSync(path.join(WEB, "read.schema.json"), "utf8"));
  assert.deepEqual(exported, JSON.parse(JSON.stringify(ReadSchema)), "run `npm run read-schema`");
});

test("a read is rejected for what the page cannot draw or the spec forbids", () => {
  const read = fixture("read-scarce");
  const changed = (change: (r: Read) => void) => {
    const copy = structuredClone(read);
    change(copy);
    return errors(copy);
  };
  // loosely typed on purpose: each case breaks the shape the types hold
  type Loose = Record<string, unknown>;
  const claim = (r: Read) => (r.sections.find((s) => s.kind === "claims") as unknown as { claims: Loose[] }).claims[0]!;
  const block = (r: Read, type: string) => r.sections.flatMap((s) => ("blocks" in s ? s.blocks : [])).find((b) => b.type === type) as unknown as Loose;
  const node = (r: Read) => (block(r, "diagram").nodes as Loose[])[0]!;
  assert.deepEqual(changed(() => {}), []);
  // a card from before has no format, and is not a read
  assert.notDeepEqual(changed((r) => delete (r as Loose).format), []);
  // every verdict and every CARD note cites a place in the paper
  assert.notDeepEqual(changed((r) => delete claim(r).where), []);
  assert.notDeepEqual(changed((r) => delete block(r, "note").where), []);
  assert.notDeepEqual(changed((r) => (claim(r).verdict = "true")), []);
  // a stored diagram carries its coordinates
  assert.notDeepEqual(changed((r) => delete node(r).x), []);
  // a colour is one of the app's names, never CSS
  assert.notDeepEqual(changed((r) => (node(r).colour = "url(https://example.org/x)")), []);
  assert.notDeepEqual(changed((r) => ((r as Loose).extra = 1)), []);
});
