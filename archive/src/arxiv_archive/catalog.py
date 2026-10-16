"""Transactional paper metadata, artifact inventory and durable acquisition queue.

Version 0 means unknown (historical source only); acquisition jobs require a known
positive version. Lower numeric priority runs first. Historical imports register
existing inventory and scan only LaTeX IDs, never payload content or hashes.

`paper_dates` indexes each paper by its first version's submission date, derived from
its metadata on every write, for listing papers by date window. A catalog that held
papers before the table existed lists nothing until `index_dates` has filled it.

The PDF policy (`settings`, key `pdfs`) is `all` or `wanted`. Under `wanted` a PDF job
asked for at background priority or below (a number of `BACKGROUND` or more) is `held`: kept, never claimed,
until someone asks for it at a higher priority, which makes it pending; one retried or
left by its worker after the policy changed is held when it would be claimed. Sources are never
held. A new catalog starts with `wanted`; one that held papers before the setting existed
keeps every PDF (`all`) until it is changed (`set_pdfs`).
"""
from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
import math
from pathlib import Path
import re
import sqlite3
import threading
import time
import uuid
from typing import Any, Callable

from .search import listing_item, published_date

PAPER_ID = re.compile(r"(?:\d{4}\.\d{4,5}|[A-Za-z][A-Za-z0-9.-]*/\d{7})\Z")
KINDS = {'pdf', 'source', 'text'}
SCHEMA = '''
CREATE TABLE IF NOT EXISTS papers (
 id TEXT PRIMARY KEY, metadata_json TEXT NOT NULL DEFAULT '{}',
 latest_version INTEGER, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS artifacts (
 paper_id TEXT NOT NULL REFERENCES papers(id), version INTEGER NOT NULL,
 kind TEXT NOT NULL, location TEXT NOT NULL, original_size INTEGER NOT NULL,
 original_sha256 TEXT, stored_size INTEGER NOT NULL, stored_sha256 TEXT,
 compression TEXT NOT NULL, media_type TEXT NOT NULL, provenance_json TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'available', updated_at REAL NOT NULL,
 PRIMARY KEY(paper_id,version,kind)
);
CREATE TABLE IF NOT EXISTS latex_locations (
 paper_id TEXT NOT NULL, shard TEXT NOT NULL, row_group INTEGER NOT NULL,
 PRIMARY KEY(paper_id,shard,row_group)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS jobs (
 id INTEGER PRIMARY KEY, paper_id TEXT NOT NULL REFERENCES papers(id),
 version INTEGER NOT NULL, kind TEXT NOT NULL, priority INTEGER NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
 not_before REAL NOT NULL DEFAULT 0, lease_until REAL, lease_token TEXT,
 error TEXT, provenance_json TEXT NOT NULL, created_at REAL NOT NULL,
 updated_at REAL NOT NULL, UNIQUE(paper_id,version,kind)
);
CREATE INDEX IF NOT EXISTS jobs_ready ON jobs(status,priority,created_at);
CREATE TABLE IF NOT EXISTS import_checkpoints (
 source TEXT PRIMARY KEY, completed_at REAL NOT NULL, rows INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_dates (
 paper_id TEXT PRIMARY KEY, published TEXT NOT NULL
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS paper_dates_published ON paper_dates(published,paper_id);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
'''
DATES_INDEXED = 'paper-dates'  # the import checkpoint saying every paper has its date row
BACKGROUND = 100  # the priority of work nobody waits on; lower numbers run first
PDF_POLICIES = ('all', 'wanted')


def _json(value: Any) -> str:
    return json.dumps(value, separators=(',', ':'), ensure_ascii=False,
                      default=lambda x: x.isoformat())



def _metadata_date(metadata: dict) -> datetime | None:
    """Compare upstream dates, not local delivery times; HF uses update_date."""
    dates = []
    for value in (metadata.get('updated'), metadata.get('update_date')):
        if not isinstance(value, str):
            continue
        try:
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            continue
        dates.append(parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC))
    return max(dates) if dates else None


