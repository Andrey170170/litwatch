# litwatch web

The user side of litwatch: the Digest, paper pages, Topics, Papers and the Settings gear
(`docs/v1/DESIGN.md` is the design, `docs/v1/SPEC.md` §4.1 the contract).

- `pnpm dev` runs Vite on 5173 and proxies `/api` to `litwatch serve --port 8766`.
- `pnpm build` writes to `src/litwatch/api/web/`, which the Python service serves at `/`.
- `pnpm generate` regenerates `src/api/schema.d.ts` from `openapi.json`; the backend writes
  that file with `uv run litwatch openapi web/openapi.json`.
- `pnpm typecheck`, `pnpm test`.

The layout switches from phone to desktop at 56rem (`desk:` in Tailwind, `useDesktop()` in
code). Colours and type come from the tokens in `src/styles.css`; dark follows the system.
