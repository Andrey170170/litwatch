"""Bounded lexical search over a rebuildable, independently updated SSD index."""
from __future__ import annotations

from contextlib import closing
from datetime import UTC, date, datetime
from email.utils import parsedate_to_datetime
import json
from pathlib import Path
import re
import sqlite3
import time
from typing import Any

SCHEMA = '''
CREATE TABLE IF NOT EXISTS documents (
 rowid INTEGER PRIMARY KEY, id TEXT NOT NULL UNIQUE,
 title TEXT NOT NULL, abstract TEXT NOT NULL, authors TEXT NOT NULL,
 categories TEXT NOT NULL, published TEXT NOT NULL, latest_version INTEGER,
 doi TEXT NOT NULL, journal_ref TEXT NOT NULL,
 has_pdf INTEGER NOT NULL, has_source INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS documents_title ON documents(title COLLATE NOCASE,id);
CREATE INDEX IF NOT EXISTS documents_date ON documents(published DESC,id);
CREATE INDEX IF NOT EXISTS documents_pdf ON documents(has_pdf,published DESC,id);
CREATE INDEX IF NOT EXISTS documents_source ON documents(has_source,published DESC,id);
CREATE TABLE IF NOT EXISTS categories (
 category TEXT NOT NULL, docid INTEGER NOT NULL, published TEXT NOT NULL, id TEXT NOT NULL,
 PRIMARY KEY(category,docid)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS categories_docid ON categories(docid);
CREATE INDEX IF NOT EXISTS categories_date ON categories(category,published DESC,id);
CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
 id, title, abstract, authors, doi, journal_ref,
 content='documents',content_rowid='rowid',tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS documents_ai AFTER INSERT ON documents BEGIN
 INSERT INTO documents_fts(rowid,id,title,abstract,authors,doi,journal_ref)
 VALUES(new.rowid,new.id,new.title,new.abstract,new.authors,new.doi,new.journal_ref);
END;
CREATE TRIGGER IF NOT EXISTS documents_ad AFTER DELETE ON documents BEGIN
 INSERT INTO documents_fts(documents_fts,rowid,id,title,abstract,authors,doi,journal_ref)
 VALUES('delete',old.rowid,old.id,old.title,old.abstract,old.authors,old.doi,old.journal_ref);
 DELETE FROM categories WHERE docid=old.rowid;
END;
CREATE TRIGGER IF NOT EXISTS documents_au AFTER UPDATE ON documents BEGIN
 INSERT INTO documents_fts(documents_fts,rowid,id,title,abstract,authors,doi,journal_ref)
 VALUES('delete',old.rowid,old.id,old.title,old.abstract,old.authors,old.doi,old.journal_ref);
 INSERT INTO documents_fts(rowid,id,title,abstract,authors,doi,journal_ref)
 VALUES(new.rowid,new.id,new.title,new.abstract,new.authors,new.doi,new.journal_ref);
END;
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY,value TEXT NOT NULL);
'''


def _text(value: Any) -> str:
    return ' '.join(str(value).split()) if value is not None else ''


def _date(value: Any) -> str:
    """The UTC calendar date of an ISO or RFC 2822 timestamp (a bare date is taken as UTC)."""
    if stamp := _timestamp(value):
        return stamp[:10]
    try:
        return date.fromisoformat(value[:10]).isoformat() if isinstance(value, str) else ''
    except ValueError:
        return ''


def published_date(metadata: dict) -> str:
    """The first version's submission date (YYYY-MM-DD, UTC), '' when no record says it. An
    explicit v1 in the version history wins: a record's `published` or `created` may describe
    a later revision."""
    for entry in metadata.get('version_history') or metadata.get('versions') or []:
        if isinstance(entry, dict) and entry.get('version') in (1, 'v1', '1'):
            if published := _date(entry.get('date') or entry.get('created')):
                return published
    for key in ('published', 'first_version_date', 'submitted', 'created'):
        if published := _date(metadata.get(key)):
            return published
    return ''


