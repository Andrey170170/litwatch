/**
 * What makes the run's images (docs/plans/deep-read.md, "The run"): a headless Chromium that draws a draft on
 * litwatch's render page and encodes pictures on a canvas, and poppler, which turns a PDF's page into an image.
 *
 * The browser has no business on the network beyond the render page: every request to another origin is refused,
 * and so is every redirect, which would lead to one; names other than the render page's host do not resolve, and
 * service workers and WebSockets are off. The page it opens is litwatch's own, and what it draws is the model's
 * text, which the page never interprets as HTML.
 */

import { execFile } from "node:child_process";
import { chromium, type Browser, type BrowserContext } from "playwright-core";
import type { Read } from "./read.ts";

/** An image as the model is shown it. */
export interface Shot {
  data: string; // base64
  mimeType: string;
  width: number;
  height: number;
}

/** A picture made for the read: the WebP stored with it, and the view the model is shown. */
export interface Picture {
  webp: Buffer;
  width: number;
  height: number;
  view: Shot;
}

/** A part of an image: [left, top, right, bottom], each a fraction of its width or height. */
export type Box = [number, number, number, number];
export type RenderWidth = "desktop" | "phone";
/** What a render shows: the whole read, one section, or one block of a section (counted from 0). */
export interface RenderTarget {
  section?: string;
  block?: number;
}

export interface Rendered {
  shots: Shot[];
  /** The target's height on the page, in CSS pixels. */
  height: number;
  /** True when the target ran past what one render shows, and only its top was returned. */
  cut: boolean;
}

/** An image that could not be made because the browser or the render page was not there, not because of the
 * draft or the picture. */
export class RenderUnavailable extends Error {}

export interface Imaging {
  /** A raster image (PNG, JPEG, GIF, WebP), whole or cut to `crop`, as a picture for the read. */
  picture(data: Buffer, mime: string, crop?: Box): Promise<Picture>;
  /** How many pages a PDF file has. */
  pdfPages(path: string): Promise<number>;
  /** One page of a PDF file as a PNG `width` pixels wide. */
  pdfPage(path: string, page: number, width: number): Promise<Buffer>;
  /** A read drawn by the web app's own components. `figures` maps a figure's name to its data URL. */
  render(read: Read, figures: Record<string, string>, target: RenderTarget, width: RenderWidth): Promise<Rendered>;
  close(): Promise<void>;
}

/** A stored figure is at most this wide (docs/integrations.md), and no taller than a WebP may be. */
export const MAX_FIGURE_WIDTH = 1600;
const MAX_FIGURE_HEIGHT = 12_000;
/** The view the model gets of a picture. */
const VIEW = { width: 1200, height: 1600 };
/** A render comes back in tiles this tall (CSS pixels), at most this many: a longer target is cut. */
const TILE = { desktop: 1200, phone: 900 };
export const MAX_TILES = 8;
const VIEWPORT = { desktop: { width: 1280, height: 900 }, phone: { width: 390, height: 844 } };
const NAVIGATION_MS = 30_000;
const POPPLER_MS = 60_000;
/** What one picture or one render may take in the browser, all told; its context is closed past it. */
const PICTURE_MS = 60_000;
const RENDER_MS = 180_000;

const base64 = (dataUrl: string, mime: string): string => {
  const prefix = `data:${mime};base64,`;
  if (!dataUrl.startsWith(prefix)) throw new Error(`the browser could not encode ${mime}`);
  return dataUrl.slice(prefix.length);
};

function run(file: string, args: string[]): Promise<Buffer> {
  return new Promise((resolve, reject) => {
    execFile(file, args, { encoding: "buffer", maxBuffer: 256 * 1024 * 1024, timeout: POPPLER_MS }, (error, stdout, stderr) => {
      if (!error) return resolve(stdout);
      const missing = (error as NodeJS.ErrnoException).code === "ENOENT";
      reject(new Error(missing ? `${file} is not installed (poppler-utils)` : `${file} failed: ${stderr.toString("utf8").trim().slice(0, 300) || error.message}`));
    });
  });
}

/** The service's Chromium and poppler. The browser starts with the first picture or render and is kept; a browser
 * that died is started again. */
export class Studio implements Imaging {
  private browser: Promise<Browser> | undefined;
  private readonly renderUrl: string;
  private readonly origin: string;

