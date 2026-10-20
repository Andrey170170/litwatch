"""The notion integration, driven through the dispatcher against an in-memory fake of Notion's
API (`tests.fakes_http.FakeNotion`). Nothing here reaches Notion."""

import re
from datetime import UTC, datetime, timedelta
from urllib.parse import unquote

import httpx
import pytest

from litwatch import events, store
from litwatch.api import create_app
from litwatch.config import Config
from litwatch.core import (
    Cause,
    Depth,
    Effective,
    Fit,
    Grade,
    Label,
    LabelOrigin,
    LabelValue,
    Opinion,
    Publication,
    Rarity,
    Taxonomy,
    TaxonomyClass,
    Topic,
    Verdict,
)
from litwatch.events import dispatch
from litwatch.feedback import word
from litwatch.integrations.notion import Notion, NotionClient
from litwatch.jobs.publications import store_record
from litwatch.store import integrations as integrations_store
from litwatch.store import notion as state
from litwatch.store import outbox, verdicts
from litwatch.store import papers as papers_store
from litwatch.store import publications as publications_store
from litwatch.store import taxonomy as taxonomy_store
from litwatch.store import word as word_store
from tests.conftest import AGENTS, FAITH, INTERP, TREE, deliver, paper, settled
from tests.fakes_http import FakeNotion, mock

PARENT = "11111111-1111-1111-1111-111111111111"
OTHER = "22222222-2222-2222-2222-222222222222"
MON = Topic(
    slug="cot-monitoring",
    depth=Depth.FOCUS,
    monogram="CM",
    colour="#0ea5e9",
    subject="Whether a model's reasoning trace can be read to catch it misbehaving.",
    rubric="A monitor that reads reasoning traces, tested against models trying to evade it.",
)
TOPICS = Taxonomy(
    classes=[
        *TREE.classes,
        TaxonomyClass(
            slug=MON.slug,
            name="Chain-of-thought monitoring",
            description=MON.subject,
            parent="interpretability",
        ),
    ],
    topics=[FAITH, MON, INTERP, AGENTS],
)
FAITHFUL, MONITORING = "Chain-of-thought faithfulness", "Chain-of-thought monitoring"
A = paper("2610.00001", title="Traces that hide steps", published="2026-10-01T12:00:00+00:00")
B = paper("2610.00002", title="Watching the reasoning", abstract="Monitors read the trace.")
C = paper("2610.00003", title="A third paper")
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
    "prior_work": [
        {"title": "Turpin et al.", "relation": "Reuses the setup.", "arxiv_id": "2305.04388"}
    ],
    "code_available": True,
    "code_url": "https://github.com/x/y",
    "limitations": ["Two models only."],
}
# Internal terms and voice the reader of the export must not meet (GLOSSARY.md, Notion export).
INTERNAL = re.compile(r"\b(central|Fit|Weight|verdict)\b|for you|required reading", re.I)


@pytest.fixture
async def world(conn):
    await taxonomy_store.save(conn, TOPICS)
    await papers_store.upsert(conn, [A, B, C])
    return conn


def notion(fake: FakeNotion, **kw) -> Notion:
    """The integration over the fake; `sleeps` on its client records what it waited."""
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    client = NotionClient("notion-token", http=mock(fake.handle), pace=0, sleep=sleep)
    client.sleeps = sleeps
    options = {"parent_page": PARENT, "owner_name": "Andrei", "adopt_wait": 0} | kw
    return Notion(client, **options)


async def start(conn, integration, now=None) -> bool:
    await settled(conn)
    return await dispatch.start(conn, integration, now)


async def grade(conn, p, topic: str, g: str) -> None:
    """A verdict whose grade, with no Weight yet, is `g`."""
    fit = {"must_read": Fit.CENTRAL, "relevant": Fit.RELATED}.get(g) or Fit(g)
    opinion = Opinion(fit=fit, reason="r", model="anthropic:x")
    await verdicts.add(
        conn,
        Verdict(
            paper_id=p.id,
            topic=topic,
            topic_version="v",
            fit=fit,
            reason="r",
            opinions={"judge": opinion},
        ),
    )
    await word.settle(conn, p.id, cause=Cause.VERDICT)


async def say(conn, p, topic: str, value: LabelValue) -> None:
    label = Label(paper_id=p.id, topic=topic, value=value, origin=LabelOrigin.READER)
    await word.label(conn, label)


async def texts(conn, p, gist: str | None = None, brief: str | None = None) -> None:
    """Store a Gist and a Brief for the paper's first revision."""
    if gist:
        await conn.execute(
            "INSERT INTO gists (paper_id, paper_revision, text, source, model) "
            "VALUES (%s, 1, %s, 'abstract', 'm')",
            (p.id, gist),
        )
    if brief:
        row = await (
            await conn.execute(
                "INSERT INTO fulltext_passes (paper_id, paper_revision, kind, reason, state, "
                "priority) VALUES (%s, 1, 'brief', 'focus', 'done', 0) RETURNING id",
                (p.id,),
            )
        ).fetchone()
        await conn.execute(
            "INSERT INTO briefs (paper_id, paper_revision, pass_id, text, text_source, "
            "text_hash, model) VALUES (%s, 1, %s, %s, 'latex', 'h', 'm')",
            (p.id, row["id"], brief),
        )


def plain(rich: list) -> str:
    return "".join(t.get("text", {}).get("content", "") for t in rich)


