"""Topic re-runs (pipeline.md, section 10; docs/plans/jobs.md, P2 and D3): after a topic edit,
the owner may grade a scope of papers for that topic again. A re-run records a new verdict per
paper under the topic's current version, each settling its paper, then works the Brief and
Weight re-checks its verdicts queued, all through its own gateway under its ceiling; its Deep
passes go to the Deep worker as background work.

The offer: each scope's papers, counted, and the estimate (calls by role times the mean cost of
the role's model over its last 200 calls billed at normal prices); the routes a ceiling cannot
bound, whose calls a re-run does not send."""

from __future__ import annotations

import logging
import math
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Literal

import psycopg

from litwatch import fulltext, grading
from litwatch.archive import ArchiveClient
from litwatch.config import Config, RoleConfig
from litwatch.core import Depth, Status, Taxonomy, Topic
from litwatch.jobs.context import ModelRun
from litwatch.llm.ceiling import Bound, Ceiling, anthropic_bound, route
from litwatch.llm.types import DEFAULT_MAX_TOKENS
from litwatch.store import calls, quality
from litwatch.store import fulltext as fulltext_store
from litwatch.store import jobs as jobs_store
from litwatch.store import papers as papers_store
from litwatch.store import verdicts as verdicts_store

type Scope = Literal["labels", "members", "keywords", "range"]
SCOPES: tuple[Scope, ...] = ("labels", "members", "keywords", "range")
CEILING = 5.0  # the offer's ceiling, until the owner changes it (DESIGN.md, the re-run offer)
GRADERS = {
    Depth.FOCUS: ("judge", "check"),
    Depth.AREA: ("judge", "area_check", "check"),
    Depth.FIELD: ("field_grade",),
}
WAKE = timedelta(seconds=30)  # a re-run whose passes all await their text looks again after

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Range:
    """A date range, both days included, in the schedule's zone."""

    since: date
    until: date


async def scope_papers(
    conn: psycopg.AsyncConnection,
    config: Config,
    tree: Taxonomy,
    topic: Topic,
    scope: Scope,
    dates: Range | None,
) -> list[str]:
    """The papers one scope names, not dismissed: those labelled for the topic or reported
    missed there; its members, as its page shows them; those its keywords match over the
    editor's 90 days; those published in the date range (none without one)."""
    match scope:
        case "labels":
            return await jobs_store.labelled(conn, topic.slug)
        case "members":
            return await jobs_store.members(conn, tree.subtree(topic.slug))
        case "keywords":
            return await quality.keyword_papers(conn, list(topic.keywords))
        case "range":
            if dates is None:
                return []
            zone = config.schedule.zone
            since = datetime.combine(dates.since, time(), zone)
            until = datetime.combine(dates.until + timedelta(days=1), time(), zone)
            return await jobs_store.published_between(conn, since, until)


async def per_paper(
    conn: psycopg.AsyncConnection, config: Config, topic: Topic
) -> tuple[float | None, list[str]]:
    """What grading one paper for the topic costs, by its graders' mean costs (an Area's `check`
    by the share of its verdicts that escalated), and the roles with no calls to estimate by:
    with any, no estimate."""
    total, missing = 0.0, []
    share = 1.0
    if topic.depth == Depth.AREA:
        share = await verdicts_store.escalated_share(conn, topic.slug)
    for role in GRADERS[topic.depth]:
        served = config.models.role(role)
        mean = await calls.mean_cost(conn, role, served.provider, served.model, served.effort)
        if mean is None:
            missing.append(role)
            continue
        total += mean * (share if topic.depth == Depth.AREA and role == "check" else 1.0)
    return (None if missing else total), missing


def _per_million(price: float) -> float:
    """A price per token as OpenRouter's per-million price, rounded up, so the bound sent never
    excludes the price it was read from."""
    return math.ceil(price * 1e6 * 1e6) / 1e6


async def bound(conn: psycopg.AsyncConnection, role: RoleConfig) -> Bound | None:
    """An OpenRouter route's price bound from what its model's last 200 calls were charged
    (`store.calls.charged_prices`), sent as `provider.max_price`: a chat route with its output
    cap where the role sets none (`DEFAULT_MAX_TOKENS`, as Anthropic's); a Decisions route
    bills its input only (models.md), so its output price is held at zero. None without
    history."""
    price_in, price_out = await calls.charged_prices(conn, role.provider, role.model)
    if price_in is None:
        return None
    if role.decisions:
        return Bound(price_in, 0.0, None, {"prompt": _per_million(price_in), "completion": 0})
    if price_out is None:
        return None
    sent = {"prompt": _per_million(price_in), "completion": _per_million(price_out)}
    return Bound(sent["prompt"] / 1e6, sent["completion"] / 1e6, DEFAULT_MAX_TOKENS, sent)


def roles(topic: Topic) -> tuple[str, ...]:
    """The roles a re-run of the topic calls: its graders, and `brief` for its passes."""
    return (*GRADERS[topic.depth], "brief")


