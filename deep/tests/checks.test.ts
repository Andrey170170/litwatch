import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { test } from "node:test";
import { check, drawErrors, type CheckContext } from "../src/checks.ts";
import { draftErrors, type Draft } from "../src/read.ts";
import { BODY, blocks, changed, draft } from "./support.ts";

const ctx = (over: Partial<CheckContext> = {}): CheckContext => ({ paper: BODY, source: "latex", found: { collection: new Set(), self: "2610.00001" }, topics: [], figures: new Set(), ...over });
const errors = (d: Draft, over?: Partial<CheckContext>) => check(d, ctx(over)).errors;
/** The draft with one paragraph of its method section saying `text`. */
const saying = (text: string) => changed((d) => (blocks(d, "method")[0]!.text = text));
const has = (list: string[], start: string, part = "") => list.some((e) => e.startsWith(start) && e.includes(part));

test("a draft whose numbers, cites and locations are the paper's passes", () => {
  assert.deepEqual(errors(draft()), []);
});

test("a number the paper's text lacks is rejected, in whatever form the paper does not give it", () => {
  const wrong = errors(saying("The probe reaches 83.4% accuracy on held-out traces."));
  assert.ok(has(wrong, "/sections/1/blocks/0/text: 83.4% is not in the paper's text"), wrong.join("\n"));
  assert.ok(has(wrong, "Numbers: give each number as the paper's text states it"), "the rule is said once, after the places");
  // the paper's source has 99.9% only in a comment, which is not its text
  assert.ok(has(errors(saying("An earlier run reached 99.9% accuracy.")), "/sections/1/blocks/0/text: 99.9%"));
  // a number is checked wherever the read gives it as the paper's: a table's cell, an experiment's finding
  const cell = errors(changed((d) => ((blocks(d, "result")[1]!.rows as { cells: unknown[] }[])[0]!.cells[1] = 72.9)));
  assert.ok(has(cell, "/sections/2/blocks/1/rows/0/cells/1: 72.9"), cell.join("\n"));
  const found = errors(changed((d) => (blocks(d, "result")[0]!.found = "The probe reaches 71.3% and the random direction 48%.")));
  assert.ok(has(found, "/sections/2/blocks/0/found: 48%"), found.join("\n"));
});

test("the paper's numbers pass in the forms a read gives them", () => {
  for (const text of [
    "The probe reaches 0.713 accuracy.", // the paper's 71.3%, as a fraction
    "The probe reaches about 71% accuracy.", // rounded to the read's precision
    "The probe is trained on 1.2k traces.", // 1{,}200 in the source
    "The probe is trained on 1200 traces from layer 12.",
    "Training runs for forty epochs at a learning rate of 3e-3.", // 0.003 in the appendix
    "Two baselines and 3 seeds are the read's own small counts.", // whole numbers under ten are not checked
    "Turpin et al. (2023) introduced the setup; see §3 and arXiv 2305.04388.", // a year, a place, an id
  ])
    assert.deepEqual(errors(saying(text)), [], text);
});

test("what the read says of its own is not held to the paper's numbers", () => {
  const own = changed((d) => {
    const s = d.sections as unknown as Record<string, unknown>[];
    (s[0]!.facts as Record<string, unknown>[])[1]!.text = "about 14 minutes";
    s[5]!.takeaway = "It builds on work that reported 62.5% on another benchmark.";
    s[6]!.takeaway = "The repository holds 37 notebooks and no training code.";
  });
  assert.deepEqual(errors(own), []);
});

test("a figure or table the paper does not have is rejected where it is cited", () => {
  const figure = errors(saying("Accuracy rises with depth, as Figure 4 shows."));
  assert.ok(has(figure, "/sections/1/blocks/0/text: cites Figure 4, but the paper's text has 1 figure"), figure.join("\n"));
  const table = errors(changed((d) => (blocks(d, "result")[2]!.where = "§3, Table 3")));
  assert.ok(has(table, "/sections/2/blocks/2/where: cites Table 3, but the paper's text has 1 table"), table.join("\n"));
  assert.deepEqual(errors(saying("Accuracy rises with depth (Fig. 1), and Table 1 gives the totals.")), []);
  // a PDF's text has no environments to count: a label passes when the text mentions it
  const pdf = "# Probes\n\n## 1 Method\n\nFigure 2 shows the probe. Tables 1–3 give the results of 1,200 traces at 71.3% and 50%, layer 12.\n";
  const cited = (text: string) => check(saying(text), ctx({ paper: pdf, source: "pdf" })).errors;
  assert.deepEqual(cited("See Figure 2 and Table 3."), []);
  assert.ok(has(cited("See Figure 5."), "/sections/1/blocks/0/text: cites Figure 5, but the paper's text never mentions Figure 5"));
});

