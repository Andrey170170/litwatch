"""The CLI's commands as work items `serve` runs (docs/plans/a2-work-gate.md, P5; the spec's "The
CLI"). A command writes its item, of kind `command:<name>` (`kind`), with its inputs in the
scope, and follows it (`litwatch.cli`); `serve` runs it here (`CommandWorker`), through the
process's Model gate and under `serve`'s configuration, so only `serve` calls models. What the
command shows comes back two ways. Its progress, as it goes, is the item's results (`Output`),
each written in a unit fenced on the item's lease, in order; what it returns, at its end, is the
item's completion record (`Done.result`), written in the unit that ends it. Each is a list of
bodies:

    {"say": text}               a line to print
    {"data": value}             a value to print as JSON
    {"to": path, "line": text}  a line of one of the command's output files, appended once
                                (results only: an evaluation's answers, its reservations)

`serve` serves the commands it can run (`kinds`): one it cannot (no archive, no deep service, no
catalog pass) the command refuses. An item whose run lock is taken waits (`Later`) and runs
again. One the database was lost under, or that `serve` gave back as it stopped, is tried again
from its start, and never does again what it committed: a command whose run closed returns that
run's record (`_closed`); a `--limit` counts what its earlier attempts did (`WorkItem.progress`,
counted in the unit each pass commits in); an evaluation leaves out what its own results hold
already, so no call is made twice for one answer the item kept."""

from __future__ import annotations

import asyncio
import json
import logging
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from litwatch import fulltext
from litwatch.archive import ArchiveClient
from litwatch.config import Config, Models, RoleConfig
from litwatch.core import Paper, Taxonomy
from litwatch.core.work import WorkItem
from litwatch.feedback import word
from litwatch.fulltext import deep as deep_passes
from litwatch.gates.database import BusyError, DatabaseGate, WriteUnit
from litwatch.gates.work import Done, Later, Worker
from litwatch.integrations import archive as archive_integration
from litwatch.integrations.deep import Deep
from litwatch.jobs import digests as digests_job
from litwatch.jobs import intake as intake_job
from litwatch.jobs import nightly as nightly_job
from litwatch.jobs import publications as lookups
from litwatch.jobs.context import UnclosedError, run_record
from litwatch.jobs.scheduler import close_backoff
from litwatch.llm import ModelGate, Request
from litwatch.publications import PublicationSource
from litwatch.store import papers as papers_store
from litwatch.store import runs
from litwatch.store import work as queue
from litwatch.store.calls import EVAL, CallJournal
from litwatch.store.fulltext import DEEP_WORK

log = logging.getLogger(__name__)

PREFIX = "command:"
# How long an item that finds the run lock taken waits before it tries again.
WAIT = timedelta(seconds=30)


def kind(name: str) -> str:
    """The work kind of the command `name`."""
    return PREFIX + name


def said(text: str) -> dict[str, Any]:
    """A completion record's line to print."""
    return {"say": text}


def shown(value: Any) -> dict[str, Any]:
    """A completion record's value to print as JSON."""
    return {"data": value}


def finished(*bodies: dict[str, Any]) -> Done:
    """The item ends done, with `bodies` its completion record."""
    return Done(result=list(bodies))


class Output:
    """An item's progress, as the command following it shows it (`store.work.put_result`), each
    a unit of its own, fenced on the item's lease: a result of a lost lease never commits, and
    the Worker stops there. One at a time, so they commit in the order of their ids, which the
    follower reads after the last it saw. Only what the follower consumes as it goes: what the
    command returns is its completion record (`finished`)."""

    def __init__(self, db: DatabaseGate, item: WorkItem) -> None:
        self.db, self.item = db, item
        self.turn = asyncio.Lock()

    async def _put(self, body: dict[str, Any]) -> None:
        async with self.turn, self.db.write(f"{self.item.kind}: result") as unit:
            await queue.put_result(unit, self.item, body)

    async def say(self, text: str) -> None:
        await self._put({"say": text})

    async def data(self, value: Any) -> None:
        await self._put({"data": value})

    async def line(self, path: str, text: str) -> None:
        await self._put({"to": path, "line": text})

    async def earlier(self, path: str) -> list[str]:
        """The lines of the file `path` that this item's results hold already: what an attempt
        before this one kept."""
        async with self.db.read("command: earlier results") as unit:
            found = await queue.results(unit, self.item.id)
        return [b["line"] for _, b in found if b.get("to") == path]


@dataclass
class Serving:
    """What `serve` runs the commands with: its gate, configuration and Model gate, and the
    archive, deep service and publication source it has."""

    db: DatabaseGate
    config: Config
    models: ModelGate
    archive: ArchiveClient | None = None
    deep: Deep | None = None
    publications: PublicationSource | None = None


