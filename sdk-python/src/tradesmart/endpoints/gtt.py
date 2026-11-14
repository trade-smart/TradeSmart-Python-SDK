"""GTT endpoints: ``PlaceGTTOrder``, ``ModifyGTTOrder``, ``CancelGTTOrder`` (order calls) and the
reads ``GetPendingGTTOrder``, ``GetTrgOi``, ``GetEnabledGTTs``.

The three writes are state-changing: never retried, an ambiguous outcome is
``OrderStatusUnknownError``, and success is only the documented ``stat`` word (compared
ignoring letter case only, spec F32) with a non-blank ``al_id`` (checked by the codec). No
``ordersource`` and no ``algo_id`` are sent (the docs list neither for GTT).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from tradesmart._client_core import ACK_ID_FIELDS, SUCCESS_STATS, SUCCESS_STATS_IGNORE_CASE
from tradesmart._logging import redact
from tradesmart.endpoints.base import Call, EndpointSpec, parse_list
from tradesmart.enums import (
    Exchange,
    GttAlertType,
    GttValidity,
    OrderType,
    Product,
    TransactionType,
    Validity,
)
from tradesmart.errors import OrderStatusUnknownError, ResponseParseError
from tradesmart.models.base import PriceInput, validate_request
from tradesmart.models.gtt import (
    CancelGttRequest,
    Gtt,
    ModifyGttRequest,
    PlaceGttRequest,
    TriggeredGtt,
)

_log = logging.getLogger("tradesmart")


def _alert_id(endpoint: str) -> Callable[[Any], str]:
    """The ``al_id`` of an acknowledgement the codec accepted (word, ``al_id``, no ``emsg``).

    The server accepted the request, so a reply that is somehow unusable here is an unknown
    outcome, never a failure the caller could answer by sending again.
    """

    def _parse(body: Any) -> str:
        alert_id = body.get("al_id") if isinstance(body, dict) else None
        if isinstance(alert_id, str) and alert_id.strip():
            return alert_id
        raise OrderStatusUnknownError(
            endpoint,
            f"{endpoint} was accepted by the server but the acknowledgement carried no alert id; "
            "check pending_gtts() before acting",
            http_status=None,
            body=json.dumps(redact(body), default=str),
        )

    return _parse


def _write(name: str) -> EndpointSpec[str]:
    return EndpointSpec(
        name=name,
        parse=_alert_id(name),
        state_changing=True,
        success_stats=SUCCESS_STATS[name],
        requires_ack_id=ACK_ID_FIELDS[name],
        stats_ignore_case=name in SUCCESS_STATS_IGNORE_CASE,
    )


# State-changing endpoints: never retried (an ambiguous outcome raises
# ``OrderStatusUnknownError``). Do not add retries to these specs.
PLACE_GTT_ORDER: EndpointSpec[str] = _write("PlaceGTTOrder")
MODIFY_GTT_ORDER: EndpointSpec[str] = _write("ModifyGTTOrder")
CANCEL_GTT_ORDER: EndpointSpec[str] = _write("CancelGTTOrder")

GET_PENDING_GTT_ORDER: EndpointSpec[list[Gtt]] = EndpointSpec(
    name="GetPendingGTTOrder",
    parse=parse_list(Gtt, "GetPendingGTTOrder"),
    expect="list",
    allow_empty=True,
)

GET_TRG_OI: EndpointSpec[list[TriggeredGtt]] = EndpointSpec(
    name="GetTrgOi",
    parse=parse_list(TriggeredGtt, "GetTrgOi"),
    expect="list",
    allow_empty=True,
)


def _bad_alert_types(body: Any) -> ResponseParseError:
    return ResponseParseError(
        "GetEnabledGTTs: ai_ts must be a list of objects with a text ai_t",
        endpoint="GetEnabledGTTs",
        http_status=None,
        body=json.dumps(redact(body), default=str),
    )


def _parse_alert_types(body: Any) -> list[str]:
    """The ``ai_ts[].ai_t`` values (alert families such as ``LTP``), in the server's order.

    ``[]`` is the "no data" reply of this read; an ``Ok`` object without ``ai_ts`` has none.
    ``request_time`` (``DDMMYYYYHHMMSS`` here) is not parsed.
    """
    if isinstance(body, list) and not body:
        return []
    if not isinstance(body, dict):
        raise _bad_alert_types(body)
    items = body.get("ai_ts")
    if items is None:
        return []
    if not isinstance(items, list):
        raise _bad_alert_types(body)
    families: list[str] = []
    for item in items:
        family = item.get("ai_t") if isinstance(item, dict) else None
        if not isinstance(family, str):
            raise _bad_alert_types(body)
        families.append(family)
    return families


GET_ENABLED_GTTS: EndpointSpec[list[str]] = EndpointSpec(
    name="GetEnabledGTTs",
    parse=_parse_alert_types,
    allow_empty=True,
)


def place_gtt_call(
    uid: str,
    *,
    exchange: Exchange | str,
    tradingsymbol: str,
    alert_type: GttAlertType | str,
    trigger_value: PriceInput,
    quantity: int,
    transaction_type: TransactionType | str,
    product: Product | str,
    order_type: OrderType | str,
    tag: str,
    price: PriceInput | None = None,
    trigger_price: PriceInput | None = None,
    validity: Validity | str = Validity.DAY,
    alert_validity: GttValidity | str = GttValidity.GTT,
    disclosed_quantity: int | None = None,
) -> Call[str]:
    """Build the ``PlaceGTTOrder`` call (``trgprc`` and ``dscqty`` only when given)."""
    request = validate_request(
        PlaceGttRequest,
        uid=uid,
        actid=uid,
        exch=exchange,
        tsym=tradingsymbol,
        ai_t=alert_type,
        d=trigger_value,
        validity=alert_validity,
        remarks=tag,
        trantype=transaction_type,
        prctyp=order_type,
        prd=product,
        ret=validity,
        qty=quantity,
        prc=price,
        trgprc=trigger_price,
        dscqty=disclosed_quantity,
    )
    return Call(PLACE_GTT_ORDER, request.to_wire())


def modify_gtt_call(
    uid: str,
    alert_id: str,
    *,
    exchange: Exchange | str,
    tradingsymbol: str,
    alert_type: GttAlertType | str,
    trigger_value: PriceInput,
    quantity: int,
    transaction_type: TransactionType | str,
    product: Product | str,
    order_type: OrderType | str,
    tag: str,
    price: PriceInput | None = None,
    trigger_price: PriceInput | None = None,
    validity: Validity | str,
    alert_validity: GttValidity | str,
    disclosed_quantity: int | None = None,
) -> Call[str]:
    """Build the ``ModifyGTTOrder`` call: every field again (``ai_t`` cannot change; the server
    decides), with ``al_id`` third. ``validity`` and ``alert_validity`` have no defaults: they are
    sent on every modify, so a default would silently change them."""
    request = validate_request(
        ModifyGttRequest,
        uid=uid,
        actid=uid,
        al_id=alert_id,
        exch=exchange,
        tsym=tradingsymbol,
        ai_t=alert_type,
        d=trigger_value,
        validity=alert_validity,
        remarks=tag,
        trantype=transaction_type,
        prctyp=order_type,
        prd=product,
        ret=validity,
        qty=quantity,
        prc=price,
        trgprc=trigger_price,
        dscqty=disclosed_quantity,
    )
    return Call(MODIFY_GTT_ORDER, request.to_wire())


def acknowledged_alert_id(endpoint: str, sent: str, returned: str) -> str:
    """The alert id a Modify / Cancel GTT ack confirms: the server's ``al_id``, as returned.

    It may differ from ``sent`` (the docs do not say it is the same); then one DEBUG line on the
    ``tradesmart`` logger says so (no ids or other fields are logged).
    """
    if returned != sent:
        _log.debug("%s: the acknowledged alert id differs from the one sent", endpoint)
    return returned


def cancel_gtt_call(uid: str, alert_id: str) -> Call[str]:
    """Build the ``CancelGTTOrder`` call (``uid`` and ``al_id``; no ``actid``, as documented)."""
    request = validate_request(CancelGttRequest, uid=uid, al_id=alert_id)
    return Call(CANCEL_GTT_ORDER, request.to_wire())


def pending_gtts_call(uid: str) -> Call[list[Gtt]]:
    """Build the ``GetPendingGTTOrder`` call (``uid`` only, as documented)."""
    return Call(GET_PENDING_GTT_ORDER, {"uid": uid})


def triggered_gtts_call(uid: str) -> Call[list[TriggeredGtt]]:
    """Build the ``GetTrgOi`` call (``uid`` and ``actid``, as documented)."""
    return Call(GET_TRG_OI, {"uid": uid, "actid": uid})


def gtt_alert_types_call(uid: str) -> Call[list[str]]:
    """Build the ``GetEnabledGTTs`` call (``uid`` only, as documented)."""
    return Call(GET_ENABLED_GTTS, {"uid": uid})
