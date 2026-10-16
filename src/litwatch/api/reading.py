"""The reading side's routes: the status line, Papers, the Topics tab and a topic's page, a
paper's page, and the owner's word (docs/specs/v1/DESIGN.md, Shell, Pages and Interactions)."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from typing import Annotated, Any

import psycopg
from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import PlainTextResponse, RedirectResponse

from litwatch import store
from litwatch.api import integrations as integrations_api
from litwatch.api import schemas as s
from litwatch.api.deps import Conn, budget_lines, set_token_cookie, token_ok
from litwatch.archive import ArchiveError
from litwatch.core import (
    Depth,
    Effective,
    Hand,
    Label,
    LabelOrigin,
    PageStanding,
    Publication,
    Reaction,
    ReactionReason,
    SearchFilters,
    Taxonomy,
    Topic,
    Weight,
    badge_order,
    owners_word,
    page_standing,
)
from litwatch.core.classification import Membership, Source, standing
from litwatch.core.digests import DAILY, DigestPeriod
from litwatch.core.rules import FAILED_RUNS, REACTION_WORDS
from litwatch.feedback import misses, word
from litwatch.feedback.word import Settled
from litwatch.intake.search import NoAnswerError
from litwatch.jobs import publications as lookups
from litwatch.publications import SourceError, dblp
from litwatch.publications.bibtex import Cited, entries
from litwatch.store import digests as digests_store
from litwatch.store import papers as papers_store
from litwatch.store import publications as publications_store
from litwatch.store import reading
from litwatch.store import taxonomy as taxonomy_store
from litwatch.store import word as word_store

router = APIRouter(prefix="/api")

NOT_FOUND = {404: {"description": "No such paper or topic"}}
# An owner action that fetches a paper from arXiv, when arXiv does not answer in time.
ARXIV_SILENT = {503: {"description": "arXiv did not answer in time; nothing was stored"}}
ARCHIVE_DOWN = "The archive did not answer"
# Past the archive's wait for a PDF, the time left to read one that became ready at its end.
PDF_READ_SLACK = 10.0


def _config(request: Request) -> Any:
    return request.app.state.config


async def _shown(c: psycopg.AsyncConnection, slug: str) -> tuple[str, Topic] | None:
    """The shown topic on class `slug`, with its class's name."""
    tree = await taxonomy_store.load(c)
    topic = next((t for t in tree.shown() if t.slug == slug), None) if tree else None
    return (tree.get(slug).name, topic) if topic else None


async def _topic(c: psycopg.AsyncConnection, slug: str) -> tuple[str, Topic]:
    """The shown topic on class `slug`, with its class's name; 404 for any other."""
    found = await _shown(c, slug)
    if found is None:
        raise HTTPException(status_code=404, detail="no such topic")
    return found


async def _known(c: psycopg.AsyncConnection, paper_id: str) -> None:
    if await papers_store.get(c, paper_id) is None:
        raise HTTPException(status_code=404, detail="no such paper")


def _looks(tree: Taxonomy | None) -> dict[str, s.Badge]:
    """Every shown topic's badge, in the fixed order."""
    if tree is None:
        return {}
    return {
        t.slug: s.Badge(
            slug=t.slug,
            name=tree.get(t.slug).name,
            depth=t.depth,
            monogram=t.monogram,
            colour=t.colour,
        )
        for t in tree.shown()
    }


def _standings(
    tree: Taxonomy | None,
    members: Iterable[str],
    grades: list[dict[str, Any]],
    weight: Weight | None,
) -> dict[str, PageStanding]:
    """The shown topics on whose page the paper is, as `store.reading.SCOPE` finds them, each as
    its page shows it (`page_standing`), in the fixed order: the topics at or above a class it
    stands in, and those graded it or with a followed descendant that did."""
    if tree is None:
        return {}
    shown = [t.slug for t in tree.shown()]
    effective = {
        g["topic"]: Effective(fit=g["fit"], grade=g["grade"], rarity=g["rarity"])
        for g in grades
        if g["topic"] in shown
    }
    classes = {c.slug for c in tree.classes}
    on = {a for c in [*members, *effective] if c in classes for a in [c, *tree.ancestors(c)]}
    found = {}
    for slug in shown:
        if slug in on:
            under = [t.slug for t in tree.under(slug) if t.slug != slug and t.slug in effective]
            below = [(t, effective[t]) for t in under]
            found[slug] = page_standing(effective.get(slug), below, weight)
    return found


