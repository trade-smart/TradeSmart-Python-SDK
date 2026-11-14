import asyncio

import httpx
import pytest

from tradesmart._transport.ratelimit import RateLimiter, RateLimiterRegistry
from tradesmart._transport.retry import RetryPolicy
from tradesmart._transport.sync_http import apply_retry_after
from tradesmart.errors import APIError, NetworkError, RateLimitError, ServerError


def test_limiter_per_second_window():
    t = [0.0]
    lim = RateLimiter([(2, 1.0)], clock=lambda: t[0])
    assert lim.try_acquire() == 0.0 and lim.try_acquire() == 0.0
    assert lim.try_acquire() == pytest.approx(1.0)
    t[0] = 1.01
    assert lim.try_acquire() == 0.0


def test_limiter_minute_window_dominates():
    t = [0.0]
    lim = RateLimiter([(30, 1.0), (600, 60.0)], clock=lambda: t[0])
    for i in range(600):
        t[0] = i * 0.05
        assert lim.try_acquire() == 0.0
    assert lim.try_acquire() > 0


class _Slept(Exception):
    pass


def test_order_rate_defaults_to_10_per_second():
    reg = RateLimiterRegistry("raise")
    limiter = reg.get("place_order")
    assert limiter is not None
    limiter._clock = lambda: 1000.0  # 11 orders in the same instant
    for _ in range(10):
        reg.acquire("place_order")
    with pytest.raises(RateLimitError):
        reg.acquire("place_order")


def test_order_rate_is_configurable_up_to_30():
    reg = RateLimiterRegistry("raise", limits={"place_order": ((30, 1.0), (600, 60.0))})
    limiter = reg.get("place_order")
    assert limiter is not None
    limiter._clock = lambda: 1000.0
    for _ in range(30):
        reg.acquire("place_order")
    with pytest.raises(RateLimitError):
        reg.acquire("place_order")


def test_order_rate_wait_mode_waits_for_the_11th_order():
    reg = RateLimiterRegistry("wait")
    limiter = reg.get("place_order")
    assert limiter is not None
    limiter._clock = lambda: 1000.0
    for _ in range(10):
        reg.acquire("place_order")
    slept: list[float] = []

    def stop(seconds: float) -> None:
        slept.append(seconds)
        raise _Slept

    # the frozen clock never advances, so stop the retry loop at the first sleep
    with pytest.raises(_Slept):
        reg.acquire("place_order", sleep=stop)
    assert slept == [pytest.approx(1.0)]


def test_registry_limits_override_only_the_named_group():
    reg = RateLimiterRegistry("raise", limits={"place_order": ((5, 1.0),)})
    quotes = reg.get("quotes")
    assert quotes is not None and quotes._limits == ((100, 1.0), (1000, 60.0))
    orders = reg.get("place_order")
    assert orders is not None and orders._limits == ((5, 1.0),)


