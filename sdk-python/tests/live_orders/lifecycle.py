"""Helpers of the UAT lifecycle tests (pinned by tests/unit/test_uat_lifecycle_helpers.py).

The lifecycle tests run only in UAT mode (``safety.uat_mode``): orders there move no real money,
so an order may rest, be modified and be cancelled, an order at the last price may fill, and other
order types (IOC, stops, MARKET) are tried. Every production rule of ``safety.run_order`` still
applies (one place call, never resent, cancelled once in ``finally`` while open), except that a
fill is returned instead of failing; ``close_position`` then sells it back.

Funded mode (``safety.funded_mode``) uses the same tracked run with the money rules of
``funded.py`` on top (see ``run_tracked``).
"""

from __future__ import annotations

import dataclasses
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import date
from datetime import time as dtime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import httpx

from tests._environment import environment_name
from tests.live_orders import safety
from tests.live_orders.safety import (
    CHECK_THE_ACCOUNT,
    SEGMENTS,
    TAG_PREFIX,
    UAT_ENV_NAME,
    CheckTheAccount,
    Instrument,
    OrderOutcome,
    OrderPlan,
    OrderSession,
    PlacedOrder,
    Refusal,
    Segment,
    ceil_to_tick,
    floor_to_tick,
    is_final,
)
from tradesmart import TradeSmart
from tradesmart.errors import TradeSmartError
from tradesmart.models.market import Quote
from tradesmart.models.orders import Order, Trade
from tradesmart.models.portfolio import Position
from tradesmart.models.stream import OrderUpdate

if TYPE_CHECKING:  # funded.py builds on this module: no import of it at run time here
    from tests.live_orders.funded import MoneyBudget

UAT_ONLY = (
    "UAT lifecycle tests run only in UAT mode (TRADESMART_ENV=uat and a UAT REST host); skipped"
)
OPEN = "OPEN"
PER_TEST_CAP = 2  # one order and the SELL that closes its fill
STOP_TRIGGER = Decimal("1.03")  # a stop BUY triggers 3 % above the last price ...
STOP_LIMIT = Decimal("1.04")  # ... with its limit 4 % above it
# Options move more in a few seconds: their stop BUY triggers 10 % above, limit 12 % above.
OPTION_STOP_TRIGGER = Decimal("1.10")
OPTION_STOP_LIMIT = Decimal("1.12")
OPTION_STRIKE_TRIES = 5  # strikes quoted (nearest first) to find one with a last price

# -- the UAT matrix: instruments per segment -------------------------------------------
# Production keeps safety.SEGMENTS. UAT data (2026-11-11): SBIN-EQ has no last price there
# (ITC-EQ has), MCX has no data yet. Option segments pick the at-the-money strike of the nearest
# expiry (pick_option). Ids are short: they become part of the order tag.

UAT_SEGMENTS: dict[str, Segment] = {
    "mcx": SEGMENTS["mcx"],
    "nse": Segment("nse", "NSE", "CNC", dtime(9, 15), dtime(15, 30), tradingsymbol="ITC-EQ"),
    # BSE cash trading symbols carry no "-EQ" suffix (scrip search: BSE "TCS", NSE "TCS-EQ").
    "bse": Segment("bse", "BSE", "CNC", dtime(9, 15), dtime(15, 30), tradingsymbol="ITC"),
    "nfo": SEGMENTS["nfo"],
    "nfoopt": Segment(
        "nfoopt", "NFO", "NRML", dtime(9, 15), dtime(15, 30), instrument="OPTIDX", symbol="NIFTY"
    ),
    # BFO script master (production and UAT, 2026-11-12): SENSEX options have Symbol BSXOPT
    # (TradingSymbol SENSEX...CE/PE), its futures BSXFUT; SX50* is SENSEX 50, never used here.
    "bfoopt": Segment(
        "bfoopt",
        "BFO",
        "NRML",
        dtime(9, 15),
        dtime(15, 30),
        instrument="OPTIDX",
        symbol="BSXOPT",
        future_symbol="BSXFUT",
        name="SENSEX",
    ),
}
# Product of the buy-then-sell round trips: intraday on cash and options, NRML on the future.
ROUND_TRIP_PRODUCT: dict[str, str] = {
    "nse": "MIS",
    "bse": "MIS",
    "nfo": "NRML",
    "nfoopt": "MIS",
    "bfoopt": "MIS",
}
OPTION_SEGMENT_IDS = frozenset({"nfoopt", "bfoopt"})
_UAT_SKIPS = {"mcx": "MCX: UAT has no MCX data yet (owner is enabling it); skipped"}


