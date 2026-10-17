"""Search delivery, citation safety, and a browser surface without write authority."""
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest

from arxiv_archive.api import PaperServer
from arxiv_archive.citations import bibtex
from arxiv_archive.store import Store
from arxiv_archive.web_server import WebServer


class SearchServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = Store(work=root / 'work', archive=root / 'archive', guard=False)
        self.token = 'private-test-' + 'a' * 40
        self.api = PaperServer(('127.0.0.1', 0), self.store, self.token)
        self.web = WebServer(('127.0.0.1', 0), f'http://127.0.0.1:{self.api.server_port}', self.token)
        self.threads = []
        for server in (self.api, self.web):
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.threads.append(thread)
        self.store.catalog.upsert_paper('1706.03762', {
            'title': 'Attention Is All You Need', 'abstract': 'A transformer architecture.',
            'authors_parsed': [['Vaswani', 'Ashish', '']], 'authors': 'Ashish Vaswani',
            'categories': 'cs.CL cs.LG', 'versions': [{'version': 'v1', 'created': 'Mon, 12 Jun 2017 17:57:34 GMT'}]}, 7)

    def tearDown(self):
        for server in (self.web, self.api):
            server.shutdown()
            server.server_close()
        for thread in self.threads:
            thread.join()
        self.store.catalog.close()
        self.temp.cleanup()

    def request(self, path, method='GET', body=None, *, api=False):
        server = self.api if api else self.web
        client = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
        try:
            client.request(method, path, body=body)
            r = client.getresponse()
            return r.status, dict(r.getheaders()), r.read()
        finally:
            client.close()

    def test_private_api_and_read_only_proxy(self):
        self.assertEqual(self.request('/v1/papers/1706.03762', api=True)[0], 401)
        status, _, body = self.request('/v1/papers/1706.03762')
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)['display']['published'], '2017-06-12')
        item = json.loads(body)['item']
        self.assertEqual((item['id'], item['authors'], item['published'], item['categories']),
                         ('1706.03762', ['Ashish Vaswani'], '2017-06-12', ['cs.CL', 'cs.LG']))
        self.assertNotIn(self.token.encode(), body)
        for method in ('POST', 'PUT', 'DELETE', 'PATCH'):
            self.assertEqual(self.request('/v1/papers/1706.03762', method, '{}')[0], 405)
        self.assertEqual(self.request('/v1/jobs/1')[0], 404)
        self.assertEqual(self.request('/v1/status')[0], 404)
        self.assertEqual(self.request('/v1/papers/../status')[0], 400)

    def test_static_assets_revalidate(self):
        for path, media_type in [('/', 'text/html'), ('/papers/1706.03762', 'text/html'),
                                 ('/assets/app.js', 'text/javascript'), ('/assets/favicon.svg', 'image/svg+xml'),
                                 ('/favicon.ico', 'image/svg+xml')]:
            status, headers, body = self.request(path)
            self.assertEqual(status, 200, path)
            self.assertTrue(headers['Content-Type'].startswith(media_type), path)
            self.assertTrue(body, path)
        status, headers, _ = self.request('/assets/style.css')
        self.assertEqual(headers['Cache-Control'], 'private, no-cache')
        client = http.client.HTTPConnection('127.0.0.1', self.web.server_port, timeout=5)
        try:
            client.request('GET', '/assets/style.css', headers={'If-None-Match': headers['ETag']})
            response = client.getresponse()
            self.assertEqual((response.status, response.read()), (304, b''))
        finally:
            client.close()

    def test_bibtex_and_missing_selection(self):
        status, headers, body = self.request('/v1/papers/1706.03762/bibtex')
        self.assertEqual(status, 200)
        self.assertIn(b'year = {2017}', body)
        self.assertIn(b'author = {Vaswani, Ashish}', body)
        self.assertIn('attachment', headers['Content-Disposition'])
        self.assertEqual(self.request('/v1/bibtex?ids=1706.03762,1706.03762')[2].count(b'@misc'), 1)
        self.assertEqual(self.request('/v1/bibtex?ids=1706.03762,1706.00000')[0], 404)
        self.assertEqual(self.request('/v1/bibtex')[0], 400)

    def test_search_not_started_is_explicit(self):
        status, _, body = self.request('/v1/search/status')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['indexed_papers'], 0)
        self.assertEqual(self.request('/v1/search?q=attention')[0], 503)
        self.assertEqual(self.request('/v1/search?limit=oops')[0], 400)
        self.assertEqual(self.request('/v1/search?q=a&q=b')[0], 400)

    def test_sort_through_browser_proxy(self):
        from arxiv_archive.search_indexer import Indexer
        self.store.catalog.upsert_paper('2609.00001', {'title': 'Zebra', 'published': '2026-09-01'}, 1)
        indexer = Indexer(self.store.catalog.path, self.store.work / 'search/search.sqlite')
        try:
            indexer.step()
        finally:
            indexer.close()
        for sort, first in [('title_asc', '1706.03762'), ('newest', '2609.00001')]:
            status, _, body = self.request('/v1/search?sort=' + sort)
            self.assertEqual(status, 200, body)
            self.assertEqual(json.loads(body)['items'][0]['id'], first)
        self.assertEqual(self.request('/v1/search?sort=invalid')[0], 400)
        self.assertEqual(self.request('/v1/search?sort=newest&sort=oldest')[0], 400)

    def test_snapshot_author_lists_and_ambiguous_names(self):
        from arxiv_archive.citations import raw_authors
        self.assertEqual(raw_authors("C. Bal\\'azs, E. L. Berger, C.-P. Yuan"),
                         'C. Balázs and E. L. Berger and C.-P. Yuan')
        self.assertEqual(raw_authors('Einstein, Albert'), '{Einstein, Albert}')

    def test_bibtex_escapes_untrusted_metadata(self):
        output = bibtex({'id': '1706.03762', 'metadata': {
            'title': 'Bad }\n@article{injection, & % title', 'authors': ['A', 'B']}})
        self.assertNotIn('\n@article', output)
        self.assertIn(r'\}', output)
        self.assertIn('A and B', output)


if __name__ == '__main__':
    unittest.main()
