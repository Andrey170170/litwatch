import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { test } from "node:test";
import { check, drawErrors, type CheckContext } from "../src/checks.ts";
import { PaperIndex, cites, figureLabel } from "../src/places.ts";
import { draftErrors, type Draft } from "../src/read.ts";
import { BODY, blocks, changed, draft } from "./support.ts";

type Loose = Record<string, unknown>;
type Over = Partial<Omit<CheckContext, "figures">> & { paper?: string; source?: string; figures?: Record<string, { label?: string }> };
/** The check's context: `BODY` as the paper, or another text; the session's figures by name. */
const ctx = ({ paper = BODY, source = "latex", figures = {}, ...over }: Over = {}): CheckContext => ({
  index: new PaperIndex(paper, source),
  found: { collection: new Set(), self: "2610.00001" },
  topics: [],
  figures: new Map(Object.entries(figures)),
  ...over,
});
const errors = (d: Draft, over?: Over) => check(d, ctx(over)).errors;
/** The draft with one paragraph of its method section saying `text`. */
const saying = (text: string) => changed((d) => (blocks(d, "method")[0]!.text = text));
/** The draft with its one claim resting on `where`. */
const resting = (where: string) => changed((d) => ((d.sections[3] as unknown as { claims: Loose[] }).claims[0]!.where = where));
const has = (list: string[], start: string, part = "") => list.some((e) => e.startsWith(start) && e.includes(part));

/** `BODY` with a subsection under its second section, with a figure in its appendix, and without its appendix. */
const NESTED = BODY.replace("\\section{Results}", "\\subsection{Probe training}\nThe probe trains.\n\\section{Results}");
const APPENDIX_FIGURE = BODY.replace("The learning rate is", "\\begin{figure}\n\\includegraphics{figs/loss.pdf}\n\\caption{Training loss.}\n\\end{figure}\nThe learning rate is");
const NO_APPENDIX = BODY.slice(0, BODY.indexOf("\\appendix")) + "\\end{document}\n";
/** A PDF's text of the same paper, numbered as a PDF's headings are. */
const PDF = "# Probes read the trace\n\n## 1 Introduction\n\nFigure 2 shows the probe.\n\n## 2 Method\n\n### 2.1 Probe training\n\nLayer 12 of Qwen3-8B, 1,200 traces.\n\n## 3 Results\n\nTables 1–3 give 71.3% against 50%.\n";

test("a draft whose numbers, cites and locations are the paper's passes", () => {
  assert.deepEqual(check(draft(), ctx()), { errors: [], plots: [], unverified: [] });
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
    "The probe reaches .713 accuracy.", // written from its point
    "The probe reaches about 71% accuracy.", // rounded to the read's precision
    "The probe is trained on 1.2k traces.", // 1{,}200 in the source
    "The probe is trained on 1200 traces from layer 12.",
    "Training runs for forty epochs at a learning rate of 3e-3.", // 0.003 in the appendix
    "Two baselines and 3 seeds are the read's own small counts.", // whole numbers under ten are not checked
    "Turpin et al. (2023) introduced the setup; see §3 and arXiv 2305.04388.", // a year, a place, an id
  ])
    assert.deepEqual(errors(saying(text)), [], text);
});

test("a decimal written from its point is the same number as one written with its zero, both ways", () => {
  // the paper writes .95 and 0.05; the read writes 0.95 and .05
  const paper = BODY.replace("against 50\\% for a random direction", "against 50\\% for a random direction, at p < 0.05 and a rank correlation of .95");
  for (const text of ["The rank correlation is 0.95.", "The rank correlation is .95.", "The difference holds at p < .05.", "The difference holds at p < 0.05.", "The correlation is 95%, significant at 5%."])
    assert.deepEqual(errors(saying(text), { paper }), [], text);
  assert.ok(has(errors(saying("The rank correlation is .59."), { paper }), "/sections/1/blocks/0/text: .59 is not in the paper's text"));
  assert.ok(has(errors(saying("The rank correlation is .95.")), "/sections/1/blocks/0/text: .95"), "a paper without it does not hold it");
});