type Handler = Callable[[Serving, WorkItem, Output], Awaitable[Done | Later]]


class CommandWorker:
    """Runs the command items of the kinds it serves (`kinds`), each by its handler."""

    def __init__(self, serving: Serving) -> None:
        self.serving = serving

    def kinds(self) -> dict[str, Worker]:
        """The command kinds `serve` can run, each to this Worker: all but those that need an
        archive or a deep service it lacks."""
        s = self.serving
        served = [name for name, (_, needs) in HANDLERS.items() if needs(s)]
        return {kind(name): self for name in served}

    async def __call__(self, item: WorkItem) -> Done | Later:
        handler, _ = HANDLERS[item.kind.removeprefix(PREFIX)]
        try:
            return await handler(self.serving, item, Output(self.serving.db, item))
        except BusyError:
            log.info("command: %s waits, another job holds the run lock", item.kind)
            return Later(datetime.now(UTC) + WAIT)


# -- the pipeline's commands ----------------------------------------------------------------------


def _record(run: Mapping[str, Any]) -> list[dict[str, Any]]:
    """A run's record, as the command shows it: its cost, then its counts."""
    return [said(f"run {run['id']}: cost ${run['cost']}"), shown(run["counts"])]


async def _ran(db: DatabaseGate, run_id: int, *bodies: dict[str, Any]) -> Done:
    """Done, with `bodies` and the record of the run `run_id`, which this item opened."""
    async with db.read("command: run record") as unit:
        run = await runs.get(unit, run_id)
    assert run is not None  # the run this item opened
    return finished(*bodies, *_record(run))


async def _closed(db: DatabaseGate, item: WorkItem, kind: str) -> Done | None:
    """Done, when an earlier attempt of `item` closed its run of `kind` (its work committed, and
    then `serve` stopped before the item ended): that run's record, its work not done again."""
    async with db.read("command: closed already") as unit:
        run = await runs.closed_by(unit, item.id, kind)
    if run is None:
        return None
    return finished(said(f"run {run['id']} closed in an earlier attempt"), *_record(run))


async def _nightly(s: Serving, item: WorkItem, out: Output) -> Done | Later:
    """`litwatch nightly`. A run that did not close is tried again after a backoff, as a
    scheduled fire's is: its jobs wait for a nightly that closes."""
    if closed := await _closed(s.db, item, "nightly"):
        return closed
    o = item.scope
    try:
        run_id = await nightly_job.run(
            s.db, s.config, models=s.models, fetch=o["fetch"], days=o["days"],
            limit=o["limit"], with_digests=o["digests"],
        )  # fmt: skip
    except UnclosedError as exc:
        after = close_backoff(item.attempts)
        log.warning("command: the nightly did not close (%s); again in %s", exc.__cause__, after)
        return Later(datetime.now(UTC) + after)
    return await _ran(s.db, run_id)


async def _digests(s: Serving, item: WorkItem, out: Output) -> Done:
    """`litwatch digests write`."""
    if closed := await _closed(s.db, item, "digests"):
        return closed
    first, last = (date.fromisoformat(item.scope[k]) for k in ("since", "until"))
    return await _ran(s.db, await digests_job.window(s.db, s.config, first, last, models=s.models))


async def _intake(s: Serving, item: WorkItem, out: Output) -> Done:
    """`litwatch intake`: from arXiv."""
    if closed := await _closed(s.db, item, "intake"):
        return closed
    result = await intake_job.run(s.db, s.config, item.scope["days"])
    return finished(
        said(
            f"intake {result.details['status']}: {result.fetched} papers, "
            f"{result.inserted} new, {result.updated} updated, {result.unchanged} unchanged"
        )
    )


async def _catalog(s: Serving, item: WorkItem, out: Output) -> Done:
    """`litwatch intake --catalog-from`: the catalog pass alone. A pass the database was lost
    under is tried again, from the checkpoint it left."""
    if closed := await _closed(s.db, item, "intake"):
        return closed

    async def report(found: dict[str, Any]) -> None:
        await out.say(
            f"catalog through {found['through']}: {found['records']} records, "
            f"{found['registered']} registered"
        )

    found = await intake_job.catalog_from(
        s.db, s.config, date.fromisoformat(item.scope["since"]), report
    )
    if not found["complete"]:
        raise RuntimeError(f"catalog pass stopped: {found['error']}")
    return finished(said(f"catalog pass complete through {found['through']}"))


