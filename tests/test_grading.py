"""Grading as a nightly step (pipeline.md, section 4): the graders each depth asks, what a
verdict records with its event, failures, and grading a stuck paper now. Models are scripted
through the real Gateway, as in test_steps."""

from contextlib import asynccontextmanager

import httpx
import pytest

from litwatch import grading
from litwatch.api import create_app
from litwatch.core import FAILED_RUNS, Fit
from litwatch.core.grading import FIT_DEFINITIONS
from litwatch.feedback import misses
from litwatch.llm import ProviderError
from litwatch.store import classifications as classifications_store
from litwatch.store import failures
from litwatch.store import papers as papers_store
from litwatch.store import taxonomy as taxonomy_store
from litwatch.store import verdicts as verdicts_store
from tests.conftest import FAITH, INTERP, TREE
from tests.test_steps import CONFIG, A, B, Scripted, decision, night, rows

TOKEN = "s3cret"


@pytest.fixture
async def models(conn):
    await taxonomy_store.save(conn, TREE)
    await papers_store.upsert(conn, [A, B])
    return Scripted()


def fit(value: str, reason: str = "Because.") -> dict:
    return {"reason": reason, "fit": value}


async def verdicts(conn, paper_id: str) -> list[dict]:
    return await rows(conn, "SELECT * FROM verdicts WHERE paper_id = %s ORDER BY id", paper_id)


def test_every_grader_is_given_the_fit_definitions_word_for_word():
    for meaning in FIT_DEFINITIONS.values():
        assert meaning in grading.SYSTEM and meaning in grading.AREA_CHECK


async def test_a_focus_topic_is_graded_by_both_labs_and_the_more_inclusive_fit_stands(conn, models):
    # B matches CoT faithfulness's keyword: graded for that Focus topic alone.
    models.fits[("judge", B.title)] = fit("central")
    models.fits[("check", B.title)] = fit("adjacent")
    counts = await night(conn, models)
    assert counts["graded"] == 1
    (v,) = await verdicts(conn, B.id)
    assert (v["topic"], v["fit"], v["split"], v["escalated"]) == (
        FAITH.slug,
        "central",
        True,
        False,
    )
    assert (v["topic_version"], v["paper_revision"]) == (FAITH.version, 1)
    assert {r: o["fit"] for r, o in v["opinions"].items()} == {
        "judge": "central",
        "check": "adjacent",
    }
    assert v["opinions"]["check"]["model"] == "openrouter:test/check"
    (event,) = await rows(conn, "SELECT payload FROM outbox WHERE type = 'verdict.recorded'")
    assert event["payload"] == {
        "topic": FAITH.slug,
        "revision": 1,
        "verdict_id": v["id"],
        "fit": "central",
        "weight": "solid",
        "grade": "must_read",
        "split": True,
        "escalated": False,
    }
    # The verdict settles the paper in the same run: its effective grade, rarity and library.
    (g,) = await rows(conn, "SELECT * FROM effective_grades WHERE paper_id = %s", B.id)
    assert (g["fit"], g["grade"], g["rarity"]) == ("central", "must_read", "epic")
    (saved,) = await rows(conn, "SELECT saved FROM library WHERE paper_id = %s", B.id)
    assert saved["saved"] is True
    # Subject for every topic, the rubric for a Focus topic.
    asked = next(u for u in models.users if u.startswith("Topic: ") and B.title in u)
    assert FAITH.subject in asked and FAITH.rubric in asked
    await night(conn, models)
    assert len(await verdicts(conn, B.id)) == 1  # graded once per revision


@pytest.mark.parametrize(
    ("holds", "escalated"),
    [(0.9, False), (0.6, True), (0.3, True)],  # sure; a doubtful yes; a no
)
async def test_an_area_topic_escalates_to_check_on_doubt_and_the_less_inclusive_fit_stands(
    conn, models, holds, escalated
):
    models.decisions[A.title] = decision("other", interpretability=0.9)
    models.fits[("judge", A.title)] = fit("central")
    models.fits[("area_check", A.title)] = {"holds": holds}
    models.fits[("check", A.title)] = fit("adjacent")
    await night(conn, models)
    (v,) = await verdicts(conn, A.id)
    assert (v["topic"], v["escalated"], v["split"]) == (INTERP.slug, escalated, escalated)
    assert v["fit"] == ("adjacent" if escalated else "central")
    assert v["opinions"]["area_check"]["probability"] == holds
    assert (("check", A.title) in models.asked) is escalated
    asked = next(u for u in models.users if u.startswith("Topic: ") and A.title in u)
    assert INTERP.subject in asked and "Rubric:" not in asked
    # An Area grade never saves.
    assert await rows(conn, "SELECT 1 FROM library WHERE saved AND paper_id = %s", A.id) == []


