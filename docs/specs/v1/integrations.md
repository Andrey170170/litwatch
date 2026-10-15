# Events and integrations

Mostly carried from the first v1 (built in M6, PRs #26 and #28). Changes are marked **Changed**.

## The outbox

A transactional outbox: a pipeline step writes its event in the same transaction as its data. The `Integration`
protocol is `name`, `wants(event)`, `handle(conn, event)`, idempotent; the connection lets it read through the
repositories and emit events of its own. A dispatcher delivers per integration with its own cursor, retries with
backoff and exposes failures to the API. Integrations deliver only events below the cluster's oldest open
transaction, so litwatch's Postgres should not be shared with services that hold transactions open.

Integrations are the extension system and the built-in ones get no private access: each is one module using only
`events`, `store` and `core`, keeps its own state in its own table, and may emit events. The config names which to
load, by short name or dotted Python path. An integration enabled for the first time starts reading the outbox from
its current end. `docs/integrations.md` documents the contract, the payloads and a worked example.

## Events

`paper.added`, `paper.revised`, `paper.saved`, `paper.unsaved`, `grade.changed` (a paper's effective grade for a topic
changed, whatever caused it), `publication.found`, `card.requested`, `card.ready`.

**Changed:** `take.written` is replaced by `brief.written` (paper, paper revision) and `gist.written`; `verdict.recorded`
carries Fit, Weight and the derived grade; new `digest.written` (topic, period, day) for exports such as Notion.

## Built-in integrations

| Integration | Reacts to | Does |
| --- | --- | --- |
| `zotero` | `paper.saved`, `paper.unsaved`, `grade.changed`, `gist.written`, `brief.written`, `publication.found`, `card.ready` | Keeps a Zotero item, tags and notes in step; on unsave it tags the item as dropped and never deletes it |
| `webdav` | `grade.changed` to must read, `paper.saved` | Stores the PDF for Zotero's file sync, read from the archive |
| `deep` | see below | Requests a Deep card; emits `card.ready` |
| `webhook` | any, by filter | POSTs the event to a URL; n8n and the Notion export hang off it |

**Changed: `archive` is no longer an integration.** It is part of the stack ([archive.md](archive.md)); the archive
client moves to its own module used by intake, the full-text queue, search and `webdav`, and `SearchProvider` stays as
the protocol through which it answers searches.

**Changed: Zotero.** The collections 0.x named "Tier 1" and "Tier 2" become "Must read" and "Relevant", renamed at the
switch. The note on an item carries the Gist and Brief instead of a take, and the Deep card when there is one.

**Changed: deep.** A card is requested for a Focus paper at must read, an Area pick, and any paper the owner asks for
(`card.requested`, honoured whatever the grade). It goes through the full-text queue like any pass, so it starts when
the text is there. The card gains a section placing the paper among the collection: recent papers in the same
classes that it agrees with, contradicts, extends or supersedes, linked by arXiv id, using deep's existing search over
the collection (its own search-only token, built in M7). Its prior-work entries, which already carry an arXiv id
where one is known, become links to paper pages. When it collects a card, `deep` also reads the job's usage and
records it with the attempt, for the cost figures in [models.md](models.md). An integration acting on a grade checks the effective grade and the
library when it handles the event (first v1).

A card's automatic reasons are its own, not the library's: picks are not saved (decision 3) and still get cards. When
the pass is about to start, `deep` checks its reason still holds: for a Focus must read, the effective grade is still
must read; for a pick, the digest that picked it is published. Either way the paper must not be unsaved: an Unsave
cancels automatic card passes that have not started. A card the owner asked for is never cancelled by an Unsave, and a
card already made is kept.

Notion stays outside the built-in set: a documented n8n workflow subscribes by webhook and reads the API. The PI's
Notion view carries Focus topics only until the owner has talked to her (first v1, decision 5).
