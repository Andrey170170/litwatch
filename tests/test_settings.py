"""Settings: the taxonomy and editing topics, Quality, the schedule and configuration."""

import httpx
import pytest

from litwatch.api import create_app
from litwatch.config import Config
from litwatch.core import Cause, Fit, Opinion, Verdict
from litwatch.feedback import word
from litwatch.store import papers as papers_store
from litwatch.store import taxonomy as taxonomy_store
from litwatch.store import verdicts
from tests.conftest import AGENTS, FAITH, INTERP, TREE, paper

TOKEN = "s3cret"
P = paper("2610.00001", title="Faithful traces", abstract="Chain-of-thought omits steps.")
Q = paper("2610.00002", title="Protein folding", abstract="Structures.")


@pytest.fixture
async def client(conn):
    await taxonomy_store.save(conn, TREE)
    await papers_store.upsert(conn, [P, Q])
    dsn = f"{conn.test_dsn}?options=-csearch_path%3D{conn.test_schema}"
    app = create_app(dsn, TOKEN, Config())
    transport = httpx.ASGITransport(app=app)
    headers = {"authorization": f"Bearer {TOKEN}"}
    async with httpx.AsyncClient(transport=transport, base_url="http://t", headers=headers) as c:
        yield c


def _body(topic) -> dict:
    return topic.model_dump(mode="json", exclude={"slug"})


async def test_the_taxonomy_reads_back_as_saved(client):
    assert (await client.get("/api/settings/taxonomy")).json() == TREE.model_dump(
        mode="json", by_alias=True
    )


async def test_a_topic_edit_applies_forward_and_a_depth_change_settles_its_papers(client, conn):
    async def revision() -> int:
        row = await (
            await conn.execute("SELECT revision FROM topics WHERE class = %s", (FAITH.slug,))
        ).fetchone()
        return row["revision"]

    body = _body(FAITH)
    url = f"/api/settings/topics/{FAITH.slug}"
    saved = await client.put(url, json=body | {"colour": "#000000"})
    assert saved.json()["colour"] == "#000000" and await revision() == 1  # nothing graded changed
    body["subject"] = "A subject reworded: the class's description follows it."
    assert (await client.put(f"/api/settings/topics/{FAITH.slug}", json=body)).status_code == 200
    assert await revision() == 2
    tree = (await client.get("/api/settings/taxonomy")).json()
    (cls,) = [c for c in tree["classes"] if c["slug"] == FAITH.slug]
    assert cls["description"] == body["subject"]
    bad = await client.put(f"/api/settings/topics/{FAITH.slug}", json=body | {"subject": "short"})
    assert bad.status_code == 422
    nowhere = await client.put("/api/settings/topics/no-such-class", json=body)
    assert nowhere.status_code == 404

    await verdicts.add(
        conn,
        Verdict(
            paper_id=P.id, topic=FAITH.slug, topic_version="v", fit=Fit.RELATED, reason="r",
            opinions={},
        ),
    )  # fmt: skip
    assert (await word.settle(conn, P.id, cause=Cause.VERDICT)).saved
    area = body | {"depth": "area", "rubric": None}
    assert (await client.put(f"/api/settings/topics/{FAITH.slug}", json=area)).status_code == 200
    row = await (await conn.execute("SELECT saved FROM library")).fetchone()
    assert row["saved"] is False


async def test_a_class_not_followed_can_be_followed(client):
    other = {"depth": "field", "monogram": "OT", "colour": "#111111"}
    other["subject"] = "Papers in the configured categories that fit nothing else."
    assert (await client.put("/api/settings/topics/other", json=other)).status_code == 200
    tree = (await client.get("/api/settings/taxonomy")).json()
    assert [t["class"] for t in tree["topics"]] == [*(t.slug for t in TREE.topics), "other"]


async def test_the_owners_order(client):
    order = ["agents", "interpretability", FAITH.slug]
    assert (
        await client.put("/api/settings/topics-order", json={"slugs": order})
    ).status_code == 204
    topics = (await client.get("/api/settings/taxonomy")).json()["topics"]
    assert [t["class"] for t in topics] == order


