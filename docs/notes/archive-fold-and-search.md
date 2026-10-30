# Folding the archive in, then rebuilding search: the plan for the planning

Status (2026-10-21): an outline agreed in conversation, before shaping. Two projects come out of it, shaped and
built in this order. Nothing here is a spec yet; the agreed points become spec text when each project is shaped.

## How we got here

Search was the starting point: it is hard to use. "Comparing sparsified MLPs and Transcoders: …" (2610.24521) was
found only once the owner had typed "Comparing sparsified" whole; "Comparing spars" and "Comparing sparsafied"
found nothing. The two engines today have no prefix matching and no typo handling, and they disagree: litwatch's
collection uses PostgreSQL's English stemming, the archive's FTS5 index none. The MCP tool promises `-word` and `OR`,
which the archive reads as required words.

Making the ranking consistent across the collection and the archive led to one index over every paper, which put
the index in litwatch's PostgreSQL, where topic, rarity, saved and thumbs are joins. That led to the real friction:
the archive is a separate package only by history (decision 49 moved its repository in as it was). Unlike the
integrations and deep, it is required (the only downloader, the source of full text, soon the search corpus), and its
catalog would share litwatch's database. So it folds into litwatch.

## Agreed

- **The archive folds into litwatch.** One Python package, one image. The catalog and the acquisition queue become
  tables in litwatch's PostgreSQL through `store` and its migrations; their records become `core` types. The
  acquisition worker stays its own process (one image, several commands), so a stuck download never touches the API.
  The HTTP API and client between litwatch and the archive, the socket forwarder and the vendored wheel go.
- **Everything runs in Docker,** the owner's instance included: its `paper-archive*` systemd units move into the
  compose stack with `/srv/arxiv-work` mounted.
- **The archive stays required;** what varies between installs is its size. A fresh install's catalog fills from
  intake; the owner's holds 3.17M papers and, since intake's catalog pass (decision 88), every new paper's metadata.
  One code path serves both.
- **Order: the fold first, to the point where current behaviour is reproduced; then search on top.**
- **Search stays lexical,** fast, and much better with partial words, word forms and typos; indexes may be rebuilt.
  Typos are corrected automatically and said so ("Showing results for … · search instead for …"). Search runs as the
  owner pauses typing. Snippets with highlighted matches are wanted. The web app and MCP share one query syntax:
  `"phrase"`, `-word`, `OR`, `title:`, `author:`, `abstract:`. Ranking is one, the same for every band.
- **With a query, the web app keeps rarity shelves;** MCP gets one list by relevance.
- **Not now:** searching Gists and Briefs; semantic search, though the design leaves room for it (vectors beside the
  catalog, hybrid ranking).

## Project 1: the fold

Done when the archive's code lives in litwatch, its data in litwatch's PostgreSQL, the owner's instance runs it all
in Docker, and every current behaviour is reproduced: intake, acquisition at priorities, full text, PDFs and pages,
MCP reading, backups, and search returning what it returns today.

Shaping settles:

- the layout: where catalog, acquisition, file store and text extraction land among litwatch's packages, and how the
  `paper-archive` commands become `litwatch` commands;
- search during the fold: FTS5's indexer reads the catalog's SQLite today, so the fold either keeps it working
  against PostgreSQL for a while or brings the new engine in early (the spike below informs this);
- the cutover of the owner's instance: 3.17M catalog records (an 8.2 GB SQLite file), the queue's state, the
  artifact inventory; the SQLite files kept until the owner is satisfied;
- backups (`backup.py` and its export generations, against `pg_dump` plus the file manifests), `recovery.py`, and
  `campaign.py` (the recovery campaign's ledger): kept, replaced or dropped;
- PostgreSQL sizing (it runs with the default 128 MB `shared_buffers`) and its image;
- the archive's tests moving from unittest to pytest on `TEST_DATABASE_URL`;
- `docs/install.md` and `deploy/` for a lab mate's install;
- the specs and decisions it revises: decision 49 and `archive.md` (where the code lives; "no other database server
  is needed"), and the archive's own read-only web interface, retired.

The codebase review's findings for the fold follow; they are input to shaping, not decisions.

### From the codebase review (2026-10-21)

A read-only review of `eb9a401` went through `archive/` and its seam with litwatch module by module. The wider
review's other findings are in [codebase-review-2026-10-21.md](codebase-review-2026-10-21.md).

**Where each module goes.**

- *Moves as it is:* `source_text.py` (LaTeX to text, safe tar reading; litwatch has nothing like it); `pacing.py`, as
  one copy imported directly, still across processes, since intake and the worker stay separate processes; the path,
  fsync and free-space helpers in `storage.py`, without its hard-coded ZFS datasets and mount roots, which don't
  describe Docker volumes.
- *Rewritten against `store` and `core`:* `catalog.py`, keeping deduplication, priority promotion, leases, the merging
  of late metadata and unknown historical versions; `worker.py`'s orchestration, its acquisition behaviour kept;
  `store.py`, which mixes files, SQL and presentation: its file publication moves, its `Catalog` goes to `store`.
- *Replaced:* `search.py`'s FTS5 queries (its metadata normalisation, used outside search too, moves to the shared
  conversion); `search_indexer.py`, whose triggers, baseline copy and idle writes (about 43,000 transactions a day)
  exist only because the catalog and its index are two SQLite files; `citations.py` by `publications/bibtex.py`,
  bringing over its parsed family names and escaping; `cli.py` by `litwatch` commands; the archive's token, config
  and logging by litwatch's.
- *Dropped:* `api.py`'s transport (its operations stay), `web_server.py` and `web/`, `recovery_status.py` (reads
  systemd), the separate package identity, and the one-off tools `Catalog.import_history`, `index_dates`,
  `index_titles`, `strip_source` and `sweep_replaced_sources` once cutover is checked, their inputs kept recoverable.
