"""The full-text queue and what its passes wrote (data.md: `fulltext_passes`, `briefs`, and the
full-text rows of `weights` and `gists`).

Each pass of the regular queue (not a re-run's, which its job works) has a work item, keyed by
the pass's id, of the kind its Worker runs (`work_kind`; docs/plans/a2-work-gate.md, P2): every
write here that leaves such a pass where a claim can take it (ready, awaiting its text, or
running with its claim let go) submits the item in its unit, for when it may be taken, and
`submit_open` submits what is open when a process starts serving them. The item claims its
pass as every worker does (`claim`), so a pass worked inline by the nightly leaves its item
nothing to do. As in the queue before: due passes run in `claim`'s order of priorities
(`_place`), text passes a wave's width at once, and Deep passes one at a time (`_admission`).
A paper brought back after a withdrawal has its passes submitted again (`submit_restored`)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from litwatch.core import FAILED_RUNS, Paper, TaxonomyClass, Weight
from litwatch.core.digests import PUBLISHED
from litwatch.core.fulltext import (
    AUTOMATIC_PRIORITY,
    AWAITING_TOO_LONG,
    BACKGROUND_PRIORITY,
    REQUEST_PRIORITY,
    REQUESTED,
    TEXT_PRIORITY,
    TEXT_REQUEST_PRIORITY,
    TEXT_WAIT,
    TEXT_WAVE,
    UNSTARTED,
    DeepPass,
    PassKind,
    PassState,
    TextPass,
    TextRead,
)
from litwatch.gates.database import ReadUnit, WriteUnit
from litwatch.store import work
from litwatch.store.papers import COLUMNS, row_to_paper
from litwatch.store.taxonomy import require_lock, row_to_class

# The kinds of the passes' work items: text passes, and Deep passes, each run by its Worker.
TEXT_WORK = "fulltext"
DEEP_WORK = "deep"
# The states in which a claim can take a pass (a running one once its claim's lease lapsed).
_CLAIMABLE = [PassState.READY.value, PassState.AWAITING_TEXT.value, PassState.RUNNING.value]


# Where in line a pass's item that is due now goes: `take` runs due items in `not_before` order,
# so it is submitted at a time long past that stands for the pass's priority, a day apart from
# the next, as `claim` takes passes: the owner's, then the automatic ones, then a re-run's. A
# pending item submitted again is brought forward to it (the owner's request jumping the line).
# One that waits (for its text, out the Deep pause) is due at its wait's end instead.
_LINE = datetime(1970, 1, 1, tzinfo=UTC)


def _place(priority: int) -> datetime:
    """The `not_before` of a pass due now at `priority`."""
    return _LINE + timedelta(days=priority - TEXT_REQUEST_PRIORITY)


def work_kind(kind: PassKind) -> str:
    """The kind of work item a pass of `kind` is."""
    return DEEP_WORK if kind == PassKind.DEEP else TEXT_WORK


def _admission(kind: PassKind, pass_id: int) -> str | None:
    """Which items a pass's item is admitted one at a time with: a text pass's spread over
    `TEXT_WAVE` slots by its id, so the service works as many text passes at once as a wave
    did; Deep passes one at a time, by their kind."""
    return None if kind == PassKind.DEEP else f"{TEXT_WORK}:{pass_id % TEXT_WAVE}"


async def _submit(unit: WriteUnit, pass_id: int, at: datetime | None = None) -> None:
    """Submit the pass's work item, to run no earlier than `at`; when not given, now, in its
    priority's place in line (`_place`). A pass gone meanwhile (a sweep reads its passes
    without locking them) has none."""
    row = await (
        await unit.execute("SELECT kind, priority FROM fulltext_passes WHERE id = %s", (pass_id,))
    ).fetchone()
    if row is None:
        return
    kind = PassKind(row["kind"])
    await work.submit(
        unit,
        work_kind(kind),
        str(pass_id),
        not_before=_place(row["priority"]) if at is None else at,
        admission=_admission(kind, pass_id),
    )


async def submit_open(unit: WriteUnit, *, deep: bool = False) -> int:
    """Submit the work item of each text pass of the regular queue that a claim can take, or
    with `deep` of each Deep pass: what a process that starts serving them finds queued or
    runnable, a pass claimed by a process now gone included. Returns how many."""
    rows = await (
        await unit.execute(
            "SELECT id FROM fulltext_passes WHERE job_id IS NULL AND state = ANY(%s) "
            "AND (kind = 'deep') = %s ORDER BY id",
            (_CLAIMABLE, deep),
        )
    ).fetchall()
    for r in rows:
        await _submit(unit, r["id"])
    return len(rows)


async def submit_restored(unit: WriteUnit, paper_ids: Sequence[str]) -> None:
    """Submit the work item of each pass of the regular queue that a claim can take, of those
    of the papers not dropped: for papers withdrawn and brought back by a later version, in the
    unit that stores it. While a paper was dropped no claim took its passes, and their items
    ended with nothing to do; a version that changes neither title nor abstract queues none."""
    rows = await (
        await unit.execute(
            "SELECT f.id FROM fulltext_passes f JOIN papers p ON p.id = f.paper_id "
            "WHERE f.paper_id = ANY(%s) AND NOT p.dropped AND f.job_id IS NULL "
            "AND f.state = ANY(%s) ORDER BY f.id",
            (list(paper_ids), _CLAIMABLE),
        )
    ).fetchall()
    for r in rows:
        await _submit(unit, r["id"])


# Whether text pass `f` read paper `p`'s text for its latest arXiv update, or none is known: what
# keeps a done pass current while the paper's version is unknown.
COVERS_UPDATE = "coalesce(f.paper_updated >= p.updated, p.updated IS NULL)"

_PUBLISHED = "(" + ", ".join(f"'{s.value}'" for s in PUBLISHED) + ")"
# The topics whose published digests picked paper `p`'s current revision: a pick of text that a
# rewrite replaced was a judgement of text that no longer exists.
PICKED_BY = f"""ARRAY(
    SELECT DISTINCT d.topic FROM digest_papers dp JOIN digests d ON d.id = dp.digest_id
    WHERE dp.paper_id = p.id AND dp.role = 'pick' AND dp.paper_revision = p.revision
      AND d.state IN {_PUBLISHED})"""


@dataclass(frozen=True)
class Facts:
    """What deciding a paper's text pass reads, under its settle lock."""

    revision: int
    version: int | None
    dropped: bool  # dismissed or withdrawn (`papers.dropped`)
    last: TextPass | None  # the newest text pass queued for the current revision
    has_text_result: bool  # a text pass of any revision is done
    stale: bool  # a text pass for an older revision is not done: `drop_stale` may remove it