def uat_segment_skip_reason(segment_id: str) -> str | None:
    """Why a segment is not tested in UAT (``None``: it is)."""
    return _UAT_SKIPS.get(segment_id)


def uat_only_reason(environ: Mapping[str, str]) -> str | None:
    """A skip reason unless ``TRADESMART_ENV`` is ``uat`` (decided before any API call)."""
    return None if environment_name(environ) == UAT_ENV_NAME else UAT_ONLY


def uat_session(
    environ: Mapping[str, str], open_session: Callable[[], OrderSession]
) -> OrderSession:
    """The run's session, only in UAT mode; ``open_session`` is not called outside UAT."""
    reason = uat_only_reason(environ)
    if reason is not None:
        raise Refusal(reason)
    session = open_session()
    if not session.uat:
        raise Refusal(UAT_ONLY)
    return session


def _require_price(last_price: Decimal | None, tick_size: Decimal | None) -> None:
    if last_price is None or last_price <= 0:
        raise Refusal("no last price from quote() (market closed or no trades yet)")
    if tick_size is None or tick_size <= 0:
        raise Refusal("no tick size from quote() or the script master")


def at_last_price(
    last_price: Decimal | None,
    tick_size: Decimal | None,
    side: str,
    *,
    lower_circuit: Decimal | None = None,
    upper_circuit: Decimal | None = None,
) -> Decimal:
    """The last price on the tick, rounded toward a fill (up for a BUY, down for a SELL),
    then kept within the circuit limits on the tick (``uc`` rounded down, ``lc`` rounded up).

    UAT fills a limit order at exactly the last price (probe 2026-11-11); 0.5 % above it rested.
    """
    _require_price(last_price, tick_size)
    assert last_price is not None and tick_size is not None
    if side == "BUY":
        price = ceil_to_tick(last_price, tick_size)
    else:
        price = floor_to_tick(last_price, tick_size)
    if upper_circuit is not None and upper_circuit > 0 and price > upper_circuit:
        price = floor_to_tick(upper_circuit, tick_size)
    if lower_circuit is not None and lower_circuit > 0 and price < lower_circuit:
        price = ceil_to_tick(lower_circuit, tick_size)
    return price


def reference_price(quote: Quote, side: str, *, use_depth: bool) -> Decimal | None:
    """The price an order at the market starts from: with ``use_depth`` the best offer
    (``sp1``) for a BUY or the best bid (``bp1``) for a SELL when above 0, else the last price.
    """
    if use_depth:
        best = quote.sp1 if side == "BUY" else quote.bp1 if side == "SELL" else None
        if best is not None and best > 0:
            return best
    return quote.last_price


def stop_ratios(segment_id: str) -> tuple[Decimal, Decimal]:
    """``(trigger, limit)`` multipliers of the last price for a segment's stop BUY."""
    if segment_id in OPTION_SEGMENT_IDS:
        return OPTION_STOP_TRIGGER, OPTION_STOP_LIMIT
    return STOP_TRIGGER, STOP_LIMIT


def stop_buy_prices(
    last_price: Decimal | None,
    tick_size: Decimal | None,
    upper_circuit: Decimal | None,
    *,
    ratios: tuple[Decimal, Decimal] = (STOP_TRIGGER, STOP_LIMIT),
) -> tuple[Decimal, Decimal]:
    """``(trigger, price)`` of a stop BUY that is not hit: by default 3 % and 4 % above the
    last price (``ratios``; see ``stop_ratios``).

    Both on the tick (rounded up); the limit never above the upper circuit (rounded down) and
    never below the trigger, so a trigger above the circuit is refused.
    """
    _require_price(last_price, tick_size)
    assert last_price is not None and tick_size is not None
    trigger = ceil_to_tick(last_price * ratios[0], tick_size)
    price = ceil_to_tick(last_price * ratios[1], tick_size)
    if upper_circuit is not None and upper_circuit > 0 and price > upper_circuit:
        price = floor_to_tick(upper_circuit, tick_size)
    if price < trigger:
        raise Refusal(
            f"no stop price within the upper circuit {upper_circuit} "
            f"(last price {last_price}, trigger {trigger})"
        )
    return trigger, price


