/**
 * The ChatGPT subscription credential: one JSON file on the state volume, refreshed in place by
 * pi-ai under a per-provider lock. Login is Sign in with ChatGPT; the server has no browser, so
 * the operator opens the printed URL elsewhere and pastes back the final redirect URL.
 */

import { randomUUID } from "node:crypto";
import { mkdirSync, readFileSync, renameSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { createInterface } from "node:readline/promises";
import {
  createModels,
  type AuthOperationOptions,
  type Credential,
  type CredentialInfo,
  type CredentialStore,
  type Models,
} from "@earendil-works/pi-ai";
import { openaiProvider } from "@earendil-works/pi-ai/providers/openai";

export const PROVIDER = "openai";

export class FileCredentialStore implements CredentialStore {
  private chain: Promise<unknown> = Promise.resolve();
  private readonly path: string;

  constructor(path: string) {
    this.path = path;
  }

  private load(): Record<string, Credential> {
    try {
      return JSON.parse(readFileSync(this.path, "utf8")) as Record<string, Credential>;
    } catch {
      return {};
    }
  }

  private save(all: Record<string, Credential>): void {
    mkdirSync(dirname(this.path), { recursive: true, mode: 0o700 });
    const tmp = `${this.path}.tmp`;
    writeFileSync(tmp, JSON.stringify(all, null, 2), { mode: 0o600 });
    renameSync(tmp, this.path);
  }

  private serialized<T>(fn: () => Promise<T>): Promise<T> {
    const next = this.chain.then(fn, fn);
    this.chain = next.catch(() => undefined);
    return next;
  }

  read(providerId: string, _options?: AuthOperationOptions): Promise<Credential | undefined> {
    return this.serialized(async () => this.load()[providerId]);
  }

  list(_options?: AuthOperationOptions): Promise<readonly CredentialInfo[]> {
    return this.serialized(async () =>
      Object.entries(this.load()).map(([providerId, c]) => ({ providerId, type: c.type })),
    );
  }

  modify(
    providerId: string,
    fn: (current: Credential | undefined) => Promise<Credential | undefined>,
    _options?: AuthOperationOptions,
  ): Promise<Credential | undefined> {
    return this.serialized(async () => {
      const all = this.load();
      const next = await fn(all[providerId]);
      if (next !== undefined) {
        all[providerId] = next;
        this.save(all);
      }
      return all[providerId];
    });
  }

  delete(providerId: string, _options?: AuthOperationOptions): Promise<void> {
    return this.serialized(async () => {
      const all = this.load();
      delete all[providerId];
      this.save(all);
    });
  }
}

export function buildModels(authFile: string): Models {
  const models = createModels({ credentials: new FileCredentialStore(authFile) });
  models.setProvider(openaiProvider());
  return models;
}

/** The installation's stable ID, which Sign in with ChatGPT requires; created on first login. */
function deviceId(authFile: string): string {
  const path = join(dirname(authFile), "device-id");
  try {
    return readFileSync(path, "utf8").trim();
  } catch {
    const id = randomUUID();
    mkdirSync(dirname(path), { recursive: true, mode: 0o700 });
    writeFileSync(path, `${id}\n`, { mode: 0o600 });
    return id;
  }
}

/** Sign in with ChatGPT: prints the URL, reads the pasted redirect URL, persists the credential. */
export async function login(models: Models, authFile: string, out: (line: string) => void): Promise<void> {
  const rl = createInterface({ input: process.stdin, output: process.stdout });
  try {
    await models.login(
      PROVIDER,
      "oauth",
      {
        prompt: async (p) => {
          if (p.type !== "manual_code") throw new Error(`unexpected login prompt: ${p.type}`);
          return rl.question(`${p.message}\n> `, { signal: p.signal });
        },
        notify: (event) => {
          if (event.type === "auth_url") out(`Open ${event.url}\n${event.instructions ?? ""}`.trim());
          else if (event.type === "info" || event.type === "progress") out(event.message);
        },
      },
      { getDeviceId: () => deviceId(authFile) },
    );
  } finally {
    rl.close();
  }
  out("Logged in; the credential is stored and refreshes automatically.");
}

export type AuthState = "ok" | "missing" | "error";

/** Whether a usable credential exists; refreshes it if it has expired. */
export async function authState(models: Models): Promise<{ state: AuthState; detail?: string }> {
  try {
    const auth = await models.getAuth(PROVIDER);
    if (!auth) return { state: "missing", detail: "no credential stored; run `login`" };
    // The provider also accepts OPENAI_API_KEY, which would bill the API instead of the subscription.
    if (auth.source !== "OAuth") return { state: "error", detail: `expected the ChatGPT subscription, got ${auth.source}` };
    return { state: "ok" };
  } catch (e) {
    return { state: "error", detail: e instanceof Error ? e.message : String(e) };
  }
}

/** Refuse to run unless requests would bill the ChatGPT subscription; called before every paper. */
export async function requireSubscription(models: Models): Promise<void> {
  const { state, detail } = await authState(models);
  if (state !== "ok") throw new Error(`not running on the ChatGPT subscription: ${detail ?? state}`);
}
