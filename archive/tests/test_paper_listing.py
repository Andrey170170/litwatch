"""Listing papers by the date window they were submitted in."""
from pathlib import Path
import tempfile
import unittest

from arxiv_archive.catalog import Catalog

# The three forms metadata arrives in: the HF snapshot, the archive's own arXivRaw
# discovery, and litwatch's registration of a paper it took in.
HF = {'title': 'Attention  is all you need', 'abstract': 'Transformers.',
      'authors': 'Ashish Vaswani, Noam Shazeer', 'categories': 'cs.CL cs.LG',
      'authors_parsed': [['Vaswani', 'Ashish', ''], ['Shazeer', 'Noam', '']],
      'versions': [{'version': 'v1', 'created': 'Thu, 1 Oct 2026 17:57:34 GMT'},
                   {'version': 'v2', 'created': 'Mon, 5 Oct 2026 09:00:00 GMT'}],
      'update_date': '2026-10-05', 'journal-ref': 'NeurIPS'}
DISCOVERY = {'title': 'Raw', 'abstract': 'From arXivRaw.', 'authors': ['A. One, B. Two and C. Three'],
             'authors_raw': 'A. One, B. Two and C. Three', 'categories': ['cs.AI'],
             'published': '2026-10-02T23:30:00+00:00', 'updated': '2026-10-02T23:30:00+00:00',
             'version_history': [{'version': 1, 'date': '2026-10-02T23:30:00+00:00', 'size': '1kb'}]}
LITWATCH = {'id': '2610.00003', 'title': 'Taken in', 'abstract': 'By litwatch.',
            'authors': ['Alice Smith'], 'categories': ['stat.ML', 'cs.LG'], 'primary_category': 'stat.ML',
            'published': '2026-10-03T00:00:00+00:00', 'updated': '2026-10-04T00:00:00+00:00'}


class ListingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Catalog(Path(self.temp.name) / 'catalog.sqlite')

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def every(self, since, until, categories=None, limit=500, scan=20000):
        """Follow `next` to the end, as a client does."""
        items, after, pages = [], None, 0
        while True:
            page = self.db.list_papers(since, until, categories, after, limit, scan)
            items, after, pages = items + page['items'], page['next'], pages + 1
            if after is None:
                return items, pages

    def test_one_shape_from_every_metadata_form(self):
        self.db.upsert_paper('2610.00001', HF, 2)
        self.db.upsert_paper('2610.00002', DISCOVERY, 1)
        self.db.upsert_paper('2610.00003', LITWATCH)
        items, _ = self.every('2026-10-01', '2026-10-03')
        self.assertEqual([i['id'] for i in items], ['2610.00001', '2610.00002', '2610.00003'])
        hf, raw, taken = items
        self.assertEqual(hf['title'], 'Attention is all you need')
        self.assertEqual(hf['authors'], ['Ashish Vaswani', 'Noam Shazeer'])
        self.assertEqual(hf['categories'], ['cs.CL', 'cs.LG'])
        self.assertEqual((hf['published'], hf['updated'], hf['version']), ('2026-10-01', '2026-10-05', 2))
        self.assertEqual(hf['version_history'][1], {'version': 2, 'date': '2026-10-05T09:00:00+00:00'})
        self.assertEqual(hf['journal_ref'], 'NeurIPS')
        self.assertEqual(raw['authors'], ['A. One', 'B. Two', 'C. Three'])
        self.assertEqual((raw['published'], raw['version']), ('2026-10-02', 1))
        self.assertEqual(raw['version_history'][0]['size'], '1kb')
        self.assertEqual(taken['primary_category'], 'stat.ML')
        self.assertEqual((taken['published'], taken['updated'], taken['version']),
                         ('2026-10-03', '2026-10-04', None))
        self.assertIsNone(taken['doi'])

    def test_window_and_categories(self):
        self.db.upsert_paper('2610.00001', HF, 2)
        self.db.upsert_paper('2610.00002', DISCOVERY, 1)
        self.db.upsert_paper('2610.00003', LITWATCH)
        self.db.upsert_paper('2610.00004', {'title': 'Undated'})  # nothing says when: never listed
        ids = lambda *args: [i['id'] for i in self.every(*args)[0]]
        self.assertEqual(ids('2026-10-02', '2026-10-02'), ['2610.00002'])
        self.assertEqual(ids('2026-10-01', '2026-10-31', {'cs.LG'}), ['2610.00001', '2610.00003'])
        self.assertEqual(ids('2026-10-01', '2026-10-31', {'cs.AI', 'cs.CL'}), ['2610.00001', '2610.00002'])
        self.assertEqual(ids('2026-09-01', '2026-09-30'), [])
        # A revision keeps the paper on the day it was first submitted.
        self.db.upsert_paper('2610.00002', {**DISCOVERY, 'updated': '2026-10-20T00:00:00+00:00'}, 2)
        self.assertEqual(ids('2026-10-02', '2026-10-02'), ['2610.00002'])

    def test_pages_continue_from_their_cursor_without_loss(self):
        for n in range(1, 30):
            day = f'2026-10-{1 + n % 5:02d}'
            category = 'cs.CL' if n % 3 else 'cs.AI'
            self.db.upsert_paper(f'2610.{n:05d}', {'title': str(n), 'published': day, 'categories': [category]})
        self.db.upsert_paper('hep-th/9901001', {'title': 'old style', 'published': '2026-10-03',
                                                'categories': ['cs.CL']})
        everything, _ = self.every('2026-10-01', '2026-10-31')
        keys = [(i['published'], i['id']) for i in everything]
        self.assertEqual(keys, sorted(keys))
        self.assertEqual(len(everything), 30)
        paged, pages = self.every('2026-10-01', '2026-10-31', limit=4)
        self.assertEqual(paged, everything)
        self.assertEqual(pages, 8)
        # A sparse category over a small scan gives short pages; none is lost.
        sparse, pages = self.every('2026-10-01', '2026-10-31', {'cs.AI'}, limit=50, scan=3)
        self.assertEqual(sparse, [i for i in everything if i['categories'] == ['cs.AI']])
        self.assertGreater(pages, 3)

    def test_a_catalog_dated_before_the_index_lists_once_indexed(self):
        self.assertTrue(self.db.dates_indexed())  # an empty catalog is complete
        self.db.upsert_paper('2610.00002', DISCOVERY, 1)
        with self.db.lock, self.db.db:
            self.db.db.execute('DELETE FROM paper_dates')
            self.db.db.execute('DELETE FROM import_checkpoints')
        self.db.close()
        self.db = Catalog(Path(self.temp.name) / 'catalog.sqlite')
        self.assertFalse(self.db.dates_indexed())
        self.assertEqual(self.db.index_dates(batch=1), {'papers': 1, 'dated': 1})
        self.assertTrue(self.db.dates_indexed())
        self.assertEqual([i['id'] for i in self.every('2026-10-01', '2026-10-31')[0]], ['2610.00002'])


if __name__ == '__main__':
    unittest.main()
