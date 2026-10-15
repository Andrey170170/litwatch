# litwatch web

The user side of litwatch. For now a placeholder page: the first design's app was removed, and the one in
`docs/specs/v1/DESIGN.md` is built in N7 (`docs/plans/v1-build.md`). The typed API client (`src/api/client.ts`),
the tokens and the build stay.

- `pnpm dev` runs Vite on 5173 and proxies `/api` to `litwatch serve --port 8766`; set `LITWATCH_API` to
  proxy elsewhere (`LITWATCH_API=http://127.0.0.1:8767 pnpm dev`).
- `pnpm build` writes to `src/litwatch/api/web/`, which the Python service serves at `/`.
- `pnpm generate` regenerates `src/api/schema.d.ts` from `openapi.json`; the backend writes
  that file with `uv run litwatch openapi web/openapi.json`.
- `pnpm typecheck`, `pnpm test`.

The layout switches from phone to desktop at 56rem (`desk:` in Tailwind). Colours and type come from
the tokens in `src/styles.css`; dark follows the system.
