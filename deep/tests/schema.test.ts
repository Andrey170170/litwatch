import assert from "node:assert/strict";
import { test } from "node:test";
import { resultSchema, validate } from "../src/schema.ts";

const core = {
  summary: "s".repeat(320),
  key_claim: "Probes on residual streams predict CoT unfaithfulness.",
  paper_kind: "method",
  evaluation_setup: { models: ["Qwen3-8B"], tasks: ["GSM8K"], generalization: "Not tested across models." },
  prior_work: [{ title: "Turpin et al.", relation: "Reuses the biased-context setup for hints.", arxiv_id: "2305.04388" }],
  among_collection: [
    { arxiv_id: "2604.01234", title: "Probing hidden steps", relation: "extends", note: "Same probes, here across four tasks." },
  ],
  code_available: true,
  code_url: "https://github.com/x/y",
  weights_available: false,
  limitations: ["Only two models were evaluated in the study."],
};

const faithfulness = {
  mismatch: ["omission"],
  ground_truth: "internals",
  detection_method: "d".repeat(100),
  definition: "f".repeat(100),
  monitor_type: "white-box",
  internals_signal: ["probes"],
  generality: "g".repeat(50),
};

const monitoring = {
  target: ["reward hacking"],
  monitor_type: "black-box",
  internals_signal: [],
  monitor_method: "m".repeat(100),
  pressure: "optimization against the monitor",
  robustness: "r".repeat(50),
  generality: "g".repeat(50),
};

const good = { ...core, faithfulness };

test("a core result validates with no extensions", () => {
  assert.deepEqual(validate(core), { ok: true, value: core });
});

test("a result with the requested extension validates", () => {
  assert.deepEqual(validate(good, ["faithfulness"]), { ok: true, value: good });
});

test("a requested extension is required and an unrequested one is rejected", () => {
  const missing = validate(core, ["faithfulness"]);
  assert.equal(missing.ok, false);
  if (!missing.ok) assert.ok(missing.errors.some((e) => e.includes("faithfulness")), missing.errors.join("\n"));
  const unrequested = validate(good);
  assert.equal(unrequested.ok, false);
  if (!unrequested.ok) assert.ok(unrequested.errors.some((e) => e.startsWith("/faithfulness: extension not requested")), unrequested.errors.join("\n"));
});

test("resultSchema throws on an unknown extension", () => {
  assert.throws(() => resultSchema(["nope"]), /unknown extension "nope"/);
});

test("errors name the path once and list allowed values", () => {
  const bad = { ...good, paper_kind: "essay", faithfulness: { ...faithfulness, ground_truth: "guess" }, summary: "short", extra: 1 };
  const v = validate(bad, ["faithfulness"]);
  assert.equal(v.ok, false);
  if (v.ok) return;
  assert.ok(v.errors.some((e) => e.startsWith("/paper_kind: must be one of: method, analysis, benchmark, position")), v.errors.join("\n"));
  assert.ok(v.errors.some((e) => e.startsWith("/faithfulness/ground_truth: must be one of: task construction, internals, meta-evaluation, none")), v.errors.join("\n"));
  assert.ok(v.errors.some((e) => e.startsWith("/summary: must not have fewer than 300")), v.errors.join("\n"));
  assert.ok(v.errors.some((e) => e.includes("extra")), v.errors.join("\n"));
  assert.equal(v.errors.filter((e) => e.startsWith("/paper_kind")).length, 1);
});

test("the monitoring extension validates alone or next to faithfulness", () => {
  const alone = { ...core, monitoring };
  assert.deepEqual(validate(alone, ["monitoring"]), { ok: true, value: alone });
  const both = { ...good, monitoring };
  assert.deepEqual(validate(both, ["faithfulness", "monitoring"]), { ok: true, value: both });
  const hidden = validate({ ...core, monitoring: { ...monitoring, monitor_type: "hybrid" } }, ["monitoring"]);
  assert.equal(hidden.ok, false);
  if (!hidden.ok) assert.ok(hidden.errors.some((e) => e.startsWith("/monitoring/internals_signal")), hidden.errors.join("\n"));
  const badPressure = validate({ ...core, monitoring: { ...monitoring, pressure: "lots" } }, ["monitoring"]);
  assert.equal(badPressure.ok, false);
});

