"""The zotero and webdav integrations, driven through the dispatcher against in-memory fakes
of the Zotero Web API, the WebDAV share and arXiv. Nothing here reaches a real service."""

import io
import zipfile

import pytest

from litwatch import events
from litwatch.core import Fit, Hand, Label, LabelOrigin, LabelValue, Opinion, Publication, Verdict
from litwatch.feedback import word
from litwatch.integrations import webdav as webdav_mod
from litwatch.integrations.webdav import WebDav, WebDAVWriter
from litwatch.integrations.zotero import Zotero, ZoteroClient, attachment_filename, is_item_of
from litwatch.jobs import publications as lookups
from litwatch.store import integrations as state
from litwatch.store import outbox, verdicts
from litwatch.store import papers as papers_store
from litwatch.store import taxonomy as taxonomy_store
from tests.conftest import FAITH, TREE, deliver, paper, start
from tests.fakes_http import PDF, FakeArxiv, FakeDAV, FakeZotero, mock

P = paper(
    "2610.00001",
    title="Traces that hide steps",
    abstract="Hidden computation in CoT.",
    published="2026-10-01T12:00:00+00:00",
)
# What a stored card says it was written from.
LABEL = {"revision": 1, "version": 1, "source": "latex", "pass_id": None}
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
    await taxonomy_store.save(conn, TREE)
    await papers_store.upsert(conn, [P])
    return conn


async def grade(conn, g: str) -> None:
    """A verdict whose grade, with no Weight yet (solid), is `g`."""
    fit = {"must_read": Fit.CENTRAL, "relevant": Fit.RELATED}.get(g) or Fit(g)
    await verdicts.add(
        conn,
        Verdict(
            paper_id=P.id,
            topic=FAITH.slug,
            topic_version="v",
            fit=fit,
            reason="r",
            opinions={"judge": Opinion(fit=fit, reason="r", model="anthropic:x")},
        ),
    )
    await word.settle(conn, P.id)


async def say(conn, value: LabelValue) -> None:
    await word.label(
        conn, Label(paper_id=P.id, topic=FAITH.slug, value=value, origin=LabelOrigin.READER)
    )


def client(fake: FakeZotero) -> ZoteroClient:
    return ZoteroClient("1", "key", http=mock(fake.handle), retry_seconds=0)


def zotero(fake: FakeZotero, **kw) -> Zotero:
    return Zotero(client(fake), **kw)


async def run(conn, *integrations) -> None:
    for integration in integrations:
        await deliver(conn, integration)


# -- zotero ----------------------------------------------------------------------------------------


async def test_a_library_paper_gets_an_item_that_follows_its_grade_and_is_never_deleted(world):
    conn, fake = world, FakeZotero()
    z = zotero(fake)
    await start(conn, z)

    await grade(conn, "must_read")
    await run(conn, z)
    (item,) = fake.tops()
    key = item["key"]
    assert await state.zotero_item(conn, P.id) == key
    assert fake.collection_names(key) == {"Litwatch", "Tier 1"}
    assert "litwatch:watch:cot-faithfulness:must_read" in fake.tags(key)
    (note,) = fake.notes(key)
    assert "litwatch:screen schema=2" in note and "Chain-of-thought faithfulness" in note

    await say(conn, LabelValue.RELATED)
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


async def test_an_item_0x_made_is_found_and_updated_not_made_again(world):
    conn, fake = world, FakeZotero()
    # Found by its title; 0.x's tier and selection tags give way to v1's grade tag.
    fake.add_item(
        "OLDITEM1",
        {
            "itemType": "preprint",
            "title": P.title,
            "collections": ["MINE"],
            "tags": [{"tag": "litwatch:tier:1"}, {"tag": "litwatch:sel:cot-faithfulness:1"}],
        },
    )
    fake.items["OLDITEM1"]["data"]["tags"].append({"tag": "read-later"})
    z = zotero(fake)
    await start(conn, z)
    await grade(conn, "must_read")
    await run(conn, z)

    assert [i["key"] for i in fake.tops()] == ["OLDITEM1"]
    assert fake.tags("OLDITEM1") == {
        "read-later",
        "litwatch:watch:cot-faithfulness:must_read",
    }
    assert fake.collection_names("OLDITEM1") == {"MINE", "Litwatch", "Tier 1"}


