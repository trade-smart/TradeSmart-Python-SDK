"""One order-call pacer per UAT matrix session (live smoke run, 2026-11-13).

TradeSmart's API order check (``ALGO_CHK: More than 10 orders received in the previous second``)
counts every state-changing call of the user: place, modify, cancel, exit (and the GTT writes).
The SDK's own limiter paces only new orders, so the matrix paces them all here: at most
``ORDER_CALLS_PER_SECOND`` (6; at 8 one ALGO_CHK refusal was still seen, 13 Nov 2026) in any
rolling second. Every never-resend
wrapper of ``calls.py`` / ``calls_async.py`` (the GTT wrappers included) waits on the session's
pacer before its intent is written, never between the SDK call and its outcome. A sliced place
counts as its slice count (``max_slices``).

Thread-safe (one lock around a short window check); the async wrappers sleep with the given
coroutine (``asyncio.sleep`` for a real session), never blocking the event loop.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Awaitable, Callable

ORDER_CALLS_PER_SECOND = 6  # TradeSmart allows 10 per second (ALGO_CHK); 8 still tripped it once
WINDOW_SECONDS = 1.0
MIN_SLEEP = 0.001  # a fake clock still moves on when float rounding leaves a tiny wait


class OrderPacer:
    """A rolling window over this session's state-changing calls."""

    def __init__(
        self,
        *,
        per_second: int = ORDER_CALLS_PER_SECOND,
        window: float = WINDOW_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.per_second = per_second
        self.window = window
        self.clock = clock
        self.sleep = sleep
        self._stamps: deque[float] = deque()
        self._lock = threading.Lock()

    def reserve(self, n: int = 1) -> float:
        """Count ``n`` calls now and return 0.0 when they fit the window; else count nothing and
        return how long to wait. A weight above the limit waits for an empty window."""
        if n < 1:
            raise ValueError(f"an order call counts at least 1, got {n}")
        with self._lock:
            now = self.clock()
            while self._stamps and now - self._stamps[0] >= self.window:
                self._stamps.popleft()
            over = len(self._stamps) - max(self.per_second - n, 0)
            if over <= 0:
                self._stamps.extend([now] * n)
                return 0.0
            return self._stamps[over - 1] + self.window - now

    def wait(self, n: int = 1) -> None:
        """Block until ``n`` more calls fit the window, then count them."""
        while (delay := self.reserve(n)) > 0.0:
            self.sleep(max(delay, MIN_SLEEP))

    async def wait_async(self, n: int, sleep: Callable[[float], Awaitable[None]]) -> None:
        """``wait`` for the async wrappers: sleeps with ``sleep`` (never blocks the loop)."""
        while (delay := self.reserve(n)) > 0.0:
            await sleep(max(delay, MIN_SLEEP))
