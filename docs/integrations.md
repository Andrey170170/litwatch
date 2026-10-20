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
  and `/skip`). A failing `setup` is retried the same way, and Retry tries it again at once; the
  integrations API reports it as `setup_failing`, apart from a failing event.
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
  - `async def setup(self, conn)`: awaited whenever the dispatcher starts, before any event, under
    the same lock as `handle`. It must be safe to run again. When it raises, no event is handed
    over; the error is shown under Settings → Integrations and setup is tried again after the
    same backoff as a failed event (or at once, on Retry).
  - `async def aclose(self)`: awaited when the service stops. Close HTTP clients here.
  - `requires: tuple[str, ...]`: names of integrations that must be loaded too; loading fails
    otherwise.
  - `async def idle(self, conn)`: awaited when the integration has no event to be handed, at
    most about once a minute, under the same lock as `handle`, for work no event brings
    (retrying a delivery that waits on something outside litwatch). A failure is logged and changes
    nothing else; keep it short and bounded, since events wait while it runs.

Deep cards come from the built-in `deep` integration only: the owner's request (`POST
/api/papers/<id>/card`) queues a Deep pass, which the service's Deep worker submits to it, and
only with it loaded does the paper page offer the request. Another integration may react to
`card.requested` (a webhook passes it on) but makes no card.

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

Every event below but `digest.written` carries the paper's id in `paper_id`.

| Type | When | Payload |
| --- | --- | --- |
| `paper.added` | Intake stored a paper new to the collection | `version`: arXiv version or null; `source`: where intake read it (`oai`, `atom`, `archive`) |
| `paper.revised` | A stored paper got a new arXiv version, or its title or abstract was rewritten (by arXiv, or corrected by the owner) | `version`, `previous_version`, `rewritten`: bool, `source`; an owner's correction has `rewritten: true, corrected: true` and no versions or source |
| `paper.classified` | Classification recorded the paper's classes for its current text, by the classifier or by the owner's hand | `revision`: the paper revision; `primary`: the primary class; `classes`: the classes it is now a direct member of, the owner's class labels applied; `by_owner`: bool |
| `gist.written` | The paper's current Gist was written (from the abstract by analysis; from the full text by a full-text pass, which then stays current for the revision) | `revision`, `source`: `abstract` or `full_text`, `gist_id` |
| `brief.written` | A full-text pass wrote a Brief for the paper; one that finished after a rewrite is labelled with the older revision it read | `revision`: the paper revision it was queued for, `arxiv_version`: of the text it read, or null when unknown; `brief_id` |
| `verdict.recorded` | Grading recorded a verdict for a followed topic (Focus, Area or Field) | `topic`, `revision`: the paper revision, `verdict_id`, `fit`: `central`, `related`, `adjacent` or `none`; `weight`: the Weight it was graded with, or null before analysis; `grade`: derived from the two; `split`, `escalated`, `one_lab`: bool (`one_lab`: a Focus grade one grader's every model refused, so the other's Fit stands) |
| `grade.changed` | A paper's effective grade or Fit for a followed topic changed, by a verdict, a new Weight, the owner's word or a taxonomy edit | `topic`, `before`, `after`: `must_read`, `relevant`, `adjacent`, `none` or null; `fit_before`, `fit_after`: `central`, `related`, `adjacent`, `none` or null; `cause`: `verdict` (a new verdict), `weight` (the pipeline moved the grade but not the Fit: a new Weight, or one a rewrite of the paper set aside), `owner` (the owner's word, or taking it back) or `taxonomy` (a topic edit or the owner's order); absent on events written before litwatch recorded it (October 2026) |
| `paper.saved` | The paper entered the library | none |
| `paper.unsaved` | The paper left the library (its last reason went, or the owner unsaved it) | none |
| `publication.found` | A published record was stored for the paper, by lookup or by the owner | `dblp_key`, `venue`, `year`, `doi`, `by_owner`: bool |
| `card.requested` | The owner asked for a deep card (`POST /api/papers/<id>/card`), whatever the grade, a card already made included; the Deep pass is queued | `pass_id` |
| `card.ready` | The `deep` integration stored a card | `model`, `pass_id` |
| `digest.written` | A digest was published: written, or saying "Nothing new" (a failed one is not; it is published when written again). `paper_id` is null | `digest`: its id, for `GET /api/digests/<id>`; `topic`; `period`: `day` or `week`; `day`: the day, or the week's Monday; `state`: `written` or `nothing_new` |

`grade.changed` is the one to watch for grades; `verdict.recorded` is the pipeline's internal
record and can disagree with what the owner sees. The **effective Fit** is the owner's word
where there is one, else the current verdict's; the **effective grade** comes from it and the
paper's current Weight through the grade table, and the owner's word of central or related never
yields less than `relevant`. Every reader-facing surface uses it, and so should an integration.
Grades exist for every followed topic; check the topic's depth (`GET /api/topics`) if only Focus
topics matter to you. The **library** is every paper at an effective grade of `relevant` or above
for some Focus topic, plus what the owner saved by hand, minus what the owner unsaved.

## Reading litwatch's data

Through the repositories in `litwatch.store`. Each function takes the connection first.

| Call | Returns |
| --- | --- |
| `store.papers.get(conn, paper_id)` | `core.Paper` (title, abstract, authors, categories, published, url, version, doi, ...) or None |
| `store.word.effective_for(conn, paper_id)` | `{topic slug: core.Effective}` for every followed topic that has one, Focus, Area or Field: its effective `fit`, `grade` and `rarity` on the topic's page |
| `store.word.focus_grades(conn, paper_id)` | `{topic slug: core.Grade}`, the effective grades on the Focus topics only: what the library reads |
| `store.word.library_row(conn, paper_id)` | a dict with `saved` (bool) and `hand` (`save`, `unsave` or None), or None |
| `store.reading.paper(conn, paper_id)` | what the paper page shows: `saved`, and `topics`, each with `slug` (its class's), `name`, `depth` (`focus`, `area`, `field`) and `grade` |
| `store.publications.found(conn, [paper_id])` | `{paper_id: core.Publication}` for those with a published record |
| `store.taxonomy.topics(conn, core.Status.ACTIVE)` | the active `core.Topic`s, in the owner's order; `store.taxonomy.load(conn)` gives the whole `core.Taxonomy`, class names included |
| `store.integrations.card(conn, paper_id)` | the deep card row (`card`, `model`, `stats`, `error`, `written_at`, and what it was written from: `revision`, `arxiv_version`, `source`) or None |

## Configuration and loading

Integrations are configured in `litwatch.toml`; each section loads one:

```toml
[integrations.zotero]          # a built-in, by its short name
user_id = "1234567"