def created(fake: FakeNotion, arxiv_id: str) -> dict:
    """The body of the request that made the paper's page."""
    (body,) = [
        b
        for _, _, b in fake.kept("POST", "/pages")
        if b["parent"]["type"] == "data_source_id"
        and plain(b["properties"]["arXiv id"]["rich_text"]) == arxiv_id
    ]
    return body


async def last_day(conn) -> str:
    """The day of the newest `grade.changed`, as Why lines write it."""
    row = await (
        await conn.execute(
            "SELECT created_at FROM outbox WHERE type = %s ORDER BY id DESC LIMIT 1",
            (events.GRADE_CHANGED,),
        )
    ).fetchone()
    d = row["created_at"].astimezone(UTC)
    return f"{d.day} {d:%b %Y}"


# -- what a page holds -----------------------------------------------------------------------------


async def test_a_must_read_with_a_deep_card_gets_every_column_and_its_deep_read(world):
    conn, fake = world, FakeNotion(PARENT)
    await texts(conn, A, gist="Probes catch hidden steps.", brief="First.\n\nSecond.")
    await integrations_store.put_card(
        conn, A.id, CARD, "deep", {}, [], revision=1, version=1, source="latex", pass_id=None
    )
    await grade(conn, A, FAITH.slug, "must_read")
    n = notion(fake)
    assert await start(conn, n)

    db = await state.database(conn, n.name)
    column = db.topics[FAITH.slug]["column"]
    props = created(fake, A.id)["properties"]
    assert props["title"]["title"][0]["text"]["content"] == A.title
    assert props["Grade"] == {"select": {"name": "Must read"}}
    assert props[column] == {"select": {"name": "Must read"}}
    assert props["Topic"] == {"multi_select": [{"name": FAITHFUL}]}
    assert plain(props["TL;DR"]["rich_text"]) == "Probes catch hidden steps."
    assert props["Published"] == {"date": {"start": "2026-10-01"}}
    assert plain(props["Authors"]["rich_text"]) == "Alice Smith, Bob Jones"
    assert props["arXiv"] == {"url": f"https://arxiv.org/abs/{A.id}"}
    assert props["PDF"] == {"url": f"https://arxiv.org/pdf/{A.id}"}
    assert props["Why"] == {"rich_text": []}
    assert "Status" not in props  # the reader's

    body = created(fake, A.id)["children"]
    guide = body[0]["callout"]
    assert guide["icon"]["emoji"] == "💡" and guide["rich_text"][0]["mention"]["page"] == {
        "id": db.guide_page
    }
    headings = [plain(b["heading_2"]["rich_text"]) for b in body if b["type"] == "heading_2"]
    assert headings == ["TL;DR", "Summary", "Deep read"]
    summary = [plain(b["paragraph"]["rich_text"]) for b in body if b["type"] == "paragraph"]
    assert summary == ["Probes catch hidden steps.", "First.", "Second."]  # the Brief
    deep = body[-1]["heading_2"]
    assert deep["is_toggleable"]
    icons = [c["callout"]["icon"]["emoji"] for c in deep["children"] if c["type"] == "callout"]
    assert icons == ["💬", "📖", "🎯", "🧪", "📚", "⚠️"]  # no monitoring section on this card
    prior = deep["children"][4]["callout"]["rich_text"]
    assert {"url": "https://arxiv.org/abs/2305.04388"} in [t["text"].get("link") for t in prior]
    code = deep["children"][-1]["paragraph"]["rich_text"]
    assert code[-1]["text"]["link"] == {"url": "https://github.com/x/y"}

    # The views: All papers is the new database's own view, grouped by Grade; the topic's is
    # filtered to its grade column and grouped by it; both newest published first and listing
    # every column, only Title, Grade, Topic, TL;DR, Published and Status shown.
    source = fake.source()["properties"]
    ids = {name: unquote(p["id"]) for name, p in source.items()}
    everything, by_topic = fake.view_named("All papers"), fake.view_named(FAITHFUL)
    assert db.all_view == everything["id"] and db.topics[FAITH.slug]["view"] == by_topic["id"]
    assert everything["configuration"]["group_by"]["property_id"] == ids["Grade"]
    assert by_topic["configuration"]["group_by"]["property_id"] == column
    assert by_topic["filter"] == {"property": column, "select": {"is_not_empty": True}}
    for view in (everything, by_topic):
        assert view["sorts"] == [{"property": ids["Published"], "direction": "descending"}]
        listed = {c["property_id"]: c["visible"] for c in view["configuration"]["properties"]}
        assert set(listed) == set(ids.values())
        shown = {name for name, i in ids.items() if listed[i]}
        assert shown == {"Title", "Grade", "Topic", "TL;DR", "Published", "Status"}

    # "How to read this" names the owner, the topics and the grades in plain words.
    guide_page = fake.body(db.guide_page)
    words = " ".join(plain(b[b["type"]]["rich_text"]) for b in guide_page)
    assert "Andrei" in words and FAITHFUL in words and MONITORING in words
    assert "Must read" in words and "Worth a look" in words and "No longer listed" in words
    assert not INTERNAL.search(words)


