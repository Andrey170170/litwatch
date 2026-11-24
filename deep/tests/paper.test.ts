import assert from "node:assert/strict";
import { test } from "node:test";
import { headings, outline, splitPaper } from "../src/paper.ts";
import { BODY, latex } from "./support.ts";

/** A PDF's text, as litwatch sends one: with no outline. */
const pdf = (body: string) => ({ body, body_source: "pdf" as const });
/** `BODY` without its appendix. */
const NO_APPENDIX = BODY.slice(0, BODY.indexOf("\\appendix")) + "\\end{document}\n";

// A LaTeX source is read by the outline litwatch sends with it (its parsing is tested in tests/test_outline.py).

test("a LaTeX paper's bibliography is cut where its outline puts it, before the appendix, and the appendix to the end", () => {
  const { main, cuts } = splitPaper(latex(BODY));
  assert.deepEqual(cuts.map((c) => c.part), ["references", "appendix"]);
  assert.ok(main.includes("The probe reaches 71.3\\% accuracy"), "the results stay");
  assert.ok(!main.includes("Turpin"), "no bibliography entry");
  assert.ok(!main.includes("learning rate"), "no appendix");
  assert.equal(BODY.slice(cuts[0]!.start, cuts[0]!.end).trim(), BODY.slice(BODY.indexOf("\\begin{thebibliography}"), BODY.indexOf("\\appendix")).trim());
  assert.equal(cuts[1]!.end, BODY.length);
  assert.equal(main.length + cuts.reduce((n, c) => n + c.end - c.start, 0), BODY.length, "nothing is lost or kept twice");
  // a bibliography the appendix starts inside ends where the appendix starts
  const text = latex(BODY);
  const inside = { ...text, outline: { ...text.outline!, appendix: text.outline!.references!.start + 10 } };
  assert.deepEqual(splitPaper(inside).cuts.map((c) => [c.part, c.end - c.start]), [["references", 10], ["appendix", BODY.length - inside.outline.appendix]]);
});

test("a paper with no appendix loses only its bibliography, and one with neither is all main text", () => {
  const { main, cuts } = splitPaper(latex(NO_APPENDIX));
  assert.deepEqual(cuts.map((c) => c.part), ["references"]);
  assert.ok(main.includes("\\section{Results}") && main.endsWith("\\end{document}\n"), "what follows the bibliography stays");
  assert.ok(!main.includes("Turpin"));
  const bare = "\\documentclass{article}\\begin{document}\\section{Method}We probe.\\end{document}";
  assert.deepEqual(splitPaper(latex(bare)), { main: bare, cuts: [] });
});

test("a LaTeX paper's headings are its outline's: their numbers as sent, their titles as plain text, one level down per depth", () => {
  const body = "\\section{Results on \\textsc{BBH}\\label{sec:res}}\nText.\n\\subsection{Probes \\& more}\nMore.\n";
  const sent = { body, body_source: "latex" as const, outline: { sections: [
    { title: "Results on \\textsc{BBH}\\label{sec:res}", depth: 0, number: "IV", start: 0, end: body.length },
    { title: "Probes \\& more", depth: 1, start: body.indexOf("\\subsection"), end: body.length },
  ] } };
  assert.deepEqual(headings(sent), [
    { level: 1, title: "Results on BBH", number: "IV", start: 0, end: body.length, chars: body.length },
    { level: 2, title: "Probes & more", start: body.indexOf("\\subsection"), end: body.length, chars: body.length - body.indexOf("\\subsection") },
  ]);
});

