"""`db import-feedback`: the owner's feedback copied from one v1 database into another."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from litwatch import store
from litwatch.core import (
    Grade,
    Hand,
    Label,
    LabelOrigin,
    LabelValue,
    Reaction,
    ReactionReason,
    ReactionValue,
    Take,
    TakeSource,
    Verdict,
)
from litwatch.feedback import calibrate, carry, misses, word
from litwatch.store import digest as digest_store
from litwatch.store import feedback as feedback_store
from litwatch.store import papers as papers_store
from litwatch.store import topics as topics_store
from litwatch.store import verdicts
from tests.conftest import FAITH, TOPICS, paper

P1, P2, P3, P4, P5 = (paper(f"2610.0000{n}") for n in range(1, 6))
EARLY, LATE = datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 10, 5, tzinfo=UTC)


@pytest.fixture
async def source(conn):
    """A second v1 database: another schema on the test cluster, holding one paper more."""
    schema = "test_" + uuid4().hex
    async with await store.connect(conn.test_dsn) as admin:
        await admin.execute(f'CREATE SCHEMA "{schema}"')
        try:
            async with await store.connect(conn.test_dsn, schema) as c:
                await store.migrate(c)
                for db, papers in ((conn, [P1, P2, P3, P4]), (c, [P1, P2, P3, P4, P5])):
                    for topic in TOPICS:
                        await topics_store.save(db, topic)
                    await papers_store.upsert(db, papers)
                yield c
        finally:
            await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')


def label(p, value: LabelValue, origin=LabelOrigin.READER, grade=None) -> Label:
    return Label(paper_id=p.id, topic=FAITH.slug, value=value, origin=origin, grade=grade)


async def at(conn, table: str, column: str, when: datetime, paper_id: str) -> None:
    await conn.execute(f"UPDATE {table} SET {column} = %s WHERE paper_id = %s", (when, paper_id))


async def rows(conn, query: str, *params):
    return await (await conn.execute(query, params)).fetchall()


async def test_feedback_carries_with_its_times_and_a_second_run_adds_nothing(conn, source):
    # Here: a verdict that saves P1, an early label on P1, a later one on P3, and P4 accepted
    # in bulk.
    await verdicts.add(
        conn, Verdict(paper_id=P1.id, topic=FAITH.slug, topic_revision=1, grade=Grade.RELEVANT)
    )
    await word.label(conn, label(P1, LabelValue.ADJACENT))
    await at(conn, "labels", "labelled_at", EARLY, P1.id)
    await word.label(conn, label(P3, LabelValue.NOT_RELEVANT))
    await at(conn, "labels", "labelled_at", LATE, P3.id)
    await feedback_store.add_provisional(
        conn, label(P4, LabelValue.RELEVANT, LabelOrigin.LEGACY_POSITIVE, Grade.RELEVANT)
    )
    await word.settle(conn, P1.id)

    # There: the owner's word on every paper, one of them a paper this database lacks.
    src = source
    await word.label(src, label(P1, LabelValue.NOT_RELEVANT))
    await at(src, "labels", "labelled_at", LATE, P1.id)
    await word.label(src, label(P3, LabelValue.RELEVANT, grade=Grade.MUST_READ))
    await at(src, "labels", "labelled_at", EARLY, P3.id)
    await feedback_store.add_provisional(src, label(P2, LabelValue.NOT_RELEVANT))
    for p in (P2, P5):
        await word.react(
            src,
            Reaction(
                paper_id=p.id,
                topic=FAITH.slug,
                value=ReactionValue.UP,
                reason=ReactionReason.GREAT_FIND,
            ),
        )
    await misses.report(src, P4.id, FAITH.slug)
    await word.toggle(src, P3.id, Hand.SAVE)
    await src.execute(
        "INSERT INTO appraisals (paper_id, value, note) VALUES (%s, 'matters', 'Key.')", (P2.id,)
    )
    take = await digest_store.add_take(
        src,
        Take(
            paper_id=P2.id,
            topic=FAITH.slug,
            paper_revision=1,
            topic_revision=1,
            grade=Grade.RELEVANT,
            source=TakeSource.OWNER,
            text="It finds hidden steps.",
        ),
    )
    await src.execute("INSERT INTO take_ratings (take_id, accurate) VALUES (%s, true)", (take.id,))
    session = (await rows(src, "INSERT INTO calibration_sessions (seed) VALUES (7) RETURNING id"))[
        0
    ]
    for position, (p, answer) in enumerate([(P1, "not_relevant"), (P5, "relevant")], 1):
        await src.execute(
            "INSERT INTO calibration_items (session_id, position, paper_id, topic, why, answer, "
            "answered_at) VALUES (%s, %s, %s, %s, 'above', %s, now())",
            (session["id"], position, p.id, FAITH.slug, answer),
        )

    counts = await carry.import_feedback(conn, src)
    assert {k: v for k, v in counts.items() if not k.startswith("settled")} == {
        "no_paper": 2,  # P5's thumb and its Calibrate item
        "labels": 2,  # P1's newer word and P4's miss over the bulk label; P3's is older
        "reactions": 1,
        "appraisals": 1,
        "take_ratings": 1,
        "calibration_items": 1,
        "library": 1,
        "misses": 1,
    }

    grades = await rows(conn, "SELECT paper_id, grade FROM effective_grades ORDER BY 1")
    assert [(r["paper_id"], r["grade"]) for r in grades] == [
        (P1.id, "none"),  # the source's later label
        (P2.id, "relevant"),  # great find
        (P3.id, "none"),  # this database's later label stands
        (P4.id, "relevant"),  # the miss
    ]
    labels = {x.paper_id: x for x in await feedback_store.labels(conn)}
    assert labels[P1.id].labelled_at == LATE and labels[P1.id].origin == LabelOrigin.READER
    assert labels[P4.id].origin == LabelOrigin.MISS and not labels[P4.id].provisional
    saved = await rows(conn, "SELECT paper_id, hand FROM library WHERE saved ORDER BY 1")
    assert [(r["paper_id"], r["hand"]) for r in saved] == [
        (P2.id, None),
        (P3.id, "save"),
        (P4.id, None),
    ]
    (miss,) = await rows(conn, "SELECT trace, trace_error FROM misses")
    assert miss["trace"] == "failed" and "trace it again" in miss["trace_error"]
    # The rated take is history: bound to nothing here, so P2 is owed a take of its own.
    (copied,) = await rows(conn, "SELECT * FROM takes")
    assert (copied["paper_revision"], copied["topic_revision"]) == (0, 0)
    due = await rows(conn, "SELECT paper_id, for_take, for_issue FROM due ORDER BY 1")
    assert (P2.id, True) in {(d["paper_id"], d["for_take"]) for d in due}
    assert not any(d["for_issue"] for d in due)
    # The carried Calibrate answer counts toward precision here.
    (faith,) = await calibrate.precision(conn)
    assert (faith["precision"], faith["precision_n"]) == (0.0, 1)

    again = await carry.import_feedback(conn, src)
    assert {k: v for k, v in again.items() if k not in ("no_paper", "settled_papers")} == {
        k: 0 for k in again if k not in ("no_paper", "settled_papers")
    }
    assert len(await rows(conn, "SELECT id FROM takes")) == 1


async def test_the_source_must_be_a_v1_database(conn, source):
    await source.execute(
        "DELETE FROM schema_migrations WHERE version >= %s", (carry.SOURCE_AT_LEAST,)
    )
    with pytest.raises(carry.SourceError, match="db migrate"):
        await carry.import_feedback(conn, source)
