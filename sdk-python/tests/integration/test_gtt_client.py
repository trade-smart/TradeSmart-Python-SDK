"""The six GTT methods on ``TradeSmart`` and ``AsyncTradeSmart``, end to end on the offline broker.

Spec: ``specifications/sdk-behaviour.md`` 2.8; docs
``03-gtt-orders.md``. Every case runs on both clients.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import pytest

from tradesmart import (
    AsyncTradeSmart,
    Gtt,
    GttAlertType,
    GttValidity,
    OrderRejectedError,
    OrderStatusUnknownError,
    RequestValidationError,
    TradeSmart,
    TriggeredGtt,
    Validity,
)
from tradesmart.testing import OfflineBroker, drills

Client = TradeSmart | AsyncTradeSmart

GTT_METHODS = (
    "place_gtt",
    "modify_gtt",
    "cancel_gtt",
    "pending_gtts",
    "triggered_gtts",
    "gtt_alert_types",
)

_PLACE: dict[str, Any] = {
    "exchange": "NSE",
    "tradingsymbol": "ITC-EQ",
    "alert_type": GttAlertType.LTP_ABOVE,
    "trigger_value": "1500",
    "quantity": 1,
    "transaction_type": "BUY",
    "product": "CNC",
    "order_type": "LIMIT",
    "price": "100.05",
    "tag": "sdk-test-gtt",
}

# modify_gtt has no defaults for validity and alert_validity (they are sent on every modify).
_MODIFY: dict[str, Any] = {**_PLACE, "validity": "DAY", "alert_validity": "GTT"}

NO_DATA = {"stat": "Not_Ok", "emsg": 'Error Occurred : 5 "no data"'}


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

    def sent(self, endpoint: str | None = None) -> list[dict[str, Any]]:
        return [r.payload for r in self.broker.requests if endpoint in (None, r.endpoint)]


@pytest.fixture(params=["sync", "async"])
async def rig(request: pytest.FixtureRequest) -> AsyncIterator[Rig]:
    with OfflineBroker() as broker:
        broker.add_instrument("NSE", "1660", "ITC-EQ", lot_size=1)
        built = Rig(broker, request.param)
        try:
            yield built
        finally:
            for client in built.clients:
                if isinstance(client, AsyncTradeSmart):
                    await client.aclose()
                else:
                    client.close()


# --- surface -----------------------------------------------------------------------------------


@pytest.mark.parametrize("method", GTT_METHODS)
def test_same_signature_on_both_clients(method: str) -> None:
    sync_method = getattr(TradeSmart, method)
    async_method = getattr(AsyncTradeSmart, method)
    assert inspect.signature(sync_method) == inspect.signature(async_method)
    assert inspect.iscoroutinefunction(async_method)
    assert not inspect.iscoroutinefunction(sync_method)


def test_place_gtt_signature() -> None:
    parameters = inspect.signature(TradeSmart.place_gtt).parameters
    assert list(parameters) == [
        "self",
        "exchange",
        "tradingsymbol",
        "alert_type",
        "trigger_value",
        "quantity",
        "transaction_type",
        "product",
        "order_type",
        "tag",
        "price",
        "trigger_price",
        "validity",
        "alert_validity",
        "disclosed_quantity",
    ]
    assert all(
        p.kind is inspect.Parameter.KEYWORD_ONLY for name, p in parameters.items() if name != "self"
    )
    assert parameters["validity"].default is Validity.DAY
    assert parameters["alert_validity"].default is GttValidity.GTT
    assert parameters["tag"].default is inspect.Parameter.empty
    modify = inspect.signature(TradeSmart.modify_gtt).parameters
    assert list(modify)[:2] == ["self", "alert_id"]
    assert list(modify)[2:] == list(parameters)[1:]
    # Review fix: modify_gtt sends both validities on every call, so it has no default for them.
    assert modify["validity"].default is inspect.Parameter.empty
    assert modify["alert_validity"].default is inspect.Parameter.empty
    assert modify["validity"].kind is inspect.Parameter.KEYWORD_ONLY
    assert modify["alert_validity"].kind is inspect.Parameter.KEYWORD_ONLY


# --- lifecycle on the offline broker -------------------------------------------------------


async def test_place_read_modify_cancel(rig: Rig) -> None:
    client = rig.client()
    alert_id = await rig.run(client, "place_gtt", **_PLACE)
    assert isinstance(alert_id, str) and alert_id.strip()
    (payload,) = rig.sent("PlaceGTTOrder")
    assert payload["ai_t"] == "LTP_A_O" and payload["validity"] == "GTT" and payload["ret"] == "DAY"

    (gtt,) = await rig.run(client, "pending_gtts")
    assert isinstance(gtt, Gtt)
    assert gtt.alert_id == alert_id
    assert gtt.tag == "sdk-test-gtt"
    assert gtt.alert_type is GttAlertType.LTP_ABOVE
    assert gtt.trigger_value == Decimal("1500")
    assert gtt.quantity == 1

    modified = await rig.run(client, "modify_gtt", alert_id, **{**_MODIFY, "trigger_value": "1550"})
    assert modified == alert_id
    (payload,) = rig.sent("ModifyGTTOrder")
    assert list(payload)[:3] == ["uid", "actid", "al_id"] and payload["al_id"] == alert_id
    (gtt,) = await rig.run(client, "pending_gtts")
    assert gtt.trigger_value == Decimal("1550")

    assert await rig.run(client, "cancel_gtt", alert_id) == alert_id
    assert rig.sent("CancelGTTOrder") == [{"uid": rig.broker.client_id, "al_id": alert_id}]
    assert await rig.run(client, "pending_gtts") == []
    assert await rig.run(client, "triggered_gtts") == []


async def test_alert_ids_are_unique(rig: Rig) -> None:
    client = rig.client()
    first = await rig.run(client, "place_gtt", **_PLACE)
    second = await rig.run(client, "place_gtt", **_PLACE)
    assert first != second
    assert [g.alert_id for g in await rig.run(client, "pending_gtts")] == [first, second]


async def test_trigger_drill_places_the_order_with_parent_id(rig: Rig) -> None:
    client = rig.client()
    alert_id = await rig.run(client, "place_gtt", **_PLACE)
    assert rig.broker.orders == []  # a GTT never fires on its own in the offline broker
    order_id = rig.broker.trigger_gtt(alert_id)
    assert await rig.run(client, "pending_gtts") == []
    (triggered,) = await rig.run(client, "triggered_gtts")
    assert isinstance(triggered, TriggeredGtt)
    assert triggered.alert_id == alert_id and triggered.norentm is not None
    (order,) = await rig.run(client, "orders")
    assert order.order_id == order_id
    assert order.parent_id == alert_id
    assert order.tag == "sdk-test-gtt"
    assert (order.quantity, order.price) == (1, Decimal("100.05"))
    assert rig.sent("PlaceOrder") == []  # the server places it, not the client


async def test_trigger_drill_needs_a_pending_gtt(rig: Rig) -> None:
    with pytest.raises(ValueError, match="pending"):
        rig.broker.trigger_gtt("26111299999999")


async def test_modify_or_cancel_an_unknown_gtt_is_rejected(rig: Rig) -> None:
    client = rig.client()
    with pytest.raises(OrderRejectedError):
        await rig.run(client, "modify_gtt", "26111299999999", **_MODIFY)
    with pytest.raises(OrderRejectedError):
        await rig.run(client, "cancel_gtt", "26111299999999")


async def test_cancel_a_triggered_gtt_is_rejected(rig: Rig) -> None:
    client = rig.client()
    alert_id = await rig.run(client, "place_gtt", **_PLACE)
    rig.broker.trigger_gtt(alert_id)
    with pytest.raises(OrderRejectedError):
        await rig.run(client, "cancel_gtt", alert_id)


async def test_gtt_alert_types_documented_example(rig: Rig) -> None:
    assert await rig.run(rig.client(), "gtt_alert_types") == ["ATP", "LTP"]
    assert rig.sent("GetEnabledGTTs") == [{"uid": rig.broker.client_id}]


async def test_read_payloads(rig: Rig) -> None:
    client = rig.client()
    await rig.run(client, "pending_gtts")
    await rig.run(client, "triggered_gtts")
    uid = rig.broker.client_id
    assert rig.sent("GetPendingGTTOrder") == [{"uid": uid}]
    assert rig.sent("GetTrgOi") == [{"uid": uid, "actid": uid}]


# --- empty lists --------------------------------------------------------------------------------


@pytest.mark.parametrize("body", [[], NO_DATA])
@pytest.mark.parametrize(
    ("method", "endpoint"), [("pending_gtts", "GetPendingGTTOrder"), ("triggered_gtts", "GetTrgOi")]
)
async def test_empty_list_shapes(rig: Rig, body: Any, method: str, endpoint: str) -> None:
    rig.broker.set_reply(endpoint, body)
    assert await rig.run(rig.client(), method) == []


async def test_gtt_alert_types_no_data_is_empty(rig: Rig) -> None:
    rig.broker.set_reply("GetEnabledGTTs", NO_DATA)
    assert await rig.run(rig.client(), "gtt_alert_types") == []


async def test_pending_documented_row_with_numeric_qty(rig: Rig) -> None:
    row = {
        "stat": "Ok",
        "ai_t": "LTP_B_O",
        "al_id": "26111200000010",
        "tsym": "INFY-EQ",
        "exch": "NSE",
        "token": "1594",
        "remarks": "Buy INFY below 1500",
        "validity": "GTT",
        "pp": "2",
        "ls": "1",
        "actid": "TEST123",
        "trantype": "B",
        "prctyp": "LMT",
        "qty": 1,
        "prc": "1500.00",
        "prd": "C",
        "s_prdt_ali": "CNC",
        "ordersource": "API",
        "d": "1500.00",
    }  # no "ret": validity stays None (Review Focus 3)
    rig.broker.set_reply("GetPendingGTTOrder", [row])
    (gtt,) = await rig.run(rig.client(), "pending_gtts")
    assert gtt.quantity == 1
    assert gtt.validity is None
    assert gtt.alert_validity is GttValidity.GTT


# --- refusals send nothing ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "changes",
    [
        {"alert_type": "LTP_X_O"},
        {"alert_validity": "EOS"},
        {"exchange": "CDS"},
        {"exchange": "BCD"},
        {"product": "MIS"},
        {"product": "BO"},
        {"tag": "  "},
        {"trigger_value": True},
        {"trigger_value": float("nan")},
        {"price": None},
        {"order_type": "SL-LMT"},
        {"trigger_price": "99"},
        {"quantity": 0},
        {"quantity": 2, "disclosed_quantity": 3},
    ],
)
async def test_place_refusals_send_nothing(rig: Rig, changes: dict[str, Any]) -> None:
    client = rig.client()
    with pytest.raises(RequestValidationError):
        await rig.run(client, "place_gtt", **{**_PLACE, **changes})
    with pytest.raises(RequestValidationError):
        await rig.run(client, "modify_gtt", "26111200000010", **{**_MODIFY, **changes})
    assert rig.sent() == []


@pytest.mark.parametrize("alert_id", [26111200000010, "", "   "])
async def test_alert_id_refusals_send_nothing(rig: Rig, alert_id: Any) -> None:
    client = rig.client()
    with pytest.raises(RequestValidationError):
        await rig.run(client, "cancel_gtt", alert_id)
    with pytest.raises(RequestValidationError):
        await rig.run(client, "modify_gtt", alert_id, **_MODIFY)
    assert rig.sent() == []


async def test_mcx_quantity_is_sent_in_trade_units(rig: Rig) -> None:
    # MCX quantity is in trade units, a whole multiple of the lot size (CRUDEOIL lot 100: 2 lots).
    await rig.run(
        rig.client(),
        "place_gtt",
        **{**_PLACE, "exchange": "MCX", "tradingsymbol": "CRUDEOIL25DECFUT", "quantity": 200},
    )
    (payload,) = rig.sent("PlaceGTTOrder")
    assert payload["qty"] == "200"


async def test_config_algo_id_is_never_sent_on_gtt(rig: Rig) -> None:
    client = rig.client(algo_id="153763")
    alert_id = await rig.run(client, "place_gtt", **_PLACE)
    await rig.run(client, "modify_gtt", alert_id, **_MODIFY)
    await rig.run(client, "cancel_gtt", alert_id)
    for endpoint in ("PlaceGTTOrder", "ModifyGTTOrder", "CancelGTTOrder"):
        (payload,) = rig.sent(endpoint)
        assert "algo_id" not in payload and "ordersource" not in payload


# --- outcomes ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "endpoint", "stat"),
    [
        ("place_gtt", "PlaceGTTOrder", "OI created"),  # the UAT answer (spec F32)
        ("place_gtt", "PlaceGTTOrder", "oi CREATED"),
        ("modify_gtt", "ModifyGTTOrder", "OI replaced"),
        ("cancel_gtt", "CancelGTTOrder", "OI DELETE SUCCESS"),
    ],
)
async def test_word_in_another_letter_case_is_success_and_sent_once(
    rig: Rig, method: str, endpoint: str, stat: str
) -> None:
    rig.broker.set_reply(endpoint, {"stat": stat, "al_id": "26111300000001"})
    arguments = {"place_gtt": _PLACE, "modify_gtt": _MODIFY, "cancel_gtt": {}}[method]
    positional = () if method == "place_gtt" else ("26111300000001",)
    assert await rig.run(rig.client(), method, *positional, **arguments) == "26111300000001"
    assert len(rig.sent(endpoint)) == 1


async def test_uat_cancel_word_oi_deleted_is_unknown_and_sent_once(rig: Rig) -> None:
    # UAT answers "OI deleted": a different word from "Oi delete success", not just case.
    rig.broker.set_reply("CancelGTTOrder", {"stat": "OI deleted", "al_id": "26111300000001"})
    with pytest.raises(OrderStatusUnknownError, match=r"pending_gtts\(\)"):
        await rig.run(rig.client(), "cancel_gtt", "26111300000001")
    assert len(rig.sent("CancelGTTOrder")) == 1


@pytest.mark.parametrize("stat", ["Oi  created", "Oi created ", "Ok", " Oi created", "OI created."])
async def test_wrong_word_is_unknown_and_sent_once(rig: Rig, stat: str) -> None:
    rig.broker.set_reply("PlaceGTTOrder", {"stat": stat, "al_id": "26111200000010"})
    with pytest.raises(OrderStatusUnknownError, match=r"pending_gtts\(\)"):
        await rig.run(rig.client(), "place_gtt", **_PLACE)
    assert len(rig.sent("PlaceGTTOrder")) == 1


@pytest.mark.parametrize(
    ("method", "endpoint", "stat"),
    [("modify_gtt", "ModifyGTTOrder", "Oi created"), ("cancel_gtt", "CancelGTTOrder", "Ok")],
)
async def test_another_endpoints_word_is_unknown(
    rig: Rig, method: str, endpoint: str, stat: str
) -> None:
    rig.broker.set_reply(endpoint, {"stat": stat, "al_id": "26111200000010"})
    arguments = _MODIFY if method == "modify_gtt" else {}
    with pytest.raises(OrderStatusUnknownError):
        await rig.run(rig.client(), method, "26111200000010", **arguments)
    assert len(rig.sent(endpoint)) == 1


async def test_word_without_al_id_is_unknown(rig: Rig) -> None:
    rig.broker.set_reply("PlaceGTTOrder", {"stat": "Oi created"})
    with pytest.raises(OrderStatusUnknownError):
        await rig.run(rig.client(), "place_gtt", **_PLACE)


async def test_timeout_after_send_is_unknown_never_retried(rig: Rig) -> None:
    client = rig.client()
    rig.broker.fail_next("PlaceGTTOrder", drills.timeout_after_send())
    with pytest.raises(OrderStatusUnknownError):
        await rig.run(client, "place_gtt", **_PLACE)
    assert len(rig.sent("PlaceGTTOrder")) == 1
    # It took effect on the server: the caller recovers it by tag.
    (gtt,) = await rig.run(client, "pending_gtts")
    assert gtt.tag == _PLACE["tag"]


async def test_reject_drill_is_a_rejection(rig: Rig) -> None:
    rig.broker.fail_next("PlaceGTTOrder", drills.reject("Invalid Input : ai_t"))
    with pytest.raises(OrderRejectedError, match="ai_t"):
        await rig.run(rig.client(), "place_gtt", **_PLACE)
    assert len(rig.sent("PlaceGTTOrder")) == 1


async def test_reads_are_retried(rig: Rig) -> None:
    rig.broker.fail_next("GetPendingGTTOrder", drills.server_error())
    assert await rig.run(rig.client(), "pending_gtts") == []
    assert len(rig.sent("GetPendingGTTOrder")) == 2


async def test_outcome_line_names_the_alert_id(rig: Rig, caplog: pytest.LogCaptureFixture) -> None:
    client = rig.client()
    with caplog.at_level(logging.INFO, logger="tradesmart.orders"):
        alert_id = await rig.run(client, "place_gtt", **_PLACE)
        await rig.run(client, "cancel_gtt", alert_id)
    lines = [r.getMessage() for r in caplog.records if r.name == "tradesmart.orders"]
    assert lines[0].startswith(f"PlaceGTTOrder accepted al_id={alert_id} in ")
    assert lines[1].startswith(f"CancelGTTOrder accepted al_id={alert_id} in ")
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "sdk-test-gtt" not in text and "1500" not in text and "ITC-EQ" not in text


# --- request() escape hatch (Review Focus 2) ----------------------------------------------------


@pytest.mark.parametrize("name", ["placegttorder", "PLACEGTTORDER", "/PlaceGttOrder/"])
async def test_request_canonicalises_and_honours_the_word(rig: Rig, name: str) -> None:
    client = rig.client()
    payload = {
        "actid": rig.broker.client_id,
        "exch": "NSE",
        "tsym": "ITC-EQ",
        "ai_t": "LTP_A_O",
        "d": "1500",
        "validity": "GTT",
        "remarks": "raw",
        "trantype": "B",
        "prctyp": "LMT",
        "prd": "C",
        "ret": "DAY",
        "qty": "1",
        "prc": "100",
    }
    # state_changing=False: the canonical name still makes it a never-retried order call.
    body = await rig.run(client, "request", name, payload, state_changing=False)
    assert body["stat"] == "Oi created" and body["al_id"]
    assert [r.endpoint for r in rig.broker.requests] == ["PlaceGTTOrder"]


async def test_request_timeout_is_unknown_never_retried(rig: Rig) -> None:
    rig.broker.fail_next("PlaceGTTOrder", drills.timeout_after_send())
    with pytest.raises(OrderStatusUnknownError):
        await rig.run(
            rig.client(), "request", "placegttorder", {"exch": "NSE"}, state_changing=False
        )
    assert len(rig.sent("PlaceGTTOrder")) == 1


async def test_offline_broker_refuses_an_alert_type_change(rig: Rig) -> None:
    client = rig.client()
    alert_id = await rig.run(client, "place_gtt", **_PLACE)
    with pytest.raises(OrderRejectedError, match="ai_t cannot be modified"):
        await rig.run(client, "modify_gtt", alert_id, **{**_MODIFY, "alert_type": "LTP_B_O"})


@pytest.mark.parametrize("endpoint", ["placegttorder", "modifygttorder"])
async def test_offline_broker_refuses_an_incomplete_gtt(rig: Rig, endpoint: str) -> None:
    with pytest.raises(OrderRejectedError, match="Invalid Input"):
        await rig.run(rig.client(), "request", endpoint, {"exch": "NSE"}, state_changing=False)


# --- review fix round 1 ---------------------------------------------------------------------------


@pytest.mark.parametrize("missing", ["validity", "alert_validity"])
async def test_modify_without_a_validity_is_refused_and_sends_nothing(
    rig: Rig, missing: str
) -> None:
    client = rig.client()
    alert_id = await rig.run(client, "place_gtt", **_PLACE)
    arguments = {k: v for k, v in _MODIFY.items() if k != missing}
    with pytest.raises(TypeError, match=missing):
        await rig.run(client, "modify_gtt", alert_id, **arguments)
    assert rig.sent("ModifyGTTOrder") == []


async def test_modify_sends_the_validities_given(rig: Rig) -> None:
    client = rig.client()
    alert_id = await rig.run(client, "place_gtt", **{**_PLACE, "alert_validity": "DAY"})
    await rig.run(
        client, "modify_gtt", alert_id, **{**_MODIFY, "validity": "EOS", "alert_validity": "DAY"}
    )
    (payload,) = rig.sent("ModifyGTTOrder")
    assert (payload["validity"], payload["ret"]) == ("DAY", "EOS")


@pytest.mark.parametrize(
    ("method", "endpoint", "word"),
    [
        ("modify_gtt", "ModifyGTTOrder", "Oi replaced"),
        ("cancel_gtt", "CancelGTTOrder", "Oi delete success"),
    ],
)
@pytest.mark.parametrize("returned", ["26111299999999", " 26111200000010 "])
async def test_a_different_returned_alert_id_is_kept_and_logged_at_debug(
    rig: Rig,
    caplog: pytest.LogCaptureFixture,
    method: str,
    endpoint: str,
    word: str,
    returned: str,
) -> None:
    rig.broker.set_reply(endpoint, {"stat": word, "al_id": returned})
    arguments = _MODIFY if method == "modify_gtt" else {}
    with caplog.at_level(logging.DEBUG, logger="tradesmart"):
        result = await rig.run(rig.client(), method, "26111200000010", **arguments)
    assert result == returned  # the server's id, as returned (never stripped)
    lines = [
        r.getMessage()
        for r in caplog.records
        if r.name == "tradesmart" and r.levelno == logging.DEBUG and "alert id" in r.getMessage()
    ]
    assert lines == [f"{endpoint}: the acknowledged alert id differs from the one sent"]


@pytest.mark.parametrize(
    ("method", "endpoint", "word"),
    [
        ("modify_gtt", "ModifyGTTOrder", "Oi replaced"),
        ("cancel_gtt", "CancelGTTOrder", "Oi delete success"),
    ],
)
async def test_the_same_returned_alert_id_logs_nothing(
    rig: Rig, caplog: pytest.LogCaptureFixture, method: str, endpoint: str, word: str
) -> None:
    rig.broker.set_reply(endpoint, {"stat": word, "al_id": "26111200000010"})
    arguments = _MODIFY if method == "modify_gtt" else {}
    with caplog.at_level(logging.DEBUG, logger="tradesmart"):
        await rig.run(rig.client(), method, "26111200000010", **arguments)
    assert not [r for r in caplog.records if "alert id" in r.getMessage()]
