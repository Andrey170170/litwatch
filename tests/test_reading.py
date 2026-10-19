"""The reading API: the status line, Papers, the Topics tab and a topic's page, a paper's page,
the owner's word with its acknowledgements, and misses. Data is built through the store, as
the pipeline steps would record it; no model is called."""

import asyncio
import re

import httpx
import pytest

from litwatch.api import create_app
from litwatch.config import Config
from litwatch.core import Analysis, Cause, Facets, Fit, Opinion, Status, Verdict, Weight
from litwatch.core.classification import Membership, Source
from litwatch.feedback import word
from litwatch.store import analyses as analyses_store
from litwatch.store import classifications as classifications_store
from litwatch.store import failures, runs, verdicts
from litwatch.store import papers as papers_store
from litwatch.store import reading as reading_store
from litwatch.store import taxonomy as taxonomy_store
from tests.conftest import AGENTS, FAITH, INTERP, TREE, answering, paper

TOKEN = "s3cret"
AUTH = {"authorization": f"Bearer {TOKEN}"}
P = paper("2610.00001", title="Traces that hide steps", abstract="Hidden computation in CoT.")
Q = paper("2610.00002", title="A near neighbour", abstract="Sparse features.")
VERSIONS = {c.slug: c.version for c in TREE.classes}


@pytest.fixture
async def client(conn):
    await taxonomy_store.save(conn, TREE)
    await papers_store.upsert(conn, [P, Q])
    dsn = f"{conn.test_dsn}?options=-csearch_path%3D{conn.test_schema}"
    app = create_app(dsn, TOKEN, Config())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test", headers=AUTH) as c:
        yield c


async def classify(conn, p, *classes, keyword=()) -> None:
    """A classification: the first class primary, each a classifier membership; `keyword`
    Focus classes joined by the keyword check alone."""
    primary = classes[0] if classes else "other"
    await classifications_store.add(conn, p, TREE.classes_version, primary, None, "test")
    found = [
        Membership(slug=c, source=Source.CLASSIFIER) for c in dict.fromkeys([primary, *classes])
    ]
    found += [Membership(slug=k, source=Source.KEYWORD) for k in keyword]
    await classifications_store.replace(conn, p, found, VERSIONS)
    await word.settle(conn, p.id, cause=Cause.VERDICT)


async def analyse(conn, p, weight="solid", *, code=False, gist="Probes find hidden steps.") -> None:
    facets = Facets(contribution="method", releases_code=code, releases_weights=False)
    found = Analysis(gist=gist, weight=Weight(weight), weight_reason="Sound work.", facets=facets)
    await analyses_store.add(conn, p, "other", found, "test")
    await word.settle(conn, p.id, cause=Cause.VERDICT)


async def grade(conn, p, fit: str, topic=FAITH, *, check=None, escalated=False) -> None:
    """A verdict of `fit` on `topic`, on the paper's current text; `check` a second grader's
    differing Fit (a split)."""
    opinions = {"judge": Opinion(fit=Fit(fit), reason="judge's reason", model="anthropic:x")}
    if check or topic is FAITH:
        opinions["check"] = Opinion(fit=Fit(check or fit), reason="check's", model="openrouter:y")
    await verdicts.add(
        conn,
        Verdict(
            paper_id=p.id,
            topic=topic.slug,
            topic_version=topic.version,
            paper_revision=await papers_store.revision(conn, p.id),
            fit=Fit(fit),
            reason="judge's reason",
            opinions=opinions,
            split=check is not None,
            escalated=escalated,
        ),
    )
    await word.settle(conn, p.id, cause=Cause.VERDICT)


async def stop(conn, p, step: str, topic: str = "") -> None:
    """Three unusable answers in a row: the step waits for the owner."""
    for _ in range(3):
        await failures.failed(conn, p, step, "unusable", counts=True, topic=topic)


async def ids(client, path="/api/papers", **params) -> list[str]:
    return [r["id"] for r in (await client.get(path, params=params)).json()["rows"]]


@pytest.fixture
async def shelves(client, conn):
    """Five papers, one per place on Papers: legendary (an Area must read on a landmark), epic
    (a Focus must read), rare (an ungraded landmark in the Focus class), common (a Field
    adjacent on an incremental paper) and one not classified yet."""
    legendary = paper("2610.00003", title="Sparse features at scale", abstract="Dictionaries.")
    rare = paper("2610.00004", title="Probing a trace", abstract="Linear probes.")
    common = paper("2610.00005", title="Agents with tools", abstract="Tool use.")
    unread = paper("2610.00006", title="Not read yet", abstract="Pending.")
    await papers_store.upsert(conn, [legendary, rare, common, unread])
    await classify(conn, P, FAITH.slug)
    await analyse(conn, P)
    await grade(conn, P, "central")
    await classify(conn, legendary, INTERP.slug)
    await analyse(conn, legendary, "landmark")
    await grade(conn, legendary, "related", INTERP)
    await classify(conn, rare, FAITH.slug)
    await analyse(conn, rare, "landmark")
    await classify(conn, common, AGENTS.slug)
    await analyse(conn, common, "incremental", code=True)
    await grade(conn, common, "adjacent", AGENTS)
    return legendary, P, rare, common, unread


# -- Papers ----------------------------------------------------------------------------------------


async def test_papers_are_rarest_first_with_the_counts_the_shelves_need(client, shelves):
    legendary, epic, rare, common, unread = shelves
    page = (await client.get("/api/papers")).json()
    # Rarest first, then newest; ties (here all undated) by id.
    assert [r["id"] for r in page["rows"]] == [p.id for p in shelves[:4]] + [Q.id, unread.id]
    assert [r["rarity"] for r in page["rows"]] == [
        "legendary", "epic", "rare", "common", None, None,
    ]  # fmt: skip
    counts = {"legendary": 1, "epic": 1, "rare": 1, "uncommon": 0, "common": 1, "unrated": 2}
    assert page["counts"] == counts and page["total"] == 6
    # A shelf: a level filter keeps its rows and total; the counts stay the bar's.
    shelf = (await client.get("/api/papers", params={"rarity": ["epic", "legendary"]})).json()
    assert [r["id"] for r in shelf["rows"]] == [legendary.id, epic.id]
    assert (shelf["total"], shelf["counts"]) == (2, counts)
    # A row: its current Gist, Weight, facets, badge, library state and no open state.
    row = page["rows"][1]
    assert (row["gist"], row["weight"], row["saved"], row["state"]) == (
        "Probes find hidden steps.", "solid", True, None,
    )  # fmt: skip
    assert row["facets"] == {
        "contribution": "method",
        "releases_code": False,
        "releases_weights": False,
    }
    assert row["badges"] == [
        {
            "slug": FAITH.slug,
            "name": "Chain-of-thought faithfulness",
            "depth": "focus",
            "monogram": "CF",
            "colour": FAITH.colour,
            "grade": "must_read",
            "rarity": "epic",
        },
        {  # its Area, by its Focus topic's grade
            "slug": INTERP.slug,
            "name": "Interpretability",
            "depth": "area",
            "monogram": "IN",
            "colour": INTERP.colour,
            "grade": "must_read",
            "rarity": "epic",
        },
    ]
    assert page["rows"][4]["gist"] is None and page["rows"][4]["badges"] == []
    # Paging: a page past the end still counts the filter.
    assert await ids(client, limit=2, offset=1) == [epic.id, rare.id]
    beyond = (await client.get("/api/papers", params={"offset": 50})).json()
    assert (beyond["rows"], beyond["total"]) == ([], 6)


