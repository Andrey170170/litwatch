"""Digests outside the nightly run: those of a window of days (the restart, a missed night), a
run of its own, and a failed digest written again at the owner's word, a job's (`jobs.worker`).
Each is written under the run lock, so no other job writes a digest meanwhile."""

from __future__ import annotations

from datetime import date, timedelta

import psycopg

from litwatch import digests
from litwatch.config import Config
from litwatch.core import Status
from litwatch.core.digests import DigestPeriod, DigestState
from litwatch.jobs.context import GatewayFactory, ModelRun, default_gateway, model_run
from litwatch.jobs.lock import run_lock
from litwatch.store import digests as store_
from litwatch.store import taxonomy as taxonomy_store


async def window(
    conn: psycopg.AsyncConnection,
    config: Config,
    first: date,
    last: date,
    gateway: GatewayFactory = default_gateway,
) -> int:
    """The daily digests of every day from `first` to `last` in order, and the weekly digests
    of every Monday-to-Sunday week inside it, each after the next Monday's dailies (the last
    week's after its Sunday's, when the window ends there): what the nightly run would have
    written on those days, over the papers due now, after the failed digests are written again
    (as the nightly run does) so a day of the window does not take their papers. Published ones
    stay. Returns the run id."""
    async with run_lock(conn), model_run(conn, config, "digests", gateway=gateway) as run:
        tree = await taxonomy_store.load(conn)
        if tree is not None:
            await digests.retry_failed(conn, run, tree)
        day = first
        while tree is not None and day <= last:
            await digests.write_day(conn, run, tree, day)
            if day.weekday() == 0 and day - timedelta(days=7) >= first:
                await digests.write_week(conn, run, tree, day - timedelta(days=7))
            day += timedelta(days=1)
        if tree is not None and last.weekday() == 6 and last - timedelta(days=6) >= first:
            await digests.write_week(conn, run, tree, last - timedelta(days=6))
        return run.run_id


async def again(
    conn: psycopg.AsyncConnection, run: ModelRun, digest_id: int
) -> tuple[str, str | None]:
    """Write a failed digest of an active topic again now, over the papers due for it now, on
    the job's run, under the run lock the jobs worker holds: how its job ends, `done` once the
    digest is published (written now, or meanwhile by another run), else `failed` with why (the
    writer failed again, its topic is no longer active)."""
    tree = await taxonomy_store.load(conn)
    row = await store_.get(conn, digest_id)
    if row is None:
        return "failed", "the digest is gone"
    if row["state"] != DigestState.FAILED:
        return "done", None
    topic = tree.topic(row["topic"]) if tree else None
    if tree is None or topic is None or topic.status != Status.ACTIVE:
        status = topic.status.value if topic else "unfollowed"
        return "failed", f"its topic is {status}"
    state = await digests.write(conn, run, tree, topic, DigestPeriod(row["period"]), row["day"])
    if state == DigestState.FAILED:
        error = (await store_.get(conn, digest_id) or {}).get("error")
        return "failed", f"the writer failed again: {error or 'no text'}"
    return "done", None
