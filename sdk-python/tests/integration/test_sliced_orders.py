"""``place_sliced_order`` end to end on the offline broker, through the real sync and async clients.

Live production values (2026-11-12, spec 5.7): NIFTY future lot 65 / ``frzqty`` 1801 (largest
slice 1755), SENSEX option lot 20 / ``frzqty`` 1000, MCX CRUDEOIL lot 100 / ``frzqty`` 2500
(25 lots; MCX quantities are in units, like NFO). Every case runs on ``TradeSmart`` and
``AsyncTradeSmart``.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import pytest

from tradesmart import AsyncTradeSmart, SlicedOrderResult, TradeSmart
from tradesmart.errors import (
    OrderRejectedError,
    OrderStatusUnknownError,
    RateLimitError,
    RequestValidationError,
)
from tradesmart.testing import OfflineBroker, drills
from tradesmart.testing.drills import Drill

NIFTY = "NIFTY26NOVFUT"
BANKNIFTY = "BANKNIFTY26NOVFUT"  # no freeze quantity in the offline broker (frzqty blank)
SENSEX = "SENSEX26N1985200CE"
CRUDE = "CRUDEOIL26DECFUT"
ITC = "ITC-EQ"
EXPIRY = date.today() + timedelta(days=14)

Client = TradeSmart | AsyncTradeSmart


def _instruments(broker: OfflineBroker) -> None:
    broker.add_instrument(
        "NFO", "35001", NIFTY, lot_size=65, instrument="FUTIDX", expiry=EXPIRY,
        underlying="NIFTY", frzqty=1801,
    )  # fmt: skip
    broker.add_instrument(
        "NFO", "35002", BANKNIFTY, lot_size=30, instrument="FUTIDX", expiry=EXPIRY,
        underlying="BANKNIFTY",
    )  # fmt: skip
    broker.add_instrument(
        "BFO", "801", SENSEX, lot_size=20, instrument="OPTIDX", expiry=EXPIRY, strike=85200,
        option_type="CE", underlying="BSXOPT", frzqty=1000,
    )  # fmt: skip
    broker.add_instrument(
        "MCX", "472780", CRUDE, lot_size=100, instrument="FUTCOM", expiry=EXPIRY,
        underlying="CRUDEOIL", frzqty=2500,
    )  # fmt: skip
    broker.add_instrument("NSE", "1660", ITC, lot_size=1, frzqty=350854)


@dataclass
class Rig:
    broker: OfflineBroker
    kind: str
    clients: list[Client] = field(default_factory=list)

    def client(self, **overrides: Any) -> Client:
        built: Client = (
            self.broker.client(**overrides)
            if self.kind == "sync"
            else self.broker.async_client(**overrides)
        )
        self.clients.append(built)
        return built

    async def sliced(self, client: Client, **kwargs: Any) -> SlicedOrderResult:
        result = client.place_sliced_order(**kwargs)
        if inspect.isawaitable(result):
            result = await result
        assert isinstance(result, SlicedOrderResult)
        return result

    def sent(self, endpoint: str = "PlaceOrder") -> list[dict[str, Any]]:
        return [r.payload for r in self.broker.requests if r.endpoint == endpoint]

    def quantities(self) -> list[int]:
        return [int(p["qty"]) for p in self.sent()]


@pytest.fixture(params=["sync", "async"])
async def rig(request: pytest.FixtureRequest) -> AsyncIterator[Rig]:
    with OfflineBroker() as broker:
        _instruments(broker)
        built = Rig(broker, request.param)
        try:
            yield built
        finally:
            for client in built.clients:
                if isinstance(client, AsyncTradeSmart):
                    await client.aclose()
                else:
                    client.close()


def _order(**overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "exchange": "NFO",
        "tradingsymbol": NIFTY,
        "quantity": 3965,
        "transaction_type": "BUY",
        "product": "M",
        "order_type": "LMT",
        "price": "24000.50",
        "tag": "slicer",
    }
    kwargs.update(overrides)
    return kwargs


def _arm_on_call(rig: Rig, client: Client, number: int, drill: Drill) -> None:
    """Arm ``drill`` for the ``number``-th ``place_order`` call (1-based) of ``client``."""
    original: Callable[..., Any] = client.place_order
    calls = 0

    def arm() -> None:
        nonlocal calls
        calls += 1
        if calls == number:
            rig.broker.fail_next("PlaceOrder", drill)

    if inspect.iscoroutinefunction(original):

        async def wrapped_async(**kwargs: Any) -> str:
            arm()
            return str(await original(**kwargs))

        client.place_order = wrapped_async  # type: ignore[method-assign]
    else:

        def wrapped(**kwargs: Any) -> str:
            arm()
            return str(original(**kwargs))

        client.place_order = wrapped  # type: ignore[method-assign]


# --- the offline broker serves a freeze quantity ---------------------------------------------


async def test_offline_broker_security_info_carries_frzqty(rig: Rig) -> None:
    client = rig.client()
    info = client.security_info("NFO", "35001")
    other = client.security_info("NFO", "35002")
    if inspect.isawaitable(info):
        info = await info
    if inspect.isawaitable(other):
        other = await other
    assert info.frzqty == 1801 and info.lot_size == 65
    assert other.frzqty is None  # blank, as before


# --- slicing end to end -------------------------------------------------------------------


async def test_nifty_order_is_sent_as_three_slices(rig: Rig) -> None:
    result = await rig.sliced(rig.client(), **_order())

    assert rig.quantities() == [1755, 1755, 455]
    payloads = rig.sent()
    assert {p["prc"] for p in payloads} == {"24000.50"}
    assert {p["remarks"] for p in payloads} == {"slicer"}
    assert {p["prd"] for p in payloads} == {"M"}
    assert {p["tsym"] for p in payloads} == {NIFTY}
    assert {p["trantype"] for p in payloads} == {"B"}
    assert result.requested_quantity == 3965
    assert result.slice_quantities == (1755, 1755, 455)
    assert result.order_ids == tuple(order.id for order in rig.broker.orders)
    assert len(result.order_ids) == 3
    assert result.placed_quantity == 3965
    assert result.unsent_quantity == 0
    assert result.complete is True
    assert result.error is None


@pytest.mark.parametrize(
    ("quantity", "extra"),
    [(130, {}), (65, {}), (130, {"max_slice_quantity": 650})],
)
async def test_quantity_within_one_slice_is_one_place_order(
    rig: Rig, quantity: int, extra: dict[str, Any]
) -> None:
    result = await rig.sliced(rig.client(), **_order(quantity=quantity, **extra))

    assert rig.quantities() == [quantity]
    assert result.slice_quantities == (quantity,)
    assert result.complete and result.placed_quantity == quantity and len(result.order_ids) == 1


async def test_sensex_option_slices_equal_the_freeze_quantity(rig: Rig) -> None:
    result = await rig.sliced(
        rig.client(), **_order(exchange="BFO", tradingsymbol=SENSEX, quantity=2000)
    )
    assert rig.quantities() == [1000, 1000]
    assert result.complete and result.placed_quantity == 2000


async def test_mcx_quantity_is_in_units_like_nfo(rig: Rig) -> None:
    # F21 (owner, 2026-11-12): 60 CRUDEOIL lots are quantity 6000; frzqty 2500 = 25 lots a slice.
    result = await rig.sliced(
        rig.client(), **_order(exchange="MCX", tradingsymbol=CRUDE, quantity=6000)
    )
    assert rig.quantities() == [2500, 2500, 1000]
    assert result.slice_quantities == (2500, 2500, 1000)
    assert result.complete and result.placed_quantity == 6000


async def test_mcx_quantity_below_one_slice_is_one_order(rig: Rig) -> None:
    result = await rig.sliced(
        rig.client(), **_order(exchange="MCX", tradingsymbol=CRUDE, quantity=200)
    )
    assert rig.quantities() == [200]
    assert result.complete and result.placed_quantity == 200


async def test_mcx_quantity_not_whole_lots_is_refused(rig: Rig) -> None:
    await _refused(
        rig, ValueError, "whole number of lots", exchange="MCX", tradingsymbol=CRUDE, quantity=150
    )


async def test_equity_contract_is_found(rig: Rig) -> None:
    result = await rig.sliced(
        rig.client(), **_order(exchange="NSE", tradingsymbol=ITC, quantity=1, product="I")
    )
    assert rig.quantities() == [1] and result.complete


@pytest.mark.parametrize("given", [NIFTY.lower(), f"  {NIFTY}  ", f" {NIFTY.lower()}\t"])
async def test_tradingsymbol_case_and_padding_are_ignored_in_the_lookup(
    rig: Rig, given: str
) -> None:
    result = await rig.sliced(rig.client(), **_order(tradingsymbol=given))
    assert rig.quantities() == [1755, 1755, 455]
    assert result.complete


async def test_max_slice_quantity_lowers_the_slice_size(rig: Rig) -> None:
    result = await rig.sliced(rig.client(), **_order(quantity=1300, max_slice_quantity=650))
    assert rig.quantities() == [650, 650]
    assert result.complete


async def test_blank_freeze_quantity_uses_max_slice_quantity(rig: Rig) -> None:
    result = await rig.sliced(
        rig.client(), **_order(tradingsymbol=BANKNIFTY, quantity=150, max_slice_quantity=60)
    )
    assert rig.quantities() == [60, 60, 30]
    assert result.complete


async def test_summary_line_on_the_orders_logger(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="tradesmart")
    await rig.sliced(rig.client(), **_order())
    lines = [
        r.getMessage()
        for r in caplog.records
        if r.name == "tradesmart.orders" and "place_sliced_order" in r.getMessage()
    ]
    assert len(lines) == 1
    assert "3/3" in lines[0]
    assert NIFTY not in lines[0] and "24000" not in lines[0]
    assert [
        r.levelno
        for r in caplog.records
        if r.name == "tradesmart.orders" and "place_sliced_order" in r.getMessage()
    ] == [logging.INFO]


# --- stop at the first failure --------------------------------------------------------------


async def test_rejection_on_slice_two_stops_and_returns_the_partial_result(rig: Rig) -> None:
    client = rig.client()
    _arm_on_call(rig, client, 2, drills.reject("Rejected : RMS: margin exceeded"))

    result = await rig.sliced(client, **_order())

    assert rig.quantities() == [1755, 1755]  # no third call
    assert result.complete is False
    assert len(result.order_ids) == 1
    assert result.order_ids == (rig.broker.orders[0].id,)
    assert result.placed_quantity == 1755
    assert result.unsent_quantity == 2210
    assert result.slice_quantities == (1755, 1755, 455)
    assert isinstance(result.error, OrderRejectedError)


async def test_ambiguous_slice_two_raises_with_the_partial_result(rig: Rig) -> None:
    client = rig.client()
    _arm_on_call(rig, client, 2, drills.timeout_after_send())

    with pytest.raises(OrderStatusUnknownError) as caught:
        await rig.sliced(client, **_order())

    assert rig.quantities() == [1755, 1755]  # no third call
    partial = caught.value.sliced_result
    assert partial is not None
    assert partial.order_ids == (rig.broker.orders[0].id,)
    assert partial.placed_quantity == 1755
    assert partial.unsent_quantity == 2210
    assert partial.complete is False and partial.error is None
    notes = " ".join(getattr(caught.value, "__notes__", []))
    assert "orders()" in notes and "slice 2 of 3" in notes


async def test_the_minute_window_stops_the_slices_and_returns_the_rate_limit_error(
    rig: Rig,
) -> None:
    # The up-front room check passes, then another thread uses the slots (a race, simulated by a
    # check that sees room). 199 other orders were sent 30 s ago (the same minute, not the same
    # second): slice 1 is the 200th order of the minute, slice 2 would wait 30 s > 1 s.
    client = rig.client()
    limiter = client._transport._limiter.get("place_order")
    assert limiter is not None
    client._transport.order_room_wait = lambda n: 0.0  # type: ignore[method-assign]
    limiter._clock = lambda: 1000.0  # every slice at the same instant however slow the host
    limiter._stamps.extend([970.0] * 199)

    result = await rig.sliced(client, **_order(quantity=3510))

    assert rig.quantities() == [1755]
    assert result.complete is False
    assert isinstance(result.error, RateLimitError)  # returned, never raised
    assert "(200 per minute)" in str(result.error)
    assert len(result.order_ids) == 1
    assert result.placed_quantity == 1755 and result.unsent_quantity == 1755


# --- slices the order rate limit would stop: refused before sending (I2) ---------------------


@dataclass
class _FakeTime:
    now: float = 1000.0

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    async def asleep(self, seconds: float) -> None:
        self.now += seconds


def _fake_time(client: Client) -> _FakeTime:
    """Run ``client``'s order limiter on a fake clock: its waits advance time, never block."""
    fake = _FakeTime()
    limiter = client._transport._limiter.get("place_order")
    assert limiter is not None
    limiter._clock = fake.clock
    client._transport._sleep = fake.asleep if isinstance(client, AsyncTradeSmart) else fake.sleep  # type: ignore[assignment]
    return fake