def stop_modify(
    trigger: Decimal, price: Decimal, tick_size: Decimal, upper_circuit: Decimal | None
) -> tuple[Decimal, Decimal]:
    """The modified ``(trigger, price)``: each one tick higher while the circuit allows."""
    up = price + tick_size
    new_price = up if upper_circuit is None or upper_circuit <= 0 or up <= upper_circuit else price
    new_trigger = trigger + tick_size
    return (new_trigger if new_trigger <= new_price else trigger), new_price


def script_master_reason(client: TradeSmart, exchange: str) -> str | None:
    """Why ``exchange``'s script master cannot be used (``None``: it has rows).

    Read-only (the client keeps the file for the day). A download or parse failure
    (``TradeSmartError``, ``httpx.HTTPError``, ``ValueError``, ``OSError``) or an empty file is a
    data gap of the environment and skips the test; any other exception is a bug and propagates.
    """
    prefix = f"the {exchange} script master is unavailable on this environment"
    try:
        rows = client.instruments(exchange)
    except (TradeSmartError, httpx.HTTPError, ValueError, OSError) as exc:
        return f"{prefix} ({type(exc).__name__}: {exc}); no order placed"
    if not rows:
        return f"{prefix} (the file is empty); no order placed"
    return None


# -- options: the at-the-money strike with a last price ---------------------------------


def _option_rows(
    segment: Segment, rows: Sequence[Mapping[str, str]], option_type: str, today: date
) -> list[tuple[Decimal, dict[str, str]]]:
    """``(strike, row)`` of the nearest expiry after today."""
    found: list[tuple[date, Decimal, dict[str, str]]] = []
    for row in rows:
        if (row.get("Exchange", segment.exchange) or "").strip().upper() != segment.exchange:
            continue
        if (row.get("Instrument") or "").strip().upper() != segment.instrument:
            continue
        if (row.get("Symbol") or "").strip().upper() != segment.symbol:
            continue
        if (row.get("OptionType") or "").strip().upper() != option_type:
            continue
        expiry = safety._expiry(row.get("Expiry"))
        strike = safety._decimal(safety.strike_text(row))
        if expiry is not None and expiry > today and strike is not None:
            found.append((expiry, strike, dict(row)))
    if not found:
        return []
    nearest = min(expiry for expiry, _, _ in found)
    return [(strike, row) for expiry, strike, row in found if expiry == nearest]


def _quote_lp(client: TradeSmart, exchange: str, token: str) -> Decimal | None:
    try:
        return client.quote(exchange, token).last_price
    except Exception:  # any failure: no price for this contract
        return None


