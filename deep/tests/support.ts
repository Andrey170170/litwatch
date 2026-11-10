/**
 * What the tests share: a small paper and a draft read of it, images and archives made in code (no paper's figure
 * is committed), a stand-in for the browser and poppler, and models whose provider is pi-ai's faux one.
 */

import { deflateSync, gzipSync } from "node:zlib";
import { readFileSync } from "node:fs";
import { createServer } from "node:http";
import type { AddressInfo } from "node:net";
import { createModels, fauxAssistantMessage, fauxProvider, fauxToolCall, type AssistantMessage, type Models } from "@earendil-works/pi-ai";
import type { RunOptions } from "../src/agent.ts";
import { PROVIDER } from "../src/auth.ts";
import { makeGuard, type HostConfig } from "../src/hosts.ts";
import type { Box, Imaging, RenderTarget, RenderWidth } from "../src/imaging.ts";
import type { Draft, Read } from "../src/read.ts";
import type { Verdict } from "../src/review.ts";

export const OPTS: RunOptions = { model: "faux-1", effort: "low", maxCells: 12, cellTimeoutMs: 5000, wallClockMs: 60_000, maxOutputChars: 4000 };
/** Hosts for a test that reaches nothing: litwatch and arXiv are names that do not resolve. */
export const HOSTS: HostConfig = { litwatchUrl: "http://litwatch.invalid", arxivUrl: "http://arxiv.invalid", maxFetchChars: 1000, maxFetches: 60, maxFetchBytes: 1 << 20 };
/** The network guard that lets a test's own server on this machine stand for a public site. */
export const OPEN = makeGuard(() => true);

/** A LaTeX paper with a commented-out number, one figure, one table, a bibliography before its appendix, and an
 * appendix. */
export const BODY = String.raw`\documentclass{article}
\begin{document}
\title{Probes read the trace}
\begin{abstract}
Linear probes on the residual stream predict unfaithful traces.
\end{abstract}
\section{Introduction}
Earlier work left open whether a probe can tell an unfaithful trace from a faithful one.
% an earlier draft reported 99.9\% here
\section{Method}
The authors train a linear probe on layer 12 of Qwen3-8B over 1{,}200 labelled traces.
\begin{figure}
\includegraphics{figs/probe.pdf}
\caption{Probe accuracy by layer.}
\end{figure}
\section{Results}
The probe reaches 71.3\% accuracy against 50\% for a random direction, see Table~\ref{tab:main}.
\begin{table}
\begin{tabular}{lr} Probe & 71.3 \\ Random & 50.0 \end{tabular}
\caption{Accuracy.}
\end{table}
\begin{thebibliography}{9}
\bibitem{turpin} Turpin et al. Language models don't always say what they think. 2023.
\end{thebibliography}
\appendix
\section{Probe training details}
The learning rate is 0.003 and training runs for 40 epochs.
\end{document}
`;

export const PAPER = { id: "2610.00001", title: "Probes read the trace", abstract: "A.", authors: ["A. Author"], body: BODY, body_source: "latex", body_version: 2 };

