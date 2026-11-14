"""Sliced orders: split a quantity into slices that each fit the exchange freeze limit.

Pure functions (no I/O). The clients' ``place_sliced_order`` resolves the contract's lot size
and freeze quantity, plans the slices here and sends them one after another
(``specifications/sdk-behaviour.md`` section 5.7).

Quantities are in trade units on every exchange, MCX included: a whole multiple of the lot
size (F21). The freeze quantity (``GetSecurityInfo`` ``frzqty``) and the lot size are in the
same units.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

from tradesmart._logging import get_logger
from tradesmart._transport.ratelimit import describe_window

# A runtime import (not TYPE_CHECKING): typing.get_type_hints(SlicedOrderResult) must resolve
# ``error``. tradesmart.errors imports this module only under TYPE_CHECKING, so no cycle.
from tradesmart.errors import (
    ConfigurationError,
    OrderStatusUnknownError,
    RateLimitError,
    TradeSmartError,
)

if TYPE_CHECKING:
    from tradesmart.config import ClientConfig
    from tradesmart.models.market import SecurityInfo

__all__ = ["DEFAULT_MAX_SLICES", "SlicedOrderResult", "max_slice_size", "plan_slices"]

DEFAULT_MAX_SLICES = 25

_log = get_logger("orders")


@dataclass(frozen=True)
class SlicedOrderResult:
    """`place_sliced_order()` result: the planned slices and what was placed."""

    requested_quantity: int
    slice_quantities: tuple[int, ...]
    order_ids: tuple[str, ...]
    placed_quantity: int
    unsent_quantity: int
    complete: bool
    error: TradeSmartError | None


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _require_positive_int(value: Any, name: str) -> int:
    if not _is_int(value) or value < 1:
        raise ValueError(f"{name} must be an int of at least 1, got {value!r}")
    return int(value)


def _check_exchange(exchange: str) -> None:
    if not isinstance(exchange, str):
        raise ValueError(f"exchange must be a str, got {exchange!r}")


def max_slice_size(
    exchange: str,
    lot_size: int,
    freeze_quantity: int | None,
    max_slice_quantity: int | None,
) -> int:
    """The largest quantity one slice may carry, in trade units (MCX included).

    From the freeze quantity: ``floor(frzqty / lot) * lot`` units (a slice may equal
    ``frzqty``). ``max_slice_quantity`` lowers it (never raises it) and must itself be a whole
    number of lots. A freeze quantity of ``None`` or ``<= 0`` is unknown: ``max_slice_quantity``
    is then required. Raises ``ValueError`` for invalid arguments or a maximum below one lot.

    ``exchange`` must be a ``str`` (else ``ValueError``) but no longer changes the sizing: the
    rule is the same on every exchange, MCX included.
    """
    _check_exchange(exchange)
    if not _is_int(lot_size) or lot_size < 1:
        raise ValueError(f"lot size must be an int of at least 1, got {lot_size!r}")
    if freeze_quantity is not None and not _is_int(freeze_quantity):
        raise ValueError(f"freeze quantity must be an int or None, got {freeze_quantity!r}")
    cap = (
        None
        if max_slice_quantity is None
        else _require_positive_int(max_slice_quantity, "max_slice_quantity")
    )
    if cap is not None and cap % lot_size:
        raise ValueError(
            f"max_slice_quantity {cap} is not a whole number of lots (lot size {lot_size})"
        )
    if freeze_quantity is None or freeze_quantity <= 0:
        if cap is None:
            raise ValueError("freeze quantity unknown for this contract: pass max_slice_quantity")
        return cap
    lots = freeze_quantity // lot_size
    if lots < 1:
        raise ValueError(
            f"freeze quantity {freeze_quantity} is below one lot (lot size {lot_size})"
        )
    largest = lots * lot_size
    return largest if cap is None else min(largest, cap)


def plan_slices(
    exchange: str,
    quantity: int,
    lot_size: int,
    freeze_quantity: int | None,
    *,
    max_slice_quantity: int | None = None,
    max_slices: int = DEFAULT_MAX_SLICES,
) -> tuple[int, ...]:
    """Split ``quantity`` into slices: as many full slices as fit, then the remainder.

    ``quantity`` is in trade units on every exchange, MCX included, and must be a whole number
    of lots. Raises ``ValueError`` when an argument is invalid, when the slice size cannot be
    found (:func:`max_slice_size`) or when more than ``max_slices`` slices are needed.
    ``exchange`` is validated (a ``str``) but does not change the sizing.
    """
    quantity = _require_positive_int(quantity, "quantity")
    max_slices = _require_positive_int(max_slices, "max_slices")
    size = max_slice_size(exchange, lot_size, freeze_quantity, max_slice_quantity)
    if quantity % lot_size:
        raise ValueError(f"quantity {quantity} is not a whole number of lots (lot size {lot_size})")
    full, remainder = divmod(quantity, size)
    count = full + (1 if remainder else 0)
    if count > max_slices:
        raise ValueError(
            f"quantity {quantity} needs {count} slices of at most {size}, more than "
            f"max_slices={max_slices}; pass max_slices={count} or more to allow it"
        )
    return (size,) * full + ((remainder,) if remainder else ())


# --------------------------------------------------------------------------- client assembly
# Non-I/O steps shared by ``TradeSmart.place_sliced_order`` and ``AsyncTradeSmart``'s: each
# client resolves the contract and sends the slices; these functions check and account.


def check_slice_arguments(
    quantity: Any, max_slice_quantity: Any, max_slices: Any, tradingsymbol: Any
) -> None:
    """Refuse bad sizes and a non-string trading symbol before any request (``ValueError``)."""
    _require_positive_int(quantity, "quantity")
    _require_positive_int(max_slices, "max_slices")
    if max_slice_quantity is not None:
        _require_positive_int(max_slice_quantity, "max_slice_quantity")
    if not isinstance(tradingsymbol, str) or not tradingsymbol.strip():
        raise ValueError(f"tradingsymbol must be a non-empty str, got {tradingsymbol!r}")


def contract_token_and_lot(
    row: Mapping[str, str] | None, exchange: str, tradingsymbol: str
) -> tuple[str, int]:
    """The script-master row's ``Token`` and ``LotSize``; ``ValueError`` when unusable."""
    if row is None:
        raise ValueError(f"{tradingsymbol.strip()} not found in the {exchange} script master")
    token = row.get("Token", "").strip()
    text = row.get("LotSize", "").strip()
    try:
        number = Decimal(text)
    except InvalidOperation:
        number = Decimal(0)
    if not token or not number.is_finite() or number < 1 or number != number.to_integral_value():
        raise ValueError(
            f"{tradingsymbol.strip()} has no usable token or lot size in the {exchange} "
            f"script master (LotSize {text!r})"
        )
    return token, int(number)


