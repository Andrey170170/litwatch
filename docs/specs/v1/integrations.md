# Events and integrations

Mostly carried from the first v1 (built in M6, PRs #26 and #28). Changes are marked **Changed**.

## The outbox

A transactional outbox: a pipeline step writes its event in the same transaction as its data. The `Integration`
protocol is `name`, `wants(event)`, `handle(conn, event)`, idempotent; the connection lets it read through the
repositories and emit events of its own. A dispatcher delivers per integration with its own cursor, retries with
backoff and exposes failures to the API, a failing setup's as well as a failing event's. Integrations deliver only events below the cluster's oldest open
transaction, so litwatch's Postgres should not be shared with services that hold transactions open.

Integrations are the extension system and the built-in ones get no private access: each is one module using only
`events`, `store` and `core`, keeps its own state in its own table, and may emit events. The config names which to
load, by short name or dotted Python path. An integration enabled for the first time starts reading the outbox from
its current end. `docs/integrations.md` documents the contract, the payloads and a worked example.

## Events

`paper.added`, `paper.revised`, `paper.saved`, `paper.unsaved`, `grade.changed` (a paper's effective grade for a topic
changed, whatever caused it), `publication.found`, `card.requested`, `card.ready`.

**Changed:** `take.written` is replaced by `brief.written` (paper, paper revision) and `gist.written`; `verdict.recorded`
carries Fit, Weight and the derived grade; new `digest.written` (topic, period, day) for exports.

## Built-in integrations

| Integration | Reacts to | Does |
| --- | --- | --- |
| `zotero` | `paper.saved`, `paper.unsaved`, `grade.changed`, `gist.written`, `brief.written`, `publication.found`, `card.ready` | Keeps a Zotero item, tags and notes in step; on unsave it tags the item as dropped and never deletes it |
| `webdav` | `grade.changed` to must read, `paper.saved` | Stores the PDF for Zotero's file sync, read from the archive |
| `deep` | see below | Requests a Deep card; emits `card.ready` |
| `notion` | `grade.changed`, `gist.written`, `card.ready`, `publication.found`, `paper.revised` | Keeps a Notion database of the Focus papers for a reader outside litwatch (below) |
| `webhook` | any, by filter | POSTs the event to a URL, for n8n and the like |

**Changed: `archive` is no longer an integration.** It is part of the stack ([archive.md](archive.md)); the archive
client moves to its own module used by intake, the full-text queue, search and `webdav`, and `SearchProvider` stays as
the protocol through which it answers searches. What litwatch tells the archive still rides the outbox, under
`[integrations.archive]` (on in the stack): new and revised papers to register (`paper.added`, `paper.revised`), and
the PDFs the storage policy keeps (`grade.changed` to relevant or must read, `paper.saved`).

**Changed: Zotero.** The collections 0.x named "Tier 1" and "Tier 2" become "Must read" and "Relevant", renamed when
Zotero is switched on after the switch (decision 66). The note on an item carries the Gist and Brief instead of a take, and the Deep card when there is one.

**Changed: deep.** A card is requested for a Focus paper at must read, an Area pick, and any paper the owner asks for
(`card.requested`, honoured whatever the grade). It goes through the full-text queue like any pass, so it starts when
the text is there. The card gains a section placing the paper among the collection: recent papers in the same
classes that it agrees with, contradicts, extends or supersedes, linked by arXiv id, using deep's existing search over
the collection (its own search-only token, built in M7). Its prior-work entries, which already carry an arXiv id
where one is known, become links to paper pages. When a job ends, with a card or failed, `deep` also reads the
job's usage and records it with the attempt (unknown when the job was lost before reporting, never zero), for the cost figures in [models.md](models.md).
A failed job says whose failure it was: the paper's (no card submitted within the job's budget, an invalid card) or
the service's (its model call failed: a sign-in expired, a usage limit, the provider); an error submitting or following
a job is the service's, except a submission turned down for what it is (a 4xx other than 404, 408 and 429, an
unknown extension included), which is the paper's ([pipeline.md](pipeline.md), section 6). A submission carries its attempt as an idempotency key, which `deep` answers with the job it
already started for it, so a submission retried after a lost reply never starts a second job. Only the paper's counts
against it; the service's pauses the queue and shows on the status
document ([pipeline.md](pipeline.md), section 6). Cards have no daily cap: the subscription's use by litwatch is small
beside the owner's own, and a re-run's passes wait behind the night's. An integration acting on a grade checks the effective grade and the
library when it handles the event (first v1).

A card's automatic reasons are its own, not the library's: picks are not saved (decision 3) and still get cards. When
the pass is about to start, `deep` checks its reason still holds: for a Focus must read, the effective grade is still
must read; for a pick, the digest that picked it is published. Either way the paper must not be unsaved: an Unsave
cancels automatic card passes that have not started. A card the owner asked for is never cancelled by an Unsave, and a
card already made is kept.

## The Notion export

