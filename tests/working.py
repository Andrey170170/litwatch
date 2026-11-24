"""A Work gate in a test: waiting on what must happen (`until`), serving a gate's Workers for a
block (`serving`), and reading a key's work items back (`items_of`)."""

import asyncio
import contextlib
from collections.abc import AsyncGenerator, Awaitable, Callable, Collection, Mapping
from typing import Any

from litwatch.gates.work import Worker, WorkGate
from tests.sessions import Session

WAIT = 10.0  # how long a test waits on something that must happen


async def until(check: Callable[[], Awaitable[bool]], what: str) -> None:
    """Wait until `check` holds, looking again every 50 ms, for at most `WAIT`; a timeout says
    `what` it waited for."""
    try:
        async with asyncio.timeout(WAIT):
            while not await check():
                await asyncio.sleep(0.05)
    except TimeoutError:
        raise TimeoutError(f"waited {WAIT:.0f}s for: {what}") from None


@contextlib.asynccontextmanager
async def serving(
    gate: WorkGate,
    workers: Mapping[str, Worker],
    keys: Mapping[str, Collection[str]] | None = None,
) -> AsyncGenerator[asyncio.Task]:
    """`gate.run(workers, keys=keys)` for the block, listening and asleep before it starts;
    stopped at the end."""
    task = asyncio.create_task(gate.run(workers, keys=keys))

    async def idle() -> bool:
        return gate.listening.is_set() and gate.idle

    try:
        await until(idle, "the gate listens and sleeps")
        yield task
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def items_of(conn: Session, kind: str, key: str) -> list[dict[str, Any]]:
    """The work items of `kind` and `key`, oldest first."""
    return await (
        await conn.execute(
            "SELECT * FROM work_items WHERE kind = %s AND key = %s ORDER BY id", (kind, key)
        )
    ).fetchall()
