"""Exception hierarchy for the TradeSmart SDK."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from tradesmart._slicing import SlicedOrderResult

_BODY_LIMIT = 2000


class TradeSmartError(Exception):
    """Base class for every error raised by this SDK."""


class ConfigurationError(TradeSmartError):
    """Invalid client configuration."""


class RequestValidationError(TradeSmartError):
    """Request parameters failed client-side validation."""

    def __init__(self, errors: list[dict[str, Any]]) -> None:
        self.errors = errors
        super().__init__(f"Invalid request parameters: {len(errors)} error(s)")


class SubscriptionLimitError(TradeSmartError):
    """A stream subscription limit was exceeded."""


class TransportError(TradeSmartError):
    """Base class for network-level failures."""


class NetworkError(TransportError):
    """Connection could not be established or was lost."""


class RequestTimeoutError(TransportError):
    """The request timed out."""


class OrderStatusUnknownError(TradeSmartError):
    """A state-changing request may or may not have been executed by the server.

    Raised for timeouts/disconnects after the request may have been sent and for
    ambiguous responses (non-JSON or unexpected body). ``http_status`` and ``body``
    (truncated) are set when a response was received. ``sliced_result`` is the partial result
    of ``place_sliced_order`` when one of its slices had this outcome, else ``None``.
    """

    # A class default keeps the instance state, ``str()``, ``repr()`` and pickling unchanged
    # without a sliced result; ``place_sliced_order`` may attach one after construction.
    sliced_result: SlicedOrderResult | None = None

    def __init__(
        self,
        endpoint: str,
        message: str,
        *,
        http_status: int | None = None,
        body: str | None = None,
        sliced_result: SlicedOrderResult | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.message = message
        self.http_status = http_status
        self.body = body[:_BODY_LIMIT] if body is not None else None
        if sliced_result is not None:
            self.sliced_result = sliced_result
        super().__init__(f"{endpoint}: order status unknown: {message}")


class APIError(TradeSmartError):
    """The API answered with an error (``stat`` other than ``Ok`` or HTTP error)."""

    def __init__(
        self,
        message: str,
        *,
        endpoint: str,
        http_status: int | None,
        emsg: str | None = None,
        request_time: str | None = None,
        raw: Any = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.endpoint = endpoint
        self.http_status = http_status
        self.emsg = emsg
        self.request_time = request_time
        self.raw = raw


class AuthenticationError(APIError):
    """Missing, invalid or expired credentials."""


class RateLimitError(APIError):
    """The API rate limit was exceeded."""

    def __init__(
        self,
        message: str,
        *,
        endpoint: str,
        http_status: int | None,
        emsg: str | None = None,
        request_time: str | None = None,
        raw: Any = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(
            message,
            endpoint=endpoint,
            http_status=http_status,
            emsg=emsg,
            request_time=request_time,
            raw=raw,
        )
        self.retry_after = retry_after


class NotFoundError(APIError):
    """The requested resource does not exist."""


class ServerError(APIError):
    """The server failed to process the request (HTTP 5xx or equivalent)."""


class OrderRejectedError(APIError):
    """An order request was rejected."""


class ResponseParseError(TradeSmartError):
    """The response body was not the expected JSON."""

    def __init__(
        self,
        message: str,
        *,
        endpoint: str,
        http_status: int | None,
        body: str,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.endpoint = endpoint
        self.http_status = http_status
        self.body = body[:_BODY_LIMIT]
