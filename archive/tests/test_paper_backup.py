"""Catalog backups must include committed WAL changes and remain restorable."""
from pathlib import Path
from contextlib import closing
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from unittest import mock
from types import SimpleNamespace

from arxiv_archive import backup as BACKUP


# The flat layout an export wrote before `current`, as the owner's destination holds it.
LEGACY = ('catalog.sqlite', 'progress.sqlite', 'export.json', 'acquisition-job.json',
          'README.md', 'SHA256SUMS', 'pyproject.toml', 'requirements.lock', 'campaigns',
          'manifests')


def flat_layout(destination: Path) -> None:
    destination.mkdir(exist_ok=True)
    for name in LEGACY:
        if name in ('campaigns', 'manifests'):
            (destination / name).mkdir()
            (destination / name / 'kept').write_text('legacy')
        elif name == 'export.json':
            (destination / name).write_text(json.dumps(
                {'created_at': '2026-10-01T00:00:00+00:00', 'databases': {}, 'campaigns': {},
                 'restore': 'Restore catalog.sqlite to catalog/catalog.sqlite.'}))
        else:
            (destination / name).write_bytes(b'legacy')


def sources(root: Path) -> tuple[Path, Path]:
    """A work directory with both databases and a project with its manifests."""
    work, repo = root / 'work', root / 'repo'
    (work / 'catalog').mkdir(parents=True)
    (work / 'state').mkdir()
    (repo / 'manifests').mkdir(parents=True)
    (repo / 'manifests/frozen.json').write_text('{}')
    for name in ('catalog/catalog.sqlite', 'state/progress.sqlite'):
        with closing(sqlite3.connect(work / name)) as database:
            database.execute('CREATE TABLE records (id INTEGER)')
    return work, repo