async def _from_archive(s: Serving, item: WorkItem, out: Output) -> Done:
    """`litwatch intake archive`: a window's papers read from `serve`'s archive."""
    assert s.archive is not None  # served only with one
    if closed := await _closed(s.db, item, "intake"):
        return closed
    first, last = date.fromisoformat(item.scope["first"]), date.fromisoformat(item.scope["last"])
    result = await intake_job.from_archive(s.db, s.config, s.archive, first, last)
    return finished(said(archived(first, last, result)))


def archived(first: date, last: date, result: Any) -> str:
    """What `litwatch intake archive` says it did (`intake.IntakeResult`)."""
    skipped = result.details["skipped"]
    return (
        f"intake from the archive, {first} to {last}: {result.fetched} papers, "
        f"{result.inserted} new, {result.updated} updated"
        + (f", {len(skipped)} without text" if skipped else "")
    )


def _counter(item: WorkItem, name: str) -> tuple[int, Callable[[WriteUnit, int], Awaitable[None]]]:
    """What the item's earlier attempts counted of `name` (`WorkItem.progress`), and what counts
    more of it, in the unit that commits it (`store.work.count`)."""

    async def count(unit: WriteUnit, n: int) -> None:
        await queue.count(unit, item, name, n)

    return item.progress.get(name, 0), count


def _left(limit: int | None, before: int) -> int | None:
    return None if limit is None else max(0, limit - before)


async def _fulltext(s: Serving, item: WorkItem, out: Output) -> Done:
    """`litwatch fulltext`: the text passes due, tried once each, and again while some await
    their text, for at most `wait` seconds, as the passes' own items would. Its `--limit`
    counts the passes worked by its earlier attempts too."""
    assert s.archive is not None  # served only with one
    before, counted = _counter(item, "worked")
    if (limit := _left(item.scope["limit"], before)) == 0:
        return finished(said(f"{before} passes worked already, the limit"))

    async def report(found: Counter[str]) -> None:
        await out.data(dict(found))

    total = await fulltext.work_waiting(
        s.db, s.config, s.archive, limit=limit, wait=item.scope["wait"], models=s.models,
        report=report, counted=counted,
    )  # fmt: skip
    return finished(shown(dict(total)))


async def _deep(s: Serving, item: WorkItem, out: Output) -> Done:
    """`litwatch fulltext --deep`: the Deep passes due, one at a time, under the `deep`
    admission its item has (one Deep job at a time, the passes' own items' included). Its
    `--limit` counts the passes claimed, by its earlier attempts too: a claim is where a Deep
    pass starts to spend."""
    assert s.archive is not None and s.deep is not None  # served only with both
    wait = item.scope["wait"]
    before, count = _counter(item, "claimed")
    claims = [before]

    async def claimed(unit: WriteUnit, n: int) -> None:
        await count(unit, n)
        claims[0] += n

    total: Counter[str] = Counter()
    deadline = asyncio.get_running_loop().time() + wait
    while (limit := _left(item.scope["limit"], claims[0])) != 0:
        found = await deep_passes.work(
            s.db, s.archive, s.deep, rules=word.rules(s.config), limit=limit,
            fires=s.config.schedule.enabled, claimed=claimed,
        )  # fmt: skip
        await out.data(dict(found))
        total.update(found)
        left = deadline - asyncio.get_running_loop().time()
        if not found["awaiting_text"] or left <= 0:
            break
        await asyncio.sleep(min(fulltext.TICK_SECONDS, left))
    return finished(shown(dict(total)))


async def _publications(s: Serving, item: WorkItem, out: Output) -> Done:
    """`litwatch publications`: the library papers due (under the `publications` admission, so
    never beside a scheduled lookup), or one paper."""
    source = s.publications
    assert source is not None  # `serve` has one
    if (paper := item.scope.get("paper")) is not None:
        found = await lookups.look_up(s.db, source, paper)
        return finished(shown(found.model_dump(mode="json") if found else None))
    if closed := await _closed(s.db, item, "publications"):
        return closed
    return finished(shown(await lookups.run(s.db, source, item.scope["limit"])))


# -- the evaluations and the ping -----------------------------------------------------------------


async def _replay(s: Serving, item: WorkItem, out: Output) -> Done:
    """`litwatch eval replay`, of the replay the command started in the unit that submitted
    this item (or `--resume` names), as far as it got: its results are the evaluation tables',
    so a run tried again resumes there."""
    from litwatch.quality import replay  # noqa: PLC0415

    eval_run_id = item.scope["eval_run_id"]
    await out.say(f"replay {eval_run_id}")
    async with s.db.read("replay: resume") as unit:
        r = await replay.resume(unit, s.config, eval_run_id)
    found = await replay.run(s.db, s.config, r, models=s.models)
    return finished(shown(found), said(f"report: litwatch eval report {eval_run_id}"))


