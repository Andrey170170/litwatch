# Installing litwatch

This guide sets up a fresh litwatch with Docker Compose, from an empty database to a signed-in web app and a first
nightly run. Do the steps in order; each ends with a check, and the next step assumes it passed. Every command runs
from `deploy/` in the checkout unless it says otherwise. The terms are those of [`GLOSSARY.md`](../GLOSSARY.md).

## What you get and what it needs

The stack (`deploy/compose.yaml`) runs:

- `litwatch`: the service. It serves the web app and the API, runs the nightly run on a schedule (intake,
  classification, analysis, grading, the full-text queue, digests) and drives the integrations.
- `db`: its PostgreSQL.
- `archive`, `archive-worker`, `archive-indexer`: the archive's API, the one process that downloads from arXiv, and
  its search indexer ([`docs/specs/v1/archive.md`](specs/v1/archive.md)).
- `deep`, optional, under the `deep` profile: the service that writes Deep cards.

It needs:

- **Docker with the Compose plugin** (`docker compose version` answers). The first build fetches Node, Python and
  their packages, and takes a few minutes.
- **Disk for the archive**, which grows every night. It keeps the LaTeX text of every paper intake stores and, by
  default, only the PDFs litwatch wants (papers graded relevant or above, saved, or opened). Keeping every PDF is about
  4 GB a day for three busy categories. The indexer stops when less than 1 GB is free.
- **Model provider keys.** With the default models both are needed:

  | Provider | Roles on it by default |
  | --- | --- |
  | Anthropic (`ANTHROPIC_API_KEY`) | `analyse`, `judge`, `field_grade`, `brief`, `writer`; Sonnet fallbacks of `judge` and `writer` |
  | OpenRouter (`OPENROUTER_API_KEY`) | `classify`, `area_check`, `check`; the other fallbacks of `analyse`, `judge`, `brief`, `writer` |

  Without the OpenRouter key nothing is classified, so nothing goes further. The roles and their defaults are in
  [`docs/specs/v1/models.md`](specs/v1/models.md); moving a role to the other provider is configuration (step 3).
  The default three categories bring 700 to 1,550 papers on a weekday; measured costs are in
  [`docs/specs/v1/pipeline.md`](specs/v1/pipeline.md), section 11.
- **An email address** arXiv can write to (`ARXIV_CONTACT`).
- Optional: a ChatGPT account for Deep cards (the `deep` service signs in with it and runs on its subscription), and
  a Semantic Scholar API key for `deep` (without one its lookups are rate-limited).

## 1. Get the code

```sh
git clone https://github.com/Andrey170170/litwatch.git
cd litwatch/deploy
```

Check: `ls` shows `compose.yaml`, `env.example`, `litwatch.toml` and `README.md`.

## 2. Fill in `.env`

```sh
cp env.example .env
```

Compose reads `.env` beside `compose.yaml`. Fill in:

| Variable | Required | What it is | How to choose it |
| --- | --- | --- | --- |
| `LITWATCH_DB_PASSWORD` | yes | Postgres's password, set when the database is first created | generate (below) |
| `LITWATCH_API_TOKEN` | yes | The web app's and the API's token: the only sign-in | generate |
| `LITWATCH_ARCHIVE_TOKEN` | yes | The token litwatch and the archive share | generate |
| `LITWATCH_READ_TOKEN` | with deep | A second token that may only search and read papers; `deep` uses it for a card's "Among the collection" | generate |
| `LITWATCH_MCP_TOKEN` | no | The MCP server's token, for research agents; unset, `/mcp` is not served (step 10) | generate |
| `ANTHROPIC_API_KEY` | for the nightly run | Anthropic API key | from the Anthropic console |
| `OPENROUTER_API_KEY` | for the nightly run | OpenRouter API key | from openrouter.ai |
| `ARXIV_CONTACT` | yes | An address arXiv can write to, sent by the archive worker's downloads and by `deep`'s fetches | a real, monitored address |
| `LITWATCH_BIND` | no | The host address the web app listens on (`127.0.0.1`) | leave it unless a proxy elsewhere must reach it (step 9) |
| `LITWATCH_PORT` | no | The host port of the web app (`8080`) | any free port |
| `LITWATCH_TOML` | no | Another configuration file (`./litwatch.toml`) | leave it |
| `LITWATCH_TAG` | no | The tag of the images built here (`local`) | leave it |
| `S2_API_KEY` | no | Semantic Scholar key for `deep` | from semanticscholar.org |

