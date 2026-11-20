"""Digests outside the nightly run: those of a window of days (the restart, a missed night), a
run of its own, and a failed digest written again at the owner's word, a job's (`jobs.worker`).
Each is written under the run lock, so no other job writes a digest meanwhile."""

from __future__ import annotations

from datetime import date, timedelta

from litwatch import digests
from litwatch.config import Config
from litwatch.core import Status
from litwatch.core.digests import DigestPeriod, DigestState
from litwatch.feedback import word
from litwatch.gates.database import DatabaseGate
from litwatch.jobs.context import GatewayFactory, ModelRun, default_gateway, model_run
from litwatch.store import digests as store_
from litwatch.store import taxonomy as taxonomy_store


async def window(
    db: DatabaseGate,
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
    rules = word.rules(config)
    async with (
        db.held("run"),
        model_run(db, config, "digests", gateway=gateway) as run,
    ):
        async with db.read("write digests: taxonomy") as unit:
            tree = await taxonomy_store.load(unit)
        if tree is not None:
            await digests.retry_failed(db, run, tree, rules=rules)
        day = first
        while tree is not None and day <= last:
            await digests.write_day(db, run, tree, day, rules=rules)
            if day.weekday() == 0 and day - timedelta(days=7) >= first:
                await digests.write_week(db, run, tree, day - timedelta(days=7), rules=rules)
            day += timedelta(days=1)
        if tree is not None and last.weekday() == 6 and last - timedelta(days=6) >= first:
            await digests.write_week(db, run, tree, last - timedelta(days=6), rules=rules)
        return run.run_id


async def again(
    db: DatabaseGate, run: ModelRun, digest_id: int, *, rules: word.Rules
) -> tuple[str, str | None]:
    """Write a failed digest of an active topic again now, over the papers due for it now, on
    the job's run, under the run lock the jobs worker holds: how its job ends, `done` once the
    digest is published (written now, or meanwhile by another run), else `failed` with why (the
    writer failed again, its topic is no longer active)."""
    async with db.read("digest again") as unit:
        tree = await taxonomy_store.load(unit)
        row = await store_.get(unit, digest_id)
    if row is None:
        return "failed", "the digest is gone"
    if row["state"] != DigestState.FAILED:
        return "done", None
    topic = tree.topic(row["topic"]) if tree else None
    if tree is None or topic is None or topic.status != Status.ACTIVE:
        status = topic.status.value if topic else "unfollowed"
        return "failed", f"its topic is {status}"
    state = await digests.write(
        db, run, tree, topic, DigestPeriod(row["period"]), row["day"], rules=rules
    )
    if state == DigestState.FAILED:
        async with db.read("digest again: error") as unit:
            error = (await store_.get(unit, digest_id) or {}).get("error")
        return "failed", f"the writer failed again: {error or 'no text'}"
    return "done", None
