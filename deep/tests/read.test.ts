import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { test } from "node:test";
import { DraftSchema, draftErrors, ReadSchema, readErrors, SECTION_KINDS, type Read } from "../src/read.ts";

const WEB = path.resolve(import.meta.dirname, "../../web/src/deepread");
const fixture = (name: string): Read => JSON.parse(readFileSync(path.join(WEB, "fixtures", `${name}.json`), "utf8"));
const errors = readErrors;

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

test("what submit takes differs from what is stored only in the coordinates, a WHY box's question and the notes", () => {
  const read = fixture("read-scarce");
  type Loose = Record<string, unknown>;
  const diagram = (r: Read) => r.sections.flatMap((s) => ("blocks" in s ? s.blocks : [])).find((b) => b.type === "diagram") as unknown as { nodes: Loose[]; edges: Loose[] } & Loose;
  const { process_notes: _, ...model } = structuredClone(read);
  assert.deepEqual(draftErrors(model), [], "a stored read without the service's notes is a draft");
  // a draft's diagram may leave its coordinates to the layout; a stored one may not
  const bare = structuredClone(model);
  for (const n of diagram(bare as Read).nodes) for (const key of ["x", "y", "w", "h"]) delete n[key];
  delete diagram(bare as Read).width;
  delete diagram(bare as Read).height;
  assert.deepEqual(draftErrors(bare), []);
  assert.ok(readErrors(bare).some((e) => /\/nodes\/0\/x: is required$/.test(e)), readErrors(bare).join("\n"));
  // a WHY box asks its question in a draft; a stored read from before P6 has none
  const open = structuredClone(model);
  const why = open.sections.flatMap((s) => ("blocks" in s ? s.blocks : [])).find((b) => b.type === "why") as Loose;
  delete why.question;
  assert.deepEqual(readErrors(open), []);
  assert.ok(draftErrors(open).some((e) => /\/question: is required$/.test(e)), draftErrors(open).join("\n"));
  // the service's notes are the stored read's only
  const review = { kind: "review", source: "service", note: "The read did not pass its review: scores below 4: clarity 3." };
  assert.deepEqual(readErrors({ ...model, process_notes: [review] }), []);
  assert.ok(draftErrors({ ...model, process_notes: [review] }).some((e) => e.startsWith("/process_notes/0/source")));
  // one schema, built twice: everything else is the same
  const strip = (schema: unknown) => JSON.stringify(schema, (key, value: unknown) => (key === "process_notes" || key === "description" ? undefined : value)).replace(/"required":\[[^\]]*\]/g, "");
  assert.equal(strip(DraftSchema), strip(ReadSchema));
});
