import assert from "node:assert/strict";
import { test } from "node:test";
import { systemPrompt } from "../src/prompt.ts";

const selections = [
  { name: "cot-faithfulness", subject: "Chain-of-thought faithfulness and monitoring", rubric: "Tier 1: measures or detects unfaithful traces." },
  { name: "sae-methods", subject: "Sparse autoencoder methodology", rubric: null },
];

test("the system prompt names each selection with subject and rubric", () => {
  const p = systemPrompt(40, 8000, [], selections);
  assert.ok(p.includes("cot-faithfulness: Chain-of-thought faithfulness and monitoring"));
  assert.ok(p.includes("Rubric: Tier 1: measures or detects unfaithful traces."));
  assert.ok(p.includes("sae-methods: Sparse autoencoder methodology"));
  assert.ok(p.includes("Budget: 40 cells"));
});

test("the extension schema appears only when requested", () => {
  const without = systemPrompt(40, 8000, [], selections);
  const withExt = systemPrompt(40, 8000, ["faithfulness"], selections);
  assert.ok(without.includes('"summary"'));
  assert.ok(!without.includes('"faithfulness"'));
  assert.ok(withExt.includes('"faithfulness"'));
  assert.ok(withExt.includes('"detection_method"'));
  assert.ok(withExt.includes('"required"') && withExt.includes('"faithfulness"'));
});
