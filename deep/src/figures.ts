/**
 * The paper's figures in one run's session (docs/plans/deep-read.md, D2): `figures()` and `figure()` take them
 * from the paper's e-print, fetched from arXiv once per run at the version litwatch read, and `pdfPage()` and
 * `crop()` take them from the paper's PDF, which litwatch serves or sends on to arXiv for. Each figure made is kept
 * as one record under the name the read cites it by: the stored image, the view the model saw of it, and the paper's
 * label for it when the writer gave one. Every image a function makes is also shown to the model with the cell's
 * output.
 */

import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { figureFiles, kindOf, type EprintFile } from "./eprint.ts";
import { fetchFile, readCapped, type HostConfig, type Pace } from "./hosts.ts";
import { MAX_FIGURE_WIDTH, type Box, type Imaging, type Picture, type Shot } from "./imaging.ts";
import { figureLabel } from "./places.ts";
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

/** A figure the run made. */
export interface Made {
  /** The WebP litwatch stores. */
  webp: Buffer;
  /** The view the model was shown of it, which the reviewer is shown too. */
  view: Shot;
  /** The paper's label for it ("3", "A.1"), when the writer said which of the paper's figures it is. */
  label?: string;
}

export class Figures {
  /** The figures made so far, by name. */
  readonly made = new Map<string, Made>();
  /** True once the e-print or the PDF was taken at arXiv's latest version because litwatch could not say which
   * version its text is of. */
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

  /** The arXiv version litwatch's text is of, or undefined when it cannot say. */
  private version(): number | undefined {
    const version = this.paper.body_version;
    return typeof version === "number" && Number.isInteger(version) && version >= 1 ? version : undefined;
  }

  /** One of arXiv's files of the paper (`e-print`, `pdf`) at the version litwatch's text is of, or at the latest
   * when litwatch cannot say which that is. */
  private async arxiv(what: "e-print" | "pdf"): Promise<{ status: number; bytes: Buffer; url: string }> {
    const version = this.version();
    const url = `${this.cfg.arxivUrl ?? "https://arxiv.org"}/${what}/${this.paper.id}${version ? `v${version}` : ""}`;
    const got = await fetchFile(url, { maxBytes: MAX_DOWNLOAD_BYTES, contact: this.cfg.contact, pace: this.pace, ended: this.ended, guard: this.cfg.guard });
    if (got.status === 200 && !version) this.usedLatest = true;
    return { ...got, url };
  }

  /** The e-print's figure files, fetched on first use. A failed fetch is tried again by the next call. */
  private source(): Promise<EprintFile[]> {
    this.eprint ??= (async () => {
      const got = await this.arxiv("e-print");
      if (got.status !== 200) throw new Error(`arXiv answered ${got.status} for the e-print (${got.url})`);
      return figureFiles(got.bytes);
    })();
    this.eprint.catch(() => (this.eprint = undefined));
    return this.eprint;
  }

  private scratch(): Promise<string> {
    return (this.dir ??= mkdtemp(path.join(tmpdir(), "litwatch-deep-")));
  }

