"""Prepare, freeze and submit a one-time recovery selection to the shared worker."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import csv
from datetime import date, datetime, timezone
import fcntl
import hashlib
import json
from pathlib import Path
import re
import sys
import time

from .campaign import Campaign, _atomic_write
from .discovery import OaiDiscovery, ResumptionTokenExpired
from .storage import storage_guard
from .store import Store

UTC = timezone.utc


def timestamp(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def select_record(record, scope):
    """Scope on first submission, not OAI metadata modification dates."""
    if record['deleted']:
        return None
    metadata = record['metadata']
    history = metadata.get('version_history', [])
    if not history:
        raise ValueError(f"No revision history for {record['id']}")
    first = min(timestamp(v['date']) for v in history)
    cutoff = timestamp(scope['cutoff'])
    if first.date() < date.fromisoformat(scope['start']) or first > cutoff:
        return None
    categories = metadata.get('categories', [])
    if isinstance(categories, str):
        categories = categories.split()
    if not set(categories).intersection(scope['categories']):
        return None
    eligible = [v for v in history if timestamp(v['date']) <= cutoff]
    if not eligible:
        return None
    latest = max(eligible, key=lambda v: v['version'])
    # Observed metadata may be newer than the immutable artifact cutoff. Keep
    # those identities separate instead of mislabeling titles or blocking resume.
    metadata = {**metadata, 'metadata_observed_version': record['latest_version']}
    return record['id'], latest['version'], metadata


def seed_missing(campaign, catalog, manifest):
    seeded = {row['paper_id'] for row in campaign.candidates() if 'missing_pdf' in row['reasons']}
    with manifest.open(newline='') as source:
        for row in csv.DictReader(source):
            paper_id = row['paper_id']
            if paper_id in seeded:
                continue
            paper = catalog.get_paper(paper_id)
            version = paper['latest_version'] if paper else None
            metadata = paper['metadata'] if paper else dict(row)
            entry = campaign.add_candidate(paper_id, version, 'pdf', metadata, 'missing_pdf')
            if version is None:
                campaign.mark(entry, 'unresolved', error='No explicit version in existing catalog')


def prepare(campaign, catalog, discovery, manifest, *, max_pages=None):
    """Resume discovery. No acquisition job is created during preparation."""
    if campaign.frozen:
        return campaign.statistics()
    if not campaign.get_progress('missing_seed'):
        with campaign.transaction():
            seed_missing(campaign, catalog, manifest)
            campaign.set_progress('missing_seed', {'done': True})
    pages = 0
    for category in campaign.scope['categories']:
        checkpoint = campaign.get_checkpoint(category) or {}
        if checkpoint.get('done'):
            continue
        token = checkpoint.get('token')
        resets = 0
        while True:
            try:
                page = discovery.fetch_page(category, campaign.scope['start'],
                                            None, token=token)
            except ResumptionTokenExpired:
                if resets >= 1:
                    raise
                resets += 1
                token = None
                campaign.checkpoint(category, token=None, done=False, error='Expired token; replaying')
                continue
            unknown = {r['paper_id']: r for r in campaign.candidates(('unresolved',))}
            with campaign.transaction():
                for record in page.records:
                    if record['id'] in unknown and not record['deleted']:
                        eligible = [v for v in record['metadata'].get('version_history', [])
                                    if timestamp(v['date']) <= timestamp(campaign.scope['cutoff'])]
                        if eligible:
                            campaign.resolve(unknown[record['id']]['id'],
                                             max(v['version'] for v in eligible),
                                             {**record['metadata'], 'metadata_observed_version':
                                              record['latest_version']})
                    selected = select_record(record, campaign.scope)
                    if selected is not None:
                        paper_id, version, metadata = selected
                        for kind in ('pdf', 'source'):
                            campaign.add_candidate(paper_id, version, kind, metadata, 'backfill')
                token = page.token
                campaign.checkpoint(category, token=token, done=not token, response=page.response_date)
            pages += 1
            print(json.dumps({'category': category, 'page_records': len(page.records),
                              'more': bool(token), 'candidates': campaign.statistics()['candidates']}),
                  flush=True)
            if max_pages and pages >= max_pages:
                return campaign.statistics()
            if not token:
                break
    published = set()
    # Presence checks reuse publication evidence; never rehash the existing collection.
    for entry in campaign.candidates():
        if entry['version'] is None:
            continue
        observed = entry['metadata'].get('metadata_observed_version', entry['version'])
        key = (entry['paper_id'], observed)
        if key not in published:
            publish_metadata(catalog, entry)
            published.add(key)
        artifact = catalog.get_artifact(entry['paper_id'], entry['kind'], entry['version'])
        if artifact:
            campaign.mark(entry['id'], 'already_available', original_size=artifact['original_size'],
                          stored_size=artifact['stored_size'])
    campaign.freeze(allow_unresolved=True)
    return campaign.statistics()


def publish_metadata(catalog, entry):
    metadata = dict(entry['metadata'])
    existing = catalog.get_paper(entry['paper_id'])
    if existing and existing['metadata'].get('authors'):
        metadata.pop('authors', None)
    catalog.upsert_paper(entry['paper_id'], metadata,
                         metadata.get('metadata_observed_version', entry['version']))


def request_failed_retries(campaign, catalog):
    """Operator-authorized endpoint retries stay in the feeder, not an unbounded queue."""
    if not campaign.frozen:
        raise ValueError('Prepare and freeze the complete campaign before retrying')
    selected = []
    for entry in campaign.candidates(('failed',)):
        job = catalog.get_job(entry['job_id']) if entry['job_id'] else None
        error = (job or {}).get('error') or ''
        if job and job['status'] == 'failed' and (
                error == 'unexpected arXiv HTTP 406' or error.startswith('arXiv HTTP 406') or
                error.startswith('arXiv transport error:') or
                error in ('empty arXiv response', 'arXiv body length differs from Content-Length')):
            selected.append({'candidate_id': entry['id'], 'job_id': job['id'],
                             'paper_id': entry['paper_id'], 'version': entry['version'],
                             'kind': entry['kind'], 'error': error, 'attempts': job['attempts']})
    receipt = campaign.path.parent / f'endpoint-retries-{time.time_ns()}.json'
    _atomic_write(receipt, (json.dumps(selected, indent=2) + '\n').encode())
    with campaign.transaction():
        for entry in selected:
            campaign.mark(entry['candidate_id'], 'retry_pending', error=entry['error'])
    return {'retries_requested': len(selected), 'receipt': str(receipt), **campaign.statistics()}


def submit(campaign, catalog, *, limit=None, paper_id=None):
    """Idempotently publish frozen intents through the existing catalog/queue API."""
    if not campaign.frozen:
        raise ValueError('Prepare and freeze the complete campaign before submission')
    count = 0
    for entry in campaign.pending(limit=limit if paper_id is None and limit else 1_000_000_000):
        if paper_id is not None and entry['paper_id'] != paper_id:
            continue
        if limit is not None and count >= limit:
            break
        publish_metadata(catalog, entry)
        job = catalog.enqueue(entry['paper_id'], entry['version'], entry['kind'],
                              priority=campaign.scope['priority'],
                              provenance={'origin':'recovery', 'campaign':campaign.scope['name'],
                                          'reasons':entry['reasons']})
        if entry['status'] == 'retry_pending' and job['status'] == 'failed':
            job = catalog.retry_job(job['id'])
        update_job(campaign, catalog, entry, job)
        count += 1
    return {'submitted': count, **campaign.statistics()}


def update_job(campaign, catalog, entry, job):
    """Record what the job and its artifact say of the candidate; nothing when that is what
    they say already. The artifact is read every time: `strip-sources` rewrites one without
    touching its finished job."""
    # Preserve operator intent until the bounded feeder actually resets the job.
    if entry['status'] == 'retry_pending' and job['status'] == 'failed':
        return
    status = {'pending':'queued', 'held':'queued', 'running':'acquiring', 'complete':'available',
              'failed':'failed', 'unavailable':'unavailable'}[job['status']]
    sizes = {}
    if status == 'available':
        artifact = catalog.get_artifact(entry['paper_id'], entry['kind'], entry['version'])
        if not artifact:
            status = 'failed'
            job = {**job, 'error':'Completed job has no available artifact'}
        else:
            sizes = {'original_size':artifact['original_size'], 'stored_size':artifact['stored_size']}
    if (entry['status'], entry['job_id'], entry['error']) == (status, job['id'], job.get('error')) \
            and all(entry[k] == v for k, v in sizes.items()):
        return
    campaign.mark(entry['id'], status, job_id=job['id'], error=job.get('error'), **sizes)


def reconcile(campaign, catalog):
    """Bring every submitted candidate up to its job, in one transaction for the cycle."""
    with campaign.transaction():
        for entry in campaign.candidates():
            if entry.get('job_id') is not None:
                job = catalog.get_job(int(entry['job_id']))
                if job:
                    update_job(campaign, catalog, entry, job)
    result = campaign.statistics()
    path = campaign.path.parent / 'summary.json'
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(result, indent=2, default=str)+'\n')
    temporary.replace(path)
    return result


def feed(campaign, catalog, max_outstanding=100, interval=30):
    """Bound recovery backlog; interruption leaves committed jobs and intents resumable."""
    if max_outstanding < 1 or interval <= 0:
        raise ValueError('Feeder bounds must be positive')
    if not campaign.frozen:
        raise ValueError('Prepare and freeze the campaign before running')
    while True:
        result = reconcile(campaign, catalog)
        states = result['states']
        outstanding = states.get('queued', 0) + states.get('acquiring', 0)
        if (states.get('pending', 0) + states.get('retry_pending', 0)) and outstanding < max_outstanding:
            result = submit(campaign, catalog, limit=max_outstanding-outstanding)
            states = result['states']
        print(json.dumps(result, default=str), flush=True)
        if not any(states.get(name, 0) for name in ('pending', 'retry_pending', 'queued', 'acquiring')):
            return reconcile(campaign, catalog)
        time.sleep(interval)


@contextmanager
def campaign_lock(directory):
    with (directory / '.command.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work', type=Path, default=Path('/srv/arxiv-work'))
    parser.add_argument('--archive', type=Path, default=Path('/srv/arxiv-archive'))
    parser.add_argument('--campaign', required=True)
    commands = parser.add_subparsers(dest='command', required=True)
    prep = commands.add_parser('prepare')
    prep.add_argument('--project', type=Path, required=True)
    prep.add_argument('--start', default='2026-08-13')
    prep.add_argument('--until', default=datetime.now(UTC).date().isoformat())
    prep.add_argument('--categories', default='cs.CL,cs.AI,cs.LG')
    prep.add_argument('--priority', type=int, default=200)
    prep.add_argument('--max-pages', type=int)
    send = commands.add_parser('submit')
    send.add_argument('--limit', type=int)
    send.add_argument('--paper', help='Restrict a pilot to one selected paper')
    status = commands.add_parser('status')
    status.add_argument('--json', action='store_true', help='Machine-readable details')
    commands.add_parser('refresh')
    commands.add_parser('retry-failed', help='Requeue endpoint failures through the bounded feeder; preserve unavailable files')
    runner = commands.add_parser('run')
    runner.add_argument('--max-outstanding', type=int, default=100)
    runner.add_argument('--interval', type=float, default=30)
    args = parser.parse_args(argv)
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}', args.campaign):
        parser.error('Unsafe campaign name')
    if args.command == 'status':
        from .recovery_status import format_status, read_status
        try:
            path = args.work.resolve() / 'campaigns' / args.campaign / 'manifest.sqlite'
            if not path.is_file():
                raise ValueError('Campaign does not exist; prepare it first')
            result = read_status(path, args.work.resolve() / 'catalog/catalog.sqlite')
            print(json.dumps(result, indent=2) if args.json else format_status(result))
            return 0
        except (ValueError, RuntimeError, OSError) as exc:
            print(f'paper-archive-recovery: {exc}', file=sys.stderr)
            return 1
    storage_guard(args.work, args.archive)
    directory = args.work / 'campaigns' / args.campaign
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / 'manifest.sqlite'
    try:
        with campaign_lock(directory):
            scope = None
            manifest = None
            if args.command == 'prepare':
                manifest = args.project / 'manifests/accepted/missing-pdfs.csv'
                if not path.exists():
                    now = datetime.now(UTC)
                    until = date.fromisoformat(args.until)
                    start = date.fromisoformat(args.start)
                    if start > until or until > now.date():
                        raise ValueError('Invalid date window')
                    cutoff = now if until == now.date() else datetime.combine(until, datetime.max.time(), UTC)
                    scope = {'name':args.campaign, 'start':start.isoformat(), 'until':until.isoformat(),
                             'cutoff':cutoff.isoformat(), 'categories':args.categories.split(','),
                             'priority':args.priority, 'missing_manifest_sha256':
                             hashlib.sha256(manifest.read_bytes()).hexdigest(),
                             'policy':'missing-pdf-and-cs-ai-gap-v1'}
            elif not path.is_file():
                raise ValueError('Campaign does not exist; prepare it first')
            campaign = Campaign(path, scope=scope)
            store = Store(work=args.work, archive=args.archive)
            try:
                if args.command == 'prepare':
                    if hashlib.sha256(manifest.read_bytes()).hexdigest() != campaign.scope['missing_manifest_sha256']:
                        raise ValueError('Missing-PDF input changed; preserve the original selection')
                    with OaiDiscovery(pacing_dir=args.work / 'pacing') as discovery:
                        result = prepare(campaign, store.catalog, discovery, manifest, max_pages=args.max_pages)
                elif args.command == 'submit':
                    if args.limit is not None and args.limit < 1:
                        raise ValueError('Limit must be positive')
                    result = submit(campaign, store.catalog, limit=args.limit, paper_id=args.paper)
                elif args.command == 'retry-failed':
                    result = request_failed_retries(campaign, store.catalog)
                elif args.command == 'run':
                    result = feed(campaign, store.catalog, args.max_outstanding, args.interval)
                else:
                    result = reconcile(campaign, store.catalog)
                print(json.dumps(result, indent=2, default=str), flush=True)
            finally:
                store.catalog.close()
                campaign.close()
    except (ValueError, RuntimeError, OSError) as exc:
        print(f'paper-archive-recovery: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
