"""The Brief harness: a dry run of the `brief` role over the preference set's papers, read from
the archive, that resumes and refuses mixed answers; and its report's rule checks."""

import json

import pytest

from litwatch import seeds
from litwatch.archive import ArchiveClient
from litwatch.config import Models, RoleConfig
from litwatch.llm import Gateway, Reply, Stop, Usage
from litwatch.quality import briefs
from tests.conftest import paper
from tests.fakes import ProviderError, ScriptedProvider
from tests.fakes_http import FakeArchive, mock

P = paper(
    "2610.00001", title="Probes for hidden steps", abstract="We detect all hidden steps.", version=1
)
Q = paper("2610.00002", title="Monitors under pressure", abstract="Monitors fail.")
ROLE = RoleConfig(provider="anthropic", model="test-brief")
OTHER = RoleConfig(provider="openrouter", model="lab/fallback")
BRIEF = " ".join(["Probes trained on every layer recover the hidden steps of a computation."] * 9)
ANSWER = {
    "overstatement": "The abstract overstates: the probes find hidden steps on two tasks only.",
    "brief": BRIEF,
    "gist": "Linear probes trained on every layer recover the hidden reasoning steps of a model "
    "on two arithmetic tasks only.",
    "weight_reason": "A careful analysis.",
    "weight": "solid",
}

GIST_IN_VOICE = (
    "Monitors you can trust fail under optimization pressure on every task the authors test, "
    "losing most of their catch rate."
)


def gateway(asked: list[str]) -> Gateway:
    def respond(role, request):
        asked.append(request.user)
        return json.dumps(ANSWER)

    return Gateway(Models(brief=ROLE), {"anthropic": ScriptedProvider("anthropic", respond)})


def entries() -> list[briefs.Entry]:
    return [briefs.Entry(id=P.id, area="cot-faithfulness"), briefs.Entry(id=Q.id, overstates=False)]


async def test_a_run_reads_each_text_from_the_archive_and_resumes_what_had_none(tmp_path):
    fake = FakeArchive()
    fake.texts[P.id] = (1, "\\section{Method} Probes on two tasks. " * 20)
    archive = ArchiveClient("http://archive.test", "archive-token", http=mock(fake.handle))
    out, asked = tmp_path / "brief.jsonl", []
    pairs = list(zip([P, Q], entries(), strict=True))
    first = await briefs.run(gateway(asked), seeds.taxonomy(), pairs, archive, out, wait_seconds=0)
    assert [(r.paper_id, r.error) for r in first] == [
        (P.id, None),
        (Q.id, f"{Q.id}: no arXiv version known and no LaTeX text"),
    ]
    (user,) = asked
    assert "Area: Chain-of-thought faithfulness" in user and "Probes on two tasks." in user
    assert first[0].source == "latex" and first[0].output.weight == "solid"

    fake.texts[Q.id] = (None, "\\section{Setup} Monitors. " * 20)
    second = await briefs.run(gateway(asked), seeds.taxonomy(), pairs, archive, out, wait_seconds=0)
    assert [r.paper_id for r in second] == [Q.id] and second[0].version is None
    assert set(briefs.results(out)) == {P.id, Q.id}

    # A paper whose area changed is answered again; the rest are kept.
    moved = [(P, entries()[0].model_copy(update={"area": "cot-monitoring"})), pairs[1]]
    third = await briefs.run(gateway(asked), seeds.taxonomy(), moved, archive, out, wait_seconds=0)
    assert [r.paper_id for r in third] == [P.id]
    assert "Area: Chain-of-thought monitoring" in asked[-1]

    # A billed failure answered again later counts in what the run spent, not in its answers.
    billed = briefs.Result(paper_id=P.id, error="invalid", cost=0.2, request=third[0].request)
    with out.open("a") as f:
        f.write(billed.model_dump_json() + "\n" + third[0].model_dump_json() + "\n")
    assert sum(briefs.spent(out)) == pytest.approx(0.203 + third[0].cost)
    assert briefs.results(out)[P.id].output is not None

    other = Gateway(Models(brief=ROLE.model_copy(update={"model": "another"})), {})
    with pytest.raises(ValueError, match="another request"):
        await briefs.run(other, seeds.taxonomy(), pairs, archive, out)


