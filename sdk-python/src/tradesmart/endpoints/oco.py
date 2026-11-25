"""OCO endpoints: ``PlaceOCOOrder``, ``ModifyOCOOrder``, ``CancelOCOOrder``.

State-changing like the GTT writes: never retried, an ambiguous outcome is
``OrderStatusUnknownError``, success only on the observed ``stat`` word with a non-blank ``al_id``
(see ``alert_write_spec``). The result is the alert id.
"""

from __future__ import annotations

from typing import Any

from tradesmart.endpoints.base import Call, EndpointSpec
from tradesmart.endpoints.gtt import alert_write_spec
from tradesmart.enums import Exchange, GttValidity, Product, TransactionType, Validity
from tradesmart.errors import RequestValidationError
from tradesmart.models.base import validate_request
from tradesmart.models.oco import CancelOcoRequest, ModifyOcoRequest, OcoLeg, PlaceOcoRequest

# State-changing endpoints: never retried. Do not add retries to these specs.
PLACE_OCO_ORDER: EndpointSpec[str] = alert_write_spec("PlaceOCOOrder")
MODIFY_OCO_ORDER: EndpointSpec[str] = alert_write_spec("ModifyOCOOrder")
CANCEL_OCO_ORDER: EndpointSpec[str] = alert_write_spec("CancelOCOOrder")


def _check_legs(stop_loss: object, target: object) -> None:
    """Refuse anything that is not an ``OcoLeg`` (no coercion), one issue per bad leg."""
    errors: list[dict[str, Any]] = [
        {
            "type": "model_type",
            "loc": (key,),
            "msg": "must be an OcoLeg",
            "input": type(leg).__name__,
        }
        for key, leg in (("place_order_params", stop_loss), ("place_order_params_leg2", target))
        if not isinstance(leg, OcoLeg)
    ]
    if errors:
        raise RequestValidationError(errors)


def _fields(
    uid: str,
    *,
    exchange: Exchange | str,
    tradingsymbol: str,
    transaction_type: TransactionType | str,
    quantity: int,
    product: Product | str,
    stop_loss: OcoLeg,
    target: OcoLeg,
    tag: str | None,
    validity: Validity | str,
    alert_validity: GttValidity | str,
) -> dict[str, object]:
    _check_legs(stop_loss, target)

    def order(leg: OcoLeg) -> dict[str, object]:
        return {"prctyp": leg.order_type, "prc": leg.price, "trgprc": leg.trigger_price}

    return {
        "uid": uid,
        "tsym": tradingsymbol,
        "exch": exchange,
        "validity": alert_validity,
        "trantype": transaction_type,
        "prd": product,
        "ret": validity,
        "qty": quantity,
        "remarks": tag,
        "oivariable": [
            {"var_name": "x", "d": stop_loss.trigger_value},
            {"var_name": "y", "d": target.trigger_value},
        ],
        "place_order_params": order(stop_loss),
        "place_order_params_leg2": order(target),
    }


def place_oco_call(
    uid: str,
    *,
    exchange: Exchange | str,
    tradingsymbol: str,
    transaction_type: TransactionType | str,
    quantity: int,
    product: Product | str,
    stop_loss: OcoLeg,
    target: OcoLeg,
    tag: str | None = None,
    validity: Validity | str = Validity.DAY,
    alert_validity: GttValidity | str = GttValidity.GTT,
) -> Call[str]:
    """Build the ``PlaceOCOOrder`` call (the stop-loss is always ``x``, the target ``y``)."""
    request = validate_request(
        PlaceOcoRequest,
        **_fields(
            uid,
            exchange=exchange,
            tradingsymbol=tradingsymbol,
            transaction_type=transaction_type,
            quantity=quantity,
            product=product,
            stop_loss=stop_loss,
            target=target,
            tag=tag,
            validity=validity,
            alert_validity=alert_validity,
        ),
    )
    return Call(PLACE_OCO_ORDER, request.to_wire())


def modify_oco_call(
    uid: str,
    alert_id: str,
    *,
    exchange: Exchange | str,
    tradingsymbol: str,
    transaction_type: TransactionType | str,
    quantity: int,
    product: Product | str,
    stop_loss: OcoLeg,
    target: OcoLeg,
    tag: str | None = None,
    validity: Validity | str,
    alert_validity: GttValidity | str,
) -> Call[str]:
    """Build the ``ModifyOCOOrder`` call: the whole OCO again, ``al_id`` second. ``validity`` and
    ``alert_validity`` have no defaults (a default would silently change them)."""
    request = validate_request(
        ModifyOcoRequest,
        al_id=alert_id,
        **_fields(
            uid,
            exchange=exchange,
            tradingsymbol=tradingsymbol,
            transaction_type=transaction_type,
            quantity=quantity,
            product=product,
            stop_loss=stop_loss,
            target=target,
            tag=tag,
            validity=validity,
            alert_validity=alert_validity,
        ),
    )
    return Call(MODIFY_OCO_ORDER, request.to_wire())


def cancel_oco_call(uid: str, alert_id: str) -> Call[str]:
    """Build the ``CancelOCOOrder`` call (``uid`` and ``al_id``)."""
    request = validate_request(CancelOcoRequest, uid=uid, al_id=alert_id)
    return Call(CANCEL_OCO_ORDER, request.to_wire())
