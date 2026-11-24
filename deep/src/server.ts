/**
 * Stack-internal HTTP surface: start a job, watch it, read the outcome. No auth by design. A submission is checked
 * against the job's input contract (`contracts.ts`). Its `Idempotency-Key` header names it: a key the service already
 * has is answered (200) with that key's job, and `GET /keys/<key>` finds that job (404 when the service has none).
 */

import { createServer, type IncomingMessage, type Server, type ServerResponse } from "node:http";
import { JobInputSchema, type JobInput } from "./contracts.ts";
import type { JobQueue } from "./jobs.ts";
import { LENS_QUESTIONS } from "./prompt.ts";
import { schemaErrors } from "./schema.ts";

export interface ServerDeps {
  jobs: JobQueue;
  health: () => Promise<Record<string, unknown>>;
}

const MAX_BODY = 8 * 1024 * 1024;

export function buildServer(deps: ServerDeps): Server {
  return createServer(async (req, res) => {
    try {
      await route(req, res, deps);
    } catch (e) {
      const status = e instanceof HttpError ? e.status : 500;
      json(res, status, { error: e instanceof Error ? e.message : String(e) });
    }
  });
}

class HttpError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function route(req: IncomingMessage, res: ServerResponse, deps: ServerDeps): Promise<void> {
  const url = new URL(req.url ?? "/", "http://local");
  const path = url.pathname.replace(/\/+$/, "") || "/";
  if (req.method === "GET" && path === "/healthz") return json(res, 200, { ...(await deps.health()), ...deps.jobs.active });
  if (req.method === "GET" && path === "/jobs") return json(res, 200, deps.jobs.list());
  const keyed = /^\/keys\/([^/]+)$/.exec(path);
  if (req.method === "GET" && keyed) {
    const job = deps.jobs.byKey(decodeURIComponent(keyed[1]));
    if (!job) throw new HttpError(404, "no job for that key");
    return json(res, 200, { id: job.id, status: job.status });
  }
  const one = /^\/jobs\/([0-9a-f-]{36})$/.exec(path);
  if (req.method === "GET" && one) {
    const job = deps.jobs.get(one[1]);
    if (!job) throw new HttpError(404, "no such job");
    return json(res, 200, job);
  }
  if (req.method === "POST" && path === "/process") {
    const body = await readJson(req);
    const key = req.headers["idempotency-key"];
    const known = typeof key === "string" ? deps.jobs.byKey(key) : undefined;
    if (known) return json(res, 200, { id: known.id, status: known.status });
    const wrong = schemaErrors(JobInputSchema, body);
    if (wrong.length) throw new HttpError(422, `not a job's input: ${wrong.slice(0, 20).join("; ")}`);
    const { paper, extensions = [], selections = [] } = body as JobInput;
    // the outline is how a LaTeX source is read, and a PDF's text is read without one
    if ((paper.body_source === "latex") !== (paper.outline !== undefined))
      throw new HttpError(422, "a LaTeX source comes with its outline (paper.outline), and no other text does");
    for (const name of extensions)
      if (!Object.hasOwn(LENS_QUESTIONS, name)) throw new HttpError(400, `unknown extension "${name}"; known: ${Object.keys(LENS_QUESTIONS).join(", ")}`);
    const job = deps.jobs.submit(paper, extensions, selections, typeof key === "string" ? key : undefined);
    return json(res, 202, { id: job.id, status: job.status });
  }
  throw new HttpError(404, "not found");
}

async function readJson(req: IncomingMessage): Promise<unknown> {
  const chunks: Buffer[] = [];
  let size = 0;
  for await (const chunk of req) {
    size += (chunk as Buffer).length;
    if (size > MAX_BODY) throw new HttpError(413, "body too large");
    chunks.push(chunk as Buffer);
  }
  try {
    return JSON.parse(Buffer.concat(chunks).toString("utf8"));
  } catch {
    throw new HttpError(400, "invalid JSON");
  }
}

function json(res: ServerResponse, status: number, body: unknown): void {
  res.writeHead(status, { "content-type": "application/json" });
  res.end(JSON.stringify(body));
}
