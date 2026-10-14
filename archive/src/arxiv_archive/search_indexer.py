"""Resumable metadata indexing with durable, generation-checked change capture."""
from __future__ import annotations

import argparse
import fcntl
import json
from pathlib import Path
import shutil
import sqlite3
import time

from .search import SCHEMA, SearchIndex, normalize
from .storage import WORK, mount_guard

# Correlated lookups use the catalog's existing paper_id-leading primary indexes.
PROJECTION = '''p.id,p.metadata_json,p.latest_version,
 EXISTS(SELECT 1 FROM artifacts a WHERE a.paper_id=p.id AND a.kind='pdf'
        AND a.status='available') AS has_pdf,
 (EXISTS(SELECT 1 FROM artifacts a WHERE a.paper_id=p.id AND a.kind='source'
         AND a.status='available') OR
  EXISTS(SELECT 1 FROM latex_locations l WHERE l.paper_id=p.id)) AS has_source'''


def install_tracking(db: sqlite3.Connection) -> None:
    """Install all triggers atomically before taking the baseline high-water mark."""
    with db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('''CREATE TABLE IF NOT EXISTS search_dirty (
            paper_id TEXT PRIMARY KEY,generation INTEGER NOT NULL)''')
        for table, key in [('papers', 'id'), ('artifacts', 'paper_id'), ('latex_locations', 'paper_id')]:
            for operation, refs in [('INSERT', ['NEW']), ('UPDATE', ['OLD', 'NEW']), ('DELETE', ['OLD'])]:
                # An UPDATE can change paper identity; invalidate both names. Repeated
                # increments for an unchanged name are harmless generation tokens.
                changes = '\n'.join(f'''INSERT INTO search_dirty(paper_id,generation)
                    VALUES({ref}.{key},1) ON CONFLICT(paper_id)
                    DO UPDATE SET generation=search_dirty.generation+1;''' for ref in refs)
                db.execute(f'''CREATE TRIGGER IF NOT EXISTS search_{table}_{operation.lower()}
                    AFTER {operation} ON {table} BEGIN {changes} END''')


