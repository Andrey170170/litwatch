"""Loading integrations from the configuration, and the archive and webhook integrations against
in-memory fakes (deep's are in test_deep.py). Nothing here reaches a real service."""

import hashlib
import hmac
import json
from datetime import date

import httpx
import pytest

from litwatch import events, integrations
from litwatch.archive import BACKGROUND, WANTED, ArchiveClient
from litwatch.config import Config
from litwatch.core import Effective, Fit, Grade, Rarity, SearchFilters, Status
from litwatch.integrations import IntegrationError
from litwatch.integrations.archive import Archive
from litwatch.integrations.webhook import Webhook
from litwatch.store import outbox
from litwatch.store import papers as papers_store
from litwatch.store import taxonomy as taxonomy_store
from litwatch.store import word as word_store
from tests.conftest import INTERP, TREE, deliver, paper, start
from tests.fakes_http import FakeArchive, mock

P = paper("2610.00001", title="Traces that hide steps", abstract="Hidden computation.", version=3)


@pytest.fixture
async def world(conn):
    await taxonomy_store.save(conn, TREE)
    return conn


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
    # webdav reads PDFs through the archive once it is configured.
    assert loaded[1].archive is not None and loaded[3].max_per_day == 3


def test_a_configuration_that_cannot_work_is_refused_with_its_section(secrets, monkeypatch):
    with pytest.raises(IntegrationError, match="enable a zotero integration"):
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


def test_deep_is_refused_without_an_archive_to_read_papers_from(secrets, monkeypatch):
    monkeypatch.delenv("LITWATCH_ARCHIVE_URL", raising=False)
    with pytest.raises(IntegrationError, match="deep reads papers from the archive"):
        integrations.load(Config(integrations={"deep": {"url": "http://deep.test"}}))


def test_webdav_finds_its_zotero_by_module_whatever_the_section_is_called(secrets):
    config = Config(
        integrations={
            "library": {"use": "zotero", "user_id": "4242"},
            "webdav": {"url": "https://dav.test/zotero", "username": "u"},
            "papers": {"use": "archive", "url": "http://archive.test"},
        }
    )
    library, webdav, _ = integrations.load(config)
    assert library.name == "library"
    assert webdav.zotero.base.endswith("/users/4242")
    assert webdav.archive is not None  # found as `papers`, by the module it uses


def test_the_archive_gives_up_on_a_search_before_search_gives_up_on_it(secrets):
    with pytest.raises(IntegrationError, match="below 5"):
        integrations.load(
            Config(integrations={"archive": {"url": "http://a.test", "search_timeout_seconds": 5}})
        )


def test_webhook_events_must_be_a_list_of_patterns(secrets):
    for events_ in ({"paper": "saved"}, "paper.*", [], ["paper.*", 3]):
        with pytest.raises(IntegrationError, match="list of event-type patterns"):
            integrations.load(
                Config(
                    integrations={
                        "hook": {"use": "webhook", "url": "http://h.test", "events": events_}
                    }
                )
            )
    with pytest.raises(IntegrationError, match="seconds above zero"):
        integrations.load(
            Config(integrations={"hook": {"use": "webhook", "url": "http://h.test", "timeout": 0}})
        )
    (hook,) = integrations.load(
        Config(integrations={"hook": {"use": "webhook", "url": "http://h.test", "timeout": 300}})
    )
    assert hook.http.timeout.read == 300


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


async def _graded(conn, paper_id: str, topic: str, grade: Grade) -> None:
    """The paper's effective grade on `topic` changes to `grade`, with its event."""
    fit = Fit.CENTRAL if grade != Grade.NONE else Fit.NONE
    await word_store.set_effective(
        conn, paper_id, topic, Effective(fit=fit, grade=grade, rarity=Rarity.RARE)
    )
    await events.emit(conn, events.GRADE_CHANGED, paper_id, topic=topic, after=grade.value)


async def test_the_storage_policy_asks_for_the_pdfs_of_graded_up_and_saved_papers(world):
    conn, fake = world, FakeArchive()
    a = archive(fake)
    others = [paper(f"2610.0001{i}", version=1) for i in range(5)]
    await papers_store.upsert(conn, [P, *others])
    await start(conn, a)
    relevant, adjacent, saved, gone, paused = others
    await _graded(conn, P.id, "cot-faithfulness", Grade.MUST_READ)
    await _graded(conn, relevant.id, "agents", Grade.RELEVANT)
    await _graded(conn, adjacent.id, "interpretability", Grade.ADJACENT)
    await word_store.set_saved(conn, saved.id, True)
    await events.emit(conn, events.PAPER_SAVED, saved.id)
    # Graded relevant, then down again before the event is handled: nothing is asked for.
    await _graded(conn, gone.id, "agents", Grade.RELEVANT)
    await word_store.set_effective(conn, gone.id, "agents", None)
    # A paused topic makes no new judgements, and keeps no new PDFs.
    await taxonomy_store.save(
        conn,
        TREE.model_copy(
            update={
                "topics": [
                    t if t.slug != INTERP.slug else t.model_copy(update={"status": Status.PAUSED})
                    for t in TREE.topics
                ]
            }
        ),
    )
    await _graded(conn, paused.id, "interpretability", Grade.MUST_READ)
    await deliver(conn, a)

    asked = [(j["paper_id"], j["version"], j["kind"], j["priority"]) for j in fake.jobs]
    assert asked == [
        (P.id, 3, "pdf", WANTED),
        (relevant.id, 1, "pdf", WANTED),
        (saved.id, 1, "pdf", WANTED),
    ]
    assert WANTED < BACKGROUND  # an archive under `pdfs wanted` holds background PDFs only


async def test_an_archive_that_does_not_register_only_answers_searches(world, secrets):
    with pytest.raises(IntegrationError, match="register must be true or false"):
        integrations.load(
            Config(integrations={"archive": {"url": "http://a.test", "register": "no"}})
        )
    (loaded,) = integrations.load(
        Config(integrations={"archive": {"url": "http://a.test", "register": False}})
    )
    assert loaded.register is False

    conn, fake = world, FakeArchive()
    a = archive(fake)
    a.register = False
    await start(conn, a)
    await papers_store.upsert(conn, [P])
    await word_store.set_saved(conn, P.id, True)
    await events.emit(conn, events.PAPER_SAVED, P.id)
    await deliver(conn, a)
    assert fake.records == {} and fake.jobs == []
    fake.search_items = [{"id": "2401.00001", "title": "First", "authors": "", "published": ""}]
    assert [h.id for h in a.search("q", SearchFilters(limit=5))] == ["2401.00001"]


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
    window = SearchFilters(since=date(2024, 1, 1), until=date(2024, 1, 31), limit=10)
    hits = archive(fake).search("hidden traces", window)
    assert [h.id for h in hits] == ["2401.00001", "2401.00002"]
    assert hits[0].authors == ["Ann Lee", "Bo Chen", "Cy Dee"]
    assert hits[0].published.year == 2024 and hits[1].published is None
    assert hits[0].score > hits[1].score
    assert fake.searches == [
        {"q": "hidden traces", "limit": "10", "date_from": "2024-01-01", "date_to": "2024-01-31"}
    ]


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
    assert not hook.wants(events.Event(type=events.GRADE_CHANGED))
