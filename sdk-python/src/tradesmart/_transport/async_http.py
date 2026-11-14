"""Asynchronous HTTP transport."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import TypeVar

import httpx

from tradesmart._transport.outcome import (
    SendProgress,
    is_order_call,
    log_cancelled,
    log_failure,
    log_outcome,
)
from tradesmart._transport.ratelimit import RateLimiterRegistry
from tradesmart._transport.retry import RetryPolicy
from tradesmart._transport.sync_http import (
    NOT_SENT_ERRORS,
    closed_error,
    finish_response,
    log_payload,
    log_response,
    map_transport_error,
    map_unexpected_error,
    order_timeout,
    pool_limits,
    prepare_request,
    request_headers,
)
from tradesmart.config import ClientConfig, header_user_agent
from tradesmart.endpoints.base import Call
from tradesmart.errors import TradeSmartError

T = TypeVar("T")


class AsyncTransport:
    def __init__(
        self,
        config: ClientConfig,
        token_provider: Callable[[], str],
        *,
        http_client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._config = config
        self._token_provider = token_provider
        self._owns_client = http_client is None
        self._client = (
            http_client
            if http_client is not None
            else httpx.AsyncClient(limits=pool_limits(config), follow_redirects=False)
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

    def use_time(
        self, clock: Callable[[], float], sleep: Callable[[float], Awaitable[None]]
    ) -> None:
        """Internal test hook: rebuild the rate limiters on ``clock`` and wait with ``sleep``."""
        self._sleep = sleep
        self._limiter = self._build_limiter(clock)

    def order_room_wait(self, n: int) -> float:
        """Seconds until ``n`` more PlaceOrder calls fit the per-minute window (``0.0``: now)."""
        return self._limiter.wait_for("place_order", n, 60.0)

    @property
    def http_client(self) -> httpx.AsyncClient:
        """The underlying (possibly user-injected) httpx client."""
        return self._client

    def ensure_open(self) -> None:
        """Raise ``ConfigurationError`` once closed (also when the caller closed its client)."""
        if self._closed or self._client.is_closed:
            raise closed_error()

    async def execute(self, call: Call[T]) -> T:
        if not is_order_call(call):
            self.ensure_open()
            return await self._execute(call, SendProgress())
        started = time.perf_counter()
        progress = SendProgress()
        try:
            self.ensure_open()
            result = await self._execute(call, progress)
        except TradeSmartError as exc:
            log_failure(call, exc, started, sent=progress.sent)
            raise
        except BaseException as exc:  # like the sync transport: the order may be live
            # A cancellation while sending is logged where it happens (``_send``).
            if progress.sent and not isinstance(exc, asyncio.CancelledError):
                log_cancelled(call, started)
            raise
        log_outcome(call, "accepted", started, result)
        return result

    async def _execute(self, call: Call[T], progress: SendProgress) -> T:
        spec = call.spec
        attempt = 0
        while True:
            await self._limiter.aacquire(spec.rate_group, sleep=self._sleep, endpoint=spec.name)
            try:
                return await self._attempt(call, progress)
            except TradeSmartError as exc:
                if not progress.retryable or not self._policy.should_retry(
                    attempt, exc, state_changing=spec.state_changing
                ):
                    raise
                await self._sleep(self._policy.delay(attempt, getattr(exc, "retry_after", None)))
                attempt += 1

    async def _attempt(self, call: Call[T], progress: SendProgress) -> T:
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
        response = await self._send(call, request, progress, started)
        log_response(spec, response, (time.perf_counter() - started) * 1000)
        return finish_response(spec, response)

    async def _send(
        self, call: Call[T], request: httpx.Request, progress: SendProgress, started: float
    ) -> httpx.Response:
        """Send ``request``; a failure is mapped and raised outside the handler.

        Raising outside the ``except`` leaves no ``__context__``: the httpx error holds the
        request (the bearer token in its headers, the GenAcsTok code in its body).
        """
        spec = call.spec
        error: TradeSmartError
        try:
            # never re-send a request elsewhere (also injected clients)
            return await self._client.send(request, follow_redirects=False)
        except httpx.RequestError as exc:  # TransportError, DecodingError, TooManyRedirects
            progress.sent = not isinstance(exc, NOT_SENT_ERRORS)
            error = map_transport_error(exc, spec)
        except asyncio.CancelledError:
            if is_order_call(call):  # the request may already be on the wire
                log_cancelled(call, started)
            raise
        except Exception as exc:  # e.g. an injected client's hook or transport
            progress.retryable = False  # deterministic: re-sending will not help
            if isinstance(exc, TradeSmartError) and not spec.state_changing:
                raise  # the caller's own SDK error, unchanged
            error = map_unexpected_error(exc, spec)
        raise error from None

    async def aclose(self) -> None:
        self._closed = True
        if self._owns_client:
            await self._client.aclose()
