# litwatch

Insurance against missing a paper on a research question you watch. Every new arXiv paper in
the configured categories is stored, sorted into classes and judged against your topics, and
the results are served through litwatch's own web app.

This is v1, which replaced release 0.14 on 2026-10-17. The specification is in `docs/specs/v1/`
and the terms in `GLOSSARY.md`. The nightly run is intake, classification, analysis, grading, the
full-text queue and the digests; the web app serves the papers, the topics, a paper's page and the
digests.

## Running it

`deploy/` holds the stack: litwatch, its Postgres, the archive and, optionally, the deep service, with
`docker compose` (see `deploy/README.md`). The setup below is for development.

## Setup

```sh
uv sync --extra dev
export LITWATCH_DATABASE_URL=postgresql://…    # or put these in .env
export ANTHROPIC_API_KEY=… OPENROUTER_API_KEY=…
litwatch db migrate
litwatch taxonomy import                       # the seed tree and topics, or a YAML file of your own
```

Models, categories and the schedule are set in `litwatch.toml` (see `docs/specs/v1/data.md`,
configuration); with no file the defaults apply. `litwatch ping judge` sends one small request
through a role.

## Commands

```sh
litwatch nightly                     # the nightly run: intake, classification, analysis, grading
litwatch nightly --no-fetch --limit 20   # classify, analyse and grade a few stored papers, no intake
litwatch nightly --no-fetch --no-digests # the restart: all but the wait and today's digests
litwatch intake --days 1             # intake alone
litwatch intake archive --days 14    # a window's papers read from the archive instead of arXiv
litwatch digests write --since … --until …  # a window's dailies and weeklies (the restart)
litwatch eval classify jev | classify-report jev    # the classifier comparison (candidates in evals/configs/classify.toml)
litwatch eval replay                 # labelled and missed papers through classification and grading; shows the cost, asks
litwatch eval replay --resume 7      # go on with replay 7 where it stopped or failed
litwatch eval report [7] --out evals/….md   # a replay's four recall numbers per cohort and Focus topic, no calls
litwatch serve                       # the API and web app; needs LITWATCH_API_TOKEN
litwatch taxonomy check [PATH]       # validate a taxonomy file (default: the seed)
litwatch taxonomy import | export    # the taxonomy in the database, as YAML in and out
litwatch library sync                # settle effective grades, rarity and the library again
litwatch publications                # look up published versions
```

`LITWATCH_READ_TOKEN`, when set, is a second token that may only search and read papers
(`GET /api/papers` and `/api/papers/{id}`); the deep service uses it.

Evaluation runs never write to verdicts or the outbox. Results are in `evals/`. A replay reads its
papers from the database (labelled papers it lacks are left out and counted) and writes only
`eval_runs`, `eval_labels` and `eval_results`, plus the run record and call journal that carry its
cost; `--cohort` limits it to cohorts, and `--negatives COHORT=N` samples a cohort's negatives with
a fixed seed (positives are always all replayed, since every recall number is over them).

## Tests

```sh
TEST_DATABASE_URL=postgresql://…/disposable uv run pytest
```

Tests that need PostgreSQL create and drop their own schema and are skipped without the
variable. Never point it at a database you care about.

## License

Apache License 2.0; see `LICENSE`.
