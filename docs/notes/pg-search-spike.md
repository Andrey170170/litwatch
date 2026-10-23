# pg_search over the whole archive: a throwaway check

2026-10-21. The question: can ParadeDB's `pg_search`, inside Postgres, be the one search engine over all 3.17M
archive papers on the owner's server, fast enough for search as you type and better than today's FTS5 search on
partial words and typos? Targets, warm, measured at the client: p95 ≤ 150 ms for the collection, ≤ 300 ms for the
whole search, broad queries included. Scripts were in `/tmp/lwbench/` and are not kept.

**Verdict: yes, with three things the design must do** (below). Whole-archive search: every one of the 15 queries
has a warm p95 ≤ 89 ms, against 1,265 ms for `language model` on FTS5 today. The target paper ranks first for every
prefix and typo query; FTS5 finds none of them. Collection-filtered search (join on an id set): p95 ≤ 162 ms, a
little over the 150 ms target in 2 of 30 runs. An indexed flag measured 6 ms instead, which closes the gap. The
first query after a Postgres restart takes 1–4 s, and the design must warm the index after a restart.

## Setup

- Image `paradedb/paradedb:latest-pg17`: PostgreSQL 17.11, `pg_search` 0.26.2, with `vector` 0.8.6 preinstalled
  (`CREATE EXTENSION vector` works). Licence: ParadeDB Community is AGPL-3.0, single node; Enterprise is
  commercial and adds replication.
- Container `lw-pgsearch-spike`, `--cpus 3 --memory 8g`, `PDB_TUNE=false`, `shared_buffers=2GB`,
  `maintenance_work_mem=1GB`, `work_mem=64MB`, `effective_cache_size=6GB`, `max_parallel_maintenance_workers=2`,
  `max_parallel_workers_per_gather=2`, `max_wal_size=8GB`, `synchronous_commit=off`. Production ran alongside.
- Data: `documents` from `/srv/arxiv-work/search/search.sqlite` (read-only), 3,170,429 rows: id, title,
  abstract, authors, categories, published, doi.
- Index: `USING paradedb (id, title stemmed (simple + stemmer=english), title unstemmed (alias title_raw),
  abstract stemmed, abstract unstemmed (alias abstract_raw), authors, published)`. Also a btree on a normalized
  title key (lowercase, non-alphanumerics to spaces), for the exact-title tier.

| Step | Time | Size | Peak container memory |
|---|---|---|---|
| COPY 3.17M rows | 95 s | heap 3.9 GB | 0.4 GiB |
| BM25 index, stemmed fields only (first try) | 3 min 04 s | 1.9 GB | 3.2 GiB |
| BM25 index with unstemmed aliases (used) | 5 min 02 s | 3.7 GB | 3.4 GiB |
| Exact-title btree | 21 s | 315 MB | — |
| Word vocabulary (title + abstract tokens, ≥3 uses, 3–40 chars) + trigram index | 3 min 41 s | 462k words, 44 MB | 0.9 GiB |

The container used 2.1 GiB at rest after the runs. FTS5 today is 9.3 GB. Here the table and its indexes come to
8.2 GB, and they hold the text itself, not only an index of it.

## Search method

Each search runs tiers. Each tier is a Top-K query (20). The tiers run in order and stop once the page is full:

1. exact title (btree), newest first;
2. title phrase, last word a prefix (`pdb.phrase_prefix` on `title_raw`);
3. every word in the title in any order, last word a prefix;
4. abstract phrase, last word a prefix;
5. BM25 with every word required in some field, title boosted 8×, last word also as a prefix.

Tiers 2–4 add a scoring clause, `title ||| q::pdb.boost(8) OR abstract ||| q OR id @@@ pdb.all()`, so rows are
ordered by BM25 within each tier. Before the tiers, a **correction** step runs. The vocabulary is searched with
`pg_trgm` and ordered by `levenshtein`. Only words of at least 4 letters used fewer than 1,000 times are looked up.
The last word is left alone while it begins a known word. A word is replaced if it is unknown, or if a word 1 edit
away is used at least 100× as often. Timing was taken client-side (psycopg over loopback), one connection, one
query at a time. "First" means the first run after `docker restart`, queries in order; the OS page cache was not
dropped (no sudo). Warm figures are p50 and p95 over 20 runs.

## Whole archive

| Query | Target | FTS5 today: rank, warm p50 | pg_search: rank | first | p50 | p95 | corrected |
|---|---|---|---|---|---|---|---|
| Comparing | 2610.24521 | –, 22 | – | 977 | 29 | 29 | |
| Comparing spars | 2610.24521 | none found, 2 | **1** | 36 | 42 | 49 | |
| Comparing sparsafied | 2610.24521 | none found, 1 | **1** | 140 | 50 | 57 | sparsified |
| Comparing sparsified | 2610.24521 | 1, 8 | 1 | 51 | 49 | 50 | |
| attention is all you need | 1706.03762 | 1, 88 | 1 | 55 | 11 | 11 | |
| language model | | 1,265 | | 4,118 | 80 | 85 | |
| neural network training | | 45 | | 142 | 22 | 22 | |
| sparse autoencoder interpretability | | 17 | | 22 | 7 | 7 | |
| transcoder | 2610.24521 | 9, 2 | **2** | 42 | 34 | 35 | |
| author:olah (`authors` field only) | | 5 (all fields) | | 16 | 1 | 2 | |
| sparse autoencoders find highly interpretible features in language models | 2309.08600 | none found, 5 | **1** | 128 | 58 | 64 | interpretable |
| denoising difusion probabilistic models | 2006.11239 | none found, 3 | **1** | 57 | 19 | 20 | diffusion |
| language models are few shot lerners | 2005.14165 | none found, 3 | **1** | 128 | 82 | 89 | learners |
| deep residual lerning for image recognition | 1512.03385 | none found, 3 | **1** | 122 | 47 | 49 | learning |
| transcoders neurons disguise | 2610.24521 | 1, 3 | 1 | 39 | 27 | 31 | |

