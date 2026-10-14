"""Provider ownership persists across processes and coroutine cancellation."""
import asyncio
import json
import multiprocessing
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from arxiv_archive.pacing import Cooldown, PacingError, SharedPacer


def child_try(directory, result):
    try:
        with SharedPacer(directory).slot(wait=False):
            result.put('acquired')
    except Cooldown:
        result.put('blocked')


class PacingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self.tmp.name)
        self.pacer = SharedPacer(self.directory)

    def tearDown(self):
        self.tmp.cleanup()

    def test_cross_process_lock_and_durable_retry_after(self):
        context = multiprocessing.get_context('spawn')
        result = context.Queue()
        with self.pacer.slot():
            process = context.Process(target=child_try, args=(self.directory, result))
            process.start()
            self.assertEqual(result.get(timeout=10), 'blocked')
            process.join(timeout=10)
            self.assertEqual(process.exitcode, 0)
            self.pacer.defer('120')
        with self.assertRaises(Cooldown) as caught:
            with SharedPacer(self.directory).slot(wait=False):
                self.fail('cooldown bypassed')
        self.assertGreater(caught.exception.retry_after, 115)
        result.close()

    def test_waiting_async_cancellation_does_not_leak_lock(self):
        async def run():
            with self.pacer.slot():
                async def wait():
                    async with SharedPacer(self.directory).async_slot():
                        self.fail('must not acquire held lock')
                task = asyncio.create_task(wait())
                await asyncio.sleep(0.02)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            self.pacer.state_path.unlink()
            with SharedPacer(self.directory).slot(wait=False):
                pass
        asyncio.run(run())

    def test_async_owner_cancellation_closes_lock_and_persists_gap(self):
        async def run():
            entered = asyncio.Event()
            async def owner():
                async with self.pacer.async_slot():
                    entered.set()
                    await asyncio.Event().wait()
            task = asyncio.create_task(owner())
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertGreater(self.pacer.read_cooldown(), time.time())
            self.pacer.state_path.unlink()
            with SharedPacer(self.directory).slot(wait=False):
                pass
        asyncio.run(run())

    def test_damaged_state_and_missing_directory_fail_closed(self):
        self.pacer.state_path.write_text(json.dumps({'not_before': 'bad'}))
        with self.assertRaises(PacingError):
            with self.pacer.slot():
                self.fail('damaged cooldown accepted')
        with self.assertRaises(PacingError):
            with SharedPacer(self.directory / 'missing').slot():
                self.fail('private pacing directory created')

    def test_timeout_does_not_shorten_retry_after(self):
        with self.pacer.slot():
            self.pacer.defer('3600')
        with self.assertRaises(Cooldown):
            with self.pacer.slot(timeout=1):
                self.fail('timeout bypassed provider cooldown')
        self.assertGreater(self.pacer.read_cooldown(), time.time() + 3590)

    def test_cooldown_extends_from_response_completion(self):
        with patch('arxiv_archive.pacing.time.time', return_value=1000):
            with self.pacer.slot():
                with patch('arxiv_archive.pacing.time.time', return_value=1010):
                    self.pacer.defer('40')
        self.assertEqual(self.pacer.read_cooldown(), 1050)