test("a verdict and a CARD note each name a place in the paper", () => {
  type Loose = Record<string, unknown>;
  const claim = (d: Draft) => (d.sections[3] as unknown as { claims: Loose[] }).claims[0]!;
  // left out: the schema's
  assert.ok(has(draftErrors(changed((d) => delete claim(d).where)), "/sections/3/claims/0/where: is required"));
  assert.ok(has(draftErrors(changed((d) => delete blocks(d, "result")[2]!.where)), "/sections/2/blocks/2/where: is required"));
  // given, but no place
  const vague = errors(changed((d) => (claim(d).where = "throughout the results")));
  assert.ok(has(vague, '/sections/3/claims/0/where: "throughout the results" names no place in the paper'), vague.join("\n"));
  const note = errors(changed((d) => (blocks(d, "result")[2]!.where = "see above")));
  assert.ok(has(note, "/sections/2/blocks/2/where:", "names no place"), note.join("\n"));
  // a section the paper's main text does not have
  const beyond = errors(changed((d) => (claim(d).where = "§7")));
  assert.ok(has(beyond, "/sections/3/claims/0/where: cites §7, but the paper's main text has 3 sections"), beyond.join("\n"));
  for (const where of ["Appendix A", "Table 1", "§2", "Abstract", "p. 4", "Eq. 2"]) assert.deepEqual(errors(changed((d) => (claim(d).where = where))), [], where);
});

test("TeX that KaTeX cannot parse is rejected, in an equation and inline", () => {
  const eq = (tex: string) => changed((d) => blocks(d, "method").push({ type: "eq", tex }));
  const block = errors(eq(String.raw`\frac{a}{`));
  assert.ok(has(block, "/sections/1/blocks/2/tex: KaTeX cannot parse it"), block.join("\n"));
  const inline = errors(saying(String.raw`The probe is $w^\top h + \frac{b$ on layer 12.`));
  assert.ok(has(inline, "/sections/1/blocks/0/text: KaTeX cannot parse $"), inline.join("\n"));
  assert.deepEqual(errors(eq(String.raw`\hat{y} = \sigma(w^\top h_\ell + b)`)), []);
  assert.deepEqual(errors(saying(String.raw`The probe is $\sigma(w^\top h + b)$ on layer 12, at a cost of \$3 a run.`)), []);
});

test("a value read off a plot passes with its figure cited in the same sentence, and goes to the reviewer", () => {
  const plot = "Accuracy peaks near 0.84 at layer 9 (Fig. 1). The probe is linear.";
  const unshown = check(saying(plot), ctx());
  assert.deepEqual(unshown.errors, []);
  assert.deepEqual(unshown.plots, { shown: [], unshown: ["Figure 1"] }, "the read shows no such figure");

  const showing = changed((d) => {
    blocks(d, "method")[0]!.text = plot;
    blocks(d, "method").push({ type: "fig", name: "probe", number: "Figure 1", caption: "Probe accuracy by layer, peaking at 0.84." });
  });
  const shown = check(showing, ctx({ figures: new Set(["probe"]) }));
  assert.deepEqual(shown.errors, [], "a figure's own caption gives what the plot shows");
  assert.deepEqual(shown.plots, { shown: ["probe"], unshown: [] });

  // without the cite, or with it in another sentence, the value is only a number the paper lacks
  assert.ok(has(errors(saying("Accuracy peaks near 0.84 at layer 9.")), "/sections/1/blocks/0/text: 0.84"));
  assert.ok(has(errors(saying("Accuracy peaks near 0.84 at layer 9. The curve is in Fig. 1.")), "/sections/1/blocks/0/text: 0.84"));
  // a table cited is no plot
  assert.ok(has(errors(saying("Accuracy peaks near 0.84 at layer 9 (Table 1).")), "/sections/1/blocks/0/text: 0.84"));
});

test("a fig block shows only a figure this session made", () => {
  const d = changed((r) => blocks(r, "method").push({ type: "fig", name: "probe", number: "Figure 1", caption: "Probe accuracy by layer." }));
  assert.ok(has(errors(d), '/sections/1/blocks/2/name: this session made no figure "probe"'));
  assert.deepEqual(errors(d, { figures: new Set(["probe"]) }), []);
});

const diagram = (over: Record<string, unknown> = {}) => ({
  type: "diagram",
  nodes: [
    { id: "trace", label: "Trace", tip: "A labelled trace from the model." },
    { id: "probe", label: "Linear probe", tip: "Reads layer 12 of the residual stream." },
  ],
  edges: [{ from: "trace", to: "probe", tip: "The residual stream at each token." }],
  ...over,
});
const drawing = (over: Record<string, unknown>) => changed((d) => blocks(d, "method").push(diagram(over)));

