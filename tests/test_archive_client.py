"""Archive reuse, promotion, and outage behavior without external traffic."""

import json

import httpx
import pytest

from litwatch import archive_client, route, text
from litwatch.archive_client import (
    ArchiveClient,
    ArchiveError,
    ArchiveUnavailable,
    ArchiveVersionUnknown,
)
from litwatch.models import Paper

PAPER = Paper(id="2609.29266", title="Test", abstract="Abstract", version=2)
PDF = b"%PDF-1.7 test payload"


class FakeArchive:
    def __init__(self, status="complete"):
        self.status = status
        self.calls = []
        self.jobs = {}

    def handle(self, req):
        self.calls.append(req)
        assert req.headers["Authorization"] == "Bearer secret-token"
        if req.url.path.endswith("/pdf"):
            assert req.url.params["version"] == "2"
            return httpx.Response(200, content=PDF)
        if req.url.path == "/v1/jobs":
            body = json.loads(req.content)
            key = (body["paper_id"], body["version"], body["kind"])
            job = self.jobs.setdefault(key, {"id": len(self.jobs) + 1, **body})
            job["priority"] = min(job["priority"], body["priority"])
            job["status"] = self.status
            return httpx.Response(202, json=job)
        if req.url.path.startswith("/v1/jobs/"):
            return httpx.Response(200, json={"id": 1, "status": self.status})
        return httpx.Response(
            200,
            json={"id": PAPER.id, "latest_version": 2, "metadata": PAPER.model_dump(mode="json")},
        )

    def client(self, wait_seconds=0):
        return ArchiveClient(
            "http://archive",
            "secret-token",
            httpx.AsyncClient(transport=httpx.MockTransport(self.handle)),
            wait_seconds=wait_seconds,
        )


async def test_priority_promotion_reuses_one_job_and_returns_versioned_pdf():
    fake = FakeArchive()
    client = fake.client()
    original = await client.request(PAPER)
    assert original["priority"] == 100
    assert await client.pdf(PAPER) == PDF
    assert len(fake.jobs) == 1
    assert next(iter(fake.jobs.values()))["priority"] == 0
    assert len([r for r in fake.calls if r.url.path.endswith("/pdf")]) == 1


async def test_source_jobs_share_metadata_and_keep_explicit_version():
    fake = FakeArchive()
    job = await fake.client().request(PAPER, kind="source")
    assert job["kind"] == "source" and job["version"] == 2
    metadata_write = json.loads(fake.calls[0].content)
    assert metadata_write["latest_version"] == 2


@pytest.mark.parametrize("state", ["pending", "running", "failed", "unavailable"])
async def test_pending_and_failed_archive_jobs_do_not_duplicate_provider_fetch(state):
    fake = FakeArchive(state)
    calls = []
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda req: calls.append(req) or httpx.Response(200, content=PDF)
        )
    )
    router = route.Router(object(), None, http=http, archive=fake.client())
    assert await router._download(PAPER) is None
    assert not calls


@pytest.mark.parametrize("status", [401, 403, 429, 503])
async def test_outage_falls_back_to_versioned_provider_pdf(monkeypatch, status):
    monkeypatch.setattr(route, "ARXIV_PACING_SECONDS", 0)
    archive = ArchiveClient(
        "http://archive",
        "secret-token",
        httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(status))),
    )
    calls = []
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda req: calls.append(req) or httpx.Response(200, content=PDF)
        )
    )
    router = route.Router(object(), None, http=http, archive=archive)
    assert await router._download(PAPER) == PDF
    assert [r.url.path for r in calls] == [f"/pdf/{PAPER.id}v2"]


async def test_transport_exception_does_not_expose_token():
    def unavailable(req):
        raise httpx.ConnectError("failure", request=req)

    archive = ArchiveClient(
        "http://archive",
        "secret-token",
        httpx.AsyncClient(transport=httpx.MockTransport(unavailable)),
    )
    with pytest.raises(ArchiveUnavailable, match="transport unavailable") as exc:
        await archive.metadata(PAPER.id)
    assert "secret-token" not in str(exc.value)


async def test_unknown_revision_requires_matching_metadata_timestamp():
    fake = FakeArchive()
    paper = PAPER.model_copy(update={"version": None, "updated": "2026-09-25T00:00:00Z"})
    with pytest.raises(ArchiveVersionUnknown):
        await fake.client().request(paper)
    assert not fake.jobs