Times are in ms. "–" means not in the top 20. "Comparing" alone matches thousands of titles, so neither engine
can be expected to find the paper from it.

## Collection filter

The same 15 queries were run restricted to id sets: the 7,713 real collection ids (read-only from production),
and 120k ids (those plus a random sample, standing in for a year from now). Each tier gets
`AND id IN (SELECT id FROM coll)`.

| Scope | warm p95, max over queries | warm p50 range |
|---|---|---|
| 7.7k ids | 160 ms (Comparing spars); the rest ≤ 115 | 10–119 ms |
| 120k ids | 162 ms (denoising difusion…); the rest ≤ 134 | 10–117 ms |

With a filter, `pg_search` loses the Top-K plan: it scans every match, hash-joins and sorts, so the cost grows
with the number of matches. Broad BM25 tier (`language model`), alone:

| Filter strategy | p50 | max |
|---|---|---|
| `id IN (SELECT …)`, 7.7k / 120k | 135 / 163 ms | 2,722 (first run) / 207 ms |
| `id = ANY($ids)`, 7.7k / 120k | 99 / 686 ms | 170 / 984 ms |
| `id @@@ pdb.term_set($ids)`, 7.7k / 120k | 130 / 797 ms | 164 / 838 ms |
| **indexed boolean flag in the BM25 index** (7.7k of a 120k-row index) | **6 ms** | 9 ms |
| a separate collection-size index (120k rows), no filter | 12 ms | 26 ms |

## Updates

Upserting 1,000 rows (500 updates, 500 inserts) took 91 ms. They were searchable as soon as the transaction
committed (all 1,000 found, 37 ms): the index is transactional, with no refresh lag. A single-row update took
0.6 ms p50 (max 4.3), which is what a paper joining the collection would cost if it sets a flag.

## What the design must do (quality findings)

1. **Prefix matching needs an unstemmed field and `pdb.phrase_prefix`.** On the stemmed field, `::pdb.fuzzy(0, t)`
   and the parser's `word*` stem the query first: `spars` became `spar`, and `sparsi` found 0 titles where 2,772 hold
   a word starting with it.
2. **`phrase_prefix` expands to only 50 terms by default, taken alphabetically, and drops the rest silently.**
   `comparing spars` found nothing at 50 and the paper at 500. 1,000 was used here and cost little.
3. **Prefix and phrase queries all score 1.0.** Without the added BM25 clause, a tier comes back in id order:
   for `transcoder` the paper was 27th; with the clause it is 2nd.
4. **Typos: correct from a vocabulary, not with fuzzy matching.** `pdb.fuzzy` on every word (distance 1–2) took
   p50 210–340 ms and p95 310–530 ms. It also missed 2006.11239 in the top 20 and put the sparsified paper 4th.
   The vocabulary lookup costs 6–14 ms per word looked up and gets all five typo cases right. The corpus itself
   contains typos (`difusion` 15 times, `transfomer` 21), which is why the rule compares frequencies. Words under
   4 letters must be left alone: an early version turned `is` into `ism` and `in` into `int`.
5. **Snippets:** `pdb.snippet` works on stemmed fields when the query has a clause on that field (14–21 ms for 20
   rows). It fails on an alias field ("Var attname should be in lookup") and is not supported for fuzzy queries.
   So snippets come from the stemmed scoring clause, and a prefix only highlights where its full word matched.
6. **Collection filters should push down**: put a flag (or the filters that matter) into the BM25 index, or give
   the collection its own index. A join on ids is the slow path.
7. **Cold start:** the first query after a Postgres restart took up to 4.1 s (`language model`), 0.9–1.0 s for
   others. The OS cache was not dropped, so a disk-cold start would be slower. Warming the index after a restart
   (e.g. `pg_prewarm`) belongs in the design.

## Caveats

- One machine, shared with production. The page cache also holds the 9.3 GB SQLite index and serves both engines,
  so the FTS5 numbers are warm too. Production load during the runs was not controlled.
- One client and no concurrent queries. 15 queries are a smoke test, not the labelled eval the spec calls for.
- The correction rule (thresholds 1,000 and 100×, 4 letters) was tuned on these same examples, so it may overfit.
  The vocabulary leaves out author names and needs upkeep as papers arrive (rebuilt, or kept incrementally).
- The 120k id set is a random sample, not a real collection. Real topic and rarity filters are richer joins than
  an id set.
- The update test changed 500 rows of the scratch copy. That has no effect on the timings above.