async def test_papers_filter_by_topic_with_its_subtree_depth_grade_and_facts(client, conn, shelves):
    legendary, epic, rare, common, _ = shelves
    # Interpretability's page holds its Focus topic's papers too.
    assert await ids(client, topic=INTERP.slug) == [legendary.id, epic.id, rare.id]
    assert await ids(client, topic=[FAITH.slug, AGENTS.slug]) == [epic.id, rare.id, common.id]
    assert await ids(client, depth="field") == [common.id]
    assert await ids(client, topic=FAITH.slug, depth="area") == []
    assert (await client.get("/api/papers", params={"topic": "nope"})).status_code == 404
    # A grade is a badge's, on a topic in view.
    assert await ids(client, grade="must_read") == [legendary.id, epic.id]
    assert await ids(client, grade="must_read", topic=AGENTS.slug) == []
    assert await ids(client, grade="ungraded", topic=FAITH.slug) == [rare.id]
    assert await ids(client, saved=True) == [epic.id]
    assert await ids(client, code=True) == [common.id]
    assert await ids(client, weight="landmark") == [legendary.id, rare.id]
    await conn.execute(
        "UPDATE papers SET published = '2026-10-10T23:00:00Z' WHERE id = %s", (rare.id,)
    )
    assert await ids(client, since="2026-10-10", until="2026-10-10") == [rare.id]
    assert await ids(client, since="2026-10-11") == []


async def test_a_keyword_or_band_membership_shows_a_paper_only_once_graded(client, conn):
    """The uncertain band and the keyword check are for grading only (pipeline.md, section 2)."""
    await classify(conn, Q, "other", keyword=[FAITH.slug])
    assert await ids(client, topic=FAITH.slug) == []
    await grade(conn, Q, "adjacent")
    assert await ids(client, topic=FAITH.slug) == [Q.id]
    # Graded for a followed descendant only, it is on the parent's page and counts in its card.
    assert await ids(client, topic=INTERP.slug) == [Q.id]
    assert await ids(client, f"/api/topics/{INTERP.slug}") == [Q.id]
    cards = {t["slug"]: t for g in (await client.get("/api/topics")).json() for t in g["topics"]}
    assert (cards[INTERP.slug]["papers"], cards[INTERP.slug]["counts"]["uncommon"]) == (1, 1)


async def test_search_reads_title_abstract_and_authors_best_match_first(client, conn):
    # Newer than P: rarest (then newest) would put it first, the best match puts P first.
    hidden = paper(
        "2610.00007",
        title="Other things",
        abstract="Traces hide steps, briefly.",
        published="2026-10-14T00:00:00+00:00",
    )
    await papers_store.upsert(conn, [hidden])
    assert set(await ids(client, q="Alice Smith")) == {P.id, Q.id, hidden.id}
    assert await ids(client, q="hide steps", sort="rarest") == [hidden.id, P.id]
    # A search without a sort is best match first; an explicit sort wins (L5-4).
    assert await ids(client, q="hide steps") == [P.id, hidden.id]
    assert await ids(client, q="hide steps", sort="best") == [P.id, hidden.id]
    assert await ids(client) == [hidden.id, P.id, Q.id]  # no search: rarest, newest first
    assert await ids(client, q="sparse") == [Q.id]
    rows = (await client.get("/api/papers", params={"q": "hidden computation"})).json()["rows"]
    assert [r["id"] for r in rows] == [P.id]


async def test_a_row_carries_its_open_state(client, conn, shelves):
    _, epic, rare, common, _ = shelves
    await grade(conn, epic, "central", check="adjacent")  # a split, not yet labelled
    await stop(conn, rare, "analysis")
    await stop(conn, common, "grading", AGENTS.slug)
    states = {r["id"]: r["state"] for r in (await client.get("/api/papers")).json()["rows"]}
    assert (states[epic.id], states[rare.id], states[common.id]) == ("word", "failure", "failure")
    # A topic's states count in that topic's lists; a paper's own everywhere.
    rows = (await client.get(f"/api/topics/{INTERP.slug}")).json()["rows"]
    assert {r["id"]: r["state"] for r in rows} == {
        epic.id: "word",
        rare.id: "failure",
        shelves[0].id: None,
    }
    await client.put(f"/api/labels/{FAITH.slug}/{epic.id}", json={"value": "central"})
    assert {r["id"]: r["state"] for r in (await client.get("/api/papers")).json()["rows"]}[
        epic.id
    ] is None
    # Dismissal ends a paper's work: asked for by id (a lookup), it has no open state.
    await conn.execute("UPDATE papers SET dismissed_at = now() WHERE id = %s", (rare.id,))
    tree = await taxonomy_store.load(conn)
    (row,) = (await reading_store.papers(conn, tree, reading_store.Query(ids=[rare.id]))).rows
    assert row["state"] is None


async def test_badges_put_the_topic_giving_the_highest_level_first(client, conn):
    await classify(conn, P, FAITH.slug, AGENTS.slug)
    await grade(conn, P, "none")  # Focus none: common
    await grade(conn, P, "central", AGENTS)  # Field must read: epic, lowered to uncommon
    (row,) = (await client.get("/api/papers", params={"q": "hidden"})).json()["rows"]
    assert [b["slug"] for b in row["badges"]] == [AGENTS.slug, FAITH.slug, INTERP.slug]
    assert row["rarity"] == "uncommon"