def pick_option(
    client: TradeSmart,
    segment: Segment,
    rows: list[dict[str, str]],
    today: date,
    record: dict[str, Any],
    option_type: str = "CE",
) -> Instrument:
    """The nearest-expiry ``option_type`` whose strike is closest to the future's last price.

    The future is the underlying's nearest index future (``FUTIDX``, ``safety.future_segment``).
    Strikes are quoted nearest first (at most ``OPTION_STRIKE_TRIES``) until one has a last
    price above 0: UAT has no trades on many strikes. Raises ``Refusal`` (the test skips) when
    the script master or UAT lacks the data.
    """
    try:
        future = safety.pick_instrument(safety.future_segment(segment), rows, today)
    except Refusal as exc:
        raise Refusal(
            f"no {segment.label} index future (FUTIDX) in the {segment.exchange} script master: "
            f"its last price picks the at-the-money strike ({exc.reason})"
        ) from exc
    reference = _quote_lp(client, segment.exchange, future.token)
    info: dict[str, Any] = {
        "underlying": future.tradingsymbol,
        "underlying_last_price": None if reference is None else str(reference),
    }
    record["option"] = info
    if reference is None or reference <= 0:
        raise Refusal(f"no last price for the underlying future {future.tradingsymbol} on UAT")
    options = _option_rows(segment, rows, option_type, today)
    if not options:
        raise Refusal(
            f"no {segment.instrument} {segment.label} {option_type} of a future expiry in the "
            f"{segment.exchange} script master"
        )
    options.sort(key=lambda item: (abs(item[0] - reference), item[0]))
    tried: list[dict[str, Any]] = []
    info["tried"] = tried
    for _, row in options[:OPTION_STRIKE_TRIES]:
        tradingsymbol = (row.get("TradingSymbol") or "").strip()
        token = (row.get("Token") or "").strip()
        lp = _quote_lp(client, segment.exchange, token)
        tried.append({"tradingsymbol": tradingsymbol, "lp": None if lp is None else str(lp)})
        if lp is None or lp <= 0:
            continue
        lot = safety._lot_size(row.get("LotSize"))
        if lot is None or not tradingsymbol or not token:
            raise Refusal(f"script-master row of {tradingsymbol or token} is incomplete")
        info["strike"] = safety.strike_text(row)
        tick = safety._decimal(row.get("TickSize"))
        return Instrument(
            tradingsymbol=tradingsymbol,
            token=token,
            lot_size=lot,
            tick_size=tick if tick is not None and tick > 0 else None,
            expiry=safety._expiry(row.get("Expiry")),
        )
    raise Refusal(
        f"no {segment.label} {option_type} strike near {reference} with a last price on UAT "
        f"(tried {', '.join(t['tradingsymbol'] for t in tried)})"
    )


def normal_status(value: Any) -> str | None:
    """Upper-case status text; ``CANCELLED`` is spelt ``CANCELED`` (as in the order book)."""
    if value is None:
        return None
    text = str(value).strip().upper()
    if text == "CANCELLED":
        return "CANCELED"
    return text or None


class OrderUpdates:
    """Collects stream order updates (``on_order_update``) per order id, in arrival order."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._statuses: dict[str, list[str]] = {}

    def add(self, update: OrderUpdate) -> None:
        order_id = (update.order_id or "").strip()
        status = normal_status(update.status)
        if not order_id or status is None:
            return
        with self._lock:
            self._statuses.setdefault(order_id, []).append(status)

    def statuses(self, order_id: str) -> list[str]:
        with self._lock:
            return list(self._statuses.get(order_id.strip(), []))


def wait_until(
    condition: Callable[[], bool],
    timeout: float,
    *,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    poll: float = 0.5,
) -> bool:
    """Poll ``condition`` until it is true (``True``) or ``timeout`` seconds pass (``False``)."""
    deadline = clock() + timeout
    while True:
        if condition():
            return True
        if clock() >= deadline:
            return False
        sleep(poll)


def states_agree(
    book_status: Any, history_statuses: Sequence[Any], stream_statuses: Sequence[Any]
) -> list[str]:
    """What ``orders()``, ``order_history()`` and the stream disagree on (empty: they agree).

    The order must be final in the book; the history must show ``OPEN`` and the book's final
    status and no other final status (its order is not assumed, F14); the stream must show
    ``OPEN`` and its last final status must be the book's.
    """
    book = normal_status(book_status)
    history = [s for s in (normal_status(v) for v in history_statuses) if s is not None]
    stream = [s for s in (normal_status(v) for v in stream_statuses) if s is not None]
    problems: list[str] = []
    if not is_final(book):
        problems.append(f"the order book status is {book}, not a final status")
    if OPEN not in history:
        problems.append(f"order_history() has no {OPEN} entry: {history}")
    if book not in history:
        problems.append(f"order_history() has no {book} entry: {history}")
    others = sorted({s for s in history if is_final(s) and s != book})
    if others:
        problems.append(f"order_history() also shows {', '.join(others)} (order book {book})")
    if not stream:
        problems.append("the stream sent no order update for the order")
        return problems
    if OPEN not in stream:
        problems.append(f"the stream sent no {OPEN} update: {stream}")
    finals = [s for s in stream if is_final(s)]
    if not finals:
        problems.append(f"the stream sent no final status: {stream}")
    elif finals[-1] != book:
        problems.append(f"the stream ended {finals[-1]}, the order book {book}")
    return problems


def tag_problems(placed: Sequence[PlacedOrder], book: Sequence[Order]) -> list[str]:
    """Tags of this run are unique and found in ``orders()`` on the right order (empty: fine).

    Also no suite tag (``sdklive-``) appears on two orders of the book: that would be a resend.
    An order without an order id (refused in the place reply) need not be in the book.
    """
    problems: list[str] = []
    tags = [p.tag for p in placed]
    for tag in sorted({t for t in tags if tags.count(t) > 1}):
        problems.append(f"tag {tag} was used for more than one order this run")
    rows = [((o.tag or "").strip(), (o.order_id or "").strip()) for o in book]
    suite = [tag for tag, _ in rows if tag.startswith(TAG_PREFIX)]
    for tag in sorted({t for t in suite if suite.count(t) > 1}):
        problems.append(f"the order book has more than one order with tag {tag}")
    for p in placed:
        if p.order_id is None:
            continue
        ids = [order_id for tag, order_id in rows if tag == p.tag]
        if not ids:
            problems.append(f"tag {p.tag} (order {p.order_id}) is not in orders()")
        elif p.order_id not in ids:
            problems.append(f"tag {p.tag} belongs to order {ids[0]} in orders(), not {p.order_id}")
    return problems


def _same(a: str | None, b: str) -> bool:
    return (a or "").strip().upper() == b.strip().upper()


def net_quantity(positions: Sequence[Position], exchange: str, tradingsymbol: str) -> int:
    """The instrument's net quantity over all its position rows (every product)."""
    return sum(
        row.net_quantity or 0
        for row in positions
        if _same(row.exchange, exchange) and _same(row.tradingsymbol, tradingsymbol)
    )


