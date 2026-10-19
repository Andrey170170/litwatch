"""The fallback replay (docs/plans/failure-handling.md, P1): ladders and cases as files, each
case's request built as the pipeline builds it, calls under a ceiling that holds across runs,
resuming, and the report's tables. Providers are scripted."""

import json

import pytest

from litwatch import analysis, grading
from litwatch.archive import ArchiveClient
from litwatch.config import RoleConfig
from litwatch.fulltext import brief
from litwatch.llm import ProviderError, Reply, Request, Stop, Usage
from litwatch.quality import ladders
from litwatch.store import papers as papers_store
from tests.conftest import FAITH, INTERP, TREE, paper
from tests.fakes import ScriptedProvider
from tests.fakes_http import FakeArchive, mock
from tests.test_digests import TUE, Writer, day, focus, listed

LADDERS = """
[judge]
output_tokens = 100
reserve_output_tokens = 1000
[[judge.candidates]]
name = "own"
price = [10.0, 10.0]
provider = "anthropic"
model = "lab-a/own"
[[judge.candidates]]
name = "other"
price = [1.0, 1.0]
provider = "openrouter"
model = "lab-b/other"
effort = "high"
"""
P = paper("2610.00001", title="Probes for hidden steps", abstract="Probes find hidden steps.")
Q = paper("2610.00002", title="Monitors under pressure", abstract="Monitors fail under pressure.")
FIT = grading.Output(reason="It is about the topic.", fit="central")


def ladder(tmp_path, text: str = LADDERS) -> dict[str, ladders.Ladder]:
    path = tmp_path / "ladders.toml"
    path.write_text(text)
    return ladders.ladders(path)


def judged(*papers, label: str | None = None) -> list[ladders.Case]:
    return [
        ladders.Case(set="quality", role="judge", paper=p.id, subject=FAITH.slug, label=label)
        for p in papers
    ]


def todo(found, chosen, done=()) -> list[ladders.Todo]:
    by_id = {p.id: p for p in (P, Q)}
    built = {c.key: grading.request(by_id[c.paper], FAITH, "CoT") for c in chosen}
    return ladders.todo(chosen, found, built, list(done))


def test_ladders_and_cases_load_from_their_files(tmp_path):
    found = ladder(tmp_path)
    assert [c.name for c in found["judge"].candidates] == ["own", "other"]
    assert found["judge"].own.role == RoleConfig(provider="anthropic", model="lab-a/own")
    assert found["judge"].candidates[1].role.effort == "high"
    with pytest.raises(ValueError, match="asked alone"):
        ladder(tmp_path, LADDERS + 'fallback = "brief_fallback"\n')
    cases = tmp_path / "cases.csv"
    cases.write_text(
        "set,role,paper,subject,label,note\n"
        "quality,judge,2610.00001,cot-faithfulness,central,\n"
        "refusals,writer,,12,,a digest\n"
    )
    first, second = ladders.cases(cases)
    assert first.label == "central" and first.note is None
    assert second.paper is None and second.key == "writer//12"
    cases.write_text(cases.read_text() + "quality,judge,2610.00001,cot-faithfulness,none,\n")
    with pytest.raises(ValueError, match="listed twice"):
        ladders.cases(cases)


def test_the_budget_reserves_before_a_call_and_once_refused_starts_none():
    b = ladders.Budget(ceiling=1.0, spent=0.5)
    assert b.reserve(0.3) and not b.reserve(0.3)
    assert not b.reserve(0.01)  # stopped: a later, smaller call does not slip in
    b.settle(0.3, None)  # an unknown charge counts at its reservation
    assert b.spent == pytest.approx(0.8) and b.reserved == 0


