"""Retry policy for idempotent requests."""

from __future__ import annotations

import math
import random
from collections.abc import Callable
from dataclasses import dataclass

from tradesmart.errors import NetworkError, RateLimitError, RequestTimeoutError, ServerError

_RETRYABLE = (NetworkError, RequestTimeoutError, RateLimitError, ServerError)


@dataclass(frozen=True)
class RetryPolicy:
    max_retries: int
    base: float
    cap: float
    rng: Callable[[], float] = random.random

    def should_retry(self, attempt: int, exc: BaseException, *, state_changing: bool) -> bool:
        if state_changing or attempt >= self.max_retries:
            return False
        return isinstance(exc, _RETRYABLE)

    def delay(self, attempt: int, retry_after: float | None = None) -> float:
        if retry_after is not None and math.isfinite(retry_after):
            return max(0.0, min(self.cap, retry_after))  # a negative server value means "now"
        # No usable Retry-After (absent, NaN or infinite): the backoff schedule.
        return min(self.cap, self.base * 2.0**attempt) * (0.5 + self.rng() / 2)