test("a PDF's text is cut at its References and Appendix headings", () => {
  const md = "# Probes\n\n## 1 Method\n\nWe probe.\n\n## References\n\n- [1] Turpin et al.\n\n## Appendix A\n\nDetails.\n\n### A.1 More\n\nMore details.\n";
  const { main, cuts } = splitPaper(pdf(md));
  assert.deepEqual(cuts.map((c) => c.part), ["references", "appendix"]);
  assert.equal(main, "# Probes\n\n## 1 Method\n\nWe probe.\n\n");
  // references last, with no appendix; and an appendix before the references
  const last = "# Probes\n\n## Method\n\nWe probe.\n\n## References\n\n- [1] Turpin et al.\n";
  assert.deepEqual(splitPaper(pdf(last)), { main: "# Probes\n\n## Method\n\nWe probe.\n\n", cuts: [{ part: "references", start: last.indexOf("## References"), end: last.length }] });
  const swapped = "# Probes\n\n## Method\n\nWe probe.\n\n## Appendix\n\nDetails.\n\n## References\n\n- [1] Turpin et al.\n";
  assert.deepEqual(splitPaper(pdf(swapped)).cuts.map((c) => c.part), ["appendix"], "a bibliography inside the appendix goes with it");
  // an appendix a level below the references, with no heading after it at theirs, ends the bibliography
  const below = "# Probes\n\n## Method\n\nWe probe.\n\n## References\n\n- [1] Turpin et al.\n\n### Appendix A\n\nDetails.\n";
  const appendix = below.indexOf("### Appendix A");
  assert.deepEqual(splitPaper(pdf(below)).cuts, [
    { part: "references", start: below.indexOf("## References"), end: appendix },
    { part: "appendix", start: appendix, end: below.length },
  ]);
});

// The forms litwatch's archive gives a PDF's text (src/litwatch/archive/text.py): pymupdf4llm's Markdown, whose
// headings come bold with a space after and whose appendix is lettered headings after the references; and, without
// that reader, pypdfium2's plain text, whose headings are lines like any other.
const MARKDOWN = [
  "# **Probes read the trace** ", "", "## **1 Method** ", "", "We probe, see Appendix A for details.", "", "## **References** ", "", "- Turpin et al. Language models don't always say what they think.", "",
  "## **A Additional Samples** ", "", "See Table 6.", "", "### **A.1 BBH** ", "", "More.", "",
].join("\n");
const PLAIN = [
  "Probes read the trace", "Contents", "1 Introduction", "References", "Appendix", "1 Introduction", "We probe 3 models, see", "Appendix A for more details). Then we compare", "2 Ghost Whispers", "References",
  "A. Vaswani, N. Shazeer. Attention is all you need.", "J. Andreas. Language models as agent models. 2022.", "15", "A Additional Samples", "See Table 6.", "C.1 BBH", "More.", "",
].join("\n");

