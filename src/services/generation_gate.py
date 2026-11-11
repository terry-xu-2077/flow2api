"""Global admission gate for image generation.

Caps how many image generations run at the same time. Requests above the cap either wait
in a FIFO queue or are rejected right away, depending on ``mode``. The limits are read on
every admission, so a config change applies to new requests without a restart.
"""

import asyncio
from collections import deque
from typing import Callable, Deque, Optional


class GateRejected(Exception):
    """Raised when a request is refused instead of queued."""

    def __init__(self, reason: str, active: int, waiting: int):
        super().__init__(reason)
        self.reason = reason
        self.active = active
        self.waiting = waiting


class GenerationGate:
    def __init__(self, limit: Callable[[], int], mode: Callable[[], str],
                 max_wait: Callable[[], float], max_queue: Callable[[], int]):
        self._limit = limit
        self._mode = mode
        self._max_wait = max_wait
        self._max_queue = max_queue
        self.active = 0
        self._waiters: Deque[asyncio.Future] = deque()

    @property
    def waiting(self) -> int:
        return len(self._waiters)

    def position(self, waiter: asyncio.Future) -> int:
        """1-based place in the queue, 0 if the waiter is no longer queued."""
        for index, item in enumerate(self._waiters, start=1):
            if item is waiter:
                return index
        return 0

    async def acquire(self, on_wait: Optional[Callable[[int, int], None]] = None,
                      notify_interval: float = 5.0) -> bool:
        """Take a slot. Returns True when a slot is held (caller must release()).

        Returns False when the gate is disabled (limit <= 0); nothing to release then.
        ``on_wait(position, active)`` is called when queued and then every
        ``notify_interval`` seconds so a streaming caller can show progress.
        """
        limit = int(self._limit() or 0)
        if limit <= 0:
            return False
        if self.active < limit and not self._waiters:
            self.active += 1
            return True

        if str(self._mode()).strip().lower() == "reject":
            raise GateRejected("busy", self.active, self.waiting)
        max_queue = int(self._max_queue() or 0)
        if max_queue > 0 and self.waiting >= max_queue:
            raise GateRejected("queue_full", self.active, self.waiting)

        loop = asyncio.get_running_loop()
        waiter: asyncio.Future = loop.create_future()
        self._waiters.append(waiter)
        deadline = loop.time() + max(1.0, float(self._max_wait() or 0))
        try:
            while True:
                if on_wait is not None:
                    on_wait(self.position(waiter), self.active)
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise GateRejected("wait_timeout", self.active, self.waiting)
                try:
                    await asyncio.wait_for(
                        asyncio.shield(waiter), min(notify_interval, remaining)
                    )
                    return True  # release() handed this waiter a slot
                except asyncio.TimeoutError:
                    continue
        except BaseException:
            if waiter.done() and not waiter.cancelled():
                # A slot was handed over just as we gave up: pass it on.
                self._handoff()
            else:
                self._discard(waiter)
            raise

    def _discard(self, waiter: asyncio.Future):
        try:
            self._waiters.remove(waiter)
        except ValueError:
            pass
        if not waiter.done():
            waiter.cancel()

    def _handoff(self):
        """Give the freed slot to the first live waiter, else lower the active count."""
        limit = int(self._limit() or 0)
        while self._waiters:
            waiter = self._waiters[0]
            if waiter.done():
                self._waiters.popleft()
                continue
            if limit > 0 and self.active > limit:
                break  # limit was lowered; let active drain below it first
            self._waiters.popleft()
            waiter.set_result(True)
            return
        self.active = max(0, self.active - 1)

    def release(self):
        self._handoff()
