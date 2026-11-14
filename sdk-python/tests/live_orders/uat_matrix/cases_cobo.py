"""UAT matrix family M5, cover and bracket orders and exit_order (``m_cobo``, owner answer U11).

Cases (ids ``cobo-<role>-<product>-<prctyp>-day-buy[-fill|-exit]``), on NSE-B, NFO-F and NFO-CE:

- **probe** (``co`` / ``bo`` LMT, far below the market, rests ``OPEN``): CO: modify the price (one
  tick up), then the book-loss price (one tick up), then ``order_margin`` with the order's id
  (read-only); BO: modify the book-loss, the book-profit and the trailing price in turn (one tick
  up each). Then the engine cancels it (once). A modify that is not acknowledged turns a passing
  probe into a record (undocumented on UAT);
- **stop** (``sl-lmt``, a stop BUY above the market, rests ``TRIGGER_PENDING``), cancelled;
- **market** (``mkt``; BO with ``market_protection``): refused with ALGO_CHK (F27, the engine's
  market path; an accepted one counts as a fill);
- **fill** (``-fill``, on NSE-T, the low-priced stock no other fill family uses, so the M4 leftovers
  do not hold the position ceiling): a CO / BO BUY at the last price (15:30 IST cut-off and the
  per-instrument position ceiling apply), followed until it fills; then one ``exit_order``
  (``calls.exit``): for its SNO leg when the book shows one carrying the entry's own tag (adopted
  by the ledger), else with the entry's own id (the documentation names "the SNO leg"; which id
  the server wants is recorded with each leg's ``snonum`` / ``sno_fillid``). After the exit the
  book is followed for up to the confirm window: every open row on the symbol and product that
  carries the entry's tag (new ones too) is followed and, when still open, cancelled once (an
  unknown cancel outcome is CHECK THE ACCOUNT); any open row there that is not an order of this
  run is CHECK THE ACCOUNT (the open-order invariant stops the run before the next batch; it is
  never touched). The exit is cleanup: the case keeps the engine's verdict unless that is the
  expected one (``record``; ``market order filled`` for an accepted MKT), which the exit's
  outcome then replaces (CHECK THE ACCOUNT overrides any);
- **exit on a regular order** (``cobo-nse-b-mis-lmt-day-buy-exit``): a far MIS LIMIT, then
  ``exit_order(id, "CO")`` on it (recorded), then the engine cancels it.

Every batch runs on the session's run (``family.run_cases``): a case is placed at most once per
run; the family's amended or skipped results replace the cached ones (``family.remember``). The
cases are registered for resume (``cases.register``).

Book-loss / book-profit / trailing prices are points away from the entry (the documented examples:
``blprc`` 2 on a price of 1,587), from the same fresh quote: 2 %, 2 % and 0.5 % of the last price,
at least one tick.

``prarr`` (U11): where the account's profile does not enable CO or BO on an exchange, only the
first probe of that product on that exchange is sent (it records the refusal) and every other case
of that product on that exchange skips; a probe refused while enabled (or not stated) also skips
the rest of its product on its exchange.
"""

from __future__ import annotations

import contextlib
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

from tests.live_orders.safety import CHECK_THE_ACCOUNT, Refusal, ceil_to_tick, is_final, status_of
from tests.live_orders.uat_matrix import batch, calls, family
from tests.live_orders.uat_matrix import cases as case_table
from tests.live_orders.uat_matrix import contracts as contract_table
from tests.live_orders.uat_matrix.batch import BatchItem, CaseResult, MatrixRun, Placed
from tests.live_orders.uat_matrix.calls import ACKNOWLEDGED, RESOLVED, CaseRef, OrderOutcome
from tests.live_orders.uat_matrix.cases import (
    FULL,
    RECORD,
    REJECT,
    REST,
    SMOKE,
    TRIGGER_PENDING,
    Case,
)
from tests.live_orders.uat_matrix.contracts import Contract
from tests.live_orders.uat_matrix.family_tools import (
    amended,
    book_row,
    calls_of,
    fill_plan,
    skipped,
    supplement,
    wait_for_fill,
)
from tests.live_orders.uat_matrix.ledger import read_book
from tests.live_orders.uat_matrix.pricing import PriceSnapshot
from tradesmart.enums import Product
from tradesmart.models.orders import Order

