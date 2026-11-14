"""Offline broker core: state, order lifecycle and drills. No I/O.

The REST adapter and the stream server both go through one ``BrokerState`` and its lock. Every
order or position change is pushed to the listeners as a wire stream message (``om``, ``pm``,
``tf``, ``df``), outside the lock.
"""

from __future__ import annotations

import copy
import dataclasses
import logging
import threading
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, cast

from tradesmart._logging import redact
from tradesmart.enums import Product
from tradesmart.models.base import IST
from tradesmart.testing import _replies
from tradesmart.testing.drills import Drill

_log = logging.getLogger("tradesmart.testing")

Listener = Callable[[dict[str, Any]], None]

OPEN_STATES = ("PENDING", "OPEN", "TRIGGER_PENDING")
_FILLABLE = ("OPEN", "TRIGGER_PENDING")
_NO_PRICE = ("MKT", "SL-MKT")
_DEFAULT_PLACE = ("open", "pending", "reject")
_EXITABLE = ("H", "B")  # ExitSNOOrder: cover and bracket orders only (documented)
# The documented required fields of PlaceGTTOrder / ModifyGTTOrder besides uid and actid.
_GTT_FIELDS = (
    "exch",
    "tsym",
    "ai_t",
    "d",
    "validity",
    "remarks",
    "trantype",
    "prctyp",
    "prd",
    "ret",
    "qty",
    "prc",
)
# The order fields a triggered GTT places (its order book row also gets "add_ord_id").
_GTT_ORDER_FIELDS = (
    "exch",
    "tsym",
    "trantype",
    "prctyp",
    "prd",
    "ret",
    "qty",
    "prc",
    "trgprc",
    "dscqty",
    "remarks",
)


@dataclass
class OfflineOrder:
    """An order the strategy sent, as the offline broker's order book holds it.

    ``side`` is ``"B"`` / ``"S"``; ``order_type`` and ``product`` are wire codes (``"LMT"``,
    ``"M"``); ``status`` is one of ``PENDING OPEN TRIGGER_PENDING COMPLETE CANCELED REJECTED``.
    ``payload`` is the request as received (secrets masked); ``history`` holds the
    ``SingleOrdHist`` rows, newest first.
    """

    id: str
    exchange: str
    symbol: str
    side: str
    quantity: int
    price: Decimal | None
    trigger_price: Decimal | None
    order_type: str
    product: str
    status: str
    filled_quantity: int = 0
    average_price: Decimal | None = None
    reject_reason: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    history: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class OfflineRequest:
    """One REST call the broker received: the endpoint and its decoded payload (secrets masked)."""

    endpoint: str
    payload: dict[str, Any]


def _mask(payload: Mapping[str, Any]) -> dict[str, Any]:
    return cast("dict[str, Any]", redact(dict(payload)))


