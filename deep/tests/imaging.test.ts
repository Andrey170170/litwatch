/**
 * The real browser and poppler. These run where both are installed (the image, a developer's machine after
 * `npx playwright-core install chromium-headless-shell`) and skip where they are not.
 */

import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { createServer, type Server } from "node:http";
import type { AddressInfo } from "node:net";
import { tmpdir } from "node:os";
import path from "node:path";
import { after, test } from "node:test";
import { RenderUnavailable, Studio } from "../src/imaging.ts";
import { layout } from "../src/layout.ts";
import type { Read } from "../src/read.ts";
import { blocks, changed, draft, pdf, png } from "./support.ts";

const studio = new Studio("http://127.0.0.1:9");
const browser = await studio.picture(png(2, 2), "image/png").then(
  () => undefined,
  (e: Error) => `Chromium is not installed here (${e.message.slice(0, 80)})`,
);
const poppler = spawnSync("pdftoppm", ["-v"]).error ? "poppler-utils is not installed here" : undefined;
after(() => studio.close());

const listen = async (server: Server): Promise<string> => {
  await new Promise<void>((r) => server.listen(0, "127.0.0.1", r));
  return `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
};
const isWebp = (b: Buffer) => b.subarray(0, 4).toString() === "RIFF" && b.subarray(8, 12).toString() === "WEBP";
const isJpeg = (base64: string) => Buffer.from(base64, "base64").subarray(0, 2).equals(Buffer.from([0xff, 0xd8]));

test("a picture is stored as WebP, whole or cropped, no wider than 1600 px, with a JPEG for the model", { skip: browser }, async () => {
  const whole = await studio.picture(png(400, 200), "image/png");
  assert.ok(isWebp(whole.webp), "WebP bytes");
  assert.deepEqual([whole.width, whole.height], [400, 200]);
  assert.ok(isJpeg(whole.view.data) && whole.view.mimeType === "image/jpeg");
  const top = await studio.picture(png(400, 200), "image/png", [0.25, 0, 0.75, 0.2]);
  assert.deepEqual([top.width, top.height], [200, 40], "the box, as fractions of the image");
  const wide = await studio.picture(png(3200, 100), "image/png");
  assert.deepEqual([wide.width, wide.height], [1600, 50]);
  assert.ok(wide.view.width <= 1200, "the model's view is smaller still");
  await assert.rejects(studio.picture(Buffer.from("not an image"), "image/png"), /the image could not be read/);
});

test("poppler counts a PDF's pages and draws one at the width asked for", { skip: browser ?? poppler }, async () => {
  const dir = mkdtempSync(path.join(tmpdir(), "deep-test-"));
  try {
    const file = path.join(dir, "paper.pdf");
    writeFileSync(file, pdf(2));
    assert.equal(await studio.pdfPages(file), 2);
    const page = await studio.pdfPage(file, 2, 500);
    assert.deepEqual(page.subarray(0, 4), Buffer.from([0x89, 0x50, 0x4e, 0x47]), "a PNG");
    const picture = await studio.picture(page, "image/png");
    assert.equal(picture.width, 500);
    assert.ok(Math.abs(picture.height - 647) <= 1, `US letter at 500 px wide is 647 px tall, got ${picture.height}`);
    await assert.rejects(studio.pdfPage(path.join(dir, "missing.pdf"), 1, 500), /pdftoppm failed/);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

/** A render page of the test's own: it draws each section 100 px tall plus 50 px per block, 854 px wide, with the
 * web app's markers; and first tries to reach another origin, which makes the drawing 5000 px taller if it can. */
const page = (other: string) => `<!doctype html><meta charset="utf-8"><body style="margin:0"><script>
window.renderRead = async (read) => {
  const fetched = await fetch("${other}/leak").then(() => true, () => false);
  const named = await fetch("http://other.example/leak").then(() => true, () => false);
  const image = await new Promise((done) => { const i = new Image(); i.onload = () => done(true); i.onerror = () => done(false); i.src = "${other}/pixel.png"; });
  const root = document.createElement("div");
  root.className = "dread";
  root.style.cssText = "width: 854px; max-width: 100%; background: #fff";
  if (fetched || named || image) root.style.paddingTop = "5000px";
  for (const s of read.sections) {
    const section = document.createElement("section");
    section.dataset.section = s.id;
    section.style.paddingTop = "100px";
    (s.blocks || []).forEach((b, i) => {
      const block = document.createElement("div");
      block.dataset.block = s.id + "." + i;
      block.style.height = "50px";
      section.append(block);
    });
    root.append(section);
  }
  document.body.replaceChildren(root);
};
</script>`;

test("a render draws the target in tiles at either width, and the browser reaches nothing but the render page", { skip: browser }, async () => {
  let asked = 0;
  const other = createServer((_req, res) => {
    asked += 1;
    res.writeHead(200, { "content-type": "image/png", "access-control-allow-origin": "*" }).end(png(1, 1));
  });
  const otherUrl = await listen(other);
  const site = createServer((req, res) => (req.url === "/render.html" ? res.writeHead(200, { "content-type": "text/html" }).end(page(otherUrl)) : res.writeHead(404).end()));
  const local = new Studio(await listen(site));
  try {
    // glance, method (2 blocks), result (3), claims, limits, related, repro: 7 × 100 + 5 × 50
    const read = await layout(draft());
    const whole = await local.render(read, {}, {}, "desktop");
    assert.deepEqual({ shots: whole.shots.length, height: whole.height, cut: whole.cut }, { shots: 1, height: 950, cut: false }, "5000 px taller if the page reached another origin");
    assert.equal(asked, 0, "no request left the render page's origin");
    assert.ok(isJpeg(whole.shots[0]!.data));
    assert.deepEqual([whole.shots[0]!.width, whole.shots[0]!.height], [854, 950]);

    const section = await local.render(read, {}, { section: "result" }, "desktop");
    assert.deepEqual([section.shots.length, section.height], [1, 250]);
    const block = await local.render(read, {}, { section: "result", block: 1 }, "desktop");
    assert.deepEqual([block.shots[0]!.width, block.shots[0]!.height], [854, 50]);
    const phone = await local.render(read, {}, { section: "result" }, "phone");
    assert.deepEqual([phone.shots[0]!.width, phone.shots[0]!.height], [780, 500], "390 px wide, at two device pixels each");
    await assert.rejects(local.render(read, {}, { section: "nope" }, "desktop"), /the page drew nothing for nope/);

    // a long target comes back in 1200 px tiles, and is cut after eight
    const long = (n: number) => layout(changed((d) => blocks(d, "method").push(...Array.from({ length: n }, () => ({ type: "p", text: "The probe is linear." })))));
    const tiled = await local.render(await long(48), {}, { section: "method" }, "desktop");
    assert.deepEqual({ shots: tiled.shots.length, height: tiled.height, cut: tiled.cut }, { shots: 3, height: 2600, cut: false });
    assert.deepEqual(tiled.shots.map((s) => s.height), [1200, 1200, 200]);
    const cut = await local.render(await long(200), {}, { section: "method" }, "desktop");
    assert.deepEqual({ shots: cut.shots.length, cut: cut.cut }, { shots: 8, cut: true });
    assert.equal(asked, 0);
  } finally {
    await local.close();
    for (const server of [site, other]) {
      server.closeAllConnections();
      server.close();
    }
  }
});

test("a render page that is not there is the service's trouble, not the draft's", { skip: browser }, async () => {
  const read = await layout(draft());
  await assert.rejects(studio.render(read, {}, {}, "desktop"), (e) => e instanceof RenderUnavailable && /render page could not be opened/.test(e.message));
  const site = createServer((_req, res) => void res.writeHead(404).end());
  const missing = new Studio(await listen(site));
  try {
    await assert.rejects(missing.render(read, {}, {}, "desktop"), (e) => e instanceof RenderUnavailable && /render page answered 404/.test(e.message));
  } finally {
    await missing.close();
    site.close();
  }
});

// The web app's own render page, when this checkout has built it (`pnpm build` in web/).
const WEB = path.resolve(import.meta.dirname, "../../src/litwatch/api/web");
const built = existsSync(path.join(WEB, "render.html")) ? undefined : "the web app is not built in this checkout";
const TYPES: Record<string, string> = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".woff2": "font/woff2", ".svg": "image/svg+xml" };

test("the web app's render page draws a read with its figure, at the desktop's column and the phone's", { skip: browser ?? built }, async () => {
  const site = createServer((req, res) => {
    const file = path.join(WEB, path.normalize(new URL(req.url ?? "/", "http://local").pathname));
    if (!file.startsWith(WEB) || !existsSync(file)) return void res.writeHead(404).end();
    res.writeHead(200, { "content-type": TYPES[path.extname(file)] ?? "application/octet-stream" }).end(readFileSync(file));
  });
  const local = new Studio(await listen(site));
  try {
    const read = JSON.parse(readFileSync(path.resolve(import.meta.dirname, "../../web/src/deepread/fixtures/read-scarce.json"), "utf8")) as Read;
    const figure = read.sections.flatMap((s) => ("blocks" in s ? s.blocks.flatMap((b, i) => (b.type === "fig" ? [{ section: s.id, block: i, name: b.name }] : [])) : []))[0]!;
    const made = await studio.picture(png(900, 500), "image/png");
    const figures = { [figure.name]: `data:image/webp;base64,${made.webp.toString("base64")}` };
    const desktop = await local.render(read, figures, { section: figure.section, block: figure.block }, "desktop");
    assert.equal(desktop.shots.length, 1);
    assert.ok(desktop.shots[0]!.width <= 854 && desktop.shots[0]!.width >= 600, `the read's column, got ${desktop.shots[0]!.width}`);
    assert.ok(desktop.height > 200, `the figure is drawn, not its placeholder line: ${desktop.height} px`);
    const phone = await local.render(read, figures, { section: figure.section }, "phone");
    assert.ok(phone.shots[0]!.width <= 780, `390 px at two device pixels each, got ${phone.shots[0]!.width}`);
    const whole = await local.render(read, figures, {}, "desktop");
    assert.ok(whole.shots.length > 1 && whole.height > 1200, "a whole read is several tiles");
  } finally {
    await local.close();
    site.closeAllConnections();
    site.close();
  }
});