**Changed (decisions 68–76).** Notion becomes a built-in integration, `notion`, that talks to Notion's API directly;
n8n is no longer involved, and the first v1's n8n workflow is removed. It is the PI's view of litwatch, not the
owner's: the owner reads the web app, the PI reads a Notion table and does not use litwatch itself. She tries it for a
week from 2026-10-19; if she does not use it, it is switched off in the configuration.

**What it holds.** Papers only, no digests: every paper whose effective grade for an exported topic is relevant or
above. The exported topics default to the Focus topics (now CoT faithfulness, CoT monitoring, Circuits) and the
minimum grade to relevant; both are options, and each exported topic's name in Notion can be set (it defaults to
the topic's name). The export follows grades, not the library: Save and Unsave do not move a paper in or out of it,
since the library is the owner's own and the PI's view is what litwatch judges. A page stands on its own: the PI cannot reach litwatch, so nothing
links into it; it links to arXiv, the PDF and the published version.

**Its words.** None of litwatch's internal terms reach Notion. Grades read **Must read** and **Worth a look**
(relevant); a paper taken off the list reads **No longer listed**. The detail levels read **TL;DR** (the Gist),
**Abstract** (the paper's own; Briefs are not exported yet) and **Deep read** (the Deep card). Topics carry short
names. A "How to read this" page beside the database explains the topics, the grades, the text on each paper and
which column is the reader's. The words are recorded in the glossary under *Notion export*.

**The database.** One row per paper. Its columns: Title, Grade, Topic (the exported topics it is or was listed for), TL;DR,
Published, Status, Authors, Venue, arXiv, PDF, Added, Why, and one grade column per exported topic. Grade is the best
of the paper's topic grades; the topic grade columns are what the topic views group by, since one paper can be a
must read for one topic and worth a look for another. Views: All papers, grouped by Grade, and one per exported
topic, showing its papers grouped by that topic's grade; each sorted newest published first. Grades are
coloured from Notion's palette: Must read orange, Worth a look blue, No longer listed grey. Rarity, the web app's
colour, is not exported: it spans all followed classes, and the PI reads only these topics.

**The page.** A short callout pointing to "How to read this", the TL;DR, the Abstract, and the Deep read when there is
one: a collapsed toggle holding one coloured callout per section of the card, as 0.14's reading card had them (key
claim, summary, the faithfulness and monitoring sections when the card has them, evaluation, closest prior work with
arXiv links, limitations, code). litwatch owns the body and rewrites it when its text changes.

**Leaving and returning.** A paper whose grade for a topic falls below the minimum keeps its page and stays in that
topic's view: that topic's grade becomes No longer listed, and Grade too once no topic lists it. **Why** holds one
line per topic the paper has left, in plain words and with the date: who took it off and why. The cause is the
owner's word (the owner named as the configuration says), litwatch re-checking the paper, or a change to the topic's
definition; and it was judged off-topic or less important (its Fit fell, or only its Weight). The cause comes from
the event: `grade.changed` gains a `cause` field (`verdict`, `weight`, `owner`, `taxonomy`), written by whatever
changed the grade, while the integration still checks the present grade before writing. Pages are never deleted.
A paper listed again for a topic loses that topic's line. A paper the owner dismisses is No longer listed for every
topic it was listed for, with a line saying the owner set it aside; a dismissed paper with no page gets none. The integration keeps each topic's line in its own
table, so a rewrite for another event or after a restart keeps it, and a `grade.changed` touches only its topic's line.

**Who owns what.** The integration creates the database, its columns and its views once, when it is set up, under a
page the configuration names. After that it writes only its own properties and the page body; views, column widths
and what a view hides are the owner's to arrange and are never reset. When it starts, the integration compares the
exported topics with the database: a topic newly exported gets its grade column and its view, and its qualifying
papers are filled in; a topic no longer exported keeps its column, view and rows, which are no longer written; a
changed name renames that topic's option and column. A raised minimum grade turns the papers below it No longer listed, with a Why
that says so; a lowered one lists them again. **Status** (To read, Read, Useful, Not useful) is the reader's and is never written; comments on a page
are not part of its body and survive rewrites. A paper's page moved to Notion's trash or deleted was removed on purpose:
it is never written or made again. "How to read this" is litwatch's and is made again; a database in the trash stops
the integration, shown under Settings. Nothing flows back from Notion into litwatch.

**First fill and after.** When it is set up the integration fills the database with every paper that qualifies at
that moment, the papers v1 already holds included; from then on it follows the events above from its cursor. It
reads everything through the repositories, so a repeated event writes the same page.

**Access.** It authenticates with its own Notion internal integration's token (a secret, never `litwatch.toml`),
which the owner shares with the one Notion page that holds the database.

**The owner's instance.** The database goes on the existing LitWatch page, which the PI can already see. Once the
owner has checked it, 0.14's Papers, Digest and Selections databases and the LitWatch page's untitled linked view go
to Notion's trash (decision 76). The Research workspace's own Papers source is fed from Zotero by other n8n
workflows, has no relation to 0.14's databases and is not touched.