async def bounds(
    conn: psycopg.AsyncConnection, config: Config, topic: Topic
) -> dict[str, Bound | None]:
    """The OpenRouter routes' bounds, for every model of the re-run's roles' lists."""
    found: dict[str, Bound | None] = {}
    for name in roles(topic):
        for model in config.models.role(name).models:
            if model.provider == "openrouter" and route(model) not in found:
                found[route(model)] = await bound(conn, model)
    return found


async def unpriced(conn: psycopg.AsyncConnection, config: Config, topic: Topic) -> list[str]:
    """The models of the re-run's roles' lists that have no price bound, as `role: model`: a
    re-run does not send their calls."""
    found = await bounds(conn, config, topic)
    out = []
    for name in roles(topic):
        for model in config.models.role(name).models:
            if model.provider == "anthropic":
                missing = anthropic_bound(model, 0) is None
            else:
                missing = found.get(route(model)) is None
            if missing:
                out.append(f"{name}: {model.model}")
    return out


async def ceiling(
    conn: psycopg.AsyncConnection, config: Config, job: dict[str, Any], topic: Topic
) -> Ceiling:
    """The job's ceiling as its run opens: what is left of it after what its runs spent."""
    left = float(job["ceiling"]) - await jobs_store.spent(conn, job["id"])
    return Ceiling(max(left, 0.0), await bounds(conn, config, topic))


@dataclass
class Chunk:
    """How one chunk of a re-run ended: `more` to do, its passes `waiting` for their text,
    `stopped` by its ceiling (`where`), `done` (`failed` its papers whose grading failed), or
    `gone` (no longer this session's)."""

    outcome: Literal["more", "waiting", "stopped", "done", "gone"]
    where: str | None = None
    failed: int = 0


def active_topic(tree: Taxonomy | None, slug: str) -> Topic | None:
    topic = tree.topic(slug) if tree else None
    return topic if topic and topic.status == Status.ACTIVE else None


async def work(
    conn: psycopg.AsyncConnection,
    config: Config,
    run: ModelRun,
    job: dict[str, Any],
    tree: Taxonomy,
    archive: ArchiveClient | None,
    size: int,
) -> Chunk:
    """The re-run's next chunk: grade the next `size` papers of its scope that have no verdict
    under the topic's current version (D3), each recorded verdict settling its paper; once its
    scope is graded, work the text passes it queued. Calls go through the run's gateway, under
    the job's ceiling."""
    slug = job["scope"]["topic"]
    topic = active_topic(tree, slug)
    assert topic is not None  # the worker ends a re-run whose topic is not active
    counts: Counter[str] = Counter()
    papers = job["scope"]["papers"]
    if job["done"] < len(papers):
        ids = papers[job["done"] : job["done"] + size]
        todo = await verdicts_store.ungraded_under(conn, slug, topic.version, ids)
        found = await papers_store.get_many(conn, todo)
        due = [grading.Due(p, topic, tree.get(slug).name) for p in found]
        failed: list[grading.Graded] = []
        async with grading.grade(due, run.gateway, config.grading) as results:
            async for g in results:
                if g.verdict is not None:
                    if await grading.record(conn, g, job=job["id"]):
                        counts["graded"] += 1
                else:
                    log.warning("re-run %s: %s failed: %s", job["id"], g.due.paper.id, g.error)
                    failed.append(g)
        reached = run.gateway.ceiling is not None and run.gateway.ceiling.reached
        if not reached:  # a grading the ceiling cut short is not a failure: it is done on
            counts["grading_failed"] += len(failed)
        after = await _progress(conn, job, run, counts, job["done"] + (0 if reached else len(ids)))
        if after is None:
            return Chunk("gone")
        if reached:
            graded = after["counts"].get("graded", 0)
            return Chunk("stopped", f"stopped at the ceiling after {graded} of {len(papers)}")
        return Chunk("more")
    if archive is not None:
        done = await fulltext.work(conn, config, archive, run=run, job=job["id"], limit=size)
        counts.update({k: v for k, v in done.items() if k != "awaiting_text"})
        after = await _progress(conn, job, run, counts, job["done"], on_run=False)
        if after is None:
            return Chunk("gone")
        if run.gateway.ceiling is not None and run.gateway.ceiling.reached:
            left = await fulltext_store.job_open(conn, job["id"])
            return Chunk("stopped", f"stopped at the ceiling with {left} full-text passes left")
        if await fulltext_store.job_open(conn, job["id"]):
            return Chunk("more" if fulltext.worked(done) else "waiting")
    failed_n = job["counts"].get("grading_failed", 0) + counts["grading_failed"]
    return Chunk("done", failed=failed_n)


async def _progress(
    conn: psycopg.AsyncConnection,
    job: dict[str, Any],
    run: ModelRun,
    counts: Counter[str],
    done: int,
    *,
    on_run: bool = True,
) -> dict[str, Any] | None:
    """Record the chunk on the job and, unless they are there already (`on_run`), on its run;
    None when the job is no longer this session's."""
    merged = dict(job["counts"])
    for k, v in counts.items():
        merged[k] = merged.get(k, 0) + v
        if on_run:
            run.add(k, v)
    return await jobs_store.progress(conn, job["id"], done, merged)
