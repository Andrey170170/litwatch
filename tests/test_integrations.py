"""Loading integrations from the configuration, and the archive, deep and webhook
integrations against in-memory fakes. Nothing here reaches a real service."""

import asyncio
import contextlib
import hashlib
import hmac
import json

import httpx
import pytest

from litwatch import events, integrations
from litwatch.config import Config
from litwatch.core import (
    Grade,
    Hand,
    Label,
    LabelOrigin,
    LabelValue,
    Opinion,
    SearchFilters,
    Verdict,
)
from litwatch.events import dispatch
from litwatch.feedback import word
from litwatch.integrations import IntegrationError, papertext
from litwatch.integrations.archive import Archive, ArchiveClient
from litwatch.integrations.deep import MAX_SUBMISSIONS, Deep, DeepClient
from litwatch.integrations.papertext import BodyFetcher, html_to_markdown
from litwatch.integrations.webhook import Webhook
from litwatch.store import integrations as state
from litwatch.store import outbox, verdicts
from litwatch.store import papers as papers_store
from litwatch.store import topics as topics_store
from tests.conftest import FAITH, TOPICS, deliver, paper, settled, start
from tests.fakes_http import FakeArchive, FakeArxiv, FakeDeep, mock

P = paper("2610.00001", title="Traces that hide steps", abstract="Hidden computation.", version=3)
CARD = {"summary": "S.", "key_claim": "K.", "paper_kind": "method"}
# An arXiv HTML rendering long enough to count as the paper.
HTML = (
    "<html><body><article><h1 class='ltx_title'>Traces that hide steps</h1>"
    "<div class='ltx_authors'>Alice</div>"
    + "".join(f"<p>Paragraph {i} with <math alttext='x_{i}'>x</math> in it.</p>" for i in range(80))
    + "</article></body></html>"
)


@pytest.fixture
async def world(conn):
    for topic in TOPICS:
        await topics_store.save(conn, topic)
    return conn


async def grade(conn, g: str, pid: str = P.id) -> None:
    await verdicts.add(
        conn,
        Verdict(
            paper_id=pid,
            topic=FAITH.slug,
            topic_revision=1,
            grade=Grade(g),
            judge=Opinion(grade=Grade(g), reason="r", model="anthropic:x"),
            check=Opinion(grade=Grade(g), reason="r", model="openrouter:y"),
        ),
    )
    await word.settle(conn, pid)


# -- loading ---------------------------------------------------------------------------------------


@pytest.fixture
def secrets(monkeypatch):
    for name in ("ZOTERO_API_KEY", "WEBDAV_PASSWORD", "LITWATCH_ARCHIVE_TOKEN", "HOOK_SECRET"):
        monkeypatch.setenv(name, "s3cret")


def test_nothing_is_loaded_unless_configured():
    assert integrations.load(Config()) == []
    assert integrations.load(Config(integrations={"zotero": {"enabled": False}})) == []


def test_built_ins_load_by_short_name_and_a_section_may_name_its_module(secrets):
    config = Config(
        integrations={
            "zotero": {"user_id": "1"},
            "webdav": {"url": "https://dav.test/zotero", "username": "u"},
            "archive": {"url": "http://archive.test"},
            "deep": {"url": "http://deep.test", "max_per_day": 3},
            "notion": {"use": "webhook", "url": "http://n8n.test/hook", "events": ["paper.*"]},
            "slack": {"use": "webhook", "url": "http://slack.test", "enabled": False},
        }
    )
    loaded = integrations.load(config)
    assert [i.name for i in loaded] == ["zotero", "webdav", "archive", "deep", "notion"]
    assert all(isinstance(i, events.Integration) for i in loaded)
    # webdav and deep read PDFs through the archive once it is configured.
    assert loaded[1].archive is not None and loaded[3].fetcher.archive is not None


def test_a_configuration_that_cannot_work_is_refused_with_its_section(secrets, monkeypatch):
    with pytest.raises(IntegrationError, match="webdav needs the zotero"):
        integrations.load(
            Config(
                integrations={
                    "zotero": {"user_id": "1", "enabled": False},
                    "webdav": {"url": "https://dav.test", "username": "u"},
                }
            )
        )
    with pytest.raises(IntegrationError, match="'slack' is neither a built-in"):
        integrations.load(Config(integrations={"slack": {"url": "http://slack.test"}}))
    monkeypatch.delenv("ZOTERO_API_KEY")
    with pytest.raises(IntegrationError, match=r"\[integrations.zotero\].*ZOTERO_API_KEY"):
        integrations.load(Config(integrations={"zotero": {"user_id": "1"}}))


# -- archive ---------------------------------------------------------------------------------------


def archive(fake: FakeArchive) -> Archive:
    return Archive(
        ArchiveClient("http://archive.test", "archive-token", http=mock(fake.handle)),
        search_http=httpx.Client(transport=httpx.MockTransport(fake.handle)),
    )


