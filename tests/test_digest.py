"""Digest rendering and the per-run digest set."""

from types import SimpleNamespace

from litwatch.digest import (
    EMPTY_DIGEST,
    GENERAL,
    RECOMMENDED_CAP,
    Digester,
    GeneralOutput,
    Note,
    Section,
    SelectionOutput,
    assign,
    by_tier,
    render_general_digest,
    render_general_input,
    render_revisions,
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
GEN_OUT = GeneralOutput(
    overview="A quiet day.",
    sections=[
        Section(
            slug="cot-faithfulness",
            overview="Hints again.",
            must_read=[Note(id="1", text="Hint test.")],
            recommended=[Note(id="2", text="Proxy.")],
        )
    ],
)


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
    assert body.endswith(
        "Plus 3 more recommended and 1 good to know in the Papers table, view CoT faithfulness."
    )
    assert "Other" not in body


def test_selection_input_carries_monitoring_cards():
    card = {
        "key_claim": "Monitors catch reward hacking.",
        "summary": "Summary text.",
        "monitoring": {
            "target": ["reward hacking"],
            "monitor_type": "black-box",
            "monitor_method": "A weaker model reads the trace.",
            "pressure": "optimization against the monitor",
            "robustness": "Obfuscation after RL.",
            "generality": "Coding tasks only.",
        },
    }
    papers = [member("1", "First", {"cot-faithfulness": 1}, card)]
    text = render_selection_input(FAITH, by_tier(FAITH, papers))
    assert "Monitor target: reward hacking" in text
    assert "Pressure tested: optimization against the monitor" in text


def test_assign_places_a_paper_under_the_selection_where_it_ranks_highest():
    papers = [
        member("1", "First", {"cot-faithfulness": 2, "mech-interp": 1}),
        member("2", "Second", {"cot-faithfulness": 2, "mech-interp": 2}),  # tie: earlier wins
        member("3", "Third", {"cot-faithfulness": 4}),
    ]
    owned = assign([FAITH, INTERP], papers)
    assert [r["id"] for r in owned["mech-interp"][1]] == ["1"]
    assert [r["id"] for r in owned["cot-faithfulness"][2]] == ["2"]
    assert not owned["cot-faithfulness"][1] and not owned["mech-interp"][2]
    text = render_general_input([FAITH, INTERP], owned)
    assert "# Mechanistic interpretability (slug: mech-interp)\n" in text
    assert "## Tier 1: First (arXiv 1)" in text and "Third" not in text


async def test_write_all_makes_general_plus_flagged_selection_digests():
    client, completions = fake_client({SelectionOutput: SEL_OUT, GeneralOutput: GEN_OUT})
    papers = [
        member("1", "First", {"cot-faithfulness": 1, "mech-interp": 2}, CARD),
        member("2", "Second", {"cot-faithfulness": 2}),
        member("3", "Third", {"mech-interp": 3}),
    ]
    digests = await Digester(client, "k").write_all([FAITH_D, INTERP], papers)
    assert [d.source for d in digests] == [GENERAL, "cot-faithfulness"]
    general, faith = digests
    link = "[First](https://arxiv.org/abs/1)"
    # One section per selection with members; the suffix names other tier 1/2 memberships.
    assert general.body == (
        "A quiet day.\n\n"
        "**CoT faithfulness** · 1 must read, 1 recommended, 0 good to know. Hints again.\n"
        f"- {link} (also Mechanistic interpretability) — Hint test.\n"
        "- [Second](https://arxiv.org/abs/2) — Proxy.\n\n"
        "**Mechanistic interpretability** · 1 must read or recommended listed under other "
        "selections, 1 good to know."
    )
    assert faith.body == (
        "Two omission tests today.\n\n"
        f"**Must read**\n- {link} — Plants hints and checks disclosure.\n\n"
        "**Recommended**\n- [Second](https://arxiv.org/abs/2) — A proxy study."
    )
    assert faith.model == "k" and faith.cost == 0.002 and general.cost == 0.002
    assert len(completions.calls) == 2
    assert completions.calls[0]["extra_body"]["usage"] == {"include": True}


async def test_general_lists_tier1_even_when_the_model_skips_the_section():
    out = GeneralOutput(overview="Quiet.", sections=[])
    client, _ = fake_client({GeneralOutput: out})
    papers = [member("1", "First", {"cot-faithfulness": 1}, CARD)]
    (general,) = await Digester(client, "k").write_all([FAITH], papers)
    assert general.body == (
        "Quiet.\n\n**CoT faithfulness** · 1 must read, 0 recommended, 0 good to know.\n"
        "- [First](https://arxiv.org/abs/1) — Probes predict unfaithful reasoning."
    )


async def test_tier3_only_day_gets_an_overview_but_no_general_call():
    quiet = SelectionOutput(overview="Only proxies today.", must_read=[], recommended=[])
    client, completions = fake_client({SelectionOutput: quiet})
    papers = [member("3", "Only", {"cot-faithfulness": 3})]
    general, faith = await Digester(client, "k").write_all([FAITH_D], papers)
    assert general.body == (
        "**CoT faithfulness** · nothing must read or recommended, 1 good to know."
    )
    assert faith.body == (
        "Only proxies today.\n\nNothing reached must read or recommended today; "
        "the 1 good to know paper is in the Papers table, view CoT faithfulness."
    )
    # The tier 3 titles and reasons are what the model sees on such a day.
    assert len(completions.calls) == 1
    assert (
        "## Tier 3 (1 papers)\n- Only (arXiv 3): Because."
        in completions.calls[0]["messages"][1]["content"]
    )
    assert faith.cost == 0.002 and general.cost is None


async def test_empty_run_needs_no_model_call_and_still_writes_the_pages():
    client, completions = fake_client({})
    general, faith = await Digester(client, "k").write_all([FAITH_D], [])
    assert general.body == EMPTY_DIGEST and completions.calls == []
    assert faith.body == (
        "Nothing new for CoT faithfulness in this run: no paper passed the screen for it."
    )


async def test_a_bad_selection_digest_is_skipped_not_fatal():
    client, completions = fake_client({SelectionOutput: "not json", GeneralOutput: GEN_OUT})
    papers = [member("1", "First", {"cot-faithfulness": 1}, CARD)]
    digests = await Digester(client, "k").write_all([FAITH_D], papers)
    assert [d.source for d in digests] == [GENERAL]
    assert len(completions.calls) == 3  # general once, the selection twice before giving up


def older(row: dict, version: int = 3) -> dict:
    return {**row, "version": version, "first_posted": "2026-05-27T03:26:03+00:00"}


def test_older_papers_are_badged_at_tier1_and_grouped_at_tier2():
    papers = [
        older(member("1", "Old must", {"cot-faithfulness": 1}, CARD)),
        member("2", "New rec", {"cot-faithfulness": 2}),
        older(member("3", "Old rec", {"cot-faithfulness": 2})),
        older(member("4", "Old interp", {"mech-interp": 2}), version=1),
    ]
    owned = assign([FAITH, INTERP], papers)
    text = render_general_input([FAITH, INTERP], owned)
    assert "(arXiv 1 · v3, first posted May 27, 2026)" in text
    assert "## Tier 2: Old rec" not in text and "Mechanistic interpretability (slug" not in text
    assert text.endswith(
        "# Older papers, newly updated (2 tier 2 papers)\n"
        "- Old rec (arXiv 3): CoT faithfulness: Because.\n"
        "- Old interp (arXiv 4): Mechanistic interpretability: Because."
    )
    out = GeneralOutput(
        overview="Day.",
        sections=[Section(slug="cot-faithfulness", overview="O.", must_read=[], recommended=[])],
        updated=[Note(id="4", text="Interp update.")],
    )
    body = render_general_digest([FAITH, INTERP], owned, out)
    assert (
        "**CoT faithfulness** · 1 must read, 1 recommended, 1 older newly updated, "
        "0 good to know. O.\n"
        "- [Old must](https://arxiv.org/abs/1) · v3, first posted May 27, 2026 — Probes predict"
    ) in body
    assert body.endswith(
        "**Older papers, newly updated** · 2 recommended papers first posted weeks or months ago.\n"
        "- [Old interp](https://arxiv.org/abs/4) · Mechanistic interpretability · "
        "first posted May 27, 2026 — Interp update.\n"
        "Plus 1 more in the Papers table."
    )


def test_shared_count_skips_older_papers_grouped_in_the_updated_section():
    papers = [
        older(member("1", "Old shared", {"cot-faithfulness": 2, "mech-interp": 2})),
        member("2", "Third", {"mech-interp": 3}),
    ]
    body = render_general_digest([FAITH, INTERP], assign([FAITH, INTERP], papers), None)
    # The shared paper is not under CoT faithfulness but in the updated section.
    assert (
        "**Mechanistic interpretability** · nothing must read or recommended, 1 good to know."
        in body
    )


def test_selection_digest_lists_older_papers_apart_and_counts_the_rest():
    papers = [
        member("2", "New rec", {"cot-faithfulness": 2}),
        older(member("3", "Old rec", {"cot-faithfulness": 2})),
        older(member("4", "Old two", {"cot-faithfulness": 2})),
    ]
    groups = by_tier(FAITH, papers)
    text = render_selection_input(FAITH, groups)
    assert "Abstract" in text.split("## Older papers")[0]
    assert "## Older papers, newly updated (2 tier 2 papers)\n- Old rec (arXiv 3)" in text
    out = SelectionOutput(
        overview="O.",
        must_read=[],
        recommended=[Note(id="2", text="New.")],
        updated=[Note(id="3", text="Revised.")],
    )
    body = render_selection_digest(FAITH, groups, out)
    assert (
        "**Older papers, newly updated**\n- [Old rec](https://arxiv.org/abs/3) · v3, "
        "first posted May 27, 2026 — Revised."
    ) in body
    assert body.endswith(
        "Plus 1 older recommended paper newly updated in the Papers table, view CoT faithfulness."
    )


def test_revision_notes_name_version_tiers_and_first_coverage():
    row = {
        **member("1", "Paper", {"cot-faithfulness": 1, "mech-interp": 2}),
        "version": 2,
        "covered_at": "2026-09-12T08:00:00+00:00",
    }
    assert render_revisions([FAITH, INTERP], [row], GENERAL) == (
        "**Updated since we covered them**\n- [Paper](https://arxiv.org/abs/1) · v2, "
        "tier 1 in CoT faithfulness, tier 2 in Mechanistic interpretability · "
        "first covered Sep 12, 2026"
    )
    assert "Mechanistic" not in render_revisions([FAITH, INTERP], [row], FAITH.slug)


def test_historical_title_matching_is_exact_and_unambiguous():
    from litwatch.digest_history import historical_ids

    title = "Faithfulness Metrics Don't Measure Faithfulness: A Meta-Evaluation"
    papers = [
        {"id": "1", "title": title, "url": None},
        {"id": "2", "title": "Short title", "url": None},
        {"id": "3", "title": "A duplicated paper title that is ambiguous", "url": None},
        {"id": "4", "title": "A duplicated paper title that is ambiguous", "url": None},
    ]
    prose = title.replace("Don't", "Don’t").replace(" ", "\n ")
    prose += " Short title. A duplicated paper title that is ambiguous."
    assert historical_ids(prose, papers) == {"1"}
    assert historical_ids(title.replace("Meta-Evaluation", "Evaluation"), papers) == set()
    assert historical_ids("[link](http://arxiv.org/abs/2v3)", papers) == {"2"}