/** A draft that passes every check against `BODY`, with no figure and no lens. */
export function draft(): Draft {
  return {
    format: "read",
    sections: [
      {
        id: "glance",
        kind: "glance",
        title: "At a glance",
        claim: "A linear probe on the residual stream tells unfaithful traces from faithful ones.",
        points: ["The probe reaches 71.3% accuracy.", "A random direction reaches 50%."],
        facts: [
          { name: "Kind", text: "method" },
          { name: "Read", text: "about 3 minutes" },
        ],
      },
      {
        id: "method",
        kind: "method",
        title: "Method",
        takeaway: "A linear probe on layer 12, trained on 1,200 labelled traces.",
        blocks: [
          { type: "p", text: "The authors train a linear probe on layer 12 of Qwen3-8B over 1,200 labelled traces." },
          { type: "why", text: "A linear probe, because it reads what is linearly present and adds little of its own." },
        ],
      },
      {
        id: "result",
        kind: "finding",
        title: "Finding · The probe beats chance",
        takeaway: "The probe reaches 71.3% against 50% for a random direction.",
        blocks: [
          { type: "xp", question: "Does the probe beat a random direction?", did: "The authors compare the two on held-out traces.", found: "The probe reaches 71.3% and the random direction 50%.", means: "The signal is linearly readable." },
          { type: "table", columns: ["Direction", "Accuracy"], rows: [{ cells: ["Probe", 71.3] }, { cells: ["Random", "50.0"] }], caption: "Rebuilt from Table 1." },
          { type: "note", text: "The paper reports no interval for the 71.3%.", where: "§3, Table 1" },
        ],
      },
      {
        id: "claims",
        kind: "claims",
        title: "Claims",
        takeaway: "The one claim holds on the paper's own data.",
        claims: [{ text: "Probes predict unfaithful traces.", verdict: "holds", evidence: "71.3% against 50% for a random direction.", where: "§3, Table 1" }],
      },
      { id: "limits", kind: "limits", title: "Limitations", takeaway: "One model.", limitations: [{ by: "read", text: "Only one model was probed." }] },
      { id: "related", kind: "related", title: "Related work", takeaway: "It builds on biased-context work.", prior_work: [{ title: "Turpin et al.", relation: "Reuses the biased-context setup for hints." }] },
      { id: "repro", kind: "repro", title: "Reproducibility", takeaway: "No code is released.", facts: [{ name: "Code", tags: [{ text: "code", state: "no" }] }] },
    ],
    among_collection: [],
  };
}

/** `draft()` with a change made to it. */
export function changed(change: (d: Draft) => void): Draft {
  const d = draft();
  change(d);
  return d;
}

/** A section's blocks, for a change to one of them. */
export const blocks = (d: Draft | Read, id: string) => (d.sections.find((s) => s.id === id) as unknown as { blocks: Record<string, unknown>[] }).blocks;

// ── images and archives made in code ────────────────────────────────────────────────────────────────

const crcTable = Array.from({ length: 256 }, (_, n) => {
  let c = n;
  for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
  return c >>> 0;
});
const crc32 = (bytes: Buffer): number => (bytes.reduce((c, b) => crcTable[(c ^ b) & 0xff]! ^ (c >>> 8), 0xffffffff) ^ 0xffffffff) >>> 0;

/** A PNG of one colour, `width` by `height`, with a band of another colour across its top fifth. */
export function png(width: number, height: number): Buffer {
  const chunk = (type: string, data: Buffer) => {
    const body = Buffer.concat([Buffer.from(type), data]);
    const out = Buffer.alloc(8 + data.length + 4);
    out.writeUInt32BE(data.length, 0);
    body.copy(out, 4);
    out.writeUInt32BE(crc32(body), 8 + data.length);
    return out;
  };
  const header = Buffer.alloc(13);
  header.writeUInt32BE(width, 0);
  header.writeUInt32BE(height, 4);
  header.set([8, 2, 0, 0, 0], 8); // 8-bit RGB
  const row = (r: number, g: number, b: number) => Buffer.concat([Buffer.from([0]), Buffer.alloc(width * 3, Buffer.from([r, g, b]))]);
  const rows = Array.from({ length: height }, (_, y) => (y < height / 5 ? row(200, 30, 30) : row(30, 90, 200)));
  return Buffer.concat([Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]), chunk("IHDR", header), chunk("IDAT", deflateSync(Buffer.concat(rows))), chunk("IEND", Buffer.alloc(0))]);
}

/** A PDF of `pages` US-letter pages, each with one filled rectangle. */
export function pdf(pages = 1): Buffer {
  const objects: string[] = [];
  const kids = Array.from({ length: pages }, (_, i) => `${3 + i * 2} 0 R`).join(" ");
  objects.push("<< /Type /Catalog /Pages 2 0 R >>", `<< /Type /Pages /Kids [${kids}] /Count ${pages} >>`);
  for (let i = 0; i < pages; i++) {
    const stream = "0.2 0.4 0.8 rg 100 500 300 150 re f";
    objects.push(`<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents ${4 + i * 2} 0 R >>`, `<< /Length ${stream.length} >>\nstream\n${stream}\nendstream`);
  }
  let out = "%PDF-1.4\n";
  const offsets: number[] = [];
  objects.forEach((body, i) => {
    offsets.push(out.length);
    out += `${i + 1} 0 obj\n${body}\nendobj\n`;
  });
  const xref = out.length;
  out += `xref\n0 ${objects.length + 1}\n0000000000 65535 f \n${offsets.map((o) => `${String(o).padStart(10, "0")} 00000 n \n`).join("")}`;
  out += `trailer\n<< /Size ${objects.length + 1} /Root 1 0 R >>\nstartxref\n${xref}\n%%EOF\n`;
  return Buffer.from(out, "latin1");
}

