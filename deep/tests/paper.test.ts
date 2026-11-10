import assert from "node:assert/strict";
import { test } from "node:test";
import { headings, outline, splitPaper } from "../src/paper.ts";
import { BODY } from "./support.ts";

/** `BODY` without its appendix. */
const NO_APPENDIX = BODY.slice(0, BODY.indexOf("\\appendix")) + "\\end{document}\n";

test("a LaTeX paper's bibliography is cut where it sits before the appendix, and the appendix to the end", () => {
  const { main, cuts } = splitPaper(BODY, "latex");
  assert.deepEqual(cuts.map((c) => c.part), ["references", "appendix"]);
  assert.ok(main.includes("The probe reaches 71.3\\% accuracy"), "the results stay");
  assert.ok(!main.includes("Turpin"), "no bibliography entry");
  assert.ok(!main.includes("learning rate"), "no appendix");
  assert.equal(BODY.slice(cuts[0]!.start, cuts[0]!.end).trim(), BODY.slice(BODY.indexOf("\\begin{thebibliography}"), BODY.indexOf("\\appendix")).trim());
  assert.equal(cuts[1]!.end, BODY.length);
  assert.equal(main.length + cuts.reduce((n, c) => n + c.end - c.start, 0), BODY.length, "nothing is lost or kept twice");
  // the space LaTeX allows after \begin and \end moves neither bound
  const spaced = BODY.replace("\\begin{thebibliography}", "\\begin {thebibliography}").replace("\\end{thebibliography}", "\\end {thebibliography}");
  const both = splitPaper(spaced, "latex").cuts;
  assert.deepEqual(both.map((c) => c.part), ["references", "appendix"]);
  assert.equal(spaced.slice(both[0]!.start, both[0]!.end).trim(), spaced.slice(spaced.indexOf("\\begin {thebibliography}"), spaced.indexOf("\\appendix")).trim());
});

test("a paper with no appendix loses only its bibliography, and one with neither is all main text", () => {
  const { main, cuts } = splitPaper(NO_APPENDIX, "latex");
  assert.deepEqual(cuts.map((c) => c.part), ["references"]);
  assert.ok(main.includes("\\section{Results}") && main.endsWith("\\end{document}\n"), "what follows the bibliography stays");
  assert.ok(!main.includes("Turpin"));
  const bare = "\\documentclass{article}\\begin{document}\\section{Method}We probe.\\end{document}";
  assert.deepEqual(splitPaper(bare, "latex"), { main: bare, cuts: [] });
});

test("an appendix that is commented out, or only mentioned, is not one", () => {
  const body = "\\begin{document}\n\\section{Method}\nSee the appendix.\n% \\appendix\n\\section{Results}\nDone.\n\\end{document}";
  assert.deepEqual(splitPaper(body, "latex").cuts, []);
  const named = "\\begin{document}\n\\section{Method}\nWe probe.\n\\section{Appendix: proofs}\nLemma.\n\\end{document}";
  assert.deepEqual(splitPaper(named, "latex").cuts.map((c) => c.part), ["appendix"], "a section named Appendix starts it");
  const ieee = "\\begin{document}\n\\section{Method}\nWe probe.\n\\appendices\n\\section{Proofs}\nLemma.\n\\end{document}";
  assert.deepEqual(splitPaper(ieee, "latex").cuts.map((c) => c.part), ["appendix"], "IEEEtran's \\appendices starts it");
});

