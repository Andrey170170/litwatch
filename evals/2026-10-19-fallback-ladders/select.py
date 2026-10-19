"""Draw the fallback replay's cases (docs/plans/failure-handling.md, P1) into cases.csv.

Run from the repository root, with the scratch sandbox copied from production on 2026-10-19
(`fh_p1_sandbox`) and the host archive (read only: catalog, search and text, never a job):

    set -a; . <main checkout>/.env.v1; set +a
    LITWATCH_DATABASE_URL=<dev DSN for fh_p1_sandbox> uv run python \
        evals/2026-10-19-fallback-ladders/select.py

The quality set, ordinary papers:
- `judge` and `check`: every paper-topic pair the owner labelled central, related or adjacent in
  evals/labels/fit.csv (cohort `owner`), and 60 of those labelled none, drawn with SEED.
  Production holds no Calibrate session and no label, so these are all the owner's labels.
- `field_grade`: 60 Field verdicts of production, drawn with SEED, each paper's topic.
- `analyse`: 60 papers of production, drawn with SEED, each in its primary class as analysis
  judges it.
- `brief`: the 10 papers of the preference set (evals/preferences/briefs.yaml, the owner's
  hand-read Focus papers), each in its area there.
- `writer`: five daily digests of production, two Focus and three Area, chosen by hand to span
  one to 56 papers.

The refusals set: the three papers refused so far, and up to five papers per theme from the
archive's search (biosecurity, chemical weapons and CBRN, cyber offence, jailbreaks), the first
hits of 2026 whose text or PDF the archive holds, each asked of every role but `writer`: graded for
alignment-and-safety (the Area topic the two were refused in) by `judge` and `check`, for
language-models by `field_grade`, and judged by `analyse` and `brief` in its theme's class (the
classes the three refused papers were sorted into).
"""

import csv
import os
import random
import sys
from pathlib import Path

import httpx
import psycopg
import yaml
from psycopg.rows import dict_row

HERE = Path(__file__).parent
SEED = 20261019
NONE_DRAWN = 60
DRAWN = 60
# circuits 10-16, cot-faithfulness 10-16, cot-monitoring 10-19, interpretability 10-16,
# alignment-and-safety 10-19
DIGESTS = (23, 21, 26, 24, 30)
# The papers refused so far, and the class each was sorted into.
REFUSED = {
    "2610.13116": "safety-evaluations",
    "2610.21200": "model-security",
    "2610.17907": "jailbreaks",
}
JUDGED = "Refused by `judge` (Opus 5.5), `refused: bio`, on the nights of 2026-10-17 to 19."
NOTES = {
    "2610.13116": JUDGED,
    "2610.21200": JUDGED,
    "2610.17907": "Refused by Opus 5.5 for the Brief in N6 (cyber); Sonnet 5.5 answered it then.",
}
THEMES = {  # the queries, in order, and the class each theme's papers are judged in
    "biosecurity": (
        ("biosecurity", "biological weapons", "dual-use biology"),
        "safety-evaluations",
    ),
    "chemical weapons and CBRN": (
        ("chemical weapons", "CBRN", "hazardous chemistry"),
        "safety-evaluations",
    ),
    "cyber offence": (
        ("malware generation LLM", "exploit generation vulnerability agent", "offensive cyber"),
        "model-security",
    ),
    "jailbreaks": (
        ("jailbreak attack harmful", "jailbreak prompt large language model"),
        "jailbreaks",
    ),
}
PER_THEME = 5
COLUMNS = ("set", "role", "paper", "subject", "label", "note")


def owner_pairs() -> list[dict]:
    with open("evals/labels/fit.csv", newline="") as f:
        rows = [r for r in csv.DictReader(f) if r["cohort"] == "owner" and r["fit"] != "unsure"]
    labelled = [r for r in rows if r["fit"] != "none"]
    nones = sorted(
        (r for r in rows if r["fit"] == "none"), key=lambda r: (r["paper_id"], r["topic"])
    )
    chosen = labelled + random.Random(SEED).sample(nones, NONE_DRAWN)
    chosen.sort(key=lambda r: (r["topic"], r["paper_id"]))
    return [
        {"set": "quality", "role": role, "paper": r["paper_id"], "subject": r["topic"],
         "label": r["fit"]}
        for role in ("judge", "check")
        for r in chosen
    ]  # fmt: skip