class BackupTests(unittest.TestCase):
    def test_online_snapshot_contains_committed_wal_and_is_independent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with sqlite3.connect(root / 'live.sqlite') as live:
                live.execute('PRAGMA journal_mode=WAL')
                live.execute('CREATE TABLE papers (id TEXT PRIMARY KEY)')
                live.execute("INSERT INTO papers VALUES ('2609.12345')")
                live.commit()
                BACKUP.snapshot_database(root / 'live.sqlite', root / 'restore.sqlite')
                live.execute("INSERT INTO papers VALUES ('2609.54321')")
                live.commit()
                with sqlite3.connect(root / 'restore.sqlite') as restored:
                    self.assertEqual(restored.execute('SELECT id FROM papers').fetchall(), [('2609.12345',)])
                    self.assertEqual(restored.execute('PRAGMA integrity_check').fetchone()[0], 'ok')

    def test_snapshot_completes_while_another_connection_keeps_committing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with closing(sqlite3.connect(root / 'live.sqlite')) as live:
                live.execute('PRAGMA journal_mode=WAL')
                live.execute('CREATE TABLE blobs (id INTEGER PRIMARY KEY, body BLOB)')
                live.executemany('INSERT INTO blobs(body) VALUES (?)', [(bytes(4096),)] * 3000)
                live.commit()
            stop, done = threading.Event(), threading.Event()

            def write_continuously():
                with closing(sqlite3.connect(root / 'live.sqlite')) as writer:
                    while not stop.is_set():
                        writer.execute('INSERT INTO blobs(body) VALUES (?)', (b'x',))
                        writer.commit()

            def snapshot():
                BACKUP.snapshot_database(root / 'live.sqlite', root / 'restore.sqlite')
                done.set()

            writer = threading.Thread(target=write_continuously, daemon=True)
            writer.start()
            threading.Thread(target=snapshot, daemon=True).start()
            finished = done.wait(timeout=30)
            stop.set()
            writer.join()
            self.assertTrue(finished, 'snapshot restarted indefinitely under concurrent commits')
            with closing(sqlite3.connect(root / 'restore.sqlite')) as restored:
                self.assertGreaterEqual(restored.execute('SELECT count(*) FROM blobs').fetchone()[0], 3000)
                self.assertEqual(restored.execute('PRAGMA integrity_check').fetchone()[0], 'ok')

    def test_missing_database_does_not_replace_previous_export(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            work, repo, destination = root / 'work', root / 'repo', root / 'backup'
            (work / 'catalog').mkdir(parents=True)
            (work / 'state').mkdir()
            (repo / 'manifests').mkdir(parents=True)
            flat_layout(destination)
            previous = destination / 'catalog.sqlite'
            previous.write_bytes(b'previous snapshot')
            with self.assertRaises(FileNotFoundError):
                BACKUP.export(work, repo, destination)
            self.assertEqual(previous.read_bytes(), b'previous snapshot')

    def test_export_snapshots_campaign_wal_and_only_recovery_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            work, repo, destination = root / 'work', root / 'repo', root / 'backup'
            (work / 'catalog').mkdir(parents=True)
            (work / 'state').mkdir()
            (repo / 'manifests').mkdir(parents=True)
            (repo / 'manifests/frozen.json').write_text('{}')
            for name in ('catalog/catalog.sqlite', 'state/progress.sqlite'):
                with sqlite3.connect(work / name) as database:
                    database.execute('CREATE TABLE records (id INTEGER)')
            campaign = work / 'campaigns/recovery'
            campaign.mkdir(parents=True)
            for filename in ('selection.jsonl', 'receipt.json', 'summary.json', 'endpoint-retries-123.json'):
                (campaign / filename).write_text('{"frozen": true}\n')
            (campaign / 'worker.log').write_text('not selected')
            (campaign / 'paper.pdf').write_bytes(b'not selected')
            with sqlite3.connect(campaign / 'manifest.sqlite') as live:
                live.execute('PRAGMA journal_mode=WAL')
                live.execute('CREATE TABLE candidates (id TEXT)')
                live.execute("INSERT INTO candidates VALUES ('2609.12345')")
                live.commit()
                self.assertGreater((campaign / 'manifest.sqlite-wal').stat().st_size, 0)
                report = BACKUP.export(work, repo, destination)
                live.execute("INSERT INTO candidates VALUES ('2609.54321')")
                live.commit()
                exported = destination / 'current'
                saved = exported / 'campaigns/recovery'
                with closing(sqlite3.connect(saved / 'manifest.sqlite')) as restored:
                    self.assertEqual(restored.execute('SELECT id FROM candidates').fetchall(),
                                     [('2609.12345',)])
                    self.assertEqual(restored.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            self.assertEqual({p.name for p in saved.iterdir()},
                             {'manifest.sqlite', 'selection.jsonl', 'receipt.json', 'summary.json', 'endpoint-retries-123.json'})
            self.assertEqual((saved / 'receipt.json').read_bytes(),
                             (campaign / 'receipt.json').read_bytes())
            self.assertEqual(json.loads((exported / 'export.json').read_text()), report)
            self.assertIn('campaigns/recovery/manifest.sqlite', report['campaigns']['recovery']['files'])
            self.assertIn('work directory campaigns/', report['restore'])
            self.assertTrue((exported / 'catalog.sqlite').is_file())
            self.assertTrue((exported / 'progress.sqlite').is_file())
            self.assertTrue((exported / 'manifests/frozen.json').is_file())
            self.assertFalse([p for p in root.rglob('.arxiv-export-*')], 'staging left behind')
            with closing(sqlite3.connect(exported / 'catalog.sqlite')) as database:
                self.assertEqual(database.execute('PRAGMA journal_mode').fetchone()[0], 'delete')

    def test_an_export_deletes_only_what_it_proves_an_export_wrote(self):
        # archive.md, Deferred to N5: names never prove ownership. The flat layout, someone's
        # notes and look-alike names stay; marked staging and earlier generations go.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            work, repo = sources(root)
            destination = root / 'backup'
            flat_layout(destination)
            (destination / 'notes.md').write_text('mine')
            (destination / '.arxiv-export-x').mkdir()
            (destination / '.arxiv-export-x/catalog.sqlite').write_bytes(b'unmarked')
            (destination / 'export-123').mkdir()
            (destination / 'export-123/data').write_text('no export.json')
            killed = destination / '.arxiv-export-k1ll3d_x'
            killed.mkdir()
            (killed / BACKUP.MARKER).touch()
            (killed / 'catalog.sqlite').write_bytes(b'partial')
            strangers = {p.name for p in destination.iterdir()} - {killed.name}
            BACKUP.export(work, repo, destination)
            first = (destination / 'current').resolve().name
            BACKUP.export(work, repo, destination)
            second = (destination / 'current').resolve().name
            self.assertEqual({p.name for p in destination.iterdir()},
                             strangers | {'current', '.export.lock', second})
            self.assertNotEqual(first, second)
            self.assertEqual((destination / 'catalog.sqlite').read_bytes(), b'legacy')
            self.assertEqual((destination / '.arxiv-export-x/catalog.sqlite').read_bytes(), b'unmarked')
            self.assertFalse((destination / second / BACKUP.MARKER).exists())

    def test_a_generation_is_durable_before_current_names_it_and_its_predecessor_goes_after(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            work, repo = sources(root)
            destination = root / 'backup'
            BACKUP.export(work, repo, destination)
            predecessor = (destination / 'current').resolve()
            events = []
            fsync, replace, rmtree = BACKUP.os.fsync, BACKUP.os.replace, BACKUP.shutil.rmtree

            def synced(descriptor):
                events.append(('fsync', Path(os.readlink(f'/proc/self/fd/{descriptor}'))))
                fsync(descriptor)

            def swapped(source, target):
                events.append(('replace', Path(target)))
                replace(source, target)

            def removed(path, *args, **kwargs):
                events.append(('rmtree', Path(path)))
                rmtree(path, *args, **kwargs)

            with mock.patch.object(BACKUP.os, 'fsync', synced), \
                    mock.patch.object(BACKUP.os, 'replace', swapped), \
                    mock.patch.object(BACKUP.shutil, 'rmtree', removed):
                BACKUP.export(work, repo, destination)
            generation = (destination / 'current').resolve()
            swap = events.index(('replace', destination / 'current'))
            before = {path for kind, path in events[:swap] if kind == 'fsync'}
            written = {generation, *generation.rglob('*')}
            self.assertEqual(written - before, set(), 'not durable before current names it')
            retired = events.index(('rmtree', predecessor))
            self.assertIn(('fsync', destination), events[swap:retired])

    def test_a_failed_export_leaves_the_previous_one_whole_and_current(self):
        # archive.md, Deferred to N5: refreshing the export entry by entry left new databases
        # beside old directories when it failed part-way.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            work, repo, destination = root / 'work', root / 'repo', root / 'backup'
            (work / 'catalog').mkdir(parents=True)
            (work / 'state').mkdir()
            (repo / 'manifests').mkdir(parents=True)
            (repo / 'manifests/frozen.json').write_text('{"first": true}')
            for name in ('catalog/catalog.sqlite', 'state/progress.sqlite'):
                with closing(sqlite3.connect(work / name)) as database:
                    database.execute('CREATE TABLE records (id INTEGER)')
            first = BACKUP.export(work, repo, destination)
            self.assertEqual({p.name for p in destination.iterdir()} - {'current', '.export.lock'},
                             {(destination / 'current').resolve().name})
            with closing(sqlite3.connect(work / 'catalog/catalog.sqlite')) as database:
                database.execute('INSERT INTO records VALUES (1)')
                database.commit()
            (repo / 'manifests/frozen.json').write_text('{"first": false}')
            for failing in ('copytree', 'replace'):
                with self.subTest(failing=failing):
                    target = BACKUP.shutil if failing == 'copytree' else BACKUP.os
                    with mock.patch.object(target, failing, side_effect=OSError('disk gone')):
                        with self.assertRaises(OSError):
                            BACKUP.export(work, repo, destination)
                    current = destination / 'current'
                    self.assertEqual(json.loads((current / 'export.json').read_text()), first)
                    self.assertEqual((current / 'manifests/frozen.json').read_text(), '{"first": true}')
                    with closing(sqlite3.connect(current / 'catalog.sqlite')) as database:
                        self.assertEqual(database.execute('SELECT count(*) FROM records').fetchone()[0], 0)
            second = BACKUP.export(work, repo, destination)
            current = destination / 'current'
            self.assertEqual(json.loads((current / 'export.json').read_text()), second)
            self.assertEqual((current / 'manifests/frozen.json').read_text(), '{"first": false}')
            self.assertEqual({p.name for p in destination.iterdir()},
                             {'current', '.export.lock', current.resolve().name})

    def test_a_destination_shared_with_others_keeps_their_entries(self):
        # A wrong destination (say /srv/backups) must not lose its other entries.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            work, repo = sources(root)
            destination = root / 'backup'
            destination.mkdir()
            (destination / 'notes.txt').write_text('keep me')
            (destination / 'other-backups').mkdir()
            (destination / 'other-backups/data').write_text('keep me too')
            (destination / 'export-old').write_text('not a generation')
            before = {p.name for p in destination.iterdir()}
            BACKUP.export(work, repo, destination)
            BACKUP.export(work, repo, destination)
            self.assertLessEqual(before, {p.name for p in destination.iterdir()})
            self.assertEqual((destination / 'other-backups/data').read_text(), 'keep me too')
            # A `current` that is not an export's symlink is someone else's: refused untouched.
            elsewhere = root / 'elsewhere'
            elsewhere.mkdir()
            (elsewhere / 'current').write_text('mine')
            with self.assertRaisesRegex(RuntimeError, 'current is not the symlink'):
                BACKUP.export(work, repo, elsewhere)
            self.assertEqual({p.name for p in elsewhere.iterdir()}, {'current', '.export.lock'})

    def test_campaign_discovery_rejects_symlink_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            work = root / 'work'
            campaigns = work / 'campaigns'
            campaigns.mkdir(parents=True)
            outside = root / 'outside'
            outside.mkdir()
            (outside / 'manifest.sqlite').write_bytes(b'outside')
            campaign = campaigns / 'recovery'
            campaign.symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(RuntimeError, 'Unsafe campaign directory'):
                BACKUP.campaign_sources(work)
            campaign.unlink()
            campaign.mkdir()
            (campaign / 'manifest.sqlite').symlink_to(outside / 'manifest.sqlite')
            with self.assertRaisesRegex(RuntimeError, 'Unsafe campaign metadata'):
                BACKUP.campaign_sources(work)
            (campaign / 'manifest.sqlite').unlink()
            (campaign / 'manifest.sqlite').write_bytes(b'database')
            (campaign / 'receipt.json').symlink_to(outside / 'manifest.sqlite')
            with self.assertRaisesRegex(RuntimeError, 'Unsafe campaign metadata'):
                BACKUP.campaign_sources(work)

    def test_space_guard_includes_campaign_metadata_and_wal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            work, repo, destination = root / 'work', root / 'repo', root / 'backup'
            (work / 'catalog').mkdir(parents=True)
            (work / 'state').mkdir()
            (repo / 'manifests').mkdir(parents=True)
            for name in ('catalog/catalog.sqlite', 'state/progress.sqlite'):
                (work / name).write_bytes(b'base')
            campaign = work / 'campaigns/recovery'
            campaign.mkdir(parents=True)
            (campaign / 'manifest.sqlite').write_bytes(b'campaign')
            (campaign / 'manifest.sqlite-wal').write_bytes(b'wal pages')
            (campaign / 'selection.jsonl').write_bytes(b'frozen selection')
            required = 256 * 1024**2 + 8 + 8 + 9 + 16
            with mock.patch.object(BACKUP.shutil, 'disk_usage',
                                   return_value=SimpleNamespace(free=required - 1)):
                with self.assertRaisesRegex(RuntimeError, 'Insufficient backup storage'):
                    BACKUP.export(work, repo, destination)
            self.assertFalse((destination / 'catalog.sqlite').exists())


if __name__ == '__main__':
    unittest.main()