test("what the read says of its own is not held to the paper's numbers: the reading time, Related work, years, small counts", () => {
  const own = changed((d) => {
    const s = d.sections as unknown as Loose[];
    (s[0]!.facts as Loose[])[1]!.text = "about 14 minutes";
    s[5]!.takeaway = "It builds on work from 2023 that reported 62.5% on another benchmark.";
  });
  assert.deepEqual(errors(own), []);
});

test("a year is one only in the range a paper's could be, and Reproducibility's numbers are the paper's", () => {
  assert.deepEqual(errors(saying("The setup follows work from 1997 and 2023.")), []);
  for (const number of ["2048", "1949", String(new Date().getFullYear() + 2)]) {
    const beyond = errors(saying(`The context holds ${number} tokens.`));
    assert.ok(has(beyond, `/sections/1/blocks/0/text: ${number} is not in the paper's text`), beyond.join("\n"));
  }
  const repro = errors(changed((d) => ((d.sections[6] as Loose).takeaway = "The repository holds 37 notebooks and no training code.")));
  assert.ok(has(repro, "/sections/6/takeaway: 37 is not in the paper's text"), repro.join("\n"));
  const fact = errors(changed((d) => ((d.sections[6] as unknown as { facts: Loose[] }).facts[0]!.text = "Trained for 64 GPU hours.")));
  assert.ok(has(fact, "/sections/6/facts/0/text: 64"), fact.join("\n"));
  assert.deepEqual(errors(changed((d) => ((d.sections[6] as Loose).takeaway = "Training runs for 40 epochs; no code is released."))), [], "what the paper says of its training passes");
});

test("captions, fact tags and a table's group headings give the paper's numbers; the read's own labels and an equation's TeX do not", () => {
  const table = errors(changed((d) => (blocks(d, "result")[1]!.caption = "Rebuilt from Table 1, keeping 88.8% of its rows.")));
  assert.ok(has(table, "/sections/2/blocks/1/caption: 88.8%"), table.join("\n"));
  // how the read cut the table is its own to count; a result in the same caption is still the paper's
  for (const caption of ["Rebuilt from Table 1, 11 of its 14 rows.", "Table 1's first 11 rows and 3 of 16 columns.", "Rebuilt from Table 1: 11 of the 14 main rows, 13 columns."])
    assert.deepEqual(errors(changed((d) => (blocks(d, "result")[1]!.caption = caption))), [], caption);
  const mixed = errors(changed((d) => (blocks(d, "result")[1]!.caption = "Rebuilt from Table 1, 11 of its 14 rows, where the probe reaches 88.8%.")));
  assert.equal(mixed[0], "/sections/2/blocks/1/caption: 88.8% is not in the paper's text", mixed.join("\n"));
  const prose = errors(saying("The table has 11 of its 14 rows."));
  assert.ok(has(prose, "/sections/1/blocks/0/text: 11, 14 are not in the paper's text"), "only a table's caption counts its cut");
  const group = errors(changed((d) => (blocks(d, "result")[1]!.rows as unknown[]).unshift({ group: "Layer 99" })));
  assert.ok(has(group, "/sections/2/blocks/1/rows/0/group: 99"), group.join("\n"));
  const diagram = errors(
    changed((d) => blocks(d, "method").push({ type: "diagram", caption: "The pipeline, over 4,096 traces.", nodes: [{ id: "probe", label: "Linear probe", tip: "Reads layer 12 of the residual stream." }], edges: [] })),
  );
  assert.ok(has(diagram, "/sections/1/blocks/2/caption: 4,096"), diagram.join("\n"));
  const tags = (i: number) => changed((d) => ((d.sections[i] as unknown as { facts: Loose[] }).facts[0]!.tags = [{ text: "Qwen3-77B" }, { text: "Qwen3-8B" }]));
  assert.ok(has(errors(tags(0)), "/sections/0/facts/0/tags/0/text: 77B"), errors(tags(0)).join("\n"));
  assert.ok(has(errors(tags(6)), "/sections/6/facts/0/tags/0/text: 77B"));
  assert.equal(errors(tags(0)).filter((e) => e.startsWith("/sections/")).length, 1, "the paper's own model name passes");

  const labels = changed((d) => {
    (d.sections[2] as Loose).title = "Finding · 99 probes";
    (d.sections[0] as unknown as { facts: Loose[] }).facts[0]!.name = "Kind 99";
    blocks(d, "method").push({ type: "h3", text: "Step 99" }, { type: "eq", tex: String.raw`\hat{y} = \sigma(w^\top h_{4096} + \tfrac{1}{99})`, caption: "The probe on layer 12." });
  });
  assert.deepEqual(errors(labels), []);
  const caption = errors(changed((d) => blocks(d, "method").push({ type: "eq", tex: "y = wx", caption: "The probe on layer 99." })));
  assert.ok(has(caption, "/sections/1/blocks/2/caption: 99"), "an equation's caption is prose");
});