def _badges(looks: dict[str, s.Badge], standings: dict[str, PageStanding]) -> list[s.GradedBadge]:
    """The paper's badges: its followed topics, the one giving the highest level first (ties in
    the fixed order); `grade` is null where neither the topic nor a descendant graded it."""
    levels = [(t, looks[t].depth, st.rarity) for t, st in standings.items()]
    return [
        s.GradedBadge(**looks[t].model_dump(), grade=standings[t].grade, rarity=standings[t].rarity)
        for t in badge_order(levels)
    ]


def _facets(r: dict[str, Any]) -> s.Facets | None:
    if r["contribution"] is None:
        return None
    return s.Facets(
        contribution=r["contribution"],
        releases_code=r["releases_code"],
        releases_weights=r["releases_weights"],
    )


def _row[R: s.Row](
    tree: Taxonomy | None, looks: dict[str, s.Badge], r: dict[str, Any], model: type[R]
) -> R:
    extra = {k: r[k] for k in ("grade", "via") if k in r}
    weight = Weight(r["weight"]) if r["weight"] else None
    return model(
        **{k: r[k] for k in ("id", "title", "authors", "published", "gist", "weight", "rarity")},
        facets=_facets(r),
        badges=_badges(looks, _standings(tree, r["members"], r["grades"], weight)),
        saved=r["saved"],
        state=r["state"],
        **extra,
    )


def _query(params: s.ListQuery) -> reading.Query:
    found = params.model_dump(include=set(s.ListQuery.model_fields))
    if isinstance(params, s.PaperQuery):
        found |= {"topics": params.topic, "depth": params.depth}
    return reading.Query(**found)


def _topics_known(tree: Taxonomy | None, slugs: list[str]) -> None:
    shown = {t.slug for t in tree.shown()} if tree else set()
    if unknown := [x for x in slugs if x not in shown]:
        raise HTTPException(status_code=404, detail=f"no such topic: {', '.join(unknown)}")


# -- the status line -------------------------------------------------------------------------------


async def run_status(request: Request, c: psycopg.AsyncConnection) -> s.RunStatus:
    spend = await budget_lines(c, _config(request).budget)
    over = [line.provider for line in spend if line.over]
    return s.RunStatus(**await reading.status(c), over_budget=over)


@router.get("/status")
async def status(request: Request, c: Conn) -> s.RunStatus:
    return await run_status(request, c)


# -- papers ----------------------------------------------------------------------------------------


def _shows_outside(query: s.PaperQuery) -> bool:
    """Whether a Papers filter keeps the outside band: a paper outside the collection has no
    topic, level, Weight or analysis, and is not saved, so a filter on any of those leaves
    it out."""
    ours = (query.topic, query.depth, query.rarity, query.grade, query.weight)
    return (
        query.source != "collection"
        and not any(ours)
        and query.code is None
        and query.saved is not True
    )


@router.get("/papers", responses=NOT_FOUND)
async def papers(request: Request, c: Conn, params: Annotated[s.PapersIn, Query()]) -> s.Papers:
    """Papers: the whole collection, rarest first. A search also asks the search providers, on
    its first page, for papers the collection lacks: the `outside` band. A filter only the
    collection has (topic, depth, a level, saved, code, Weight) leaves the band out; `source`
    keeps one band only."""
    tree = await taxonomy_store.load(c)
    _topics_known(tree, params.topic)
    looks = _looks(tree)
    answer = s.Papers(rows=[], total=0, counts=s.RarityCounts())
    if params.source != "outside":
        listed = await reading.papers(
            c, tree, _query(params), limit=params.limit, offset=params.offset
        )
        answer.rows = [_row(tree, looks, r, s.Row) for r in listed.rows]
        answer.total, answer.counts = listed.total, s.RarityCounts(**listed.rarities)
    if params.q and params.offset == 0 and _shows_outside(params):
        # The providers rank by relevance whatever the collection's sort; a date sort asks them
        # for their newest.
        filters = SearchFilters(
            since=params.since,
            until=params.until,
            limit=params.limit,
            sort="newest" if params.sort == "newest" else "relevance",
        )
        answer.outside, answer.left_out = await integrations_api.search_outside(
            request, c, params.q, filters
        )
    return answer