test("a PDF's text is cut at its References and Appendix headings", () => {
  const md = "# Probes\n\n## 1 Method\n\nWe probe.\n\n## References\n\n- [1] Turpin et al.\n\n## Appendix A\n\nDetails.\n\n### A.1 More\n\nMore details.\n";
  const { main, cuts } = splitPaper(md, "pdf");
  assert.deepEqual(cuts.map((c) => c.part), ["references", "appendix"]);
  assert.equal(main, "# Probes\n\n## 1 Method\n\nWe probe.\n\n");
  // references last, with no appendix; and an appendix before the references
  const last = "# Probes\n\n## Method\n\nWe probe.\n\n## References\n\n- [1] Turpin et al.\n";
  assert.deepEqual(splitPaper(last, "pdf"), { main: "# Probes\n\n## Method\n\nWe probe.\n\n", cuts: [{ part: "references", start: last.indexOf("## References"), end: last.length }] });
  const swapped = "# Probes\n\n## Method\n\nWe probe.\n\n## Appendix\n\nDetails.\n\n## References\n\n- [1] Turpin et al.\n";
  assert.deepEqual(splitPaper(swapped, "pdf").cuts.map((c) => c.part), ["appendix"], "a bibliography inside the appendix goes with it");
  // an appendix a level below the references, with no heading after it at theirs, ends the bibliography
  const below = "# Probes\n\n## Method\n\nWe probe.\n\n## References\n\n- [1] Turpin et al.\n\n### Appendix A\n\nDetails.\n";
  const appendix = below.indexOf("### Appendix A");
  assert.deepEqual(splitPaper(below, "pdf").cuts, [
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
  const { main, cuts } = splitPaper(MARKDOWN, "pdf");
  assert.deepEqual(cuts.map((c) => [c.part, MARKDOWN.slice(c.start, c.start + 22)]), [["references", "## **References** \n\n- "], ["appendix", "## **A Additional Samp"]]);
  assert.equal(main, MARKDOWN.slice(0, MARKDOWN.indexOf("## **References**")), "the main text ends where the references start");
  assert.equal(cuts[1]!.end, MARKDOWN.length);
  assert.deepEqual(headings(MARKDOWN, "pdf").map((h) => h.title), ["Probes read the trace", "1 Method", "References", "A Additional Samples", "A.1 BBH"], "a title is given without its emphasis");
  // named, with the references after it or before it
  const named = MARKDOWN.replace("## **A Additional Samples** ", "## **Appendix A: Additional Samples** ");
  assert.deepEqual(splitPaper(named, "pdf").cuts.map((c) => c.part), ["references", "appendix"]);
  const first = "# **Probes** \n\n## **1 Method** \n\nWe probe.\n\n## **Appendix** \n\nDetails.\n\n## **References** \n\n- Turpin et al.\n";
  assert.deepEqual(splitPaper(first, "pdf").cuts.map((c) => [c.part, c.start]), [["appendix", first.indexOf("## **Appendix**")]]);
  // what follows the references under a heading that is no appendix's stays main text
  const methods = MARKDOWN.replace("## **A Additional Samples** ", "## **Methods** ").replace("### **A.1 BBH** ", "### **Probes** ");
  const kept = splitPaper(methods, "pdf");
  assert.deepEqual(kept.cuts.map((c) => c.part), ["references"]);
  assert.ok(kept.main.includes("## **Methods**") && !kept.main.includes("Turpin"));
});

test("a PDF's plain text is cut at its References line, and at the appendix's: lettered after it, or named anywhere", () => {
  const { main, cuts } = splitPaper(PLAIN, "pdf");
  const references = PLAIN.lastIndexOf("References");
  // the last line that reads References is the heading, not the table of contents' entry, and the contents'
  // Appendix line, before the Introduction, is none either; a line of prose that starts "Appendix A" is no
  // heading, nor is a line of the bibliography that starts with an initial
  assert.deepEqual(cuts, [
    { part: "references", start: references, end: PLAIN.indexOf("A Additional Samples") },
    { part: "appendix", start: PLAIN.indexOf("A Additional Samples"), end: PLAIN.length },
  ]);
  assert.equal(main, PLAIN.slice(0, references));
  assert.deepEqual(headings(PLAIN, "pdf").map((h) => h.title), ["References", "A Additional Samples"], "the two headings a plain text can be told to have");
  // an appendix that says so
  for (const line of ["Appendix", "Appendix A", "Appendix A: Additional Samples", "APPENDIX", "Supplementary Material"]) {
    const named = PLAIN.replace("A Additional Samples", line);
    assert.deepEqual(splitPaper(named, "pdf").cuts.map((c) => [c.part, c.start]), [["references", references], ["appendix", named.lastIndexOf(line)]], line);
  }
  // no appendix: the references run to the end
  const none = PLAIN.slice(0, PLAIN.indexOf("15\n"));
  assert.deepEqual(splitPaper(none, "pdf").cuts, [{ part: "references", start: references, end: none.length }]);

  // an appendix that says so needs no References before it: it may stand ahead of them, or the paper have none
  for (const line of ["Appendix", "Appendix A", "Appendix A: Training Details"]) {
    const ahead = ["Probes read the trace", "1 Introduction", "We probe 3 models.", line, "Probes train for 5 epochs.", "References", "A. Vaswani, N. Shazeer. Attention is all you need.", ""].join("\n");
    const start = ahead.indexOf(`${line}\n`);
    assert.deepEqual(splitPaper(ahead, "pdf"), { main: ahead.slice(0, start), cuts: [{ part: "appendix", start, end: ahead.length }] }, `${line}, ahead of the references`);
    const alone = ahead.slice(0, ahead.indexOf("References"));
    assert.deepEqual(splitPaper(alone, "pdf"), { main: alone.slice(0, start), cuts: [{ part: "appendix", start, end: alone.length }] }, `${line}, with no references`);
    assert.deepEqual(headings(alone, "pdf").map((h) => h.title), [line]);
  }
  // a lettered line reads as a heading only after the references
  const lettered = ["Probes read the trace", "1 Introduction", "We probe 3 models.", "A Additional Samples", "See Table 6.", ""].join("\n");
  assert.deepEqual(splitPaper(lettered, "pdf"), { main: lettered, cuts: [] });
});

test("a LaTeX source's headings carry its numbers: sections counted, lettered from the appendix on, starred ones bare", () => {
  const nested = BODY.replace("\\section{Results}", "\\subsection{Probe training}\nIt trains.\n\\subsection*{Aside}\nAn aside.\n\\subsection{Data}\nTraces.\n\\section{Results}");
  assert.deepEqual(
    headings(nested, "latex").map((h) => `${h.number ?? "-"} ${h.title}`),
    ["- Abstract", "1 Introduction", "2 Method", "2.1 Probe training", "- Aside", "2.2 Data", "3 Results", "- References", "A Probe training details"],
  );
  // a source that numbers its sections its own way is not guessed at
  const own = BODY.replace("\\appendix", "\\setcounter{section}{0}\\renewcommand{\\thesection}{S\\arabic{section}}");
  assert.ok(headings(own, "latex").every((h) => h.number === undefined));
  // at any level, and by a class: one number set by the source leaves none of them LaTeX's to count
  for (const custom of [
    "\\setcounter{subsection}{3}",
    "\\addtocounter{section}{1}",
    "\\renewcommand\\thesubsection{\\thesection.\\alph{subsection}}",
    "\\numberwithin{subsection}{section}",
    "\\setcounter{secnumdepth}{1}",
    "\\usepackage{chngcntr}",
  ])
    assert.ok(headings(nested.replace("\\subsection{Probe training}", `${custom}\n\\subsection{Probe training}`), "latex").every((h) => h.number === undefined), custom);
  for (const cls of ["\\documentclass[journal]{IEEEtran}", "\\documentclass[aps,prl]{revtex4-2}", "\\documentclass{report}"])
    assert.ok(headings(nested.replace("\\documentclass{article}", cls), "latex").every((h) => h.number === undefined), cls);
  // past the alphabet's 26, a section is still counted; only an appendix's letter runs out
  const many = BODY.replace("\\section{Results}", `${Array.from({ length: 25 }, (_, i) => `\\section{Part ${i}}\nText.\n`).join("")}\\section{Results}`);
  assert.equal(headings(many, "latex").find((h) => h.title === "Results")!.number, "28");
  // a counter that numbers no section, and a setting in a comment, change nothing
  for (const other of ["\\setcounter{figure}{3}", "% \\setcounter{subsection}{3}"])
    assert.equal(headings(nested.replace("\\subsection{Probe training}", `${other}\n\\subsection{Probe training}`), "latex").find((h) => h.title === "Data")!.number, "2.2", other);
});

test("the outline gives each heading's number and says which were left out of the message", () => {
  const { cuts } = splitPaper(BODY, "latex");
  assert.equal(
    outline(headings(BODY, "latex").slice(3), cuts).replace(/\d+ characters/g, "n characters"),
    "- 3 Results (n characters)\n- References (n characters, bibliography: in the session only)\n- A Probe training details (n characters, appendix: in the session only)",
  );
});
