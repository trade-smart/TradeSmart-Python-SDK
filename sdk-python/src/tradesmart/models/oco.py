"""Models for the OCO endpoints (``PlaceOCOOrder``, ``ModifyOCOOrder``, ``CancelOCOOrder``).

An OCO (one cancels the other) is a multi-day exit: a stop-loss order and a target order wait on
the server, each behind its own trigger value; when one trigger is reached its order is sent and
the other is cancelled. A trigger that is already crossed when the OCO is placed may fire at once.

The request models take the call's shared fields flat (``trantype``, ``prd``, ``ret``, ``qty``,
``remarks``) and the two legs as small wire-keyed models, so that every validation error carries
the wire path of the field at fault (``oivariable``, 0, ``d``; ``place_order_params``, ``prc``;
``prd``) and each fault gives exactly one issue. ``to_wire()`` builds the nested body (its key
order is part of the contract).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SkipValidation,
    ValidationInfo,
    field_validator,
    model_validator,
)

from tradesmart.enums import (
    Exchange,
    GttAlertType,
    GttValidity,
    OrderType,
    Product,
    TransactionType,
    Validity,
)
from tradesmart.models.base import (
    Money,
    PriceInput,
    RequestModel,
    StrictQuantity,
    WireValue,
    to_wire_value,
)
from tradesmart.models.gtt import _EXCHANGES, _PRODUCTS, _NonBlank
from tradesmart.models.orders import _MARKET_TYPES, _STOP_TYPES, _default_market_price

_ORDER_SOURCE = "API"


class OcoLeg(BaseModel):
    """One side of an OCO: the trigger value and the order sent when it is reached.

    ``trigger_value`` is the value the last price must reach (sent as given, digits kept). The
    order is ``order_type`` (``LIMIT`` by default) at ``price`` (``0`` when omitted on a market
    order); ``trigger_price`` is the stop trigger of that order for ``SL`` / ``SL_M`` and is
    refused for the other types (it is not the OCO trigger value). Nothing is checked here: the
    values are validated, with the wire path of each error, when the call is built.

    Fields are kept exactly as given: no normalisation, so equality compares the raw values
    (``order_type`` may be a plain ``str``, and ``"1300"`` is not equal to ``Decimal("1300")``).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    trigger_value: Annotated[PriceInput, SkipValidation]
    price: Annotated[PriceInput | None, SkipValidation] = None
    order_type: Annotated[OrderType | str, SkipValidation] = OrderType.LIMIT
    trigger_price: Annotated[PriceInput | None, SkipValidation] = None


class _OcoVariable(RequestModel):
    """One ``oivariable`` item: ``x`` (the stop-loss trigger) or ``y`` (the target trigger)."""

    var_name: str
    d: Money = Field(gt=0)


class _OcoLegOrder(RequestModel):
    """The leg-specific part of one order of the OCO (``prctyp``, ``prc``, ``trgprc``)."""

    prctyp: OrderType
    prc: Money = Field(ge=0)
    trgprc: Money | None = Field(default=None, ge=0, validate_default=True)

    @model_validator(mode="before")
    @classmethod
    def _market_price_default(cls, data: Any) -> Any:
        """A MKT / SL-MKT leg without a price is sent with ``prc`` 0, as in ``place_order``."""
        return _default_market_price(data)

    @field_validator("prc")
    @classmethod
    def _price_rule(cls, value: Decimal, info: ValidationInfo) -> Decimal:
        order_type: OrderType | None = info.data.get("prctyp")
        if order_type is not None and order_type not in _MARKET_TYPES and value <= 0:
            raise ValueError(f"price must be greater than 0 for {order_type.value} orders")
        return value

    @field_validator("trgprc")
    @classmethod
    def _trigger_price_rule(cls, value: Decimal | None, info: ValidationInfo) -> Decimal | None:
        order_type: OrderType | None = info.data.get("prctyp")
        if order_type is None:
            return value
        if order_type in _STOP_TYPES and (value is None or value <= 0):
            raise ValueError(
                f"trigger_price is required and must be greater than 0 for {order_type.value}"
            )
        if value is not None and order_type not in _STOP_TYPES:
            raise ValueError("trigger_price applies to SL-LMT and SL-MKT orders only")
        return value


