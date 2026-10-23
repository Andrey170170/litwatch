"""The jobs API (docs/plans/jobs.md; DESIGN.md, Jobs): the owner's queue in four groups, with
the Deep cards they asked for read alongside from their passes; the actions on a job; Process,
which fetches a paper and queues the job that processes it now (D1); and the re-run offer after
a topic edit, which starts a re-run or keeps it under Not run."""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any

from fastapi import HTTPException, Query, Request

from litwatch.api import schemas as s
from litwatch.api.deps import Conn, protected
from litwatch.config import Config
from litwatch.core import FAILED_RUNS, Cause, Topic
from litwatch.feedback import misses, word
from litwatch.intake.search import NoAnswerError
from litwatch.jobs import reruns
from litwatch.store import failures
from litwatch.store import fulltext as fulltext_store
from litwatch.store import jobs as jobs_store
from litwatch.store import papers as papers_store
from litwatch.store import quality
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


# Why Process and Try again refuse a Process job whose paper waits on the owner: it would find
# nothing due and fail at once, and the owner's action on Needs you settles it (DESIGN.md, Jobs).
WAITS = "the paper waits on you on Needs you"


def _actions(row: dict[str, Any]) -> list[s.JobAction]:
    if row["source"] == "card":  # Try again only once it waits on the owner, as on Needs you
        return ["again"] if row["stuck"] else []
    return [
        a
        for a, (sources, _) in MOVES.items()
        if row["state"] in sources and not (a == "again" and row["stuck"])
    ]


def _money(value: Any) -> float | None:
    return None if value is None else float(value)


def _job(row: dict[str, Any]) -> s.Job:
    card = row["source"] == "card"
    rerun = row["kind"] == "rerun"
    return s.Job(
        key=f"{row['source']}:{row['id']}",
        kind=row["kind"],
        id=None if card else row["id"],
        pass_id=row["id"] if card else None,
        state=row["state"],
        paper=s.JobPaper(id=row["paper_id"], title=row["title"])
        if row["title"] is not None
        else None,
        topic=s.JobTopic(
            slug=row["topic"],
            name=row["topic_name"] or row["topic"],
            revision=None if row["revision"] is None else int(row["revision"]),
        )
        if rerun
        else None,
        scopes=row["scopes"] or [],
        papers=row["papers"],
        done=row["done"],
        graded=row["counts"].get("graded", 0) if rerun else None,
        cost=_money(row["cost"]),
        estimate=_money(row["estimate"]),
        ceiling=_money(row["ceiling"]),
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
    409: {
        "description": "The paper is in the collection already, dismissed or waits on the owner, "
        "or no taxonomy"
    },
    503: {"description": "arXiv did not answer in time; nothing was stored"},
}


@router.post("/process", status_code=202, responses=PROCESS_REFUSED)
async def process(body: s.ProcessIn, request: Request, c: Conn) -> s.ProcessJob | None:
    """Process a paper outside the collection now (D1): fetch it from arXiv when litwatch lacks
    it, with no transaction open, then store it and queue its Process job in one. Asked again,
    it answers with the job already queued or running, changing nothing (its paper classified
    meanwhile included), and puts a failed one back in the queue, unless a step of the paper
    waits on the owner (409: it would find nothing due, and the owner's action on Needs you
    settles the job). Without a taxonomy there is nothing to sort the paper into: 409, nothing
    kept. With the schedule off no worker runs jobs: the paper is fetched only, for the next run,
    and the answer is null."""
    try:
        paper_id, fetched = await misses.resolve(c, body.paper_id)
    except misses.NotFoundError as exc:
        raise HTTPException(status_code=404, detail="no such paper") from exc
    except NoAnswerError as exc:
        raise HTTPException(status_code=503, detail=misses.SILENT) from exc
    async with c.transaction():
        # A job already under way is only answered, before any change: the owner's settle would
        # ask a running one again (`settle_process`), and it would run once more for nothing.
        # Under the paper's locks, so a Process in flight (a double click) has either committed
        # its job or not started.
        await word.lock_many(c, [paper_id])
        found = await jobs_store.process_of(c, paper_id, lock=True)
        if (
            found is not None
            and found["state"] in jobs_store.ACTIVE
            and await papers_store.dismissed_at(c, paper_id) is None
        ):
            return s.ProcessJob(id=found["id"], state=found["state"], error=found["error"])
        async with word.change(c, paper_id, cause=Cause.OWNER):
            if await taxonomy_store.load(c) is None:  # nothing to sort the paper into
                raise HTTPException(status_code=409, detail="no taxonomy yet")
            await misses.keep(c, paper_id, fetched)
            if await word_store.in_collection(c, paper_id):
                raise HTTPException(status_code=409, detail="already in the collection")
            if await papers_store.dismissed_at(c, paper_id) is not None:
                raise HTTPException(status_code=409, detail="the paper is dismissed")
            if await failures.waits_on_owner(c, [paper_id], FAILED_RUNS):
                raise HTTPException(status_code=409, detail=WAITS)
            if not request.app.state.config.schedule.enabled:
                return None
            job = await jobs_store.process(c, paper_id)
    request.app.state.jobs_wake.set()
    return s.ProcessJob(id=job["id"], state=job["state"], error=job["error"])


