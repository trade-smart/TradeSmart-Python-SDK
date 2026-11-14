"""Order outcome logging shared by the sync and async transports (logger ``tradesmart.orders``).

Only the endpoint, a result word, the order id and the latency are logged: never payloads,
prices, symbols or tokens. Only the order endpoints are logged: ``GenAcsTok`` is
state-changing but not an order, so it is skipped (its unknown outcome reaches the caller as an
``AuthenticationError``).
"""

from __future__ import annotations

import time
from typing import Any

from tradesmart._client_core import STATE_CHANGING_ENDPOINTS
from tradesmart._logging import get_logger
from tradesmart._transport.codec import GTT_WRITE_ENDPOINTS, check_hint
from tradesmart.endpoints.base import Call
from tradesmart.errors import OrderStatusUnknownError

_log = get_logger("orders")
# The order endpoints: PlaceOrder, PlaceMultiOrder, ModifyOrder, CancelOrder, ExitSNOOrder, the
# GTT writes (PlaceGTTOrder, ModifyGTTOrder, CancelGTTOrder) and the iceberg writes.
ORDER_ENDPOINTS = STATE_CHANGING_ENDPOINTS - {"GenAcsTok"}


def is_order_call(call: Call[Any]) -> bool:
    """Whether ``call`` goes to an order endpoint, whose outcome is logged.

    By name only: ``request(..., state_changing=True)`` to any other endpoint (e.g.
    ``OrderBook``) is never retried but is not an order, so it gets no outcome line.
    """
    return call.spec.name in ORDER_ENDPOINTS


def _id_key(call: Call[Any]) -> str:
    """``al_id`` (the GTT alert id) for the GTT writes, else ``order_id``."""
    return "al_id" if call.spec.name in GTT_WRITE_ENDPOINTS else "order_id"


def _order_id(call: Call[Any], result: Any) -> str:
    if call.spec.name in GTT_WRITE_ENDPOINTS:
        # The ack's al_id (a str from a typed method, a dict from request()), else the payload's.
        ack_id = result if isinstance(result, str) else getattr(result, "al_id", None)
        if ack_id is None and isinstance(result, dict):
            ack_id = result.get("al_id")
        payload_key = "al_id"
    else:
        ack_id = getattr(result, "order_id", None)
        if ack_id is None and isinstance(result, dict):
            ack_id = result.get("norenordno")
        payload_key = "norenordno"
    if isinstance(ack_id, str) and ack_id.strip():
        return ack_id.strip()
    return call.payload.get(payload_key) or "-"


class SendProgress:
    """Whether an order request may have reached the server (set by the transports).

    It stays False for errors raised before the request is written (missing or expired
    token, client-side rate limit, closed client) and is reset to False for connect-phase
    failures (``ConnectError``, ``ConnectTimeout``, ``PoolTimeout``). ``retryable`` is set to
    False when the caller's own event hook or transport raised: that is deterministic, so
    the request is never re-sent.
    """

    __slots__ = ("retryable", "sent")

    def __init__(self) -> None:
        self.sent = False
        self.retryable = True


def log_outcome(call: Call[Any], word: str, started: float, result: Any = None) -> None:
    """One INFO line: ``<endpoint> <word> order_id=<id> in <ms> ms`` (``al_id=`` for GTT).

    ``word`` is ``accepted``, ``rejected``, ``unknown`` or ``not_sent``.
    """
    _log.info(
        "%s %s %s=%s in %.0f ms",
        call.spec.name,
        word,
        _id_key(call),
        _order_id(call, result),
        (time.perf_counter() - started) * 1000,
    )


def log_failure(call: Call[Any], exc: BaseException, started: float, *, sent: bool) -> None:
    """Log a failed order call; an unknown outcome also gets its single WARNING.

    ``not_sent``: nothing reached the server; ``unknown``: it may have been executed;
    ``rejected``: the server answered with an error. An ``OrderStatusUnknownError`` is
    always ``unknown`` (with the WARNING), whatever ``sent`` says.
    """
    if isinstance(exc, OrderStatusUnknownError):
        log_outcome(call, "unknown", started)
        _log.warning(
            "%s: order status unknown; check %s before acting",
            call.spec.name,
            check_hint(call.spec.name),
        )
    elif not sent:
        log_outcome(call, "not_sent", started)
    else:
        log_outcome(call, "rejected", started)


def log_cancelled(call: Call[Any], started: float) -> None:
    """The awaiting task was cancelled after the request may have been sent."""
    log_outcome(call, "unknown", started)
    _log.warning("%s: outcome unknown - check %s", call.spec.name, check_hint(call.spec.name))
