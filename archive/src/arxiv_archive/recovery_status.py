"""Compact read-only progress for one-time recovery campaigns."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import subprocess
import time


def _unit_state(name):
    try:
        result = subprocess.run(['systemctl', 'show', name, '-p', 'ActiveState', '--value'],
                                capture_output=True, text=True, timeout=3)
        return result.stdout.strip() if result.returncode == 0 else 'unknown'
    except (OSError, subprocess.TimeoutExpired):
        return 'unknown'


def read_status(path: Path, catalog: Path) -> dict:
    """Read only metadata; no campaign lock, schema setup, payload reads or updates."""
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        scope = json.loads(db.execute("SELECT value FROM settings WHERE key='scope'").fetchone()[0])
        frozen = db.execute("SELECT 1 FROM settings WHERE key='receipt'").fetchone() is not None
        rows = [dict(r) for r in db.execute(
            'SELECT kind,status,count(*) AS count FROM candidates GROUP BY kind,status')]
        totals = db.execute("SELECT count(DISTINCT paper_id),coalesce(sum(CASE WHEN status='available' "
                           "THEN original_size ELSE 0 END),0),coalesce(sum(CASE WHEN status='available' "
                           "THEN stored_size ELSE 0 END),0) FROM candidates").fetchone()
        reasons = dict(db.execute('SELECT reason,count(*) FROM reasons GROUP BY reason'))
        now = time.time()
        recent, elapsed = 0, 0
        if catalog.is_file():
            db.execute('ATTACH DATABASE ? AS archive', (catalog.as_uri() + '?mode=ro',))
            timing = db.execute('''SELECT min(j.created_at),
                count(CASE WHEN j.status IN ('complete','failed','unavailable')
                           AND j.updated_at>=? THEN 1 END)
                FROM candidates c JOIN archive.jobs j ON j.id=c.job_id''', (now-900,)).fetchone()
            recent = timing[1]
            if timing[0] is not None:
                elapsed = now - max(timing[0], now-900)
    states, kinds = {}, {}
    for row in rows:
        states[row['status']] = states.get(row['status'], 0) + row['count']
        kinds[row['kind']] = kinds.get(row['kind'], 0) + row['count']
    return {'scope':scope, 'frozen':frozen, 'candidates':sum(states.values()), 'papers':totals[0],
            'states':states, 'kinds':kinds, 'reasons':reasons,
            'acquired_original_bytes':totals[1], 'acquired_stored_bytes':totals[2],
            'by_kind':rows, 'recent_settled':recent, 'sample_seconds':elapsed,
            'feeder':_unit_state(f"paper-archive-recovery@{scope['name']}.service"),
            'worker':_unit_state('paper-archive-worker.service')}


def _duration(seconds):
    hours = max(1, round(seconds / 3600))
    return f'{hours // 24}d {hours % 24}h' if hours >= 24 else f'{hours}h'


def format_status(data: dict) -> str:
    states, total = data['states'], data['candidates']
    reused, new = states.get('already_available', 0), states.get('available', 0)
    saved = reused + new
    failed = states.get('failed', 0)
    unavailable = states.get('unavailable', 0)
    unresolved = states.get('unresolved', 0)
    waiting = sum(states.get(s, 0) for s in ('pending','retry_pending','queued','acquiring'))
    if not data['frozen']:
        state = 'PREPARING SELECTION'
    elif not waiting:
        state = 'FINISHED WITH GAPS' if failed+unavailable+unresolved else 'COMPLETE'
    elif data['worker'] == 'failed' or data['feeder'] == 'failed':
        state = 'SERVICE FAILED'
    elif data['worker'] != 'active':
        state = 'WORKER STOPPED' if data['worker'] != 'unknown' else 'SERVICE STATE UNKNOWN'
    elif data['feeder'] == 'active':
        state = 'RUNNING'
    else:
        state = 'FEEDER STOPPED (queued downloads can finish)'
    lines = [f'Recovery: {state}',
             f"Scope: {data['scope']['start']} to {data['scope']['until']} | {data['papers']:,} papers",
             '', f'Files saved: {saved:,} / {total:,} ({saved / total * 100 if total else 0:.1f}%)']
    for kind, label in [('pdf','PDFs'),('source','LaTeX sources')]:
        held = sum(r['count'] for r in data['by_kind']
                   if r['kind']==kind and r['status'] in ('available','already_available'))
        lines.append(f"  {label:<14} {held:>7,} / {data['kinds'].get(kind,0):,}")
    lines += [f'Newly downloaded: {new:,} | Already held: {reused:,}',
              f'Left to download: {waiting:,}',
              f'Need attention: {failed:,} failed | {unavailable:,} unavailable | {unresolved:,} unresolved',
              f"New data: {data['acquired_original_bytes']/1e9:.2f} GB original -> "
              f"{data['acquired_stored_bytes']/1e9:.2f} GB on HDD"]
    if states.get('retry_pending', 0):
        lines.append(f"Retry backlog: {states['retry_pending']:,} (included in left to download)")
    elapsed, recent = data['sample_seconds'], data['recent_settled']
    if state == 'RUNNING' and elapsed >= 300 and recent >= 30:
        rate = recent / elapsed
        lines.append(f'Pace: {rate*3600:,.0f} files/hour | First-pass ETA: ~{_duration(waiting/rate)}')
        lines.append('  ETA includes failed attempts; unresolved files still need follow-up.')
    elif waiting:
        lines.append('ETA: collecting a longer running sample' if state == 'RUNNING'
                     else 'ETA: unavailable while stopped')
    lines.append('Counts refresh about every 30 seconds. Use --json for details.')
    return '\n'.join(lines)