# Declared before the paper's page, whose path would take "/pdf" as part of the id.
@router.get(
    "/papers/{paper_id:path}/pdf",
    response_class=Response,
    responses={
        200: {"content": {"application/pdf": {}}, "description": "The PDF, from the archive"},
        307: {"description": "arXiv's PDF: no archive, or none it could get in time"},
    },
)
async def pdf(paper_id: str, request: Request) -> Response:
    """The paper's PDF as the owner opens it: from the archive, which is asked for it urgently
    (the storage policy fetches PDFs it does not keep when opened, archive.md) and waited on
    for its `wait_seconds`. A paper litwatch has not fetched is known by the archive's record
    of it. arXiv's PDF without an archive, for a paper neither holds, or when the archive
    cannot get it in time. The database is read before the wait, which holds no connection."""
    archive = request.app.state.archive
    async with await store.connect(request.app.state.dsn) as c:
        held = await papers_store.get(c, paper_id)
    body = None
    if archive is not None:
        try:
            # One bound over every archive call: the record, the request, the wait, the file.
            async with asyncio.timeout(archive.wait_seconds + PDF_READ_SLACK):
                known = held if held is not None else await archive.paper(paper_id)
                body = await archive.pdf(known) if known is not None else None
        except (ArchiveError, TimeoutError):  # unreachable, slow, no known version, a broken file
            body = None
        if body is not None:
            name = paper_id.replace("/", "_")
            return Response(
                body,
                media_type="application/pdf",
                headers={"Content-Disposition": f'inline; filename="{name}.pdf"'},
            )
    return RedirectResponse(f"https://arxiv.org/pdf/{paper_id}", status_code=307)


@router.get(
    "/papers/{paper_id:path}",
    responses={
        **NOT_FOUND,
        503: {"description": "The archive, asked for a paper litwatch lacks, did not answer"},
    },
)
async def paper(paper_id: str, request: Request, c: Conn) -> s.Paper:
    """The paper's page; for a paper litwatch has not fetched, the archive's record of it."""
    found = await reading.paper(c, paper_id)
    if found is None:
        return await _unfetched(request, paper_id)
    tree = await taxonomy_store.load(c)
    looks = _looks(tree)
    weight = Weight(found["weight"]) if found["weight"] else None
    standings = _standings(tree, _members(tree, found["memberships"]), found["grades"], weight)
    rail = _rail(tree, looks, found, standings)
    states = (
        [
            s.OpenState(kind=kind, revision=found["revision"], error=f["last_error"])
            for f in found["failures"]
            for step, kind in (
                ("classification", "awaiting_classification"),
                ("analysis", "analysis_stopped"),
            )
            if f["step"] == step and f["failed_runs"] >= FAILED_RUNS
        ]
        + [
            s.OpenState(
                kind=t.state,
                revision=found["revision"],
                topic=t.topic.slug,
                error=next(
                    (f["last_error"] for f in found["failures"] if f["topic"] == t.topic.slug), None
                )
                if t.state == "ungraded"
                else None,
            )
            for t in rail
            if t.state in ("split", "escalated", "ungraded")
        ]
        + [
            s.OpenState(
                kind="awaiting_text"
                if f["state"] != "failed"
                else "card_failed"
                if f["kind"] == "deep"
                else "fulltext_failed",
                revision=found["revision"],
                error=f["last_error"],
                pass_id=f["id"],
            )
            for f in found["stuck_passes"]
        ]
    )
    published = await publications_store.get(c, paper_id)
    card, card_pass, card_requestable = await integrations_api.card(request, c, paper_id)
    unattached = next((r for r in found["reactions"] if r["topic"] is None), None)
    return s.Paper(
        **{
            k: found[k]
            for k in ("id", "title", "authors", "abstract", "published", "updated", "version")
        },
        versions=[
            s.Version(version=v["version"], date=v.get("date"))
            for v in found["version_history"] or []
            if v.get("version")
        ],
        revision=found["revision"],
        arxiv_url=f"https://arxiv.org/abs/{paper_id}",
        pdf_url=f"/api/papers/{paper_id}/pdf",
        source_url=f"https://arxiv.org/e-print/{paper_id}",
        in_collection=found["in_collection"],
        rarity=found["rarity"],
        badges=_badges(looks, standings),
        gist=s.Gist(text=found["gist"], source=found["gist_source"]) if found["gist"] else None,
        brief=s.Brief(**found["brief"]) if found["brief"] else None,
        text_pass=s.TextPass(**found["text_pass"]) if found["text_pass"] else None,
        weight=s.WeightState(
            weight=weight, reason=found["weight_reason"], source=found["weight_source"]
        )
        if weight
        else None,
        facets=_facets(found),
        saved=found["saved"],
        hand=found["hand"],
        topics=rail,
        reaction=s.ReactionState(**unattached) if unattached else None,
        classification=_classification(tree, found),
        states=states,
        history=await _history(c, looks, found["history"]),
        publication=published["record"] if published else None,
        publication_by_owner=bool(published and published["by_owner"]),
        card=card,
        card_pass=card_pass,
        card_requestable=card_requestable,
    )


