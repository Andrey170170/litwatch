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

from fastapi import APIRouter, HTTPException, Query, Request

from litwatch import events
from litwatch.api import schemas as s
from litwatch.api.deps import Conn
from litwatch.core import SearchFilters, SearchHit, SearchProvider
from litwatch.fulltext import deep as deep_passes
from litwatch.integrations.deep import Deep
from litwatch.store import fulltext as fulltext_store
from litwatch.store import integrations as integrations_store
from litwatch.store import outbox
from litwatch.store import papers as papers_store

router = APIRouter(prefix="/api")

# Searches run on a pool of their own: a provider whose thread hangs past its timeout (the
# thread cannot be stopped) ties up a worker here, never the default pool `to_thread` uses.
SEARCH_WORKERS = 4


def loaded(request: Request) -> list[events.Integration]:
    return request.app.state.integrations


def providers(request: Request) -> list[SearchProvider]:
    return [i for i in loaded(request) if isinstance(i, SearchProvider)]


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
    (its cursor kept, so its lag shows what turning it on again would hand it)."""
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
        cards.append(
            s.IntegrationCard(
                name=name,
                description=integration.description if integration else None,
                enabled=integration is not None,
                lag=cursor.lag if cursor else 0,
                failing=failing,
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


@router.post("/settings/integrations/{name}/retry", status_code=204)
async def retry(name: str, request: Request, c: Conn) -> None:
    """Try the failing event again now, without waiting out its backoff."""
    _known(request, name)
    if not await outbox.retry(c, name):
        raise HTTPException(status_code=409, detail="nothing is failing")


@router.post("/settings/integrations/{name}/skip")
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
}


@router.post("/papers/{paper_id:path}/card", status_code=202, responses=CARD_REFUSED)
async def request_card(paper_id: str, request: Request, c: Conn) -> s.CardRequested:
    """Ask for a Deep card on this paper, whatever its grade, or for a new one ("Write again")
    when it has one: the request goes through the full-text queue, first in line, and the card
    shows on the paper once it is written. Asking again while one is queued or being written
    changes nothing."""
    if await papers_store.get(c, paper_id) is None:
        raise HTTPException(status_code=404, detail="no such paper")
    if not answers_card_requests(loaded(request)):
        raise HTTPException(status_code=409, detail="no card provider is configured")
    pass_id = await deep_passes.request(c, paper_id)
    if pass_id is None:
        raise HTTPException(status_code=409, detail="the paper is dismissed")
    request.app.state.deep_wake.set()
    return s.CardRequested(pass_id=pass_id)


async def card(
    request: Request, c: Any, paper_id: str
) -> tuple[s.DeepCard | None, s.CardPass | None, bool]:
    """The paper page's Deep card rung: the stored card, shown whatever is loaded; with a card
    provider loaded, its open pass and whether "Deep read" (or "Write again") applies."""
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
    if not answers_card_requests(loaded(request)):
        return found, None, False
    open_pass = await fulltext_store.open_deep(c, paper_id)
    return found, s.CardPass(**open_pass) if open_pass else None, open_pass is None


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


async def search_outside(
    request: Request, c: Any, query: str, filters: SearchFilters, sort: str | None
) -> tuple[list[s.OutsideRow], list[str]]:
    """Ask every search provider at once, each with a short timeout. Returns the hits the
    collection lacks, merged by arXiv id (the first provider to name a paper keeps it), and
    the names of the providers left out because they failed or were too slow.

    The band sorts by published date, newest first; with `sort=best`, by the providers'
    scores when every hit carries one."""
    asked = providers(request)
    if not asked:
        return [], []
    timeout = request.app.state.search_timeout
    answers = await asyncio.gather(
        *(_ask(request.app.state.search_pool, p, query, filters, timeout) for p in asked),
        return_exceptions=True,
    )
    stats = request.app.state.search_stats
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
            score=hit.score,
        )
        for name, hit in hits.values()
    ]
    if sort == "best" and rows and all(r.score is not None for r in rows):
        # Scores mean something only within one provider.
        order = {p.name: i for i, p in enumerate(asked)}
        rows.sort(key=lambda r: (order[r.source], -r.score))
    else:
        rows.sort(key=lambda r: r.published or datetime.min.replace(tzinfo=UTC), reverse=True)
    return rows, left_out


def _aware(at: datetime | None) -> datetime | None:
    return at.replace(tzinfo=UTC) if at and at.tzinfo is None else at