test("the structure checks: edges, ids, cell counts, node heights and the anchors", () => {
  assert.deepEqual(errors(drawing({})), []);
  const edge = errors(drawing({ edges: [{ from: "trace", to: "prob", tip: "The residual stream at each token." }] }));
  assert.ok(has(edge, '/sections/1/blocks/2/edges/0/to: no node "prob" in this diagram'), edge.join("\n"));
  const twice = diagram().nodes.map((n) => ({ ...n, id: "trace" }));
  assert.ok(has(errors(drawing({ nodes: twice })), '/sections/1/blocks/2/nodes/1/id: "trace" is used by an earlier node'));
  const low = diagram().nodes.map((n, i) => ({ ...n, x: 20 + i * 200, y: 20, w: 120, h: 30 }));
  assert.ok(has(errors(drawing({ nodes: low })), "/sections/1/blocks/2/nodes/0/h: at least 40"));
  const half = diagram().nodes.map((n, i) => (i === 0 ? { ...n, x: 20, y: 20, w: 120, h: 44 } : n));
  assert.ok(has(errors(drawing({ nodes: half })), "/sections/1/blocks/2/nodes: give x and y on every node, or on none"));

  const row = errors(changed((d) => (blocks(d, "result")[1]!.rows as { cells: unknown[] }[])[1]!.cells.push("extra")));
  assert.ok(has(row, "/sections/2/blocks/1/rows/1/cells: 3 cells for 2 columns"), row.join("\n"));
  assert.deepEqual(drawErrors(changed((d) => (blocks(d, "result")[1]!.rows as { cells: unknown[] }[])[1]!.cells.pop())).length, 1, "a render refuses the same table");

  const ids = errors(changed((d) => (d.sections[2]!.id = "method")));
  assert.ok(has(ids, '/sections/2/id: "method" is used by an earlier section'), ids.join("\n"));
  const order = errors(changed((d) => d.sections.splice(3, 2, d.sections[4]!, d.sections[3]!)));
  assert.ok(has(order, "/sections: the order must be At a glance"), order.join("\n"));
  assert.ok(has(errors(changed((d) => d.sections.splice(1, 2))), "/sections: the order must be"), "a read with none of the paper's own sections");
});

test("a read has one lens per topic it is written for, and no other", () => {
  const lens = (topic: string, id = topic) => ({ id, kind: "lens", topic, title: "CoT faithfulness", takeaway: "The probe is a white-box check of the trace.", blocks: [{ type: "p", text: "The probe reads internals against the trace." }] });
  const withLenses = (...lenses: unknown[]) => changed((d) => (d.sections as unknown[]).splice(4, 0, ...lenses));
  assert.deepEqual(errors(withLenses(lens("cot-faithfulness")), { topics: ["cot-faithfulness"] }), []);
  assert.ok(has(errors(draft(), { topics: ["cot-faithfulness"] }), '/sections: no lens for the topic "cot-faithfulness"'));
  assert.ok(has(errors(withLenses(lens("sae-methods"))), '/sections/4/topic: "sae-methods" is not a topic this read is written for'));
  const two = errors(withLenses(lens("cot-faithfulness"), lens("cot-faithfulness", "again")), { topics: ["cot-faithfulness"] });
  assert.ok(has(two, '/sections/5/topic: a second lens for "cot-faithfulness"'), two.join("\n"));
});

test("among_collection names only what a search in the session found", () => {
  const among = changed((d) => (d.among_collection = [{ arxiv_id: "2604.01234", title: "Probing hidden steps", relation: "extends", note: "Same probes, here across four tasks." }]));
  assert.ok(has(errors(among), "/among_collection/0/arxiv_id: no store.search in this session returned it"));
  assert.deepEqual(errors(among, { found: { collection: new Set(["2604.01234"]), self: "2610.00001" } }), []);
});

test("the web app's fixtures pass the checks against their own text", () => {
  for (const name of ["read-icl", "read-scarce"]) {
    const read = JSON.parse(readFileSync(path.resolve(import.meta.dirname, "../../web/src/deepread/fixtures", `${name}.json`), "utf8")) as Draft;
    const { process_notes: _, ...model } = read;
    const lenses = read.sections.flatMap((s) => (s.kind === "lens" ? [s.topic] : []));
    const figures = new Set(read.sections.flatMap((s) => ("blocks" in s ? s.blocks.flatMap((b) => (b.type === "fig" ? [b.name] : [])) : [])));
    const found = { collection: new Set(read.among_collection.map((a) => a.arxiv_id)), self: "0000.00000" };
    // the read's own JSON stands for a paper that says everything the read does
    assert.deepEqual(check(model, { paper: JSON.stringify(read), source: "pdf", found, topics: lenses, figures }).errors, [], name);
  }
});