/** One tar entry: a regular file by default, or another kind by its type flag. */
export interface TarEntry {
  path: string;
  data?: Buffer;
  type?: string;
  /** Written in ustar's prefix field, before the name. */
  prefix?: string;
}

/** A ustar archive of the entries, ended as tar ends one. */
export function tar(entries: TarEntry[]): Buffer {
  const blocks: Buffer[] = [];
  for (const { path, data = Buffer.alloc(0), type = "0", prefix = "" } of entries) {
    const header = Buffer.alloc(512);
    header.write(path, 0, 100, "utf8");
    header.write("0000644\0", 100);
    header.write("0000000\0", 108);
    header.write("0000000\0", 116);
    header.write(`${data.length.toString(8).padStart(11, "0")}\0`, 124);
    header.write("00000000000\0", 136);
    header.write("        ", 148);
    header.write(type, 156);
    header.write("ustar\0", 257);
    header.write("00", 263);
    header.write(prefix, 345, 155, "utf8");
    header.write(`${header.reduce((sum, b) => sum + b, 0).toString(8).padStart(6, "0")}\0 `, 148);
    blocks.push(header, data, Buffer.alloc((512 - (data.length % 512)) % 512));
  }
  return Buffer.concat([...blocks, Buffer.alloc(1024)]);
}

/** An e-print as arXiv serves a LaTeX submission: a gzipped tar. */
export const eprint = (entries: TarEntry[]): Buffer => gzipSync(tar(entries));

/** The smallest WebP header litwatch takes for one (`RIFF....WEBP`), for a stand-in's figures. */
export const WEBP = Buffer.concat([Buffer.from("RIFF"), Buffer.from([4, 0, 0, 0]), Buffer.from("WEBP")]);

/** A stand-in for the browser and poppler that records what it was asked for. A picture's view says how many bytes
 * it was made from, so a test can tell one picture's view from another's; a PDF has the pages its file has. */
export function fakeImaging() {
  const calls = {
    pictures: [] as { mime: string; crop?: Box; bytes: number }[],
    pages: [] as { page: number; width: number }[],
    renders: [] as { read: Read; figures: Record<string, string>; target: RenderTarget; width: RenderWidth }[],
  };
  const imaging: Imaging = {
    picture: async (data, mime, crop) => {
      calls.pictures.push({ mime, crop, bytes: data.length });
      return { webp: WEBP, width: 800, height: 400, view: { data: Buffer.from(`view of ${data.length} bytes`).toString("base64"), mimeType: "image/jpeg", width: 800, height: 400 } };
    },
    pdfPages: async (file) => (readFileSync(file, "latin1").match(/\/Type \/Page\b/g) ?? []).length,
    pdfPage: async (_path, page, width) => {
      calls.pages.push({ page, width });
      return Buffer.from(`page ${page} at ${width}`);
    },
    render: async (read, figures, target, width) => {
      calls.renders.push({ read, figures, target, width });
      return { shots: [{ data: Buffer.from("render").toString("base64"), mimeType: "image/jpeg", width: 854, height: 600 }], height: 600, cut: false };
    },
    close: async () => undefined,
  };
  return { imaging, calls };
}

// ── the model ───────────────────────────────────────────────────────────────────────────────────────

/** Models whose `openai` provider is pi-ai's faux one, signed in to the subscription. `contextWindow` is the
 * model's, in tokens, for a test of what fits it. */
export function models(contextWindow?: number): { models: Models; faux: ReturnType<typeof fauxProvider> } {
  const faux = fauxProvider({ provider: PROVIDER, models: [{ id: OPTS.model, input: ["text", "image"], ...(contextWindow ? { contextWindow } : {}) }] });
  const m = createModels();
  m.setProvider(faux.provider);
  m.getAuth = (async () => ({ auth: { apiKey: "t" }, source: "OAuth" })) as Models["getAuth"];
  return { models: m, faux };
}

