"""Deep processing: the job protocol with litwatch-deep and what gets written to Zotero."""

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest

from litwatch import deep as deep_mod
from litwatch.deep import DeepClient, DeepProcessor, enrichment_note, enrichment_tags
from litwatch.models import Paper, Verdict
from tests.conftest import FAITH, INTERP

RESULT = {
    "summary": "First paragraph.\n\nSecond paragraph.",
    "key_claim": "Probes predict unfaithful reasoning.",
    "paper_kind": "method",
    "faithfulness": {
        "mismatch": ["omission"],
        "ground_truth": "task construction",
        "detection_method": "Hints whose use is revealed by the answer; probes on the residual.",
        "definition": "A step is faithful iff it acknowledges the hint it followed.",
        "monitor_type": "white-box",
        "internals_signal": ["probes", "sparse autoencoders"],
        "generality": "Three tasks, two models.",
    },
    "evaluation_setup": {
        "models": ["Qwen3-8B"],
        "tasks": ["MMLU"],
        "generalization": "Not tested.",
    },
    "prior_work": [
        {"title": "Turpin et al.", "relation": "Reuses the setup.", "arxiv_id": "2305.04388"}
    ],
    "code_available": True,
    "code_url": "https://github.com/x/y",
    "weights_available": False,
    "limitations": ["Two models only."],
}
STATS = {
    "cells": 12,
    "turns": 14,
    "input": 1000,
    "output": 200,
    "reasoning": 50,
    "cache_read": 800,
    "list_cost": 0.42,
    "seconds": 300,
}


