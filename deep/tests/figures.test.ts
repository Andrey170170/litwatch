import assert from "node:assert/strict";
import { test } from "node:test";
import { Figures, MAX_FIGURE_BYTES } from "../src/figures.ts";
import { pacer } from "../src/hosts.ts";
import type { Shot } from "../src/imaging.ts";
import { fakeImaging, HOSTS, OPEN, png, WEBP, withSite } from "./support.ts";

/** A run's figures against the site, with the images it showed the model. */
function session(url: string, body_version: number | null | undefined) {
  const { imaging, calls } = fakeImaging();
  const shown: Shot[] = [];
  const figures = new Figures({ id: "2610.00001", body_version }, { ...HOSTS, litwatchUrl: url, arxivUrl: url, litwatchToken: "secret", guard: OPEN }, imaging, (s) => shown.push(s), pacer());
  return { figures, hosts: figures.hosts(), calls, shown };
}

test("the figures come from the e-print of the version litwatch read, fetched once", async () => {
  await withSite(async ({ url, asked }) => {
    const { figures, hosts } = session(url, 2);
    try {
      const listed = (await hosts.figures!([])) as { file: string; kind: string; bytes: number }[];
      assert.deepEqual(listed.map((f) => [f.file, f.kind]), [["a/setup.png", "png"], ["b/setup.png", "png"], ["figs/arch.pdf", "pdf"], ["figs/old.eps", "eps"], ["figs/probe.png", "png"]]);
      await hosts.figure!(["figs/probe.png"]);
      assert.deepEqual(asked, ["/e-print/2610.00001v2"], "the version's e-print, once for the run");
      assert.equal(figures.usedLatest, false);
    } finally {
      await figures.dispose();
    }
  });
});

test("with no version from litwatch the figures come from the latest e-print, and the run says so", async () => {
  await withSite(async ({ url, asked }) => {
    for (const version of [null, undefined]) {
      const { figures, hosts } = session(url, version);
      assert.equal(figures.usedLatest, false, "nothing was taken yet");
      await hosts.figures!([]);
      assert.equal(asked.at(-1), "/e-print/2610.00001");
      assert.equal(figures.usedLatest, true);
      await figures.dispose();
    }
    // the PDF litwatch holds, when it can't say the text's version, is no more the text's
    const { figures, hosts } = session(url, null);
    try {
      await hosts.pdfPage!([1]);
      assert.equal(asked.at(-1), "/api/papers/2610.00001/pdf Bearer secret");
      assert.equal(figures.usedLatest, true);
    } finally {
      await figures.dispose();
    }
  });
});

