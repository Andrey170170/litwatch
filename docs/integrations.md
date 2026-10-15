# Integrations

An integration is anything outside litwatch's core that reacts to what happens to papers:
keeping a Zotero library in step, storing PDFs, asking for deep cards, notifying a phone,
exporting to Notion. litwatch works fully with none of them. Each is one Python module, off
unless configured, and a new one needs no change anywhere else in litwatch.

This page is everything needed to write one: the two contracts, the events and their payloads,
what an integration may read, how it is configured and loaded, and a worked example.

## How it works

Every pipeline step writes an **event** to the **outbox** (a table) in the same transaction as
the data the event describes, so an event exists exactly when its change does. A
**dispatcher** runs inside `litwatch serve` and hands each loaded integration the events after
its own **cursor**, in order:

- The order is the order of transactions, then of events within one: an event's place is its
  writing transaction's id, then its own id. Only events whose transactions are older than every
  transaction still running are handed over, so an event that commits late is never passed
  over. A long-open transaction anywhere on the database server delays delivery until it ends;
  nothing is lost.
- An integration enabled for the first time starts at the outbox's current end: it is not woken
  by anything that happened before it was turned on. Turning it off keeps its cursor, and
  turning it on again resumes from there.
- When `handle` raises, the cursor stays before that event, the error is recorded, and the event
  is tried again after a backoff (30 seconds, doubling, at most an hour). That integration's
  later events wait behind it; every other integration goes on. The owner can retry the event
  at once or skip it, under Settings → Integrations (`POST /api/settings/integrations/<name>/retry`
  and `/skip`).
- After a failure or a restart, the event in hand is handed over again. **`handle` must be
  idempotent.**

Delivery is therefore **at least once, and in transaction order, not commit order**: of two
transactions, the one that began first is delivered first even if it committed second, and an
event may arrive twice. A handler should not rely on events arriving in the order their changes
took effect. Read the paper's current state when handling an event, through the store (or
litwatch's API, for a webhook receiver), and do not trust a payload's `before` and `after` to be
the latest: a later change may already be in place. Every built-in integration, and the n8n
workflow, brings its target in line with the paper as it is now, so order does not change the
result.

## The `Integration` contract

Defined in `litwatch.events`:

```python
class Integration(Protocol):
    name: str          # unique; also the key of its cursor
    description: str   # one or two sentences for its card under Settings

    def wants(self, event: Event) -> bool: ...
    async def handle(self, conn: psycopg.AsyncConnection, event: Event) -> None: ...
```

- `wants` is a cheap filter on the event alone (no I/O). Events it does not want are passed
  over without calling `handle`.
- `handle` does the work. `conn` is an autocommit async psycopg connection (rows are dicts);
  group writes with `async with conn.transaction():`. The connection is the integration's own
  and serves one event at a time.
- Optional, used when present:
  - `async def setup(self, conn)`: awaited once whenever the dispatcher starts, before any event.
    It must be safe to run again.
  - `async def aclose(self)`: awaited when the service stops. Close HTTP clients here.
  - `requires: tuple[str, ...]`: names of integrations that must be loaded too; loading fails
    otherwise.
  - `makes_cards = True`: it answers `card.requested` by storing a deep card. Only then does the
    paper page offer to request one. Wanting the event is not enough (a webhook may pass it on).
  - `async def idle(self, conn)`: awaited when the integration has no event to be handed, at
    most about once a minute, under the same lock as `handle`, for work no event brings
    (`deep` retries papers still waiting for their text there). A failure is logged and changes
    nothing else; keep it short and bounded, since events wait while it runs.

`Event` is `litwatch.core.Event`:

| Field | Type | Meaning |
| --- | --- | --- |
| `id` | int | Unique, increasing in the order of insertion (not of commit) |
| `type` | str | One of the types below |
| `paper_id` | str or None | The version-stripped arXiv id, e.g. `2610.01234` or `cs/9901001` |
| `payload` | dict | Per type, below |
| `created_at` | datetime | When it was written |
| `xid` | int | The writing transaction's id; `(xid, id)` is the event's place in delivery order |

### Rules

