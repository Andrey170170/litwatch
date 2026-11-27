"""Changes and settles for tests: each helper runs one on a test's connection, in a unit of its
own (`sessions.Bridge`), by the rules every test settles by (`RULES`). A test of what runs
inside a change opens `change` and passes its unit, as the code does."""

import asyncio
import contextvars
from collections.abc import AsyncIterator, Coroutine, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import date

from litwatch.core import Cause, Fit, Hand, Label, Paper, Reaction, Taxonomy
from litwatch.feedback import misses, word
from litwatch.gates.context import Attribution, attributed
from litwatch.gates.database import Connection
from litwatch.intake import batch
from litwatch.store import papers as papers_store
from litwatch.store import taxonomy as taxonomy_store
from tests.sessions import Bridge

# Automatic Deep cards on, as with the `deep` integration configured: conftest makes every
# configuration give these (`word.rules`); a test of them off passes its own.
RULES = word.Rules(automatic_deep=True)
OFF = word.Rules(automatic_deep=False)
configured = word.rules  # the configuration's own rules, which conftest replaces


def change(
    conn: Connection,
    *paper_ids: str,
    cause: Cause,
    job: int | None = None,
    rules: word.Rules = RULES,
) -> AbstractAsyncContextManager[word.Change]:
    """A change of the papers (`word.change`) on the connection."""
    return word.change(Bridge(conn), *paper_ids, cause=cause, rules=rules, job=job)


def apart[T](work: Coroutine[None, None, T]) -> asyncio.Task[T]:
    """Start `work` as another request's, attributed as one: a task started inside a unit counts
    as inside it (the nesting check), and this one does not, wherever it is started from."""

    async def requested() -> T:
        with attributed(Attribution.request("test: another request")):
            return await work

    return asyncio.create_task(requested(), context=contextvars.Context())


@asynccontextmanager
async def held(
    conn: Connection, *paper_ids: str, cause: Cause = Cause.OWNER
) -> AsyncIterator[word.Change]:
    """A change of the papers kept open while the block runs, in a task of its own: what the
    block starts is not inside its unit, as another request's work is not. It settles and
    commits when the block ends."""
    opened: asyncio.Future[word.Change] = asyncio.get_running_loop().create_future()
    leave = asyncio.Event()

    async def hold() -> None:
        async with change(conn, *paper_ids, cause=cause) as done:
            opened.set_result(done)
            await leave.wait()

    holder = asyncio.create_task(hold())
    try:
        await asyncio.wait({holder, opened}, return_when=asyncio.FIRST_COMPLETED)
        if not opened.done():
            await holder  # raises what kept it from opening
        yield opened.result()
    finally:
        leave.set()
        await holder


async def settle(
    conn: Connection,
    paper_id: str,
    *,
    cause: Cause,
    claimed: int | None = None,
    job: int | None = None,
    rules: word.Rules = RULES,
) -> word.Settled:
    """Settle one paper in a unit of its own, and give what that changed."""
    db = Bridge(conn)
    async with db.write("settle", taxonomy="shared", papers=[paper_id]) as unit:
        done = await word.settle_many(
            unit, [paper_id], cause=cause, rules=rules, claimed=claimed, job=job
        )
    return done[paper_id]


async def settle_all(
    conn: Connection, paper_ids: list[str], *, cause: Cause, rules: word.Rules = RULES
) -> None:
    await word.settle_all(Bridge(conn), paper_ids, cause=cause, rules=rules)


async def sync(conn: Connection, rules: word.Rules = RULES) -> dict[str, int]:
    return await word.sync(Bridge(conn), rules=rules)


# The owner's word, each a change of its paper: the stored word where there is one, and the
# change, settled.


async def label(conn: Connection, given: Label) -> tuple[Label, word.Change]:
    async with change(conn, given.paper_id, cause=Cause.OWNER) as done:
        stored = await word.label(done.unit, given)
    return stored, done


async def unlabel(conn: Connection, paper_id: str, topic: str) -> word.Change:
    async with change(conn, paper_id, cause=Cause.OWNER) as done:
        await word.unlabel(done.unit, paper_id, topic)
    return done


async def react(conn: Connection, given: Reaction) -> tuple[Reaction, word.Change]:
    async with change(conn, given.paper_id, cause=Cause.OWNER) as done:
        stored = await word.react(done.unit, given)
    return stored, done


async def unreact(conn: Connection, paper_id: str, topic: str | None) -> word.Change:
    async with change(conn, paper_id, cause=Cause.OWNER) as done:
        await word.unreact(done.unit, paper_id, topic)
    return done


async def toggle(conn: Connection, paper_id: str, hand: Hand) -> word.Change:
    async with change(conn, paper_id, cause=Cause.OWNER) as done:
        await word.toggle(done.unit, paper_id, hand)
    return done


async def keep(conn: Connection, paper_id: str, fetched: Paper | None) -> tuple[Paper, bool]:
    """Keep a paper an owner's request fetched (`misses.keep`), in a change of it."""
    async with change(conn, paper_id, cause=Cause.OWNER) as done:
        return await misses.keep(done.unit, paper_id, fetched)


async def report_miss(
    conn: Connection, paper_id: str, topic: str, fit: Fit = Fit.CENTRAL
) -> tuple[int, bool]:
    """Report the paper as a miss for the topic (`misses.report`), in a change of it."""
    async with change(conn, paper_id, cause=Cause.OWNER) as done:
        return await misses.report(done.unit, paper_id, topic, fit)


async def store(
    conn: Connection,
    papers: list[Paper],
    *,
    source: str,
    listed: Mapping[str, date] | None = None,
) -> papers_store.Upserted:
    """Store what a source read, as intake does (`batch.store`)."""
    return await batch.store(Bridge(conn), papers, source=source, rules=RULES, listed=listed)


# Taxonomy edits, each in a unit holding the taxonomy lock exclusively.


async def save_tree(conn: Connection, tree: Taxonomy) -> taxonomy_store.Saved:
    """Save the tree as it is, settling no paper: a test's seed, or a test of the store's save
    alone. `save_taxonomy` is the owner's edit, which settles what it reaches."""
    async with Bridge(conn).write("save tree", taxonomy="exclusive") as unit:
        return await taxonomy_store.save(unit, tree)


async def set_order(conn: Connection, slugs: list[str]) -> None:
    """Store the owner's order of the topics, settling no paper (`reorder` settles)."""
    async with Bridge(conn).write("set order", taxonomy="exclusive") as unit:
        await taxonomy_store.set_order(unit, slugs)


async def save_taxonomy(
    conn: Connection, tree: Taxonomy, rules: word.Rules = RULES
) -> taxonomy_store.Saved:
    async with Bridge(conn).write("save taxonomy", taxonomy="exclusive") as unit:
        return await word.save_taxonomy(unit, tree, rules=rules)


async def reorder(conn: Connection, slugs: list[str], rules: word.Rules = RULES) -> None:
    async with Bridge(conn).write("reorder topics", taxonomy="exclusive") as unit:
        await word.reorder(unit, slugs, rules=rules)
