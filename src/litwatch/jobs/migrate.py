"""Moving 0.14's verdicts into v1 (spec section 7), once, on the adopted copy that becomes
v1's database.

The production path for 0.x's positives is this one, not `eval seed --legacy`: their tiers
become migrated verdicts, which a later re-grade replaces like any verdict. Seeding them as
provisional labels as well would make 0.14's grade stand as the owner's word over every
later verdict, so this refuses a database that has them, and the seeding refuses a migrated
one. The regression set comes along here, which leaves the seeding nothing to add.
"""

from __future__ import annotations

from typing import Any

import psycopg

from litwatch.config import Config
from litwatch.core import TopicKind
from litwatch.feedback import word
from litwatch.jobs import digest as digest_job
from litwatch.jobs.context import GatewayFactory, default_gateway, model_run
from litwatch.jobs.lock import run_lock
from litwatch.store import legacy, runs
from litwatch.store import topics as topics_store
from litwatch.store import triage as triage_store


class MigrationError(RuntimeError):
    """The database is not one this migration can run on."""


async def has_provisional(conn: psycopg.AsyncConnection) -> bool:
    row = await (
        await conn.execute("SELECT EXISTS (SELECT 1 FROM labels WHERE provisional) AS found")
    ).fetchone()
    return row["found"]


# The run kind that marks the data phase done. It is written in that phase's transaction, so
# the phase happens exactly once: a later run must not count papers ingested since as triaged.
DATA_RUN = "migrate-0x"


async def _data_run(conn: psycopg.AsyncConnection) -> dict[str, Any] | None:
    """The run that recorded the data phase, once it committed."""
    return await (
        await conn.execute(
            "SELECT id, counts FROM runs WHERE kind = %s AND finished_at IS NOT NULL "
            "ORDER BY id LIMIT 1",
            (DATA_RUN,),
        )
    ).fetchone()


async def from_0x(
    conn: psycopg.AsyncConnection, config: Config, gateway: GatewayFactory = default_gateway
) -> dict[str, Any]:
    """Migrate an adopted 0.x database whose topics are imported.

    The data phase, in one transaction with its marker: Watch tiers become migrated verdicts,
    Radar and Pulse tiers 1 to 3 memberships, and every paper stored at that moment counts as
    triaged. Then every paper is settled without being announced, and the writer writes a
    take, source `migrated`, for each pair the verdicts put at relevant or above. Running it
    again resumes after the data phase: it settles again (a no-op for a settled paper) and
    writes the takes still owed. Returns what it did.
    """
    if not await legacy.adopted(conn):
        raise MigrationError("no 0.x tables here; run `litwatch db adopt` on a copy first")
    topics = await topics_store.all(conn, None)
    if not any(t.kind == TopicKind.WATCH for t in topics):
        raise MigrationError(
            "no Watch topic yet; import the topics first (`litwatch topics import`)"
        )
    if await has_provisional(conn):
        raise MigrationError(
            "this database has provisional labels from `eval seed --legacy`; their grades would "
            "stand as the owner's word over the migrated verdicts"
        )
    async with run_lock(conn):
        done = await _data_run(conn)
        if done:
            counts: dict[str, Any] = {"resumed_after_run": done["id"]}
        else:
            async with conn.transaction():
                counts = {
                    "verdicts": await legacy.migrate_verdicts(conn),
                    "memberships": await legacy.migrate_memberships(conn),
                    "regression": await legacy.migrate_regression(conn),
                    "left_behind": await legacy.unmatched(conn),
                    "triaged": await triage_store.mark_migrated(conn),
                }
                await runs.finish(conn, await runs.start(conn, DATA_RUN), counts=counts)
        counts["settled"] = await word.sync(conn)
        async with model_run(conn, config, "writer", gateway=gateway) as run:
            await digest_job.write_takes(conn, config, run)
        counts["takes"] = run.counts.get("takes", 0)
        counts["takes_failed"] = run.counts.get("takes_failed", 0)
    return counts