async def test_a_worth_a_look_without_a_brief_shows_its_abstract(world):
    conn, fake = world, FakeNotion(PARENT)
    await grade(conn, B, MON.slug, "relevant")
    assert await start(conn, notion(fake))

    page = created(fake, B.id)
    assert page["properties"]["Grade"] == {"select": {"name": "Worth a look"}}
    assert page["properties"]["Topic"] == {"multi_select": [{"name": MONITORING}]}
    assert page["properties"]["TL;DR"] == {"rich_text": []}
    body = page["children"]
    headings = [plain(b["heading_2"]["rich_text"]) for b in body if b["type"] == "heading_2"]
    assert headings == ["Summary"]  # no Gist, no card
    assert plain(body[-1]["paragraph"]["rich_text"]) == B.abstract
    # Papers below the minimum get no page.
    assert [fake.value(r, "arXiv id") for r in fake.rows()] == [B.id]


async def test_venue_links_to_the_published_version(world):
    conn, fake = world, FakeNotion(PARENT)
    await grade(conn, A, FAITH.slug, "must_read")
    async with conn.transaction():
        record = Publication(venue="NeurIPS", year=2026, doi="10.5555/x")
        await publications_store.put(conn, A.id, record)
    assert await start(conn, notion(fake))
    venue = created(fake, A.id)["properties"]["Venue"]["rich_text"]
    assert plain(venue) == "NeurIPS 2026"
    assert venue[0]["text"]["link"] == {"url": "https://doi.org/10.5555/x"}

    async with conn.transaction():  # the owner's DOI, which DBLP did not know
        await publications_store.put(conn, A.id, Publication(doi="10.5555/y"), by_owner=True)
    assert await start(conn, notion(fake))
    venue = fake.row(A.id)["properties"][unquote(fake.source()["properties"]["Venue"]["id"])]
    assert plain(venue["rich_text"]) == "Published version"
    assert venue["rich_text"][0]["text"]["link"] == {"url": "https://doi.org/10.5555/y"}


async def test_new_texts_a_card_and_a_published_record_reach_the_page_as_they_come(world):
    conn, fake = world, FakeNotion(PARENT)
    await grade(conn, A, FAITH.slug, "must_read")
    n = notion(fake)
    assert await start(conn, n)
    row = fake.row(A.id)

    def headings() -> list[str]:
        return [
            plain(b["heading_2"]["rich_text"])
            for b in fake.body(row["id"])
            if b["type"] == "heading_2"
        ]

    async def emit(kind: str) -> None:
        async with conn.transaction():
            await events.emit(conn, kind, A.id)

    await texts(conn, A, gist="Probes catch hidden steps.")
    await emit(events.GIST_WRITTEN)
    await deliver(conn, n)
    assert fake.value(row, "TL;DR") == "Probes catch hidden steps."
    await texts(conn, A, brief="From the full text.")
    await emit(events.BRIEF_WRITTEN)
    await deliver(conn, n)
    assert "From the full text." in [
        plain(b["paragraph"]["rich_text"]) for b in fake.body(row["id"]) if b["type"] == "paragraph"
    ]
    await integrations_store.put_card(
        conn, A.id, CARD, "deep", {}, [], revision=1, version=1, source="latex", pass_id=None
    )
    await emit(events.CARD_READY)
    await deliver(conn, n)
    assert headings() == ["TL;DR", "Summary", "Deep read"]
    await store_record(conn, A.id, Publication(venue="ICLR", year=2027))  # emits its event
    await deliver(conn, n)
    assert fake.value(row, "Venue") == "ICLR 2027"
    assert (await outbox.cursor(conn, n.name)).failing is None

    asked = len(fake.requests)  # each again: nothing to write
    for event in await outbox.after(conn, outbox.START, 1000):
        if n.wants(event) and event.type != events.GRADE_CHANGED:
            await n.handle(conn, event)
    assert len(fake.requests) == asked


async def test_a_delisted_paper_says_why_and_listing_it_again_clears_that(world):
    conn, fake = world, FakeNotion(PARENT)
    await grade(conn, A, FAITH.slug, "relevant")
    n = notion(fake)
    assert await start(conn, n)
    row = fake.row(A.id)

    await say(conn, A, FAITH.slug, LabelValue.NONE)
    await deliver(conn, n)
    why = f"{FAITHFUL}, {await last_day(conn)}: Andrei marked it off-topic."
    assert fake.value(row, "Grade") == "No longer listed"
    assert fake.value(row, FAITHFUL) == "No longer listed"
    assert fake.value(row, "Topic") == [FAITHFUL]  # it stays in the topic's view
    assert fake.value(row, "Why") == why
    (top, *_) = fake.body(row["id"])
    assert top["callout"]["icon"]["emoji"] == "🗄️"
    assert plain(top["callout"]["rich_text"]) == f"No longer listed.\n{why}"

    await word.unlabel(conn, A.id, FAITH.slug)
    await deliver(conn, n)
    assert fake.value(row, "Grade") == "Worth a look" and fake.value(row, "Why") == ""
    assert [b["callout"]["icon"]["emoji"] for b in fake.body(row["id"])[:1]] == ["💡"]
    assert len(fake.rows()) == 1  # never deleted, never made twice