async def test_the_schedule_says_when_each_job_next_fires_in_its_zone(conn):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from litwatch.config import Schedule

    cfg = Config(schedule=Schedule(nightly="0 6 * * *", timezone="America/Denver"))
    dsn = f"{conn.test_dsn}?options=-csearch_path%3D{conn.test_schema}"
    transport = httpx.ASGITransport(app=create_app(dsn, TOKEN, cfg))
    headers = {"authorization": f"Bearer {TOKEN}"}
    async with httpx.AsyncClient(transport=transport, base_url="http://t", headers=headers) as c:
        out = (await c.get("/api/settings/schedule")).json()
    assert out["enabled"] is False and out["zone"] == "America/Denver"
    by_kind = {j["kind"]: j for j in out["jobs"]}
    assert set(by_kind) == {"nightly", "publications"}
    assert by_kind["nightly"]["cron"] == "0 6 * * *"
    denver = ZoneInfo("America/Denver")
    at = datetime.fromisoformat(by_kind["nightly"]["next"])
    assert at.tzinfo is not None and at > datetime.now(denver)
    local = at.astimezone(denver)
    assert (local.hour, local.minute) == (6, 0)


async def test_quality_lists_last_nights_splits_and_escalations_and_hides_secrets(client, conn):
    await conn.execute("INSERT INTO runs (kind, finished_at) VALUES ('nightly', now())")
    judge = Opinion(fit=Fit.CENTRAL, reason="yes", model="a")
    disagrees = {"judge": judge, "check": Opinion(fit=Fit.NONE, reason="no", model="b")}
    agrees = {"judge": judge, "check": Opinion(fit=Fit.CENTRAL, reason="yes", model="b")}
    # An Area escalation asks only when its graders disagreed: one where they agreed is not listed.
    marks = {
        FAITH.slug: {"split": True, "opinions": disagrees},
        "interpretability": {"split": True, "escalated": True, "opinions": disagrees},
        "agents": {"escalated": True, "opinions": agrees},
    }
    for topic, marked in marks.items():
        await verdicts.add(
            conn,
            Verdict(
                paper_id=P.id,
                topic=topic,
                topic_version="v",
                fit=Fit.CENTRAL,
                reason="yes",
                **marked,
            ),
        )
    page = (await client.get("/api/settings/quality")).json()
    split, escalated = sorted(page["night"]["splits"], key=lambda r: r["topic"])
    assert (split["topic"], split["opinions"]["check"]["fit"]) == (FAITH.slug, "none")
    assert (escalated["topic"], escalated["depth"]) == ("interpretability", "area")
    assert [c["provider"] for c in page["cost"]] == ["anthropic", "openrouter"]
    config = (await client.get("/api/settings/config")).json()
    assert config["models"]["writer"]["model"] == "claude-opus-5-5"


def test_configuration_masks_what_a_url_may_carry():
    from litwatch.api.settings import _mask

    shown = _mask({"integrations": {"hook": {"url": "https://n8n.test/webhook/s3cret?k=1"}}})
    assert shown["integrations"]["hook"]["url"] == "https://n8n.test/••••"
    assert _mask({"archive_url": "http://archive:8787"}) == {"archive_url": "http://archive:8787"}
    assert _mask({"url": "https://u:p@dav.test"}) == {"url": "https://dav.test/••••"}
    # A limit whose name carries a secret word is a number to show, not a credential.
    limits = {"max_output_tokens": 4096, "max_tokens": 10, "api_key": "k", "token_file": "f"}
    assert _mask({"judge": limits}) == {
        "judge": {
            "max_output_tokens": 4096,
            "max_tokens": 10,
            "api_key": "••••",
            "token_file": "••••",
        }
    }