async def test_a_refusal_the_fallback_answered_counts_in_its_papers_cost(tmp_path):
    fake = FakeArchive()
    fake.texts[P.id] = (1, "\\section{Method} Probes on two tasks. " * 20)
    archive = ArchiveClient("http://archive.test", "archive-token", http=mock(fake.handle))

    def refuse(role, request):
        return Reply("", Stop.REFUSED, Usage(model=role.model, cost=0.05), refusal="cyber")

    charge = [0.02]

    def answer(role, request):
        return Reply(json.dumps(ANSWER), Stop.COMPLETE, Usage(model=role.model, cost=charge[0]))

    models = Models(
        brief=ROLE.model_copy(update={"fallback": "brief_fallback"}), brief_fallback=OTHER
    )
    providers = {
        "anthropic": ScriptedProvider("anthropic", refuse),
        "openrouter": ScriptedProvider("openrouter", answer),
    }
    out = tmp_path / "brief.jsonl"
    pair = [(P, entries()[0])]
    (result,) = await briefs.run(Gateway(models, providers), seeds.taxonomy(), pair, archive, out)
    assert result.output is not None and result.cost == pytest.approx(0.07)
    # A charge the provider did not report leaves the paper's cost unknown, not a subtotal.
    charge[0] = None
    (unknown,) = await briefs.run(
        Gateway(models, providers), seeds.taxonomy(), pair, archive, tmp_path / "unknown.jsonl"
    )
    assert unknown.output is not None and unknown.cost is None
    # So does a fallback that timed out, after a refusal that was charged.

    def down(role, request):
        raise ProviderError("APITimeoutError", transient=True)

    providers["openrouter"] = ScriptedProvider("openrouter", down)
    gw = Gateway(models, providers, retry_delays=())
    (failed,) = await briefs.run(gw, seeds.taxonomy(), pair, archive, tmp_path / "down.jsonl")
    assert failed.error and failed.cost is None
    # Another fallback is another configuration: the run is not resumed into the same file.
    other = Models(
        brief=models.brief, brief_fallback=OTHER.model_copy(update={"model": "lab/another"})
    )
    with pytest.raises(ValueError, match="another request"):
        await briefs.run(Gateway(other, providers), seeds.taxonomy(), pair, archive, out)


def test_the_report_checks_lengths_and_voice_and_shows_the_owners_read():
    p_in, q_in = (briefs.inputs(p, e) for p, e in zip([P, Q], entries(), strict=True))
    found = {
        P.id: briefs.Result(
            paper_id=P.id, version=1, source="latex", chars=900, model="m", cost=0.2, request="x",
            inputs=p_in, output=briefs.brief.Output.model_validate(ANSWER),
        ),
        Q.id: briefs.Result(
            paper_id=Q.id, version=1, source="latex", chars=900, model="m", cost=0.2, request="x",
            inputs=q_in, output=briefs.brief.Output.model_validate(
                {**ANSWER, "overstatement": None, "gist": GIST_IN_VOICE}
            ),
        ),
    }  # fmt: skip
    text = briefs.report(
        "Briefs", entries(), {P.id: P, Q.id: Q}, found, {P.id: "An old Gist."}, [0.6]
    )
    assert (
        f"| {P.id} | yes | not read yet | 108 | 19 | Brief of 108 words (asked 120–200) |" in text
    )
    assert (
        f"| {Q.id} | no | does not overstate | 108 | 20 |" in text and "Gist voice: “you”" in text
    )
    assert "**Gist from the abstract.** An old Gist." in text
    assert "**Overstatement.** The abstract overstates: the probes" in text
    assert "$0.60 in all" in text

    # A charge not known makes the total a lower bound, and its paper's cost unknown.
    unknown = {**found, Q.id: found[Q.id].model_copy(update={"cost": None})}
    text = briefs.report("Briefs", entries(), {P.id: P, Q.id: Q}, unknown, {}, [0.6, None])
    assert "≥ $0.60 in all" in text and "· m · cost unknown" in text

    # An answer to an abstract since changed is not shown beside the new one.
    revised = P.model_copy(update={"abstract": "We detect some hidden steps."})
    text = briefs.report("Briefs", entries(), {P.id: revised, Q.id: Q}, found, {}, [0.6])
    assert f"| {P.id} | answered for other inputs: run it again |" in text
    assert "We detect all hidden steps." not in text

    # Nor is one to a paper updated since while its version stays unknown.
    later = Q.model_copy(update={"updated": "2026-10-14T00:00:00Z"})
    text = briefs.report("Briefs", entries(), {P.id: P, Q.id: later}, found, {}, [0.6])
    assert f"| {Q.id} | answered for other inputs: run it again |" in text