test("a PDF's text is cut at the bold headings its Markdown form has: References, and the appendix by letter or by name", () => {
  const { main, cuts } = splitPaper(pdf(MARKDOWN));
  assert.deepEqual(cuts.map((c) => [c.part, MARKDOWN.slice(c.start, c.start + 22)]), [["references", "## **References** \n\n- "], ["appendix", "## **A Additional Samp"]]);
  assert.equal(main, MARKDOWN.slice(0, MARKDOWN.indexOf("## **References**")), "the main text ends where the references start");
  assert.equal(cuts[1]!.end, MARKDOWN.length);
  assert.deepEqual(headings(pdf(MARKDOWN)).map((h) => h.title), ["Probes read the trace", "1 Method", "References", "A Additional Samples", "A.1 BBH"], "a title is given without its emphasis");
  // named, with the references after it or before it
  const named = MARKDOWN.replace("## **A Additional Samples** ", "## **Appendix A: Additional Samples** ");
  assert.deepEqual(splitPaper(pdf(named)).cuts.map((c) => c.part), ["references", "appendix"]);
  const first = "# **Probes** \n\n## **1 Method** \n\nWe probe.\n\n## **Appendix** \n\nDetails.\n\n## **References** \n\n- Turpin et al.\n";
  assert.deepEqual(splitPaper(pdf(first)).cuts.map((c) => [c.part, c.start]), [["appendix", first.indexOf("## **Appendix**")]]);
  // what follows the references under a heading that is no appendix's stays main text
  const methods = MARKDOWN.replace("## **A Additional Samples** ", "## **Methods** ").replace("### **A.1 BBH** ", "### **Probes** ");
  const kept = splitPaper(pdf(methods));
  assert.deepEqual(kept.cuts.map((c) => c.part), ["references"]);
  assert.ok(kept.main.includes("## **Methods**") && !kept.main.includes("Turpin"));
  // a contents list in headings ahead of the Introduction names the parts, and starts neither
  const listed = "# **Probes** \n\n## **Contents** \n\n### **1 Introduction** \n\n### **References** \n\n### **Appendix A** \n\n" + MARKDOWN.replace("## **1 Method** ", "## **1 Introduction** ");
  const contents = splitPaper(pdf(listed));
  assert.deepEqual(contents.cuts.map((c) => [c.part, c.start]), [["references", listed.lastIndexOf("## **References**")], ["appendix", listed.indexOf("## **A Additional Samples**")]]);
  assert.ok(contents.main.includes("We probe, see Appendix A"), "the main text stays in the message");
  // so does a plain contents line naming References, in a paper whose bibliography has no heading
  const plain = "Probes\nContents\n1 Introduction\nReferences\n\n## **1 Introduction** \n\nWe probe the trace.\n\n## **2 Results** \n\nIt works.\n";
  assert.deepEqual(splitPaper(pdf(plain)).cuts, []);
  // an Introduction heading may end in a full stop
  const stopped = "## **Contents** \n\n### **1 Introduction** \n\n### **References** \n\n### **Appendix A** \n\n" + MARKDOWN.replace("## **1 Method** ", "## **1. Introduction.** ");
  assert.deepEqual(splitPaper(pdf(stopped)).cuts.map((c) => c.part), ["references", "appendix"]);
  assert.ok(splitPaper(pdf(stopped)).main.includes("We probe, see Appendix A"));
  // an appendix's own Introduction, a level below the paper's, moves neither cut
  const nested = MARKDOWN.replace("## **1 Method** ", "## **1 Introduction** ") + "\n### **Introduction.** \n\nThe appendix opens.\n";
  assert.deepEqual(splitPaper(pdf(nested)).cuts.map((c) => [c.part, c.start]), [["references", nested.indexOf("## **References**")], ["appendix", nested.indexOf("## **A Additional Samples**")]]);
  // and in a paper with no Introduction of its own, the appendix's is none
  const bare = "## **Method** \n\nWe probe.\n\n## **References** \n\n- Turpin et al.\n\n## **Appendix A** \n\n### **Introduction** \n\nMore.\n";
  assert.deepEqual(splitPaper(pdf(bare)).cuts.map((c) => [c.part, c.start]), [["references", bare.indexOf("## **References**")], ["appendix", bare.indexOf("## **Appendix A**")]]);
  const lettered = bare.replace("## **Appendix A** ", "## **A Additional Samples** ");
  assert.deepEqual(splitPaper(pdf(lettered)).cuts.map((c) => [c.part, c.start]), [["references", lettered.indexOf("## **References**")], ["appendix", lettered.indexOf("## **A Additional Samples**")]]);
  // plain text keeps the rules it had before: an appendix's Introduction line loses the appendix cut but not the
  // References one, a stray Markdown heading changes neither, and "Introduction." is no Introduction line
  const lines = "Probes\n1 Introduction\nWe probe the trace.\nReferences\nA. Author. A paper. 2024.\nAppendix A\nIntroduction\nMore.\n";
  assert.deepEqual(splitPaper(pdf(lines)).cuts.map((c) => c.part), ["references"]);
  assert.deepEqual(splitPaper(pdf(lines.replace("References\n", "# Failed\n37\nReferences\n"))).cuts.map((c) => c.part), ["references"]);
  // a Markdown Introduction after a plain appendix line is the appendix's, and the References heading still cuts
  const mixed = "# **Paper** \n\n## **1 Introduction** \n\nMain text.\n\n## **References** \n\nAuthor. A paper. 2024.\n\nAppendix A\n\n## **Introduction** \n\nSupplement.\n";
  assert.deepEqual(splitPaper(pdf(mixed)).cuts.map((c) => c.part)[0], "references");
  assert.equal(splitPaper(pdf(mixed)).cuts[0]!.start, mixed.indexOf("## **References**"));
  const ahead = "Probes\n1 Introduction\nWe probe the trace.\nAppendix A\nIntroduction.\nMore.\nReferences\nA. Author. A paper. 2024.\n";
  assert.deepEqual(splitPaper(pdf(ahead)).cuts.map((c) => [c.part, c.start]), [["appendix", ahead.indexOf("Appendix A")]]);
});

