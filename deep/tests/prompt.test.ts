import assert from "node:assert/strict";
import { test } from "node:test";
import { systemPrompt } from "../src/prompt.ts";

const selections = [
  { name: "cot-faithfulness", subject: "Chain-of-thought faithfulness and monitoring", rubric: "Tier 1: measures or detects unfaithful traces.", grade: "must_read" },
  { name: "sae-methods", subject: "Sparse autoencoder methodology", rubric: null, grade: "adjacent" },
];

test("the system prompt names each selection with its grade, subject and rubric", () => {
  const p = systemPrompt(40, 8000, [], selections);
  assert.ok(p.includes("cot-faithfulness (graded must read): Chain-of-thought faithfulness and monitoring"));
  assert.ok(p.includes("Rubric: Tier 1: measures or detects unfaithful traces."));
  assert.ok(p.includes("sae-methods (graded adjacent): Sparse autoencoder methodology"));
  assert.ok(!p.includes("graded this paper must read"), "a topic's grade is its own, not must read for all");
  assert.ok(p.includes("Budget: 40 cells"));
  assert.ok(p.includes("process_notes say what you could not do because of your tools, access or budget"));
  assert.ok(p.includes("is what the paper offers, not something you could not do"), "a 404 or an empty repository is no process note");
  assert.ok(p.includes('"process_notes"'), "the schema offers them");
});

test("a card for no topic says it was asked for", () => {
  assert.ok(systemPrompt(40, 8000, [], []).includes("Topics: none graded this paper; a lab member asked for its card."));
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
