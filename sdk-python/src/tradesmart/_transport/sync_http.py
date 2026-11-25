"""Blocking HTTP transport."""

from __future__ import annotations

import contextlib
import json
import logging
import math
import re
import time
from collections.abc import Callable, Mapping
from typing import Any, TypeVar

import httpx

from tradesmart._logging import get_logger, redact
from tradesmart._transport.codec import (
    TOO_DEEP,
    check_hint,
    encode_request_body,
    parse_response,
)
from tradesmart._transport.outcome import (
    SendProgress,
    is_order_call,
    log_cancelled,
    log_failure,
    log_outcome,
)
from tradesmart._transport.ratelimit import RateLimiterRegistry
from tradesmart._transport.retry import RetryPolicy
from tradesmart.config import ClientConfig, header_user_agent
from tradesmart.endpoints.base import Call, EndpointSpec
from tradesmart.errors import (
    ConfigurationError,
    NetworkError,
    OrderStatusUnknownError,
    RateLimitError,
    RequestTimeoutError,
    ResponseParseError,
    TradeSmartError,
)
from tradesmart.models.base import WireValue

T = TypeVar("T")
_log = get_logger("transport")

CLOSED_MESSAGE = "client is closed; create a new client"


def closed_error() -> ConfigurationError:
    """Definitive (nothing was sent) and never retried."""
    return ConfigurationError(CLOSED_MESSAGE)


# Connect-phase failures: they prove nothing reached the server.
NOT_SENT_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)


def map_transport_error(exc: httpx.RequestError, spec: EndpointSpec[Any]) -> TradeSmartError:
    """Translate an httpx failure; ambiguous failures on state-changing calls are 'unknown'.

    Only failures that prove nothing was sent (connect error/timeout, pool timeout) are
    definitive for state-changing calls; everything else (read/write timeouts, protocol
    errors, ``DecodingError``, ``TooManyRedirects`` ...) is ``OrderStatusUnknownError``.
    """
    if isinstance(exc, httpx.ConnectTimeout):
        return RequestTimeoutError(f"{spec.name}: connect timed out")
    if isinstance(exc, httpx.ConnectError):
        return NetworkError(f"{spec.name}: could not connect ({type(exc).__name__})")
    if spec.state_changing and not isinstance(exc, httpx.PoolTimeout):
        return OrderStatusUnknownError(
            spec.name,
            f"{type(exc).__name__} after the request may have been sent; "
            f"check {check_hint(spec.name)} before retrying",
        )
    if isinstance(exc, httpx.TimeoutException):
        return RequestTimeoutError(f"{spec.name}: request timed out")
    return NetworkError(f"{spec.name}: network error ({type(exc).__name__})")


def map_unexpected_error(exc: Exception, spec: EndpointSpec[Any]) -> TradeSmartError:
    """Translate any other exception raised while sending (e.g. from an injected client's
    event hook or transport). Only the type name is kept: ``str(exc)`` may hold a URL or headers.
    """
    name = type(exc).__name__
    if spec.state_changing:
        return OrderStatusUnknownError(
            spec.name, f"{name} during the request; check {check_hint(spec.name)} before retrying"
        )
    return NetworkError(f"{spec.name}: unexpected error ({name})")


def pool_limits(config: ClientConfig) -> httpx.Limits:
    """Connection-pool limits for SDK-created httpx clients."""
    return httpx.Limits(
        max_connections=config.max_connections,
        max_keepalive_connections=config.max_keepalive_connections,
        keepalive_expiry=config.keepalive_expiry,
    )


def order_timeout(config: ClientConfig) -> httpx.Timeout:
    """Timeout for state-changing calls: wait at most ``connect_timeout`` for a pool slot.

    A ``PoolTimeout`` proves nothing was sent, so an order fails fast and definitively
    instead of queueing for the full read timeout behind other requests.
    """
    return httpx.Timeout(
        config.read_timeout, connect=config.connect_timeout, pool=config.connect_timeout
    )


def apply_retry_after(exc: TradeSmartError, response: httpx.Response) -> None:
    """Copy a finite numeric ``Retry-After`` header onto a ``RateLimitError``.

    ``nan`` and ``inf`` parse as floats but are ignored, like an HTTP date.
    """
    if isinstance(exc, RateLimitError) and response.status_code == 429:
        with contextlib.suppress(KeyError, ValueError):
            value = float(response.headers["Retry-After"])
            if math.isfinite(value):
                exc.retry_after = value


