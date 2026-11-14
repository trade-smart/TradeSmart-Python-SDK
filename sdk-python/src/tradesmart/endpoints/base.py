"""Endpoint specification shared by the sync and async transports."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Generic, Literal, TypeVar

from tradesmart._transport.codec import OK_STATS, redact_body
from tradesmart.errors import RequestValidationError, ResponseParseError
from tradesmart.models.base import TradeSmartModel

T = TypeVar("T")
M = TypeVar("M", bound=TradeSmartModel)


@dataclass(frozen=True)
class EndpointSpec(Generic[T]):
    """Static description of one REST endpoint."""

    name: str
    parse: Callable[[Any], T]
    authenticated: bool = True
    state_changing: bool = False
    expect: Literal["object", "list"] = "object"
    allow_empty: bool = False
    rate_group: str | None = None
    # The `stat` words that mean success (the GTT and iceberg writes have their own).
    success_stats: frozenset[str] = OK_STATS
    # A field that must be a non-blank string on success (GTT writes: "al_id").
    requires_ack_id: str | None = None
    # Compare ``success_stats`` ignoring letter case only (GTT writes, spec F32).
    stats_ignore_case: bool = False

    @property
    def path(self) -> str:
        return "/" + self.name


@dataclass(frozen=True)
class Call(Generic[T]):
    """An endpoint spec bound to a wire payload, ready for a transport."""

    spec: EndpointSpec[T]
    payload: dict[str, str]


def invalid_field(field: str, message: str) -> RequestValidationError:
    """A ``RequestValidationError`` for one field (the value is never echoed)."""
    error = RequestValidationError([{"type": "value_error", "loc": (field,), "msg": message}])
    error.args = (f"Invalid request parameters: {field} ({message})",)
    return error


def require_str(value: Any, field: str) -> str:
    """``value`` when it is a ``str``; else ``RequestValidationError`` for ``field``.

    Every value on the wire is a string, and ids (order numbers, instrument tokens) are
    ``str`` in this SDK: an ``int`` is not converted (it would go out as a JSON number).
    """
    if not isinstance(value, str):
        raise invalid_field(field, "must be a string")
    return value


def parse_object(model: type[M], endpoint: str) -> Callable[[Any], M]:
    """Build a parser turning a response object into ``model``."""

    def _parse(body: Any) -> M:
        return model.from_wire(body, endpoint=endpoint)

    return _parse


def parse_list(model: type[M], endpoint: str) -> Callable[[Any], list[M]]:
    """Build a parser turning a response list into a list of ``model``."""

    def _parse(body: Any) -> list[M]:
        if not isinstance(body, list):
            raise ResponseParseError(
                f"{endpoint}: expected a JSON list",
                endpoint=endpoint,
                http_status=None,
                body=redact_body(body),
            )
        return [model.from_wire(item, endpoint=endpoint) for item in body]

    return _parse