class PlaceOcoRequest(RequestModel):
    """Validated ``PlaceOCOOrder`` parameters; ``to_wire()`` is the nested body."""

    uid: str
    tsym: str
    exch: Exchange
    validity: GttValidity
    trantype: TransactionType
    prd: Product
    ret: Validity
    qty: StrictQuantity = Field(ge=1)
    remarks: _NonBlank | None = None
    oivariable: list[_OcoVariable] = Field(min_length=2, max_length=2)
    place_order_params: _OcoLegOrder
    place_order_params_leg2: _OcoLegOrder

    @field_validator("exch")
    @classmethod
    def _documented_exchange(cls, value: Exchange) -> Exchange:
        if value not in _EXCHANGES:
            raise ValueError("an OCO is offered on NSE, NFO, BSE, BFO and MCX only")
        return value

    @field_validator("prd")
    @classmethod
    def _documented_product(cls, value: Product) -> Product:
        if value not in _PRODUCTS:
            raise ValueError("an OCO order's product must be CNC, NRML or CO (not MIS, BO or MTF)")
        return value

    @field_validator("oivariable")
    @classmethod
    def _side_check(cls, value: list[_OcoVariable], info: ValidationInfo) -> list[_OcoVariable]:
        """BUY closes a short: stop-loss trigger above the target's; SELL: below; never equal."""
        side: TransactionType | None = info.data.get("trantype")
        if side is None:
            return value
        stop_loss, target = value[0].d, value[1].d
        if side is TransactionType.BUY and not stop_loss > target:
            raise ValueError("a BUY OCO needs the stop-loss trigger above the target trigger")
        if side is TransactionType.SELL and not stop_loss < target:
            raise ValueError("a SELL OCO needs the stop-loss trigger below the target trigger")
        return value

    def _leg_wire(self, leg: _OcoLegOrder) -> dict[str, WireValue]:
        wire: dict[str, WireValue] = {
            "tsym": self.tsym,
            "exch": to_wire_value(self.exch),
            "trantype": to_wire_value(self.trantype),
            "prctyp": to_wire_value(leg.prctyp),
            "prd": to_wire_value(self.prd),
            "ret": to_wire_value(self.ret),
            "actid": self.uid,
            "uid": self.uid,
            "qty": to_wire_value(self.qty),
            "prc": to_wire_value(leg.prc),
            "ordersource": _ORDER_SOURCE,
        }
        if leg.trgprc is not None:
            wire["trgprc"] = to_wire_value(leg.trgprc)
        if self.remarks is not None:
            wire["remarks"] = self.remarks
        return wire

    def _alert_id_wire(self) -> dict[str, WireValue]:
        return {}

    def to_wire(self) -> dict[str, WireValue]:
        body: dict[str, WireValue] = {"uid": self.uid, **self._alert_id_wire()}
        body["tsym"] = self.tsym
        body["exch"] = to_wire_value(self.exch)
        body["validity"] = to_wire_value(self.validity)
        body["ai_t"] = (
            GttAlertType.OCO_BUY.value
            if self.trantype is TransactionType.BUY
            else GttAlertType.OCO_SELL.value
        )
        body["oivariable"] = [
            {"var_name": item.var_name, "d": to_wire_value(item.d)} for item in self.oivariable
        ]
        body["place_order_params"] = self._leg_wire(self.place_order_params)
        body["place_order_params_leg2"] = self._leg_wire(self.place_order_params_leg2)
        return body


class ModifyOcoRequest(PlaceOcoRequest):
    """Validated ``ModifyOCOOrder`` parameters: every Place field again, ``al_id`` second."""

    al_id: _NonBlank

    def _alert_id_wire(self) -> dict[str, WireValue]:
        return {"al_id": self.al_id}


class CancelOcoRequest(RequestModel):
    """Validated ``CancelOCOOrder`` parameters (``uid`` and ``al_id`` only)."""

    uid: str
    al_id: _NonBlank