def request_headers(
    user_agent: str, spec: EndpointSpec[Any], token_provider: Callable[[], str]
) -> dict[str, str]:
    """``User-Agent`` and, for authenticated calls, the bearer token (may raise
    ``AuthenticationError`` when no usable token is set: nothing is sent)."""
    headers = {"User-Agent": user_agent}
    if spec.authenticated:
        headers["Authorization"] = f"Bearer {token_provider()}"
    return headers


# Bytes h11 refuses in a header value at send time (CR, LF, NUL and other controls; tab is
# allowed): checked when the request is built so the failure is a definitive "not sent".
_ILLEGAL_HEADER_BYTES = re.compile(rb"[\x00-\x08\x0a-\x1f\x7f]")


def prepare_request(
    client: httpx.Client | httpx.AsyncClient,
    url: str,
    spec: EndpointSpec[Any],
    payload: Mapping[str, WireValue],
    headers: Mapping[str, str],
    timeout: httpx.Timeout,
) -> httpx.Request:
    """Build the request (body and headers encoded) before anything is sent.

    Raises ``RequestValidationError`` for a payload that is not valid UTF-8 and
    ``ConfigurationError`` for headers that cannot be sent (a non-ASCII or control
    character in the token, ``user_agent`` or the ``http_client``'s own headers). Both are
    definitive: nothing was sent. Values are never echoed and nothing is chained.
    """
    content = encode_request_body(payload)
    request: httpx.Request | None = None
    problem = "a header value is not printable ASCII"
    try:
        request = client.build_request(
            "POST", url, content=content, headers=headers, timeout=timeout
        )
    except Exception as exc:  # UnicodeEncodeError for a non-ASCII header value, ...
        problem = type(exc).__name__
    if request is not None and not any(
        _ILLEGAL_HEADER_BYTES.search(name) or _ILLEGAL_HEADER_BYTES.search(value)
        for name, value in request.headers.raw
    ):
        return request
    raise ConfigurationError(
        f"{spec.name}: the request could not be built ({problem}); check the access token, "
        "user_agent and the http_client's headers; nothing was sent"
    )


def too_deep_error(spec: EndpointSpec[Any], status_code: int) -> TradeSmartError:
    """A reply nested too deeply to parse or redact (``RecursionError``)."""
    what = f"response is nested too deeply to parse (HTTP {status_code})"
    if spec.state_changing:
        return OrderStatusUnknownError(
            spec.name,
            f"{what}; the request may have been executed, "
            f"check {check_hint(spec.name)} before retrying",
            http_status=status_code,
            body=TOO_DEEP,
        )
    return ResponseParseError(
        f"{spec.name}: {what}", endpoint=spec.name, http_status=status_code, body=TOO_DEEP
    )


def finish_response(spec: EndpointSpec[T], response: httpx.Response) -> T:
    """Classify and parse ``response`` (the body is parsed whatever the HTTP status)."""
    try:
        try:
            body = parse_response(
                endpoint=spec.name,
                status_code=response.status_code,
                text=response.text,
                expect=spec.expect,
                allow_empty=spec.allow_empty,
                state_changing=spec.state_changing,
                success_stats=spec.success_stats,
                requires_ack_id=spec.requires_ack_id,
                stats_ignore_case=spec.stats_ignore_case,
                failure_stats=spec.failure_stats,
            )
        except TradeSmartError as exc:
            apply_retry_after(exc, response)
            raise
        return spec.parse(body)
    except RecursionError:
        pass  # raised below, outside the handler, so nothing is chained
    raise too_deep_error(spec, response.status_code) from None


def log_response(spec: EndpointSpec[Any], response: httpx.Response, ms: float) -> None:
    if not _log.isEnabledFor(logging.DEBUG):
        return
    stat = None
    with contextlib.suppress(ValueError, RecursionError):
        data = json.loads(response.text)
        if isinstance(data, dict):
            stat = data.get("stat")
    _log.debug("POST %s -> HTTP %s stat=%s in %.0f ms", spec.path, response.status_code, stat, ms)


def log_payload(
    config: ClientConfig, spec: EndpointSpec[Any], payload: Mapping[str, WireValue]
) -> None:
    if config.log_payloads:
        _log.debug("POST %s payload=%s", spec.path, redact(payload))


