"""The pure rules: reconciliation, which verdict counts, and what the owner's label does."""

from datetime import UTC, datetime, timedelta

import pytest

from litwatch.config import Config, Models, RoleConfig
from litwatch.core import (
    Grade,
    Label,
    LabelOrigin,
    LabelValue,
    Opinion,
    Status,
    TopicKind,
    Verdict,
    current_verdict,
    effective_grade,
    keyword_match,
    reconcile,
)
from tests.conftest import FAITH, paper

T0 = datetime(2026, 10, 1, tzinfo=UTC)


def opinion(grade: str) -> Opinion:
    return Opinion(grade=Grade(grade), reason="r")


def verdict(grade: str, *, revision=1, version=1, at=T0, id=1) -> Verdict:
    return Verdict(
        id=id,
        paper_id="p",
        topic="t",
        topic_revision=revision,
        paper_version=version,
        grade=Grade(grade),
        recorded_at=at,
    )


def label(value: str, grade: str | None = None) -> Label:
    return Label(
        paper_id="p",
        topic="t",
        value=LabelValue(value),
        grade=Grade(grade) if grade else None,
        origin=LabelOrigin.INBOX,
    )


def test_disagreement_takes_the_more_inclusive_grade_and_marks_the_verdict_split():
    assert reconcile(opinion("relevant"), opinion("relevant")) == (Grade.RELEVANT, False)
    assert reconcile(opinion("none"), opinion("must_read")) == (Grade.MUST_READ, True)
    assert reconcile(opinion("relevant"), opinion("adjacent")) == (Grade.RELEVANT, True)


def test_the_current_verdict_is_chosen_by_version_first_and_time_second():
    old_text = verdict("must_read", version=1, at=T0 + timedelta(days=9), id=1)
    new_text = verdict("none", version=2, at=T0, id=2)
    # A late re-grade of an old paper version does not overtake the newer version's verdict.
    assert current_verdict([old_text, new_text]) is new_text
    newer_topic = verdict("adjacent", version=2, revision=2, at=T0, id=3)
    assert current_verdict([old_text, new_text, newer_topic]) is newer_topic
    again = verdict("relevant", version=2, revision=2, at=T0 + timedelta(hours=1), id=4)
    assert current_verdict([newer_topic, again]) is again
    assert current_verdict([]) is None


def test_the_owners_label_outranks_the_verdict():
    assert effective_grade(None, verdict("adjacent")) == Grade.ADJACENT
    assert effective_grade(label("unsure"), verdict("adjacent")) == Grade.ADJACENT
    assert effective_grade(label("not_relevant"), verdict("must_read")) == Grade.NONE
    assert effective_grade(label("adjacent"), verdict("must_read")) == Grade.ADJACENT
    # Relevant without a grade keeps a verdict that already says so, and lifts one that does not.
    assert effective_grade(label("relevant"), verdict("must_read")) == Grade.MUST_READ
    assert effective_grade(label("relevant"), verdict("none")) == Grade.RELEVANT
    assert effective_grade(label("relevant"), None) == Grade.RELEVANT
    assert effective_grade(label("relevant", "adjacent"), verdict("must_read")) == Grade.ADJACENT
    assert effective_grade(None, None) is None


def test_a_topic_version_follows_what_a_model_sees_and_nothing_else():
    renamed = FAITH.model_copy(update={"name": "Renamed", "status": Status.PAUSED, "revision": 7})
    assert renamed.version == FAITH.version
    reordered = FAITH.model_copy(update={"keywords": list(reversed(FAITH.keywords))})
    assert reordered.version == FAITH.version
    assert FAITH.model_copy(update={"rubric": "Another rubric."}).version != FAITH.version
    assert FAITH.model_copy(update={"kind": TopicKind.RADAR}).version != FAITH.version


def test_keyword_audit_matches_title_or_abstract_case_insensitively():
    assert keyword_match(FAITH, paper(title="Is chain-of-thought FAITHFUL?")) == r"chain.of.thought"
    assert keyword_match(FAITH, paper(abstract="We study Scot and cots.")) is None


def test_judge_and_check_must_come_from_different_labs():
    opus = RoleConfig(provider="anthropic", model="claude-opus-5-5")
    # The lab is who trained the model: one OpenRouter key can serve two labs.
    Models(
        judge=RoleConfig(provider="openrouter", model="openai/gpt-6-luna"),
        check=RoleConfig(provider="openrouter", model="meta/muse-spark-1.3"),
    )
    with pytest.raises(ValueError, match="different labs"):
        Models(judge=opus, check=RoleConfig(provider="anthropic", model="claude-haiku-5-5"))
    with pytest.raises(ValueError, match="different labs"):
        Models(
            judge=RoleConfig(provider="openrouter", model="anthropic/claude-opus-5-5"),
            check=opus,
        )
    assert Config().models.judge.lab != Config().models.check.lab
