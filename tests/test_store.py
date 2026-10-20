"""Migrations: a fresh database, and the refusal of a 0.x one."""

import psycopg
import pytest
from psycopg.types.json import Jsonb

from litwatch import store
from litwatch.store import db
from litwatch.store import papers as papers_store
from tests.conftest import paper


async def tables(conn) -> set[str]:
    rows = await (
        await conn.execute("SELECT tablename FROM pg_tables WHERE schemaname = current_schema()")
    ).fetchall()
    return {r["tablename"] for r in rows}


async def test_migrations_apply_once(conn):
    assert await store.migrate(conn) == []
    assert {"papers", "runs", "inference_calls", "outbox"} <= await tables(conn)


async def test_a_0x_database_is_refused(conn):
    # The schema as 0.x left it: no migration record, its own selections.
    for table in await tables(conn):
        await conn.execute(f'DROP TABLE IF EXISTS "{table}" CASCADE')
    await conn.execute("CREATE TABLE selections (slug text PRIMARY KEY, name text NOT NULL)")
    with pytest.raises(store.LegacyDatabaseError):
        await store.migrate(conn)
    assert await tables(conn) == {"selections"}


# -- 0036: traces per attempt ----------------------------------------------------------------------

TRACES = 36


def turns(n: int) -> list[dict]:
    """A Deep job's trace of `n` turns, with a cell between them."""
    turn = {"t": 0, "type": "assistant", "text": "", "thinking": "", "stop": "toolUse"}
    cell = {"t": 1, "type": "cell", "n": 1, "code": "1", "output": "1", "ms": 3}
    return [turn, cell] * (n - 1) + [turn]


@pytest.fixture
async def before_traces(conn, monkeypatch):
    """`conn`'s schema rebuilt as it was before 0036, with a paper; `migrate` then applies it."""
    for table in await tables(conn):
        await conn.execute(f'DROP TABLE IF EXISTS "{table}" CASCADE')
    every = db._migrations()
    monkeypatch.setattr(db, "_migrations", lambda: [m for m in every if m[0] < TRACES])
    await store.migrate(conn)
    monkeypatch.setattr(db, "_migrations", lambda: every)
    await papers_store.upsert(conn, [paper("2610.00001"), paper("2610.00002")])
    return conn


async def attempt(conn, paper_id: str, job: str | None, usage: dict | None, ended=True) -> int:
    row = await (
        await conn.execute(
            "INSERT INTO deep_attempts (paper_id, automatic, job_id, usage, ended_at) "
            "VALUES (%s, false, %s, %s, CASE WHEN %s THEN now() END) RETURNING id",
            (paper_id, job, Jsonb(usage) if usage is not None else None, ended),
        )
    ).fetchone()
    return row["id"]


async def card_with(conn, paper_id: str, trace: list) -> None:
    await conn.execute(
        "INSERT INTO deep_cards (paper_id, card, trace, written_at) VALUES (%s, '{}', %s, now())",
        (paper_id, Jsonb(trace)),
    )


async def test_a_cards_trace_moves_to_the_attempt_whose_job_ran(before_traces):
    conn = before_traces
    ran = await attempt(conn, "2610.00001", "job-1", {"turns": 2})
    # The remake that followed was turned down before its job ran: the card kept the old trace.
    turned_down = await attempt(conn, "2610.00001", None, {"turns": 0})
    running = await attempt(conn, "2610.00001", "job-2", None, ended=False)
    await card_with(conn, "2610.00001", turns(2))
    # A lost job's failure wiped its card's trace: nothing to move.
    lost = await attempt(conn, "2610.00002", "job-3", None)
    await card_with(conn, "2610.00002", [])

    assert await store.migrate(conn) == ["0036_deep_traces.sql"]
    rows = await (await conn.execute("SELECT id, trace FROM deep_attempts")).fetchall()
    kept = {r["id"]: r["trace"] for r in rows}
    assert kept == {ran: turns(2), turned_down: None, running: None, lost: None}
    columns = await (await conn.execute("SELECT * FROM deep_cards")).fetchone()
    assert "trace" not in columns


@pytest.mark.parametrize(
    "attempts",
    [
        [("job-1", {"turns": 3})],  # its turns don't match
        [("job-1", None)],  # its usage is unknown
        [("job-1", {"turns": 2}), ("job-2", {"turns": 4})],  # the latest is another job's
        [(None, {"turns": 2})],  # no attempt whose job ran
    ],
)
async def test_a_trace_without_its_attempt_stops_the_whole_migration(before_traces, attempts):
    conn = before_traces
    for job, usage in attempts:
        await attempt(conn, "2610.00001", job, usage)
    await card_with(conn, "2610.00001", turns(2))

    with pytest.raises(psycopg.errors.RaiseException, match="deep card 2610.00001"):
        await store.migrate(conn)
    (card,) = await (await conn.execute("SELECT trace FROM deep_cards")).fetchall()
    assert card["trace"] == turns(2)
    column = "SELECT 1 FROM information_schema.columns WHERE table_schema = current_schema() "
    column += "AND table_name = 'deep_attempts' AND column_name = 'trace'"
    assert await (await conn.execute(column)).fetchone() is None
    done = await (await conn.execute("SELECT max(version) AS v FROM schema_migrations")).fetchone()
    assert done["v"] == TRACES - 1
