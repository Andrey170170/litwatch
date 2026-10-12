"""The zotero and webdav integrations, driven through the dispatcher against in-memory fakes
of the Zotero Web API, the WebDAV share and arXiv. Nothing here reaches a real service."""

import io
import zipfile

import pytest

from litwatch import events
from litwatch.core import Grade, Hand, Label, LabelOrigin, LabelValue, Opinion, Publication, Verdict
from litwatch.events import dispatch
from litwatch.feedback import word
from litwatch.integrations import webdav as webdav_mod
from litwatch.integrations.webdav import WebDav, WebDAVWriter
from litwatch.integrations.zotero import Zotero, ZoteroClient, attachment_filename
from litwatch.jobs import publications as lookups
from litwatch.store import integrations as state
from litwatch.store import outbox, verdicts
from litwatch.store import papers as papers_store
from litwatch.store import topics as topics_store
from tests.conftest import FAITH, TOPICS, paper
from tests.fakes_http import PDF, FakeArxiv, FakeDAV, FakeZotero, mock
from tests.test_reading import night

P = paper(
    "2610.00001",
    title="Traces that hide steps",
    abstract="Hidden computation in CoT.",
    published="2026-10-01T12:00:00+00:00",
)
CARD = {
    "summary": "First paragraph.\n\nSecond paragraph.",
    "key_claim": "Probes predict unfaithful reasoning.",
    "paper_kind": "method",
    "faithfulness": {
        "mismatch": ["omission"],
        "ground_truth": "task construction",
        "detection_method": "Hints whose use is revealed by the answer.",
        "definition": "A step is faithful iff it acknowledges the hint it followed.",
        "monitor_type": "white-box",
        "internals_signal": ["probes"],
        "generality": "Three tasks, two models.",
    },
    "evaluation_setup": {"models": ["Qwen3-8B"], "tasks": ["MMLU"], "generalization": "No."},
    "prior_work": [{"title": "Turpin et al.", "relation": "Reuses the setup."}],
    "code_available": True,
    "code_url": "https://github.com/x/y",
    "limitations": ["Two models only."],
}


@pytest.fixture
async def world(conn):
    for topic in TOPICS:
        await topics_store.save(conn, topic)
    await papers_store.upsert(conn, [P])
    return conn


async def grade(conn, g: str) -> None:
    await verdicts.add(
        conn,
        Verdict(
            paper_id=P.id,
            topic=FAITH.slug,
            topic_revision=1,
            grade=Grade(g),
            judge=Opinion(grade=Grade(g), reason="r", model="anthropic:x"),
            check=Opinion(grade=Grade(g), reason="r", model="openrouter:y"),
        ),
    )
    await word.settle(conn, P.id)


async def say(conn, value: LabelValue, g: Grade | None = None) -> None:
    await word.label(
        conn,
        Label(paper_id=P.id, topic=FAITH.slug, value=value, grade=g, origin=LabelOrigin.READER),
    )


def zotero(fake: FakeZotero, **kw) -> Zotero:
    return Zotero(ZoteroClient("1", "key", http=mock(fake.handle)), **kw)


async def run(conn, *integrations) -> None:
    for integration in integrations:
        await dispatch.deliver(conn, integration)


# -- zotero ----------------------------------------------------------------------------------------


async def test_a_library_paper_gets_an_item_that_follows_its_grade_and_is_never_deleted(world):
    conn, fake = world, FakeZotero()
    z = zotero(fake)
    await dispatch.start(conn, z)

    await grade(conn, "must_read")
    await run(conn, z)
    (item,) = fake.tops()
    key = item["key"]
    assert await state.zotero_item(conn, P.id) == key
    assert fake.collection_names(key) == {"Litwatch", "Tier 1"}
    assert "litwatch:watch:cot-faithfulness:must_read" in fake.tags(key)
    (note,) = fake.notes(key)
    assert "litwatch:screen schema=2" in note and "CoT faithfulness" in note

    await say(conn, LabelValue.RELEVANT, Grade.RELEVANT)
    await run(conn, z)
    assert fake.collection_names(key) == {"Litwatch", "Tier 2"}
    assert "litwatch:watch:cot-faithfulness:relevant" in fake.tags(key)
    assert "litwatch:watch:cot-faithfulness:must_read" not in fake.tags(key)

    # Unsaved: out of the collections and tagged dropped, but still there.
    await word.toggle(conn, P.id, Hand.UNSAVE)
    await run(conn, z)
    assert fake.collection_names(key) == set()
    assert fake.tags(key) == {"litwatch:dropped"}
    assert not any(c.startswith("DELETE") for c in fake.calls)

    # Saved again: back, and the same item.
    await word.toggle(conn, P.id, Hand.SAVE)
    await run(conn, z)
    assert [i["key"] for i in fake.tops()] == [key]
    assert fake.collection_names(key) == {"Litwatch", "Tier 2"}
    assert "litwatch:dropped" not in fake.tags(key)