async def test_badges_and_rail_cover_every_followed_topic_whose_page_holds_the_paper(client, conn):
    """A badge per followed topic whose subtree the paper is in, graded there or not, and per
    followed ancestor of a topic that graded it; the rail shows the same topics."""
    await classify(conn, P, FAITH.slug)  # not graded yet, as for a topic just followed
    await analyse(conn, P, "landmark")
    (row,) = (await client.get("/api/papers", params={"q": "hidden"})).json()["rows"]
    assert [(b["slug"], b["grade"], b["rarity"]) for b in row["badges"]] == [
        (FAITH.slug, None, "rare"),  # the landmark Weight's floor
        (INTERP.slug, None, "rare"),
    ]
    # Admitted by the keyword check alone and graded there: its Area shows it too.
    await classify(conn, Q, "other", keyword=[FAITH.slug])
    await grade(conn, Q, "adjacent")
    (row,) = (await client.get("/api/papers", params={"q": "sparse"})).json()["rows"]
    assert [(b["slug"], b["grade"]) for b in row["badges"]] == [
        (FAITH.slug, "adjacent"),
        (INTERP.slug, "adjacent"),
    ]
    rail = (await client.get(f"/api/papers/{Q.id}")).json()["topics"]
    assert [(t["topic"]["slug"], t["fit"], t["via"]) for t in rail] == [
        (FAITH.slug, "adjacent", None),
        (INTERP.slug, None, FAITH.slug),
    ]


# -- topics ----------------------------------------------------------------------------------------


async def test_topics_are_grouped_by_depth_in_the_owners_order(client, conn, shelves):
    await word.reorder(conn, [AGENTS.slug, INTERP.slug, FAITH.slug])
    groups = (await client.get("/api/topics")).json()
    assert [(g["depth"], [t["slug"] for t in g["topics"]]) for g in groups] == [
        ("focus", [FAITH.slug]),
        ("area", [INTERP.slug]),
        ("field", [AGENTS.slug]),
    ]
    interp = groups[1]["topics"][0]
    assert (interp["papers"], interp["saved"], interp["monogram"]) == (3, 1, "IN")
    assert interp["counts"] == {
        "legendary": 1, "epic": 1, "rare": 1, "uncommon": 0, "common": 0, "unrated": 0,
    }  # fmt: skip
    assert interp["newest_must_read"]["id"] in {shelves[0].id, P.id}
    assert groups[0]["topics"][0]["subject"] == FAITH.subject


async def test_a_parent_topic_shows_its_own_grade_else_its_best_descendants(client, conn, shelves):
    legendary, epic, rare, _, _ = shelves
    page = (await client.get(f"/api/topics/{INTERP.slug}")).json()
    assert page["topic"]["depth"] == "area" and page["topic"]["status"] == "active"
    shown = {r["id"]: (r["grade"], r["rarity"], r["via"]) for r in page["rows"]}
    assert shown == {
        legendary.id: ("must_read", "legendary", None),  # its own
        epic.id: ("must_read", "epic", FAITH.slug),  # its Focus topic's
        rare.id: (None, "rare", None),  # ungraded: its landmark Weight's floor
    }
    assert page["grades"] == {
        "must_read": 2, "relevant": 0, "adjacent": 0, "none": 0, "ungraded": 1,
    }  # fmt: skip
    assert page["counts"]["legendary"] == 1 and page["total"] == 3
    assert await ids(client, f"/api/topics/{INTERP.slug}", grade="ungraded") == [rare.id]
    # A paper the parent graded itself shows the parent's grade, whatever its descendants say.
    await grade(conn, epic, "none", INTERP)
    page = (await client.get(f"/api/topics/{INTERP.slug}", params={"grade": "none"})).json()
    ((row,),) = [page["rows"]]
    assert (row["id"], row["grade"], row["rarity"], row["via"]) == (epic.id, "none", "common", None)
    assert (await client.get("/api/topics/other")).status_code == 404


# -- a paper ---------------------------------------------------------------------------------------


async def test_a_paper_page_has_its_header_rail_classes_states_and_history(client, conn):
    await conn.execute(
        "UPDATE papers SET version = 2, version_history = %s WHERE id = %s",
        ('[{"version": 1, "date": "2026-10-01T00:00:00Z"}, {"version": 2}]', P.id),
    )
    await classify(conn, P, FAITH.slug, AGENTS.slug)
    await analyse(conn, P, "landmark")
    await grade(conn, P, "central", check="related")  # a Focus split
    await grade(conn, P, "adjacent", AGENTS)
    await client.put(f"/api/reactions/{P.id}", json={"topic": AGENTS.slug, "value": "up"})
    await stop(conn, P, "grading", INTERP.slug)

    page = (await client.get(f"/api/papers/{P.id}")).json()
    assert (page["rarity"], page["in_collection"], page["revision"]) == ("legendary", True, 1)
    assert [b["slug"] for b in page["badges"]] == [FAITH.slug, INTERP.slug, AGENTS.slug]
    assert page["weight"] == {"weight": "landmark", "reason": "Sound work.", "source": "abstract"}
    assert page["gist"] == {"text": "Probes find hidden steps.", "source": "abstract"}
    assert [v["version"] for v in page["versions"]] == [1, 2] and page["version"] == 2
    assert page["source_url"] == f"https://arxiv.org/e-print/{P.id}"

    rail = {t["topic"]["slug"]: t for t in page["topics"]}
    assert list(rail) == [FAITH.slug, INTERP.slug, AGENTS.slug]  # the fixed order
    faith = rail[FAITH.slug]
    assert (faith["fit"], faith["grade"], faith["rarity"], faith["state"]) == (
        "central", "must_read", "legendary", "split",
    )  # fmt: skip
    assert faith["verdict"]["opinions"]["check"]["fit"] == "related"
    assert faith["word"] is None and faith["label"] is None
    interp = rail[INTERP.slug]  # graded by its Focus topic, and stopped grading on its own
    assert (interp["fit"], interp["grade"], interp["via"], interp["state"]) == (
        None, "must_read", FAITH.slug, "ungraded",
    )  # fmt: skip
    assert rail[AGENTS.slug]["reaction"] == {"value": "up", "reason": None}
    assert [(s["kind"], s["topic"], s["revision"]) for s in page["states"]] == [
        ("split", FAITH.slug, 1),
        ("ungraded", INTERP.slug, 1),
    ]
    assert page["states"][1]["error"] == "unusable"

    c = page["classification"]
    assert (c["primary"], c["primary_name"], c["paper_revision"]) == (
        FAITH.slug, "Chain-of-thought faithfulness", 1,
    )  # fmt: skip
    faith_class = next(k for k in c["classes"] if k["slug"] == FAITH.slug)
    assert faith_class["path"] == ["Interpretability", "Chain-of-thought faithfulness"]
    assert faith_class["member"] and faith_class["sources"][0]["source"] == "classifier"
    assert [h["what"] for h in page["history"]] == [
        "classified", "graded", "graded", "reacted",
    ]  # fmt: skip

    # Unfollowed, a topic leaves the rail; its verdict stays in the history.
    unfollowed = [t for t in TREE.topics if t.slug != AGENTS.slug]
    await word.save_taxonomy(conn, TREE.model_copy(update={"topics": unfollowed}))
    page = (await client.get(f"/api/papers/{P.id}")).json()
    assert AGENTS.slug not in [t["topic"]["slug"] for t in page["topics"]]
    graded = [h for h in page["history"] if h["what"] == "graded"]
    assert {h["topic"]: h["followed"] for h in graded} == {FAITH.slug: True, AGENTS.slug: False}
    assert next(h for h in graded if h["topic"] == AGENTS.slug)["name"] == "Agents and tool use"
    assert (await client.get("/api/papers/2610.99999")).status_code == 404


