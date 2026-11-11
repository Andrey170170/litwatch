import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { test } from "node:test";
import { LENS_QUESTIONS, findingsMessage, reviewerPrompt, schemaJson, systemPrompt, topicKey } from "../src/prompt.ts";
import { DraftSchema } from "../src/read.ts";
import { JUDGED } from "./support.ts";

const selections = [
  { name: "CoT faithfulness", slug: "cot-faithfulness", subject: "Chain-of-thought faithfulness and monitoring", rubric: "Tier 1: measures or detects unfaithful traces.", grade: "must_read" },
  { name: "sae-methods", subject: "Sparse autoencoder methodology", rubric: null, grade: "adjacent" },
];

test("the writer's prompt names each topic by its lens key, with its grade, subject and rubric", () => {
  const p = systemPrompt(150, 8000, [], selections);
  assert.ok(p.includes('- "cot-faithfulness" · CoT faithfulness (graded must read): Chain-of-thought faithfulness and monitoring'));
  assert.ok(p.includes("Rubric: Tier 1: measures or detects unfaithful traces."));
  assert.ok(p.includes('- "sae-methods" (graded adjacent): Sparse autoencoder methodology'), "a topic sent without a slug is named by its name");
  assert.deepEqual(selections.map(topicKey), ["cot-faithfulness", "sae-methods"], "the keys submit holds the lenses to");
  assert.ok(p.includes("Budget: 150 cells"));
  assert.ok(p.includes("process_notes say what you could not do because of your tools, access or budget"));
  assert.ok(p.includes("is what the paper offers, not something you could not do"), "a 404 or an empty repository is no process note");
  assert.ok(systemPrompt(150, 8000, [], []).includes("Topics: no topic grades this paper above none, so the read has no lens section."));
});

test("the prompt carries the schema submit takes, once and compact", () => {
  const p = systemPrompt(150, 8000, [], selections);
  assert.equal(schemaJson(), JSON.stringify(DraftSchema));
  assert.equal(p.split(schemaJson()).length, 2, "once");
  assert.ok(!schemaJson().includes('"service"') && !schemaJson().includes('"review"'), "the model is offered its own process notes only");
  // the schema is most of the prompt: indented, it alone would be twice this
  assert.ok(p.length < 50_000, `the prompt is ${p.length} characters`);
});

test("a topic's lens questions are asked only when its extension is", () => {
  const without = systemPrompt(150, 8000, [], selections);
  const withOne = systemPrompt(150, 8000, ["faithfulness"], selections);
  assert.ok(!without.includes(LENS_QUESTIONS.faithfulness!) && !without.includes(LENS_QUESTIONS.monitoring!));
  assert.ok(withOne.includes(LENS_QUESTIONS.faithfulness!) && !withOne.includes(LENS_QUESTIONS.monitoring!));
  assert.ok(withOne.includes("omission: the trace lacks a computation the model performed"));
});

test("the prose rules are litwatch's own, as src/litwatch/writing.py has them", () => {
  const python = readFileSync(path.resolve(import.meta.dirname, "../../src/litwatch/writing.py"), "utf8");
  const constant = (name: string) => new RegExp(`^${name} = """\\\\\\n([\\s\\S]*?)"""`, "m").exec(python)![1]!.replace(/\\\n/g, "");
  const p = systemPrompt(150, 8000, [], selections);
  assert.ok(p.includes(constant("VOICE")), "VOICE, whole");
  const reader = constant("READER");
  assert.ok(p.includes(reader.slice(reader.indexOf("- You have read the source"))), "READER's rules, word for word");
  // the reviewer names the same three faults the writer is held to
  const faults = p.slice(p.indexOf("Three faults make model writing hard to read."), p.indexOf("\n\nAfter a submit passes"));
  assert.ok(faults.includes("- Hidden actors and actions.") && faults.includes("- Vague wording.") && faults.includes("- Empty statements and misplaced emphasis."));
  assert.ok(reviewerPrompt(selections).includes(faults));
});

test("the reviewer's prompt carries the rubric, the pass rule and the topics", () => {
  const p = reviewerPrompt(selections);
  for (const name of ["faithfulness", "clarity", "depth", "calibration", "anchors", "experiments", "why_boxes", "voice", "length"]) assert.ok(p.includes(`- ${name}: `), name);
  assert.ok(p.includes("A 5 means a reader never rereads a sentence"), "clarity's anchors");
  assert.ok(p.includes("A read passes when every score, the blind reader's included, is at least 4 and every check passes."));
  assert.ok(p.includes('- "cot-faithfulness" · CoT faithfulness (graded must read)'));
});

test("the reviewer's prompt says what code left unchecked: the places it names, and the numbers its check leaves out", () => {
  const p = reviewerPrompt(selections);
  const unchecked = p.slice(p.indexOf("What code has not checked, which is yours:"), p.indexOf("Findings point at a place"));
  assert.ok(unchecked.includes("The places the message lists as not confirmed."));
  for (const anchor of ["equation", "algorithm", "theorem", "footnote", "page", "read from its PDF", "numbering the source sets itself"]) assert.ok(unchecked.includes(anchor), anchor);
  // every exemption of the number check (src/checks.ts), so the reviewer checks those numbers itself
  for (const exempt of ["whole numbers under ten", "years", "every number in Related work", "an equation's TeX", "the reading time", "a table caption's counts of the rows and columns the read kept", "the read's own headings and labels"])
    assert.ok(unchecked.includes(exempt), exempt);
});

test("the findings reach the writer most important first, with what is left", () => {
  const review = {
    ...JUDGED,
    round: 2,
    passed: false,
    scores: { ...JUDGED.scores, depth: 3 },
    checks: { ...JUDGED.checks, why_boxes: false },
    findings: [
      { about: "depth" as const, where: "method", text: "The probe's training is one sentence; the paper gives the data split." },
      { about: "why_boxes" as const, where: "method.1", text: "Why layer 12 matters to the result is only in the folded answer." },
    ],
  };
  const message = findingsMessage(review, 1);
  assert.ok(message.startsWith("The read did not pass (review 2)."));
  assert.ok(message.includes("(below: depth 3)") && message.includes("Checks failed: why_boxes."));
  assert.ok(message.indexOf("1. [depth] method: ") < message.indexOf("2. [why_boxes] method.1: "));
  assert.ok(message.includes("this is the last revision"));
});
