"""The owner's queue (docs/plans/jobs.md): jobs, their chunks' work items, their claim, and what
the Jobs page lists, Deep cards asked for read alongside from their passes.

A job's chunk is a work item of kind `WORK` keyed by the job's id (`jobs.worker`). The job's row
decides when it runs: every write to it arranges its next chunk in the same unit, after the row
is locked (`arrange`, `submit`): submitted if the job may run (queued, or running between
chunks, or on tonight's run once a nightly finished after it was put there), and, written by
the job's own chunk, none but that one. So a chunk's item ends with the successor its job's
last write decided, whatever ends it. The lock order on every path that takes both is the job's
row, then its item's; a write that settles papers takes their locks before either. Putting a job
on tonight's run and a nightly's release (`release`) both hold the `tonight` lock, so one of the
two sees the other."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, overload

from psycopg.types.json import Jsonb

from litwatch.core.fulltext import REQUESTED
from litwatch.core.work import WorkItem
from litwatch.gates.context import current_work
from litwatch.gates.database import ReadUnit, WriteUnit
from litwatch.store import work
from litwatch.store.failures import ACTIONABLE
from litwatch.store.fulltext import STARTED, STUCK, stuck_params
from litwatch.store.reading import SHOWN_MEMBER
from litwatch.store.runs import ALIVE, SESSION
from litwatch.store.text import stored

# The states each of the Jobs page's groups shows (DESIGN.md, Jobs); `discarded` is hidden.
NOT_RUN = ("offered", "stopped")
ACTIVE = ("tonight", "queued", "running")

# The kinds the owner waits on, which go first: each is one short chunk, keeping no run open.
FIRST = ("process", "digest")

# The work items of jobs' chunks (`jobs.worker`), keyed by the job's id, one running at a time.
WORK = "job"

# Whether job `j` may run a chunk now, but for its `wake_at`: queued, running (between chunks, or
# its chunk's item taken back), or on tonight's run once a nightly run finished after it was put
# there.
_RUNNABLE = (
    "(j.state IN ('queued', 'running') OR (j.state = 'tonight' AND EXISTS ("
    "SELECT 1 FROM runs n WHERE n.kind = 'nightly' AND n.finished_at >= j.queued_at)))"
)


async def create(
    unit: WriteUnit,
    kind: str,
    scope: dict[str, Any],
    papers: int,
    state: str,
    *,
    ceiling: float | None = None,
    estimate: float | None = None,
) -> int:
    """A new job in `state`, its chunk submitted if it may run. One put on tonight's run is made
    under the `tonight` lock, as `move` there is."""
    if state == "tonight":
        _holds(unit, "tonight")
    row = await (
        await unit.execute(
            "INSERT INTO jobs (kind, scope, papers, state, ceiling, estimate, queued_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, clock_timestamp()) RETURNING id",
            (kind, Jsonb(scope), papers, state, ceiling, estimate),
        )
    ).fetchone()
    assert row is not None  # the insert returns it
    await submit(unit, row["id"])
    return row["id"]


# How a job's chunk is scheduled (`submit`).
How = Literal["submit", "ensure", "wake"]


async def submit(unit: WriteUnit, job_id: int, *, how: How = "submit") -> None:
    """Schedule the job's next chunk as its work item if it may run (`_RUNNABLE`), its row locked
    first, then its item's: due when it was queued, so the oldest goes first, or at its
    `wake_at` when later; those the owner waits on (`FIRST`) before the others. `ensure` leaves
    an open item as it stands (`store.work.ensure`), as the startup sweep has it; `wake` only
    brings a pending one forward to when it is due, as the nightly's end does. A write that
    makes a job runnable from outside its chunk calls it in its unit; the chunk's own writes
    `arrange`."""
    await _schedule(unit, await _locked(unit, job_id), how)


async def _locked(unit: WriteUnit, job_id: int) -> dict[str, Any] | None:
    """The job's row locked, with what scheduling its chunk reads of it."""
    return await (
        await unit.execute(
            f"SELECT j.id, j.kind, {_RUNNABLE} AS runnable, "
            "GREATEST(j.queued_at, j.wake_at) AS due FROM jobs j WHERE j.id = %s FOR UPDATE",
            (job_id,),
        )
    ).fetchone()


