/**
 * The typed client over `openapi.json` (regenerate with `pnpm generate`). Every call rides on
 * the session cookie; a 401 anywhere is handed to `onUnauthorised`.
 */
import createClient, { type Middleware } from "openapi-fetch";
import type { paths, components } from "./schema";

export type Schemas = components["schemas"];

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

let unauthorised: () => void = () => {};
export function onUnauthorised(handler: () => void) {
  unauthorised = handler;
}

const auth: Middleware = {
  async onResponse({ response }) {
    if (response.status === 401) unauthorised();
    return response;
  },
};

// fetch is looked up per call rather than captured here, so tests can script it.
export const api = createClient<paths>({ baseUrl: globalThis.location?.origin ?? "/", credentials: "same-origin", fetch: (request) => globalThis.fetch(request) });
api.use(auth);

/** Unwrap an openapi-fetch result, throwing `ApiError` on any failure. */
export function ok<T>(result: { data?: T; error?: unknown; response: Response }): T {
  if (result.error !== undefined || !result.response.ok) {
    const detail = (result.error as { detail?: unknown } | undefined)?.detail;
    const text = typeof detail === "string" ? detail : result.response.statusText || "request failed";
    throw new ApiError(result.response.status, text);
  }
  return result.data as T;
}

export async function signIn(token: string): Promise<boolean> {
  const r = await api.POST("/api/session", { body: { token } });
  return r.response.ok;
}