def freeze_quantity(info: SecurityInfo, lot_size: int, tradingsymbol: str) -> int | None:
    """``frzqty`` of ``security_info()`` (``None`` when blank; a ``frzqty`` that is not an
    integer already failed the ``security_info()`` read with ``ResponseParseError``).

    Its lot size (``ls``), when present, must equal the script master's (``ValueError``).
    """
    if info.lot_size is not None and info.lot_size != lot_size:
        raise ValueError(
            f"lot size of {tradingsymbol.strip()} differs: security_info() says {info.lot_size}, "
            f"the script master {lot_size} (stale data; refresh with "
            "download_instruments(force=True))"
        )
    return info.frzqty


def check_disclosed_quantity(disclosed_quantity: Any, slices: tuple[int, ...]) -> None:
    """``disclosed_quantity`` may not exceed the smallest slice (``ValueError``)."""
    if _is_int(disclosed_quantity) and disclosed_quantity > min(slices):
        raise ValueError(
            f"disclosed_quantity {disclosed_quantity} is larger than the smallest slice "
            f"{min(slices)}"
        )


def check_order_rate_fit(slice_count: int, config: ClientConfig) -> None:
    """Refuse (``ConfigurationError``, nothing sent) slices the client order limiter would stop.

    More slices than ``order_rate_limit_per_minute`` could never go out within one minute and
    are always refused. More slices than ``order_rate_limit_per_second`` need a wait of up to
    one second for the next window: ``rate_limit_mode="raise"`` never waits, and an
    ``order_rate_limit_max_wait`` below 1.0 cannot wait that long, so the call would always
    stop part-way.
    """
    per_minute = config.order_rate_limit_per_minute
    if slice_count > per_minute:
        raise ConfigurationError(
            f"place_sliced_order: {slice_count} slices are more than "
            f"order_rate_limit_per_minute={per_minute}, so the client-side rate limit could "
            "never send them all within one minute; nothing was sent. Raise "
            "order_rate_limit_per_minute (at most 600) or send fewer slices"
        )
    per_second = config.order_rate_limit_per_second
    if slice_count <= per_second:
        return
    if config.rate_limit_mode == "raise":
        reason = 'rate_limit_mode="raise" never waits for the next second'
    elif config.order_rate_limit_max_wait < 1.0:
        reason = (
            f"order_rate_limit_max_wait={config.order_rate_limit_max_wait} is below the "
            "1 second window"
        )
    else:
        return
    raise ConfigurationError(
        f"place_sliced_order: {slice_count} slices are more than "
        f"order_rate_limit_per_second={per_second} and {reason}, so the client-side rate "
        f"limit would stop the order after {per_second} slices; nothing was sent. Use "
        'rate_limit_mode="wait" with order_rate_limit_max_wait of at least 1.0, or send '
        "fewer slices"
    )