/** The writer running one cell. */
export const cell = (code: string) => fauxAssistantMessage(fauxToolCall("run_code", { code }), { stopReason: "toolUse" });
/** The writer submitting a read. */
export const submits = (read: Draft) => cell(`await submit(${JSON.stringify(read)})`);

export const PASSING: Verdict = {
  scores: { faithfulness: 5, clarity: 4, depth: 4, calibration: 5 },
  checks: { anchors: true, experiments: true, why_boxes: true, voice: true, length: true },
  findings: [],
};
/** The reviewer giving a verdict. */
export const reviews = (verdict: unknown) => fauxAssistantMessage(fauxToolCall("review", verdict as Parameters<typeof fauxToolCall>[1]), { stopReason: "toolUse" });

/** One message of the conversation a model call was sent: the system prompt with its tools first, then the turns. */
export interface SentMessage {
  role: string;
  content: string | ({ type: "text"; text: string } | { type: "image"; data: string; mimeType: string } | { type: string })[];
  toolsAdded?: { name: string }[];
}
export interface Sent {
  messages: SentMessage[];
}

/** A response that also keeps the conversation the model was sent, for a test to look at. */
export function seeing(answer: AssistantMessage, into: Sent[]): (context: unknown) => AssistantMessage {
  return (context) => {
    into.push(structuredClone(context) as Sent);
    return answer;
  };
}

/** A message's text, and its images. */
export const textOf = (m: SentMessage): string => (typeof m.content === "string" ? m.content : m.content.flatMap((c) => ("text" in c ? [c.text] : [])).join("\n"));
export const imagesOf = (m: SentMessage): string[] => (typeof m.content === "string" ? [] : m.content.flatMap((c) => ("data" in c ? [c.data] : [])));

// ── arXiv and litwatch, on this machine ─────────────────────────────────────────────────────────────

/** The e-print the site serves: two figures by their own names, an EPS, and one name in two directories. */
export const EPRINT = eprint([
  { path: "main.tex", data: Buffer.from("\\documentclass{article}") },
  { path: "figs/probe.png", data: png(8, 8) },
  { path: "figs/arch.pdf", data: pdf() },
  { path: "figs/old.eps", data: Buffer.from("%!PS") },
  { path: "a/setup.png", data: png(4, 4) },
  { path: "b/setup.png", data: png(4, 4) },
]);

/** arXiv and litwatch as one local server: the e-print at any version (or `eprintStatus` in its place), and the
 * paper's PDF, which litwatch serves itself (three pages) or, with `archived` false, sends on to arXiv's latest
 * for. arXiv's PDF has as many pages as its version's number, and the latest is version 5. `asked` keeps each
 * request's path, with the authorization it carried. */
export async function withSite<T>(work: (site: { url: string; asked: string[] }) => Promise<T>, { eprintStatus = 200, archived = true } = {}): Promise<T> {
  const asked: string[] = [];
  const server = createServer((req, res) => {
    asked.push(`${req.url}${req.headers.authorization ? ` ${req.headers.authorization}` : ""}`);
    if (req.url?.startsWith("/e-print/")) res.writeHead(eprintStatus, { "content-type": "application/gzip" }).end(eprintStatus === 200 ? EPRINT : "");
    else if (req.url === `/api/papers/${PAPER.id}/pdf` && !archived) res.writeHead(307, { location: `/pdf/${PAPER.id}` }).end();
    else if (req.url === `/api/papers/${PAPER.id}/pdf`) res.writeHead(200, { "content-type": "application/pdf" }).end(pdf(3));
    else if (req.url?.startsWith(`/pdf/${PAPER.id}`)) res.writeHead(200, { "content-type": "application/pdf" }).end(pdf(Number(/v(\d+)$/.exec(req.url)?.[1] ?? 5)));
    else res.writeHead(404).end();
  });
  await new Promise<void>((r) => server.listen(0, "127.0.0.1", r));
  try {
    return await work({ url: `http://127.0.0.1:${(server.address() as AddressInfo).port}`, asked });
  } finally {
    server.closeAllConnections();
    server.close();
  }
}
