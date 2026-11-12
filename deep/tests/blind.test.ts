import assert from "node:assert/strict";
import { test } from "node:test";
import { readerText } from "../src/blind.ts";
import type { Read } from "../src/read.ts";
import { blocks, draft } from "./support.ts";

const read = () => structuredClone({ ...draft(), among_collection: [] }) as unknown as Read;

test("the blind reader gets the read as the page shows it, whatever order the JSON has", () => {
  const r = read();
  // an experiment written field by field in reverse still reads question first, as the page shows it
  const xp = blocks(r, "result")[0]!;
  blocks(r, "result")[0] = Object.fromEntries(Object.entries(xp).reverse());
  // and a claim written evidence first still reads the claim first, its evidence folded as on the page
  const claims = (r.sections.find((x) => x.kind === "claims") as unknown as { claims: Record<string, unknown>[] }).claims;
  claims[0] = Object.fromEntries(Object.entries(claims[0]!).reverse());
  (r as unknown as { among_collection: unknown[] }).among_collection = [{ id: "2609.00002", relation: "extends", note: "Probes on another model." }];
  const rows = (blocks(r, "result").find((b) => b.type === "table") as { rows: { cells: unknown[] }[] }).rows;
  rows[0]!.cells[1] = { note: "Best of three seeds.", value: 71.3 };
  rows.push({ cells: [12, 80.2] });
  blocks(r, "method").push({
    type: "diagram",
    nodes: [{ id: "probe", label: "Probe", tip: "Reads layer 12.", x: 4.5, y: 10, w: 80, h: 40 }, { id: "out", label: "Verdict", x: 4.5, y: 90, w: 80, h: 40 }],
    edges: [{ from: "probe", to: "out", title: "The probe's verdict", tip: "A score per trace.", where: "§3", points: [[4.5, 50], [4.5, 90]] }],
    width: 100,
    height: 140,
  });
  const { text } = readerText(r);
  const at = (s: string) => text.indexOf(s);
  assert.ok(at("question: ") > -1 && at("question: ") < at("did: ") && at("did: ") < at("found: ") && at("found: ") < at("means: "), text);
  assert.ok(!text.includes("takeaway") && !text.includes("A linear probe on layer 12, trained on 1,200 labelled traces."), "a takeaway the page does not show");
  assert.ok(at(`text: ${claims[0]!.text}`) > -1 && at(`text: ${claims[0]!.text}`) < at("evidence, shown on a click: "), text);
  // a table's row of numbers is read; a diagram edge's waypoints are not
  assert.match(text, /cells:\n\s+12\n\s+80\.2/);
  // what the page shows only on a click is marked so: a diagram's tips, a table cell's note
  assert.match(text, /tip, shown on a click: Reads layer 12/);
  assert.match(text, /title, shown on a click: The probe's verdict/);
  assert.match(text, /where, shown on a click: §3/);
  assert.match(text, /value: 71\.3\n\s+note, shown on a click: Best of three seeds/);
  assert.ok(!text.slice(at("edges:")).includes("points:") && !text.includes("4.5"), "no waypoints");
  assert.ok(at("among the collection:") > at("== Related work"), "the collection's papers under Related work, as the page shows them");
  assert.ok(text.includes("[WHY box, folded] (method.1) Why a linear probe?\n  answer, shown on a click: A linear probe"), text);
});