async def test_the_why_is_the_newest_change_that_took_it_off(world):
    conn, fake = world, FakeNotion(PARENT)
    await grade(conn, A, FAITH.slug, "relevant")
    await grade(conn, B, FAITH.slug, "relevant")
    await grade(conn, C, FAITH.slug, "relevant")
    n = notion(fake)
    assert await start(conn, n)
    verdicts = f"{FAITHFUL}, {{}}: litwatch re-checked it and judged it off-topic."

    # Handled one by one: the owner takes A off; then it returns and a verdict takes it off.
    await say(conn, A, FAITH.slug, LabelValue.NONE)
    await deliver(conn, n)
    assert "Andrei marked it" in fake.value(fake.row(A.id), "Why")
    await word.unlabel(conn, A.id, FAITH.slug)
    await grade(conn, A, FAITH.slug, "adjacent")
    await deliver(conn, n)
    assert fake.value(fake.row(A.id), "Why") == verdicts.format(await last_day(conn))

    # Handed over together: every event finds B already off for the verdict's reason.
    await say(conn, B, FAITH.slug, LabelValue.NONE)
    await word.unlabel(conn, B.id, FAITH.slug)
    await grade(conn, B, FAITH.slug, "adjacent")
    await deliver(conn, n)
    assert fake.value(fake.row(B.id), "Why") == verdicts.format(await last_day(conn))

    # Dismissed after such a sequence, the topic it had left keeps the newest reason.
    await say(conn, C, FAITH.slug, LabelValue.NONE)
    await deliver(conn, n)
    await word.unlabel(conn, C.id, FAITH.slug)
    await grade(conn, C, FAITH.slug, "adjacent")
    day = await last_day(conn)
    await papers_store.dismiss(conn, C.id)
    assert await start(conn, notion(fake))
    assert fake.value(fake.row(C.id), "Why") == verdicts.format(day)


async def test_a_topic_exported_again_says_why_its_paper_left_meanwhile(world):
    conn, fake = world, FakeNotion(PARENT)
    await grade(conn, A, FAITH.slug, "relevant")
    await grade(conn, A, MON.slug, "relevant")
    assert await start(conn, notion(fake))
    only = notion(fake, topics={MON.slug: MONITORING})
    assert await start(conn, only)
    await grade(conn, A, FAITH.slug, "adjacent")  # not wanted while FAITH is not exported
    await deliver(conn, only)

    assert await start(conn, notion(fake))
    row = fake.row(A.id)
    assert fake.value(row, FAITHFUL) == "No longer listed"
    why = f"{FAITHFUL}, {await last_day(conn)}: litwatch re-checked it and judged it off-topic."
    assert fake.value(row, "Why") == why


async def test_an_event_from_before_causes_were_recorded_reads_as_a_verdict(world):
    conn, fake = world, FakeNotion(PARENT)
    await grade(conn, A, FAITH.slug, "relevant")
    n = notion(fake)
    assert await start(conn, n)
    adjacent = Effective(fit=Fit.ADJACENT, grade=Grade.ADJACENT, rarity=Rarity.UNCOMMON)
    async with conn.transaction():
        await word_store.set_effective(conn, A.id, FAITH.slug, adjacent)
        await events.emit(
            conn,
            events.GRADE_CHANGED,
            A.id,
            topic=FAITH.slug,
            before="relevant",
            after="adjacent",
            fit_before="related",
            fit_after="adjacent",
        )  # no `cause`
    await deliver(conn, n)
    assert (await outbox.cursor(conn, n.name)).failing is None
    day = await last_day(conn)
    expected = f"{FAITHFUL}, {day}: litwatch re-checked it and judged it off-topic."
    assert fake.value(fake.row(A.id), "Why") == expected


async def test_a_repeated_event_or_a_restart_sends_no_write(world):
    conn, fake = world, FakeNotion(PARENT)
    await grade(conn, A, FAITH.slug, "must_read")
    n = notion(fake)
    assert await start(conn, n)
    await say(conn, A, FAITH.slug, LabelValue.RELATED)
    await deliver(conn, n)
    assert fake.value(fake.row(A.id), "Grade") == "Worth a look"
    before = len(fake.requests)

    # The same event again: the page already says it.
    changes = [e for e in await outbox.after(conn, outbox.START, 1000) if n.wants(e)]
    await n.handle(conn, changes[-1])
    assert len(fake.requests) == before

    # A restart: nothing is written, and a view the owner deleted is not made again.
    fake.views.pop((await state.database(conn, n.name)).topics[FAITH.slug]["view"])
    assert await start(conn, notion(fake))
    assert [r[0] for r in fake.requests[before:]] and all(
        r[0] == "GET" for r in fake.requests[before:]
    )


# -- Notion failing --------------------------------------------------------------------------------


async def test_a_429_is_waited_out_as_retry_after_says(world):
    conn, fake = world, FakeNotion(PARENT)
    await grade(conn, A, FAITH.slug, "must_read")
    n = notion(fake)
    fake.limited = 1
    assert await start(conn, n)
    assert n.notion.sleeps == [7.0]
    assert fake.requests[0][:2] == fake.requests[1][:2]  # the same request, sent again
    assert len(fake.rows()) == 1


async def test_a_page_whose_creation_reply_was_lost_is_adopted_after_a_restart(world):
    conn, fake = world, FakeNotion(PARENT)
    await grade(conn, A, FAITH.slug, "must_read")
    t0 = datetime(2026, 10, 19, 12, tzinfo=UTC)
    fake.lose_answers = 1
    assert not await start(conn, notion(fake), t0)  # made, but its answer was a 502
    assert len(fake.rows()) == 1
    assert (await state.page(conn, "notion", A.id)).page_id is None  # begun, not recorded

    # Restarted at once, with Notion's query still behind the fresh page: it asks again.
    fake.hidden = 1
    assert await start(conn, notion(fake), t0 + timedelta(minutes=1))
    (row,) = fake.rows()
    assert (await state.page(conn, "notion", A.id)).page_id == row["id"]
    assert len(fake.kept("POST", "/data_sources")) >= 2
    # The adopted page is written whole: one body, not two.
    headings = [b for b in fake.body(row["id"]) if b["type"] == "heading_2"]
    assert len(headings) == 1


