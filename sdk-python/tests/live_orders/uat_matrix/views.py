"""Cross-checks of one order across the views of the account (read-only data, no calls here).

The order book row is the reference. ``cross_check`` lists every disagreement with it:

- ``order_history()`` (newest first): its newest entry has the book's status, quantity and price;
- the order stream (``om`` updates, in arrival order): the last update has the book's status;
- ``trades()``: the fills of the order add up to the book's filled quantity.

A disagreement is recorded with the case (inventory "views and stream" family asserts on it);
the batch engine never fails a case on it by itself. ``OrderWatcher`` collects the stream's order
updates per order id (thread-safe), as the ``on_order_update`` callback of a ``TradeSmartStream``.

Added for the views family (M9; also used by M2), all optional:

- ``cross_check(..., full=True)``: field-level agreement of the newest history entry and the
  last stream update with the book (trigger, fills, average price, order type, side, validity,
  product, tag, exchange order id, reject reason), history newest first (by ``norentm``), and
  the average price of the order's trades;
- ``cross_check(..., positions=PositionDelta)``: the change of ``positions()`` for the order's
  exchange, symbol and product: its own side's day quantity is the book's filled quantity and its
  day amount the value of its fills (``position_totals``, ``position_key``, ``position_delta``);
- ``wait_for_status``: a bounded wait for the stream to catch up with the book (the lag);
- ``views_record``: one case's views and mismatches; ``RecordViews``: the reads behind it for
  a case record (``record.Recorder(enrich=...)``), with the raw bodies of the case's calls.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

from tests.live_orders.safety import status_of
from tests.live_orders.uat_matrix.record import calls_from_ledger
from tradesmart import ClientConfig, TradeSmartStream
from tradesmart.enums import OrderType, Product, TransactionType, Validity
from tradesmart.models.orders import Order, OrderHistoryEntry, Trade
from tradesmart.models.portfolio import Position
from tradesmart.models.stream import OrderUpdate

STREAM_WAIT = 10.0  # seconds the stream is given to catch up with the book
AMOUNT_TOLERANCE = Decimal("0.01")


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip().upper()
    return text or None


def _same_number(a: Any, b: Any) -> bool:
    if a is None or b is None:
        return True  # a view that does not carry the field says nothing about it
    return Decimal(str(a)) == Decimal(str(b))


def filled_by_trades(trades: Sequence[Trade], order_id: str) -> int:
    """Units filled for ``order_id`` according to the trade book (sum of ``flqty``)."""
    total = 0
    for trade in trades:
        if (trade.order_id or "").strip() != order_id:
            continue
        total += trade.flqty if trade.flqty is not None else (trade.filled_quantity or 0)
    return total


@dataclass(frozen=True)
class PositionDelta:
    """Day buy / sell quantity and amount of one exchange, symbol and product (or a change)."""

    buy_quantity: int = 0
    buy_amount: Decimal = Decimal(0)
    sell_quantity: int = 0
    sell_amount: Decimal = Decimal(0)

    def __sub__(self, other: PositionDelta) -> PositionDelta:
        return PositionDelta(
            self.buy_quantity - other.buy_quantity,
            self.buy_amount - other.buy_amount,
            self.sell_quantity - other.sell_quantity,
            self.sell_amount - other.sell_amount,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "buy_quantity": self.buy_quantity,
            "buy_amount": f"{self.buy_amount:.2f}",
            "sell_quantity": self.sell_quantity,
            "sell_amount": f"{self.sell_amount:.2f}",
        }


_ENUMS: dict[str, type[Enum]] = {
    "product": Product,
    "order_type": OrderType,
    "transaction_type": TransactionType,
    "validity": Validity,
}
_NUMBERS = frozenset({"price", "quantity", "trigger_price", "filled_quantity", "average_price"})
# Fields compared in the field-level mode (status, quantity and price always are).
FULL_FIELDS = (
    "trigger_price",
    "filled_quantity",
    "average_price",
    "order_type",
    "transaction_type",
    "validity",
    "product",
    "tag",
    "exchange_order_id",
    "reject_reason",
)


def _norm(name: str, value: Any) -> Any:
    """A comparable form of one field (enums by name, numbers as Decimal); ``None`` when the
    view does not carry it (missing or blank)."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    enum = _ENUMS.get(name)
    if enum is not None:
        if isinstance(value, enum):
            return value.name
        try:
            return enum(str(value).strip()).name
        except ValueError:
            return str(value).strip().upper()
    if name in _NUMBERS:
        try:
            return Decimal(str(value))
        except InvalidOperation:
            return str(value).strip()
    return str(value).strip()