class Indexer:
    """One writer; read short catalog snapshots and commit index before acknowledgment."""

    def __init__(self, catalog_path: str | Path, index_path: str | Path):
        self.path = Path(index_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = (self.path.parent / 'indexer.lock').open('a')
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close()
            raise RuntimeError('A metadata indexer is already running.') from None
        try:
            self.catalog = sqlite3.connect(f'{Path(catalog_path).resolve().as_uri()}?mode=rw', uri=True, timeout=5)
            self.catalog.row_factory = sqlite3.Row
            self.catalog.execute('PRAGMA cache_size=-8192')
            install_tracking(self.catalog)
            self.db = sqlite3.connect(str(self.path), timeout=5)
            self.db.row_factory = sqlite3.Row
            self.db.execute('PRAGMA journal_mode=WAL')
            self.db.execute('PRAGMA synchronous=FULL')
            self.db.execute('PRAGMA cache_size=-32768')
            self.db.execute('PRAGMA temp_store=MEMORY')
            self.db.executescript(SCHEMA)
            with self.db:
                self.db.execute("INSERT INTO documents_fts(documents_fts,rank) VALUES('rank','bm25(12.0,8.0,1.0,3.0,4.0,1.0)')")
                if self._get('baseline_high_water') is None:
                    high_water = self.catalog.execute('SELECT coalesce(max(rowid),0) FROM papers').fetchone()[0]
                    for key, value in [('baseline_high_water', high_water), ('baseline_rowid', 0),
                                       ('baseline_complete', False), ('indexed_papers', 0)]:
                        self._set(key, value)
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        for name in ('db', 'catalog', 'lock'):
            handle = getattr(self, name, None)
            if handle is not None:
                handle.close()
                setattr(self, name, None)

    def _get(self, key: str):
        row = self.db.execute('SELECT value FROM state WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def _set(self, key: str, value) -> None:
        self.db.execute('INSERT INTO state VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                        (key, json.dumps(value)))

    def _snapshot(self, batch_size: int) -> tuple[list, list, list, int, bool]:
        """Read both lanes in one short snapshot, then release the catalog immediately."""
        baseline_rowid = self._get('baseline_rowid')
        baseline_complete = self._get('baseline_complete')
        self.catalog.execute('BEGIN')
        try:
            baseline = []
            if not baseline_complete:
                baseline = self.catalog.execute(f'''SELECT p.rowid AS catalog_rowid,{PROJECTION}
                    FROM papers p WHERE p.rowid>? AND p.rowid<=? ORDER BY p.rowid LIMIT ?''',
                    (baseline_rowid, self._get('baseline_high_water'), batch_size)).fetchall()
                if baseline:
                    baseline_rowid = baseline[-1]['catalog_rowid']
                baseline_complete = len(baseline) < batch_size or baseline_rowid >= self._get('baseline_high_water')
            dirty = self.catalog.execute('SELECT paper_id,generation FROM search_dirty ORDER BY rowid LIMIT ?',
                                         (batch_size,)).fetchall()
            changed = []
            for entry in dirty:
                row = self.catalog.execute(f'SELECT {PROJECTION} FROM papers p WHERE p.id=?',
                                           (entry['paper_id'],)).fetchone()
                changed.append((entry['paper_id'], dict(row) if row else None))
            return baseline, dirty, changed, baseline_rowid, baseline_complete
        finally:
            self.catalog.rollback()

    def _publish(self, changes: dict[str, dict | None], baseline_rowid: int, baseline_complete: bool) -> None:
        with self.db:
            count = self._get('indexed_papers')
            for paper_id, row in changes.items():
                old = self.db.execute('SELECT * FROM documents WHERE id=?', (paper_id,)).fetchone()
                if row is None:
                    if old:
                        self.db.execute('DELETE FROM documents WHERE rowid=?', (old['rowid'],))
                        count -= 1
                    continue
                item = normalize(row)
                item['categories'] = json.dumps(item['categories'], ensure_ascii=False)
                columns = tuple(item)
                values = tuple(item.values())
                if old and all(old[k] == item[k] for k in columns):
                    continue
                self.db.execute(f'''INSERT INTO documents({','.join(columns)})
                    VALUES({','.join('?' for _ in columns)}) ON CONFLICT(id) DO UPDATE SET
                    {','.join(k+'=excluded.'+k for k in columns if k != 'id')}''', values)
                if not old:
                    count += 1
                docid = old['rowid'] if old else self.db.execute('SELECT rowid FROM documents WHERE id=?', (paper_id,)).fetchone()[0]
                self.db.execute('DELETE FROM categories WHERE docid=?', (docid,))
                self.db.executemany('INSERT INTO categories VALUES(?,?,?,?)',
                                    [(cat, docid, item['published'], paper_id) for cat in json.loads(item['categories'])])
            for key, value in [('indexed_papers', count), ('baseline_rowid', baseline_rowid),
                               ('baseline_complete', baseline_complete), ('updated_at', time.time())]:
                self._set(key, value)

    def _acknowledge(self, dirty: list) -> None:
        with self.catalog:
            self.catalog.executemany('DELETE FROM search_dirty WHERE paper_id=? AND generation=?',
                                     [(r['paper_id'], r['generation']) for r in dirty])

    def prepare_planner(self) -> bool:
        """Sample filter indexes once after baseline; never scan FTS shadow tables."""
        if not self._get('baseline_complete') or self._get('planner_ready'):
            return False
        with self.db:
            self.db.execute('PRAGMA analysis_limit=1000')
            self.db.execute('ANALYZE documents')
            self.db.execute('ANALYZE categories')
            self._set('planner_ready', True)
        return True

    def step(self, batch_size: int = 500) -> dict:
        if type(batch_size) is not int or not 1 <= batch_size <= 5000:
            raise ValueError('Batch size must be 1–5000.')
        started = time.monotonic()
        baseline, dirty, changed, rowid, complete = self._snapshot(batch_size)
        changes = {r['id']: dict(r) for r in baseline}
        changes.update(changed)
        self._publish(changes, rowid, complete)
        # Index commit precedes acknowledgment. A crash replays an idempotent batch;
        # intervening catalog mutations change generation and survive acknowledgment.
        self._acknowledge(dirty)
        return {'baseline_rows': len(baseline), 'changes': len(dirty),
                'baseline_complete': complete, 'indexed_papers': self._get('indexed_papers'),
                'elapsed_ms': round((time.monotonic() - started) * 1000, 1)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work', type=Path, default=WORK)
    parser.add_argument('--status', action='store_true', help='Show read-only indexing progress.')
    parser.add_argument('--json', action='store_true', help='Use JSON for status.')
    parser.add_argument('--once', action='store_true', help='Process one bounded batch and exit.')
    parser.add_argument('--batch-size', type=int, default=1000)
    parser.add_argument('--interval', type=float, default=2, help='Idle polling interval in seconds.')
    args = parser.parse_args()
    if not 1 <= args.batch_size <= 5000 or not 0.1 <= args.interval <= 300:
        parser.error('batch-size must be 1–5000 and interval 0.1–300 seconds')
    if args.status:
        status = SearchIndex(args.work / 'search/search.sqlite').status()
        if args.json:
            print(json.dumps(status))
        else:
            state = 'READY' if status['baseline_complete'] else 'BUILDING'
            print(f"Search index: {state}\nPapers indexed: {status['indexed_papers']:,}")
            if not status['baseline_complete'] and status.get('baseline_high_water'):
                print(f"Initial scan: {100 * status.get('baseline_rowid', 0) / status['baseline_high_water']:.1f}%")
        return
    mount_guard(args.work, 'tank/arxiv-work')
    indexer = None
    try:
        # SSD-only: metadata indexing does not need an available payload/HDD mount.
        if shutil.disk_usage(args.work).free < 8_000_000_000:
            raise RuntimeError('Insufficient free SSD space for search indexing.')
        indexer = Indexer(args.work / 'catalog/catalog.sqlite', args.work / 'search/search.sqlite')
        last_log = 0.0
        while True:
            mount_guard(args.work, 'tank/arxiv-work')
            if shutil.disk_usage(args.work).free < 8_000_000_000:
                raise RuntimeError('Insufficient free SSD space for search indexing.')
            result = indexer.step(args.batch_size)
            if args.once or time.monotonic() - last_log >= 30:
                print(json.dumps(result), flush=True)
                last_log = time.monotonic()
            if args.once:
                break
            if result['baseline_complete']:
                indexer.prepare_planner()
            if result['baseline_complete'] and result['changes'] == 0:
                time.sleep(args.interval)
            else:
                time.sleep(0.05)
    finally:
        if indexer:
            indexer.close()


if __name__ == '__main__':
    main()