async def test_a_page_whose_reply_was_lost_is_delisted_if_its_paper_left_meanwhile(world):
    conn, fake = world, FakeNotion(PARENT)
    await grade(conn, A, FAITH.slug, "must_read")
    t0 = datetime(2026, 10, 19, 12, tzinfo=UTC)
    fake.lose_answers = 1
    assert not await start(conn, notion(fake), t0)
    await grade(conn, A, FAITH.slug, "adjacent")

    assert await start(conn, notion(fake), t0 + timedelta(minutes=1))
    (row,) = fake.rows()
    assert fake.value(row, "Grade") == "No longer listed"
    assert "litwatch re-checked it" in fake.value(row, "Why")


async def test_a_creation_that_never_happened_makes_no_page_once_its_paper_left(world):
    conn, fake = world, FakeNotion(PARENT)
    assert await start(conn, notion(fake))
    listed = {"grade": "must_read", "shown": "Must read", "why": None, "since": None}
    begun = state.Page(A.id, added=datetime(2026, 10, 19).date(), topics={FAITH.slug: listed})
    await state.save_page(conn, "notion", begun)  # saved, then stopped before the request
    await grade(conn, A, FAITH.slug, "adjacent")

    assert await start(conn, notion(fake))
    assert fake.rows() == []
    assert await state.page(conn, "notion", A.id) is None


async def test_a_column_put_back_is_filled_even_when_the_reply_was_lost(world):
    conn, fake = world, FakeNotion(PARENT)
    await texts(conn, A, gist="Probes catch hidden steps.")
    await grade(conn, A, FAITH.slug, "must_read")
    t0 = datetime(2026, 10, 19, 12, tzinfo=UTC)
    assert await start(conn, notion(fake), t0)
    del fake.source()["properties"]["TL;DR"]  # the owner deleted the column
    fake.lose_patches = 1
    assert not await start(conn, notion(fake), t0 + timedelta(minutes=1))

    assert await start(conn, notion(fake), t0 + timedelta(minutes=2))
    assert fake.value(fake.row(A.id), "TL;DR") == "Probes catch hidden steps."


async def test_an_event_is_written_with_what_another_services_setup_made(world):
    conn, fake = world, FakeNotion(PARENT)
    await grade(conn, A, FAITH.slug, "must_read")
    n = notion(fake)
    assert await start(conn, n)
    del fake.source()["properties"][FAITHFUL]  # deleted, then made again by another service
    assert await start(conn, notion(fake))

    await say(conn, A, FAITH.slug, LabelValue.RELATED)
    await deliver(conn, n)
    assert (await outbox.cursor(conn, n.name)).failing is None
    assert fake.value(fake.row(A.id), FAITHFUL) == "Worth a look"


async def test_a_write_failing_part_way_and_then_undone_is_put_right(world):
    conn, fake = world, FakeNotion(PARENT)
    await grade(conn, A, FAITH.slug, "relevant")
    n = notion(fake)
    assert await start(conn, n)
    row = fake.row(A.id)
    body = [b["type"] for b in fake.body(row["id"])]
    await say(conn, A, FAITH.slug, LabelValue.NONE)
    fake.refuse_appends = 1  # its properties written, its body deleted and not yet replaced
    await deliver(conn, n)
    assert (await outbox.cursor(conn, n.name)).failing is not None
    assert fake.body(row["id"]) == []

    await word.unlabel(conn, A.id, FAITH.slug)  # undone before the retry
    await outbox.retry(conn, n.name)
    await deliver(conn, n)
    assert fake.value(row, "Grade") == "Worth a look" and fake.value(row, "Why") == ""
    assert [b["callout"]["icon"]["emoji"] for b in fake.body(row["id"])[:1]] == ["💡"]
    assert [b["type"] for b in fake.body(row["id"])] == body


async def test_a_failing_setup_is_retried_after_backoff_and_shown(world):
    conn, fake = world, FakeNotion(PARENT)
    n = notion(fake)
    dsn = f"{conn.test_dsn}?options=-csearch_path%3D{conn.test_schema}"
    app = create_app(dsn, "t", Config(), integrations=[n])
    api = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"authorization": "Bearer t"},
    )
    t0 = datetime(2026, 10, 19, 12, tzinfo=UTC)
    fake.down = True
    assert not await start(conn, n, t0)

    (card,) = (await api.get("/api/settings/integrations")).json()
    assert card["failing"] is None
    assert card["setup_failing"]["failures"] == 1
    assert "500" in card["setup_failing"]["error"]
    assert card["setup_failing"]["retry_at"] == "2026-10-19T12:00:30Z"
    assert (await api.get("/api/summary")).json()["settings"]["failing"] == ["notion"]

    asked = len(fake.requests)
    assert not await start(conn, n, t0 + timedelta(seconds=10))  # still backing off
    assert len(fake.requests) == asked
    assert not await start(conn, n, t0 + timedelta(seconds=31))
    assert len(fake.requests) > asked
    cursor = await outbox.cursor(conn, n.name)
    assert cursor.setup_failures == 2
    assert cursor.setup_retry_at == t0 + timedelta(seconds=91)  # 60 s after the second

    # The owner's retry waives the backoff.
    assert (await api.post("/api/settings/integrations/notion/retry")).status_code == 204
    fake.down = False
    assert await start(conn, n, t0 + timedelta(seconds=32))
    (card,) = (await api.get("/api/settings/integrations")).json()
    assert card["setup_failing"] is None
    await api.aclose()


