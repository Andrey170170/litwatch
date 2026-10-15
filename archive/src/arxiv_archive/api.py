#!/usr/bin/env python3
"""Authenticated internal archive HTTP API.

HTTP always requires a bearer token. Uploads are streamed with explicit length,
size and concurrency bounds; clients submit acquisition intents separately.
"""
from __future__ import annotations

import hmac
import json
import os
from pathlib import Path
import re
import shutil
import socket
import stat
import tempfile
import threading
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

from .catalog import validate_id
from .store import Store

JSON_LIMIT = 1024 * 1024


def reject_nonfinite(value: str):
    raise ValueError("nonfinite JSON number")


def read_token(path: Path) -> str:
    """Require a private token file; never return its contents in diagnostics."""
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
        raise ValueError("token file must be a regular file with mode 0600 or stricter")
    token = path.read_text().strip()
    if len(token) < 32 or len(token) > 512 or not re.fullmatch(r"[A-Za-z0-9._~-]+", token):
        raise ValueError("token must contain 32-512 URL-safe characters")
    return token


class APIError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class PaperServer(ThreadingHTTPServer):
    """Bound request concurrency before creating threads or reading uploads."""
    daemon_threads = False
    allow_reuse_address = True

    def __init__(self, address, store: Store, token: str, max_workers: int = 4):
        if max_workers < 1:
            raise ValueError("max_workers must be positive")
        self.store = store
        self.token = token
        self.slots = threading.BoundedSemaphore(max_workers)
        self.search_slots = threading.BoundedSemaphore(2)
        super().__init__(address, PaperHandler)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            try:
                request.settimeout(2)
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\nRetry-After: 2\r\n\r\n")
            except OSError:
                pass
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


class PaperHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "PaperArchive/1"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(30)

    def log_message(self, format, *args):
        # Do not log URLs, query strings, credentials, metadata or request bodies.
        pass

    def handle_expect_100(self):
        try:
            self.authenticate()
            self.content_length(self.server.store.max_bytes)
        except APIError as exc:
            self.json_response(exc.status, {"error": str(exc)})
            return False
        self.send_response_only(100)
        self.end_headers()
        return True

    def authenticate(self):
        values = self.headers.get_all("Authorization", [])
        expected = "Bearer " + self.server.token
        if len(values) != 1 or not hmac.compare_digest(values[0].encode(), expected.encode()):
            raise APIError(401, "bearer authentication required")

    def content_length(self, limit: int) -> int:
        if self.headers.get("Transfer-Encoding") is not None:
            raise APIError(400, "chunked uploads are unsupported; send Content-Length")
        values = self.headers.get_all("Content-Length", [])
        if len(values) != 1 or not re.fullmatch(r"[0-9]+", values[0]):
            raise APIError(411, "one valid Content-Length is required")
        length = int(values[0])
        if length > limit:
            raise APIError(413, "request exceeds size limit")
        return length

    def body_json(self) -> dict:
        length = self.content_length(JSON_LIMIT)
        body = self.rfile.read(length)
        if len(body) != length:
            raise APIError(400, "incomplete request body")
        try:
            result = json.loads(body, parse_constant=reject_nonfinite)
        except (ValueError, UnicodeDecodeError):
            raise APIError(400, "invalid JSON") from None
        if not isinstance(result, dict):
            raise APIError(400, "JSON object required")
        return result

    def json_response(self, status: int, data: dict):
        body = json.dumps(data, ensure_ascii=False, allow_nan=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.send_header("Cache-Control", "no-store")
        if status == 401:
            self.send_header("WWW-Authenticate", 'Bearer realm="paper-archive"')
        self.end_headers()
        self.close_connection = True
        self.wfile.write(body)

    @staticmethod
    def version(query: dict, required: bool = False) -> int | None:
        values = query.get("version")
        if values is None:
            if required:
                raise APIError(400, "explicit positive version is required")
            return None
        if len(values) != 1 or not re.fullmatch(r"[0-9]{1,6}", values[0]):
            raise APIError(400, "invalid version")
        value = int(values[0])
        if required and value < 1:
            raise APIError(400, "explicit positive version is required")
        return value

    def text_response(self, body: str, *, filename: str):
        encoded = body.encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/x-bibtex; charset=utf-8')
        self.send_header('Content-Disposition', f'attachment; filename="{filename}"')
        self.send_header('Content-Length', str(len(encoded)))
        self.send_header('Cache-Control', 'private, no-store')
        self.send_header('Connection', 'close')
        self.end_headers()
        self.close_connection = True
        self.wfile.write(encoded)

    def search_response(self, query: dict, *, status: bool = False):
        from .search import SearchIndex
        if not self.server.search_slots.acquire(blocking=False):
            raise APIError(503, 'Search is busy; retry shortly')
        try:
            index = SearchIndex(self.server.store.work / 'search/search.sqlite')
            if status:
                return self.json_response(200, index.status())
            allowed = {'q', 'category', 'date_from', 'date_to', 'availability', 'limit', 'offset', 'sort'}
            if set(query) - allowed or any(len(v) != 1 for v in query.values()):
                raise APIError(400, 'Invalid search parameters')
            params = {key: values[0] for key, values in query.items()}
            for name in ('limit', 'offset'):
                if name in params:
                    params[name] = int(params[name])
            return self.json_response(200, index.search(**params))
        except ValueError as exc:
            raise APIError(400, str(exc)) from None
        except RuntimeError as exc:
            # Search errors are deliberately user-facing and contain no SQL/paths.
            raise APIError(503, str(exc)) from None
        finally:
            self.server.search_slots.release()

    def papers_response(self, query: dict):
        """GET /v1/papers: the papers arXiv received as new in a date window, read from the
        catalog itself (not the search index, which is rebuilt apart and can lag).

        `since`, `until` (YYYY-MM-DD, required, inclusive) bound the first version's
        submission date: the date arXiv stamps v1 with, which litwatch's arXiv intake stores
        as `published`. A window of submission dates is what "the papers of these weeks"
        means: a revision does not bring an old paper back, and a paper keeps its date when
        its metadata is edited, unlike an OAI datestamp. A paper is announced a day or so
        after it is submitted, so the newest days of a window still fill as arXiv announces.
        `categories` (comma-separated) keeps papers listed in any of them, cross-lists
        included; omitted, every category. `limit` (1-1000, default 500) caps a page;
        `cursor` is the previous page's `next`, opaque. The answer is {items, next}: each item
        a `search.listing_item`, `next` null once the window is exhausted. A page may be short
        or empty before the end, so continue until `next` is null.
        """
        allowed = {'since', 'until', 'categories', 'cursor', 'limit'}
        if set(query) - allowed or any(len(v) != 1 for v in query.values()):
            raise APIError(400, 'Invalid listing parameters')
        params = {key: values[0] for key, values in query.items()}
        window = []
        for name in ('since', 'until'):
            try:
                value = params.get(name, '')
                if date.fromisoformat(value).isoformat() != value:
                    raise ValueError
            except ValueError:
                raise APIError(400, f'{name} must be a date (YYYY-MM-DD)') from None
            window.append(value)
        if window[0] > window[1]:
            raise APIError(400, 'since must not follow until')
        categories = {c for c in params.get('categories', '').split(',') if c}
        if any(not re.fullmatch(r'[A-Za-z][A-Za-z0-9.-]{0,39}', c) for c in categories):
            raise APIError(400, 'Invalid arXiv category')
        limit = params.get('limit', '500')
        if not re.fullmatch(r'[0-9]{1,4}', limit) or not 1 <= int(limit) <= 1000:
            raise APIError(400, 'limit must be 1-1000')
        after = None
        if 'cursor' in params:
            published, _, paper_id = params['cursor'].partition(':')
            try:
                date.fromisoformat(published)
                validate_id(paper_id)
            except ValueError:
                raise APIError(400, 'Invalid cursor') from None
            after = (published, paper_id)
        catalog = self.server.store.catalog
        if not catalog.dates_indexed():
            raise APIError(503, 'Paper dates are not indexed yet; run paper-archive index-dates')
        result = catalog.list_papers(*window, categories, after, int(limit))
        if result['next']:
            result['next'] = ':'.join(result['next'])
        return self.json_response(200, result)

    def file_response(self, paper_id: str, kind: str, version: int | None,
                      source_path: str | None = None):
        store = self.server.store
        with store.read_lock:
            if source_path is None:
                path = store.get(paper_id, kind=kind, version=version)
            else:
                path = store.source_file(paper_id, source_path, version=version)
            # Opening under the cache lock pins the inode against eviction.
            stream = path.open("rb")
            size = os.fstat(stream.fileno()).st_size
        with stream:
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf" if kind == "pdf" else "application/octet-stream")
            self.send_header("Content-Length", str(size))
            self.send_header("Cache-Control", "private, no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            shutil.copyfileobj(stream, self.wfile, length=1024 * 1024)

    def upload(self, paper_id: str, kind: str, version: int):
        store = self.server.store
        length = self.content_length(store.max_bytes)
        if length == 0:
            raise APIError(400, "empty artifact")
        # Store guards mount identity before paths are created or uploads written.
        with store.read_lock:
            store.status()
            if shutil.disk_usage(store.scratch).free < length + 8 * 1024**3:
                raise APIError(507, "insufficient staging space")
            descriptor, name = tempfile.mkstemp(prefix="http-upload-", dir=store.scratch)
        path = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as output:
                remaining = length
                while remaining:
                    chunk = self.rfile.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise APIError(400, "incomplete artifact upload")
                    output.write(chunk)
                    remaining -= len(chunk)
            with store.read_lock:
                result = store.put(paper_id, version, kind, path,
                                   provenance={"origin": "internal-http-upload"})
            self.json_response(200, result)
        finally:
            path.unlink(missing_ok=True)

    def dispatch(self):
        self.authenticate()
        target = urlsplit(self.path)
        route = unquote(target.path)
        query = parse_qs(target.query, keep_blank_values=True)
        store = self.server.store
        method = self.command
        if len(self.path) > 8192:
            raise APIError(400, 'Request URL is too long')
        if route in ('/v1/search', '/v1/search/status') and method == 'GET':
            return self.search_response(query, status=route.endswith('/status'))
        if route == '/v1/papers' and method == 'GET':
            return self.papers_response(query)
        if route == '/v1/bibtex' and method == 'GET':
            from .citations import bibtex
            values = query.get('ids', [])
            if len(values) != 1:
                raise APIError(400, 'One ids parameter is required')
            ids = list(dict.fromkeys(values[0].split(',')))
            if not 1 <= len(ids) <= 50:
                raise APIError(400, 'Select between 1 and 50 papers')
            papers = [store.catalog.get_paper(paper_id) for paper_id in ids]
            if any(p is None for p in papers):
                raise FileNotFoundError()
            return self.text_response('\n'.join(bibtex(p) for p in papers), filename='papers.bib')
        if route == "/v1/status" and method == "GET":
            with store.read_lock:
                result = store.status()
            return self.json_response(200, result)
        if route == "/v1/jobs" and method == "POST":
            body = self.body_json()
            required = ("paper_id", "version", "kind")
            if any(key not in body for key in required):
                raise APIError(400, "paper_id, version and kind are required")
            if type(body["version"]) is not int or body["version"] < 1:
                raise APIError(400, "explicit positive version is required")
            if body["kind"] not in ("pdf", "source"):
                raise APIError(400, "kind must be pdf or source")
            priority = body.get("priority", 100)
            if type(priority) is not int or not 0 <= priority <= 1000:
                raise APIError(400, "priority must be an integer from 0 to 1000")
            provenance = body.get("provenance", {})
            if not isinstance(provenance, dict):
                raise APIError(400, "provenance must be an object")
            with store.read_lock:
                result = store.catalog.enqueue(body["paper_id"], body["version"], body["kind"],
                                               priority=priority, provenance=provenance)
            return self.json_response(202, result)
        retry_match = re.fullmatch(r"/v1/jobs/([0-9]+)/retry", route)
        if retry_match and method == "POST":
            if self.body_json():
                raise APIError(400, "retry accepts an empty JSON object")
            with store.read_lock:
                result = store.catalog.retry_job(int(retry_match.group(1)))
            return self.json_response(202, result)
        if route.startswith("/v1/jobs/") and method == "GET":
            job_id = route.removeprefix("/v1/jobs/")
            if not re.fullmatch(r"[0-9]+", job_id):
                raise APIError(400, "invalid job id")
            with store.read_lock:
                result = store.catalog.get_job(int(job_id))
            if result is None:
                raise FileNotFoundError()
            return self.json_response(200, result)
        if route.startswith("/v1/papers/"):
            paper_id = route.removeprefix("/v1/papers/")
            action = "metadata"
            # Suffix dispatch preserves historical IDs such as hep-th/9901001.
            for suffix in ("source-file", "sources", "source", "pdf", "bibtex", "text"):
                if paper_id.endswith("/" + suffix):
                    paper_id = paper_id[:-(len(suffix) + 1)]
                    action = suffix
                    break
            version = self.version(query, required=method == "PUT" and action in ("pdf", "source"))
            if action == 'bibtex' and method == 'GET':
                from .citations import bibtex
                result = store.catalog.get_paper(paper_id)
                if result is None:
                    raise FileNotFoundError()
                return self.text_response(bibtex(result), filename=paper_id.replace('/', '_') + '.bib')
            if action in ("pdf", "source"):
                if method == "GET":
                    return self.file_response(paper_id, action, version)
                if method == "PUT":
                    return self.upload(paper_id, action, version)
            elif action == "sources" and method == "GET":
                with store.read_lock:
                    result = store.sources(paper_id, version=version)
                return self.json_response(200, {"files": result})
            elif action == "text" and method == "GET":
                # The paper's LaTeX as one text, and the arXiv version it is of (null when
                # unknown: the historical LaTeX). Only a source's text: a paper without one
                # is read from its PDF by the client.
                result = store.text(paper_id, version=version)
                return self.json_response(200, {"paper_id": paper_id, "source": "latex", **result})
            elif action == "source-file" and method == "GET":
                paths = query.get("path", [])
                if len(paths) != 1 or not paths[0]:
                    raise APIError(400, "one source path is required")
                return self.file_response(paper_id, "source", version, paths[0])
            elif action == "metadata":
                if method == "GET":
                    with store.read_lock:
                        result = store.catalog.get_paper(paper_id)
                        if result is not None:
                            result["artifacts"] = store.catalog.list_artifacts(paper_id)
                            result["has_historical_source"] = bool(store.catalog.latex_locations(paper_id))
                    if result is None:
                        raise FileNotFoundError()
                    with store.catalog.lock:
                        result['jobs'] = [dict(row) for row in store.catalog.db.execute(
                            'SELECT kind,version,status,error FROM jobs WHERE paper_id=? ORDER BY version DESC,kind LIMIT 50',
                            (paper_id,))]
                    from .search import normalize_paper
                    result['display'] = normalize_paper(result)
                    result['display']['has_pdf'] = any(a['kind'] == 'pdf' and a['status'] == 'available'
                                                       for a in result['artifacts'])
                    result['display']['has_source'] = result['has_historical_source'] or any(
                        a['kind'] == 'source' and a['status'] == 'available' for a in result['artifacts'])
                    return self.json_response(200, result)
                if method == "PUT":
                    body = self.body_json()
                    metadata = body.get("metadata", {})
                    latest = body.get("latest_version")
                    if not isinstance(metadata, dict) or (latest is not None and (type(latest) is not int or latest < 1)):
                        raise APIError(400, "metadata must be an object and latest_version a positive integer")
                    with store.read_lock:
                        result = store.catalog.upsert_paper(paper_id, metadata, latest_version=latest)
                    return self.json_response(200, result)
        raise APIError(404, "endpoint not found")

    def handle_request(self):
        try:
            self.dispatch()
        except APIError as exc:
            self.json_response(exc.status, {"error": str(exc)})
        except FileNotFoundError:
            self.json_response(404, {"error": "paper, artifact or job not found"})
        except FileExistsError:
            self.json_response(409, {"error": "artifact conflicts with existing content"})
        except (ValueError, TypeError, KeyError):
            self.json_response(400, {"error": "invalid request or artifact"})
        except (socket.timeout, TimeoutError):
            self.close_connection = True
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except (RuntimeError, OSError):
            self.json_response(503, {"error": "archive temporarily unavailable"})
        except Exception:
            # Unexpected failures must not reveal filesystem paths or SQL details.
            self.json_response(500, {"error": "internal archive error"})

    do_GET = handle_request
    do_PUT = handle_request
    do_POST = handle_request