def _field(view: Any, name: str) -> Any:
    if name == "product" and isinstance(view, OrderUpdate):
        return view.pcode  # the order update names the product pcode
    return getattr(view, name, None)


def normalize(name: str, value: Any) -> Any:
    """The comparable form of a value of field ``name`` (``None``: missing or blank)."""
    return _norm(name, value)


def field_value(view: Any, name: str) -> Any:
    """The comparable value of field ``name`` in a book row, history entry or order update."""
    return _norm(name, _field(view, name))


def _fields(where: str, view: Any, row: Order) -> list[str]:
    mismatches: list[str] = []
    for name in FULL_FIELDS:
        theirs, ours = _norm(name, _field(view, name)), _norm(name, getattr(row, name, None))
        if theirs is not None and ours is not None and theirs != ours:
            mismatches.append(f"{where}: {name} {theirs}, book {ours}")
    return mismatches


def _newest_first(history: Sequence[OrderHistoryEntry]) -> list[str]:
    times = [entry.norentm for entry in history]
    if any(t is None for t in times):
        return []
    for index in range(len(times) - 1):
        newer, older = times[index], times[index + 1]
        assert newer is not None and older is not None
        if newer < older:
            return [f"history: not newest first (entry {index} at {newer}, then {older})"]
    return []


def _fills(trades: Sequence[Trade], order_id: str) -> tuple[int, Decimal]:
    quantity, amount = 0, Decimal(0)
    for trade in trades:
        if (trade.order_id or "").strip() != order_id:
            continue
        units = trade.flqty if trade.flqty is not None else (trade.filled_quantity or 0)
        quantity += units
        price = trade.flprc if trade.flprc is not None else trade.average_price
        if price is not None:
            amount += price * units
    return quantity, amount


def _is_buy(row: Order) -> bool:
    return bool(_norm("transaction_type", row.transaction_type) == TransactionType.BUY.name)


def _positions(row: Order, delta: PositionDelta, trades: Sequence[Trade] | None) -> list[str]:
    buy = _is_buy(row)
    side = "buy" if buy else "sell"
    filled = row.filled_quantity or 0
    quantity = delta.buy_quantity if buy else delta.sell_quantity
    amount = delta.buy_amount if buy else delta.sell_amount
    mismatches: list[str] = []
    if quantity != filled:
        mismatches.append(f"positions: day {side} quantity {quantity}, book filled {filled}")
    expected: Decimal | None = Decimal(0) if not filled else None
    if filled and trades:
        units, value = _fills(trades, (row.order_id or "").strip())
        expected = value if units else None
    if expected is None and filled and row.average_price is not None:
        expected = row.average_price * filled
    if expected is not None and abs(amount - expected) > AMOUNT_TOLERANCE:
        mismatches.append(f"positions: day {side} amount {amount}, fills {expected}")
    return mismatches


def cross_check(
    row: Order,
    *,
    history: Sequence[OrderHistoryEntry] | None = None,
    updates: Sequence[OrderUpdate] | None = None,
    trades: Sequence[Trade] | None = None,
    positions: PositionDelta | None = None,
    full: bool = False,
) -> list[str]:
    """Every disagreement between the book ``row`` and the other views given (``full``: the
    field-level checks too; ``positions``: the change of the order's day position)."""
    order_id = (row.order_id or "").strip()
    status = status_of(row)
    mismatches: list[str] = []
    if history:
        newest = history[0]
        if status_of(newest) != status:
            mismatches.append(
                f"history: newest entry {status_of(newest)}, book {status} (newest first)"
            )
        if not _same_number(newest.quantity, row.quantity):
            mismatches.append(f"history: quantity {newest.quantity}, book {row.quantity}")
        if not _same_number(newest.price, row.price):
            mismatches.append(f"history: price {newest.price}, book {row.price}")
        if full:
            mismatches += _fields("history", newest, row) + _newest_first(history)
    if updates:
        last = updates[-1]
        if _text(last.status) != status:
            mismatches.append(f"stream: last update {_text(last.status)}, book {status}")
        if not _same_number(last.quantity, row.quantity):
            mismatches.append(f"stream: quantity {last.quantity}, book {row.quantity}")
        if not _same_number(last.price, row.price):
            mismatches.append(f"stream: price {last.price}, book {row.price}")
        if full:
            mismatches += _fields("stream", last, row)
    if trades is not None:
        filled = filled_by_trades(trades, order_id)
        if filled != (row.filled_quantity or 0):
            mismatches.append(f"trades: {filled} filled, book {row.filled_quantity or 0}")
        units, value = _fills(trades, order_id)
        if full and units and row.average_price is not None:
            average = value / units
            if abs(average - row.average_price) > AMOUNT_TOLERANCE:
                mismatches.append(f"trades: average {average:.2f}, book {row.average_price}")
    if positions is not None:
        mismatches += _positions(row, positions, trades)
    return mismatches


