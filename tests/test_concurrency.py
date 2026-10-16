"""The bounded-concurrency helper the pipeline's steps share: its limit, its order, its stop."""

import asyncio

import pytest

from litwatch.concurrency import bounded


async def test_results_come_as_they_complete_at_most_limit_at_a_time():
    running, most = 0, 0

    async def work(delay: float) -> float:
        nonlocal running, most
        running += 1
        most = max(most, running)
        await asyncio.sleep(delay)
        running -= 1
        return delay

    async with bounded([0.06, 0.02, 0.04], work, limit=2) as results:
        assert [r async for r in results] == [0.02, 0.06, 0.04]
    assert most == 2


async def test_leaving_early_stops_every_task_before_the_block_ends():
    cancelled = []

    async def work(n: int) -> int:
        if n == 0:
            return n
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append(n)
            raise
        return n

    with pytest.raises(RuntimeError, match="the caller failed"):
        async with bounded(range(3), work, limit=3) as results:
            async for _ in results:
                raise RuntimeError("the caller failed")
    assert sorted(cancelled) == [1, 2]