async def test_an_item_whose_title_differs_is_found_by_its_arxiv_id(world):
    conn, fake = world, FakeZotero()
    # The owner retitled 0.x's item; another item only cites the paper in its abstract.
    fake.add_item(
        "CITES001",
        {"itemType": "preprint", "title": "A survey", "abstractNote": "Builds on 2610.00001."},
    )
    fake.add_item(
        "CITES002",
        {
            "itemType": "preprint",
            "title": "A reply",
            "url": "https://arxiv.org/abs/2610.00002",
            "extra": "Related: https://arxiv.org/abs/2610.00001",
        },
    )
    fake.add_item(
        "OLDITEM1",
        {
            "itemType": "preprint",
            "title": "Hidden steps (my notes)",
            "url": "https://arxiv.org/abs/2610.00001v1",
        },
    )
    z = zotero(fake)
    await start(conn, z)
    await grade(conn, "must_read")
    await run(conn, z)

    assert len(fake.tops()) == 3
    assert await state.zotero_item(conn, P.id) == "OLDITEM1"


async def test_grades_published_versions_and_cards_reach_the_notes(world):
    conn, fake = world, FakeZotero()
    z = zotero(fake, inbox_collection="INBOX", inbox_topics=("cot-faithfulness",))
    await start(conn, z)
    await grade(conn, "must_read")
    await run(conn, z)
    (item,) = fake.tops()
    key = item["key"]
    assert "Chain-of-thought faithfulness</b> · must read" in fake.notes(key)[0]

    await lookups.store_record(
        conn, P.id, Publication(venue="NeurIPS", year=2026, doi="10.1/x"), by_owner=True
    )
    await run(conn, z)
    (note,) = fake.notes(key)
    assert "NeurIPS 2026" in note and "doi:10.1/x" in note

    await state.put_card(conn, P.id, CARD, "deep-model", {"cells": 3}, [], **LABEL)
    await events.emit(conn, events.CARD_READY, P.id, model="deep-model")
    await run(conn, z)
    notes = fake.notes(key)
    assert len(notes) == 2
    assert any("Probes predict unfaithful reasoning." in n for n in notes)
    assert {"litwatch:processed", "litwatch:kind:method", "litwatch:code"} <= fake.tags(key)
    assert "INBOX" in fake.items[key]["data"]["collections"]


async def test_the_inbox_holds_a_paper_only_while_its_card_and_grade_say_so(world):
    conn, fake = world, FakeZotero()
    z = zotero(fake, inbox_collection="INBOX", inbox_topics=("cot-faithfulness",))
    await start(conn, z)
    await grade(conn, "must_read")
    await state.put_card(conn, P.id, CARD, "deep-model", {}, [], **LABEL)
    await events.emit(conn, events.CARD_READY, P.id, model="deep-model")
    await run(conn, z)
    (item,) = fake.tops()
    key = item["key"]

    def inbox() -> bool:
        return "INBOX" in fake.items[key]["data"]["collections"]

    assert inbox()
    await say(conn, LabelValue.RELATED)  # below must read: out of the inbox
    await run(conn, z)
    assert not inbox() and "Tier 2" in fake.collection_names(key)
    await say(conn, LabelValue.CENTRAL)  # back
    await run(conn, z)
    assert inbox()
    await word.toggle(conn, P.id, Hand.UNSAVE)  # unsaved: out with the other collections
    await run(conn, z)
    assert not inbox() and fake.collection_names(key) == set()


async def test_a_create_whose_answer_was_lost_is_made_once(world):
    conn, fake = world, FakeZotero()
    fake.lose_answers = 100  # every create happens, then answers 502
    z = zotero(fake)
    await start(conn, z)
    await grade(conn, "must_read")
    await run(conn, z)
    assert (await outbox.cursor(conn, z.name)).failing is None
    (item,) = fake.tops()
    assert sorted(c["data"]["name"] for c in fake.collections.values()) == [
        "Litwatch",
        "Tier 1",
        "Tier 2",
    ]
    assert len(fake.notes(item["key"])) == 1
    assert await state.zotero_item(conn, P.id) == item["key"]


async def test_whats_there_is_looked_for_past_the_first_page(world):
    conn, fake = world, FakeZotero()
    # Many collections before litwatch's own, and an 0.x item with many notes before its own.
    for i in range(120):
        fake.add_item(f"COL{i:05d}", {})
        fake.collections[f"COL{i:05d}"] = fake.items.pop(f"COL{i:05d}")
        fake.collections[f"COL{i:05d}"]["data"]["name"] = f"Reading {i}"
    fake.collections["COL00119"]["data"]["name"] = "Litwatch"  # on the second page
    fake.add_item("OLDITEM1", {"itemType": "preprint", "title": P.title, "collections": []})
    for i in range(130):
        fake.add_item(f"N{i:05d}", {"itemType": "note", "parentItem": "OLDITEM1", "note": "mine"})
    fake.add_item(
        "OLDNOTE1",
        {
            "itemType": "note",
            "parentItem": "OLDITEM1",
            "note": "<!-- litwatch:screen schema=1 --><p>0.x</p>",
        },
    )
    z = zotero(fake)
    await start(conn, z)
    await grade(conn, "must_read")
    await run(conn, z)

    names = [c["data"]["name"] for c in fake.collections.values()]
    assert names.count("Litwatch") == 1 and "Litwatch" in fake.collection_names("OLDITEM1")
    screens = [n for n in fake.notes("OLDITEM1") if "litwatch:screen" in n]
    assert len(screens) == 1 and "schema=2" in screens[0]