- **Check the present, not the event.** An event says what changed then; by the time you handle
  it the owner may have overruled a grade or unsaved the paper. Anything that acts on a grade
  reads the paper's current effective grade and library state when it handles the event
  (`litwatch.integrations.must_read_and_saved` does exactly that for `must_read`). An overruled
  `must_read`, one a later verdict superseded, or an unsaved paper gets nothing. When an unsaved
  paper is saved again, `paper.saved` arrives, so also listen to it to do what you skipped.
- **Use only `events`, `store` and `core`** from litwatch (and your own HTTP client), plus the
  small helpers in `litwatch.integrations`. Never import `api`, `jobs` or a pipeline package.
- **Keep your own state in your own table.** Read litwatch's data through the `store`
  repositories; do not write to its tables.
- **You may emit events** of your own with `litwatch.events.emit(conn, type, paper_id, **payload)`,
  in the same transaction as the state they describe. Other integrations can subscribe to them
  (`deep` emits `card.ready`, which `zotero` turns into a note).
- **Keep secrets out of logs and errors.** An error message is stored and shown under Settings.

## Events

Every event below carries the paper's id in `paper_id`.

| Type | When | Payload |
| --- | --- | --- |
| `paper.added` | Intake stored a paper new to the collection | `version`: arXiv version or null; `source`: where intake read it (`oai`, `atom`, `archive`) |
| `paper.revised` | A stored paper got a new arXiv version, or its title or abstract was rewritten (by arXiv, or corrected by the owner) | `version`, `previous_version`, `rewritten`: bool, `source`; an owner's correction has `rewritten: true, corrected: true` and no versions or source |
| `paper.classified` | Classification recorded the paper's classes for its current text, by the classifier or by the owner's hand | `revision`: the paper revision; `primary`: the primary class; `classes`: the classes it is now a direct member of, the owner's class labels applied; `by_owner`: bool |
| `gist.written` | A Gist was written for the paper (from the abstract by analysis; from the full text later) | `revision`, `source`: `abstract` or `full_text`, `gist_id` |
| `verdict.recorded` | Grading recorded a verdict for a Focus topic | `topic`, `grade`, `split`: bool, `verdict_id` |
| `grade.changed` | A paper's effective grade for a Focus topic changed, by a verdict or the owner's word | `topic`, `before`, `after`: `must_read`, `relevant`, `adjacent`, `none` or null |
| `paper.saved` | The paper entered the library | none |
| `paper.unsaved` | The paper left the library (its last reason went, or the owner unsaved it) | none |
| `publication.found` | A published record was stored for the paper, by lookup or by the owner | `dblp_key`, `venue`, `year`, `doi`, `by_owner`: bool |
| `card.requested` | The owner asked for a deep card (`POST /api/papers/<id>/card`), whatever the grade | none |
| `card.ready` | The `deep` integration stored a card | `model` |

`grade.changed` is the one to watch for grades; `verdict.recorded` is the pipeline's internal
record and can disagree with what the owner sees. The **effective grade** is the owner's word
where there is one, else the current verdict; every reader-facing surface uses it, and so should
an integration. The **library** is every paper at an effective grade of `relevant` or above for
some Focus topic, plus what the owner saved by hand, minus what the owner unsaved.

## Reading litwatch's data

Through the repositories in `litwatch.store`. Each function takes the connection first.

