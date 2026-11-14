"""The shared GTT fixtures in ``specifications/test-fixtures/`` parse with the SDK.

Spec: ``specifications/sdk-behaviour.md`` 2.8 and 4.2.1 (the exact success words, a non-blank
``al_id``); ``specifications/models.md`` (``Gtt``, ``TriggeredGtt``).
"""

import json
from datetime import datetime
from decimal import Decimal

import pytest

from tests.conftest import fixture
from tradesmart._client_core import ACK_ID_FIELDS, SUCCESS_STATS
from tradesmart._transport.codec import parse_response
from tradesmart.endpoints.gtt import (
    CANCEL_GTT_ORDER,
    GET_ENABLED_GTTS,
    GET_PENDING_GTT_ORDER,
    GET_TRG_OI,
    MODIFY_GTT_ORDER,
    PLACE_GTT_ORDER,
)
from tradesmart.enums import (
    GttAlertType,
    GttValidity,
    OrderType,
    Product,
    TransactionType,
    Validity,
)
from tradesmart.models import base


@pytest.mark.parametrize(
    ("name", "spec", "word"),
    [
        ("place_gtt_success", PLACE_GTT_ORDER, "Oi created"),
        ("modify_gtt_success", MODIFY_GTT_ORDER, "Oi replaced"),
        ("cancel_gtt_success", CANCEL_GTT_ORDER, "Oi delete success"),
    ],
)
def test_gtt_ack_fixture_is_a_success(name: str, spec: object, word: str) -> None:
    reply = fixture(name)
    assert reply["stat"] == word
    endpoint = spec.name  # type: ignore[attr-defined]
    body = parse_response(
        endpoint=endpoint,
        status_code=200,
        text=json.dumps(reply),
        expect="object",
        allow_empty=False,
        state_changing=True,
        success_stats=SUCCESS_STATS[endpoint],
        requires_ack_id=ACK_ID_FIELDS[endpoint],
    )
    assert spec.parse(body) == reply["al_id"]  # type: ignore[attr-defined]


def test_pending_gtts_fixture_parses() -> None:
    rows = GET_PENDING_GTT_ORDER.parse(fixture("pending_gtts_success"))
    assert len(rows) == 2
    first, second = rows
    assert first.alert_id == "26111200000010"
    assert first.alert_type is GttAlertType.LTP_BELOW
    assert first.trigger_value == Decimal("1500.00")
    assert first.alert_validity is GttValidity.GTT
    assert first.validity is Validity.DAY
    assert first.transaction_type is TransactionType.BUY
    assert first.order_type is OrderType.LIMIT
    assert first.product is Product.CNC
    assert first.quantity == 1
    assert first.tag == "Buy INFY below 1500"
    assert first.trigger_price is None
    assert second.order_type is OrderType.SL
    assert second.trigger_price == Decimal("1605.00")
    assert second.alert_type is GttAlertType.LTP_ABOVE


def test_triggered_gtts_fixture_parses() -> None:
    (row,) = GET_TRG_OI.parse(fixture("triggered_gtts_success"))
    assert row.alert_id == "26111200000010"
    assert row.norentm == datetime(2026, 11, 13, 10, 41, 27, tzinfo=base.IST)
    assert (row.sms_flag, row.email_flag, row.push_flag) == ("0", "0", "0")


def test_enabled_gtts_fixture_parses() -> None:
    assert GET_ENABLED_GTTS.parse(fixture("enabled_gtts_success")) == ["ATP", "LTP"]
