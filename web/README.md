# litwatch web

The user side of litwatch, built to `docs/specs/v1/DESIGN.md`: the shell, Papers, Topics and the topic page, and a
paper's page (W1 in `docs/plans/v1-build.md`). Settings, the Digest and the phone's gestures come in later stages;
the shell links to them as placeholders.

- `pnpm dev` runs Vite on 5173 and proxies `/api` to `litwatch serve --port 8766`; set `LITWATCH_API` to
  proxy elsewhere (`LITWATCH_API=http://127.0.0.1:8767 pnpm dev`).
- `pnpm build` writes to `src/litwatch/api/web/`, which the Python service serves at `/`.
- `pnpm generate` regenerates `src/api/schema.d.ts` from `openapi.json`; the backend writes
  that file with `uv run litwatch openapi web/openapi.json`.
- `pnpm typecheck`, `pnpm test`.

## Layout

- `src/api/`: the typed client over `openapi.json`, the query options (one per endpoint, one per shelf of a list)
  and the mutations, which patch what the page can show and mark lists stale without reordering them.
- `src/lib/`: pure helpers the pages share: rarity and grade names and shelves, the status line, a list's search
  params, reasons for a thumb, dates and names.
- `src/components/`: the shell (sidebar, phone header, drawer, slots for the phone's title and bottom bar), rows,
  shelves and list controls; `paper/` holds a paper's page: header, open-state notes, ladder, deep card, topic
  rows, actions and the phone's bars.
- `src/routes/`: one file per page; `src/router.tsx` wires them.
- `src/styles.css`: the look's tokens (light and dark through `light-dark()`, overridable with
  `html[data-theme]`), the faces (self-hosted through Fontsource) and the classes the pages use.

The layout switches from phone to desktop at 56rem (`desk:` in Tailwind, `useDesktop()` in code). Tests run with
vitest and Testing Library on a scripted `fetch` (`src/test/harness.tsx`).
