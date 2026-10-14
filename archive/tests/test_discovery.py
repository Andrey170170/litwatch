"""Raw OAI discovery preserves revisions and resumes without weakening pacing."""
from contextlib import contextmanager
from datetime import date
import unittest
from unittest.mock import Mock
import xml.etree.ElementTree as ET

import requests

from arxiv_archive.discovery import (
    DiscoveryError, OaiDiscovery, RecordUnavailable, ResumptionTokenExpired, parse_record,
)

RECORD = '''<record xmlns="http://www.openarchives.org/OAI/2.0/">
<header><identifier>oai:arXiv.org:2608.12345</identifier><datestamp>2026-09-23</datestamp></header>
<metadata><arXivRaw xmlns="http://arxiv.org/OAI/arXivRaw/">
<id>2608.12345</id><version version="v2"><date>Tue, 22 Sep 2026 13:00:00 GMT</date>
<size>789kb</size><source_type>latex</source_type></version>
<version version="v1"><date>Thu, 13 Aug 2026 15:00:00 GMT</date></version>
<title>A useful paper</title><authors>A. Author and B. Author</authors>
<abstract>Line one.\nLine two.</abstract><categories>cs.CL cs.AI</categories>
<doi>10.1234/paper</doi></arXivRaw></metadata></record>'''


def envelope(content):
    return ('<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">'
            '<responseDate>2026-09-27T12:00:00Z</responseDate>' + content + '</OAI-PMH>').encode()


def response(content=b'', status=200, headers=None):
    item = Mock(status_code=status, headers=headers or {})
    item.iter_content.return_value = [content]
    return item


class Pacer:
    def __init__(self):
        self.events = []
        self.held = False

    @contextmanager
    def slot(self, **kwargs):
        assert kwargs['timeout'] > 0
        assert not self.held
        self.held = True
        self.events.append('enter')
        try:
            yield self
        finally:
            self.held = False
            self.events.append('exit')

    def defer(self, value):
        assert self.held
        self.events.append(('defer', value))


