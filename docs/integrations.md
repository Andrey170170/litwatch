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
**delivery** runs inside `litwatch serve` for each loaded integration that reacts to events (a
`Reacting` one, below), as work items of the service's Work gate, one at a time per
integration, woken as each event commits: it hands the integration the events after its own
**cursor**, in order:

- The order is the order of transactions, then of events within one: an event's place is its
  writing transaction's id, then its own id. Only events whose transactions are older than every
  transaction still running are handed over, so an event that commits late is never passed
  over. A long-open transaction anywhere on the database server delays delivery until it ends;
  nothing is lost.
- An integration enabled for the first time starts at the outbox's current end: it is not woken
  by anything that happened before it was turned on. Turning it off keeps its cursor, and
  turning it on again resumes from there.
- When `wants` or `handle` raises, the cursor stays before that event, the error is recorded, and the event
  is tried again after a backoff (30 seconds, doubling, at most an hour). That integration's
  later events wait behind it; every other integration goes on. The owner can retry the event
  at once or skip it, under Settings → Integrations (`POST /api/settings/integrations/<name>/retry`
  and `/skip`). A failing `setup` is retried the same way, and Retry tries it again at once; the
  integrations API reports it as `setup_failing`, apart from a failing event.
- The cursor is saved once a turn's batch (up to 100 events) is handed over, or where a failure
  stops it. After a failure, the failing event is handed over again; after a restart, every event
  after the cursor last saved is, up to a batch of them, those already handled included.
  **`handle` must be idempotent.**

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

class Reacting(Integration, Protocol):
    def wants(self, event: Event) -> bool: ...
    async def handle(self, db: DatabaseGate, event: Event) -> None: ...
