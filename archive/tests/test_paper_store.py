import gzip
import hashlib
import io
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from arxiv_archive.source_text import NoTextSource
from arxiv_archive.store import Store

PDF = b'%PDF-1.4\nfixture original bytes\n%%EOF\n'


def tar_bytes(members, compress=True, mtime=1_700_000_000):
    """A source package from (name, bytes) pairs; a None body adds a symlink."""
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode='w:gz' if compress else 'w') as tar:
        for name, body in members:
            entry = tarfile.TarInfo(name)
            entry.mtime = mtime
            if body is None:
                entry.type, entry.linkname = tarfile.SYMTYPE, 'main.tex'
                tar.addfile(entry)
            else:
                entry.size = len(body)
                tar.addfile(entry, io.BytesIO(body))
    return out.getvalue()


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / 'work').mkdir()
        (self.root / 'archive').mkdir()
        self.store = Store(self.root/'work', self.root/'archive', guard=False)
        self.input = self.root/'input'

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_one_publication_check_and_idempotent_roundtrip(self):
        self.input.write_bytes(PDF)
        with patch.object(self.store, '_verify_roundtrip', wraps=self.store._verify_roundtrip) as check:
            result = self.store.put('2609.12345', 1, 'pdf', self.input)
            self.assertEqual(check.call_count, 1)
            self.assertEqual(self.store.get('2609.12345').read_bytes(), PDF)
            self.assertEqual(self.store.put('2609.12345', 1, 'pdf', self.input), result)
            self.assertEqual(check.call_count, 1)
        self.input.write_bytes(PDF + b'changed')
        with self.assertRaises(FileExistsError):
            self.store.put('2609.12345', 1, 'pdf', self.input)

    def test_corrupt_write_is_not_published(self):
        self.input.write_bytes(PDF)
        with patch.object(self.store, '_verify_roundtrip', side_effect=ValueError('corrupt')):
            with self.assertRaises(ValueError):
                self.store.put('2609.12345', 1, 'pdf', self.input)
        self.assertIsNone(self.store.catalog.get_artifact('2609.12345', 'pdf'))
        self.assertFalse(list(self.store.archive.rglob('*.part')))
        self.assertFalse(list(self.store.archive.rglob('*.zst')))

    def test_recover_rename_before_catalog_commit(self):
        self.input.write_bytes(PDF)
        with patch.object(self.store.catalog, 'put_artifact', side_effect=RuntimeError('crash')):
            with self.assertRaises(RuntimeError):
                self.store.put('2609.12345', 1, 'pdf', self.input)
        self.assertEqual(len(list(self.store.archive.rglob('*.zst'))), 1)
        self.store.put('2609.12345', 1, 'pdf', self.input)
        self.assertEqual(self.store.get('2609.12345').read_bytes(), PDF)

    def test_source_updates_and_safe_member_read(self):
        self.input.write_bytes(gzip.compress(b'\\documentclass{article}\nversion one'))
        self.store.put('2609.12345', 1, 'source', self.input)
        data = b'\\documentclass{article}\nversion two'
        package = tar_bytes([('nested/main.tex', data), ('figs/plot.png', b'\x89PNG' + b'0' * 5000)])
        self.input.write_bytes(package)
        artifact = self.store.put('2609.12345', 2, 'source', self.input)
        self.assertEqual(artifact['media_type'], 'application/x-tar')
        self.assertEqual(artifact['provenance']['upstream_sha256'], hashlib.sha256(package).hexdigest())
        self.assertEqual((artifact['provenance']['filter'], artifact['provenance']['dropped_members']),
                         ('text-v1', 1))
        with tarfile.open(self.store.get('2609.12345', 'source')) as tar:
            self.assertEqual(tar.getnames(), ['nested/main.tex'])
        self.assertEqual(self.store.sources('2609.12345'), [{'path':'nested/main.tex','size':len(data)}])
        self.assertEqual(self.store.source_file('2609.12345', 'nested/main.tex').read_bytes(), data)
        self.assertIn(b'version one', self.store.source_file('2609.12345','main.tex',1).read_bytes())
        with self.assertRaises(ValueError):
            self.store.source_file('2609.12345', '../secret')

    def test_text_only_sources_are_deterministic_and_keep_only_text(self):
        text = [('main.tex', b'\\input{sec}'), ('sec.tex', b'body'), ('refs.bib', b'@a{}'),
                ('00README.XXX', b'nostamp'), ('README-build.notes.txt', b'make'),
                ('fig.pgf', b'figure code')]
        first = tar_bytes(text + [('fig.pdf', b'%PDF-1.5 figure')], mtime=1)
        second = tar_bytes(list(reversed(text)), compress=False, mtime=2)
        self.input.write_bytes(first)
        stored = self.store.put('2609.12345', 1, 'source', self.input)
        self.assertEqual(self.store.put('2609.12345', 1, 'source', self.input), stored)
        self.input.write_bytes(second)
        # Same text in another order, wrapper and timestamps converges on the same bytes.
        self.assertEqual(self.store.put('2609.12345', 1, 'source', self.input)['original_sha256'],
                         stored['original_sha256'])
        self.assertEqual([e['path'] for e in self.store.sources('2609.12345')],
                         ['00README.XXX', 'README-build.notes.txt', 'main.tex', 'refs.bib', 'sec.tex'])

    def test_package_without_text_is_not_stored(self):
        self.input.write_bytes(tar_bytes([('fig.png', b'\x89PNG'), ('../escape.tex', b'x'),
                                          ('link.tex', None)]))
        with self.assertRaises(NoTextSource):
            self.store.put('2609.12345', 1, 'source', self.input)
        self.assertIsNone(self.store.catalog.get_artifact('2609.12345', 'source'))
        self.assertFalse(list(self.store.archive.rglob('*.zst')))
        self.input.write_bytes(gzip.compress(b'%PDF-1.4 a gzipped pdf'))
        with self.assertRaises(NoTextSource):
            self.store.put('2609.12345', 1, 'source', self.input)

    def legacy_source(self, package, version=1):
        """A source stored the old way: original package bytes, no filter provenance."""
        self.input.write_bytes(package)
        compressed = self.root / 'legacy.zst'
        sha, size = self.store._compress(self.input, compressed)
        location = f'source/2609/2609.12345v{version}.source.zst'
        target = self.store._publish(compressed, location, size, sha)
        self.store.catalog.put_artifact(dict(
            paper_id='2609.12345', version=version, kind='source', location=location,
            original_size=size, original_sha256=sha, stored_size=target.stat().st_size,
            stored_sha256=None, compression='zstd', media_type='application/gzip',
            provenance={'source': 'arxiv'}, status='available'))
        return target

    def test_strip_rewrites_legacy_sources_and_survives_interruption(self):
        package = tar_bytes([('main.tex', b'\\documentclass{article}'), ('fig.png', b'\x89PNG' * 2000)])
        old = self.legacy_source(package)
        self.input.write_bytes(package)
        # A refetch of the same upstream package converges on the legacy artifact.
        self.assertEqual(self.store.put('2609.12345', 1, 'source', self.input)['location'],
                         'source/2609/2609.12345v1.source.zst')
        preview = self.store.strip_source('2609.12345', 1, dry_run=True)
        self.assertEqual(preview['status'], 'dry-run')
        self.assertLess(preview['after'], preview['before'])
        self.assertTrue(old.exists())
        with patch.object(self.store.catalog, 'put_artifact', side_effect=RuntimeError('crash')):
            with self.assertRaises(RuntimeError):
                self.store.strip_source('2609.12345', 1)
        self.assertTrue(old.exists())
        self.assertEqual(self.store.catalog.unfiltered_sources(), [{'paper_id': '2609.12345', 'version': 1}])
        result = self.store.strip_source('2609.12345', 1)
        self.assertEqual(result['status'], 'filtered')
        self.assertFalse(old.exists())
        artifact = self.store.catalog.get_artifact('2609.12345', 'source', 1)
        self.assertEqual(artifact['location'], 'source/2609/2609.12345v1.source-text.zst')
        self.assertEqual(artifact['provenance']['upstream_sha256'], hashlib.sha256(package).hexdigest())
        self.assertEqual([e['path'] for e in self.store.sources('2609.12345')], ['main.tex'])
        self.assertEqual(self.store.catalog.unfiltered_sources(), [])
        # A crash between the catalog commit and deletion leaves the old file for the sweep.
        old.write_bytes(b'leftover')
        self.assertEqual(self.store.sweep_replaced_sources(), 1)
        self.assertFalse(old.exists())
        self.assertEqual(self.store.strip_source('2609.12345', 1)['status'], 'already-filtered')

    def test_strip_limit_never_widens_to_all_sources(self):
        from arxiv_archive.cli import parser
        self.legacy_source(tar_bytes([('main.tex', b'x')]))
        self.assertEqual(self.store.catalog.unfiltered_sources(0), [])
        self.assertEqual(len(self.store.catalog.unfiltered_sources(1)), 1)
        with self.assertRaises(ValueError):
            self.store.catalog.unfiltered_sources(-1)
        for bad in ('0', '-1'):
            with self.assertRaises(SystemExit), patch('sys.stderr'):
                parser().parse_args(['strip-sources', '--limit', bad])

    def test_sweep_ignores_client_supplied_replaced_location(self):
        self.input.write_bytes(PDF)
        self.store.put('2609.12345', 1, 'pdf', self.input)
        pdf = self.store.catalog.get_artifact('2609.12345', 'pdf', 1)
        self.input.write_bytes(tar_bytes([('main.tex', b'x')]))
        stored = self.store.put('2609.12345', 1, 'source', self.input,
                                provenance={'replaced_location': pdf['location']})
        self.assertNotIn('replaced_location', stored['provenance'])
        # Even a recorded value is honored only when it is the row's own original path.
        self.store.catalog.put_artifact({**stored, 'location': 'source/2609/2609.12345v1.source-text.zst',
                                         'provenance': {**stored['provenance'],
                                                        'replaced_location': pdf['location']}})
        self.assertEqual(self.store.sweep_replaced_sources(), 0)
        self.assertEqual(self.store.get('2609.12345').read_bytes(), PDF)

    def test_strip_keeps_a_corrupt_original(self):
        old = self.legacy_source(tar_bytes([('fig.png', b'\\x89PNG')]))
        artifact = self.store.catalog.get_artifact('2609.12345', 'source', 1)
        self.store.catalog.put_artifact({**artifact, 'original_sha256': '0' * 64})
        with self.assertRaisesRegex(ValueError, 'catalog hash'):
            self.store.strip_source('2609.12345', 1)
        self.assertTrue(old.exists())

    def test_strip_marks_textless_legacy_sources_unavailable(self):
        old = self.legacy_source(tar_bytes([('fig.png', b'\x89PNG')]))
        self.assertEqual(self.store.strip_source('2609.12345', 1)['status'], 'unavailable')
        self.assertFalse(old.exists())
        with self.assertRaises(FileNotFoundError):
            self.store.get('2609.12345', 'source', 1)
        # A crash before the deletion leaves the original where the sweep still finds it.
        old.write_bytes(b'leftover')
        self.assertEqual(self.store.sweep_replaced_sources(), 1)
        self.assertFalse(old.exists())

    def test_historical_latex_locator(self):
        import pyarrow as pa
        import pyarrow.parquet as pq
        shard = self.store.archive/'hf/latex/example.parquet'; shard.parent.mkdir(parents=True)
        pq.write_table(pa.table({'paper_id':['2609.12345','2609.99999'],
            'path':['main.tex','unrelated.tex'],'size':[5,4],'content':[b'hello',b'else']}),shard)
        self.store.catalog.db.execute('INSERT INTO latex_locations VALUES (?,?,?)',
                                     ('2609.12345','hf/latex/example.parquet',0))
        self.store.catalog.db.commit()
        self.assertEqual(self.store.sources('2609.12345'),[{'path':'main.tex','size':5}])
        self.assertEqual(self.store.source_file('2609.12345','main.tex').read_bytes(),b'hello')
        with tarfile.open(self.store.get('2609.12345','source')) as tar:
            self.assertEqual(tar.getnames(),['main.tex'])
            self.assertEqual(tar.extractfile('main.tex').read(),b'hello')
        with self.assertRaises(FileNotFoundError):
            self.store.get('2609.12345','source',1)

    def test_mount_guard_precedes_creation_and_size_guard(self):
        with patch('arxiv_archive.store.storage_guard', side_effect=RuntimeError('missing mount')):
            with self.assertRaises(RuntimeError):
                Store(self.root/'absent',self.root/'absent_archive')
        self.assertFalse((self.root/'absent').exists())
        self.store.max_bytes=4
        self.input.write_bytes(PDF)
        with self.assertRaises(ValueError):
            self.store.put('2609.12345',1,'pdf',self.input)

    def test_source_expansion_and_extension_headers_are_bounded(self):
        self.input.write_bytes(gzip.compress(b'x' * 10000))
        self.store.put('2609.12345', 1, 'source', self.input)
        self.store.max_bytes = 5000
        with self.assertRaises(ValueError):
            self.store.sources('2609.12345')
        self.input.write_bytes(gzip.compress(b'x' * 10000))
        with self.assertRaisesRegex(ValueError, 'expansion limit'):
            self.store.put('2609.12345', 2, 'source', self.input)
        self.store.max_bytes = 2_000_000_000
        header = tarfile.TarInfo('pax')
        header.type, header.size = tarfile.XHDTYPE, 100_000_000
        self.input.write_bytes(header.tobuf())
        with self.assertRaisesRegex(ValueError, 'extension header'):
            self.store.put('2609.12345', 2, 'source', self.input)

if __name__ == '__main__':
    unittest.main()