async def _schedule(unit: WriteUnit, row: dict[str, Any] | None, how: How) -> None:
    if row is None or not row["runnable"]:
        return
    key = str(row["id"])
    if how == "wake":
        await work.bring_forward(unit, WORK, key, row["due"])
        return
    await (work.ensure if how == "ensure" else work.submit)(
        unit, WORK, key, not_before=row["due"], priority=0 if row["kind"] in FIRST else 1
    )


async def arrange(unit: WriteUnit, job_id: int) -> None:
    """Arrange the job's next chunk as the last step of a write to its row that can change
    whether, or when, it runs: decided under the row's lock from the row alone. In a unit of
    the job's own chunk, what was submitted while it ran is forgotten first, since this read
    sees it, so the chunk's item ends with this decision (`store.work.end`): the next chunk, or
    none. Elsewhere, the chunk is submitted if the job may run."""
    row = await _locked(unit, job_id)
    running = current_work()
    if running is not None and running.item.kind == WORK and running.item.key == str(job_id):
        await work.forget_resubmission(unit, running.item)
    await _schedule(unit, row, "submit")


async def open_ids(unit: ReadUnit) -> list[int]:
    """The jobs that may run a chunk, in id order: those the startup sweep makes sure have
    their item."""
    rows = await (
        await unit.execute(f"SELECT j.id FROM jobs j WHERE {_RUNNABLE} ORDER BY j.id")
    ).fetchall()
    return [r["id"] for r in rows]


async def _each(unit: WriteUnit, how: How) -> None:
    """Schedule the chunk of every job that may run (`submit`), in id order: each job's row,
    then its item's, so two such units, or one and a chunk's own write, take them in one order."""
    for job_id in await open_ids(unit):
        await submit(unit, job_id, how=how)


async def release(unit: WriteUnit) -> None:
    """Submit the chunk of every job that may run, as a nightly closes its run, in the unit that
    closes it (`jobs.nightly.locked`): one that waited for the nightly runs now, in its place in
    line, and those on tonight's run go in the queue. Under the `tonight` lock, which putting a
    job there holds too, each side stamped by the clock after taking it (`queued_at`, the
    run's `finished_at`): one put there before this unit is released here, one put there after
    it sees the run closed after it, and waits for the next nightly."""
    _holds(unit, "tonight")
    await _each(unit, "submit")


async def wake(unit: WriteUnit) -> None:
    """Bring the waiting chunk of every job that may run forward to when it is due: one a
    nightly held back (looking again after `jobs.worker.WAIT`, or the run lock taken) goes once
    the nightly let go of the run lock. Pending items only: a running chunk is left as it is."""
    await _each(unit, "wake")


async def nightly_waits(unit: ReadUnit, *, fires: bool = True) -> bool:
    """Whether a nightly run is due or running: a nightly run open and alive; a nightly's work
    item that waits to run (for its time, the run lock, or a retry), or is running and has not
    closed its run without an error (opening it included, before its run's row exists; its
    lease lapsed too, as a take-back makes it pending again; a run that failed too, which its
    item may try again). No job's chunk starts then, and no re-run's pass. An item whose run
    closed cleanly is running no more: what the nightly held back goes as it closes
    (`release`). A CLI command's item (`command:nightly`) counts always; a scheduled fire's
    only with `fires` (the reader's configuration has the schedule on, so its `serve` runs
    them): a fire no process here runs holds nothing back."""
    nightly = "(w.kind = 'command:nightly' OR (%(fires)s AND w.kind = 'nightly'))"
    row = await (
        await unit.execute(
            f"SELECT EXISTS (SELECT 1 FROM work_items w WHERE {nightly} AND w.state = 'pending') "
            f"OR EXISTS (SELECT 1 FROM work_items w WHERE {nightly} "
            "AND w.state = 'running' AND NOT EXISTS ("
            "SELECT 1 FROM runs r WHERE r.lease_token = w.lease_token "
            "AND r.finished_at IS NOT NULL AND r.error IS NULL)) "
            "OR EXISTS (SELECT 1 FROM runs r WHERE r.kind = 'nightly' AND r.finished_at IS NULL "
            f"AND {ALIVE}) AS waits",
            {"fires": fires},
        )
    ).fetchone()
    assert row is not None  # a SELECT of a value has its row
    return row["waits"]


