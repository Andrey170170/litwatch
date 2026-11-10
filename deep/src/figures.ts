/**
 * The paper's figures in one run's session (docs/plans/deep-read.md, D2): `figures()` and `figure()` take them
 * from the paper's e-print, fetched from arXiv once per run at the version litwatch read, and `pdfPage()` and
 * `crop()` take them from the paper's PDF, which litwatch serves or sends on to arXiv for. Each figure made is kept
 * under the name the read cites it by; every image a function makes is also shown to the model with the cell's
 * output.
 */

import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { figureFiles, kindOf, type EprintFile } from "./eprint.ts";
import { fetchFile, readCapped, type HostConfig, type Pace } from "./hosts.ts";
import { MAX_FIGURE_WIDTH, type Box, type Imaging, type Shot } from "./imaging.ts";
import type { HostFn } from "./repl.ts";

/** A figure's name, as a read's `fig` block cites it and litwatch stores it (docs/integrations.md). */
export const FIGURE_NAME = /^[A-Za-z0-9][A-Za-z0-9._-]*$/;
/** An e-print or a PDF larger than this is not taken. */
const MAX_DOWNLOAD_BYTES = 150 * 1024 * 1024;
/** The figures one run may make, and what they may weigh together: far above what a read shows, and within what
 * litwatch takes of a job (32 MiB, docs/integrations.md), since a job that returns more is refused whole. */
const MAX_FIGURES = 60;
export const MAX_FIGURE_BYTES = 24 * 1024 * 1024;
/** A page shown to the model to look at, and a page a figure is cut from. */
const PAGE_VIEW_WIDTH = 1100;
const PAGE_CROP_WIDTH = 2200;
const RASTER: Record<string, string> = { png: "image/png", jpg: "image/jpeg", jpeg: "image/jpeg", gif: "image/gif", webp: "image/webp" };

export interface FigurePaper {
  id: string;
  /** The arXiv version litwatch's text is of; null or absent when it cannot say. */
  body_version?: number | null;
}

export class Figures {
  /** The figures made so far, by name: the WebP litwatch stores. */
  readonly made = new Map<string, Buffer>();
  /** True once figures were taken from the latest e-print because litwatch could not say the version. */
  usedLatest = false;

  private eprint: Promise<EprintFile[]> | undefined;
  private pdf: Promise<{ file: string; pages: number }> | undefined;
  private dir: Promise<string> | undefined;
  private files = 0;

  private readonly paper: FigurePaper;
  private readonly cfg: HostConfig;
  private readonly imaging: Imaging;
  private readonly show: (shot: Shot) => void;
  private readonly pace: Pace;
  private readonly ended: AbortSignal | undefined;

  /** `show` is given every image a function makes, for the model to see. */
  constructor(paper: FigurePaper, cfg: HostConfig, imaging: Imaging, show: (shot: Shot) => void, pace: Pace, ended?: AbortSignal) {
    this.paper = paper;
    this.cfg = cfg;
    this.imaging = imaging;
    this.show = show;
    this.pace = pace;
    this.ended = ended;
  }

  /** The e-print's figure files, fetched on first use. A failed fetch is tried again by the next call. */
  private source(): Promise<EprintFile[]> {
    this.eprint ??= (async () => {
      const version = this.paper.body_version;
      const known = typeof version === "number" && Number.isInteger(version) && version >= 1;
      const url = `${this.cfg.arxivUrl ?? "https://arxiv.org"}/e-print/${this.paper.id}${known ? `v${version}` : ""}`;
      const got = await fetchFile(url, { maxBytes: MAX_DOWNLOAD_BYTES, contact: this.cfg.contact, pace: this.pace, ended: this.ended, guard: this.cfg.guard });
      if (got.status !== 200) throw new Error(`arXiv answered ${got.status} for the e-print (${url})`);
      if (!known) this.usedLatest = true;
      return figureFiles(got.bytes);
    })();
    this.eprint.catch(() => (this.eprint = undefined));
    return this.eprint;
  }

  private scratch(): Promise<string> {
    return (this.dir ??= mkdtemp(path.join(tmpdir(), "litwatch-deep-")));
  }

  /** The paper's PDF from litwatch, fetched on first use and kept as a file for poppler. */
  private document(): Promise<{ file: string; pages: number }> {
    this.pdf ??= (async () => {
      const url = `${this.cfg.litwatchUrl}/api/papers/${encodeURIComponent(this.paper.id)}/pdf`;
      const resp = await this.pace(new URL(url).host, () =>
        fetch(url, {
          headers: this.cfg.litwatchToken ? { authorization: `Bearer ${this.cfg.litwatchToken}` } : {},
          // a request carrying the token goes nowhere else
          redirect: "manual",
          signal: this.ended ? AbortSignal.any([this.ended, AbortSignal.timeout(120_000)]) : AbortSignal.timeout(120_000),
        }),
      );
      let bytes: Buffer;
      const elsewhere = resp.status >= 300 && resp.status < 400 ? resp.headers.get("location") : null;
      if (elsewhere !== null) {
        // litwatch sends the reader on to arXiv's PDF when the archive has none: a public fetch, without the token
        await resp.body?.cancel();
        const got = await fetchFile(new URL(elsewhere, url).href, { maxBytes: MAX_DOWNLOAD_BYTES, contact: this.cfg.contact, pace: this.pace, ended: this.ended, guard: this.cfg.guard });
        if (got.status !== 200) throw new Error(`litwatch has no PDF of the paper, and arXiv answered ${got.status} for it`);
        bytes = got.bytes;
      } else if (resp.status !== 200) {
        await resp.body?.cancel();
        throw new Error(`litwatch answered ${resp.status} for the paper's PDF`);
      } else {
        const read = await readCapped(resp.body as Parameters<typeof readCapped>[0], MAX_DOWNLOAD_BYTES);
        if (read.cut) throw new Error(`the paper's PDF is larger than ${MAX_DOWNLOAD_BYTES} bytes`);
        bytes = read.bytes;
      }
      const file = path.join(await this.scratch(), "paper.pdf");
      await writeFile(file, bytes);
      return { file, pages: await this.imaging.pdfPages(file) };
    })();
    this.pdf.catch(() => (this.pdf = undefined));
    return this.pdf;
  }