async def facts_of(unit: ReadUnit, paper_ids: list[str]) -> dict[str, Facts]:
    """The facts of each of the papers litwatch has, by paper."""
    rows = await (
        await unit.execute(
            f"""
            SELECT p.id AS paper_id, p.revision, p.version, p.dropped,
                   f.id, f.kind, f.reason, f.state, f.arxiv_version,
                   NOT {COVERS_UPDATE} AS newer_update,
                   EXISTS (SELECT 1 FROM fulltext_passes d WHERE d.paper_id = p.id
                           AND d.kind <> 'deep' AND d.state = 'done') AS has_text_result,
                   EXISTS (SELECT 1 FROM fulltext_passes s WHERE s.paper_id = p.id
                           AND s.paper_revision < p.revision AND s.kind <> 'deep'
                           AND s.state <> 'done') AS stale
            FROM papers p
            LEFT JOIN LATERAL (
                SELECT id, kind, reason, state, arxiv_version, paper_updated FROM fulltext_passes
                WHERE paper_id = p.id AND paper_revision = p.revision AND kind <> 'deep'
                ORDER BY id DESC LIMIT 1
            ) f ON true
            WHERE p.id = ANY(%s)
            """,
            (paper_ids,),
        )
    ).fetchall()
    found: dict[str, Facts] = {}
    for row in rows:
        last = (
            TextPass(
                row["id"],
                PassKind(row["kind"]),
                row["reason"],
                PassState(row["state"]),
                row["arxiv_version"],
                row["state"] == PassState.DONE and row["newer_update"],
            )
            if row["id"]
            else None
        )
        found[row["paper_id"]] = Facts(
            row["revision"],
            row["version"],
            row["dropped"],
            last,
            row["has_text_result"],
            row["stale"],
        )
    return found


@dataclass(frozen=True)
class DeepFacts:
    """What deciding a paper's Deep pass reads, under its settle lock."""

    revision: int
    dropped: bool  # dismissed or withdrawn
    open_pass: DeepPass | None  # the one not done
    has_card: bool  # of any revision
    picked_by: frozenset[str]  # the topics whose published digests picked its current revision


async def deep_facts(unit: ReadUnit, paper_id: str) -> DeepFacts | None:
    return (await deep_facts_of(unit, [paper_id])).get(paper_id)


async def deep_facts_of(unit: ReadUnit, paper_ids: list[str]) -> dict[str, DeepFacts]:
    """The Deep facts of each of the papers litwatch has, by paper."""
    rows = await (
        await unit.execute(
            "SELECT p.id AS paper_id, p.revision, p.dropped, f.id, f.reason, f.state, "
            "EXISTS (SELECT 1 FROM deep_cards c WHERE c.paper_id = p.id AND c.card IS NOT NULL) "
            f"AS has_card, {PICKED_BY} AS picked_by FROM papers p LEFT JOIN fulltext_passes f "
            "ON f.paper_id = p.id AND f.kind = 'deep' AND f.state <> 'done' "
            "WHERE p.id = ANY(%s)",
            (paper_ids,),
        )
    ).fetchall()
    found: dict[str, DeepFacts] = {}
    for row in rows:
        open_pass = (
            DeepPass(row["id"], row["reason"], PassState(row["state"])) if row["id"] else None
        )
        found[row["paper_id"]] = DeepFacts(
            row["revision"], row["dropped"], open_pass, row["has_card"], frozenset(row["picked_by"])
        )
    return found


async def request_card(unit: WriteUnit, paper_id: str, revision: int) -> int:
    """The owner asks for a Deep card: their request takes over the paper's open Deep pass, an
    automatic one included, so an Unsave never cancels it and it goes first in line, and a
    failed one is tried again afresh; without an open one, one is queued. Call it under the
    paper's settle lock. Returns the pass's id."""
    found = await deep_facts(unit, paper_id)
    open_pass = found.open_pass if found else None
    if open_pass is not None:
        await unit.execute(
            "UPDATE fulltext_passes SET reason = %s, priority = %s, "
            "retry_text = retry_text OR state = 'failed', "
            "failed_runs = CASE WHEN state = 'failed' THEN 0 ELSE failed_runs END, "
            "acknowledged_at = CASE WHEN state = 'failed' THEN NULL ELSE acknowledged_at END, "
            "state = CASE WHEN state = 'failed' THEN 'ready' ELSE state END WHERE id = %s",
            (REQUESTED, REQUEST_PRIORITY, open_pass.id),
        )
        await _submit(unit, open_pass.id)
        return open_pass.id
    return await queue(unit, paper_id, revision, PassKind.DEEP, REQUESTED, REQUEST_PRIORITY)


async def queue_deep(
    unit: WriteUnit,
    paper_id: str,
    revision: int,
    reason: str,
    priority: int = AUTOMATIC_PRIORITY,
) -> int:
    return await queue(unit, paper_id, revision, PassKind.DEEP, reason, priority)


async def owners_first(unit: WriteUnit, paper_ids: Sequence[str]) -> None:
    """The papers' open passes go as the owner's requests do: a text pass ahead of the others,
    a Deep pass ahead of the automatic ones (a paper the owner had processed, docs/plans/jobs.md),
    in the regular queue, a re-run's text pass too. The reason stays: a settle may still drop or
    relabel the pass. A pass this moves (its priority raised, or taken from a re-run) has its
    work item submitted, ahead of the automatic ones, if a claim can take it; one already the
    owner's is left alone, so settling the paper again does not cut a try's wait short."""
    first = "CASE WHEN kind = 'deep' THEN %(deep)s ELSE %(text)s END"
    rows = await (
        await unit.execute(
            f"UPDATE fulltext_passes SET priority = least(priority, {first}), job_id = NULL "
            "WHERE paper_id = ANY(%(papers)s) AND state <> 'done' "
            f"AND (priority > {first} OR job_id IS NOT NULL) RETURNING id, state",
            {
                "deep": REQUEST_PRIORITY,
                "text": TEXT_REQUEST_PRIORITY,
                "papers": list(paper_ids),
            },
        )
    ).fetchall()
    for r in rows:
        if r["state"] in _CLAIMABLE:
            await _submit(unit, r["id"])


