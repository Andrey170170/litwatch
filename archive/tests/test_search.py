from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

from arxiv_archive.catalog import Catalog
from arxiv_archive.search import SearchIndex, normalize_paper
from arxiv_archive.search_indexer import Indexer


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.catalog = Catalog(self.root / 'catalog.sqlite')
        self.path = self.root / 'search/search.sqlite'
        self.indexer = None
        self.search = SearchIndex(self.path)

    def tearDown(self):
        if self.indexer:
            self.indexer.close()
        self.catalog.close()
        self.tmp.cleanup()

    def start(self):
        self.indexer = Indexer(self.catalog.path, self.path)
        return self.indexer

    def paper(self, n, title='Interpretability', **metadata):
        paper_id = f'2609.{n:05}'
        self.catalog.upsert_paper(paper_id, {'title': title, **metadata}, 1)
        return paper_id

    def test_baseline_restart_and_live_mutations(self):
        a = self.paper(1, 'Old title')
        b = self.paper(2, 'To delete')
        worker = self.start()
        self.assertFalse(worker.step(1)['baseline_complete'])
        worker.close()
        self.indexer = None
        self.catalog.upsert_paper(a, {'title': 'New title'}, 2)
        with self.catalog.db:
            self.catalog.db.execute('DELETE FROM papers WHERE id=?', (b,))
        c = self.paper(3, 'New arrival')
        worker = self.start()
        for _ in range(4):
            worker.step(1)
        self.assertEqual({p['id'] for p in self.search.search()['items']}, {a, c})
        self.assertEqual(self.search.search(q='New title')['items'][0]['latest_version'], 2)
        self.assertEqual(self.search.status()['indexed_papers'], 2)
        self.assertTrue(self.search.status()['baseline_complete'])
        self.assertEqual(self.catalog.db.execute('SELECT count(*) FROM search_dirty').fetchone()[0], 0)

    def test_concurrent_update_between_index_commit_and_ack_survives(self):
        worker = self.start()
        paper_id = self.paper(1, 'Before')
        original = worker._acknowledge
        def mutate_and_ack(rows):
            self.catalog.upsert_paper(paper_id, {'title': 'After'}, 2)
            original(rows)
        with patch.object(worker, '_acknowledge', side_effect=mutate_and_ack):
            worker.step()
        self.assertEqual(self.search.search(q='Before')['items'][0]['id'], paper_id)
        self.assertEqual(self.catalog.db.execute('SELECT count(*) FROM search_dirty').fetchone()[0], 1)
        worker.step()
        self.assertEqual(self.search.search(q='After')['items'][0]['id'], paper_id)
        self.assertFalse(self.search.search(q='Before')['items'])

    def test_crash_after_publish_replays_without_duplicate_or_counter_drift(self):
        worker = self.start()
        paper_id = self.paper(1)
        with patch.object(worker, '_acknowledge', side_effect=RuntimeError('simulated crash')):
            with self.assertRaisesRegex(RuntimeError, 'crash'):
                worker.step()
        worker.close()
        self.indexer = None
        worker = self.start()
        worker.step()
        self.assertEqual(self.search.status()['indexed_papers'], 1)
        with self.catalog.db:
            self.catalog.db.execute('DELETE FROM papers WHERE id=?', (paper_id,))
        worker.step()
        self.assertEqual(self.search.status()['indexed_papers'], 0)
        self.assertFalse(self.search.search(q='Interpretability')['items'])

    def test_availability_tracks_artifacts_and_historical_sources(self):
        paper_id = self.paper(1, categories='cs.AI cs.CL', first_version_date='2026-09-01T01:02:03')
        worker = self.start()
        worker.step()
        self.assertFalse(self.search.search(availability='any')['items'])
        with self.catalog.db:
            self.catalog.db.execute('INSERT INTO latex_locations VALUES(?,?,?)', (paper_id, 'shard', 0))
            self.catalog.db.execute('''INSERT INTO artifacts VALUES(?,1,'pdf','pdf/file',100,NULL,90,NULL,
                'zstd','application/pdf','{}','available',0)''', (paper_id,))
        worker.step()
        result = self.search.search(category='cs.CL', availability='both', date_from='2026-09-01', date_to='2026-09-01')
        self.assertEqual(result['items'][0]['id'], paper_id)
        self.assertFalse(self.search.search(category='cs.LG')['items'])
        with self.catalog.db:
            self.catalog.db.execute("UPDATE artifacts SET status='unavailable' WHERE paper_id=?", (paper_id,))
            self.catalog.db.execute('DELETE FROM latex_locations WHERE paper_id=?', (paper_id,))
        worker.step()
        self.assertEqual(self.search.search(availability='none')['items'][0]['id'], paper_id)

    def test_literal_search_weighting_paging_and_exact_id(self):
        self.paper(1, 'Neural circuits', abstract='A study', published='2026-09-01', authors=['Ada Lovelace'])
        self.paper(2, 'A study', abstract='Neural circuits', published='2026-09-02')
        self.paper(3, 'Neural circuits OR logic', published='2026-09-03')
        self.start().step()
        ranked = self.search.search(q='neural circuits')
        ids = [p['id'] for p in ranked['items']]
        self.assertLess(ids.index('2609.00001'), ids.index('2609.00002'))
        self.assertEqual([x['id'] for x in ranked['items']], [x['id'] for x in self.search.search(q='neural circuits')['items']])
        self.assertEqual(len(self.search.search(q='neural OR')['items']), 1)
        self.assertEqual(self.search.search(q='Ada')['items'][0]['id'], '2609.00001')
        self.assertEqual(self.search.search(q='arXiv:2609.00002v1')['items'][0]['id'], '2609.00002')
        first = self.search.search(limit=2)
        self.assertTrue(first['has_more'])
        self.assertEqual(first['next_offset'], 2)
        self.assertEqual(len(self.search.search(limit=2, offset=2)['items']), 1)
        plan = [r[3] for r in self.indexer.db.execute("EXPLAIN QUERY PLAN SELECT d.id FROM documents d JOIN documents_fts ON documents_fts.rowid=d.rowid WHERE documents_fts MATCH 'neural' ORDER BY documents_fts.rank LIMIT 20")]
        self.assertFalse(any('TEMP B-TREE' in line for line in plan), plan)

    def test_exact_title_then_phrase_then_title_over_abstract(self):
        # The live index's case (archive.md, Search): titles repeating the query's words outrank
        # the paper whose whole title it is, unless exact titles and phrases come first.
        for n, title, abstract in [
                (1, "Attention Is All You Need But You Don't Need All Of It", ''),
                (2, 'Not All Attention Is All You Need', ''),
                (3, 'Is attention all you need? You need all of it', ''),
                (4, 'Attention Is All You Need', 'The Transformer, attention only.'),
                (5, 'Attention Is All You Need In Speech Separation', ''),
                (6, 'Speech separation', 'We show attention is all you need here.'),
                (7, 'Neural circuits', ''),
                (8, 'A study', 'Neural circuits, circuits and more neural circuits.'),
                (9, 'Re-thinking pré-training', 'Rethinking.')]:
            self.paper(n, title, abstract=abstract, published=f'2026-09-{n:02}')
        self.start().step()
        ids = [p['id'][-1] for p in self.search.search(q='attention is all you need')['items']]
        # The exact title; titles holding the phrase; the phrase in an abstract; the words apart.
        self.assertEqual(ids[0], '4')
        self.assertEqual(sorted(ids[1:4]), ['1', '2', '5'])
        self.assertEqual(ids[4:], ['6', '3'])
        # Case, punctuation and diacritics do not keep an exact title from matching.
        self.assertEqual(self.search.search(q='ATTENTION is all-you-need')['items'][0]['id'][-1], '4')
        self.assertEqual(self.search.search(q='re thinking PRE-training')['items'][0]['id'][-1], '9')
        # Pages run on across the tiers; filters hold in every tier.
        paged = [p['id'][-1] for o in (0, 2, 4)
                 for p in self.search.search(q='attention is all you need', limit=2, offset=o)['items']]
        self.assertEqual(paged, ids)
        later = self.search.search(q='attention is all you need', date_from='2026-09-05')['items']
        self.assertEqual([p['id'][-1] for p in later], ['5', '6'])
        # Past EXACT_SCAN titles starting with the phrase, the exact title is matched by its
        # letters, case aside.
        with patch('arxiv_archive.search.EXACT_SCAN', 0):
            self.assertEqual(self.search.search(q='attention is all you need')['items'][0]['id'][-1], '4')
        # One word: the title weighs more than an abstract that repeats it.
        self.assertEqual(self.search.search(q='circuits')['items'][0]['id'][-1], '7')
        # Date orders stay available and take no tiers.
        newest = self.search.search(q='attention is all you need', sort='newest')['items']
        self.assertEqual([p['id'][-1] for p in newest], ['6', '5', '4', '3', '2', '1'])

    def test_past_exact_scan_an_exact_title_still_matches_by_its_words(self):
        # A title repeating the phrase outranks the exact one by BM25; the exact tier keeps it
        # first even when too many titles start with the phrase to read them all.
        self.paper(1, 'Deep learning, deep learning, deep learning', published='2026-09-02')
        self.paper(2, 'Deep Learning?', published='2026-09-01')
        self.start().step()
        for scan in (256, 0):
            with patch('arxiv_archive.search.EXACT_SCAN', scan):
                # As typed, or with the query's own punctuation aside.
                for q in ('Deep  learning', '"deep learning"'):
                    self.assertEqual([p['id'][-1] for p in self.search.search(q=q)['items']],
                                     ['2', '1'], q)

    def test_sorting_filters_and_page_boundaries(self):
        for n, title, published in [(1, 'beta', '2026-09-03'),
                                    (2, 'Alpha', '2026-09-02'),
                                    (3, 'alpha', '2026-09-02'),
                                    (4, 'Zebra', '2026-09-01')]:
            self.paper(n, title, published=published, abstract='shared term', categories='cs.CL')
        self.paper(5, 'Excluded', abstract='shared term', categories='cs.AI')
        self.start().step()
        expected = {'newest': [1, 2, 3, 4], 'oldest': [4, 3, 2, 1],
                    'title_asc': [2, 3, 1, 4], 'title_desc': [4, 1, 3, 2]}
        for sort, ids in expected.items():
            for q in ('', 'shared term'):
                with self.subTest(sort=sort, q=q):
                    options = dict(q=q, category='cs.CL', sort=sort, limit=2)
                    first = self.search.search(**options)
                    second = self.search.search(**options, offset=first['next_offset'])
                    actual = [p['id'] for p in first['items'] + second['items']]
                    self.assertEqual(actual, [f'2609.{n:05}' for n in ids])
                    self.assertIsNone(second['next_offset'])
        for sort in expected:
            self.assertEqual(self.search.search(q='2609.00001', sort=sort)['items'][0]['id'], '2609.00001')
        self.assertEqual(self.search.search(sort='title_asc', date_from='2026-09-03')['items'][0]['id'], '2609.00001')

    def test_broad_sorted_matches_use_complete_result_set(self):
        for n in range(1, 301):
            self.paper(n, f'Paper {n:03}', abstract='shared topic', published='2026-09-01',
                       categories='cs.CL' if n % 2 else 'cs.AI')
        self.start().step()
        for sort in ('title_desc', 'oldest'):
            result = self.search.search(q='shared topic', category='cs.CL', sort=sort, offset=20, limit=2)
            self.assertEqual([p['id'] for p in result['items']], ['2609.00259', '2609.00257'])

    def test_normalization_and_validation(self):
        paper = normalize_paper({'id': '2609.00001', 'latest_version': 2, 'metadata': {
            'title': ' A\n paper ', 'authors': [{'name': 'Ada'}, {'keyname': 'Turing', 'forenames': 'Alan'}],
            'categories': ['cs.CL', 'cs.AI', 'cs.CL'], 'doi': '10.1/example',
            'versions': [{'version': 'v1', 'created': 'Mon, 2 Apr 2007 19:18:42 GMT'}]}})
        self.assertEqual(paper['authors'], 'Ada, Alan Turing')
        self.assertEqual(paper['published'], '2007-04-02')
        self.assertEqual(paper['categories'], ['cs.AI', 'cs.CL'])
        for kwargs in [{'q': 'x' * 301}, {'q': 'x ' * 13}, {'q': '***'}, {'offset': 1001},
                       {'limit': 51}, {'limit': True}, {'date_from': '2026-02-30'},
                       {'date_from': '2026-09-02', 'date_to': '2026-09-01'},
                       {'category': 'cs.CL"'}, {'availability': 'bogus'},
                       {'sort': 'invalid'}, {'sort': None}, {'sort': 'title; DROP TABLE documents'}]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.search.search(**kwargs)

    def test_planner_statistics_only_after_baseline_and_once_across_restart(self):
        self.paper(1, categories='cs.CL')
        self.paper(2, categories='cs.LG')
        worker = self.start()
        trace = []
        worker.db.set_trace_callback(trace.append)
        self.assertFalse(worker.prepare_planner())
        worker.step(1)
        self.assertFalse(worker.prepare_planner())
        worker.step(1)
        self.assertTrue(worker.prepare_planner())
        analyzed = [sql for sql in trace if sql.startswith('ANALYZE')]
        self.assertEqual(analyzed, ['ANALYZE documents', 'ANALYZE categories'])
        self.assertTrue(self.search.status()['planner_ready'])
        self.assertFalse(worker.prepare_planner())
        worker.close()
        self.indexer = None
        worker = self.start()
        self.assertFalse(worker.prepare_planner())
        self.paper(3, 'Incremental after statistics')
        worker.step()
        self.assertEqual(self.search.search(q='Incremental')['items'][0]['id'], '2609.00003')
        self.assertEqual(self.search.status()['indexed_papers'], 3)

    def test_query_budget_and_single_writer(self):
        for n in range(1, 1001):
            self.paper(n)
        self.start().step(1000)
        with self.assertRaisesRegex(RuntimeError, 'already running'):
            Indexer(self.catalog.path, self.path)
        constrained = SearchIndex(self.path, query_timeout=-1)
        with self.assertRaisesRegex(RuntimeError, 'too broad'):
            constrained.search(q='Interpretability', limit=50)


if __name__ == '__main__':
    unittest.main()
