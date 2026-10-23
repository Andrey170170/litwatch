# Codebase review (2026-10-21): what to improve after v1

Status (2026-10-21): findings of a read-only review of `eb9a401`, triaged with the owner. All of it is worth doing,
after v1's plan ends (a few more stable nights, Zotero and WebDAV on, 0.14 removed; about the end of that week).
Nothing here is shaped yet. What concerns the archive is in
[archive-fold-and-search.md](archive-fold-and-search.md), since the fold may start earlier.

The review looked for improvement points, not bugs: dead code, history that now hurts, code written to work first and
due a better pass. Five reviewers split the tree (Python foundations, Python pipeline and API, the archive, the web
app, everything else); each claim cites code, and the top ones were checked by hand.

## Small, one PR each

- **`.dockerignore`** misses `.env.v1` (it holds secrets), `deep/node_modules` (149 MiB) and nested `.venv`s, all sent
  as build context; the final image does not copy them.
- **Deep is still registered with the event dispatcher** (`api/app.py:70`) though its `wants()` is always false and
  `handle()` empty since its work moved to the full-text queue (`b899029`): a connection, a cursor and a 5 s poll for
  nothing, and an integration lag shown for it. The dispatcher also owns its `aclose()`, which must move with it.
- **The pre-OAI arXiv transport is dead:** `_ArxivSession`, `_ArxivClient`, `search`, `window_query`, `fetch_window`
  in `intake/search.py`, and `oai.fetch_versions`; only tests call them. `fetch_by_ids`, `id_from_url` and
  `NoAnswerError` stay.
- **`usePaperBusy`** (`web/src/api/mutations.ts:73`) subscribes every Star to every pending write: 1,000 renders when
  one write starts and 1,000 when it ends, reproduced. Filter by paper id inside the subscription.
- **Queries ignore React Query's `signal`** (`web/src/api/queries.ts`), so an outdated search keeps its four shelf
  requests and the archive request running. The search project wants older requests cancelled anyway.
- **`/summary`** (`api/summary.py:22,36`) sums the month's spend twice and loads full split verdicts, calibration
  aggregates and integration cards to return a few counts; the shell polls it every minute.
- **Calibrate** reads each topic's 30-day pool twice (`feedback/calibrate.py:43`), once per side, filtering in Python.
- **`httpx2`** is imported by both provider adapters but not declared; it arrives through the SDKs.
- **Web scaffold leftovers:** `class-variance-authority`, `clsx`, `tailwind-merge`, `tw-animate-css`; `WEIGHT_NAME`,
  `weightFloor`, `clearSnapshots`; the `.oldrev` style.
- **Stale guidance:** `docs/agents/verification.md` speaks of "the replay N2 builds", "Digests arrive in N4", the pages
  "W1 built"; the spec README says v1 is being built; N9 still says MIT licence (it is Apache-2.0 since `e4bef49`) and
  that `evals/` leaves the repository, though current commands read their labels, preferences and configs from it.

## Projects, roughly by value

1. **Settling a paper is expensive.** About twenty statements for an unchanged paper; it loads topics, then the whole
   taxonomy with topics again, then every historical verdict (`feedback/word.py:156,247,346`). A taxonomy edit settles
   per changed topic, so a paper under several is settled several times, under the exclusive taxonomy lock. One
   taxonomy snapshot per transaction, current verdicts only, affected ids united first, then bulk reads for the
   nightly reconciliation; the lock order and events unchanged.
2. **Frontend polish and speed.** The owner's own notices first: switching quickly to a topic tab or a paper not loaded
   before, elements jump while it loads (under a second, but visible); collect the rest as they come. From the review:
   - every loaded row stays mounted and each page re-renders the whole list; phone navigation clones the screen and
     keeps up to eight snapshots (`Shelves.tsx:44,59`, `lib/snapshots.ts`). Windowed rendering, done properly, with
     scroll restoration and paper order kept;
   - the four shelves each recompute list-wide rarity counts, folded shelves included, and so does every "load 50
     more" (`store/reading.py:308`, `api/reading.py:748`): one metadata request, then lean pages;
   - Save and publication edits invalidate every cached digest and the summary; give each mutation its own effects;
   - integration Retry fires refreshes at 0, 3, 10 and 30 s regardless, stacking on repeat (`mutations.ts:540`);
   - the app is one 554 kB chunk (169 kB gzip), Settings included, through `Shell`'s import of `SettingsSide`.
3. **Contracts across boundaries.**
   - Quality, Runs and Calibrate responses are untyped in OpenAPI, so the web app keeps handwritten shapes behind
     `as unknown as` (`queries.ts:184`, `lib/settings.ts:49`); the Deep card likewise (`DeepCard.tsx:43`).
   - `whoseFit` (`TopicRow.tsx:52`) reconstructs where an owner's word came from; the API should say.
   - Python and deep declare each other's types by hand (`deep/src/jobs.ts`, `integrations/deep.py:46,222`;
     `PaperRow` in `deep/src/hosts.ts`), and the two test fakes of deep drift from the service.
   - Two LaTeX outline parsers, `deep/src/prelude.js:54` and `api/mcp_reading.py:122`, already disagree.
4. **SQL into `store`.** 31 grandfathered places in `tests/test_sql_home.py`: calibrate, misses, intake checkpoints,
   the scheduler's lock, event insertion, BibTeX metadata, the search exclusion. Groundwork for the fold, too.
5. **What is still used: an analysis of its own.** Config options, `llm` roles, evaluation tables and cohorts,
   migrations-era guards, API routes with only test callers (`GET /api/bibtex`, publication DELETE). The review kept
   them all on static evidence; the owner wants a proper pass against production use before deciding.
6. **Evals as checks.** Separate the maintained inputs in `evals/` (labels, preferences, configs, ladder cases) from
   historical reports (`RESULTS.md` still opens on the retired screener), and settle N9's "evals leave the repository"
   accordingly. Then a performance evaluation of the app that CI can run: API latency on the main pages and search at
   collection size, bundle size, render counts; the search project's labelled query set is one part of it.
7. **Tests where they protect most.** The `change` contract with several papers: overlapping sets in reverse order
   across two connections without deadlock, and rollback of the first paper's settlement when the second fails.
   Shared helpers out of `test_*` modules (about ten import from each other) into support modules.
8. **Smaller wins.**
   - Intake updates unchanged rows on the three-day overlap (`store/papers.py:108`); compare the resolved values.
   - Zotero does three parent GETs and two children GETs per unchanged item, on each of four event kinds.
   - The classifier and Brief harnesses can't resume past a half-written last line; `quality/ladders.py:289` can.
   - Release CI checks less than PR CI (no archive, web or stack); one reusable workflow, run on the tag.
   - Paper's class and text correction forms duplicate Needs you's and lack its safeguards (taxonomy errors, dirty
     state, revision changes under a draft).

## Looked at and left

- One framework over the LLM steps: `Gateway` already owns retries, batching, journalling and fallbacks.
- Notion as legacy: it is a live export.
- Folding deep into Python: Pi, ChatGPT OAuth, `isolated-vm` and long agent jobs keep it a Node service.
- Batching DBLP: its pacing follows the source's terms.
- Replacing polling with an event stream: summary, today's run and one open paper poll within bounds.