async def open_deep(unit: ReadUnit, paper_id: str) -> dict[str, Any] | None:
    """The paper's open Deep pass, as the paper page shows it."""
    return await (
        await unit.execute(
            "SELECT id, state, reason, queued_at, failed_runs, last_error AS error "
            "FROM fulltext_passes WHERE paper_id = %s AND kind = 'deep' AND state <> 'done'",
            (paper_id,),
        )
    ).fetchone()


async def queue(
    unit: WriteUnit,
    paper_id: str,
    revision: int,
    kind: PassKind,
    reason: str,
    priority: int,
    job_id: int | None = None,
) -> int:
    """Queue a pass; one `job_id` names is that re-run's to work (docs/plans/jobs.md), and the
    others have their work item submitted."""
    row = await (
        await unit.execute(
            "INSERT INTO fulltext_passes (paper_id, paper_revision, kind, reason, priority, "
            "job_id) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (paper_id, revision, kind.value, reason, priority, job_id),
        )
    ).fetchone()
    assert row is not None  # the insert returns it
    if job_id is None:
        await _submit(unit, row["id"])
    return row["id"]


def _unstarted(claimed: bool) -> list[str]:
    # A pass the caller has claimed but not yet worked counts as not started.
    return [s.value for s in UNSTARTED] + ([PassState.RUNNING.value] if claimed else [])


async def relabel(
    unit: WriteUnit, pass_id: int, kind: PassKind, reason: str, *, claimed=False
) -> None:
    """Say why a pass that has not started (or that the caller `claimed`) runs now."""
    await unit.execute(
        "UPDATE fulltext_passes SET kind = %s, reason = %s WHERE id = %s AND state = ANY(%s)",
        (kind.value, reason, pass_id, _unstarted(claimed)),
    )


async def drop(unit: WriteUnit, pass_id: int, *, claimed: bool = False) -> None:
    """Remove a pass that has not started (or that the caller `claimed`); it wrote nothing."""
    await unit.execute(
        "DELETE FROM fulltext_passes WHERE id = %s AND state = ANY(%s)",
        (pass_id, _unstarted(claimed)),
    )


async def drop_stale(
    unit: WriteUnit, paper_id: str, revision: int, claimed: int | None = None
) -> None:
    """Remove the text passes for older revisions of the paper that wrote nothing and are not
    running (not started, `claimed` but not yet started included, or their worker died): their
    text no longer exists. A running one finishes and stores its result labelled with its
    revision."""
    await unit.execute(
        "DELETE FROM fulltext_passes WHERE paper_id = %s AND paper_revision < %s "
        "AND kind <> 'deep' AND (state = ANY(%s) OR id = %s "
        "OR (state = 'running' AND lease_until < now()))",
        (paper_id, revision, [s.value for s in UNSTARTED], claimed),
    )


@dataclass(frozen=True)
class Claimed:
    """A pass a worker holds until `lease_until`: its paper as stored now, and the area its
    Weight is judged against (the primary class of its newest classification; None before
    any)."""

    id: int
    kind: PassKind
    reason: str
    revision: int  # the revision the pass was queued for
    paper: Paper
    area: TaxonomyClass | None
    lease_until: datetime
    retry_text: bool = False  # the archive is to retry the downloads it gave up on
    use_pdf: bool = False  # the owner's "Use the PDF": read its PDF, not its LaTeX
    unusable_runs: int = 0  # how far down `brief`'s list it starts (models.md, roles)
    job: int | None = None  # the re-run whose pass it is, which its settling keeps working


# Whether Deep pass `f` has an attempt open: its job was submitted, or may have been.
STARTED = "EXISTS (SELECT 1 FROM deep_attempts a WHERE a.pass_id = f.id AND a.ended_at IS NULL)"


