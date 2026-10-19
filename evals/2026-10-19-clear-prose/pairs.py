"""Write pairs.md: each replayed text beside production's, for the owner's read.

Production's Briefs are read from the clear_prose copy; the Gists and digests carry their own.

    uv run python evals/2026-10-19-clear-prose/pairs.py
"""

from __future__ import annotations

import json
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from litwatch import config as configuration

HERE = Path(__file__).parent


def _lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _words(text: str | None) -> int:
    return len((text or "").split())


def main() -> None:
    briefs = {r["paper_id"]: r["output"] for r in _lines(HERE / "round2.jsonl") if r.get("output")}
    with psycopg.connect(configuration.database_url(), row_factory=dict_row) as conn:
        old = {
            r["paper_id"]: r
            for r in conn.execute(
                """SELECT DISTINCT ON (b.paper_id) b.paper_id, p.title, b.text, b.overstatement,
                          g.text AS gist, w.weight
                   FROM briefs b JOIN papers p ON p.id = b.paper_id
                   LEFT JOIN gists g ON g.pass_id = b.pass_id
                   LEFT JOIN weights w ON w.pass_id = b.pass_id
                   WHERE b.paper_id = ANY(%s) ORDER BY b.paper_id, b.at DESC""",
                (list(briefs),),
            )
        }
    out = ["# The clear-prose replay, side by side", ""]
    out += ["Production's text first, then the new prompt's. Word counts in brackets.", ""]
    out += ["## Briefs and their Gists", ""]
    for pid, new in briefs.items():
        was = old[pid]
        out += [f"### {was['title']} ({pid})", ""]
        out += [f"**Before** [{_words(was['text'])}], Weight {was['weight']}", ""]
        if was["overstatement"]:
            out += [f"> {was['overstatement']}", ""]
        out += [was["text"], "", f"*Gist:* {was['gist']}", ""]
        out += [f"**After** [{_words(new['brief'])}], Weight {new['weight']}", ""]
        if new["overstatement"]:
            out += [f"> {new['overstatement']}", ""]
        out += [new["brief"], "", f"*Gist:* {new['gist']}", ""]
    out += ["## Gists from the abstract", ""]
    for r in _lines(HERE / "gists.jsonl"):
        out += [f"### {r['title']} ({r['paper_id']})", ""]
        out += [
            f"- **Before** [{_words(r['old']['gist'])}], {r['old']['weight']}: {r['old']['gist']}"
        ]
        out += [
            f"- **After** [{_words(r['new']['gist'])}], {r['new']['weight']}: {r['new']['gist']}",
            "",
        ]
    out += ["## Digests", ""]
    for r in _lines(HERE / "digests.jsonl"):
        out += [f"### {r['topic']}, {r['period']} of {r['day']}", ""]
        for label, d in (("Before", r["old"]), ("After", r["new"])):
            out += [f"**{label}** (Gist [{_words(d['gist'])}], text [{_words(d['text'])}])", ""]
            out += [f"*Gist:* {d['gist']}", "", d["text"] or "", ""]
    (HERE / "pairs.md").write_text("\n".join(out), encoding="utf-8")


if __name__ == "__main__":
    main()