FAMILY = "cobo"
ROLES = ("nse-b", "nfo-nifty-f", "nfo-nifty-ce-atm")
SMOKE_ROLE = "nse-b"
FILL_ROLE = "nse-t"
EXIT_ROLE = "nse-b"
PRODUCTS = ("CO", "BO")
FILL_SUFFIX = "-fill"
EXIT_SUFFIX = "-exit"
BOOK_LOSS = Decimal("0.02")  # book-loss points: 2 % of the last price
BOOK_PROFIT = Decimal("0.02")  # book-profit points (BO)
TRAIL = Decimal("0.005")  # trailing points (BO)
# UAT refused a smaller trailing price (13 Nov 2026: "trailing price check failed : Set : 1.00
# Current: 0.70"): at least 1.00, on the tick. A UAT setting, not an SDK rule.
MIN_TRAIL = Decimal("1.00")
MARKET_PROTECTION = Decimal("5")  # percent (BO MKT)
_ACCEPTED = (ACKNOWLEDGED, RESOLVED)


def _case(role: str, product: str, prctyp: str, outcome: str, *, suffix: str = "",
          tier: str = FULL) -> Case:  # fmt: skip
    return Case(
        id=f"{FAMILY}-{role}-{product}-{prctyp}-day-buy{suffix}".lower(),
        family=FAMILY,
        role=role,
        product=product,
        prctyp=prctyp,
        ret="DAY",
        side="BUY",
        orders=1,
        outcome=outcome,
        tier=tier,
    )


def family_cases() -> list[Case]:
    """Every M5 case: per role and product the probe, the stop and the market case; the two
    fill cases; the exit on a regular order."""
    result: list[Case] = []
    for role in ROLES:
        for product in PRODUCTS:
            tier = SMOKE if role == SMOKE_ROLE else FULL
            result.append(_case(role, product, "LMT", REST, tier=tier))
            result.append(_case(role, product, "SL-LMT", TRIGGER_PENDING))
            result.append(_case(role, product, "MKT", REJECT))  # F27: the oracle expects ALGO_CHK
    result += [_case(FILL_ROLE, p, "LMT", RECORD, suffix=FILL_SUFFIX) for p in PRODUCTS]
    result.append(_case(EXIT_ROLE, "MIS", "LMT", RECORD, suffix=EXIT_SUFFIX))
    return result


case_table.register(family_cases())  # a resumed run restores this family's state too


def is_probe(case: Case) -> bool:
    return case.product in PRODUCTS and case.prctyp == "LMT" and not case.id.endswith(FILL_SUFFIX)


def is_fill(case: Case) -> bool:
    return case.id.endswith(FILL_SUFFIX)


def _key(case: Case) -> tuple[str, str]:
    return case.exchange, case.product


# -- plans ------------------------------------------------------------------------------------


def legs(case: Case, contract: Contract, snapshot: PriceSnapshot) -> dict[str, Any]:
    """The cover / bracket fields of ``case``, in points, from the fresh snapshot."""
    if case.product not in PRODUCTS:
        return {}
    tick, last = snapshot.tick_size, snapshot.last_price

    def points(ratio: Decimal) -> Decimal:
        return max(tick, ceil_to_tick(last * ratio, tick))

    fields: dict[str, Any] = {"book_loss_price": points(BOOK_LOSS)}
    if case.product == "BO":
        fields["book_profit_price"] = points(BOOK_PROFIT)
        fields["trail_price"] = max(points(TRAIL), ceil_to_tick(MIN_TRAIL, tick))
        if case.prctyp == "MKT":
            fields["market_protection"] = MARKET_PROTECTION
    return fields


def plan(case: Case, contract: Contract, snapshot: PriceSnapshot) -> dict[str, Any]:
    """The engine's default plan of ``case`` plus its cover / bracket fields."""
    result = batch.default_plan(case, contract, snapshot)
    result.update(legs(case, contract, snapshot))
    return result


# -- hooks ------------------------------------------------------------------------------------


def _message(outcome: OrderOutcome) -> str | None:
    error = outcome.error or {}
    value = error.get("emsg") or error.get("message")
    return None if value is None else str(value)