def _merge_metadata(old: dict, incoming: dict, prefer_old: bool) -> dict:
    # Default empty lists/nulls in a minimal feed observation are not deletions.
    incoming = {key: value for key, value in incoming.items()
                if value is not None and value != [] and value != ''}
    if prefer_old and _metadata_date(old) is not None:
        for key in ('updated', 'update_date'):
            if key not in old:
                incoming.pop(key, None)
    merged = {**incoming, **old} if prefer_old else {**old, **incoming}
    # A delayed observation can still contribute previously unknown revision history.
    histories = [incoming.get('version_history', []), old.get('version_history', [])]
    if not prefer_old:
        histories.reverse()
    versions = {}
    for history in histories:
        if not isinstance(history, list):
            continue
        for entry in history:
            if isinstance(entry, dict) and isinstance(entry.get('version'), int):
                version = entry['version']
                versions[version] = {**versions.get(version, {}), **entry}
    if versions:
        merged['version_history'] = [versions[v] for v in sorted(versions)]
    return merged


def validate_id(paper_id: str) -> None:
    if not isinstance(paper_id, str) or not PAPER_ID.fullmatch(paper_id):
        raise ValueError('expected canonical unversioned arXiv paper ID')


def _artifact_key(paper_id: str, version: int, kind: str, *, job: bool = False) -> None:
    validate_id(paper_id)
    if isinstance(version, bool) or not isinstance(version, int) or version < (1 if job else 0):
        raise ValueError('version must be a positive integer' if job else 'version must be nonnegative')
    if kind not in KINDS:
        raise ValueError('kind must be pdf, source or text')