async def test_a_classified_paper_has_a_rarity_whatever_else_fails(conn, models):
    models.analyses[A.title] = {"gist": "Too short."}  # invalid: no Weight
    await night(conn, models)  # A leads to no topic either
    (rarity,) = await rows(conn, "SELECT * FROM rarities WHERE paper_id = %s", A.id)
    assert (rarity["level"], rarity["best_topic"]) == ("uncommon", None)  # solid's floor


async def test_a_field_topic_is_graded_by_field_grade_alone(conn, models):
    models.decisions[A.title] = decision("agents", agents=0.9)
    await night(conn, models)
    (v,) = await verdicts(conn, A.id)
    assert (v["topic"], list(v["opinions"]), v["split"]) == ("agents", ["field_grade"], False)
    assert ("judge", A.title) not in models.asked


async def test_a_rewritten_paper_is_graded_again_where_it_held_a_grade(conn, models):
    models.decisions[A.title] = decision("other", interpretability=0.9)
    await night(conn, models)
    rewritten = A.model_copy(update={"abstract": "An entirely different abstract about robots."})
    await papers_store.upsert(conn, [rewritten])
    models.decisions[A.title] = decision("other")  # no longer under Interpretability
    await night(conn, models)
    assert [(v["paper_revision"], v["topic"]) for v in await verdicts(conn, A.id)] == [
        (1, INTERP.slug),
        (2, INTERP.slug),
    ]


async def test_a_rewrite_leaves_a_topic_that_held_none(conn, models):
    models.decisions[A.title] = decision("other", interpretability=0.9)
    models.fits[("judge", A.title)] = fit("none")
    await night(conn, models)
    await papers_store.upsert(conn, [A.model_copy(update={"abstract": "Something else."})])
    models.decisions[A.title] = decision("other")
    await night(conn, models)
    assert [v["paper_revision"] for v in await verdicts(conn, A.id)] == [1]


async def test_a_topic_followed_later_grades_nothing_until_a_rerun_but_a_miss_does(conn, models):
    await night(conn, models)  # A leads to no topic; its pass is done all the same
    assert await verdicts(conn, A.id) == []
    models.decisions[A.title] = decision("other", interpretability=0.9)
    await classifications_store.put_owner(
        conn, A, "interpretability", True, TREE.get("interpretability").version
    )  # now under a followed topic, as if a class had moved
    await night(conn, models)
    assert await verdicts(conn, A.id) == []
    await misses.report(conn, A.id, INTERP.slug, Fit.RELATED)
    await night(conn, models)
    assert [v["topic"] for v in await verdicts(conn, A.id)] == [INTERP.slug]
    assert await verdicts_store.candidates(conn, FAILED_RUNS) == []  # the miss is done


async def test_the_limit_caps_papers_not_gradings(conn, models):
    for p in (A, B):
        models.decisions[p.title] = decision("other", interpretability=0.9)
        models.fits[("judge", p.title)] = ProviderError("APITimeoutError", timeout=True)
    await night(conn, models)  # classified; every grading left to try again
    assert len(await verdicts_store.candidates(conn, FAILED_RUNS, limit=1)) == 1
    tree = await taxonomy_store.load(conn)
    due, _ = await grading.due(conn, tree, FAILED_RUNS, limit=1)
    assert [(d.paper.id, d.topic.slug) for d in due] == [(A.id, INTERP.slug)]
    due, _ = await grading.due(conn, tree, FAILED_RUNS)
    assert {(d.paper.id, d.topic.slug) for d in due} == {
        (A.id, INTERP.slug),
        (B.id, INTERP.slug),
        (B.id, FAITH.slug),
    }


@pytest.mark.parametrize(
    "error",
    [ProviderError("APITimeoutError", timeout=True), ProviderError("AuthenticationError: no key")],
)
async def test_a_grader_that_did_not_answer_is_retried_without_counting(conn, models, error):
    models.fits[("check", B.title)] = error
    for _ in range(FAILED_RUNS + 1):
        models.asked.clear()
        assert (await night(conn, models))["grading_failed"] == 1
        assert ("check", B.title) in models.asked
    (failure,) = await rows(conn, "SELECT * FROM step_failures WHERE step = 'grading'")
    assert (failure["topic"], failure["failed_runs"]) == (FAITH.slug, 0)
    assert await verdicts(conn, B.id) == []


