/**
 * The typed client over `openapi.json` (regenerate with `pnpm generate`). Every call rides on
 * the session cookie; a 401 anywhere surfaces as the token prompt, through `onUnauthorised`.
 */
import createClient, { type Middleware } from "openapi-fetch";
import type { paths, components } from "./schema";

export type Schemas = components["schemas"];
export type Status = Schemas["Status"];
export type Digest = Schemas["Digest"];
export type Issue = Schemas["Issue"];
export type IssueSummary = Schemas["IssueSummary"];
export type Section = Schemas["Section"];
export type Entry = Schemas["Entry"];
export type Paper = Schemas["Paper"];
export type PaperTopic = Schemas["PaperTopic"];
export type Chosen = Schemas["Chosen"];
export type Row = Schemas["Row"];
export type Papers = Schemas["Papers"];
export type TopicCard = Schemas["TopicCard"];
export type TopicRef = Schemas["TopicRef"];
export type TopicKind = Schemas["TopicKind"];
export type Grade = Schemas["Grade"];
export type Ack = Schemas["Ack"];
export type ReactionValue = Schemas["ReactionValue"];
export type ReactionReason = Schemas["ReactionReason"];
export type LabelValue = Schemas["LabelValue"];
export type Lookup = Schemas["Lookup"];

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

export const api = createClient<paths>({ baseUrl: "/", credentials: "same-origin" });
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
