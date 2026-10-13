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
from litwatch.store import legacy
from litwatch.store import topics as topics_store
from litwatch.store import triage as triage_store


class MigrationError(RuntimeError):
    """The database is not one this migration can run on."""


async def has_provisional(conn: psycopg.AsyncConnection) -> bool:
    row = await (
        await conn.execute("SELECT EXISTS (SELECT 1 FROM labels WHERE provisional) AS found")
    ).fetchone()
    return row["found"]


async def from_0x(
    conn: psycopg.AsyncConnection, config: Config, gateway: GatewayFactory = default_gateway
) -> dict[str, Any]:
    """Migrate an adopted 0.x database whose topics are imported. Running it again adds
    nothing and retries the takes a failed call left owed.

    Watch tiers become migrated verdicts, Radar and Pulse tiers 1 to 3 memberships; every
    stored paper counts as triaged; every paper is settled without being announced; then the
    writer writes a take, source `migrated`, for each pair the verdicts put at relevant or
    above. Returns what it did.
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
        async with conn.transaction():
            counts: dict[str, Any] = {
                "verdicts": await legacy.migrate_verdicts(conn),
                "memberships": await legacy.migrate_memberships(conn),
                "regression": await legacy.migrate_regression(conn),
                "left_behind": await legacy.unmatched(conn),
            }
        counts["triaged"] = await triage_store.mark_migrated(conn)
        counts["settled"] = await word.sync(conn)
        async with model_run(conn, config, "migrate-0x", gateway=gateway) as run:
            await digest_job.write_takes(conn, config, run)
        counts["takes"] = run.counts.get("takes", 0)
        counts["takes_failed"] = run.counts.get("takes_failed", 0)
    return counts