class Catalog:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(path), timeout=30, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.executescript(SCHEMA)
        with self.db:
            # An empty catalog is completely indexed: every later write keeps it so.
            self.db.execute('''INSERT OR IGNORE INTO import_checkpoints
                SELECT ?,?,0 WHERE NOT EXISTS (SELECT 1 FROM papers)''', (DATES_INDEXED, time.time()))
            # A new catalog keeps only the PDFs asked for above background priority.
            self.db.execute('''INSERT OR IGNORE INTO settings
                SELECT 'pdfs','wanted' WHERE NOT EXISTS (SELECT 1 FROM papers)''')

    def close(self) -> None:
        with self.lock:
            self.db.close()

    @staticmethod
    def _decode(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        result = dict(row)
        for field in ('metadata', 'provenance'):
            if field + '_json' in result:
                result[field] = json.loads(result.pop(field + '_json'))
        return result

    def _index_date(self, paper_id: str, metadata: dict) -> None:
        """Keep the paper's date row in step with its metadata; call inside the write."""
        if published := published_date(metadata):
            self.db.execute('''INSERT INTO paper_dates VALUES (?,?) ON CONFLICT(paper_id)
                DO UPDATE SET published=excluded.published''', (paper_id, published))
        else:
            self.db.execute('DELETE FROM paper_dates WHERE paper_id=?', (paper_id,))

    def _ensure_paper(self, paper_id: str) -> None:
        self.db.execute('INSERT OR IGNORE INTO papers(id,updated_at) VALUES (?,?)',
                        (paper_id, time.time()))

    def upsert_paper(self, paper_id: str, metadata: dict, latest_version: int | None = None) -> dict:
        validate_id(paper_id)
        if not isinstance(metadata, dict):
            raise ValueError('metadata must be an object')
        if latest_version is not None and (isinstance(latest_version, bool) or
                not isinstance(latest_version, int) or latest_version < 1):
            raise ValueError('latest_version must be a positive integer or null')
        with self.lock, self.db:
            existing = self.get_paper(paper_id)
            if existing:
                old_version = existing['latest_version']
                old_metadata = existing['metadata']
                old_date, incoming_date = _metadata_date(old_metadata), _metadata_date(metadata)
                higher_version = latest_version is not None and latest_version > (old_version or 0)
                # Version identity wins first. Within a revision (including unknown),
                # delivery order must not roll back metadata-only edits or canonical dates.
                prefer_old = bool(old_version and
                                  (latest_version is None or latest_version < old_version))
                if not higher_version and old_date is not None:
                    prefer_old = prefer_old or incoming_date is None or incoming_date < old_date
                metadata = _merge_metadata(old_metadata, metadata, prefer_old)
                latest_version = max(old_version or 0, latest_version or 0) or None
            self.db.execute('''INSERT INTO papers VALUES (?,?,?,?) ON CONFLICT(id)
                DO UPDATE SET metadata_json=excluded.metadata_json,
                latest_version=excluded.latest_version,updated_at=excluded.updated_at''',
                (paper_id, _json(metadata), latest_version, time.time()))
            self._index_date(paper_id, metadata)
        return self.get_paper(paper_id)

    def get_paper(self, paper_id: str) -> dict | None:
        validate_id(paper_id)
        with self.lock:
            return self._decode(self.db.execute('SELECT * FROM papers WHERE id=?', (paper_id,)).fetchone())

    def dates_indexed(self) -> bool:
        with self.lock:
            return self.db.execute('SELECT 1 FROM import_checkpoints WHERE source=?',
                                   (DATES_INDEXED,)).fetchone() is not None

    def index_dates(self, batch: int = 5000, progress: Callable[[dict], None] | None = None) -> dict:
        """Derive every paper's date row from its metadata, then mark the index complete.
        Idempotent, and safe beside live writes, which keep their own rows current."""
        last, count = 0, 0
        while True:
            with self.lock:
                # Write-lock before reading, so another process's write cannot fall between
                # this read and the rows derived from it.
                self.db.execute('BEGIN IMMEDIATE')
                try:
                    rows = self.db.execute('SELECT rowid,id,metadata_json FROM papers WHERE rowid>? '
                                           'ORDER BY rowid LIMIT ?', (last, batch)).fetchall()
                    for row in rows:
                        self._index_date(row['id'], json.loads(row['metadata_json']))
                    self.db.commit()
                except BaseException:
                    self.db.rollback()
                    raise
            if not rows:
                break
            last, count = rows[-1]['rowid'], count + len(rows)
            if progress:
                progress({'papers': count})
        with self.lock, self.db:
            self.db.execute('INSERT OR REPLACE INTO import_checkpoints VALUES (?,?,?)',
                            (DATES_INDEXED, time.time(), count))
            dated = self.db.execute('SELECT count(*) FROM paper_dates').fetchone()[0]
        return {'papers': count, 'dated': dated}

    def list_papers(self, since: str, until: str, categories: set[str] | None = None,
                    after: tuple[str, str] | None = None, limit: int = 500,
                    scan: int = 20000) -> dict:
        """Papers whose first version was submitted from `since` to `until` (YYYY-MM-DD,
        both inclusive) in (date, id) order, as `listing_item`s; with `categories`, only
        papers in at least one of them. `after` is the (date, id) key a previous page ended
        at. A page reads at most `scan` date rows, so a sparse category can give a short or
        empty page; `next` is the key to go on from, None once the window is exhausted."""
        items, key = [], after or (since, '')
        while len(items) < limit and scan > 0:
            size = min(1000, scan)
            with self.lock:
                rows = self.db.execute('''SELECT d.published,d.paper_id,p.metadata_json
                    FROM paper_dates d JOIN papers p ON p.id=d.paper_id
                    WHERE (d.published,d.paper_id)>(?,?) AND d.published<=?
                    ORDER BY d.published,d.paper_id LIMIT ?''', (*key, until, size)).fetchall()
            scan -= len(rows)
            for row in rows:
                key = (row['published'], row['paper_id'])
                item = listing_item(row['paper_id'], json.loads(row['metadata_json']))
                if not categories or categories & set(item['categories']):
                    items.append(item)
                    if len(items) == limit:
                        break
            if len(rows) < size and len(items) < limit:
                return {'items': items, 'next': None}
        return {'items': items, 'next': key}

    def put_artifact(self, artifact: dict) -> dict:
        a = dict(artifact)
        _artifact_key(a['paper_id'], a['version'], a['kind'])
        if a.get('status', 'available') not in {'available', 'superseded', 'unavailable'}:
            raise ValueError('invalid artifact status')
        for field in ('original_size', 'stored_size'):
            if not isinstance(a[field], int) or a[field] < 0:
                raise ValueError(f'{field} must be nonnegative')
        with self.lock, self.db:
            self._ensure_paper(a['paper_id'])
            if a['version'] > 0:
                self.db.execute('''UPDATE papers SET latest_version=max(coalesce(latest_version,0),?),
                    updated_at=? WHERE id=?''', (a['version'], time.time(), a['paper_id']))
            self.db.execute('''INSERT INTO artifacts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(paper_id,version,kind) DO UPDATE SET
                location=excluded.location,original_size=excluded.original_size,
                original_sha256=excluded.original_sha256,stored_size=excluded.stored_size,
                stored_sha256=excluded.stored_sha256,compression=excluded.compression,
                media_type=excluded.media_type,provenance_json=excluded.provenance_json,
                status=excluded.status,updated_at=excluded.updated_at''',
                (a['paper_id'], a['version'], a['kind'], a['location'], a['original_size'],
                 a.get('original_sha256'), a['stored_size'], a.get('stored_sha256'),
                 a['compression'], a['media_type'], _json(a.get('provenance', {})),
                 a.get('status', 'available'), time.time()))
        return self.get_artifact(a['paper_id'], a['kind'], a['version'])

    def get_artifact(self, paper_id: str, kind: str, version: int | None = None) -> dict | None:
        _artifact_key(paper_id, 0 if version is None else version, kind)
        sql = "SELECT * FROM artifacts WHERE paper_id=? AND kind=? AND status='available'"
        params: list = [paper_id, kind]
        if version is not None:
            sql += ' AND version=?'
            params.append(version)
        sql += ' ORDER BY version DESC LIMIT 1'
        with self.lock:
            return self._decode(self.db.execute(sql, params).fetchone())

    def list_artifacts(self, paper_id: str) -> list[dict]:
        validate_id(paper_id)
        with self.lock:
            return [self._decode(r) for r in self.db.execute(
                'SELECT * FROM artifacts WHERE paper_id=? ORDER BY kind,version DESC', (paper_id,))]

    def unfiltered_sources(self, limit: int | None = None) -> list[dict]:
        """Available source packages stored before text-only filtering, oldest first.

        `limit` caps a destructive rewrite, so only None means all rows.
        """
        if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 0):
            raise ValueError('limit must be a nonnegative integer')
        sql = ("SELECT paper_id, version FROM artifacts WHERE kind='source' AND status='available' "
               "AND version>0 AND json_extract(provenance_json,'$.filter') IS NULL ORDER BY updated_at")
        with self.lock:
            rows = self.db.execute(sql + (' LIMIT ?' if limit is not None else ''),
                                   (limit,) if limit is not None else ())
            return [dict(r) for r in rows]

    def replaced_sources(self) -> list[dict]:
        """Source rows a text-only rewrite recorded as replacing an original package."""
        with self.lock:
            return [dict(r) for r in self.db.execute(
                "SELECT location, json_extract(provenance_json,'$.replaced_location') AS replaced "
                "FROM artifacts WHERE kind='source' "
                "AND json_extract(provenance_json,'$.replaced_location') IS NOT NULL")]

    def latex_locations(self, paper_id: str) -> list[dict]:
        validate_id(paper_id)
        with self.lock:
            return [dict(r) for r in self.db.execute(
                'SELECT shard,row_group FROM latex_locations WHERE paper_id=? ORDER BY shard,row_group',
                (paper_id,))]

    def pdfs(self) -> str:
        """The PDF policy: `all`, or `wanted` (see the module's docstring)."""
        with self.lock:
            row = self.db.execute("SELECT value FROM settings WHERE key='pdfs'").fetchone()
        return row[0] if row else 'all'

    def set_pdfs(self, policy: str) -> dict:
        """Change the PDF policy and bring the queue in line with it: under `wanted` pending
        background PDF jobs are held, under `all` held jobs are pending again. Stored files
        are never touched. Returns the policy and how many jobs changed."""
        if policy not in PDF_POLICIES:
            raise ValueError('the PDF policy is all or wanted')
        with self.lock, self.db:
            self.db.execute("INSERT INTO settings VALUES ('pdfs',?) ON CONFLICT(key) "
                            "DO UPDATE SET value=excluded.value", (policy,))
            if policy == 'wanted':
                cursor = self.db.execute('''UPDATE jobs SET status='held',updated_at=?
                    WHERE status='pending' AND kind='pdf' AND priority>=?''', (time.time(), BACKGROUND))
            else:
                cursor = self.db.execute("UPDATE jobs SET status='pending',updated_at=? "
                                         "WHERE status='held'", (time.time(),))
            return {'pdfs': policy, 'jobs_changed': cursor.rowcount}

    def _held(self, kind: str, priority: int, policy: str | None = None) -> bool:
        """Whether the PDF policy holds a job of this kind at this priority."""
        return kind == 'pdf' and priority >= BACKGROUND and (policy or self.pdfs()) == 'wanted'

    def enqueue(self, paper_id: str, version: int, kind: str, priority: int = BACKGROUND,
                provenance: dict | None = None) -> dict:
        """Ask for an artifact; asking again keeps one job and raises its priority. Under the
        `wanted` PDF policy a background PDF job is held until asked for above background."""
        _artifact_key(paper_id, version, kind, job=True)
        if isinstance(priority, bool) or not isinstance(priority, int):
            raise ValueError('priority must be an integer (lower runs first)')
        with self.lock, self.db:
            self._ensure_paper(paper_id)
            now = time.time()
            available = self.get_artifact(paper_id, kind, version) is not None
            status = 'complete' if available else 'held' if self._held(kind, priority) else 'pending'
            self.db.execute('''INSERT INTO jobs
                (paper_id,version,kind,priority,status,provenance_json,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(paper_id,version,kind) DO UPDATE SET
                priority=min(jobs.priority,excluded.priority), updated_at=excluded.updated_at,
                status=CASE WHEN excluded.status='complete' THEN 'complete'
                    WHEN jobs.status='held' AND excluded.status='pending' THEN 'pending'
                    ELSE jobs.status END''',
                (paper_id, version, kind, priority, status, _json(provenance or {}), now, now))
            return self._decode(self.db.execute('SELECT * FROM jobs WHERE paper_id=? AND version=? AND kind=?',
                                               (paper_id, version, kind)).fetchone())

    def get_job(self, job_id: int) -> dict | None:
        with self.lock:
            return self._decode(self.db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone())

    def claim_job(self, lease_seconds: float = 300) -> dict | None:
        if not math.isfinite(lease_seconds) or lease_seconds <= 0:
            raise ValueError('lease_seconds must be positive')
        with self.lock:
            self.db.execute('BEGIN IMMEDIATE')
            try:
                now, policy = time.time(), self.pdfs()
                while True:
                    row = self.db.execute('''SELECT * FROM jobs WHERE
                        (status='pending' AND not_before<=?) OR (status='running' AND lease_until<=?)
                        ORDER BY priority,created_at,id LIMIT 1''', (now, now)).fetchone()
                    if row is None:
                        self.db.commit()
                        return None
                    # A job retried or left by its worker after the policy changed is held now.
                    if not self._held(row['kind'], row['priority'], policy):
                        break
                    self.db.execute('''UPDATE jobs SET status='held',lease_token=NULL,lease_until=NULL,
                        updated_at=? WHERE id=?''', (now, row['id']))
                token = uuid.uuid4().hex
                self.db.execute('''UPDATE jobs SET status='running',attempts=attempts+1,
                    lease_token=?,lease_until=?,updated_at=? WHERE id=?''',
                    (token, now + lease_seconds, now, row['id']))
                self.db.commit()
                return self.get_job(row['id'])
            except BaseException:
                self.db.rollback()
                raise

    def finish_job(self, job_id: int, lease_token: str, status: str = 'complete',
                   error: str | None = None, retry_after: float = 0,
                   count_attempt: bool = True) -> bool:
        if status not in {'complete', 'pending', 'failed', 'unavailable'}:
            raise ValueError('invalid terminal/retry state')
        if not math.isfinite(retry_after):
            raise ValueError('retry_after must be finite')
        with self.lock, self.db:
            now = time.time()
            cursor = self.db.execute('''UPDATE jobs SET status=?,error=?,not_before=?,
                lease_token=NULL,lease_until=NULL,updated_at=?,attempts=max(0,attempts-?)
                WHERE id=? AND status='running' AND lease_token=? AND lease_until>?''',
                (status, error, now + max(0, retry_after), now, int(not count_attempt), job_id, lease_token, now))
            return cursor.rowcount == 1

    def renew_job(self, job_id: int, lease_token: str, lease_seconds: float = 300) -> bool:
        if not math.isfinite(lease_seconds) or lease_seconds <= 0:
            raise ValueError('lease_seconds must be positive and finite')
        with self.lock, self.db:
            now = time.time()
            cursor = self.db.execute("""UPDATE jobs SET lease_until=?,updated_at=?
                WHERE id=? AND status='running' AND lease_token=? AND lease_until>?""",
                (now + lease_seconds, now, job_id, lease_token, now))
            return cursor.rowcount == 1

    def retry_job(self, job_id: int) -> dict:
        """Explicit operator retry resets terminal failure and its attempt budget."""
        with self.lock, self.db:
            cursor = self.db.execute("""UPDATE jobs SET status='pending',attempts=0,
                not_before=0,error=NULL,lease_until=NULL,lease_token=NULL,updated_at=?
                WHERE id=? AND status IN ('failed','unavailable')""", (time.time(), job_id))
            if not cursor.rowcount:
                raise ValueError('job must exist in failed or unavailable state')
            return self.get_job(job_id)

    def status(self) -> dict:
        with self.lock:
            jobs = {r['status']: r['n'] for r in self.db.execute('SELECT status,count(*) n FROM jobs GROUP BY status')}
            oldest = self.db.execute("SELECT min(created_at) FROM jobs WHERE status IN ('pending','running')").fetchone()[0]
            return {'pdfs': self.pdfs(), 'papers': self.db.execute('SELECT count(*) FROM papers').fetchone()[0],
                    'artifacts': [dict(r) for r in self.db.execute('''SELECT kind,status,count(*) count,
                        sum(stored_size) stored_bytes FROM artifacts GROUP BY kind,status''')],
                    'latex_locations': self.db.execute('SELECT count(*) FROM latex_locations').fetchone()[0],
                    'jobs': jobs, 'oldest_pending_age_seconds': max(0, time.time() - oldest) if oldest else None,
                    'import_checkpoints': self.db.execute('SELECT count(*) FROM import_checkpoints').fetchone()[0]}

    def stats(self) -> dict:
        return self.status()

    def backup(self, destination: str | Path) -> None:
        """Produce a consistent standalone SQLite snapshot, including WAL contents."""
        if Path(destination).resolve() == self.path.resolve():
            raise ValueError('backup destination must differ from catalog')
        with self.lock, sqlite3.connect(str(destination)) as target:
            self.db.backup(target)

    def import_history(self, archive_root: str | Path, ledger_path: str | Path,
                       progress: Callable[[dict], None] | None = None) -> dict:
        """Restart-safe registration; caller must verify archive mount before import.

        Checkpoints commit with each batch. Import fills historical metadata only
        when absent, so reruns cannot regress a live record. Artifact evidence is
        copied from the trusted acquisition ledger without a second content audit.
        """
        import pyarrow.parquet as pq
        archive_root = Path(archive_root)
        def done(source: str) -> bool:
            with self.lock:
                return self.db.execute('SELECT 1 FROM import_checkpoints WHERE source=?', (source,)).fetchone() is not None
        def checkpoint(source: str, rows: int) -> None:
            self.db.execute('INSERT INTO import_checkpoints VALUES (?,?,?)', (source, time.time(), rows))
        def report(source: str, rows: int) -> None:
            if progress:
                progress({'source': source, 'rows': rows})

        metadata_files = sorted((archive_root / 'hf/metadata').glob('*.parquet'))
        latex_files = sorted((archive_root / 'hf/latex').glob('*.parquet'))
        if not metadata_files or not latex_files or not Path(ledger_path).is_file():
            raise FileNotFoundError('historical metadata, LaTeX shards or ledger missing')
        for path in metadata_files:
            parquet = pq.ParquetFile(path)
            for rg in range(parquet.num_row_groups):
                key = f'{path.relative_to(archive_root)}:{rg}'
                if done(key):
                    continue
                rows = parquet.read_row_group(rg).to_pylist()
                now = time.time()
                values = [(r['paper_id'], _json(r), r.get('n_versions'), now) for r in rows]
                with self.lock, self.db:
                    self.db.executemany('''INSERT INTO papers VALUES (?,?,?,?) ON CONFLICT(id)
                        DO UPDATE SET metadata_json=CASE WHEN papers.metadata_json='{}' THEN excluded.metadata_json
                        ELSE papers.metadata_json END,latest_version=max(coalesce(papers.latest_version,0),
                        coalesce(excluded.latest_version,0))''', values)
                    for row in self.db.execute('SELECT id,metadata_json FROM papers WHERE id IN '
                                               '(SELECT value FROM json_each(?))',
                                               (_json([v[0] for v in values]),)).fetchall():
                        self._index_date(row['id'], json.loads(row['metadata_json']))
                    checkpoint(key, len(rows))
                report(key, len(rows))

        # Each rowgroup contributes one locator per paper, rather than one entry per TeX file.
        for path in latex_files:
            parquet = pq.ParquetFile(path)
            shard = str(path.relative_to(archive_root))
            for rg in range(parquet.num_row_groups):
                key = f'{shard}:{rg}'
                if done(key):
                    continue
                ids = set(parquet.read_row_group(rg, columns=['paper_id']).column(0).to_pylist())
                with self.lock, self.db:
                    self.db.executemany('INSERT OR IGNORE INTO latex_locations VALUES (?,?,?)',
                                        ((paper_id, shard, rg) for paper_id in ids))
                    checkpoint(key, len(ids))
                report(key, len(ids))

        ledger = sqlite3.connect(Path(ledger_path).resolve().as_uri() + '?mode=ro', uri=True)
        ledger.row_factory = sqlite3.Row
        try:
            cursor = ledger.execute('SELECT * FROM artifacts ORDER BY dest')
            batch = 0
            while rows := cursor.fetchmany(5000):
                key = f'historical-ledger:{batch}'
                batch += 1
                if done(key):
                    continue
                now = time.time()
                with self.lock, self.db:
                    self.db.executemany('INSERT OR IGNORE INTO papers(id,updated_at) VALUES (?,?)',
                                        ((r['paper_id'], now) for r in rows))
                    self.db.executemany('''INSERT OR IGNORE INTO artifacts VALUES
                        (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                        ((r['paper_id'], r['version'], 'pdf', r['dest'], r['original_size'],
                          r['original_sha256'], r['stored_size'], r['stored_sha256'], r['compression'],
                          'application/pdf', _json({'source': 'secemp9/arxiv-complete',
                           'shard': r['shard'], 'original_path': r['original_path'],
                           'verified_at': r['completed']}), 'available', now) for r in rows))
                    checkpoint(key, len(rows))
                report(key, len(rows))
        finally:
            ledger.close()
        return self.status()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--catalog', required=True)
    parser.add_argument('--archive', required=True)
    parser.add_argument('--ledger', required=True)
    args = parser.parse_args()
    catalog = Catalog(args.catalog)
    try:
        print(json.dumps(catalog.import_history(args.archive, args.ledger,
              progress=lambda row: print(json.dumps(row), flush=True)), indent=2))
    finally:
        catalog.close()


if __name__ == '__main__':
    main()