  /** The file of the e-print a name means: its path, its path without the extension (as \includegraphics often
   * names it), or its file name alone when only one file has it. */
  private async find(name: string): Promise<EprintFile> {
    const files = await this.source();
    const wanted = name.replace(/^\.\//, "");
    const bare = (p: string) => p.replace(/\.[A-Za-z0-9]+$/, "");
    const base = (p: string) => p.slice(p.lastIndexOf("/") + 1);
    const matches =
      [files.filter((f) => f.path === wanted), files.filter((f) => bare(f.path) === wanted), files.filter((f) => base(f.path) === base(wanted) || bare(base(f.path)) === base(wanted))].find((m) => m.length > 0) ?? [];
    if (matches.length === 1) return matches[0]!;
    if (matches.length > 1) throw new Error(`${JSON.stringify(name)} names ${matches.length} files (${matches.map((f) => f.path).join(", ")}); give the whole path`);
    throw new Error(files.length ? `the e-print has no figure file ${JSON.stringify(name)}; figures() lists them` : "the e-print has no figure files: cut the figure from the PDF with pdfPage() and crop()");
  }

  private keep(name: string, webp: Buffer): void {
    if (!FIGURE_NAME.test(name)) throw new Error(`a figure's name is letters, digits, ".", "_" and "-", starting with a letter or digit; got ${JSON.stringify(name)}`);
    if (!this.made.has(name) && this.made.size >= MAX_FIGURES) throw new Error(`this run has made ${MAX_FIGURES} figures already`);
    let others = 0;
    for (const [made, bytes] of this.made) if (made !== name) others += bytes.length;
    if (others + webp.length > MAX_FIGURE_BYTES)
      throw new Error(`the run's figures would weigh more than ${MAX_FIGURE_BYTES / 1024 / 1024} MiB together; crop a smaller part, or make it again under a name no longer needed`);
    this.made.set(name, webp);
  }

  private page(pages: number, n: unknown): number {
    if (typeof n !== "number" || !Number.isInteger(n) || n < 1 || n > pages) throw new Error(`the PDF has pages 1 to ${pages}; got ${JSON.stringify(n)}`);
    return n;
  }

  /** The host functions the session calls. */
  hosts(): Record<string, HostFn> {
    return {
      figures: async () => (await this.source()).map((f) => ({ file: f.path, kind: kindOf(f.path), bytes: f.data.length })),
      figure: async ([file, name]) => {
        if (typeof file !== "string" || !file) throw new Error("figure(file, name?): file is a path from figures()");
        const found = await this.find(file);
        const kind = kindOf(found.path);
        const as = name === undefined || name === null ? found.path.slice(found.path.lastIndexOf("/") + 1).replace(/\.[A-Za-z0-9]+$/, "").replace(/[^A-Za-z0-9._-]/g, "_").replace(/^[^A-Za-z0-9]+/, "") : String(name);
        let picture;
        if (kind === "pdf") {
          const pdf = path.join(await this.scratch(), `figure-${++this.files}.pdf`);
          await writeFile(pdf, found.data);
          picture = await this.imaging.picture(await this.imaging.pdfPage(pdf, 1, MAX_FIGURE_WIDTH), "image/png");
        } else if (RASTER[kind]) picture = await this.imaging.picture(found.data, RASTER[kind]);
        else throw new Error(`${found.path} is ${kind.toUpperCase()}, which is not converted here: cut the figure from the PDF with pdfPage() and crop()`);
        this.keep(as, picture.webp);
        this.show(picture.view);
        return { name: as, file: found.path, width: picture.width, height: picture.height };
      },
      pdfPage: async ([n]) => {
        const { file, pages } = await this.document();
        const page = this.page(pages, n);
        const picture = await this.imaging.picture(await this.imaging.pdfPage(file, page, PAGE_VIEW_WIDTH), "image/png");
        this.show(picture.view);
        return { page, pages, width: picture.view.width, height: picture.view.height };
      },
      crop: async ([n, box, name]) => {
        const { file, pages } = await this.document();
        const page = this.page(pages, n);
        const b = Array.isArray(box) && box.length === 4 && box.every((v) => typeof v === "number" && v >= 0 && v <= 1) ? (box as Box) : undefined;
        if (!b || b[0] >= b[2] || b[1] >= b[3]) throw new Error("crop(page, [left, top, right, bottom], name): the box is four fractions of the page, 0 to 1, left < right and top < bottom");
        if (typeof name !== "string") throw new Error("crop(page, box, name): give the name the read cites the figure by");
        const picture = await this.imaging.picture(await this.imaging.pdfPage(file, page, PAGE_CROP_WIDTH), "image/png", b);
        this.keep(name, picture.webp);
        this.show(picture.view);
        return { name, page, width: picture.width, height: picture.height };
      },
    };
  }

  /** Removes the run's files. */
  async dispose(): Promise<void> {
    const dir = await this.dir?.catch(() => undefined);
    if (dir) await rm(dir, { recursive: true, force: true });
  }
}
