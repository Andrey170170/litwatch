"""The reading side's routes: the status line, papers, topics, and the owner's word."""

from __future__ import annotations

from typing import Annotated, Any, Literal

import psycopg
from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import PlainTextResponse

from litwatch.api import integrations as integrations_api
from litwatch.api import schemas as s
from litwatch.api.deps import Conn, set_token_cookie, token_ok
from litwatch.core import (
    Hand,
    Label,
    LabelOrigin,
    Publication,
    Reaction,
    ReactionReason,
    SearchFilters,
    TopicKind,
)
from litwatch.feedback import misses, word
from litwatch.feedback.word import Settled
from litwatch.jobs import publications as lookups
from litwatch.publications import SourceError, dblp
from litwatch.publications.bibtex import Cited, entries
from litwatch.store import papers as papers_store
from litwatch.store import publications as publications_store
from litwatch.store import reading
from litwatch.store import topics as topics_store

router = APIRouter(prefix="/api")


def _config(request: Request) -> Any:
    return request.app.state.config


async def _topics(c: psycopg.AsyncConnection) -> dict[str, Any]:
    return {t.slug: t for t in await topics_store.all(c, None)}


# -- the status line -------------------------------------------------------------------------------


@router.get("/status")
async def status(c: Conn) -> s.Status:
    return s.Status(**await reading.status(c))


# -- papers ----------------------------------------------------------------------------------------


