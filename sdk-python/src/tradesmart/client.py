"""Synchronous TradeSmart client."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import time
from pathlib import Path
from types import TracebackType
from typing import Any, Literal, TypeVar, cast

import httpx

from tradesmart import _session
from tradesmart._client_core import Credentials, build_raw_call, env_kwargs
from tradesmart._contract_cache import ContractCache
from tradesmart._option_contracts import (
    ContractIndex,
    PriceLike,
    buildup_reading,
    chain_snapshot,
    check_prices,
    check_rates,
    is_contract_quote_failure,
    option_greeks_result,
    option_has_expired,
    plan_option_greeks,
    since_open_window,
)
from tradesmart._slicing import (
    DEFAULT_MAX_SLICES,
    SlicedOrderResult,
    SliceProgress,
    check_disclosed_quantity,
    check_order_rate_fit,
    check_order_room,
    check_slice_arguments,
    contract_token_and_lot,
    freeze_quantity,
    plan_slices,
)
from tradesmart._transport.sync_http import SyncTransport
from tradesmart.analytics import BuildupReading, OptionChainSnapshot
from tradesmart.auth import AccessToken
from tradesmart.config import ClientConfig
from tradesmart.endpoints.gtt import (
    acknowledged_alert_id,
    cancel_gtt_call,
    gtt_alert_types_call,
    modify_gtt_call,
    pending_gtts_call,
    place_gtt_call,
    triggered_gtts_call,
)
from tradesmart.endpoints.market import (
    TimeInput,
    eod_chart_call,
    eod_symbol,
    exchange_code,
    option_chain_call,
    quote_call,
    search_call,
    security_info_call,
    time_price_series_call,
)
from tradesmart.endpoints.oco import cancel_oco_call, modify_oco_call, place_oco_call
from tradesmart.endpoints.orders import (
    cancel_order_call,
    exit_sno_order_call,
    modify_ack_order_id,
    modify_order_call,
    order_book_call,
    order_history_call,
    order_margin_call,
    place_order_call,
    trade_book_call,
)
from tradesmart.endpoints.portfolio import holdings_call, limits_call, positions_call
from tradesmart.endpoints.user import user_details_call
from tradesmart.enums import (
    Exchange,
    GttAlertType,
    GttValidity,
    OrderType,
    Product,
    TransactionType,
    Validity,
)
from tradesmart.errors import (
    APIError,
    AuthenticationError,
    OrderStatusUnknownError,
    ResponseParseError,
    TradeSmartError,
)
from tradesmart.greeks import DEFAULT_DIVIDEND_YIELD, DEFAULT_RATE, OptionGreeks
from tradesmart.instruments import Instruments, load, vanished_error
from tradesmart.models.base import PriceInput
from tradesmart.models.gtt import Gtt, TriggeredGtt
from tradesmart.models.market import (
    Candle,
    DailyCandle,
    OptionContract,
    Quote,
    ScripMatch,
    SecurityInfo,
)
from tradesmart.models.oco import OcoLeg
from tradesmart.models.orders import Order, OrderHistoryEntry, OrderMargin, Trade
from tradesmart.models.portfolio import Holding, Limits, Position
from tradesmart.models.user import UserDetails

_T = TypeVar("_T")


class TradeSmart:
    """Blocking client for the TradeSmart API V2.

    Order methods return the order id once the OMS has acknowledged the request; an
    acknowledgement is not an execution (confirm with ``order_history()`` or stream order
    updates). A timeout, or an accepted acknowledgement that cannot be read, raises
    ``OrderStatusUnknownError``: the order may exist, so check ``orders()`` before retrying.
    """

    def __init__(
        self,
        client_id: str,
        *,
        access_token: str | AccessToken | None = None,
        app_key: str | None = None,
        secret_key: str | None = None,
        config: ClientConfig | None = None,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.client_id = client_id
        self.config = config if config is not None else ClientConfig()
        self._creds = Credentials(
            client_id,
            self.config,
            access_token=access_token,
            app_key=app_key,
            secret_key=secret_key,
        )
        self._transport = SyncTransport(self.config, self._creds.bearer, http_client=http_client)
        self._instruments = Instruments(self._transport.http_client, self.config)
        self._contracts = ContractCache()

    @classmethod
    def from_env(cls, *, config: ClientConfig | None = None) -> TradeSmart:
        """Build a client from the ``TRADESMART_*`` environment variables or the saved login.

        A missing ``TRADESMART_CLIENT_ID`` / ``TRADESMART_ACCESS_TOKEN`` is taken from the token
        file written by ``python -m tradesmart login`` (``TRADESMART_TOKEN_FILE`` overrides its
        location). Raises ``ConfigurationError`` (``AuthenticationError`` for an expired saved
        login) naming that command when no usable token is found. ``TRADESMART_ACCESS_TOKEN``
        is only used together with ``TRADESMART_CLIENT_ID`` (else ``ConfigurationError``).
        """
        config = config if config is not None else ClientConfig()
        kwargs, missing_token_reason = env_kwargs(config)
        client = cls(config=config, **kwargs)
        client._creds.missing_token_reason = missing_token_reason
        return client

    # -- login -------------------------------------------------------------------

    def login_url(self) -> str:
        """URL to open in a browser so the user can authorise the app."""
        return _session.login_url(self._creds, self.config)

    def generate_session(self, code: str) -> AccessToken:
        """Exchange an authorisation code for an access token and store it on the client."""
        call = _session.token_call(self._creds, code)
        parse_failed = False
        try:
            token = self._transport.execute(call)
        except AuthenticationError:
            raise
        except APIError as exc:
            raise _session.as_authentication_error(exc) from exc
        except OrderStatusUnknownError as exc:
            raise _session.unknown_outcome(exc) from exc
        except ResponseParseError:
            parse_failed = True
        if parse_failed:
            raise _session.parse_failure()
        self.set_access_token(token)
        return token

    def set_access_token(self, token: str | AccessToken) -> None:
        """Replace the access token used for authenticated calls."""
        self._creds.set_token(token)

    # -- account -----------------------------------------------------------------

    def profile(self) -> UserDetails:
        """Fetch basic details of the logged-in client."""
        return self._transport.execute(user_details_call(self.client_id))

    def margins(self) -> Limits:
        """Fetch funds, limits and margins."""
        return self._transport.execute(limits_call(self.client_id))

    def positions(self) -> list[Position]:
        """Fetch open positions (empty list when there are none)."""
        return self._transport.execute(positions_call(self.client_id))

    def holdings(self, product: Product | str = Product.CNC) -> list[Holding]:
        """Fetch holdings for a product (default CNC)."""
        return self._transport.execute(holdings_call(self.client_id, product))

    # -- orders ------------------------------------------------------------------

    def place_order(
        self,
        *,
        exchange: Exchange | str,
        tradingsymbol: str,
        quantity: int,
        transaction_type: TransactionType | str,
        product: Product | str,
        order_type: OrderType | str,
        price: PriceInput | None = None,
        trigger_price: PriceInput | None = None,
        validity: Validity | str = Validity.DAY,
        disclosed_quantity: int | None = None,
        amo: bool = False,
        tag: str | None = None,
        book_loss_price: PriceInput | None = None,
        book_profit_price: PriceInput | None = None,
        trail_price: PriceInput | None = None,
        market_protection: PriceInput | None = None,
        algo_id: str | None = None,
    ) -> str:
        """Place an order and return its order id (never retried; see the class docstring).

        ``algo_id`` (sent as ``algo_id``) overrides ``ClientConfig.algo_id`` for this order;
        when both are ``None`` no ``algo_id`` is sent.
        """
        call = place_order_call(
            self.client_id,
            exchange=exchange,
            tradingsymbol=tradingsymbol,
            quantity=quantity,
            transaction_type=transaction_type,
            product=product,
            order_type=order_type,
            price=price,
            trigger_price=trigger_price,
            validity=validity,
            disclosed_quantity=disclosed_quantity,
            amo=amo,
            tag=tag,
            book_loss_price=book_loss_price,
            book_profit_price=book_profit_price,
            trail_price=trail_price,
            market_protection=market_protection,
            algo_id=self.config.algo_id if algo_id is None else algo_id,
        )
        ack = self._transport.execute(call)
        return cast(str, ack.order_id)  # the ack parser guarantees a non-empty id

    def place_sliced_order(
        self,
        *,
        exchange: Exchange | str,
        tradingsymbol: str,
        quantity: int,
        transaction_type: TransactionType | str,
        product: Product | str,
        order_type: OrderType | str,
        price: PriceInput | None = None,
        trigger_price: PriceInput | None = None,
        validity: Validity | str = Validity.DAY,
        disclosed_quantity: int | None = None,
        amo: bool = False,
        tag: str | None = None,
        book_loss_price: PriceInput | None = None,
        book_profit_price: PriceInput | None = None,
        trail_price: PriceInput | None = None,
        market_protection: PriceInput | None = None,
        algo_id: str | None = None,
        max_slice_quantity: int | None = None,
        max_slices: int = DEFAULT_MAX_SLICES,
    ) -> SlicedOrderResult:
        """Place an order larger than the exchange freeze limit as slices, one after another.

        ``quantity`` and ``max_slice_quantity`` are in trade units, whole multiples of the lot
        size, on every exchange (MCX too: 2 CRUDEOIL lots of 100 are 200). The slice size comes
        from the script master's lot size and ``security_info().frzqty``; every other argument
        is sent unchanged on every slice through ``place_order`` (never retried, paced by the
        order rate limit). A quantity within one slice goes out as one order. Raises
        ``ValueError`` (or ``place_order``'s own validation error, or ``ConfigurationError``
        when ``rate_limit_mode="raise"`` or an ``order_rate_limit_max_wait`` below 1.0 would
        stop more slices than ``order_rate_limit_per_second`` part-way, or more slices than
        ``order_rate_limit_per_minute``), or ``RateLimitError`` (with ``retry_after``) when the
        current minute has no room for all the slices, before any order is sent.
        Sending stops at the first failure: a definite one is returned as
        ``SlicedOrderResult(complete=False, error=...)``; an ambiguous one raises
        ``OrderStatusUnknownError`` with ``.sliced_result`` (check ``orders()`` before sending
        anything again). An interruption (Ctrl-C, a cancellation, ...) is re-raised unchanged
        with a note listing the order ids placed and, where the exception accepts it,
        ``.sliced_result``.
        """
        check_slice_arguments(quantity, max_slice_quantity, max_slices, tradingsymbol)
        exch = exchange_code(exchange)
        row = self._contract_index(exch).instrument_row(tradingsymbol)
        token, lot_size = contract_token_and_lot(row, exch, tradingsymbol)
        info = self.security_info(exch, token)
        slices = plan_slices(
            exch,
            quantity,
            lot_size,
            freeze_quantity(info, lot_size, tradingsymbol),
            max_slice_quantity=max_slice_quantity,
            max_slices=max_slices,
        )
        check_disclosed_quantity(disclosed_quantity, slices)
        check_order_rate_fit(len(slices), self.config)
        check_order_room(self._transport.order_room_wait(len(slices)), self.config)
        order: dict[str, Any] = {
            "exchange": exchange,
            "tradingsymbol": tradingsymbol,
            "transaction_type": transaction_type,
            "product": product,
            "order_type": order_type,
            "price": price,
            "trigger_price": trigger_price,
            "validity": validity,
            "disclosed_quantity": disclosed_quantity,
            "amo": amo,
            "tag": tag,
            "book_loss_price": book_loss_price,
            "book_profit_price": book_profit_price,
            "trail_price": trail_price,
            "market_protection": market_protection,
            "algo_id": algo_id,
        }
        place_order_call(self.client_id, quantity=slices[0], **order)  # place_order's checks
        progress = SliceProgress(quantity, slices)
        for size in slices:
            try:
                progress.order_ids.append(self.place_order(quantity=size, **order))
            except OrderStatusUnknownError as error:
                progress.unknown(error)
                raise
            except TradeSmartError as error:
                return progress.stopped(error)
            except BaseException as interruption:  # Ctrl-C, cancellation, ...: never lose ids
                progress.interrupted(interruption)
                raise
        return progress.done()

    def modify_order(
        self,
        order_id: str,
        *,
        exchange: Exchange | str,
        tradingsymbol: str,
        quantity: int,
        price: PriceInput | None,
        order_type: OrderType | str,
        product: Product | str,
        trigger_price: PriceInput | None = None,
        validity: Validity | str | None = None,
        tag: str | None = None,
        book_loss_price: PriceInput | None = None,
        book_profit_price: PriceInput | None = None,
        trail_price: PriceInput | None = None,
        algo_id: str | None = None,
    ) -> str:
        """Modify an open order (never retried) and return its order id.

        A ``stat: Ok`` acknowledgement is an acceptance; the id is the ack's ``norenordno``,
        else its ``result``, else ``order_id``.
        ``algo_id`` overrides ``ClientConfig.algo_id`` for this call, as in ``place_order``.
        """
        call = modify_order_call(
            self.client_id,
            order_id=order_id,
            exchange=exchange,
            tradingsymbol=tradingsymbol,
            quantity=quantity,
            price=price,
            order_type=order_type,
            product=product,
            trigger_price=trigger_price,
            validity=validity,
            tag=tag,
            book_loss_price=book_loss_price,
            book_profit_price=book_profit_price,
            trail_price=trail_price,
            algo_id=self.config.algo_id if algo_id is None else algo_id,
        )
        ack = self._transport.execute(call)
        return modify_ack_order_id(ack, order_id)

    def cancel_order(self, order_id: str) -> str:
        """Cancel an open order and return ``order_id`` (never retried)."""
        self._transport.execute(cancel_order_call(self.client_id, order_id))
        return order_id

    def exit_order(self, order_id: str, product: Product | str) -> str:
        """Exit a cover (``CO``) or bracket (``BO``) order and return ``order_id``."""
        self._transport.execute(exit_sno_order_call(self.client_id, order_id, product))
        return order_id

    def orders(self) -> list[Order]:
        """Fetch all orders of the day (empty list when there are none)."""
        return self._transport.execute(order_book_call(self.client_id))

    def order_history(self, order_id: str) -> list[OrderHistoryEntry]:
        """Fetch the state transitions of one order (empty list when unknown)."""
        return self._transport.execute(order_history_call(self.client_id, order_id))

    def trades(self) -> list[Trade]:
        """Fetch all trades executed today (empty list when there are none)."""
        return self._transport.execute(trade_book_call(self.client_id))

    def order_margin(
        self,
        *,
        exchange: Exchange | str,
        tradingsymbol: str,
        quantity: int,
        price: PriceInput,
        product: Product | str,
        transaction_type: TransactionType | str,
        order_type: OrderType | str,
        trigger_price: PriceInput | None = None,
        book_loss_price: PriceInput | None = None,
        order_id: str | None = None,
        sno_number: str | None = None,
    ) -> OrderMargin:
        """Calculate the margin required for an order (read-only; nothing is placed)."""
        call = order_margin_call(
            self.client_id,
            exchange=exchange,
            tradingsymbol=tradingsymbol,
            quantity=quantity,
            price=price,
            product=product,
            transaction_type=transaction_type,
            order_type=order_type,
            trigger_price=trigger_price,
            book_loss_price=book_loss_price,
            order_id=order_id,
            sno_number=sno_number,
        )
        return self._transport.execute(call)

    # -- GTT orders ----------------------------------------------------------------

    def place_gtt(
        self,
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
    ) -> str:
        """Place a GTT and return its alert id (never retried).

        The order (``quantity`` ... ``disclosed_quantity``, as in ``place_order``; ``quantity``
        in trade units, a whole multiple of the lot size, on MCX too) is held on the server and
        sent when the value ``alert_type`` watches crosses ``trigger_value``. A GTT whose
        condition is already true when it is placed fires at once. ``alert_validity`` ``GTT``
        keeps it until it triggers or is cancelled (at most 365 days or the contract expiry,
        whichever is earlier); ``DAY`` ends it with the trading day. ``tag`` is required: after
        ``OrderStatusUnknownError`` find the GTT by its tag in ``pending_gtts()`` before placing
        anything again. Products CNC, NRML and CO only.
        """
        call = place_gtt_call(
            self.client_id,
            exchange=exchange,
            tradingsymbol=tradingsymbol,
            alert_type=alert_type,
            trigger_value=trigger_value,
            quantity=quantity,
            transaction_type=transaction_type,
            product=product,
            order_type=order_type,
            tag=tag,
            price=price,
            trigger_price=trigger_price,
            validity=validity,
            alert_validity=alert_validity,
            disclosed_quantity=disclosed_quantity,
        )
        return self._transport.execute(call)

    def modify_gtt(
        self,
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
    ) -> str:
        """Modify a pending GTT (never retried) and return the alert id the server acknowledged.

        Send every field again, also the unchanged ones; ``alert_type`` cannot be changed
        (send the original; the server decides). ``validity`` and ``alert_validity`` have no
        defaults: both are sent on every modify, so pass the GTT's current values to keep them.
        A modify that makes the condition already true fires the GTT at once (a real order).
        The returned id is the server's ``al_id``, as returned; it may differ from
        ``alert_id`` (then a DEBUG line on the ``tradesmart`` logger says so).
        """
        call = modify_gtt_call(
            self.client_id,
            alert_id,
            exchange=exchange,
            tradingsymbol=tradingsymbol,
            alert_type=alert_type,
            trigger_value=trigger_value,
            quantity=quantity,
            transaction_type=transaction_type,
            product=product,
            order_type=order_type,
            tag=tag,
            price=price,
            trigger_price=trigger_price,
            validity=validity,
            alert_validity=alert_validity,
            disclosed_quantity=disclosed_quantity,
        )
        returned = self._transport.execute(call)
        return acknowledged_alert_id("ModifyGTTOrder", alert_id, returned)

    def cancel_gtt(self, alert_id: str) -> str:
        """Cancel a pending (not yet triggered) GTT (never retried).

        Returns the server's acknowledged ``al_id``, as returned; it may differ from
        ``alert_id`` (then a DEBUG line on the ``tradesmart`` logger says so).
        """
        returned = self._transport.execute(cancel_gtt_call(self.client_id, alert_id))
        return acknowledged_alert_id("CancelGTTOrder", alert_id, returned)

    # -- OCO orders ----------------------------------------------------------------

    def place_oco(
        self,
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
    ) -> str:
        """Place an OCO (one cancels the other) and return its alert id (never retried).

        A stop-loss order and a target order wait on the server, each behind its own trigger
        value (``stop_loss`` and ``target`` are an ``OcoLeg`` each); when one trigger is reached
        its order is sent and the other is cancelled. A BUY OCO needs the stop-loss trigger above
        the target trigger, a SELL OCO below it (never equal). A trigger already crossed when the
        OCO is placed may fire at once. ``alert_validity`` ``GTT`` keeps it until one side
        triggers or it is cancelled, ``DAY`` ends it with the trading day. After
        ``OrderStatusUnknownError`` check ``pending_gtts()`` before placing anything again.
        """
        call = place_oco_call(
            self.client_id,
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
        )
        return self._transport.execute(call)

    def modify_oco(
        self,
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
    ) -> str:
        """Modify a pending OCO (never retried) and return the alert id the server acknowledged.

        Send the whole OCO again, also the unchanged parts. ``validity`` and ``alert_validity``
        have no defaults: both are sent on every modify. The side rule of ``place_oco`` applies,
        and a modify that makes a trigger already crossed may fire it at once. The returned id
        is the server's ``al_id``, as returned (a DEBUG line says so when it differs).
        """
        call = modify_oco_call(
            self.client_id,
            alert_id,
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
        )
        returned = self._transport.execute(call)
        return acknowledged_alert_id("ModifyOCOOrder", alert_id, returned)

    def cancel_oco(self, alert_id: str) -> str:
        """Cancel a pending (not yet triggered) OCO (never retried).

        Returns the server's acknowledged ``al_id``, as returned.
        """
        returned = self._transport.execute(cancel_oco_call(self.client_id, alert_id))
        return acknowledged_alert_id("CancelOCOOrder", alert_id, returned)

    def pending_gtts(self) -> list[Gtt]:
        """Fetch the GTTs that have not triggered yet (empty list when there are none)."""
        return self._transport.execute(pending_gtts_call(self.client_id))

    def triggered_gtts(self) -> list[TriggeredGtt]:
        """Fetch the GTTs that triggered (their orders carry ``Order.parent_id``)."""
        return self._transport.execute(triggered_gtts_call(self.client_id))

    def gtt_alert_types(self) -> list[str]:
        """The GTT alert families enabled for the account (e.g. ``["ATP", "LTP"]``)."""
        return self._transport.execute(gtt_alert_types_call(self.client_id))

    # -- market data -------------------------------------------------------------

    def search_instruments(self, text: str) -> list[ScripMatch]:
        """Search instruments by text (empty list when nothing matches)."""
        return self._transport.execute(search_call(self.client_id, text))

    def quote(self, exchange: Exchange | str, token: str) -> Quote:
        """Fetch a snapshot quote with market depth."""
        return self._transport.execute(quote_call(self.client_id, exchange, token))

    def security_info(self, exchange: Exchange | str, token: str) -> SecurityInfo:
        """Fetch the security's properties."""
        return self._transport.execute(security_info_call(self.client_id, exchange, token))

    def option_chain(
        self,
        exchange: Exchange | str,
        tradingsymbol: str,
        strike_price: PriceInput,
        count: int,
    ) -> list[OptionContract]:
        """Fetch ``count`` strikes either side of ``strike_price`` (CE and PE)."""
        call = option_chain_call(self.client_id, exchange, tradingsymbol, strike_price, count)
        return self._transport.execute(call)

    def historical_data(
        self,
        exchange: Exchange | str,
        token: str,
        from_date: TimeInput | None = None,
        to_date: TimeInput | None = None,
        interval: int | str | None = None,
    ) -> list[Candle]:
        """Fetch intraday candles (newest first); ``interval`` in minutes, default 1."""
        call = time_price_series_call(self.client_id, exchange, token, from_date, to_date, interval)
        return self._transport.execute(call)

    def daily_history(
        self,
        exchange: Exchange | str,
        tradingsymbol: str,
        from_date: TimeInput,
        to_date: TimeInput,
    ) -> list[DailyCandle]:
        """Fetch daily candles for ``tradingsymbol`` (e.g. ``"SBIN-EQ"``) on ``exchange``."""
        call = eod_chart_call(eod_symbol(exchange, tradingsymbol), from_date, to_date)
        return self._transport.execute(call)

    # -- option analytics (estimates computed by the SDK) -------------------------

    def option_greeks(
        self,
        exchange: Exchange | str,
        tradingsymbol: str,
        *,
        rate: float = DEFAULT_RATE,
        dividend_yield: float = DEFAULT_DIVIDEND_YIELD,
        option_price: PriceLike | None = None,
        underlying_price: PriceLike | None = None,
        spot_price: PriceLike | None = None,
        expiry_time: time | None = None,
    ) -> OptionGreeks:
        """Implied volatility and Greeks of an option (estimates; see ``tradesmart.greeks``).

        Black-76 on the underlying future (selected from the script master, or
        ``underlying_price`` as the future's price); Black-Scholes-Merton with
        ``dividend_yield`` when ``spot_price`` is given. A selected future of another expiry is
        brought to the option's expiry (not on MCX) as ``F · e^{−(rate − dividend_yield)·(Tf −
        To)}``: pass the index's dividend yield, or for a currency option the foreign interest
        rate, as ``dividend_yield`` (default 0). Prices not supplied are fetched with
        ``quote()`` (at most two calls). Raises ``ValueError`` for an unknown option, when no
        underlying future exists, or for a price that is not a number (a ``bool``, an int too
        large for a float), before any request; a ``GetQuotes`` ``Not_Ok`` for the option or
        the future raises ``APIError``. A quote with no last price gives ``status="no_price"``
        (no exception). ``expiry_time`` (naive, IST) overrides the exchange's expiry close.

        The parsed script master is kept per client and exchange, and parsed again only when
        the file changes (the daily refresh); ``instruments()`` still returns a fresh list.

        After the expiry instant the option itself is not quoted: the result is
        ``status="expired"`` (intrinsic value from the underlying), and ``option_price`` is the
        one you passed or ``None``. If the future's quote is then refused for that contract
        (``Not_Ok``, not found, unreadable reply), the result is ``status="no_price"``.
        """
        rate, dividend_yield = check_rates(rate, dividend_yield)
        check_prices(
            option_price=option_price, underlying_price=underlying_price, spot_price=spot_price
        )
        exch = exchange_code(exchange)
        plan = plan_option_greeks(
            self._contract_index(exch),
            exch,
            tradingsymbol,
            underlying_price=underlying_price,
            spot_price=spot_price,
        )
        expired = option_has_expired(plan, expiry_time)
        if plan.future is not None:
            try:
                underlying_price = self.quote(exch, plan.future.token).last_price
            except TradeSmartError as error:
                if not (expired and is_contract_quote_failure(error)):
                    raise
                underlying_price = None
        if option_price is None and not expired:
            option_price = self.quote(exch, plan.option.token).last_price
        return option_greeks_result(
            plan,
            option_price=option_price,
            underlying_price=spot_price if spot_price is not None else underlying_price,
            rate=rate,
            dividend_yield=dividend_yield,
            expiry_time=expiry_time,
        )

    def option_chain_snapshot(
        self,
        exchange: Exchange | str,
        tradingsymbol: str,
        strike_price: PriceInput,
        count: int = 25,
    ) -> OptionChainSnapshot:
        """``option_chain()`` plus one ``quote()`` per contract, one after another.

        A contract whose quote fails for that contract alone (``Not_Ok``, not found, unreadable
        reply) is listed in ``missing``; session, rate-limit, server, configuration and network
        errors raise. Rows are sorted by strike, then CE before PE.
        """
        exch = exchange_code(exchange)
        contracts = self.option_chain(exch, tradingsymbol, strike_price, count)
        quoted = [(c, self._quote_or_none(exch, c.token)) for c in contracts]
        return chain_snapshot(exch, tradingsymbol, quoted)

    def buildup(self, exchange: Exchange | str, token: str) -> BuildupReading:
        """Buildup: price change since the previous close (``lp − c``); OI change since today's
        open (sum of ``intoi``).

        REST has no previous-day open interest; the stream's ``poi`` gives the full-day view
        (``tradesmart.analytics.buildup_from_snapshot``).
        """
        quote = self.quote(exchange, token)
        start, end = since_open_window()
        candles = self.historical_data(exchange, token, from_date=start, to_date=end, interval=1)
        return buildup_reading(quote, candles)

    def _quote_or_none(self, exchange: str, token: str | None) -> Quote | None:
        if not token:
            return None
        try:
            return self.quote(exchange, token)
        except TradeSmartError as error:
            if is_contract_quote_failure(error):
                return None
            raise

    def _contract_index(self, exchange: str) -> ContractIndex:
        """Script-master lookups of the current file (parsed once per file)."""
        self._transport.ensure_open()
        return self._read_script_master(exchange, lambda p: self._contracts.get(exchange, p))

    def _read_script_master(self, exchange: Exchange | str, read: Callable[[Path], _T]) -> _T:
        """Download (or reuse) the script master and ``read`` it.

        A file removed between the download and the read (a cleanup job, another process) is
        downloaded once more; removed again → ``ResponseParseError``.
        """
        path = self._instruments.download(exchange)
        try:
            return read(path)
        except FileNotFoundError:
            path = self._instruments.download(exchange)
        try:
            return read(path)
        except FileNotFoundError:
            raise vanished_error(path) from None

    # -- instruments -------------------------------------------------------------

    def instruments(self, exchange: Exchange | str) -> list[dict[str, str]]:
        """Script-master rows for ``exchange`` (downloaded at most once per daily refresh)."""
        self._transport.ensure_open()
        return self._read_script_master(exchange, lambda path: load(path))

    def download_instruments(
        self,
        exchange: Exchange | str,
        dest_dir: str | Path | None = None,
        *,
        force: bool = False,
    ) -> Path:
        """Download ``<EXCH>_symbols.txt`` (default dir ``config.instruments_dir``); its path."""
        self._transport.ensure_open()
        return self._instruments.download(exchange, dest_dir, force=force)

    # -- low level ---------------------------------------------------------------

    def request(
        self,
        endpoint: str,
        payload: Mapping[str, Any],
        *,
        state_changing: bool,
        expect: Literal["object", "list"] = "object",
    ) -> Any:
        """Low-level call to any endpoint; returns the parsed JSON body."""
        call = build_raw_call(
            self.client_id, endpoint, payload, state_changing=state_changing, expect=expect
        )
        return self._transport.execute(call)

    def close(self) -> None:
        """Release connections; later calls raise ``ConfigurationError`` (nothing is sent)."""
        self._transport.close()

    def __enter__(self) -> TradeSmart:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"TradeSmart(client_id={self.client_id!r})"