[integrations.n8n]             # several of one kind: name the module with `use`
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
- `SearchFilters` has `since` and `until` (published on or after, on or before, when set), `limit`
  and `sort`: `relevance` (the default) or `newest`. A provider returns its hits in that order,
  best match first by its own ranking, or newest first.
- `SearchHit` has `id` (the version-stripped arXiv id), `title`, `authors` (list of names) and
  `published` (aware datetime or None).
- Hits are merged by arXiv id. Papers litwatch holds are shown from the collection; the rest
  form a second band under the collection's results, each row naming the provider that found it.
  The band keeps each provider's order, the providers in turn; asked for the newest, it is newest
  first across them. Topic and saved filters apply to the collection only, so with either set no
  provider is asked.

The built-in `archive` integration is a search provider.

## The built-in integrations

| Name | Reacts to | Does |
| --- | --- | --- |
| `zotero` | `paper.saved`, `paper.unsaved`, `grade.changed`, `publication.found`, `card.ready` | Keeps a Zotero item per library paper with tags and notes in step; tags it `litwatch:dropped` when it leaves the library and never deletes it |
| `webdav` | `grade.changed` to `must_read`, `paper.saved` | Stores the PDF of a `must_read` library paper for Zotero's file sync (needs `zotero`) |
| `archive` | `paper.added`, `paper.revised` | Registers the paper with arxiv-archive (metadata, PDF and source), unless intake read it from the archive; answers searches. With `register = false` it registers nothing and only answers searches and serves PDFs, for running beside another litwatch that already registers the same papers |
| `deep` | none: its work is the full-text queue's Deep passes (a saved Focus must read without a card, the owner's request) | Asks the deep service for a reading card and records each attempt's usage; emits `card.ready`. Loads only with an archive to read papers from |
| `notion` | `grade.changed` for an exported topic, `gist.written`, `card.ready`, `publication.found`, `paper.revised` | Keeps a Notion database of the papers listed for the exported topics, for a reader who does not use litwatch; never deletes a page. See below |
| `webhook` | any, by pattern | POSTs the event as JSON to a URL |

Each module's docstring lists its options and environment variables.

### The Notion export

