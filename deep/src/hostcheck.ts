/**
 * The access state of a link on a supported host, from the host's API, never from a page or a model's reading: a
 * GitHub repository (`api.github.com/repos/<owner>/<repo>`, its contents, its tree, its releases) or a Hugging Face
 * model, dataset or space (`huggingface.co/api/<kind>s/<owner>/<name>`). The finder takes a supported link's state
 * from it whatever the model drafted; litwatch's weekly re-check (`litwatch.links.adapter`) is the same check in
 * Python, and one table of fixture responses (`tests/fixtures/host-checks.json`) drives both suites.
 *
 * - Hugging Face: its `gated` flag (`true`, "auto", "manual"; `false` or absent is not; anything else says nothing) is
 *   gated; else, for weights, a listed weight file is public; for code, a listed file beside the filler; a repository
 *   with neither is a placeholder. A 401 or 404 is missing (it answers 401 for a repository it does not have).
 * - GitHub: for code, a file beside the filler in its contents is public; for weights, a weight file in its tree or a
 *   release asset with a weight file's name; anything else a placeholder. A 404 is missing.
 *
 * The API is asked by a plain GET of its own (`apiGet`), not the model's fetch: its JSON whole, `MAX_BYTES` at most (a
 * large GitHub tree fits), and a redirect (a renamed repository: GitHub answers 301, Hugging Face 307) followed
 * `REDIRECTS` times at most, each hop over HTTPS on the host asked, as the re-check follows it; a hop elsewhere says
 * nothing, even one that would come back. An answer that is not the API's (not JSON, too large, not the shape the API
 * gives) says nothing either.
 */

import type { LinksAnswer } from "./contracts.ts";

type Link = LinksAnswer["links"][number];

/** What a GET answered, after the redirects it followed: its status, the URL that answered, its body. */
export interface Got {
  status: number;
  url: string;
  text: string;
}
/** A GET of a host's API; null when it got no answer (the network, a refusal, too large, too many redirects). */
export type Get = (url: string) => Promise<Got | null>;

/** How many redirects a check follows, and the most an answer may hold; litwatch's re-check takes the same
 * (`litwatch.links.adapter`). */
export const REDIRECTS = 3;
export const MAX_BYTES = 8_000_000;
const TIMEOUT_MS = 30_000;

type Fetch = (url: string, init: RequestInit) => Promise<Response>;

/** The host checks' own GET (`fetchImpl` the network; tests give a fake one): each redirect followed by hand, only
 * over HTTPS on the host asked, the answer to a hop elsewhere standing; a body read whole, `MAX_BYTES` at most. Under
 * `signal` (the find's session): once it is aborted no request starts, and one under way stops. */
export function apiGet(fetchImpl: Fetch = fetch, signal?: AbortSignal): Get {
  return async (url) => {
    try {
      const host = new URL(url).hostname;
      let at = url;
      for (let hop = 0; hop <= REDIRECTS; hop++) {
        if (signal?.aborted) return null;
        const timeout = AbortSignal.timeout(TIMEOUT_MS);
        const resp = await fetchImpl(at, {
          redirect: "manual",
          headers: { accept: "application/json", "user-agent": "litwatch-deep" },
          signal: signal ? AbortSignal.any([signal, timeout]) : timeout,
        });
        const location = resp.headers.get("location");
        if (resp.status >= 300 && resp.status < 400 && location) {
          const next = new URL(location, at);
          await resp.body?.cancel();
          if (next.protocol !== "https:" || next.hostname !== host) return { status: resp.status, url: at, text: "" };
          at = next.href;
          continue;
        }
        const text = await bounded(resp, MAX_BYTES);
        return text === null ? null : { status: resp.status, url: at, text };
      }
      return null;
    } catch {
      return null;
    }
  };
}

/** A response's body as text, null past `max` bytes. */
async function bounded(resp: Response, max: number): Promise<string | null> {
  if (!resp.body) return "";
  const reader = resp.body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    size += value.byteLength;
    if (size > max) {
      await reader.cancel();
      return null;
    }
    chunks.push(value);
  }
  return Buffer.concat(chunks).toString("utf8");
}

/** What the host says of a link: its state, its license when it gives one, and the status its API answered with. */
export interface HostState {
  state: Link["state"];
  license?: string;
  status: number;
}

/** What a repository holds beside its code when it holds no code yet: a README, a licence, git's and GitHub's files, a
 * citation. */
const FILLER = /^(readme|licen[cs]e|copying|citation|notice)(\.\w+)?$|^\.git(ignore|attributes|hub)?$/i;
/** A file of weights, whole or one shard of them (`model-00001-of-00004.safetensors`, `model.gguf.part1of2`). */
export const WEIGHTS = /\.(safetensors|bin|pt|pth|ckpt|gguf|onnx|h5|msgpack|npz)(\.part\d+(of\d+)?)?$/i;

const base = (path: string) => path.split("/").at(-1) ?? "";
const filler = (names: string[]) => names.every((n) => FILLER.test(base(n)));

/** The API page a supported link is checked at, and its host; null for a link on another host, or not a
 * repository's own page (a file or folder in one). */