test("a figure or table the paper does not have is rejected where it is cited", () => {
  const figure = errors(saying("Accuracy rises with depth, as Figure 4 shows."));
  assert.ok(has(figure, "/sections/1/blocks/0/text: cites Figure 4, but the paper's source has 1 figure"), figure.join("\n"));
  const table = errors(changed((d) => (blocks(d, "result")[2]!.where = "§3, Table 3")));
  assert.deepEqual(table, ["/sections/2/blocks/2/where: cites Table 3, but the paper's source has 1 table"]);
  assert.deepEqual(errors(saying("Accuracy rises with depth (Fig. 1), and Table 1 gives the totals.")), []);
});

test("a figure code can neither find nor rule out is the reviewer's, by name: in a PDF's text, past the main text's count, under the source's own numbering", () => {
  const left = (d: Draft, over?: Over) => {
    const checked = check(d, ctx(over));
    return [checked.errors, checked.unverified];
  };
  // a PDF's text has no environments to count: a label is there when the text mentions it, and unknown when not
  assert.deepEqual(left(saying("See Figure 2 and Table 3."), { paper: PDF, source: "pdf" }), [[], []]);
  assert.deepEqual(left(saying("See Figure 5."), { paper: PDF, source: "pdf" }), [[], ["Figure 5"]]);
  // one figure in the main text and one in the appendix: Figure 2 may be the appendix's, or A.1 may be
  assert.deepEqual(left(saying("See Figure 1."), { paper: APPENDIX_FIGURE }), [[], []]);
  assert.deepEqual(left(saying("See Figure 2."), { paper: APPENDIX_FIGURE }), [[], ["Figure 2"]]);
  assert.deepEqual(left(saying("See Figure 3."), { paper: APPENDIX_FIGURE }), [["/sections/1/blocks/0/text: cites Figure 3, but the paper's source has at most 2 figures"], []]);
  // a source that sets the figure counter itself: no number within the count is known to be the paper's
  const reset = APPENDIX_FIGURE.replace("\\appendix", "\\appendix\\setcounter{figure}{0}\\renewcommand{\\thefigure}{S\\arabic{figure}}");
  assert.deepEqual(left(saying("See Figures 1 and S1."), { paper: reset }), [[], ["Figure 1", "Figure S1"]]);
  assert.deepEqual(left(saying("See Figure 3."), { paper: reset }), [[], ["Figure 3"]], "past the count too: the count says nothing of numbers the source sets");
});