  /** `litwatchUrl` is where the render page is served (`<litwatchUrl>/render.html`). */
  constructor(litwatchUrl: string) {
    const base = new URL(litwatchUrl);
    this.origin = base.origin;
    this.renderUrl = new URL("render.html", base.href.endsWith("/") ? base.href : `${base.href}/`).href;
  }

  /** The browser, started where none is running. Every call waits on the one before it, and its promise is kept
   * before anything is awaited, so calls that arrive together share one start: the first call's, and the restart
   * of a browser that died. */
  private launch(): Promise<Browser> {
    const before = this.browser;
    const browser = (async () => {
      const running = await before?.catch(() => undefined);
      if (running?.isConnected()) return running;
      return chromium.launch({
        headless: true,
        args: [
          // names resolve for the render page's host only
          `--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE ${new URL(this.origin).hostname}`,
          // a container's /dev/shm is too small for a long page
          "--disable-dev-shm-usage",
        ],
      });
    })();
    this.browser = browser;
    return browser;
  }

  /** A context whose requests go to `allowed` only, or nowhere, and which is closed after `limitMs` whatever the
   * page is doing: nothing a page runs is waited on for longer. `done` ends it. */
  private async context(options: Parameters<Browser["newContext"]>[0], limitMs: number, allowed?: string): Promise<{ context: BrowserContext; done: () => Promise<void> }> {
    const context = await this.launch()
      .then((browser) => browser.newContext({ ...options, serviceWorkers: "block" }))
      .catch((e: unknown) => {
        throw new RenderUnavailable(`the browser could not start: ${e instanceof Error ? e.message.split("\n")[0] : String(e)}`);
      });
    await context.route("**/*", async (route) => {
      let origin: string | undefined;
      try {
        origin = new URL(route.request().url()).origin;
      } catch {
        // not a URL the page could have a business with
      }
      if (allowed === undefined || origin !== allowed) return route.abort("blockedbyclient");
      // The request is made here and its answer handed to the page. Left to the browser, a redirect would be
      // followed without this handler being asked again, so a 307 from the allowed origin would lead anywhere.
      // The render page and its files are served, never redirected to, so a redirect is refused whole.
      try {
        const answer = await route.fetch({ maxRedirects: 0 });
        const redirect = answer.status() >= 300 && answer.status() < 400 && answer.status() !== 304;
        await (redirect ? route.abort("blockedbyclient") : route.fulfill({ response: answer }));
      } catch {
        // the server is not there, or the context closed under the request
        await route.abort("failed").catch(() => undefined);
      }
    });
    await context.routeWebSocket("**", (ws) => void ws.close());
    const limit = setTimeout(() => void context.close().catch(() => undefined), limitMs);
    return {
      context,
      done: async () => {
        clearTimeout(limit);
        await context.close().catch(() => undefined);
      },
    };
  }

  async picture(data: Buffer, mime: string, crop?: Box): Promise<Picture> {
    const { context, done } = await this.context({}, PICTURE_MS);
    try {
      const page = await context.newPage();
      const made = await page.evaluate(
        async ({ url, crop, stored, view }) => {
          const img = new Image();
          img.src = url;
          await img.decode();
          const [x0, y0, x1, y1] = crop ?? [0, 0, 1, 1];
          const sx = Math.round(x0 * img.naturalWidth);
          const sy = Math.round(y0 * img.naturalHeight);
          const sw = Math.max(1, Math.round((x1 - x0) * img.naturalWidth));
          const sh = Math.max(1, Math.round((y1 - y0) * img.naturalHeight));
          const draw = (max: { width: number; height: number }, type: string, quality: number) => {
            const k = Math.min(1, max.width / sw, max.height / sh);
            const canvas = document.createElement("canvas");
            canvas.width = Math.max(1, Math.round(sw * k));
            canvas.height = Math.max(1, Math.round(sh * k));
            const g = canvas.getContext("2d")!;
            // on white: a transparent figure is unreadable on the dark theme
            g.fillStyle = "#fff";
            g.fillRect(0, 0, canvas.width, canvas.height);
            g.imageSmoothingQuality = "high";
            g.drawImage(img, sx, sy, sw, sh, 0, 0, canvas.width, canvas.height);
            return { url: canvas.toDataURL(type, quality), width: canvas.width, height: canvas.height };
          };
          return { stored: draw(stored, "image/webp", 0.85), view: draw(view, "image/jpeg", 0.8) };
        },
        { url: `data:${mime};base64,${data.toString("base64")}`, crop: crop ?? null, stored: { width: MAX_FIGURE_WIDTH, height: MAX_FIGURE_HEIGHT }, view: VIEW },
      );
      return {
        webp: Buffer.from(base64(made.stored.url, "image/webp"), "base64"),
        width: made.stored.width,
        height: made.stored.height,
        view: { data: base64(made.view.url, "image/jpeg"), mimeType: "image/jpeg", width: made.view.width, height: made.view.height },
      };
    } catch (e) {
      throw new Error(`the image could not be read: ${e instanceof Error ? e.message.split("\n")[0] : String(e)}`);
    } finally {
      await done();
    }
  }