test("cross-field rules", () => {
  const v1 = validate({ ...good, faithfulness: { ...faithfulness, internals_signal: [] } }, ["faithfulness"]);
  assert.equal(v1.ok, false);
  const v2 = validate({ ...core, code_url: undefined });
  assert.equal(v2.ok, false);
  const v3 = validate({ ...good, faithfulness: { ...faithfulness, monitor_type: "black-box", internals_signal: [] } }, ["faithfulness"]);
  assert.equal(v3.ok, true);
});

test("among_collection names only collection papers this session's searches returned", () => {
  const found = { collection: new Set(["2604.01234"]), self: "2605.00001" };
  assert.equal(validate(core, [], found).ok, true);
  const unseen = validate({ ...core, among_collection: [{ ...core.among_collection[0], arxiv_id: "2604.09999" }] }, [], found);
  assert.equal(unseen.ok, false);
  if (!unseen.ok) assert.ok(unseen.errors.some((e) => e.startsWith("/among_collection/0/arxiv_id: no store.search")), unseen.errors.join("\n"));
  const itself = validate({ ...core, among_collection: [{ ...core.among_collection[0], arxiv_id: "2605.00001" }] }, [], { ...found, collection: new Set(["2605.00001"]) });
  assert.equal(itself.ok, false);
  const relation = validate({ ...core, among_collection: [{ ...core.among_collection[0], relation: "cites" }] }, [], found);
  assert.equal(relation.ok, false);
  if (!relation.ok) assert.ok(relation.errors.some((e) => e.startsWith("/among_collection/0/relation: must be one of: agrees, contradicts, extends, supersedes")), relation.errors.join("\n"));
  const { among_collection: _, ...without } = core;
  assert.equal(validate(without).ok, false, "required, though it may be empty");
  assert.equal(validate({ ...core, among_collection: [] }, [], found).ok, true);
});

test("an arXiv id is a modern or a legacy one, without its version", () => {
  const legacy = ["cs/9901001", "math.AG/0601001", "hep-th/9901001"];
  const found = { collection: new Set(legacy), self: "2605.00001" };
  const among = legacy.map((arxiv_id) => ({ ...core.among_collection[0], arxiv_id }));
  const prior = [{ ...core.prior_work[0], arxiv_id: "math.AG/0601001" }];
  assert.equal(validate({ ...core, among_collection: among, prior_work: prior }, [], found).ok, true);
  for (const bad of ["cs/9901001v2", "2604.01234v3", "9901001", "cs/990100", "CS/9901001x"]) {
    const result = validate({ ...core, among_collection: [{ ...core.among_collection[0], arxiv_id: bad }] });
    assert.equal(result.ok, false, bad);
  }
});

test("the model's process notes: up to six, its own, of a known kind, long enough, linking only http(s)", () => {
  const note = { kind: "access", source: "model", note: "The linked repository answered 404, so code is unverified.", url: "https://github.com/x/y" };
  const withNotes = (process_notes: unknown[]) => validate({ ...core, process_notes });
  assert.equal(validate(core).ok, true, "optional");
  assert.equal(withNotes([]).ok, true, "empty is no notes");
  for (const kind of ["access", "tool", "budget", "input"]) assert.equal(withNotes([{ ...note, kind }]).ok, true, kind);
  const { url: _, ...bare } = note;
  assert.equal(withNotes([bare]).ok, true, "a URL is optional");
  assert.equal(withNotes(Array(6).fill(note)).ok, true);
  const refused: [string, unknown, string][] = [
    ["seven notes", Array(7).fill(note), "/process_notes: "],
    ["a service note", [{ ...note, source: "service" }], "/process_notes/0/source"],
    ["no source", [{ ...bare, source: undefined }], "/process_notes/0"],
    ["an unknown kind", [{ ...note, kind: "paper" }], "/process_notes/0/kind: must be one of: access, tool, budget, input"],
    ["a short note", [{ ...note, note: "Blocked." }], "/process_notes/0/note"],
    ["a script URL", [{ ...note, url: "javascript:alert(1)" }], "/process_notes/0/url"],
    ["a relative URL", [{ ...note, url: "/r/abc" }], "/process_notes/0/url"],
    ["a broken URL", [{ ...note, url: "https://[nope" }], "/process_notes/0/url: not an absolute http(s) URL"],
    ["an extra field", [{ ...note, count: 3 }], "/process_notes/0"],
  ];
  for (const [what, notes, path] of refused) {
    const v = withNotes(notes as unknown[]);
    assert.equal(v.ok, false, what);
    if (!v.ok) assert.ok(v.errors.some((e) => e.startsWith(path)), `${what}: ${v.errors.join("\n")}`);
  }
});