JOB_REFUSED = {
    404: {"description": "No such job"},
    409: {"description": "The job's state does not allow it, or its paper waits on the owner"},
}


# -- the re-run offer ---------------------------------------------------------------------------

RERUN_REFUSED = {
    404: {"description": "No such topic"},
    409: {"description": "The topic is not active, the scopes name no paper, or no worker runs"},
}


async def _offered(
    c: Conn,
    config: Config,
    slug: str,
    scopes: list[s.RerunScope],
    since: date | None,
    until: date | None,
) -> tuple[Topic, s.RerunOffer, list[str]]:
    """The offer for the topic, and the papers of the chosen scopes, in id order."""
    tree = await taxonomy_store.load(c)
    topic = tree.topic(slug) if tree else None
    if tree is None or topic is None:
        raise HTTPException(status_code=404, detail="no such topic")
    if reruns.active_topic(tree, slug) is None:
        raise HTTPException(status_code=409, detail=f"the topic is {topic.status.value}")
    dates = reruns.Range(since, until) if since and until else None
    found = {
        scope: await reruns.scope_papers(c, config, tree, topic, scope, dates)
        for scope in reruns.SCOPES
    }
    per_paper, missing = await reruns.per_paper(c, config, topic)
    chosen = sorted({p for scope in scopes for p in found[scope]})

    def cost(n: int) -> float | None:
        return None if per_paper is None else n * per_paper

    offer = s.RerunOffer(
        topic=s.JobTopic(
            slug=slug,
            name=tree.get(slug).name,
            revision=(await quality.topic_revisions(c)).get(slug),
        ),
        rows=[
            s.RerunScopeRow(scope=scope, papers=len(ids), estimate=cost(len(ids)))
            for scope, ids in found.items()
        ],
        papers=len(chosen),
        estimate=cost(len(chosen)),
        missing=missing,
        unpriced=await reruns.unpriced(c, config, topic),
        ceiling=reruns.CEILING,
    )
    return topic, offer, chosen


@router.get("/reruns/offer", responses=RERUN_REFUSED)
async def rerun_offer(
    topic: str,
    request: Request,
    c: Conn,
    scopes: Annotated[list[s.RerunScope], Query()] = [],  # noqa: B006 (FastAPI copies it)
    since: date | None = None,
    until: date | None = None,
) -> s.RerunOffer:
    """The re-run offer after an edit of the topic: each scope with its papers and estimate, and
    the chosen `scopes` together (a date range from `since` to `until`, both included)."""
    _, offer, _ = await _offered(c, request.app.state.config, topic, scopes, since, until)
    return offer


