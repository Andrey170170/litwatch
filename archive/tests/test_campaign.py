"""Recovery selection safety, durable resume, and immutable acquisition receipts."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from arxiv_archive.campaign import Campaign


class CampaignTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'manifest.sqlite'
        self.scope = {'categories': ['cs.CL', 'cs.AI'], 'cutoff': '2026-09-27'}
        self.campaign = Campaign(self.path, self.scope)
        self.addCleanup(lambda: self.campaign.close())

    def complete_discovery(self):
        for category in self.scope['categories']:
            self.campaign.checkpoint(category, done=True, response='2026-09-27T20:00:00Z')

    def test_scope_cannot_change_on_reopen(self):
        with Campaign(self.path, dict(self.scope)) as opened:
            self.assertEqual(opened.scope, self.scope)
        with self.assertRaisesRegex(ValueError, 'scope differs'):
            Campaign(self.path, {**self.scope, 'cutoff': '2026-10-01'})
        with self.assertRaises(FileNotFoundError):
            Campaign(self.path.with_name('absent.sqlite'))

    def test_candidate_dedup_preserves_reasons_and_versions(self):
        first = self.campaign.add_candidate('2608.12345', 2, 'pdf', {'title': 'Example'}, 'missing_pdf')
        same = self.campaign.add_candidate('2608.12345', 2, 'pdf', {'abstract': 'Words'}, 'backfill')
        other = self.campaign.add_candidate('2608.12345', 1, 'pdf', {}, 'missing_pdf')
        self.assertEqual(first, same)
        self.assertNotEqual(first, other)
        self.assertEqual(self.campaign.get(first)['reasons'], ['backfill', 'missing_pdf'])
        self.assertEqual(self.campaign.get(first)['metadata'], {'title': 'Example', 'abstract': 'Words'})
        self.assertEqual(len(self.campaign.pending()), 2)
        with Campaign(self.path) as reopened:
            self.assertEqual(reopened.get(first), self.campaign.get(first))

    def test_unknown_resolution_merges_identity_without_losing_reasons(self):
        unknown = self.campaign.add_candidate('2608.12345', None, 'pdf', {'title': 'Title'}, 'missing_pdf')
        known = self.campaign.add_candidate('2608.12345', 3, 'pdf', {'abstract': 'Text'}, 'backfill')
        self.assertEqual(len(self.campaign.pending()), 1)
        self.assertEqual(self.campaign.resolve(unknown, 3), known)
        self.assertEqual(self.campaign.get(known)['reasons'], ['backfill', 'missing_pdf'])
        self.assertEqual(self.campaign.get(known)['metadata'], {'title': 'Title', 'abstract': 'Text'})
        self.assertEqual(self.campaign.statistics()['candidates'], 1)
        with self.assertRaises(KeyError):
            self.campaign.get(unknown)
        with self.assertRaisesRegex(ValueError, 'already known'):
            self.campaign.resolve(known, 4)

    def test_discovery_page_and_checkpoint_commit_or_rollback_together(self):
        with self.assertRaisesRegex(RuntimeError, 'page failure'):
            with self.campaign.transaction():
                self.campaign.add_candidate('2608.12345', 1, 'pdf', {}, 'backfill')
                self.campaign.checkpoint('cs.CL', token='next-page', records=1)
                raise RuntimeError('page failure')
        self.assertEqual(self.campaign.pending(), [])
        self.assertIsNone(self.campaign.get_checkpoint('cs.CL'))
        with self.campaign.transaction():
            self.campaign.add_candidate('2608.12345', 1, 'pdf', {}, 'backfill')
            self.campaign.checkpoint('cs.CL', token='next-page', records=1)
        with Campaign(self.path) as opened:
            checkpoint = opened.get_checkpoint('cs.CL')
            self.assertEqual(checkpoint['token'], 'next-page')
            self.assertEqual(checkpoint['details'], {'records': 1})
            self.assertEqual(len(opened.pending()), 1)

    def test_freeze_requires_completed_discovery_and_explicit_unresolved_errors(self):
        unknown = self.campaign.add_candidate('2608.12345', None, 'pdf', {}, 'missing_pdf')
        with self.assertRaisesRegex(ValueError, 'discovery incomplete'):
            self.campaign.freeze()
        self.complete_discovery()
        with self.assertRaisesRegex(ValueError, 'unresolved versions'):
            self.campaign.freeze(allow_unresolved=True)
        self.campaign.mark(unknown, 'unresolved', error='upstream record absent')
        with self.assertRaisesRegex(ValueError, 'unresolved versions'):
            self.campaign.freeze()
        receipt = self.campaign.freeze(allow_unresolved=True)
        self.assertEqual(receipt['unresolved_count'], 1)
        self.assertEqual(json.loads((self.path.parent / 'selection.jsonl').read_text())['version'], None)
        self.assertEqual(self.campaign.pending(), [])
        with self.assertRaisesRegex(ValueError, 'unknown version'):
            self.campaign.mark(unknown, 'queued', job_id=42)

    def test_frozen_selection_survives_delivery_and_reopen(self):
        first = self.campaign.add_candidate('2608.12345', 2, 'pdf', {}, 'backfill')
        second = self.campaign.add_candidate('2608.12346', 1, 'source', {}, 'backfill')
        self.complete_discovery()
        receipt = self.campaign.freeze()
        selection_path = self.path.parent / 'selection.jsonl'
        original = selection_path.read_bytes()
        self.assertEqual(receipt['selection_sha256'], hashlib.sha256(original).hexdigest())
        self.campaign.mark(first, 'queued', job_id=42)
        self.campaign.mark(first, 'acquiring')
        self.assertEqual(self.campaign.get(first)['job_id'], 42)
        self.campaign.mark(first, 'available', original_size=100, stored_size=70)
        self.campaign.mark(second, 'already_available', original_size=60, stored_size=40)
        self.assertEqual(self.campaign.statistics()['acquired_stored_bytes'], 70)
        self.assertEqual(self.campaign.freeze(), receipt)
        self.assertEqual(selection_path.read_bytes(), original)
        with Campaign(self.path) as opened:
            self.assertTrue(opened.frozen)
            self.assertEqual(opened.statistics()['states'], {'already_available': 1, 'available': 1})
            with self.assertRaisesRegex(ValueError, 'frozen'):
                opened.add_candidate('2608.12347', 1, 'pdf', {}, 'backfill')
            with self.assertRaisesRegex(ValueError, 'frozen'):
                opened.checkpoint('cs.CL', done=False)
            with self.assertRaisesRegex(ValueError, 'frozen'):
                opened.resolve(first, 2)

    def test_selection_order_is_deterministic(self):
        self.campaign.add_candidate('2608.12346', 1, 'source', {'b': 2, 'a': 1}, 'backfill')
        self.campaign.add_candidate('2608.12345', 2, 'pdf', {}, 'missing_pdf')
        self.complete_discovery()
        first = self.campaign.freeze()
        other_path = self.path.parent / 'other' / 'manifest.sqlite'
        with Campaign(other_path, self.scope) as other:
            other.add_candidate('2608.12345', 2, 'pdf', {}, 'missing_pdf')
            other.add_candidate('2608.12346', 1, 'source', {'a': 1, 'b': 2}, 'backfill')
            for category in self.scope['categories']:
                other.checkpoint(category, done=True)
            second = other.freeze()
        self.assertEqual(first['selection_sha256'], second['selection_sha256'])

    def test_phase_progress_persists_without_changing_scope_or_receipt(self):
        self.assertIsNone(self.campaign.get_progress('missing_seed'))
        with self.campaign.transaction():
            self.campaign.add_candidate('2608.12345', 1, 'pdf', {}, 'missing_pdf')
            self.campaign.set_progress('missing_seed', {'done': True})
        with Campaign(self.path) as opened:
            self.assertEqual(opened.get_progress('missing_seed'), {'done': True})
        self.complete_discovery()
        receipt = self.campaign.freeze()
        receipt_bytes = (self.path.parent / 'receipt.json').read_bytes()
        self.campaign.set_progress('metadata', {'cursor': 1})
        self.campaign.set_progress('scope', {'forged': True})
        self.campaign.set_progress('receipt', {'forged': True})
        with Campaign(self.path) as opened:
            self.assertEqual(opened.get_progress('metadata'), {'cursor': 1})
            self.assertEqual(opened.scope, self.scope)
            self.assertEqual(opened.freeze(), receipt)
        self.assertEqual((self.path.parent / 'receipt.json').read_bytes(), receipt_bytes)

    def test_phase_checkpoint_rolls_back_with_selection_changes(self):
        with self.assertRaises(RuntimeError):
            with self.campaign.transaction():
                self.campaign.add_candidate('2608.12345', 1, 'pdf', {}, 'missing_pdf')
                self.campaign.set_progress('missing_seed', {'done': True})
                raise RuntimeError('interrupted seed')
        self.assertIsNone(self.campaign.get_progress('missing_seed'))
        self.assertEqual(self.campaign.statistics()['candidates'], 0)
        with self.assertRaises(ValueError):
            self.campaign.set_progress('metadata', [])
        with self.assertRaises(ValueError):
            self.campaign.get_progress('')

    def test_invalid_id_version_and_state_fail_before_submission(self):
        for version in (0, -1, True, 1.5):
            with self.subTest(version=version), self.assertRaises(ValueError):
                self.campaign.add_candidate('2608.12345', version, 'pdf', {}, 'backfill')
        with self.assertRaises(ValueError):
            self.campaign.add_candidate('2608.12345v2', 2, 'pdf', {}, 'backfill')
        candidate = self.campaign.add_candidate('2608.12345', 2, 'pdf', {}, 'backfill')
        with self.assertRaises(ValueError):
            self.campaign.mark(candidate, 'unresolved')
        with self.assertRaises(ValueError):
            self.campaign.mark(candidate, 'available', stored_size=-1)
        self.campaign.mark(candidate, 'failed', error='transient outage')
        self.assertEqual(self.campaign.pending(), [])
        self.campaign.mark(candidate, 'pending')
        self.assertIsNone(self.campaign.get(candidate)['error'])
        self.assertEqual(len(self.campaign.pending()), 1)


if __name__ == '__main__':
    unittest.main()