  /** The paper's PDF, fetched on first use and kept as a file for poppler: litwatch's own, of the version the text is
   * of where litwatch can say, or arXiv's when litwatch has none and sends the reader on. */
  private document(): Promise<{ file: string; pages: number }> {
    this.pdf ??= (async () => {
      const version = this.version();
      const url = `${this.cfg.litwatchUrl}/api/papers/${encodeURIComponent(this.paper.id)}/pdf${version ? `?version=${version}` : ""}`;
      const resp = await this.pace(new URL(url).host, () =>
        fetch(url, {
          headers: this.cfg.litwatchToken ? { authorization: `Bearer ${this.cfg.litwatchToken}` } : {},
          // a request carrying the token goes nowhere else
          redirect: "manual",
          signal: this.ended ? AbortSignal.any([this.ended, AbortSignal.timeout(120_000)]) : AbortSignal.timeout(120_000),
        }),
      );
      let bytes: Buffer;
      if (resp.status >= 300 && resp.status < 400) {
        // litwatch sends the reader on to arXiv's latest PDF when the archive has none. The crops must be of the
        // version the text is of, so the PDF is asked of arXiv at that version rather than where the redirect
        // points: a public fetch, without the token.
        await resp.body?.cancel();
        const got = await this.arxiv("pdf");
        if (got.status !== 200) throw new Error(`litwatch has no PDF of the paper, and arXiv answered ${got.status} for it (${got.url})`);
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

  /** The paper's label a figure is made as, from the number the writer gave ("Figure 6", "A.1", 6), if any. */
  private label(number: unknown): string | undefined {
    if (number === undefined || number === null) return undefined;
    const label = typeof number === "string" || typeof number === "number" ? figureLabel(String(number)) : undefined;
    if (label === undefined) throw new Error(`a figure's number is the paper's own for it, such as "Figure 6" or "Figure A.1"; got ${JSON.stringify(number)}`);
    return label;
  }

  /** Keeps a picture as the figure of this name: its image, its view and its label together, so no later call can
   * pair one figure's name with another's image. */
  private keep(name: string, picture: Picture, label: string | undefined): void {
    if (!FIGURE_NAME.test(name)) throw new Error(`a figure's name is letters, digits, ".", "_" and "-", starting with a letter or digit; got ${JSON.stringify(name)}`);
    if (!this.made.has(name) && this.made.size >= MAX_FIGURES) throw new Error(`this run has made ${MAX_FIGURES} figures already`);
    let others = 0;
    for (const [made, figure] of this.made) if (made !== name) others += figure.webp.length;
    if (others + picture.webp.length > MAX_FIGURE_BYTES)
      throw new Error(`the run's figures would weigh more than ${MAX_FIGURE_BYTES / 1024 / 1024} MiB together; crop a smaller part, or make it again under a name no longer needed`);
    this.made.set(name, { webp: picture.webp, view: picture.view, ...(label === undefined ? {} : { label }) });
    this.show(picture.view);
  }

  private page(pages: number, n: unknown): number {
    if (typeof n !== "number" || !Number.isInteger(n) || n < 1 || n > pages) throw new Error(`the PDF has pages 1 to ${pages}; got ${JSON.stringify(n)}`);
    return n;
  }

  /** The host functions the session calls. */
  hosts(): Record<string, HostFn> {
    return {
      figures: async () => (await this.source()).map((f) => ({ file: f.path, kind: kindOf(f.path), bytes: f.data.length })),
      figure: async ([file, name, number]) => {
        if (typeof file !== "string" || !file) throw new Error("figure(file, name?, number?): file is a path from figures()");
        const label = this.label(number);
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
        this.keep(as, picture, label);
        return { name: as, file: found.path, width: picture.width, height: picture.height, ...(label === undefined ? {} : { number: `Figure ${label}` }) };
      },
      pdfPage: async ([n]) => {
        const { file, pages } = await this.document();
        const page = this.page(pages, n);
        const picture = await this.imaging.picture(await this.imaging.pdfPage(file, page, PAGE_VIEW_WIDTH), "image/png");
        this.show(picture.view);
        return { page, pages, width: picture.view.width, height: picture.view.height };
      },
      crop: async ([n, box, name, number]) => {
        const label = this.label(number);
        const { file, pages } = await this.document();
        const page = this.page(pages, n);
        const b = Array.isArray(box) && box.length === 4 && box.every((v) => typeof v === "number" && v >= 0 && v <= 1) ? (box as Box) : undefined;
        if (!b || b[0] >= b[2] || b[1] >= b[3]) throw new Error("crop(page, [left, top, right, bottom], name, number?): the box is four fractions of the page, 0 to 1, left < right and top < bottom");
        if (typeof name !== "string") throw new Error("crop(page, box, name, number?): give the name the read cites the figure by");
        const picture = await this.imaging.picture(await this.imaging.pdfPage(file, page, PAGE_CROP_WIDTH), "image/png", b);
        this.keep(name, picture, label);
        return { name, page, width: picture.width, height: picture.height, ...(label === undefined ? {} : { number: `Figure ${label}` }) };
      },
    };
  }

  /** Removes the run's files. */
  async dispose(): Promise<void> {
    const dir = await this.dir?.catch(() => undefined);
    if (dir) await rm(dir, { recursive: true, force: true });
  }
}
