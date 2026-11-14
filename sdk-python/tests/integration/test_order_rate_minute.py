"""Per-minute order window (F33): 200 PlaceOrder calls per rolling 60 s by default.

Runs the real clients against the offline broker on a fake clock (``use_time``), sync and async.
"""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import pytest

from tradesmart import AsyncTradeSmart, TradeSmart
from tradesmart.errors import RateLimitError
from tradesmart.testing import OfflineBroker

Client = TradeSmart | AsyncTradeSmart

_PLACE: dict[str, Any] = {
    "exchange": "NSE",
    "tradingsymbol": "ITC-EQ",
    "quantity": 1,
    "transaction_type": "BUY",
    "product": "CNC",
    "order_type": "LIMIT",
    "price": "100.05",
}
_MODIFY: dict[str, Any] = {
    "exchange": "NSE",
    "tradingsymbol": "ITC-EQ",
    "quantity": 1,
    "price": "100.10",
    "order_type": "LIMIT",
    "product": "CNC",
}


@dataclass
class Rig:
    broker: OfflineBroker
    kind: str
    now: float = 0.0
    sleeps: list[float] = field(default_factory=list)

    def client(self, **overrides: Any) -> Client:
        if self.kind == "sync":
            sync = self.broker.client(**overrides)

            def sleep(seconds: float) -> None:
                self.sleeps.append(seconds)
                self.now += seconds

            sync._transport.use_time(lambda: self.now, sleep)
            return sync
        asynchronous = self.broker.async_client(**overrides)

        async def asleep(seconds: float) -> None:
            self.sleeps.append(seconds)
            self.now += seconds

        asynchronous._transport.use_time(lambda: self.now, asleep)
        return asynchronous

    async def run(self, client: Client, method: str, *args: Any, **kwargs: Any) -> Any:
        result = getattr(client, method)(*args, **kwargs)
        if inspect.isawaitable(result):
            result = await result
        return result

    def sent(self, endpoint: str) -> int:
        return sum(1 for r in self.broker.requests if r.endpoint == endpoint)

    async def place_200(self, client: Client) -> str:
        order_id = ""
        for i in range(200):
            self.now = i * 0.2
            order_id = await self.run(client, "place_order", **_PLACE)
        return order_id


@pytest.fixture(params=["sync", "async"])
async def rig(request: pytest.FixtureRequest) -> AsyncIterator[Rig]:
    with OfflineBroker() as broker:
        broker.add_instrument("NSE", "1660", "ITC-EQ", lot_size=1, frzqty=350854)
        yield Rig(broker, request.param)


async def test_201st_place_in_a_minute_raises_rate_limit_error(rig: Rig) -> None:
    client = rig.client()
    await rig.place_200(client)
    rig.now = 40.0
    with pytest.raises(RateLimitError) as caught:
        await rig.run(client, "place_order", **_PLACE)
    assert "client-side rate limit for place_order (200 per minute)" in str(caught.value)
    assert caught.value.retry_after == pytest.approx(20.0, abs=0.01)
    assert caught.value.http_status is None
    assert rig.sent("PlaceOrder") == 200


async def test_raise_mode_minute_window(rig: Rig) -> None:
    client = rig.client(rate_limit_mode="raise")
    await rig.place_200(client)
    rig.now = 40.0
    with pytest.raises(RateLimitError) as caught:
        await rig.run(client, "place_order", **_PLACE)
    assert "client-side rate limit for place_order (200 per minute)" in str(caught.value)
    assert caught.value.retry_after == pytest.approx(20.0, abs=0.01)
    assert caught.value.http_status is None
    assert rig.sent("PlaceOrder") == 200


async def test_cancel_and_modify_not_limited_after_200_places(rig: Rig) -> None:
    client = rig.client()
    order_id = await rig.place_200(client)
    rig.now = 40.0
    rig.sleeps.clear()
    await rig.run(client, "modify_order", order_id, **_MODIFY)
    await rig.run(client, "cancel_order", order_id)
    assert rig.sent("ModifyOrder") == 1 and rig.sent("CancelOrder") == 1
    assert rig.sleeps == []


async def test_custom_per_minute_600(rig: Rig) -> None:
    client = rig.client(order_rate_limit_per_minute=600)
    await rig.place_200(client)
    rig.now = 40.0
    await rig.run(client, "place_order", **_PLACE)
    assert rig.sent("PlaceOrder") == 201


async def test_second_window_refusal_names_the_second_window(rig: Rig) -> None:
    client = rig.client(rate_limit_mode="raise")
    rig.now = 0.0
    for _ in range(10):
        await rig.run(client, "place_order", **_PLACE)
    with pytest.raises(RateLimitError) as caught:
        await rig.run(client, "place_order", **_PLACE)
    assert str(caught.value).startswith("client-side rate limit for place_order")
    assert "(10 per second)" in str(caught.value)
    assert "(200 per minute)" not in str(caught.value)
    assert rig.sent("PlaceOrder") == 10
