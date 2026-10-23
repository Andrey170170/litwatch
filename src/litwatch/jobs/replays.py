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

import psycopg

from litwatch.config import Config
from litwatch.jobs import reruns
from litwatch.jobs.context import ModelRun
from litwatch.llm.ceiling import Ceiling
from litwatch.quality import replay
from litwatch.store import evaluation
from litwatch.store import jobs as jobs_store
from litwatch.store import taxonomy as taxonomy_store

ROLES = replay.ROLES
CEILING = 5.0  # the offer's ceiling, until the owner changes it


@dataclass
class Offer:
    """What Run replay would do: answer with `running`, a replay already queued or running; else
    go on with `job`, a replay its ceiling stopped that resumes under the configuration now; else
    start a new replay, setting aside `stale`, one not run that no longer resumes (the
    configuration or taxonomy changed). `replay` is what that leaves to do (None while one
    runs), `estimate` what it would cost, `unpriced` the models with no price bound, whose calls
    a job does not send."""

    replay: replay.Replay | None
    estimate: float = 0.0
    unpriced: list[str] = field(default_factory=list)
    running: dict[str, Any] | None = None
    job: dict[str, Any] | None = None
    stale: dict[str, Any] | None = None


async def new(conn: psycopg.AsyncConnection) -> replay.Replay:
    """A new replay of every cohort's labels on the active Focus topics, on the papers the
    database holds."""
    tree = await taxonomy_store.load(conn)
    if tree is None:
        raise ValueError("no taxonomy yet")
    every = replay.load(replay.LABELS) + await replay.misses(conn)
    return await replay.prepare(conn, replay.select(every, replay.focus(tree)))


async def offer(conn: psycopg.AsyncConnection, config: Config) -> Offer:
    """What Run replay would do now, read under the replay lock (`open_replay`), so `make` in
    the same transaction acts on what it read."""
    found = await jobs_store.open_replay(conn)
    if found is not None and found["state"] in jobs_store.ACTIVE:
        return Offer(None, running=found)
    r = None
    if found is not None:
        with contextlib.suppress(ValueError):
            r = await replay.resume(conn, config, found["scope"]["eval_run"])
    o = Offer(r, job=found) if r is not None else Offer(await new(conn), stale=found)
    assert o.replay is not None
    o.estimate = sum((await replay.estimate(conn, config, o.replay)).values())
    o.unpriced = await reruns.unpriced(conn, config, ROLES)
    return o


async def make(
    conn: psycopg.AsyncConnection, config: Config, o: Offer, ceiling: float
) -> dict[str, Any]:
    """Queue the offer's replay under `ceiling`: the stopped one goes on; else the stale one is
    discarded and a new one started, its labels frozen with its job. In `offer`'s transaction."""
    assert o.running is None and o.replay is not None
    if o.job is not None:
        moved = await jobs_store.move(conn, o.job["id"], jobs_store.NOT_RUN, "queued", ceiling)
        assert moved is not None  # its row is locked
        return moved
    if o.stale is not None:
        await jobs_store.move(conn, o.stale["id"], jobs_store.NOT_RUN, "discarded")
    params = {"cohorts": None, "negatives": {}, "seed": replay.SEED, "limit": None}
    eval_run = await replay.start(conn, config, o.replay, params, "run from Overview")
    job_id = await jobs_store.create(
        conn,
        "replay",
        {"eval_run": eval_run},
        len(o.replay.papers),
        "queued",
        ceiling=ceiling,
        estimate=o.estimate,
    )
    made = await jobs_store.get(conn, job_id)
    assert made is not None
    return made


async def ceiling(conn: psycopg.AsyncConnection, config: Config, job: dict[str, Any]) -> Ceiling:
    return await reruns.ceiling(conn, config, job, ROLES)


@dataclass
class Chunk:
    """How one chunk of a replay ended: `more` to do, `stopped` by its ceiling (`where`),
    `done`, or `gone` (no longer this session's)."""

    outcome: Literal["more", "stopped", "done", "gone"]
    where: str | None = None


async def work(
    conn: psycopg.AsyncConnection,
    config: Config,
    run: ModelRun,
    job: dict[str, Any],
    tried: set[str],
    size: int,
) -> Chunk:
    """The replay's next chunk: the next `size` papers with a stage left that this run has not
    `tried` (the worker keeps it with the run), through classification and grading, as
    `--resume` goes on (a configuration or taxonomy changed since raises `ValueError`). Its
    progress is the papers concluded; done once this run has tried every paper left, the
    evaluation run then finished."""
    eval_run = job["scope"]["eval_run"]
    r = await replay.resume(conn, config, eval_run)
    ids = [p for p in r.left if p not in tried][:size]
    before = Counter({k: v for k, v in run.counts.items() if isinstance(v, int)})
    await replay.work(conn, config, r, run, ids)
    cut = run.gateway.ceiling is not None and run.gateway.ceiling.reached
    if not cut:
        tried.update(ids)
    counts = Counter(job["counts"])
    for k, v in run.counts.items():
        if isinstance(v, int):
            counts[k] += v - before[k]
    concluded = len(r.papers) - len(r.left)
    if await jobs_store.progress(conn, job["id"], concluded, dict(counts)) is None:
        return Chunk("gone")
    if cut:
        return Chunk("stopped", f"stopped at the ceiling after {concluded} of {len(r.papers)}")
    if any(p not in tried for p in r.left):
        return Chunk("more")
    await evaluation.finish(conn, eval_run)
    return Chunk("done")