async def test_default_config_sends_25_slices_at_10_per_second(rig: Rig) -> None:
    client = rig.client()
    fake = _fake_time(client)

    result = await rig.sliced(client, **_order(quantity=25 * 1755))

    assert rig.quantities() == [1755] * 25
    assert result.complete and len(result.order_ids) == 25 and result.error is None
    assert 2.0 <= fake.now - 1000.0 < 3.0  # waited for the 2nd and 3rd second's slots


@pytest.mark.parametrize(
    ("config", "reason"),
    [
        ({"rate_limit_mode": "raise"}, 'rate_limit_mode="raise"'),
        ({"rate_limit_mode": "raise", "order_rate_limit_per_second": 2}, "raise"),
        ({"order_rate_limit_max_wait": 0.0}, "order_rate_limit_max_wait=0.0"),
        (
            {"order_rate_limit_max_wait": 0.5, "order_rate_limit_per_second": 2},
            "order_rate_limit_max_wait=0.5",
        ),
    ],
)
async def test_more_slices_than_the_limiter_lets_through_is_refused_before_sending(
    rig: Rig, config: dict[str, Any], reason: str
) -> None:
    from tradesmart.errors import ConfigurationError

    per_second = config.get("order_rate_limit_per_second", 10)
    quantity = (per_second + 1) * 1755  # one slice more than one second's allowance
    with pytest.raises(ConfigurationError, match=reason) as caught:
        await rig.sliced(rig.client(**config), **_order(quantity=quantity))
    assert "nothing was sent" in str(caught.value)
    assert rig.sent() == []


