"""Digest rendering and the run-level digest step."""

from types import SimpleNamespace

from litwatch.digest import EMPTY_DIGEST, Digester, render_papers
from litwatch.models import (
    Confidence,
    Paper,
    ScreenOutput,
    ScreenResult,
    Stage,
    StageRole,
    Usage,
)


class FakeCompletions:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def create(self, **kw):
        self.calls.append(kw)
        usage = SimpleNamespace(prompt_tokens=10, completion_tokens=5, model_extra={"cost": 0.002})
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="  A digest.  "))],
            usage=usage,
        )


def fake_client() -> tuple[object, FakeCompletions]:
    completions = FakeCompletions()
    return SimpleNamespace(chat=SimpleNamespace(completions=completions)), completions


def positive(paper_id: str, title: str) -> tuple[Paper, ScreenResult]:
    paper = Paper(id=paper_id, title=title, abstract="Abstract text.", authors=["A B"])
    out = ScreenOutput(
        is_cot_monitoring_or_faithfulness=True,
        uses_internals=True,
        is_mech_interp=False,
        proposes_definition_or_benchmark=False,
        confidence=Confidence.HIGH,
        reason="Monitors CoT with probes.",
    )
    stage = Stage(role=StageRole.SCREEN, model="m", output=out, usage=Usage(model="m"))
    return paper, ScreenResult(paper_id=paper_id, prompt_version="v2", stages=[stage])


async def test_empty_run_needs_no_model_call():
    client, completions = fake_client()
    text, usage = await Digester(client, "z-ai/glm-5.3").write([])
    assert text == EMPTY_DIGEST and usage is None and completions.calls == []


async def test_digest_call_carries_papers_and_cost():
    client, completions = fake_client()
    papers = [positive("2601.00001", "First Paper"), positive("2601.00002", "Second Paper")]
    text, usage = await Digester(client, "z-ai/glm-5.3").write(papers)
    assert text == "A digest."
    assert usage is not None and usage.model == "z-ai/glm-5.3" and usage.cost == 0.002
    (call,) = completions.calls
    assert call["model"] == "z-ai/glm-5.3"
    user = call["messages"][1]["content"]
    assert user == render_papers(papers)
    assert "Title: First Paper" in user and "Tier: must read" in user
    assert call["extra_body"]["usage"] == {"include": True}