async def test_a_paper_page_names_its_own_open_states(client, conn):
    await stop(conn, Q, "classification")
    page = (await client.get(f"/api/papers/{Q.id}")).json()
    assert (page["in_collection"], page["rarity"], page["topics"]) == (False, None, [])
    assert [(s["kind"], s["revision"]) for s in page["states"]] == [("awaiting_classification", 1)]
    await classify(conn, P, FAITH.slug)
    await stop(conn, P, "analysis")
    page = (await client.get(f"/api/papers/{P.id}")).json()
    assert [s["kind"] for s in page["states"]] == ["analysis_stopped"]
    assert (page["gist"], page["weight"], page["rarity"]) == (None, None, "uncommon")
    assert page["topics"][0]["grade"] is None and page["topics"][0]["rarity"] == "uncommon"
    # A grading stopped on a paused topic waits on nothing: it grades nothing new.
    await stop(conn, P, "grading", FAITH.slug)
    assert "ungraded" in [
        s["kind"] for s in (await client.get(f"/api/papers/{P.id}")).json()["states"]
    ]
    paused = FAITH.model_copy(update={"status": Status.PAUSED})
    await word.save_taxonomy(conn, TREE.following(paused))
    page = (await client.get(f"/api/papers/{P.id}")).json()
    assert [s["kind"] for s in page["states"]] == ["analysis_stopped"]


# -- the owner's word ------------------------------------------------------------------------------


async def test_a_thumb_takes_a_reason_from_feedback_and_says_only_what_the_page_cannot(
    client, conn
):
    await classify(conn, P, FAITH.slug)
    await grade(conn, P, "related")  # relevant: in the library

    async def thumb(value, reason, topic=FAITH.slug):
        return await client.put(
            f"/api/reactions/{P.id}", json={"topic": topic, "value": value, "reason": reason}
        )

    said = await thumb("down", "wrong_topic")
    assert said.json() == {
        "message": "Marked not on Chain-of-thought faithfulness; it leaves the library",
        "saved": False,
        "grades": {FAITH.slug: "none"},
    }
    back = await client.delete(f"/api/reactions/{P.id}", params={"topic": FAITH.slug})
    assert back.json()["message"] == (
        "Mark on Chain-of-thought faithfulness taken back; it joins the library"
    )
    assert (await thumb("up", "great_find")).json()["message"] == (
        "Marked on Chain-of-thought faithfulness"
    )
    # A collected reason changes nothing the page cannot show: no message.
    for value, reason in [("down", "overrated"), ("down", "not_interesting"),
                          ("down", "already_knew"), ("up", "underrated")]:  # fmt: skip
        assert (await thumb(value, reason)).json()["message"] is None
    assert (await thumb("down", "wrong_topic", INTERP.slug)).json()["message"] is None
    assert (await client.delete(f"/api/reactions/{P.id}", params={"topic": INTERP.slug})).json()[
        "message"
    ] is None
    assert (await thumb("up", "overrated")).status_code == 422
    assert (await thumb("down", "underrated")).status_code == 422
    assert (await thumb("down", "unrelated")).status_code == 422  # the first v1's name
    assert (await thumb("up", None, "nope")).status_code == 404
    # A thumb on a topic no longer followed can still be cleared; it says nothing.
    await thumb("up", None, AGENTS.slug)
    unfollowed = [t for t in TREE.topics if t.slug != AGENTS.slug]
    await word.save_taxonomy(conn, TREE.model_copy(update={"topics": unfollowed}))
    cleared = await client.delete(f"/api/reactions/{P.id}", params={"topic": AGENTS.slug})
    assert (cleared.status_code, cleared.json()["message"]) == (200, None)
    (left,) = await (
        await conn.execute("SELECT count(*) AS n FROM reactions WHERE topic = %s", (AGENTS.slug,))
    ).fetchall()
    assert left["n"] == 0
    saved = await client.put(f"/api/library/{Q.id}", json={"saved": True})
    assert saved.json() == {"message": None, "saved": True, "grades": {}}


async def test_a_label_is_a_fit_or_unsure_on_any_topic(client, conn):
    await classify(conn, P, INTERP.slug)
    await grade(conn, P, "related", INTERP, check="adjacent", escalated=True)

    async def rail():
        page = (await client.get(f"/api/papers/{P.id}")).json()
        return page["topics"][0]

    assert (await rail())["state"] == "escalated"
    url = f"/api/labels/{INTERP.slug}/{P.id}"
    said = await client.put(url, json={"value": "central"})
    assert said.json() == {"message": None, "saved": False, "grades": {INTERP.slug: "must_read"}}
    last = await (
        await conn.execute(
            "SELECT payload FROM outbox WHERE type = 'grade.changed' ORDER BY id DESC"
        )
    ).fetchone()
    assert last["payload"]["cause"] == "owner"
    topic = await rail()
    assert (topic["word"], topic["fit"], topic["state"]) == ("central", "central", None)
    assert topic["label"] == {"value": "central", "origin": "reader", "provisional": False}
    # Unsure is stored and is no word: the verdict's Fit stands.
    await client.put(url, json={"value": "unsure"})
    topic = await rail()
    assert (topic["word"], topic["fit"], topic["label"]["value"]) == (None, "related", "unsure")
    assert (await client.put(url, json={"value": "relevant"})).status_code == 422
    assert (await client.put(f"/api/labels/other/{P.id}", json={"value": "none"})).status_code == (
        404
    )
    assert (await client.delete(url)).json()["message"] is None
    assert (await rail())["label"] is None


