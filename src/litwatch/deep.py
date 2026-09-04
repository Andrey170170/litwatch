"""Deep processing: hand tier 1 papers to litwatch-deep, keep the outcome, write it to Zotero.

litwatch owns all state. The deep service is a stateless job runner: it gets the paper text,
returns the validated result with a trace and token stats, and litwatch stores them, writes
the `litwatch:enrichment` note and `litwatch:*` tags, and adds the item to `Inbox` so the
personal pipeline picks it up.
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
from litwatch.text import BodyFetcher
from litwatch.zotero import ENRICH_MARKER, ENRICH_TAG, ZoteroClient, _esc

log = logging.getLogger(__name__)

TAG_PREFIXES = (
    "litwatch:kind:",
    "litwatch:monitor:",
    "litwatch:signal:",
    "litwatch:threat:",
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

    async def process(self, paper: dict[str, Any]) -> Outcome:
        """Runs one paper to completion. Progress is logged whenever the job advances."""
        resp = await self.http.post(f"{self.base_url}/process", json={"paper": paper})
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
        inbox_collection: str | None = None,
        fetcher: BodyFetcher | None = None,
    ) -> None:
        self.client = client
        self.zotero = zotero
        self.inbox = inbox_collection
        self.fetcher = fetcher or BodyFetcher()

    async def process_pending(
        self, conn: psycopg.AsyncConnection, paper_id: str | None = None, limit: int = 20
    ) -> ProcessSummary:
        s = ProcessSummary()
        for row in await db.deep_pending(conn, paper_id, limit):
            error = await self._process_one(conn, row, s)
            if error:
                s.failed += 1
                s.failures.append((row["id"], error))
                log.warning("%s: deep processing failed: %s", row["id"], error)
            else:
                s.processed += 1
        if self.zotero is not None:
            for row in await db.zotero_enrichment_pending(conn, paper_id):
                try:
                    await self._write_back(row)
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
        self, conn: psycopg.AsyncConnection, row: dict[str, Any], s: ProcessSummary
    ) -> str | None:
        body, source = row.get("body"), row.get("body_source")
        if not body:
            fetched = await self.fetcher.fetch(row["id"])
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
            }
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

    async def _write_back(self, row: dict[str, Any]) -> None:
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
        if self.inbox:
            await self.zotero.add_to_collection(key, self.inbox)


# -- pure helpers ---------------------------------------------------------------------------------


def _slug(value: str) -> str:
    return value.replace(" ", "-")


def enrichment_tags(result: dict[str, Any]) -> list[str]:
    tags = [
        "litwatch:processed",
        f"litwatch:kind:{_slug(result['paper_kind'])}",
        f"litwatch:monitor:{_slug(result['monitor_type'])}",
        *(f"litwatch:signal:{_slug(x)}" for x in result.get("internals_signal", [])),
        *(f"litwatch:threat:{_slug(x)}" for x in result.get("threat_model", [])),
    ]
    if result.get("code_available"):
        tags.append("litwatch:code")
    return tags


def enrichment_note(
    paper: Paper,
    result: dict[str, Any],
    model: str | None = None,
    stats: dict[str, Any] | None = None,
) -> str:
    """The `litwatch:enrichment` note: the reading card as HTML."""
    setup = result.get("evaluation_setup", {})
    parts = [
        ENRICH_MARKER,
        "<p><b>litwatch reading card</b></p>",
        f"<p><b>Key claim.</b> {_esc(result['key_claim'])}</p>",
        *(f"<p>{_esc(p)}</p>" for p in _paragraphs(result["summary"])),
        "<table>",
        _row("Kind", result["paper_kind"]),
        _row("Monitor type", result["monitor_type"]),
        _row("Internals signal", ", ".join(result.get("internals_signal", [])) or "none"),
        _row("Threat model", ", ".join(result.get("threat_model", []))),
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
        "<p><b>Faithfulness definition and ground truth.</b> "
        f"{_esc(result['faithfulness_definition'])}</p>",
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
