"""`db migrate-0x`: 0.14's verdicts carried into an adopted database (spec section 7)."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from litwatch import store
from litwatch.config import Config
from litwatch.core import Label, LabelOrigin, LabelValue
from litwatch.feedback import calibrate, seed
from litwatch.jobs import migrate
from litwatch.llm import Gateway
from litwatch.store import feedback as feedback_store
from litwatch.store import topics as topics_store
from tests.conftest import TOPICS
from tests.fakes import ScriptedProvider

LONG_AGO = datetime(2026, 9, 1, tzinfo=UTC)
# paper, selection, tier, reason, screened
VERDICTS = [
    ("2609.1", "cot-faithfulness", 1, "Detects hidden steps.", LONG_AGO),
    (
        "2609.2",
        "cot-faithfulness",
        2,
        "Measures faithfulness.",
        datetime.now(UTC) - timedelta(days=1),
    ),
    ("2609.3", "cot-faithfulness", 3, "Near the question.", LONG_AGO),
    ("2609.4", "cot-faithfulness", 4, "Not about it.", LONG_AGO),
    ("2609.5", "interpretability", 2, "Probes.", LONG_AGO),
    ("2609.5", "agents", 4, "No.", LONG_AGO),
    ("2609.6", "agents", 3, "Tool use.", LONG_AGO),
    ("2609.7", "old-selection", 1, "Gone.", LONG_AGO),
]


async def tables(conn) -> list[str]:
    rows = await (
        await conn.execute("SELECT tablename FROM pg_tables WHERE schemaname = current_schema()")
    ).fetchall()
    return [r["tablename"] for r in rows]


async def as_0x(conn) -> None:
    """Rebuild the schema as 0.14 left it, with its verdicts, and adopt it."""
    for table in await tables(conn):
        await conn.execute(f'DROP TABLE IF EXISTS "{table}" CASCADE')
    await conn.execute(
        """
        CREATE TABLE papers (id text PRIMARY KEY, title text NOT NULL, abstract text NOT NULL,
            authors jsonb NOT NULL DEFAULT '[]', categories text[] NOT NULL DEFAULT '{}',
            primary_category text, published timestamptz, updated timestamptz, url text,
            fetched_at timestamptz NOT NULL DEFAULT now(), metadata_source text NOT NULL
            DEFAULT 'arxiv', version integer, version_history jsonb NOT NULL DEFAULT '[]',
            doi text, journal_ref text, license text,
            search tsvector GENERATED ALWAYS AS
                (to_tsvector('english', title || ' ' || abstract)) STORED);
        CREATE TABLE runs (id bigserial PRIMARY KEY, kind text NOT NULL,
            started_at timestamptz NOT NULL DEFAULT now(), finished_at timestamptz,
            window_from timestamptz, window_to timestamptz, fetched integer NOT NULL DEFAULT 0,
            inserted integer NOT NULL DEFAULT 0, updated integer NOT NULL DEFAULT 0,
            cost numeric(10, 4) NOT NULL DEFAULT 0, error text, intake jsonb);
        CREATE TABLE selections (slug text PRIMARY KEY, name text NOT NULL, status text NOT NULL,
            subject text NOT NULL, rubric text, keywords text[] NOT NULL DEFAULT '{}');
        CREATE TABLE verdicts (paper_id text NOT NULL REFERENCES papers (id) ON DELETE CASCADE,
            selection text NOT NULL, tier smallint NOT NULL, confidence text NOT NULL,
            reason text NOT NULL, version text NOT NULL, model text,
            screened_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY (paper_id, selection));
        CREATE INDEX verdicts_selection_tier_idx ON verdicts (selection, tier, screened_at DESC);
        """
    )
    for pid in sorted({v[0] for v in VERDICTS}):
        await conn.execute(
            "INSERT INTO papers (id, title, abstract, version) VALUES (%s, %s, 'Abstract.', 2)",
            (pid, f"Paper {pid}"),
        )
    for pid, selection, tier, reason, screened in VERDICTS:
        await conn.execute(
            "INSERT INTO verdicts VALUES (%s, %s, %s, 'high', %s, 'h', 'openai:gpt-5-mini', %s)",
            (pid, selection, tier, reason, screened),
        )
    await store.adopt_0x(conn)


class Writer:
    def __init__(self):
        self.asked: list[str] = []

    def respond(self, role, request):
        self.asked.append(request.user)
        return json.dumps({"take": f"Take {len(self.asked)}."})

    def gateway(self, config, journal):
        providers = {n: ScriptedProvider(n, self.respond) for n in ("anthropic", "openrouter")}
        return Gateway(config.models, providers, journal, retry_delays=())


@pytest.fixture
async def adopted(conn):
    await as_0x(conn)
    for topic in TOPICS:
        await topics_store.save(conn, topic)
    return conn


async def rows(conn, query: str, *params):
    return await (await conn.execute(query, params)).fetchall()


async def test_tiers_become_migrated_verdicts_memberships_and_two_takes(adopted):
    conn, writer = adopted, Writer()
    counts = await migrate.from_0x(conn, Config(), writer.gateway)

    assert counts["verdicts"] == {"must_read": 1, "relevant": 1, "adjacent": 1, "none": 1}
    assert (counts["memberships"], counts["regression"], counts["triaged"]) == (2, 3, 7)
    assert counts["left_behind"] == ["old-selection"]
    assert (counts["takes"], counts["takes_failed"]) == (2, 0)

    (first,) = await rows(conn, "SELECT * FROM verdicts WHERE paper_id = '2609.1'")
    assert first["migrated"] and not first["split"] and first["check_opinion"] is None
    assert first["judge"] == {
        "grade": "must_read",
        "reason": "Detects hidden steps.",
        "model": "openai:gpt-5-mini",
    }
    assert first["recorded_at"] == LONG_AGO and first["paper_version"] == 2
    grades = await rows(conn, "SELECT paper_id, grade FROM effective_grades ORDER BY 1")
    assert [(r["paper_id"], r["grade"]) for r in grades] == [
        ("2609.1", "must_read"),
        ("2609.2", "relevant"),
        ("2609.3", "adjacent"),
        ("2609.4", "none"),
    ]
    saved = await rows(conn, "SELECT paper_id FROM library WHERE saved ORDER BY 1")
    assert [r["paper_id"] for r in saved] == ["2609.1", "2609.2"]

    # One take per positive, written from the single opinion; none of them due for an issue.
    takes = await rows(conn, "SELECT paper_id, grade, source FROM takes ORDER BY paper_id")
    assert [(t["paper_id"], t["grade"], t["source"]) for t in takes] == [
        ("2609.1", "must_read", "migrated"),
        ("2609.2", "relevant", "migrated"),
    ]
    assert any("Detects hidden steps." in user for user in writer.asked)
    assert await rows(conn, "SELECT * FROM due") == []

    members = await rows(conn, "SELECT paper_id, topic, created_at FROM memberships ORDER BY 1")
    assert [(m["paper_id"], m["topic"], m["created_at"]) for m in members] == [
        ("2609.5", "interpretability", LONG_AGO),
        ("2609.6", "agents", LONG_AGO),
    ]
    # A migrated verdict is 0.14's grade, not v1's: Calibrate never draws it, recent or not.
    session = await calibrate.session(conn, await calibrate.draw(conn, seed=1))
    assert session["items"] == []

    again = await migrate.from_0x(conn, Config(), writer.gateway)
    assert again["verdicts"] == {} and again["memberships"] == again["regression"] == 0
    assert (again["triaged"], again["takes"]) == (0, 0)
    assert again["settled"]["grades_changed"] == 0
    assert len(await rows(conn, "SELECT id FROM verdicts")) == 4


async def test_it_will_not_leave_provisional_labels_beside_the_migrated_verdicts(adopted):
    conn = adopted
    await feedback_store.add_provisional(
        conn,
        Label(
            paper_id="2609.1",
            topic="cot-faithfulness",
            value=LabelValue.RELEVANT,
            origin=LabelOrigin.LEGACY_POSITIVE,
        ),
    )
    with pytest.raises(migrate.MigrationError, match="provisional labels"):
        await migrate.from_0x(conn, Config(), Writer().gateway)
    await conn.execute("DELETE FROM labels")

    await migrate.from_0x(conn, Config(), Writer().gateway)
    with pytest.raises(RuntimeError, match="migrate-0x"):
        await seed.from_legacy_verdicts(conn)


async def test_it_needs_an_adopted_database_with_watch_topics(conn):
    with pytest.raises(migrate.MigrationError, match="db adopt"):
        await migrate.from_0x(conn, Config(), Writer().gateway)
    await as_0x(conn)
    with pytest.raises(migrate.MigrationError, match="topics import"):
        await migrate.from_0x(conn, Config(), Writer().gateway)