async def claim(
    unit: WriteUnit,
    lease: timedelta,
    limit: int | None = None,
    besides: set[int] | None = None,
    *,
    deep: bool = False,
    owners: bool | None = None,
    job: int | None = None,
    background: bool = True,
    pass_id: int | None = None,
) -> list[Claimed]:
    """Take up to `limit` text passes to try, least recently tried first, `besides` those
    already tried: passes awaiting text, those to be tried again, and running ones whose lease
    ran out (the worker died). Each is held for `lease`. Only passes for their paper's current
    revision are taken (`drop_stale` removes the others); a dropped paper's are left alone.
    With `deep`, Deep passes instead, the owner's requests first, of any revision (a Deep pass
    reads the text the paper has when it runs); one whose job was submitted is taken whatever a
    dismissal says, as following its job spends nothing new and ends its attempt. Text passes
    the owner ran now only when `owners` is True, none of them when it is False. Text passes a
    re-run works only for that `job`, and none of them without it. Without `background`, no
    Deep pass at a background priority but one whose job was submitted (a nightly is running
    or due, pipeline.md, section 10). With `pass_id`, that pass alone (its work item's
    claim)."""
    which = (
        f"f.kind = 'deep' AND ({STARTED} OR NOT p.dropped)"
        if deep
        else "f.kind <> 'deep' AND f.paper_revision = p.revision AND NOT p.dropped "
        "AND f.job_id IS NOT DISTINCT FROM %(job)s"
    )
    if not deep and owners is not None:
        which += f" AND f.priority {'<' if owners else '>='} {TEXT_PRIORITY}"
    if deep and not background:
        which += f" AND (f.priority < {BACKGROUND_PRIORITY} OR {STARTED})"
    rows = await (
        await unit.execute(
            f"""
            WITH taken AS (
                SELECT f.id FROM fulltext_passes f JOIN papers p ON p.id = f.paper_id
                WHERE {which} AND NOT f.id = ANY(%(besides)s)
                  AND (%(pass)s::bigint IS NULL OR f.id = %(pass)s)
                  AND (f.state IN ('awaiting_text', 'ready')
                       OR (f.state = 'running' AND f.lease_until < now()))
                ORDER BY f.priority, f.tried_at NULLS FIRST, f.id
                LIMIT %(limit)s FOR UPDATE OF f SKIP LOCKED
            ), claimed AS (
                UPDATE fulltext_passes f SET state = 'running', tried_at = now(),
                                             lease_until = now() + %(lease)s
                FROM taken WHERE f.id = taken.id
                RETURNING f.id, f.kind, f.reason, f.paper_revision, f.lease_until, f.paper_id,
                          f.retry_text, f.use_pdf, f.unusable_runs, f.job_id
            )
            SELECT {COLUMNS}, x.id AS pass_id, x.kind, x.reason, x.paper_revision AS pass_revision,
                   x.lease_until, x.retry_text, x.use_pdf, x.unusable_runs, x.job_id,
                   k.slug, k.name, k.description, k.parent, k.threshold
            FROM claimed x JOIN papers p ON p.id = x.paper_id
            LEFT JOIN LATERAL (SELECT primary_class FROM classifications
                               WHERE paper_id = p.id
                               ORDER BY paper_revision DESC, id DESC LIMIT 1) c ON true
            LEFT JOIN classes k ON k.slug = c.primary_class
            ORDER BY x.id
            """,
            {
                "besides": list(besides or ()),
                "limit": limit,
                "lease": lease,
                "job": job,
                "pass": pass_id,
            },
        )
    ).fetchall()
    return [
        Claimed(
            id=r["pass_id"],
            kind=PassKind(r["kind"]),
            reason=r["reason"],
            revision=r["pass_revision"],
            paper=row_to_paper(r),
            area=row_to_class(r) if r["slug"] else None,
            lease_until=r["lease_until"],
            retry_text=r["retry_text"],
            use_pdf=r["use_pdf"],
            unusable_runs=r["unusable_runs"],
            job=r["job_id"],
        )
        for r in rows
    ]


@dataclass(frozen=True)
class Standing:
    """A pass as a claim that did not take it finds it: its state, and the end of its claim's
    lease while it runs under a live one (`held_until`); or `locked`, its row held by a unit
    still open (a settle relabelling it), when what it will be is not known yet."""

    state: PassState | None
    held_until: datetime | None = None
    locked: bool = False


async def standing(unit: WriteUnit, pass_id: int) -> Standing | None:
    """How the pass stands (`Standing`), in the unit of a claim that did not take it; None when
    the pass is gone. A claim skips a row another unit holds, so the row is read the same way:
    one held is `locked`, not judged by the state it had before that unit."""
    row = await (
        await unit.execute(
            "SELECT state, CASE WHEN state = 'running' AND lease_until > now() "
            "THEN lease_until END AS held_until FROM fulltext_passes WHERE id = %s "
            "FOR UPDATE SKIP LOCKED",
            (pass_id,),
        )
    ).fetchone()
    if row is not None:
        return Standing(PassState(row["state"]), row["held_until"])
    there = await (
        await unit.execute("SELECT 1 FROM fulltext_passes WHERE id = %s", (pass_id,))
    ).fetchone()
    return Standing(None, locked=True) if there else None


async def job_open(unit: ReadUnit, job_id: int) -> int:
    """The re-run's text passes it still works: to try, running, or awaiting their text, short
    of the wait after which a pass is the owner's (`STUCK`)."""
    row = await (
        await unit.execute(
            "SELECT count(*) AS n FROM fulltext_passes f JOIN papers p ON p.id = f.paper_id "
            "WHERE f.job_id = %s AND f.paper_revision = p.revision AND NOT p.dropped "
            "AND (f.state IN ('ready', 'running') "
            "OR (f.state = 'awaiting_text' AND f.queued_at >= now() - %s))",
            (job_id, AWAITING_TOO_LONG),
        )
    ).fetchone()
    return row["n"]


async def release_job(unit: WriteUnit, job_id: int) -> None:
    """The re-run ended: what it left of its text passes (failed, or awaiting text too long)
    goes to the regular queue, where a nightly tries it again or the owner sees it. The
    taxonomy lock is taken shared first, as in `retry_failed`. Those a claim can take have their
    work items submitted."""
    require_lock(unit)
    rows = await (
        await unit.execute(
            "UPDATE fulltext_passes SET job_id = NULL WHERE job_id = %s AND state <> 'done' "
            "RETURNING id, state",
            (job_id,),
        )
    ).fetchall()
    for r in rows:
        if r["state"] in _CLAIMABLE:
            await _submit(unit, r["id"])


async def take_back(unit: WriteUnit, lease: timedelta) -> int:
    """End the leases on text passes taken for longer than `lease`: a batching nightly's, which a
    nightly holding the run lock calls, so the run that held them is dead. Returns how many. The
    unit holds the taxonomy lock shared, as in `retry_failed`."""
    require_lock(unit)
    rows = await (
        await unit.execute(
            "UPDATE fulltext_passes SET lease_until = now() - interval '1 second' "
            "WHERE kind <> 'deep' AND state = 'running' AND lease_until - tried_at > %s "
            "RETURNING id, job_id",
            (lease,),
        )
    ).fetchall()
    for r in rows:
        if r["job_id"] is None:
            await _submit(unit, r["id"])
    return len(rows)


async def held(unit: WriteUnit, c: Claimed) -> bool:
    """Whether the worker still holds the pass, its row locked to the end of the transaction:
    no other worker took it over after its lease ran out."""
    row = await (
        await unit.execute(
            "SELECT 1 FROM fulltext_passes WHERE id = %s AND state = 'running' "
            "AND lease_until = %s FOR UPDATE",
            (c.id, c.lease_until),
        )
    ).fetchone()
    return row is not None


