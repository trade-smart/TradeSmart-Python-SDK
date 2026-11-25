"""OCO legs of the rows of ``GetPendingGTTOrder`` / ``GetTrgOi`` (spec ``sdk-behaviour.md`` 2.9).

The leg rows carry no trigger value: the triggers stay in ``Gtt.oco_triggers`` (as on the wire)
and ``OcoSide`` joins a leg with its trigger.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import ClassVar

from pydantic import Field

from tradesmart.models.base import (
    LenientStr,
    OrderTypeField,
    Price,
    ProductField,
    Quantity,
    TradeSmartModel,
    TransactionTypeField,
)


class OcoTrigger(TradeSmartModel):
    """One trigger value of an OCO row of ``GetPendingGTTOrder`` / ``GetTrgOi`` (an item of ``oivariable``): ``var_name`` ``x`` or ``y`` names the leg it belongs to."""  # noqa: E501

    lenient_parsing: ClassVar[bool] = True

    var_name: LenientStr = None
    trigger_value: Price = Field(default=None, alias="d")


class OcoLegRow(TradeSmartModel):
    """One order leg of an OCO row (``place_order_params`` / ``place_order_params_leg2``). Its trigger value is not here: it is the ``oivariable`` item with the same slot (``x`` for the first leg, ``y`` for the second), see ``Gtt.oco_triggers``. Other keys of the leg (``s_prdt_ali``, ``C``, ``ipaddr``) are kept as extras."""  # noqa: E501

    lenient_parsing: ClassVar[bool] = True

    transaction_type: TransactionTypeField = Field(default=None, alias="trantype")
    order_type: OrderTypeField = Field(default=None, alias="prctyp")
    quantity: Quantity = Field(default=None, alias="qty")
    price: Price = Field(default=None, alias="prc")
    trigger_price: Price = Field(default=None, alias="trgprc")
    product: ProductField = Field(default=None, alias="prd")
    account_id: LenientStr = Field(default=None, alias="actid")
    ordersource: LenientStr = None
    tag: LenientStr = Field(default=None, alias="remarks")


@dataclass(frozen=True)
class OcoSide:
    """One side of an OCO row, the result of ``Gtt.stop_loss`` / ``Gtt.target``: the leg and its trigger value. A derived, read-only result (not a wire model)."""  # noqa: E501

    leg: OcoLegRow
    trigger_value: Decimal