def trade_book_fill(trades: Sequence[Trade], order_id: str) -> int | None:
    """The order's filled quantity in ``trades()``: the LARGEST ``fillshares`` of its rows.

    ``fillshares`` is the order's running total ("Total Traded Quantity of this order"),
    repeated on each trade row (``flqty`` is the one trade), so it is never summed. ``None``
    when no row of the order carries it.
    """
    values = [
        t.filled_quantity
        for t in trades
        if (t.order_id or "").strip() == order_id.strip() and t.filled_quantity is not None
    ]
    return max(values) if values else None


def has_trade(trades: Sequence[Trade], order_id: str) -> bool:
    """Whether ``trades()`` lists any trade of ``order_id``."""
    return any((t.order_id or "").strip() == order_id.strip() for t in trades)


FILL_READ_SECONDS = 10.0  # how long a fill quantity that is not yet known is read again


def known_fill(
    session: OrderSession,
    order_id: str,
    *,
    wait: float = FILL_READ_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> int | None:
    """The order's filled quantity, read again: its current ``orders()`` row (above 0) for up
    to ``wait`` seconds, then the larger of that and the largest ``trades()`` total; ``None``
    when neither shows it.
    """
    found: list[int] = []

    def from_book() -> bool:
        try:
            book = session.client.orders()
        except Exception:
            return False
        row = next((o for o in book if (o.order_id or "").strip() == order_id), None)
        quantity = None if row is None else safety.fill_of(row)
        if quantity is not None and quantity > 0:
            found.append(quantity)
            return True
        return False

    wait_until(from_book, wait, sleep=sleep, clock=clock)
    return _larger(found[-1] if found else None, _trade_total(session, order_id))


def _trade_total(session: OrderSession, order_id: str) -> int | None:
    try:
        return trade_book_fill(session.client.trades(), order_id)
    except Exception:
        return None


def _larger(*values: int | None) -> int | None:
    known = [v for v in values if v is not None and v > 0]
    return max(known) if known else None


def fill_quantity(
    session: OrderSession,
    order_id: str,
    book_value: int | None,
    *,
    wait: float = FILL_READ_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> int | None:
    """An order's filled quantity: the LARGER of the final order-book row's value
    (``book_value``, from ``run_order``) and the largest ``trades()`` total of the order.

    When neither is known (above 0), the order book is read again (``known_fill``); never an
    earlier row's value. ``None``: still unknown.
    """
    quantity = _larger(book_value, _trade_total(session, order_id))
    if quantity is not None:
        return quantity
    return known_fill(session, order_id, wait=wait, sleep=sleep, clock=clock)


def close_quantity(
    session: OrderSession,
    plan: OrderPlan,
    outcome: OrderOutcome,
    record: dict[str, Any],
    *,
    wait: float = FILL_READ_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    """How much of a filled BUY to sell back: ``min(filled quantity, plan.quantity)``, above 0.

    The filled quantity is ``fill_quantity``: the larger of the final order-book row's value
    (``outcome.filled_quantity``) and the trade book's, read again while unknown. Never the
    plan's quantity by default: an unknown quantity raises ``CheckTheAccount`` and no SELL is
    sent.
    """
    order_id = str(outcome.order_id)
    filled = fill_quantity(
        session, order_id, outcome.filled_quantity, wait=wait, sleep=sleep, clock=clock
    )
    record["filled_quantity"] = filled
    if filled is None or filled <= 0:
        raise CheckTheAccount(
            f"{CHECK_THE_ACCOUNT}: fill quantity unknown for order {order_id} "
            f"({plan.tradingsymbol}, {plan.exchange}); no closing order sent"
        )
    if filled > plan.quantity:
        record["fill_above_order_quantity"] = filled
    return min(filled, plan.quantity)


def _check_room(budget: safety.OrderBudget, need: int, what: str) -> None:
    if budget.limit - budget.used < need:
        raise Refusal(
            f"{what}: at most {budget.limit} orders; a BUY is sent only while one more order "
            "is left to close its fill"
        )


def _funded_money(session: OrderSession, plan: OrderPlan) -> tuple[MoneyBudget, Decimal]:
    """Funded mode's checks before any call: the session's money budget and the order value.

    The side must be exactly BUY or SELL. A BUY passes ``money.check``. A SELL only closes
    what a BUY of this run filled and has not yet sold (``session.fills``); such a closing SELL
    is not held to the per-order value (its quantity is already capped by the fill, and a
    premium that rose while the BUY filled must not leave the position open).
    """
    from tests.live_orders.funded import order_value  # here: funded.py imports this module

    money = session.money
    if money is None:
        raise Refusal("funded mode: the session has no money budget; refusing to place orders")
    if plan.transaction_type not in ("BUY", "SELL"):
        raise Refusal(
            f"funded mode sends BUY or SELL only; refusing transaction type "
            f"{plan.transaction_type!r}"
        )
    value = order_value(plan)
    if plan.transaction_type == "SELL":
        held = session.fills.get(plan.tradingsymbol, 0)
        if plan.quantity > held:
            raise Refusal(
                f"funded mode: a SELL only closes a filled BUY of the same test; SELL of "
                f"{plan.quantity} {plan.tradingsymbol} refused ({held} bought and not yet sold)"
            )
    else:
        money.check(value)
    return money, value


def _never_accepted(outcome: OrderOutcome | None, record: dict[str, Any], sent: bool) -> bool:
    """Whether a BUY surely holds nothing: not sent, refused by client-side validation,
    rejected, or final without any fill seen (``run_order`` checks book, history and trades)."""
    if not sent:
        return True
    if record.get("place", {}).get("outcome") == "not sent (client-side validation)":
        return True
    if outcome is None:
        return False  # sent, then the run failed: unknown, keep everything
    if outcome.order_id is None:
        return outcome.definitive_rejection
    return not outcome.filled and is_final(outcome.final_status)


def run_tracked(
    session: OrderSession, plan: OrderPlan, record: dict[str, Any], **kwargs: Any
) -> OrderOutcome:
    """``safety.run_order`` in UAT or funded mode (a fill is accepted); every sent order is
    remembered.

    Refuses a session in neither mode (or both) before any call. A BUY is sent only when the
    run cap and the test's own cap (``session.test_budget``, ``PER_TEST_CAP``) leave one more
    order to close its fill with a SELL. Before a BUY the instrument's ``positions()`` net
    quantity is read into ``record["net_before"]`` (``close_position`` checks the account
    against it); if it cannot be read, no order is sent. The tag (and order id when known) goes
    into ``session.placed`` whenever the order was sent, also when the run then fails.

    Funded mode needs the test's own budget (``session.test_budget``, else refused before any
    call) and adds the money rules (``funded.MoneyBudget``), all before any call: a BUY
    passes ``money.check`` and reserves its value, and gets it back only when it ends with no
    fill or is never accepted (a partial fill or an unknown outcome keeps it); a filled BUY's
    fill quantity is added to ``session.fills``; a SELL is refused unless its quantity is at
    most ``session.fills`` of its instrument (then it is exempt from the per-order value and
    reserves nothing), and is subtracted from it once sent (a SELL refused by client-side
    validation was never sent: the fill stays, so the position is known to be open).
    """
    if session.uat and session.funded:
        raise Refusal("UAT mode and funded mode cannot both be on; refusing to place orders")
    if not (session.uat or session.funded):
        raise Refusal(UAT_ONLY)
    money: MoneyBudget | None = None
    value = Decimal("0")
    if session.funded:
        money, value = _funded_money(session, plan)
        if session.test_budget is None:
            raise Refusal(
                "funded mode: no per-test order budget (session.test_budget; set by the funded "
                "test fixture); refusing to place orders"
            )
    is_sell = plan.transaction_type == "SELL"
    need = 1 if is_sell else 2
    _check_room(session.budget, need, "order cap reached")
    if session.test_budget is not None:
        _check_room(session.test_budget, need, "order cap per test reached")
    if not is_sell:
        net = _read_net(session, plan)
        if net is None:
            raise Refusal("positions() could not be read before the BUY; no order placed")
        record["net_before"] = net
    reserved = money is not None and not is_sell
    if money is not None and reserved:
        money.reserve(value)  # may refuse: nothing is spent yet
        record["money"] = {"order_value": str(value), "committed": str(money.committed)}
    # The test's own budget is spent by run_order right before the place call (N4: an order
    # refused by the open-order check is not counted as sent).
    used = session.budget.used
    outcome: OrderOutcome | None = None
    try:
        outcome = safety.run_order(
            session.client,
            plan,
            session.budget,
            record,
            responses=session.responses,
            recorder=session.recorder,
            allow_fill=True,
            uat=session.uat,
            funded=session.funded,
            ignore_open=session.ignored_open,
            test_budget=session.test_budget,
            **kwargs,
        )
    finally:
        sent = session.budget.used > used
        if sent:
            order_id = outcome.order_id if outcome is not None else None
            if order_id is None:
                order_id = record.get("place", {}).get("order_id")
            session.placed.append(PlacedOrder(plan.segment_id, plan.tag, order_id))
        not_sent = record.get("place", {}).get("outcome") == "not sent (client-side validation)"
        if money is not None and is_sell and sent and not not_sent:  # else the position is open
            left = session.fills.get(plan.tradingsymbol, 0) - plan.quantity
            if left > 0:
                session.fills[plan.tradingsymbol] = left
            else:
                session.fills.pop(plan.tradingsymbol, None)
        if money is not None and reserved and _never_accepted(outcome, record, sent):
            money.release(value)
            record.setdefault("money", {})["released"] = str(value)
    assert outcome is not None  # run_order returned
    if money is not None and reserved and outcome.filled:
        filled = fill_quantity(
            session,
            str(outcome.order_id),
            outcome.filled_quantity,
            sleep=kwargs.get("sleep", time.sleep),
            clock=kwargs.get("clock", time.monotonic),
        )
        record.setdefault("money", {})["filled_quantity"] = filled
        if filled is not None and filled > 0:  # unknown: nothing may be sold against it
            session.fills[plan.tradingsymbol] = session.fills.get(plan.tradingsymbol, 0) + filled
    return outcome


FILL_WAIT_SECONDS = 60.0  # how long an order at the last price is given to fill
NET_WAIT_SECONDS = 10.0  # how long positions() is given to show the closed position


def _read_net(session: OrderSession, plan: OrderPlan) -> int | None:
    """The instrument's ``positions()`` net quantity; ``None`` if it cannot be read."""
    try:
        positions = session.client.positions()
    except Exception:
        return None
    return net_quantity(positions, plan.exchange, plan.tradingsymbol)


def close_position(
    session: OrderSession,
    plan: OrderPlan,
    quantity: int,
    record: dict[str, Any],
    *,
    net_before: int,
    settle_seconds: float = FILL_WAIT_SECONDS,
    poll_interval: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    use_depth: bool = False,
) -> OrderOutcome:
    """Close a filled BUY of ``plan``: one DAY LIMIT SELL of ``quantity`` at the last price
    (with ``use_depth``: at the best bid when there is one; see ``reference_price``).

    ``quantity`` comes from ``close_quantity`` (never more than was bought, never 0). The SELL
    goes through ``run_tracked`` (one place call, never resent; cancelled once if it has not
    filled within ``settle_seconds``). It is judged by its own order-book row: closed only
    when its filled quantity equals ``quantity``, else ``CheckTheAccount`` ("position left open
    (<sold> of <quantity>)"). Then the account itself: the instrument's ``positions()`` net
    quantity must be back at ``net_before`` (read before the BUY by ``run_tracked``) within
    ``NET_WAIT_SECONDS``, so a wrong (too small) fill count fails too ("position left open:
    positions() net quantity ..."). Observations go to ``record["close"]``.
    """
    if quantity <= 0:
        raise ValueError(f"nothing to close: quantity {quantity}")
    entry: dict[str, Any] = {"quantity": quantity}
    record["close"] = entry

    def left_open(sold: int | None) -> str:
        return (
            f"{CHECK_THE_ACCOUNT}: position left open "
            f"({'unknown' if sold is None else sold} of {quantity}): {plan.tradingsymbol} "
            f"({plan.exchange}, product {plan.product})"
        )

    try:
        quote = session.client.quote(plan.exchange, plan.token)
        tick = quote.tick_size if quote.tick_size is not None and quote.tick_size > 0 else None
        price = at_last_price(
            reference_price(quote, "SELL", use_depth=use_depth),
            tick or plan.tick_size,
            "SELL",
            lower_circuit=quote.lc,
            upper_circuit=quote.uc,
        )
    except Exception as exc:  # no price, no closing order
        raise CheckTheAccount(f"{left_open(0)}: no last price to close it ({exc})") from exc
    sell = dataclasses.replace(
        plan,
        transaction_type="SELL",
        order_type="LIMIT",
        validity="DAY",
        trigger_price=None,
        price=price,
        quantity=quantity,
        tag=safety.new_tag(plan.segment_id),
        last_price=quote.last_price,
    )
    try:
        outcome = run_tracked(
            session,
            sell,
            entry,
            settle_seconds=settle_seconds,
            poll_interval=poll_interval,
            sleep=sleep,
            clock=clock,
        )
    except Refusal as exc:
        raise CheckTheAccount(
            f"{left_open(0)}: the closing SELL was not sent ({exc.reason})"
        ) from exc
    sold: int | None = 0
    if outcome.order_id is not None and outcome.filled:
        sold = fill_quantity(
            session, outcome.order_id, outcome.filled_quantity, sleep=sleep, clock=clock
        )
    entry["sold_quantity"] = sold
    if sold != quantity:
        ended = outcome.final_status or f"{outcome.exception} at place_order"
        raise CheckTheAccount(
            f"{left_open(sold)}: the closing SELL {outcome.order_id} ended {ended}"
        )

    seen: list[int | None] = []

    def back_where_it_began() -> bool:
        seen.append(_read_net(session, plan))
        return seen[-1] == net_before

    wait_until(back_where_it_began, NET_WAIT_SECONDS, sleep=sleep, clock=clock)
    net_after = seen[-1] if seen else None
    entry["net_before"] = net_before
    entry["net_after_close"] = net_after
    account = f"{CHECK_THE_ACCOUNT}: position left open: positions()"
    if net_after is None:
        raise CheckTheAccount(
            f"{account} unreadable after the close of {quantity} {plan.tradingsymbol} "
            f"({plan.exchange}, product {plan.product}); it was {net_before} before the buy"
        )
    if net_after != net_before:
        raise CheckTheAccount(
            f"{account} net quantity of {plan.tradingsymbol} ({plan.exchange}, product "
            f"{plan.product}) is {net_after}, {net_before} before the buy; {quantity} were "
            "sold back"
        )
    return outcome
