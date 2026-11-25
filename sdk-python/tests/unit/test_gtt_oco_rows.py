"""OCO legs read back from ``GetPendingGTTOrder`` / ``GetTrgOi`` rows.

Spec: ``specifications/sdk-behaviour.md`` 2.9 "Reading back"; ``specifications/models.md``
(``OcoTrigger``, ``OcoLegRow``, ``OcoSide``, ``Gtt.oco_*``).
"""

import copy
from dataclasses import FrozenInstanceError
from decimal import Decimal
from typing import Any

import pytest

from tests.conftest import fixture
from tradesmart import Gtt, GttAlertType, OrderType, Product, TransactionType, TriggeredGtt
from tradesmart.endpoints.gtt import GET_PENDING_GTT_ORDER, GET_TRG_OI
from tradesmart.models import OcoLegRow, OcoSide, OcoTrigger


def _row(
    alert: str,
    x: str,
    y: str,
    *,
    order: tuple[str, str] = ("x", "y"),
    qty: Any = 1,
) -> dict[str, Any]:
    values = {"x": x, "y": y}
    return {
        "stat": "Ok",
        "ai_t": alert,
        "al_id": "26111800000099",
        "tsym": "RELIANCE-EQ",
        "d": x,
        "place_order_params": {
            "actid": "TESTUID",
            "trantype": "B",
            "prctyp": "LMT",
            "qty": qty,
            "prc": "1340.00",
            "prd": "C",
            "ordersource": "API",
            "ipaddr": "0.0.0.0",  # noqa: S104
            "s_prdt_ali": "CNC",
        },
        "place_order_params_leg2": {"trantype": "B", "prctyp": "LMT", "qty": 1, "prc": "1160.00"},
        "oivariable": [{"var_name": name, "d": values[name]} for name in order],
    }


def _gtt(row: dict[str, Any]) -> Gtt:
    return Gtt.from_wire(row)


def test_buy_oco_row_reads_back_the_higher_trigger_as_stop_loss() -> None:
    gtt = _gtt(_row("LMT_BOS_O", "1400.00", "1170.00"))
    assert gtt.is_oco is True
    assert gtt.alert_type is GttAlertType.OCO_BUY
    assert gtt.stop_loss is not None and gtt.target is not None
    assert gtt.stop_loss.trigger_value == Decimal("1400.00")
    assert gtt.stop_loss.leg is gtt.oco_first
    assert gtt.target.trigger_value == Decimal("1170.00")
    assert gtt.target.leg.price == Decimal("1160.00")


def test_sell_oco_row_with_the_target_in_x_takes_the_lower_trigger_as_stop_loss() -> None:
    gtt = _gtt(_row("LMT_SOB_O", "1430.00", "1170.00"))
    assert gtt.stop_loss is not None and gtt.target is not None
    assert gtt.stop_loss.trigger_value == Decimal("1170.00")
    assert gtt.stop_loss.leg is gtt.oco_second
    assert gtt.target.trigger_value == Decimal("1430.00")
    assert gtt.target.leg is gtt.oco_first


def test_triggers_pair_with_legs_by_name_not_position() -> None:
    gtt = _gtt(_row("LMT_BOS_O", "1400.00", "1170.00", order=("y", "x")))
    assert [t.var_name for t in gtt.oco_triggers or []] == ["y", "x"]
    assert gtt.stop_loss is not None and gtt.target is not None
    assert gtt.stop_loss.leg is gtt.oco_first
    assert gtt.stop_loss.trigger_value == Decimal("1400.00")
    assert gtt.target.leg is gtt.oco_second


