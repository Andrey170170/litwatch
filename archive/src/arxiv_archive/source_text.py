"""Text-only source packages: keep a submission's LaTeX and bibliography, drop the rest.

Figures, data and other binaries are already in the PDF, so stored sources keep only
what serves as text. The output is a deterministic tar (sorted members, fixed metadata),
so the same upstream package always yields the same bytes and hash. Parsing is bounded
like reads: decompressed size, member count and tar extension headers are capped.

`latex_text` reads a package back as the paper's text: its main file with the files it
inputs inlined, comments dropped and the bibliography in place, which is what a reader
of the full text (litwatch's Briefs, Deep cards) is given.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import posixpath
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


# The files a paper's text is read from, and how they pull each other in: `\input{name}`,
# `\include{name}`, `\subfile{name}`, TeX's bare `\input name`, and the import package's
# `\import{dir}{name}` and `\subimport{dir}{name}`.
LATEX_SUFFIXES = ('.tex', '.ltx', '.latex')
INPUT = re.compile(r'\\(?:input|include|subfile)\s*\{([^}]*)\}|\\input\s+([^\s{}\\%]+)'
                   r'|\\(?:sub)?import\*?\s*\{([^}]*)\}\s*\{([^}]*)\}')
BIBLIOGRAPHY = re.compile(r'\\bibliography\s*\{[^}]*\}')
# A `%` after an even run of backslashes (none, or a `\\\\` line break) starts a comment.
COMMENT = re.compile(r'((?:^|[^\\])(?:\\\\)*)%.*')
MAX_INPUT_DEPTH = 8


def _input(match: re.Match) -> str:
    """The file an INPUT match names, relative as written."""
    if match.group(3) is not None:
        return posixpath.join(match.group(3).strip(), match.group(4).strip())
    return match.group(1) or match.group(2)


def _decode(body: bytes) -> str:
    try:
        text = body.decode('utf-8')
    except UnicodeDecodeError:
        text = body.decode('latin-1')  # older submissions; every byte decodes
    return '\n'.join(COMMENT.sub(r'\1', line) for line in text.splitlines())


def _members(package: Path, limit: int) -> dict[str, str]:
    """The package's LaTeX and .bbl files by path, decoded and with comments dropped. A bare
    file is `main.tex`."""
    found: dict[str, str] = {}
    total = 0
    with source_stream(package, limit) as raw:
        if not is_tar(raw):
            blocks, size = [], 0
            for block in iter(lambda: raw.read(CHUNK), b''):
                size += len(block)
                if size > MAX_TEXT_BYTES:
                    raise ValueError('Source text exceeds size limit')
                blocks.append(block)
            return {'main.tex': _decode(b''.join(blocks))}
        with tarfile.open(fileobj=raw, mode='r|', tarinfo=BoundedTarInfo) as archive:
            for count, member in enumerate(archive, 1):
                total += member.size
                if count > MAX_MEMBERS or total > min(limit, MAX_TEXT_BYTES):
                    raise ValueError('Source package exceeds expansion limits')
                if not member.isfile():
                    continue
                try:
                    name = safe_relative(member.name)
                except ValueError:
                    continue
                if name.lower().endswith(LATEX_SUFFIXES + ('.bbl',)):
                    with archive.extractfile(member) as f:
                        found[name] = _decode(f.read())
    return found


def _mains(files: dict[str, str]) -> list[str]:
    """The files that start a document: one no other file inputs (a preamble, or a subfile
    with a \\documentclass of its own, is not it), then one that begins the document (its
    \\documentclass may sit in a preamble it inputs, so it is not asked for), else one with
    \\documentclass, then the shallowest path. Independent documents that tie (a paper and its supplement) are all kept,
    in path order, as arXiv compiles them into one PDF: picking one by name could drop the
    paper."""
    latex = [n for n in files if n.lower().endswith(LATEX_SUFFIXES)]
    inputs = set()
    for here in latex:
        for match in INPUT.finditer(files[here]):
            inputs.update(_candidates(_input(match), '', here))

    def rank(n: str) -> tuple:
        begins = '\\begin{document}' in files[n]
        return (n in inputs, not begins, not begins and '\\documentclass' not in files[n],
                n.count('/'))
    roots = [n for n in latex if '\\begin{document}' in files[n] or '\\documentclass' in files[n]]
    best = min(map(rank, roots), default=None)
    return sorted(n for n in roots if rank(n) == best)


def _inside(path: str) -> str | None:
    """`path` with `.` and `..` resolved, or None when it leaves the package."""
    path = posixpath.normpath(path)
    return None if path.startswith(('../', '/')) or path == '..' else path


def _candidates(name: str, main: str, here: str) -> list[str]:
    """Where an input of `name` from file `here` may be, in the order TeX would look: beside the
    main file (its working directory), then, as authors often write, beside `here`."""
    found = []
    for base in (posixpath.dirname(main), posixpath.dirname(here)):
        for candidate in (name.strip(), name.strip() + '.tex'):
            if (path := _inside(posixpath.join(base, candidate))) is not None:
                found.append(path)
    return found


def latex_text(package: Path, limit: int) -> str:
    """The paper's text from its source package: the main file with every input inlined (each
    file once, at most `MAX_INPUT_DEPTH` deep), comments dropped, and `\\bibliography` replaced
    by the main file's .bbl (arXiv names it after the main file) where there is one; another
    document's .bbl in the package is never taken. Several independent documents follow each
    other in path order (`_mains`). Without a main file, every LaTeX file in path order. Raises NoTextSource when the package holds no LaTeX."""
    files = _members(package, limit)
    latex = sorted(n for n in files if n.lower().endswith(LATEX_SUFFIXES))
    if not latex:
        raise NoTextSource('Source package has no LaTeX files')
    mains = _mains(files)
    if not mains:
        return '\n\n'.join(files[n] for n in latex).strip() + '\n'
    used = set(mains)

    def document(main: str) -> str:
        def resolve(name: str, here: str) -> str | None:
            return next((p for p in _candidates(name, main, here) if p in files and p not in used), None)

        def expand(body: str, here: str, depth: int) -> str:
            def inline(match: re.Match) -> str:
                path = resolve(_input(match), here) if depth < MAX_INPUT_DEPTH else None
                if path is None:
                    return ''
                used.add(path)
                return expand(files[path], path, depth + 1)
            return INPUT.sub(inline, body)

        text = expand(files[main], main, 0)
        bbl = files.get(str(PurePosixPath(main).with_suffix('.bbl')))
        if bbl is not None:
            if BIBLIOGRAPHY.search(text):
                text = BIBLIOGRAPHY.sub(lambda _: bbl, text, count=1)
                text = BIBLIOGRAPHY.sub('', text)
            elif '\\begin{thebibliography}' not in text:
                text += '\n' + bbl
        return text

    text = '\n\n'.join(document(main) for main in mains)
    return re.sub(r'\n{3,}', '\n\n', text).strip() + '\n'