def drawn(conn: psycopg.Connection, query: str) -> list[dict]:
    rows = conn.execute(query).fetchall()
    return random.Random(SEED).sample(rows, min(DRAWN, len(rows)))


def production(conn: psycopg.Connection) -> list[dict]:
    fields = drawn(
        conn,
        # One row a pair, however many verdicts it has at the revision (the sandbox had none
        # with two, so the draw is the one cases.csv holds).
        """SELECT DISTINCT ON (v.paper_id, v.topic) v.paper_id, v.topic FROM verdicts v
           JOIN topics t ON t.class = v.topic
           JOIN papers p ON p.id = v.paper_id AND p.revision = v.paper_revision
           WHERE t.depth = 'field' AND v.opinions ? 'field_grade'
           ORDER BY v.paper_id, v.topic, v.id DESC""",
    )
    analysed = drawn(
        conn,
        """SELECT DISTINCT ON (a.paper_id) a.paper_id, a.primary_class FROM analyses a
           JOIN papers p ON p.id = a.paper_id AND p.revision = a.paper_revision
           ORDER BY a.paper_id, a.id DESC""",
    )
    found = [
        {"set": "quality", "role": "field_grade", "paper": r["paper_id"], "subject": r["topic"]}
        for r in sorted(fields, key=lambda r: (r["paper_id"], r["topic"]))
    ]
    found += [
        {"set": "quality", "role": "analyse", "paper": r["paper_id"],
         "subject": r["primary_class"]}
        for r in sorted(analysed, key=lambda r: r["paper_id"])
    ]  # fmt: skip
    briefs = yaml.safe_load(Path("evals/preferences/briefs.yaml").read_text())["papers"]
    found += [
        {"set": "quality", "role": "brief", "paper": e["id"], "subject": e["area"]} for e in briefs
    ]
    found += [{"set": "quality", "role": "writer", "subject": str(d)} for d in DIGESTS]
    return found


def searched(archive: httpx.Client) -> list[tuple[str, str, str]]:
    """(paper, theme, class) for up to PER_THEME papers a theme, first hits first, each with
    its latest version's LaTeX text or PDF in the archive (what `stored_text` reads)."""
    seen = set(REFUSED)
    found = []
    for theme, (queries, area) in THEMES.items():
        kept = 0
        for q in queries:
            params = {"q": q, "limit": 20, "date_from": "2026-01-01"}
            for item in archive.get("/v1/search", params=params).raise_for_status().json()["items"]:
                pid = item["id"]
                if kept == PER_THEME or pid in seen:
                    continue
                record = archive.get(f"/v1/papers/{pid}")
                version = record.json().get("latest_version") if record.is_success else None
                if not version:
                    continue
                held = (
                    archive.get(f"/v1/papers/{pid}/{kind}", params={"version": version})
                    for kind in ("text", "pdf")
                )
                if not any(r.is_success for r in held):
                    continue
                seen.add(pid)
                found.append((pid, theme, area))
                kept += 1
        print(f"{theme}: {kept} papers", file=sys.stderr)
    return found


def refusals(found: list[tuple[str, str, str]]) -> list[dict]:
    rows = []
    for pid, theme, area in [(p, None, a) for p, a in REFUSED.items()] + found:
        note = NOTES.get(pid) or f"From the archive's search, theme {theme}."
        for role, subject in (
            ("judge", "alignment-and-safety"),
            ("check", "alignment-and-safety"),
            ("field_grade", "language-models"),
            ("analyse", area),
            ("brief", area),
        ):
            rows.append(
                {"set": "refusals", "role": role, "paper": pid, "subject": subject, "note": note}
            )
    return rows


def main() -> None:
    token = Path(os.environ["LITWATCH_ARCHIVE_TOKEN_FILE"]).read_text().strip()
    archive = httpx.Client(
        base_url=os.environ["LITWATCH_ARCHIVE_URL"],
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    with psycopg.connect(os.environ["LITWATCH_DATABASE_URL"], row_factory=dict_row) as conn:
        rows = owner_pairs() + production(conn)
    rows += refusals(searched(archive))
    with (HERE / "cases.csv").open("w", newline="") as f:
        out = csv.DictWriter(f, COLUMNS)
        out.writeheader()
        out.writerows(rows)
    print(f"{len(rows)} cases", file=sys.stderr)


if __name__ == "__main__":
    main()
