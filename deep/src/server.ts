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
    const wrong = refusal(body);
    if (wrong) throw new HttpError(wrong.status, wrong.error);
    const { paper, extensions = [], selections = [] } = body as JobInput;
    const job = deps.jobs.submit(paper, extensions, selections, typeof key === "string" ? key : undefined);
    return json(res, 202, { id: job.id, status: job.status });
  }
  throw new HttpError(404, "not found");
}

/** Why a job's input is turned down, as `POST /process` answers it: not the contract's (422), or asking for an
 * extension the service has not (400). None when it is a job's input. `run FILE.json` checks its file by it too. */
export function refusal(body: unknown): { status: 400 | 422; error: string } | undefined {
  const wrong = schemaErrors(JobInputSchema, body);
  if (wrong.length) return { status: 422, error: `not a job's input: ${wrong.slice(0, 20).join("; ")}` };
  for (const name of (body as JobInput).extensions ?? [])
    if (!Object.hasOwn(LENS_QUESTIONS, name)) return { status: 400, error: `unknown extension "${name}"; known: ${Object.keys(LENS_QUESTIONS).join(", ")}` };
  return undefined;
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