async def test_raise_mode_within_one_second_allowance_is_sent(rig: Rig) -> None:
    client = rig.client(rate_limit_mode="raise", order_rate_limit_per_second=3)
    _fake_time(client)
    result = await rig.sliced(client, **_order(quantity=3965))
    assert rig.quantities() == [1755, 1755, 455] and result.complete


# --- the minute window refuses a sliced order that does not fit, before sending (D6) ---------

_ITC_ORDER: dict[str, Any] = {
    "exchange": "NSE",
    "tradingsymbol": ITC,
    "quantity": 1,
    "transaction_type": "BUY",
    "product": "CNC",
    "order_type": "LIMIT",
    "price": "100.05",
}


async def _minute_rig(rig: Rig, **config: Any) -> tuple[Client, _FakeTime]:
    """A client on a fake clock that has placed 198 orders (0.2 s apart): 2 slots left."""
    client = rig.client(**config)
    fake = _FakeTime(now=0.0)
    if isinstance(client, AsyncTradeSmart):
        client._transport.use_time(fake.clock, fake.asleep)
    else:
        client._transport.use_time(fake.clock, fake.sleep)
    for i in range(198):
        fake.now = i * 0.2
        placed = client.place_order(**_ITC_ORDER)
        if inspect.isawaitable(placed):
            await placed
    return client, fake


