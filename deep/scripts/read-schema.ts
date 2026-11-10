/**
 * Writes the Deep read's schema (`src/read.ts`) as JSON Schema for the web app, whose types are generated from it
 * (`pnpm generate:read` in `web/`). CI runs both and fails when a committed file is stale.
 */
import { writeFileSync } from "node:fs";
import path from "node:path";
import { ReadSchema } from "../src/read.ts";

const out = path.resolve(import.meta.dirname, "../../web/src/deepread/read.schema.json");
writeFileSync(out, `${JSON.stringify(ReadSchema, null, 2)}\n`);
console.log(`wrote ${out}`);