@asynccontextmanager
async def api(conn, models):
    dsn = f"{conn.test_dsn}?options=-csearch_path%3D{conn.test_schema}"
    app = create_app(dsn, TOKEN, CONFIG, gateway=models.gateway)
    transport = httpx.ASGITransport(app=app)
    headers = {"authorization": f"Bearer {TOKEN}"}
    async with httpx.AsyncClient(transport=transport, base_url="http://t", headers=headers) as c:
        yield c


async def stuck(conn, models) -> None:
    """B's grading for its Focus topic failed three runs in a row: an unusable judge."""
    models.fits[("judge", B.title)] = fit("very central")
    for n in range(1, FAILED_RUNS + 1):
        assert (await night(conn, models))["grading_failed"] == 1
        (failure,) = await rows(conn, "SELECT * FROM step_failures WHERE step = 'grading'")
        assert failure["failed_runs"] == n and failure["last_error"].startswith("invalid")
    del models.fits[("judge", B.title)]


async def test_an_unusable_answer_is_no_verdict_and_three_leave_it_for_the_owner(conn, models):
    # One grader answering unusably is enough: the other's answer alone is no verdict.
    await stuck(conn, models)
    assert await verdicts(conn, B.id) == []
    assert await verdicts_store.candidates(conn, FAILED_RUNS) == []
    models.asked.clear()
    await night(conn, models)
    assert ("judge", B.title) not in models.asked  # it waits for the owner now

    async with api(conn, models) as client:
        status = (await client.get("/api/status")).json()
        assert status["state"] == "attention" and status["ungraded"] == 1
        quality = (await client.get("/api/settings/quality")).json()
        (listed,) = quality["night"]["ungraded"]
        assert (listed["paper_id"], listed["topic"], listed["revision"]) == (B.id, FAITH.slug, 1)

        url = "/api/settings/grading/{}/{}/now"
        now = url.format(FAITH.slug, B.id)
        assert (await client.post(now, json={"revision": 2})).status_code == 409
        elsewhere = url.format(INTERP.slug, B.id)
        assert (await client.post(elsewhere, json={"revision": 1})).status_code == 409
        unknown = url.format("nope", B.id)
        assert (await client.post(unknown, json={"revision": 1})).status_code == 404

        done = await client.post(now, json={"revision": 1})
        assert done.json() == {"graded": True, "fit": "related", "error": None}
        assert (await client.post(now, json={"revision": 1})).status_code == 409  # done
    (v,) = await verdicts(conn, B.id)
    assert v["fit"] == "related"
    assert await failures.stuck(conn, "grading", FAILED_RUNS) == []


NOW = f"/api/settings/grading/{FAITH.slug}/{B.id}/now"


async def test_grading_now_cut_short_is_left_to_the_next_night(conn, models, monkeypatch):
    await stuck(conn, models)

    async def crash(conn, g):
        raise RuntimeError("the request went away")

    monkeypatch.setattr(grading, "record", crash)
    async with api(conn, models) as client:
        with pytest.raises(RuntimeError):
            await client.post(NOW, json={"revision": 1})
    monkeypatch.undo()
    (failure,) = await rows(conn, "SELECT * FROM step_failures WHERE step = 'grading'")
    assert failure["failed_runs"] == 0  # pending, with three runs to go
    assert (await night(conn, models))["graded"] == 1
    assert [v["fit"] for v in await verdicts(conn, B.id)] == ["related"]


async def test_grading_now_on_text_that_changed_meanwhile_is_refused(conn, models, monkeypatch):
    await stuck(conn, models)

    async def rewritten(conn, paper):
        return False

    monkeypatch.setattr(papers_store, "still_current", rewritten)
    async with api(conn, models) as client:
        assert (await client.post(NOW, json={"revision": 1})).status_code == 409
    monkeypatch.undo()
    assert await verdicts(conn, B.id) == []
    (run,) = await rows(conn, "SELECT counts FROM runs WHERE kind = 'grading'")
    assert "graded" not in run["counts"] and "grading_failed" not in run["counts"]
