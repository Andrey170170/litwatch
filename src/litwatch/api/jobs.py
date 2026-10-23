"""The jobs API (docs/plans/jobs.md; DESIGN.md, Jobs): the owner's queue in four groups, with
the Deep cards they asked for read alongside from their passes; the actions on a job; and
Process, which fetches a paper and queues the job that processes it now (D1)."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request

from litwatch.api import schemas as s
from litwatch.api.deps import Conn, protected
from litwatch.core import FAILED_RUNS, Cause
from litwatch.feedback import misses, word
from litwatch.intake.search import NoAnswerError
from litwatch.store import jobs as jobs_store
from litwatch.store import papers as papers_store
from litwatch.store import taxonomy as taxonomy_store
from litwatch.store import word as word_store

router = protected("/api/jobs")

# The newest done jobs and cards the page lists.
DONE_SHOWN = 20

# Each group's states (`store.jobs`), and the order within it.
_GROUPS: dict[str, tuple[str, ...]] = {
    "not_run": jobs_store.NOT_RUN,
    "running": jobs_store.ACTIVE,
    "failed": ("failed",),
    "done": ("done",),
}
_STATE_ORDER = {"running": 0, "queued": 1, "tonight": 2}

# What each action moves a job from, and to.
MOVES: dict[s.JobAction, tuple[tuple[str, ...], str]] = {
    "run": (jobs_store.NOT_RUN, "queued"),
    "tonight": (jobs_store.NOT_RUN, "tonight"),
    "discard": (jobs_store.NOT_RUN, "discarded"),
    "again": (("failed",), "queued"),
}


def _actions(row: dict[str, Any]) -> list[s.JobAction]:
    if row["source"] == "card":  # Try again only once it waits on the owner, as on Needs you
        return ["again"] if row["stuck"] else []
    return [a for a, (sources, _) in MOVES.items() if row["state"] in sources]


def _job(row: dict[str, Any]) -> s.Job:
    card = row["source"] == "card"
    return s.Job(
        key=f"{row['source']}:{row['id']}",
        kind=row["kind"],
        id=None if card else row["id"],
        pass_id=row["id"] if card else None,
        state=row["state"],
        paper=s.JobPaper(id=row["paper_id"], title=row["title"])
        if row["title"] is not None
        else None,
        papers=row["papers"],
        done=row["done"],
        cost=float(row["cost"]) if row["cost"] is not None else None,
        error=row["error"],
        at=row["at"],
        actions=_actions(row),
    )


async def _listing(c: Conn) -> s.Jobs:
    rows = await jobs_store.listing(c, DONE_SHOWN, FAILED_RUNS)
    groups: dict[str, list[dict[str, Any]]] = {
        name: [r for r in rows if r["state"] in states] for name, states in _GROUPS.items()
    }
    # Waiting and running in the order they run; the others newest first, as read.
    groups["running"].sort(key=lambda r: (_STATE_ORDER[r["state"]], r["queued_at"], r["id"]))
    groups["not_run"].sort(key=lambda r: (r["queued_at"], r["id"]))
    return s.Jobs(**{name: [_job(r) for r in found] for name, found in groups.items()})


@router.get("")
async def jobs(c: Conn) -> s.Jobs:
    """The jobs and the Deep cards asked for, in the Jobs page's groups."""
    return await _listing(c)


PROCESS_REFUSED = {
    404: {"description": "No such paper"},
    409: {"description": "The paper is in the collection already or dismissed, or no taxonomy"},
    503: {"description": "arXiv did not answer in time; nothing was stored"},
}


@router.post("/process", status_code=202, responses=PROCESS_REFUSED)
async def process(body: s.ProcessIn, request: Request, c: Conn) -> s.ProcessJob | None:
    """Process a paper outside the collection now (D1): fetch it from arXiv when litwatch lacks
    it, with no transaction open, then store it and queue its Process job in one. Asked again,
    it answers with the job already queued or running, and puts a failed one back in the
    queue. Without a taxonomy there is nothing to sort the paper into: 409, nothing kept. With
    the schedule off no worker runs jobs: the paper is fetched only, for the next run, and the
    answer is null."""
    try:
        paper_id, fetched = await misses.resolve(c, body.paper_id)
    except misses.NotFoundError as exc:
        raise HTTPException(status_code=404, detail="no such paper") from exc
    except NoAnswerError as exc:
        raise HTTPException(status_code=503, detail=misses.SILENT) from exc
    async with word.change(c, paper_id, cause=Cause.OWNER):
        if await taxonomy_store.load(c) is None:  # nothing to sort the paper into
            raise HTTPException(status_code=409, detail="no taxonomy yet")
        await misses.keep(c, paper_id, fetched)
        if await word_store.in_collection(c, paper_id):
            raise HTTPException(status_code=409, detail="already in the collection")
        if await papers_store.dismissed_at(c, paper_id) is not None:
            raise HTTPException(status_code=409, detail="the paper is dismissed")
        if not request.app.state.config.schedule.enabled:
            return None
        job = await jobs_store.process(c, paper_id)
    request.app.state.jobs_wake.set()
    return s.ProcessJob(id=job["id"], state=job["state"], error=job["error"])


JOB_REFUSED = {
    404: {"description": "No such job"},
    409: {"description": "The job's state does not allow it"},
}


@router.post("/{job_id}/{action}", responses=JOB_REFUSED)
async def act(job_id: int, action: s.JobAction, request: Request, c: Conn) -> s.Jobs:
    """Run now, With tonight's run or Discard a job not run; Try again (`again`) a failed one.
    Answers with the page's groups as they stand after it."""
    sources, to = MOVES[action]
    if await jobs_store.move(c, job_id, sources, to) is None:
        job = await jobs_store.get(c, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="no such job")
        raise HTTPException(status_code=409, detail=f"the job is {job['state']} now")
    if to == "queued":
        request.app.state.jobs_wake.set()
    return await _listing(c)
