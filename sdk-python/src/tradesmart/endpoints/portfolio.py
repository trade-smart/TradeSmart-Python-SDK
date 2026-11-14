"""``Holdings``, ``Limits`` and ``PositionBook`` endpoints."""

from __future__ import annotations

from tradesmart.endpoints.base import Call, EndpointSpec, parse_list, parse_object
from tradesmart.enums import Product
from tradesmart.models.base import validate_request
from tradesmart.models.portfolio import Holding, HoldingsRequest, Limits, Position

HOLDINGS: EndpointSpec[list[Holding]] = EndpointSpec(
    name="Holdings",
    parse=parse_list(Holding, "Holdings"),
    expect="list",
    allow_empty=True,
)

LIMITS: EndpointSpec[Limits] = EndpointSpec(
    name="Limits",
    parse=parse_object(Limits, "Limits"),
)

POSITION_BOOK: EndpointSpec[list[Position]] = EndpointSpec(
    name="PositionBook",
    parse=parse_list(Position, "PositionBook"),
    expect="list",
    allow_empty=True,
)


def holdings_call(uid: str, product: Product | str = Product.CNC) -> Call[list[Holding]]:
    """Build the ``Holdings`` call; a string ``product`` resolves through ``Product`` (any case).

    An unknown product raises ``RequestValidationError`` (field ``prd``) before anything is sent.
    """
    request = validate_request(HoldingsRequest, uid=uid, actid=uid, prd=product)
    return Call(HOLDINGS, request.to_wire())


def limits_call(uid: str) -> Call[Limits]:
    """Build the ``Limits`` call."""
    return Call(LIMITS, {"uid": uid, "actid": uid})


def positions_call(uid: str) -> Call[list[Position]]:
    """Build the ``PositionBook`` call."""
    return Call(POSITION_BOOK, {"uid": uid, "actid": uid})
