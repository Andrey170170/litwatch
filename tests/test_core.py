"""The pure rules: which verdict counts, versions, and configuration."""

from datetime import UTC, datetime, timedelta
from urllib.parse import unquote, urlsplit

import pytest

from litwatch import config
from litwatch.config import Config, Models, RoleConfig
from litwatch.core import Depth, Fit, Status, Verdict, current_verdict
from tests.conftest import FAITH, TREE

T0 = datetime(2026, 10, 1, tzinfo=UTC)


def verdict(fit: str, *, text=1, version="v1", at=T0, id=1) -> Verdict:
    return Verdict(
        id=id,
        paper_id="p",
        topic="t",
        topic_version=version,
        paper_revision=text,
        fit=Fit(fit),
        reason="r",
        opinions={},
        recorded_at=at,
    )


def test_the_current_verdict_is_chosen_by_text_first_and_time_second():
    old_text = verdict("central", text=1, at=T0 + timedelta(days=9), id=1)
    new_text = verdict("none", text=2, at=T0, id=2)
    # A late re-grade of the old text does not overtake the verdict on the rewrite.
    assert current_verdict([old_text, new_text]) is new_text
    # The topic's version plays no part: a verdict stays valid on its topic whatever version.
    again = verdict("related", text=2, version="v0", at=T0 + timedelta(hours=1), id=3)
    assert current_verdict([old_text, new_text, again]) is again
    assert current_verdict([old_text]) is old_text  # until the new text is graded
    assert current_verdict([]) is None


def test_a_topic_version_follows_what_a_grader_reads_and_nothing_else():
    badge = {"monogram": "XX", "colour": "#000000", "status": Status.PAUSED}
    assert FAITH.model_copy(update=badge).version == FAITH.version
    reordered = FAITH.model_copy(update={"keywords": list(reversed(FAITH.keywords))})
    assert reordered.version == FAITH.version
    for change in (
        {"rubric": "Another rubric."},
        {"depth": Depth.AREA},
        {"subject": "Another subject entirely."},
        {"interests": "Something."},
        {"keywords": ["faithful"]},
    ):
        assert FAITH.model_copy(update=change).version != FAITH.version


def test_a_class_version_follows_name_description_parent_and_threshold():
    c = TREE.get(FAITH.slug)
    assert c.model_copy(update={"slug": "renamed-slug"}).version == c.version
    for change in (
        {"name": "Another name"},
        {"description": "Another description."},
        {"parent": None},
        {"threshold": 0.7},
    ):
        assert c.model_copy(update=change).version != c.version


def test_judge_and_check_must_come_from_different_labs():
    opus = RoleConfig(provider="anthropic", model="claude-opus-5-5")
    # The lab is who trained the model: one OpenRouter key can serve two labs.
    Models(
        judge=RoleConfig(provider="openrouter", model="openai/gpt-6-luna"),
        check=RoleConfig(provider="openrouter", model="meta/muse-spark-1.3"),
    )
    with pytest.raises(ValueError, match="share no lab"):
        Models(judge=opus, check=RoleConfig(provider="anthropic", model="claude-haiku-5-5"))
    with pytest.raises(ValueError, match="share no lab"):
        Models(
            judge=RoleConfig(provider="openrouter", model="anthropic/claude-opus-5-5"),
            check=opus,
        )


def test_a_database_password_with_url_characters_is_quoted(monkeypatch):
    odd = "@/#:%"  # not a real credential; these are the characters that would break a URL
    monkeypatch.delenv("LITWATCH_DATABASE_URL", raising=False)
    monkeypatch.setenv("LITWATCH_DB_HOST", "db")
    monkeypatch.setenv("LITWATCH_DB_PASSWORD", odd)
    url = urlsplit(config.database_url())
    assert (url.hostname, url.port, url.path) == ("db", 5432, "/litwatch")
    assert unquote(url.password) == odd


def test_a_database_url_is_read_from_its_file(monkeypatch, tmp_path):
    (tmp_path / "url").write_text("postgresql://u@db/litwatch\n")
    monkeypatch.delenv("LITWATCH_DATABASE_URL", raising=False)
    monkeypatch.delenv("LITWATCH_DB_HOST", raising=False)
    monkeypatch.setenv("LITWATCH_DATABASE_URL_FILE", str(tmp_path / "url"))
    assert config.database_url() == "postgresql://u@db/litwatch"