@overload
async def get(
    unit: ReadUnit, job_id: int, *, lock: Literal[False] = False
) -> dict[str, Any] | None: ...
@overload
async def get(unit: WriteUnit, job_id: int, *, lock: Literal[True]) -> dict[str, Any] | None: ...
async def get(unit: ReadUnit, job_id: int, *, lock: bool = False) -> dict[str, Any] | None:
    """The job; with `lock`, its row locked to the end of the unit, a write unit."""
    return await (
        await unit.execute(
            "SELECT * FROM jobs WHERE id = %s" + (" FOR UPDATE" if lock else ""), (job_id,)
        )
    ).fetchone()


@overload
async def process_of(
    unit: ReadUnit, paper_id: str, *, lock: Literal[False] = False
) -> dict[str, Any] | None: ...
@overload
async def process_of(
    unit: WriteUnit, paper_id: str, *, lock: Literal[True]
) -> dict[str, Any] | None: ...
async def process_of(unit: ReadUnit, paper_id: str, *, lock: bool = False) -> dict[str, Any] | None:
    """The paper's newest Process job not discarded; with `lock`, its row locked to the end of
    the unit, a write unit."""
    return await (
        await unit.execute(
            "SELECT * FROM jobs WHERE kind = 'process' AND scope -> 'papers' = %s "
            "AND state <> 'discarded' ORDER BY id DESC LIMIT 1" + (" FOR UPDATE" if lock else ""),
            (Jsonb([paper_id]),),
        )
    ).fetchone()


async def process(unit: WriteUnit, paper_id: str) -> dict[str, Any]:
    """Queue the paper's Process job (D1): the one already queued or running, the one not run or
    failed put back in the queue, or a new one. Asked again after a lost reply, it finds the job
    the first asking queued. Call it in a change of the paper: the job's row stays locked to its
    end, so the Jobs page's actions wait and then see what this did."""
    found = await process_of(unit, paper_id, lock=True)
    if found is not None and found["state"] in ACTIVE:
        return found
    if found is not None and found["state"] != "done":
        moved = await move(unit, found["id"], (*NOT_RUN, "failed"), "queued")
        assert moved is not None  # its row is locked: nothing moved it meanwhile
        return moved
    job = await get(unit, await create(unit, "process", {"papers": [paper_id]}, 1, "queued"))
    assert job is not None
    return job


async def processing(unit: ReadUnit, paper_id: str) -> bool:
    """Whether the paper's Process job is under way: on tonight's run, queued or running."""
    row = await (
        await unit.execute(
            "SELECT 1 FROM jobs WHERE kind = 'process' AND scope -> 'papers' = %s "
            "AND state = ANY(%s)",
            (Jsonb([paper_id]), list(ACTIVE)),
        )
    ).fetchone()
    return row is not None


async def with_process(unit: ReadUnit, paper_ids: list[str]) -> set[str]:
    """Those of the papers that have a Process job, in whatever state: the only ones
    `settle_process` and `processing` have anything to find for."""
    rows = await (
        await unit.execute(
            "SELECT DISTINCT scope -> 'papers' ->> 0 AS paper_id FROM jobs "
            "WHERE kind = 'process' AND scope -> 'papers' IN "
            "(SELECT jsonb_build_array(id) FROM unnest(%s::text[]) AS id)",
            (paper_ids,),
        )
    ).fetchall()
    return {r["paper_id"] for r in rows}


async def settle_process(unit: WriteUnit, paper_id: str, *, owner: bool = False) -> None:
    """A failed Process job settles with its paper's failures (DESIGN.md, Jobs): once none is
    left (a later run did the step, the owner acted on Needs you), it goes back in the queue to
    do what is left, or, the paper dropped, it is discarded. A running one the `owner` acted
    on meanwhile (a Try again on Needs you, say, which its steps may have passed already) is
    asked again: queued since it started, which `finish` reads. Call it under the paper's settle
    lock."""
    if owner:
        await unit.execute(
            "UPDATE jobs SET queued_at = clock_timestamp() WHERE kind = 'process' "
            "AND state = 'running' AND scope -> 'papers' = %s",
            (Jsonb([paper_id]),),
        )
    rows = await (
        await unit.execute(
            f"""
            UPDATE jobs j SET changed_at = now(), queued_at = now(), done = 0, counts = '{{}}',
                   error = NULL,
                   state = CASE WHEN NOT p.dropped THEN 'queued' ELSE 'discarded' END
            FROM papers p
            WHERE p.id = %s AND j.kind = 'process' AND j.state = 'failed'
              AND j.scope -> 'papers' = %s
              AND NOT EXISTS (SELECT 1 FROM step_failures f
                              WHERE f.paper_id = p.id AND {ACTIONABLE})
            RETURNING j.id
            """,
            (paper_id, Jsonb([paper_id])),
        )
    ).fetchall()
    for r in sorted(rows, key=lambda r: r["id"]):
        await submit(unit, r["id"])


