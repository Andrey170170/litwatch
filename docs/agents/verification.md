# Verification method

Three layers. **Checks** run in CI on every PR and prove the code holds together. **End-to-end runs** are done by
the agent on each change: the real app, on real data, driven the way the owner uses it. **The quality harness**
measures what the models write and decide against the owner's recorded judgments. What only weeks of use can show
(an interaction that works but doesn't feel right) is left to the owner's use and is not chased here.

The commands below assume the dev setup in [dev-host.md](dev-host.md).

## Kinds of change

### Pipeline and core logic (store, jobs, classification and grading flow, digest assembly)
- Works means: the step does what the spec says, on real Postgres, and leaves the records the spec names.
- Evidence: a pytest that fails before the change and passes after; for a change to what a night produces, an
  end-to-end run of the affected step on the sandbox over a stated set of papers, with the run record.
- How: `uv run pytest --strict-markers tests/<file>`; on the sandbox, `litwatch <job> …` with the papers or window
  named in the claim, then the result read back through the API or the run record.

### What the models write or decide (prompts, request shapes, roles, models, thresholds)
- Works means: quality holds or improves on the gold set at a known cost.
- Evidence: the quality harness before and after on the same gold set; for classification and grading,
  `litwatch eval replay` and `litwatch eval report` against the labels (recall per Focus class, cost per call). A report
  in `evals/` is committed when it sets a default or records a milestone check.
- How: the harness command for the artifact (built with the milestone that introduces it, see Known gaps).

### Model providers (`llm/`)
- Works means: real replies still parse into the request shape, and cost lands in the journal.
- Evidence: tests against faked SDK objects (`tests/fakes.py`), plus one real call per changed provider.
- How: `uv run pytest tests/test_llm.py`; `litwatch ping <role>`.

### API
- Works means: endpoints answer as specified, and the web client's types match.
- Evidence: pytest through the app; the regenerated `web/openapi.json` and a passing web type check.
- How: `uv run pytest tests/test_api*.py`; `uv run litwatch openapi web/openapi.json`, `pnpm generate`,
  `pnpm typecheck` in `web/`.

### Web app
- Works means: the affected flow does what `docs/specs/v1/DESIGN.md` says, on both widths, on every surface it
  reaches.
- Evidence: logic in vitest; the flow walked in the real app on the sandbox through T3's browser panel
  (`preview_*` tools), with before and after screenshots at phone (390 px) and desktop (1440 px) widths, and a short
  recording where motion or timing is the point. Walk every surface that applies and say which did:
  - every page that shows the thing: digest, topic page, paper page, search, Settings;
  - the way back from every new action: save and unsave, a reaction and its undo;
  - empty and quiet states: a quiet day, a topic with nothing new, no search results;
  - dark and light;
  - integrations the action triggers.
- How: `litwatch serve --port 8766` on the sandbox and `pnpm dev` in `web/`; open with `preview_open` and drive
  with the preview tools. If T3's browser panel is unavailable, report that and stop the visual check.

### Schema and data migrations
- Works means: a fresh database migrates, and existing data comes through intact.
- Evidence: pytest migrating a fresh schema; a data migration also runs on a fresh sandbox copy, with counts
  before and after and a check that nothing the owner touched (labels, reactions, saved papers, Zotero items, Deep
  cards) is lost.
- How: refresh the sandbox, run the migration there, count with SQL before and after.

### Integrations (Zotero, WebDAV, deep, archive, webhook)
- Works means: an event produces the right external effect, and a failure is retried and visible.
- Evidence: tests against faked HTTP (`tests/fakes_http.py`); a real run against a test target: Zotero against
  the test account, never the owner's library; deep against a dev instance of `deep/` started for the run on its
  own port (a card request is a real, paid job); the archive's search against the live archive with
  `register = false`. Registration and PDF fetches write to the archive (a fetch queues a job), so they run only
  against a disposable archive.
- How: integration configured in a dev `litwatch.toml` on the sandbox; the dispatcher run; the target inspected.

### Deep-card service (`deep/`)
- Works means: a paper submitted gets a card back with the lab's fields filled from the paper.
- Evidence: `npm test` and `npm run typecheck`; for a change to the agent or its prompt, cards on the gold set
  scored by the quality harness.
- How: in `deep/`, `npm test && npm run typecheck`.

### Stack and container
- Works means: the image builds and runs, and a fresh stack starts and serves.
- Evidence: CI's container smoke test; the stack smoke test once it exists.
- How: CI.

### Specs and docs
- Works means: an implementer reading it would build the intended thing.
- Evidence: one spec review round, as `git-workflow` says. No runtime evidence.

## Acceptable evidence

- **Real Postgres, always.** SQL is never mocked; tests use `TEST_DATABASE_URL`.
- **Fakes stand in for model providers and HTTP services in tests.** A claim about what a model writes or decides
  is shown by the quality harness or an evaluation, never by a fake.
- **End-to-end runs use the sandbox**, a one-way copy of the v1 deployment's database with the scheduler and
  integrations off. Data flows in only. An empty database is a bad test; hand-written rows are for visual states
  (long titles, odd counts) only, never to claim that behavior works.
- **The quality harness** scores an artifact (Gist, Brief, digest, Deep card, classification) on a gold set:
  papers with the owner's recorded judgments, the **preference set**. Each entry holds the key facts that must
  survive, the claims that must not appear, and ratings of earlier outputs. Scoring is rule checks (length,
  objective voice, the overstatement line), a judge model from a different lab than the writer (GPT-6.1-Sol for an
  Anthropic writer) checking coverage and faithfulness against the full text, and a pairwise comparison with the
  last approved output. Every hand check the owner does in a milestone is added to the preference set.
- **Owner checks.** A claim only the owner can judge (a milestone check that says "read by the owner") is
  prepared by the agent and listed in the PR as pending; the PR does not merge until the owner has done it.
- **Spending.** Model calls for evidence may cost up to $5 per change without asking; above that, ask first.
- **Never touched**: production 0.14, the v1 deployment's database (it is only ever read, to refresh the
  sandbox) and its deep service, and the owner's Zotero library.

## Runtime evidence

Screenshots, recordings and command output go in the final report in the T3 thread; the PR body lists each claim
and how it was shown. Committed: `evals/` reports that set a default or record a milestone check, and the
preference set.

## Known gaps

- **The sandbox** has no refresh script yet; `web_demo` on the dev Postgres stands in. Closing it: a script that
  restores a read-only dump of the v1 deployment into a fresh database. Next, before N0.
- **The quality harness and the preference set** don't exist yet. Each arrives with the milestone that introduces
  what it measures: classification and Gists in N1–N2, Briefs in N3, digests in N4, Deep cards with the first
  change to `deep/`.
- **The stack smoke test** (a fresh stack on a seeded database built from real papers, headless Playwright visiting
  every page at both widths, failing on console errors, 5xx or an empty page, screenshots kept as CI artifacts)
  comes with N5, when the archive joins the stack; its page list grows with N7.
- **A disposable archive** for registration and PDF fetches comes with N5's fresh stack; until then those are
  shown by faked HTTP only.