- `campaign.py` is the recovery campaign's ledger (frozen selection, reasons, delivery receipts), not the downloader.
  With `recovery.py` (hard-coded manifest, start date, CS categories) and `discovery.py` it leaves the runtime once the
  outstanding recovery is settled; its records and receipts are kept. Retry, enqueue at a priority and status stay as
  ordinary acquisition commands.

**Duplicates to merge.** OAI raw records and GetRecord in `discovery.py:62,244` and `intake/oai.py:154,238` (neither
parser keeps everything alone: the archive's holds more raw fields and requires v1, litwatch's joins canonical
authors); three Retry-After parsers (`pacing.py:31`, `intake/http.py:20`, `intake/search.py:40`) and two XML retry
loops; arXiv id validation in five places; metadata passed through several lossy shapes on its way to `Paper`, dates
rebuilt as midnight.

**A bug the fold fixes, or a small PR before it.** `store.identity` (`archive/.../store.py:28`) accepts only
lowercase old-style ids, while the catalog and litwatch accept `math.AG/0601001`: for such a paper the metadata is
found and its PDF, text and source fail.

**What the seam contorts, and goes with it.**

- Registration is one outbox event turned into a metadata PUT and two job POSTs, replayed when delivery is partial
  (`integrations/archive.py:164`); it becomes local writes in one transaction, and the branch that keeps
  archive-origin events from registering back (`:67`) goes.
