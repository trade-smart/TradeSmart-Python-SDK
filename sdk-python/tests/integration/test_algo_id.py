"""``algo_id`` on ``PlaceOrder`` / ``ModifyOrder`` end to end on the offline broker (F29).

Docs (PlaceOrder and ModifyOrder): ``algo_id`` is an optional String, "Required when placing
more than 10 orders per second". The SDK sends it, after the documented fields, when
``ClientConfig.algo_id`` or the per-call ``algo_id=`` is set (the per-call value wins), and
never on any other endpoint. Every case runs on ``TradeSmart`` and ``AsyncTradeSmart``.
"""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import pytest

from tradesmart import AsyncTradeSmart, SlicedOrderResult, TradeSmart
from tradesmart.errors import RequestValidationError
from tradesmart.testing import OfflineBroker

NIFTY = "NIFTY26NOVFUT"
EXPIRY = date.today() + timedelta(days=14)

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
_SLICED: dict[str, Any] = {
    "exchange": "NFO",
    "tradingsymbol": NIFTY,
    "quantity": 3965,  # 3 slices: 1755 + 1755 + 455
    "transaction_type": "BUY",
    "product": "M",
    "order_type": "LMT",
    "price": "24000.50",
}


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

    async def run(self, client: Client, method: str, *args: Any, **kwargs: Any) -> Any:
        result = getattr(client, method)(*args, **kwargs)
        if inspect.isawaitable(result):
            result = await result
        return result

    def sent(self, endpoint: str) -> list[dict[str, Any]]:
        return [r.payload for r in self.broker.requests if r.endpoint == endpoint]


@pytest.fixture(params=["sync", "async"])
async def rig(request: pytest.FixtureRequest) -> AsyncIterator[Rig]:
    with OfflineBroker() as broker:
        broker.add_instrument(
            "NFO", "35001", NIFTY, lot_size=65, instrument="FUTIDX", expiry=EXPIRY,
            underlying="NIFTY", frzqty=1801,
        )  # fmt: skip
        broker.add_instrument("NSE", "1660", "ITC-EQ", lot_size=1, frzqty=350854)
        built = Rig(broker, request.param)
        try:
            yield built
        finally:
            for client in built.clients:
                if isinstance(client, AsyncTradeSmart):
                    await client.aclose()
                else:
                    client.close()


def _algo_last(payload: dict[str, Any], value: str) -> None:
    """``algo_id`` is sent once, with ``value``, after every documented field."""
    keys = list(payload)
    assert keys.count("algo_id") == 1
    assert keys[-1] == "algo_id"
    assert payload["algo_id"] == value


# --- place_order -------------------------------------------------------------------------


async def test_place_order_without_algo_id_sends_no_algo_id(rig: Rig) -> None:
    await rig.run(rig.client(), "place_order", **_PLACE)
    (payload,) = rig.sent("PlaceOrder")
    assert "algo_id" not in payload
    assert list(payload)[-1] == "ordersource"  # the payload is unchanged


async def test_place_order_sends_the_config_algo_id(rig: Rig) -> None:
    await rig.run(rig.client(algo_id="153763"), "place_order", **_PLACE)
    (payload,) = rig.sent("PlaceOrder")
    _algo_last(payload, "153763")


async def test_place_order_per_call_algo_id_overrides_the_config(rig: Rig) -> None:
    await rig.run(rig.client(algo_id="153763"), "place_order", **_PLACE, algo_id="99999")
    (payload,) = rig.sent("PlaceOrder")
    _algo_last(payload, "99999")


async def test_place_order_per_call_algo_id_without_config(rig: Rig) -> None:
    await rig.run(rig.client(), "place_order", **_PLACE, algo_id="99999")
    (payload,) = rig.sent("PlaceOrder")
    _algo_last(payload, "99999")


async def test_place_order_blank_per_call_algo_id_is_refused_before_sending(rig: Rig) -> None:
    with pytest.raises(RequestValidationError, match="algo_id"):
        await rig.run(rig.client(algo_id="153763"), "place_order", **_PLACE, algo_id=" ")
    assert rig.sent("PlaceOrder") == []


# --- modify_order ------------------------------------------------------------------------