async def test_an_implementation_without_setup_clears_its_predecessors_failure(world):
    conn, fake = world, FakeNotion(PARENT)
    fake.down = True
    assert not await start(conn, notion(fake))

    class Plain:
        name = "notion"

        def wants(self, event):
            return False

    assert await start(conn, Plain())
    assert (await outbox.cursor(conn, "notion")).setup_failures == 0


async def test_a_setup_while_another_holds_the_lock_makes_nothing(world):
    conn, fake = world, FakeNotion(PARENT)
    n = notion(fake)
    async with await store.connect(conn.test_dsn, conn.test_schema) as other:
        key = (dispatch.DISPATCH_LOCK, n.name)
        await other.execute("SELECT pg_advisory_lock(%s, hashtext(%s))", key)
        assert not await start(conn, n)
        assert fake.requests == []
        await other.execute("SELECT pg_advisory_unlock(%s, hashtext(%s))", key)
    assert await start(conn, n)
    assert len(fake.kept("POST", "/databases")) == 1


async def test_another_parent_page_drops_the_old_state(world):
    conn, fake = world, FakeNotion(PARENT, OTHER)
    await grade(conn, A, FAITH.slug, "must_read")
    assert await start(conn, notion(fake))
    first = await state.page(conn, "notion", A.id)

    assert await start(conn, notion(fake, parent_page=OTHER))
    db = await state.database(conn, "notion")
    assert db.parent_page == OTHER
    assert fake.objects[db.database_id]["parent"] == OTHER
    again = await state.page(conn, "notion", A.id)
    assert again.page_id != first.page_id
    assert fake.objects[again.page_id]["source"] == db.data_source_id


# -- reconcile follows the configuration -----------------------------------------------------------


async def test_a_raised_minimum_delists_with_its_why_and_a_lowered_one_lists_again(world):
    conn, fake = world, FakeNotion(PARENT)
    await grade(conn, A, FAITH.slug, "must_read")
    await grade(conn, B, FAITH.slug, "relevant")
    await grade(conn, C, FAITH.slug, "relevant")
    n = notion(fake)
    assert await start(conn, n)
    await say(conn, C, FAITH.slug, LabelValue.ADJACENT)
    await deliver(conn, n)
    owners = f"{FAITHFUL}, {await last_day(conn)}: Andrei marked it off-topic."
    assert fake.value(fake.row(C.id), "Why") == owners

    day = datetime(2026, 10, 19, 23, 30, tzinfo=UTC)
    assert await start(conn, notion(fake, minimum=Grade.MUST_READ, now=lambda: day))
    raised = f"{FAITHFUL}, 19 Oct 2026: the list now shows only Must read papers."
    assert fake.value(fake.row(A.id), "Grade") == "Must read"
    assert fake.value(fake.row(B.id), "Grade") == "No longer listed"
    assert fake.value(fake.row(B.id), "Why") == raised
    assert fake.value(fake.row(C.id), "Why") == owners  # kept

    assert await start(conn, notion(fake))
    assert fake.value(fake.row(B.id), "Grade") == "Worth a look"
    assert fake.value(fake.row(B.id), "Why") == ""
    assert fake.value(fake.row(C.id), "Why") == owners
    assert len(fake.rows()) == 3


async def test_a_raised_minimum_keeps_the_cause_of_a_change_not_yet_handed_over(world):
    conn, fake = world, FakeNotion(PARENT)
    await grade(conn, A, FAITH.slug, "relevant")
    assert await start(conn, notion(fake))
    await say(conn, A, FAITH.slug, LabelValue.NONE)  # then a restart, before it is delivered

    assert await start(conn, notion(fake, minimum=Grade.MUST_READ))
    why = f"{FAITHFUL}, {await last_day(conn)}: Andrei marked it off-topic."
    assert fake.value(fake.row(A.id), "Why") == why

    # A change that moves the Fit but not the grade (a Weight holding it) says nothing new.
    async with conn.transaction():
        await events.emit(
            conn,
            events.GRADE_CHANGED,
            A.id,
            topic=FAITH.slug,
            before="relevant",
            after="relevant",
            fit_before="related",
            fit_after="central",
            cause=Cause.VERDICT.value,
        )
    assert await start(conn, notion(fake, minimum=Grade.MUST_READ))
    assert fake.value(fake.row(A.id), "Why") == why


