"""Digests on a test's connection by a scripted writer (`Writer`): the papers and the days of
October 2026 they fall on, a paper listed on a day or graded on a topic, a day's and a week's
digests written, and a digest as it was shown (`shown`)."""

import json
from datetime import date

from litwatch import digests
from litwatch.config import Config
from litwatch.digests import writer
from litwatch.jobs.context import model_run
from litwatch.llm import Gateway
from litwatch.store import digests as store_
from litwatch.store import taxonomy as taxonomy_store
from litwatch.store import verdicts
from tests import settling
from tests.conftest import FAITH, INTERP, gate_on, paper
from tests.fakes import ScriptedProvider
from tests.seed import analyse, classify, grade
from tests.sessions import Bridge, Session, in_read, in_write

TUE, WED, THU, SAT = date(2026, 10, 6), date(2026, 10, 7), date(2026, 10, 8), date(2026, 10, 10)
MON = date(2026, 10, 5)


class Writer:
    """The `writer` role, scripted: a valid answer built from the request's answer model (the
    first candidates as picks, a Focus daily marking its first paper, a Focus weekly bringing
    back the first earlier paper), or each of `failures` raised in turn first."""

    def __init__(self) -> None:
        self.failures: list[Exception] = []
        self.asked: list[str] = []

    def respond(self, role, request) -> str:
        self.asked.append(request.user)
        if self.failures:
            raise self.failures.pop(0)
        out = request.output
        answer = {"text": " ".join(["Probes recover hidden steps."] * 20), "gist": "words " * 40}
        if issubclass(out, writer.Picked):
            n = out.picks_range[1] if out.picks_range[0] else min(1, out.picks_range[1])
            picks = sorted(out.candidates)[:n]
            answer |= {"picks": [{"paper": p, "why": "It stands out."} for p in picks]}
        if issubclass(out, writer.FocusDay):
            answer["text"] += f" [The first]({min(out.papers)}) shows it."
        if issubclass(out, writer.FocusWeek) and out.earlier:
            answer["text"] += f" As [an earlier paper]({min(out.earlier)}) found."
        return json.dumps(answer)

    def gateway(self, config: Config, journal) -> Gateway:
        providers = {n: ScriptedProvider(n, self.respond) for n in ("anthropic", "openrouter")}
        return Gateway(config.models, providers, journal, retry_delays=(0.0,))


async def listed(conn: Session, day: date, *papers) -> None:
    await conn.execute(
        "UPDATE papers SET announced = %s WHERE id = ANY(%s)", (day, [p.id for p in papers])
    )


async def focus(conn: Session, p, fit: str) -> None:
    """Graded on the Focus topic at `fit`."""
    await classify(conn, p, FAITH.slug)
    await analyse(conn, p)
    await grade(conn, p, fit)


async def area(conn: Session, p, fit: str, weight: str = "solid") -> None:
    """A member of the Area topic's class, graded there at `fit`, grading finished."""
    await classify(conn, p, INTERP.slug)
    await analyse(conn, p, weight)
    await grade(conn, p, fit, INTERP)
    await in_write(conn, verdicts.passed, [(p.id, 1)])


async def day(conn: Session, w: Writer, on: date, config: Config | None = None) -> bool:
    tree = await in_read(conn, taxonomy_store.load)
    async with (
        gate_on(conn) as db,
        model_run(db, config or Config(), "digests", gateway=w.gateway) as run,
    ):
        return await digests.write_day(Bridge(conn), run, tree, on, rules=settling.RULES)


async def week(conn: Session, w: Writer, monday: date) -> None:
    tree = await in_read(conn, taxonomy_store.load)
    async with (
        gate_on(conn) as db,
        model_run(db, Config(), "digests", gateway=w.gateway) as run,
    ):
        await digests.write_week(Bridge(conn), run, tree, monday, rules=settling.RULES)


P = paper("2610.00001", title="Traces that hide steps")
Q = paper("2610.00002", title="A near neighbour")
R = paper("2610.00003", title="A distant cousin")
S = paper("2610.00004", title="Late to the party")


async def shown(
    conn: Session, topic: str, period: str, on: date
) -> tuple[str, list[tuple[str, str]]]:
    """The digest's state, and its papers with their roles in its order."""
    row = await (
        await conn.execute(
            "SELECT id, state FROM digests WHERE topic = %s AND period = %s AND day = %s",
            (topic, period, on),
        )
    ).fetchone()
    if row is None:
        return None, []
    return row["state"], [
        (p["paper_id"], p["role"]) for p in await in_read(conn, store_.papers_of, row["id"])
    ]
