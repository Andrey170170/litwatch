"""Filesystem publication primitives and verified data-mount guards."""
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess

CHUNK = 1024 * 1024
WORK = Path('/srv/arxiv-work')
ARCHIVE = Path('/srv/arxiv-archive')

def safe_relative(value: str) -> str:
    p = PurePosixPath(value)
    if p.is_absolute() or not p.parts or '..' in p.parts or '\\' in value:
        raise ValueError(f'Unsafe relative path: {value}')
    return str(p)


def sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def mount_guard(path: Path, expected: str) -> None:
    # Check the exact mounted dataset, not merely that an ordinary directory exists.
    result = subprocess.run(['findmnt', '-rn', '-T', str(path), '-o', 'SOURCE,FSTYPE,TARGET'],
                            capture_output=True, text=True, check=True)
    fields = result.stdout.strip().split()
    if fields != [expected, 'zfs', str(path)]:
        raise RuntimeError(f'Wrong or missing data mount at {path}: {result.stdout.strip()}')
    if path.resolve() != path:
        raise RuntimeError(f'Data mount must not be a symlink: {path}')


def storage_guard(work: Path = WORK, archive: Path = ARCHIVE) -> None:
    mount_guard(work, 'tank/arxiv-work')
    mount_guard(archive, 'bulk/arxiv-archive')
    for path, minimum in [(work, 8_000_000_000), (archive, 12_000_000_000)]:
        if shutil.disk_usage(path).free < minimum:
            raise RuntimeError(f'Insufficient free space on {path}; pause and inspect')