class FakeDeep:
    """litwatch-deep's job protocol: a job that needs two polls to finish."""

    def __init__(self, outcome: dict) -> None:
        self.outcome = outcome
        self.polls = 0
        self.submitted: dict | None = None

    def handle(self, req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and req.url.path == "/process":
            self.request = json.loads(req.content)
            self.submitted = self.request["paper"]
            return httpx.Response(202, json={"id": "job-1", "status": "queued"})
        if req.url.path == "/jobs/job-1":
            self.polls += 1
            if self.polls < 2:
                return httpx.Response(
                    200, json={"status": "running", "stats": {"cells": 3}, "last_event": "cell 3"}
                )
            return httpx.Response(200, json={"status": "done", **self.outcome})
        return httpx.Response(404)


def client(fake: FakeDeep) -> DeepClient:
    return DeepClient(
        "http://deep",
        http=httpx.AsyncClient(transport=httpx.MockTransport(fake.handle)),
        poll_seconds=0,
    )


async def test_client_follows_the_job_to_its_outcome():
    fake = FakeDeep(
        {
            "result": RESULT,
            "trace": [{"type": "submit", "ok": True}],
            "stats": STATS,
            "model": "openai-codex/gpt-5.6-sol",
        }
    )
    outcome = await client(fake).process({"id": "2601.00001", "title": "T", "body": "# T"})
    assert outcome.result == RESULT and outcome.error is None and outcome.stats == STATS
    assert fake.submitted["body"] == "# T"


async def test_client_reports_a_failed_job():
    fake = FakeDeep(
        {
            "result": None,
            "trace": [],
            "stats": STATS,
            "error": "cell budget exhausted without a submit",
        }
    )
    outcome = await client(fake).process({"id": "x", "title": "T", "body": "b"})
    assert outcome.result is None and outcome.error == "cell budget exhausted without a submit"


class FakeDb:
    """The db functions the processor touches, over one in-memory row."""

    def __init__(self, row: dict) -> None:
        self.row = row
        self.calls: list[tuple] = []

    async def deep_pending(self, conn, paper_id=None, limit=20):
        return [] if self.row.get("enrichment") else [self.row]

    async def zotero_enrichment_pending(self, conn, paper_id=None):
        return (
            [self.row]
            if self.row.get("enrichment") and not self.row.get("zotero_enriched_at")
            else []
        )

    async def verdicts_for(self, conn, paper_id):
        return {"cot-faithfulness": Verdict(tier=1, confidence="high", reason="r")}

    async def store_body(self, conn, paper_id, body, source):
        self.row.update(body=body, body_source=source)

    async def store_enrichment(self, conn, paper_id, result, model, trace, stats):
        self.row.update(enrichment=result, enrichment_model=model, deep_stats=stats)
        self.calls.append(("store_enrichment", paper_id))

    async def store_enrichment_error(self, conn, paper_id, error, trace=None, stats=None):
        self.calls.append(("store_enrichment_error", paper_id, error))

    async def mark_zotero_enriched(self, conn, paper_id):
        self.row["zotero_enriched_at"] = "now"
        self.calls.append(("mark_zotero_enriched", paper_id))

    _iso = staticmethod(deep_mod.db._iso)
    row_to_paper = staticmethod(deep_mod.db.row_to_paper)


class FakeZotero:
    def __init__(self) -> None:
        self.notes: list[tuple] = []
        self.tags: list[tuple] = []
        self.collections: list[tuple] = []

    async def upsert_note(self, key, html, marker=None, tag=None):
        self.notes.append((key, html, marker, tag))
        return "N1"

    async def set_tags(self, key, tags, prefixes):
        self.tags.append((key, tags, prefixes))

    async def add_to_collection(self, key, collection_key):
        self.collections.append((key, collection_key))


class FakeFetcher:
    async def fetch(self, paper_id):
        return "# Fetched body\n\nText.", "html"


@pytest.fixture
def row() -> dict:
    return {
        "id": "2601.00001",
        "title": "A Paper",
        "abstract": "Abstract.",
        "authors": ["A B"],
        "published": datetime(2026, 1, 1, tzinfo=UTC),
        "url": None,
        "zotero_key": "ZK1",
        "body": None,
        "body_source": None,
        "enrichment": None,
    }


async def test_processor_fetches_text_stores_result_and_writes_zotero(row, monkeypatch):
    fake_db = FakeDb(row)
    monkeypatch.setattr(deep_mod, "db", fake_db)
    fake = FakeDeep(
        {"result": RESULT, "trace": [], "stats": STATS, "model": "openai-codex/gpt-5.6-sol"}
    )
    zot = FakeZotero()
    proc = DeepProcessor(
        client(fake),
        zot,
        [FAITH, INTERP],
        extensions={"cot-faithfulness": "faithfulness"},
        inbox_collection="INBOX",
        inbox_selections=("cot-faithfulness",),
        fetcher=FakeFetcher(),
    )
    conn = SimpleNamespace(execute=None)

    s = await proc.process_pending(conn)
    assert (s.processed, s.failed, s.list_cost) == (1, 0, 0.42)
    assert (
        fake.submitted["body"].startswith("# Fetched body")
        and fake.submitted["body_source"] == "html"
    )
    assert fake.request["extensions"] == ["faithfulness"]
    assert fake.request["selections"] == [
        {"name": FAITH.name, "subject": FAITH.subject, "rubric": FAITH.rubric}
    ]
    assert row["enrichment"] == RESULT
    assert [c[0] for c in fake_db.calls] == ["store_enrichment", "mark_zotero_enriched"]
    (key, html, marker, tag) = zot.notes[0]
    assert (
        key == "ZK1"
        and marker == "<!-- litwatch:enrichment schema=1 -->"
        and tag == "litwatch:enrichment"
    )
    assert "Probes predict unfaithful reasoning." in html
    assert zot.tags[0][1] == [
        "litwatch:processed",
        "litwatch:kind:method",
        "litwatch:code",
        "litwatch:monitor:white-box",
        "litwatch:signal:probes",
        "litwatch:signal:sparse-autoencoders",
        "litwatch:mismatch:omission",
        "litwatch:ground-truth:task-construction",
    ]
    assert zot.collections == [("ZK1", "INBOX")]

    again = await proc.process_pending(conn)
    assert (again.processed, again.failed) == (0, 0) and len(zot.notes) == 1, "idempotent"


async def test_processor_records_a_failed_run_without_touching_zotero(row, monkeypatch):
    fake_db = FakeDb({**row, "body": "# Body", "body_source": "html"})
    monkeypatch.setattr(deep_mod, "db", fake_db)
    fake = FakeDeep(
        {
            "result": None,
            "trace": [],
            "stats": STATS,
            "error": "the model stopped without submitting",
        }
    )
    zot = FakeZotero()
    s = await DeepProcessor(client(fake), zot, [FAITH], fetcher=FakeFetcher()).process_pending(
        SimpleNamespace()
    )
    assert s.failed == 1 and s.failures == [("2601.00001", "the model stopped without submitting")]
    assert fake_db.calls == [
        ("store_enrichment_error", "2601.00001", "the model stopped without submitting")
    ]
    assert not zot.notes and not zot.tags


def test_note_and_tags_are_pure():
    paper = Paper(id="2601.00001", title="A Paper", abstract="x")
    html = enrichment_note(paper, RESULT, "m", STATS)
    assert html.startswith("<!-- litwatch:enrichment schema=1 -->")
    assert "<p>First paragraph.</p><p>Second paragraph.</p>" in html
    assert "Turpin et al. (arXiv:2305.04388): Reuses the setup." in html
    assert "<b>Detection method.</b> Hints whose use" in html
    core_only = {k: v for k, v in RESULT.items() if k != "faithfulness"}
    assert "Faithfulness" not in enrichment_note(paper, core_only)
    assert enrichment_tags(core_only) == [
        "litwatch:processed",
        "litwatch:kind:method",
        "litwatch:code",
    ]
    assert "12 cells, 14 turns, 300s" in html
    assert "litwatch:code" in enrichment_tags(RESULT)
    assert "litwatch:code" not in enrichment_tags({**RESULT, "code_available": False})


MONITORING = {
    "target": ["reward hacking", "deception"],
    "monitor_type": "hybrid",
    "internals_signal": ["probes"],
    "monitor_method": "A weaker model reads the trace; a probe on the residual flags the rest.",
    "pressure": "optimization against the monitor",
    "robustness": "Detection drops after RL against the monitor; obfuscated traces evade it.",
    "generality": "Coding and agentic tasks, two model families.",
}


def test_monitoring_extension_in_note_and_tags():
    paper = Paper(id="2601.00002", title="A Monitor", abstract="x")
    result = {k: v for k, v in RESULT.items() if k != "faithfulness"} | {"monitoring": MONITORING}
    html = enrichment_note(paper, result)
    assert "<b>Monitoring.</b>" in html and "Faithfulness" not in html
    assert "<b>Monitor method.</b> A weaker model" in html
    assert enrichment_tags(result) == [
        "litwatch:processed",
        "litwatch:kind:method",
        "litwatch:code",
        "litwatch:monitor:hybrid",
        "litwatch:signal:probes",
        "litwatch:target:reward-hacking",
        "litwatch:target:deception",
        "litwatch:pressure:optimization-against-the-monitor",
    ]
    # A paper tier 1 in both selections carries both extensions; shared tags appear once.
    both = enrichment_tags(RESULT | {"monitoring": MONITORING})
    assert both.count("litwatch:signal:probes") == 1