async def test_a_run_asks_every_candidate_alone_and_resumes_what_failed_for_want_of_a_provider(
    tmp_path,
):
    found, chosen, out = ladder(tmp_path), judged(P, Q), tmp_path / "runs"
    down = {"other": True}

    def own(role, request):
        assert role.fallback is None
        if "Monitors" in request.user:
            return Reply("", Stop.REFUSED, Usage(model=role.model, cost=0.01), refusal="bio")
        return Reply(FIT.model_dump_json(), Stop.COMPLETE, Usage(model=role.model, cost=0.02))

    def other(role, request):
        if down["other"]:
            raise ProviderError("the completion has no choices")
        return FIT.model_dump_json()

    providers = {
        "anthropic": ScriptedProvider("anthropic", own),
        "openrouter": ScriptedProvider("openrouter", other),
    }
    ran = await ladders.run(todo(found, chosen), providers, out, ceiling=5.0)
    outcomes = {(r.case.split("/")[1], r.candidate): r.outcome for r in ran.rows}
    assert outcomes == {
        (P.id, "own"): "answered",
        (Q.id, "own"): "refused",
        (P.id, "other"): "provider",
        (Q.id, "other"): "provider",
    }
    refused = next(r for r in ran.rows if r.outcome == "refused")
    assert (
        refused.cost == 0.01 and refused.detail == "bio" and refused.model == "anthropic:lab-a/own"
    )
    # Asked again: only what failed for want of the provider; a refusal is the model's answer.
    down["other"] = False
    left = todo(found, chosen, ladders.rows(out))
    assert [(t.case.paper, t.candidate.name) for t in left] == [(P.id, "other"), (Q.id, "other")]
    await ladders.run(left, providers, out, ceiling=5.0)
    assert todo(found, chosen, ladders.rows(out)) == []
    last = ladders.latest(ladders.rows(out))
    assert last[(f"judge/{Q.id}/{FAITH.slug}", "other")].output == FIT.model_dump(mode="json")
    # Another request, or another configuration of a candidate, is asked afresh.
    moved = [c.model_copy(update={"subject": INTERP.slug}) for c in chosen]
    built = {c.key: grading.request(paper(c.paper), INTERP, "Interp") for c in moved}
    assert len(ladders.todo(moved, found, built, ladders.rows(out))) == 4


async def test_the_ceiling_holds_across_runs_and_records_where_it_stopped(tmp_path):
    found, out = ladder(tmp_path), tmp_path / "runs"
    work = todo(found, judged(P, Q), [])
    providers = {
        "anthropic": ScriptedProvider(
            "anthropic", lambda role, request: Reply("{", Stop.COMPLETE, Usage(model=role.model))
        ),
        "openrouter": ScriptedProvider("openrouter", lambda role, request: FIT.model_dump_json()),
    }
    # Each own call reserves its input at half a token a character and 1,000 output tokens.
    hold = ladders.reservation(work[0].ladder, work[0].candidate, work[0].request)
    assert hold == pytest.approx(
        ((len(work[0].request.system) + len(work[0].request.user)) / 2 + 1000) * 10 / 1e6
    )
    ran = await ladders.run(work[:1], providers, out, ceiling=hold * 1.5, concurrency=1)
    (first,) = ran.rows
    assert first.outcome == "invalid" and first.cost is None and first.reserved == hold
    # The unknown charge counts at its reservation, in the next run too: the cheaper candidate's
    # call still fits, the next own call does not, and after it nothing starts, however cheap.
    ran = await ladders.run(work[1:], providers, out, ceiling=hold * 1.5, concurrency=1)
    assert [(r.paper, r.candidate) for r in ran.rows] == [(P.id, "other")]
    assert ran.stopped_at is work[2]
    (stop,) = [json.loads(line) for line in (out / ladders.BUDGET).read_text().splitlines()]
    assert stop["first_not_run"] == {"case": work[2].case.key, "candidate": "own"}
    assert stop["not_run"] == 2
    assert stop["spent"] == pytest.approx(hold + ran.rows[0].cost, abs=1e-4)
    assert len(ladders.rows(out)) == 2