async def test_an_item_is_found_past_the_first_page_of_matches(world):
    conn, fake = world, FakeZotero()
    # Thirty other items whose titles contain the paper's, then the paper's own.
    for i in range(30):
        fake.add_item(f"REPLY{i:03d}", {"itemType": "preprint", "title": f"{P.title}: reply {i}"})
    fake.add_item("THEITEM1", {"itemType": "preprint", "title": P.title})
    z = zotero(fake)
    await start(conn, z)
    await grade(conn, "must_read")
    await run(conn, z)
    assert len(fake.tops()) == 31
    assert await state.zotero_item(conn, P.id) == "THEITEM1"


async def test_a_paper_never_in_the_library_gets_no_item(world):
    conn, fake = world, FakeZotero()
    z = zotero(fake)
    await start(conn, z)
    await grade(conn, "adjacent")
    await run(conn, z)
    assert fake.items == {} and fake.calls == []


# -- webdav ----------------------------------------------------------------------------------------


@pytest.fixture
def pdfs(monkeypatch):
    monkeypatch.setattr(webdav_mod, "ARXIV_PACING_SECONDS", 0)
    fake, dav, arxiv = FakeZotero(), FakeDAV(), FakeArxiv()
    w = WebDav(
        client(fake),
        WebDAVWriter("https://dav.test/zotero", "u", "p", http=mock(dav.handle)),
        http=mock(arxiv.handle),
    )
    return fake, dav, arxiv, w


async def test_a_must_read_pdf_waits_for_its_item_then_is_stored_once(world, pdfs):
    conn = world
    fake, dav, arxiv, w = pdfs
    z = zotero(fake)
    for integration in (z, w):
        await start(conn, integration)
    await grade(conn, "must_read")

    await run(conn, w)  # before zotero made the item: retried later
    assert (await outbox.cursor(conn, w.name)).failing is not None
    await run(conn, z)
    await outbox.retry(conn, w.name)
    await run(conn, w)

    (item,) = fake.tops()
    (attachment,) = fake.attachments(item["key"])
    assert await state.webdav_file(conn, P.id) == {
        "item_key": item["key"],
        "attachment_key": attachment,
        "state": "stored",
    }
    assert "litwatch:pdf" in fake.tags(attachment)
    with zipfile.ZipFile(io.BytesIO(dav.files[f"{attachment}.zip"])) as zf:
        assert zf.read(attachment_filename(P)) == PDF
    assert f"{attachment}.prop" in dav.files
    assert arxiv.calls == [f"/pdf/{P.id}"]

    # Handled again (a restart mid-way, say): nothing is fetched or attached twice.
    for event in await outbox.after(conn, outbox.START, 100):
        if w.wants(event):
            await w.handle(conn, event)
    assert len(fake.attachments(item["key"])) == 1 and len(arxiv.calls) == 1


async def test_an_overruled_or_unsaved_must_read_fetches_no_pdf_until_it_is_back(world, pdfs):
    conn = world
    fake, dav, arxiv, w = pdfs
    z = zotero(fake)
    for integration in (z, w):
        await start(conn, integration)

    # The verdict says must read; the owner says relevant before webdav gets to it.
    await grade(conn, "must_read")
    await say(conn, LabelValue.RELATED)
    await run(conn, z, w)
    assert arxiv.calls == [] and dav.files == {}

    # Must read again, but unsaved by hand: still nothing.
    await say(conn, LabelValue.CENTRAL)
    await word.toggle(conn, P.id, Hand.UNSAVE)
    await run(conn, z, w)
    assert arxiv.calls == []

    # Saved again: `paper.saved` does what was skipped.
    await word.toggle(conn, P.id, Hand.SAVE)
    await run(conn, z, w)
    assert arxiv.calls == [f"/pdf/{P.id}"] and await state.webdav_file(conn, P.id)