async def _unfetched(request: Request, paper_id: str) -> s.Paper:
    """An archive paper litwatch has not fetched: its metadata, Process, Deep read (which
    fetches it first) and Report as a miss. 404 without an archive or when the archive lacks
    it; 503 when the archive cannot answer, which says nothing of the paper."""
    archive = request.app.state.archive
    try:
        found = await archive.paper(paper_id) if archive is not None else None
    except ArchiveError as exc:
        raise HTTPException(status_code=503, detail=ARCHIVE_DOWN) from exc
    if found is None:
        raise HTTPException(status_code=404, detail="no such paper")
    return s.Paper(
        **found.model_dump(include={"id", "title", "authors", "abstract", "published", "updated"}),
        version=found.version,
        versions=[
            s.Version(version=v["version"], date=v.get("date"))
            for v in found.version_history
            if v.get("version")
        ],
        arxiv_url=f"https://arxiv.org/abs/{found.id}",
        pdf_url=f"/api/papers/{found.id}/pdf",
        source_url=f"https://arxiv.org/e-print/{found.id}",
        in_collection=False,
        stored=False,
        topics=[],
        card_requestable=integrations_api.answers_card_requests(integrations_api.loaded(request)),
    )


# The sources that put a paper in a class on a page; the others are for grading only.
SHOWN = (Source.CLASSIFIER, Source.OWNER, Source.MISS)


def _members(tree: Taxonomy | None, rows: list[dict[str, Any]]) -> set[str]:
    """The current tree's classes a standing membership puts the paper in."""
    found = [Membership(slug=m["class"], source=m["source"], member=m["member"]) for m in rows]
    classes = {c.slug for c in tree.classes} if tree else set()
    return {m.slug for m in standing(found) if m.source in SHOWN and m.slug in classes}


def _rail(
    tree: Taxonomy | None,
    looks: dict[str, s.Badge],
    found: dict[str, Any],
    standings: dict[str, PageStanding],
) -> list[s.PaperTopic]:
    """The topics of the paper's badges (`_standings`), and the shown topics it has a label,
    thumb, miss or failed grading on, in the fixed order."""
    if tree is None:
        return []
    fits = {g["topic"]: g["fit"] for g in found["grades"]}
    verdicts = {v["topic"]: v for v in found["verdicts"]}
    labels = {x["topic"]: x for x in found["labels"]}
    reactions = {r["topic"]: r for r in found["reactions"] if r["topic"]}
    missed = {m["topic"] for m in found["misses"]}
    failed = {f["topic"]: f for f in found["failures"] if f["step"] == "grading"}
    involved = set(standings) | set(failed) | set(labels) | set(reactions) | missed
    rail = []
    for slug, look in looks.items():
        if slug not in involved:
            continue
        topic = tree.topic(slug)
        shows = standings.get(slug)
        v, label, thumb = verdicts.get(slug), labels.get(slug), reactions.get(slug)
        said = owners_word(
            Label(paper_id=found["id"], **label) if label else None,
            Reaction(paper_id=found["id"], **thumb) if thumb else None,
            v["fit"] if v else None,
            focus=topic.depth == Depth.FOCUS,
        )
        state = None
        if slug in failed and failed[slug]["failed_runs"] >= FAILED_RUNS:
            state = "ungraded"
        elif v and (v["split"] or v["escalated"]) and not (label and not label["provisional"]):
            state = "escalated" if v["escalated"] else "split"
        elif slug in failed:
            state = "grading_again"
        rail.append(
            s.PaperTopic(
                topic=look,
                fit=fits.get(slug),
                grade=shows.grade if shows else None,
                rarity=shows.rarity if shows else None,
                via=shows.via if shows else None,
                verdict=s.CurrentVerdict(**v) if v else None,
                word=said,
                label=s.LabelState(**label) if label else None,
                reaction=s.ReactionState(**thumb) if thumb else None,
                missed=slug in missed,
                state=state,
            )
        )
    return rail