```

An integration that reacts to events is a `Reacting` one; only those are handed events and keep
a cursor. `deep`, whose work is the full-text queue's, is a plain `Integration`.

- `wants` is a cheap filter on the event alone (no I/O). Events it does not want are passed
  over without calling `handle`.
- `handle` does the work. `db` is litwatch's Database gate (`litwatch.gates.database`), and
  every database access goes through a unit opened on it: `async with db.read("name") as unit:`
  for reads, which see one snapshot, and `async with db.write("name") as unit:` for writes,
  which commit together at the end of the block (rows are dicts, as a unit speaks a psycopg
  connection's `execute`). Keep units short and make no network call inside one: read what
  you need, close the unit, call out, then write what came back in a unit of its own. Units do
  not nest.
- Optional, used when present (`aclose` and `requires` on any integration, the rest on a
  `Reacting` one):
  - `async def setup(self, db)`: awaited before the first event in each `litwatch serve` that
    delivers to it, never beside a `handle` of the same integration. It must be safe to run
    again. When it raises, no event is handed
    over; the error is shown under Settings → Integrations and setup is tried again after the
    same backoff as a failed event (or at once, on Retry).
  - `async def aclose(self)`: awaited when the service stops. Close HTTP clients here.
  - `requires: tuple[str, ...]`: names of integrations that must be loaded too; loading fails
    otherwise.
  - `async def idle(self, db)`: awaited on a delivery turn that hands the integration no event,
    at most about once a minute, never beside a `handle` of the same integration, for work no
    event brings (retrying a delivery that waits on something outside litwatch). A failure is
    logged and changes nothing else; keep it short and bounded, since events wait while it runs.

Deep reads come from the built-in `deep` integration only: the owner's request (`POST
/api/papers/<id>/card`) queues a Deep pass, which the service's Deep pass worker submits to it, and
only with it loaded does the paper page offer the request. Another integration may react to
`card.requested` (a webhook passes it on) but makes no card. What the deep service returns, and
what litwatch stores of it, is under "The deep service's result" below.

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
- **Use only `events`, `store`, `core` and `gates.database`** from litwatch (and your own HTTP
  client), plus the small helpers in `litwatch.integrations`. Never import `api`, `jobs` or a pipeline package.
- **Keep your own state in your own table.** Read litwatch's data through the `store`
  repositories; do not write to its tables.
- **You may emit events** of your own with `litwatch.store.outbox.emit(unit, type, paper_id,
  **payload)`, in the same write unit as the state they describe. Other integrations can subscribe to them
  (`deep` emits `card.ready`, which `zotero` and `notion` react to).
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

Through the repositories in `litwatch.store`, inside a read unit. Each function takes the unit
first.

| Call | Returns |
| --- | --- |
| `store.papers.get(unit, paper_id)` | `core.Paper` (title, abstract, authors, categories, published, url, version, doi, ...) or None |
| `store.word.effective_for(unit, paper_id)` | `{topic slug: core.Effective}` for every followed topic that has one, Focus, Area or Field: its effective `fit`, `grade` and `rarity` on the topic's page |
| `store.word.focus_grades(unit, paper_id)` | `{topic slug: core.Grade}`, the effective grades on the Focus topics only: what the library reads |
| `store.word.library_row(unit, paper_id)` | a dict with `saved` (bool) and `hand` (`save`, `unsave` or None), or None |
| `store.reading.paper(unit, paper_id)` | what the paper page shows: `saved`, and `topics`, each with `slug` (its class's), `name`, `depth` (`focus`, `area`, `field`) and `grade` |
| `store.publications.found(unit, [paper_id])` | `{paper_id: core.Publication}` for those with a published record |
| `store.taxonomy.topics(unit, core.Status.ACTIVE)` | the active `core.Topic`s, in the owner's order; `store.taxonomy.load(unit)` gives the whole `core.Taxonomy`, class names included |
| `store.integrations.card(unit, paper_id)` | the deep card row (`card`, `model`, `stats`, `error`, `written_at`, and what it was written from: `revision`, `arxiv_version`, `source`) or None. `card` is a Deep read when its `format` is `"read"` (`core.deepread.is_read`; `core.deepread.section` and `inline` take one apart) and a Deep card, the format from before, when it has none: handle both, and take a field a read lacks as absent. Either may hold `process_notes`, which describe Deep's process, not the paper: the built-in exports leave them out, but for a read's `review` note, which says the read failed its review |

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
| `zotero` | `paper.saved`, `paper.unsaved`, `grade.changed`, `publication.found`, `card.ready` | Keeps a Zotero item per library paper with tags and notes in step; tags it `litwatch:dropped` when it leaves the library and never deletes it. A Deep read gets no note and no tags of its own (it is read on the paper page) and still counts as written for the inbox tag; a Deep card from before keeps its note and tags |
| `webdav` | `grade.changed` to `must_read`, `paper.saved` | Stores the PDF of a `must_read` library paper for Zotero's file sync (needs `zotero`) |
| `archive` | `paper.added`, `paper.revised` | Registers the paper with arxiv-archive (metadata, PDF and source), unless intake read it from the archive; answers searches. With `register = false` it registers nothing and only answers searches and serves PDFs, for running beside another litwatch that already registers the same papers. With `harvest = true` (and registering), intake's catalog pass also registers every new or revised arXiv paper outside intake's categories, as metadata only: `litwatch intake --catalog-from DATE` backfills it from a day |
| `deep` | none: its work is the full-text queue's Deep passes (a saved Focus must read without a card, the owner's request) and the finds of release links | Asks the deep service for a Deep read, stores it with its figures, and records each attempt's usage, trace and reviews (`litwatch deep trace`); emits `card.ready`. Finds where a paper's code and weights are released. `automatic = false` stops the automatic Deep reads, and leaves the owner's requests and the finds on. Loads only with an archive to read papers from. See below |
| `notion` | `grade.changed` for an exported topic, `gist.written`, `card.ready`, `publication.found`, `paper.revised` | Keeps a Notion database of the papers listed for the exported topics, for a reader who does not use litwatch; never deletes a page. See below |
| `webhook` | any, by pattern | POSTs the event as JSON to a URL |

Each module's docstring lists its options and environment variables.

### The deep service's result

`deep` submits a paper (`POST /process`) and follows its job (`GET /jobs/<id>`) to its end;
[`deep/README.md`](../deep/README.md) has the protocol, the trace and the failures. This is the
part of it litwatch stores. The submission and the job are contracts the service declares
(`deep/src/contracts.ts`, exported to `contracts/deep.schema.json`); litwatch's models of them
are generated from it (`core.contracts.deep`, by `scripts/contracts.sh`), so the two sides read
one definition.

**The submission's `paper`** carries the paper's `body`, `body_source` (`latex` or `pdf`, always
given) and `body_version`: the arXiv version the text is of, left out when the archive cannot say
(a text read from its historical LaTeX). The service takes the paper's figures from that
version. A LaTeX source goes with its `outline`, as litwatch parses it (`core.outline`, which
the MCP server's outline lists too): its headings with their depths, spans and numbers, the
bibliography's span and where the appendix starts. Its offsets are UTF-16 code units, as
JavaScript indexes the text (the MCP server's are Python's code points). The contract has the
two kinds of paper as two variants, so the service turns down a LaTeX source without an outline,
a PDF's text with one, or a text that does not say where it comes from, with a 422.

**A finished job** (`status: "done"`) carries, beside its `stats` and `trace`:

| Field | What it holds | Stored |
| --- | --- | --- |
| `result` | The Deep read, an object whose `format` is `"read"`, valid against the service's schema (`deep/src/read.ts`). The Deep cards from before, with no `format`, are only in storage: the service writes none | `deep_cards.card`, as returned |
| `figures` | The paper's figures the read shows, a list of `{"name", "mime", "data"}`: `name` as the read's `fig` blocks cite it (letters, digits, `.`, `_` and `-`, starting with a letter or digit, each name once), `mime` `image/webp`, `data` the image in base64, at most 1600 px wide. Absent or empty: no figures | `deep_figures`, one row per figure, replacing the paper's earlier ones in the transaction that writes the read |
| `reviews` | The review rounds the read went through, in order, a list of `{"round", "passed", "scores", "checks", "findings", "retelling"}`: `scores` the rubric's (`faithfulness`, `clarity`, `depth`, `calibration`, 1 to 5) and the blind reader's `followability`, `retelling` the blind reader's account of the paper's argument from the read, `checks` the reviewer's structure checks by name (`anchors`, `experiments`, `why_boxes`, `voice`, `length`; true: passed), apart from the schema and code checks submit runs before any review, `findings` each `{"about", "where", "text"}` with an optional `rewrite` (`about` the score or check it counts against, `followability` for the blind reader's, `where` a section's id in the read, or `<section id>.<n>` for its n-th block). A round passed when every score is at least 4 and every check passed; the first write and at most two revisions make at most three rounds. Absent or null: no review gave a verdict | `deep_attempts.reviews`, as returned, for a failed job too |

`figures` and `reviews` are the job's fields, not the read's: the read's schema takes no others.

litwatch checks only this much before it stores anything (the job's contract, then
`core.deepread.received` for the figures' bytes), and leaves the read's own shape to the service
that validated it. A job that is not the contract's (a `format` other than `"read"` or none, a
result that is not an object, `figures` that are not such a list, an unknown `mime`, a name
outside the pattern, `reviews` that are not a list), or figures given twice, not base64, not a
WebP image or more than 32 MiB together, end the job as the service's failure: nothing of the
result is stored, the card and figures before it stay, the attempt keeps its usage, trace and
reviews where they are the contract's, and the Deep queue pauses and tries the pass again, as for any failure of the service. A failed job's `result` is left out, or null
from a service from before. A reply to a poll for a job still queued or running that is not the
contract's, or that gives a status the contract does not name, is a failed poll: the job is
followed again, never ended or sent again.

**A read that still failed its review** when the rounds ran out is stored and shown: the service
adds a process note `{"kind": "review", "source": "service", "note": ...}` to it. Needs you lists
it with the read's other process notes, the Notion export opens the read's short form with a line
saying so, and the MCP server's `get_paper` says so above the read.
A read whose review could not run at all (the reviewer's call failed, or it gave no valid
verdict) is stored too, with `reviews` null and a note of kind `tool` saying it was not
reviewed: it is unreviewed, not failed. So is a revision whose review could not run: it carries
the `tool` note and no `review` note, and `reviews` holds the rounds completed on the drafts
before it. A read whose reviewer could not be given the whole paper
(a text past the model's context loses its bibliography, then its end) carries a `tool` note
saying what the reviewer did not see, whatever the verdict.

**The figures** are served to the web app at `GET /api/papers/<id>/deep/figures/<name>` (the web
app's token, as a cookie or a bearer header; the read token is refused, and an unknown name is
404). The paper page asks with `?v=` the read's time of writing, and an answer to that is cached
for good; without `v` it is fetched afresh each time.

**The read token** (`LITWATCH_READ_TOKEN`), which the service's agent uses, searches and reads
papers (`GET /api/papers`, `GET /api/papers/<id>`) and fetches a paper's PDF
(`GET /api/papers/<id>/pdf`), from which the service cuts the figures of a paper with no LaTeX
source. The service asks for the version its text is of (`?version=<n>`), which the archive is
asked for, or arXiv's PDF of it. Everything else answers it 403.

### Release links

The service also finds where a paper's code and weights are released (`POST /links`, followed at
`GET /links/<id>`, the same key protocol as a read; `deep/README.md`). A find is a work item of
kind `links` (`litwatch.links`), worked only by a `serve` with the schedule on and an archive to
read papers from. Each find is of the paper as it is when submitted (`core.links.Identity`): its
arXiv version, or its update when no version is known; every decision about it asks whether it is
of the paper as it is now. An automatic one runs when the paper's analysis says it releases code
or weights, unless the paper as it is has an answer, "none found" included, the owner's or an
automatic one's, or an automatic find of it is open; an open find of the owner's does not count,
so should it fail the automatic one still runs. A failed automatic find ends failed with its
error and cost, and a later trigger (an analysis again, or the nightly's retries of the last
week, run after its analyses; three failures at most) submits a fresh one. A new version, or
update, is found again, wherever it is stored (intake, or an owner's action fetching the paper).
The owner's "Find links" (`POST /api/papers/<id>/links`) runs a fresh find of the paper as it is,
unless one of theirs of the same is open, on a paper only in the archive too, which it stores
first. Right before a find pays, after any wait for the archive's text, and again with its answer,
under the paper's locks: a find of what the paper no longer is ends superseded without paying, or
with its answer dropped, and hands over in the same unit (the owner's is asked again of the paper
as it is, an automatic one is due again); an automatic find whose paper another find answered
meanwhile ends without asking deep. A find's answer replaces the paper's links (`release_links`:
url, kind, access state, license, when checked, what set it) and is recorded with its item's
result in one commit, by which a find tried again ends without asking again; the Work gate ends
the item in a second. A wrong answer (a URL that is not absolute http(s), a time that is not one
in UTC) fails the find and touches nothing. A GitHub or Hugging Face link takes its state from
the host's API, by one check shared by deep's finder and the weekly re-check (`deep/src/hostcheck.ts`,
`links.adapter`, both suites driven by `deep/tests/fixtures/host-checks.json`); deep confirms any
other link by a fetch of it that shows its state. A placeholder link on GitHub or Hugging Face is
checked again by that check, a week apart, four times, each check its own work item, then left
alone; no other link is. The paper page shows each link as a chip coloured by its state, and
"Find links" says how the owner's last find of the paper as it is ended ("none found", "failed");
`get_paper` lists the links, or "None found" for a current answer of none, or that a find is
queued or running. Digests and the Notion export do not show them yet.

### The Notion export

`notion` keeps one Notion database under a page of the owner's choosing: one row per paper whose
effective grade for an exported topic is at least the minimum grade (docs/specs/v1/integrations.md,
The Notion export; its words are in GLOSSARY.md). Its state is in `notion_databases` and
`notion_pages` (`store.notion`).

```toml
[integrations.notion]
parent_page = "0123456789abcdef0123456789abcdef"   # the page holding the database
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
sorted by the old one needs that set again by hand. A column litwatch writes that is turned into
another type is turned back, and its rows are rewritten. Title may be renamed. A topic may not
take the name of a column litwatch keeps, of one the reader made, or of another topic's. A grade or Topic option deleted in Notion
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
Abstract and, under a collapsed "Deep read" heading, the read's short form: its key claim and
points, then each claim with whether the paper shows it (Holds, Partly, Not shown, Overclaimed) and
each limitation with whose it is (the paper's own or the read's). Its process notes are left out,
but a read that failed its review opens with a line saying so. A Deep card from before keeps the
body it had, one callout per section, and is not rewritten. A paper that falls below the minimum for a topic says No longer listed there, with a
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

from litwatch import events
from litwatch.config import Config, secret
from litwatch.core import Event
from litwatch.gates.database import DatabaseGate
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

    async def handle(self, db: DatabaseGate, event: Event) -> None:
        # Check the present: the owner may have overruled the grade or unsaved the paper.
        async with db.read("phone: paper") as unit:
            wanted = await must_read_and_saved(unit, event.paper_id)
            paper = await papers.get(unit, event.paper_id)
        if not wanted:
            return
        # With the unit closed: no network call inside one.
        resp = await self.http.post(
            self.url,
            content=paper.title.encode(),
            headers={"Title": "Must read", "Click": f"https://arxiv.org/abs/{paper.id}"},
        )
        resp.raise_for_status()  # raising makes the delivery retry later

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
  keyed by paper (create it in `setup`, with `CREATE TABLE IF NOT EXISTS` in a write unit), and
  check it first.
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
