"""Digests outside the nightly run: those of a window of days (the restart, a missed night),
and a failed digest written again at the owner's word. Each is a run of its own, under the run
lock, so no other job writes a digest meanwhile."""

from __future__ import annotations

from datetime import date, timedelta

import psycopg

from litwatch import digests
from litwatch.config import Config
from litwatch.core import Status
from litwatch.core.digests import DigestPeriod, DigestState
from litwatch.jobs.context import GatewayFactory, default_gateway, model_run
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
    written on those days, over the papers due now. Published ones stay. Returns the run id."""
    async with run_lock(conn), model_run(conn, config, "digests", gateway=gateway) as run:
        tree = await taxonomy_store.load(conn)
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
    conn: psycopg.AsyncConnection,
    config: Config,
    digest_id: int,
    gateway: GatewayFactory = default_gateway,
) -> DigestState | None:
    """Write a failed digest of an active topic again now, over the papers due for it now.
    None when it is not one (it was written meanwhile, its topic is paused or unfollowed); the
    check is made under the run lock, so no nightly run writes it in between. Raises
    `BusyError` while another job holds the lock."""
    async with run_lock(conn):
        tree = await taxonomy_store.load(conn)
        row = await store_.get(conn, digest_id)
        topic = tree.topic(row["topic"]) if tree and row else None
        if row is None or row["state"] != DigestState.FAILED or topic is None:
            return None
        if topic.status != Status.ACTIVE:
            return None
        async with model_run(conn, config, "digests", gateway=gateway) as run:
            return await digests.write(
                conn, run, tree, topic, DigestPeriod(row["period"]), row["day"]
            )
