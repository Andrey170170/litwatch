"""Classification's decision request, built from the tree, and its answer read back as class
probabilities and a primary class (docs/specs/v1/pipeline.md, section 2). The nightly step
arrives in N2a; the comparison of classifiers (`litwatch eval classify`) uses this now."""

from __future__ import annotations

from litwatch.core.classification import Classified
from litwatch.core.taxonomy import Taxonomy
from litwatch.llm.decisions import Decision
from litwatch.llm.types import Choice, DecisionRequest, Predicate, Question

ROLE = "classify"
# Not a possible class slug (slugs have no underscore), so it never names a yes/no question.
PRIMARY = "primary_class"


def questions(tree: Taxonomy) -> tuple[Question, ...]:
    """A yes/no question per class and one choice of the primary class, offered by name:
    decision models choose by an option's name more than by its definition."""
    per_class = tuple(
        Predicate(c.slug, f"Does this arXiv paper belong to {c.name}: {c.description}")
        for c in tree.classes
    )
    primary = Choice(
        PRIMARY,
        "Which one class does this arXiv paper belong to most? Judge by its main contribution, "
        "not by every subject it mentions. Choose the most specific class that fits; choose a "
        "parent only when none of its children fits.",
        tuple((c.name, c.description) for c in tree.classes),
    )
    return (*per_class, primary)


def request(tree: Taxonomy, title: str, abstract: str) -> DecisionRequest:
    return DecisionRequest(f"Title: {title}\n\nAbstract: {abstract}", questions(tree))


def classified(tree: Taxonomy, decision: Decision) -> Classified:
    """The decision in class slugs. The gateway has checked that the choice is an option, so
    it names a class of this tree."""
    by_name = {c.name: c.slug for c in tree.classes}
    return Classified(
        probabilities=decision.probabilities,
        primary=by_name[decision.choices[PRIMARY].option],
    )