- Errors are translated three times (server exception, HTTP status, `ArchiveError`, the caller's own); typed outcomes
  replace them, keeping missing, waiting, unavailable, failed and inconsistent apart. PDFs are copied through HTTP
  into a bytearray and again into bytes; LaTeX travels as one large JSON string.
- Four waiting loops (the client's PDF wait, MCP, the eval harness, full-text ticks) become one bounded wait over the
  queue's state. Waiting itself stays: the worker is still another process.
- The synchronous search adapter reaches into the client's private headers and layers its deadline over an executor's
  (`integrations/archive.py:139,186`, `api/integrations.py:266`).
- The archive's optionality plumbing in litwatch: `register`, `reader`/`source`/`switched_off`, `fulltext.archive_for`,
  deep's archive config check, and WebDAV's own archive reader.
- Endpoints litwatch never calls: `source`, `sources`, `source-file`, `PUT pdf`/`source`, both BibTeX routes, `status`.
- Also going: the vendored wheel, the disposable-archive script in `scripts/`, archive-only CI and build steps, links
  to `docs/ARCHIVE-STAGE2.md`.

**Easy to lose at cutover.**

- An old catalog without a PDF setting means `pdfs=all`; a fresh one defaults to `wanted`.
- Source version 0 means unknown, not v1.
- `latest_version` can move ahead on artifact publication; it is not always the metadata's version (the listing avoids
  that substitution on purpose, `search.py:172`).
- Artifact paths, compression, hashes and provenance; historical LaTeX locators (PyArrow stays for them); each job's
  identity, state, priority, attempts and retry time.
- Archive records must not go through the collection's `upsert`, which adds corrections, revisions and events; 3.17M
  records load in bulk, not paper by paper.
- On litwatch's side: incremental OAI harvest by modification date, apart from the submission-window backfill; Atom
  snapshots and unresolved checkpoints; shared pacing; the PDF extraction and rendering lock with the optional AGPL
  reader.

**Fixed in the rewrite rather than carried over.**

- One cross-process read lock (`store.read_lock`, about ten places in `api.py`) is held through decompression, hash
  checks and LaTeX parsing, so metadata reads and writes and job enqueue, get and retry wait behind any cold read.
  Narrow it to cache publication and destructive swaps; keep extraction off the API's event loop.
- Downloads have connect and read timeouts but no total deadline (`worker.py:146`); a trickling response of up to
  2 GB holds the only worker while urgent jobs wait. Add a transfer budget, as `intake/http.py:79` does for reads.
- The date-window listing decodes each record's JSON before filtering categories (`catalog.py:247`); in PostgreSQL,
  index dates and categories and filter first, keyset paging kept.
- Text is reparsed from the cached source on every read, and MCP's outline and reading each fetch the whole text;
  cache derived text by artifact identity if repeated reads justify it.
- Historical LaTeX lookup scans whole row groups and checks the listing even on cache hits (`store.py:349,364`).
- Readiness is a cheap check, apart from the full inventory count `status` does under the lock today.

**For the open shaping questions.**

- Backups: `pg_dump` replaces the SQLite snapshots; manifests, source and HF data and the historical evidence are still
  backed up. Export generations need not stay an application subsystem.
- Tests: pytest collects the 13 unittest modules as they are; only the catalog and queue tests are rewritten on
  `TEST_DATABASE_URL`, tests of the HTTP and browser transport go, and the cross-test imports are fixed.
- The retired web interface also shows filters and paging, index progress, BibTeX for several papers, versioned
  downloads, artifact sizes and status, source-file browsing and acquisition errors. Shaping says which, if any,
  survive as administration, so they don't become implied requirements.
- The spike's 8.2 GB is the search table and its indexes, not catalog, queue and collection together; sizing is open.
- The SQL still outside `store` (31 grandfathered places, `tests/test_sql_home.py`) is worth moving first, as the
  ground the catalog and queue land on.

## Project 2: search

Done when the labelled query set (below) shows the hard cases fixed without exact titles getting worse, inside the
speed targets, in the web app and through MCP.

Shaping settles, starting from these recommendations, which the owner has not yet confirmed:

- the engine: ParadeDB's `pg_search` (BM25 inside PostgreSQL, with typo, prefix and snippet support), if the spike
  passes; otherwise our own correction and tiers over PostgreSQL's full-text search;
- the ranking tiers, kept from the archive's (exact title, title phrase, abstract phrase, BM25 with the title
  weighted over the abstract), with "every query word in the title" added after the title phrase; the last word
  matched as a prefix;
- typo correction: only words the index does not know or barely knows, so rare correct terms stay;
- speed targets, warm, on the server: p95 ≤ 150 ms for the collection, ≤ 300 ms for the whole search, broad queries
  such as "language model" included; requests about 200 ms after the last keystroke, older ones cancelled, the
  collection and the archive band loading on their own;
- snippets: matched words highlighted in titles; an abstract excerpt when the match is only in the abstract, in place
  of the Gist line while a query is active, and as a field in MCP results;
- the specs and decisions it revises: decisions 45 and 59 and `archive.md`, Search.

How it is judged: a labelled query set in `evals/` (the owner's failed searches, typos and prefixes generated from
known titles, the exact-title cases from N5b), scored by recall@10 and MRR, with p95 latency, before and after each
stage.

## The spike

A throwaway check, before either project is shaped: load the archive's 3.17M papers into a scratch ParadeDB
PostgreSQL on this server and measure load and index time, size, memory, latency on the queries above, typo and
prefix behaviour, filtering by collection-sized id sets, update cost, and whether `pgvector` is available. Its report
is [pg-search-spike.md](pg-search-spike.md).

It passed (2026-10-21): every whole-archive query under 90 ms p95 warm ("language model" 80 ms, against 1,265 ms on
FTS5), the owner's prefix and typo searches and four misspelt titles first, updates searchable at commit, `pgvector`
in the same image. What it puts in front of the projects: collection filters must be pushed into the BM25 index (a
flag there, 6 ms) rather than joined (up to 162 ms); prefixes need unstemmed fields and a raised expansion limit;
typos are corrected from a word vocabulary, not by fuzzy matching; the index is warmed after a restart. For the fold,
it makes the simpler path to "search returns what it returns today" moving to the ParadeDB image and porting the
archive's tiers onto `pg_search`, rather than keeping FTS5 alive against PostgreSQL.

## Facts gathered (2026-10-21)

- Collection: 6,917 papers, all classified; about 400–600 a weekday, so about 120k a year.
- Archive: catalog 8.2 GB SQLite, FTS5 index 9.3 GB, 3.17M papers; the newest 500 collection papers are all indexed.
- Server: 6 CPUs, 31 GB RAM (about 12 GB free); PostgreSQL 17, a 172 MB database.
- Today's FTS5, warm (cold): "Comparing sparsified" 8 ms (26 ms), the paper first; "attention is all you need"
  78 ms (499 ms); "language model" 1,296 ms (2,191 ms); a cold first query about 2 s.