test("a LaTeX source that numbers its figures itself has no figure number refused or confirmed", () => {
  const left = (text: string, paper: string) => {
    const checked = check(saying(text), ctx({ paper }));
    return [checked.errors, checked.unverified];
  };
  // by LaTeX's defaults the one figure is Figure 1, and there is no Figure 11
  assert.deepEqual(left("See Figures 1 and 11.", BODY), [["/sections/1/blocks/0/text: cites Figure 11, but the paper's source has 1 figure"], []]);
  // the counter set, so the one figure is Figure 11
  const eleven = BODY.replace("\\begin{figure}", "\\setcounter{figure}{10}\n\\begin{figure}");
  assert.deepEqual(left("See Figures 1 and 11.", eleven), [[], ["Figure 1", "Figure 11"]]);
  for (const custom of [
    "\\addtocounter{figure}{-1}",
    "\\renewcommand{\\thefigure}{\\Roman{figure}}",
    "\\renewcommand\\thefigure{S\\arabic{figure}}",
    "\\numberwithin{figure}{section}",
    "\\counterwithin{figure}{section}",
  ])
    assert.deepEqual(left("See Figures 1 and 11.", BODY.replace("\\begin{figure}", `${custom}\n\\begin{figure}`)), [[], ["Figure 1", "Figure 11"]], custom);
  // What renumbers every float: a continued one, the package that ties counters, chapters, a class that has
  // them. The fixture's own cite of Table 1 is then unknown as well, and with the last three its §3.
  const all = (custom: string, paper: string) => assert.deepEqual(left("See Figures 1 and 11.", paper).map((list) => list.filter((name) => name !== "§3")), [[], ["Figure 1", "Figure 11", "Table 1"]], custom);
  for (const custom of ["\\ContinuedFloat", "\\usepackage[nottoc]{chngcntr}", "\\chapter{One}"]) all(custom, BODY.replace("\\begin{figure}", `${custom}\n\\begin{figure}`));
  all("report", BODY.replace("\\documentclass{article}", "\\documentclass[11pt]{report}"));
  // another counter's setting, or one in a comment, leaves the figures' numbering LaTeX's
  for (const other of ["\\setcounter{table}{4}", "% \\setcounter{figure}{10}", "\\setcounter{topnumber}{3}"])
    assert.equal(left("See Figure 11.", BODY.replace("\\begin{figure}", `${other}\n\\begin{figure}`))[0].length, 1, other);
  assert.deepEqual(left("See Table 5.", BODY.replace("\\begin{figure}", "\\setcounter{table}{4}\n\\begin{figure}")), [[], ["Table 5", "Table 1"]], "with the fixture's own Table 1");
  // the source's own inventory still holds: one with no table has none, however it numbers
  const none = BODY.replace(/\\begin\{table\}[\s\S]*?\\end\{table\}/, "\\setcounter{table}{4}").replace("Table~\\ref{tab:main}", "the appendix");
  assert.equal(left("See Table 5.", none)[0][0], "/sections/1/blocks/0/text: cites Table 5, but the paper's source has no table");
});

test("a citation is read in any case, and a range names every label in it", () => {
  const labels = (text: string) => cites(text).map((c) => `${c.kind} ${c.label}`);
  assert.deepEqual(labels("see fig. 1 and table 2, then FIGURE 3 and Tab. 4"), ["figure 1", "table 2", "figure 3", "table 4"]);
  assert.deepEqual(labels("Figures 1–3"), ["figure 1", "figure 2", "figure 3"]);
  assert.deepEqual(labels("Figs. 1-3"), ["figure 1", "figure 2", "figure 3"]);
  assert.deepEqual(labels("Tables 2 to 4"), ["table 2", "table 3", "table 4"]);
  assert.deepEqual(labels("figures A.1–A.3 and tables S2—S4"), ["figure A.1", "figure A.2", "figure A.3", "table S2", "table S3", "table S4"]);
  // a list stays a list, and a range inside one is still a range
  assert.deepEqual(labels("Figures 1, 3 and 5"), ["figure 1", "figure 3", "figure 5"]);
  assert.deepEqual(labels("Figures 1–3 and 7"), ["figure 1", "figure 2", "figure 3", "figure 7"]);
  // ends that do not count the same way, or count down, are the two ends; a singular cites its first label
  assert.deepEqual(labels("Figures 3a–3c, Figures 2–A.1 and Figures 4–2"), ["figure 3a", "figure 3c", "figure 2", "figure A.1", "figure 4", "figure 2"]);
  assert.deepEqual(labels("Figure 1–3"), ["figure 1"]);

  // through the checks: a lowercase cite is held to the paper like any other
  assert.deepEqual(errors(saying("The numbers are in table 9.")), ["/sections/1/blocks/0/text: cites Table 9, but the paper's source has 1 table"]);
  const lower = check(saying("Accuracy peaks near 0.84 at layer 9 (fig. 1)."), ctx({ figures: { probe: { label: "1" } } }));
  assert.deepEqual([lower.errors, lower.plots], [[], ["probe"]]);
});