@router.get("/papers")
async def papers(
    request: Request,
    c: Conn,
    q: str | None = None,
    topic: str | None = None,
    adjacent: bool = False,
    saved: bool | None = None,
    year: int | None = None,
    sort: str | None = None,
    source: Literal["collection", "outside"] | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> s.Papers:
    """The table. A search also asks the search providers, on its first page, for papers the
    collection lacks: the `outside` band. Topic and saved belong to the collection, so with
    either filter set there is no outside band; `source` keeps one band only."""
    rows, total = [], 0
    if source != "outside":
        found, total = await reading.papers(
            c,
            q=q,
            topic=topic,
            adjacent=adjacent,
            saved=saved,
            year=year,
            sort=None if sort == "best" else sort,
            limit=limit,
            offset=offset,
        )
        rows = [s.Row(**r) for r in found]
    outside, left_out = [], []
    if q and offset == 0 and source != "collection" and not topic and saved is not True:
        filters = SearchFilters(year=year, limit=limit)
        outside, left_out = await integrations_api.search_outside(request, c, q, filters, sort)
    return s.Papers(rows=rows, total=total, outside=outside, left_out=left_out)


# Paper fields built below rather than copied from the row.
_SET_HERE = {"topics", "reaction", "reaction_reason", "history", "chosen"}


@router.get("/papers/{paper_id:path}")
async def paper(paper_id: str, request: Request, c: Conn) -> s.Paper:
    found = await reading.paper(c, paper_id)
    if found is None:
        raise HTTPException(status_code=404, detail="no such paper")
    topics = [
        s.PaperTopic(
            **{k: t[k] for k in ("slug", "name", "kind", "colour", "grade", "missed")},
            label=s.LabelState(**t["label"]) if t["label"] else None,
            reaction=(t["reaction"] or {}).get("value"),
            reaction_reason=(t["reaction"] or {}).get("reason"),
        )
        for t in found["topics"]
        if t["grade"] or t["reaction"] or t["label"]
    ]
    published = await publications_store.get(c, paper_id)
    card, card_requestable = await integrations_api.card(request, c, paper_id)
    return s.Paper(
        **{k: found[k] for k in s.Paper.model_fields if k in found and k not in _SET_HERE},
        arxiv_url=f"https://arxiv.org/abs/{paper_id}",
        pdf_url=f"https://arxiv.org/pdf/{paper_id}",
        topics=topics,
        reaction=(found["reaction"] or {}).get("value"),
        reaction_reason=(found["reaction"] or {}).get("reason"),
        history=[s.HistoryItem(**h) for h in found["history"]],
        versions=list(found["version_history"] or []),
        chosen=[s.Chosen(**v) for v in found["verdicts"]],
        publication=published["record"] if published else None,
        publication_by_owner=bool(published and published["by_owner"]),
        card=card,
        card_requestable=card_requestable,
    )


@router.put("/papers/{paper_id:path}/publication")
async def set_publication(
    paper_id: str, body: s.PublicationIn, request: Request, c: Conn
) -> Publication | None:
    """The owner names the published version by DBLP key or DOI, or says there is none.
    A lookup never overwrites it. A DOI DBLP does not know is kept with the arXiv entry."""
    if await papers_store.get(c, paper_id) is None:
        raise HTTPException(status_code=404, detail="no such paper")
    source = request.app.state.publications
    record = None
    try:
        if body.dblp_key:
            record = await source.record(body.dblp_key)
            if record is None:
                raise HTTPException(status_code=404, detail="DBLP has no such record")
        elif body.doi:
            record = await source.by_doi(body.doi) or Publication(doi=dblp.clean_doi(body.doi))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except SourceError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    await lookups.store_record(c, paper_id, record, by_owner=True)
    return record


@router.delete("/papers/{paper_id:path}/publication", status_code=204)
async def forget_publication(paper_id: str, c: Conn) -> None:
    """Drop the stored version, the owner's choice included; the paper is looked up again."""
    await publications_store.forget(c, paper_id)


def _cited(row: Any) -> Cited:
    published = row["published"]
    return Cited(
        id=row["id"],
        title=row["title"],
        authors=list(row["authors"] or []),
        year=published.year if published else None,
        primary_category=row.get("primary_category"),
    )


async def _bibtex(
    c: psycopg.AsyncConnection, ids: list[str], outside: list[Cited] | None = None
) -> str:
    """Entries from the stored published records, arXiv ones for the rest, then `outside`
    hits as they are, all keyed in one namespace so no two entries share a key. Never
    waits on a lookup (decision 25)."""
    rows = await (
        await c.execute(
            "SELECT id, title, authors, published, primary_category FROM papers "
            "WHERE id = ANY(%s) ORDER BY array_position(%s, id)",
            (ids, ids),
        )
    ).fetchall()
    held = [_cited(r) for r in rows]
    return entries([*held, *(outside or [])], await publications_store.found(c, ids))


@router.post("/bibtex", response_class=PlainTextResponse)
async def bibtex_selection(body: s.BibtexIn, request: Request, c: Conn) -> str:
    """BibTeX for chosen papers. One paper is looked up first, so a copied entry cites the
    published version as soon as DBLP has it; several use what is stored. `cited` are
    papers a search provider found outside the collection: they get arXiv entries from
    what the hit carries, with no fetch, no lookup and nothing written."""
    if (
        len(body.ids) == 1
        and not body.cited
        and body.filter is None
        and await papers_store.get(c, body.ids[0])
    ):
        await lookups.for_copy(c, request.app.state.publications, body.ids[0])
    ids = list(body.ids)
    source = body.filter.source if body.filter else None
    if body.filter is not None and source != "outside":
        found = body.filter.model_dump(exclude={"source"})
        rows, _ = await reading.papers(c, **found, limit=None)
        ids += [r["id"] for r in rows if r["id"] not in ids]
    outside = [
        Cited(id=h.id, title=h.title, authors=list(h.authors), year=h.year)
        for h in body.cited
        if h.id not in ids and source != "collection"
    ]
    return await _bibtex(c, ids, outside)


@router.get("/bibtex", response_class=PlainTextResponse)
async def bibtex_filter(
    c: Conn,
    q: str | None = None,
    topic: str | None = None,
    adjacent: bool = False,
    saved: bool | None = None,
    year: int | None = None,
) -> str:
    """BibTeX for every paper the table's filter matches."""
    rows, _ = await reading.papers(
        c, q=q, topic=topic, adjacent=adjacent, saved=saved, year=year, limit=None
    )
    return await _bibtex(c, [r["id"] for r in rows])


@router.post("/lookup")
async def lookup(body: s.LookupIn, c: Conn) -> s.Lookup:
    """Find a paper by arXiv id or link, fetching it when the collection lacks it."""
    try:
        found, fetched = await misses.lookup(c, body.q)
    except misses.NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    rows, _ = await reading.papers(c, limit=1, ids=[found.id])
    return s.Lookup(row=s.Row(**rows[0]), fetched=fetched)


# -- topics ----------------------------------------------------------------------------------------


@router.get("/topics")
async def topics(c: Conn) -> list[s.TopicCard]:
    return [s.TopicCard(**t) for t in await reading.topic_cards(c)]


@router.post("/topics/{slug}/visit", status_code=204)
async def visit(slug: str, c: Conn) -> None:
    await reading.visit(c, slug)


# -- the owner's word ------------------------------------------------------------------------------


def _ack(said: str, settled: Settled) -> s.Ack:
    if settled.was_saved and not settled.saved:
        said += "; it leaves the library"
    elif settled.saved and not settled.was_saved:
        said += "; it joins the library"
    return s.Ack(
        message=said,
        saved=settled.saved,
        grades={topic: after for topic, (_, after) in settled.grades.items()},
    )


async def _known(c: psycopg.AsyncConnection, paper_id: str) -> None:
    if await papers_store.get(c, paper_id) is None:
        raise HTTPException(status_code=404, detail="no such paper")


@router.put("/reactions/{paper_id:path}")
async def react(paper_id: str, body: s.ReactionIn, c: Conn) -> s.Ack:
    await _known(c, paper_id)
    topics = await _topics(c)
    topic = topics.get(body.topic) if body.topic else None
    if body.topic and topic is None:
        raise HTTPException(status_code=404, detail="no such topic")
    try:
        reaction = Reaction(
            paper_id=paper_id, topic=body.topic, value=body.value, reason=body.reason
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    _, settled = await word.react(c, reaction)
    watch = topic is not None and topic.kind == TopicKind.WATCH
    if watch and body.reason == ReactionReason.UNRELATED:
        said = f"Marked not relevant for {topic.name}"
    elif watch and body.reason == ReactionReason.GREAT_FIND:
        said = f"Marked relevant for {topic.name}"
    else:
        said = f"Remembered for {topic.name}" if topic else "Remembered"
    return _ack(said, settled)


@router.delete("/reactions/{paper_id:path}")
async def unreact(paper_id: str, c: Conn, topic: str | None = None) -> s.Ack:
    return _ack("Taken back", await word.unreact(c, paper_id, topic))


@router.put("/library/{paper_id:path}")
async def save(paper_id: str, body: s.SaveIn, c: Conn) -> s.Ack:
    await _known(c, paper_id)
    settled = await word.toggle(c, paper_id, Hand.SAVE if body.saved else Hand.UNSAVE)
    return s.Ack(
        message="Saved" if settled.saved else "Removed from the library",
        saved=settled.saved,
        grades={t: after for t, (_, after) in settled.grades.items()},
    )


@router.put("/word/{topic}/{paper_id:path}")
async def label(topic: str, paper_id: str, body: s.LabelIn, c: Conn) -> s.Ack:
    """The label row on a paper page: the owner's own word, origin `reader`."""
    known = (await _topics(c)).get(topic)
    if known is None or known.kind != TopicKind.WATCH:
        raise HTTPException(status_code=404, detail="not a Watch topic")
    await _known(c, paper_id)
    _, settled = await word.label(
        c,
        Label(
            paper_id=paper_id,
            topic=topic,
            value=body.value,
            grade=body.grade if body.value == "relevant" else None,
            origin=LabelOrigin.READER,
        ),
    )
    return _ack(f"Noted for {known.name}", settled)


@router.delete("/word/{topic}/{paper_id:path}")
async def unlabel(topic: str, paper_id: str, c: Conn) -> s.Ack:
    return _ack("Taken back", await word.unlabel(c, paper_id, topic))


@router.post("/misses")
async def report_miss(body: s.MissIn, c: Conn) -> s.Ack:
    """File a miss for a Watch topic."""
    known = (await _topics(c)).get(body.topic)
    if known is None or known.kind != TopicKind.WATCH:
        raise HTTPException(status_code=404, detail="not a Watch topic")
    await _known(c, body.paper_id)
    _, settled, new = await misses.report(c, body.paper_id, body.topic)
    if not new:
        return _ack(f"Already reported as missed for {known.name}", settled)
    return _ack(f"Reported as missed for {known.name}", settled)


# -- the session -----------------------------------------------------------------------------------

session = APIRouter(prefix="/api")


@session.post("/session", status_code=204)
async def sign_in(body: s.SessionIn, request: Request, response: Response) -> None:
    """Exchange the token for the cookie the web app then rides on."""
    if not token_ok(request, body.token):
        raise HTTPException(status_code=401, detail="wrong token")
    set_token_cookie(request, response, body.token)
