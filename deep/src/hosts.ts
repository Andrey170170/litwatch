/**
 * Host functions the isolate may call: litwatch's search, Semantic Scholar, and a fetch of any
 * public page, never a private address (decision 80). Everything returns JSON-able values; errors
 * become exceptions in the cell.
 */

import dns, { type LookupAddress, type LookupOptions } from "node:dns";
import { isIP } from "node:net";
import { setTimeout as sleep } from "node:timers/promises";
import type { ReadableStream } from "node:stream/web";
import ipaddr from "ipaddr.js";
import { Agent, fetch as undiciFetch, type Dispatcher } from "undici";
import type { HostFn } from "./repl.ts";

export interface HostConfig {
  litwatchUrl: string;
  /** litwatch's read token, which may only search and read papers. */
  litwatchToken?: string;
  s2ApiKey?: string;
  /** An address the fetched sites can write to, sent in the User-Agent; arXiv asks for one. */
  contact?: string;
  /** Text kept per fetch. */
  maxFetchChars: number;
  /** Fetches per paper; the next one throws. */
  maxFetches: number;
  /** Bytes read per fetched body; the rest is never read. */
  maxFetchBytes: number;
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

const S2 = "https://api.semanticscholar.org/graph/v1";
const S2_FIELDS = "title,year,authors.name,externalIds,citationCount,openAccessPdf,abstract";
const S2_REF_FIELDS = "references.title,references.year,references.externalIds,references.citationCount";
const PACING_MS: Record<string, number> = { "arxiv.org": 3000, "export.arxiv.org": 3000, "api.semanticscholar.org": 1100 };
/** The pace of a site the agent fetches that has none of its own above: polite, and far below the budget. */
const FETCH_PACING_MS = 1000;

type Pace = <T>(host: string, work: () => Promise<T>, fallbackMs?: number) => Promise<T>;

/** One request at a time per host, spaced by that host's pacing (else `fallbackMs`). Each paper's
 * session gets its own lanes; once `ended` aborts, work still queued in them stops waiting and never
 * starts. */
export function pacer(ended?: AbortSignal): Pace {
  const lanes = new Map<string, Promise<void>>();
  const end = new Promise<void>((r) => {
    if (ended?.aborted) r();
    else ended?.addEventListener("abort", () => r(), { once: true });
  });
  return async (host, work, fallbackMs = 0) => {
    const wait = PACING_MS[host] ?? fallbackMs;
    const previous = lanes.get(host) ?? Promise.resolve();
    let release!: () => void;
    const mine = new Promise<void>((r) => (release = r));
    lanes.set(host, previous.then(() => mine));
    await Promise.race([previous, end]);
    try {
      ended?.throwIfAborted();
      return await work();
    } finally {
      setTimeout(release, wait);
    }
  };
}

async function withRetry<T>(attempt: () => Promise<T>, ended?: AbortSignal, retries = 5): Promise<T> {
  let delay = 1000;
  for (let i = 0; ; i++) {
    try {
      return await attempt();
    } catch (e) {
      if (i >= retries || !(e instanceof RetryableError)) throw e;
      await sleep(delay, undefined, { signal: ended });
      delay *= 2;
    }
  }
}

class RetryableError extends Error {}

/** A request's time limit, cut short when its paper's session ends. */
const deadline = (ended?: AbortSignal): AbortSignal =>
  ended ? AbortSignal.any([ended, AbortSignal.timeout(30_000)]) : AbortSignal.timeout(30_000);

async function getJson(pace: Pace, url: string, headers: Record<string, string> = {}, ended?: AbortSignal): Promise<unknown> {
  const host = new URL(url).host;
  return pace(host, () =>
    withRetry(async () => {
      // A request carrying a token refuses redirects, so the token never goes anywhere else.
      const carriesToken = Object.keys(headers).some((h) => h.toLowerCase() === "authorization");
      const resp = await fetch(url, {
        headers,
        redirect: carriesToken ? "manual" : "follow",
        signal: deadline(ended),
      });
      if (resp.status >= 300 && resp.status < 400) throw new Error(`${url}: redirected (HTTP ${resp.status})`);
      if (resp.status === 429 || resp.status >= 500)
        throw new RetryableError(`${host} answered ${resp.status}`);
      if (!resp.ok) throw new Error(`${url}: HTTP ${resp.status}`);
      return resp.json();
    }, ended),
  );
}

/** The agent's host functions for one paper's session; `ended` aborts when the session ends, and
 * stops what it left queued or under way. */
export function buildHosts(cfg: HostConfig, ended?: AbortSignal): Record<string, HostFn> {
  const s2Headers: Record<string, string> = cfg.s2ApiKey ? { "x-api-key": cfg.s2ApiKey } : {};
  const litwatchHeaders: Record<string, string> = cfg.litwatchToken
    ? { authorization: `Bearer ${cfg.litwatchToken}` }
    : {};
  const pace = pacer(ended);
  let fetches = 0;
  return {
    "store.search": async ([query, limit]) => {
      const n = Math.min(Math.max(Number(limit) || 10, 1), 50);
      const q = new URLSearchParams({ q: String(query), limit: String(n) });
      const body = await getJson(pace, `${cfg.litwatchUrl}/api/papers?${q}`, litwatchHeaders, ended);
      if (!body || typeof body !== "object") throw new Error("litwatch's papers answer is not an object");
      return storeHits(body as Parameters<typeof storeHits>[0]).slice(0, n);
    },
    "s2.lookup": async ([q]) => {
      const text = String(q).trim();
      const id = s2Id(text);
      if (id) return getJson(pace, `${S2}/paper/${id}?fields=${S2_FIELDS},${S2_REF_FIELDS}`, s2Headers, ended);
      const params = new URLSearchParams({ query: text, limit: "5", fields: S2_FIELDS });
      const found = (await getJson(pace, `${S2}/paper/search?${params}`, s2Headers, ended)) as { data?: unknown[] };
      return found.data ?? [];
    },
    fetch: async ([url], detail) => {
      if (++fetches > cfg.maxFetches) throw new Error(`the fetch budget of ${cfg.maxFetches} per paper is used up`);
      const record: FetchRecord = { hops: [] };
      if (detail) detail.fetch = record;
      return fetchPublic(String(url), { maxChars: cfg.maxFetchChars, maxBytes: cfg.maxFetchBytes, contact: cfg.contact, pace, ended, record });
    },
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

/** Whether an address is on the public internet: ipaddr.js's `unicast`, after unwrapping
 * IPv4-mapped IPv6. Every other range is refused: private, loopback, link-local, unique-local,
 * site-local, carrier-grade NAT, reserved, multicast, and the forms that embed an IPv4 address
 * (NAT64, 6to4, Teredo). */
export function isPublic(address: string): boolean {
  try {
    return ipaddr.process(address).range() === "unicast";
  } catch {
    return false;
  }
}

type Resolve = (hostname: string, options: LookupOptions & { all: true }, cb: (err: NodeJS.ErrnoException | null, addresses: LookupAddress[]) => void) => void;
type LookupCallback = (err: NodeJS.ErrnoException | null, address: string | LookupAddress[], family?: number) => void;

/** A `lookup` for the socket that refuses a name unless every address it resolves to is public.
 * It checks the addresses the connection then uses, so a name can't resolve one way for a check
 * and another for the connection. */
export function guardedLookup(publicOnly = isPublic, resolve?: Resolve) {
  return (hostname: string, options: LookupOptions, callback: LookupCallback): void => {
    (resolve ?? (dns.lookup as unknown as Resolve))(hostname, { ...options, all: true }, (err, addresses) => {
      if (err) return callback(err, "");
      const refused = addresses.find((a) => !publicOnly(a.address));
      if (refused || addresses.length === 0)
        return callback(new Error(`${hostname} resolves to ${refused?.address ?? "nothing"}, not a public address`), "");
      if (options.all) callback(null, addresses);
      else callback(null, addresses[0]!.address, addresses[0]!.family);
    });
  };
}

/** What lets a fetch through: the policy for literal addresses, and the dispatcher whose sockets
 * resolve names through it. Tests build their own. */
export interface Guard {
  publicOnly: (address: string) => boolean;
  dispatcher: Dispatcher;
}

/** The guard production uses, and tests with their own policy and resolver. */
export function makeGuard(publicOnly = isPublic, resolve?: Resolve): Guard {
  return { publicOnly, dispatcher: new Agent({ connect: { lookup: guardedLookup(publicOnly, resolve) } }) };
}

let defaultGuard: Guard | undefined;
const guard = (): Guard => (defaultGuard ??= makeGuard());

const MAX_HOPS = 5;
const REDIRECTS = new Set([301, 302, 303, 307, 308]);
/** anonymous.4open.science builds its pages in the browser, and its `/r/` links answer 401 to a
 * plain fetch; its JSON API serves the same repository: `files/` lists a folder, `file/<path>`
 * returns a file. */
export function rewriteAnonymous(url: URL): URL {
  if (url.hostname !== "anonymous.4open.science") return url;
  const m = /^\/r\/([^/]+)(?:\/(.*))?$/.exec(url.pathname);
  if (!m) return url;
  const decode = (s: string) => {
    try {
      return decodeURIComponent(s);
    } catch {
      return s;
    }
  };
  const [id, rest = ""] = [decode(m[1]!), m[2] ?? ""];
  const path = rest.split("/").filter(Boolean).map(decode);
  const base = `${url.origin}/api/repo/${encodeURIComponent(id)}`;
  if (path.length === 0) return new URL(`${base}/files/`);
  if (rest.endsWith("/")) {
    const listing = new URL(`${base}/files/`);
    listing.searchParams.set("path", path.join("/"));
    return listing;
  }
  return new URL(`${base}/file/${path.map(encodeURIComponent).join("/")}`);
}

/** A URL's host as an address when it is one: WHATWG URL keeps IPv6's brackets, which `isIP` reads as a name. */
const literal = (hostname: string): string | null => {
  const bare = hostname.startsWith("[") && hostname.endsWith("]") ? hostname.slice(1, -1) : hostname;
  return isIP(bare) ? bare : null;
};

export interface FetchOptions {
  maxChars: number;
  maxBytes: number;
  /** Named in the User-Agent when set. */
  contact?: string;
  pace?: Pace;
  /** Aborts when the paper's session ends. */
  ended?: AbortSignal;
  guard?: Guard;
  /** Filled in as the fetch goes, for the trace. */
  record?: FetchRecord;
}

/** What one fetch did: each redirect it was answered with, then the URL that answered with a page,
 * its status and the body bytes read (0 for a PDF, never read). A fetch refused or failed on the
 * way keeps the hops it made. */
export interface FetchRecord {
  hops: { url: string; status: number }[];
  url?: string;
  status?: number;
  bytes?: number;
}

/** GET a public page: each hop's scheme and address checked (a literal here, a name where the
 * socket resolves it), at most five redirects, the body read only up to `maxBytes`. */
export async function fetchPublic(url: string, opts: FetchOptions): Promise<unknown> {
  const { publicOnly, dispatcher } = opts.guard ?? guard();
  const record = opts.record ?? { hops: [] };
  const pace = opts.pace ?? pacer();
  let current: URL;
  try {
    current = new URL(url);
  } catch {
    throw new Error(`not a URL: ${url}`);
  }
  for (let hop = 0; ; hop++) {
    if (current.protocol !== "https:" && current.protocol !== "http:") throw new Error(`only http(s) URLs can be fetched`);
    current = rewriteAnonymous(current);
    const address = literal(current.hostname);
    if (address && !publicOnly(address)) throw new Error(`${current.hostname} is not a public address`);
    const at = current;
    const answer = await pace<{ next?: URL; page?: unknown }>(
      at.hostname,
      async () => {
        const resp = await undiciFetch(at, {
          headers: { "user-agent": opts.contact ? `litwatch-deep (mailto:${opts.contact})` : "litwatch-deep" },
          redirect: "manual",
          dispatcher,
          signal: deadline(opts.ended),
        }).catch((e: unknown) => {
          // undici says only "fetch failed"; the agent needs the reason: a refused address, DNS, a timeout.
          const cause = e instanceof Error && e.cause instanceof Error ? e.cause : e;
          throw new Error(`${at.href}: ${cause instanceof Error ? cause.message : String(cause)}`);
        });
        const location = resp.headers.get("location");
        if (REDIRECTS.has(resp.status) && location !== null) {
          await resp.body?.cancel();
          record.hops.push({ url: at.href, status: resp.status });
          return { next: new URL(location, at) };
        }
        Object.assign(record, { url: at.href, status: resp.status, bytes: 0 });
        const contentType = resp.headers.get("content-type") ?? "";
        const media = contentType.toLowerCase();
        if (media.includes("pdf")) {
          await resp.body?.cancel();
          return { page: { status: resp.status, contentType, url: at.href, text: "", note: "PDF bodies are not fetched; use the arXiv HTML or abstract page" } };
        }
        const { raw, cut } = await readCapped(resp.body, opts.maxBytes, record);
        const text = media.includes("html") ? htmlToText(raw) : raw;
        return {
          page: {
            status: resp.status,
            contentType,
            url: at.href,
            text: text.slice(0, opts.maxChars),
            truncated: cut || text.length > opts.maxChars,
          },
        };
      },
      FETCH_PACING_MS,
    );
    if (!answer.next) return answer.page;
    if (hop + 1 > MAX_HOPS) throw new Error(`${url}: more than ${MAX_HOPS} redirects`);
    current = answer.next;
  }
}

/** A body as text, read only up to `maxBytes`: reading stops once that many have come, so a body
 * left open past the cap is never waited on. `counted.bytes` follows each chunk taken, so a read
 * cut off by the connection or a timeout still says how much came. */
async function readCapped(
  body: ReadableStream<Uint8Array> | null,
  maxBytes: number,
  counted: { bytes?: number },
): Promise<{ raw: string; cut: boolean }> {
  if (!body) return { raw: "", cut: false };
  // A whole number of bytes, or a fractional setting (DEEP_MAX_FETCH_MB=0.1) would never be reached.
  const limit = Math.floor(maxBytes);
  const reader = body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  let cut = false;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    const part = value.subarray(0, limit - size);
    chunks.push(part);
    size += part.byteLength;
    counted.bytes = size;
    if (size >= limit) {
      cut = true;
      await reader.cancel();
      break;
    }
  }
  return { raw: new TextDecoder().decode(Buffer.concat(chunks)), cut };
}

/** Readable text from HTML: scripts, styles and comments dropped, block boundaries kept as newlines.
 * It runs on the event loop, outside any cell's time limit, so every step is linear in the page:
 * a hostile page of unclosed tags must not hold the service for hours. */
export function htmlToText(html: string): string {
  return stripTags(
    dropBlocks(html)
      .replace(/<br\s*\/?>/gi, "\n")
      .replace(/<\/(p|div|li|h[1-6]|tr|section|article|blockquote|pre|dd|dt)>/gi, "\n"),
  )
    .replace(/&nbsp;/g, " ")
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/[^\S\n]+/g, " ")
    .replace(/ ?\n[ \n]*/g, "\n")
    .trim();
}

/** Each tag as a space, a tag running from a `<` to the next `>` as `/<[^>]+>/g` reads it, without
 * that regex retrying from every `<` when no `>` follows. */
function stripTags(html: string): string {
  let out = "";
  let kept = 0;
  for (let open = html.indexOf("<"); open !== -1; open = html.indexOf("<", open + 1)) {
    const close = html.indexOf(">", open + 1);
    if (close === -1) break;
    if (close === open + 1) continue;
    out += html.slice(kept, open) + " ";
    kept = close + 1;
    open = close;
  }
  return out + html.slice(kept);
}

/** HTML without its comments and its script, style, noscript, svg, nav, header and footer elements.
 * An opener whose closer never comes is left in place, and its kind is not searched for again. */
function dropBlocks(html: string): string {
  const opens = /<(script|style|noscript|svg|nav|header|footer)\b|<!--/gi;
  const unclosed = new Set<string>();
  let out = "";
  let kept = 0;
  for (let open = opens.exec(html); open; open = opens.exec(html)) {
    const kind = open[1]?.toLowerCase() ?? "!--";
    if (unclosed.has(kind)) continue;
    const close = new RegExp(kind === "!--" ? "-->" : `</${kind}>`, "gi");
    close.lastIndex = opens.lastIndex;
    const end = close.exec(html);
    if (!end) {
      unclosed.add(kind);
      continue;
    }
    out += html.slice(kept, open.index) + " ";
    kept = opens.lastIndex = end.index + end[0].length;
  }
  return out + html.slice(kept);
}
