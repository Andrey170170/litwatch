import assert from "node:assert/strict";
import { test } from "node:test";
import type { Models } from "@earendil-works/pi-ai";
import { requireSubscription } from "../src/auth.ts";

const resolving = (auth: unknown) => ({ getAuth: async () => auth }) as unknown as Models;

test("papers run only when auth resolves to the ChatGPT subscription", async () => {
  await requireSubscription(resolving({ auth: { apiKey: "t" }, source: "OAuth" }));
  await assert.rejects(requireSubscription(resolving(undefined)), /run `login`/);
  await assert.rejects(
    requireSubscription(resolving({ auth: { apiKey: "sk" }, source: "OPENAI_API_KEY" })),
    /expected the ChatGPT subscription/,
  );
});
