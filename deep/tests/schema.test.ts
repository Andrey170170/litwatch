import assert from "node:assert/strict";
import { test } from "node:test";
import { resultSchema, validate } from "../src/schema.ts";

const core = {
  summary: "s".repeat(320),
  key_claim: "Probes on residual streams predict CoT unfaithfulness.",
  paper_kind: "method",
  evaluation_setup: { models: ["Qwen3-8B"], tasks: ["GSM8K"], generalization: "Not tested across models." },
  prior_work: [{ title: "Turpin et al.", relation: "Reuses the biased-context setup for hints.", arxiv_id: "2305.04388" }],
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

test("cross-field rules", () => {
  const v1 = validate({ ...good, faithfulness: { ...faithfulness, internals_signal: [] } }, ["faithfulness"]);
  assert.equal(v1.ok, false);
  const v2 = validate({ ...core, code_url: undefined });
  assert.equal(v2.ok, false);
  const v3 = validate({ ...good, faithfulness: { ...faithfulness, monitor_type: "black-box", internals_signal: [] } }, ["faithfulness"]);
  assert.equal(v3.ok, true);
});