# -- carried from the first v1 ---------------------------------------------------------------------


async def test_signing_in_trades_the_token_for_a_cookie(client):
    bare = httpx.AsyncClient(transport=client._transport, base_url="http://test")
    assert (await bare.get("/api/status")).status_code == 401
    assert (await bare.post("/api/session", json={"token": "no"})).status_code == 401
    assert (await bare.post("/api/session", json={"token": TOKEN})).status_code == 204
    assert (await bare.get("/api/status")).json()["state"] == "none"


async def test_the_status_line_follows_the_latest_nightly_run(client, conn):
    assert (await client.get("/api/status")).json() == {
        "state": "none",
        "run_id": None,
        "started_at": None,
        "finished_at": None,
        "error": None,
        "papers": 0,
        "waiting": None,
        "awaiting_classification": 0,
        "analysis_failed": 0,
        "ungraded": 0,
        "fulltext_failed": 0,
        "digests_failed": 0,
        "over_budget": [],
    }
    # A run its process holds open (this test's own session).
    await runs.start(conn, "nightly")
    await conn.execute("UPDATE runs SET started_at = now() - '1m'::interval")
    await classify(conn, P, FAITH.slug)
    running = (await client.get("/api/status")).json()
    assert (running["state"], running["papers"], running["waiting"]) == ("running", 1, 1)
    # The owner classifying by hand meanwhile is not the run's work.
    await conn.execute(
        "INSERT INTO classifications (paper_id, paper_revision, taxonomy_revision, "
        "primary_class, model) SELECT paper_id, paper_revision, taxonomy_revision, "
        "primary_class, 'owner' FROM classifications"
    )
    assert (await client.get("/api/status")).json()["papers"] == 1
    await conn.execute("UPDATE runs SET finished_at = now()")
    done = (await client.get("/api/status")).json()
    assert (done["state"], done["papers"], done["waiting"]) == ("quiet", 1, None)
    await conn.execute("UPDATE runs SET error = 'boom'")
    status = (await client.get("/api/status")).json()
    assert (status["state"], status["error"]) == ("failed", "boom")
    # A run left open by a process that is gone failed too.
    await conn.execute("UPDATE runs SET error = NULL, finished_at = NULL, backend_pid = NULL")
    status = (await client.get("/api/status")).json()
    assert (status["state"], status["error"]) == (
        "failed",
        "the run's process ended without closing it",
    )


async def test_a_reported_miss_is_the_owners_word(client, conn):
    ack = await client.post("/api/misses", json={"paper_id": Q.id, "topic": FAITH.slug})
    assert ack.json()["message"] == (
        "Reported as missed for Chain-of-thought faithfulness; it joins the library"
    )
    (miss,) = await (await conn.execute("SELECT * FROM misses")).fetchall()
    assert (miss["paper_id"], miss["topic"]) == (Q.id, FAITH.slug)
    page = (await client.get(f"/api/papers/{Q.id}")).json()
    faith, interp = page["topics"]  # its Area shows it through the Focus topic
    assert (interp["topic"]["slug"], interp["via"]) == (INTERP.slug, FAITH.slug)
    assert faith["label"] == {"value": "central", "origin": "miss", "provisional": False}
    assert faith["missed"] and faith["word"] == "central" and faith["grade"] == "must_read"
    said = [(h["what"], h["value"]) for h in page["history"]]
    assert said == [("missed", None), ("labelled", "central")]
    # A membership of the topic's class, so grading runs for it whatever classification says,
    # and the owner's newest Save, which outranks an earlier Unsave.
    (member,) = await (await conn.execute("SELECT class, source FROM memberships")).fetchall()
    assert (member["class"], member["source"]) == (FAITH.slug, "miss")
    (hand,) = await (await conn.execute("SELECT hand FROM library")).fetchall()
    assert hand["hand"] == "save"
    await client.put(f"/api/library/{P.id}", json={"saved": False})
    body = {"paper_id": P.id, "topic": FAITH.slug, "fit": "related"}
    assert (await client.post("/api/misses", json=body)).json()["saved"] is True
    body["fit"] = "adjacent"
    assert (await client.post("/api/misses", json=body)).status_code == 422
    body = {"paper_id": P.id, "topic": INTERP.slug}
    assert (await client.post("/api/misses", json=body)).status_code == 409  # not Focus


async def test_a_miss_on_a_paper_litwatch_lacks_fetches_it_first(client, conn, monkeypatch):
    # archive.md: reporting a paper outside the collection brings it in, the owner's word first.
    import psycopg

    from litwatch.feedback import misses
    from litwatch.store.taxonomy import TAXONOMY_LOCK

    fetched = []

    async def fetch(ids):
        # arXiv may keep the fetch waiting for its budget: no transaction holds the taxonomy then.
        async with await psycopg.AsyncConnection.connect(conn.test_dsn, autocommit=True) as other:
            lock = await other.execute("SELECT pg_try_advisory_lock(%s)", (TAXONOMY_LOCK,))
            free = (await lock.fetchone())[0]
            await other.execute("SELECT pg_advisory_unlock_all()")
        assert free, "a taxonomy edit would wait on the fetch"
        fetched.append(ids)
        return [paper(i) for i in ids]

    monkeypatch.setattr(misses, "fetch_by_ids", fetch)
    wrong = await client.post("/api/misses", json={"paper_id": "2610.09999", "topic": INTERP.slug})
    assert wrong.status_code == 409  # a refused report stores nothing
    assert await papers_store.get(conn, "2610.09999") is None
    body = {"paper_id": "2610.09999v2", "topic": FAITH.slug}
    assert (await client.post("/api/misses", json=body)).json()["saved"] is True
    assert fetched == [["2610.09999"], ["2610.09999"]]
    (miss,) = await (await conn.execute("SELECT paper_id FROM misses")).fetchall()
    assert miss["paper_id"] == "2610.09999"
    # A report refused after the fetch stores nothing of it.
    from fastapi import HTTPException

    async def refused(*_):
        raise HTTPException(status_code=409, detail="refused")

    with monkeypatch.context() as m:
        m.setattr(misses, "report", refused)
        body = {"paper_id": "2610.08888", "topic": FAITH.slug}
        assert (await client.post("/api/misses", json=body)).status_code == 409
    assert await papers_store.get(conn, "2610.08888") is None
    monkeypatch.setattr(misses, "fetch_by_ids", answering(lambda ids: []))  # arXiv lacks it too
    body = {"paper_id": "2601.99999", "topic": FAITH.slug}
    assert (await client.post("/api/misses", json=body)).status_code == 404


