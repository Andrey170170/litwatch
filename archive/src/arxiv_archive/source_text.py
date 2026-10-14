"""Text-only source packages: keep a submission's LaTeX and bibliography, drop the rest.

Figures, data and other binaries are already in the PDF, so stored sources keep only
what serves as text. The output is a deterministic tar (sorted members, fixed metadata),
so the same upstream package always yields the same bytes and hash. Parsing is bounded
like reads: decompressed size, member count and tar extension headers are capped.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import re
import tarfile
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from .storage import CHUNK, safe_relative

FILTER = 'text-v1'
TEXT_SUFFIXES = frozenset(
    {
        'tex',
        'ltx',
        'latex',
        'sty',
        'cls',
        'clo',
        'bst',
        'bbl',
        'bib',
        'def',
        'cfg',
        'dtx',
        'ins',
        'fd',
    }
)
# arXiv processing directives (00README, 00README.XXX/.json) and any author README*.
README = re.compile(r'(?:00)?README', re.IGNORECASE)
MAX_MEMBERS = 10_000
# Kept text is buffered to sort it; real submissions hold a few MB at most.
MAX_TEXT_BYTES = 256_000_000


class NoTextSource(ValueError):
    """The package holds no text members; nothing worth storing."""


class BoundedTarInfo(tarfile.TarInfo):
    def _proc_member(self, archive):
        # tarfile reads extension payloads before yielding a regular member.
        # Bound these first to prevent a giant PAX/GNU header allocation.
        if self.type in (
            tarfile.XHDTYPE,
            tarfile.XGLTYPE,
            tarfile.GNUTYPE_LONGNAME,
            tarfile.GNUTYPE_LONGLINK,
        ):
            if self.size > CHUNK:
                raise ValueError('Oversized tar extension header')
        return super()._proc_member(archive)


class BoundedSourceReader:
    """Bound decompressed package bytes, including tar extension headers."""

    def __init__(self, stream, limit):
        self.stream, self.limit, self.count = stream, limit, 0

    def read(self, size=-1):
        # Normal tar streaming uses small blocks. Reject giant extension-header
        # allocations before tarfile can allocate attacker-controlled lengths.
        if size < 0 or size > 16 * CHUNK:
            raise ValueError('Oversized source parser read')
        if self.count + size > self.limit:
            size = self.limit - self.count + 1
        block = self.stream.read(size)
        self.count += len(block)
        if self.count > self.limit:
            raise ValueError('Source package exceeds expansion limit')
        return block

    def seek(self, offset):
        if offset != 0:
            raise ValueError('Unsupported source seek')
        self.stream.seek(0)
        self.count = 0


@contextmanager
def source_stream(package: Path, limit: int):
    """The package's decompressed bytes: gzip is unwrapped, anything else read as is."""
    with package.open('rb') as raw:
        magic = raw.read(2)
        raw.seek(0)
        if magic == b'\x1f\x8b':
            with gzip.GzipFile(fileobj=raw) as stream:
                yield BoundedSourceReader(stream, limit)
        else:
            yield BoundedSourceReader(raw, limit)


def is_tar(raw: BoundedSourceReader) -> bool:
    header = raw.read(512)
    raw.seek(0)
    # tarfile recognizes older tar headers without the ustar marker too.
    try:
        tarfile.TarInfo.frombuf(header, 'utf-8', 'surrogateescape')
        return True
    except (tarfile.HeaderError, ValueError):
        return False


def is_text_path(path: str) -> bool:
    name = PurePosixPath(path).name
    return bool(README.match(name)) or name.rsplit('.', 1)[-1].lower() in TEXT_SUFFIXES


def _media_type(header: bytes) -> str:
    if header.startswith(b'\x1f\x8b'):
        return 'application/gzip'
    if header[257:262] == b'ustar':
        return 'application/x-tar'
    return 'text/x-tex'


def text_package(package: Path, output: Path, limit: int) -> dict:
    """Write `package`'s text members to `output` as a deterministic tar.

    Returns provenance: the filter version, the upstream package's identity and what
    was dropped. A bare (gzipped) file is one LaTeX file, kept as `main.tex` unless it
    is binary. Raises NoTextSource when nothing is kept.
    """
    digest, size = hashlib.sha256(), 0
    with package.open('rb') as raw:
        header = raw.read(1024)
        raw.seek(0)
        for block in iter(lambda: raw.read(CHUNK), b''):
            digest.update(block)
            size += len(block)
    kept: dict[str, bytes] = {}
    dropped, dropped_bytes, total, kept_bytes = 0, 0, 0, 0
    text_limit = min(limit, MAX_TEXT_BYTES)
    with source_stream(package, limit) as raw:
        if is_tar(raw):
            with tarfile.open(fileobj=raw, mode='r|', tarinfo=BoundedTarInfo) as archive:
                for count, member in enumerate(archive, 1):
                    total += member.size
                    if count > MAX_MEMBERS or total > limit:
                        raise ValueError('Source package exceeds expansion limits')
                    if member.isdir():
                        continue
                    try:
                        name = safe_relative(member.name)
                    except ValueError:
                        name = None
                    if name is None or not member.isfile() or not is_text_path(name):
                        dropped += 1
                        dropped_bytes += member.size
                        continue
                    kept_bytes += member.size
                    if kept_bytes > text_limit:
                        raise ValueError('Source text exceeds size limit')
                    with archive.extractfile(member) as f:
                        kept[name] = f.read()  # A repeated name keeps its last copy, as tar does.
        else:
            blocks = []
            for block in iter(lambda: raw.read(CHUNK), b''):
                blocks.append(block)
                kept_bytes += len(block)
                if kept_bytes > text_limit:
                    raise ValueError('Source text exceeds size limit')
            body = b''.join(blocks)
            if b'\0' in body[:8192] or body.startswith((b'%PDF-', b'%!PS')):
                dropped, dropped_bytes = 1, len(body)
            else:
                kept['main.tex'] = body
    if not kept:
        raise NoTextSource(f'Source package has no text members ({dropped} dropped)')
    with tarfile.open(output, 'w', format=tarfile.PAX_FORMAT) as tar:
        for name in sorted(kept):
            entry = tarfile.TarInfo(name)
            entry.size, entry.mode, entry.mtime = len(kept[name]), 0o644, 0
            entry.uid = entry.gid = 0
            entry.uname = entry.gname = ''
            tar.addfile(entry, io.BytesIO(kept[name]))
    return {
        'filter': FILTER,
        'upstream_sha256': digest.hexdigest(),
        'upstream_size': size,
        'upstream_media_type': _media_type(header),
        'kept_members': len(kept),
        'dropped_members': dropped,
        'dropped_bytes': dropped_bytes,
    }
