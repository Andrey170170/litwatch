/**
 * Host functions the isolate may call: litwatch's search, Semantic Scholar, and an
 * allowlisted fetch. Everything returns JSON-able values; errors become exceptions in the cell.
 */

import type { HostFn } from "./repl.ts";

export interface HostConfig {
  litwatchUrl: string;
  /** litwatch's read token, which may only search and read papers. */
  litwatchToken?: string;
  s2ApiKey?: string;
  maxFetchChars: number;
}

/** One `store.search` hit, as the agent has always been given it. */
export interface StoreHit {
  id: string;
  title: string;
  authors: string[];
  published: string | null;
  /** 1 must read, 2 relevant, 3 adjacent, 4 not about it; null when no Watch topic graded it. */
  tier: number | null;
  /** litwatch's Gist of the paper, if it wrote one. */
  reason: string | null;
  /** false for a paper litwatch never ingested, found by a search provider (the archive). */
  in_collection: boolean;
}

interface PaperRow {
  id: string;
  title: string;
  authors?: string[];
  published?: string | null;
  /** One per followed topic whose page holds the paper; grade null where none graded it. */
  badges?: { grade?: string | null }[];
  gist?: string | null;
}

const TIERS: Record<string, number> = { must_read: 1, relevant: 2, adjacent: 3, none: 4 };
/** The highest grade across the badges: the first badge gives the highest rarity after the depth
 * offsets, which need not be the highest grade. */
const tier = (badges: { grade?: string | null }[] | undefined): number | null => {
  const tiers = (badges ?? []).flatMap((b) => (b.grade && Object.hasOwn(TIERS, b.grade) ? [TIERS[b.grade]!] : []));
  return tiers.length ? Math.min(...tiers) : null;
};

/** v1's `GET /api/papers` answer as `store.search` hits: the collection first, then the
 * papers only a search provider holds. */
export function storeHits(body: { rows?: PaperRow[]; outside?: PaperRow[] }): StoreHit[] {
  const hit = (row: PaperRow, inCollection: boolean): StoreHit => ({
    id: row.id,
    title: row.title,
    authors: row.authors ?? [],
    published: row.published ?? null,
    tier: tier(row.badges),
    reason: row.gist ?? null,
    in_collection: inCollection,
  });
  return [...(body.rows ?? []).map((r) => hit(r, true)), ...(body.outside ?? []).map((r) => hit(r, false))];
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
      // A request carrying a token refuses redirects, so the token never goes anywhere else.
      const carriesToken = Object.keys(headers).some((h) => h.toLowerCase() === "authorization");
      const resp = await fetch(url, {
        headers,
        redirect: carriesToken ? "manual" : "follow",
        signal: AbortSignal.timeout(30_000),
      });
      if (resp.status >= 300 && resp.status < 400) throw new Error(`${url}: redirected (HTTP ${resp.status})`);
      if (resp.status === 429 || resp.status >= 500)
        throw new RetryableError(`${host} answered ${resp.status}`);
      if (!resp.ok) throw new Error(`${url}: HTTP ${resp.status}`);
      return resp.json();
    }),
  );
}

export function buildHosts(cfg: HostConfig): Record<string, HostFn> {
  const s2Headers: Record<string, string> = cfg.s2ApiKey ? { "x-api-key": cfg.s2ApiKey } : {};
  const litwatchHeaders: Record<string, string> = cfg.litwatchToken
    ? { authorization: `Bearer ${cfg.litwatchToken}` }
    : {};
  return {
    "store.search": async ([query, limit]) => {
      const n = Math.min(Math.max(Number(limit) || 10, 1), 50);
      const q = new URLSearchParams({ q: String(query), limit: String(n) });
      const body = await getJson(`${cfg.litwatchUrl}/api/papers?${q}`, litwatchHeaders);
      if (!body || typeof body !== "object") throw new Error("litwatch's papers answer is not an object");
      return storeHits(body as Parameters<typeof storeHits>[0]).slice(0, n);
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
  const arxiv = /^(?:arxiv:)?(\d{4}\.\d{4,5}|[a-z-]+(?:\.[a-z]{2})?\/\d{7})(?:v\d+)?$/i.exec(text);
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
