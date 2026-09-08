"""Digest rendering and the per-run digest set."""

from types import SimpleNamespace

from litwatch.digest import (
    EMPTY_DIGEST,
    GENERAL,
    RECOMMENDED_CAP,
    Digester,
    GeneralOutput,
    Note,
    SelectionOutput,
    by_tier,
    render_selection_digest,
    render_selection_input,
)
from tests.conftest import FAITH, INTERP

FAITH_D = FAITH.model_copy(update={"digest": True})
INTERP_D = INTERP.model_copy(update={"digest": True})


class FakeCompletions:
    """Answers with a canned output per schema; records every call."""

    def __init__(self, outputs: dict[type, object]) -> None:
        self.outputs, self.calls = outputs, []

    async def create(self, **kw):
        self.calls.append(kw)
        usage = SimpleNamespace(prompt_tokens=10, completion_tokens=5, model_extra={"cost": 0.002})
        name = kw["response_format"]["json_schema"]["name"]
        out = self.outputs[
            {"SelectionOutput": SelectionOutput, "GeneralOutput": GeneralOutput}[name]
        ]
        content = out if isinstance(out, str) else f"```json\n{out.model_dump_json()}\n```"
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))], usage=usage
        )


def fake_client(outputs: dict[type, object]) -> tuple[object, FakeCompletions]:
    completions = FakeCompletions(outputs)
    return SimpleNamespace(chat=SimpleNamespace(completions=completions)), completions


def member(paper_id: str, title: str, tiers: dict[str, int], card: dict | None = None) -> dict:
    return {
        "id": paper_id,
        "title": title,
        "url": f"https://arxiv.org/abs/{paper_id}",
        "abstract": "Abstract text.",
        "enrichment": card,
        "verdicts": {
            slug: {"tier": t, "confidence": "high", "reason": "Because."}
            for slug, t in tiers.items()
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
SEL_OUT = SelectionOutput(
    overview="Two omission tests today.",
    must_read=[Note(id="1", text="Plants hints and checks disclosure.")],
    recommended=[Note(id="2", text="A proxy study."), Note(id="999", text="Hallucinated id.")],
)
GEN_OUT = GeneralOutput(overview="A quiet day.", must_read=[Note(id="1", text="Hint test.")])


def test_selection_input_carries_cards_for_tier1_and_abstracts_for_tier2():
    papers = [
        member("1", "First", {"cot-faithfulness": 1}, CARD),
        member("2", "Second", {"cot-faithfulness": 2}),
    ]
    text = render_selection_input(FAITH, by_tier(FAITH, papers))
    assert text.startswith("# CoT faithfulness\n")
    assert "## Tier 1: First (arXiv 1)" in text and "Detection method: Hint injection" in text
    assert "## Tier 2: Second (arXiv 2)" in text and "Abstract: Abstract text." in text


def test_selection_markdown_links_titles_and_counts_the_rest():
    papers = [member("1", "First  Paper", {"cot-faithfulness": 1}, CARD)]
    papers += [
        member(str(i), f"P{i}", {"cot-faithfulness": 2}) for i in range(2, 2 + RECOMMENDED_CAP + 3)
    ]
    papers += [
        member("50", "T3", {"cot-faithfulness": 3}),
        member("51", "Other", {"mech-interp": 2}),
    ]
    out = SelectionOutput(
        overview="Overview.",
        must_read=[],  # the model skipped the tier 1 paper: it is listed anyway
        recommended=[Note(id=str(i), text=f"note {i}") for i in range(2, 2 + RECOMMENDED_CAP + 1)],
    )
    body = render_selection_digest(FAITH, by_tier(FAITH, papers), out)
    assert "**Must read**\n- [First Paper](https://arxiv.org/abs/1) — Probes predict" in body
    assert body.count("\n- [P") == RECOMMENDED_CAP
    assert "Plus 3 more recommended and 1 good to know" in body
    assert "Other" not in body


async def test_write_all_makes_general_plus_flagged_selection_digests():
    client, completions = fake_client({SelectionOutput: SEL_OUT, GeneralOutput: GEN_OUT})
    papers = [
        member("1", "First", {"cot-faithfulness": 1, "mech-interp": 2}, CARD),
        member("2", "Second", {"cot-faithfulness": 2}),
    ]
    digests = await Digester(client, "k").write_all([FAITH_D, INTERP], papers)
    assert [d.source for d in digests] == [GENERAL, "cot-faithfulness"]
    general, faith = digests
    # The suffix names the selections where the paper is tier 1, not every membership.
    link = "[First](https://arxiv.org/abs/1)"
    head = f"A quiet day.\n\n**Must read**\n- {link} (CoT faithfulness) — Hint test."
    assert general.body.startswith(head)
    assert "- CoT faithfulness: 1 must read, 1 recommended, 0 good to know" in general.body
    assert (
        "- Mechanistic interpretability: 0 must read, 1 recommended, 0 good to know" in general.body
    )
    assert faith.body == (
        "Two omission tests today.\n\n"
        f"**Must read**\n- {link} — Plants hints and checks disclosure.\n\n"
        "**Recommended**\n- [Second](https://arxiv.org/abs/2) — A proxy study."
    )
    assert faith.model == "k" and faith.cost == 0.002 and general.cost == 0.002
    assert len(completions.calls) == 2
    assert completions.calls[0]["extra_body"]["usage"] == {"include": True}


async def test_only_tier3_members_need_no_model_call():
    client, completions = fake_client({GeneralOutput: GEN_OUT})
    papers = [member("3", "Only", {"cot-faithfulness": 3})]
    general, faith = await Digester(client, "k").write_all([FAITH_D], papers)
    assert (
        faith.body
        == "Plus 1 good to know in the Papers table (filter Selections = CoT faithfulness)."
    )
    assert faith.cost is None and len(completions.calls) == 1  # the general digest still runs


async def test_empty_run_needs_no_model_call():
    client, completions = fake_client({})
    (general,) = await Digester(client, "k").write_all([FAITH_D], [])
    assert general.body == EMPTY_DIGEST and completions.calls == []


async def test_a_bad_selection_digest_is_skipped_not_fatal():
    client, completions = fake_client({SelectionOutput: "not json", GeneralOutput: GEN_OUT})
    papers = [member("1", "First", {"cot-faithfulness": 1}, CARD)]
    digests = await Digester(client, "k").write_all([FAITH_D], papers)
    assert [d.source for d in digests] == [GENERAL]
    assert len(completions.calls) == 3  # general once, the selection twice before giving up
