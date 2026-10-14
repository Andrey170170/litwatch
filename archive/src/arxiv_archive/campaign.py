"""Durable recovery selection and delivery ledger, separate from the live catalog.

Selection is mutable until freeze, then only delivery outcomes may change. A
campaign never downloads bytes or modifies the service's catalog directly.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
from typing import Any, Iterator

from .catalog import PAPER_ID

STATES = frozenset({
    'pending', 'retry_pending', 'unresolved', 'queued', 'acquiring', 'already_available',
    'available', 'unavailable', 'failed',
})
SCHEMA = '''
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS candidates (
 id INTEGER PRIMARY KEY, paper_id TEXT NOT NULL, version INTEGER,
 kind TEXT NOT NULL CHECK(kind IN ('pdf','source')),
 metadata_json TEXT NOT NULL, status TEXT NOT NULL,
 job_id INTEGER, error TEXT, original_size INTEGER, stored_size INTEGER,
 updated_at REAL NOT NULL,
 CHECK(version IS NULL OR version > 0)
);
CREATE UNIQUE INDEX IF NOT EXISTS candidate_identity
 ON candidates(paper_id,coalesce(version,0),kind);
CREATE INDEX IF NOT EXISTS candidate_status ON candidates(status,id);
CREATE TABLE IF NOT EXISTS reasons (
 candidate_id INTEGER NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
 reason TEXT NOT NULL, PRIMARY KEY(candidate_id,reason)
);
CREATE TABLE IF NOT EXISTS discovery (
 category TEXT PRIMARY KEY, token TEXT, done INTEGER NOT NULL,
 response TEXT, details_json TEXT NOT NULL, updated_at REAL NOT NULL
);
'''


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def _atomic_write(path: Path, data: bytes) -> None:
    temporary = path.with_name(path.name + '.part')
    with temporary.open('wb') as out:
        out.write(data)
        out.flush()
        os.fsync(out.fileno())
    os.replace(temporary, path)
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class Campaign:
    """Open/create a manifest; a supplied scope must exactly match an existing one.

    ``scope['categories']`` lists mandatory discovery checkpoints. Callers provide
    the verified SSD path and enforce mount guards before constructing a campaign.
    The connection is intended for a single orchestration thread. ``transaction``
    may wrap all candidates from a page and its checkpoint for atomic discovery.
    """

    def __init__(self, path: str | Path, scope: dict | None = None):
        self.path = Path(path)
        if not self.path.exists() and scope is None:
            raise FileNotFoundError(self.path)
        if scope is not None and not isinstance(scope, dict):
            raise ValueError('scope must be a JSON object')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('PRAGMA foreign_keys=ON')
        self.conn.execute('PRAGMA journal_mode=WAL')
        self.conn.execute('PRAGMA synchronous=FULL')
        self.conn.executescript(SCHEMA)
        try:
            with self.transaction():
                existing = self._setting('scope')
                if existing is None:
                    if scope is None:
                        raise ValueError('manifest has no scope')
                    self.conn.execute('INSERT INTO settings VALUES (?,?)', ('scope', _json(scope)))
                elif scope is not None and existing != _json(scope):
                    raise ValueError('campaign scope differs from immutable recorded scope')
        except Exception:
            self.conn.close()
            raise

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Campaign:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Commit a whole discovery page together; nested calls join its transaction."""
        if self.conn.in_transaction:
            yield
            return
        self.conn.execute('BEGIN IMMEDIATE')
        try:
            yield
        except BaseException:
            self.conn.rollback()
            raise
        else:
            self.conn.commit()

    def _setting(self, key: str) -> str | None:
        row = self.conn.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        return row[0] if row else None

    def get_progress(self, name: str) -> dict | None:
        """Read an orchestration checkpoint distinct from immutable selection state."""
        if not isinstance(name, str) or not name:
            raise ValueError('progress name must be a nonempty string')
        value = self._setting('progress:' + name)
        return json.loads(value) if value is not None else None

    def set_progress(self, name: str, progress: dict) -> None:
        """Persist a phase/cursor checkpoint, including after selection freeze.

        Join ``transaction()`` when the checkpoint acknowledges ledger changes.
        Prefixing the key prevents callers from overwriting scope or receipt.
        """
        if not isinstance(name, str) or not name or not isinstance(progress, dict):
            raise ValueError('progress requires a nonempty name and JSON object')
        with self.transaction():
            self.conn.execute(
                'INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                ('progress:' + name, _json(progress)),
            )

    @property
    def scope(self) -> dict:
        return json.loads(self._setting('scope'))

    @property
    def frozen(self) -> bool:
        return self._setting('receipt') is not None

    def _mutable(self) -> None:
        if self.frozen:
            raise ValueError('campaign selection is frozen')

    def add_candidate(self, paper_id: str, version: int | None, kind: str,
                      metadata: dict, reason: str) -> int:
        if not isinstance(paper_id, str) or not PAPER_ID.fullmatch(paper_id):
            raise ValueError('canonical unversioned arXiv ID required')
        if version is not None and (type(version) is not int or version < 1):
            raise ValueError('version must be a positive integer or None')
        if kind not in {'pdf', 'source'} or not isinstance(metadata, dict) or not reason.strip():
            raise ValueError('valid artifact kind, metadata object and reason required')
        with self.transaction():
            self._mutable()
            row = self.conn.execute(
                'SELECT id,metadata_json FROM candidates WHERE paper_id=? AND version IS ? AND kind=?',
                (paper_id, version, kind),
            ).fetchone()
            if row:
                candidate_id = row['id']
                merged = {**json.loads(row['metadata_json']), **metadata}
                self.conn.execute('UPDATE candidates SET metadata_json=?,updated_at=? WHERE id=?',
                                  (_json(merged), time.time(), candidate_id))
            else:
                result = self.conn.execute(
                    'INSERT INTO candidates(paper_id,version,kind,metadata_json,status,updated_at) '
                    'VALUES (?,?,?,?,?,?)',
                    (paper_id, version, kind, _json(metadata),
                     'unresolved' if version is None else 'pending', time.time()),
                )
                candidate_id = result.lastrowid
            self.conn.execute('INSERT OR IGNORE INTO reasons VALUES (?,?)', (candidate_id, reason))
            return candidate_id

    def resolve(self, candidate_id: int, version: int, metadata: dict | None = None) -> int:
        """Resolve unknown identity, merging its reasons into an existing known row."""
        if type(version) is not int or version < 1:
            raise ValueError('version must be a positive integer')
        with self.transaction():
            self._mutable()
            row = self.get(candidate_id)
            if row['version'] is not None and row['version'] != version:
                raise ValueError('cannot change an already known version')
            merged = {**row['metadata'], **(metadata or {})}
            target_id = candidate_id
            for reason in row['reasons']:
                target_id = self.add_candidate(row['paper_id'], version, row['kind'], merged, reason)
            if target_id != candidate_id:
                self.conn.execute('DELETE FROM candidates WHERE id=?', (candidate_id,))
            return target_id

    def checkpoint(self, category: str, token: str | None = None, done: bool = False,
                   response: str | None = None, **details: Any) -> None:
        with self.transaction():
            self._mutable()
            if category not in self.scope.get('categories', []):
                raise ValueError('category is outside campaign scope')
            self.conn.execute(
                'INSERT INTO discovery VALUES (?,?,?,?,?,?) ON CONFLICT(category) DO UPDATE SET '
                'token=excluded.token,done=excluded.done,response=excluded.response,'
                'details_json=excluded.details_json,updated_at=excluded.updated_at',
                (category, token, int(done), response, _json(details), time.time()),
            )

    def get_checkpoint(self, category: str) -> dict | None:
        row = self.conn.execute('SELECT * FROM discovery WHERE category=?', (category,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result['done'] = bool(result['done'])
        result['details'] = json.loads(result.pop('details_json'))
        return result

    def _record(self, row: sqlite3.Row) -> dict:
        result = dict(row)
        result['metadata'] = json.loads(result.pop('metadata_json'))
        result['reasons'] = [r[0] for r in self.conn.execute(
            'SELECT reason FROM reasons WHERE candidate_id=? ORDER BY reason', (row['id'],))]
        return result

    def get(self, candidate_id: int) -> dict:
        row = self.conn.execute('SELECT * FROM candidates WHERE id=?', (candidate_id,)).fetchone()
        if row is None:
            raise KeyError(candidate_id)
        return self._record(row)

    def candidates(self, statuses: list[str] | tuple[str, ...] | None = None,
                   limit: int | None = None) -> list[dict]:
        params: list[Any] = []
        sql = 'SELECT * FROM candidates'
        if statuses is not None:
            if not statuses:
                return []
            if not set(statuses) <= STATES:
                raise ValueError('unknown candidate status')
            sql += ' WHERE status IN (' + ','.join('?' for _ in statuses) + ')'
            params.extend(statuses)
        sql += ' ORDER BY paper_id,coalesce(version,0),kind'
        if limit is not None:
            if limit < 0:
                raise ValueError('limit must be nonnegative')
            sql += ' LIMIT ?'
            params.append(limit)
        return [self._record(row) for row in self.conn.execute(sql, params)]

    def pending(self, limit: int = 100) -> list[dict]:
        return self.candidates(('pending', 'retry_pending'), limit)

    def mark(self, candidate_id: int, status: str, job_id: int | None = None,
             error: str | None = None, original_size: int | None = None,
             stored_size: int | None = None) -> None:
        """Record a delivery observation without changing frozen selection identity."""
        if status not in STATES:
            raise ValueError('unknown candidate status')
        for size in (original_size, stored_size):
            if size is not None and (type(size) is not int or size < 0):
                raise ValueError('artifact sizes must be nonnegative integers')
        with self.transaction():
            row = self.get(candidate_id)
            if row['version'] is None and status != 'unresolved':
                raise ValueError('unknown version must remain unresolved until resolved')
            if row['version'] is not None and status == 'unresolved':
                raise ValueError('known version cannot be unresolved')
            self.conn.execute(
                'UPDATE candidates SET status=?,job_id=coalesce(?,job_id),error=?,'
                'original_size=coalesce(?,original_size),stored_size=coalesce(?,stored_size),'
                'updated_at=? WHERE id=?',
                (status, job_id, error, original_size, stored_size, time.time(), candidate_id),
            )

    def freeze(self, allow_unresolved: bool = False) -> dict:
        """Publish a deterministic selection and one digest; repeated freeze is a no-op.

        Unknown versions require explicit acknowledgement and a recorded error.
        They stay visible in the selection but can never become acquisition jobs.
        """
        with self.transaction():
            receipt = self._setting('receipt')
            if receipt is not None:
                return json.loads(receipt)
            incomplete = [category for category in self.scope.get('categories', [])
                          if not (self.get_checkpoint(category) or {}).get('done')]
            if incomplete:
                raise ValueError('discovery incomplete: ' + ', '.join(incomplete))
            unresolved = self.candidates(('unresolved',))
            if unresolved and (not allow_unresolved or any(not row['error'] for row in unresolved)):
                raise ValueError('unresolved versions require explicit acknowledgement and errors')
            rows = self.candidates()
            selection = []
            for row in rows:
                record = {key: row[key] for key in
                          ('paper_id', 'version', 'kind', 'metadata', 'reasons')}
                if row['version'] is None:
                    record['unresolved_error'] = row['error']
                selection.append(_json(record) + '\n')
            data = ''.join(selection).encode('utf-8')
            receipt = {
                'format_version': 1, 'scope': self.scope, 'candidate_count': len(rows),
                'unresolved_count': len(unresolved), 'selection_sha256': hashlib.sha256(data).hexdigest(),
                'selection_file': 'selection.jsonl', 'frozen_at': time.time(),
                'discovery': {category: self.get_checkpoint(category)
                              for category in self.scope.get('categories', [])},
                'statistics_at_freeze': self.statistics(),
            }
            _atomic_write(self.path.parent / 'selection.jsonl', data)
            _atomic_write(self.path.parent / 'receipt.json', (_json(receipt) + '\n').encode())
            self.conn.execute('INSERT INTO settings VALUES (?,?)', ('receipt', _json(receipt)))
            return receipt

    def statistics(self) -> dict:
        states = {row[0]: row[1] for row in self.conn.execute(
            'SELECT status,count(*) FROM candidates GROUP BY status')}
        kinds = {row[0]: row[1] for row in self.conn.execute(
            'SELECT kind,count(*) FROM candidates GROUP BY kind')}
        reasons = {row[0]: row[1] for row in self.conn.execute(
            'SELECT reason,count(*) FROM reasons GROUP BY reason')}
        totals = self.conn.execute(
            "SELECT count(DISTINCT paper_id),coalesce(sum(CASE WHEN status='available' "
            "THEN original_size ELSE 0 END),0),coalesce(sum(CASE WHEN status='available' "
            "THEN stored_size ELSE 0 END),0) FROM candidates"
        ).fetchone()
        return {'scope': self.scope, 'frozen': self.frozen, 'candidates': sum(states.values()),
                'papers': totals[0], 'states': states, 'kinds': kinds, 'reasons': reasons,
                'acquired_original_bytes': totals[1], 'acquired_stored_bytes': totals[2]}
