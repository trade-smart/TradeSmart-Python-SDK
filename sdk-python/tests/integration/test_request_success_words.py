"""``request()`` to the GTT and iceberg write endpoints: canonical name, sent once, exact words."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx

from tests.conftest import BASE
from tradesmart import (
    AsyncTradeSmart,
    NetworkError,
    OrderRejectedError,
    OrderStatusUnknownError,
    TradeSmart,
)

GTT_ACK = {"request_time": "10:00:00 12-11-2026", "stat": "Oi created", "al_id": "26111200000010"}
ICEBERG_ACK = {"request_time": "10:00:00 12-11-2026", "stat": "true", "aq_id": "26111200000020"}


@pytest.mark.parametrize("name", ["placegttorder", "PLACEGTTORDER", " /PlaceGttOrder/ "])
@respx.mock
def test_request_place_gtt_any_case_honours_the_word(client: TradeSmart, name: str) -> None:
    route = respx.post(f"{BASE}/PlaceGTTOrder").respond(200, json=GTT_ACK)
    assert client.request(name, {"exch": "NSE"}, state_changing=False) == GTT_ACK
    assert route.call_count == 1


@respx.mock
def test_request_place_gtt_ok_is_unknown(client: TradeSmart) -> None:
    respx.post(f"{BASE}/PlaceGTTOrder").respond(200, json={"stat": "Ok", "al_id": "1"})
    with pytest.raises(OrderStatusUnknownError):
        client.request("placegttorder", {}, state_changing=False)


@respx.mock
def test_request_cancel_gtt_without_al_id_is_unknown(client: TradeSmart) -> None:
    respx.post(f"{BASE}/CancelGTTOrder").respond(200, json={"stat": "Oi delete success"})
    with pytest.raises(OrderStatusUnknownError):
        client.request("cancelgttorder", {"al_id": "1"}, state_changing=False)


@pytest.mark.parametrize(
    ("endpoint", "stat"),
    [
        ("PlaceGTTOrder", "OI created"),
        ("PlaceGTTOrder", "oi CREATED"),
        ("ModifyGTTOrder", "OI REPLACED"),
        ("CancelGTTOrder", "oi delete SUCCESS"),
    ],
)
@respx.mock
def test_request_gtt_word_ignores_letter_case(client: TradeSmart, endpoint: str, stat: str) -> None:
    ack = {"stat": stat, "al_id": "26111300000001"}
    route = respx.post(f"{BASE}/{endpoint}").respond(200, json=ack)
    assert client.request(endpoint.lower(), {}, state_changing=False) == ack
    assert route.call_count == 1


@pytest.mark.parametrize(
    ("endpoint", "stat"),
    [
        ("CancelGTTOrder", "OI deleted"),
        ("PlaceGTTOrder", "Oi  created"),
        ("PlaceGTTOrder", " Oi created"),
    ],
)
@respx.mock
def test_request_gtt_other_words_and_spacing_stay_unknown(
    client: TradeSmart, endpoint: str, stat: str
) -> None:
    route = respx.post(f"{BASE}/{endpoint}").respond(200, json={"stat": stat, "al_id": "1"})
    with pytest.raises(OrderStatusUnknownError):
        client.request(endpoint, {}, state_changing=False)
    assert route.call_count == 1


@respx.mock
async def test_async_request_gtt_word_ignores_letter_case(async_client: AsyncTradeSmart) -> None:
    ack = {"stat": "OI created", "al_id": "26111300000001"}
    respx.post(f"{BASE}/PlaceGTTOrder").respond(200, json=ack)
    assert await async_client.request("PlaceGTTOrder", {}, state_changing=False) == ack
    respx.post(f"{BASE}/CancelGTTOrder").respond(200, json={"stat": "OI deleted", "al_id": "1"})
    with pytest.raises(OrderStatusUnknownError):
        await async_client.request("CancelGTTOrder", {}, state_changing=False)
    await async_client.aclose()


@respx.mock
def test_request_iceberg_word_stays_case_sensitive(client: TradeSmart) -> None:
    respx.post(f"{BASE}/PlaceIceBergOrder").respond(200, json={"stat": "TRUE", "aq_id": "1"})
    with pytest.raises(OrderStatusUnknownError):
        client.request("PlaceIceBergOrder", {}, state_changing=False)


@respx.mock
def test_request_modify_gtt_word(client: TradeSmart) -> None:
    ack = {"stat": "Oi replaced", "al_id": "1"}
    respx.post(f"{BASE}/ModifyGTTOrder").respond(200, json=ack)
    assert client.request("ModifyGTTOrder", {"al_id": "1"}, state_changing=False) == ack


@pytest.mark.parametrize("name", ["PLACEICEBERGORDER", "modifyicebergorder", "CancelIcebergOrder"])
@respx.mock
def test_request_iceberg_true_is_success(client: TradeSmart, name: str) -> None:
    canonical = {
        "placeicebergorder": "PlaceIceBergOrder",
        "modifyicebergorder": "ModifyIceBergOrder",
        "cancelicebergorder": "CancelIceBergOrder",
    }[name.lower()]
    route = respx.post(f"{BASE}/{canonical}").respond(200, json=ICEBERG_ACK)
    assert client.request(name, {}, state_changing=False) == ICEBERG_ACK
    assert route.call_count == 1


@respx.mock
def test_request_iceberg_false_is_rejected(client: TradeSmart) -> None:
    respx.post(f"{BASE}/PlaceIceBergOrder").respond(
        200, json={"stat": "false", "rejreason": "Insufficient margin"}
    )
    with pytest.raises(OrderRejectedError) as info:
        client.request("PLACEICEBERGORDER", {}, state_changing=False)
    assert "Insufficient margin" in str(info.value)


@pytest.mark.parametrize(
    "endpoint",
    [
        "PlaceGTTOrder",
        "ModifyGTTOrder",
        "CancelGTTOrder",
        "PlaceIceBergOrder",
        "ModifyIceBergOrder",
        "CancelIceBergOrder",
    ],
)
@pytest.mark.parametrize(
    "reply",
    [
        {"side_effect": httpx.ReadTimeout("slow")},
        {"side_effect": httpx.RemoteProtocolError("reset")},
        {"return_value": httpx.Response(503, text="down")},
        {"return_value": httpx.Response(500, json={"hello": "world"})},
    ],
    ids=["read_timeout", "reset", "http_503_text", "http_500_unexpected_json"],
)
@respx.mock
def test_request_writes_are_sent_once(
    client: TradeSmart, endpoint: str, reply: dict[str, Any]
) -> None:
    route = respx.post(f"{BASE}/{endpoint}").mock(**reply)
    with pytest.raises(OrderStatusUnknownError):
        client.request(endpoint.lower(), {}, state_changing=False)
    assert route.call_count == 1


@respx.mock
def test_request_connect_error_on_gtt_is_definitive_and_not_retried(client: TradeSmart) -> None:
    route = respx.post(f"{BASE}/PlaceGTTOrder").mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(NetworkError):
        client.request("placegttorder", {}, state_changing=False)
    assert route.call_count == 1


@respx.mock
async def test_async_request_place_gtt_honours_the_word(async_client: AsyncTradeSmart) -> None:
    route = respx.post(f"{BASE}/PlaceGTTOrder").respond(200, json=GTT_ACK)
    assert await async_client.request("placegttorder", {}, state_changing=False) == GTT_ACK
    assert route.call_count == 1
    await async_client.aclose()


@respx.mock
async def test_async_request_iceberg_words(async_client: AsyncTradeSmart) -> None:
    respx.post(f"{BASE}/PlaceIceBergOrder").respond(200, json=ICEBERG_ACK)
    assert await async_client.request("PLACEICEBERGORDER", {}, state_changing=False) == ICEBERG_ACK
    respx.post(f"{BASE}/CancelIceBergOrder").respond(200, json={"stat": "True"})
    with pytest.raises(OrderStatusUnknownError):
        await async_client.request("cancelicebergorder", {}, state_changing=False)
    await async_client.aclose()


@respx.mock
async def test_async_request_gtt_sent_once_on_timeout(async_client: AsyncTradeSmart) -> None:
    route = respx.post(f"{BASE}/ModifyGTTOrder").mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(OrderStatusUnknownError):
        await async_client.request("modifygttorder", {}, state_changing=False)
    assert route.call_count == 1
    await async_client.aclose()


WRITES = (
    "PlaceGTTOrder",
    "ModifyGTTOrder",
    "CancelGTTOrder",
    "PlaceIceBergOrder",
    "ModifyIceBergOrder",
    "CancelIceBergOrder",
)
LIST_BODIES = ([], [{"stat": "Oi created", "al_id": "26111200000010"}], [{"stat": "true"}])


@pytest.mark.parametrize("endpoint", WRITES)
@pytest.mark.parametrize("body", LIST_BODIES, ids=["empty", "gtt_ack_in_list", "true_in_list"])
@respx.mock
def test_request_list_body_on_a_write_is_unknown_even_with_expect_list(
    client: TradeSmart, endpoint: str, body: list[Any]
) -> None:
    route = respx.post(f"{BASE}/{endpoint}").respond(200, json=body)
    with pytest.raises(OrderStatusUnknownError):
        client.request(endpoint.lower(), {}, state_changing=False, expect="list")
    assert route.call_count == 1


@pytest.mark.parametrize("endpoint", WRITES)
@pytest.mark.parametrize("body", LIST_BODIES, ids=["empty", "gtt_ack_in_list", "true_in_list"])
@respx.mock
async def test_async_request_list_body_on_a_write_is_unknown_even_with_expect_list(
    async_client: AsyncTradeSmart, endpoint: str, body: list[Any]
) -> None:
    route = respx.post(f"{BASE}/{endpoint}").respond(200, json=body)
    with pytest.raises(OrderStatusUnknownError):
        await async_client.request(endpoint.upper(), {}, state_changing=True, expect="list")
    assert route.call_count == 1
    await async_client.aclose()


@respx.mock
def test_request_place_multi_order_list_is_still_returned(client: TradeSmart) -> None:
    respx.post(f"{BASE}/PlaceMultiOrder").respond(200, json=[])
    assert client.request("placemultiorder", {}, state_changing=False, expect="list") == []