async def test_sliced_refused_when_minute_room_short(rig: Rig) -> None:
    client, fake = await _minute_rig(rig)
    fake.now = 40.0  # the oldest order leaves the minute at 60.0: 20 s away
    with pytest.raises(RateLimitError) as caught:
        await rig.sliced(client, **_order(quantity=3965))  # 3 slices, 2 slots
    assert "client-side rate limit for place_order (200 per minute)" in str(caught.value)
    assert caught.value.retry_after == pytest.approx(20.0, abs=0.01)
    assert caught.value.retry_after > 1.0
    assert caught.value.endpoint == "PlaceOrder" and caught.value.http_status is None
    assert len(rig.sent()) == 198  # no slice was sent


async def test_sliced_waits_when_room_frees_within_max_wait(rig: Rig) -> None:
    client, fake = await _minute_rig(rig, order_rate_limit_max_wait=1.0)
    fake.now = 59.5  # the first order leaves the minute 0.5 s later
    result = await rig.sliced(client, **_order(quantity=3965))
    assert result.complete and len(result.order_ids) == 3
    assert len(rig.sent()) == 198 + 3


async def test_sliced_raise_mode_no_room(rig: Rig) -> None:
    client, fake = await _minute_rig(rig, rate_limit_mode="raise")
    fake.now = 59.5
    with pytest.raises(RateLimitError) as caught:
        await rig.sliced(client, **_order(quantity=3965))
    assert caught.value.retry_after == pytest.approx(0.5, abs=0.01)
    assert len(rig.sent()) == 198


