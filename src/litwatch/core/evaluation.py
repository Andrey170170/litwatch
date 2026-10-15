"""What a replay concluded for a paper and a Focus topic, and the four recall numbers over the
papers a cohort holds positive (docs/specs/v1/feedback.md, evaluation):

- classification: the classifier alone made the paper a member, at threshold;
- membership: the classifier, the uncertain band or the keyword check did;
- grading: graded related or above, wherever classification put it;
- end to end: a member and graded related or above.

The owner's memberships (source `owner` or `miss`) are never among them: a replay sorts the paper
afresh, blind to the owner. A failed classification makes no member except by keyword, and a
failed grading counts as a miss. A stage that has not run yet (a run cut short) leaves the paper
out of that number's denominator, so each number says over how many papers it stands.
"""

from __future__ import annotations

from collections.abc import Iterable

from pydantic import BaseModel

from litwatch.core.grading import Fit

SURFACED = (Fit.CENTRAL, Fit.RELATED)


class Outcome(BaseModel):
    """One paper and Focus topic in a replay, with the cohort's label on it."""

    paper_id: str
    topic: str
    cohort: str
    positive: bool
    classifier: bool | None = None  # None: not classified, or classification failed
    uncertain: bool | None = None
    keyword: str | None = None  # the pattern the keyword check matched
    error: str | None = None  # why classification failed
    fit: Fit | None = None
    grade_error: str | None = None  # why grading failed

    @property
    def classified(self) -> bool:
        """Classification ran and concluded, by an answer or a failure."""
        return self.classifier is not None or self.error is not None

    @property
    def graded(self) -> bool:
        return self.fit is not None or self.grade_error is not None

    @property
    def member(self) -> bool:
        return bool(self.classifier) or bool(self.uncertain) or self.keyword is not None

    @property
    def surfaced(self) -> bool:
        return self.fit in SURFACED


class Share(BaseModel):
    hits: int
    n: int

    @property
    def value(self) -> float | None:
        return self.hits / self.n if self.n else None


class Recall(BaseModel):
    """The four numbers for one cohort and Focus topic, each over the positives the stages it
    needs have concluded on; and how many of the cohort's negatives came through end to end."""

    cohort: str
    topic: str
    positives: int
    classification: Share
    membership: Share
    grading: Share
    end_to_end: Share
    grading_failed: int
    negatives: Share  # members graded related or above, among the cohort's negatives


def _share(outcomes: list[Outcome], hit) -> Share:
    return Share(hits=sum(bool(hit(o)) for o in outcomes), n=len(outcomes))


def recall(outcomes: Iterable[Outcome]) -> list[Recall]:
    """One line per cohort and topic, in the order first met; never pooled across cohorts."""
    groups: dict[tuple[str, str], list[Outcome]] = {}
    for o in outcomes:
        groups.setdefault((o.cohort, o.topic), []).append(o)
    lines = []
    for (cohort, topic), group in groups.items():
        held = [o for o in group if o.positive]
        classified = [o for o in held if o.classified]
        graded = [o for o in held if o.graded]
        both = [o for o in held if o.classified and o.graded]
        others = [o for o in group if not o.positive and o.classified and o.graded]
        lines.append(
            Recall(
                cohort=cohort,
                topic=topic,
                positives=len(held),
                classification=_share(classified, lambda o: o.classifier),
                membership=_share(classified, lambda o: o.member),
                grading=_share(graded, lambda o: o.surfaced),
                end_to_end=_share(both, lambda o: o.member and o.surfaced),
                grading_failed=sum(o.grade_error is not None and o.fit is None for o in held),
                negatives=_share(others, lambda o: o.member and o.surfaced),
            )
        )
    return lines
