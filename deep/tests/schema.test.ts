import assert from "node:assert/strict";
import { test } from "node:test";
import { validate } from "../src/schema.ts";

const good = {
  summary: "s".repeat(320),
  key_claim: "Probes on residual streams predict CoT unfaithfulness.",
  paper_kind: "method",
  monitor_type: "white-box",
  internals_signal: ["probes"],
  threat_model: ["hint non-acknowledgement"],
  faithfulness_definition: "f".repeat(100),
  evaluation_setup: { models: ["Qwen3-8B"], tasks: ["GSM8K"], generalization: "Not tested across models." },
  prior_work: [{ title: "Turpin et al.", relation: "Reuses the biased-context setup for hints.", arxiv_id: "2305.04388" }],
  code_available: true,
  code_url: "https://github.com/x/y",
  weights_available: false,
  limitations: ["Only two models were evaluated in the study."],
};

test("a complete result validates", () => {
  assert.deepEqual(validate(good), { ok: true, value: good });
});

test("errors name the path once and list allowed values", () => {
  const bad = { ...good, paper_kind: "essay", internals_signal: [], summary: "short", extra: 1 };
  const v = validate(bad);
  assert.equal(v.ok, false);
  if (v.ok) return;
  assert.ok(v.errors.some((e) => e.startsWith("/paper_kind: must be one of: method, analysis, benchmark, position")), v.errors.join("\n"));
  assert.ok(v.errors.some((e) => e.startsWith("/summary: must not have fewer than 300")), v.errors.join("\n"));
  assert.ok(v.errors.some((e) => e.includes("extra")), v.errors.join("\n"));
  assert.equal(v.errors.filter((e) => e.startsWith("/paper_kind")).length, 1);
});

test("cross-field rules", () => {
  const v1 = validate({ ...good, internals_signal: [] });
  assert.equal(v1.ok, false);
  const v2 = validate({ ...good, code_url: undefined });
  assert.equal(v2.ok, false);
  const v3 = validate({ ...good, monitor_type: "black-box", internals_signal: [] });
  assert.equal(v3.ok, true);
});
