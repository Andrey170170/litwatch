import assert from "node:assert/strict";
import { test } from "node:test";
import { measure } from "../src/length.ts";

test("a read's length counts what a reader reads, against the main text from its first heading", () => {
  const body = String.raw`\documentclass{article}
\newcommand{\probe}{a long macro definition that no read retells at all}
\begin{document}
\section{Method}
${"x".repeat(100)}
\begin{thebibliography}{9}
\bibitem{a} ${"y".repeat(500)}
\end{thebibliography}
\appendix
\section{More}
${"z".repeat(500)}
\end{document}
`;
  const read = { format: "read", sections: [{ id: "method", kind: "method", title: "Method", blocks: [{ type: "p", text: "abcdefghij", where: "§1" }] }] };
  const m = measure(read, { body, body_source: "latex" });
  assert.equal(m.read, "Method".length + "abcdefghij".length, "ids, kinds, places and the format are no text");
  // the Method section alone, give or take the line break at a cut
  const method = body.slice(body.indexOf("\\section{Method}"), body.indexOf("\\begin{thebibliography}")).length;
  assert.ok(Math.abs(m.main - method) <= 1, `no preamble, bibliography or appendix: ${m.main} against ${method}`);
  assert.equal(m.share, m.read / m.main);
});
