"""Fetch P1's texts from an archive into a local cache, as a full-text pass reads them, so the
trimming can be measured without model calls. Reads the papers from $LITWATCH_DATABASE_URL and the
archive from $LITWATCH_ARCHIVE_URL / $LITWATCH_ARCHIVE_TOKEN; writes OUT/<id>.<source>.txt.

    uv run python evals/2026-10-16-sonnet-briefs/texts.py PAPERS.yaml OUT
"""

import asyncio
import os
import sys
from pathlib import Path

import psycopg
import yaml
from psycopg.rows import dict_row

from litwatch.archive import URGENT, ArchiveClient, read_text
from litwatch.archive.text import TextUnavailable
from litwatch.store import papers as papers_store


async def main(papers_yaml: Path, out: Path) -> None:
    ids = [p["id"] for p in yaml.safe_load(papers_yaml.read_text())["papers"]]
    async with await psycopg.AsyncConnection.connect(
        os.environ["LITWATCH_DATABASE_URL"], row_factory=dict_row
    ) as conn:
        papers = await papers_store.get_many(conn, ids)
    archive = ArchiveClient(
        os.environ["LITWATCH_ARCHIVE_URL"], os.environ["LITWATCH_ARCHIVE_TOKEN"]
    )
    out.mkdir(parents=True, exist_ok=True)
    left = {p.id: p for p in papers if not list(out.glob(f"{p.id}.*.txt"))}
    try:
        while left:
            for pid, paper in list(left.items()):
                try:
                    text = await read_text(archive, paper, URGENT)
                except TextUnavailable as exc:
                    print(f"{pid}: {exc}")
                    del left[pid]
                    continue
                if text is not None:
                    (out / f"{pid}.{text.source}.txt").write_text(text.body, encoding="utf-8")
                    print(f"{pid}: {text.source} v{text.version} {len(text.body)} chars")
                    del left[pid]
            if left:
                print(f"waiting for {len(left)}")
                await asyncio.sleep(20)
    finally:
        await archive.aclose()


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1]), Path(sys.argv[2])))
