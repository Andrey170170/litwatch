import assert from "node:assert/strict";
import { test } from "node:test";
import { draftErrors, type Draft } from "../src/read.ts";
import { amongErrors } from "../src/schema.ts";
import { blocks, changed, draft } from "./support.ts";

type Loose = Record<string, unknown>;
const among = { arxiv_id: "2604.01234", title: "Probing hidden steps", relation: "extends", note: "Same probes, here across four tasks." };
const has = (errors: string[], start: string) => errors.some((e) => e.startsWith(start));

test("a draft's errors name the path once, list the allowed values, and speak of the block it is", () => {
  assert.deepEqual(draftErrors(draft()), []);
  const bad = changed((d) => {
    const s = d.sections as unknown as Loose[];
    (s[3]!.claims as Loose[])[0]!.verdict = "true";
    s[1]!.takeaway = "";
    (d as Loose).extra = 1;
    const table = blocks(d, "result")[1]!;
    table.colums = table.columns;
    delete table.columns;
    blocks(d, "method")[0]!.type = "para";
  });
  const errors = draftErrors(bad);
  const expected = [
    "/sections/3/claims/0/verdict: must be one of: holds, partly, not shown, overclaimed",
    "/sections/1/takeaway: ",
    "/extra: is not a field here",
    // a table with a misspelt field is told so, not that it fails to be each other kind of block
    "/sections/2/blocks/1/columns: is required",
    "/sections/2/blocks/1/colums: is not a field here",
    "/sections/1/blocks/0/type: must be one of: p, h3, list, why, note, eq, fig, xp, table, diagram",
  ];
  for (const start of expected) assert.ok(has(errors, start), `${start}\n${errors.join("\n")}`);
  assert.equal(errors.length, expected.length, errors.join("\n"));
  assert.ok(has(draftErrors(changed((d) => ((d.sections[1] as Loose).kind = "methods"))), "/sections/1/kind: must be one of: glance, problem, method, finding, claims, lens, limits, related, repro"));
  assert.deepEqual(draftErrors("a read"), ["/: must be an object"]);
});

test("among_collection names only collection papers this session's searches returned", () => {
  const found = { collection: new Set(["2604.01234"]), self: "2605.00001" };
  assert.deepEqual(amongErrors([among], found), []);
  assert.deepEqual(amongErrors([{ ...among, arxiv_id: "2604.09999" }], found), ["/among_collection/0/arxiv_id: no store.search in this session returned it with in_collection true"]);
  assert.deepEqual(amongErrors([{ ...among, arxiv_id: "2605.00001" }], { ...found, collection: new Set(["2605.00001"]) }), ["/among_collection/0/arxiv_id: is this paper itself"]);
  const withAmong = (entry: Loose) => draftErrors(changed((d) => ((d as Loose).among_collection = [entry])));
  assert.deepEqual(withAmong(among), []);
  assert.ok(has(withAmong({ ...among, relation: "cites" }), "/among_collection/0/relation: must be one of: agrees, contradicts, extends, supersedes"));
  assert.ok(has(draftErrors(changed((d) => delete (d as Loose).among_collection)), "/among_collection: is required"), "required, though it may be empty");
});

test("an arXiv id is a modern or a legacy one, without its version", () => {
  const withId = (arxiv_id: string) => draftErrors(changed((d) => ((d as Loose).among_collection = [{ ...among, arxiv_id }])));
  for (const good of ["2604.01234", "cs/9901001", "math.AG/0601001", "hep-th/9901001"]) assert.deepEqual(withId(good), [], good);
  for (const bad of ["cs/9901001v2", "2604.01234v3", "9901001", "cs/990100", "CS/9901001x"]) assert.ok(has(withId(bad), "/among_collection/0/arxiv_id"), bad);
});

test("the model's process notes: up to six, its own, of a known kind, long enough, linking only http(s)", () => {
  const note = { kind: "access", source: "model", note: "The linked repository answered 404, so code is unverified.", url: "https://github.com/x/y" };
  const withNotes = (process_notes: unknown) => draftErrors({ ...draft(), process_notes } as unknown as Draft);
  assert.deepEqual(withNotes([]), [], "empty is no notes");
  for (const kind of ["access", "tool", "budget", "input"]) assert.deepEqual(withNotes([{ ...note, kind }]), [], kind);
  const { url: _, ...bare } = note;
  assert.deepEqual(withNotes([bare]), [], "a URL is optional");
  assert.deepEqual(withNotes(Array(6).fill(note)), []);
  const refused: [string, unknown, string][] = [
    ["seven notes", Array(7).fill(note), "/process_notes: "],
    ["a service note", [{ ...note, source: "service" }], "/process_notes/0/source"],
    ["a review note", [{ ...note, kind: "review" }], "/process_notes/0/kind: must be one of: access, tool, budget, input"],
    ["no source", [bare && { kind: note.kind, note: note.note }], "/process_notes/0/source: is required"],
    ["an unknown kind", [{ ...note, kind: "paper" }], "/process_notes/0/kind: must be one of: access, tool, budget, input"],
    ["a short note", [{ ...note, note: "Blocked." }], "/process_notes/0/note"],
    ["a script URL", [{ ...note, url: "javascript:alert(1)" }], "/process_notes/0/url"],
    ["a relative URL", [{ ...note, url: "/r/abc" }], "/process_notes/0/url"],
    ["an extra field", [{ ...note, count: 3 }], "/process_notes/0/count: is not a field here"],
  ];
  for (const [what, notes, start] of refused) assert.ok(has(withNotes(notes), start), `${what}: ${withNotes(notes).join("\n")}`);
});