def test_the_schema_declares_the_settings_errors_a_page_can_meet():
    """The client's types come from the schema: every error a Settings action can answer with
    is declared on its route."""
    paths = create_app("postgresql://x/y", TOKEN, Config()).openapi()["paths"]
    declared = {
        ("/api/settings/topics/{slug}", "put"): {"404"},
        ("/api/settings/calibrate/{session_id}", "get"): {"404"},
        ("/api/settings/calibrate/{session_id}/more", "post"): {"404"},
        ("/api/settings/calibrate/{session_id}/items/{position}", "put"): {"404"},
        ("/api/settings/integrations/{name}/retry", "post"): {"404", "409"},
        ("/api/settings/integrations/{name}/skip", "post"): {"404", "409"},
    }
    for (path, method), codes in declared.items():
        assert codes <= set(paths[path][method]["responses"]), (path, method)


async def test_bad_input_is_refused_not_crashed(client):
    body = _body(FAITH) | {"keywords": ["["]}
    assert (await client.put(f"/api/settings/topics/{FAITH.slug}", json=body)).status_code == 422
    order = {"slugs": ["agents", "agents", FAITH.slug]}
    assert (await client.put("/api/settings/topics-order", json=order)).status_code == 422


async def test_settings_refuse_what_does_not_exist(client):
    assert (await client.post("/api/settings/calibrate/999/more")).status_code == 404


async def _classify(conn, paper, probabilities, primary):
    """Record a classification as the nightly step would, with the memberships it makes."""
    from litwatch import classification
    from litwatch.core.classification import Classified, memberships

    found = Classified(probabilities=probabilities, primary=primary)
    c = classification.Classification(
        paper=paper,
        taxonomy_revision=TREE.classes_version,
        versions={k.slug: k.version for k in TREE.classes},
        classified=found,
        memberships=memberships(TREE, found, paper.title, paper.abstract),
        response={"probabilities": probabilities},
        model="m",
    )
    assert await classification.record(conn, c)


async def test_a_miss_carries_what_each_stage_concluded(client, conn):
    from litwatch.core import Reaction, ReactionReason, ReactionValue
    from litwatch.feedback import misses

    # Reported before any stage ran: every cell pending.
    await misses.report(conn, Q.id, FAITH.slug)
    (m,) = (await client.get("/api/settings/misses")).json()
    assert (m["paper_id"], m["topic"], m["trace"]["classified"]) == (Q.id, FAITH.slug, False)
    assert m["trace"]["classifier"] is None and m["trace"]["graded"] is False
    # Classified by hand from Needs you: the owner's word, not the classifier's; still pending.
    from litwatch import classification

    assert await classification.by_hand(conn, Q, TREE, [FAITH.slug])
    (m,) = (await client.get("/api/settings/misses")).json()
    assert m["trace"]["classified"] is False and m["trace"]["probability"] is None
    # Classified under the Focus threshold, kept in by the band; graded by both labs.
    await _classify(conn, Q, {FAITH.slug: 0.3, "other": 0.9}, "other")
    await verdicts.add(
        conn,
        Verdict(
            paper_id=Q.id,
            topic=FAITH.slug,
            topic_version="v",
            fit=Fit.RELATED,
            reason="r",
            opinions={
                "judge": Opinion(fit=Fit.RELATED, reason="y", model="a"),
                "check": Opinion(fit=Fit.ADJACENT, reason="n", model="b"),
            },
        ),
    )
    (m,) = (await client.get("/api/settings/misses")).json()
    t = m["trace"]
    assert t["classified"] and t["probability"] == 0.3 and t["threshold"] == 0.5
    assert (t["classifier"], t["uncertain"], t["keyword"]) == (False, True, False)
    assert t["below"] is False
    assert (t["graded"], t["fit"]) == (True, "related")
    assert [(g["role"], g["fit"]) for g in t["graders"]] == [
        ("judge", "related"),
        ("check", "adjacent"),
    ]
    # A miss on the parent: the classifier's membership in the Focus class stands in it too.
    from litwatch.feedback import misses as misses_store

    await misses_store.report(conn, P.id, "interpretability")
    await _classify(conn, P, {FAITH.slug: 0.9, "interpretability": 0.1}, FAITH.slug)
    found = (await client.get("/api/settings/misses")).json()
    (parent,) = [x for x in found if x["topic"] == "interpretability"]
    assert parent["trace"]["classifier"] is True and parent["trace"]["probability"] == 0.1
    assert parent["trace"]["below"] is True  # in a class under it, not in its own
    # The classifier choosing the topic's own class, under its threshold, is no class under it.
    await _classify(conn, P, {FAITH.slug: 0.1, "interpretability": 0.1}, "interpretability")
    found = (await client.get("/api/settings/misses")).json()
    (parent,) = [x for x in found if x["topic"] == "interpretability"]
    assert (parent["trace"]["classifier"], parent["trace"]["below"]) == (True, False)
    # The band admits a paper to one Focus class for grading, not to the classes above it.
    await misses_store.report(conn, Q.id, "interpretability")
    found = (await client.get("/api/settings/misses")).json()
    (band,) = [x for x in found if x["topic"] == "interpretability" and x["paper_id"] == Q.id]
    assert (band["trace"]["classifier"], band["trace"]["uncertain"]) == (False, False)

    # Reactions over 30 days per topic and reason, for Overview.
    await word.react(
        conn,
        Reaction(
            paper_id=Q.id,
            topic=FAITH.slug,
            value=ReactionValue.DOWN,
            reason=ReactionReason.OVERRATED,
        ),
    )
    await word.react(
        conn, Reaction(paper_id=Q.id, topic="agents", value=ReactionValue.UP, reason=None)
    )
    page = (await client.get("/api/settings/quality")).json()
    # In the owner's topic order (agents comes first alphabetically, last in the taxonomy).
    assert page["reactions"] == [
        {"topic": FAITH.slug, "reason": "overrated", "n": 1},
        {"topic": "agents", "reason": None, "n": 1},
    ]