async def test_sliced_more_than_per_minute_is_refused_as_configuration(rig: Rig) -> None:
    from tradesmart.errors import ConfigurationError

    client = rig.client(order_rate_limit_per_second=2, order_rate_limit_per_minute=2)
    with pytest.raises(
        ConfigurationError, match="order_rate_limit_per_minute=2"
    ) as caught:  # 3 > 2
        await rig.sliced(client, **_order(quantity=3965))
    assert "nothing was sent" in str(caught.value)
    assert rig.sent() == []


# --- an interruption mid-loop: noted, logged, re-raised; nothing more sent (I1) -------------


def _noted(exc: BaseException) -> BaseException:
    """``exc`` or the exception it chains (``__cause__`` / ``__context__``) that has notes."""
    seen: BaseException | None = exc
    while seen is not None:
        if getattr(seen, "__notes__", None):
            return seen
        seen = seen.__cause__ or seen.__context__
    raise AssertionError(f"no notes on {exc!r} or its chain")


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == "tradesmart.orders"
        and r.levelno == logging.WARNING
        and r.getMessage().startswith("place_sliced_order")
    ]


def test_sync_keyboard_interrupt_on_slice_two_is_noted_and_reraised(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="tradesmart")
    interruption = KeyboardInterrupt()
    with OfflineBroker() as broker:
        _instruments(broker)
        client = broker.client()
        original = client.place_order
        calls = 0

        def place_order(**kwargs: Any) -> str:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise interruption  # Ctrl-C while slice 2 is being placed
            return original(**kwargs)

        client.place_order = place_order  # type: ignore[method-assign]
        with pytest.raises(KeyboardInterrupt) as caught:
            client.place_sliced_order(**_order())
        placed = [o.id for o in broker.orders]
        client.close()

    assert caught.value is interruption  # the same exception, re-raised
    assert calls == 2 and len(placed) == 1  # nothing sent after the interruption
    notes = " ".join(interruption.__notes__)
    assert placed[0] in notes and "2210 unsent" in notes and "slice 2 of 3" in notes
    assert interruption.sliced_result.order_ids == tuple(placed)  # type: ignore[attr-defined]
    [line] = _warnings(caplog)
    assert "interrupted (KeyboardInterrupt)" in line and "1/3" in line


async def test_async_cancellation_on_slice_two_is_noted_and_reraised(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import asyncio

    caplog.set_level(logging.INFO, logger="tradesmart")
    with OfflineBroker() as broker:
        _instruments(broker)
        client = broker.async_client()
        original = client.place_order
        reached = asyncio.Event()
        calls = 0

        async def place_order(**kwargs: Any) -> str:
            nonlocal calls
            calls += 1
            if calls == 2:
                reached.set()
                await asyncio.sleep(3600)  # slice 2 in flight when the task is cancelled
            return await original(**kwargs)

        client.place_order = place_order  # type: ignore[method-assign]
        task = asyncio.create_task(client.place_sliced_order(**_order()))
        await reached.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError) as caught:
            await task
        placed = [o.id for o in broker.orders]
        await client.aclose()

    assert calls == 2 and len(placed) == 1
    noted = _noted(caught.value)
    assert isinstance(noted, asyncio.CancelledError)
    notes = " ".join(noted.__notes__)
    assert placed[0] in notes and "2210 unsent" in notes and "slice 2 of 3" in notes
    assert noted.sliced_result.order_ids == tuple(placed)  # type: ignore[attr-defined]
    [line] = _warnings(caplog)
    assert "interrupted (CancelledError)" in line and "1/3" in line


