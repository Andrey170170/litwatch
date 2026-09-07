"""Digest rendering and the run-level digest step."""

from types import SimpleNamespace

from litwatch.digest import EMPTY_DIGEST, Digester, normalize, render_papers, render_tier3
from tests.conftest import SELECTIONS


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


def member(paper_id: str, title: str, tier: int, card: dict | None = None) -> dict:
    return {
        "id": paper_id,
        "title": title,
        "abstract": "Abstract text.",
        "enrichment": card,
        "verdicts": {
            "cot-faithfulness": {"tier": tier, "confidence": "high", "reason": "Because."}
        },
    }


CARD = {
    "key_claim": "Probes predict unfaithful reasoning.",
    "summary": "Summary text.",
    "faithfulness": {
        "mismatch": ["omission"],
        "ground_truth": "task construction",
        "detection_method": "Hint injection with a labelling pipeline.",
        "generality": "13 tasks, 10 models.",
    },
}


async def test_empty_run_needs_no_model_call():
    client, completions = fake_client()
    text, usage = await Digester(client, "k").write(SELECTIONS, [])
    assert text == EMPTY_DIGEST and usage is None and completions.calls == []
    non_member = {**member("1", "T", 3), "verdicts": {}}
    assert (await Digester(client, "k").write(SELECTIONS, [non_member]))[0] == EMPTY_DIGEST


async def test_digest_call_groups_by_selection_and_tier():
    client, completions = fake_client()
    papers = [member("1", "First Paper", 1, CARD), member("2", "Second Paper", 2)]
    text, usage = await Digester(client, "k").write(SELECTIONS, papers)
    assert text == "A digest."
    assert usage is not None and usage.model == "k" and usage.cost == 0.002
    (call,) = completions.calls
    user = call["messages"][1]["content"]
    assert user == render_papers(SELECTIONS, papers)
    assert user.startswith("# CoT faithfulness\n") and "Mechanistic" not in user
    assert "## Tier 1: First Paper" in user and "Detection method: Hint injection" in user
    assert "## Tier 2: Second Paper" in user and "Abstract: Abstract text." in user
    assert call["extra_body"]["usage"] == {"include": True}


async def test_tier3_titles_are_listed_by_code_not_the_model():
    client, completions = fake_client()
    papers = [member("1", "First Paper", 2), member("2", "Second  Paper", 3), member("3", "T3", 3)]
    text, _ = await Digester(client, "k").write(SELECTIONS, papers)
    (call,) = completions.calls
    assert "Second" not in call["messages"][1]["content"]
    tail = "CoT faithfulness, good to know: Second Paper; T3."
    assert text == f"A digest.\n\n{tail}" and render_tier3(SELECTIONS, papers) == tail


async def test_only_tier3_members_need_no_model_call():
    client, completions = fake_client()
    text, usage = await Digester(client, "k").write(SELECTIONS, [member("2", "Only Paper", 3)])
    assert text == "CoT faithfulness, good to know: Only Paper." and usage is None
    assert completions.calls == []


def test_normalize_joins_wrapped_lines_and_keeps_paragraphs():
    wrapped = "First line\ncontinues here.  \n\n\nSecond paragraph\nalso wrapped.\n"
    assert normalize(wrapped) == "First line continues here.\n\nSecond paragraph also wrapped."