@router.post("/reruns", status_code=201, responses=RERUN_REFUSED)
async def rerun(body: s.RerunIn, request: Request, c: Conn) -> s.Jobs:
    """Run the offer's re-run now (`run`), or keep it under Not run (Not now). Its papers are
    those of its scopes as they stand now; it grades them under the topic's version now (D3),
    and fails should the topic be edited before its grading is done. The same re-run not ended
    yet is not made again. Run needs the schedule on, whose worker runs jobs. Answers with the
    Jobs page's groups."""
    config = request.app.state.config
    if body.run and not config.schedule.enabled:
        raise HTTPException(status_code=409, detail="the schedule is off: no worker runs jobs")
    topic, offer, chosen = await _offered(
        c, config, body.topic, body.scopes, body.since, body.until
    )
    if not chosen:
        raise HTTPException(status_code=409, detail="the scopes name no paper")
    scope = {
        "topic": topic.slug,
        "revision": offer.topic.revision,
        "version": topic.version,
        "scopes": [k for k in reruns.SCOPES if k in body.scopes],  # one order, so one identity
        "since": body.since.isoformat() if body.since else None,
        "until": body.until.isoformat() if body.until else None,
        "papers": chosen,
    }
    state = "queued" if body.run else "offered"
    async with c.transaction():
        # Asked again after a lost reply, the same re-run is found, not made twice; Run puts
        # one under Not run in the queue, under the ceiling given, as Run now on Jobs does.
        found = await jobs_store.open_rerun(c, scope)
        if found is None:
            await jobs_store.create(
                c, "rerun", scope, len(chosen), state, ceiling=body.ceiling, estimate=offer.estimate
            )
        elif body.run and found["state"] in jobs_store.NOT_RUN:
            await _above_spent(c, found, body.ceiling)
            await jobs_store.move(c, found["id"], jobs_store.NOT_RUN, "queued", body.ceiling)
    if body.run:
        request.app.state.jobs_wake.set()
    return await _listing(c)


async def _above_spent(c: Conn, job: dict[str, Any], ceiling: float | None) -> None:
    """A stopped re-run goes on only under a ceiling above what it spent."""
    if job["state"] != "stopped":
        return
    spent = await jobs_store.spent(c, job["id"])
    if (ceiling or float(job["ceiling"])) <= spent:
        raise HTTPException(
            status_code=409, detail=f"the ceiling must be above what it spent, ${spent:.2f}"
        )


@router.post("/{job_id}/{action}", responses=JOB_REFUSED)
async def act(
    job_id: int, action: s.JobAction, request: Request, c: Conn, body: s.JobRun | None = None
) -> s.Jobs:
    """Run now, With tonight's run or Discard a job not run; Try again (`again`) a failed one,
    a Process job only while no step of its paper waits on the owner, checked under the paper's
    settle lock. Run now and With tonight's run take a re-run's new ceiling, which a job its
    ceiling stopped needs above what it spent; a re-run runs only with the schedule on, as in
    `rerun`. A discarded re-run lets go of its text passes. Answers with the page's groups as
    they stand after it."""
    sources, to = MOVES[action]
    ceiling = body.ceiling if body else None
    schedule_on = request.app.state.config.schedule.enabled
    async with c.transaction():
        job = await jobs_store.get(c, job_id)
        if job is not None and action == "again" and job["kind"] == "process":
            await word.lock_many(c, job["scope"]["papers"])
            if await failures.waits_on_owner(c, job["scope"]["papers"], FAILED_RUNS):
                raise HTTPException(status_code=409, detail=WAITS)
        # The job's row after its paper's settle lock, the order a settle takes them in.
        job = await jobs_store.get(c, job_id, lock=True)
        if job is None:
            raise HTTPException(status_code=404, detail="no such job")
        if job["state"] not in sources:
            raise HTTPException(status_code=409, detail=f"the job is {job['state']} now")
        if job["kind"] == "rerun" and to in ("queued", "tonight") and not schedule_on:
            raise HTTPException(status_code=409, detail="the schedule is off: no worker runs jobs")
        if to in ("queued", "tonight"):
            await _above_spent(c, job, ceiling)
        await jobs_store.move(c, job_id, sources, to, ceiling if job["kind"] == "rerun" else None)
        if to == "discarded":
            await fulltext_store.release_job(c, job_id)
    if to == "queued":
        request.app.state.jobs_wake.set()
    return await _listing(c)
