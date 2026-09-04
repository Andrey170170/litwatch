/**
 * Host functions the isolate may call: litwatch's own index, Semantic Scholar, and an
 * allowlisted fetch. Everything returns JSON-able values; errors become exceptions in the cell.
 */

import type { HostFn } from "./repl.ts";

export interface HostConfig {
  litwatchUrl: string;
  s2ApiKey?: string;
  maxFetchChars: number;
}

const ALLOWED_HOSTS = [
  "arxiv.org",
  "export.arxiv.org",
  "api.semanticscholar.org",
  "www.semanticscholar.org",
  "openreview.net",
  "api.openreview.net",
  "api2.openreview.net",
  "github.com",
  "raw.githubusercontent.com",
  "api.github.com",
  "huggingface.co",
];

const S2 = "https://api.semanticscholar.org/graph/v1";
const S2_FIELDS = "title,year,authors.name,externalIds,citationCount,openAccessPdf,abstract";
const S2_REF_FIELDS = "references.title,references.year,references.externalIds,references.citationCount";
const PACING_MS: Record<string, number> = { "arxiv.org": 3000, "export.arxiv.org": 3000, "api.semanticscholar.org": 1100 };

/** One request at a time per host, spaced by that host's pacing. */
const lanes = new Map<string, Promise<void>>();
async function paced<T>(host: string, work: () => Promise<T>): Promise<T> {
  const wait = PACING_MS[host] ?? 0;
  const previous = lanes.get(host) ?? Promise.resolve();
  let release!: () => void;
  const mine = new Promise<void>((r) => (release = r));
  lanes.set(host, previous.then(() => mine));
  await previous;
  try {
    return await work();
  } finally {
    setTimeout(release, wait);
  }
}

async function withRetry<T>(attempt: () => Promise<T>, retries = 5): Promise<T> {
  let delay = 1000;
  for (let i = 0; ; i++) {
    try {
      return await attempt();
    } catch (e) {
      if (i >= retries || !(e instanceof RetryableError)) throw e;
      await new Promise((r) => setTimeout(r, delay));
      delay *= 2;
    }
  }
}

class RetryableError extends Error {}

async function getJson(url: string, headers: Record<string, string> = {}): Promise<unknown> {
  const host = new URL(url).host;
  return paced(host, () =>
    withRetry(async () => {
      const resp = await fetch(url, { headers, signal: AbortSignal.timeout(30_000) });
      if (resp.status === 429 || resp.status >= 500)
        throw new RetryableError(`${host} answered ${resp.status}`);
      if (!resp.ok) throw new Error(`${url}: HTTP ${resp.status}`);
      return resp.json();
    }),
  );
}

export function buildHosts(cfg: HostConfig): Record<string, HostFn> {
  const s2Headers: Record<string, string> = cfg.s2ApiKey ? { "x-api-key": cfg.s2ApiKey } : {};
  return {
    "store.search": async ([query, limit]) => {
      const q = new URLSearchParams({ q: String(query), limit: String(Number(limit) || 10) });
      return getJson(`${cfg.litwatchUrl}/papers/search?${q}`);
    },
    "s2.lookup": async ([q]) => {
      const text = String(q).trim();
      const id = s2Id(text);
      if (id) return getJson(`${S2}/paper/${id}?fields=${S2_FIELDS},${S2_REF_FIELDS}`, s2Headers);
      const params = new URLSearchParams({ query: text, limit: "5", fields: S2_FIELDS });
      const found = (await getJson(`${S2}/paper/search?${params}`, s2Headers)) as { data?: unknown[] };
      return found.data ?? [];
    },
    fetch: async ([url]) => fetchAllowed(String(url), cfg.maxFetchChars),
  };
}

/** Semantic Scholar paper id syntax for an arXiv id, DOI, or S2 hash; null means "search". */
export function s2Id(text: string): string | null {
  const arxiv = /^(?:arxiv:)?(\d{4}\.\d{4,5})(?:v\d+)?$/i.exec(text);
  if (arxiv) return `arXiv:${arxiv[1]}`;
  if (/^10\.\d{4,9}\/\S+$/.test(text)) return `DOI:${text}`;
  if (/^[0-9a-f]{40}$/.test(text)) return text;
  return null;
}

export async function fetchAllowed(url: string, maxChars: number): Promise<unknown> {
  let parsed: URL;
  try {
    parsed = new URL(url);
  } catch {
    throw new Error(`not a URL: ${url}`);
  }
  if (parsed.protocol !== "https:" && parsed.protocol !== "http:")
    throw new Error(`only http(s) URLs can be fetched`);
  if (!ALLOWED_HOSTS.includes(parsed.hostname))
    throw new Error(`${parsed.hostname} is not on the allowlist (${ALLOWED_HOSTS.join(", ")})`);
  return paced(parsed.hostname, async () => {
    const resp = await fetch(url, {
      headers: { "user-agent": "litwatch-deep (mailto:andrey24122004@gmail.com)" },
      redirect: "follow",
      signal: AbortSignal.timeout(30_000),
    });
    const contentType = resp.headers.get("content-type") ?? "";
    if (contentType.includes("pdf")) {
      return { status: resp.status, contentType, text: "", note: "PDF bodies are not fetched; use the arXiv HTML or abstract page" };
    }
    const raw = await resp.text();
    const text = contentType.includes("html") ? htmlToText(raw) : raw;
    return {
      status: resp.status,
      contentType,
      text: text.slice(0, maxChars),
      truncated: text.length > maxChars,
    };
  });
}

/** Readable text from HTML: scripts and styles dropped, block boundaries kept as newlines. */
export function htmlToText(html: string): string {
  return html
    .replace(/<(script|style|noscript|svg|nav|header|footer)\b[\s\S]*?<\/\1>/gi, " ")
    .replace(/<!--[\s\S]*?-->/g, " ")
    .replace(/<br\s*\/?>/gi, "\n")
    .replace(/<\/(p|div|li|h[1-6]|tr|section|article|blockquote|pre|dd|dt)>/gi, "\n")
    .replace(/<[^>]+>/g, " ")
    .replace(/&nbsp;/g, " ")
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/[ \t]+/g, " ")
    .replace(/\s*\n\s*/g, "\n")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}