async def move(
    unit: WriteUnit,
    job_id: int,
    sources: tuple[str, ...],
    to: str,
    ceiling: float | None = None,
) -> dict[str, Any] | None:
    """Move the job from one of `sources` to `to`; None when it is in none of them. Put in the
    queue or on tonight's run, it waits from now, under `ceiling` when given; a failed job put
    back starts over, its steps taking only what is still due; a stopped one goes on from
    where it stopped. Put in the queue, its chunk is submitted (`submit`). Put on tonight's
    run, under the `tonight` lock, stamped by the clock after it (`release`)."""
    if to == "tonight":
        _holds(unit, "tonight")
    moved = await (
        await unit.execute(
            """
            UPDATE jobs SET state = %(to)s, changed_at = now(), wake_at = NULL,
                   queued_at = CASE WHEN %(to)s IN ('tonight', 'queued')
                                    THEN clock_timestamp() ELSE queued_at END,
                   ceiling = coalesce(%(ceiling)s, ceiling),
                   done = CASE WHEN state = 'failed' THEN 0 ELSE done END,
                   counts = CASE WHEN state = 'failed' THEN '{}' ELSE counts END,
                   error = CASE WHEN state IN ('failed', 'stopped') AND %(to)s <> 'discarded'
                                THEN NULL ELSE error END
            WHERE id = %(id)s AND state = ANY(%(sources)s) RETURNING *
            """,
            {"to": to, "id": job_id, "sources": list(sources), "ceiling": ceiling},
        )
    ).fetchone()
    if moved is not None:
        await submit(unit, job_id)
    return moved


async def may_run(unit: ReadUnit, job: dict[str, Any]) -> bool:
    """Whether the job may run a chunk now, but for its `wake_at` (`_RUNNABLE`)."""
    row = await (
        await unit.execute(f"SELECT {_RUNNABLE} AS ok FROM jobs j WHERE j.id = %s", (job["id"],))
    ).fetchone()
    return bool(row and row["ok"])


async def claim(unit: WriteUnit, job_id: int, *, afresh: bool) -> dict[str, Any]:
    """Mark the job running for its chunk's item, which holds it while its lease lasts: one
    item per job, so no other chunk runs it meanwhile. Call it with the job's row locked and
    `may_run` true. `afresh`: no run of this process has the job open, so one found running
    was another's (its process gone, its item taken back, or this process restarted); a
    Process job then starts over from its paper, so a step the owner's action left due while it
    ran is done too, and the mark of that action (`settle_process`) is handled: its claim is
    stamped by the clock after the row's lock, so `finish` does not ask the job again. The session
    writing it goes in the old liveness columns, which the image before the Work gate reads
    until P7. Its next chunk is arranged (`arrange`): one running goes on."""
    row = await (
        await unit.execute(
            f"""
            UPDATE jobs j SET state = 'running',
                   (backend_pid, backend_start) = (SELECT a.pid, a.backend_start FROM {SESSION}),
                   done = CASE WHEN %(afresh)s AND j.kind = 'process' AND j.state = 'running'
                               THEN 0 ELSE j.done END,
                   changed_at = CASE WHEN j.state = 'running'
                                          AND NOT (%(afresh)s AND j.kind = 'process')
                                     THEN j.changed_at ELSE clock_timestamp() END
            WHERE j.id = %(id)s RETURNING j.*
            """,
            {"id": job_id, "afresh": afresh},
        )
    ).fetchone()
    assert row is not None  # locked by the caller
    await arrange(unit, job_id)
    return row


async def link(unit: WriteUnit, job_id: int, item: WorkItem, run_id: int) -> None:
    """Record that the job's chunk `item` works on the run `run_id`: the run is alive by this
    claim of the item (`store.work.link_run`), and the job's latest run is this one, which a
    process that kept another open for it reads as that one no longer being the job's. The
    job's row first, then its item's, as every path that takes both."""
    await unit.execute("UPDATE jobs SET run_id = %s WHERE id = %s", (run_id, job_id))
    await work.link_run(unit, item, run_id)


