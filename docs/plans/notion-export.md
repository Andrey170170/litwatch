# The Notion export

The owner approved this plan on 2026-10-19. It is the reference for building the `notion` integration: the PI's
Notion view of litwatch. What the integration does is specified in the spec:
[integrations.md](../specs/v1/integrations.md), section *The Notion export*, and
[decisions.md](../specs/v1/decisions.md), decisions 68–76. Its words are in the glossary under *The Notion export*.
This plan adds one sentence to that section: what happens when the minimum grade changes. Where the plan and the
spec differ, the spec wins and the plan is corrected. Terms are the glossary's.

## Goal and approach

Ana, the owner's PI, asked on 2026-10-19 for a week to see whether a Notion view of litwatch is useful to her. She
does not use litwatch itself. The integration keeps a Notion database of every paper whose effective grade for an
exported topic is relevant or above. It runs on the owner's instance, on the LitWatch page she can already see.

The work comes in three stages:

1. Add a `cause` to `grade.changed`, so a page can say why a paper left the list.
2. Build the integration and test it against a scratch Notion page through a development integration.
3. Switch it on in production with the owner present, then clear away 0.14's Notion databases.

The page and database layout is the sample the owner approved. The sample is the Notion page "litwatch · papers for
Ana (SAMPLE, not shared)", id `3fe89c45-f79d-8182-89de-d0a0f5f4b6b1`. Its layout is described under *Layout* below.

## Choices made without asking

### The cause

- **The caller of `settle` names the cause.** `word.change(conn, *ids, cause=…)`, `settle`, `settle_all`,
  `settle_ties`, `settle_topic` and `sync` all take a required keyword `cause`, and `settle` puts it on every
  `grade.changed` it emits. `settle` cannot work it out alone: a cleared owner's word and a re-check both end on the
  verdict.
  - `owner`: the owner's actions. These are the reading actions in `api/reading.py`, Calibrate, misses, and the
    owner's actions in `api/settings.py` and `api/integrations.py`.
  - `taxonomy`: a topic's definition changed. This covers `settle_topic` and `settle_ties` after a topic edit or
    reorder.
  - `verdict`: grading, analysis, classification, full-text and Deep passes, digest picks, intake, the nightly's
    reconcile, and `sync`.
- **`settle` turns `verdict` into `weight` when the Fit did not move** (`fit_before == fit_after`), because only the
  grade table's Weight changed. An owner's or taxonomy cause is never rewritten.
- `docs/integrations.md` documents the field with the rest of the payload.

### The client and its pace

- **A plain `httpx` client on Notion API version `2026-03-11`, no SDK.** The views endpoint is new, the client needs
  only a handful of calls, and the sample used exactly these calls. It has the same shape as `zotero.py`'s client.
- **It paces itself to about three requests a second.** On a 429 it waits as Retry-After says. Any other failure
  raises, so the dispatcher retries the event with backoff and shows it under Settings → Integrations.
- **The outbox is the queue.** litwatch only writes events, each in its own transaction. The `notion` loop publishes
  from its own cursor at Notion's pace and holds up no other integration.
- **No database transaction stays open across a Notion call.** The outbox reads the snapshot of the whole database
  cluster, so a transaction left open would hide fresh events from every integration. Check how the dispatcher's
  connection handles transactions (autocommit or not) before relying on this. Read in short transactions and call
  Notion outside them.

### Reconcile on start

- **One idempotent pass, reconcile, runs as the `setup` hook on every start.** It creates whatever is missing:
  - the database under `parent_page`;
  - its columns;
  - the "How to read this" page;
  - the All papers view;
  - a column and a view for each exported topic that has none.

  Then it walks every paper that qualifies or already has a page, and writes whatever changed. The same pass serves
  the first fill, a topic added or dropped, a renamed topic and a changed minimum grade.
- **A topic no longer exported** keeps its column, view and rows. They are no longer written.
- **A renamed topic** gets its Topic option, its grade column and its view title renamed.
- **A paper below a raised minimum grade** becomes No longer listed for that topic. Its Why line reads, for example,
  "19 Oct 2026: the list now shows only Must read papers." A lowered minimum lists papers again and removes that line.
- **Views are created once and recorded in the integration's state.** A view the owner rearranges or deletes is
  never touched again. Columns the integration writes are added back if missing.
- Reconcile is idempotent, so a crash halfway through is finished by the next start.

### State

