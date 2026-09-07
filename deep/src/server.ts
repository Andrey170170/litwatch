/** Stack-internal HTTP surface: start a job, watch it, read the outcome. No auth by design. */

import { createServer, type IncomingMessage, type Server, type ServerResponse } from "node:http";
import type { JobQueue } from "./jobs.ts";
import type { PaperInput, SelectionContext } from "./prompt.ts";
import { EXTENSIONS } from "./schema.ts";

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
  const one = /^\/jobs\/([0-9a-f-]{36})$/.exec(path);
  if (req.method === "GET" && one) {
    const job = deps.jobs.get(one[1]);
    if (!job) throw new HttpError(404, "no such job");
    return json(res, 200, job);
  }
  if (req.method === "POST" && path === "/process") {
    const body = (await readJson(req)) as {
      paper?: Partial<PaperInput>;
      extensions?: string[];
      selections?: SelectionContext[];
    };
    const paper = body.paper;
    if (!paper || typeof paper.id !== "string" || typeof paper.title !== "string" || typeof paper.body !== "string")
      throw new HttpError(422, "paper.id, paper.title, and paper.body are required");
    const extensions = body.extensions ?? [];
    for (const name of extensions)
      if (!(name in EXTENSIONS)) throw new HttpError(400, `unknown extension "${name}"; known: ${Object.keys(EXTENSIONS).join(", ")}`);
    const job = deps.jobs.submit(
      {
        id: paper.id,
        title: paper.title,
        abstract: paper.abstract ?? "",
        authors: paper.authors ?? [],
        published: paper.published ?? null,
        url: paper.url ?? null,
        body: paper.body,
        body_source: paper.body_source ?? null,
      },
      extensions,
      body.selections ?? [],
    );
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