class DiscoveryTests(unittest.TestCase):
    def client(self, *responses, **kwargs):
        pacer, session = Pacer(), Mock()
        session.get.side_effect = responses
        for item in responses:
            if not isinstance(item, Exception):
                item.close.side_effect = lambda: self.assertTrue(pacer.held)
        return OaiDiscovery(session=session, pacer=pacer, **kwargs), session, pacer

    def test_page_preserves_explicit_history_and_metadata_change_date(self):
        client, session, _ = self.client(response(envelope(
            '<ListRecords>' + RECORD + '<resumptionToken>opaque+/==</resumptionToken></ListRecords>')))
        page = client.fetch_page('cs.CL', date(2026, 8, 13), '2026-09-27')
        self.assertEqual(page.token, 'opaque+/==')
        record = page.records[0]
        self.assertEqual(record['latest_version'], 2)
        self.assertEqual(record['datestamp'], '2026-09-23')
        metadata = record['metadata']
        self.assertEqual(metadata['published'], '2026-08-13T15:00:00+00:00')
        self.assertEqual(metadata['updated'], '2026-09-22T13:00:00+00:00')
        self.assertEqual(metadata['version_history'][1]['size'], '789kb')
        self.assertEqual(metadata['version_history'][1]['source_type'], 'latex')
        self.assertEqual(metadata['authors_raw'], 'A. Author and B. Author')
        self.assertEqual(metadata['categories'], ['cs.CL', 'cs.AI'])
        self.assertEqual(session.get.call_args.kwargs['params'], {
            'verb': 'ListRecords', 'metadataPrefix': 'arXivRaw', 'from': '2026-08-13',
            'until': '2026-09-27', 'set': 'cs:cs:CL'})
        self.assertFalse(session.get.call_args.kwargs['allow_redirects'])

    def test_open_ended_harvest_omits_until_but_still_validates_since(self):
        client, session, _ = self.client(response(envelope('<ListRecords>' + RECORD + '</ListRecords>')))
        page = client.fetch_page('cs.CL', '2026-08-13', None)
        self.assertEqual(len(page.records), 1)
        self.assertEqual(session.get.call_args.kwargs['params'], {
            'verb': 'ListRecords', 'metadataPrefix': 'arXivRaw', 'from': '2026-08-13',
            'set': 'cs:cs:CL'})
        with self.assertRaises(ValueError):
            client.fetch_page('cs.CL', 'not-a-date', None)
        self.assertEqual(session.get.call_count, 1)

    def test_resumption_request_uses_only_verb_and_token(self):
        client, session, _ = self.client(response(envelope('<ListRecords>' + RECORD + '</ListRecords>')))
        page = client.fetch_page('cs.AI', '2026-08-13', '2026-09-27', token='opaque+/==')
        self.assertIsNone(page.token)
        self.assertEqual(session.get.call_args.kwargs['params'], {
            'verb': 'ListRecords', 'resumptionToken': 'opaque+/=='})

    def test_deleted_header_remains_visible_without_artifact_version(self):
        deleted = ('<record xmlns="http://www.openarchives.org/OAI/2.0/">'
                   '<header status="deleted"><identifier>oai:arXiv.org:hep-th/9901001</identifier>'
                   '<datestamp>2026-09-27</datestamp></header></record>')
        result = parse_record(ET.fromstring(deleted))
        self.assertTrue(result['deleted'])
        self.assertEqual(result['id'], 'hep-th/9901001')
        self.assertIsNone(result['latest_version'])
        self.assertEqual(result['metadata'], {})

    def test_protocol_errors_distinguish_expiry_and_missing(self):
        for code, exception in [('badResumptionToken', ResumptionTokenExpired),
                                ('idDoesNotExist', RecordUnavailable),
                                ('cannotDisseminateFormat', DiscoveryError)]:
            with self.subTest(code=code):
                client, _, _ = self.client(response(envelope(f'<error code="{code}">bad</error>')))
                with self.assertRaises(exception):
                    client.fetch_page('cs.LG', '2026-08-13', '2026-09-27', token='old')

    def test_no_records_match_is_valid_only_for_first_page(self):
        empty = envelope('<error code="noRecordsMatch">none</error>')
        client, _, _ = self.client(response(empty), response(empty))
        self.assertEqual(client.fetch_page('cs.CL', '2026-08-13', '2026-09-27').records, [])
        with self.assertRaises(DiscoveryError):
            client.fetch_page('cs.CL', '2026-08-13', '2026-09-27', token='existing')

    def test_retry_after_and_transport_retries_use_separate_owned_slots(self):
        unavailable = response(status=503, headers={'Retry-After': '120'})
        good = response(envelope('<GetRecord>' + RECORD + '</GetRecord>'))
        client, session, pacer = self.client(unavailable, requests.Timeout(), good)
        self.assertEqual(client.get_record('2608.12345')['latest_version'], 2)
        self.assertEqual(session.get.call_count, 3)
        self.assertEqual(pacer.events.count('enter'), 3)
        self.assertEqual(pacer.events.count('exit'), 3)
        self.assertIn(('defer', '120'), pacer.events)
        unavailable.close.assert_called_once()
        good.close.assert_called_once()

    def test_repeated_throttle_is_bounded_and_never_returns_partial_page(self):
        client, session, _ = self.client(*(response(status=429) for _ in range(3)))
        with self.assertRaisesRegex(DiscoveryError, 'budget exhausted'):
            client.get_record('2608.12345')
        self.assertEqual(session.get.call_count, 3)

    def test_missing_versions_identity_mismatch_and_duplicate_versions_fail_closed(self):
        bad_records = [RECORD.replace('version="v1"', 'version="v2"'),
                       RECORD.replace('<id>2608.12345</id>', '<id>2608.12346</id>'),
                       RECORD.replace('version="v1"', 'version=""'),
                       RECORD.replace('version="v1"', 'version="v3"')]
        for record in bad_records:
            with self.subTest(record=record):
                with self.assertRaises(DiscoveryError):
                    parse_record(ET.fromstring(record))

    def test_get_record_identity_checked_and_unknown_version_not_accepted(self):
        client, _, _ = self.client(response(envelope('<GetRecord>' + RECORD + '</GetRecord>')))
        with self.assertRaisesRegex(DiscoveryError, 'different identifier'):
            client.get_record('2608.54321')
        with self.assertRaises(ValueError):
            client.get_record('2608.12345v2')

    def test_malformed_oversized_and_entity_xml_close_response(self):
        for payload, limit in [(b'garbage', 100), (b'a' * 101, 100),
                               (b'<!DOCTYPE OAI-PMH><OAI-PMH/>', 100)]:
            with self.subTest(payload=payload):
                reply = response(payload)
                client, _, _ = self.client(reply, max_bytes=limit)
                with self.assertRaises(DiscoveryError):
                    client.get_record('2608.12345')
                reply.close.assert_called_once()

    def test_input_scope_and_window_fail_before_network(self):
        client, session, _ = self.client()
        with self.assertRaises(ValueError):
            client.fetch_page('math.AG', '2026-08-13', '2026-09-27')
        with self.assertRaises(ValueError):
            client.fetch_page('cs.CL', '2026-09-27', '2026-08-13')
        session.get.assert_not_called()


if __name__ == '__main__':
    unittest.main()