async def test_a_dropped_topic_keeps_its_fields_and_its_own_pages_are_not_written(world):
    conn, fake = world, FakeNotion(PARENT)
    await grade(conn, A, FAITH.slug, "must_read")  # its own page
    await grade(conn, B, FAITH.slug, "must_read")  # a page shared with the topic kept
    await grade(conn, B, MON.slug, "must_read")
    assert await start(conn, notion(fake))
    a, b = fake.row(A.id), fake.row(B.id)
    await grade(conn, A, FAITH.slug, "relevant")
    await grade(conn, B, FAITH.slug, "relevant")
    await grade(conn, B, MON.slug, "relevant")
    asked = len(fake.requests)

    assert await start(conn, notion(fake, topics={MON.slug: MONITORING}))
    assert not [r for r in fake.requests[asked:] if a["id"] in r[1]]
    assert fake.value(a, "Grade") == "Must read" and fake.value(a, FAITHFUL) == "Must read"
    assert fake.value(b, "Grade") == "Worth a look" and fake.value(b, MONITORING) == "Worth a look"
    assert fake.value(b, FAITHFUL) == "Must read"  # the dropped topic's, as last written
    assert fake.value(b, "Topic") == [MONITORING, FAITHFUL]  # exported ones first
    # The dropped topic keeps its column and its view.
    assert FAITHFUL in fake.source()["properties"] and fake.view_named(FAITHFUL)


async def test_an_added_topic_gets_its_column_view_and_papers_without_rewriting_the_rest(world):
    conn, fake = world, FakeNotion(PARENT)
    await grade(conn, A, FAITH.slug, "must_read")
    await grade(conn, B, MON.slug, "relevant")
    assert await start(conn, notion(fake, topics={FAITH.slug: FAITHFUL}))
    assert [fake.value(r, "arXiv id") for r in fake.rows()] == [A.id]
    a = fake.row(A.id)
    asked = len(fake.requests)

    assert await start(conn, notion(fake))
    assert MONITORING in fake.source()["properties"]
    view = fake.view_named(MONITORING)
    column = (await state.database(conn, "notion")).topics[MON.slug]["column"]
    assert view["configuration"]["group_by"]["property_id"] == column
    assert fake.value(fake.row(B.id), MONITORING) == "Worth a look"
    assert not [r for r in fake.requests[asked:] if a["id"] in r[1]]


async def test_a_renamed_topic_renames_its_column_and_option_but_not_its_view(world):
    conn, fake = world, FakeNotion(PARENT)
    await grade(conn, A, FAITH.slug, "must_read")
    assert await start(conn, notion(fake, topics={FAITH.slug: "CoT faithfulness"}))
    column = (await state.database(conn, "notion")).topics[FAITH.slug]["column"]

    assert await start(conn, notion(fake, topics={FAITH.slug: "Faithfulness"}))
    props = fake.source()["properties"]
    assert "CoT faithfulness" not in props
    assert unquote(props["Faithfulness"]["id"]) == column
    assert [o["name"] for o in props["Topic"]["multi_select"]["options"]] == ["Faithfulness"]
    assert fake.value(fake.row(A.id), "Topic") == ["Faithfulness"]
    assert fake.value(fake.row(A.id), "Faithfulness") == "Must read"
    assert fake.view_named("CoT faithfulness")  # the owner's title


async def test_a_topic_option_deleted_in_notion_is_put_back_on_its_rows(world):
    conn, fake = world, FakeNotion(PARENT)
    await grade(conn, A, FAITH.slug, "must_read")
    assert await start(conn, notion(fake))
    kept = {"Topic": {"multi_select": {"options": [{"name": MONITORING}]}}}
    fake._properties(fake.source()["id"], kept)  # the owner deleted the option
    assert fake.value(fake.row(A.id), "Topic") == []

    assert await start(conn, notion(fake))
    assert fake.value(fake.row(A.id), "Topic") == [FAITHFUL]


async def test_a_rename_failing_part_way_and_then_undone_puts_topic_back(world):
    conn, fake = world, FakeNotion(PARENT)
    await grade(conn, A, FAITH.slug, "must_read")
    t0 = datetime(2026, 10, 19, 12, tzinfo=UTC)
    assert await start(conn, notion(fake, topics={FAITH.slug: "Research"}), t0)
    fake.refuse_appends = 1  # the old option goes from the rows, then the guide's rewrite fails
    later = t0 + timedelta(minutes=1)
    assert not await start(conn, notion(fake, topics={FAITH.slug: "Faith"}), later)
    assert fake.value(fake.row(A.id), "Topic") == []

    assert await start(
        conn, notion(fake, topics={FAITH.slug: "Research"}), later + timedelta(hours=1)
    )
    assert fake.value(fake.row(A.id), "Topic") == ["Research"]


async def test_a_topic_may_not_take_the_name_of_one_exported_before(world):
    conn, fake = world, FakeNotion(PARENT)
    assert await start(conn, notion(fake, topics={FAITH.slug: "Faith"}))
    assert not await start(conn, notion(fake, topics={MON.slug: "Faith"}))
    assert "Faith" in (await outbox.cursor(conn, "notion")).setup_error
    # Nor that of one renamed in the same change.
    renamed = {FAITH.slug: "Faithfulness", MON.slug: "Faith"}
    assert not await start(
        conn, notion(fake, topics=renamed), datetime.now(UTC) + timedelta(hours=1)
    )
    assert len({e["column"] for e in (await state.database(conn, "notion")).topics.values()}) == 1


