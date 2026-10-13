# litwatch

Insurance against missing a paper on a research question you watch. Every new arXiv paper in
the configured categories is stored; a cheap model triages each one against your topics, a
keyword audit catches what the model drops, and two models from different labs grade every
candidate for a Watch topic.

This branch is v1 in progress. The design is in `docs/v1/SPEC.md`; milestones 0 to 2 are
built (collection, model gateway, Watch pipeline and its evaluation). The web app, weekly
reviews, published versions and integrations are not. Release 0.14 lives on `main`.

## Setup

```sh
uv sync --extra dev
export LITWATCH_DATABASE_URL=postgresql://…    # or put these in .env
export ANTHROPIC_API_KEY=… OPENROUTER_API_KEY=…
litwatch db migrate
litwatch topics import --starter                # or a directory of your own YAML topics
```

Models, categories and limits are set in `litwatch.toml` (see section 4.4 of the spec); with
no file the defaults apply. `litwatch ping judge` sends one small request through a role.

## Commands

```sh
litwatch nightly                     # intake, triage with the keyword audit, grading
litwatch intake --days 1             # intake alone
litwatch mark-triaged                # count stored papers as triaged, so runs start from new ones
litwatch topics list | import | export
litwatch eval seed | replay | sample-rejects | report    # dry runs against your labels
litwatch serve                       # the API and web app; needs LITWATCH_API_TOKEN
```

`LITWATCH_READ_TOKEN`, when set, is a second token that may only search and read papers
(`GET /api/papers` and `/api/papers/{id}`); the deep service uses it.

Evaluation runs never write to candidates, verdicts or the outbox. Results are in `evals/`.

## Tests

```sh
TEST_DATABASE_URL=postgresql://…/disposable uv run pytest
```

Tests that need PostgreSQL create and drop their own schema and are skipped without the
variable. Never point it at a database you care about.
