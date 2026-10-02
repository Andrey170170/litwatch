"""Deep processing: hand tier 1 papers to litwatch-deep, keep the outcome, write it to Zotero.

litwatch owns all state. The deep service is a stateless job runner: it gets the paper text, the
selections that placed the paper in tier 1, and the card extensions those selections ask for; it
returns the validated result with a trace and token stats. litwatch stores them, writes the
`litwatch:enrichment` note and `litwatch:*` tags, and adds the item to `Inbox` when one of the
tier 1 selections is on the personal list.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx
import psycopg

from litwatch import db
from litwatch.models import Paper
from litwatch.selection import Selection
from litwatch.text import BodyFetcher
from litwatch.zotero import ENRICH_MARKER, ENRICH_TAG, ZoteroClient, _esc

log = logging.getLogger(__name__)

TAG_PREFIXES = (
    "litwatch:kind:",
    "litwatch:monitor:",
    "litwatch:signal:",
    "litwatch:mismatch:",
    "litwatch:ground-truth:",
    "litwatch:threat:",  # pre-0.6 cards; cleared on rewrite
    "litwatch:code",
    "litwatch:processed",
)


@dataclass
class Outcome:
    result: dict[str, Any] | None
    trace: list[dict[str, Any]]
    stats: dict[str, Any]
    model: str
    error: str | None = None


@dataclass
class ProcessSummary:
    processed: int = 0
    failed: int = 0
    list_cost: float = 0.0
    failures: list[tuple[str, str]] = field(default_factory=list)


class DeepClient:
    """HTTP client for litwatch-deep: submit a job, follow its progress, collect the outcome."""

    def __init__(
        self,
        base_url: str,
        http: httpx.AsyncClient | None = None,
        poll_seconds: float = 15.0,
        timeout_seconds: float = 45 * 60,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.http = http or httpx.AsyncClient(timeout=60.0)
        self.poll_seconds = poll_seconds
        self.timeout_seconds = timeout_seconds

    async def health(self) -> dict[str, Any]:
        resp = await self.http.get(f"{self.base_url}/healthz")
        resp.raise_for_status()
        return resp.json()

    async def jobs(self) -> list[dict[str, Any]]:
        resp = await self.http.get(f"{self.base_url}/jobs")
        resp.raise_for_status()
        return resp.json()

    async def process(
        self,
        paper: dict[str, Any],
        extensions: list[str] | None = None,
        selections: list[dict[str, Any]] | None = None,
    ) -> Outcome:
        """Runs one paper to completion. Progress is logged whenever the job advances."""
        resp = await self.http.post(
            f"{self.base_url}/process",
            json={"paper": paper, "extensions": extensions or [], "selections": selections or []},
        )
        resp.raise_for_status()
        job_id = resp.json()["id"]
        deadline = asyncio.get_running_loop().time() + self.timeout_seconds
        seen = ""
        while True:
            await asyncio.sleep(self.poll_seconds)
            resp = await self.http.get(f"{self.base_url}/jobs/{job_id}")
            if resp.status_code == 404:
                return Outcome(None, [], {}, "", "deep job vanished (service restarted?)")
            resp.raise_for_status()
            job = resp.json()
            stats = job.get("stats") or {}
            mark = f"{job['status']} {job.get('last_event', '')} cells={stats.get('cells', 0)}"
            if mark != seen:
                log.info(
                    "%s: %s turns=%s tokens=%s/%s list_cost=$%.2f %ss",
                    paper["id"],
                    mark,
                    stats.get("turns", 0),
                    stats.get("input", 0),
                    stats.get("output", 0),
                    stats.get("list_cost", 0.0),
                    stats.get("seconds", 0),
                )
                seen = mark
            if job["status"] in ("done", "error"):
                return Outcome(
                    job.get("result"),
                    job.get("trace") or [],
                    stats,
                    job.get("model") or "",
                    job.get("error"),
                )
            if asyncio.get_running_loop().time() > deadline:
                return Outcome(None, [], stats, "", "litwatch gave up waiting for the deep job")


class DeepProcessor:
    """The pipeline step: text, deep run, storage, Zotero write-back, in that order."""

    def __init__(
        self,
        client: DeepClient,
        zotero: ZoteroClient | None,
        selections: list[Selection],
        extensions: dict[str, str] | None = None,
        inbox_collection: str | None = None,
        inbox_selections: tuple[str, ...] = (),
        fetcher: BodyFetcher | None = None,
    ) -> None:
        self.client = client
        self.zotero = zotero
        self.selections = {s.slug: s for s in selections}
        self.extensions = extensions or {}
        self.inbox = inbox_collection
        self.inbox_selections = set(inbox_selections)
        self.fetcher = fetcher or BodyFetcher()

    async def process_pending(
        self, conn: psycopg.AsyncConnection, paper_id: str | None = None, limit: int = 20
    ) -> ProcessSummary:
        s = ProcessSummary()
        for row in await db.deep_pending(conn, paper_id, limit):
            verdicts = await db.verdicts_for(conn, row["id"])
            tier1 = [
                slug for slug, v in verdicts.items() if v.tier == 1 and slug in self.selections
            ]
            if paper_id and not tier1:
                tier1 = [slug for slug in self.selections if self.selections[slug].has_rubric]
            try:
                error = await self._process_one(conn, row, s, tier1)
            except Exception as exc:  # noqa: BLE001 - one paper must not end the batch
                error = f"{type(exc).__name__}: {exc}"
                log.exception("%s: deep processing raised", row["id"])
                try:
                    await db.store_enrichment_error(conn, row["id"], error)
                except Exception:  # noqa: BLE001 - the attempt is lost, the batch goes on
                    log.exception("%s: could not record the deep failure", row["id"])
            if error:
                s.failed += 1
                s.failures.append((row["id"], error))
                log.warning("%s: deep processing failed: %s", row["id"], error)
            else:
                s.processed += 1
        if self.zotero is not None:
            for row in await db.zotero_enrichment_pending(conn, paper_id):
                try:
                    verdicts = await db.verdicts_for(conn, row["id"])
                    await self._write_back(
                        row, [slug for slug, v in verdicts.items() if v.tier == 1]
                    )
                    await db.mark_zotero_enriched(conn, row["id"])
                except Exception as exc:  # noqa: BLE001 - retried on the next run
                    error = f"zotero: {type(exc).__name__}: {exc}"
                    log.warning("%s: %s", row["id"], error)
                    await conn.execute(
                        "UPDATE papers SET enrichment_error = %s WHERE id = %s", (error, row["id"])
                    )
                    s.failures.append((row["id"], error))
        return s

    async def _process_one(
        self,
        conn: psycopg.AsyncConnection,
        row: dict[str, Any],
        s: ProcessSummary,
        tier1: list[str],
    ) -> str | None:
        body, source = row.get("body"), row.get("body_source")
        if not body:
            fetched = await self.fetcher.fetch(db.row_to_paper(row))
            if fetched is None:
                await db.store_enrichment_error(conn, row["id"], "no paper text available")
                return "no paper text available"
            body, source = fetched
            await db.store_body(conn, row["id"], body, source)
            log.info("%s: stored %s text (%d chars)", row["id"], source, len(body))
        outcome = await self.client.process(
            {
                "id": row["id"],
                "title": row["title"],
                "abstract": row["abstract"],
                "authors": list(row.get("authors") or []),
                "published": db._iso(row.get("published")),
                "url": row.get("url"),
                "body": body,
                "body_source": source,
            },
            extensions=sorted({self.extensions[x] for x in tier1 if x in self.extensions}),
            selections=[
                {
                    "name": self.selections[x].name,
                    "subject": self.selections[x].subject,
                    "rubric": self.selections[x].rubric,
                }
                for x in tier1
            ],
        )
        s.list_cost += float(outcome.stats.get("list_cost") or 0.0)
        if outcome.error or outcome.result is None:
            error = outcome.error or "no result"
            await db.store_enrichment_error(conn, row["id"], error, outcome.trace, outcome.stats)
            return error
        await db.store_enrichment(
            conn, row["id"], outcome.result, outcome.model, outcome.trace, outcome.stats
        )
        log.info(
            "%s: enriched in %s cells, %s turns, list cost $%.2f",
            row["id"],
            outcome.stats.get("cells"),
            outcome.stats.get("turns"),
            outcome.stats.get("list_cost", 0.0),
        )
        return None

    async def _write_back(self, row: dict[str, Any], tier1: list[str]) -> None:
        assert self.zotero is not None
        key = row["zotero_key"]
        paper = db.row_to_paper(
            {**row, "categories": [], "primary_category": None, "updated": None}
        )
        await self.zotero.upsert_note(
            key,
            enrichment_note(
                paper, row["enrichment"], row.get("enrichment_model"), row.get("deep_stats")
            ),
            marker=ENRICH_MARKER,
            tag=ENRICH_TAG,
        )
        await self.zotero.set_tags(key, enrichment_tags(row["enrichment"]), TAG_PREFIXES)
        if self.inbox and self.inbox_selections & set(tier1):
            await self.zotero.add_to_collection(key, self.inbox)


# -- pure helpers ---------------------------------------------------------------------------------


def _slug(value: str) -> str:
    return value.replace(" ", "-")


def enrichment_tags(result: dict[str, Any]) -> list[str]:
    tags = ["litwatch:processed", f"litwatch:kind:{_slug(result['paper_kind'])}"]
    if result.get("code_available"):
        tags.append("litwatch:code")
    if faith := result.get("faithfulness"):
        tags += [
            f"litwatch:monitor:{_slug(faith['monitor_type'])}",
            *(f"litwatch:signal:{_slug(x)}" for x in faith.get("internals_signal", [])),
            *(f"litwatch:mismatch:{_slug(x)}" for x in faith.get("mismatch", [])),
            f"litwatch:ground-truth:{_slug(faith['ground_truth'])}",
        ]
    if mon := result.get("monitoring"):
        tags += [
            f"litwatch:monitor:{_slug(mon['monitor_type'])}",
            *(f"litwatch:signal:{_slug(x)}" for x in mon.get("internals_signal", [])),
            *(f"litwatch:target:{_slug(x)}" for x in mon.get("target", [])),
            f"litwatch:pressure:{_slug(mon['pressure'])}",
        ]
    return sorted(set(tags), key=tags.index)


def enrichment_note(
    paper: Paper,
    result: dict[str, Any],
    model: str | None = None,
    stats: dict[str, Any] | None = None,
) -> str:
    """The `litwatch:enrichment` note: the reading card as HTML, core first, extensions after."""
    setup = result.get("evaluation_setup", {})
    parts = [
        ENRICH_MARKER,
        "<p><b>litwatch reading card</b></p>",
        f"<p><b>Key claim.</b> {_esc(result['key_claim'])}</p>",
        *(f"<p>{_esc(p)}</p>" for p in _paragraphs(result["summary"])),
        "<table>",
        _row("Kind", result["paper_kind"]),
        _row("Models", ", ".join(setup.get("models", []))),
        _row("Tasks", ", ".join(setup.get("tasks", []))),
        _row("Generalization", setup.get("generalization", "")),
        _row(
            "Code",
            (
                f"available: {result['code_url']}"
                if result.get("code_available")
                else "not available"
            )
            + ("; weights released" if result.get("weights_available") else ""),
        ),
        "</table>",
    ]
    if faith := result.get("faithfulness"):
        parts += [
            "<p><b>Faithfulness.</b></p><table>",
            _row("Mismatch detected", ", ".join(faith.get("mismatch", [])) or "none"),
            _row("Ground truth", faith["ground_truth"]),
            _row("Monitor type", faith["monitor_type"]),
            _row("Internals signal", ", ".join(faith.get("internals_signal", [])) or "none"),
            "</table>",
            f"<p><b>Detection method.</b> {_esc(faith['detection_method'])}</p>",
            f"<p><b>Definition.</b> {_esc(faith['definition'])}</p>",
            f"<p><b>Generality.</b> {_esc(faith['generality'])}</p>",
        ]
    if mon := result.get("monitoring"):
        parts += [
            "<p><b>Monitoring.</b></p><table>",
            _row("Target", ", ".join(mon.get("target", [])) or "none"),
            _row("Monitor type", mon["monitor_type"]),
            _row("Internals signal", ", ".join(mon.get("internals_signal", [])) or "none"),
            _row("Pressure", mon["pressure"]),
            "</table>",
            f"<p><b>Monitor method.</b> {_esc(mon['monitor_method'])}</p>",
            f"<p><b>Robustness.</b> {_esc(mon['robustness'])}</p>",
            f"<p><b>Generality.</b> {_esc(mon['generality'])}</p>",
        ]
    parts += [
        "<p><b>Closest prior work.</b></p><ul>",
        *(
            f"<li>{_esc(w['title'])}"
            + (f" (arXiv:{w['arxiv_id']})" if w.get("arxiv_id") else "")
            + f": {_esc(w['relation'])}</li>"
            for w in result.get("prior_work", [])
        ),
        "</ul><p><b>Limitations.</b></p><ul>",
        *(f"<li>{_esc(x)}</li>" for x in result.get("limitations", [])),
        "</ul>",
    ]
    if model or stats:
        st = stats or {}
        parts.append(
            f"<p><i>{_esc(model or '')}: {st.get('cells', '?')} cells, "
            f"{st.get('turns', '?')} turns, {st.get('seconds', '?')}s; arXiv {paper.id}</i></p>"
        )
    return "".join(parts)


def _paragraphs(text: str) -> list[str]:
    return [p.strip() for p in text.replace("\r", "").split("\n\n") if p.strip()]


def _row(label: str, value: str) -> str:
    return f"<tr><td><b>{label}</b></td><td>{_esc(value)}</td></tr>"