Generate each token and the password with:

```sh
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

The stack starts without the model keys; the nightly run's model steps fail without them. Set
`LITWATCH_DB_PASSWORD` before the first start and do not change it afterwards: Postgres keeps the password it was
created with.

Check: `docker compose config --quiet` prints nothing. A missing required variable stops it with
`set <NAME> in .env`.

## 3. Edit `litwatch.toml`

`litwatch.toml` holds everything that is not a secret; anything left out takes its default. The full list of keys
is [`docs/specs/v1/data.md`](specs/v1/data.md), Configuration, and `src/litwatch/config.py`. The ones to decide now:

```toml
[intake]
categories = ["cs.CL", "cs.AI", "cs.LG"]   # the arXiv categories intake reads
# window_days = 3                           # how many days each nightly run looks back

[schedule]
enabled = true            # false: nothing runs on its own; run the nightly by hand
nightly = "0 2 * * *"     # cron, read in `timezone`
timezone = "Europe/Berlin" # an IANA zone; left out, the container's zone, which is UTC
deadline = "07:30"        # the nightly's Anthropic batches (half price) end here; the rest goes as normal calls
# publications = "30 9 * * *"   # the daily DBLP lookup of published versions

[budget]                  # USD a month per provider; over it the status line turns amber, nothing stops
anthropic_monthly = 100
openrouter_monthly = 25

[integrations.archive]    # keep: the archive is part of the stack
```

The file in `deploy/` ships with `timezone = "UTC"`: set your own zone.

Models: each role is a `[models.<role>]` section with `provider` (`anthropic` or `openrouter`), `model`, and
optionally `effort` and `fallbacks`. A role written here replaces its default, fallbacks included; a role left out
keeps its default. The judge's and the check's model lists must share no lab, and only OpenRouter serves a
`decisions = true` model. See [`docs/specs/v1/models.md`](specs/v1/models.md).

Check: the file is valid TOML (with Python 3.11 or later, `python3 -c 'import tomllib; tomllib.load(open("litwatch.toml","rb"))'`). litwatch
validates the keys when it starts (step 4) and refuses an unknown key or a bad cron expression or zone.

## 4. First start

```sh
docker compose up -d --build
```

On start, `litwatch serve` applies the database migrations, then serves the web app.

The scheduler runs a job whose time passed less than six hours ago and has not run since. With `[schedule]
enabled = true`, a first start within six hours after the nightly time starts the first nightly run at once, before
the topics are in. To avoid that, start with `enabled = false` and turn it on after step 5
(`docker compose restart litwatch`).

Check:

```sh
docker compose ps
docker compose logs litwatch | grep -E 'migration|scheduler|Uvicorn'
docker compose exec litwatch litwatch --help
docker compose exec litwatch litwatch db migrate
```

`ps` shows `db` and `litwatch` as `(healthy)` and the archive services as `Up`; `db migrate` answers `applied:
nothing, already up to date`. The log shows `applied migration …` lines, `scheduler on` (or nothing about the scheduler when it is off) and
`Uvicorn running on http://0.0.0.0:8080`. Any `litwatch` command runs as `docker compose exec litwatch litwatch …`.

## 5. Topics

The taxonomy (the class tree every paper is sorted into) and its topics (the classes followed, each at Focus, Area
or Field) live in the database, not in configuration. Nothing is classified until there is one. Decide:

**The seed.** litwatch ships a taxonomy of 58 classes over language models, interpretability, alignment and safety,
the rest of machine learning, and AI and society, with nine topics: three Focus topics (chain-of-thought faithfulness,
chain-of-thought monitoring, circuits and causal analysis), two Area topics, four Field topics
([`docs/specs/v1/taxonomy.md`](specs/v1/taxonomy.md)).

```sh
docker compose exec litwatch litwatch taxonomy import
```

**Your own.** Start from the seed's file (`src/litwatch/seeds/taxonomy.yaml` in the checkout) or from an export, edit
it, check it, then import it (`export` needs a first import). The container cannot see host files, so pass the file on standard input:

```sh
docker compose exec -T litwatch litwatch taxonomy export > taxonomy.yaml
docker compose exec -T litwatch litwatch taxonomy check /dev/stdin < taxonomy.yaml
docker compose exec -T litwatch litwatch taxonomy import /dev/stdin < taxonomy.yaml
```

The file's shape (`src/litwatch/seeds/__init__.py`, `src/litwatch/core/taxonomy.py`):