def test_minute_window_of_600_is_unchanged_at_10_per_second():
    t = [0.0]
    lim = RateLimiter([(10, 1.0), (600, 60.0)], clock=lambda: t[0])
    for i in range(600):
        t[0] = (i // 10) * 1.0 + (i % 10) * 0.0625  # 10 per second for 60 s (exact floats)
        assert lim.try_acquire() == 0.0
    t[0] = 59.9375  # still inside the first order's 60 s window
    assert lim.try_acquire() > 0


def test_registry_raise_mode():
    reg = RateLimiterRegistry("raise")
    for _ in range(10):
        reg.acquire("place_order")
    with pytest.raises(RateLimitError) as ei:
        reg.acquire("place_order")
    assert ei.value.retry_after is not None and ei.value.retry_after > 0
    reg.acquire(None)  # ungrouped endpoints are unlimited


def test_registry_unknown_group_is_value_error():
    with pytest.raises(ValueError):
        RateLimiterRegistry("wait").get("nope")
    assert RateLimiterRegistry("wait").get(None) is None


def test_registry_wait_mode_sleeps_then_proceeds():
    reg = RateLimiterRegistry("wait")
    for _ in range(10):
        reg.acquire("place_order")
    slept: list[float] = []
    reg.acquire("place_order", sleep=slept.append)
    assert slept and slept[0] > 0


async def test_aacquire_wait_mode_awaits_sleep():
    reg = RateLimiterRegistry("wait")
    lim = reg.get("quotes")
    assert lim is not None
    lim._limits = ((1, 0.05),)  # tiny window
    await reg.aacquire("quotes")
    await reg.aacquire("quotes")  # must wait ~0.05s, then succeed
    await reg.aacquire(None)


async def test_aacquire_uses_the_injected_async_sleep():
    reg = RateLimiterRegistry("wait")
    lim = reg.get("quotes")
    assert lim is not None
    now = [0.0]
    lim._clock = lambda: now[0]
    lim._limits = ((1, 1.0),)
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        now[0] += seconds

    await reg.aacquire("quotes", sleep=fake_sleep)
    # The fake clock only moves in fake_sleep: asyncio.sleep would wait forever.
    await asyncio.wait_for(reg.aacquire("quotes", sleep=fake_sleep), timeout=2.0)
    assert slept == [pytest.approx(1.0)]


async def test_aacquire_raise_mode():
    reg = RateLimiterRegistry("raise")
    for _ in range(10):
        await reg.aacquire("place_order")
    with pytest.raises(RateLimitError):
        await reg.aacquire("place_order")


def test_retry_never_for_state_changing():
    p = RetryPolicy(max_retries=3, base=0.5, cap=8.0, rng=lambda: 1.0)
    assert not p.should_retry(0, NetworkError("x"), state_changing=True)
    assert p.should_retry(0, ServerError("x", endpoint="e", http_status=502), state_changing=False)
    assert not p.should_retry(3, NetworkError("x"), state_changing=False)
    assert not p.should_retry(0, APIError("x", endpoint="e", http_status=200), state_changing=False)
    assert p.delay(10) == 8.0 and p.delay(0, retry_after=2.0) == 2.0
    assert p.delay(0, retry_after=100.0) == 8.0


# --- order wait cap (review 2026-11-10 C3a) ---------------------------------------------


def test_registry_wait_mode_order_wait_beyond_cap_raises_without_sleeping():
    reg = RateLimiterRegistry("wait", max_wait={"place_order": 0.1})
    for _ in range(10):
        reg.acquire("place_order")
    slept: list[float] = []
    with pytest.raises(RateLimitError) as ei:
        reg.acquire("place_order", sleep=slept.append)
    assert slept == []
    assert ei.value.retry_after is not None and ei.value.retry_after > 0.1


def test_registry_wait_mode_order_wait_within_cap_sleeps():
    reg = RateLimiterRegistry("wait", max_wait={"place_order": 5.0})
    for _ in range(10):
        reg.acquire("place_order")
    slept: list[float] = []
    reg.acquire("place_order", sleep=slept.append)
    assert slept and 0 < slept[0] <= 1.0


def test_registry_wait_cap_applies_only_to_its_group():
    reg = RateLimiterRegistry("wait", max_wait={"place_order": 0.0})
    lim = reg.get("quotes")
    assert lim is not None
    lim._limits = ((1, 0.05),)
    reg.acquire("quotes")
    slept: list[float] = []
    reg.acquire("quotes", sleep=slept.append)  # reads still wait
    assert slept


async def test_aacquire_order_wait_beyond_cap_raises():
    reg = RateLimiterRegistry("wait", max_wait={"place_order": 0.0})
    for _ in range(10):
        await reg.aacquire("place_order")
    with pytest.raises(RateLimitError):
        await reg.aacquire("place_order")


@pytest.mark.parametrize(
    ("retry_after", "expected"), [(-5.0, 0.0), (-0.1, 0.0), (0.0, 0.0), (3.0, 3.0), (99.0, 8.0)]
)
def test_retry_after_is_clamped_between_zero_and_cap(retry_after: float, expected: float) -> None:
    # A negative Retry-After from a server must never reach sleep() (which raises ValueError).
    p = RetryPolicy(max_retries=3, base=0.5, cap=8.0, rng=lambda: 1.0)
    assert p.delay(0, retry_after) == expected


@pytest.mark.parametrize("retry_after", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_retry_after_falls_back_to_the_backoff_schedule(retry_after: float) -> None:
    p = RetryPolicy(max_retries=3, base=0.5, cap=8.0, rng=lambda: 1.0)
    assert p.delay(1, retry_after) == p.delay(1) == 1.0


@pytest.mark.parametrize(
    ("header", "expected"),
    [("2", 2.0), ("0.5", 0.5), ("nan", None), ("inf", None), ("-inf", None), ("soon", None)],
)
def test_apply_retry_after_keeps_only_finite_numbers(header: str, expected: object) -> None:
    exc = RateLimitError("x", endpoint="Limits", http_status=429)
    apply_retry_after(exc, httpx.Response(429, headers={"Retry-After": header}))
    assert exc.retry_after == expected


def test_registry_uses_the_injected_clock_and_two_windows():
    t = [0.0]
    reg = RateLimiterRegistry(
        "raise", limits={"place_order": ((10, 1.0), (200, 60.0))}, clock=lambda: t[0]
    )
    for i in range(200):
        t[0] = i * 0.2
        reg.acquire("place_order")
    t[0] = 40.0
    with pytest.raises(RateLimitError) as caught:
        reg.acquire("place_order")
    assert caught.value.retry_after == pytest.approx(20.0)


def test_place_order_fallback_limit_is_200_per_minute():
    from tradesmart._transport.ratelimit import RATE_LIMITS

    assert RATE_LIMITS["place_order"] == ((10, 1.0), (200, 60.0))


def test_wait_for_counts_only_the_named_window() -> None:
    t = [0.0]
    lim = RateLimiter(((10, 1.0), (5, 60.0)), clock=lambda: t[0])
    for i in range(4):
        t[0] = float(i)
        assert lim.try_acquire() == 0.0
    t[0] = 10.0
    assert lim.wait_for(1, 60.0) == 0.0
    assert lim.wait_for(3, 60.0) == pytest.approx(51.0)  # stamps 0 and 1 must expire
    assert lim.wait_for(6, 60.0) == float("inf")
    assert len(lim._stamps) == 4  # nothing recorded


def test_wait_for_ignores_stamps_outside_the_window_and_unknown_window_is_refused() -> None:
    t = [0.0]
    lim = RateLimiter(((10, 1.0), (2, 60.0)), clock=lambda: t[0])
    assert lim.try_acquire() == 0.0
    t[0] = 61.0
    assert lim.wait_for(2, 60.0) == 0.0  # the old stamp has expired
    with pytest.raises(ValueError, match="window"):
        lim.wait_for(1, 30.0)


def test_registry_wait_for_delegates_and_unlimited_group_is_zero() -> None:
    t = [0.0]
    reg = RateLimiterRegistry(
        "wait", limits={"place_order": ((10, 1.0), (2, 60.0))}, clock=lambda: t[0]
    )
    reg.acquire("place_order")
    reg.acquire("place_order")
    t[0] = 20.0
    assert reg.wait_for("place_order", 1, 60.0) == pytest.approx(40.0)
    assert reg.wait_for("place_order", 3, 60.0) == float("inf")