async def label_of(unit: ReadUnit, pass_id: int) -> tuple[PassKind, str] | None:
    """The pass's kind and reason, or None when it is gone (dropped)."""
    row = await (
        await unit.execute("SELECT kind, reason FROM fulltext_passes WHERE id = %s", (pass_id,))
    ).fetchone()
    return (PassKind(row["kind"]), row["reason"]) if row else None


async def release(unit: WriteUnit, c: Claimed) -> None:
    """Give a claimed pass back unworked: it awaits its next claim, behind those not tried. One
    whose paper is dropped has no work item until the paper comes back (`submit_restored`): no
    claim takes it meanwhile."""
    row = await (
        await unit.execute(
            "UPDATE fulltext_passes f SET state = 'awaiting_text', lease_until = NULL "
            "FROM papers p WHERE f.id = %s AND f.state = 'running' AND f.lease_until = %s "
            "AND p.id = f.paper_id RETURNING p.dropped",
            (c.id, c.lease_until),
        )
    ).fetchone()
    if row is not None and not row["dropped"] and c.job is None:
        await _submit(unit, c.id)


async def let_go(unit: WriteUnit, c: Claimed, error: str, again: datetime) -> None:
    """End the worker's lease on a pass it started, now: it stays running, so no settle drops
    it, and the next claim takes it over at once, its work item from `again`."""
    cur = await unit.execute(
        "UPDATE fulltext_passes SET lease_until = now(), last_error = %s "
        "WHERE id = %s AND state = 'running' AND lease_until = %s",
        (error[:2000], c.id, c.lease_until),
    )
    if cur.rowcount and c.job is None:
        await _submit(unit, c.id, again)


async def wait_again(
    unit: WriteUnit, c: Claimed, error: str, again: datetime | None = None
) -> None:
    """A pass that could not be worked through no fault of its paper (a Deep pass the service
    failed, with no job of it left; a re-run's pass its ceiling stopped): back in the queue,
    the failure not counted, behind those tried less recently, its work item from `again`
    (now when not given). Call it in a transaction that `held` it."""
    await unit.execute(
        "UPDATE fulltext_passes SET state = 'ready', lease_until = NULL, last_error = %s "
        "WHERE id = %s",
        (error[:2000], c.id),
    )
    if c.job is None:
        await _submit(unit, c.id, again)


async def retry_asked(unit: WriteUnit, c: Claimed) -> None:
    """The archive was asked to retry what it gave up on for the pass."""
    await unit.execute("UPDATE fulltext_passes SET retry_text = false WHERE id = %s", (c.id,))


async def awaiting(unit: WriteUnit, c: Claimed, error: str | None = None) -> None:
    """The archive has no text for it yet (or cannot be reached: `error`): it waits again, and
    is tried again after `TEXT_WAIT`."""
    cur = await unit.execute(
        "UPDATE fulltext_passes SET state = 'awaiting_text', lease_until = NULL, "
        "last_error = %s WHERE id = %s AND state = 'running' AND lease_until = %s",
        (error, c.id, c.lease_until),
    )
    if cur.rowcount and c.job is None:
        await _submit(unit, c.id, datetime.now(UTC) + TEXT_WAIT)


def _read(text: TextRead | None) -> dict[str, Any]:
    return {
        "version": text.version if text else None,
        "source": text.source if text else None,
        "hash": text.sha256 if text else None,
        "arrived": text is not None,
    }


async def failed(
    unit: WriteUnit,
    c: Claimed,
    error: str,
    *,
    counts: bool,
    text: TextRead | None = None,
    unusable: bool = False,
    refused: bool = False,
) -> None:
    """The pass failed this run; it is tried again on the next. Only a failure that `counts`
    (no text to be had, an unusable answer) adds a failed run, and only an `unusable` answer
    starts the next try one model further down `brief`'s list; one every model of `brief`
    `refused` stops at once, counted as `FAILED_RUNS`. A pass left for the owner is not run
    again but on their word, or while it awaits its text, so a failure here is a new one: it
    clears the owner's Acknowledge. Call it in a transaction that `held` the pass."""
    await unit.execute(
        """
        UPDATE fulltext_passes SET state = 'failed', lease_until = NULL, last_error = %(error)s,
            failed_at = now(),
            failed_runs = greatest(failed_runs + %(counts)s,
                                   CASE WHEN %(refused)s THEN %(floor)s ELSE 0 END),
            unusable_runs = unusable_runs + %(unusable)s,
            refused = %(refused)s, acknowledged_at = NULL,
            text_arrived_at = CASE WHEN %(arrived)s THEN coalesce(text_arrived_at, now())
                                   ELSE text_arrived_at END,
            arxiv_version = CASE WHEN %(arrived)s THEN %(version)s ELSE arxiv_version END,
            text_source = coalesce(%(source)s, text_source),
            text_hash = coalesce(%(hash)s, text_hash)
        WHERE id = %(id)s
        """,
        {
            "id": c.id,
            "error": error[:2000],
            "counts": int(counts),
            "unusable": int(unusable),
            "refused": refused,
            "floor": FAILED_RUNS,
            **_read(text),
        },
    )


@dataclass(frozen=True)
class Written:
    """The ids of what one pass wrote."""

    brief: int
    gist: int


async def add(
    unit: WriteUnit,
    c: Claimed,
    text: TextRead,
    *,
    brief: str,
    overstatement: str | None,
    gist: str,
    weight: Weight,
    weight_reason: str,
    model: str,
) -> Written:
    """Store what the pass wrote, labelled with the revision it was queued for and the version
    it read, and mark it done. Call it in a transaction that `held` the pass."""
    pass_row = (c.paper.id, c.revision, text.version)
    brief_row = await (
        await unit.execute(
            "INSERT INTO briefs (paper_id, paper_revision, arxiv_version, pass_id, text, "
            "overstatement, text_source, text_hash, model) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (*pass_row, c.id, brief, overstatement, text.source, text.sha256, model),
        )
    ).fetchone()
    await unit.execute(
        "INSERT INTO weights (paper_id, paper_revision, weight, source, reason, model, pass_id) "
        "VALUES (%s, %s, %s, 'full_text', %s, %s, %s)",
        (c.paper.id, c.revision, weight.value, weight_reason, model, c.id),
    )
    gist_row = await (
        await unit.execute(
            "INSERT INTO gists (paper_id, paper_revision, arxiv_version, text, source, model, "
            "pass_id) VALUES (%s, %s, %s, %s, 'full_text', %s, %s) RETURNING id",
            (*pass_row, gist, model, c.id),
        )
    ).fetchone()
    await done(unit, c, text)
    return Written(brief_row["id"], gist_row["id"])