async def test_an_item_0x_made_is_taken_over_not_made_again(world):
    conn, fake = world, FakeZotero()
    await conn.execute("ALTER TABLE papers ADD COLUMN zotero_key text")
    await conn.execute("UPDATE papers SET zotero_key = 'OLDITEM1' WHERE id = %s", (P.id,))
    # Titled differently, so only the stored key can find it.
    fake.add_item(
        "OLDITEM1",
        {
            "itemType": "preprint",
            "title": "As 0.x saved it",
            "collections": ["MINE"],
            "tags": [{"tag": "litwatch:tier:1"}, {"tag": "litwatch:sel:cot-faithfulness:1"}],
        },
    )
    fake.items["OLDITEM1"]["data"]["tags"].append({"tag": "read-later"})
    z = zotero(fake)
    await dispatch.start(conn, z)
    await grade(conn, "must_read")
    await run(conn, z)

    assert [i["key"] for i in fake.tops()] == ["OLDITEM1"]
    assert fake.tags("OLDITEM1") == {
        "read-later",
        "litwatch:watch:cot-faithfulness:must_read",
    }
    assert fake.collection_names("OLDITEM1") == {"MINE", "Litwatch", "Tier 1"}


async def test_takes_published_versions_and_cards_reach_the_notes(world):
    conn, fake = world, FakeZotero()
    z = zotero(fake, inbox_collection="INBOX", inbox_topics=("cot-faithfulness",))
    await dispatch.start(conn, z)
    await grade(conn, "must_read")
    await run(conn, z)
    (item,) = fake.tops()
    key = item["key"]
    assert "A take." not in fake.notes(key)[0]

    await night(conn)  # writes the take: take.written
    await lookups.store_record(
        conn, P.id, Publication(venue="NeurIPS", year=2026, doi="10.1/x"), by_owner=True
    )
    await run(conn, z)
    (note,) = fake.notes(key)
    assert "A take." in note and "NeurIPS 2026" in note and "doi:10.1/x" in note

    await state.put_card(conn, P.id, CARD, "deep-model", {"cells": 3}, [])
    await events.emit(conn, events.CARD_READY, P.id, model="deep-model")
    await run(conn, z)
    notes = fake.notes(key)
    assert len(notes) == 2
    assert any("Probes predict unfaithful reasoning." in n for n in notes)
    assert {"litwatch:processed", "litwatch:kind:method", "litwatch:code"} <= fake.tags(key)
    assert "INBOX" in fake.items[key]["data"]["collections"]


async def test_a_paper_never_in_the_library_gets_no_item(world):
    conn, fake = world, FakeZotero()
    z = zotero(fake)
    await dispatch.start(conn, z)
    await grade(conn, "adjacent")
    await run(conn, z)
    assert fake.items == {} and fake.calls == []


# -- webdav ----------------------------------------------------------------------------------------


@pytest.fixture
def pdfs(monkeypatch):
    monkeypatch.setattr(webdav_mod, "ARXIV_PACING_SECONDS", 0)
    fake, dav, arxiv = FakeZotero(), FakeDAV(), FakeArxiv()
    w = WebDav(
        ZoteroClient("1", "key", http=mock(fake.handle)),
        WebDAVWriter("https://dav.test/zotero", "u", "p", http=mock(dav.handle)),
        http=mock(arxiv.handle),
    )
    return fake, dav, arxiv, w


async def test_a_must_read_pdf_waits_for_its_item_then_is_stored_once(world, pdfs):
    conn = world
    fake, dav, arxiv, w = pdfs
    z = zotero(fake)
    for integration in (z, w):
        await dispatch.start(conn, integration)
    await grade(conn, "must_read")

    await run(conn, w)  # before zotero made the item: retried later
    assert (await outbox.cursor(conn, w.name)).failing is not None
    await run(conn, z)
    await outbox.retry(conn, w.name)
    await run(conn, w)

    (item,) = fake.tops()
    (attachment,) = fake.attachments(item["key"])
    assert await state.webdav_file(conn, P.id) == attachment
    with zipfile.ZipFile(io.BytesIO(dav.files[f"{attachment}.zip"])) as zf:
        assert zf.read(attachment_filename(P)) == PDF
    assert f"{attachment}.prop" in dav.files
    assert arxiv.calls == [f"/pdf/{P.id}"]

    # Handled again (a restart mid-way, say): nothing is fetched or attached twice.
    for event in await outbox.after(conn, 0, 100):
        if w.wants(event):
            await w.handle(conn, event)
    assert len(fake.attachments(item["key"])) == 1 and len(arxiv.calls) == 1


async def test_an_overruled_or_unsaved_must_read_fetches_no_pdf_until_it_is_back(world, pdfs):
    conn = world
    fake, dav, arxiv, w = pdfs
    z = zotero(fake)
    for integration in (z, w):
        await dispatch.start(conn, integration)

    # The verdict says must read; the owner says relevant before webdav gets to it.
    await grade(conn, "must_read")
    await say(conn, LabelValue.RELEVANT, Grade.RELEVANT)
    await run(conn, z, w)
    assert arxiv.calls == [] and dav.files == {}

    # Must read again, but unsaved by hand: still nothing.
    await say(conn, LabelValue.RELEVANT, Grade.MUST_READ)
    await word.toggle(conn, P.id, Hand.UNSAVE)
    await run(conn, z, w)
    assert arxiv.calls == []

    # Saved again: `paper.saved` does what was skipped.
    await word.toggle(conn, P.id, Hand.SAVE)
    await run(conn, z, w)
    assert arxiv.calls == [f"/pdf/{P.id}"] and await state.webdav_file(conn, P.id)