async def test_a_new_topic_never_takes_a_view_litwatch_made_for_another(world):
    conn, fake = world, FakeNotion(PARENT)
    assert await start(conn, notion(fake, topics={MON.slug: MONITORING}))
    db = await state.database(conn, "notion")
    fake.views[db.all_view]["name"] = FAITHFUL  # the owner renamed All papers

    assert await start(conn, notion(fake))
    db = await state.database(conn, "notion")
    view = fake.views[db.topics[FAITH.slug]["view"]]
    assert view["id"] != db.all_view and "filter" in view

    # The same, with the topic view made but its reply lost: that one is adopted.
    await conn.execute(
        "UPDATE notion_databases SET topics = jsonb_set(topics, %s, 'null')",
        ([FAITH.slug, "view"],),
    )
    assert await start(conn, notion(fake))
    assert (await state.database(conn, "notion")).topics[FAITH.slug]["view"] == view["id"]
    assert len([v for v in fake.views.values() if v["name"] == FAITHFUL]) == 2


# -- taken away: a dismissed paper, a page trashed in Notion ---------------------------------------


async def test_a_dismissed_paper_is_no_longer_listed_in_the_owners_words(world):
    conn, fake = world, FakeNotion(PARENT)
    await grade(conn, A, FAITH.slug, "must_read")
    await grade(conn, A, MON.slug, "relevant")
    await grade(conn, C, FAITH.slug, "must_read")
    await papers_store.dismiss(conn, C.id)  # dismissed before it had a page
    n = notion(fake)
    assert await start(conn, n)
    assert [fake.value(r, "arXiv id") for r in fake.rows()] == [A.id]
    await say(conn, A, MON.slug, LabelValue.NONE)  # MON drops it first, for its own reason
    await deliver(conn, n)
    off = f"{MONITORING}, {await last_day(conn)}: Andrei marked it off-topic."

    await papers_store.dismiss(conn, A.id)
    assert await start(conn, notion(fake))
    d = (await papers_store.dismissed_at(conn, A.id)).astimezone(UTC)
    row = fake.row(A.id)
    assert fake.value(row, "Grade") == "No longer listed"
    assert fake.value(row, FAITHFUL) == "No longer listed"
    aside = f"{FAITHFUL}, {d.day} {d:%b %Y}: Andrei set it aside."
    assert fake.value(row, "Why") == f"{aside}\n{off}"
    assert [fake.value(r, "arXiv id") for r in fake.rows()] == [A.id]  # still none for C

    # Undone (no path does it yet), the paper is listed again and its line goes.
    await conn.execute("UPDATE papers SET dismissed_at = NULL WHERE id = %s", (A.id,))
    assert await start(conn, notion(fake))
    assert fake.value(row, FAITHFUL) == "Must read" and fake.value(row, "Why") == off


async def test_a_page_taken_away_in_notion_is_left_alone_and_never_made_again(world):
    conn, fake = world, FakeNotion(PARENT)
    await grade(conn, A, FAITH.slug, "must_read")
    await grade(conn, B, FAITH.slug, "must_read")
    n = notion(fake)
    assert await start(conn, n)
    a, b = fake.row(A.id), fake.row(B.id)
    fake.trash(a["id"])
    fake.delete(b["id"])

    await say(conn, A, FAITH.slug, LabelValue.RELATED)
    await say(conn, B, FAITH.slug, LabelValue.RELATED)
    await deliver(conn, n)
    assert (await outbox.cursor(conn, n.name)).failing is None  # the events did not fail
    assert (await state.page(conn, n.name, A.id)).gone_at is not None  # trashed
    assert (await state.page(conn, n.name, B.id)).gone_at is not None  # deleted
    assert fake.value(a, "Grade") == "Must read"  # as it was taken away

    # From then on nothing goes to them, events and restarts alike, and none is made again.
    asked = len(fake.requests)
    await say(conn, A, FAITH.slug, LabelValue.NONE)
    await deliver(conn, n)
    assert await start(conn, notion(fake))
    assert not [r for r in fake.requests[asked:] if a["id"] in r[1] or b["id"] in r[1]]
    made = [r for r in fake.kept("POST", "/pages") if "data_source_id" in r[2]["parent"]]
    assert len(made) == 2  # A's and B's first pages only


async def test_a_guide_taken_away_is_made_again_on_the_next_start(world):
    conn, fake = world, FakeNotion(PARENT)
    assert await start(conn, notion(fake))
    first = (await state.database(conn, "notion")).guide_page
    fake.trash(first)

    assert await start(conn, notion(fake))
    again = (await state.database(conn, "notion")).guide_page
    assert again != first
    assert fake.objects[again]["parent"] == PARENT
    assert any(b["type"] == "heading_2" for b in fake.body(again))


async def test_a_trashed_database_fails_the_event_and_the_setup_and_takes_no_page_for_gone(world):
    conn, fake = world, FakeNotion(PARENT)
    await grade(conn, A, FAITH.slug, "must_read")
    n = notion(fake)
    assert await start(conn, n)
    db = await state.database(conn, n.name)
    fake.trash(PARENT)  # the page holding it: its rows and data source read as trashed too

    await say(conn, A, FAITH.slug, LabelValue.RELATED)
    await deliver(conn, n)
    cursor = await outbox.cursor(conn, n.name)
    assert cursor.failing is not None and "trash" in cursor.last_error
    assert (await state.page(conn, n.name, A.id)).gone_at is None

    # Restored, the event goes through; then a trashed database fails setup, with nothing to write.
    del fake.objects[PARENT]["in_trash"]
    await outbox.retry(conn, n.name)
    await deliver(conn, n)
    assert (await outbox.cursor(conn, n.name)).failing is None
    fake.trash(db.database_id)
    assert not await start(conn, notion(fake))
    assert "trash" in (await outbox.cursor(conn, n.name)).setup_error