def check_order_room(wait: float, config: ClientConfig) -> None:
    """Refuse (``RateLimitError``, nothing sent) slices the current minute has no room for.

    ``wait`` is :meth:`order_room_wait` of the transport: seconds until the whole order fits
    the per-minute window. ``rate_limit_mode="raise"`` never waits, and a wait above
    ``order_rate_limit_max_wait`` is longer than the limiter would wait, so the order would
    stop part-way; refuse it before the first slice instead.
    """
    if wait <= 0.0:
        return
    if config.rate_limit_mode == "raise" or wait > config.order_rate_limit_max_wait:
        raise RateLimitError(
            "client-side rate limit for place_order"
            + describe_window((config.order_rate_limit_per_minute, 60.0)),
            endpoint="PlaceOrder",
            http_status=None,
            retry_after=wait,
        )


@dataclass
class SliceProgress:
    """The slices of one ``place_sliced_order`` call and the ids acknowledged so far."""

    requested: int
    slices: tuple[int, ...]
    order_ids: list[str] = field(default_factory=list)

    def result(self, error: TradeSmartError | None = None) -> SlicedOrderResult:
        placed = sum(self.slices[: len(self.order_ids)])
        return SlicedOrderResult(
            requested_quantity=self.requested,
            slice_quantities=self.slices,
            order_ids=tuple(self.order_ids),
            placed_quantity=placed,
            unsent_quantity=self.requested - placed,
            complete=len(self.order_ids) == len(self.slices),
            error=error,
        )

    def done(self) -> SlicedOrderResult:
        """Every slice placed: the complete result, logged."""
        return self._logged("complete", self.result())

    def stopped(self, error: TradeSmartError) -> SlicedOrderResult:
        """A definite failure on the next slice: the partial result with ``error``, logged."""
        return self._logged(f"stopped ({type(error).__name__})", self.result(error))

    def unknown(self, error: OrderStatusUnknownError) -> None:
        """An ambiguous slice: attach the partial result and a note to ``error`` (re-raised)."""
        partial = self.result()
        error.sliced_result = partial
        error.add_note(
            f"place_sliced_order: slice {len(self.order_ids) + 1} of {len(self.slices)} has an "
            f"unknown outcome; {len(self.order_ids)} slice(s) placed "
            f"({partial.placed_quantity} of {self.requested}), nothing more was sent. "
            "Check orders() before sending anything again."
        )
        self._logged("unknown", partial)

    def interrupted(self, exc: BaseException) -> None:
        """Anything else (Ctrl-C, ``SystemExit``, a task cancellation, ...) stopped the loop.

        Adds a note with the order ids placed so far and the unsent quantity, attaches the
        partial result as ``sliced_result`` when ``exc`` accepts attributes, and logs a
        WARNING summary; never fails. The caller re-raises ``exc`` unchanged.
        """
        partial = self.result()
        count = len(self.order_ids)
        pending = (
            f"slice {count + 1} of {len(self.slices)} may have been sent; "
            if count < len(self.slices)
            else ""
        )
        note = (
            f"place_sliced_order interrupted ({type(exc).__name__}): {count} slice(s) placed "
            f"(order ids: {', '.join(self.order_ids) or 'none'}), quantity "
            f"{partial.placed_quantity} of {self.requested} placed, {partial.unsent_quantity} "
            f"unsent; {pending}nothing more was sent. Check orders() before sending anything "
            "again."
        )
        with contextlib.suppress(Exception):  # __notes__ replaced by something not a list
            exc.add_note(note)
        with contextlib.suppress(Exception):  # an exception type that refuses attributes
            exc.sliced_result = partial  # type: ignore[attr-defined]
        self._logged(f"interrupted ({type(exc).__name__})", partial, warn=True)

    def _logged(
        self, word: str, result: SlicedOrderResult, *, warn: bool = False
    ) -> SlicedOrderResult:
        _log.log(
            logging.INFO if result.complete and not warn else logging.WARNING,
            "place_sliced_order %s: %d/%d slices placed, quantity %d placed, %d unsent",
            word,
            len(result.order_ids),
            len(result.slice_quantities),
            result.placed_quantity,
            result.unsent_quantity,
        )
        return result