async def test_every_new_paper_is_registered_with_its_pdf_and_source(world):
    conn, fake = world, FakeArchive()
    a = archive(fake)
    await start(conn, a)
    unknown = paper("2610.00002")  # no arXiv version, and none the archive knows
    await papers_store.upsert(conn, [P, unknown])
    await deliver(conn, a)

    assert fake.records[P.id]["latest_version"] == 3
    assert "revision" not in fake.records[P.id]["metadata"]
    assert [(j["paper_id"], j["version"], j["kind"], j["priority"]) for j in fake.jobs] == [
        (P.id, 3, "pdf", 100),
        (P.id, 3, "source", 100),
    ]
    assert unknown.id in fake.records  # its metadata, and nothing to fetch yet


async def test_the_archive_answers_searches_by_arxiv_id_with_its_rank_as_score():
    fake = FakeArchive()
    fake.search_items = [
        {
            "id": "2401.00001",
            "title": "First",
            "authors": "Ann Lee, Bo Chen and Cy Dee",
            "published": "2024-01-05",
        },
        {"id": "2401.00002", "title": "Second", "authors": "", "published": ""},
    ]
    hits = archive(fake).search("hidden traces", SearchFilters(year=2024, limit=10))
    assert [h.id for h in hits] == ["2401.00001", "2401.00002"]
    assert hits[0].authors == ["Ann Lee", "Bo Chen", "Cy Dee"]
    assert hits[0].published.year == 2024 and hits[1].published is None
    assert hits[0].score > hits[1].score
    assert fake.searches == [
        {"q": "hidden traces", "limit": "10", "date_from": "2024-01-01", "date_to": "2024-12-31"}
    ]


# -- deep ------------------------------------------------------------------------------------------


def make_deep(fake: FakeDeep, arxiv: FakeArxiv, **kw) -> Deep:
    return Deep(
        DeepClient("http://deep.test", http=mock(fake.handle), poll_seconds=0),
        BodyFetcher(http=mock(arxiv.handle)),
        **{"max_per_day": 10, **kw},
    )


@pytest.fixture
def deep(monkeypatch):
    monkeypatch.setattr(papertext, "ARXIV_PACING_SECONDS", 0)
    fake = FakeDeep({"result": CARD, "stats": {"cells": 4}, "model": "deep-model", "trace": []})
    arxiv = FakeArxiv(html={P.id: HTML, "2610.00009": HTML, "2610.00010": HTML})
    return fake, arxiv, make_deep(fake, arxiv)


def test_the_html_rendering_becomes_markdown_with_its_math():
    md = html_to_markdown(HTML)
    assert md.startswith("# Traces that hide steps") and "$x_3$" in md and "Alice" not in md


async def test_a_must_read_library_paper_gets_a_card_and_card_ready(world, deep):
    conn = world
    fake, arxiv, d = deep
    await papers_store.upsert(conn, [P])
    await start(conn, d)
    await grade(conn, "must_read")
    await deliver(conn, d)

    (sent,) = fake.requests
    assert sent["extensions"] == ["faithfulness"]
    assert [s["name"] for s in sent["selections"]] == [FAITH.name]
    assert sent["paper"]["body_source"] == "html" and "Paragraph 79" in sent["paper"]["body"]
    stored = await state.card(conn, P.id)
    assert stored["card"] == CARD and stored["model"] == "deep-model"
    assert (await papers_store.body(conn, P.id))[1] == "html"
    ready = [e for e in await outbox.after(conn, outbox.START, 100) if e.type == events.CARD_READY]
    assert [(e.paper_id, e.payload) for e in ready] == [(P.id, {"model": "deep-model"})]

    # Heard again, or saved again: one card per paper.
    await word.toggle(conn, P.id, Hand.UNSAVE)
    await word.toggle(conn, P.id, Hand.SAVE)
    await deliver(conn, d)
    assert len(fake.requests) == 1 and arxiv.calls == [f"/html/{P.id}v3"]


async def test_an_overruled_must_read_requests_no_card_but_the_owner_can_ask(world, deep):
    conn = world
    fake, _, d = deep
    await papers_store.upsert(conn, [P])
    await start(conn, d)
    await grade(conn, "must_read")
    await word.label(
        conn,
        Label(
            paper_id=P.id,
            topic=FAITH.slug,
            value=LabelValue.ADJACENT,
            origin=LabelOrigin.READER,
        ),
    )
    await deliver(conn, d)
    assert fake.requests == []

    # Asked by hand, whatever the grade: every active Watch topic is the card's context.
    await events.emit(conn, events.CARD_REQUESTED, P.id)
    await deliver(conn, d)
    (sent,) = fake.requests
    assert [s["name"] for s in sent["selections"]] == [FAITH.name]
    assert (await state.card(conn, P.id))["card"] == CARD