The integration keeps its state in its own tables, created by one migration:

- **`notion_pages`**: paper → page id, a hash of the properties last written, a hash of the body last written, and
  the Why line for each topic.
- **A small table for the database itself**: database and data source ids, the "How to read this" page id, and the
  view id and column for each topic.

A page is written only when one of its hashes changes, so restarts and repeated events cost no requests.

### Events and pages

- **Events handled:**
  - `grade.changed`, for an exported topic;
  - `gist.written`;
  - `brief.written`;
  - `card.ready`;
  - `publication.found`;
  - `paper.revised`.

  `wants` drops every other event. `handle` reads the present state through the repositories, rebuilds the one page,
  and writes what changed, as the integration contract says.
- **The Why line comes from the `grade.changed` that delisted the paper.** It is stored in `notion_pages`. Only that
  event's topic line changes; a paper listed again for the topic loses the line. In plain words:
  - `owner`: "Andrei marked it off-topic" or "… less important" (the name is `owner_name`);
  - `verdict` and `weight`: "litwatch re-checked it and judged it off-topic" or "… less important";
  - `taxonomy`: "the topic's definition changed".

  "Off-topic" means the Fit fell. "Less important" means only the grade fell, the Fit staying the same.
- **The body is replaced whole when its hash changes**: delete litwatch's blocks, then append the new ones. TL;DR,
  Summary and Deep read change only with new text, so this is rare.

### Options

The `[integrations.notion]` options:

- `parent_page`: the id of the Notion page that holds the database.
- `topics`: a table of slug → name. It defaults to every Focus topic under its own name.
- `minimum_grade`: defaults to `relevant`.
- `owner_name`: the name used in Why lines.

The token comes from `NOTION_TOKEN`, or the file `NOTION_TOKEN_FILE` names, through `required_secret`.

On the owner's instance: `cot-faithfulness` = "CoT faithfulness", `cot-monitoring` = "CoT monitoring",
`circuits` = "Circuits". Read the slugs from the taxonomy; these are the expected ones. `owner_name = "Andrei"`.

### Layout

The layout is the approved sample's.

**Columns:**
- Title.
- Grade: a select with Must read orange, Worth a look blue, No longer listed grey.
- Topic: a multi-select.
- TL;DR.
- Published.
- Status: a select of To read, Read, Useful, Not useful. Ana owns it and litwatch never writes it.
- Authors, Venue, arXiv (url), PDF (url), Added and Why.
- One grade column per exported topic, a select with the same three options.

**Views:** All papers, grouped by Grade; one view per exported topic, filtered to that topic and grouped by its grade
column. Every view is sorted by Published, newest first.

**The page body:**
1. A 💡 callout pointing to "How to read this".
2. Heading TL;DR, then the Gist.
3. Heading Summary, then the Brief, or the abstract when there is no Brief.
4. When the paper has a Deep card, a collapsed toggle heading "Deep read" holding one callout per section of the
   card:
   - 💬 gray: Key claim
   - 📖 blue: Summary
   - 🎯 purple: Faithfulness
   - 🔎 red: Monitoring
   - 🧪 green: Evaluation
   - 📚 yellow: Closest prior work, with arXiv links
   - ⚠️ orange: Limitations
   - Then a Code link.

   The faithfulness and monitoring callouts appear only when the card has those sections.

A paper that is no longer listed also gets a 🗄️ gray callout at the top: "No longer listed." followed by its Why
lines.

"How to read this" explains:
- the topics;
- the grades;
- what each paper's text holds;
- that Status is Ana's column;
- that comments belong at the page level.

## Decisions

- **D1. Development uses its own Notion integration, "litwatch-dev".** It is shared only with the page "Litwatch dev
  scratch", id `3fe89c45-f79d-8064-be60-f7588e2f34b7`. The owner created both on 2026-10-19. A development run cannot
  reach Ana's page even with a wrong `parent_page`. The cost is one more integration in Notion's settings.
- **D2. The database is set up and filled automatically when the dispatcher starts** (reconcile, above). Turning the
  section on in `litwatch.toml` and restarting is the whole set-up. The same pass reconciles later changes to the
  configuration, so there is no separate command and no second code path.

## Risks

- **R1. Notion's rate limit, about three requests a second.** A page with a Deep read takes several calls. The
  client paces itself. The outbox and the integration's own loop keep the queue away from everything else. A failure
  is retried by the dispatcher and shown under Settings.
