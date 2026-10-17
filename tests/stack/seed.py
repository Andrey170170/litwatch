"""Seed the smoke test's empty archive with real papers (`papers.json`, as the archive lists them)
and a LaTeX source for each, built from its title and abstract: the stack must not download from
arXiv in CI. Runs inside the litwatch container ($LITWATCH_ARCHIVE_URL, $LITWATCH_ARCHIVE_TOKEN);
litwatch then reads the day's papers from the archive (`litwatch intake archive`)."""

from __future__ import annotations

import gzip
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path


def call(method: str, path: str, body: bytes, kind: str) -> dict:
    request = urllib.request.Request(
        os.environ["LITWATCH_ARCHIVE_URL"] + path,
        data=body,
        method=method,
        headers={
            "Authorization": f"Bearer {os.environ['LITWATCH_ARCHIVE_TOKEN']}",
            "Content-Type": kind,
        },
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def latex(item: dict) -> bytes:
    """A one-file source package, gzipped as arXiv serves one."""
    text = (
        "\\documentclass{article}\n"
        f"\\title{{{item['title']}}}\n"
        "\\begin{document}\n\\maketitle\n"
        f"\\begin{{abstract}}\n{item['abstract']}\n\\end{{abstract}}\n"
        "\\section{Introduction}\n"
        "This text was built by litwatch's stack smoke test from the paper's abstract.\n"
        "\\end{document}\n"
    )
    return gzip.compress(text.encode())


def main(fixture: Path) -> None:
    items = json.loads(fixture.read_text())
    for item in items:
        path = "/v1/papers/" + urllib.parse.quote(item["id"], safe="")
        metadata = {k: v for k, v in item.items() if v is not None}
        call(
            "PUT",
            path,
            json.dumps({"metadata": metadata, "latest_version": item["version"]}).encode(),
            "application/json",
        )
        call("PUT", f"{path}/source?version={item['version']}", latex(item), "application/gzip")
    print(f"seeded {len(items)} papers, published {sorted({i['published'] for i in items})}")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