async def test_the_daily_cap_counts_automatic_submissions_and_never_the_owners(world, deep):
    conn = world
    fake, _, d = deep
    d.max_per_day = 1
    asked, other = paper("2610.00009", version=1), paper("2610.00010", version=1)
    await papers_store.upsert(conn, [P, asked, other])
    await start(conn, d)
    await events.emit(conn, events.CARD_REQUESTED, asked.id)  # the owner's: not counted
    await grade(conn, "must_read")  # automatic: the day's one
    await grade(conn, "must_read", other.id)  # automatic: past the cap
    await deliver(conn, d)
    assert [r["paper"]["id"] for r in fake.requests] == [asked.id, P.id]
    assert "passed over" in (await state.card(conn, other.id))["error"]
    assert (await outbox.cursor(conn, d.name)).failing is None
    # The owner can still ask for the one passed over.
    await events.emit(conn, events.CARD_REQUESTED, other.id)
    await deliver(conn, d)
    assert [r["paper"]["id"] for r in fake.requests][-1] == other.id


async def test_a_failed_poll_is_followed_up_not_submitted_again(world, deep):
    conn = world
    fake, _, d = deep
    fake.fail_polls = 1
    await papers_store.upsert(conn, [P])
    await start(conn, d)
    await grade(conn, "must_read")
    await deliver(conn, d)
    assert (await outbox.cursor(conn, d.name)).failing is not None
    assert (await state.card(conn, P.id))["job_id"] == "job-1"

    await outbox.retry(conn, d.name)
    await deliver(conn, d)
    assert len(fake.requests) == 1
    stored = await state.card(conn, P.id)
    assert stored["card"] == CARD and stored["job_id"] is None


async def test_after_a_restart_the_running_job_is_followed_not_paid_for_twice(world, deep):
    conn = world
    fake, arxiv, d = deep
    fake.hold = True
    d.client.poll_seconds = 0.01
    await papers_store.upsert(conn, [P])
    await start(conn, d)
    await grade(conn, "must_read")
    await settled(conn)
    running = asyncio.create_task(dispatch.deliver(conn, d))
    for _ in range(500):
        if fake.requests and (await state.card(conn, P.id) or {}).get("job_id"):
            break
        await asyncio.sleep(0.01)
    running.cancel()  # the service stops mid-job
    with contextlib.suppress(asyncio.CancelledError):
        await running

    fake.hold = False
    restarted = make_deep(fake, arxiv)
    await deliver(conn, restarted)
    assert len(fake.requests) == 1
    assert (await state.card(conn, P.id))["card"] == CARD


async def test_a_submission_without_an_answer_counts_and_is_not_repeated_forever(world, deep):
    conn = world
    fake, _, d = deep
    fake.lose_answers = 10
    await papers_store.upsert(conn, [P])
    await start(conn, d)
    await events.emit(conn, events.CARD_REQUESTED, P.id)
    for _ in range(4):
        await deliver(conn, d)
        await outbox.retry(conn, d.name)
    assert len(fake.requests) == MAX_SUBMISSIONS
    assert await state.attempts(conn, P.id) == MAX_SUBMISSIONS
    assert "gave up" in (await state.card(conn, P.id))["error"]
    assert (await outbox.cursor(conn, d.name)).failing is None


async def test_a_failed_job_is_recorded_and_not_retried_by_itself(world, deep):
    conn = world
    fake, _, d = deep
    fake.outcome = {"error": "the model gave up", "stats": {}, "trace": []}
    await papers_store.upsert(conn, [P])
    await start(conn, d)
    await grade(conn, "must_read")
    await deliver(conn, d)
    stored = await state.card(conn, P.id)
    assert stored["card"] is None and stored["error"] == "the model gave up"
    assert (await outbox.cursor(conn, d.name)).failing is None


# -- webhook ---------------------------------------------------------------------------------------


async def test_a_webhook_posts_matching_events_signed_and_retries_on_failure(conn):
    received, status = [], [500]

    def receive(req: httpx.Request) -> httpx.Response:
        received.append(req)
        return httpx.Response(status[0])

    hook = Webhook(
        "http://n8n.test/hook", patterns=("paper.*",), secret="s3cret", http=mock(receive)
    )
    await start(conn, hook)
    await papers_store.upsert(conn, [P])
    saved = await events.emit(conn, events.PAPER_SAVED, P.id)
    await deliver(conn, hook)
    assert (await outbox.cursor(conn, hook.name)).failing is not None  # the 500

    status[0] = 204
    await outbox.retry(conn, hook.name)
    await deliver(conn, hook)
    assert [json.loads(r.content)["type"] for r in received] == ["paper.added"] * 2 + [
        "paper.saved"
    ]
    last = received[-1]
    assert json.loads(last.content)["id"] == saved
    assert last.headers["X-Litwatch-Event"] == str(saved)
    expected = hmac.new(b"s3cret", last.content, hashlib.sha256).hexdigest()
    assert last.headers["X-Litwatch-Signature"] == f"sha256={expected}"
    assert not hook.wants(events.Event(type=events.TAKE_WRITTEN))