test("a fig block's number is looked up however it is written", () => {
  const showing = (number: string) => changed((d) => blocks(d, "method").push({ type: "fig", name: "probe", number, caption: "Probe accuracy by layer." }));
  const made = { figures: { probe: {} } };
  assert.deepEqual(errors(showing("999"), made), ["/sections/1/blocks/2/number: cites Figure 999, but the paper's source has 1 figure"]);
  assert.deepEqual(errors(showing("Figure 999"), made), ["/sections/1/blocks/2/number: cites Figure 999, but the paper's source has 1 figure"]);
  for (const number of ["1", "Fig. 1", "Figure 1"]) assert.deepEqual(check(showing(number), ctx(made)), { errors: [], plots: [], unverified: [] }, number);
  // a bare number shows the figure as that one: a value cited to it has its image
  const plot = changed((d) => {
    blocks(d, "method")[0]!.text = "Accuracy peaks near 0.84 at layer 9 (Fig. 1).";
    blocks(d, "method").push({ type: "fig", name: "probe", number: "1", caption: "Probe accuracy by layer." });
  });
  assert.deepEqual(check(plot, ctx(made)).plots, ["probe"]);
  // what code cannot look up is named: a number a PDF's text does not mention, and one with no number in it
  assert.deepEqual(check(showing("7"), ctx({ ...made, paper: PDF, source: "pdf" })).unverified, ["Figure 7"]);
  assert.deepEqual(check(showing("Teaser"), ctx(made)), { errors: [], plots: [], unverified: ['the figure numbered "Teaser"'] });
});

test("a LaTeX source with no table has none: every cite of one is rejected, an appendix's too", () => {
  const none = BODY.replace(/\\begin\{table\}[\s\S]*?\\end\{table\}\n/, "");
  const cited = errors(draft(), { paper: none });
  assert.ok(has(cited, "/sections/2/blocks/1/caption: cites Table 1, but the paper's source has no table"), cited.join("\n"));
  assert.ok(has(cited, "/sections/3/claims/0/where: cites Table 1, but the paper's source has no table"));
  assert.ok(has(errors(saying("The totals are in Table A.1."), { paper: none }), "/sections/1/blocks/0/text: cites Table A.1, but the paper's source has no table"));
  // where the source has tables, an appendix's own numbering is not in it: the cite is the reviewer's to check
  const unknown = check(saying("The totals are in Table A.1."), ctx());
  assert.deepEqual([unknown.errors, unknown.unverified], [[], ["Table A.1"]]);
});

test("a paper's Figure 1 and its Figure A.1 are two figures: a label is read whole", () => {
  assert.deepEqual(["Figure A.1", "Fig. 3", "Figure S2 (left)", "3b", "Figure A1", "Extended Data Fig. 4"].map(figureLabel), ["A.1", "3", "S2", "3", "A1", "4"]);
  const plot = (label: string) => saying(`Loss falls to about 0.31 by epoch 9 (Fig. ${label}). The probe is linear.`);
  // the session made the appendix's figure, not the main text's
  const made = { paper: APPENDIX_FIGURE, figures: { loss: { label: "A.1" } } };
  const appendix = check(plot("A.1"), ctx(made));
  assert.deepEqual([appendix.errors, appendix.plots, appendix.unverified], [[], ["loss"], ["Figure A.1"]]);
  assert.deepEqual(check(plot("A1"), ctx(made)).plots, ["loss"], "A1 and A.1 are one label");
  const main = errors(plot("1"), made);
  assert.ok(has(main, "/sections/1/blocks/0/text: 0.31 is not in the paper's text, and this session has no image of Figure 1"), main.join("\n"));
  // a fig block's number is held to the label its figure was made as
  const showing = (number: string) => changed((d) => blocks(d, "method").push({ type: "fig", name: "loss", number, caption: "Training loss by epoch." }));
  assert.deepEqual(errors(showing("Figure A.1"), made), []);
  assert.deepEqual(errors(showing("Figure 1"), made), ['/sections/1/blocks/2/number: "Figure 1", but figure "loss" was made as the paper\'s Figure A.1']);
});