def _modify(order_id: str) -> dict[str, Any]:
    return {
        "exchange": "NSE",
        "tradingsymbol": "ITC-EQ",
        "quantity": 1,
        "price": "100.10",
        "order_type": "LIMIT",
        "product": "CNC",
        "order_id": order_id,
    }


async def test_modify_order_without_algo_id_sends_no_algo_id(rig: Rig) -> None:
    client = rig.client()
    order_id = await rig.run(client, "place_order", **_PLACE)
    await rig.run(client, "modify_order", **_modify(order_id))
    (payload,) = rig.sent("ModifyOrder")
    assert "algo_id" not in payload


async def test_modify_order_sends_the_config_algo_id(rig: Rig) -> None:
    client = rig.client(algo_id="153763")
    order_id = await rig.run(client, "place_order", **_PLACE)
    await rig.run(client, "modify_order", **_modify(order_id))
    (payload,) = rig.sent("ModifyOrder")
    _algo_last(payload, "153763")


async def test_modify_order_per_call_algo_id_overrides_the_config(rig: Rig) -> None:
    client = rig.client(algo_id="153763")
    order_id = await rig.run(client, "place_order", **_PLACE)
    await rig.run(client, "modify_order", **_modify(order_id), algo_id="99999")
    (payload,) = rig.sent("ModifyOrder")
    _algo_last(payload, "99999")


# --- place_sliced_order ------------------------------------------------------------------


async def test_every_slice_carries_the_config_algo_id(rig: Rig) -> None:
    result = await rig.run(rig.client(algo_id="153763"), "place_sliced_order", **_SLICED)
    assert isinstance(result, SlicedOrderResult) and result.complete
    payloads = rig.sent("PlaceOrder")
    assert len(payloads) == 3
    for payload in payloads:
        _algo_last(payload, "153763")


async def test_every_slice_carries_the_per_call_algo_id(rig: Rig) -> None:
    client = rig.client(algo_id="153763")
    result = await rig.run(client, "place_sliced_order", **_SLICED, algo_id="99999")
    assert isinstance(result, SlicedOrderResult) and result.complete
    payloads = rig.sent("PlaceOrder")
    assert len(payloads) == 3
    for payload in payloads:
        _algo_last(payload, "99999")


async def test_slices_without_algo_id_send_none(rig: Rig) -> None:
    await rig.run(rig.client(), "place_sliced_order", **_SLICED)
    payloads = rig.sent("PlaceOrder")
    assert len(payloads) == 3 and all("algo_id" not in p for p in payloads)


async def test_sliced_order_blank_algo_id_is_refused_before_sending(rig: Rig) -> None:
    with pytest.raises(RequestValidationError, match="algo_id"):
        await rig.run(rig.client(), "place_sliced_order", **_SLICED, algo_id="")
    assert rig.sent("PlaceOrder") == []


# --- other endpoints ---------------------------------------------------------------------


async def test_algo_id_is_not_sent_on_any_other_endpoint(rig: Rig) -> None:
    client = rig.client(algo_id="153763")
    order_id = await rig.run(client, "place_order", **_PLACE)
    await rig.run(client, "orders")
    await rig.run(client, "order_history", order_id)
    await rig.run(client, "trades")
    await rig.run(client, "cancel_order", order_id)
    await rig.run(client, "margins")
    await rig.run(client, "security_info", "NSE", "1660")
    await rig.run(client, "request", "OrderBook", {}, state_changing=False, expect="list")
    others = [r for r in rig.broker.requests if r.endpoint not in ("PlaceOrder", "ModifyOrder")]
    assert {r.endpoint for r in others} >= {
        "OrderBook",
        "SingleOrdHist",
        "TradeBook",
        "CancelOrder",
        "Limits",
        "GetSecurityInfo",
    }
    assert all("algo_id" not in r.payload for r in others)


async def test_request_sends_the_payload_as_given(rig: Rig) -> None:
    # The low-level escape hatch adds nothing: the config algo_id is not injected.
    client = rig.client(algo_id="153763")
    payload = {
        "exch": "NSE", "tsym": "ITC-EQ", "qty": "1", "prc": "100.05", "prd": "C",
        "trantype": "B", "prctyp": "LMT", "ret": "DAY",
    }  # fmt: skip
    await rig.run(client, "request", "PlaceOrder", payload, state_changing=True)
    (sent,) = rig.sent("PlaceOrder")
    assert "algo_id" not in sent