class _Sum(BaseModel):
    total: int


async def _ping(s: Serving, item: WorkItem, out: Output) -> Done:
    """`litwatch ping`: one small request through a role of `serve`'s, to check its key, model
    and price. Not journalled."""
    request = Request(
        system="You answer arithmetic questions with the structured output only.",
        user="What is 17 + 25?",
        output=_Sum,
    )
    answer = await s.models.on().ask(item.scope["role"], request)
    usage = answer.usage
    return finished(
        said(
            f"{answer.model} -> {answer.value.total}  "
            f"({usage.prompt_tokens} in, {usage.completion_tokens} out, cost {usage.cost}, "
            f"served by {usage.provider})"
        )
    )


async def _papers(db: DatabaseGate, ids: list[str]) -> list[Paper]:
    async with db.read("evaluation papers") as unit:
        found = {p.id: p for p in await papers_store.get_many(unit, ids)}
    return [found[i] for i in ids if i in found]


def _journal(db: DatabaseGate, run_id: int) -> CallJournal:
    """An evaluation's calls' journal: on its run, an evaluation's, which production's reuse and
    price estimates do not read; it reuses nothing itself, so each candidate is asked afresh."""
    return CallJournal(db, run_id, observes=False, reuses=False)


async def _classify(s: Serving, item: WorkItem, out: Output) -> Done:
    """`litwatch eval classify`: each candidate's papers sorted as its `classify` role, each
    answer a line of its file (`to`) as it arrives."""
    from litwatch.quality import classify  # noqa: PLC0415

    tree = Taxonomy.model_validate(item.scope["tree"])
    concurrency = item.scope["concurrency"]
    summary = []
    async with run_record(s.db, EVAL, tallied=True) as record:
        for c in item.scope["candidates"]:
            name, path = c["name"], c["out"]
            done = {
                classify.Result.model_validate_json(x).paper_id for x in await out.earlier(path)
            }
            papers = await _papers(s.db, [i for i in c["papers"] if i not in done])
            models = Models(classify=RoleConfig.model_validate(c["role"]))
            gateway = s.models.on(_journal(s.db, record.run_id), models=models)

            async def keep(
                r: classify.Result,
                name: str = name,
                path: str = path,
                seen: list[int] = [0],  # noqa: B006
            ) -> None:
                await out.line(path, r.model_dump_json())
                seen[0] += 1
                if r.error or seen[0] % 100 == 0:
                    await out.say(f"{name}: {seen[0]} {r.paper_id} {r.error or ''}")

            new = await classify.ask(gateway, tree, papers, keep, concurrency=concurrency)
            cost = sum(r.cost or 0 for r in new)
            failed = sum(r.error is not None for r in new)
            summary.append(said(f"{name}: {len(new)} sorted, {failed} failed, ${cost:.2f}"))
    return finished(*summary)


async def _briefs(s: Serving, item: WorkItem, out: Output) -> Done:
    """`litwatch eval briefs`: the Brief of each paper left, with the command's `brief` role,
    its text read from `serve`'s archive, each answer a line of the run's file as it arrives."""
    from litwatch.quality import briefs  # noqa: PLC0415

    assert s.archive is not None  # served only with one
    o = item.scope
    path = o["out"]
    done = {briefs.Result.model_validate_json(x).paper_id for x in await out.earlier(path)}
    entries = [briefs.Entry.model_validate(e) for e in o["entries"]]
    found = {p.id: p for p in await _papers(s.db, [e.id for e in entries if e.id not in done])}
    papers = [(found[e.id], e) for e in entries if e.id in found]

    async def keep(r: briefs.Result) -> None:
        await out.line(path, r.model_dump_json())
        await out.say(f"{r.paper_id}: {r.error or 'answered'} {briefs.dollars([r.cost])}")

    async with run_record(s.db, EVAL, tallied=True) as record:
        new = await briefs.ask(
            s.models, RoleConfig.model_validate(o["role"]), Taxonomy.model_validate(o["tree"]),
            papers, s.archive, keep, journal=_journal(s.db, record.run_id),
            concurrency=o["concurrency"], text=o["text"], wait_seconds=o["wait_seconds"],
        )  # fmt: skip
    cost = briefs.dollars([r.cost for r in new])
    return finished(
        said(f"{sum(r.output is not None for r in new)} of {len(new)} answered, {cost}")
    )