`notion` keeps one Notion database under a page of the owner's choosing: one row per paper whose
effective grade for an exported topic is at least the minimum grade (docs/specs/v1/integrations.md,
The Notion export; its words are in GLOSSARY.md). Its state is in `notion_databases` and
`notion_pages` (`store.notion`).

```toml
[integrations.notion]
parent_page = "3fe89c45-f79d-8064-be60-f7588e2f34b7"   # the page holding the database
owner_name = "Andrei"                                  # who Why lines name for the owner's word
minimum_grade = "relevant"                             # or "must_read"

[integrations.notion.topics]                           # slug = name in Notion; the Focus topics by default
cot-faithfulness = "CoT faithfulness"
cot-monitoring = "CoT monitoring"
```

The token is a Notion internal integration's, from `NOTION_TOKEN` or the file `NOTION_TOKEN_FILE`
names; share the parent page with that integration. An exported topic must be followed (active or
paused); names must differ from each other, from the columns below and from a topic exported
before. The topics are read on start: without `topics`, a topic made Focus is exported from the next
start.

**On start** (`setup`) it reconciles, safely again and again: it looks for, or makes, the database
"litwatch papers", its columns, the page "How to read this" beside it, the view All papers, and per
exported topic a grade column and a view; then it writes every paper that qualifies or already has a
page. Anything it would make is looked for first (the database and the guide by title, views by
name, pages by arXiv id, asking again for a moment after a lost reply), so nothing is made twice. A
view is made once and is then the owner's: one deleted in Notion is not made again. A column
litwatch writes that is deleted in Notion is put back under a new id, so a view grouped, filtered or
sorted by the old one needs that set again by hand; one turned into another type is turned back,
and its rows written again. Title may be renamed. A grade or Topic option deleted in Notion
is put back with its colour at the next start, and every row written again; a page written before
then brings the option back by itself, uncoloured, and the other rows stay blank there until they
change, so leave the options alone. A view is made showing Title, Grade, Topic,
TL;DR, Published and Status; the other columns are in it, hidden, for the owner to show. A topic
added gets its column, view and papers; a topic dropped keeps its column, view and values, no longer
written; a renamed topic has its column renamed and its new name written to each row's Topic
(Notion's API cannot rename an option, so the old one is removed). A raised minimum turns the papers
below it No longer listed; a lowered one lists them again. A changed `parent_page` starts afresh
under the new page and leaves the old database as it is.

**Each page**: Title, Grade (the best over the exported topics), Topic, TL;DR (the Gist), Published,
Authors, Venue (linked to the published version), arXiv, PDF, Added (the day it entered the list),
Why, one grade column per exported topic, and a hidden arXiv id used to find the row. Status is the
reader's and never written. The body is a callout pointing to "How to read this", the TL;DR, the
Abstract and, with a Deep card, a collapsed Deep read with one callout
per section. A paper that falls below the minimum for a topic says No longer listed there, with a
dated Why line from the newest `grade.changed` that took it off (a missing `cause` reads as
`verdict`). A dismissed paper that has a page is No longer listed for every topic, "set it aside" by
the owner, from the next write of it (dismissal emits no event, so in practice the next start; a
published record the owner clears is likewise taken off at the next start); one with no page gets
none. A page is written only when its properties or body changed; requests are paced to about three
a second, and a 429 is waited out.

**Taken away in Notion.** A paper's page that Notion says is in its trash or deleted was removed on
purpose: it is recorded as gone (`notion_pages.gone_at`), never written or made again, and the
event goes on. "How to read this" is checked on every start and made again when gone. When the
database itself is in the trash, or the page holding it is (its rows then read as trashed too),
the event fails and setup fails, shown under Settings, and no page is taken for gone.

### The webhook's request

`POST <url>` with `Content-Type: application/json` and the event as the body:

```json
{"id": 4512, "type": "grade.changed", "paper_id": "2610.01234",
 "payload": {"topic": "cot-faithfulness", "before": "adjacent", "after": "relevant",
             "fit_before": "adjacent", "fit_after": "related", "cause": "verdict"},
 "created_at": "2026-10-12T06:41:09.312Z", "xid": 508211}
```

Headers: `X-Litwatch-Event: <id>`, so a receiver can drop a repeat; with `secret_env` set, also
`X-Litwatch-Signature: sha256=<hex HMAC-SHA256 of the raw body, keyed by the secret>`. Any answer
other than 2xx is a failure and the event is sent again after the backoff. The receiver reads
anything else it needs from litwatch's API with the shared token, e.g. `GET /api/papers/<id>`.
Answer only once the work is done: litwatch sends a webhook's events one at a time and waits for each
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