def _number(value: Any, name: str) -> Decimal | None:
    """``value`` as a finite Decimal (``None`` when absent or blank); a clear error otherwise."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        number = Decimal(str(value).strip())
    except ArithmeticError:
        raise ValueError(f"{name} must be a number, got {value!r}") from None
    if not number.is_finite():
        raise ValueError(f"{name} must be a number, got {value!r}")
    return number


def _whole(value: Any, name: str) -> int:
    """``value`` as an int; whole-number text such as ``"10.0"`` is accepted."""
    number = _number(value, name) or Decimal(0)
    if number != number.to_integral_value():
        raise ValueError(f"{name} must be a whole number, got {value!r}")
    return int(number)


def _order_price(order_type: str, value: Any) -> Decimal | None:
    return None if order_type in _NO_PRICE else _number(value, "prc")


def _working_status(order_type: str) -> str:
    return "TRIGGER_PENDING" if order_type.startswith("SL") else "OPEN"


def _checked(
    fields: Mapping[str, Any], friendly: Mapping[str, str], known: Mapping[str, bool], what: str
) -> dict[str, Any]:
    """``fields`` with friendly names mapped to wire keys and values as wire text.

    An unknown name or a value that must be a number and is not raises ``ValueError``, so a
    typo in a test fails at once instead of being served and ignored.
    """
    wire: dict[str, Any] = {}
    for name, value in fields.items():
        key = friendly.get(name, name)
        if key not in known:
            names = ", ".join(friendly)
            raise ValueError(f"unknown {what} field {name!r}; use {names} or the API's names")
        if known[key]:
            _number(value, name)
        wire[key] = _replies.wire_value(value)
    return wire


def _quote_fields(fields: Mapping[str, Any]) -> dict[str, Any]:
    return _checked(fields, _replies.QUOTE_FIELDS, _replies.QUOTE_WIRE, "quote")


def _check_candle(candle: Mapping[str, Any]) -> None:
    stamp = candle.get("time")
    if stamp is not None and not isinstance(stamp, (date, str)):  # datetime is a date
        raise ValueError(f"candle time must be a datetime, a date or text, got {stamp!r}")
    _checked(
        {k: v for k, v in candle.items() if k != "time"},
        _replies.CANDLE_FIELDS,
        _replies.CANDLE_WIRE,
        "candle",
    )


def _recompute_position(row: dict[str, Any]) -> None:
    """Derive the net, average and realised values of a position from its buy/sell totals.

    The row's quantities and amounts must already be valid (see ``_normalise_position``).
    """

    def qty(key: str) -> int:
        return _whole(row.get(key), key)

    def amount(key: str) -> Decimal:
        return _number(row.get(key), key) or Decimal(0)

    def avg(total: Decimal, count: int) -> Decimal:
        return total / count if count else Decimal(0)

    buy_qty = qty("cfbuyqty") + qty("daybuyqty")
    sell_qty = qty("cfsellqty") + qty("daysellqty")
    buy_amt = amount("cfbuyamt") + amount("daybuyamt")
    sell_amt = amount("cfsellamt") + amount("daysellamt")
    buy_avg, sell_avg = avg(buy_amt, buy_qty), avg(sell_amt, sell_qty)
    net = buy_qty - sell_qty
    net_avg = buy_avg if net > 0 else sell_avg if net < 0 else Decimal(0)
    average, money = _replies.average_text, _replies.price_text
    row.update(
        netqty=str(net),
        netavgprc=average(net_avg),
        dayavgprc=average(net_avg),
        bep=average(net_avg),
        daybuyavgprc=average(avg(amount("daybuyamt"), qty("daybuyqty"))),
        daysellavgprc=average(avg(amount("daysellamt"), qty("daysellqty"))),
        cfbuyavgprc=average(avg(amount("cfbuyamt"), qty("cfbuyqty"))),
        cfsellavgprc=average(avg(amount("cfsellamt"), qty("cfsellqty"))),
        totbuyamt=money(buy_amt),
        totsellamt=money(sell_amt),
        totbuyavgprc=average(buy_avg),
        totsellavgprc=average(sell_avg),
        rpnl=money(min(buy_qty, sell_qty) * (sell_avg - buy_avg)),
    )


def _product_code(value: Any) -> str:
    """The API's product code for a product name the client accepts (``"MIS"``) or a code."""
    try:
        return Product(str(value)).value
    except ValueError:
        names = ", ".join(product.name for product in Product)
        raise ValueError(f"unknown product {value!r}; use {names} or the API's code") from None


def _normalise_position(values: dict[str, Any]) -> None:
    """Check a test-given position row: quantities become whole-number text, amounts must parse,
    a product name becomes the API's code."""
    if "prd" in values:
        values["prd"] = _product_code(values["prd"])
    for key in _replies.POSITION_QTY:
        if key in values:
            values[key] = str(_whole(values[key], key))
    for key in _replies.POSITION_AMOUNT:
        if key in values:
            _number(values[key], key)


