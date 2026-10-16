# The archive

arxiv-archive is the store of arXiv papers: metadata, LaTeX source text and PDFs, with search over them. In v1 it
stops being an optional integration and becomes **part of the stack**: litwatch is deployed with it.

## Role

- **The only downloader.** litwatch never fetches a paper's files from arXiv itself. The archive downloads them under
  one shared arXiv pacing (at least 3 seconds between connections; one paper is a source and a PDF, about 6 seconds),
  so the limit is respected in one place. litwatch's current direct-arXiv fallback for PDFs is removed.
- **Priorities.** Intake registers each new paper at background priority. A full-text pass asks for the paper's
  source at urgent priority ([pipeline.md](pipeline.md), section 6), so the 10–30 papers a night that matter arrive
  within minutes while the rest of the night's 1,000 trail behind.
- **Full text.** The archive serves the text of a paper's LaTeX source (its text-only filter keeps the LaTeX and
  drops figures and data: from several GB to a few MB per paper). When a paper has no source, the PDF's text is used.
- **Search.** The archive is the search engine for papers (see below).
- **Later.** Semantic search, a citation graph and an MCP server for research agents belong in the archive and become
  available to litwatch through it.

## Deployment

The template's compose file runs litwatch, its Postgres, the archive and, optionally, `deep`. A fresh archive starts
**empty** and fills from litwatch's intake; the owner's instance was seeded with almost all of arXiv (3.17M papers),
which a lab mate does not need.

### Storage policy

PDFs are about 4 GB a day at the owner's intake; LaTeX text is a few MB per paper. The policy, for a fresh instance
and for the owner's (whose payload disk has 2.0 TB free):

- **LaTeX text** for every paper intake registers;
- **PDFs** only for papers that are members of a followed topic's subtree with a grade of relevant or above, or
  saved, or opened by the owner, or with a full-text pass queued and no LaTeX source; others are fetched when
  opened.

The policy is configuration (an instance can keep every PDF), and changing it does not delete files already stored.

How it is kept (N5a). The archive holds the setting, `paper-archive pdfs all|wanted`: a new archive starts at
`wanted`, and one that held papers before the setting existed reads `all`. Under `wanted` a PDF asked for at
background priority waits as `held`; asking for it above background releases it, and `pdfs all` releases every held
job (`pdfs wanted` holds the background ones again). LaTeX is never held. litwatch asks above background for:

- a paper graded relevant or must read on an active topic, or saved: the archive integration reacts to
  `grade.changed` and `paper.saved` and asks at the time it handles the event, so a grade already taken back asks
  for nothing;
- a PDF opened: `GET /api/papers/{id}/pdf` asks urgently and waits up to the client's `wait_seconds`, then sends
  the reader to arXiv;
- a full-text pass with no LaTeX source: the queue already asks urgently for the PDF (N3).

## Search

One search box, one result list, as in the first v1 (decision 6), with these changes:

- **The archive ranks.** A query with no collection-only filter goes to the archive, which holds every paper including
  the collection's; litwatch keeps the archive's order and adds its own Mark to each hit that is in the collection
  (rarity, badges, Gist). Relevance is the default order whenever there is a query; date orders stay available.
- **Collection filters** (topic, saved, rarity) search litwatch's own index of the collection, ranked by relevance
  the same way.
- **Two fixes before the switch**, found 2026-10-13: the archive's relevance ranking does not put an exact title
  first ("attention is all you need" does not return 1706.03762 at the top; adding "Vaswani" does), so exact title
  and phrase matches must rank first and title must weigh more than abstract; and litwatch currently re-sorts the
  archive's hits by date, which discards the ranking.

## Papers outside the collection

Every arXiv paper has a paper page in litwatch, whether or not litwatch processed it. A paper outside the collection
shows its title, authors, abstract, links, BibTeX and no rarity. Two actions bring it into the collection, where it is
processed like any new paper: **Process** (classification, analysis, grading where it lands) and **Deep read** (the
same, then a Deep card). Reporting it as a miss does the same with the owner's word first
([feedback.md](feedback.md)).

litwatch keeps only the papers it processed, each with the text exactly as it was judged. That copy is a record of
what was judged, not a second source of truth: the archive holds the current paper.

## Where the archive's code lives

The archive is today a separate repository (`Andrey170170/arxiv-archive`, 21 commits) with its own runbooks. One of
v1's aims is one place for papers, so its code (the package, its tests and its build files) moves into this repository
as `archive/`, beside `deep/`, as one squashed commit, deployed as its own container or containers. A submodule would
keep two repositories and make every cross-cutting change two commits. The old repository stays as the record of the
owner's instance: its history, the acquisition manifests and evidence, the historical downloader and the runbooks
for the owner's server. Its read-only web interface is retired once
litwatch's Papers tab and paper pages cover search and paper pages, keeping its API. Decision 49 in
[decisions.md](decisions.md), agreed 2026-10-13.

## Deferred to N5

Found in review when the code moved in (PR #41), left as they were in the old repository. N5a derived
`__version__` from the package metadata and made `pyarrow` and `zstandard` base dependencies for the image; the
other two are N5b's:

- `backup.py` refreshes the stable export path one entry at a time, so a failure part-way leaves new database files
  beside old directories; swap the whole export at once.
- `recovery.py`'s reconcile marks every submitted candidate in its own fsynced transaction on each feed cycle; batch
  the writes and skip unchanged ones.
