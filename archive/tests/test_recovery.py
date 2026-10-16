from pathlib import Path
import tempfile
import unittest

from arxiv_archive.campaign import Campaign
from arxiv_archive.discovery import Page, ResumptionTokenExpired
from arxiv_archive.recovery import prepare, reconcile, select_record, submit
from arxiv_archive.store import Store


SCOPE = {'name':'test', 'categories':['cs.CL'], 'start':'2026-08-13', 'until':'2026-09-27',
         'cutoff':'2026-09-27T19:00:00+00:00', 'priority':200}

def record(paper_id='2608.10001', published='2026-08-14T10:00:00+00:00'):
    return {'id':paper_id, 'deleted':False, 'latest_version':1, 'metadata': {
        'categories':['cs.CL'], 'title':'Paper', 'published':published,
        'version_history':[{'version':1,'date':published}]}}

class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root/'work', self.root/'archive', guard=False)
        self.store.catalog.set_pdfs('all')  # a PDF campaign under `wanted` needs priority below 100
        self.campaign = Campaign(self.root/'campaign'/'manifest.sqlite', SCOPE)
        self.manifest = self.root/'missing.csv'
        self.manifest.write_text('paper_id\n2608.10001\n')
        self.store.catalog.upsert_paper('2608.10001',record()['metadata'],1)
    def tearDown(self):
        self.campaign.close()
        self.store.close()
        self.tmp.cleanup()
    def test_discovery_freezes_before_jobs_and_overlap_converges(self):
        class Discovery:
            def fetch_page(self,*args,**kwargs):
                return Page([record()],None,'2026-09-27T19:01:00Z')
        with self.assertRaises(ValueError):
            submit(self.campaign,self.store.catalog)
        prepare(self.campaign,self.store.catalog,Discovery(),self.manifest)
        self.assertTrue(self.campaign.frozen)
        self.assertEqual(self.campaign.statistics()['candidates'],2)
        self.assertIsNone(self.store.catalog.claim_job())
        result=submit(self.campaign,self.store.catalog)
        self.assertEqual(result['submitted'],2)
        self.assertEqual(submit(self.campaign,self.store.catalog)['submitted'],0)
        jobs=self.store.catalog.db.execute('SELECT priority FROM jobs').fetchall()
        self.assertEqual([r[0] for r in jobs],[200,200])
        pdf=next(r for r in self.campaign.candidates() if r['kind']=='pdf')
        self.assertEqual(pdf['reasons'],['backfill','missing_pdf'])
        self.assertEqual(reconcile(self.campaign,self.store.catalog)['states'],{'queued':2})
    def test_existing_verified_pdf_is_not_submitted(self):
        path=self.root/'paper.pdf';path.write_bytes(b'%PDF-1.7\nexample')
        self.store.put('2608.10001',1,'pdf',path)
        class Discovery:
            def fetch_page(self,*args,**kwargs):
                return Page([],None,'2026-09-27T19:01:00Z')
        prepare(self.campaign,self.store.catalog,Discovery(),self.manifest)
        self.assertEqual(submit(self.campaign,self.store.catalog)['submitted'],0)
        self.assertEqual(self.campaign.statistics()['states'],{'already_available':1})
    def test_expired_token_replays_without_duplicate_selection(self):
        self.campaign.checkpoint('cs.CL',token='expired')
        class Discovery:
            def fetch_page(self,*args,**kwargs):
                if kwargs['token']:
                    raise ResumptionTokenExpired('expired')
                return Page([record()],None,'2026-09-27T19:01:00Z')
        prepare(self.campaign,self.store.catalog,Discovery(),self.manifest)
        self.assertEqual(self.campaign.statistics()['candidates'],2)
    def test_first_submission_scope_and_cutoff(self):
        self.assertIsNone(select_record(record(published='2020-01-01T00:00:00Z'),SCOPE))
        self.assertIsNone(select_record(record(published='2026-09-27T20:00:00Z'),SCOPE))
        self.assertIsNotNone(select_record(record(),SCOPE))
        changed=record();changed['latest_version']=2
        changed['metadata']['version_history'].append({'version':2,'date':'2026-09-27T20:00:00Z'})
        selected=select_record(changed,SCOPE)
        self.assertEqual(selected[1],1)
        self.assertEqual(selected[2]['metadata_observed_version'],2)

    def test_endpoint_retry_intents_survive_refresh_and_obey_feeder_bound(self):
        from unittest.mock import patch
        from arxiv_archive.recovery import feed, request_failed_retries
        class Discovery:
            def fetch_page(self, *args, **kwargs):
                return Page([record()], None, '2026-09-27T19:01:00Z')
        prepare(self.campaign, self.store.catalog, Discovery(), self.manifest)
        submit(self.campaign, self.store.catalog)
        for _ in range(2):
            job = self.store.catalog.claim_job()
            self.store.catalog.finish_job(job['id'], job['lease_token'], 'failed', 'unexpected arXiv HTTP 406')
        reconcile(self.campaign, self.store.catalog)
        receipt = (self.campaign.path.parent / 'receipt.json').read_bytes()
        result = request_failed_retries(self.campaign, self.store.catalog)
        self.assertEqual(result['retries_requested'], 2)
        self.assertTrue(Path(result['receipt']).is_file())
        self.assertEqual(reconcile(self.campaign, self.store.catalog)['states'], {'retry_pending': 2})
        self.assertEqual(request_failed_retries(self.campaign, self.store.catalog)['retries_requested'], 0)
        self.assertEqual(self.store.catalog.db.execute("SELECT count(*) FROM jobs WHERE status='pending'").fetchone()[0], 0)
        def work(_interval):
            self.assertEqual(self.store.catalog.db.execute("SELECT count(*) FROM jobs WHERE status='pending'").fetchone()[0], 1)
            job = self.store.catalog.claim_job()
            self.assertEqual(job['attempts'], 1)
            self.store.catalog.finish_job(job['id'], job['lease_token'], 'unavailable', 'HTTP 404')
        with patch('arxiv_archive.recovery.time.sleep', side_effect=work):
            result = feed(self.campaign, self.store.catalog, max_outstanding=1, interval=1)
        self.assertEqual(result['states'], {'unavailable': 2})
        self.assertEqual(request_failed_retries(self.campaign, self.store.catalog)['retries_requested'], 0)
        self.assertEqual((self.campaign.path.parent / 'receipt.json').read_bytes(), receipt)

    def test_retry_request_excludes_validation_errors_and_completed_files(self):
        from arxiv_archive.recovery import request_failed_retries
        class Discovery:
            def fetch_page(self, *args, **kwargs):
                return Page([record()], None, '2026-09-27T19:01:00Z')
        prepare(self.campaign, self.store.catalog, Discovery(), self.manifest)
        submit(self.campaign, self.store.catalog)
        job = self.store.catalog.claim_job()
        self.store.catalog.finish_job(job['id'], job['lease_token'], 'failed', 'invalid PDF')
        reconcile(self.campaign, self.store.catalog)
        self.assertEqual(request_failed_retries(self.campaign, self.store.catalog)['retries_requested'], 0)

    def test_missing_seed_version_does_not_change_on_resume(self):
        class Discovery:
            def fetch_page(self,*args,**kwargs):
                return Page([], 'next', '2026-09-27T19:01:00Z')
        prepare(self.campaign,self.store.catalog,Discovery(),self.manifest,max_pages=1)
        self.store.catalog.upsert_paper('2608.10001',{},2)
        prepare(self.campaign,self.store.catalog,Discovery(),self.manifest,max_pages=1)
        self.assertEqual([r['version'] for r in self.campaign.candidates()],[1])

    def test_feeder_bounds_queue_and_reports_unavailable(self):
        from unittest.mock import patch
        from arxiv_archive.recovery import feed
        class Discovery:
            def fetch_page(self,*args,**kwargs):
                return Page([record()],None,'2026-09-27T19:01:00Z')
        prepare(self.campaign,self.store.catalog,Discovery(),self.manifest)
        completed=[]
        def work(_interval):
            outstanding=self.store.catalog.db.execute(
                "SELECT count(*) FROM jobs WHERE status IN ('pending','running')").fetchone()[0]
            self.assertEqual(outstanding,1)
            job=self.store.catalog.claim_job()
            self.store.catalog.finish_job(job['id'],job['lease_token'],'unavailable','HTTP 404')
            completed.append(job['id'])
        with patch('arxiv_archive.recovery.time.sleep',side_effect=work):
            result=feed(self.campaign,self.store.catalog,max_outstanding=1,interval=1)
        self.assertEqual(len(completed),2)
        self.assertEqual(result['states'],{'unavailable':2})

    def test_reconcile_writes_changes_in_one_transaction_and_skips_the_unchanged(self):
        # archive.md, Deferred to N5: each candidate was marked in its own fsynced transaction
        # on every feed cycle, changed or not.
        class Discovery:
            def fetch_page(self,*args,**kwargs):
                return Page([record()],None,'2026-09-27T19:01:00Z')
        prepare(self.campaign,self.store.catalog,Discovery(),self.manifest)
        submit(self.campaign,self.store.catalog)
        statements=[]
        self.campaign.conn.set_trace_callback(statements.append)
        def writes():
            found=[s.split()[0].upper() for s in statements if s.split()[0].upper() in ('UPDATE','COMMIT')]
            statements.clear()
            return found
        self.assertEqual(reconcile(self.campaign,self.store.catalog)['states'],{'queued':2})
        self.assertNotIn('UPDATE',writes())
        for _ in range(2):
            job=self.store.catalog.claim_job()
            self.store.catalog.finish_job(job['id'],job['lease_token'],'unavailable','HTTP 404')
        self.assertEqual(reconcile(self.campaign,self.store.catalog)['states'],{'unavailable':2})
        self.assertEqual(writes(),['UPDATE','UPDATE','COMMIT'])

    def test_reconcile_follows_an_artifact_changed_after_its_job(self):
        # strip-sources rewrites a source, or finds no text in it, and leaves its job as it was.
        class Discovery:
            def fetch_page(self,*args,**kwargs):
                return Page([record()],None,'2026-09-27T19:01:00Z')
        prepare(self.campaign,self.store.catalog,Discovery(),self.manifest)
        submit(self.campaign,self.store.catalog)
        def artifact(job,size,status='available'):
            self.store.catalog.put_artifact({'paper_id':job['paper_id'],'version':job['version'],
                'kind':job['kind'],'location':f"{job['kind']}.zst",'original_size':size,
                'stored_size':size,'compression':'zstd','media_type':'application/octet-stream',
                'status':status})
        jobs=[]
        for _ in range(2):
            job=self.store.catalog.claim_job();jobs.append(job)
            artifact(job,100)
            self.store.catalog.finish_job(job['id'],job['lease_token'])
        sizes=lambda:sorted(r['stored_size'] for r in self.campaign.candidates())
        self.assertEqual(reconcile(self.campaign,self.store.catalog)['states'],{'available':2})
        self.assertEqual(sizes(),[100,100])
        artifact(jobs[0],40)
        artifact(jobs[1],0,'unavailable')
        result=reconcile(self.campaign,self.store.catalog)
        self.assertEqual(result['states'],{'available':1,'failed':1})
        self.assertIn(40,sizes())