async def test_matching_catalog_timestamp_can_resolve_revision():
    def handle(req):
        return httpx.Response(
            200, json={"latest_version": 3, "metadata": {"updated": "2026-09-25T00:00:00+00:00"}}
        )

    client = ArchiveClient(
        "http://archive", "token", httpx.AsyncClient(transport=httpx.MockTransport(handle))
    )
    paper = PAPER.model_copy(update={"version": None, "updated": "2026-09-25T00:00:00Z"})
    assert (await client.resolve(paper)).version == 3


async def test_pdf_read_is_bounded_and_rejects_non_pdf(monkeypatch):
    fake = FakeArchive()
    monkeypatch.setattr(archive_client, "MAX_PDF_BYTES", 3)
    with pytest.raises(ArchiveError, match="read limit"):
        await fake.client().available_pdf(PAPER.id, 2)
    monkeypatch.setattr(archive_client, "MAX_PDF_BYTES", 1000)
    client = ArchiveClient(
        "http://archive",
        "token",
        httpx.AsyncClient(
            transport=httpx.MockTransport(lambda req: httpx.Response(200, content=b"<html>"))
        ),
    )
    with pytest.raises(ArchiveError, match="invalid PDF"):
        await client.available_pdf(PAPER.id, 2)


async def test_body_reuses_complete_pdf_without_arxiv_html(monkeypatch):
    fake = FakeArchive()
    monkeypatch.setattr(text, "pdf_to_markdown", lambda pdf: "paper body " * 300)

    def no_network(req):
        pytest.fail("available archive PDF must avoid upstream requests")

    fetcher = text.BodyFetcher(
        httpx.AsyncClient(transport=httpx.MockTransport(no_network)), archive=fake.client()
    )
    result = await fetcher.fetch(PAPER)
    assert result[1] == "pdf"


async def test_body_promotes_before_html_and_does_not_wait_for_pdf_if_html_sufficient(monkeypatch):
    fake = FakeArchive("pending")
    monkeypatch.setattr(text, "ARXIV_PACING_SECONDS", 0)

    def html(req):
        assert next(iter(fake.jobs.values()))["priority"] == 0
        assert req.url.path == f"/html/{PAPER.id}v2"
        return httpx.Response(200, text="<article>" + "paper body " * 300 + "</article>")

    fetcher = text.BodyFetcher(
        httpx.AsyncClient(transport=httpx.MockTransport(html)), archive=fake.client()
    )
    result = await fetcher.fetch(PAPER)
    assert result[1] == "html"
    assert not any(r.url.path.endswith("/pdf") for r in fake.calls)


async def test_body_pending_pdf_after_html_missing_never_fetches_duplicate(monkeypatch):
    fake = FakeArchive("pending")
    monkeypatch.setattr(text, "ARXIV_PACING_SECONDS", 0)
    calls = []
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: calls.append(req) or httpx.Response(404))
    )
    assert await text.BodyFetcher(http, archive=fake.client()).fetch(PAPER) is None
    assert [r.url.path for r in calls] == [f"/html/{PAPER.id}v2"]


async def test_id_only_body_promotion_does_not_clear_existing_catalog_fields():
    writes = []

    def handle(req):
        if req.method == "GET":
            return httpx.Response(
                200,
                json={
                    "latest_version": 2,
                    "metadata": {
                        "title": "Existing title",
                        "abstract": "Existing abstract",
                        "authors": "Historical author encoding",
                        "categories": ["cs.AI"],
                    },
                },
            )
        if req.method == "PUT":
            writes.append(json.loads(req.content))
            return httpx.Response(200, json={})
        return httpx.Response(202, json={"id": 1, "status": "pending"})

    client = ArchiveClient(
        "http://archive", "token", httpx.AsyncClient(transport=httpx.MockTransport(handle))
    )
    paper, job = await client.prepare_pdf(PAPER.id)
    assert paper.version == 2 and job["status"] == "pending"
    metadata = writes[0]["metadata"]
    assert metadata["title"] == "Existing title"
    assert "authors" not in metadata and "categories" not in metadata


async def test_artifact_delivery_can_leave_canonical_metadata_untouched():
    fake = FakeArchive()
    job = await fake.client().request(PAPER, kind="source", publish_metadata=False)
    assert job["kind"] == "source" and job["version"] == 2
    assert [(r.method, r.url.path) for r in fake.calls] == [("POST", "/v1/jobs")]
