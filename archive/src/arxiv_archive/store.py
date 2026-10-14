"""Shared PDF/source store: bounded SSD work, one publication check on HDD.

Sources are stored text-only (see source_text); PDFs keep their original bytes.
"""
from __future__ import annotations

import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import tarfile
import tempfile
import threading
import time

import pyarrow.parquet as pq
import zstandard as zstd

from .storage import CHUNK, storage_guard, safe_relative, sync_directory
from .catalog import Catalog
from .source_text import (FILTER, BoundedTarInfo, NoTextSource, is_tar,
                          source_stream, text_package)

ID = re.compile(r'(?:\d{4}\.\d{4,5}|[a-z][a-z.-]*/\d{7})\Z')


def identity(paper_id: str, version: int | None = None) -> tuple[str, int | None]:
    if not isinstance(paper_id, str) or not ID.fullmatch(paper_id):
        raise ValueError('Expected canonical arXiv ID without version suffix')
    if version is not None and (isinstance(version, bool) or not isinstance(version, int) or version < 0):
        raise ValueError('Version must be a nonnegative integer (0 = unknown historical source)')
    return paper_id, version


class ProcessLock:
    """Reentrant thread lock plus a cooperative cross-process file lock."""
    def __init__(self, path: Path):
        self.path, self.lock, self.depth, self.fd = path, threading.RLock(), 0, None

    def __enter__(self):
        self.lock.acquire()
        try:
            if self.depth == 0:
                self.fd = self.path.open('a+b')
                fcntl.flock(self.fd, fcntl.LOCK_EX)
            self.depth += 1
        except BaseException:
            self.lock.release()
            raise
        return self

    def __exit__(self, *args):
        self.depth -= 1
        if not self.depth:
            self.fd.close()
            self.fd = None
        self.lock.release()


