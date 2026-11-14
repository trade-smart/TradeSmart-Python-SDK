"""Failure drills: arm one with ``broker.fail_next(endpoint, drill)``; it fires on the next call.

Each drill fires once, on the next call of that endpoint, and is raised at once (no test waits
for a real timeout).
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "Drill",
    "bad_body",
    "connect_error",
    "rate_limit",
    "reject",
    "server_error",
    "session_expired",
    "timeout_after_send",
]


@dataclass(frozen=True)
class Drill:
    """One armed failure; build it with the functions of this module."""

    kind: str
    message: str | None = None
    status: int | None = None
    retry_after: float | None = None
    json: bool = False


def reject(message: str) -> Drill:
    """The server answers ``Not_Ok`` with ``message`` as ``emsg``."""
    return Drill("reject", message=message)


def timeout_after_send() -> Drill:
    """The call takes effect (an order IS placed) but the client gets a read timeout."""
    return Drill("timeout_after_send")


def connect_error() -> Drill:
    """The connection fails before anything is sent; nothing takes effect."""
    return Drill("connect_error")


def rate_limit(retry_after: float | None = None) -> Drill:
    """HTTP 429 with a JSON ``Not_Ok`` body (assumed), and a ``Retry-After`` header when
    ``retry_after`` is given."""
    return Drill("rate_limit", retry_after=retry_after)


def server_error(status: int = 503, *, json: bool = False) -> Drill:
    """An HTTP server error with this status.

    By default the body is an HTML gateway page, as a real gateway 5xx normally is: on an order
    the outcome is unknown (``OrderStatusUnknownError``). ``json=True`` sends a JSON ``Not_Ok``
    body instead (assumed shape): on an order, a definite rejection (``OrderRejectedError``).
    """
    return Drill("server_error", status=status, json=json)


def bad_body() -> Drill:
    """A reply body that is not JSON."""
    return Drill("bad_body")


def session_expired() -> Drill:
    """``Not_Ok`` "Session Expired": the login is no longer valid."""
    return Drill("session_expired")
