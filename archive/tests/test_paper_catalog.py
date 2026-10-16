import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from arxiv_archive.catalog import Catalog


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = Catalog(self.root / 'catalog.sqlite')

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def test_new_catalog_holds_background_pdfs_until_asked_above_background(self):
        self.assertEqual(self.db.pdfs(), 'wanted')
        self.db.upsert_paper('2607.02101', {'title': 'x'}, 1)
        self.assertEqual(self.db.enqueue('2607.02101', 1, 'source')['status'], 'pending')
        self.assertEqual(self.db.enqueue('2607.02101', 1, 'pdf')['status'], 'held')
        self.assertEqual(self.db.claim_job()['kind'], 'source')
        self.assertIsNone(self.db.claim_job())  # a held job is never claimed
        self.assertEqual(self.db.enqueue('2607.02101', 1, 'pdf', priority=100)['status'], 'held')
        wanted = self.db.enqueue('2607.02101', 1, 'pdf', priority=50)
        self.assertEqual((wanted['status'], wanted['priority']), ('pending', 50))
        self.assertEqual(self.db.claim_job()['kind'], 'pdf')
        self.assertEqual(self.db.status()['pdfs'], 'wanted')

    def test_changing_the_pdf_policy_brings_the_queue_in_line(self):
        self.db.upsert_paper('2607.02101', {'title': 'x'}, 1)
        self.db.upsert_paper('2607.02102', {'title': 'y'}, 1)
        self.db.enqueue('2607.02101', 1, 'pdf')
        self.assertEqual(self.db.set_pdfs('all'), {'pdfs': 'all', 'jobs_changed': 1})
        self.db.enqueue('2607.02102', 1, 'pdf', priority=0)
        self.db.enqueue('2607.02102', 1, 'source')
        self.assertEqual(self.db.enqueue('2607.02101', 1, 'pdf')['status'], 'pending')
        # Only background PDF jobs are held: the urgent one and the source stay pending.
        self.assertEqual(self.db.set_pdfs('wanted'), {'pdfs': 'wanted', 'jobs_changed': 1})
        statuses = dict(((r['paper_id'], r['kind']), r['status']) for r in self.db.db.execute(
            'SELECT paper_id,kind,status FROM jobs'))
        self.assertEqual(statuses, {('2607.02101', 'pdf'): 'held', ('2607.02102', 'pdf'): 'pending',
                                    ('2607.02102', 'source'): 'pending'})
        with self.assertRaises(ValueError):
            self.db.set_pdfs('some')

    def test_a_background_pdf_in_flight_when_wanted_is_set_is_held_not_claimed_again(self):
        self.db.set_pdfs('all')
        self.db.upsert_paper('2607.02101', {'title': 'x'}, 1)
        self.db.upsert_paper('2607.02102', {'title': 'y'}, 1)
        self.db.enqueue('2607.02101', 1, 'pdf')
        self.db.enqueue('2607.02102', 1, 'pdf')
        retried = self.db.claim_job()
        self.db.claim_job(lease_seconds=0.001)
        self.db.set_pdfs('wanted')
        # One comes back for a retry, the other's lease runs out: neither is claimed again.
        self.assertTrue(self.db.finish_job(retried['id'], retried['lease_token'], 'pending'))
        with patch('arxiv_archive.catalog.time.time', return_value=time.time() + 1):
            self.assertIsNone(self.db.claim_job())
        self.assertEqual([r[0] for r in self.db.db.execute('SELECT status FROM jobs')], ['held', 'held'])

    def test_a_catalog_with_papers_before_the_setting_keeps_every_pdf(self):
        path = self.root / 'old.sqlite'
        old = sqlite3.connect(path)
        old.executescript("CREATE TABLE papers (id TEXT PRIMARY KEY, metadata_json TEXT NOT NULL "
                          "DEFAULT '{}', latest_version INTEGER, updated_at REAL NOT NULL);"
                          "INSERT INTO papers VALUES ('2607.02101','{}',1,0);")
        old.close()
        catalog = Catalog(path)
        try:
            self.assertEqual(catalog.pdfs(), 'all')
            self.assertEqual(catalog.enqueue('2607.02101', 1, 'pdf')['status'], 'pending')
        finally:
            catalog.close()

    def test_metadata_revisions_do_not_regress(self):
        self.db.upsert_paper('2607.02101', {'title': 'new'}, 3)
        paper = self.db.upsert_paper('2607.02101', {'title': 'old', 'doi': 'x'}, 1)
        self.assertEqual(paper['latest_version'], 3)
        self.assertEqual(paper['metadata'], {'title': 'new', 'doi': 'x'})
        self.db.upsert_paper('hep-th/9901001', {'title': 'legacy'})
        with self.assertRaises(ValueError):
            self.db.upsert_paper('../not-an-id', {})

    def test_same_revision_metadata_dates_are_monotonic(self):
        self.db.upsert_paper('2607.02101', {
            'title': 'current', 'updated': '2026-09-28T12:00:00Z', 'doi': 'new-doi'}, 2)
        paper = self.db.upsert_paper('2607.02101', {
            'title': 'stale', 'updated': '2026-09-28T12:30:00+02:00',
            'doi': 'old-doi', 'license': 'previously-unobserved'}, 2)
        self.assertEqual(paper['latest_version'], 2)
        self.assertEqual(paper['metadata']['title'], 'current')
        self.assertEqual(paper['metadata']['doi'], 'new-doi')
        self.assertEqual(paper['metadata']['updated'], '2026-09-28T12:00:00Z')
        self.assertEqual(paper['metadata']['license'], 'previously-unobserved')

    def test_unknown_revision_metadata_dates_and_historical_dates_are_monotonic(self):
        self.db.upsert_paper('2607.02101', {'title': 'current', 'update_date': '2026-09-28'})
        paper = self.db.upsert_paper('2607.02101',
                                    {'title': 'stale', 'updated': '2026-09-27T23:00:00Z'})
        self.assertEqual(paper['metadata']['title'], 'current')
        self.assertNotIn('updated', paper['metadata'])
        self.assertIsNone(paper['latest_version'])
        newer = self.db.upsert_paper('2607.02101',
                                    {'title': 'newer', 'updated': '2026-09-29T01:00:00Z'})
        self.assertEqual(newer['metadata']['title'], 'newer')

    def test_minimal_feed_cannot_erase_canonical_fields(self):
        canonical = {'title': 'Canonical', 'updated': '2026-09-28T12:00:00Z',
                     'authors': ['Alice Smith', 'Bob Jones'], 'doi': '10.1234/x',
                     'version_history': [{'version': 2, 'date': '2026-09-28T12:00:00Z'}]}
        self.db.upsert_paper('2607.02101', canonical, 2)
        paper = self.db.upsert_paper('2607.02101',
                                    {'title': 'Feed', 'authors': [], 'updated': None,
                                     'doi': None, 'version_history': []}, 2)
        self.assertEqual(paper['metadata'], canonical)
        self.db.upsert_paper('2607.02102', {'title': 'Untimed', 'authors': ['Alice']}, 1)
        paper = self.db.upsert_paper('2607.02102', {'authors': [], 'title': ''}, 1)
        self.assertEqual(paper['metadata'], {'title': 'Untimed', 'authors': ['Alice']})

    def test_delayed_metadata_preserves_revision_history_and_artifact_inventory(self):
        self.db.upsert_paper('2607.02101', {
            'title': 'current', 'updated': '2026-09-28T12:00:00Z',
            'version_history': [{'version': 2, 'date': '2026-09-28'}]}, 2)
        artifact = dict(paper_id='2607.02101', version=2, kind='pdf',
                        location='pdf/current.zst', original_size=10, stored_size=12,
                        compression='zstd', media_type='application/pdf')
        self.db.put_artifact(artifact)
        paper = self.db.upsert_paper('2607.02101', {
            'title': 'stale', 'updated': '2026-09-27T12:00:00Z',
            'version_history': [{'version': 1, 'date': '2026-09-01'}]})
        self.assertEqual(paper['latest_version'], 2)
        self.assertEqual([r['version'] for r in paper['metadata']['version_history']], [1, 2])
        self.assertEqual(self.db.get_artifact('2607.02101', 'pdf')['location'], 'pdf/current.zst')

    def test_jobs_deduplicate_promote_and_survive_restart(self):
        low = self.db.enqueue('2607.02101', 1, 'pdf', provenance={'reason': 'archive'})
        self.db.enqueue('2607.02102', 1, 'pdf', priority=50)
        promoted = self.db.enqueue('2607.02101', 1, 'pdf', priority=0)
        self.assertEqual(low['id'], promoted['id'])
        self.db.close()
        self.db = Catalog(self.root / 'catalog.sqlite')
        claimed = self.db.claim_job()
        self.assertEqual(claimed['id'], low['id'])
        self.assertEqual(claimed['priority'], 0)
        self.assertTrue(self.db.finish_job(claimed['id'], claimed['lease_token']))
        self.assertFalse(self.db.finish_job(claimed['id'], claimed['lease_token'], status='failed'))

    def test_expired_lease_recovery_and_retry_delay(self):
        with patch('arxiv_archive.catalog.time.time', return_value=1000):
            job = self.db.enqueue('2607.02101', 1, 'source')
            first = self.db.claim_job(lease_seconds=5)
        with patch('arxiv_archive.catalog.time.time', return_value=1006):
            second = self.db.claim_job(lease_seconds=5)
            self.assertEqual(second['attempts'], 2)
            self.assertFalse(self.db.finish_job(job['id'], first['lease_token']))
            self.assertTrue(self.db.finish_job(job['id'], second['lease_token'], 'pending', 'busy', 20))
            self.assertIsNone(self.db.claim_job())
        with patch('arxiv_archive.catalog.time.time', return_value=1027):
            self.assertIsNotNone(self.db.claim_job())

    def test_available_artifact_skips_acquisition(self):
        self.db.put_artifact(dict(paper_id='2607.02101', version=2, kind='source',
            location='sources/x.zst', original_size=10, stored_size=12,
            compression='zstd', media_type='application/gzip', provenance={'reason': 'demo'}))
        self.assertEqual(self.db.get_paper('2607.02101')['latest_version'], 2)
        self.db.upsert_paper('2607.02101', {'title': 'kept'}, 3)
        self.db.put_artifact(dict(paper_id='2607.02101', version=0, kind='source',
            location='sources/historical.zst', original_size=5, stored_size=7,
            compression='zstd', media_type='text/x-tex'))
        self.assertEqual(self.db.get_paper('2607.02101')['latest_version'], 3)
        self.assertEqual(self.db.get_paper('2607.02101')['metadata']['title'], 'kept')
        job = self.db.enqueue('2607.02101', 2, 'source')
        self.assertEqual(job['status'], 'complete')
        self.assertIsNone(self.db.claim_job())
        self.assertEqual(self.db.get_artifact('2607.02101', 'source')['provenance']['reason'], 'demo')
        self.assertIsNone(self.db.get_artifact('2607.02101', 'pdf'))

    def test_consistent_backup_restores_wal_state(self):
        self.db.upsert_paper('2607.02101', {'title': 'backup'}, 1)
        self.db.enqueue('2607.02101', 1, 'source')
        target = self.root / 'backup.sqlite'
        self.db.backup(target)
        restored = Catalog(target)
        try:
            self.assertEqual(restored.get_paper('2607.02101')['metadata']['title'], 'backup')
            self.assertEqual(restored.status()['jobs']['pending'], 1)
            self.assertEqual(restored.db.execute('pragma integrity_check').fetchone()[0], 'ok')
        finally:
            restored.close()

    def test_historical_import_is_restart_safe_and_reads_only_latex_ids(self):
        import pyarrow as pa
        import pyarrow.parquet as pq
        for sub in ['metadata', 'latex']:
            (self.root / 'hf' / sub).mkdir(parents=True)
        pq.write_table(pa.table({'paper_id': ['2607.02101', '2607.02102'],
                                'title': ['historical', 'metadata only'], 'n_versions': [1, 2]}),
                       self.root / 'hf/metadata/part.parquet', row_group_size=1)
        pq.write_table(pa.table({'paper_id': ['2607.02101'] * 3,
                                'content': [b'huge omitted content'] * 3}),
                       self.root / 'hf/latex/part.parquet', row_group_size=2)
        ledger_path = self.root / 'ledger.sqlite'
        with sqlite3.connect(ledger_path) as old:
            old.execute('''CREATE TABLE artifacts(dest,shard,paper_id,version,original_path,
                original_size,original_sha256,stored_size,stored_sha256,compression,completed)''')
            old.execute('INSERT INTO artifacts VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                ('pdf/test.zst', 'pdf/part.parquet', '2607.02101', 1, 'original.pdf', 10, 'a', 8, 'b', 'zstd:9', 100))
        original_read = pq.ParquetFile.read_row_group
        calls = []
        def read(instance, i, columns=None, **kw):
            if 'content' in instance.schema_arrow.names:
                calls.append(columns)
            return original_read(instance, i, columns=columns, **kw)
        with patch.object(pq.ParquetFile, 'read_row_group', read):
            self.db.import_history(self.root, ledger_path)
        self.assertEqual(calls, [['paper_id'], ['paper_id']])
        self.assertEqual(len(self.db.latex_locations('2607.02101')), 2)
        self.assertEqual(self.db.get_artifact('2607.02101', 'pdf')['original_sha256'], 'a')
        self.assertEqual(self.db.list_artifacts('2607.02102'), [])
        self.db.upsert_paper('2607.02101', {'title': 'live update'}, 3)
        before = self.db.status()
        self.db.import_history(self.root, ledger_path)
        self.assertEqual(before, self.db.status())
        self.assertEqual(self.db.get_paper('2607.02101')['metadata']['title'], 'live update')


if __name__ == '__main__':
    unittest.main()