class Store:
    def __init__(self, work=Path('/srv/arxiv-work'), archive=Path('/srv/arxiv-archive'),
                 guard=True, max_bytes=2_000_000_000, cache_bytes=2_000_000_000):
        self.work, self.archive, self.guard = Path(work), Path(archive), guard
        self.max_bytes, self.cache_bytes = max_bytes, cache_bytes
        self._guard()
        self.scratch = self.work / 'service-staging'
        self.cache = self.work / 'reading-cache'
        for path in (self.scratch, self.cache, self.work / 'catalog'):
            path.mkdir(parents=True, exist_ok=True)
        self.catalog = Catalog(self.work / 'catalog/catalog.sqlite')
        self.write_lock = ProcessLock(self.work / 'catalog/writer.lock')
        self.read_lock = ProcessLock(self.work / 'catalog/reader.lock')

    def _guard(self):
        if self.guard:
            storage_guard(self.work, self.archive)

    def _location(self, location: str) -> Path:
        path = self.archive / safe_relative(location)
        if not path.resolve().is_relative_to(self.archive.resolve()):
            raise ValueError('Artifact path escapes archive')
        return path

    def _mkdir(self, path: Path):
        if not path.exists():
            self._mkdir(path.parent)
            path.mkdir(exist_ok=True)
            sync_directory(path.parent)

    @staticmethod
    def _kind(kind):
        if kind not in ('pdf', 'source'):
            raise ValueError('Artifact kind must be pdf or source')

    def put(self, paper_id, version, kind, input_path, provenance=None):
        """Hash while compressing; verify one HDD decompression before publication.

        Input bytes are immutable by identity. Conflicting bytes for an existing
        paper/version/kind are rejected. New revisions get distinct destinations.
        A source package is first reduced to its text members (NoTextSource when
        none remain); provenance then records the upstream package's identity.
        """
        identity(paper_id, version)
        self._kind(kind)
        if not version:
            raise ValueError('Writes require an explicit positive version')
        input_path = Path(input_path)
        if input_path.stat().st_size > self.max_bytes:
            raise ValueError('Artifact exceeds configured size limit')
        with input_path.open('rb') as raw:
            header = raw.read(1024)
        if not header:
            raise ValueError('Empty artifact')
        if kind == 'pdf' and not header.startswith(b'%PDF-'):
            raise ValueError('PDF input does not have a PDF header')
        if kind == 'source' and header.lstrip().lower().startswith((b'<!doctype html', b'<html')):
            raise ValueError('HTML error response is not a source package')
        # Rewrites own `replaced_location`; a client value must never steer deletion.
        provenance = {k: v for k, v in (provenance or {}).items() if k != 'replaced_location'}
        self._guard()
        # A single writer bounds compressed staging to one artifact and prevents
        # conflicting CLI/service publication; network producers have their own cap.
        with self.write_lock:
            self._guard()
            with tempfile.TemporaryDirectory(prefix='write-', dir=self.scratch) as tmp:
                media_type = 'application/pdf'
                if kind == 'source':
                    filtered = Path(tmp) / 'source.tar'
                    provenance.update(text_package(input_path, filtered, self.max_bytes))
                    input_path, media_type = filtered, 'application/x-tar'
                compressed = Path(tmp) / 'payload.zst'
                original_sha, size = self._compress(input_path, compressed)
                previous = self.catalog.get_artifact(paper_id, kind, version)
                if previous and previous.get('status') == 'available':
                    # A source stored before filtering matches by its upstream package.
                    same = (previous['original_sha256'], previous['original_size']) == (original_sha, size)
                    if kind == 'source' and 'filter' not in previous['provenance']:
                        same = (previous['original_sha256'], previous['original_size']) == (
                            provenance['upstream_sha256'], provenance['upstream_size'])
                    if not same:
                        raise FileExistsError('Different bytes already stored for this paper/version/kind')
                    existing = self._location(previous['location'])
                    if existing.is_file() and existing.stat().st_size == previous['stored_size']:
                        return previous
                    raise RuntimeError('Catalog artifact is missing or truncated; explicit repair required')
                encoded_id = paper_id.replace('/', '_')
                bucket = paper_id.split('/')[-1][:4]
                suffix = '.pdf.zst' if kind == 'pdf' else '.source.zst'
                location = f'{"pdf" if kind == "pdf" else "source"}/{bucket}/{encoded_id}v{version}{suffix}'
                target = self._publish(compressed, location, size, original_sha)
                artifact = dict(paper_id=paper_id, version=version, kind=kind,
                                location=location, original_size=size, original_sha256=original_sha,
                                stored_size=target.stat().st_size, stored_sha256=None,
                                compression='zstd', media_type=media_type,
                                provenance=provenance, status='available')
                self.catalog.put_artifact(artifact)
                return self.catalog.get_artifact(paper_id, kind, version)

    def _compress(self, input_path, compressed):
        """zstd-compress input to SSD staging; return its SHA-256 and size."""
        digest, size = hashlib.sha256(), 0
        with input_path.open('rb') as src, compressed.open('wb') as dst:
            with zstd.ZstdCompressor(level=9, write_checksum=True).stream_writer(dst) as enc:
                for block in iter(lambda: src.read(CHUNK), b''):
                    size += len(block)
                    if size > self.max_bytes:
                        raise ValueError('Artifact exceeds configured size limit')
                    digest.update(block)
                    enc.write(block)
        return digest.hexdigest(), size

    def _publish(self, compressed, location, size, sha):
        """Copy staged bytes to their final HDD location once, verified, and return it."""
        target = self._location(location)
        self._guard()
        self._mkdir(target.parent)
        partial = target.with_name(target.name + '.part')
        if not target.exists():
            try:
                with compressed.open('rb') as src, partial.open('wb') as dst:
                    shutil.copyfileobj(src, dst, CHUNK)
                    dst.flush()
                    os.fsync(dst.fileno())
                # One check after the actual HDD write covers both the
                # compressor roundtrip and SSD-to-HDD transfer.
                self._verify_roundtrip(partial, size, sha)
                os.replace(partial, target)
                sync_directory(target.parent)
            finally:
                partial.unlink(missing_ok=True)
        else:
            # Recover a prior crash after rename but before catalog commit.
            self._verify_roundtrip(target, size, sha)
        return target

    def strip_source(self, paper_id, version, dry_run=False):
        """Rewrite one source stored before filtering as text-only, then delete the original.

        The text-only package gets its own location, so a crash leaves either the old
        catalog row and file intact or the new row with an old file that a rerun removes.
        A package without text becomes unavailable and its bytes are deleted; its row
        points at the would-be text location so the same sweep finds the original.
        Readers are held off for the swap and deletion, in the HTTP writers' lock order
        (read, then write), so a request never opens a file deleted under it.
        """
        identity(paper_id, version)
        self._guard()
        with self.read_lock, self.write_lock:
            artifact = self.catalog.get_artifact(paper_id, 'source', version)
            if artifact is None:
                return dict(paper_id=paper_id, version=version, status='missing')
            old = self._location(artifact['location'])
            if 'filter' in artifact['provenance']:
                replaced = artifact['provenance'].get('replaced_location')
                if replaced and not dry_run and self._location(replaced).exists():
                    self._location(replaced).unlink()
                    sync_directory(self._location(replaced).parent)
                return dict(paper_id=paper_id, version=version, status='already-filtered')
            with tempfile.TemporaryDirectory(prefix='strip-', dir=self.scratch) as tmp:
                upstream, filtered = Path(tmp) / 'upstream', Path(tmp) / 'source.tar'
                # Verify the original against the catalog before any path can delete it.
                digest, received = hashlib.sha256(), 0
                with old.open('rb') as src, zstd.ZstdDecompressor().stream_reader(src) as reader, \
                        upstream.open('wb') as out:
                    for block in iter(lambda: reader.read(CHUNK), b''):
                        received += len(block)
                        if received > artifact['original_size']:
                            raise ValueError('Stored source differs from its catalog hash')
                        digest.update(block)
                        out.write(block)
                if (digest.hexdigest(), received) != (artifact['original_sha256'], artifact['original_size']):
                    raise ValueError('Stored source differs from its catalog hash')
                result = dict(paper_id=paper_id, version=version, before=artifact['stored_size'])
                try:
                    stats = text_package(upstream, filtered, self.max_bytes)
                except NoTextSource as exc:
                    stats = None
                    reason = str(exc)
                if dry_run:
                    after = len(zstd.ZstdCompressor(level=9).compress(filtered.read_bytes())) if stats else 0
                    return {**result, 'status': 'dry-run', 'after': after}
                provenance = {**artifact['provenance'], 'replaced_location': artifact['location']}
                location = artifact['location'].removesuffix('.source.zst') + '.source-text.zst'
                if stats is None:
                    self.catalog.put_artifact({**artifact, 'status': 'unavailable', 'location': location,
                                               'provenance': {**provenance, 'filter': FILTER,
                                                              'unavailable': reason}})
                    result.update(status='unavailable', after=0)
                else:
                    compressed = Path(tmp) / 'payload.zst'
                    sha, size = self._compress(filtered, compressed)
                    target = self._publish(compressed, location, size, sha)
                    self.catalog.put_artifact({**artifact, 'location': location, 'original_size': size,
                                               'original_sha256': sha, 'stored_size': target.stat().st_size,
                                               'media_type': 'application/x-tar',
                                               'provenance': {**provenance, **stats}})
                    result.update(status='filtered', after=target.stat().st_size)
                old.unlink(missing_ok=True)
                sync_directory(old.parent)
                return result

    def sweep_replaced_sources(self):
        """Delete original packages a text-only rewrite replaced but a crash left behind.

        Only the original path a rewrite derives from its own new location qualifies, so
        no recorded value can point the sweep at another artifact.
        """
        removed = 0
        self._guard()
        with self.write_lock:
            for row in self.catalog.replaced_sources():
                location, replaced = row['location'], row['replaced']
                if not location.endswith('.source-text.zst') or \
                        replaced != location.removesuffix('.source-text.zst') + '.source.zst':
                    continue
                path = self._location(replaced)
                if path.exists():
                    path.unlink()
                    sync_directory(path.parent)
                    removed += 1
        return removed

    @staticmethod
    def _verify_roundtrip(path, size, sha):
        digest, received = hashlib.sha256(), 0
        with path.open('rb') as src, zstd.ZstdDecompressor().stream_reader(src) as reader:
            for block in iter(lambda: reader.read(CHUNK), b''):
                received += len(block)
                if received > size:
                    raise ValueError('Stored artifact expands beyond expected size')
                digest.update(block)
        if received != size or digest.hexdigest() != sha:
            raise ValueError('Stored artifact failed publication verification')

    def _prune(self, required, keep=None):
        if required > self.cache_bytes:
            raise ValueError('Artifact exceeds reading cache capacity')
        files = sorted((p for p in self.cache.iterdir() if p.is_file() and p != keep),
                       key=lambda p: p.stat().st_mtime)
        used = sum(p.stat().st_size for p in files)
        for p in files:
            if used + required <= self.cache_bytes:
                break
            used -= p.stat().st_size
            p.unlink()

    def get(self, paper_id, kind='pdf', version=None):
        identity(paper_id, version)
        self._kind(kind)
        self._guard()
        with self.read_lock:
            artifact = self.catalog.get_artifact(paper_id, kind, version)
            if artifact and artifact.get('status') == 'available':
                source = self._location(artifact['location'])
                if source.stat().st_size != artifact['stored_size']:
                    raise ValueError('Stored artifact size differs from catalog')
                ext = '.pdf' if kind == 'pdf' else '.source'
                target = self.cache / (artifact['original_sha256'] + ext)
                if target.exists() and target.stat().st_size == artifact['original_size']:
                    target.touch()
                    return target
                self._prune(artifact['original_size'], target)
                partial = target.with_suffix(target.suffix + '.part')
                try:
                    received, digest = 0, hashlib.sha256()
                    with source.open('rb') as src, zstd.ZstdDecompressor().stream_reader(src) as reader, partial.open('wb') as out:
                        for block in iter(lambda: reader.read(CHUNK), b''):
                            received += len(block)
                            if received > artifact['original_size']:
                                raise ValueError('Artifact expands beyond catalog size')
                            digest.update(block)
                            out.write(block)
                    if digest.hexdigest() != artifact['original_sha256']:
                        raise ValueError('Decompressed artifact differs from catalog hash')
                    if received != artifact['original_size']:
                        raise ValueError('Decompressed artifact size differs from catalog')
                    os.replace(partial, target)
                finally:
                    partial.unlink(missing_ok=True)
                return target
            if kind == 'source' and version in (None, 0):
                return self._historical_bundle(paper_id)
            raise FileNotFoundError(f'No available {kind} for {paper_id} version {version}')

    def _latex_rows(self, paper_id, content=False):
        locations = self.catalog.latex_locations(paper_id)
        if not locations:
            raise FileNotFoundError(f'No historical LaTeX for {paper_id}')
        columns = ['paper_id', 'path', 'size'] + (['content'] if content else [])
        # Process one bounded batch, not an entire multi-GB content row group.
        for locator in locations:
            shard = self._location(locator['shard'])
            for batch in pq.ParquetFile(shard).iter_batches(batch_size=8 if content else 8192,
                                                            row_groups=[locator['row_group']], columns=columns):
                for row in batch.to_pylist():
                    if row['paper_id'] == paper_id:
                        row['path'] = safe_relative(row['path'])
                        yield row

    def _historical_bundle(self, paper_id):
        listing = list(self._latex_rows(paper_id))
        # Tar headers/padding; original sizes are from the immutable index.
        estimated = sum(int(r['size']) + 2048 for r in listing) + 10240
        target = self.cache / (paper_id.replace('/', '_') + '-historical.tar')
        if target.exists():
            target.touch()
            return target
        self._prune(estimated, target)
        partial = target.with_suffix('.part')
        total = 0
        try:
            with tarfile.open(partial, 'w') as bundle:
                for row in self._latex_rows(paper_id, content=True):
                    payload = row['content']
                    if isinstance(payload, str):
                        payload = payload.encode()
                    total += len(payload)
                    if total > self.max_bytes:
                        raise ValueError('Historical source exceeds size limit')
                    entry = tarfile.TarInfo(row['path'])
                    entry.size, entry.mode, entry.mtime = len(payload), 0o644, 0
                    bundle.addfile(entry, io.BytesIO(payload))
            if partial.stat().st_size > self.cache_bytes:
                raise ValueError('Historical source exceeds cache limit')
            os.replace(partial, target)
        finally:
            partial.unlink(missing_ok=True)
        return target

    def _package_entries(self, package, wanted=None):
        """Inspect or read safe regular members without filesystem extraction."""
        total, count = 0, 0
        with source_stream(package, self.max_bytes) as raw:
            if is_tar(raw):
                with tarfile.open(fileobj=raw, mode='r|', tarinfo=BoundedTarInfo) as archive:
                    for member in archive:
                        count += 1
                        total += member.size
                        if count > 10000 or total > self.max_bytes:
                            raise ValueError('Source package exceeds expansion limits')
                        name = safe_relative(member.name)
                        if not member.isfile():
                            if member.isdir():
                                continue
                            raise ValueError('Source package contains non-regular member')
                        if wanted is None:
                            yield dict(path=name, size=member.size)
                        elif name == wanted:
                            with archive.extractfile(member) as f:
                                for block in iter(lambda: f.read(CHUNK), b''):
                                    yield block
                            return
            else:
                if wanted is None:
                    # Count decompressed size with a limit, do not retain it in RAM.
                    size = 0
                    for block in iter(lambda: raw.read(CHUNK), b''):
                        size += len(block)
                        if size > self.max_bytes:
                            raise ValueError('Source exceeds expansion limit')
                    yield dict(path='main.tex', size=size)
                elif wanted == 'main.tex':
                    for block in iter(lambda: raw.read(CHUNK), b''):
                        yield block

    def sources(self, paper_id, version=None):
        identity(paper_id, version)
        self._guard()
        with self.read_lock:
            artifact = self.catalog.get_artifact(paper_id, 'source', version)
            if artifact and artifact.get('status') == 'available':
                return list(self._package_entries(self.get(paper_id, 'source', version)))
            if version in (None, 0):
                return [dict(path=row['path'], size=row['size']) for row in self._latex_rows(paper_id)]
            raise FileNotFoundError('Source not available')

    def source_file(self, paper_id, path, version=None):
        identity(paper_id, version)
        path = safe_relative(path)
        self._guard()
        with self.read_lock:
            artifact = self.catalog.get_artifact(paper_id, 'source', version)
            if artifact and artifact.get('status') == 'available':
                package = self.get(paper_id, 'source', version)
                listing = list(self._package_entries(package))
                matching = [entry for entry in listing if entry['path'] == path]
                if not matching:
                    raise FileNotFoundError(path)
                expected_size = matching[0]['size']
                blocks = self._package_entries(package, path)
            elif version in (None, 0):
                matching = [row for row in self._latex_rows(paper_id) if row['path'] == path]
                if not matching:
                    raise FileNotFoundError(path)
                expected_size = matching[0]['size']
                def historical_blocks():
                    for row in self._latex_rows(paper_id, content=True):
                        if row['path'] == path:
                            payload = row['content']
                            if isinstance(payload, str):
                                payload = payload.encode()
                            yield payload
                            return
                blocks = historical_blocks()
            else:
                raise FileNotFoundError(path)
            self._prune(expected_size)
            digest, size = hashlib.sha256(), 0
            with tempfile.NamedTemporaryFile(dir=self.cache, suffix='.part', delete=False) as out:
                partial = Path(out.name)
                try:
                    for block in blocks:
                        size += len(block)
                        if size > expected_size or size > self.max_bytes:
                            raise ValueError('Source member exceeds declared size')
                        digest.update(block)
                        out.write(block)
                    if size != expected_size:
                        raise ValueError('Source member size differs from manifest')
                    out.close()
                    target = self.cache / (digest.hexdigest() + '.tex')
                    os.replace(partial, target)
                    return target
                finally:
                    partial.unlink(missing_ok=True)

    def status(self):
        self._guard()
        return dict(catalog=self.catalog.stats(),
                    storage={str(p): dict(free=shutil.disk_usage(p).free) for p in (self.work, self.archive)},
                    cache_bytes=sum(p.stat().st_size for p in self.cache.iterdir() if p.is_file()))

    def close(self):
        self.catalog.close()
