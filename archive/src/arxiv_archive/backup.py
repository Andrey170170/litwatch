#!/usr/bin/env python3
"""Export consistent catalog/acquisition snapshots for selective Restic backup.

Payloads stay on HDD. Each database uses SQLite's online backup API; catalog,
acquisition and campaign snapshots are individually consistent, not one transaction.

The export is one directory, `export-<time>`, named by the symlink `current` in the
destination. A new export is written beside it and `current` is swapped to it in one
rename, so a failure part-way leaves the previous export whole and current. An export only
ever deletes what an export wrote, and refuses a destination holding anything else.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
from fnmatch import fnmatchcase
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
from datetime import datetime, timezone

ARCHIVE = Path('/srv/arxiv-archive')
WORK = Path('/srv/arxiv-work')
DESTINATION = Path('/srv/backups/arxiv-catalog')



def require_mount(path: Path, expected: str) -> None:
    result = subprocess.run(['findmnt', '-rn', '-M', str(path), '-o', 'SOURCE'],
                            text=True, capture_output=True, check=True)
    if result.stdout.strip() != expected:
        raise RuntimeError(f'Wrong mounted source at {path}; expected {expected}')


def snapshot_database(source: Path, destination: Path) -> None:
    """Copy a live WAL database without touching its original journal mode."""
    if source.is_symlink() or not source.is_file():
        raise RuntimeError(f'Missing or unsafe database: {source}')
    with contextlib.closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as incoming:
        with contextlib.closing(sqlite3.connect(destination)) as outgoing:
            # One step copies from a single WAL read snapshot. Incremental steps restart
            # whenever another connection commits, which livelocks under the live worker.
            incoming.backup(outgoing, pages=-1)
            # A standalone copy: no WAL side files to pair wrongly with a later export.
            outgoing.execute('PRAGMA journal_mode=DELETE')
    with destination.open('rb') as saved:
        os.fsync(saved.fileno())


def campaign_sources(work: Path) -> dict[str, dict[str, Path]]:
    """Select campaign recovery metadata, never logs or staged payloads."""
    root = work / 'campaigns'
    if root.is_symlink():
        raise RuntimeError(f'Unsafe campaign directory: {root}')
    if not root.exists():
        return {}
    if not root.is_dir():
        raise RuntimeError(f'Unsafe campaign directory: {root}')
    selected = {}
    for directory in sorted(root.iterdir()):
        if directory.is_symlink():
            raise RuntimeError(f'Unsafe campaign directory: {directory}')
        if not directory.is_dir():
            continue
        manifest = directory / 'manifest.sqlite'
        if not manifest.exists() and not manifest.is_symlink():
            continue
        files = {}
        names = ['manifest.sqlite', 'selection.jsonl', 'receipt.json', 'summary.json']
        names.extend(p.name for p in sorted(directory.glob('endpoint-retries-*.json')))
        for name in names:
            source = directory / name
            if source.is_symlink():
                raise RuntimeError(f'Unsafe campaign metadata: {source}')
            if source.exists():
                if not source.is_file() or not source.resolve().is_relative_to(root.resolve()):
                    raise RuntimeError(f'Unsafe campaign metadata: {source}')
                files[name] = source
        selected[directory.name] = files
    return selected


def database_storage_size(source: Path) -> int:
    """Budget the main file plus committed pages still resident in its WAL."""
    size = source.stat().st_size
    wal = source.with_name(source.name + '-wal')
    if wal.is_symlink():
        raise RuntimeError(f'Unsafe database WAL: {wal}')
    try:
        size += wal.stat().st_size
    except FileNotFoundError:
        pass
    return size


CURRENT = 'current'
LOCK = '.export.lock'
# The names the flat layout before `current` wrote; a later export replaces them.
FLAT = ('catalog.sqlite', 'catalog.sqlite-wal', 'catalog.sqlite-shm', 'progress.sqlite',
        'progress.sqlite-wal', 'progress.sqlite-shm', 'campaigns', 'manifests', 'export.json',
        'acquisition-job.json', '*.md', 'SHA256SUMS*', 'requirements.lock', 'pyproject.toml')


def owned(entry: Path) -> bool:
    """Whether an export wrote this entry of the destination: its lock, `current` and its
    swap, a generation, a staging directory, or a name of the flat layout."""
    if entry.name in (CURRENT, '.current-new'):
        return entry.is_symlink()
    if re.fullmatch(r'export-\d+|\.arxiv-export-.+', entry.name):
        return entry.is_dir() and not entry.is_symlink()
    return entry.name == LOCK or any(fnmatchcase(entry.name, pattern) for pattern in FLAT)


def export(work: Path, repo: Path, destination: Path) -> dict:
    """Write a complete export beside the current one, then swap `current` to it under the
    lock; earlier exports and the flat layout's files go once it is current. A destination
    holding anything an export did not write is refused before anything is written there."""
    if destination.is_symlink():
        raise RuntimeError(f'Unsafe backup destination: {destination}')
    if destination.is_dir():
        unknown = sorted(entry.name for entry in destination.iterdir() if not owned(entry))
        if unknown:
            raise RuntimeError(f'Refusing to export into {destination}: it holds entries no '
                               f'export wrote ({", ".join(unknown[:5])})')
    destination.mkdir(parents=True, exist_ok=True)
    with (destination / LOCK).open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        # A killed export (SIGTERM skips cleanup) leaves staging behind.
        for stale in destination.glob('.arxiv-export-*'):
            shutil.rmtree(stale)
        # Staging shares the destination dataset so the final renames stay atomic.
        # Both backups exclude it and only read while holding this lock shared.
        staging = Path(tempfile.mkdtemp(prefix='.arxiv-export-', dir=destination))
        try:
            report = _write(work, repo, staging)
            generation = destination / f'export-{time.time_ns()}'
            os.rename(staging, generation)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        swap = destination / '.current-new'
        swap.unlink(missing_ok=True)
        swap.symlink_to(generation.name, target_is_directory=True)
        os.replace(swap, destination / CURRENT)
        _fsync_directory(destination)
        for entry in destination.iterdir():
            if entry.name in (LOCK, CURRENT, generation.name) or not owned(entry):
                continue
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry)
            else:
                entry.unlink()
        _fsync_directory(destination)
        return report


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write(work: Path, repo: Path, staging: Path) -> dict:
    """The export's files, written into `staging`; returns its report."""
    sources = {'catalog.sqlite': work / 'catalog/catalog.sqlite',
               'progress.sqlite': work / 'state/progress.sqlite'}
    campaigns = campaign_sources(work)
    campaign_databases = [files['manifest.sqlite'] for files in campaigns.values()]
    metadata_files = [source for files in campaigns.values()
                      for name, source in files.items() if name != 'manifest.sqlite']
    metadata_files.extend(p for p in (repo / 'manifests').rglob('*') if p.is_file())
    required = (sum(database_storage_size(p) for p in [*sources.values(), *campaign_databases])
                + sum(p.stat().st_size for p in metadata_files) + 256 * 1024**2)
    if shutil.disk_usage(staging).free < required:
        raise RuntimeError('Insufficient backup storage for fresh database snapshots')
    for name, source in sources.items():
        snapshot_database(source, staging / name)
    (staging / 'campaigns').mkdir()
    for name, files in campaigns.items():
        target = staging / 'campaigns' / name
        target.mkdir()
        snapshot_database(files['manifest.sqlite'], target / 'manifest.sqlite')
        for filename, source in files.items():
            if filename != 'manifest.sqlite':
                shutil.copy2(source, target / filename)
    shutil.copytree(repo / 'manifests', staging / 'manifests')
    for pattern in ('*.md', 'SHA256SUMS*', 'requirements.lock', 'pyproject.toml'):
        for source in repo.glob(pattern):
            if source.is_file() and not source.is_symlink():
                shutil.copy2(source, staging / source.name)
    receipt = work / 'state/job.json'
    if receipt.is_file():
        shutil.copy2(receipt, staging / 'acquisition-job.json')
    report = {'created_at': datetime.now(timezone.utc).isoformat(),
              'databases': {name: str(path) for name, path in sources.items()},
              'campaigns': {
                  name: {'source': str(work / 'campaigns' / name),
                         'files': [str(Path('campaigns') / name / filename) for filename in files]}
                  for name, files in campaigns.items()},
              'restore': f'Restore from the export `{CURRENT}` names. Stop the paper service and campaign runners; restore catalog.sqlite to catalog/catalog.sqlite and progress.sqlite to state/progress.sqlite. Restore campaigns/ to the work directory campaigns/ before resuming a campaign. Restore source and HF payload directories from the selective snapshot. Bulk PDFs are intentionally absent.'}
    (staging / 'export.json').write_text(json.dumps(report, indent=2) + '\n')
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check-storage', action='store_true')
    parser.add_argument('--project', type=Path, required=True, help='Project checkout containing manifests and plans')
    args = parser.parse_args()
    # The export has its own unsnapshotted dataset: daily full rewrites would
    # otherwise pin a catalog-sized copy in every tank/backups snapshot.
    for path, source in ((WORK, 'tank/arxiv-work'), (ARCHIVE, 'bulk/arxiv-archive'),
                         (DESTINATION, 'tank/backups/arxiv-catalog')):
        require_mount(path, source)
    for source in (WORK / 'catalog/catalog.sqlite', WORK / 'state/progress.sqlite', args.project / 'manifests'):
        if not source.exists():
            raise RuntimeError(f'Required backup source missing: {source}')
    if args.check_storage:
        print('Paper catalog backup mounts and sources verified.')
    else:
        print(json.dumps(export(WORK, args.project, DESTINATION), sort_keys=True))


if __name__ == '__main__':
    main()