async def _ladders(s: Serving, item: WorkItem, out: Output) -> Done:
    """`litwatch eval ladders`: the calls the command left to make, each case's request built
    again here as the command built it, under the ceiling, of which the command's directory had
    spent `spent`; what this item's results hold counts too, and is not asked again. Refused
    before any call when a request built here is not the one the command estimated and the
    owner approved (its fingerprint, `ladders.asked`), or when the results of this item's
    earlier attempts hold calls the ceiling can't count (`ladders.refuse`)."""
    from litwatch.quality import ladders  # noqa: PLC0415

    o = item.scope
    directory = Path(o["out"])
    cases = [ladders.Case.model_validate(c) for c in o["cases"]]
    found = {r: ladders.Ladder.model_validate(x) for r, x in o["ladders"].items()}
    built = await ladders.requests(s.db, s.archive, cases)
    by_key, named = (
        {c.key: c for c in cases},
        {(r, c.name): c for r, x in found.items() for c in x.candidates},
    )
    work, wrong = [], []
    for key, name, approved in o["todo"]:
        case, request = by_key.get(key), built.get(key)
        candidate = named.get((case.role, name)) if case else None
        if case is None or candidate is None:
            wrong.append(f"{key} {name}: no such {'candidate' if case else 'case'}")
        elif not isinstance(request, Request):
            wrong.append(f"{key} {name}: {request or 'not built'}")
        elif ladders.asked(candidate, request) != approved:
            wrong.append(f"{key} {name}: its request is not the one approved")
        else:
            work.append(ladders.Todo(case, found[case.role], candidate, request))
    if wrong:
        raise ValueError("refused, nothing asked: " + "; ".join(wrong))
    earlier: dict[str, list[str]] = {}
    for name in {f"{c.role}.jsonl" for c in cases} | {ladders.PENDING}:
        earlier[name] = await out.earlier(str(directory / name))
    done_rows = [
        ladders.Row.model_validate_json(x)
        for name, lines in earlier.items()
        if name != ladders.PENDING
        for x in lines
    ]
    ladders.refuse(done_rows, f"item {item.id}'s earlier attempts")
    reserved = {r["attempt"]: r["reserved"] for r in map(json.loads, earlier[ladders.PENDING])}

    async def keep(name: str, line: str) -> None:
        await out.line(str(directory / name), line)
        if name.endswith(".jsonl") and name not in (ladders.PENDING, ladders.BUDGET):
            r = ladders.Row.model_validate_json(line)
            if r.outcome != "answered":
                await out.say(f"{r.case} {r.candidate}: {r.outcome} {r.detail or ''}")

    async with run_record(s.db, EVAL, tallied=True) as record:
        ran = await ladders.ask(
            ladders.left(work, done_rows), s.models, o["ceiling"],
            o["spent"] + ladders.spent(done_rows, reserved), keep,
            journal=_journal(s.db, record.run_id), concurrency=o["concurrency"],
        )  # fmt: skip
    kinds = Counter(r.outcome for r in ran.rows)
    summary = [said(f"{len(ran.rows)} calls: {dict(kinds)}; ${ladders.spent(ran.rows):.2f}")]
    if ran.why is not None:
        t = ran.stopped_at
        first = f", first not run: {t.case.key} {t.candidate.name}" if t else ""
        summary.append(
            said(f"stopped: {ran.why}{first} (recorded in {directory / ladders.BUDGET})")
        )
    return finished(*summary)


def _always(_: Serving) -> bool:
    return True


def _archived(s: Serving) -> bool:
    return s.archive is not None


def _harvests(s: Serving) -> bool:
    """The catalog pass is on: the archive section harvests and registers."""
    return archive_integration.harvester(s.config) is not None


def _deep_served(s: Serving) -> bool:
    return s.archive is not None and s.deep is not None


# Each command by name: its handler, and whether `serve` can run it.
HANDLERS: Mapping[str, tuple[Handler, Callable[[Serving], bool]]] = {
    "nightly": (_nightly, _always),
    "digests": (_digests, _always),
    "intake": (_intake, _always),
    "intake-catalog": (_catalog, _harvests),
    "intake-archive": (_from_archive, _archived),
    "fulltext": (_fulltext, _archived),
    "deep": (_deep, _deep_served),
    "publications": (_publications, _always),
    "replay": (_replay, _always),
    "ping": (_ping, _always),
    "classify": (_classify, _always),
    "briefs": (_briefs, _archived),
    "ladders": (_ladders, _always),
}

# The admission a command's item takes when it is not its own: the Deep passes' (one Deep job
# at a time) and the publication lookup's (never beside a scheduled one).
ADMISSIONS = {"deep": DEEP_WORK, "publications": "publications"}