```yaml
classes:                       # the tree, at most three levels, siblings in your order
  - slug: language-models      # lowercase words joined by hyphens
    name: Language models      # unique; the classifier chooses by the name more than the description
    children:
      - slug: retrieval
        name: Retrieval-augmented generation
        description: Retrieval, knowledge bases, grounding and citation for language models.
        # threshold: 0.6       # optional: this class's own membership threshold
  - slug: other
    name: Other
    description: Papers that fit no other class.
topics:                        # the followed classes, in your order
  - class: language-models     # a followed class has no description: its topic's subject is it
    depth: field               # focus, area or field
    monogram: LM               # one or two characters, for its badge
    colour: '#0284c7'
    subject: >-
      What the topic covers and what it leaves out; the graders read it as the Fit definition.
    # status: active           # active (default), paused, or draft
    # interests: what matters most within it
    # rubric: |-               # required for a Focus topic, refused for the others
    #   - Central: ...
    #   - Related: ...
    #   - Adjacent: ...
    # keywords: [regex, ...]   # the Focus keyword check's patterns
```

Every paper intake stores must fit somewhere, so keep a catch-all class such as `Other`. Importing again makes the
file the taxonomy: a changed class or topic gets a new revision that applies forward, a class left out is retired, a
topic left out unfollowed.

**Later edits.** In the web app, Settings → Topics follows a class, edits a topic (depth, subject, rubric, keywords,
status, badge) and orders the topics. Classes (names, descriptions, thresholds, the tree) change only by a YAML
import; Settings → Taxonomy has Export YAML.

Check: the import prints the counts (`58 classes, 9 topics` for the seed) and the new revisions it made;
`taxonomy export` prints what is now in the database.

## 6. Sign in

Open `http://127.0.0.1:8080` (your `LITWATCH_PORT`) in a browser on the same host, or through an SSH tunnel
(`ssh -L 8080:127.0.0.1:8080 <server>`). Paste `LITWATCH_API_TOKEN` once; the browser keeps it in a cookie for 90
days.

Check: Settings lists the topics and classes you imported. From a shell:

```sh
T=$(grep '^LITWATCH_API_TOKEN=' .env | cut -d= -f2-)
curl -s -H "Authorization: Bearer $T" http://127.0.0.1:8080/api/status
```

answers JSON with `"state": "none"` before the first run; without the token it answers 401.

## 7. Check the model keys

```sh
docker compose exec litwatch litwatch ping check
docker compose exec litwatch litwatch ping judge
```

`ping <role>` sends one small request through a role's model as a chat completion: `analyse`, `judge`, `check`,
`field_grade`, `brief` or `writer`. Ping at least one role per provider; with the defaults `check` is on OpenRouter
and `judge` on Anthropic. Don't ping `classify` or `area_check`: they ask OpenRouter for decisions, which `ping` does
not, so it can fail with good keys.

Check: each prints the model, `-> 42` and the tokens and cost. `CallError: provider: no credentials for …` means
that provider's key is not in the container (Troubleshooting).

## 8. The first night

Choose one:

- **On the schedule.** With `[schedule] enabled = true` the first nightly runs at the `nightly` time. It reads the
  last `window_days` (3) days.
- **By hand, now.** A trial that takes in one day and stops each model step after 20 papers:

  ```sh
  docker compose exec litwatch litwatch nightly --days 1 --limit 20
  ```

  `litwatch nightly --days 1` is the whole night for one day. A run by hand outside the night (after the
  `deadline`) sends normal calls, not half-price batches. Waiting for the full-text passes before the digests can take
  up to `[digests] wait_minutes` (120). `litwatch intake --days 1` takes papers in without any model step.

What to expect: intake reads the categories' new and revised papers from arXiv, paced to one connection every 3
seconds, shared with the archive worker, and registers each with the archive, which then downloads their text. Then
classification, analysis and grading; the full-text queue writes Briefs from the archive's text; the digests are
written last. The command prints the run's id, cost and counts.

Check: Settings → Runs and cost shows the run, and the Digest page shows the day's digests. The archive fills:

```sh
docker compose exec archive paper-archive --no-mount-guard status
```

## 9. Exposure

The web app listens on `127.0.0.1` of the host only. To reach it from elsewhere, put a reverse proxy with TLS in
front of it, at the root of its own host name. The proxy must pass `X-Forwarded-Proto`, so the sign-in cookie is
marked Secure. With Caddy on the same host:

```
litwatch.example.org {
    reverse_proxy 127.0.0.1:8080
}
```

A proxy that is not on the host reaches litwatch at the address `LITWATCH_BIND` names; bind only to an interface
that proxy alone can reach. The token is the only protection: keep it long and keep it out of URLs.

Never route the metrics port (step 11) through a public proxy: it answers without a token.

## 10. Optional parts

Two rules for all of them:

- An integration is a section in `litwatch.toml` and is on unless it says `enabled = false`
  ([`docs/integrations.md`](integrations.md)); `docker compose restart litwatch` applies it. It starts at the
  outbox's end: it acts on what happens from then on. Notion is the exception: switched on, it first fills its
  database with every paper that already qualifies.
- Secrets come from the environment, never from `litwatch.toml`. The compose file passes only its own variables to
  `litwatch`, so add an integration's secrets in `compose.override.yaml` beside `compose.yaml` (Compose reads it
  without being told) and their values to `.env`, then `docker compose up -d`:

  ```yaml
  services:
    litwatch:
      environment:
        ZOTERO_API_KEY: ${ZOTERO_API_KEY}
  ```

Each integration's card is under Settings → Integrations, with its errors.

**Deep cards** (`deep`). The `deep` service reads a paper with a ChatGPT-signed-in model and writes its Deep card;
cards are queued for Focus must reads, Area picks, and papers you ask for. Set `LITWATCH_READ_TOKEN` (and optionally
`S2_API_KEY`) in `.env`, uncomment `[integrations.deep]` in `litwatch.toml`, then:

```sh
docker compose --profile deep build deep
docker compose --profile deep run --rm deep login
docker compose --profile deep up -d
docker compose restart litwatch
```

`login` prints a URL: open it in any browser, sign in with ChatGPT, and paste the final redirect URL back; the
session stays in the `deep-auth` volume. Use `--profile deep` on every later `up` and `build`. Check: the `deep` component of the status document is
healthy, and a paper's page offers a card. Its settings: [`deep/README.md`](../deep/README.md).

**Zotero** (`zotero`). One Zotero item per library paper, with litwatch's tags and notes. `[integrations.zotero]`
with `user_id`; the key in `ZOTERO_API_KEY`. Options: `src/litwatch/integrations/zotero.py`.

**WebDAV** (`webdav`, needs `zotero`). Stores the PDF of each must-read library paper on the WebDAV share Zotero's
file sync reads. `url` and `username` in the section; the password in `WEBDAV_PASSWORD`.

**Notion** (`notion`). Keeps a Notion database of the exported topics' papers, for a reader who does not use
litwatch. `parent_page`, `owner_name`, `minimum_grade`, `[integrations.notion.topics]`; the token of a Notion internal
integration in `NOTION_TOKEN`, with the parent page shared with it. See [`docs/integrations.md`](integrations.md), The
Notion export.

**Webhook** (`use = "webhook"`). POSTs matching events as JSON to a URL, for n8n and the like: `url`, `events`,
`secret_env` (the variable holding the signing secret), `timeout`. A client that polls reads `GET /api/events`
instead. See [`docs/integrations.md`](integrations.md).

**The MCP server.** Research agents (Claude Code, Codex) search the archive and the collection, read what
litwatch made of a paper or wrote in a digest, and read a paper's text and see its PDF's pages, read-only, at `/mcp`
(streamable HTTP, plain JSON answers). A paper the archive has not fetched is asked for at urgent priority, as opening
its PDF in the web app does. Set
`LITWATCH_MCP_TOKEN` in `.env` and `docker compose up -d litwatch`; agents send it as `Authorization: Bearer …`, and
neither the web app's token nor the read token opens it. Behind a proxy, add the host name it is reached by to
`[mcp] allowed_hosts` in `litwatch.toml` (commented there): any other `Host` gets 421. Check, on the host:

```sh
M=$(grep '^LITWATCH_MCP_TOKEN=' .env | cut -d= -f2-)
curl -s -H "Authorization: Bearer $M" -H 'Accept: application/json, text/event-stream' \
  -H 'Content-Type: application/json' -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' \
  http://127.0.0.1:8080/mcp
```

answers JSON listing `search_papers`, `get_paper`, `list_digests`, `read_digest`, `paper_outline`, `read_paper`
and `view_pdf_page`.

**Your own integration.** A Python module named with `use`; [`docs/integrations.md`](integrations.md) is the guide.

## 11. Monitoring

