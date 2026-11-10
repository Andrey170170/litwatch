import assert from "node:assert/strict";
import { test } from "node:test";
import { outline, splitPaper } from "../src/paper.ts";
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
});

test("the outline says which headings were left out of the message", () => {
  const { cuts } = splitPaper(BODY, "latex");
  const at = (text: string) => BODY.indexOf(text);
  const text = outline(
    [
      { level: 1, title: "Results", start: at("\\section{Results}"), chars: 300 },
      { level: 1, title: "References", start: at("\\begin{thebibliography}"), chars: 120 },
      { level: 1, title: "Probe training details", start: at("\\section{Probe training"), chars: 90 },
    ],
    cuts,
  );
  assert.equal(text, "- Results (300 characters)\n- References (120 characters, bibliography: in the session only)\n- Probe training details (90 characters, appendix: in the session only)");
});
