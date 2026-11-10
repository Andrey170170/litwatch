/**
 * A paper's e-print as arXiv serves it, read for its figure files (docs/plans/deep-read.md, D2). An e-print is a
 * gzipped tar of the submission's files, a gzipped single TeX file, or, for a paper submitted as a PDF, that PDF:
 * only the first has figure files. The archive is read in memory and only its regular files are taken, so nothing
 * in it (a path, a link) reaches the filesystem.
 */

import { gunzipSync } from "node:zlib";

/** One file of the e-print: its path in the archive and its bytes. */
export interface EprintFile {
  path: string;
  data: Buffer;
}

/** What a figure file may be, by its extension: what `\includegraphics` takes. `ps` and `eps` are listed, so the
 * agent sees them, but nothing here converts them. */
export const FIGURE_KINDS = ["pdf", "png", "jpg", "jpeg", "gif", "webp", "eps", "ps"] as const;
const FIGURE = new RegExp(`\\.(${FIGURE_KINDS.join("|")})$`, "i");
export const kindOf = (path: string): string => /\.([A-Za-z0-9]+)$/.exec(path)?.[1]?.toLowerCase() ?? "";

/** What an unpacked e-print may weigh: far above any submission arXiv takes. */
export const MAX_UNPACKED_BYTES = 400 * 1024 * 1024;
const BLOCK = 512;

const text = (block: Buffer, from: number, to: number): string => {
  const end = block.indexOf(0, from);
  return block.toString("utf8", from, end === -1 || end > to ? to : end);
};

/** A path inside the archive, or undefined for one that would leave it. */
function safe(path: string): string | undefined {
  const parts = path.split("/").filter((p) => p !== "" && p !== ".");
  return path.startsWith("/") || parts.includes("..") || parts.length === 0 ? undefined : parts.join("/");
}

/** The regular files of a tar archive that `keep` wants. Long names in GNU's and pax's forms are read; links,
 * directories and devices are skipped. Throws on an archive cut short. */
export function untar(tar: Buffer, keep: (path: string) => boolean): EprintFile[] {
  const files: EprintFile[] = [];
  let longName: string | undefined;
  for (let at = 0; at + BLOCK <= tar.length; ) {
    const header = tar.subarray(at, at + BLOCK);
    if (header.every((b) => b === 0)) break;
    const size = Number.parseInt(text(header, 124, 136).trim() || "0", 8);
    if (!Number.isFinite(size) || size < 0) throw new Error("the e-print's archive has an entry of no readable size");
    const body = at + BLOCK;
    if (body + size > tar.length) throw new Error("the e-print's archive is cut short");
    const type = String.fromCharCode(header[156]!);
    const data = tar.subarray(body, body + size);
    if (type === "L") longName = text(data, 0, data.length);
    else if (type === "x") {
      // pax records: "<length> <key>=<value>\n"
      const path = /(?:^|\n)\d+ path=([^\n]*)\n/.exec(data.toString("utf8"));
      if (path) longName = path[1];
    } else {
      const prefix = text(header, 257, 262) === "ustar" ? text(header, 345, 500) : "";
      const name = longName ?? (prefix ? `${prefix}/${text(header, 0, 100)}` : text(header, 0, 100));
      longName = undefined;
      const path = type === "0" || type === "\0" ? safe(name) : undefined;
      if (path !== undefined && keep(path)) files.push({ path, data: Buffer.from(data) });
    }
    at = body + Math.ceil(size / BLOCK) * BLOCK;
  }
  return files;
}

/** Whether bytes open with a tar header: its checksum adds up. */
function isTar(bytes: Buffer): boolean {
  if (bytes.length < BLOCK) return false;
  const stated = Number.parseInt(text(bytes, 148, 156).trim(), 8);
  let sum = 0;
  for (let i = 0; i < BLOCK; i++) sum += i >= 148 && i < 156 ? 32 : bytes[i]!;
  return Number.isFinite(stated) && stated === sum;
}

/** The figure files of an e-print, by path. Empty for one with none: a single TeX file, or a PDF. */
export function figureFiles(eprint: Buffer): EprintFile[] {
  if (eprint.length < 2 || eprint[0] !== 0x1f || eprint[1] !== 0x8b) return isTar(eprint) ? untar(eprint, (p) => FIGURE.test(p)) : [];
  let unpacked: Buffer;
  try {
    unpacked = gunzipSync(eprint, { maxOutputLength: MAX_UNPACKED_BYTES });
  } catch (e) {
    throw new Error(`the e-print could not be unpacked: ${e instanceof Error ? e.message : String(e)}`);
  }
  return isTar(unpacked) ? untar(unpacked, (p) => FIGURE.test(p)).sort((a, b) => a.path.localeCompare(b.path)) : [];
}
