"""Turn triage and audit results into memberships and candidates."""

from __future__ import annotations

import psycopg

from litwatch.core import Candidate, Grade, Paper, Topic, TopicKind
from litwatch.screening.triage import Triaged
from litwatch.store import triage as triage_store
from litwatch.store import verdicts


async def _graded_before_rewrite(
    conn: psycopg.AsyncConnection, paper: Paper, topic: str
) -> Grade | None:
    """The grade the paper's earlier text holds for the topic, when it is above `none`."""
    verdict = await verdicts.current(conn, paper.id, topic)
    if verdict and verdict.paper_revision < paper.revision and verdict.grade != Grade.NONE:
        return verdict.grade
    return None


async def record(
    conn: psycopg.AsyncConnection,
    triaged: Triaged,
    keywords: dict[str, str],
    topics: list[Topic],
) -> list[Candidate]:
    """Store one paper's triage outcome. Returns the candidates it became.

    `keywords` maps Watch topic slugs to the pattern the audit matched for this paper. A
    keyword match makes a candidate even when the triage call failed; the paper itself
    stays untriaged and is tried again.
    """
    paper = triaged.paper
    revisions = {t.slug: t.revision for t in topics}
    candidates = []
    async with conn.transaction():
        if triaged.error is None:
            answers = {s: {"yes": yes, "why": why} for s, (yes, why) in triaged.answers.items()}
            await triage_store.store_done(conn, paper, answers, revisions, triaged.model)
            fields = {t.slug: t.revision for t in topics if t.kind != TopicKind.WATCH}
            members = {slug for slug in fields if triaged.yes(slug)}
            await triage_store.set_memberships(conn, paper, fields, members)
        else:
            await triage_store.store_failed(conn, paper, triaged.error)
        for topic in topics:
            if topic.kind != TopicKind.WATCH:
                continue
            by_triage, pattern = triaged.yes(topic.slug), keywords.get(topic.slug)
            if by_triage:
                why = triaged.answers[topic.slug][1]
            elif pattern:
                why = f"keyword: {pattern}"
            elif triaged.error is None and (
                was := await _graded_before_rewrite(conn, paper, topic.slug)
            ):
                # Nothing puts the new text forward, but the old text has a verdict that
                # would stay current for ever. The graders decide, not triage alone.
                why = f"rewritten: was {was.value}"
            else:
                continue
            candidate = Candidate(
                paper_id=paper.id,
                topic=topic.slug,
                topic_revision=topic.revision,
                paper_version=paper.version,
                paper_revision=paper.revision,
                by_triage=by_triage,
                by_keyword=bool(pattern),
                reason=why,
            )
            await triage_store.put_candidate(conn, candidate)
            candidates.append(candidate)
    return candidates