`LITWATCH_METRICS_PORT` has litwatch also serve, without a token, `/metrics` in Prometheus' text format and
`/status` as a `homelab.status/v1` document (the nightly, the pipeline, digests, each integration, each model
provider, the archive, deep, the budget, and the MCP server while it is served; [`docs/specs/v1/data.md`](specs/v1/data.md), Configuration). Set it in
`compose.override.yaml`, publishing the port only where your monitoring reaches it:

```yaml
services:
  litwatch:
    environment:
      LITWATCH_METRICS_PORT: "9108"
    ports:
      - "127.0.0.1:9108:9108"
```

Apply it with `docker compose up -d litwatch`, then check: `curl -s http://127.0.0.1:9108/status`. Before the first
successful nightly its `nightly` component reads degraded. A provider that a configured role uses reads degraded while
its key is missing.

Logs are one JSON object per line (`docker compose logs litwatch`); `LITWATCH_LOG_FORMAT=text` in the environment
gives plain lines.

## 12. Backups

| Volume | Holds | Back it up |
| --- | --- | --- |
| `db` | Postgres: papers, verdicts, digests, the taxonomy, your word, runs, integrations' state | with `pg_dump`, below |
| `archive-work` | the archive's catalog (SQLite) and search index | the catalog; the index rebuilds |
| `archive-files` | the papers' sources and PDFs | optional: large, and arXiv has them again |
| `arxiv-pacing` | the shared arXiv pacing | no |
| `deep-auth` | `deep`'s sign-in | no: sign in again |

Also keep `.env` and `litwatch.toml`. The database:

```sh
docker compose exec -T db pg_dump -U litwatch -Fc litwatch > litwatch-$(date +%F).dump
```

To restore, stop `litwatch` first:

```sh
docker compose stop litwatch
docker compose exec -T db pg_restore -U litwatch -d litwatch --clean --if-exists < FILE
docker compose start litwatch
```

The archive's work volume is copied with its services stopped (`archive-files` the same way, from
`/srv/arxiv-archive`):

```sh
docker compose stop archive archive-worker archive-indexer
docker compose run --rm --no-deps -T --entrypoint tar archive czf - -C /srv/arxiv-work . > archive-work.tgz
docker compose start archive archive-worker archive-indexer
```

## 13. Upgrades

```sh
docker compose exec -T db pg_dump -U litwatch -Fc litwatch > litwatch-before-upgrade.dump
git pull
docker compose up -d --build
```

With deep, the last command is `docker compose --profile deep up -d --build`, so `deep` is rebuilt with litwatch.
`litwatch serve` applies new migrations as it starts. `.env` is ignored by git. `git pull` refuses to overwrite your edited `litwatch.toml` when
the upstream one changed: `git stash`, `git pull`, `git stash pop`, and settle any conflict. See what changed in
`deploy/` before pulling (`git fetch`, then `git diff HEAD origin/main -- deploy/`) and bring `.env` in line too.

Check: `docker compose ps` shows `litwatch` healthy and the log lists the migrations applied.

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `set LITWATCH_… in .env` from any `docker compose` command | A required variable is empty (step 2). |
| `litwatch` restarts; the log says a configuration problem | `litwatch.toml` has an unknown key, a bad cron expression or zone, or an integration lacks an option or secret; the message names it. Fix and `docker compose restart litwatch`. |
| `password authentication failed` in the log | `LITWATCH_DB_PASSWORD` changed after the database was created. Put the old one back. |
| `CallError: provider: no credentials for …` | The key is not in the container. Set it in `.env` and run `docker compose up -d`: `restart` does not read `.env` again. |
| `ping` or the run reports an `account` error (401, 402, 403) | The key is revoked or the provider's credits are used up; the status document shows it on `provider-<name>`. |
| The nightly run classified nothing | No taxonomy (`no taxonomy: nothing is classified until one is imported` in the log): step 5. Or no OpenRouter key. |
| The nightly ran at once on the first start | The catch-up of step 4. Harmless; set `enabled = false` for a first start you want to control. |
| A Deep card fails with a sign-in error | The `deep` session expired: `docker compose --profile deep run --rm deep login` again. |
| `archive-indexer` keeps restarting with `Insufficient free SSD space for search indexing.` | Less than 1 GB is free on the disk holding the volumes. |

The specification of everything above is [`docs/specs/v1/`](specs/v1/README.md); the stack's reference is
[`deploy/README.md`](../deploy/README.md).