@pytest.mark.parametrize("second_caller", ["owner", "nightly"])
async def test_two_requests_storing_the_same_fetched_paper_both_get_it(conn, second_caller):
    """An owner request and another owner request, or the nightly intake, that fetched the same
    unfetched paper store it once: the second waits on the first's settle lock, then stores
    over it as intake would, and there is one `paper.added` (L5-2)."""
    from litwatch import store
    from litwatch.feedback import misses
    from litwatch.intake import batch

    fetched = paper("2610.06666")
    async with await store.connect(conn.test_dsn, conn.test_schema) as other:
        async with other.transaction():
            first = await misses.keep(other, fetched.id, fetched)
            # The second runs into the first's settle lock, and waits on it.
            second = asyncio.create_task(
                misses.keep(conn, fetched.id, fetched)
                if second_caller == "owner"
                else batch.store(conn, [fetched], source="arxiv")
            )
            for _ in range(100):
                waiting = await (
                    await other.execute(
                        "SELECT count(*) AS n FROM pg_stat_activity "
                        "WHERE pid = %s AND wait_event_type = 'Lock'",
                        (conn.info.backend_pid,),
                    )
                ).fetchone()
                if waiting["n"]:
                    break
                await asyncio.sleep(0.02)
            assert waiting["n"], "the second did not wait on the first"
        second = await asyncio.wait_for(second, 5)
    assert first[1] is True
    if second_caller == "owner":
        assert second == (first[0], False)
    else:
        assert second.inserted == 0 and second.rewritten == []
    added = await (
        await conn.execute(
            "SELECT count(*) AS n FROM outbox WHERE type = 'paper.added' AND paper_id = %s",
            (fetched.id,),
        )
    ).fetchone()
    assert added["n"] == 1


async def test_an_owners_delayed_fetch_never_overwrites_newer_metadata(conn, monkeypatch):
    """The owner's request resolves a paper from arXiv; before it is kept, the nightly intake
    stores newer metadata without a version (OAI's); the owner's older record then changes
    nothing, though its version cannot be compared (L6-3)."""
    from litwatch.feedback import misses
    from litwatch.intake import batch

    pid = "2610.04444"
    older = paper(pid, title="As first submitted", version=1, updated="2026-10-01T10:00:00Z")
    monkeypatch.setattr(misses, "fetch_by_ids", answering(lambda ids: [older]))
    _, fetched = await misses.resolve(conn, pid)
    newer = paper(pid, title="As revised since", updated="2026-10-05T00:00:00Z")
    await batch.store(conn, [newer], source="oai")
    stored, inserted = await misses.keep(conn, pid, fetched)
    assert (stored.title, stored.version, inserted) == ("As revised since", None, False)
    assert stored.updated.startswith("2026-10-05")
    revised = await (
        await conn.execute("SELECT count(*) AS n FROM outbox WHERE type = 'paper.revised'")
    ).fetchone()
    assert revised["n"] == 0


async def test_an_owner_request_ends_when_arxiv_stays_silent(client, conn, monkeypatch, slow_arxiv):
    """Process and Report as a miss on a paper litwatch lacks wait on arXiv for its budget at
    most (P5), however slowly arXiv trickles its answer in: then a 503 that says so, and
    nothing stored."""
    from litwatch.feedback import misses
    from litwatch.intake import search

    monkeypatch.setattr(search, "LOOKUP_BUDGET", 0.3)
    monkeypatch.delenv("ARXIV_PACING_DIR", raising=False)
    for path, body in [
        ("/api/lookup", {"q": "2610.07777"}),
        ("/api/misses", {"paper_id": "2610.07777", "topic": FAITH.slug}),
    ]:
        answer = await asyncio.wait_for(client.post(path, json=body), 5)
        assert answer.status_code == 503, path
        assert answer.json()["detail"] == misses.SILENT
    assert await papers_store.get(conn, "2610.07777") is None


async def test_a_miss_taken_back_can_be_reported_again(client, conn):
    """Taking a miss back withdraws the owner's word; reporting again gives it back, keeping
    the miss, and never over a correction the owner made since."""

    async def faith():
        page = (await client.get(f"/api/papers/{Q.id}")).json()
        return next(t for t in page["topics"] if t["topic"]["slug"] == FAITH.slug)

    await client.post("/api/misses", json={"paper_id": Q.id, "topic": FAITH.slug})
    (miss,) = await (await conn.execute("SELECT id FROM misses")).fetchall()
    assert (await faith())["word"] == "central"
    await client.delete(f"/api/labels/{FAITH.slug}/{Q.id}")
    assert (await faith())["word"] is None
    ack = await client.post("/api/misses", json={"paper_id": Q.id, "topic": FAITH.slug})
    assert ack.json()["message"].startswith("Already reported as missed")
    assert (await faith())["word"] == "central" and (await faith())["label"]["origin"] == "miss"
    assert [m["id"] for m in await (await conn.execute("SELECT id FROM misses")).fetchall()] == [
        miss["id"]
    ]
    # A newer thumb outranks it as the owner's word, though the central label is still stored.
    body = {"topic": FAITH.slug, "value": "down", "reason": "wrong_topic"}
    await client.put(f"/api/reactions/{Q.id}", json=body)
    assert (await faith())["word"] == "none"
    await client.delete(f"/api/reactions/{Q.id}", params={"topic": FAITH.slug})
    assert (await faith())["word"] == "central"
    # A correction since stands: the owner said its Fit is none after all.
    await client.put(f"/api/labels/{FAITH.slug}/{Q.id}", json={"value": "none"})
    await client.post("/api/misses", json={"paper_id": Q.id, "topic": FAITH.slug})
    assert (await faith())["label"]["value"] == "none" and (await faith())["word"] == "none"
    # The history shows the correction, the label keeping the miss's origin.
    history = (await client.get(f"/api/papers/{Q.id}")).json()["history"]
    assert [(h["what"], h["value"]) for h in history if h["what"] == "labelled"] == [
        ("labelled", "none")
    ]