def _row_view(row: Order | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "status": status_of(row),
        "price": row.price,
        "book_loss_price": row.book_loss_price,
        "book_profit_price": row.book_profit_price,
        "trail_price": row.trail_price,
    }


def _modify_steps(placed: Placed, steps: Sequence[Mapping[str, Any]]) -> None:
    """One modify per step (never repeated), each with the latest book row as ``before``."""
    notes: list[dict[str, Any]] = placed.notes.setdefault("modifies", [])
    for changes in steps:
        outcome = placed.modify(**changes)
        entry: dict[str, Any] = {
            "changes": {k: str(v) for k, v in changes.items()},
            "verdict": outcome.verdict,
            "message": _message(outcome),
        }
        notes.append(entry)
        if outcome.check_the_account:
            return  # no further call for this order
        row = book_row(placed)
        entry["row"] = _row_view(row)
        if row is not None:
            placed.row = row


def _leg_fields(placed: Placed) -> dict[str, Decimal]:
    keys = ("book_loss_price", "book_profit_price", "trail_price")
    return {k: Decimal(str(placed.plan[k])) for k in keys if k in placed.plan}


def _co_probe(placed: Placed) -> None:
    """CO: modify the price, then the book-loss price; then its margin (read-only)."""
    tick = placed.snapshot.tick_size
    price = Decimal(str(placed.plan["price"])) + tick
    fields = _leg_fields(placed)
    first = {"price": price, **fields}
    second = {**first, "book_loss_price": fields["book_loss_price"] + tick}
    _modify_steps(placed, [first, second])
    _margin(placed, price, second["book_loss_price"])


def _bo_probe(placed: Placed) -> None:
    """BO: modify the book-loss, the book-profit and the trailing price in turn."""
    tick = placed.snapshot.tick_size
    fields = _leg_fields(placed)
    placed.notes["trail_minimum"] = f"trail_price at least {MIN_TRAIL} (UAT: Set : 1.00)"
    steps = []
    for name in ("book_loss_price", "book_profit_price", "trail_price"):
        fields = {**fields, name: fields[name] + tick}
        steps.append(dict(fields))
    _modify_steps(placed, steps)


def _margin(placed: Placed, price: Decimal, book_loss: Decimal) -> None:
    """``order_margin`` for the open cover order, with its order id (read-only)."""
    session = placed.run.session
    try:
        margin = session.client.order_margin(
            exchange=placed.plan["exchange"],
            tradingsymbol=placed.plan["tradingsymbol"],
            quantity=int(placed.plan["quantity"]),
            price=price,
            product=placed.plan["product"],
            transaction_type=placed.plan["transaction_type"],
            order_type=placed.plan["order_type"],
            book_loss_price=book_loss,
            order_id=placed.order_id,
        )
    except contract_table.SESSION_ERRORS:
        raise
    except contract_table.DATA_ERRORS as exc:
        placed.notes["order_margin_error"] = f"{type(exc).__name__}: {exc}"
        return
    placed.notes["order_margin"] = dict(margin.raw) or margin.model_dump(mode="json")


def _exit_regular(placed: Placed) -> None:
    """``exit_order(id, "CO")`` on a regular order (recorded; never repeated)."""
    assert placed.order_id is not None
    outcome = calls.exit(placed.run.session, placed.order_id, "CO", placed.ref)
    placed.calls.append(outcome)
    placed.notes["exit"] = {"product": "CO", "verdict": outcome.verdict,
                            "message": _message(outcome)}  # fmt: skip


def _item(case: Case) -> BatchItem:
    if case.id.endswith(EXIT_SUFFIX):
        return BatchItem(case, plan=plan, after_settle=_exit_regular)
    if is_probe(case):
        return BatchItem(
            case, plan=plan, after_settle=_co_probe if case.product == "CO" else _bo_probe
        )
    return BatchItem(case, plan=plan)


# -- the family -------------------------------------------------------------------------------


def _refused(result: CaseResult) -> str | None:
    """Why the probe's order was refused (place reply or the book), else ``None``."""
    places = calls_of(result, "place")
    verdict = places[0].get("verdict") if places else None
    if result.status == "REJECTED" or verdict in (calls.REJECTED, "not sent"):
        return result.reason
    return None