def _classification(tree: Taxonomy | None, found: dict[str, Any]) -> s.Classification | None:
    c = found["classification"]
    if c is None or tree is None:
        return None
    members = _members(tree, found["memberships"])
    by_class: dict[str, list[s.ClassSource]] = {}
    for m in found["memberships"]:
        if m["class"] in {k.slug for k in tree.classes}:
            by_class.setdefault(m["class"], []).append(
                s.ClassSource(source=m["source"], member=m["member"], probability=m["probability"])
            )
    order = [k.slug for k in tree.classes]
    return s.Classification(
        primary=c["primary_class"],
        primary_name=c["primary_name"],
        model=c["model"],
        at=c["classified_at"],
        paper_revision=c["paper_revision"],
        classes=[
            s.PaperClass(
                slug=slug,
                name=tree.get(slug).name,
                path=[tree.get(a).name for a in reversed([slug, *tree.ancestors(slug)])],
                member=slug in members,
                sources=by_class[slug],
            )
            for slug in sorted(by_class, key=order.index)
        ],
    )


async def _history(
    c: psycopg.AsyncConnection, looks: dict[str, s.Badge], rows: list[dict[str, Any]]
) -> list[s.HistoryItem]:
    names = await reading.class_names(c, list({r["topic"] for r in rows if r["topic"]}))
    return [
        s.HistoryItem(
            **r,
            name=names.get(r["topic"]),
            followed=r["what"] != "classified" and r["topic"] in looks,
        )
        for r in rows
    ]


@router.put("/papers/{paper_id:path}/publication")
async def set_publication(
    paper_id: str, body: s.PublicationIn, request: Request, c: Conn
) -> Publication | None:
    """The owner names the published version by DBLP key or DOI, or says there is none.
    A lookup never overwrites it. A DOI DBLP does not know is kept with the arXiv entry."""
    await _known(c, paper_id)
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


async def _matching(c: psycopg.AsyncConnection, query: s.PaperQuery) -> list[str]:
    """Every collection paper a Papers filter matches, in its order; none when it asks for
    the papers outside the collection."""
    if query.source == "outside":
        return []
    tree = await taxonomy_store.load(c)
    _topics_known(tree, query.topic)
    listed = await reading.papers(c, tree, _query(query), limit=None, ids_only=True)
    return [r["id"] for r in listed.rows]


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
    if body.filter is not None:
        ids += [i for i in await _matching(c, body.filter) if i not in ids]
    # The band's hits as the list showed them: none under a filter that leaves the band out.
    cited = body.cited if body.filter is None or _shows_outside(body.filter) else []
    outside = [
        Cited(id=h.id, title=h.title, authors=list(h.authors), year=h.year)
        for h in cited
        if h.id not in ids
    ]
    return await _bibtex(c, ids, outside)


@router.get("/bibtex", response_class=PlainTextResponse)
async def bibtex_filter(c: Conn, params: Annotated[s.PaperQuery, Query()]) -> str:
    """BibTeX for every collection paper a Papers filter matches."""
    return await _bibtex(c, await _matching(c, params))


@router.post("/lookup", responses={**NOT_FOUND, **ARXIV_SILENT})
async def lookup(body: s.LookupIn, c: Conn) -> s.Lookup:
    """Find a paper by arXiv id or link, fetching it when the collection lacks it."""
    try:
        found, fetched = await misses.lookup(c, body.q)
    except misses.NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except NoAnswerError as exc:
        raise HTTPException(status_code=503, detail=misses.SILENT) from exc
    tree = await taxonomy_store.load(c)
    listed = await reading.papers(c, tree, reading.Query(ids=[found.id]), limit=1)
    return s.Lookup(row=_row(tree, _looks(tree), listed.rows[0], s.Row), fetched=fetched)