async def test_reporting_a_miss_again_waits_for_a_correction_in_flight(client, conn):
    """Looking for the owner's standing word and giving it again happen under the paper's
    lock, so a correction being written meanwhile is seen, and stands."""
    from litwatch import store
    from litwatch.core import Label, LabelOrigin, LabelValue
    from litwatch.store import feedback as feedback_store

    await client.post("/api/misses", json={"paper_id": Q.id, "topic": FAITH.slug})
    await client.delete(f"/api/labels/{FAITH.slug}/{Q.id}")
    async with await store.sibling(conn) as other:
        async with other.transaction():
            await word.lock_many(other, [Q.id])  # a correction, part way through
            again = asyncio.create_task(
                client.post("/api/misses", json={"paper_id": Q.id, "topic": FAITH.slug})
            )
            await asyncio.sleep(0.5)
            assert not again.done()
            await feedback_store.set_label(
                other,
                Label(
                    paper_id=Q.id,
                    topic=FAITH.slug,
                    value=LabelValue.NONE,
                    origin=LabelOrigin.READER,
                ),
            )
        assert (await again).status_code == 200
    (label,) = await (await conn.execute("SELECT value, origin FROM labels")).fetchall()
    assert (label["value"], label["origin"]) == ("none", "reader")


async def test_a_paper_without_authors_still_reads(client, conn):
    await conn.execute("UPDATE papers SET authors = 'null'::jsonb WHERE id = %s", (P.id,))
    rows = (await client.get("/api/papers", params={"q": "hidden computation"})).json()["rows"]
    assert rows[0]["authors"] == []
    assert (await client.get(f"/api/papers/{P.id}")).json()["authors"] == []
    assert "@misc" in (await client.post("/api/bibtex", json={"ids": [P.id]})).text


async def test_the_cookie_is_secure_behind_https(client):
    signed = await client.post(
        "/api/session", json={"token": TOKEN}, headers={"x-forwarded-proto": "https"}
    )
    assert "secure" in signed.headers["set-cookie"].lower()
    plain = await client.post("/api/session", json={"token": TOKEN})
    assert "secure" not in plain.headers["set-cookie"].lower()


def test_lookup_reads_old_style_ids():
    from litwatch.intake.search import id_from_url

    assert id_from_url("https://arxiv.org/abs/cs/9901001v2") == "cs/9901001"
    assert id_from_url("math.AG/0601001") == "math.AG/0601001"
    assert id_from_url("arXiv:2610.00001v3") == "2610.00001"


async def test_bibtex_covers_a_filter_and_papers_a_provider_found_outside(client, conn):
    """A search hit the collection lacks gets an arXiv entry from what the hit carries,
    beside the collection's papers, without being fetched (decision 6)."""
    hit = {
        "id": "2610.99901",
        "title": "Found by the archive",
        "authors": ["Ada Lovelace"],
        "year": 2026,
    }
    text = (await client.post("/api/bibtex", json={"ids": [P.id], "cited": [hit]})).text
    assert text.count("@misc") == 2
    assert "eprint = {2610.99901}" in text and "year = {2026}" in text
    assert "author = {Ada Lovelace}" in text
    assert await papers_store.get(conn, "2610.99901") is None
    # A hit that is also chosen from the collection is not cited twice.
    hit["id"] = P.id
    text = (await client.post("/api/bibtex", json={"ids": [P.id], "cited": [hit]})).text
    assert text.count("@misc") == 1
    # The collection's entries and the hits share one key namespace: no duplicate keys.
    twin = {"id": "2610.99902", "title": "Traces elsewhere", "authors": P.authors, "year": None}
    text = (await client.post("/api/bibtex", json={"ids": [P.id], "cited": [twin]})).text
    keys = re.findall(r"^@\w+\{([^,]+),", text, re.MULTILINE)
    assert len(keys) == 2 and len(set(keys)) == 2
    # A Papers filter is resolved on the server, in the same request as the band's hits.
    body = {"filter": {"q": "hidden computation"}, "cited": [twin]}
    text = (await client.post("/api/bibtex", json=body)).text
    assert "eprint = {2610.00001}" in text and "eprint = {2610.99902}" in text
    # A filter kept to one band exports that band only, as the list shows it.
    body["filter"]["source"] = "outside"
    text = (await client.post("/api/bibtex", json=body)).text
    assert "eprint = {2610.00001}" not in text and "eprint = {2610.99902}" in text
    body["filter"]["source"] = "collection"
    text = (await client.post("/api/bibtex", json=body)).text
    assert "eprint = {2610.00001}" in text and "eprint = {2610.99902}" not in text
    # So does a filter only the collection has, as the list leaves the band out under it.
    body["filter"] = {"q": "hidden computation", "source": "outside", "rarity": ["rare"]}
    assert "@" not in (await client.post("/api/bibtex", json=body)).text
    text = (await client.get("/api/bibtex", params={"q": "sparse"})).text
    assert "eprint = {2610.00002}" in text and "2610.00001" not in text
    outside = {"q": "hidden computation", "source": "outside"}
    assert "@" not in (await client.get("/api/bibtex", params=outside)).text


# -- a paper outside the collection ----------------------------------------------------------------