class BrokerState:
    """Everything the offline broker knows; the test drives it, the strategy reads it."""

    def __init__(self, client_id: str = "OFFLINE01") -> None:
        self.client_id = client_id
        self.lock = threading.RLock()
        self.default_place = "open"
        self.default_reject_message = "RMS:Rejected by OfflineBroker"
        self.refuse_login_once = False
        self.silenced = False
        self.orders: list[OfflineOrder] = []
        self.requests: list[OfflineRequest] = []
        self.trades: list[dict[str, Any]] = []
        self.positions: list[dict[str, Any]] = []
        self.instruments: dict[tuple[str, str], dict[str, Any]] = {}
        self.quotes: dict[tuple[str, str], dict[str, Any]] = {}
        self.candles: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self.funds: dict[str, Any] = {}
        self.holdings: list[dict[str, Any]] = []
        self.user: dict[str, Any] = {}
        self.replies: dict[str, dict[str, Any] | list[Any]] = {}
        self._drills: dict[str, deque[Drill]] = {}
        self._listeners: list[Listener] = []
        # Events wait here (appended under ``lock``) and are delivered in order by ``_drain``.
        self._outbox: deque[dict[str, Any]] = deque()
        self._emit_lock = threading.RLock()
        self._day = datetime.now(IST).strftime("%y%m%d")
        self._order_seq = 0
        self._fill_seq = 0
        self._filled_value: dict[str, Decimal] = {}  # order id -> sum of fill price x quantity
        # GTTs (wire payloads plus "al_id"): pending ones in placement order, triggered ones with
        # the time they fired ("norentm"). A GTT fires only when a test calls trigger_gtt().
        self.gtts: list[dict[str, Any]] = []
        self.triggered_gtts: list[dict[str, Any]] = []
        self._gtt_seq = 0

    # --- setup ---------------------------------------------------------------------------

    def add_instrument(
        self,
        exchange: str,
        token: str,
        symbol: str,
        *,
        lot_size: int = 1,
        tick_size: Any = "0.05",
        instrument: str = "EQ",
        expiry: date | None = None,
        strike: Any = None,
        option_type: str | None = None,
        underlying: str | None = None,
        **wire_extra: Any,
    ) -> None:
        """Add an instrument (served by search, chains, quotes and the script master)."""
        row = _replies.instrument_row(
            exchange,
            token,
            symbol,
            lot_size=lot_size,
            tick_size=tick_size,
            instrument=instrument,
            expiry=expiry,
            strike=strike,
            option_type=option_type,
            underlying=underlying,
            extra=wire_extra,
        )
        with self.lock:
            self.instruments[(exchange, str(token))] = row

    def find_instrument(self, exchange: str, symbol: str) -> dict[str, Any] | None:
        """The instrument row with this trading symbol, or ``None``."""
        with self.lock:
            for (exch, _), row in self.instruments.items():
                if exch == exchange and row["tsym"] == symbol:
                    return row
        return None

    def set_quote(self, exchange: str, token: str, **fields: Any) -> None:
        """Set quote fields (friendly names such as ``last_price`` or wire keys)."""
        with self.lock:
            self.quotes.setdefault((exchange, str(token)), {}).update(_quote_fields(fields))

    def quote(self, exchange: str, token: str) -> dict[str, Any] | None:
        """The ``GetQuotes`` reply for an instrument, or ``None`` if no quote was set."""
        with self.lock:
            key = (exchange, str(token))
            fields = self.quotes.get(key)
            if fields is None:
                return None
            return _replies.quote(
                exchange, key[1], self.instruments.get(key), fields, datetime.now(IST)
            )

    def set_candles(self, exchange: str, token: str, candles: Iterable[Mapping[str, Any]]) -> None:
        """Set the candles served for an instrument (friendly names or wire keys).

        An unknown field name or a value that is not a number raises ``ValueError`` before any
        candle changes.
        """
        given = [dict(candle) for candle in candles]
        for candle in given:
            _check_candle(candle)
        with self.lock:
            self.candles[(exchange, str(token))] = given

    def set_funds(self, **wire: Any) -> None:
        """Set ``Limits`` fields (wire keys)."""
        with self.lock:
            self.funds.update({k: _replies.wire_value(v) for k, v in wire.items()})

    def set_holdings(self, rows: Iterable[Mapping[str, Any]]) -> None:
        """Set the ``Holdings`` records (wire keys)."""
        with self.lock:
            self.holdings = [dict(row) for row in rows]

    def set_positions(self, rows: Iterable[Mapping[str, Any]]) -> None:
        """Set the starting positions (wire keys; at least ``exch``, ``tsym``, ``prd``).

        A row with only ``netqty`` / ``netavgprc`` is carried forward as a buy or sell.
        Quantities may be whole-number text (``"10.0"``); anything else raises ``ValueError``
        before any position changes.
        """
        with self.lock:
            positions = []
            for given in rows:
                values = {k: _replies.wire_value(v) for k, v in given.items()}
                _normalise_position(values)
                row = self._new_position(
                    str(values.get("exch", "")),
                    str(values.get("tsym", "")),
                    str(values.get("prd", "")),
                )
                row.update(values)
                carried = ("cfbuyqty", "cfsellqty", "daybuyqty", "daysellqty")
                net = _whole(values.get("netqty"), "netqty")
                if net and not any(key in values for key in carried):
                    side = "buy" if net > 0 else "sell"
                    price = _number(values.get("netavgprc"), "netavgprc") or Decimal(0)
                    row[f"cf{side}qty"] = str(abs(net))
                    row[f"cf{side}amt"] = _replies.price_text(abs(net) * price)
                _recompute_position(row)
                positions.append(row)
            self.positions = positions

    def set_user(self, **wire: Any) -> None:
        """Set ``UserDetails`` fields (wire keys)."""
        with self.lock:
            self.user.update({k: _replies.wire_value(v) for k, v in wire.items()})

    def set_reply(self, endpoint: str, body: dict[str, Any] | list[Any]) -> None:
        """Answer ``endpoint`` with this body (for endpoints the offline broker does not model)."""
        with self.lock:
            self.replies[endpoint] = copy.deepcopy(body)

    # --- requests and drills ---------------------------------------------------------------

    def record(self, endpoint: str, payload: Mapping[str, Any]) -> OfflineRequest:
        """Record a REST call (secrets masked)."""
        request = OfflineRequest(endpoint, _mask(payload))
        with self.lock:
            self.requests.append(request)
        return request

    def fail_next(self, endpoint: str, drill: Drill) -> None:
        """Arm ``drill`` for the next call of ``endpoint`` (drills queue up per endpoint)."""
        if not isinstance(drill, Drill):
            raise TypeError(
                "fail_next needs a drill such as drills.reject('...') "
                f"(call the function), not {drill!r}"
            )
        with self.lock:
            self._drills.setdefault(endpoint, deque()).append(drill)

    def take_drill(self, endpoint: str) -> Drill | None:
        """Remove and return the next drill armed for ``endpoint``, or ``None``."""
        with self.lock:
            queue = self._drills.get(endpoint)
            return queue.popleft() if queue else None

    # --- events ------------------------------------------------------------------------------

    def add_listener(self, fn: Listener) -> None:
        """Call ``fn`` with every stream message (``om``, ``pm``, ``tf``, ``df``)."""
        with self.lock:
            self._listeners.append(fn)

    def _drain(self) -> None:
        """Deliver queued events one by one, in the order the changes were made.

        Listeners run without ``lock`` (another thread may change the state meanwhile) but under
        ``_emit_lock``, so two threads never deliver interleaved. A change made by a listener is
        queued behind the events already waiting and delivered after them.
        """
        with self._emit_lock:
            while True:
                with self.lock:
                    if not self._outbox:
                        return
                    event = self._outbox.popleft()
                    listeners = list(self._listeners)
                for fn in listeners:
                    try:
                        fn(event)
                    except Exception:
                        _log.exception(
                            "OfflineBroker stream listener failed on a %r message", event["t"]
                        )

    def tick(self, exchange: str, token: str, **fields: Any) -> None:
        """Update the quote and push the change as touchline (``tf``) and depth (``df``) feeds."""
        wire = _quote_fields(fields)
        now = datetime.now(IST)
        with self.lock:
            self.quotes.setdefault((exchange, str(token)), {}).update(wire)
            self._outbox.append(_replies.touchline_feed(exchange, str(token), wire, now))
            self._outbox.append(_replies.depth_feed(exchange, str(token), wire, now))
        self._drain()

    # --- orders: strategy side -----------------------------------------------------------
    # Every change is computed and checked first and applied last, so a failure changes nothing.

    def place(self, payload: Mapping[str, Any]) -> OfflineOrder:
        """Book a new order as ``default_place`` says (``open``, ``pending`` or ``reject``)."""
        with self.lock:
            order = self._book(payload)
        self._drain()
        return order

    def _book(self, payload: Mapping[str, Any]) -> OfflineOrder:
        """``place`` with the lock held and without delivering its events."""
        if self.default_place not in _DEFAULT_PLACE:
            raise ValueError(
                f"default_place must be 'open', 'pending' or 'reject', not {self.default_place!r}"
            )
        order_type = str(payload.get("prctyp", ""))
        for leg in ("blprc", "bpprc", "trailprc"):
            _number(payload.get(leg), leg)
        order = OfflineOrder(
            id=f"{self._day}{self._order_seq + 1:08d}",
            exchange=str(payload.get("exch", "")),
            symbol=str(payload.get("tsym", "")),
            side=str(payload.get("trantype", "")),
            quantity=_whole(payload.get("qty"), "qty"),
            price=_order_price(order_type, payload.get("prc")),
            trigger_price=_number(payload.get("trgprc"), "trgprc"),
            order_type=order_type,
            product=str(payload.get("prd", "")),
            status="PENDING",
            payload=_mask(payload),
        )
        now = datetime.now(IST)
        steps = [self._rows(order, "NewAck", now)]
        final: dict[str, Any] = {}
        if self.default_place == "open":
            final = {"status": _working_status(order_type)}
            steps.append(self._rows(dataclasses.replace(order, **final), "New", now))
        elif self.default_place == "reject":
            final = {"status": "REJECTED", "reject_reason": self.default_reject_message}
            steps.append(self._rows(dataclasses.replace(order, **final), "Rejected", now))
        self._order_seq += 1
        for name, value in final.items():
            setattr(order, name, value)
        order.history[:] = [row for row, _ in reversed(steps)]
        self.orders.append(order)
        self._outbox.extend(message for _, message in steps)
        return order

    def modify(self, payload: Mapping[str, Any]) -> tuple[bool, str]:
        """Apply a ``ModifyOrder``: ``(True, order id)`` or ``(False, reason)``."""
        order_id = str(payload.get("norenordno", ""))
        with self.lock:
            order = self._find(order_id)
            if order is None:
                return False, "Order not found to Modify"
            if order.status not in OPEN_STATES:
                return False, "order is not open to modify"
            order_type = str(payload.get("prctyp", order.order_type))
            changes: dict[str, Any] = {"order_type": order_type}
            if "qty" in payload:
                quantity = _whole(payload["qty"], "qty")
                if quantity <= order.filled_quantity:
                    return False, (
                        f"quantity must be more than the filled quantity {order.filled_quantity}"
                    )
                changes["quantity"] = quantity
            if "prc" in payload or order_type in _NO_PRICE:
                changes["price"] = _order_price(order_type, payload.get("prc"))
            if "trgprc" in payload:
                changes["trigger_price"] = _number(payload["trgprc"], "trgprc")
            elif not order_type.startswith("SL"):
                changes["trigger_price"] = None
            if order_type != order.order_type and order.status != "PENDING":
                changes["status"] = _working_status(order_type)
            self._update(order, "Replaced", changes)
        self._drain()
        return True, order_id

    def cancel_request(self, order_id: str) -> tuple[bool, str]:
        """Apply a ``CancelOrder``: ``(True, order id)`` or ``(False, reason)``."""
        with self.lock:
            order = self._find(order_id)
            if order is None:
                return False, "Order not found to Cancel"
            if order.status not in OPEN_STATES:
                return False, "order is not open to cancel"
            self._update(order, "Canceled", {"status": "CANCELED"})
        self._drain()
        return True, order_id

    def exit_request(self, order_id: str) -> tuple[bool, str]:
        """Apply an ``ExitSNOOrder``: ``(True, order id)`` or ``(False, reason)``.

        Only a cover (``H``) or bracket (``B``) order can be exited (documented); the offline broker
        cancels it (assumed: it models no SNO legs, so the test fills any square-off itself).
        """
        with self.lock:
            order = self._find(order_id)
            if order is None:
                return False, "Order not found to Exit"
            if order.product not in _EXITABLE:
                return False, "order is not a cover or bracket order"
            if order.status not in OPEN_STATES:
                return False, "order is not open to exit"
            self._update(order, "Canceled", {"status": "CANCELED"})
        self._drain()
        return True, order_id

    # --- GTTs --------------------------------------------------------------------------------

    @staticmethod
    def _gtt_fields(payload: Mapping[str, Any]) -> dict[str, Any]:
        """The GTT's fields from a Place / Modify payload; ``ValueError`` if one is unusable."""
        missing = [key for key in _GTT_FIELDS if not str(payload.get(key, "")).strip()]
        if missing:
            raise ValueError(f"{', '.join(missing)} required")
        _number(payload["d"], "d")
        _number(payload["prc"], "prc")
        _number(payload.get("trgprc"), "trgprc")
        _whole(payload["qty"], "qty")
        return {k: v for k, v in _mask(payload).items() if k not in ("uid", "al_id")}

    def place_gtt(self, payload: Mapping[str, Any]) -> str:
        """Store a ``PlaceGTTOrder`` as a pending GTT and return its alert id."""
        fields = self._gtt_fields(payload)
        with self.lock:
            self._gtt_seq += 1
            alert_id = f"{self._day}{self._gtt_seq:08d}"
            self.gtts.append({"al_id": alert_id, **fields})
        return alert_id

    def modify_gtt(self, payload: Mapping[str, Any]) -> tuple[bool, str]:
        """Apply a ``ModifyGTTOrder``: ``(True, alert id)`` or ``(False, reason)``."""
        alert_id = str(payload.get("al_id", ""))
        fields = self._gtt_fields(payload)
        with self.lock:
            gtt = self._pending_gtt(alert_id)
            if gtt is None:
                return False, "GTT not found to Modify"
            if fields["ai_t"] != gtt["ai_t"]:  # documented: the alert type can't be modified
                return False, "ai_t cannot be modified"
            gtt.clear()
            gtt.update({"al_id": alert_id, **fields})
        return True, alert_id

    def cancel_gtt(self, alert_id: str) -> tuple[bool, str]:
        """Apply a ``CancelGTTOrder``: ``(True, alert id)`` or ``(False, reason)``."""
        with self.lock:
            gtt = self._pending_gtt(alert_id)
            if gtt is None:
                return False, "GTT not found to Cancel"
            self.gtts.remove(gtt)
        return True, alert_id

    def trigger_gtt(self, alert_id: str) -> str:
        """Fire a pending GTT: place its order (``add_ord_id`` = the alert id) as ``place`` does,
        list the GTT as triggered and return the new order id."""
        with self.lock:
            gtt = self._pending_gtt(alert_id)
            if gtt is None:
                raise ValueError(f"no pending GTT with alert id {alert_id!r}")
            order_payload = {k: gtt[k] for k in _GTT_ORDER_FIELDS if k in gtt}
            order_payload.update({"uid": self.client_id, "actid": self.client_id})
            order_payload["add_ord_id"] = alert_id
            order = self._book(order_payload)  # checks first: a failure changes nothing
            self.gtts.remove(gtt)
            self.triggered_gtts.append({**gtt, "norentm": datetime.now(IST)})
        self._drain()
        return order.id

    def _pending_gtt(self, alert_id: str) -> dict[str, Any] | None:
        return next((gtt for gtt in self.gtts if gtt["al_id"] == alert_id), None)

    # --- orders: test side -----------------------------------------------------------------

    def accept(self, order_id: str) -> None:
        """PENDING -> OPEN (TRIGGER_PENDING for a stop order)."""
        with self.lock:
            order = self._require(order_id, ("PENDING",), "accept")
            self._update(order, "New", {"status": _working_status(order.order_type)})
        self._drain()

    def reject(self, order_id: str, message: str) -> None:
        """Reject an open order with ``message`` as the reason."""
        with self.lock:
            order = self._require(order_id, OPEN_STATES, "reject")
            self._update(order, "Rejected", {"status": "REJECTED", "reject_reason": message})
        self._drain()

    def cancel(self, order_id: str) -> None:
        """Cancel an open order (as the exchange would)."""
        with self.lock:
            order = self._require(order_id, OPEN_STATES, "cancel")
            self._update(order, "Canceled", {"status": "CANCELED"})
        self._drain()

    def trigger(self, order_id: str) -> None:
        """TRIGGER_PENDING -> OPEN."""
        with self.lock:
            order = self._require(order_id, ("TRIGGER_PENDING",), "trigger")
            self._update(order, "New", {"status": "OPEN"})
        self._drain()

    def fill(self, order_id: str, price: Any, quantity: int | None = None) -> None:
        """Fill ``quantity`` (default: all that is open) at ``price``; partials accumulate."""
        with self.lock:
            order = self._require(order_id, _FILLABLE, "fill")
            remaining = order.quantity - order.filled_quantity
            count = remaining if quantity is None else quantity
            if not 1 <= count <= remaining:
                raise ValueError(f"fill quantity must be 1 to {remaining}, got {count}")
            fill_price = _number(price, "price")
            if fill_price is None:
                raise ValueError("fill needs a price")
            filled = order.filled_quantity + count
            value = self._filled_value.get(order.id, Decimal(0)) + fill_price * count
            changes: dict[str, Any] = {
                "filled_quantity": filled,
                "average_price": value / filled,
                "status": "COMPLETE" if filled == order.quantity else "OPEN",
            }
            fill_id = str(self._fill_seq + 1)
            now = datetime.now(IST)
            filled_order = dataclasses.replace(order, **changes)
            row, om = self._rows(filled_order, "Fill", now, fill=(fill_id, count, fill_price))
            trade = _replies.trade_row(
                filled_order,
                client_id=self.client_id,
                instrument=self.find_instrument(order.exchange, order.symbol),
                when=now,
                fill_id=fill_id,
                quantity=count,
                price=fill_price,
            )
            existing, position = self._position_after_fill(order, count, fill_price)
            pm = _replies.position_update(position)
            # Everything is computed: apply it.
            self._fill_seq += 1
            self._filled_value[order.id] = value
            for name, new_value in changes.items():
                setattr(order, name, new_value)
            order.history.insert(0, row)
            self.trades.append(trade)
            if existing is None:
                self.positions.append(position)
            else:
                existing.clear()
                existing.update(position)
            self._outbox.extend([om, pm])
        self._drain()

    # --- helpers (called with the lock held) -------------------------------------------------

    def _find(self, order_id: str) -> OfflineOrder | None:
        return next((order for order in self.orders if order.id == order_id), None)

    def _require(self, order_id: str, states: tuple[str, ...], action: str) -> OfflineOrder:
        order = self._find(order_id)
        if order is None:
            raise ValueError(f"no order with id {order_id!r}")
        if order.status not in states:
            raise ValueError(
                f"cannot {action} order {order_id}: it is {order.status}, not {' or '.join(states)}"
            )
        return order

    def _rows(
        self,
        order: OfflineOrder,
        report: str,
        when: datetime,
        *,
        fill: tuple[str, int, Decimal] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """The history row and the ``om`` message for the order in this state (no change made)."""
        instrument = self.find_instrument(order.exchange, order.symbol)
        row = _replies.history_row(
            order, client_id=self.client_id, instrument=instrument, when=when, report=report
        )
        message = _replies.order_update(
            order, client_id=self.client_id, when=when, report=report, fill=fill
        )
        return row, message

    def _update(self, order: OfflineOrder, report: str, changes: dict[str, Any]) -> None:
        """Build the rows for ``changes`` first, then apply them and queue the ``om``."""
        row, message = self._rows(dataclasses.replace(order, **changes), report, datetime.now(IST))
        for name, value in changes.items():
            setattr(order, name, value)
        order.history.insert(0, row)
        self._outbox.append(message)

    def _new_position(self, exchange: str, symbol: str, product: str) -> dict[str, Any]:
        instrument = self.find_instrument(exchange, symbol)
        return _replies.position_row(
            client_id=self.client_id,
            exchange=exchange,
            token=(instrument or {}).get("token", ""),
            symbol=symbol,
            product=product,
            instrument=instrument,
        )

    def _position_after_fill(
        self, order: OfflineOrder, count: int, price: Decimal
    ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        """The order's position row (or ``None`` if new) and its new values; nothing changed."""
        key = (order.exchange, order.symbol, order.product)
        existing = next(
            (r for r in self.positions if (r["exch"], r["tsym"], r["prd"]) == key), None
        )
        row = dict(existing) if existing is not None else self._new_position(*key)
        side = "buy" if order.side == "B" else "sell"
        qty_key, amount_key = f"day{side}qty", f"day{side}amt"
        row[qty_key] = str(_whole(row.get(qty_key), qty_key) + count)
        amount = _number(row.get(amount_key), amount_key) or Decimal(0)
        row[amount_key] = _replies.price_text(amount + price * count)
        _recompute_position(row)
        return existing, row
