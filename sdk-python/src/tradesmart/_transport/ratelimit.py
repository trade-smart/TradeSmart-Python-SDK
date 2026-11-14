"""Client-side sliding-window rate limiting."""

from __future__ import annotations

import asyncio
import threading
import time
from collections import deque
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Literal

from tradesmart.errors import RateLimitError

RATE_LIMITS: dict[str, tuple[tuple[int, float], ...]] = {
    "place_order": ((10, 1.0), (200, 60.0)),
    "quotes": ((100, 1.0), (1000, 60.0)),
    "tpseries": ((100, 1.0), (1000, 60.0)),
}


def describe_window(window: tuple[int, float] | None) -> str:
    """`` (200 per minute)`` / `` (10 per second)`` for a limiter window, ``""`` when unknown."""
    if window is None:
        return ""
    count, seconds = window
    unit = {1.0: "second", 60.0: "minute"}.get(seconds, f"{seconds:g} seconds")
    return f" ({count} per {unit})"


class RateLimiter:
    """Thread-safe sliding-window limiter over one or more ``(count, seconds)`` windows."""

    def __init__(
        self,
        limits: Sequence[tuple[int, float]],
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._limits = tuple(limits)
        self._clock = clock
        self._lock = threading.Lock()
        longest = max((count for count, _ in self._limits), default=0)
        self._stamps: deque[float] = deque(maxlen=longest or None)

    def now(self) -> float:
        """Current reading of the limiter's clock."""
        return self._clock()

    def try_acquire(self) -> float:
        """Record a call and return ``0.0``, or return seconds to wait (recording nothing)."""
        return self.try_acquire_window()[0]

    def try_acquire_window(self) -> tuple[float, tuple[int, float] | None]:
        """:meth:`try_acquire` plus the ``(count, seconds)`` window that refused (else ``None``)."""
        with self._lock:
            now = self._clock()
            wait = 0.0
            refusing: tuple[int, float] | None = None
            for count, window in self._limits:
                if len(self._stamps) < count:
                    continue
                oldest_in_window = self._stamps[-count]
                if now - oldest_in_window < window:
                    needed = oldest_in_window + window - now
                    if needed > wait:
                        wait, refusing = needed, (count, window)
            if wait > 0.0:
                return wait, refusing
            self._stamps.append(now)
            return 0.0, None

    def wait_for(self, n: int, window: float) -> float:
        """Seconds until ``n`` more calls fit the ``window``-second limit (``0.0``: now).

        Records nothing. ``float("inf")`` when ``n`` is more than that window's count.
        ``ValueError`` when no limit has that window length.
        """
        counts = [count for count, seconds in self._limits if seconds == window]
        if not counts:
            raise ValueError(f"no rate limit with a window of {window} seconds")
        allowed = counts[0]
        if n > allowed:
            return float("inf")
        with self._lock:
            now = self._clock()
            active = [stamp for stamp in self._stamps if now - stamp < window]
        must_expire = len(active) - (allowed - n)
        if must_expire <= 0:
            return 0.0
        return max(0.0, active[must_expire - 1] + window - now)


class RateLimiterRegistry:
    """Lazily creates one :class:`RateLimiter` per endpoint group.

    In ``"wait"`` mode a group listed in ``max_wait`` never waits longer than its cap in
    total: when the needed wait would exceed it, ``RateLimitError`` is raised instead.
    """

    def __init__(
        self,
        mode: Literal["wait", "raise"],
        *,
        max_wait: Mapping[str, float] | None = None,
        limits: Mapping[str, tuple[tuple[int, float], ...]] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._mode = mode
        self._clock = clock
        self._max_wait = dict(max_wait or {})
        self._limits = {**RATE_LIMITS, **(limits or {})}
        self._lock = threading.Lock()
        self._limiters: dict[str, RateLimiter] = {}

    def get(self, group: str | None) -> RateLimiter | None:
        if group is None:
            return None
        with self._lock:
            limiter = self._limiters.get(group)
            if limiter is None:
                if group not in self._limits:
                    raise ValueError(f"unknown rate limit group: {group!r}")
                limiter = RateLimiter(self._limits[group], clock=self._clock)
                self._limiters[group] = limiter
            return limiter

    def wait_for(self, group: str | None, n: int, window: float) -> float:
        """:meth:`RateLimiter.wait_for` of ``group`` (``0.0`` for a group without a limiter)."""
        limiter = self.get(group)
        return 0.0 if limiter is None else limiter.wait_for(n, window)

    def _raise(
        self,
        group: str,
        wait: float,
        endpoint: str | None,
        window: tuple[int, float] | None = None,
    ) -> RateLimitError:
        """``endpoint`` is the endpoint name (e.g. ``PlaceOrder``), like every other
        ``APIError``; the group and the window that refused appear only in the message,
        e.g. ``client-side rate limit for place_order (200 per minute)``."""
        return RateLimitError(
            f"client-side rate limit for {group}{describe_window(window)}",
            endpoint=endpoint or group,
            http_status=None,
            retry_after=wait,
        )

    def _deadline(self, group: str, limiter: RateLimiter) -> float | None:
        cap = self._max_wait.get(group)
        return None if cap is None else limiter.now() + cap

    def _must_raise(self, limiter: RateLimiter, wait: float, deadline: float | None) -> bool:
        if self._mode == "raise":
            return True
        return deadline is not None and limiter.now() + wait > deadline

    def acquire(
        self,
        group: str | None,
        *,
        sleep: Callable[[float], None] = time.sleep,
        endpoint: str | None = None,
    ) -> None:
        limiter = self.get(group)
        if limiter is None or group is None:
            return
        deadline = self._deadline(group, limiter)
        while True:
            wait, window = limiter.try_acquire_window()
            if wait <= 0.0:
                return
            if self._must_raise(limiter, wait, deadline):
                raise self._raise(group, wait, endpoint, window)
            sleep(wait)

    async def aacquire(
        self,
        group: str | None,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        endpoint: str | None = None,
    ) -> None:
        limiter = self.get(group)
        if limiter is None or group is None:
            return
        deadline = self._deadline(group, limiter)
        while True:
            wait, window = limiter.try_acquire_window()
            if wait <= 0.0:
                return
            if self._must_raise(limiter, wait, deadline):
                raise self._raise(group, wait, endpoint, window)
            await sleep(wait)
