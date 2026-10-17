"""Private, read-only browser gateway. The archive credential stays server-side."""
from __future__ import annotations

import argparse
import hashlib
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlsplit, unquote

import requests

from .api import PaperServer, read_token

ASSETS = Path(__file__).with_name('web')
STATIC = {'/': ('index.html', 'text/html; charset=utf-8'),
          '/assets/app.js': ('app.js', 'text/javascript; charset=utf-8'),
          '/assets/style.css': ('style.css', 'text/css; charset=utf-8'),
          '/assets/favicon.svg': ('favicon.svg', 'image/svg+xml'),
          '/favicon.ico': ('favicon.svg', 'image/svg+xml')}


class WebServer(PaperServer):
    def __init__(self, address, api_url: str, token: str, max_workers: int = 4):
        target = urlsplit(api_url)
        if target.scheme != 'http' or target.hostname not in ('127.0.0.1', 'localhost') or target.path not in ('', '/'):
            raise ValueError('browser gateway requires a loopback HTTP API')
        self.api_url = api_url.rstrip('/')
        super().__init__(address, None, token, max_workers)
        self.RequestHandlerClass = WebHandler


class WebHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'PaperArchiveWeb/1'
    sys_version = ''

    def setup(self):
        super().setup()
        self.connection.settimeout(30)

    def log_message(self, *args):
        pass

    def response_headers(self, status: int, content_type: str, length: int | None = None,
                         cache: str = 'private, no-store'):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Cache-Control', cache)
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        self.send_header('Connection', 'close')
        if length is not None:
            self.send_header('Content-Length', str(length))
        self.close_connection = True

    def respond(self, status, body, content_type='text/plain; charset=utf-8'):
        self.response_headers(status, content_type, len(body))
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def do_GET(self):
        try:
            self.get()
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True

    do_HEAD = do_GET

    def get(self):
        target = urlsplit(self.path)
        route = unquote(target.path)
        if len(self.path) > 8192 or target.netloc or target.scheme or '..' in route.split('/'):
            return self.respond(400, b'Invalid request')
        static = STATIC.get(route)
        if route.startswith('/papers/'):
            static = STATIC['/']
        if static:
            return self.serve_static(*static)
        if not (route in ('/v1/search', '/v1/search/status', '/v1/bibtex') or
                route.startswith('/v1/papers/')):
            return self.respond(404, b'Not found')
        try:
            response = requests.get(self.server.api_url + self.path,
                headers={'Authorization': 'Bearer ' + self.server.token},
                stream=True, timeout=(5, 120), allow_redirects=False)
        except requests.RequestException:
            return self.respond(503, b'{"error":"Paper service temporarily unavailable"}', 'application/json')
        with response:
            self.response_headers(response.status_code, response.headers.get('Content-Type', 'application/octet-stream'),
                         int(response.headers['Content-Length']) if 'Content-Length' in response.headers else None)
            if response.headers.get('Content-Disposition'):
                self.send_header('Content-Disposition', response.headers['Content-Disposition'])
            self.end_headers()
            if self.command != 'HEAD':
                try:
                    for chunk in response.iter_content(128 * 1024):
                        self.wfile.write(chunk)
                except requests.RequestException:
                    self.close_connection = True

    def serve_static(self, name: str, media_type: str):
        # Assets are read per request so edits deploy without a restart; browsers
        # revalidate with the content hash instead of re-downloading each visit.
        body = (ASSETS / name).read_bytes()
        etag = '"' + hashlib.sha256(body).hexdigest()[:20] + '"'
        if etag in self.headers.get('If-None-Match', ''):
            self.response_headers(304, media_type, cache='private, no-cache')
            self.send_header('ETag', etag)
            return self.end_headers()
        self.response_headers(200, media_type, len(body), cache='private, no-cache')
        self.send_header('ETag', etag)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def do_POST(self):
        self.respond(405, b'Read-only interface')

    do_PUT = do_POST
    do_DELETE = do_POST
    do_PATCH = do_POST


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8788)
    parser.add_argument('--api-url', default='http://127.0.0.1:8787')
    parser.add_argument('--token-file', required=True, type=Path)
    args = parser.parse_args(argv)
    server = WebServer((args.host, args.port), args.api_url, read_token(args.token_file))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