async def test_an_archive_paper_has_a_page_before_process_and_shows_fetched_after(
    conn, monkeypatch
):
    """Before Process, the archive's record makes the page (DESIGN.md: title, authors,
    abstract, links; no rarity); after it, the page says the paper is fetched and waits for
    the next run (#56). Neither the archive nor litwatch holding it, nor an archive that cannot
    answer, is a 404."""
    from litwatch.archive import ArchiveClient
    from litwatch.feedback import misses
    from tests.fakes_http import FakeArchive, mock

    await taxonomy_store.save(conn, TREE)
    dsn = f"{conn.test_dsn}?options=-csearch_path%3D{conn.test_schema}"
    fake = FakeArchive()
    pid = "2610.09999"
    item = {
        "id": pid,
        "title": "Unfetched",
        "abstract": "Only in the archive.",
        "authors": ["Ada Lovelace"],
        "categories": ["cs.CL"],
        "published": "2026-10-01",
        "updated": "2026-10-03",
        "version": 2,
        "version_history": [
            {"version": 1, "date": "2026-10-01"},
            {"version": 2, "date": "2026-10-03"},
        ],
    }
    fake.records[pid] = {"id": pid, "latest_version": 2, "metadata": {}, "item": item}
    fetched = []
    monkeypatch.setattr(
        misses,
        "fetch_by_ids",
        answering(lambda ids: fetched.append(ids) or [paper(pid, title="Unfetched")]),
    )
    down = ArchiveClient(
        "http://archive.test", "archive-token", http=mock(lambda _: httpx.Response(503))
    )
    archive = ArchiveClient("http://archive.test", "archive-token", http=mock(fake.handle))
    app = create_app(dsn, TOKEN, Config(), archive=archive)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t", headers=AUTH
    ) as c:
        page = (await c.get(f"/api/papers/{pid}")).json()
        assert (page["title"], page["authors"], page["abstract"]) == (
            "Unfetched",
            ["Ada Lovelace"],
            "Only in the archive.",
        )
        assert (page["stored"], page["in_collection"], page["rarity"], page["topics"]) == (
            False,
            False,
            None,
            [],
        )
        assert page["version"] == 2 and [v["version"] for v in page["versions"]] == [1, 2]
        assert page["published"].startswith("2026-10-01")
        assert page["pdf_url"] == f"/api/papers/{pid}/pdf" and not page["card_requestable"]
        assert (await c.get("/api/papers/2610.08888")).status_code == 404
        assert fetched == [] and await papers_store.get(conn, pid) is None

        assert (await c.post("/api/lookup", json={"q": pid})).json()["fetched"]
        page = (await c.get(f"/api/papers/{pid}")).json()
        assert (page["stored"], page["in_collection"], page["next_run"]) == (True, False, True)
        assert fetched == [[pid]]
        # Dismissed, it stays fetched and no run processes it (L6-7).
        assert await papers_store.dismiss(conn, pid)
        page = (await c.get(f"/api/papers/{pid}")).json()
        assert (page["stored"], page["next_run"]) == (True, False)
    for given in (None, down):
        app = create_app(dsn, TOKEN, Config(), archive=given)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t", headers=AUTH) as c:
            # No archive: the paper is unknown. An archive that cannot answer says nothing of
            # the paper: a 503, never the missing paper's 404 (L5-8).
            unknown = await c.get("/api/papers/2610.07777")
            if given is None:
                assert unknown.status_code == 404
            else:
                assert unknown.status_code == 503
                assert unknown.json()["detail"] == "The archive did not answer"
            assert (await c.get(f"/api/papers/{pid}")).json()["stored"]


# -- a paper's PDF ---------------------------------------------------------------------------------


async def hang(req: httpx.Request) -> httpx.Response:
    await asyncio.sleep(30)
    return httpx.Response(504)


@pytest.mark.parametrize("trickle", ["headers", "body"])
async def test_an_archive_paper_page_ends_at_its_deadline_however_the_archive_trickles(
    conn, monkeypatch, slow_arxiv, trickle
):
    """Bytes trickling in keep every socket read short: the page's own deadline ends the read,
    with the declared 503 (P5)."""
    from litwatch.api import reading as reading_api
    from litwatch.archive import ArchiveClient

    slow_arxiv.trickle = trickle  # a local socket that never finishes answering, any path
    monkeypatch.setattr(reading_api, "ARCHIVE_PAGE_BUDGET", 0.5)
    await taxonomy_store.save(conn, TREE)
    dsn = f"{conn.test_dsn}?options=-csearch_path%3D{conn.test_schema}"
    archive = ArchiveClient(f"http://127.0.0.1:{slow_arxiv.server_port}", "archive-token")
    app = create_app(dsn, TOKEN, Config(), archive=archive)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t", headers=AUTH
    ) as c:
        answer = await asyncio.wait_for(c.get("/api/papers/2610.09999"), 3)
    assert answer.status_code == 503


async def test_a_pdf_opened_is_asked_for_urgently_and_served_from_the_archive(conn, monkeypatch):
    """The storage policy keeps few PDFs; the others are fetched when the owner opens them.
    Without an archive, or when it cannot get the PDF in time, arXiv's PDF is the answer."""
    from litwatch.archive import ArchiveClient
    from tests.fakes_http import PDF, FakeArchive, mock

    await taxonomy_store.save(conn, TREE)
    held = P.model_copy(update={"version": 2})
    await papers_store.upsert(conn, [held])
    dsn = f"{conn.test_dsn}?options=-csearch_path%3D{conn.test_schema}"
    fake = FakeArchive()
    only = paper("2610.05555", title="Only in the archive", version=3)
    item = {**only.model_dump(mode="json"), "version_history": [{"version": 3}]}
    fake.records[only.id] = {"id": only.id, "latest_version": 3, "metadata": {}, "item": item}
    archive = ArchiveClient("http://archive.test", "archive-token", http=mock(fake.handle))
    archive.wait_seconds = 0
    for given in (archive, None):
        app = create_app(dsn, TOKEN, Config(), archive=given)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t", headers=AUTH) as c:
            page = (await c.get(f"/api/papers/{P.id}")).json()
            assert page["pdf_url"] == f"/api/papers/{P.id}/pdf"
            opened = await c.get(page["pdf_url"])
            unknown = await c.get("/api/papers/2610.09999/pdf")
            if given is not None:
                assert opened.status_code == 200 and opened.content == PDF
                assert opened.headers["content-type"] == "application/pdf"
                # A paper only the archive holds: its record names the version (L5-7).
                only = await c.get("/api/papers/2610.05555/pdf")
                assert only.status_code == 200 and only.content == PDF
                assert [(j["kind"], j["version"], j["priority"]) for j in fake.jobs] == [
                    ("pdf", 2, 0),
                    ("pdf", 3, 0),
                ]
                assert [j["paper_id"] for j in fake.jobs][1] == "2610.05555"
                fake.status = "unavailable"  # arXiv has no PDF for the archive to fetch
                opened = await c.get(page["pdf_url"])
            assert opened.status_code == 307
            assert opened.headers["location"] == f"https://arxiv.org/pdf/{P.id}"
            assert unknown.status_code == 307
            assert unknown.headers["location"] == "https://arxiv.org/pdf/2610.09999"
    # An archive that hangs costs the owner its wait at most, whichever call hangs (P5).
    hung_archive = ArchiveClient(
        "http://archive.test", "archive-token", http=mock(hang), wait_seconds=0.2
    )
    app = create_app(dsn, TOKEN, Config(), archive=hung_archive)
    transport = httpx.ASGITransport(app=app)
    monkeypatch.setattr("litwatch.api.reading.PDF_READ_SLACK", 0.3)
    async with httpx.AsyncClient(transport=transport, base_url="http://t", headers=AUTH) as c:
        for pid in (P.id, "2610.05555"):  # held, then known only by the archive's record
            hung = await asyncio.wait_for(c.get(f"/api/papers/{pid}/pdf"), 3)
            assert hung.status_code == 307, pid
