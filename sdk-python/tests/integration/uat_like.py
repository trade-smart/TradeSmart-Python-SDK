"""A UAT-like market for dry runs of the UAT matrix on the offline broker (test helper).

It wraps ``tradesmart.testing.OfflineBroker`` through its public members only (``add_instrument``,
``set_quote``, ``default_place``, ``accept`` / ``reject`` / ``fill`` / ``cancel``,
``fail_next``, ``orders``): the offline broker's own surface does not grow. ``install(client)``
wraps one client's ``place_order`` and ``cancel_order`` on that instance, so every order placed
through it is decided right after the offline broker books it, as UAT behaved when observed:

- a BUY LIMIT at or above the last price fills at the last price; a SELL never fills
  (``fill_sells=False``, the closing-SELL known issue);
- MKT and SL-MKT are refused in the place reply with the API order check (``ALGO_CHK``), unless
  ``market="accept"`` (then a MKT order fills at the last price);
- off-tick prices, quantities that are not a lot multiple and prices beyond the circuits are
  acknowledged and then ``REJECTED``; EOS on NSE / NFO is refused the same way;
- an IOC LIMIT away from the market is cancelled by the exchange;
- BSE orders (and any symbol in ``stuck``) stay ``PENDING`` and a cancel changes nothing;
- a symbol in ``rms`` is refused in the place reply with an RMS ``RED:`` message.

Everything else rests ``OPEN`` (``TRIGGER_PENDING`` for a stop order). UAT behaviours a test may
switch on (seen live, 13 Nov 2026): ``fill_far`` (a symbol's LIMIT order away from the market
fills at the last price, in full or the given quantity), ``stop_open`` (a symbol's stop order
rests ``OPEN``, not ``TRIGGER_PENDING``) and ``band_refusal`` (a callable of the place keywords
that returns UAT's own band refusal, e.g. ``... BEYOND CIRCUIT LIMITS ...``: the order is
``REJECTED`` with it).
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from tests.live_orders.uat_matrix.contracts import Contract, Contracts
from tradesmart import TradeSmart
from tradesmart.enums import OrderType, TransactionType, Validity
from tradesmart.testing import OfflineBroker, drills

ALGO_MKT = "Rejected : ALGO_CHK: MKT Order type not allowed for API order"
ALGO_SL_MKT = "Rejected : ALGO_CHK: SL-MKT Order type not allowed for API order"
RMS = "RED:RULE:{Turnover limit} Current: INR 14,81,980.50 Set: INR 1,00,000.00"


@dataclass
class Instrument:
    exchange: str
    token: str
    tradingsymbol: str
    last: Decimal
    tick: Decimal
    lot: int
    lower: Decimal
    upper: Decimal
    kind: str
    role: str | None


@dataclass
class UatLikeMarket:
    """The UAT-like policy over one ``OfflineBroker`` (see the module docstring)."""

    broker: OfflineBroker
    fill_sells: bool = False
    market: str = "reject"  # "reject" (ALGO_CHK in the reply) or "accept" (a MKT fills)
    stuck: set[str] = field(default_factory=set)  # trading symbols whose orders stay PENDING
    rms: set[str] = field(default_factory=set)  # trading symbols refused with an RMS RED:
    reject_all: str | None = None  # every accepted order is REJECTED with this message
    fill_far: dict[str, int | None] = field(default_factory=dict)  # symbol -> qty (None: all)
    stop_open: set[str] = field(default_factory=set)  # symbols whose stop orders rest OPEN
    band_refusal: Callable[[dict[str, Any]], str | None] | None = None  # UAT's own band
    instruments: dict[str, Instrument] = field(default_factory=dict)
    on_place: Callable[[dict[str, Any]], None] | None = None  # called before each place
    cancels: list[str] = field(default_factory=list)  # every cancel_order call, in order
    max_open_seen: int = 0
    # Test-only: the first ``rendezvous`` place calls wait for each other OUTSIDE the policy lock
    # (a barrier, no sleep), so places that overlap are proven deterministically.
    rendezvous: int = 0
    max_overlap: int = 0  # most place calls seen inside the rendezvous at once
    _arrived: int = 0
    _barrier: threading.Barrier | None = None
    _lock: threading.RLock = field(default_factory=threading.RLock)
    _overlap_lock: threading.Lock = field(default_factory=threading.Lock)
    _inside: int = 0

    # -- instruments ------------------------------------------------------------------------

    def add(
        self,
        exchange: str,
        token: str,
        tradingsymbol: str,
        *,
        last: str,
        tick: str = "0.05",
        lot: int = 1,
        lower: str | None = None,
        upper: str | None = None,
        role: str | None = None,
        kind: str = "eq",
        **instrument: Any,
    ) -> None:
        price = Decimal(last)
        lc = (
            Decimal(lower)
            if lower is not None
            else (price * Decimal("0.8")).quantize(Decimal(tick))
        )
        uc = (
            Decimal(upper)
            if upper is not None
            else (price * Decimal("1.2")).quantize(Decimal(tick))
        )
        self.broker.add_instrument(
            exchange, token, tradingsymbol, lot_size=lot, tick_size=tick, **instrument
        )
        self.instruments[tradingsymbol] = Instrument(
            exchange, token, tradingsymbol, price, Decimal(tick), lot, lc, uc, kind, role
        )
        self.set_last(tradingsymbol, last)

    def set_last(self, tradingsymbol: str, last: str) -> None:
        inst = self.instruments[tradingsymbol]
        inst.last = Decimal(last)
        self.broker.set_quote(
            inst.exchange, inst.token, lp=last, ti=str(inst.tick), ls=str(inst.lot),
            lc=str(inst.lower), uc=str(inst.upper),
        )  # fmt: skip

    def contracts(self, day: date) -> Contracts:
        """The run's contracts: one per instrument added with a ``role``."""
        result = Contracts(day=day)
        for inst in self.instruments.values():
            if inst.role is None:
                continue
            result.contracts[inst.role] = Contract(
                role=inst.role,
                exchange=inst.exchange,
                tradingsymbol=inst.tradingsymbol,
                token=inst.token,
                symbol="" if inst.kind == "eq" else inst.tradingsymbol,
                kind=inst.kind,
                lot_size=inst.lot,
                tick_size=inst.tick,
                resolved_last_price=inst.last if inst.last > 0 else Decimal("1"),
                resolved_lower_circuit=inst.lower,
                resolved_upper_circuit=inst.upper,
            )
        return result

    # -- the policy ---------------------------------------------------------------------------

    def install(self, client: TradeSmart) -> TradeSmart:
        """Wrap this client's ``place_order`` and ``cancel_order`` with the policy."""
        self.broker.default_place = "pending"  # the policy decides every new order
        place: Callable[..., str] = client.place_order
        cancel: Callable[[str], str] = client.cancel_order

        def placed(**kwargs: Any) -> str:
            if self.rendezvous:
                self._wait_outside_the_lock()
            with self._lock:
                if self.on_place is not None:
                    self.on_place(kwargs)
                return self._decide(place, kwargs)

        def cancelled(order_id: str) -> str:
            with self._lock:
                self.cancels.append(order_id)
                order = next((o for o in self.broker.orders if o.id == order_id), None)
                if order is not None and self._sticks(order.exchange, order.symbol):
                    return order_id  # acknowledged; the order stays PENDING (stuck)
            return cancel(order_id)

        client.place_order = placed  # type: ignore[method-assign]
        client.cancel_order = cancelled  # type: ignore[method-assign]
        return client

    def _wait_outside_the_lock(self) -> None:
        with self._overlap_lock:
            if self._arrived >= self.rendezvous:
                return  # the rendezvous is over: later places pass straight through
            self._arrived += 1
            if self._barrier is None:
                self._barrier = threading.Barrier(self.rendezvous, timeout=10.0)
            barrier = self._barrier
            self._inside += 1
            self.max_overlap = max(self.max_overlap, self._inside)
        try:
            barrier.wait()  # BrokenBarrierError after 10 s: the places did not overlap
        finally:
            with self._overlap_lock:
                self._inside -= 1

    def open_count(self) -> int:
        return sum(
            1 for o in self.broker.orders if o.status in ("PENDING", "OPEN", "TRIGGER_PENDING")
        )

    def _sticks(self, exchange: str, tradingsymbol: str) -> bool:
        return exchange == "BSE" or tradingsymbol in self.stuck

    def _decide(self, place: Callable[..., str], kwargs: dict[str, Any]) -> str:
        order_type = OrderType(str(kwargs["order_type"]))
        tradingsymbol = str(kwargs["tradingsymbol"])
        if tradingsymbol in self.rms:
            self.broker.fail_next("PlaceOrder", drills.reject(RMS))
        elif order_type in (OrderType.MARKET, OrderType.SL_M) and self.market == "reject":
            message = ALGO_MKT if order_type is OrderType.MARKET else ALGO_SL_MKT
            self.broker.fail_next("PlaceOrder", drills.reject(message))
        order_id = place(**kwargs)  # raises on a refusal in the reply
        self.max_open_seen = max(self.max_open_seen, self.open_count())
        inst = self.instruments[tradingsymbol]
        if self._sticks(inst.exchange, tradingsymbol):
            return order_id  # PENDING, and stays so
        reason = self._invalid(inst, kwargs, order_type)
        if reason is None and self.reject_all is not None:
            reason = self.reject_all
        if reason is not None:
            self.broker.reject(order_id, reason)
            return order_id
        self.broker.accept(order_id)
        self._match(order_id, inst, kwargs, order_type)
        return order_id

    def _invalid(
        self, inst: Instrument, kwargs: dict[str, Any], order_type: OrderType
    ) -> str | None:
        if self.band_refusal is not None:
            refusal = self.band_refusal(kwargs)
            if refusal is not None:
                return refusal
        quantity = int(kwargs["quantity"])
        if quantity % inst.lot:
            return f"Rejected : ORA:Quantity {quantity} is not a multiple of lot size {inst.lot}.00"
        validity = Validity(str(kwargs.get("validity", "DAY")))
        if validity is Validity.EOS and inst.exchange in ("NSE", "NFO"):
            return f"Rejected : ORA:Invalid Retention Type EOS for {inst.exchange}"
        for name in ("price", "trigger_price"):
            value = kwargs.get(name)
            if value is None:
                continue
            price = Decimal(str(value))
            if price % inst.tick:
                return f"Rejected : ORA:Price {price} is not a multiple of tick size {inst.tick}"
            if price > inst.upper or price < inst.lower:
                return f"Rejected : ORA:Price {price} is outside the circuit limits"
        return None

    def _match(
        self, order_id: str, inst: Instrument, kwargs: dict[str, Any], order_type: OrderType
    ) -> None:
        buy = TransactionType(str(kwargs["transaction_type"])) is TransactionType.BUY
        if order_type is OrderType.MARKET:
            self.broker.fill(order_id, inst.last)
            return
        if order_type is not OrderType.LIMIT:
            if inst.tradingsymbol in self.stop_open:
                self.broker.trigger(order_id)  # UAT: the stop order rests OPEN
            return  # stop orders rest TRIGGER_PENDING
        price = Decimal(str(kwargs["price"]))
        marketable = price >= inst.last if buy else price <= inst.last
        if marketable and (buy or self.fill_sells):
            self.broker.fill(order_id, inst.last)
        elif inst.tradingsymbol in self.fill_far:  # UAT: filled at any price
            self.broker.fill(order_id, inst.last, self.fill_far[inst.tradingsymbol])
        elif Validity(str(kwargs.get("validity", "DAY"))) is Validity.IOC:
            self.broker.cancel(order_id)  # the exchange cancels an IOC that does not match


def hold_every_stock(market: UatLikeMarket, quantity: int = 10) -> None:
    """Holdings of ``quantity`` shares of every NSE / BSE stock of ``market``, so the matrix's CNC
    SELL cases go out (a CNC SELL needs sellable holdings or this run's CNC BUY fill)."""
    rows = [
        {
            "exch_tsym": [
                {
                    "exch": inst.exchange,
                    "tsym": inst.tradingsymbol,
                    "token": inst.token,
                    "ls": "1",
                    "ti": str(inst.tick),
                    "lp": str(inst.last),
                }  # fmt: skip
            ],
            "holdqty": str(quantity),
            "dpqty": "0",
            "usedqty": "0",
        }
        for inst in market.instruments.values()
        if inst.exchange in ("NSE", "BSE")
    ]
    market.broker.set_holdings(rows)