async def done(unit: WriteUnit, c: Claimed, text: TextRead) -> None:
    """The pass wrote what it writes from `text`. Call it in a transaction that `held` it."""
    await unit.execute(
        """
        UPDATE fulltext_passes SET state = 'done', lease_until = NULL, last_error = NULL,
            finished_at = now(), text_arrived_at = coalesce(text_arrived_at, now()),
            arxiv_version = %(version)s, text_source = %(source)s, text_hash = %(hash)s,
            paper_updated = %(updated)s::timestamptz
        WHERE id = %(id)s
        """,
        {"id": c.id, "updated": c.paper.updated, **_read(text)},
    )


async def submitted(unit: WriteUnit, c: Claimed, text: TextRead) -> str | None:
    """A Deep pass's text is about to be sent: the pass records what it read, of the paper's
    revision now, which is what its card is labelled with, even when the job is followed by a
    later claim. Returns the pass's reason now (the owner's request may have taken it over since
    the claim), or None when the worker no longer holds it."""
    row = await (
        await unit.execute(
            """
        UPDATE fulltext_passes SET paper_revision = %(revision)s,
            text_arrived_at = coalesce(text_arrived_at, now()), arxiv_version = %(version)s,
            text_source = %(source)s, text_hash = %(hash)s, paper_updated = %(updated)s::timestamptz
        WHERE id = %(id)s AND state = 'running' AND lease_until = %(lease)s
        RETURNING reason
        """,
            {
                "id": c.id,
                "lease": c.lease_until,
                "revision": c.paper.revision,
                "updated": c.paper.updated,
                **_read(text),
            },
        )
    ).fetchone()
    return row["reason"] if row else None


async def read_of(unit: ReadUnit, pass_id: int) -> tuple[int, TextRead | None]:
    """The paper revision a pass read and what it read (None before it read any text)."""
    row = await (
        await unit.execute(
            "SELECT paper_revision, arxiv_version, text_source, text_hash FROM fulltext_passes "
            "WHERE id = %s",
            (pass_id,),
        )
    ).fetchone()
    read = (
        TextRead(row["arxiv_version"], row["text_source"], row["text_hash"])
        if row["text_hash"]
        else None
    )
    return row["paper_revision"], read


async def outdated(unit: ReadUnit) -> list[str]:
    """The papers, not dropped, given a Brief or full-text Weight, or with a text pass for an
    older revision, whose newest text pass for its current revision is neither open nor done on
    its arXiv version (on its latest update, when the version is unknown): a new version or
    revision came after what was read or queued (`to_queue`'s rule, found from the
    database)."""
    rows = await (await unit.execute(f"{OUTDATED} ORDER BY p.id")).fetchall()
    return [r["id"] for r in rows]


OUTDATED = f"""
    SELECT DISTINCT p.id FROM fulltext_passes d JOIN papers p ON p.id = d.paper_id
    WHERE d.kind <> 'deep' AND (d.state = 'done' OR d.paper_revision < p.revision)
      AND NOT p.dropped
      -- Unless the newest pass for the current revision, the one `facts` reads, is open or read
      -- the paper's version (its latest update, the version unknown).
      AND NOT coalesce((
          SELECT f.state <> 'done' OR f.arxiv_version >= p.version
                 OR (p.version IS NULL AND {COVERS_UPDATE})
          FROM fulltext_passes f
          WHERE f.paper_id = p.id AND f.paper_revision = p.revision AND f.kind <> 'deep'
          ORDER BY f.id DESC LIMIT 1), false)
"""