test("a PDF's plain text is cut at its References line, and at the appendix's: lettered after it, or named anywhere", () => {
  const { main, cuts } = splitPaper(pdf(PLAIN));
  const references = PLAIN.lastIndexOf("References");
  // the last line that reads References is the heading, not the table of contents' entry, and the contents'
  // Appendix line, before the Introduction, is none either; a line of prose that starts "Appendix A" is no
  // heading, nor is a line of the bibliography that starts with an initial
  assert.deepEqual(cuts, [
    { part: "references", start: references, end: PLAIN.indexOf("A Additional Samples") },
    { part: "appendix", start: PLAIN.indexOf("A Additional Samples"), end: PLAIN.length },
  ]);
  assert.equal(main, PLAIN.slice(0, references));
  assert.deepEqual(headings(pdf(PLAIN)).map((h) => h.title), ["References", "A Additional Samples"], "the two headings a plain text can be told to have");
  // an appendix that says so
  for (const line of ["Appendix", "Appendix A", "Appendix A: Additional Samples", "APPENDIX", "Supplementary Material"]) {
    const named = PLAIN.replace("A Additional Samples", line);
    assert.deepEqual(splitPaper(pdf(named)).cuts.map((c) => [c.part, c.start]), [["references", references], ["appendix", named.lastIndexOf(line)]], line);
  }
  // no appendix: the references run to the end
  const none = PLAIN.slice(0, PLAIN.indexOf("15\n"));
  assert.deepEqual(splitPaper(pdf(none)).cuts, [{ part: "references", start: references, end: none.length }]);

  // an appendix that says so needs no References before it: it may stand ahead of them, or the paper have none
  for (const line of ["Appendix", "Appendix A", "Appendix A: Training Details"]) {
    const ahead = ["Probes read the trace", "1 Introduction", "We probe 3 models.", line, "Probes train for 5 epochs.", "References", "A. Vaswani, N. Shazeer. Attention is all you need.", ""].join("\n");
    const start = ahead.indexOf(`${line}\n`);
    assert.deepEqual(splitPaper(pdf(ahead)), { main: ahead.slice(0, start), cuts: [{ part: "appendix", start, end: ahead.length }] }, `${line}, ahead of the references`);
    const alone = ahead.slice(0, ahead.indexOf("References"));
    assert.deepEqual(splitPaper(pdf(alone)), { main: alone.slice(0, start), cuts: [{ part: "appendix", start, end: alone.length }] }, `${line}, with no references`);
    assert.deepEqual(headings(pdf(alone)).map((h) => h.title), [line]);
  }
  // a lettered line reads as a heading only after the references
  const lettered = ["Probes read the trace", "1 Introduction", "We probe 3 models.", "A Additional Samples", "See Table 6.", ""].join("\n");
  assert.deepEqual(splitPaper(pdf(lettered)), { main: lettered, cuts: [] });
});

test("the outline gives each heading's number and says which were left out of the message", () => {
  const { cuts } = splitPaper(latex(BODY));
  assert.equal(
    outline(headings(latex(BODY)).slice(3), cuts).replace(/\d+ characters/g, "n characters"),
    "- 3 Results (n characters)\n- References (n characters, bibliography: in the session only)\n- A Probe training details (n characters, appendix: in the session only)",
  );
});