test("a location names a place the paper has: a numbered one that exists, or a section by its title", () => {
  const claim = (d: Draft) => (d.sections[3] as unknown as { claims: Loose[] }).claims[0]!;
  // left out: the schema's
  assert.ok(has(draftErrors(changed((d) => delete claim(d).where)), "/sections/3/claims/0/where: is required"));
  assert.ok(has(draftErrors(changed((d) => delete blocks(d, "result")[2]!.where)), "/sections/2/blocks/2/where: is required"));
  // given, but no place
  for (const where of ["throughout the paper", "see above", "Section unavailable", "Equation unavailable", "Discussion"]) {
    const vague = errors(resting(where));
    assert.deepEqual(vague, [`/sections/3/claims/0/where: "${where}" names no place in the paper: give a numbered section, figure, table, appendix, equation or page of the paper, or a section by its title`], where);
  }
  const note = errors(changed((d) => (blocks(d, "result")[2]!.where = "see above")));
  assert.ok(has(note, "/sections/2/blocks/2/where:", "names no place"), note.join("\n"));
  // a number the outline lacks, at any depth
  assert.deepEqual(errors(resting("§7")), ["/sections/3/claims/0/where: cites §7, but the paper has no section 7"]);
  assert.deepEqual(errors(resting("§2.999")), ["/sections/3/claims/0/where: cites §2.999, but the paper has no section 2.999"]);
  assert.deepEqual(errors(resting("Sections 2.1 and 3")), ["/sections/3/claims/0/where: cites §2.1, but the paper has no section 2.1"], "section 2 has no subsection");
  assert.deepEqual(errors(resting("§2.1"), { paper: NESTED }), [], "it has one here");
  assert.deepEqual(errors(resting("§2.2"), { paper: NESTED }), ["/sections/3/claims/0/where: cites §2.2, but the paper has no section 2.2"]);
  assert.deepEqual(errors(resting("Appendix B")), ["/sections/3/claims/0/where: cites Appendix B, but the paper has no appendix B"]);
  assert.deepEqual(errors(resting("Appendix A.1")), ["/sections/3/claims/0/where: cites Appendix A.1, but the paper has no appendix A.1"]);
  // places it has
  for (const where of ["Appendix A", "App. A", "Appendix", "Table 1", "§2", "Sec. 3", "§1–3", "Abstract", "Results", "Method, second paragraph", "the Results section", "Probe training details"])
    assert.deepEqual(check(resting(where), ctx()), { errors: [], plots: [], unverified: [] }, where);
});

test("a section's whole title is a place however short, and a part of one only when long enough", () => {
  const short = BODY.replace("\\section{Method}", "\\section{PPO}").replace("\\section{Results}", "\\section{RL and SFT}");
  for (const where of ["PPO", "the PPO section", "PPO, second paragraph", "RL and SFT"]) assert.deepEqual(check(resting(where), ctx({ paper: short })), { errors: [], plots: [], unverified: [] }, where);
  // "RL" is no title, and too short to count as part of one
  assert.ok(has(errors(resting("RL"), { paper: short }), '/sections/3/claims/0/where: "RL" names no place in the paper'));
  assert.ok(has(errors(resting("SAC"), { paper: short }), '/sections/3/claims/0/where: "SAC" names no place in the paper'));
});

test("a place code does not look up is the reviewer's, by name: an equation, an algorithm, a page, an appendix no command starts", () => {
  const left = (where: string, over?: Over) => {
    const checked = check(resting(where), ctx(over));
    return [checked.errors, checked.unverified];
  };
  assert.deepEqual(left("Eq. 9999"), [[], ["Eq. 9999"]]);
  assert.deepEqual(left("Algorithm 9999, line 3"), [[], ["Algorithm 9999", "line 3"]]);
  assert.deepEqual(left("§3, p. 9999"), [[], ["p. 9999"]], "the section is the outline's, the page is not code's to say");
  assert.deepEqual(left("Theorem 2 and Footnote 14"), [[], ["Theorem 2", "Footnote 14"]]);
  // a source with no appendix command may still hold one under headings of its own
  assert.deepEqual(left("Appendix A", { paper: NO_APPENDIX }), [[], ["Appendix A"]]);
  assert.deepEqual(left("Appendix", { paper: NO_APPENDIX }), [[], ["the appendix"]]);
  // a source that numbers its sections itself: no number is known to be the paper's
  const reset = BODY.replace("\\appendix", "\\setcounter{section}{0}\\renewcommand{\\thesection}{S\\arabic{section}}");
  assert.deepEqual(left("§7", { paper: reset }), [[], ["§3", "§7"]], "the fixture's note cites §3");
  // a subsection counter set: the one subsection of section 2 is 2.4, which LaTeX's defaults would call 2.1
  assert.deepEqual(left("§2.4", { paper: NESTED })[0], ["/sections/3/claims/0/where: cites §2.4, but the paper has no section 2.4"]);
  const fourth = NESTED.replace("\\subsection{Probe training}", "\\setcounter{subsection}{3}\n\\subsection{Probe training}");
  assert.deepEqual(left("§2.4", { paper: fourth }), [[], ["§3", "§2.4"]]);
});

