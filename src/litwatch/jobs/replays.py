"""Replays as jobs (docs/plans/jobs.md, P3; DESIGN.md, Overview's Run replay): a replay of every
cohort's labels (`quality.replay`, as `litwatch eval replay` runs it by default), worked a chunk
of papers at a time under a ceiling. A dry run: it writes the evaluation tables, its run and its
calls only, never verdicts or the outbox (AGENTS.md).

A replay's labels are frozen when it is made, in an evaluation run its job names; each run of
the job goes on with it as `--resume` does, once over what is left: what has not concluded, or
failed in an earlier run. One its ceiling stopped waits on the owner, and Run now continues it."""

from __future__ import annotations

import contextlib
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Literal

from litwatch.config import Config
from litwatch.gates.database import DatabaseGate, ReadUnit, WriteUnit
from litwatch.jobs import reruns
from litwatch.jobs.context import ModelRun
from litwatch.quality import replay
from litwatch.store import evaluation
from litwatch.store import jobs as jobs_store
from litwatch.store import taxonomy as taxonomy_store

CEILING = 5.0  # the offer's ceiling, until the owner changes it


@dataclass
class Offer:
    """What Run replay would do: answer with `running`, a replay already queued or running; else
    go on with `job`, a replay its ceiling stopped that resumes under the configuration now; else
    start a new replay, setting aside `stale`, one not run that no longer resumes (the
    configuration or taxonomy changed). `replay` is what that leaves to do (None while one
    runs), `estimate` what it would cost, `unpriced` the models with no price bound, whose calls
    a job does not send, and `spent` what the stopped one has spent."""

    replay: replay.Replay | None
    estimate: float = 0.0
    spent: float = 0.0
    unpriced: list[str] = field(default_factory=list)
    running: dict[str, Any] | None = None
    job: dict[str, Any] | None = None
    stale: dict[str, Any] | None = None


async def new(unit: ReadUnit) -> replay.Replay:
    """A new replay of every cohort's labels on the active Focus topics, on the papers the
    database holds."""
    tree = await taxonomy_store.load(unit)
    if tree is None:
        raise ValueError("no taxonomy yet")
    every = replay.load(replay.LABELS) + await replay.misses(unit)
    return await replay.prepare(unit, replay.select(every, replay.focus(tree)))


async def offer(unit: WriteUnit, config: Config) -> Offer:
    """What Run replay would do now, read in a unit holding the replay lock (`open_replay`), so
    `make` in the same unit acts on what it read."""
    found = await jobs_store.open_replay(unit)
    if found is not None and found["state"] in jobs_store.ACTIVE:
        return Offer(None, running=found)
    r = None
    if found is not None:
        with contextlib.suppress(ValueError):
            r = await replay.resume(unit, config, found["scope"]["eval_run"])
    o = Offer(r, job=found) if r is not None else Offer(await new(unit), stale=found)
    assert o.replay is not None
    o.estimate = sum((await replay.estimate(unit, config, o.replay)).values())
    o.unpriced = await reruns.unpriced(unit, config, replay.ROLES)
    if o.job is not None:
        o.spent = await jobs_store.spent(unit, o.job["id"])
    return o


async def make(unit: WriteUnit, config: Config, o: Offer, ceiling: float) -> dict[str, Any]:
    """Queue the offer's replay under `ceiling`: the stopped one goes on; else the stale one is
    discarded and a new one started, its labels frozen with its job. In `offer`'s unit."""
    assert o.running is None and o.replay is not None
    if o.job is not None:
        moved = await jobs_store.move(unit, o.job["id"], jobs_store.NOT_RUN, "queued", ceiling)
        assert moved is not None  # its row is locked
        return moved
    if o.stale is not None:
        await jobs_store.move(unit, o.stale["id"], jobs_store.NOT_RUN, "discarded")
    params = {"cohorts": None, "negatives": {}, "seed": replay.SEED, "limit": None}
    eval_run = await replay.start(unit, config, o.replay, params, "run from Overview")
    job_id = await jobs_store.create(
        unit,
        "replay",
        {"eval_run": eval_run},
        len(o.replay.papers),
        "queued",
        ceiling=ceiling,
        estimate=o.estimate,
    )
    made = await jobs_store.get(unit, job_id)
    assert made is not None
    return made


@dataclass
class Chunk:
    """How one chunk of a replay ended: `more` to do, `stopped` by its ceiling (`where`),
    `done`, or `gone` (no longer running)."""

    outcome: Literal["more", "stopped", "done", "gone"]
    where: str | None = None


async def work(
    db: DatabaseGate,
    config: Config,
    run: ModelRun,
    job: dict[str, Any],
    tried: set[str],
    size: int,
) -> Chunk:
    """The replay's next chunk: the next `size` papers with a stage left that this run has not
    `tried` (the worker keeps it with the run; one whose text changed since counts as not
    tried), through classification and grading, as
    `--resume` goes on (a configuration or taxonomy changed since raises `ValueError`). Its
    progress is the papers concluded; done once this run has tried every paper left, the
    evaluation run then finished."""
    eval_run = job["scope"]["eval_run"]
    async with db.read("replay: resume") as unit:
        r = await replay.resume(unit, config, eval_run)
    tried.difference_update(r.stale)  # a paper whose text changed since is to do again
    ids = [p for p in r.left if p not in tried][:size]
    before = run.chunk_began()
    await replay.work(db, config, r, run, ids)
    cut = run.gateway.ceiling is not None and run.gateway.ceiling.reached
    if not cut:
        tried.update(ids)
    counts = Counter(job["counts"]) + (run.tally() - before)
    concluded = len(r.papers) - len(r.left)
    async with db.write("replay: progress") as unit:
        after = await jobs_store.progress(unit, job["id"], concluded, dict(counts))
    run.chunk_recorded()
    if after is None:
        return Chunk("gone")
    if cut:
        return Chunk("stopped", f"stopped at the ceiling after {concluded} of {len(r.papers)}")
    if any(p not in tried for p in r.left):
        return Chunk("more")
    async with db.write("replay: finish") as unit:
        await evaluation.finish(unit, eval_run)
    return Chunk("done")