async def test_each_case_is_asked_as_the_pipeline_asks_it(conn, tmp_path):
    """Graders, `analyse` and `brief` from the pipeline's own builders, a paper the database
    lacks from the archive's catalog, a text only as the archive holds it, and a published
    daily digest rebuilt as it was written."""
    from litwatch.store import taxonomy as taxonomy_store  # noqa: PLC0415

    await taxonomy_store.save(conn, TREE)
    first = paper("2610.00031", title="Traces that hide steps", version=1)
    await papers_store.upsert(conn, [first])
    w = Writer()
    await focus(conn, first, "central")
    await listed(conn, TUE, first)
    assert await day(conn, w, TUE)
    (digest,) = await (
        await conn.execute("SELECT id FROM digests WHERE state = 'written'")
    ).fetchall()

    fake = FakeArchive()
    # The catalog's listing names no version: its record's latest is the one read.
    elsewhere = {"id": "2610.00099", "title": "Only in the archive", "abstract": "A."}
    fake.records[elsewhere["id"]] = {"id": elsewhere["id"], "latest_version": 2, "item": elsewhere}
    fake.texts[first.id] = (1, "\\section{Method} Probes. " * 20)
    fake.texts[elsewhere["id"]] = (2, "\\section{Setup} Two. " * 20)
    unversioned = paper("2610.00032", title="No version known")
    await papers_store.upsert(conn, [unversioned])
    archive = ArchiveClient("http://archive.test", "archive-token", http=mock(fake.handle))
    chosen = [
        ladders.Case(set="quality", role="judge", paper=first.id, subject=FAITH.slug),
        ladders.Case(set="refusals", role="check", paper="2610.00099", subject=INTERP.slug),
        ladders.Case(set="quality", role="analyse", paper=first.id, subject=INTERP.slug),
        ladders.Case(set="quality", role="brief", paper=first.id, subject=FAITH.slug),
        ladders.Case(set="quality", role="brief", paper="2610.00099", subject=FAITH.slug),
        ladders.Case(set="quality", role="writer", subject=str(digest["id"])),
        ladders.Case(set="quality", role="brief", paper=unversioned.id, subject=FAITH.slug),
        ladders.Case(set="quality", role="judge", paper="2610.00404", subject=FAITH.slug),
    ]
    built = await ladders.requests(conn, archive, chosen)
    stored = (await papers_store.get_many(conn, [first.id]))[0]
    keys = [c.key for c in chosen]
    faith_name, interp_name = TREE.get(FAITH.slug).name, TREE.get(INTERP.slug).name
    assert built[keys[0]] == grading.request(stored, FAITH, faith_name)
    assert built[keys[1]].user.endswith("Title: Only in the archive\n\nAbstract:\nA.")
    catalog = (await archive.paper("2610.00099")).model_copy(update={"version": 2})
    assert built[keys[1]] == grading.request(catalog, INTERP, interp_name)
    assert built[keys[2]] == analysis.request(stored, TREE.get(INTERP.slug))
    text = "\\section{Method} Probes. " * 20
    assert built[keys[3]] == brief.request(stored, TREE.get(FAITH.slug), text, "latex")
    two = "\\section{Setup} Two. " * 20
    assert built[keys[4]] == brief.request(catalog, TREE.get(FAITH.slug), two, "latex")
    assert not fake.jobs  # it only read: nothing was asked for
    assert isinstance(built[keys[5]], Request) and built[keys[5]].user == w.asked[0]
    assert built[keys[6]] == "the archive holds no text of this version"
    assert built[keys[7]] == "the paper is in neither the database nor the archive"


def row(case: str, candidate: str, outcome: str = "answered", **output) -> ladders.Row:
    return ladders.Row(
        set="quality",
        role="judge",
        case=case,
        paper=case.split("/")[1],
        candidate=candidate,
        model=candidate,
        request="r",
        outcome=outcome,
        output=output or None,
        cost=0.01,
    )


def test_the_tables_compare_with_the_owner_and_the_own_model_over_what_both_answered(tmp_path):
    found = ladder(tmp_path)
    chosen = judged(P, label="central") + judged(Q, label="none")
    p, q = chosen[0].key, chosen[1].key
    calls = [
        row(p, "own", fit="central", reason="."),
        row(q, "own", fit="adjacent", reason="."),
        row(p, "other", fit="related", reason="."),
        row(q, "other", "refused"),
    ]
    calls[-1].cost = None
    text = ladders.tables(found, chosen, calls)
    lines = [
        line for line in text.splitlines() if line.startswith("| own") or line.startswith("| other")
    ]
    # own: Fit as the owner 1/2, its side 2/2; other: one answer, related to central: Fit 0/1,
    # side 1/1 against the owner and against own; its refusal's charge is unknown.
    assert lines[0].split(" | ")[3:] == [
        "2",
        "0",
        "0",
        "0",
        "0",
        "1/2",
        "2/2",
        "–",
        "–",
        "$0.02",
        "$0.0100 |",
    ]
    assert lines[1].split(" | ")[3:] == [
        "1",
        "1",
        "0",
        "0",
        "0",
        "0/1",
        "1/1",
        "0/1",
        "1/1",
        "≥ $0.01",
        "$0.0100 |",
    ]
    assert ladders.answers(found, chosen, calls) == "\n"  # the quality set is not listed
    refusals = [c.model_copy(update={"set": "refusals", "note": None}) for c in chosen]
    listed_ = ladders.answers(found, refusals, calls, {Q.id: Q.title})
    assert f"`judge` · {Q.id} · {FAITH.slug} · {Q.title}" in listed_
    assert "- own: **adjacent**. ." in listed_ and "- other: *refused*" in listed_
    assert P.id not in listed_  # every candidate answered it