- **R2. A body rewrite drops comments anchored to the replaced blocks.** Page-level comments survive. Bodies are
  rewritten only when their text changes, which is rare, and "How to read this" says comments belong at the page
  level.
- **R3. A wrong cause gives a misleading Why**, such as "Andrei marked it…" for a re-check. P1 tests the cause from
  each kind of change, a cleared owner's word included.
- **R4. The owner's view arrangement could be reset.** Views are created once and never touched again. The owner
  arranges them once after P3. V6 shows that the arrangement survives a restart.
- **R5. Ana could see a half-filled table.** The production fill runs once, on the restart, in a minute or two, and
  before Ana is told. If something goes wrong, switch the section off and trash the database; nothing else is
  touched.

## Stages

Each PR goes through `drive`.

| Stage | Work | Shows before the next starts |
| --- | --- | --- |
| P1 | **`cause` on `grade.changed`.** Every caller of `change`/`settle` and its variants names the cause; `settle` derives `weight`; `docs/integrations.md` documents the payload. A small PR. | V1. |
| P2 | **The `notion` integration.** The paced client, the state migration, reconcile on start, event handling, the page builder and Why lines; `notion` added to `BUILT_IN`; the package docstring; `docs/integrations.md`. A normal PR. | V2 in pytest. V3–V7 on the sandbox against the scratch page through litwatch-dev, with screenshots. Then the owner looks at the scratch database. |
| P3 | **Switch-on on the owner's instance.** With the owner present: the owner creates the "litwatch" integration and shares the LitWatch page with it; the token goes into the stack as a secret; the `[integrations.notion]` section goes into the stack's `litwatch.toml`; restart. Then delete the sample page. After the owner confirms the new database, trash 0.14's Papers, Digest and Selections databases and the LitWatch page's untitled linked view through the API. | V8. The owner arranges the views once and tells Ana. |

## Verification

The methods are in [docs/agents/verification.md](../agents/verification.md): tests against `tests/fakes_http.py`,
then a real run against a test target on the sandbox. For the sandbox runs, build the sandbox with
`scripts/refresh-sandbox.sh --production`, and set `NOTION_TOKEN_FILE` to the litwatch-dev token in `.env.v1`.

| Claim | Evidence |
| --- | --- |
| V1. `grade.changed` carries the cause of the change | pytest on PostgreSQL, one case each: a label, a cleared label, a reaction, a new verdict, a new Weight alone (`weight`), and a topic edit (`taxonomy`) |
| V2. Pages, properties, Why lines and bodies are built and written as specified, and only when they changed | pytest with Notion faked over HTTP. Request bodies for: a must read with a Deep card, a worth-a-look, a delisted paper. A repeated event sends no request. A 429 is waited out |
| V3. A first start builds the database, its columns, the views, "How to read this", and a page for every qualifying paper | A real run on the sandbox against the scratch page. The page count equals the sandbox's count of papers at relevant or above for the exported topics. Screenshot |
| V4. A grade that falls shows No longer listed with the right Why, and listing again clears it | On the sandbox: label a listed paper off-topic, run the dispatcher, read the page; remove the label, read it again |
| V5. New text reaches the page | On the sandbox: an event for a paper whose Brief changed rewrites its body; an event for a paper whose text did not change makes no write |
| V6. The owner's view arrangement survives a restart | In the scratch database: rearrange one view and hide columns in it, delete another view, restart, compare |
| V7. Reconcile follows the configuration | Restart with `minimum_grade = "must_read"`: worth-a-look papers become No longer listed with the minimum-grade Why. Restart with one topic dropped and another added: the dropped topic's rows stay unwritten, the new one gets its column, view and papers |
| V8. Ana's database matches what litwatch holds | After P3: the page count and the per-topic grades, compared with production's papers at relevant or above for the exported topics (a read-only query) |

## Scope

**In:**
- The `cause` field.
- The `notion` integration as specified, including reconcile for the topic set and the minimum grade.
- Its documentation.
- Switching it on for the owner's instance.
- Deleting the sample page.
- Trashing 0.14's Papers, Digest and Selections databases and the linked view.

**Out:**
- Anything flowing back from Notion into litwatch (Status, comments). Revisit only if Ana stays.
- Digests in Notion.
- Any Settings page for Notion beyond the generic integrations list.
- The Research workspace's Zotero-fed Papers source. It is never touched.
