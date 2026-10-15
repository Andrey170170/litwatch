# litwatch

Insurance against missing a paper on a research question you watch. Every new arXiv paper in
the configured categories is stored, sorted into classes and judged against your topics, and
the results are served through litwatch's own web app.

This branch is v1 in progress. The specification is in `docs/specs/v1/` and the terms in
`GLOSSARY.md`. The first v1 design's pipeline (triage, takes, reviews and issues) has been taken
out, and the redesigned core (classes, detail levels, digests per depth) is being built in its
place before the switch; until it lands, the nightly run is intake alone and the web app is a
placeholder. Release 0.14 lives on `main`.

## Setup

```sh
uv sync --extra dev
export LITWATCH_DATABASE_URL=postgresql://…    # or put these in .env
export ANTHROPIC_API_KEY=… OPENROUTER_API_KEY=…
litwatch db migrate
litwatch topics import --starter                # or a directory of your own YAML topics
```

Models, categories and the schedule are set in `litwatch.toml` (see `docs/specs/v1/data.md`,
configuration); with no file the defaults apply. `litwatch ping judge` sends one small request
through a role.

## Commands

```sh
litwatch nightly                     # the nightly run: intake, for now
litwatch intake --days 1             # intake alone
litwatch intake archive --days 14    # a window's papers read from the archive instead of arXiv
litwatch topics list | import | export
litwatch eval classify jev | classify-report jev    # the classifier comparison (candidates in evals/configs/classify.toml)
litwatch serve                       # the API and web app; needs LITWATCH_API_TOKEN
litwatch taxonomy check              # load the class tree and its followed topics
litwatch library sync                # settle effective grades and the library again
litwatch publications                # look up published versions
```

`LITWATCH_READ_TOKEN`, when set, is a second token that may only search and read papers
(`GET /api/papers` and `/api/papers/{id}`); the deep service uses it.

Evaluation runs never write to verdicts or the outbox. Results are in `evals/`.

## Tests

```sh
TEST_DATABASE_URL=postgresql://…/disposable uv run pytest
```

Tests that need PostgreSQL create and drop their own schema and are skipped without the
variable. Never point it at a database you care about.