test("figure() keeps a file of the e-print as a figure and shows it", async () => {
  await withSite(async ({ url }) => {
    const { figures, hosts, calls, shown } = session(url, 2);
    try {
      // as \includegraphics names it: no extension
      assert.deepEqual(await hosts.figure!(["figs/probe"]), { name: "probe", file: "figs/probe.png", width: 800, height: 400 });
      assert.deepEqual(figures.made.get("probe"), { webp: WEBP, view: shown[0] }, "the WebP litwatch stores, with the view the model was shown");
      assert.equal(shown.length, 1, "the model is shown what was kept");
      assert.equal(shown[0]!.mimeType, "image/jpeg");
      assert.deepEqual(calls.pictures.at(-1), { mime: "image/png", crop: undefined, bytes: png(8, 8).length });

      // a PDF figure goes through poppler first
      assert.deepEqual(await hosts.figure!(["arch.pdf", "fig-2"]), { name: "fig-2", file: "figs/arch.pdf", width: 800, height: 400 });
      assert.deepEqual(calls.pages.at(-1), { page: 1, width: 1600 });
      assert.deepEqual([...figures.made.keys()], ["probe", "fig-2"]);
      assert.equal(shown.length, 2);

      await assert.rejects(hosts.figure!(["setup.png"]), /names 2 files \(a\/setup\.png, b\/setup\.png\); give the whole path/);
      await assert.rejects(hosts.figure!(["figs/old.eps"]), /EPS, which is not converted here: cut the figure from the PDF/);
      await assert.rejects(hosts.figure!(["nope.png"]), /no figure file "nope\.png"; figures\(\) lists them/);
      await assert.rejects(hosts.figure!(["figs/probe.png", "../probe"]), /a figure's name is letters, digits/);
      assert.equal(shown.length, 2, "a refused figure shows nothing");
    } finally {
      await figures.dispose();
    }
  });
});

test("pdfPage() shows a page of litwatch's PDF and crop() keeps a part of one as a figure", async () => {
  await withSite(async ({ url, asked }) => {
    const { figures, hosts, calls, shown } = session(url, 2);
    try {
      assert.deepEqual(await hosts.pdfPage!([2]), { page: 2, pages: 3, width: 800, height: 400 });
      assert.deepEqual(calls.pages.at(-1), { page: 2, width: 1100 });
      assert.equal(figures.made.size, 0, "looking at a page keeps nothing");
      assert.equal(shown.length, 1);

      assert.deepEqual(await hosts.crop!([3, [0.1, 0.2, 0.9, 0.5], "fig-3"]), { name: "fig-3", page: 3, width: 800, height: 400 });
      assert.deepEqual(calls.pages.at(-1), { page: 3, width: 2200 }, "cut from a page drawn at twice the view's width");
      assert.deepEqual(calls.pictures.at(-1)!.crop, [0.1, 0.2, 0.9, 0.5]);
      assert.deepEqual(figures.made.get("fig-3"), { webp: WEBP, view: shown[1] });
      assert.equal(shown.length, 2);
      assert.deepEqual(asked, ["/api/papers/2610.00001/pdf?version=2 Bearer secret"], "the PDF is litwatch's, of the text's version, fetched once, and no e-print was needed");

      await assert.rejects(hosts.pdfPage!([4]), /the PDF has pages 1 to 3; got 4/);
      await assert.rejects(hosts.crop!([1, [0.5, 0.2, 0.4, 0.9], "x"]), /left < right and top < bottom/);
      await assert.rejects(hosts.crop!([1, [0, 0, 1, 1.2], "x"]), /four fractions of the page/);
      await assert.rejects(hosts.crop!([1, [0, 0, 1, 1]]), /give the name the read cites the figure by/);
    } finally {
      await figures.dispose();
    }
  });
});

test("an e-print arXiv does not give fails the call, and the next call asks again", async () => {
  await withSite(async ({ url, asked }) => {
    const { figures, hosts } = session(url, 2);
    await assert.rejects(hosts.figures!([]), /arXiv answered 404 for the e-print/);
    await assert.rejects(hosts.figures!([]), /arXiv answered 404/);
    assert.equal(asked.length, 2);
    assert.equal(figures.usedLatest, false);
    await figures.dispose();
  }, { eprintStatus: 404 });
});

test("a PDF litwatch does not hold is arXiv's at the version of litwatch's text, taken without the token", async () => {
  await withSite(async ({ url, asked }) => {
    // the text is of version 2, and arXiv's latest is version 5, which litwatch's redirect points at
    const { figures, hosts } = session(url, 2);
    try {
      assert.deepEqual(await hosts.pdfPage!([1]), { page: 1, pages: 2, width: 800, height: 400 }, "version 2's two pages, not the latest's five");
      assert.deepEqual(asked, ["/api/papers/2610.00001/pdf?version=2 Bearer secret", "/pdf/2610.00001v2"], "the token goes to litwatch only");
      assert.equal(figures.usedLatest, false);
      await assert.rejects(hosts.crop!([3, [0, 0, 1, 1], "fig-9"]), /the PDF has pages 1 to 2; got 3/, "a page only the latest version has is not there to crop");
    } finally {
      await figures.dispose();
    }
  }, { archived: false });
});

test("with no version from litwatch the PDF arXiv is asked for is its latest, and the run says so", async () => {
  await withSite(async ({ url, asked }) => {
    const { figures, hosts } = session(url, null);
    try {
      assert.deepEqual(await hosts.pdfPage!([5]), { page: 5, pages: 5, width: 800, height: 400 });
      assert.deepEqual(asked, ["/api/papers/2610.00001/pdf Bearer secret", "/pdf/2610.00001"], "no version to ask litwatch or arXiv for");
      assert.equal(figures.usedLatest, true);
    } finally {
      await figures.dispose();
    }
  }, { archived: false });
});

test("a figure is kept as one record: its image, the view the model saw, and the paper's label it was made as", async () => {
  await withSite(async ({ url }) => {
    const { figures, hosts } = session(url, 2);
    // the first figure asked for is the last made, as two figure() calls in one cell may finish
    const picture = figures["imaging"].picture;
    figures["imaging"].picture = async (data, ...rest) => {
      if (data.length === png(8, 8).length) await new Promise((r) => setTimeout(r, 50));
      return picture(data, ...rest);
    };
    const view = (bytes: number) => Buffer.from(`view of ${bytes} bytes`).toString("base64");
    try {
      const [slow, fast] = await Promise.all([hosts.figure!(["figs/probe.png", "probe", "Figure 1"]), hosts.figure!(["a/setup.png", "setup", "Fig. A.1b"])]);
      assert.deepEqual([slow, fast].map((f) => (f as { number: string }).number), ["Figure 1", "Figure A.1"]);
      assert.deepEqual([...figures.made].map(([name, f]) => [name, f.label, f.view.data]), [["setup", "A.1", view(png(4, 4).length)], ["probe", "1", view(png(8, 8).length)]], "each name with its own view, whichever finished first");
      await hosts.crop!([1, [0, 0, 1, 0.5], "fig-s2", "S2"]);
      assert.equal(figures.made.get("fig-s2")!.label, "S2");
      await assert.rejects(hosts.figure!(["figs/probe.png", "probe", "the main result"]), /a figure's number is the paper's own for it, such as "Figure 6"/);
    } finally {
      await figures.dispose();
    }
  });
});

test("the figures a run makes stay within what litwatch takes of a job", async () => {
  await withSite(async ({ url }) => {
    const { figures, hosts, calls } = session(url, 2);
    const heavy = Buffer.alloc(MAX_FIGURE_BYTES / 2 + 1);
    const picture = figures["imaging"].picture;
    figures["imaging"].picture = async (...args) => ({ ...(await picture(...args)), webp: heavy });
    try {
      await hosts.figure!(["figs/probe.png", "one"]);
      await assert.rejects(hosts.figure!(["a/setup.png", "two"]), /the run's figures would weigh more than 24 MiB together/);
      assert.deepEqual([...figures.made.keys()], ["one"]);
      await hosts.figure!(["a/setup.png", "one"]);
      assert.equal(calls.pictures.length, 3, "a name made again replaces its figure");
    } finally {
      await figures.dispose();
    }
  });
});
