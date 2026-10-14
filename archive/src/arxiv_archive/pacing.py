"""Cross-process arXiv connection serialization and durable provider cooldown.

Only standard-library dependencies: ingestion clients can share this protocol
without importing the catalog/storage server. The configured directory must
already exist; a missing bind mount must never silently create private state.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, contextmanager
from email.utils import parsedate_to_datetime
import fcntl
import json
import math
import os
from pathlib import Path
import stat
import time


class PacingError(RuntimeError):
    """Shared pacing state is unavailable or invalid; fail closed."""


class Cooldown(PacingError):
    def __init__(self, delay: float):
        super().__init__('shared arXiv cooldown or connection busy')
        self.retry_after = delay


def retry_after_seconds(value: str | None, now: float | None = None) -> float:
    if not value:
        return 0
    now = time.time() if now is None else now
    try:
        seconds = float(value)
        return max(0, seconds) if math.isfinite(seconds) else 0
    except ValueError:
        try:
            return max(0, parsedate_to_datetime(value).timestamp() - now)
        except (ValueError, TypeError, OverflowError):
            return 0


class SharedPacer:
    def __init__(self, directory: str | Path):
        self.directory = Path(directory)
        self.lock_path = self.directory / 'arxiv-rate.lock'
        self.state_path = self.directory / 'arxiv-rate.json'

    def _open_lock(self):
        if not self.directory.is_dir() or self.directory.is_symlink():
            raise PacingError('shared arXiv pacing directory is unavailable')
        try:
            fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o660)
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                os.close(fd)
                raise PacingError('shared arXiv lock is not a regular file')
            return os.fdopen(fd, 'r+b')
        except OSError as exc:
            raise PacingError('cannot open shared arXiv lock') from exc

    def read_cooldown(self) -> float:
        try:
            with os.fdopen(os.open(self.state_path, os.O_RDONLY | os.O_NOFOLLOW)) as source:
                value = json.load(source)['not_before']
        except FileNotFoundError:
            return 0
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise PacingError('invalid shared arXiv cooldown state') from exc
        if type(value) not in (int, float) or not math.isfinite(value):
            raise PacingError('invalid shared arXiv cooldown state')
        return value

    def _save(self, timestamp: float):
        temporary = self.state_path.with_suffix('.tmp')
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o660)
        with os.fdopen(fd, 'w') as output:
            json.dump({'not_before': timestamp}, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, self.state_path)
        directory_fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def defer(self, retry_after: str | None):
        """Extend provider cooldown; call only while holding a request slot."""
        delay = retry_after_seconds(retry_after)
        if delay:
            self._save(max(self.read_cooldown(), time.time() + delay))

    def _try(self, lock) -> float:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0.1
        delay = self.read_cooldown() - time.time()
        if delay > 0:
            fcntl.flock(lock, fcntl.LOCK_UN)
            return delay
        self._save(time.time() + 3)
        return 0

    @contextmanager
    def slot(self, *, wait: bool = True, timeout: float | None = None):
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._open_lock() as lock:
            while True:
                if deadline is not None and time.monotonic() >= deadline:
                    raise Cooldown(max(0.1, self.read_cooldown() - time.time()))
                delay = self._try(lock)
                if not delay:
                    break
                if not wait:
                    raise Cooldown(delay)
                if deadline is not None and time.monotonic() + delay >= deadline:
                    raise Cooldown(delay)
                time.sleep(min(delay, 0.1))
            try:
                yield self
            finally:
                self._save(max(self.read_cooldown(), time.time() + 3))

    @asynccontextmanager
    async def async_slot(self, *, timeout: float | None = None):
        # Nonblocking flock avoids an orphaned thread acquiring the lock after
        # coroutine cancellation. Closing this fd always releases ownership.
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._open_lock() as lock:
            while True:
                if deadline is not None and time.monotonic() >= deadline:
                    raise Cooldown(max(0.1, self.read_cooldown() - time.time()))
                delay = self._try(lock)
                if not delay:
                    break
                if deadline is not None and time.monotonic() + delay >= deadline:
                    raise Cooldown(delay)
                await asyncio.sleep(min(delay, 0.1))
            try:
                yield self
            finally:
                self._save(max(self.read_cooldown(), time.time() + 3))
