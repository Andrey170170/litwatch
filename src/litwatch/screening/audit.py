"""The keyword audit: no model is involved, so no model's blind spot applies."""

from __future__ import annotations

from litwatch.core import Paper, Topic, TopicKind, keyword_match


def audit(papers: list[Paper], topics: list[Topic]) -> dict[str, dict[str, str]]:
    """For each paper, the Watch topics whose keyword patterns it matches, with the pattern.

    Every paper is checked, whatever triage said and whether or not it joined a field.
    """
    watch = [t for t in topics if t.kind == TopicKind.WATCH]
    found: dict[str, dict[str, str]] = {}
    for paper in papers:
        hits = {t.slug: hit for t in watch if (hit := keyword_match(t, paper))}
        if hits:
            found[paper.id] = hits
    return found