export function apiTarget(link: string): { api: string; host: "github" | "huggingface" } | null {
  let u: URL;
  try {
    u = new URL(link);
  } catch {
    return null;
  }
  const host = u.hostname.replace(/^www\./, "");
  const parts = u.pathname.split("/").filter(Boolean);
  if (u.search) return null;
  if (host === "github.com" && parts.length === 2) return { api: `https://api.github.com/repos/${parts[0]}/${parts[1]!.replace(/\.git$/, "")}`, host: "github" };
  if (host !== "huggingface.co") return null;
  const [kind, owner, name, ...rest] = parts[0] === "datasets" || parts[0] === "spaces" ? parts : ["models", ...parts];
  return owner && name && !rest.length ? { api: `https://huggingface.co/api/${kind}/${owner}/${name}`, host: "huggingface" } : null;
}

/** A GET that answered on `host` with JSON; null when it went elsewhere or its body is not JSON. */
async function json(get: Get, url: string, host: string): Promise<{ status: number; body: unknown } | null> {
  const got = await get(url);
  if (got === null) return null;
  try {
    const at = new URL(got.url);
    if (at.protocol !== "https:" || at.hostname !== host) return null; // redirected elsewhere: it says nothing
  } catch {
    return null;
  }
  if (got.status < 200 || got.status >= 300) return { status: got.status, body: null };
  try {
    return { status: got.status, body: JSON.parse(got.text) };
  } catch {
    return null;
  }
}

/** A JSON object, not an array or null: the shape an API's record has. */
const object = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);

const strings = (items: unknown, field: string): string[] =>
  Array.isArray(items) ? items.map((i) => (i && typeof i === "object" ? (i as Record<string, unknown>)[field] : undefined)).filter((s): s is string => typeof s === "string") : [];

/** The state of a link on a supported host; undefined for a link on another host; null when the host could not say
 * (it did not answer, refused, was rate limited, or answered what is not its API's). */
export async function hostState(link: string, kind: Link["kind"], get: Get): Promise<HostState | null | undefined> {
  const target = apiTarget(link);
  if (target === null) return undefined;
  return target.host === "github" ? github(target.api, kind, get) : huggingface(target.api, kind, get);
}

async function github(api: string, kind: Link["kind"], get: Get): Promise<HostState | null> {
  const host = "api.github.com";
  const repo = await json(get, api, host);
  if (repo === null) return null;
  if (repo.status === 404) return { state: "missing", status: 404 };
  if (repo.status !== 200) return null;
  if (!object(repo.body)) return null;
  const found = repo.body as { full_name?: unknown; default_branch?: unknown; license?: unknown };
  if (typeof found.full_name !== "string") return null;
  const named_license = object(found.license) ? (found.license as { spdx_id?: unknown; name?: unknown }) : {};
  const spdx = named_license.spdx_id;
  const license = typeof spdx === "string" && spdx && spdx !== "NOASSERTION" ? spdx : typeof named_license.name === "string" ? named_license.name : undefined;
  const at = (state: Link["state"]): HostState => ({ state, ...(license ? { license } : {}), status: 200 });
  const named = `https://${host}/repos/${found.full_name}`;
  if (kind === "code") {
    const contents = await json(get, `${named}/contents`, host);
    if (contents === null) return null;
    if (contents.status === 404) return at("placeholder"); // a repository with no commit has no contents
    if (contents.status !== 200 || !Array.isArray(contents.body)) return null;
    return at(filler(strings(contents.body, "path")) ? "placeholder" : "public");
  }
  const branch = typeof found.default_branch === "string" ? found.default_branch : "HEAD";
  const tree = await json(get, `${named}/git/trees/${encodeURIComponent(branch)}?recursive=1`, host);
  if (tree === null || (tree.status !== 200 && tree.status !== 404 && tree.status !== 409)) return null; // 404, 409: no commit yet
  if (tree.status === 200) {
    const listed = object(tree.body) ? (tree.body as { tree?: unknown }).tree : undefined;
    if (!Array.isArray(listed)) return null;
    if (strings(listed, "path").some((p) => WEIGHTS.test(p))) return at("public");
  }
  const releases = await json(get, `${named}/releases?per_page=100`, host);
  if (releases === null || releases.status !== 200 || !Array.isArray(releases.body)) return null;
  const assets = releases.body.flatMap((r) => (object(r) ? strings((r as { assets?: unknown }).assets, "name") : []));
  return at(assets.some((a) => WEIGHTS.test(a)) ? "public" : "placeholder");
}

async function huggingface(api: string, kind: Link["kind"], get: Get): Promise<HostState | null> {
  const found = await json(get, api, "huggingface.co");
  if (found === null) return null;
  if (found.status === 401 || found.status === 404) return { state: "missing", status: found.status };
  if (found.status !== 200 || !object(found.body)) return null;
  const repo = found.body as { gated?: unknown; siblings?: unknown; cardData?: unknown; tags?: unknown };
  const tag = Array.isArray(repo.tags) ? repo.tags.find((t): t is string => typeof t === "string" && t.startsWith("license:")) : undefined;
  const card = object(repo.cardData) ? (repo.cardData as { license?: unknown }) : {};
  const license = typeof card.license === "string" ? card.license : tag?.slice("license:".length);
  const at = (state: Link["state"]): HostState => ({ state, ...(license ? { license } : {}), status: 200 });
  const gated = repo.gated ?? (Object.hasOwn(repo, "gated") ? null : false); // absent: not gated
  if (gated !== false && gated !== true && gated !== "auto" && gated !== "manual") return null; // not a value its API gives
  if (gated !== false) return at("gated");
  if (!Array.isArray(repo.siblings)) return null;
  const names = strings(repo.siblings, "rfilename");
  const held = kind === "weights" ? names.some((n) => WEIGHTS.test(n)) : !filler(names);
  return at(held ? "public" : "placeholder");
}
