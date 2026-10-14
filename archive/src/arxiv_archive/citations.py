"""Deterministic preprint BibTeX; published identifiers remain separate fields."""
from __future__ import annotations

import re
import unicodedata

from .catalog import validate_id
from .search import normalize_paper


def escape(value: object) -> str:
    # Decode common author-name TeX accents, then quote all remaining commands.
    accents = {"'": '\u0301', '`': '\u0300', '^': '\u0302', '"': '\u0308',
               '~': '\u0303', '=': '\u0304', 'v': '\u030c', 'c': '\u0327'}
    text = re.sub(r"\\(['`^\"~=vc])(?:\{([A-Za-z])\}|([A-Za-z]))",
                  lambda m: unicodedata.normalize('NFC', (m[2] or m[3]) + accents[m[1]]), str(value))
    replacements = {'\\': r'\textbackslash{}', '{': r'\{', '}': r'\}',
                    '%': r'\%', '&': r'\&', '#': r'\#', '_': r'\_',
                    '$': r'\$', '~': r'\textasciitilde{}', '^': r'\textasciicircum{}'}
    return ''.join(replacements.get(c, c) for c in ' '.join(text.split()))


def raw_authors(value: str) -> str:
    """Split recognizable full-name lists; preserve ambiguous strings as literals."""
    pieces = [part.strip() for part in re.split(r',\s*(?:and\s+)?|\s+and\s+', str(value)) if part.strip()]
    if len(pieces) > 1 and all(len(part.split()) >= 2 for part in pieces):
        return ' and '.join(escape(part) for part in pieces)
    escaped = escape(value)
    return '{' + escaped + '}' if ',' in str(value) else escaped


def bibtex(paper: dict) -> str:
    paper_id = paper['id']
    validate_id(paper_id)
    meta = paper['metadata']
    # HF provides parsed names; preserve family/given separation when available.
    parsed = meta.get('authors_parsed')
    if parsed and isinstance(parsed, list):
        names = []
        for name in parsed:
            if not isinstance(name, list) or not name:
                continue
            family, given, suffix = (name + ['', ''])[:3]
            names.append(', '.join(escape(p) for p in (family, suffix, given) if p))
        authors = ' and '.join(names)
    elif isinstance(meta.get('authors'), list):
        authors = ' and '.join(escape(a.get('name') or ' '.join(filter(None, [a.get('forenames'), a.get('keyname')])))
                               if isinstance(a, dict) else raw_authors(a) for a in meta['authors'])
    else:
        authors = raw_authors(meta.get('authors') or meta.get('authors_raw') or '')
    published = normalize_paper(paper)['published']
    fields = [('title', escape(meta.get('title') or paper_id)), ('author', authors)]
    if published:
        fields.append(('year', published[:4]))
    fields += [('eprint', paper_id), ('archivePrefix', 'arXiv')]
    categories = meta.get('categories') or []
    if isinstance(categories, str):
        categories = categories.split()
    if categories:
        fields.append(('primaryClass', escape(categories[0])))
    if meta.get('doi'):
        fields.append(('doi', escape(meta['doi'])))
    if meta.get('journal-ref') or meta.get('journal_ref'):
        fields.append(('note', escape(meta.get('journal-ref') or meta['journal_ref'])))
    fields.append(('url', 'https://arxiv.org/abs/' + paper_id))
    return '@misc{arxiv:' + paper_id.replace('/', '_') + ',\n' + ',\n'.join(
        f'  {key} = {{{value}}}' for key, value in fields if value) + '\n}\n'