# -- topics ----------------------------------------------------------------------------------------


# A digest strip's length: the daily digests of a Focus or Area topic, the weekly of a Field's.
STRIP = {DigestPeriod.DAY: 15, DigestPeriod.WEEK: 8}


async def _strips(c: psycopg.AsyncConnection, topics: list[Topic]) -> dict[str, list[Any]]:
    """Each topic's digest strip, oldest first."""
    found: dict[str, list[Any]] = {t.slug: [] for t in topics}
    for period, limit in STRIP.items():
        slugs = [t.slug for t in topics if (t.depth in DAILY) == (period == DigestPeriod.DAY)]
        for r in await digests_store.strips(c, slugs, period, limit):
            found[r["topic"]].append(s.StripDigest(**r))
    return found


def _head(tree: Taxonomy, topic: Topic, strip: list[Any]) -> dict[str, Any]:
    return _looks(tree)[topic.slug].model_dump() | {
        "status": topic.status,
        "subject": topic.subject,
        "strip": strip,
    }


@router.get("/topics")
async def topics(c: Conn) -> list[s.TopicGroup]:
    """The Topics tab: the shown topics grouped Focus, Area, Field, each in the owner's order."""
    tree = await taxonomy_store.load(c)
    cards = await reading.topic_cards(c, tree) if tree else {}
    shown = tree.shown() if tree else []
    strips = await _strips(c, shown)
    return [
        s.TopicGroup(
            depth=depth,
            topics=[
                s.TopicCard(
                    **_head(tree, t, strips[t.slug]),
                    papers=cards[t.slug]["papers"],
                    saved=cards[t.slug]["saved"],
                    counts=s.RarityCounts(**cards[t.slug]["rarities"]),
                    newest_must_read=cards[t.slug]["newest"],
                )
                for t in shown
                if t.depth == depth
            ],
        )
        for depth in Depth
    ]


@router.get("/topics/{slug}", responses=NOT_FOUND)
async def topic(slug: str, c: Conn, params: Annotated[s.TopicPapersIn, Query()]) -> s.TopicPage:
    """A topic's page: its papers (its subtree's), each as the topic shows it, rarest first."""
    _, found = await _topic(c, slug)
    tree = await taxonomy_store.load(c)
    listed = await reading.topic_papers(
        c, tree, slug, _query(params), limit=params.limit, offset=params.offset
    )
    looks = _looks(tree)
    return s.TopicPage(
        topic=s.TopicHead(**_head(tree, found, (await _strips(c, [found]))[found.slug])),
        rows=[_row(tree, looks, r, s.TopicRow) for r in listed.rows],
        total=listed.total,
        counts=s.RarityCounts(**listed.rarities),
        grades=s.GradeCounts(**listed.grades),
    )


# -- the owner's word ------------------------------------------------------------------------------


def _ack(settled: Settled, said: str | None = None) -> s.Ack:
    """What changed; `said` for a toast, with what it did to the library."""
    if said and settled.was_saved and not settled.saved:
        said += "; it leaves the library"
    elif said and settled.saved and not settled.was_saved:
        said += "; it joins the library"
    return s.Ack(
        message=said,
        saved=settled.saved,
        grades={topic: after for topic, (_, after) in settled.grades.items()},
    )


def _speaks(topic: Topic | None, reason: ReactionReason | None) -> bool:
    """Whether a thumb is the owner's word: a word-carrying reason on a Focus topic."""
    return topic is not None and topic.depth == Depth.FOCUS and reason in REACTION_WORDS


# Each action below checks the topic and the paper under the paper's lock (`word.lock`, after
# the taxonomy's, shared), then writes and settles in the same transaction: a topic edited
# meanwhile cannot take a write, or a message, it no longer qualifies for.