  async pdfPages(path: string): Promise<number> {
    const pages = /^Pages:\s+(\d+)/m.exec((await run("pdfinfo", [path])).toString("utf8"));
    if (!pages) throw new Error("pdfinfo did not say how many pages the PDF has");
    return Number(pages[1]);
  }

  async pdfPage(path: string, page: number, width: number): Promise<Buffer> {
    const n = String(page);
    return run("pdftoppm", ["-png", "-f", n, "-l", n, "-singlefile", "-scale-to-x", String(width), "-scale-to-y", "-1", path]);
  }

  async render(read: Read, figures: Record<string, string>, target: RenderTarget, width: RenderWidth): Promise<Rendered> {
    const reason = (e: unknown) => (e instanceof Error ? e.message.split("\n")[0] : String(e));
    const { context, done } = await this.context({ viewport: VIEWPORT[width], deviceScaleFactor: width === "phone" ? 2 : 1, colorScheme: "light" }, RENDER_MS, this.origin);
    try {
      const page = await context.newPage();
      const answer = await page.goto(this.renderUrl, { waitUntil: "load", timeout: NAVIGATION_MS }).catch((e: unknown) => {
        throw new RenderUnavailable(`litwatch's render page could not be opened: ${reason(e)}`);
      });
      if (!answer?.ok()) throw new RenderUnavailable(`litwatch's render page answered ${answer?.status() ?? "nothing"}`);
      await page.waitForFunction(() => typeof (window as unknown as { renderRead?: unknown }).renderRead === "function", null, { timeout: NAVIGATION_MS }).catch((e: unknown) => {
        throw new RenderUnavailable(`litwatch's render page did not load: ${reason(e)}`);
      });
      await page
        .evaluate(([draft, options]) => (window as unknown as { renderRead: (r: unknown, o: unknown) => Promise<void> }).renderRead(draft, options), [read, { figures, theme: "light" }] as const)
        .catch((e: unknown) => {
          throw new Error(`the render page could not draw the draft: ${reason(e)}`);
        });
      const named = target.section === undefined ? "the read" : `${target.section}${target.block === undefined ? "" : `.${target.block}`}`;
      const selector = target.section === undefined ? ".dread" : target.block === undefined ? `[data-section="${target.section}"]` : `[data-block="${target.section}.${target.block}"]`;
      const box = await page.locator(selector).first().boundingBox({ timeout: 5000 }).catch(() => null);
      if (!box || box.width < 1 || box.height < 1) throw new Error(`the page drew nothing for ${named}: a target is a section's id or "<section id>.<n>"`);
      const shots: Shot[] = [];
      const tile = TILE[width];
      const scale = width === "phone" ? 2 : 1;
      for (let top = 0; top < box.height && shots.length < MAX_TILES; top += tile) {
        const height = Math.min(tile, box.height - top);
        const jpeg = await page.screenshot({ type: "jpeg", quality: 80, fullPage: true, clip: { x: box.x, y: box.y + top, width: box.width, height } });
        shots.push({ data: jpeg.toString("base64"), mimeType: "image/jpeg", width: Math.round(box.width * scale), height: Math.round(height * scale) });
      }
      return { shots, height: Math.round(box.height), cut: box.height > tile * MAX_TILES };
    } finally {
      await done();
    }
  }

  async close(): Promise<void> {
    // forgotten before it is awaited: a call that arrives meanwhile starts a browser of its own
    const started = this.browser;
    this.browser = undefined;
    const browser = await started?.catch(() => undefined);
    await browser?.close().catch(() => undefined);
  }
}