| Call | Returns |
| --- | --- |
| `store.papers.get(conn, paper_id)` | `core.Paper` (title, abstract, authors, categories, published, url, version, doi, ...) or None |
| `store.papers.body(conn, paper_id)` | `(text, "html" or "pdf")` when the full text was fetched for the paper's current revision, else None |
| `store.word.effective_for(conn, paper_id)` | `{topic slug: core.Grade}` for the Focus topics that have one |
| `store.word.library_row(conn, paper_id)` | a dict with `saved` (bool) and `hand` (`save`, `unsave` or None), or None |
| `store.reading.paper(conn, paper_id)` | what the paper page shows: `saved`, and `topics`, each with `slug` (its class's), `name`, `depth` (`focus`, `area`, `field`) and `grade` |
| `store.publications.found(conn, [paper_id])` | `{paper_id: core.Publication}` for those with a published record |
| `store.taxonomy.topics(conn, core.Status.ACTIVE)` | the active `core.Topic`s, in the owner's order; `store.taxonomy.load(conn)` gives the whole `core.Taxonomy`, class names included |
| `store.integrations.card(conn, paper_id)` | the deep card row (`card`, `model`, `written_at`, `error`) or None |

## Configuration and loading

Integrations are configured in `litwatch.toml`; each section loads one:

```toml
[integrations.zotero]          # a built-in, by its short name
user_id = "1234567"

[integrations.notion]          # several of one kind: name the module with `use`
use = "webhook"
url = "https://n8n.example.org/webhook/litwatch"
events = ["grade.changed", "paper.unsaved"]

[integrations.phone]           # one kept outside litwatch: its module path
use = "my_litwatch_ntfy"
topic = "litwatch-must-read"

[integrations.archive]
enabled = false                # configured, but off; its cursor is kept
```

- The section's name is the integration's name (and its cursor's). `use` names the module: a
  built-in short name, or any importable module by its import path (`my_pkg.notion`); it
  defaults to the section name.
- A section is on unless it says `enabled = false`.
- Everything else in the section is passed to the module as options. **Secrets never go in
  `litwatch.toml`**: read them from the environment, where `NAME_FILE` may name a file holding
  the value (`litwatch.integrations.required_secret("NAME")`).
- The module provides one function:

  ```python
  def load(name: str, options: dict[str, Any], config: litwatch.config.Config) -> Integration
  ```

  It builds the integration, checking its options; raise `ValueError` (or
  `litwatch.integrations.IntegrationError`) for a configuration that cannot work, and
  `litwatch serve` refuses to start with the section named in the message. Helpers:
  `option(options, key, env=...)` (the section, else an environment variable),
  `required(options, key, env=...)`, `required_secret(env)`, and `litwatch.config.secret(env)`
  for an optional secret.

## Search providers

An integration may also answer searches by implementing `litwatch.core.SearchProvider`:

```python
class SearchProvider(Protocol):
    name: str
    def search(self, query: str, filters: SearchFilters) -> list[SearchHit]: ...
```

- `search` is **synchronous**: litwatch runs it in a thread beside its own search, with a timeout
  of a few seconds. A provider that raises or does not answer in time is left out of that
  response, named in its `left_out`, and counted on its card under Settings.
- `SearchFilters` has `year` (filter by published year when set) and `limit`.
- `SearchHit` has `id` (the version-stripped arXiv id), `title`, `authors` (list of names),
  `published` (aware datetime or None) and an optional `score` (higher is better; compared only
  within one provider, for the "best match" sort).
- Hits are merged by arXiv id. Papers the collection holds are shown from the collection; the
  rest form a second band under the collection's results, each row naming the provider that
  found it. Topic and saved filters apply to the collection only, so with either set no
  provider is asked.

The built-in `archive` integration is a search provider.

## The built-in integrations

| Name | Reacts to | Does |
| --- | --- | --- |
| `zotero` | `paper.saved`, `paper.unsaved`, `grade.changed`, `publication.found`, `card.ready` | Keeps a Zotero item per library paper with tags and notes in step; tags it `litwatch:dropped` when it leaves the library and never deletes it |
| `webdav` | `grade.changed` to `must_read`, `paper.saved` | Stores the PDF of a `must_read` library paper for Zotero's file sync (needs `zotero`) |
| `archive` | `paper.added`, `paper.revised` | Registers the paper with arxiv-archive (metadata, PDF and source), unless intake read it from the archive; answers searches. With `register = false` it registers nothing and only answers searches and serves PDFs, for running beside another litwatch that already registers the same papers |
| `deep` | `grade.changed` to `must_read`, `paper.saved`, `card.requested` | Asks the deep service for a reading card; emits `card.ready` |
| `webhook` | any, by pattern | POSTs the event as JSON to a URL |

Each module's docstring lists its options and environment variables.

### The webhook's request

`POST <url>` with `Content-Type: application/json` and the event as the body:

```json
{"id": 4512, "type": "grade.changed", "paper_id": "2610.01234",
 "payload": {"topic": "cot-faithfulness", "before": "adjacent", "after": "relevant"},
 "created_at": "2026-10-12T06:41:09.312Z", "xid": 508211}
```

Headers: `X-Litwatch-Event: <id>`, so a receiver can drop a repeat; with `secret_env` set, also
`X-Litwatch-Signature: sha256=<hex HMAC-SHA256 of the raw body, keyed by the secret>`. Any answer
other than 2xx is a failure and the event is sent again after the backoff. The receiver reads
anything else it needs from litwatch's API with the shared token, e.g. `GET /api/papers/<id>`.
`integrations/n8n/` has a workflow that exports Watch papers to Notion this way. Answer only
once the work is done: litwatch sends a webhook's events one at a time and waits for each
answer, so answering early turns a failed write into a lost event.

### The changes feed

A client that would rather poll than take webhooks reads `GET /api/events?after=<position>`
(with the shared token). It returns `{"events": [...], "next": "<position>"}`: the settled events
after the position, in transaction order, up to `limit` (100, at most 1000). Start without `after`,
then pass each answer's `next`. A position is opaque; one that is not a position answers 422.

## Worked example: a phone notification for every must-read paper

[ntfy](https://ntfy.sh) pushes a message to a phone when something POSTs to a topic URL. This
integration sends one when a paper becomes `must_read` and is in the library. It lives outside
the litwatch repository, in a module of its own, say `my_litwatch_ntfy.py` on the Python path of
the service:

```python
"""Pushes a phone notification, through ntfy, when a paper becomes must-read."""

from __future__ import annotations

from typing import Any

import httpx
import psycopg

from litwatch import events
from litwatch.config import Config, secret
from litwatch.core import Event
from litwatch.integrations import must_read_and_saved, option, required
from litwatch.store import papers


class Ntfy:
    description = "Pushes a notification to a phone when a paper becomes must-read."

    def __init__(self, name: str, url: str, token: str | None = None) -> None:
        self.name = name
        self.url = url
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self.http = httpx.AsyncClient(timeout=20.0, headers=headers)

    def wants(self, event: Event) -> bool:
        # Cheap, no I/O. paper.saved too: a paper saved again gets what it was denied.
        return event.type == events.PAPER_SAVED or (
            event.type == events.GRADE_CHANGED and event.payload.get("after") == "must_read"
        )

    async def handle(self, conn: psycopg.AsyncConnection, event: Event) -> None:
        # Check the present: the owner may have overruled the grade or unsaved the paper.
        if not await must_read_and_saved(conn, event.paper_id):
            return
        paper = await papers.get(conn, event.paper_id)
        resp = await self.http.post(
            self.url,
            content=paper.title.encode(),
            headers={"Title": "Must read", "Click": f"https://arxiv.org/abs/{paper.id}"},
        )
        resp.raise_for_status()  # raising makes the dispatcher retry later

    async def aclose(self) -> None:
        await self.http.aclose()


def load(name: str, options: dict[str, Any], config: Config) -> Ntfy:
    topic = required(options, "topic")
    server = option(options, "server") or "https://ntfy.sh"
    # An access token, if the server wants one, is a secret: from the environment only.
    return Ntfy(name, f"{server.rstrip('/')}/{topic}", token=secret("NTFY_TOKEN"))
```

Turn it on in `litwatch.toml` and restart `litwatch serve`:

```toml
[integrations.phone]
use = "my_litwatch_ntfy"
topic = "litwatch-must-read"
```

Its card appears under Settings → Integrations. It starts at the outbox's end, so it notifies
about papers that become must-read from now on, not about the library as it already is.

Notes on the example:

- `paper.saved` arrives for every paper entering the library, most of them `relevant`;
  `must_read_and_saved` filters them out. A paper that was already `must_read` and is saved again
  is notified again, which is what "do what was skipped" means here.
- After a failure the event comes again, so a phone may get one notification twice; for a
  notification that is acceptable. Where a repeat would do harm, keep a table of what was done,
  keyed by paper (create it in `setup` with `CREATE TABLE IF NOT EXISTS`), and check it first.
- To test it, give it an `httpx.AsyncClient(transport=httpx.MockTransport(...))` instead of a
  real one, so no test reaches the service.

## Checklist for a new integration

1. One module with a class implementing `name`, `description`, `wants` and `handle`, and a
   `load(name, options, config)` function.
2. `handle` checks the present state, is idempotent, and raises only for something worth
   retrying.
3. Secrets from the environment; options validated in `load`.
4. Close clients in `aclose`.
5. A `[integrations.<name>]` section with `use = "<your.module>"`.