def test_leg_fields_and_extras() -> None:
    gtt = _gtt(_row("LMT_BOS_O", "1400.00", "1170.00", qty="3"))
    leg = gtt.oco_first
    assert isinstance(leg, OcoLegRow)
    assert leg.transaction_type is TransactionType.BUY
    assert leg.order_type is OrderType.LIMIT
    assert leg.quantity == 3 and isinstance(leg.quantity, int)
    assert leg.price == Decimal("1340.00")
    assert leg.trigger_price is None
    assert leg.product is Product.CNC
    assert leg.account_id == "TESTUID"
    assert leg.ordersource == "API"
    assert leg.model_extra is not None and leg.model_extra["ipaddr"] == "0.0.0.0"  # noqa: S104
    assert isinstance(gtt.oco_triggers, list) and isinstance(gtt.oco_triggers[0], OcoTrigger)


def test_leg_quantity_as_json_number_is_an_int() -> None:
    leg = _gtt(_row("LMT_BOS_O", "1400.00", "1170.00", qty=2)).oco_first
    assert leg is not None and leg.quantity == 2 and isinstance(leg.quantity, int)


def test_plain_gtt_row_has_no_oco_fields() -> None:
    gtt = _gtt(fixture("pending_gtts_success")[0])
    assert gtt.is_oco is False
    assert gtt.oco_first is None and gtt.oco_second is None and gtt.oco_triggers is None
    assert gtt.stop_loss is None and gtt.target is None


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r.update(oivariable=[{"var_name": "x", "d": "1400.00"}]),
        lambda r: r.update(oivariable=[{"var_name": "x", "d": "1400.00"}] * 2),
        lambda r: r.update(
            oivariable=[{"var_name": "x", "d": "1170.00"}, {"var_name": "y", "d": "1170.00"}]
        ),
        lambda r: r.update(
            oivariable=[
                {"var_name": "x", "d": "1400.00"},
                {"var_name": "x", "d": "1400.00"},
                {"var_name": "y", "d": "1170.00"},
            ]
        ),
        lambda r: r.update(
            oivariable=[
                {"var_name": "x", "d": "1400.00"},
                {"var_name": "y", "d": "1170.00"},
                {"var_name": "y", "d": "1100.00"},
            ]
        ),
        lambda r: r.pop("oivariable"),
        lambda r: r.pop("place_order_params_leg2"),
        lambda r: r.update(oivariable=[{"var_name": "x", "d": ""}, {"var_name": "y", "d": "1"}]),
        lambda r: r.update(ai_t="LTP_A_O"),
    ],
)
def test_unresolvable_rows_give_no_sides(mutate: Any) -> None:
    row = copy.deepcopy(_row("LMT_BOS_O", "1400.00", "1170.00"))
    mutate(row)
    gtt = _gtt(row)
    assert gtt.stop_loss is None and gtt.target is None


def test_oco_side_is_a_frozen_dataclass() -> None:
    side = _gtt(_row("LMT_BOS_O", "1400.00", "1170.00")).stop_loss
    assert isinstance(side, OcoSide)
    with pytest.raises(FrozenInstanceError):
        side.trigger_value = Decimal(1)  # type: ignore[misc]


def test_pending_fixture_oco_rows_parse() -> None:
    rows = GET_PENDING_GTT_ORDER.parse(fixture("pending_gtts_success"))
    buy, sell = rows[2], rows[3]
    assert buy.is_oco and sell.is_oco
    assert buy.stop_loss is not None and buy.stop_loss.trigger_value == Decimal("1430.00")
    assert buy.target is not None and buy.target.leg.price == Decimal("1160.00")
    assert sell.stop_loss is not None and sell.stop_loss.trigger_value == Decimal("1170.00")
    assert sell.target is not None and sell.target.trigger_value == Decimal("1430.00")


def test_triggered_fixture_cancelled_oco_row_parses() -> None:
    rows = GET_TRG_OI.parse(fixture("triggered_gtts_success"))
    cancelled = next(r for r in rows if r.stat == "CANCELLED")
    assert isinstance(cancelled, TriggeredGtt)
    assert cancelled.is_oco and cancelled.alert_type is GttAlertType.OCO_BUY
    assert cancelled.stop_loss is not None and cancelled.target is not None
