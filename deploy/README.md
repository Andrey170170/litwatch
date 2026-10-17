# litwatch's stack

`compose.yaml` runs litwatch (the service, its nightly run and its web app), its Postgres, and the archive: its API,
its acquisition worker and its search indexer ([`docs/specs/v1/archive.md`](../docs/specs/v1/archive.md)). The deep
service, which writes Deep cards, runs under the `deep` profile. A fresh stack starts with an empty database and an
empty archive; intake fills both.

## Setup

From this directory:

```sh
cp env.example .env                # fill in the tokens, the API keys and ARXIV_CONTACT
$EDITOR litwatch.toml              # categories and the schedule
docker compose up -d --build
docker compose exec litwatch litwatch taxonomy import    # the seed tree and topics
```

The web app is at `http://127.0.0.1:8080` (`LITWATCH_BIND`, `LITWATCH_PORT`); sign in with `LITWATCH_API_TOKEN`.
`docker compose exec litwatch litwatch …` runs any other command, such as `intake --days 1` for a first night.

Deep cards: set `LITWATCH_READ_TOKEN` in `.env` (deep searches litwatch with it for a card's "Among the
collection"), uncomment `[integrations.deep]` in `litwatch.toml`, then

```sh
docker compose --profile deep run --rm deep login    # sign in once; the session stays in its volume
docker compose --profile deep up -d
docker compose restart litwatch    # litwatch reads its integrations when it starts
```

## The archive

The archive downloads every paper's files from arXiv, at most one connection every 3 seconds, shared with litwatch's
own arXiv calls. It keeps the LaTeX text of every paper and, by default, only the PDFs litwatch wants: those of papers
graded relevant or above, saved, or opened. The others are fetched when opened. To keep every PDF (about 4 GB a day
for three busy categories):

```sh
docker compose exec archive paper-archive --no-mount-guard pdfs all    # `pdfs wanted` to go back; `pdfs` shows it
docker compose exec archive paper-archive --no-mount-guard status
```

Its data lives in the `archive-work` (catalog, search index) and `archive-files` (sources and PDFs) volumes.

## Monitoring

`LITWATCH_METRICS_PORT` (or `litwatch serve --metrics-port`) has litwatch also serve `/metrics` in Prometheus' text
format and `/status` as a `homelab.status/v1` document on that port, without a token
([`docs/specs/v1/data.md`](../docs/specs/v1/data.md), configuration). Add it to the `litwatch` service's
`environment` and publish the port only on a network your monitoring reaches, never through a public proxy. The image
logs one JSON object per line; `LITWATCH_LOG_FORMAT=text` gives plain lines.