test("a PDF's text refuses no section: a number a line opens with is there, and any other is the reviewer's", () => {
  const left = (where: string, paper = PDF) => {
    const checked = check(resting(where), ctx({ paper, source: "pdf" }));
    return [checked.errors, checked.unverified];
  };
  for (const where of ["§2.1", "Section 3", "Method", "Probe training"]) assert.deepEqual(left(where), [[], []], where);
  assert.deepEqual(left("§2.9"), [[], ["§2.9"]]);
  assert.deepEqual(left("§4, Appendix B"), [[], ["§4", "Appendix B"]]);
  // a heading whose number and title the PDF's text has on separate lines
  const split = "Probes read the trace\n1 Introduction\nFigure 2 shows the probe.\n2\nMethod\nLayer 12 of Qwen3-8B, 1,200 traces.\n3 Results\nTables 1–3 give 71.3% against 50%.\n";
  assert.deepEqual(left("§2", split), [[], ["§2"]], "section 2 is there, though no line shows it");
  assert.deepEqual(left("§1", split), [[], []]);
  // a location that names nothing code knows may name a heading the text's reading did not find
  assert.deepEqual(left("Discussion"), [[], ['the location "Discussion"']]);
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

test("a value read off a plot passes only with its figure cited in the same sentence and that figure's image in the session", () => {
  const plot = "Accuracy peaks near 0.84 at layer 9 (Fig. 1). The probe is linear.";
  const told = "/sections/1/blocks/0/text: 0.84 is not in the paper's text, and this session has no image of Figure 1 to check it against";
  // cited, but the session has no image of it for the reviewer: with none made, and with one made as no figure of the paper's
  assert.ok(has(errors(saying(plot)), told), errors(saying(plot)).join("\n"));
  assert.ok(has(errors(saying(plot), { figures: { probe: {} } }), told));
  assert.ok(has(errors(saying(plot), { figures: { probe: { label: "2" } } }), told), "another figure's image is no evidence");
  assert.ok(has(errors(saying(plot)), "Numbers:", 'figure(file, name, "Figure 6")'), "the rule says how to give the image");

  // made as the paper's Figure 1: the value goes to the reviewer with that image, whether or not the read shows it
  const made = check(saying(plot), ctx({ figures: { probe: { label: "1" } } }));
  assert.deepEqual([made.errors, made.plots], [[], ["probe"]]);
  // made under a name only, and shown as Figure 1 by a fig block
  const showing = changed((d) => {
    blocks(d, "method")[0]!.text = plot;
    blocks(d, "method").push({ type: "fig", name: "probe", number: "Figure 1", caption: "Probe accuracy by layer, peaking at 0.84." });
  });
  const shown = check(showing, ctx({ figures: { probe: {} } }));
  assert.deepEqual([shown.errors, shown.plots], [[], ["probe"]], "a figure's own caption gives what the plot shows");
  // a table built from a figure: its cells are read off it
  const built = changed((d) => {
    blocks(d, "result")[1]!.caption = "Read off Figure 1.";
    (blocks(d, "result")[1]!.rows as { cells: unknown[] }[])[0]!.cells[1] = 0.84;
  });
  assert.deepEqual(check(built, ctx({ figures: { probe: { label: "1" } } })).plots, ["probe"]);
  assert.ok(has(errors(built), "/sections/2/blocks/1/rows/0/cells/1: 0.84 is not in the paper's text, and this session has no image of Figure 1"));

  // two figures cited for two values: either value may be either figure's, so both images are needed
  const two = { paper: BODY.replace("\\section{Results}", "\\begin{figure}\n\\includegraphics{figs/loss.pdf}\n\\caption{Loss by step.}\n\\end{figure}\n\\section{Results}") };
  const both = saying("Figures 1 and 2 show accuracy 0.84 and loss 0.31, respectively.");
  assert.deepEqual(errors(both, { ...two, figures: { probe: { label: "1" } } })[0], "/sections/1/blocks/0/text: 0.84, 0.31 are not in the paper's text, and this session has no image of Figure 2 to check them against");
  const pair = check(both, ctx({ ...two, figures: { probe: { label: "1" }, loss: { label: "2" } } }));
  assert.deepEqual([pair.errors, pair.plots], [[], ["probe", "loss"]]);
  const built2 = changed((d) => {
    blocks(d, "result")[1]!.caption = "Read off Figures 1 and 2.";
    (blocks(d, "result")[1]!.rows as { cells: unknown[] }[])[0]!.cells[1] = 0.84;
  });
  assert.ok(has(errors(built2, { ...two, figures: { probe: { label: "1" } } }), "/sections/2/blocks/1/rows/0/cells/1: 0.84 is not in the paper's text, and this session has no image of Figure 2 "), "a table built from two figures needs both");

  // a range cites every figure in it: the one in the middle needs its image too
  const three = { paper: two.paper.replace("\\section{Results}", "\\begin{figure}\n\\includegraphics{figs/norm.pdf}\n\\caption{Norm by layer.}\n\\end{figure}\n\\section{Results}") };
  const ranged = saying("Accuracy peaks near 0.84 in every setting (Figures 1–3).");
  const ends = { probe: { label: "1" }, norm: { label: "3" } };
  assert.deepEqual(errors(ranged, { ...three, figures: ends })[0], "/sections/1/blocks/0/text: 0.84 is not in the paper's text, and this session has no image of Figure 2 to check it against");
  const all = check(ranged, ctx({ ...three, figures: { ...ends, loss: { label: "2" } } }));
  assert.deepEqual([all.errors, all.plots], [[], ["probe", "loss", "norm"]]);

  // a figure's caption that cites another figure: the value may be that one's, so its image is needed as well
  const captioned = changed((d) => blocks(d, "method").push({ type: "fig", name: "probe", number: "Figure 1", caption: "Compared with Figure 2, accuracy is 0.84." }));
  assert.deepEqual(errors(captioned, { ...two, figures: { probe: {} } }), [
    "/sections/1/blocks/2/caption: 0.84 is not in the paper's text, and this session has no image of Figure 2 to check it against",
    errors(saying(plot)).at(-1)!,
  ]);
  const compared = check(captioned, ctx({ ...two, figures: { probe: {}, loss: { label: "2" } } }));
  assert.deepEqual([compared.errors, compared.plots], [[], ["probe", "loss"]]);

  // without the cite, or with it in another sentence, the value is only a number the paper lacks
  const imaged = { figures: { probe: { label: "1" } } };
  assert.ok(has(errors(saying("Accuracy peaks near 0.84 at layer 9."), imaged), "/sections/1/blocks/0/text: 0.84 is not in the paper's text"));
  assert.ok(has(errors(saying("Accuracy peaks near 0.84 at layer 9. The curve is in Fig. 1."), imaged), "/sections/1/blocks/0/text: 0.84 is not in the paper's text"));
  // a table cited is no plot
  assert.ok(has(errors(saying("Accuracy peaks near 0.84 at layer 9 (Table 1)."), imaged), "/sections/1/blocks/0/text: 0.84 is not in the paper's text"));
});

test("a fig block shows only a figure this session made", () => {
  const d = changed((r) => blocks(r, "method").push({ type: "fig", name: "probe", number: "Figure 1", caption: "Probe accuracy by layer." }));
  assert.ok(has(errors(d), '/sections/1/blocks/2/name: this session made no figure "probe"'));
  assert.deepEqual(errors(d, { figures: { probe: {} } }), []);
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

test("the web app's fixture reads pass the checks, each read's own JSON standing for its paper's text", () => {
  for (const name of ["read-icl", "read-scarce"]) {
    const read = JSON.parse(readFileSync(path.resolve(import.meta.dirname, "../../web/src/deepread/fixtures", `${name}.json`), "utf8")) as Draft;
    const { process_notes: _, ...model } = read;
    const topics = read.sections.flatMap((s) => (s.kind === "lens" ? [s.topic] : []));
    const figures = Object.fromEntries(read.sections.flatMap((s) => ("blocks" in s ? s.blocks.flatMap((b) => (b.type === "fig" ? [[b.name, {}]] : [])) : [])));
    const found = { collection: new Set(read.among_collection.map((a) => a.arxiv_id)), self: "0000.00000" };
    // a paper that says everything the read does, and shows no numbering to hold its locations to
    assert.deepEqual(errors(model, { paper: JSON.stringify(read), source: "pdf", found, topics, figures }), [], name);
  }
});