def normalize(row: dict) -> dict:
    """Accept the HF snapshot and both current arXiv metadata representations."""
    metadata = json.loads(row['metadata_json'])
    authors = metadata.get('authors_raw') or metadata.get('authors') or ''
    if isinstance(authors, list):
        authors = ', '.join(_text(a.get('name', ' '.join(filter(None, [a.get('forenames'), a.get('keyname')]))))
                            if isinstance(a, dict) else _text(a) for a in authors)
    categories = metadata.get('categories') or []
    if isinstance(categories, str):
        categories = categories.split()
    categories = sorted(set(_text(c) for c in categories if _text(c)))
    return dict(id=row['id'], title=_text(metadata.get('title')), abstract=_text(metadata.get('abstract')),
                authors=_text(authors), categories=categories, published=published_date(metadata),
                latest_version=row.get('latest_version'), doi=_text(metadata.get('doi')),
                journal_ref=_text(metadata.get('journal_ref')), has_pdf=bool(row.get('has_pdf')),
                has_source=bool(row.get('has_source')))


def normalize_paper(paper: dict) -> dict:
    """Normalize a decoded Catalog.get_paper() record for detail/BibTeX output."""
    return normalize({**paper, 'metadata_json': json.dumps(paper.get('metadata', {}))})


def _timestamp(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
        except (ValueError, TypeError, OverflowError):
            return None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).astimezone(UTC).isoformat()


def _authors(metadata: dict) -> list[str]:
    """Names in order: an observed list, HF's parsed names, else the raw string split on
    commas and "and" (the arXivRaw discovery stores the raw string as a one-item list)."""
    authors, raw = metadata.get('authors'), metadata.get('authors_raw')
    if isinstance(authors, list) and authors and not (raw and authors == [raw]):
        names = [a.get('name') or ' '.join(filter(None, [a.get('forenames'), a.get('keyname')]))
                 if isinstance(a, dict) else a for a in authors]
    elif isinstance(metadata.get('authors_parsed'), list) and metadata['authors_parsed']:
        names = []
        for name in metadata['authors_parsed']:
            if isinstance(name, list) and name:
                family, given, suffix = (name + ['', ''])[:3]
                names.append(' '.join(filter(None, [given, family, suffix])))
    else:
        text = raw or (authors if isinstance(authors, str) else '')
        names = re.split(r',\s*(?:and\s+)?|\s+and\s+', str(text))
    return [name for name in (_text(n) for n in names) if name]


def _history(metadata: dict) -> list[dict]:
    """Revisions as {version: int, date: ISO timestamp, ...}, whichever form was stored."""
    result = []
    for entry in metadata.get('version_history') or metadata.get('versions') or []:
        if not isinstance(entry, dict):
            continue
        version = entry.get('version')
        if isinstance(version, str) and (match := re.fullmatch(r'v?([1-9][0-9]*)', version)):
            version = int(match[1])
        if type(version) is not int or version < 1:
            continue
        extra = {k: entry[k] for k in ('size', 'source_type') if entry.get(k) is not None}
        result.append({'version': version, 'date': _timestamp(entry.get('date') or entry.get('created')),
                       **extra})
    return sorted(result, key=lambda entry: entry['version'])


def listing_item(paper_id: str, metadata: dict) -> dict:
    """A catalog record in one shape whatever representation it was stored in. Dates are
    YYYY-MM-DD (UTC); a field no record says is null. `version` is the one the metadata
    describes (its history or its own `version`), never the catalog's `latest_version`, which
    a stored artifact can advance past the text."""
    categories = metadata.get('categories') or []
    if isinstance(categories, str):
        categories = categories.split()
    history = _history(metadata)
    published = published_date(metadata)
    updated = (_date(metadata.get('updated') or metadata.get('update_date'))
               or _date((history[-1]['date'] or '') if history else '') or published)
    own = metadata.get('version')
    versions = [entry['version'] for entry in history]
    versions += [own] if type(own) is int and own >= 1 else []
    return {'id': paper_id,
            'version': max(versions, default=None),
            'title': _text(metadata.get('title')), 'abstract': _text(metadata.get('abstract')),
            'authors': _authors(metadata),
            'categories': list(dict.fromkeys(c for c in map(_text, categories) if c)),
            'primary_category': _text(metadata.get('primary_category')) or None,
            'published': published or None, 'updated': updated or None, 'version_history': history,
            'doi': _text(metadata.get('doi')) or None,
            'journal_ref': _text(metadata.get('journal_ref') or metadata.get('journal-ref')) or None,
            'license': _text(metadata.get('license')) or None}