async def candidates(unit: ReadUnit, lookback: timedelta) -> list[str]:
    """The papers, not dropped, whose passes the nightly run settles from the database
    whatever triggered or missed it (pipeline.md, section 6): every paper with a
    pass open; every one without a text pass for its current revision that the rule may give one
    (an active Focus topic's central or related, an active Area's must read, its own or a
    followed descendant's; or such a verdict or label from the last `lookback`, whose settle
    may have been missed); the `outdated` ones; every saved Focus must read with no card,
    or one with a recent Focus verdict or label of any value, whatever text pass it has; and
    every pick of a published digest of an active Area topic with no card, not unsaved.
    Bounded by the queue and what qualifies but has no pass, not by the library; never cut
    short, as settling spends nothing and a cut could leave the same papers out every night."""
    rows = await (
        await unit.execute(
            f"""
            WITH RECURSIVE area_scope AS (
                SELECT class AS root, class FROM topics
                WHERE depth = 'area' AND status = 'active'
                UNION
                SELECT a.root, c.slug FROM classes c JOIN area_scope a ON c.parent = a.class
                WHERE c.retired_at IS NULL
            ), focus AS (
                SELECT class FROM topics WHERE depth = 'focus' AND status = 'active'
            ), area AS (
                SELECT a.root, t.class FROM topics t JOIN area_scope a ON a.class = t.class
                WHERE t.status <> 'draft'
            ), area_must_read AS (
                -- An Area's own grade, else its followed descendants' highest
                -- (core.grading.page_standing); must read has the highest rarity.
                SELECT g.paper_id FROM effective_grades g JOIN area a ON a.class = g.topic
                WHERE g.grade = 'must_read' AND (g.topic = a.root OR NOT EXISTS (
                    SELECT 1 FROM effective_grades own
                    WHERE own.paper_id = g.paper_id AND own.topic = a.root))
            ), recent AS (
                -- Verdicts and labels whose settle may have been missed, of any value: an
                -- unsure label can let an older verdict count again.
                SELECT paper_id, topic, fit AS value FROM verdicts
                WHERE recorded_at >= now() - %(lookback)s
                UNION
                SELECT paper_id, topic, value FROM labels
                WHERE labelled_at >= now() - %(lookback)s
            ), area_picks AS (
                -- Picks of the current revision in active Area topics' published digests.
                SELECT dp.paper_id FROM digest_papers dp
                JOIN digests d ON d.id = dp.digest_id
                JOIN topics t ON t.class = d.topic AND t.depth = 'area' AND t.status = 'active'
                JOIN papers pp ON pp.id = dp.paper_id AND pp.revision = dp.paper_revision
                WHERE dp.role = 'pick' AND d.state IN {_PUBLISHED}
            ), qualifying AS (
                SELECT paper_id FROM effective_grades
                WHERE topic IN (SELECT class FROM focus) AND fit IN ('central', 'related')
                UNION
                SELECT paper_id FROM area_must_read
                UNION
                SELECT paper_id FROM recent WHERE value IN ('central', 'related')
                AND (topic IN (SELECT class FROM focus) OR topic IN (SELECT class FROM area))
            )
            SELECT p.id FROM papers p
            WHERE NOT p.dropped AND (
                EXISTS (SELECT 1 FROM fulltext_passes f WHERE f.paper_id = p.id
                        AND f.state <> 'done')
                OR (p.id IN (SELECT paper_id FROM qualifying)
                    AND NOT EXISTS (SELECT 1 FROM fulltext_passes f WHERE f.paper_id = p.id
                                    AND f.paper_revision = p.revision AND f.kind <> 'deep'))
                OR p.id IN ({OUTDATED})
                -- A Focus must read, saved, or a recent Focus verdict or label, with no card:
                -- it may want a Deep pass, a Brief pass of its own or not.
                OR ((EXISTS (SELECT 1 FROM effective_grades g WHERE g.paper_id = p.id
                             AND g.topic IN (SELECT class FROM focus) AND g.grade = 'must_read')
                     AND EXISTS (SELECT 1 FROM library l WHERE l.paper_id = p.id AND l.saved)
                     OR p.id IN (SELECT paper_id FROM recent
                                 WHERE topic IN (SELECT class FROM focus)))
                    AND NOT EXISTS (SELECT 1 FROM deep_cards c WHERE c.paper_id = p.id
                                    AND c.card IS NOT NULL))
                -- An Area pick with no card, not unsaved: it may want a Deep pass.
                OR (p.id IN (SELECT paper_id FROM area_picks)
                    AND NOT EXISTS (SELECT 1 FROM library l WHERE l.paper_id = p.id
                                    AND l.hand = 'unsave')
                    AND NOT EXISTS (SELECT 1 FROM deep_cards c WHERE c.paper_id = p.id
                                    AND c.card IS NOT NULL))
            )
            ORDER BY p.id
            """,
            {"lookback": lookback},
        )
    ).fetchall()
    return [r["id"] for r in rows]


async def retry_failed(unit: WriteUnit, runs: int) -> int:
    """Make every failed pass with fewer than `runs` failed runs ready: the run tries it again,
    and the archive what it gave up on. The unit holds the taxonomy lock shared, as every settle
    does, so this never deadlocks with a taxonomy save dropping passes."""
    require_lock(unit)
    rows = await (
        await unit.execute(
            "UPDATE fulltext_passes SET state = 'ready', retry_text = true "
            "WHERE state = 'failed' AND failed_runs < %s RETURNING id, job_id",
            (runs,),
        )
    ).fetchall()
    for r in rows:
        if r["job_id"] is None:
            await _submit(unit, r["id"])
    return len(rows)


# A pass that waits on the owner: failed `runs` runs in a row, or awaiting text too long. Of
# `fulltext_passes f`; the paper (`p`) is not dropped, and a text pass is for its current
# revision. A Deep pass awaits text only once its worker has tried it: with no deep service
# configured, nothing works it, and nothing waits on the owner.
STUCK = (
    "NOT p.dropped AND (f.kind = 'deep' OR f.paper_revision = p.revision) "
    "AND ((f.state = 'failed' AND f.failed_runs >= %(runs)s) "
    "OR (f.state = 'awaiting_text' AND f.queued_at < now() - %(long)s "
    "AND (f.kind <> 'deep' OR f.tried_at IS NOT NULL)))"
)


async def stuck(unit: ReadUnit, runs: int) -> list[dict[str, Any]]:
    """The passes waiting on the owner, with their paper's title, whether every model refused,
    since when they wait (`SINCE`) and when the owner acknowledged them, most failed first."""
    return await (
        await unit.execute(
            "SELECT f.id, f.paper_id, f.paper_revision AS revision, p.title, f.kind, f.state, "
            f"f.failed_runs, f.last_error, f.refused, f.queued_at, {SINCE} AS since, "
            "f.acknowledged_at "
            "FROM fulltext_passes f "
            f"JOIN papers p ON p.id = f.paper_id WHERE {STUCK} "
            "ORDER BY f.failed_runs DESC, f.queued_at, f.id",
            stuck_params(runs),
        )
    ).fetchall()


async def count_stuck(unit: ReadUnit, runs: int, *, unacknowledged: bool = False) -> int:
    """How many passes wait on the owner; with `unacknowledged`, only those not acknowledged."""
    seen = "AND f.acknowledged_at IS NULL" if unacknowledged else ""
    row = await (
        await unit.execute(
            "SELECT count(*) AS n FROM fulltext_passes f JOIN papers p ON p.id = f.paper_id "
            f"WHERE {STUCK} {seen}",
            stuck_params(runs),
        )
    ).fetchone()
    return row["n"]


async def stopped_between(unit: ReadUnit, since: datetime, until: datetime, runs: int) -> int:
    """The distinct papers whose Brief or Weight pass stopped between `since` and `until`:
    failed `runs` runs, or refused by every model, then. A pass left for the owner is not tried
    again, so its last failure is the one that stopped it."""
    row = await (
        await unit.execute(
            "SELECT count(DISTINCT paper_id) AS n FROM fulltext_passes WHERE kind <> 'deep' "
            "AND state = 'failed' AND failed_runs >= %s AND failed_at BETWEEN %s AND %s",
            (runs, since, until),
        )
    ).fetchone()
    return row["n"]


