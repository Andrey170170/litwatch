"""Calibrate: the one place that asks (spec §8 M3, decision 20).

A session holds about twenty papers, split evenly across the active Focus topics and, within each,
half drawn at random from the papers graded relevant or above in the trailing 30 days and
half from those graded below, all without a label for that topic. On request it continues
with papers waiting for a word: last night's splits and papers carrying a word-carrying
thumb. Each answer is stored as it is given.
"""

from __future__ import annotations

import random
from typing import Any, Literal

from pydantic import BaseModel

from litwatch.core import (
    Cause,
    Depth,
    Fit,
    Grade,
    Label,
    LabelOrigin,
    LabelValue,
    Status,
    Weight,
    grade,
)
from litwatch.feedback import word
from litwatch.gates.database import DatabaseGate, ReadUnit
from litwatch.store import calibration as store
from litwatch.store import taxonomy as taxonomy_store

SESSION = 20
DRAW_DAYS = 30
PRECISION_DAYS = 90


async def _sides(unit: ReadUnit, topic: str) -> dict[bool, list[dict[str, Any]]]:
    """The topic's current verdicts of the trailing days, made on the paper's current text and
    with no label, read once and split by side of the line (True: relevant or above), each in
    the pool's order. The side is the pipeline's grade, from the verdict's Fit and the current
    Weight, whatever the owner said."""
    sides: dict[bool, list[dict[str, Any]]] = {True: [], False: []}
    for r in await store.pool(unit, topic, DRAW_DAYS):
        weight = r["weight"] and Weight(r["weight"])
        sides[grade(Fit(r["fit"]), weight).at_least(Grade.RELEVANT)].append(r)
    return sides


async def draw(db: DatabaseGate, seed: int, size: int = SESSION) -> int:
    """Draw a session and return its id. The seed is recorded, so the draw can be repeated."""
    async with db.write("calibrate draw") as unit:
        active = await taxonomy_store.topics(unit, Status.ACTIVE)
        focus = [t.slug for t in active if t.depth == Depth.FOCUS]
        per_group = max(1, size // (2 * max(1, len(focus))))
        rng = random.Random(seed)
        picked: list[tuple[str, str, str, int | None]] = []
        seen: set[str] = set()
        for topic in focus:
            sides = await _sides(unit, topic)
            for above in (True, False):
                pool = [r for r in sides[above] if r["paper_id"] not in seen]
                for row in rng.sample(pool, min(per_group, len(pool))):
                    seen.add(row["paper_id"])
                    picked.append(
                        (row["paper_id"], topic, "above" if above else "below", row["verdict_id"])
                    )
        return await store.new_session(unit, seed, picked)


async def more(db: DatabaseGate, session_id: int) -> int:
    """Add the papers waiting for a word that are not in the session yet: splits that
    straddle the line, reported misses not yet marked, and papers with a word-carrying thumb
    but no label. Returns how many."""
    async with db.write("calibrate more") as unit:
        return await store.add(unit, session_id, await store.waiting(unit))


class Answer(BaseModel):
    """One paper's answers. Fit for the topic it was drawn or taken for (central, related,
    adjacent, none or unsure) is required on the first answer; an edit after it may leave it
    out, and the recorded answer stands. A Fit sent is a label; one left out writes none, so
    editing the appraisal alone recreates no label the owner has since taken back."""

    fit: dict[str, LabelValue]
    value: Literal["matters", "fine", "not_for_me"] | None = None
    note: str | None = None


async def answer(
    db: DatabaseGate, session_id: int, position: int, given: Answer, *, rules: word.Rules
) -> None:
    """Store an item's answers as one change of its paper (`word.change`): its labels are
    written together and settled once, by `rules`, so a Fit moved from one topic to another
    does not take the paper out of the library and back. LookupError for no such item;
    ValueError for a missing Fit or one for a topic not at Focus."""
    async with db.read("calibrate item") as unit:
        item = await store.item(unit, session_id, position)
    if item is None:
        raise LookupError("no such item")
    if item["topic"] not in given.fit and item["answered_at"] is None:
        raise ValueError(f"fit for {item['topic']} is required")
    async with word.change(
        db, item["paper_id"], cause=Cause.OWNER, rules=rules, name="calibrate answer"
    ) as done:
        unit = done.unit
        focus = {t.slug for t in await taxonomy_store.topics(unit) if t.depth == Depth.FOCUS}
        if unknown := sorted(set(given.fit) - focus):
            raise ValueError(f"not Focus topics: {', '.join(unknown)}")
        # A label already saying this Fit is not written again: writing renews its time,
        # which would put it above a reaction given since (the newest word wins).
        held = await store.label_values(unit, item["paper_id"])
        for topic, fit in given.fit.items():
            if held.get(topic) == fit.value:
                continue
            # The drawn topic's answer is the sample; any other is the owner's own choice.
            drawn = topic == item["topic"] and item["why"] in ("above", "below")
            await word.label(
                unit,
                Label(
                    paper_id=item["paper_id"],
                    topic=topic,
                    value=fit,
                    origin=LabelOrigin.SAMPLE if drawn else LabelOrigin.READER,
                ),
            )
        # An appraisal changed, cleared included, is a new row; the same one sent again is not.
        said = (given.value, given.note or None)
        if said != await store.appraisal(unit, item["paper_id"]):
            await store.appraise(unit, item["paper_id"], *said)
        answered = given.fit.get(item["topic"])
        await store.answered(unit, session_id, position, answered.value if answered else None)


async def sessions(unit: ReadUnit, limit: int = 20) -> list[dict[str, Any]]:
    """The newest sessions: how many papers each drew, how many are answered, and how many
    answers differ from the pipeline's Fit."""
    return await store.sessions(unit, limit)


async def session(unit: ReadUnit, session_id: int) -> dict[str, Any] | None:
    """A session's papers in order, each with the pipeline's Fit on the drawn topic (the verdict
    the draw read, else the current one), the answer recorded for it (`answer`, null until
    given) and what the owner has said since (`fit`, every current label; `appraisal`)."""
    return await store.session(unit, session_id)


async def precision(unit: ReadUnit) -> list[dict[str, Any]]:
    """Per Focus topic over the trailing 90 days of draws: precision at relevant (the drawn
    upper group answered central or related) and how often grading dropped a wanted paper (the
    lower group answered so). Unsure and unanswered draws count nowhere."""
    out: dict[str, dict[str, Any]] = {}
    for r in await store.drawn(unit, PRECISION_DAYS):
        entry = out.setdefault(r["topic"], {"topic": r["topic"]})
        share = r["relevant"] / r["answered"] if r["answered"] else None
        key = "precision" if r["why"] == "above" else "dropped"
        entry[key] = share
        entry[f"{key}_n"] = r["answered"]
    return list(out.values())


async def matters_no_topic(unit: ReadUnit) -> list[dict[str, Any]]:
    """Papers whose newest appraisal is "matters" and that are below relevant for every
    Focus topic, earlier words included: a missing topic or a narrow rubric."""
    return await store.matters_no_topic(unit)