def _not_acknowledged(run: MatrixRun, case: Case, result: CaseResult) -> CaseResult:
    """A passing probe whose modify was not acknowledged is recorded (undocumented on UAT)."""
    modifies = (result.record.get("notes") or {}).get("modifies") or []
    refused = [m for m in modifies if m.get("verdict") not in _ACCEPTED]
    if result.verdict != batch.PASS or not refused:
        return result
    first = refused[0]
    reason = (
        f"{result.reason}; a modify was not acknowledged ({first.get('verdict')}): "
        f"{first.get('message')} (changes {first.get('changes')})"
    )
    return amended(run, case, result, batch.RECORD, reason, kind="modifies", modifies=modifies)


def run_family(run: MatrixRun, selected: Sequence[Case]) -> dict[str, CaseResult]:
    """Run the selected M5 cases; one result per case, by id, in the order given."""
    results: dict[str, CaseResult] = {}
    blocked: dict[tuple[str, str], str] = {}
    for case in selected:
        if (
            case.product in PRODUCTS
            and run.profile.product_enabled(case.product, case.exchange) is False
        ):
            blocked.setdefault(_key(case), f"{case.product} is not enabled on {case.exchange} in "
                               "the account's prarr")  # fmt: skip
    # 1. The probes (one per exchange where the product is not enabled).
    probes: list[Case] = []
    probed: set[tuple[str, str]] = set()
    for case in (c for c in selected if is_probe(c)):
        if _key(case) in blocked and _key(case) in probed:
            results[case.id] = skipped(run, case, f"{blocked[_key(case)]}; one probe per exchange "
                                       "records the refusal")  # fmt: skip
            continue
        probed.add(_key(case))
        probes.append(case)
    probe_results = family.run_cases(run, [_item(c) for c in probes])
    for case in probes:
        result = _not_acknowledged(run, case, probe_results[case.id])
        results[case.id] = result
        why = _refused(result)
        if why is not None and _key(case) not in blocked:
            blocked[_key(case)] = f"the {case.product} probe {case.id} was refused: {why}"
    # 2. Stops, market orders, the exit on a regular order.
    others: list[Case] = []
    for case in (c for c in selected if not is_probe(c) and not is_fill(c)):
        if _key(case) in blocked:
            results[case.id] = skipped(run, case, blocked[_key(case)])
        else:
            others.append(case)
    earlier = {c.id for c in others if run.session.ledger.done(c.id)}  # no second exit
    other_results = family.run_cases(run, [_item(c) for c in others])
    for case in others:
        result = other_results[case.id]
        contract = run.contracts.get(case.role)
        filled_market = result.verdict == batch.MARKET_FILL and case.product in PRODUCTS
        follow = contract is not None and not run.session.interrupted and case.id not in earlier
        if filled_market and follow and contract is not None:
            # An accepted MKT cover / bracket order filled (F27 did not show): exit its legs.
            result = _exit(run, case, contract, result, done=batch.MARKET_FILL)
        results[case.id] = result
    # 3. The fills, one at a time, each followed by its exit.
    for case in (c for c in selected if is_fill(c)):
        if _key(case) in blocked:
            results[case.id] = skipped(run, case, blocked[_key(case)])
            continue
        results[case.id] = _fill_and_exit(run, case)
    return {c.id: results[c.id] for c in selected}


# -- fill, then exit ---------------------------------------------------------------------------


def _fill_and_exit(run: MatrixRun, case: Case) -> CaseResult:
    contract = run.contracts.get(case.role)
    hold = (
        run.positions.one_at_a_time(contract.tradingsymbol)
        if contract is not None
        else contextlib.nullcontext()
    )
    with hold:
        item = BatchItem(
            case, plan=fill_plan(run, side="BUY", extra=legs), after_settle=wait_for_fill()
        )
        earlier = run.session.ledger.done(case.id)  # run before: its result, no second exit
        result = family.run_cases(run, [item])[case.id]
        if contract is None or not result.order_ids or run.session.interrupted or earlier:
            return result
        return _exit(run, case, contract, result)


def _filled(row: Order | None) -> int:
    if row is None:
        return 0
    filled = int(row.filled_quantity or 0)
    if not filled and status_of(row) == "COMPLETE":
        filled = int(row.quantity or 0)
    return filled