# Since when a pass waiting on the owner (`STUCK`) waits, which names its failure: a failed one
# from its last try, as no run takes it again but on the owner's word; one awaiting its text from
# when it was queued, which only the owner's run-now moves. A failure after the text arrived,
# or after a retry, has another state or a later time.
SINCE = "(CASE WHEN f.state = 'failed' THEN f.tried_at ELSE f.queued_at END)"


async def acknowledge(
    unit: WriteUnit, items: list[tuple[int, datetime]], runs: int
) -> list[dict[str, Any]]:
    """The owner's Acknowledge on the named passes, each by its id and since when it waits
    (`SINCE`): each still waiting on them as named is marked seen (one marked already keeps its
    time). Returns those marked."""
    if not items:
        return []
    ids, since = map(list, zip(*items, strict=True))
    return await (
        await unit.execute(
            "UPDATE fulltext_passes f SET acknowledged_at = coalesce(f.acknowledged_at, now()) "
            "FROM papers p, "
            "unnest(%(ids)s::bigint[], %(since)s::timestamptz[]) AS x (id, since) "
            f"WHERE f.id = x.id AND {SINCE} = x.since AND p.id = f.paper_id "
            f"AND {STUCK} RETURNING f.id, {SINCE} AS since",
            {"ids": ids, "since": since} | stuck_params(runs),
        )
    ).fetchall()


async def paper_of(unit: ReadUnit, pass_id: int) -> str | None:
    row = await (
        await unit.execute("SELECT paper_id FROM fulltext_passes WHERE id = %s", (pass_id,))
    ).fetchone()
    return row["paper_id"] if row else None


async def run_now(unit: WriteUnit, pass_id: int, runs: int, *, pdf: bool = False) -> bool:
    """The owner's "run it now" on a pass waiting for them: three more runs, from the role's own
    model, and first in line.
    A Deep pass becomes the owner's request, ahead of the automatic ones;
    a text pass too, ahead of them and the service worker's while a nightly runs.
    With `pdf` ("Use the PDF"), it reads the paper's PDF from then on, not its LaTeX. A re-run's
    pass leaves it for the regular queue: the owner asked for it. Returns False when it does
    not wait for the owner."""
    cur = await unit.execute(
        "UPDATE fulltext_passes f SET state = 'ready', failed_runs = 0, unusable_runs = 0, "
        "refused = false, job_id = NULL, "
        "tried_at = NULL, acknowledged_at = NULL, "
        "queued_at = now(), retry_text = true, use_pdf = f.use_pdf OR %(pdf)s, "
        "reason = CASE WHEN f.kind = 'deep' THEN %(requested)s ELSE f.reason END, "
        "priority = CASE WHEN f.kind = 'deep' THEN %(first)s ELSE %(text_first)s END "
        f"FROM papers p WHERE f.id = %(id)s AND p.id = f.paper_id AND {STUCK}",
        {
            "id": pass_id,
            "requested": REQUESTED,
            "first": REQUEST_PRIORITY,
            "text_first": TEXT_REQUEST_PRIORITY,
            "pdf": pdf,
        }
        | stuck_params(runs),
    )
    if not cur.rowcount:
        return False
    await _submit(unit, pass_id)
    return True


# Whether paper `p` has a text pass waiting on the owner, for a query with the parameters
# `runs` and `long` (`stuck_params`).
STUCK_EXISTS = f"EXISTS (SELECT 1 FROM fulltext_passes f WHERE f.paper_id = p.id AND {STUCK})"


def stuck_params(runs: int) -> dict[str, Any]:
    return {"runs": runs, "long": AWAITING_TOO_LONG}


async def brief_for(unit: ReadUnit, paper_id: str) -> dict[str, Any] | None:
    """The paper's Brief: from the pass queued last for its current revision, else the newest
    from an older revision, kept until a new one exists (pipeline.md, section 8)."""
    return await (
        await unit.execute(
            "SELECT b.id, b.text, b.overstatement, b.paper_revision AS revision, b.arxiv_version, "
            "b.text_source AS source, b.at AS written_at FROM briefs b "
            "JOIN papers p ON p.id = b.paper_id WHERE b.paper_id = %s "
            "ORDER BY b.paper_revision = p.revision DESC, b.paper_revision DESC, b.pass_id DESC "
            "LIMIT 1",
            (paper_id,),
        )
    ).fetchone()


async def text_pass_for(unit: ReadUnit, paper_id: str) -> dict[str, Any] | None:
    """The text pass queued last for the paper's current revision: why it has a Brief or not."""
    return await (
        await unit.execute(
            "SELECT f.id, f.kind, f.state, f.queued_at, f.arxiv_version FROM fulltext_passes f "
            "JOIN papers p ON p.id = f.paper_id AND p.revision = f.paper_revision "
            "WHERE f.paper_id = %s AND f.kind <> 'deep' ORDER BY f.id DESC LIMIT 1",
            (paper_id,),
        )
    ).fetchone()


async def uses_pdf(unit: ReadUnit, paper_id: str) -> bool:
    """Whether the owner chose "Use the PDF" on any full-text pass of the paper's current
    revision, of any kind: its text is then its PDF's, not its LaTeX's. A new revision is a
    new text, read from its LaTeX until the owner says otherwise."""
    row = await (
        await unit.execute(
            "SELECT bool_or(f.use_pdf) AS use_pdf FROM fulltext_passes f "
            "JOIN papers p ON p.id = f.paper_id AND p.revision = f.paper_revision "
            "WHERE f.paper_id = %s",
            (paper_id,),
        )
    ).fetchone()
    return bool(row and row["use_pdf"])


async def count_by_state(unit: ReadUnit) -> dict[tuple[str, str], int]:
    """Passes not done, by (kind, state), every pair present: a drained queue reads 0."""
    rows = await (
        await unit.execute(
            "SELECT kind, state, count(*) AS n FROM fulltext_passes WHERE state <> 'done' "
            "GROUP BY kind, state"
        )
    ).fetchall()
    counts = {(k.value, s.value): 0 for k in PassKind for s in PassState if s != PassState.DONE}
    return counts | {(r["kind"], r["state"]): r["n"] for r in rows}