async def test_async_wait_for_timeout_on_slice_two_keeps_the_placed_ids(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import asyncio

    caplog.set_level(logging.INFO, logger="tradesmart")
    with OfflineBroker() as broker:
        _instruments(broker)
        client = broker.async_client()
        original = client.place_order
        calls = 0

        async def place_order(**kwargs: Any) -> str:
            nonlocal calls
            calls += 1
            if calls == 2:
                await asyncio.sleep(3600)  # slice 2 outlasts the caller's timeout
            return await original(**kwargs)

        client.place_order = place_order  # type: ignore[method-assign]
        with pytest.raises(TimeoutError) as caught:
            await asyncio.wait_for(client.place_sliced_order(**_order()), timeout=2.0)
        placed = [o.id for o in broker.orders]
        await client.aclose()

    assert calls == 2 and len(placed) == 1
    # wait_for raises its own TimeoutError; the cancellation that passed through
    # place_sliced_order carries the note and the partial result, chained as its cause.
    noted = _noted(caught.value)
    notes = " ".join(noted.__notes__)
    assert placed[0] in notes and "2210 unsent" in notes
    assert noted.sliced_result.order_ids == tuple(placed)  # type: ignore[attr-defined]
    [line] = _warnings(caplog)
    assert "interrupted (CancelledError)" in line


# --- summary lines: complete INFO, stopped / unknown WARNING (M3) ---------------------------


async def test_stopped_summary_line_is_a_warning(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="tradesmart")
    client = rig.client()
    _arm_on_call(rig, client, 2, drills.reject("Rejected : RMS: margin exceeded"))
    await rig.sliced(client, **_order())
    [line] = _warnings(caplog)
    assert line.startswith("place_sliced_order stopped (OrderRejectedError)") and "1/3" in line


async def test_unknown_summary_line_is_a_warning(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="tradesmart")
    client = rig.client()
    _arm_on_call(rig, client, 2, drills.timeout_after_send())
    with pytest.raises(OrderStatusUnknownError):
        await rig.sliced(client, **_order())
    [line] = _warnings(caplog)
    assert line.startswith("place_sliced_order unknown") and "1/3" in line


# --- refused before any order is sent -------------------------------------------------------


async def _refused(rig: Rig, error: type[Exception], match: str, **overrides: Any) -> None:
    with pytest.raises(error, match=match):
        await rig.sliced(rig.client(), **_order(**overrides))
    assert rig.sent() == []


async def test_unknown_tradingsymbol_is_refused(rig: Rig) -> None:
    await _refused(rig, ValueError, "NIFTY26DECFUT", tradingsymbol="NIFTY26DECFUT")
    assert rig.sent("GetSecurityInfo") == []


async def test_lot_size_mismatch_is_refused(rig: Rig) -> None:
    rig.broker.set_reply(
        "GetSecurityInfo",
        {"stat": "Ok", "exch": "NFO", "token": "35001", "ls": "75", "frzqty": "1801"},
    )
    await _refused(rig, ValueError, "lot size")


async def test_blank_freeze_quantity_without_max_slice_quantity_is_refused(rig: Rig) -> None:
    await _refused(
        rig, ValueError, "freeze quantity unknown", tradingsymbol=BANKNIFTY, quantity=150
    )


async def test_disclosed_quantity_above_the_smallest_slice_is_refused(rig: Rig) -> None:
    await _refused(rig, ValueError, "disclosed_quantity", disclosed_quantity=520)


async def test_more_slices_than_max_slices_is_refused(rig: Rig) -> None:
    await _refused(rig, ValueError, "26 slices", quantity=1755 * 26)


async def test_lower_max_slices_is_refused(rig: Rig) -> None:
    await _refused(rig, ValueError, "max_slices=3", max_slices=2)


async def test_not_whole_lots_is_refused(rig: Rig) -> None:
    await _refused(rig, ValueError, "whole number of lots", quantity=4000)


@pytest.mark.parametrize("quantity", [0, -65, True, 65.0, "130"])
async def test_bad_quantity_is_refused_before_any_request(rig: Rig, quantity: Any) -> None:
    await _refused(rig, ValueError, "quantity", quantity=quantity)
    assert rig.broker.requests == []


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("max_slices", 0),
        ("max_slices", True),
        ("max_slice_quantity", 0),
        ("max_slice_quantity", 1.5),
    ],
)
async def test_bad_limits_are_refused_before_any_request(rig: Rig, name: str, value: Any) -> None:
    await _refused(rig, ValueError, name, **{name: value})
    assert rig.broker.requests == []


async def test_place_order_validation_runs_once_before_sending(rig: Rig) -> None:
    await _refused(rig, RequestValidationError, "trantype", transaction_type="HOLD")
