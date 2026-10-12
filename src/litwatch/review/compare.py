"""Choosing the writer's model: the same takes from several models, for the owner to read
blind. A one-off outside the app (spec decision 21); it writes files and stores no take."""

from __future__ import annotations

import asyncio
import json
import random
from pathlib import Path

import psycopg

from litwatch import llm
from litwatch.config import Config, RoleConfig
from litwatch.core import Grade, TopicKind
from litwatch.jobs.context import ModelRun
from litwatch.llm import CallError
from litwatch.review import writer
from litwatch.store import papers as papers_store
from litwatch.store import topics as topics_store
from litwatch.store import verdicts as verdicts_store


def role(spec: str) -> RoleConfig:
    """`provider:model@effort`, effort optional, as the journal prints a role."""
    provider, _, rest = spec.partition(":")
    model, _, effort = rest.partition("@")
    return RoleConfig(provider=provider, model=model, effort=effort or None)


async def _pairs(conn: psycopg.AsyncConnection, n: int) -> list[tuple[str, str, Grade]]:
    """The most recently settled pairs at relevant or above, must-reads first."""
    watched = [t.slug for t in await topics_store.all(conn, None) if t.kind == TopicKind.WATCH]
    rows = await (
        await conn.execute(
            "SELECT paper_id, topic, grade FROM effective_grades "
            "WHERE topic = ANY(%s) AND grade IN ('must_read', 'relevant') "
            "ORDER BY grade = 'relevant', updated_at DESC, paper_id LIMIT %s",
            (watched, n),
        )
    ).fetchall()
    return [(r["paper_id"], r["topic"], Grade(r["grade"])) for r in rows]


async def _one(gateway: llm.Gateway, paper, topic, grade, opinions) -> str:
    try:
        return await writer.take(gateway, paper, topic, grade, opinions)
    except CallError as exc:
        return f"(failed: {exc})"


async def compare(
    conn: psycopg.AsyncConnection,
    config: Config,
    run: ModelRun,
    specs: list[str],
    n: int,
    out: Path,
    seed: int,
) -> Path:
    """Write `takes.md` (papers with their takes under shuffled letters) and `key.json`."""
    roles = {spec: role(spec) for spec in [config.models.writer.label, *specs]}
    gateways = {
        spec: llm.build(config.models.model_copy(update={"writer": r}), run.journal)
        for spec, r in roles.items()
    }
    topics = {t.slug: t for t in await topics_store.all(conn, None)}
    rng = random.Random(seed)
    lines, key = [f"# Writer comparison (seed {seed})\n"], {}
    for i, (paper_id, slug, grade) in enumerate(await _pairs(conn, n), 1):
        paper, topic = await papers_store.get(conn, paper_id), topics[slug]
        verdict = await verdicts_store.current(conn, paper_id, slug)
        opinions = [o for o in (verdict.judge, verdict.check) if o] if verdict else []

        texts = await asyncio.gather(
            *(_one(g, paper, topic, grade, opinions) for g in gateways.values())
        )
        order = list(zip(roles, texts, strict=True))
        rng.shuffle(order)
        lines.append(f"## {i}. {paper.title}\n\n*{topic.name}, {grade.value.replace('_', ' ')}*\n")
        lines.append(f"> {paper.abstract}\n")
        for letter, (_, text) in zip("ABCDEFGH", order, strict=False):
            lines.append(f"**{letter}.** {text}\n")
        key[str(i)] = {letter: spec for letter, (spec, _) in zip("ABCDEFGH", order, strict=False)}
    out.mkdir(parents=True, exist_ok=True)
    (out / "takes.md").write_text("\n".join(lines))
    (out / "key.json").write_text(json.dumps(key, indent=2))
    return out
