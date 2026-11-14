"""One quote-read pacer per UAT matrix session (full live run, 2026-11-13).

UAT limits ``GetQuotes`` to 10 per second and to 120 per minute (``Order Recieved 124 in a
current minute exceeds Limit 120 for user``). On UAT (``MatrixSession.uat_quote_pacer``) every
quote read of the matrix (contract resolution, ``pricing.fresh_quote``, the async views reads,
the modify band reads, the burst) waits on the session's pacer first: at most
``QUOTES_PER_MINUTE`` (110) in any rolling 60 s and ``QUOTES_PER_SECOND`` (8) in any rolling
second. Production has no quote pacer.

The rate-refusal retry of ``contracts.read_quote`` stays as a backup: on a ``current minute``
refusal it waits for the next wall-clock minute (``minute_wait``, at most 60 s).

Thread-safe (one lock around a short window check), like ``order_pacer.OrderPacer``; the async
reads sleep with the given coroutine, never blocking the event loop.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Awaitable, Callable

QUOTES_PER_SECOND = 8  # UAT allows 10 per second
QUOTES_PER_MINUTE = 110  # UAT allows 120 per minute
MINUTE_WORDS = "current minute"
MAX_MINUTE_WAIT = 60.0
MIN_SLEEP = 0.001


def minute_wait(message: str, *, wall: Callable[[], float] = time.time) -> float | None:
    """How long to wait after a rate refusal that names the minute limit (until the next
    wall-clock minute, plus 0.5 s, at most 60 s); ``None`` for any other message."""
    if MINUTE_WORDS not in message.lower():
        return None
    return min(MAX_MINUTE_WAIT, 60.0 - (wall() % 60.0) + 0.5)


class QuotePacer:
    """Two rolling windows over this session's quote reads (1 s and 60 s)."""

    def __init__(
        self,
        *,
        per_second: int = QUOTES_PER_SECOND,
        per_minute: int = QUOTES_PER_MINUTE,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        wall: Callable[[], float] = time.time,
    ) -> None:
        self.windows = ((per_second, 1.0), (per_minute, 60.0))
        self.clock = clock
        self.sleep = sleep
        self.wall = wall
        self.total = 0  # reads counted so far (records and tests)
        self._stamps: deque[float] = deque()
        self._lock = threading.Lock()

    def reserve(self) -> float:
        """Count one read now and return 0.0 when it fits both windows; else count nothing and
        return how long to wait."""
        with self._lock:
            now = self.clock()
            longest = max(window for _, window in self.windows)
            while self._stamps and now - self._stamps[0] >= longest:
                self._stamps.popleft()
            delay = 0.0
            for limit, window in self.windows:
                recent = [s for s in self._stamps if now - s < window]
                if len(recent) >= limit:
                    delay = max(delay, recent[len(recent) - limit] + window - now)
            if delay <= 0.0:
                self._stamps.append(now)
                self.total += 1
                return 0.0
            return delay

    def wait(self) -> None:
        """Block until one more read fits both windows, then count it."""
        while (delay := self.reserve()) > 0.0:
            self.sleep(max(delay, MIN_SLEEP))

    async def wait_async(self, sleep: Callable[[float], Awaitable[None]]) -> None:
        """``wait`` for the async reads: sleeps with ``sleep`` (never blocks the loop)."""
        while (delay := self.reserve()) > 0.0:
            await sleep(max(delay, MIN_SLEEP))

    def refusal_wait(self, message: str, default: float) -> float:
        """The backup retry's wait after a rate refusal: the minute's rest, else ``default``."""
        wait = minute_wait(message, wall=self.wall)
        return default if wait is None else wait
