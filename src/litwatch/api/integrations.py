"""What the API knows of integrations: their status under Settings, the owner's retry and
skip, Deep cards and their requests, and the search providers asked beside litwatch's own
search.

The API never imports `integrations`. The service's start-up wiring hands `create_app` the
loaded integrations, which it sees only through the contracts in `events` and `core`.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException, Query, Request

from litwatch import events
from litwatch.api import schemas as s
from litwatch.api.deps import Conn, protected
from litwatch.archive import ArchiveError
from litwatch.core import FAILED_RUNS, Cause, SearchFilters, SearchHit, SearchProvider
from litwatch.core.fulltext import REQUESTED
from litwatch.feedback import misses, word
from litwatch.fulltext import deep as deep_passes
from litwatch.intake.search import NoAnswerError
from litwatch.integrations.archive import Archive
from litwatch.integrations.deep import Deep
from litwatch.store import failures, outbox
from litwatch.store import fulltext as fulltext_store
from litwatch.store import integrations as integrations_store
from litwatch.store import jobs as jobs_store
from litwatch.store import papers as papers_store
from litwatch.store import taxonomy as taxonomy_store
from litwatch.store import word as word_store

router = protected("/api")

# Searches run on a pool of their own: a provider whose thread hangs past its timeout (the
# thread cannot be stopped) ties up a worker here, never the default pool `to_thread` uses.
SEARCH_WORKERS = 4


def loaded(request: Request) -> list[events.Integration]:
    return request.app.state.integrations


def providers(integrations: list[events.Integration]) -> list[SearchProvider]:
    return [i for i in integrations if isinstance(i, SearchProvider)]


def card_maker(integrations: list[events.Integration]) -> Deep | None:
    """The loaded `Deep` integration, if any: the service's Deep worker submits Deep passes to
    it. Wanting `card.requested` is not enough: a webhook may pass it on without any card
    coming back."""
    return next((i for i in integrations if isinstance(i, Deep)), None)


def answers_card_requests(integrations: list[events.Integration]) -> bool:
    return card_maker(integrations) is not None


# -- Settings → Integrations -----------------------------------------------------------------------


@router.get("/settings/integrations")
async def status(request: Request, c: Conn) -> list[s.IntegrationCard]:
    """One card per loaded integration, then one per configured section that is turned off
    (its cursor kept, so its lag shows what turning it on again would hand it). A card shows
    the event it is stuck on, or its failing setup."""
    found = {i.name: i for i in loaded(request)}
    off = [
        name
        for name, section in request.app.state.config.integrations.items()
        if name not in found and section.get("enabled", True) is False
    ]
    cursors = await outbox.cursors(c, [*found, *off])
    stats = request.app.state.search_stats
    cards = []
    for name in [*found, *off]:
        integration, cursor = found.get(name), cursors.get(name)
        failing = None
        if cursor and cursor.failing:
            event = await outbox.event(c, cursor.failing)
            failing = s.Failing(
                event_id=cursor.failing,
                type=event.type if event else None,
                paper_id=event.paper_id if event else None,
                failures=cursor.failures,
                retry_at=cursor.retry_at,
                error=cursor.last_error,
            )
        setup_failing = None
        if cursor and cursor.setup_failures:
            setup_failing = s.SetupFailing(
                failures=cursor.setup_failures,
                retry_at=cursor.setup_retry_at,
                error=cursor.setup_error,
                failed_at=cursor.setup_failed_at,
            )
        cards.append(
            s.IntegrationCard(
                name=name,
                description=integration.description if integration else None,
                enabled=integration is not None,
                lag=cursor.lag if cursor else 0,
                failing=failing,
                setup_failing=setup_failing,
                last_error=cursor.last_error if cursor else None,
                failed_at=cursor.failed_at if cursor else None,
                skipped=cursor.skipped if cursor else 0,
                answers_card_requests=bool(integration and answers_card_requests([integration])),
                search=s.SearchStats(**stats.get(name, {}))
                if isinstance(integration, SearchProvider)
                else None,
            )
        )
    return cards


def _known(request: Request, name: str) -> None:
    if name not in {i.name for i in loaded(request)}:
        raise HTTPException(status_code=404, detail="no such integration is loaded")


# What retry and skip answer besides success, for the API schema.
NOT_LOADED = {404: {"description": "No such integration is loaded"}}
RETRY_REFUSED = NOT_LOADED | {409: {"description": "Neither an event nor the setup is failing"}}
SKIP_REFUSED = NOT_LOADED | {
    409: {"description": "Nothing is failing, or another event than the one named is"}
}


@router.post("/settings/integrations/{name}/retry", status_code=204, responses=RETRY_REFUSED)
async def retry(name: str, request: Request, c: Conn) -> None:
    """Try the failing event, or the failing setup, again now, without waiting out its
    backoff."""
    _known(request, name)
    if not await outbox.retry(c, name):
        raise HTTPException(status_code=409, detail="nothing is failing")


@router.post("/settings/integrations/{name}/skip", responses=SKIP_REFUSED)
async def skip(name: str, request: Request, c: Conn, event_id: int | None = None) -> s.Failing:
    """Give up on the failing event; the integration goes on with the next. Returns the
    event skipped. `event_id` names the event the owner confirmed: when another one is
    failing by then, nothing is skipped (409)."""
    _known(request, name)
    cursor = (await outbox.cursors(c, [name])).get(name)
    skipped = await outbox.skip(c, name, event_id)
    if skipped is None or cursor is None:
        if cursor is not None and cursor.failing is not None and event_id is not None:
            raise HTTPException(status_code=409, detail="another event is failing now")
        raise HTTPException(status_code=409, detail="nothing is failing")
    event = await outbox.event(c, skipped)
    return s.Failing(
        event_id=skipped,
        type=event.type if event else None,
        paper_id=event.paper_id if event else None,
        failures=cursor.failures,
        error=cursor.last_error,
    )


# -- the changes feed ------------------------------------------------------------------------------


@router.get("/events")
async def changes(
    c: Conn, after: str = outbox.START.token, limit: int = Query(100, ge=1, le=1000)
) -> s.Changes:
    """The outbox in order, from after a position: for a client that polls instead of taking
    webhooks. Start without `after`, then pass the `next` of each answer to read on. Only
    events whose transactions are settled are listed, so reading on never misses one that
    committed late."""
    try:
        position = outbox.Position.parse(after)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    found = await outbox.after(c, position, limit)
    following = outbox.Position.of(found[-1]) if found else position
    return s.Changes(events=found, next=following.token)


# -- deep cards ------------------------------------------------------------------------------------


# What `request_card` answers besides success, for the API schema.
CARD_REFUSED = {
    404: {"description": "No such paper"},
    409: {"description": "No card provider is configured, or the paper is dismissed"},
    503: {"description": "arXiv did not answer in time; nothing was stored"},
}


@router.post("/papers/{paper_id:path}/card", status_code=202, responses=CARD_REFUSED)
async def request_card(paper_id: str, request: Request, c: Conn) -> s.CardRequested:
    """Ask for a Deep card on this paper, whatever its grade, or for a new one ("Write again")
    when it has one: the request goes through the full-text queue, first in line, and the card
    shows on the paper once it is written. Asking again while one is queued or being written
    changes nothing. A paper litwatch lacks is fetched first, as Process fetches it, with no
    transaction open, and stored in the request's own; a paper outside the collection gets its
    Process job too, queued in the same transaction."""
    if not answers_card_requests(loaded(request)):
        raise HTTPException(status_code=409, detail="no card provider is configured")
    try:
        paper_id, fetched = await misses.resolve(c, paper_id)
    except misses.NotFoundError as exc:
        raise HTTPException(status_code=404, detail="no such paper") from exc
    except NoAnswerError as exc:
        raise HTTPException(status_code=503, detail=misses.SILENT) from exc
    async with c.transaction():
        # Asked again while the request is open and its Process job under way: answered as it
        # stands, before any change, whose settle would ask a running job again
        # (`settle_process`) and run it once more for nothing. Under the paper's locks, so a
        # request in flight has either committed or not started.
        await word.lock_many(c, [paper_id])
        open_pass = await fulltext_store.open_deep(c, paper_id)
        if (
            open_pass is not None
            and open_pass["reason"] == REQUESTED
            and open_pass["state"] != "failed"
            and await jobs_store.processing(c, paper_id)
            and await papers_store.dismissed_at(c, paper_id) is None
        ):
            return s.CardRequested(pass_id=open_pass["id"])
        async with word.change(c, paper_id, cause=Cause.OWNER):
            await misses.keep(c, paper_id, fetched)
            pass_id = await deep_passes.request(c, paper_id)
            if pass_id is None:
                raise HTTPException(status_code=409, detail="the paper is dismissed")
            # Outside the collection, the paper is processed now too, as Process does: with the
            # schedule on, which runs the jobs worker, a taxonomy to sort it into, and no step of
            # the paper waiting on the owner.
            processing = (
                request.app.state.config.schedule.enabled
                and not await word_store.in_collection(c, paper_id)
                and await taxonomy_store.load(c) is not None
                and not await failures.waits_on_owner(c, [paper_id], FAILED_RUNS)
            )
            if processing:
                await jobs_store.process(c, paper_id)
    request.app.state.deep_wake.set()
    if processing:
        request.app.state.jobs_wake.set()
    return s.CardRequested(pass_id=pass_id)


async def card(
    c: Any, paper_id: str, integrations: list[events.Integration]
) -> tuple[s.DeepCard | None, s.CardPass | None, bool]:
    """The paper page's Deep card rung: the stored card, shown whatever is loaded; with a card
    provider among the loaded `integrations`, its open pass and whether "Deep read" (or "Write
    again") applies."""
    stored = await integrations_store.card(c, paper_id)
    found = None
    if stored and stored["card"]:
        among = [
            a
            for a in stored["card"].get("among_collection") or []
            if a.get("arxiv_id") and a.get("relation") in s.RELATIONS
        ]
        marks = await papers_store.marks(c, [a["arxiv_id"] for a in among])
        found = s.DeepCard(
            **{k: stored[k] for k in ("card", "model", "written_at", "revision", "arxiv_version")},
            source=stored["source"],
            among=[_among(a, marks.get(a["arxiv_id"])) for a in among],
        )
    if not answers_card_requests(integrations):
        return found, None, False
    # The owner may ask unless they already have: their request takes an automatic pass over,
    # a failed one included.
    open_pass = await fulltext_store.open_deep(c, paper_id)
    requestable = open_pass is None or open_pass["reason"] != REQUESTED
    return found, s.CardPass(**open_pass) if open_pass else None, requestable


def _among(entry: dict[str, Any], mark: dict[str, Any] | None) -> s.AmongRow:
    """One row of a card's "among the collection" section, with the paper as the collection
    has it now (its title and rarity), or as the card named it."""
    return s.AmongRow(
        id=entry["arxiv_id"],
        title=mark["title"] if mark else entry.get("title") or entry["arxiv_id"],
        relation=entry["relation"],
        note=entry.get("note"),
        rarity=mark["rarity"] if mark else None,
        in_collection=bool(mark and mark["in_collection"]),
    )


# -- search providers ------------------------------------------------------------------------------


def search_pool() -> ThreadPoolExecutor:
    return ThreadPoolExecutor(max_workers=SEARCH_WORKERS, thread_name_prefix="search")


async def _ask(
    pool: ThreadPoolExecutor,
    provider: SearchProvider,
    query: str,
    filters: SearchFilters,
    timeout: float,
) -> list[SearchHit]:
    running = asyncio.get_running_loop().run_in_executor(pool, provider.search, query, filters)
    return await asyncio.wait_for(running, timeout)


@router.get("/search/scope")
async def search_scope(request: Request) -> s.SearchScope:
    """The search box's scope beyond the collection (its placeholder names the archive's size)."""
    archive = next((p for p in providers(loaded(request)) if isinstance(p, Archive)), None)
    if archive is None:
        return s.SearchScope()
    try:
        count = await asyncio.wait_for(archive.archive.indexed(), request.app.state.search_timeout)
    except (ArchiveError, TimeoutError):
        count = None
    return s.SearchScope(archive_papers=count)


async def search_outside(
    c: Any,
    query: str,
    filters: SearchFilters,
    integrations: list[events.Integration],
    pool: ThreadPoolExecutor,
    timeout: float,
    stats: dict[str, dict[str, Any]],
) -> tuple[list[s.OutsideRow], list[str]]:
    """Ask every search provider among `integrations` at once on `pool`, each within
    `timeout`, and count each answer in `stats` (Settings shows them). Returns the hits the
    collection lacks, merged by arXiv id (the first provider to name a paper keeps it), and
    the names of the providers left out because they failed or were too slow.

    The band keeps each provider's order, the providers in turn (archive.md, Search: the
    archive ranks); asked for the newest (`filters.sort`), it is newest first across them."""
    asked = providers(integrations)
    if not asked:
        return [], []
    answers = await asyncio.gather(
        *(_ask(pool, p, query, filters, timeout) for p in asked),
        return_exceptions=True,
    )
    hits: dict[str, tuple[str, SearchHit]] = {}
    left_out = []
    for provider, answer in zip(asked, answers, strict=True):
        record = stats.setdefault(provider.name, {"queries": 0, "left_out": 0})
        record["queries"] += 1
        if isinstance(answer, BaseException):
            left_out.append(provider.name)
            record["left_out"] += 1
            record["last_error"] = (
                f"no answer within {timeout:g}s"
                if isinstance(answer, TimeoutError)
                else f"{type(answer).__name__}: {answer}"[:300]
            )
            record["last_error_at"] = datetime.now(UTC)
            continue
        for hit in answer:
            hits.setdefault(hit.id, (provider.name, hit))
    held = await c.execute("SELECT id FROM papers WHERE id = ANY(%s)", (list(hits),))
    for row in await held.fetchall():
        hits.pop(row["id"])
    rows = [
        s.OutsideRow(
            id=hit.id,
            title=hit.title,
            authors=hit.authors,
            published=_aware(hit.published),
            source=name,
        )
        for name, hit in hits.values()
    ]
    # `hits` holds each provider's hits in its order, the providers in turn.
    if filters.sort == "newest":
        rows.sort(key=lambda r: r.published or datetime.min.replace(tzinfo=UTC), reverse=True)
    return rows, left_out


def _aware(at: datetime | None) -> datetime | None:
    return at.replace(tzinfo=UTC) if at and at.tzinfo is None else at
