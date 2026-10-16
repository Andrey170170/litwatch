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
- `backup.py`: consistent database and manifest exports, each a directory `export-<time>` behind the symlink
  `current`; it deletes only what it proves an export wrote (Backups).
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

## PDF policy

`paper-archive pdfs [all|wanted]` shows or sets which PDFs the archive downloads. Under `wanted`, the default for a
new archive, a PDF job at priority 100 (background) or a larger number waits as `held` until someone asks for it
at a smaller one (litwatch asks at 50, or 0 when a PDF is opened), and `pdfs all` releases every held job. An archive that held papers before the setting existed reads
`all`. LaTeX sources are never held. Recovery campaigns enqueue at priority 200, so under `wanted` their PDF jobs are
held; give `paper-archive-recovery prepare` a `--priority` below 100 to fetch them.

## Exact titles

A search by relevance puts first the papers whose title is the query itself, case, accents and punctuation aside: an
indexed lookup of each title's key (`title_keys` in the search index). The indexer keys every title it writes; an
index built before the keys existed needs them once, after the indexer that keys titles is running: an older
indexer still writing would leave its titles unkeyed once the keys are marked complete. Owner step, on the owner's
server:

1. Back up the catalog (Backups).
2. Install this archive into the instance's environment and restart its services:

   ```sh
   uv pip install --python /srv/arxiv-work/env/bin/python --reinstall-package arxiv-archive /home/dell-server-central/projects/litwatch/archive
   sudo systemctl restart paper-archive.service paper-archive-worker.service paper-archive-indexer.service paper-archive-web.service paper-archive-litwatch.service
   ```

3. Key the titles, beside the running indexer:

   ```sh
   /srv/arxiv-work/env/bin/paper-archive index-titles
   ```

It works in batches, resumes after a stop and can run again; on the owner's index (3.17M papers) it took about two
minutes and adds about 0.6 GB. Until it has run, searches skip the exact-title lookup and rank by their other tiers.

## Backups

`paper-archive-backup --project <checkout>` exports the catalog, the progress ledger, the recovery campaigns'
metadata and the project's `manifests/` into `/srv/backups/arxiv-catalog`, for Restic to upload. On the owner's
server the manifests stay in the `arxiv-archive` checkout (`docs/specs/v1/archive.md`), so the export is

```sh
sudo /srv/arxiv-work/env/bin/paper-archive-backup --project /home/dell-server-central/projects/arxiv-archive
```

which `restic-backup.service` also runs before each nightly upload, and `restic-arxiv-backup.sh` before its own.

Each export is a directory `export-<time>`, made durable before the symlink `current` names it. An export deletes
only what it can prove an export wrote: a staging directory `.arxiv-export-*` holding the marker it writes when it
makes one, and an earlier `export-<time>` holding its `export.json`. Everything else stays, whatever its name.

The exports before `current` wrote their files flat into the destination; no export deletes them. Owner step, once,
after the first export in the new layout, whose report the first command shows:

```sh
sudo cat /srv/backups/arxiv-catalog/current/export.json
sudo flock /srv/backups/arxiv-catalog/.export.lock rm -r -- /srv/backups/arxiv-catalog/{acquisition-job.json,AGENTS.md,BACKFILL.md,CATALOG-SERVICE.md,IMPLEMENTATION-PLAN.md,README.md,SEARCH-RUNBOOK.md,SERVICE-RUNBOOK.md,STAGE1-DEMO.md,STAGE3-RECOVERY.md,SHA256SUMS,SHA256SUMS.handoff-2026-09-21,catalog.sqlite,progress.sqlite,export.json,pyproject.toml,requirements.lock,campaigns,manifests}
```

## Container

`Dockerfile` builds the image litwatch's stack runs (`deploy/compose.yaml`) as the API, the worker and the indexer,
with `/srv/arxiv-work` and `/srv/arxiv-archive` as volumes. In a container the API's token comes from
`PAPER_ARCHIVE_TOKEN` when no `--token-file` is given, and the commands run with `--no-mount-guard`, since the ZFS
mount checks are the owner's server's.
