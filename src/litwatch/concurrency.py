"""Bounded concurrency for the pipeline's model calls: one helper owns the tasks, so no step
keeps its own copy of the semaphore, the completion order, the cancel and the drain."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from contextlib import aclosing, asynccontextmanager


@asynccontextmanager
async def bounded[T, R](
    items: Iterable[T], work: Callable[[T], Awaitable[R]], limit: int
) -> AsyncIterator[AsyncIterator[R]]:
    """Run `work` on each item, at most `limit` at a time, and give the results in the order
    they complete. Leaving the block, early or by an exception, cancels every task still
    running and waits for them all, so no call goes on behind the caller. A task's exception
    is raised where its result would be."""
    gate = asyncio.Semaphore(limit)

    async def one(item: T) -> R:
        async with gate:
            return await work(item)

    tasks = [asyncio.create_task(one(item)) for item in items]

    async def completed() -> AsyncIterator[R]:
        for done in asyncio.as_completed(tasks):
            yield await done

    try:
        async with aclosing(completed()) as results:
            yield results
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
