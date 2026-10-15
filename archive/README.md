# arxiv-archive

The archive litwatch ships with: a catalog of arXiv papers, a store for their PDFs and the text of their LaTeX source
packages, a durable acquisition queue with priorities, and lexical search over the catalog. Its role in litwatch is
[`docs/specs/v1/archive.md`](../docs/specs/v1/archive.md).

The code came from `Andrey170170/arxiv-archive` at commit 6c57506 (package version 0.5.1). That repository keeps the
history before the move and the records of the owner's instance: the acquisition manifests and evidence, the
historical downloader, and the runbooks for the owner's server.

## Package and interfaces

The installable package is `src/arxiv_archive/`:

- `catalog.py`: paper and version metadata, the artifact inventory, LaTeX locators and durable priority jobs.
- `store.py`: PDF and source publication, retrieval and a bounded reading cache.
- `source_text.py`: bounded source-package parsing, the text-only filter and a paper's text read from its source.
- `storage.py`: mount guards and filesystem durability primitives.
- `api.py`: authenticated internal HTTP endpoints.
- `cli.py`: administration commands and server lifecycle.
- `worker.py`: acquisition, shared provider pacing and retries.
- `backup.py`: consistent database and manifest exports.
- `search.py` / `search_indexer.py`: bounded lexical search and resumable incremental indexing.
- `web_server.py` / `web/`: the read-only browser interface, retired once litwatch covers it (decision 49).
- `citations.py`: preprint BibTeX exports.

The runtime uses a canonical SQLite catalog and a separate, rebuildable FTS5 search database. Acquisition,
incremental indexing and the web interface run as bounded processes; no other database server is needed.

## Development

Python 3.12 or later. `requirements.lock` pins the tested runtime libraries; `pyproject.toml` defines the package and
its commands (`paper-archive`, `paper-archive-worker`, `paper-archive-indexer`, `paper-archive-web`,
`paper-archive-backup`, `paper-archive-recovery`).

```sh
cd archive
uv venv && uv pip install -r requirements.lock -e .
.venv/bin/python -m unittest discover -s tests
```

Tests use temporary directories and touch no real storage.
