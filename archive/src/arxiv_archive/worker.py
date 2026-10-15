"""Single archival worker with shared arXiv pacing and durable retry state.

Every cooperating client must use ArxivClient (or the same flock/state protocol).
The lock covers one HTTP connection through response close; persisted cooldowns
survive process restarts. Litwatch shares this protocol through its pacing bind mount.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import json
import logging
from pathlib import Path
import re
import signal
import tempfile
import threading
import time
from urllib.parse import urljoin, urlsplit

import requests

from .pacing import Cooldown, PacingError, SharedPacer, retry_after_seconds
from .catalog import validate_id
from .source_text import NoTextSource
from .store import Store

LOG = logging.getLogger('paper-worker')


class Retryable(Exception):
    def __init__(self, message: str, retry_after: float = 0):
        super().__init__(message)
        self.retry_after = retry_after


class EndpointFailure(Retryable):
    """A rejected/empty/interrupted artifact response, not an access or rate denial."""


class RateLimited(Retryable):
    """No request was issued; waiting must not consume the retry budget."""


class Unavailable(Exception):
    pass


class PermanentFailure(Exception):
    pass


def artifact_url(paper_id: str, version: int, kind: str) -> str:
    validate_id(paper_id)
    if type(version) is not int or version < 1:
        raise ValueError('an explicit positive version is required')
    if kind not in ('pdf', 'source'):
        raise ValueError('worker supports pdf and source')
    return f'https://arxiv.org/{"pdf" if kind == "pdf" else "src"}/{paper_id}v{version}'


def validate_url(url: str) -> None:
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or parsed.hostname not in {'arxiv.org', 'export.arxiv.org'}
            or parsed.username or parsed.password or parsed.port not in (None, 443) or parsed.fragment):
        raise PermanentFailure('arXiv request/redirect must remain on allowed HTTPS hosts')
    if parsed.path == '/api/query' and parsed.hostname == 'export.arxiv.org':
        return
    match = re.fullmatch(r'/(?:pdf|src|e-print)/(.+)v([1-9]\d*)', parsed.path)
    if parsed.query or match is None:
        raise PermanentFailure('unexpected arXiv request path')
    try:
        validate_id(match[1])
    except ValueError as exc:
        raise PermanentFailure('invalid arXiv identity in request') from exc


class ArxivClient:
    def __init__(self, state_dir: Path, session=None, user_agent='LocalPaperArchive/1.0',
                 stop: threading.Event | None = None):
        self.state_dir = Path(state_dir)
        self.pacer = SharedPacer(self.state_dir)
        self.lock_path = self.state_dir / 'arxiv-rate.lock'
        self.state_path = self.state_dir / 'arxiv-rate.json'
        self.session = session if session is not None else requests.Session()
        self.user_agent = user_agent
        self.stop = stop or threading.Event()

    def _read_cooldown(self) -> float:
        return self.pacer.read_cooldown()

    def fetch(self, url: str, destination: Path, max_bytes: int,
              heartbeat=None) -> dict:
        """Stream one bounded artifact; raise RateLimited without connecting if early.

        This also supports the official export.arxiv.org/api/query endpoint for
        explicit metadata lookups. No arbitrary URL fetch surface is exposed.
        """
        validate_url(url)
        if max_bytes <= 0:
            raise ValueError('max_bytes must be positive')
        destination = Path(destination)
        try:
            with self.pacer.slot(wait=False):
                return self._fetch_locked(url, destination, max_bytes, heartbeat)
        except Cooldown as exc:
            raise RateLimited(str(exc), exc.retry_after) from exc
        except PacingError as exc:
            raise PermanentFailure(str(exc)) from exc

    def _fetch_locked(self, url, destination, max_bytes, heartbeat):
        try:
            try:
                return self._fetch_endpoint(url, destination, max_bytes, heartbeat)
            except EndpointFailure as exc:
                parsed = urlsplit(url)
                if parsed.hostname != 'export.arxiv.org' or not parsed.path.startswith(('/pdf/', '/src/', '/e-print/')):
                    raise
                # Export can reject an artifact which the official reader serves.
                # Retry once, same identity/version, after closing/deleting partial
                # data. Keep the shared lock and pace this connection like redirects.
                fallback = url.replace('https://export.arxiv.org/', 'https://arxiv.org/', 1)
                LOG.warning('artifact endpoint fallback url=%s reason=%s', url, exc)
                self.pacer._save(max(self._read_cooldown(), time.time() + 3))
                if self.stop.wait(max(3, self._read_cooldown() - time.time())):
                    raise Retryable('worker stopping', 3)
                return self._fetch_endpoint(fallback, destination, max_bytes, heartbeat)
        except EndpointFailure as exc:
            # A persistent 406 slows the provider globally. Broken/empty bodies
            # back off only their own job so one bad file cannot stall daily work.
            if exc.retry_after:
                self.pacer._save(max(self._read_cooldown(), time.time() + exc.retry_after))
            raise Retryable(str(exc), exc.retry_after) from exc

    def _fetch_endpoint(self, url, destination, max_bytes, heartbeat):
        success = False
        response = None
        try:
            for redirect_count in range(4):
                validate_url(url)
                if self.stop.is_set():
                    raise Retryable('worker stopping', 3)
                if heartbeat:
                    heartbeat()
                response = self.session.get(url, stream=True, allow_redirects=False,
                    timeout=(15, 60), headers={'User-Agent': self.user_agent,
                                              'Accept-Encoding': 'identity'})
                code = response.status_code
                self.pacer.defer(response.headers.get('Retry-After'))
                if code in (301, 302, 303, 307, 308):
                    target = urljoin(url, response.headers.get('Location', ''))
                    validate_url(target)
                    response.close()
                    response = None
                    if redirect_count == 3:
                        raise PermanentFailure('too many arXiv redirects')
                    self.pacer._save(max(self._read_cooldown(), time.time() + 3))
                    if self.stop.wait(max(3, self._read_cooldown() - time.time())):
                        raise Retryable('worker stopping', 3)
                    url = target
                    continue
                if code in (429, 503):
                    delay = max(3, retry_after_seconds(response.headers.get('Retry-After'), time.time()))
                    self.pacer._save(max(self._read_cooldown(), time.time() + delay))
                    raise Retryable(f'arXiv HTTP {code}', delay)
                if code == 406:
                    raise EndpointFailure(f'arXiv HTTP 406 from {urlsplit(url).hostname}', 60)
                if code in (404, 410):
                    raise Unavailable(f'arXiv HTTP {code}')
                if code == 403 or 500 <= code <= 599:
                    raise Retryable(f'arXiv HTTP {code}')
                if code != 200:
                    raise PermanentFailure(f'unexpected arXiv HTTP {code}')
                advertised = response.headers.get('Content-Length')
                if advertised is not None and int(advertised) > max_bytes:
                    raise PermanentFailure('artifact exceeds configured byte limit')
                count = 0
                with destination.open('wb') as output:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if self.stop.is_set():
                            raise Retryable('worker stopping', 3)
                        if heartbeat:
                            heartbeat()
                        count += len(chunk)
                        if count > max_bytes:
                            raise PermanentFailure('artifact exceeds configured byte limit')
                        output.write(chunk)
                if advertised is not None and count != int(advertised):
                    raise EndpointFailure('arXiv body length differs from Content-Length')
                if not count:
                    raise EndpointFailure('empty arXiv response')
                success = True
                return {'url': url, 'bytes': count,
                        'content_type': response.headers.get('Content-Type'),
                        'retrieved_at': time.time()}
            raise PermanentFailure('redirect loop')
        except requests.RequestException as exc:
            raise EndpointFailure(f'arXiv transport error: {type(exc).__name__}') from exc
        finally:
            if response is not None:
                response.close()
            if not success:
                destination.unlink(missing_ok=True)


@contextmanager
def worker_lock(path: Path):
    with path.open('a+b') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError('another paper acquisition worker is running') from exc
        yield


class Worker:
    def __init__(self, store: Store, client: ArxivClient | None = None,
                 max_attempts: int = 8, stop: threading.Event | None = None):
        self.store = store
        self.stop = stop or threading.Event()
        self.client = client or ArxivClient(store.work / 'pacing', stop=self.stop)
        if max_attempts < 1:
            raise ValueError('max_attempts must be positive')
        self.max_attempts = max_attempts

    def run_once(self) -> dict | None:
        """Claim and process one eligible job; no internal retry loop."""
        with worker_lock(self.store.work / 'catalog/acquisition-worker.lock'):
            self.store._guard()
            job = self.store.catalog.claim_job(lease_seconds=300)
            if job is None:
                return None
            return self._process(job)

    def _process(self, job: dict) -> dict:
        catalog = self.store.catalog
        done, lost = threading.Event(), threading.Event()
        def heartbeat():
            if lost.is_set():
                raise RuntimeError('acquisition lease lost')
        def renew():
            while not done.wait(30):
                try:
                    if not catalog.renew_job(job['id'], job['lease_token'], 300):
                        lost.set()
                        return
                except Exception:
                    lost.set()
                    return
        thread = threading.Thread(target=renew, daemon=True)
        thread.start()
        state, error, delay, counted = 'complete', None, 0, True
        try:
            url = artifact_url(job['paper_id'], job['version'], job['kind'])
            if job.get('provenance', {}).get('origin') == 'recovery':
                url = url.replace('https://arxiv.org/', 'https://export.arxiv.org/', 1)
            if catalog.get_artifact(job['paper_id'], job['kind'], job['version']) is None:
                with tempfile.TemporaryDirectory(prefix='acquire-', dir=self.store.scratch) as tmp:
                    payload = Path(tmp) / 'payload'
                    receipt = self.client.fetch(url, payload, self.store.max_bytes, heartbeat=heartbeat)
                    heartbeat()
                    if job['kind'] == 'source':
                        with payload.open('rb') as source:
                            if source.read(5) == b'%PDF-':
                                raise Unavailable('arXiv returned PDF instead of a source package')
                    self.store.put(job['paper_id'], job['version'], job['kind'], payload,
                                   provenance={**job['provenance'], 'source': 'arxiv', **receipt})
        except RateLimited as exc:
            state, error, delay, counted = 'pending', str(exc), exc.retry_after, False
        except Retryable as exc:
            state = 'failed' if job['attempts'] >= self.max_attempts else 'pending'
            delay = max(exc.retry_after, min(3600, 30 * 2 ** min(job['attempts'] - 1, 7)))
            error = str(exc)
        except (Unavailable, NoTextSource) as exc:
            state, error = 'unavailable', str(exc)
        except (ValueError, PermanentFailure, FileExistsError) as exc:
            state, error = 'failed', str(exc)
        except Exception as exc:
            state = 'failed' if job['attempts'] >= self.max_attempts else 'pending'
            delay, error = 60, f'{type(exc).__name__}: {exc}'
        finally:
            done.set()
            thread.join(timeout=5)
        if not catalog.finish_job(job['id'], job['lease_token'], state, error, delay,
                                  count_attempt=counted):
            raise RuntimeError('acquisition lease lost before outcome recording')
        result = catalog.get_job(job['id'])
        LOG.info('job=%s paper=%s kind=%s status=%s', job['id'], job['paper_id'], job['kind'], result['status'])
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work', type=Path, default=Path('/srv/arxiv-work'))
    parser.add_argument('--archive', type=Path, default=Path('/srv/arxiv-archive'))
    parser.add_argument('--once', action='store_true', help='process one eligible job and exit')
    parser.add_argument('--max-attempts', type=int, default=8)
    parser.add_argument('--user-agent', default='LocalPaperArchive/1.0')
    parser.add_argument('--no-mount-guard', action='store_true',
                        help='skip the data-mount checks: only for a disposable archive (verification)')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    store = Store(args.work, args.archive, guard=not args.no_mount_guard)
    client = ArxivClient(store.work / 'pacing', user_agent=args.user_agent, stop=stop)
    worker = Worker(store, client, args.max_attempts, stop)
    try:
        while not stop.is_set():
            outcome = worker.run_once()
            if args.once:
                print(json.dumps(outcome))
                break
            if outcome is None:
                stop.wait(3)
            else:
                # Do not churn through a large backlog while every request is
                # held by the same provider cooldown. Signals interrupt this wait.
                stop.wait(max(0, client._read_cooldown() - time.time()))
    finally:
        store.close()


if __name__ == '__main__':
    main()