def _decode(row: sqlite3.Row) -> dict:
    item = dict(row)
    item.pop('rowid', None)
    item['categories'] = json.loads(item['categories'])
    item['has_pdf'] = bool(item['has_pdf'])
    item['has_source'] = bool(item['has_source'])
    return item


class SearchIndex:
    """Open read-only connections per request; API threads share no SQLite state."""

    def __init__(self, path: str | Path, *, query_timeout: float = 5.0):
        self.path = Path(path)
        self.query_timeout = query_timeout

    def _connect(self) -> sqlite3.Connection:
        if not self.path.is_file():
            raise RuntimeError('Search index is being prepared; try again shortly.')
        db = sqlite3.connect(f'{self.path.resolve().as_uri()}?mode=ro', uri=True, timeout=0.5)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA cache_size=-8192')
        db.execute('PRAGMA temp_store=MEMORY')
        return db

    def status(self) -> dict:
        if not self.path.is_file():
            return {'indexed_papers': 0, 'baseline_complete': False, 'updated_at': None,
                    'state': 'not_started'}
        with closing(self._connect()) as db:
            values = {r[0]: json.loads(r[1]) for r in db.execute('SELECT key,value FROM state')}
        values.setdefault('indexed_papers', 0)
        values.setdefault('baseline_complete', False)
        values.setdefault('updated_at', None)
        values['state'] = 'ready' if values['baseline_complete'] else 'building'
        return values

    def search(self, q: str = '', category: str = '', date_from: str = '', date_to: str = '',
               availability: str = 'all', limit: int = 20, offset: int = 0,
               sort: str = 'relevance') -> dict:
        started = time.monotonic()
        if not all(isinstance(v, str) for v in (q, category, date_from, date_to, availability, sort)):
            raise ValueError('Search parameters must be strings.')
        orderings = {
            'relevance': 'd.published DESC,d.id',
            'newest': 'd.published DESC,d.id',
            'oldest': 'd.published ASC,d.id DESC',
            'title_asc': 'd.title COLLATE NOCASE ASC,d.id ASC',
            'title_desc': 'd.title COLLATE NOCASE DESC,d.id DESC',
        }
        if sort not in orderings:
            raise ValueError('Sort must be relevance, newest, oldest, title_asc or title_desc.')
        if len(q) > 300:
            raise ValueError('Use a search query of at most 300 characters.')
        if type(limit) is not int or not 1 <= limit <= 50 or type(offset) is not int or not 0 <= offset <= 1000:
            raise ValueError('Limit must be 1–50 and offset 0–1000; narrow the query for deeper results.')
        if category and not re.fullmatch(r'[A-Za-z][A-Za-z0-9.-]{0,39}', category):
            raise ValueError('Invalid arXiv category.')
        for value in (date_from, date_to):
            if value:
                try:
                    if date.fromisoformat(value).isoformat() != value:
                        raise ValueError
                except ValueError:
                    raise ValueError('Dates must use YYYY-MM-DD.') from None
        if date_from and date_to and date_from > date_to:
            raise ValueError('Start date must not follow end date.')
        filters = {'all': '', 'pdf': 'd.has_pdf=1', 'source': 'd.has_source=1',
                   'both': 'd.has_pdf=1 AND d.has_source=1',
                   'any': '(d.has_pdf=1 OR d.has_source=1)',
                   'none': 'd.has_pdf=0 AND d.has_source=0'}
        if availability not in filters:
            raise ValueError('Availability must be all, pdf, source, both, any or none.')
        # Literal Unicode words only: user input cannot invoke FTS syntax or wildcard scans.
        terms = re.findall(r'[^\W_]+', q, re.UNICODE)
        if len(terms) > 12:
            raise ValueError('Use at most 12 search terms.')
        if q.strip() and not terms:
            raise ValueError('Enter a paper ID or words to search.')
        exact = q.strip().removeprefix('arXiv:').strip()
        exact = re.sub(r'v\d+$', '', exact)
        is_id = bool(re.fullmatch(r'(?:\d{4}\.\d{4,5}|[A-Za-z][A-Za-z0-9.-]*/\d{7})', exact))
        expression = ' AND '.join('"' + t + '"' for t in terms)
        clauses, params = [], []
        ordered_scan = not is_id and sort.startswith('title_')
        if terms and not is_id and sort != 'relevance':
            # Rare matches sort cheaply directly; walking the entire date index
            # to find a few recent papers in oldest order would be much slower.
            with closing(self._connect()) as probe:
                probe.set_progress_handler(lambda: time.monotonic() - started >= self.query_timeout, 1000)
                try:
                    matches = probe.execute('SELECT rowid FROM documents_fts WHERE documents_fts MATCH ? LIMIT 257',
                                            (expression,)).fetchall()
                except sqlite3.OperationalError as error:
                    if 'interrupt' in str(error).lower():
                        raise RuntimeError('This search is too broad. Add more specific search words or an arXiv ID.') from None
                    raise
            ordered_scan = len(matches) > 256
        base = 'documents d'
        if ordered_scan:
            index = 'documents_title' if sort.startswith('title_') else 'documents_date'
            base += f' INDEXED BY {index}'
        ordering = orderings[sort]
        if terms and not is_id:
            if ordered_scan:
                clauses.append('d.rowid IN (SELECT rowid FROM documents_fts WHERE documents_fts MATCH ?)')
            else:
                base += ' JOIN documents_fts ON documents_fts.rowid=d.rowid'
                clauses.append('documents_fts MATCH ?')
            params.append(expression)
            if sort == 'relevance':
                ordering = 'documents_fts.rank'
        elif is_id:
            clauses.append('d.id=?')
            params.append(exact)
        if category:
            if ordered_scan:
                clauses.append('EXISTS(SELECT 1 FROM categories c WHERE c.docid=d.rowid AND c.category=?)')
            else:
                base += ' JOIN categories c ON c.docid=d.rowid'
                clauses.append('c.category=?')
            params.append(category)
            if not ordered_scan and not terms and sort in ('relevance', 'newest', 'oldest'):
                ordering = ordering.replace('d.', 'c.')
        if date_from:
            clauses.append('d.published>=?')
            params.append(date_from)
        if date_to:
            clauses.append('d.published<=?')
            params.append(date_to)
        if filters[availability]:
            clauses.append(filters[availability])
        projection = 'd.rowid' if ordered_scan else 'd.*'
        sql = f'SELECT {projection} FROM {base}'
        if clauses:
            sql += ' WHERE ' + ' AND '.join(clauses)
        sql += f' ORDER BY {ordering} LIMIT ? OFFSET ?'
        if ordered_scan:
            # Walk covering sort keys against the FTS match set, then fetch only
            # the page's wide metadata rows. Avoid sorting thousands of abstracts.
            sql = (f'WITH page AS MATERIALIZED ({sql}) SELECT d.* FROM page '
                   f'JOIN documents d ON d.rowid=page.rowid ORDER BY {ordering}')
        params += [limit + 1, offset]
        db = self._connect()
        try:
            db.set_progress_handler(lambda: time.monotonic() - started >= self.query_timeout, 1000)
            rows = db.execute(sql, params).fetchall()
        except sqlite3.OperationalError as error:
            if 'interrupt' in str(error).lower():
                raise RuntimeError('This search is too broad. Add more specific search words or an arXiv ID; filters alone may not reduce ranking time.') from None
            raise
        finally:
            db.close()
        has_more = len(rows) > limit
        return {'items': [_decode(row) for row in rows[:limit]], 'has_more': has_more,
                'next_offset': offset + limit if has_more and offset + limit <= 1000 else None,
                'elapsed_ms': round((time.monotonic() - started) * 1000, 1)}