def _id(row: Order) -> str:
    return (row.order_id or "").strip()


def _leg_view(row: Order | None, entry_tag: str | None) -> dict[str, Any]:
    if row is None:
        return {}
    return {
        "order_id": _id(row),
        "status": status_of(row),
        "filled": _filled(row),
        "entry_tag": bool(entry_tag) and (row.tag or "").strip() == entry_tag,
        "snonum": row.sno_number,
        "sno_fillid": row.sno_fillid,
    }


class _Legs:
    """The rows of one filled cover / bracket entry to follow: on its symbol and product, not
    final when first seen, other than the entry; those carrying the entry's tag (its legs, made
    ours by the ledger) and those that are not orders of this run (foreign). Our other orders
    (other tags) are never part of it."""

    def __init__(self, run: MatrixRun, contract: Contract, product: Product, entry: str) -> None:
        self.run, self.contract, self.product, self.entry = run, contract, product, entry
        self.entry_tag = run.session.ledger.tag_of(entry)
        self.rows: dict[str, Order] = {}

    def own(self, row: Order) -> bool:
        return bool(self.entry_tag) and (row.tag or "").strip() == self.entry_tag

    def take(self, book: Sequence[Order]) -> None:
        ledger = self.run.session.ledger
        for row in book:
            order_id = _id(row)
            if order_id in self.rows:
                self.rows[order_id] = row
                continue
            if order_id in ("", self.entry) or is_final(status_of(row)):
                continue
            if row.tradingsymbol != self.contract.tradingsymbol or row.product != self.product:
                continue
            if self.own(row):
                ledger.adopt(row)  # the entry's own tag: ours (one of this run's place tags)
                self.rows[order_id] = row
            elif ledger.tag_of(order_id) is None:
                self.rows[order_id] = row  # not an order of this run: followed, never touched

    def ours(self) -> list[str]:
        return [i for i, row in self.rows.items() if self.own(row)]

    def foreign(self) -> list[str]:
        return [i for i, row in self.rows.items() if not self.own(row)]

    def open(self) -> list[str]:
        return [i for i, row in self.rows.items() if not is_final(status_of(row))]

    def follow(self) -> None:
        """Read the book for up to the confirm window (read-only), taking new rows too, until
        every row followed is final (with none seen yet, the whole window)."""
        session = self.run.session
        deadline = session.clock() + session.confirm_seconds
        while True:
            self.take(read_book(session.client, attempts=1) or [])
            if (self.rows and not self.open()) or session.clock() >= deadline:
                return
            session.sleep(session.poll_interval)


def _check(run: MatrixRun, case: Case, result: CaseResult, reason: str,
           **detail: Any) -> CaseResult:  # fmt: skip
    """CHECK THE ACCOUNT for the case; the segment's circuit breaker trips (as in the engine)."""
    run.session.guard.note_check_the_account(case.segment, reason)
    return amended(run, case, result, batch.CHECK, reason, kind="exit", **detail)


