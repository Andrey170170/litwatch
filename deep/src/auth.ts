/**
 * The Codex subscription credential: one JSON file on the state volume, refreshed in place by
 * pi-ai under a per-provider lock. Login is the device-code flow so the server needs no browser.
 */

import { mkdirSync, readFileSync, renameSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";
import {
  createModels,
  type AuthOperationOptions,
  type Credential,
  type CredentialInfo,
  type CredentialStore,
  type Models,
} from "@earendil-works/pi-ai";
import { openaiCodexProvider } from "@earendil-works/pi-ai/providers/openai-codex";

export const PROVIDER = "openai-codex";

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
  models.setProvider(openaiCodexProvider());
  return models;
}

/** Device-code login: prints the URL and code, waits for approval, persists the credential. */
export async function loginDeviceCode(models: Models, out: (line: string) => void): Promise<void> {
  await models.login(PROVIDER, "oauth", {
    prompt: async (p) => {
      if (p.type === "select") return "device_code";
      throw new Error(`unexpected login prompt: ${p.type}`);
    },
    notify: (event) => {
      if (event.type === "device_code") {
        out(`Open ${event.verificationUri} and enter the code: ${event.userCode}`);
        out(`(waiting up to ${Math.round((event.expiresInSeconds ?? 900) / 60)} minutes)`);
      } else if (event.type === "info" || event.type === "progress") {
        out(event.message);
      } else if (event.type === "auth_url") {
        out(`${event.url} ${event.instructions ?? ""}`.trim());
      }
    },
  });
  out("Logged in; the credential is stored and refreshes automatically.");
}

export type AuthState = "ok" | "missing" | "error";

/** Whether a usable credential exists; refreshes it if it has expired. */
export async function authState(models: Models): Promise<{ state: AuthState; detail?: string }> {
  try {
    const auth = await models.getAuth(PROVIDER);
    return auth ? { state: "ok" } : { state: "missing", detail: "no credential stored; run `login`" };
  } catch (e) {
    return { state: "error", detail: e instanceof Error ? e.message : String(e) };
  }
}
