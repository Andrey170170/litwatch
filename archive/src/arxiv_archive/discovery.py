"""Bounded official OAI raw discovery, with campaign-owned durable pagination.

OAI datestamps describe metadata changes, not submission dates. Callers apply
submission scope locally and commit page records with the next resumption token.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
import re
import time
import xml.etree.ElementTree as ET

import requests

from .pacing import SharedPacer

URL = 'https://oaipmh.arxiv.org/oai'
NS = {'o': 'http://www.openarchives.org/OAI/2.0/', 'r': 'http://arxiv.org/OAI/arXivRaw/'}
_ID = re.compile(r'(?:\d{4}\.\d{4,5}|[a-zA-Z][a-zA-Z.-]*/\d{7})\Z')
CATEGORIES = frozenset({'cs.CL', 'cs.AI', 'cs.LG'})


class DiscoveryError(RuntimeError):
    """A page or metadata record could not be obtained completely."""


class ResumptionTokenExpired(DiscoveryError):
    """Restart this category/window and deduplicate already committed records."""


class RecordUnavailable(DiscoveryError):
    """The provider explicitly reports that an identifier does not exist."""


@dataclass(frozen=True)
class Page:
    records: list[dict]
    token: str | None
    response_date: str


def _text(node: ET.Element, path: str, *, required: bool = True) -> str:
    value = ' '.join(node.findtext(path, '', NS).split())
    if required and not value:
        raise DiscoveryError(f'OAI field missing: {path}')
    return value


def _timestamp(value: str, *, rfc: bool = False) -> str:
    try:
        parsed = parsedate_to_datetime(value) if rfc else datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError('timestamp requires timezone')
        return parsed.astimezone(timezone.utc).isoformat()
    except (ValueError, TypeError, OverflowError) as exc:
        raise DiscoveryError(f'Invalid OAI timestamp: {value}') from exc


def parse_record(record: ET.Element) -> dict:
    """Keep explicit revisions and deleted headers; never infer a version from a date."""
    header = record.find('o:header', NS)
    if header is None:
        raise DiscoveryError('OAI record missing header')
    identifier = _text(header, 'o:identifier')
    prefix = 'oai:arXiv.org:'
    paper_id = identifier.removeprefix(prefix)
    if not identifier.startswith(prefix) or not _ID.fullmatch(paper_id):
        raise DiscoveryError(f'Invalid OAI identifier: {identifier}')
    datestamp = _text(header, 'o:datestamp')
    try:
        if len(datestamp) == 10:
            date.fromisoformat(datestamp)
        else:
            _timestamp(datestamp)
    except ValueError as exc:
        raise DiscoveryError(f'Invalid OAI datestamp: {datestamp}') from exc
    result = {'id': paper_id, 'metadata': {}, 'latest_version': None,
              'datestamp': datestamp, 'deleted': header.get('status') == 'deleted'}
    if result['deleted']:
        return result
    node = record.find('o:metadata/r:arXivRaw', NS)
    if node is None:
        raise DiscoveryError('OAI record missing arXivRaw metadata')
    if _text(node, 'r:id') != paper_id:
        raise DiscoveryError(f'OAI header/metadata identity mismatch for {paper_id}')
    history = []
    for entry in node.findall('r:version', NS):
        version = re.fullmatch(r'v([1-9]\d*)', entry.get('version', ''))
        if version is None:
            raise DiscoveryError(f'Invalid explicit version for {paper_id}')
        history.append({'version': int(version[1]),
                        'date': _timestamp(_text(entry, 'r:date'), rfc=True),
                        'size': _text(entry, 'r:size', required=False) or None,
                        'source_type': _text(entry, 'r:source_type', required=False) or None})
    if not history or len({entry['version'] for entry in history}) != len(history):
        raise DiscoveryError(f'Missing or duplicate versions for {paper_id}')
    history.sort(key=lambda entry: entry['version'])
    # A partial raw history cannot establish the original submission boundary.
    if history[0]['version'] != 1:
        raise DiscoveryError(f'Raw history missing first version for {paper_id}')
    authors_raw = _text(node, 'r:authors', required=False)
    metadata = {'id': paper_id, 'title': _text(node, 'r:title', required=False),
                'abstract': _text(node, 'r:abstract', required=False),
                'authors': [authors_raw] if authors_raw else [], 'authors_raw': authors_raw,
                'categories': _text(node, 'r:categories', required=False).split(),
                'published': history[0]['date'], 'updated': history[-1]['date'],
                'version_history': history, 'url': f'https://arxiv.org/abs/{paper_id}',
                'oai_datestamp': datestamp}
    for source, target in [('doi', 'doi'), ('journal-ref', 'journal_ref'),
                           ('license', 'license'), ('comments', 'comments')]:
        value = _text(node, f'r:{source}', required=False)
        if value:
            metadata[target] = value
    result.update(metadata=metadata, latest_version=history[-1]['version'])
    return result


class OaiDiscovery:
    """One bounded page per call; all clients share the provider connection lock."""

    def __init__(self, pacing_dir: str | Path = '/srv/arxiv-work/pacing', *,
                 session=None, pacer=None, attempts: int = 3,
                 page_timeout: float = 180, max_bytes: int = 32 * 1024 * 1024):
        if attempts < 1 or page_timeout <= 0 or max_bytes < 1:
            raise ValueError('positive discovery bounds required')
        self.session = session if session is not None else requests.Session()
        self.pacer = pacer if pacer is not None else SharedPacer(pacing_dir)
        self.attempts, self.page_timeout, self.max_bytes = attempts, page_timeout, max_bytes
        self._owns_session = session is None

    def close(self):
        if self._owns_session:
            self.session.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _xml(self, params: dict) -> ET.Element:
        deadline = time.monotonic() + self.page_timeout
        last_error = 'page timeout'
        for _ in range(self.attempts):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            # Pacing failures propagate unchanged: a long shared cooldown is not
            # evidence of a missing paper and must not consume provider attempts.
            with self.pacer.slot(timeout=remaining):
                response = None
                try:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    response = self.session.get(
                        URL, params=params, stream=True, allow_redirects=False,
                        timeout=(min(15, remaining), min(45, remaining)),
                        headers={'User-Agent': 'LocalPaperArchive/0.2 OAI discovery',
                                 'Accept': 'application/xml', 'Accept-Encoding': 'identity'})
                    self.pacer.defer(response.headers.get('Retry-After'))
                    status = response.status_code
                    if status == 429 or status in (500, 502, 503, 504):
                        last_error = f'OAI HTTP {status}'
                        continue
                    if status != 200:
                        raise DiscoveryError(f'OAI HTTP {status}')
                    chunks, size = [], 0
                    for chunk in response.iter_content(chunk_size=64 * 1024):
                        if time.monotonic() > deadline:
                            raise DiscoveryError('OAI page exceeded time budget')
                        size += len(chunk)
                        if size > self.max_bytes:
                            raise DiscoveryError('OAI page exceeded byte budget')
                        chunks.append(chunk)
                    payload = b''.join(chunks)
                    if b'<!DOCTYPE' in payload.upper() or b'<!ENTITY' in payload.upper():
                        raise DiscoveryError('OAI XML declarations are not permitted')
                    try:
                        root = ET.fromstring(payload)
                    except ET.ParseError as exc:
                        raise DiscoveryError('Malformed OAI XML') from exc
                    if root.tag != f'{{{NS["o"]}}}OAI-PMH':
                        raise DiscoveryError('Unexpected OAI root/namespace')
                    return root
                except requests.RequestException as exc:
                    last_error = f'OAI transport {type(exc).__name__}'
                finally:
                    if response is not None:
                        response.close()
        raise DiscoveryError(f'OAI attempts/time budget exhausted: {last_error}')

    @staticmethod
    def _error(root: ET.Element) -> str | None:
        error = root.find('o:error', NS)
        if error is None:
            return None
        code = error.get('code', 'unknown')
        if code == 'badResumptionToken':
            raise ResumptionTokenExpired('OAI resumption token expired')
        if code == 'idDoesNotExist':
            raise RecordUnavailable('OAI identifier does not exist')
        if code == 'noRecordsMatch':
            return code
        raise DiscoveryError(f'OAI protocol error: {code}')

    def fetch_page(self, category: str, since: date | str, until: date | str | None,
                   token: str | None = None) -> Page:
        # Omitting until lets a resumed historical campaign see records whose
        # metadata changed later; the caller still enforces its submission cutoff.
        if category not in CATEGORIES:
            raise ValueError(f'Unsupported campaign category: {category}')
        start = date.fromisoformat(str(since))
        end = date.fromisoformat(str(until)) if until is not None else None
        if end is not None and start > end:
            raise ValueError('since must not be later than until')
        if token:
            params = {'verb': 'ListRecords', 'resumptionToken': token}
        else:
            params = {'verb': 'ListRecords', 'metadataPrefix': 'arXivRaw',
                      'from': start.isoformat(), 'set': f'cs:cs:{category.split(".")[1]}'}
            if end is not None:
                params['until'] = end.isoformat()
        root = self._xml(params)
        response_date = _timestamp(_text(root, 'o:responseDate'))
        if self._error(root) == 'noRecordsMatch':
            if token:
                raise DiscoveryError('Unexpected noRecordsMatch during pagination')
            return Page([], None, response_date)
        listing = root.find('o:ListRecords', NS)
        if listing is None:
            raise DiscoveryError('OAI response missing ListRecords')
        records = [parse_record(record) for record in listing.findall('o:record', NS)]
        if not records:
            raise DiscoveryError('OAI empty page without noRecordsMatch')
        next_token = listing.findtext('o:resumptionToken', '', NS).strip() or None
        if next_token is not None and next_token == token:
            raise DiscoveryError('OAI repeated the current resumption token')
        return Page(records, next_token, response_date)

    def get_record(self, paper_id: str) -> dict:
        if not _ID.fullmatch(paper_id):
            raise ValueError('GetRecord requires a canonical unversioned arXiv ID')
        root = self._xml({'verb': 'GetRecord', 'metadataPrefix': 'arXivRaw',
                          'identifier': f'oai:arXiv.org:{paper_id}'})
        self._error(root)
        record = root.find('o:GetRecord/o:record', NS)
        if record is None:
            raise DiscoveryError('OAI response missing GetRecord')
        result = parse_record(record)
        if result['id'] != paper_id:
            raise DiscoveryError('OAI GetRecord returned a different identifier')
        return result