async def test_taxonomy_stats_and_export(client, conn):
    from datetime import UTC, datetime

    recent = P.model_copy(update={"published": datetime.now(UTC).isoformat()})
    await papers_store.upsert(conn, [recent])  # the keyword window is the last 90 days
    await _classify(conn, recent, {FAITH.slug: 0.9}, FAITH.slug)
    stats = (await client.get("/api/settings/taxonomy/stats")).json()
    # A member of the Focus class stands in its parent too.
    assert stats["members"] == {FAITH.slug: 1, "interpretability": 1}
    assert stats["topic_revisions"][FAITH.slug] == 1
    # Each class at its own threshold, or the configured one for its depth (the test config: 0.8
    # by default, 0.5 for Focus).
    assert (stats["thresholds"][FAITH.slug], stats["thresholds"]["other"]) == (0.5, 0.8)
    kw = await client.get(f"/api/settings/topics/{FAITH.slug}/keywords")
    assert kw.json() == {r"chain.of.thought": 1, r"\bCoT\b": 0, "faithful": 1}
    assert (await client.get("/api/settings/topics/other/keywords")).status_code == 404
    text = (await client.get("/api/settings/taxonomy.yaml")).text
    assert text.startswith("classes:") and f"class: {FAITH.slug}" in text


async def test_a_miss_reads_the_verdict_as_its_depth_graded_it(client, conn):
    """A Field topic is graded by `field_grade` alone, an Area topic by the judge with the check
    on doubt: the trace carries the graders asked and the Fit that stands by that depth's rule,
    not a Focus pair read as pending."""
    from litwatch.feedback import misses

    await misses.report(conn, Q.id, AGENTS.slug)
    await misses.report(conn, Q.id, INTERP.slug)
    await verdicts.add(
        conn,
        Verdict(
            paper_id=Q.id,
            topic=AGENTS.slug,
            topic_version="v",
            fit=Fit.CENTRAL,
            reason="r",
            opinions={"field_grade": Opinion(fit=Fit.CENTRAL, reason="y", model="a")},
        ),
    )
    await verdicts.add(
        conn,
        Verdict(
            paper_id=Q.id,
            topic=INTERP.slug,
            topic_version="v",
            fit=Fit.NONE,
            reason="r",
            escalated=True,
            opinions={
                "judge": Opinion(fit=Fit.CENTRAL, reason="y", model="a"),
                "area_check": Opinion(model="d", holds=False, probability=0.2),
                "check": Opinion(fit=Fit.NONE, reason="n", model="b"),
            },
        ),
    )
    found = {m["topic"]: m["trace"] for m in (await client.get("/api/settings/misses")).json()}
    field = found[AGENTS.slug]
    assert (field["graded"], field["fit"]) == (True, "central")
    assert [(g["role"], g["fit"]) for g in field["graders"]] == [("field_grade", "central")]
    area = found[INTERP.slug]
    assert (area["graded"], area["fit"]) == (True, "none")
    assert [(g["role"], g["fit"], g["holds"]) for g in area["graders"]] == [
        ("judge", "central", None),
        ("area_check", None, False),
        ("check", "none", None),
    ]


