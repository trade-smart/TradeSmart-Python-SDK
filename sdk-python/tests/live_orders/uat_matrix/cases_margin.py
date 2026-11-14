"""The UAT matrix family M10: order margin (inventory §3.2, marker ``m_margin``). Read-only.

``GetOrderMargin`` (``client.order_margin``) for every M1 shape (role × product × price type ×
side; margin takes no validity), cover and bracket orders with ``blprc`` (NSE ``ITC-EQ`` and the
NIFTY future), and invalid products (CNC and MTF on F&O). Nothing is placed: the live test is
``tests/live/test_live_uat_margin.py`` (read-only live gates, UAT mode only).

Expectations (``expectation``): ``ok`` (``stat Ok``, ``ordermargin >= 0``, ``remarks`` present)
for the documented price types (LMT, SL-LMT) on products the account enables; ``record`` (U13)
for MKT / SL-MKT (the docs list only LMT and SL-LMT), a delivery SELL on cash (no holding),
cover / bracket orders the profile does not enable, and the invalid products (``Not_Ok``
expected, recorded). Prices come from a fresh quote (``pricing``), never zero (the docs: "Order
price cannot be zero"): a market shape is priced at the last price.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from tests.live_orders.safety import ceil_to_tick
from tests.live_orders.uat_matrix import batch
from tests.live_orders.uat_matrix import cases as case_table
from tests.live_orders.uat_matrix.cases import BASE_PRODUCT, DELIVERY_PRODUCTS, FULL, SMOKE, Case
from tests.live_orders.uat_matrix.contracts import Contract
from tests.live_orders.uat_matrix.expect import Profile
from tests.live_orders.uat_matrix.pricing import PriceSnapshot
from tradesmart.models.orders import OrderMargin

FAMILY = "margin"
OK, RECORD = "ok", "record"
MARKET_TYPES = frozenset({"MKT", "SL-MKT"})
COVER_PRODUCTS = frozenset({"CO", "BO"})
COVER_ROLES = ("nse-b", "nfo-nifty-f")
INVALID = (("nfo-nifty-f", "CNC"), ("nfo-nifty-f", "MTF"), ("nfo-nifty-ce-atm", "CNC"))
BOOK_LOSS = Decimal("0.02")  # blprc: 2 % of the price, in points (the docs' examples: "2")


@dataclass(frozen=True)
class MarginCase:
    id: str
    role: str
    product: str
    prctyp: str
    side: str
    tier: str = FULL
    invalid: bool = False

    @property
    def segment(self) -> str:
        return self.role.split("-", 1)[0]

    @property
    def exchange(self) -> str:
        return case_table.SEGMENT_EXCHANGE[self.segment]

    @property
    def kind(self) -> str:
        return case_table._kind(self.role)


def _margin_case(
    role: str, product: str, prctyp: str, side: str, *, tier: str = FULL, invalid: bool = False
) -> MarginCase:
    return MarginCase(
        id=f"{FAMILY}-{role}-{product}-{prctyp}-{side}".lower(),
        role=role,
        product=product,
        prctyp=prctyp,
        side=side,
        tier=tier,
        invalid=invalid,
    )


def _build() -> list[MarginCase]:
    table: list[MarginCase] = []
    seen: set[tuple[str, str, str, str]] = set()
    for case in case_table.CASES:
        if case.family != "shapes":
            continue
        key = (case.role, case.product, case.prctyp, case.side)
        if key in seen:
            continue
        seen.add(key)
        smoke = (
            case.role in case_table.SMOKE_ROLES
            and case.product == BASE_PRODUCT[case_table._kind(case.role)]
            and case.prctyp == "LMT"
        )
        table.append(_margin_case(*key, tier=SMOKE if smoke else FULL))
    for role in COVER_ROLES:
        for product in ("CO", "BO"):
            table.append(_margin_case(role, product, "LMT", "BUY"))
    for role, product in INVALID:
        table.append(_margin_case(role, product, "LMT", "BUY", invalid=True))
    return table


MARGIN_CASES: list[MarginCase] = _build()


def expectation(case: MarginCase, profile: Profile) -> str:
    """``ok`` or ``record`` (see the module docstring)."""
    if case.invalid or case.prctyp in MARKET_TYPES:
        return RECORD
    if case.kind == "eq" and case.product in DELIVERY_PRODUCTS and case.side == "SELL":
        return RECORD
    if profile.exchanges is not None and case.exchange not in profile.exchanges:
        return RECORD
    enabled = profile.product_enabled(case.product, case.exchange)
    if case.product in COVER_PRODUCTS:
        return OK if enabled else RECORD  # U11: only where prarr enables them
    return RECORD if enabled is False else OK


def margin_kwargs(case: MarginCase, contract: Contract, snapshot: PriceSnapshot) -> dict[str, Any]:
    """The ``order_margin`` keyword arguments of ``case``, priced from ``snapshot`` only."""
    shape = Case(
        id=case.id,
        family=FAMILY,
        role=case.role,
        product=case.product,
        prctyp=case.prctyp,
        ret="DAY",
        side=case.side,
        orders=1,
        outcome=case_table.REST,
    )
    plan = batch.default_plan(shape, contract, snapshot)
    price = plan.get("price")
    kwargs: dict[str, Any] = {
        "exchange": plan["exchange"],
        "tradingsymbol": plan["tradingsymbol"],
        "quantity": plan["quantity"],
        "price": snapshot.last_price if price is None else price,
        "product": case.product,
        "transaction_type": case.side,
        "order_type": case.prctyp,
    }
    if "trigger_price" in plan:
        kwargs["trigger_price"] = plan["trigger_price"]
    if case.product in COVER_PRODUCTS:
        tick = snapshot.tick_size
        kwargs["book_loss_price"] = max(tick, ceil_to_tick(kwargs["price"] * BOOK_LOSS, tick))
    return kwargs


def _message(error: BaseException) -> str:
    emsg = getattr(error, "emsg", None)
    return str(emsg) if emsg else f"{type(error).__name__}: {error}"


def judge(case: MarginCase, want: str, result: OrderMargin | BaseException) -> tuple[str, str]:
    """``(verdict, reason)`` of one margin read (``batch.PASS`` / ``FAIL`` / ``RECORD``)."""
    if isinstance(result, BaseException):
        message = _message(result)
        if want == RECORD:
            return batch.RECORD, f"recorded refusal: {message}"
        return batch.FAIL, f"refused: {message}"
    margin, stat, remarks = result.ordermargin, result.stat, result.remarks
    seen = f"stat {stat}, ordermargin {margin}, remarks {remarks!r}"
    if want == RECORD:
        return batch.RECORD, f"recorded: {seen}"
    if stat != "Ok":
        return batch.FAIL, f"stat is not Ok: {seen}"
    if margin is None or margin < 0:
        return batch.FAIL, f"ordermargin missing or negative: {seen}"
    if not (remarks or "").strip():
        return batch.FAIL, f"no remarks: {seen}"
    return batch.PASS, f"{seen} (case {case.id})"