@router.put("/reactions/{paper_id:path}", responses=NOT_FOUND)
async def react(paper_id: str, body: s.ReactionIn, c: Conn) -> s.Ack:
    """A thumb for the topic of the surface it was given on, replacing the one there. Wrong
    topic and great find on a Focus topic are the owner's word, and say so."""
    try:
        reaction = Reaction(
            paper_id=paper_id, topic=body.topic, value=body.value, reason=body.reason
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    async with c.transaction():
        await word.lock(c, paper_id)
        await _known(c, paper_id)
        name, topic = await _topic(c, body.topic) if body.topic else (None, None)
        _, settled = await word.react(c, reaction)
    if not _speaks(topic, body.reason):
        return _ack(settled)
    if body.reason == ReactionReason.WRONG_TOPIC:
        return _ack(settled, f"Marked not on {name}")
    return _ack(settled, f"Marked on {name}")


@router.delete("/reactions/{paper_id:path}")
async def unreact(paper_id: str, c: Conn, topic: str | None = None) -> s.Ack:
    """Clear a thumb: it takes back its own word and nothing else. A thumb on a topic no longer
    shown can be cleared too; it carried no word, so nothing is said."""
    async with c.transaction():
        await word.lock(c, paper_id)
        thumbs = await word_store.reactions_for(c, paper_id)
        thumb = next((r for r in thumbs if r.topic == topic), None)
        shown = await _shown(c, topic) if thumb and topic else None
        settled = await word.unreact(c, paper_id, topic)
    if shown and _speaks(shown[1], thumb.reason):
        return _ack(settled, f"Mark on {shown[0]} taken back")
    return _ack(settled)


@router.put("/library/{paper_id:path}", responses=NOT_FOUND)
async def save(paper_id: str, body: s.SaveIn, c: Conn) -> s.Ack:
    """Save or unsave by hand; the newest use outranks every other reason."""
    await _known(c, paper_id)
    return _ack(await word.toggle(c, paper_id, Hand.SAVE if body.saved else Hand.UNSAVE))


@router.put("/labels/{topic}/{paper_id:path}", responses=NOT_FOUND)
async def label(topic: str, paper_id: str, body: s.LabelIn, c: Conn) -> s.Ack:
    """The Fit control on a paper's page: the owner's own word on any shown topic (origin
    `reader`). Unsure is stored and counts nowhere."""
    given = Label(paper_id=paper_id, topic=topic, value=body.value, origin=LabelOrigin.READER)
    async with c.transaction():
        await word.lock(c, paper_id)
        await _topic(c, topic)
        await _known(c, paper_id)
        _, settled = await word.label(c, given)
    return _ack(settled)


@router.delete("/labels/{topic}/{paper_id:path}")
async def unlabel(topic: str, paper_id: str, c: Conn) -> s.Ack:
    """Tapping the filled segment: the label goes, and the verdict's Fit stands again."""
    return _ack(await word.unlabel(c, paper_id, topic))


@router.post(
    "/misses", responses={**NOT_FOUND, 409: {"description": "Not a Focus topic"}, **ARXIV_SILENT}
)
async def report_miss(body: s.MissIn, c: Conn) -> s.Ack:
    """File a miss for a Focus topic: the owner's word, at once, and the paper saved. A paper
    litwatch lacks is fetched first, as Process fetches it, with no transaction open, and
    stored in the report's own, so a refused report stores nothing."""
    try:
        paper_id, fetched = await misses.resolve(c, body.paper_id)
    except misses.NotFoundError as exc:
        raise HTTPException(status_code=404, detail="no such paper") from exc
    except NoAnswerError as exc:
        raise HTTPException(status_code=503, detail=misses.SILENT) from exc
    async with c.transaction():
        name, topic = await _topic(c, body.topic)
        if topic.depth != Depth.FOCUS:
            raise HTTPException(status_code=409, detail="not a Focus topic")
        found, _ = await misses.keep(c, paper_id, fetched)
        await word.lock(c, found.id)
        _, settled, new = await misses.report(c, found.id, body.topic, body.fit)
    if not new:
        return _ack(settled, f"Already reported as missed for {name}")
    return _ack(settled, f"Reported as missed for {name}")


# -- the session -----------------------------------------------------------------------------------

session = APIRouter(prefix="/api")


@session.post("/session", status_code=204)
async def sign_in(body: s.SessionIn, request: Request, response: Response) -> None:
    """Exchange the token for the cookie the web app then rides on."""
    if not token_ok(request, body.token):
        raise HTTPException(status_code=401, detail="wrong token")
    set_token_cookie(request, response, body.token)