async def progress(
    unit: WriteUnit,
    job_id: int,
    done: int,
    counts: dict[str, int],
    ladder: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Record how far the running job got and what its steps recorded so far (a re-run's
    `ladder` too, when given); None when it is no longer running (the owner ended it). Its
    chunk's unit is fenced on its item's lease, so a chunk whose item was taken back writes
    nothing. Its next chunk is arranged (`arrange`)."""
    row = await (
        await unit.execute(
            """
            UPDATE jobs SET done = %(done)s, counts = %(counts)s,
                   ladder = coalesce(%(ladder)s, ladder)
            WHERE id = %(id)s AND state = 'running'
            RETURNING *
            """,
            {
                "done": done,
                "counts": Jsonb(counts),
                "ladder": Jsonb(ladder) if ladder is not None else None,
                "id": job_id,
            },
        )
    ).fetchone()
    await arrange(unit, job_id)
    return row


async def wait(unit: WriteUnit, job_id: int, until: datetime) -> None:
    """The running job is not claimed again before `until`: what it has left waits for the
    archive. Its next chunk is arranged then (`arrange`)."""
    await unit.execute(
        "UPDATE jobs SET wake_at = %s WHERE id = %s AND state = 'running'", (until, job_id)
    )
    await arrange(unit, job_id)


async def spent(unit: ReadUnit, job_id: int) -> float:
    """What the job's calls cost so far, in all its runs: a call whose charge is unknown counts
    at what it reserved against the ceiling (`inference_calls.reserved`) until its charge is
    known, as the ceiling counted it (`llm.ceiling`), whichever process or run made it."""
    row = await (
        await unit.execute(
            "SELECT coalesce(sum(coalesce((c.usage ->> 'cost')::numeric, c.reserved::numeric)), 0) "
            "AS cost "
            "FROM inference_calls c JOIN runs r ON r.id = c.run_id WHERE r.job_id = %s",
            (job_id,),
        )
    ).fetchone()
    return float(row["cost"])


async def finish(
    unit: WriteUnit,
    job_id: int,
    state: str,
    error: str | None = None,
) -> bool:
    """End the running job as done, failed, discarded or stopped (by its ceiling, `error`
    saying where, stored as a text column takes it: a chunk that cannot record its end leaves
    its job running). One done that the owner asked again while it ran (`settle_process`) goes
    back in the queue from its first paper instead, so its steps take what the owner's action
    left due. Its next chunk is arranged (`arrange`): none, unless queued again. Returns whether
    it did: False when the job is no longer running."""
    cur = await unit.execute(
        """
        UPDATE jobs SET error = %(error)s, changed_at = now(),
               backend_pid = NULL, backend_start = NULL,
               state = CASE WHEN %(state)s::text = 'done' AND queued_at > changed_at
                            THEN 'queued' ELSE %(state)s::text END,
               done = CASE WHEN %(state)s::text = 'done' AND queued_at > changed_at
                           THEN 0 ELSE done END
        WHERE id = %(id)s AND state = 'running'
        """,
        {"state": state, "error": None if error is None else stored(error), "id": job_id},
    )
    await arrange(unit, job_id)
    return cur.rowcount == 1


async def fail(unit: WriteUnit, job_id: int, error: str) -> dict[str, Any] | None:
    """Fail the job with `error` if it may run (`_RUNNABLE`): its chunk raised what its own
    handling did not record, so it runs no more until the owner tries it again. Its next chunk
    is arranged: none. Returns the job failed, None when it may not run."""
    row = await (
        await unit.execute(
            f"""
            UPDATE jobs j SET state = 'failed', error = %s, changed_at = now(),
                   backend_pid = NULL, backend_start = NULL
            WHERE j.id = %s AND {_RUNNABLE} RETURNING j.*
            """,
            (stored(error), job_id),
        )
    ).fetchone()
    await arrange(unit, job_id)
    return row


async def listing(unit: ReadUnit, done_limit: int, runs: int) -> list[dict[str, Any]]:
    """Every job not discarded and every Deep card the owner asked for, as the Jobs page shows
    them: a job with its paper (a Process job's), whether a step of that paper waits on the owner
    (`stuck`, after `runs` failed runs), and what its runs' calls cost, a run whose process died
    included (a call of unknown cost counts nothing, as in the month's spend); a
    card with its pass's state, mapped onto a job's, failed once it waits on the owner (`stuck`,
    as Needs you lists it, after `runs` failed runs or a long wait for its text). A dropped
    paper's card is left out unless done or its attempt is open: nothing else runs it. Only the
    newest `done_limit` of those done. A re-run comes with its topic, scopes, revision, ceiling
    and estimate; a replay with its ceiling and estimate; a digest written again with the
    digest's topic, period and day."""
    return await (
        await unit.execute(
            f"""
            WITH found AS (
                SELECT 'job' AS source, j.id, j.kind, j.state, j.papers, j.done, j.counts,
                       j.error, j.changed_at AS at, j.queued_at, j.ceiling, j.estimate,
                       j.scope ->> 'topic' AS topic, j.scope -> 'scopes' AS scopes,
                       j.scope ->> 'revision' AS revision,
                       CASE WHEN j.kind = 'process' THEN j.scope -> 'papers' ->> 0 END
                           AS paper_id,
                       (j.scope ->> 'digest')::bigint AS digest_id,
                       j.kind = 'process' AND EXISTS (
                           SELECT 1 FROM step_failures f JOIN papers p ON p.id = f.paper_id
                           WHERE f.paper_id = j.scope -> 'papers' ->> 0
                             AND f.failed_runs >= %(runs)s AND {ACTIONABLE}) AS stuck,
                       (SELECT sum((c.usage ->> 'cost')::numeric) FROM inference_calls c
                        JOIN runs r ON r.id = c.run_id WHERE r.job_id = j.id) AS cost
                FROM jobs j WHERE j.state <> 'discarded'
                UNION ALL
                SELECT 'card', f.id, 'deep',
                       CASE WHEN {STUCK} THEN 'failed'
                            WHEN f.state IN ('awaiting_text', 'ready') THEN 'queued'
                            ELSE f.state END,
                       1, CASE WHEN f.state = 'done' THEN 1 ELSE 0 END, '{{}}'::jsonb,
                       f.last_error, coalesce(f.finished_at, f.tried_at, f.queued_at),
                       f.queued_at, NULL, NULL, NULL, NULL, NULL, f.paper_id, NULL, {STUCK},
                       NULL
                FROM fulltext_passes f JOIN papers p ON p.id = f.paper_id
                WHERE f.kind = 'deep' AND f.reason = %(requested)s
                  AND (NOT p.dropped OR f.state = 'done' OR {STARTED})
            ), ranked AS (
                SELECT *, row_number() OVER (PARTITION BY state = 'done' ORDER BY at DESC, id DESC)
                          AS newest
                FROM found
            )
            SELECT x.*, p.title, d.topic AS digest_topic, d.period, d.day,
                   k.name AS topic_name
            FROM ranked x
            LEFT JOIN papers p ON p.id = x.paper_id LEFT JOIN digests d ON d.id = x.digest_id
            LEFT JOIN classes k ON k.slug = coalesce(x.topic, d.topic)
            WHERE x.state <> 'done' OR x.newest <= %(done)s
            ORDER BY x.at DESC, x.id DESC
            """,
            {"requested": REQUESTED, "done": done_limit} | stuck_params(runs),
        )
    ).fetchall()


def _holds(unit: WriteUnit, lock: str | tuple[str, str]) -> None:
    if lock not in unit.locks:
        raise RuntimeError(f"unit {unit.name!r} reads for a job it makes without the {lock} lock")


async def open_rerun(unit: WriteUnit, scope: dict[str, Any]) -> dict[str, Any] | None:
    """The newest re-run not ended (not run, on tonight's run, queued or running) with the
    same topic version, scopes and date range as `scope`, its row locked: one asked for again
    after a lost reply. The version is the one it grades under; the revision beside it only
    names it. The unit holds the topic's re-run lock (`("rerun", topic)`), so two asking at
    once make one."""
    _holds(unit, ("rerun", scope["topic"]))
    same = {k: scope.get(k) for k in ("topic", "version", "scopes", "since", "until")}
    return await (
        await unit.execute(
            "SELECT * FROM jobs WHERE kind = 'rerun' AND state = ANY(%s) "
            "AND jsonb_build_object('topic', scope -> 'topic', 'version', scope -> 'version', "
            "'scopes', scope -> 'scopes', 'since', scope -> 'since', 'until', scope -> 'until') "
            "= %s ORDER BY id DESC LIMIT 1 FOR UPDATE",
            ([*NOT_RUN, *ACTIVE], Jsonb(same)),
        )
    ).fetchone()


async def open_replay(unit: WriteUnit) -> dict[str, Any] | None:
    """The newest replay not ended (not run, on tonight's run, queued or running), its row
    locked. The unit holds the replay lock, so two asking at once make one."""
    _holds(unit, "replay")
    return await (
        await unit.execute(
            "SELECT * FROM jobs WHERE kind = 'replay' AND state = ANY(%s) "
            "ORDER BY id DESC LIMIT 1 FOR UPDATE",
            ([*NOT_RUN, *ACTIVE],),
        )
    ).fetchone()


@overload
async def digest_job(
    unit: ReadUnit, digest_id: int, *, lock: Literal[False] = False
) -> dict[str, Any] | None: ...
@overload
async def digest_job(
    unit: WriteUnit, digest_id: int, *, lock: Literal[True]
) -> dict[str, Any] | None: ...
async def digest_job(
    unit: ReadUnit, digest_id: int, *, lock: bool = False
) -> dict[str, Any] | None:
    """The digest's newest write job not discarded; with `lock`, its row locked to the end of
    the unit, a write unit."""
    return await (
        await unit.execute(
            "SELECT * FROM jobs WHERE kind = 'digest' AND scope -> 'digest' = %s "
            "AND state <> 'discarded' ORDER BY id DESC LIMIT 1" + (" FOR UPDATE" if lock else ""),
            (Jsonb(digest_id),),
        )
    ).fetchone()


async def write_digest(unit: WriteUnit, digest_id: int) -> dict[str, Any]:
    """Queue the failed digest's write job: the one already queued or running, the failed one
    put back in the queue, or a new one. Asked again after a lost reply, it finds the job the
    first asking queued. Call it with the digest's row locked, so two asking at once make
    one."""
    found = await digest_job(unit, digest_id, lock=True)
    if found is not None and found["state"] in ACTIVE:
        return found
    if found is not None and found["state"] == "failed":
        moved = await move(unit, found["id"], ("failed",), "queued")
        assert moved is not None  # its row is locked: nothing moved it meanwhile
        return moved
    job = await get(unit, await create(unit, "digest", {"digest": digest_id}, 1, "queued"))
    assert job is not None
    return job


async def settle_digest(unit: WriteUnit, digest_id: int) -> None:
    """The digest is published (by the nightly run, say): a failed job writing it again settles
    with it, done, as its Needs you item does (DESIGN.md, Jobs)."""
    await unit.execute(
        "UPDATE jobs SET state = 'done', error = NULL, changed_at = now() "
        "WHERE kind = 'digest' AND state = 'failed' AND scope -> 'digest' = %s",
        (Jsonb(digest_id),),
    )


# -- a topic re-run's scopes (pipeline.md, section 10) ----------------------------------------


async def labelled(unit: ReadUnit, topic: str) -> list[str]:
    """The papers the owner labelled for the topic or reported missed there, not dropped."""
    rows = await (
        await unit.execute(
            "SELECT id FROM papers WHERE NOT dropped AND id IN ("
            "SELECT paper_id FROM labels WHERE topic = %(t)s "
            "UNION SELECT paper_id FROM misses WHERE topic = %(t)s) ORDER BY id",
            {"t": topic},
        )
    ).fetchall()
    return [r["id"] for r in rows]


async def members(unit: ReadUnit, classes: list[str]) -> list[str]:
    """The papers on a topic's page (`store.reading.SCOPE`), not dropped: those standing in any
    of `classes` (its class and those under it), and those with an effective grade for it or a
    followed topic under it (a keyword hit, a reported miss)."""
    rows = await (
        await unit.execute(
            f"""
            SELECT id FROM (
                SELECT m.paper_id AS id FROM memberships m
                WHERE m.class = ANY(%(classes)s) AND {SHOWN_MEMBER}
                UNION
                SELECT g.paper_id FROM effective_grades g WHERE g.topic = ANY(%(classes)s)
            ) x JOIN papers p USING (id) WHERE NOT p.dropped ORDER BY id
            """,
            {"classes": classes},
        )
    ).fetchall()
    return [r["id"] for r in rows]


async def published_between(unit: ReadUnit, since: datetime, until: datetime) -> list[str]:
    """The papers published in [since, until), not dropped."""
    rows = await (
        await unit.execute(
            "SELECT id FROM papers WHERE NOT dropped AND published >= %s "
            "AND published < %s ORDER BY id",
            (since, until),
        )
    ).fetchall()
    return [r["id"] for r in rows]
