from pathlib import Path
import io
import json
import tarfile
import tempfile
import unittest
import requests
from unittest.mock import patch

from arxiv_archive.store import Store
from arxiv_archive.worker import ArxivClient, PermanentFailure, RateLimited, Retryable, Unavailable, Worker, artifact_url, worker_lock


class Response:
    def __init__(self, status=200, content=b'%PDF-1.7\nworker test', headers=None):
        self.status_code = status
        self.content = content
        self.headers = headers if headers is not None else {'Content-Length': str(len(content))}
        self.closed = False
    def iter_content(self, chunk_size):
        yield self.content[:4]
        yield self.content[4:]
    def close(self):
        self.closed = True


class Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.urls = []
    def get(self, url, **kwargs):
        self.urls.append(url)
        assert kwargs['allow_redirects'] is False
        return self.responses.pop(0)


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = Store(root / 'work', root / 'archive', guard=False)
        self.store.catalog.set_pdfs('all')  # these test acquisition, not the PDF policy
    def tearDown(self):
        self.store.close()
        self.temp.cleanup()
    def worker(self, *responses):
        session = Session(responses)
        return Worker(self.store, ArxivClient(self.store.work / 'catalog', session=session)), session

    def test_priority_download_and_decompressed_read(self):
        background = self.store.catalog.enqueue('2607.02101', 1, 'pdf', 100)
        urgent = self.store.catalog.enqueue('2607.02102', 2, 'pdf', 0)
        worker, session = self.worker(Response())
        result = worker.run_once()
        self.assertEqual(result['id'], urgent['id'])
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(self.store.get('2607.02102').read_bytes(), b'%PDF-1.7\nworker test')
        self.assertEqual(self.store.catalog.get_job(background['id'])['status'], 'pending')
        self.assertEqual(session.urls, ['https://arxiv.org/pdf/2607.02102v2'])

    def test_recovery_uses_dedicated_export_host(self):
        self.store.catalog.enqueue('2608.10001', 1, 'pdf', 200,
                                   provenance={'origin':'recovery'})
        worker, session = self.worker(Response())
        self.assertEqual(worker.run_once()['status'], 'complete')
        self.assertEqual(session.urls, ['https://export.arxiv.org/pdf/2608.10001v1'])

    def test_export_406_falls_back_once_and_preserves_version_and_pacing(self):
        self.store.catalog.enqueue('2608.10001', 2, 'pdf', 200, provenance={'origin': 'recovery'})
        rejected = Response(406, content=b'', headers={'Retry-After': '10'})
        worker, session = self.worker(rejected, Response())
        with patch('arxiv_archive.worker.time.time', return_value=1000), patch.object(worker.client.stop, 'wait', return_value=False) as wait:
            result = worker.run_once()
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['attempts'], 1)
        self.assertEqual(session.urls, ['https://export.arxiv.org/pdf/2608.10001v2',
                                        'https://arxiv.org/pdf/2608.10001v2'])
        self.assertTrue(rejected.closed)
        wait.assert_called_once_with(10)
        self.assertEqual(self.store.get('2608.10001').read_bytes(), b'%PDF-1.7\nworker test')

    def test_export_truncated_source_restarts_cleanly_on_reader(self):
        class Truncated(Response):
            def iter_content(self, chunk_size):
                yield b'partial broken source'
                raise requests.exceptions.ChunkedEncodingError('incomplete')
        source = b'\\documentclass{article}\n\\begin{document}ok\\end{document}'
        self.store.catalog.enqueue('2608.10001', 1, 'source', 200, provenance={'origin': 'recovery'})
        broken = Truncated(headers={})
        worker, session = self.worker(broken, Response(content=source))
        with patch.object(worker.client.stop, 'wait', return_value=False):
            result = worker.run_once()
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(self.store.source_file('2608.10001', 'main.tex').read_bytes(), source)
        self.assertTrue(broken.closed)
        self.assertEqual(len(session.urls), 2)

    def test_no_fallback_on_access_rate_limits_missing_or_metadata(self):
        for code in (403, 429, 503, 404, 410):
            with self.subTest(code=code):
                (self.store.work / 'catalog/arxiv-rate.json').unlink(missing_ok=True)
                worker, session = self.worker(Response(code))
                with self.assertRaises((Retryable, Unavailable)):
                    worker.client.fetch('https://export.arxiv.org/pdf/2608.10001v1', self.store.scratch / 'test', 100)
                self.assertEqual(len(session.urls), 1)
        (self.store.work / 'catalog/arxiv-rate.json').unlink(missing_ok=True)
        worker, session = self.worker(Response(406))
        with self.assertRaises((Retryable, Unavailable)):
            worker.client.fetch('https://export.arxiv.org/api/query?id_list=2608.10001', self.store.scratch / 'test', 100)
        self.assertEqual(len(session.urls), 1)

    def test_reader_406_is_retryable_with_global_cooldown_and_bounded_attempts(self):
        self.store.catalog.enqueue('2608.10001', 1, 'pdf', 200, provenance={'origin': 'recovery'})
        worker, session = self.worker(Response(406), Response(406))
        with patch('arxiv_archive.worker.time.time', return_value=1000), patch.object(worker.client.stop, 'wait', return_value=False):
            result = worker.run_once()
        self.assertEqual(result['status'], 'pending')
        self.assertEqual(result['attempts'], 1)
        self.assertGreaterEqual(result['not_before'], 1060)
        self.assertGreaterEqual(worker.client._read_cooldown(), 1060)
        self.assertEqual(len(session.urls), 2)
        with self.store.catalog.db:
            self.store.catalog.db.execute('UPDATE jobs SET attempts=7,not_before=0')
        (self.store.work / 'catalog/arxiv-rate.json').unlink()
        worker, session = self.worker(Response(406), Response(406))
        with patch.object(worker.client.stop, 'wait', return_value=False):
            self.assertEqual(worker.run_once()['status'], 'failed')
        self.assertEqual(len(session.urls), 2)

    def test_empty_file_does_not_cool_down_unrelated_downloads(self):
        self.store.catalog.enqueue('2608.10001', 1, 'pdf', 200, provenance={'origin': 'recovery'})
        worker, session = self.worker(Response(content=b''), Response(content=b''), Response())
        with patch('arxiv_archive.worker.time.time', return_value=1000), patch.object(worker.client.stop, 'wait', return_value=False):
            result = worker.run_once()
        self.assertEqual(result['status'], 'pending')
        self.assertGreaterEqual(result['not_before'], 1030)
        self.assertLess(worker.client._read_cooldown(), 1030)
        with patch('arxiv_archive.worker.time.time', return_value=1004):
            self.store.catalog.enqueue('2608.10002', 1, 'pdf', 100)
            self.assertEqual(worker.run_once()['status'], 'complete')
        self.assertEqual(len(session.urls), 3)

    def test_source_package_write_and_read(self):
        content = b'\\documentclass{article}\n\\begin{document}source\\end{document}'
        self.store.catalog.enqueue('hep-th/9901001', 2, 'source')
        worker, session = self.worker(Response(content=content))
        result = worker.run_once()
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(session.urls, ['https://arxiv.org/src/hep-th/9901001v2'])
        self.assertEqual(self.store.source_file('hep-th/9901001', 'main.tex').read_bytes(), content)

    def test_source_without_text_is_unavailable(self):
        package = io.BytesIO()
        with tarfile.open(fileobj=package, mode='w:gz') as tar:
            figure = tarfile.TarInfo('fig.png'); figure.size = 4
            tar.addfile(figure, io.BytesIO(b'\x89PNG'))
        self.store.catalog.enqueue('hep-th/9901001', 2, 'source')
        worker, _ = self.worker(Response(content=package.getvalue()))
        result = worker.run_once()
        self.assertEqual(result['status'], 'unavailable')
        self.assertIn('no text members', result['error'])

    def test_retry_after_persists_and_cooldown_does_not_consume_attempts(self):
        self.store.catalog.enqueue('2607.02101', 1, 'pdf')
        worker, session = self.worker(Response(429, headers={'Retry-After': '120'}))
        with patch('arxiv_archive.worker.time.time', return_value=1000):
            # Catalog imports same time module, giving the job a coherent clock.
            result = worker.run_once()
            self.assertEqual(result['status'], 'pending')
            self.assertGreaterEqual(result['not_before'], 1120)
            state = json.loads((self.store.work / 'catalog/arxiv-rate.json').read_text())
            self.assertGreaterEqual(state['not_before'], 1120)
            self.store.catalog.enqueue('2607.02102', 1, 'pdf', 0)
            second = worker.run_once()
            self.assertEqual(second['attempts'], 0)
            self.assertEqual(second['status'], 'pending')
        self.assertEqual(len(session.urls), 1)

    def test_unavailable_and_corrupt_pdf_are_distinct(self):
        self.store.catalog.enqueue('2607.02101', 1, 'pdf')
        worker, _ = self.worker(Response(404))
        self.assertEqual(worker.run_once()['status'], 'unavailable')
        (self.store.work / 'catalog/arxiv-rate.json').unlink()
        self.store.catalog.enqueue('2607.02102', 1, 'pdf')
        worker, _ = self.worker(Response(content=b'<html>bad response</html>'))
        self.assertEqual(worker.run_once()['status'], 'failed')
        self.assertIsNone(self.store.catalog.get_artifact('2607.02102', 'pdf'))

    def test_redirect_host_and_stream_byte_bound(self):
        client = ArxivClient(self.store.work / 'catalog', session=Session([
            Response(302, headers={'Location': 'https://example.org/stolen'})]))
        dest = self.store.scratch / 'test'
        with self.assertRaises(PermanentFailure):
            client.fetch(artifact_url('2607.02101', 1, 'pdf'), dest, 100)
        self.assertFalse(dest.exists())
        (self.store.work / 'catalog/arxiv-rate.json').unlink()
        client.session = Session([Response(content=b'x' * 40, headers={})])
        with self.assertRaises(PermanentFailure):
            client.fetch(artifact_url('2607.02101', 1, 'pdf'), dest, 10)
        self.assertFalse(dest.exists())

    def test_retry_budget_is_bounded(self):
        self.store.catalog.enqueue('2607.02101', 1, 'pdf')
        worker, _ = self.worker(Response(503, headers={'Retry-After': '10'}))
        worker.max_attempts = 1
        result = worker.run_once()
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['attempts'], 1)
        self.assertIn('503', result['error'])

    def test_worker_lock_lease_renewal_and_explicit_retry(self):
        with worker_lock(self.store.work / 'catalog/acquisition-worker.lock'):
            with self.assertRaises(RuntimeError):
                with worker_lock(self.store.work / 'catalog/acquisition-worker.lock'):
                    self.fail('duplicate worker entered')
        job = self.store.catalog.enqueue('2607.02101', 1, 'pdf')
        claimed = self.store.catalog.claim_job()
        self.assertFalse(self.store.catalog.renew_job(job['id'], 'wrong'))
        self.assertTrue(self.store.catalog.renew_job(job['id'], claimed['lease_token']))
        self.store.catalog.finish_job(job['id'], claimed['lease_token'], 'failed')
        retried = self.store.catalog.retry_job(job['id'])
        self.assertEqual(retried['status'], 'pending')
        self.assertEqual(retried['attempts'], 0)


if __name__ == '__main__':
    unittest.main()