def _exit(
    run: MatrixRun, case: Case, contract: Contract, result: CaseResult, *, done: str = batch.RECORD
) -> CaseResult:
    """After a filled entry: one ``exit_order``, then its legs are followed (and ours still
    open cancelled once). The case ends with the exit's outcome only when the engine's verdict
    is ``done``; else the engine's verdict stays and the exit is a supplement. CHECK THE ACCOUNT
    (an unknown outcome, a foreign row left open, a leg not final) overrides any verdict."""
    session = run.session
    entry = result.order_ids[0]
    ref = CaseRef(case.id, case.segment)
    book = read_book(session.client, attempts=session.lookup_attempts,
                     interval=session.lookup_interval, sleep=session.sleep)  # fmt: skip
    if book is None:
        reason = (f"{CHECK_THE_ACCOUNT}: the order book could not be read after the {case.product} "
                  "entry; its SNO legs are unknown (no exit sent)")  # fmt: skip
        return _check(run, case, result, reason)
    entry_row = next((o for o in book if _id(o) == entry), None)
    filled = _filled(entry_row)
    if not filled:
        return result  # nothing filled: the engine cancelled the entry; nothing to exit
    legs = _Legs(run, contract, Product(case.product), entry)
    legs.take(book)
    ours, foreign = legs.ours(), legs.foreign()
    if ours:
        target = ours[0]
        how = f"exit_order sent for the SNO leg {target} (the entry's tag)"
    elif foreign:
        target = entry
        how = (f"SNO leg(s) {', '.join(foreign)}: not an order of this run (not tagged as ours), "
               "never touched; exit_order sent with the entry order id")  # fmt: skip
    else:
        target = entry
        how = "no SNO leg seen in the book after the fill; exit_order sent with the entry order id"
    try:
        outcome = calls.exit(session, target, case.product, ref)
    except Refusal as exc:
        reason = f"{CHECK_THE_ACCOUNT}: exit_order refused before sending: {exc.reason}; {how}"
        return _check(run, case, result, reason, legs=list(legs.rows))
    legs.follow()  # after the exit: rows that show only now are followed too
    cancels: list[OrderOutcome] = []
    for leg_id in legs.ours():
        if is_final(status_of(legs.rows[leg_id])):
            continue
        with contextlib.suppress(Refusal):  # cancelled once already, or flagged: never again
            cancels.append(calls.cancel(session, leg_id, ref))
    if cancels:
        legs.follow()
    open_after = legs.open()
    leg_fills = sum(_filled(row) for row in legs.rows.values() if legs.own(row))
    run.positions.close_attempt(contract.tradingsymbol, leg_fills, case_id=case.id,
                                order_id=target, exit=True)  # fmt: skip
    detail: dict[str, Any] = {
        "exit": {"order_id": target, "product": case.product, "verdict": outcome.verdict,
                 "message": _message(outcome)},
        "exit_order_id": target,
        "entry_filled": filled,
        "legs": list(legs.rows),
        "leg_rows": [_leg_view(row, legs.entry_tag) for row in legs.rows.values()],
        "legs_final": {i: status_of(r) for i, r in legs.rows.items()},
        "legs_open_after": open_after,
        "leg_fills": leg_fills,
        "cancelled": [f"{c.order_id}: {c.verdict}" for c in cancels],
        "how": how,
    }  # fmt: skip
    exit_words = f"exit {outcome.verdict}" + (
        f" ({_message(outcome)})" if _message(outcome) else ""
    )
    if outcome.check_the_account:  # calls.exit tripped the breaker already
        reason = f"{CHECK_THE_ACCOUNT}: the exit_order outcome is unknown; {how}"
        return amended(run, case, result, batch.CHECK, reason, kind="exit", **detail)
    unknown = [c for c in cancels if c.check_the_account]
    if unknown:  # calls.cancel tripped the breaker already
        reason = (f"{CHECK_THE_ACCOUNT}: the cancel of SNO leg {unknown[0].order_id} has an "
                  f"unknown outcome; {how}; {exit_words}")  # fmt: skip
        return amended(run, case, result, batch.CHECK, reason, kind="exit", **detail)
    still_foreign = [i for i in open_after if i in legs.foreign()]
    if still_foreign:
        reason = (
            f"{CHECK_THE_ACCOUNT}: {how}; {exit_words}; leg(s) {', '.join(still_foreign)} still "
            "open, not orders of this run: the open-order invariant stops the run before the "
            "next batch"
        )
        return _check(run, case, result, reason, **detail)
    if open_after:
        for leg_id in open_after:  # ours, cancelled once and not final: stuck
            session.guard.note_stuck(case.segment, leg_id)
            session.ledger.note("stuck", order_id=leg_id, segment=case.segment)
        reason = (f"{CHECK_THE_ACCOUNT}: {how}; {exit_words}; leg(s) {', '.join(open_after)} "
                  "not final after their cancel")  # fmt: skip
        return _check(run, case, result, reason, **detail)
    kept = run.positions.net(contract.tradingsymbol)
    reason = (
        f"{case.product} BUY filled {filled}; {how}; {exit_words}; legs final "
        f"{detail['legs_final'] or '(none seen)'}; the run's position {kept} stays in the "
        "position ledger"
    )
    if result.verdict != done:  # the exit was cleanup: the engine's verdict stays
        supplement(run, case, "exit", verdict=result.verdict, reason=reason,
                   engine_verdict=result.verdict, **detail)  # fmt: skip
        return result
    return amended(run, case, result, done, reason, kind="exit", **detail)