async def test_an_interrupted_upload_is_finished_not_taken_for_done(world, pdfs):
    conn = world
    fake, dav, arxiv, w = pdfs
    z = zotero(fake)
    for integration in (z, w):
        await start(conn, integration)
    await grade(conn, "must_read")
    await run(conn, z)
    dav.fail = {".prop"}  # the zip arrives, the prop does not
    await run(conn, w)
    (item,) = fake.tops()
    (attachment,) = fake.attachments(item["key"])
    assert (await state.webdav_file(conn, P.id))["state"] == "pending"
    assert (await outbox.cursor(conn, w.name)).failing is not None

    await outbox.retry(conn, w.name)
    await run(conn, w)
    assert fake.attachments(item["key"]) == [attachment]
    assert {f"{attachment}.zip", f"{attachment}.prop"} <= set(dav.files)
    assert (await state.webdav_file(conn, P.id))["state"] == "stored"


async def test_an_attachment_made_by_a_lost_create_is_found_and_finished(world, pdfs):
    conn = world
    fake, dav, arxiv, w = pdfs
    z = zotero(fake)
    for integration in (z, w):
        await start(conn, integration)
    await grade(conn, "must_read")
    await run(conn, z)
    fake.lose_answers = 1  # the attachment is made; its answer is lost
    await run(conn, w)
    await outbox.retry(conn, w.name)
    await run(conn, w)
    (item,) = fake.tops()
    (attachment,) = fake.attachments(item["key"])
    assert await state.webdav_file(conn, P.id) == {
        "item_key": item["key"],
        "attachment_key": attachment,
        "state": "stored",
    }
    assert f"{attachment}.prop" in dav.files


async def test_the_owners_own_pdf_is_left_alone(world, pdfs):
    conn = world
    fake, dav, arxiv, w = pdfs
    z = zotero(fake)
    for integration in (z, w):
        await start(conn, integration)
    await grade(conn, "must_read")
    await run(conn, z)
    (item,) = fake.tops()
    fake.add_item(
        "OWNERPDF",
        {"itemType": "attachment", "parentItem": item["key"], "contentType": "application/pdf"},
    )
    await run(conn, w)
    assert await state.webdav_file(conn, P.id) == {
        "item_key": item["key"],
        "attachment_key": "OWNERPDF",
        "state": "owner",
    }
    assert arxiv.calls == [] and dav.files == {}


async def test_an_item_made_again_gets_its_pdf_again(world, pdfs):
    conn = world
    fake, dav, arxiv, w = pdfs
    z = zotero(fake)
    for integration in (z, w):
        await start(conn, integration)
    await grade(conn, "must_read")
    await run(conn, z, w)
    (first,) = fake.tops()
    assert (await state.webdav_file(conn, P.id))["state"] == "stored"

    # The owner deletes the item in Zotero; unsaving and saving makes litwatch a new one.
    for key in [first["key"], *fake.attachments(first["key"]), *fake.notes_keys(first["key"])]:
        del fake.items[key]
    await word.toggle(conn, P.id, Hand.UNSAVE)
    await word.toggle(conn, P.id, Hand.SAVE)
    await run(conn, z, w)
    (second,) = fake.tops()
    assert second["key"] != first["key"]
    (attachment,) = fake.attachments(second["key"])
    assert await state.webdav_file(conn, P.id) == {
        "item_key": second["key"],
        "attachment_key": attachment,
        "state": "stored",
    }
    assert f"{attachment}.prop" in dav.files


def test_an_item_is_the_papers_by_its_own_identifiers_only():
    assert is_item_of({"url": "https://arxiv.org/pdf/2610.00001v3.pdf"}, "2610.00001")
    assert is_item_of({"archiveID": "arXiv:2610.00001"}, "2610.00001")
    assert is_item_of({"DOI": "10.48550/arXiv.2610.00001"}, "2610.00001")
    assert is_item_of({"extra": "tex.key: x\narXiv: 2610.00001v2"}, "2610.00001")
    assert is_item_of({"url": "https://arxiv.org/abs/hep-th/9901001"}, "hep-th/9901001")
    assert not is_item_of({"url": "https://arxiv.org/abs/2610.000012"}, "2610.00001")
    assert not is_item_of({"extra": "Related: https://arxiv.org/abs/2610.00001"}, "2610.00001")
    assert not is_item_of({"abstractNote": "2610.00001"}, "2610.00001")
    # The Extra line litwatch itself writes, on an item since moved to its published version.
    assert is_item_of(
        {"url": "https://doi.org/10.1/x", "extra": "arXiv:2610.00001 [cs.CL]"}, "2610.00001"
    )
    # A standalone PDF is no paper's item: notes cannot hang under it.
    pdf = {"itemType": "attachment", "url": "https://arxiv.org/pdf/2610.00001"}
    assert not is_item_of(pdf, "2610.00001")