# -- positions --------------------------------------------------------------------------------


def position_key(exchange: Any, tradingsymbol: Any, product: Any) -> str:
    """``EXCH|SYMBOL|PRODUCT`` (product by name: ``MIS`` and ``I`` are one key)."""
    name = _norm("product", product) or ""
    return f"{str(exchange or '').strip().upper()}|{str(tradingsymbol or '').strip()}|{name}"


def position_totals(positions: Iterable[Position]) -> dict[str, PositionDelta]:
    """The day buy / sell quantity and amount per position key (one ``positions()`` read)."""
    totals: dict[str, PositionDelta] = {}
    for p in positions:
        key = position_key(p.exchange, p.tradingsymbol, p.product)
        totals[key] = PositionDelta(
            p.daybuyqty or 0, p.daybuyamt or Decimal(0), p.daysellqty or 0,
            p.daysellamt or Decimal(0),
        )  # fmt: skip
    return totals


def position_delta(
    before: Mapping[str, PositionDelta], after: Mapping[str, PositionDelta], key: str
) -> PositionDelta:
    """What changed for ``key`` between two reads (a missing row counts as zero)."""
    return after.get(key, PositionDelta()) - before.get(key, PositionDelta())


# -- the stream lag ---------------------------------------------------------------------------


def wait_for_status(
    watcher: OrderWatcher,
    order_id: str,
    status: str | None,
    *,
    timeout: float = STREAM_WAIT,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    interval: float = 0.2,
) -> float | None:
    """Wait (bounded) until the order's latest stream update shows ``status``; the lag in
    seconds, or ``None`` when it did not within ``timeout`` (read-only)."""
    start = clock()
    while True:
        if status is not None and watcher.latest_status(order_id) == status:
            return clock() - start
        if clock() - start >= timeout:
            return None
        sleep(interval)


def wait_for_update(
    watcher: OrderWatcher,
    order_id: str,
    match: Callable[[OrderUpdate], bool],
    *,
    after: int = 0,
    timeout: float = STREAM_WAIT,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    interval: float = 0.2,
) -> tuple[float | None, OrderUpdate | None]:
    """Wait (bounded) for an update of the order, beyond the first ``after`` ones, for which
    ``match`` holds; ``(lag in seconds, the update)``, or ``(None, None)`` (read-only)."""
    start = clock()
    while True:
        found = next((u for u in watcher.updates(order_id)[after:] if match(u)), None)
        if found is not None:
            return clock() - start, found
        if clock() - start >= timeout:
            return None, None
        sleep(interval)


# -- one case's views ---------------------------------------------------------------------------


def views_record(
    rows: Mapping[str, Order],
    *,
    history: Mapping[str, Sequence[OrderHistoryEntry]],
    trades: Sequence[Trade],
    updates: Mapping[str, Sequence[OrderUpdate]],
    deltas: Mapping[str, PositionDelta],
    lags: Mapping[str, float | None],
    want_stream: bool = False,
) -> dict[str, Any]:
    """The views of a case's orders (raw rows kept) and every mismatch with the book."""
    orders: dict[str, Any] = {}
    every: list[str] = []
    for order_id, row in rows.items():
        entries = list(history.get(order_id) or ())
        seen = list(updates.get(order_id) or ())
        key = position_key(row.exchange, row.tradingsymbol, row.product)
        mismatches = cross_check(
            row, history=entries or None, updates=seen or None, trades=trades,
            positions=deltas.get(key), full=True,
        )  # fmt: skip
        if want_stream and not seen:
            mismatches.append("stream: no update for the order")
        orders[order_id] = {
            "history": [dict(h.raw) for h in entries],
            "history_count": len(entries),
            "trades": [dict(t.raw) for t in trades if (t.order_id or "").strip() == order_id],
            "stream": [dict(u.raw) for u in seen],
            "stream_reports": [u.reporttype for u in seen],
            "stream_lag": lags.get(order_id),
            "mismatches": mismatches,
        }
        every += [f"order {order_id}: {m}" for m in mismatches]
    return {
        "orders": orders,
        "positions": {key: delta.to_dict() for key, delta in deltas.items()},
        "mismatches": every,
    }