class SyncTransport:
    def __init__(
        self,
        config: ClientConfig,
        token_provider: Callable[[], str],
        *,
        http_client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._config = config
        self._token_provider = token_provider
        self._owns_client = http_client is None
        self._client = (
            http_client
            if http_client is not None
            else httpx.Client(limits=pool_limits(config), follow_redirects=False)
        )
        self._sleep = sleep
        self._limiter = self._build_limiter(time.monotonic)
        self._policy = RetryPolicy(config.max_retries, config.backoff_base, config.backoff_cap)
        self._timeout = httpx.Timeout(config.read_timeout, connect=config.connect_timeout)
        self._order_timeout = order_timeout(config)
        self._user_agent = header_user_agent(config)
        self._closed = False

    def _build_limiter(self, clock: Callable[[], float]) -> RateLimiterRegistry:
        config = self._config
        return RateLimiterRegistry(
            config.rate_limit_mode,
            max_wait={"place_order": config.order_rate_limit_max_wait},
            limits={
                "place_order": (
                    (config.order_rate_limit_per_second, 1.0),
                    (config.order_rate_limit_per_minute, 60.0),
                )
            },
            clock=clock,
        )

    def use_time(self, clock: Callable[[], float], sleep: Callable[[float], None]) -> None:
        """Internal test hook: rebuild the rate limiters on ``clock`` and wait with ``sleep``."""
        self._sleep = sleep
        self._limiter = self._build_limiter(clock)

    def order_room_wait(self, n: int) -> float:
        """Seconds until ``n`` more PlaceOrder calls fit the per-minute window (``0.0``: now)."""
        return self._limiter.wait_for("place_order", n, 60.0)

    @property
    def http_client(self) -> httpx.Client:
        """The underlying (possibly user-injected) httpx client."""
        return self._client

    def ensure_open(self) -> None:
        """Raise ``ConfigurationError`` once closed (also when the caller closed its client)."""
        if self._closed or self._client.is_closed:
            raise closed_error()

    def execute(self, call: Call[T]) -> T:
        if not is_order_call(call):
            self.ensure_open()
            return self._execute(call, SendProgress())
        started = time.perf_counter()
        progress = SendProgress()
        try:
            self.ensure_open()
            result = self._execute(call, progress)
        except TradeSmartError as exc:
            log_failure(call, exc, started, sent=progress.sent)
            raise
        except BaseException:  # Ctrl-C, SystemExit: the order may be live; re-raised unchanged
            if progress.sent:
                log_cancelled(call, started)
            raise
        log_outcome(call, "accepted", started, result)
        return result

    def _execute(self, call: Call[T], progress: SendProgress) -> T:
        spec = call.spec
        attempt = 0
        while True:
            self._limiter.acquire(spec.rate_group, sleep=self._sleep, endpoint=spec.name)
            try:
                return self._attempt(call, progress)
            except TradeSmartError as exc:
                if not progress.retryable or not self._policy.should_retry(
                    attempt, exc, state_changing=spec.state_changing
                ):
                    raise
                self._sleep(self._policy.delay(attempt, getattr(exc, "retry_after", None)))
                attempt += 1

    def _attempt(self, call: Call[T], progress: SendProgress) -> T:
        spec = call.spec
        log_payload(self._config, spec, call.payload)
        # Built (body and headers encoded) before it is marked as sent: a failure here is
        # definitive (nothing was sent) and never retried.
        request = prepare_request(
            self._client,
            self._config.base_url + spec.path,
            spec,
            call.payload,
            request_headers(self._user_agent, spec, self._token_provider),
            self._order_timeout if spec.state_changing else self._timeout,
        )
        started = time.perf_counter()
        progress.sent = True
        response = self._send(request, spec, progress)
        log_response(spec, response, (time.perf_counter() - started) * 1000)
        return finish_response(spec, response)

    def _send(
        self, request: httpx.Request, spec: EndpointSpec[Any], progress: SendProgress
    ) -> httpx.Response:
        """Send ``request``; a failure is mapped and raised outside the handler.

        Raising outside the ``except`` leaves no ``__context__``: the httpx error holds the
        request (the bearer token in its headers, the GenAcsTok code in its body).
        """
        error: TradeSmartError
        try:
            # never re-send a request elsewhere (also injected clients)
            return self._client.send(request, follow_redirects=False)
        except httpx.RequestError as exc:  # TransportError, DecodingError, TooManyRedirects
            progress.sent = not isinstance(exc, NOT_SENT_ERRORS)
            error = map_transport_error(exc, spec)
        except Exception as exc:  # e.g. an injected client's hook or transport
            progress.retryable = False  # deterministic: re-sending will not help
            if isinstance(exc, TradeSmartError) and not spec.state_changing:
                raise  # the caller's own SDK error, unchanged
            error = map_unexpected_error(exc, spec)
        raise error from None

    def close(self) -> None:
        self._closed = True
        if self._owns_client:
            self._client.close()