async def test_a_split_on_an_older_text_no_longer_waits(client, conn):
    """A paper rewritten since its split (a new revision) is graded again by the next run: the
    old split is not the owner's to settle, on Needs you or in the sidebar's count."""
    await conn.execute("INSERT INTO runs (kind, finished_at) VALUES ('nightly', now())")
    opinions = {
        "judge": Opinion(fit=Fit.CENTRAL, reason="yes", model="a"),
        "check": Opinion(fit=Fit.NONE, reason="no", model="b"),
    }
    await verdicts.add(
        conn,
        Verdict(
            paper_id=P.id,
            topic=FAITH.slug,
            topic_version="v",
            fit=Fit.CENTRAL,
            reason="yes",
            opinions=opinions,
            split=True,
        ),
    )
    page = (await client.get("/api/settings/quality")).json()
    assert [(r["paper_id"], r["revision"]) for r in page["night"]["splits"]] == [(P.id, 1)]
    assert (await client.get("/api/summary")).json()["settings"]["waiting"] == 1
    rewritten = paper(
        P.id, title=P.title, abstract="A new abstract on wholly other steps.", version=2
    )
    await papers_store.upsert(conn, [rewritten])
    assert await papers_store.revision(conn, P.id) == 2
    page = (await client.get("/api/settings/quality")).json()
    assert page["night"]["splits"] == []
    assert (await client.get("/api/summary")).json()["settings"]["waiting"] == 0


async def test_the_summary_carries_the_status_line_the_badges_and_the_hints(client, conn):
    """One read for the shell: the run status, the shown topics in the fixed order as badges, and
    the Settings sidebar's counts, which follow the misses reported and the digests that failed."""
    before = (await client.get("/api/summary")).json()
    assert before["status"]["state"] == "none"
    assert [t["slug"] for t in before["topics"]] == [t.slug for t in TREE.shown()]
    hints = before["settings"]
    assert isinstance(hints.pop("budget"), float | None)  # the configured budgets' sum
    assert hints == {
        "waiting": 0,
        "last_session": None,
        "misses": 0,
        "topics": len(TREE.topics),
        "classes": len(TREE.classes),
        "spent": 0.0,
        "failing": [],
    }
    assert (
        await client.post("/api/misses", json={"paper_id": P.id, "topic": FAITH.slug})
    ).status_code in (200, 201)
    await conn.execute(
        "INSERT INTO digests (topic, period, day, state, error) "
        "VALUES (%s, 'day', '2026-10-08', 'failed', 'timed out')",
        (FAITH.slug,),
    )
    # A digest written by hand can fail before the first night: it waits all the same.
    after = (await client.get("/api/summary")).json()
    assert (after["status"]["state"], after["status"]["digests_failed"]) == ("none", 1)
    assert (after["settings"]["misses"], after["settings"]["waiting"]) == (1, 1)
    await conn.execute(
        "INSERT INTO runs (kind, started_at, finished_at) VALUES ('nightly', now(), now())"
    )
    after = (await client.get("/api/summary")).json()
    assert (after["status"]["state"], after["status"]["digests_failed"]) == ("attention", 1)
    assert after["settings"]["waiting"] == 1