class ViewsReader(Protocol):
    """The read-only calls ``RecordViews`` makes (``TradeSmart`` has them)."""

    def order_history(self, order_id: str) -> list[OrderHistoryEntry]: ...

    def trades(self) -> list[Trade]: ...

    def positions(self) -> list[Position]: ...


def record_rows(record: Mapping[str, Any]) -> dict[str, Order]:
    """The book rows of a case record (each order's final row, else its settled one)."""
    rows: dict[str, Order] = {}
    for order_id, entry in dict(record.get("orders") or {}).items():
        data = (entry or {}).get("final") or (entry or {}).get("settled")
        if data:
            rows[str(order_id)] = Order.model_validate(data)
    return rows


class RecordViews:
    """``Recorder(enrich=...)`` for the views: per case record, waits (bounded) for the stream
    to show each order's book status, reads each order's history, the trade book and the
    positions (the delta since this object's last read of that key), and adds the raw bodies of
    the case's calls from the ledger. Read-only calls only."""

    def __init__(
        self,
        client: ViewsReader,
        ledger_path: Path,
        *,
        watcher: OrderWatcher | None = None,
        stream_wait: float = STREAM_WAIT,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.client = client
        self.ledger_path = ledger_path
        self.watcher = watcher
        self.stream_wait = stream_wait
        self.clock = clock
        self.sleep = sleep
        self._baseline = position_totals(client.positions())

    def __call__(self, record: Mapping[str, Any]) -> dict[str, Any]:
        rows = record_rows(record)
        lags: dict[str, float | None] = {}
        updates: dict[str, list[OrderUpdate]] = {}
        if self.watcher is not None:
            for order_id, row in rows.items():
                lags[order_id] = wait_for_status(
                    self.watcher, order_id, status_of(row), timeout=self.stream_wait,
                    clock=self.clock, sleep=self.sleep,
                )  # fmt: skip
                updates[order_id] = self.watcher.updates(order_id)
        history = {order_id: self.client.order_history(order_id) for order_id in rows}
        trades = self.client.trades() if rows else []
        deltas: dict[str, PositionDelta] = {}
        if rows:
            after = position_totals(self.client.positions())
            for row in rows.values():
                key = position_key(row.exchange, row.tradingsymbol, row.product)
                deltas[key] = position_delta(self._baseline, after, key)
            for key in deltas:
                self._baseline[key] = after.get(key, PositionDelta())
        views = views_record(
            rows, history=history, trades=trades, updates=updates, deltas=deltas, lags=lags,
            want_stream=self.watcher is not None,
        )  # fmt: skip
        call_ids = [str(c.get("call")) for c in record.get("calls") or () if c.get("call")]
        return {"views": views, "raw_calls": calls_from_ledger(self.ledger_path, call_ids)}


class OrderWatcher:
    """Order-stream updates per order id (the ``on_order_update`` callback of a stream)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._updates: dict[str, list[OrderUpdate]] = {}

    def on_order_update(self, update: OrderUpdate) -> None:
        order_id = (update.order_id or "").strip()
        if not order_id:
            return
        with self._lock:
            self._updates.setdefault(order_id, []).append(update)

    def updates(self, order_id: str) -> list[OrderUpdate]:
        with self._lock:
            return list(self._updates.get(order_id, ()))

    def latest_status(self, order_id: str) -> str | None:
        updates = self.updates(order_id)
        return _text(updates[-1].status) if updates else None


# -- the order stream of a family batch -------------------------------------------------------

STREAM_START_SECONDS = 15.0
SUBSCRIBE_SETTLE = 0.5  # order subscriptions have no acknowledgement: give it a moment


@contextmanager
def order_stream(client: Any, *, config: ClientConfig | None = None) -> Iterator[OrderWatcher]:
    """A ``TradeSmartStream`` of ``client`` (its id, token and config unless ``config``)
    subscribed to order updates, feeding an ``OrderWatcher``; stopped at the end (read-only)."""
    watcher = OrderWatcher()
    stream = TradeSmartStream.from_client(
        client, config=config, on_order_update=watcher.on_order_update
    )
    stream.start(timeout=STREAM_START_SECONDS)
    try:
        stream.subscribe_orders()
        time.sleep(SUBSCRIBE_SETTLE)
        yield watcher
    finally:
        stream.stop()
