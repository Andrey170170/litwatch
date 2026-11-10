import assert from "node:assert/strict";
import { test } from "node:test";
import { gzipSync } from "node:zlib";
import { figureFiles, untar } from "../src/eprint.ts";
import { eprint, pdf, png, tar } from "./support.ts";

const image = png(4, 4);

test("an e-print's figure files are its regular files of a figure's kind, by path", () => {
  const files = figureFiles(
    eprint([
      { path: "main.tex", data: Buffer.from("\\documentclass{article}") },
      { path: "figs/", type: "5" },
      { path: "figs/probe.png", data: image },
      { path: "./figs/Arch.PDF", data: pdf() },
      { path: "plot.eps", data: Buffer.from("%!PS") },
      { path: "refs.bib", data: Buffer.from("@article{}") },
    ]),
  );
  assert.deepEqual(files.map((f) => f.path), ["figs/Arch.PDF", "figs/probe.png", "plot.eps"]);
  assert.deepEqual(files[1]!.data, image, "the file's own bytes");
});

test("nothing in the archive leaves it: links, devices and paths out of it are skipped", () => {
  const files = figureFiles(
    eprint([
      { path: "link.png", type: "2" },
      { path: "hard.png", type: "1" },
      { path: "../escape.png", data: image },
      { path: "/etc/absolute.png", data: image },
      { path: "a/../../b.png", data: image },
      { path: "kept.png", data: image },
    ]),
  );
  assert.deepEqual(files.map((f) => f.path), ["kept.png"]);
});

test("long names are read in ustar's, GNU's and pax's forms", () => {
  const long = `${"deep/".repeat(30)}figure.png`;
  const files = untar(
    tar([
      { path: "figure.png", prefix: "figs/deep", data: image },
      { path: "././@LongLink", type: "L", data: Buffer.from(`${long}\0`) },
      { path: "truncated", data: image },
      { path: "PaxHeader", type: "x", data: Buffer.from("21 path=pax/name.png\n") },
      { path: "short", data: image },
      { path: "figs/gnu.png", gnuTimes: true, data: image },
    ]),
    () => true,
  );
  assert.deepEqual(files.map((f) => f.path), ["figs/deep/figure.png", long, "pax/name.png", "figs/gnu.png"], "an old GNU header's times are no prefix");
});

test("an e-print that is one TeX file or a PDF has no figure files, and a broken one says so", () => {
  assert.deepEqual(figureFiles(gzipSync(Buffer.from("\\documentclass{article}\\begin{document}Hi\\end{document}"))), []);
  assert.deepEqual(figureFiles(pdf()), []);
  assert.deepEqual(figureFiles(tar([{ path: "a.png", data: image }])).map((f) => f.path), ["a.png"], "a tar that is not gzipped");
  const whole = tar([{ path: "a.png", data: png(64, 64) }]);
  assert.throws(() => figureFiles(gzipSync(whole.subarray(0, 600))), /cut short/);
  assert.throws(() => figureFiles(Buffer.from([0x1f, 0x8b, 1, 2, 3])), /could not be unpacked/);
});
