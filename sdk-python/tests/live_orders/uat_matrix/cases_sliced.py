"""The UAT matrix family M7: sliced orders (inventory §3.2; ``m_sliced``).

The batch engine places one order per call, so this family drives ``calls.place_sliced`` itself:
**one call per case, never resent** (all slices share the case's tag); the ledger's rules for a
partly sent sliced order apply as they are (``ledger.nothing_sent``: a slice id known means sent,
so a resume never places the case again). Each case, in table order (the cheapest first, so an
RMS turnover limit, N2 / U3, costs the fewest cases):

1. the run's checks (the open-order invariant, ``batch._prepare``: done in this run, the
   oracle, the RMS environment limit of the family, the breaker, the declared count = the
   slices), the BFO canary first for a BFO case (``family.run_cases``);
2. the sizing: ``LOTS`` = ``lots`` whole lots in slices of ``slice_lots`` lots
   (``max_slice_quantity``); ``FREEZE`` = 2 × the freeze slice + 1 lot, sliced by the SDK from
   ``security_info().frzqty`` with no ``max_slice_quantity`` (3 slices); MCX in trade units (U7);
3. a fresh quote, the far plan (``batch.default_plan``; the option freeze cases 20 % below the
   last price, ``deep_far_buy``), one ``calls.place_sliced`` (no ``MAX_OPEN`` room for all the
   slices: the case skips, nothing sent);
4. one book poll per round until every slice settled (``batch._settle``), one cancel per open
   slice and the confirm (``batch._cancel_and_confirm``: not final = stuck, the segment trips);
5. the verdict: every slice placed, in the planned quantities, distinct ids, the only orders with
   the case's tag, each resting ``OPEN`` (``CANCELED`` by the exchange for IOC, with no cancel
   from the run), nothing filled; ``disclosed_quantity`` per slice is recorded. A refusal (in
   the reply or ``REJECTED`` in the book) is judged by the oracle: RMS ``RED:`` is an
   environment limit (the family's later cases skip).

Turnover note (inventory N2): 2 × the NIFTY freeze quantity far below the market is a large
notional; the RMS refusal it may get is the environment-limit verdict, not a failure.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from tests.live_orders.safety import Refusal, ceil_to_tick, floor_to_tick, is_final, status_of
from tests.live_orders.uat_matrix import batch, calls, expect, family, pricing
from tests.live_orders.uat_matrix import cases as case_table
from tests.live_orders.uat_matrix.batch import BatchItem, CaseResult, MatrixRun
from tests.live_orders.uat_matrix.cases import (
    EXCHANGE_CANCEL,
    FULL,
    RECORD,
    REST,
    SMOKE,
    Case,
)
from tests.live_orders.uat_matrix.contracts import Contract
from tests.live_orders.uat_matrix.guard import STOP_REASON, MaxOpenReached, StopRun
from tests.live_orders.uat_matrix.ledger import INTERRUPTED, read_book
from tests.live_orders.uat_matrix.pricing import PriceSnapshot

FAMILY = "sliced"
LOTS = "lots"  # ``lots`` whole lots, slices of ``slice_lots`` lots (max_slice_quantity)
FREEZE = "freeze"  # 2 × the freeze slice + 1 lot, sliced by the SDK from frzqty (3 slices)


@dataclass(frozen=True)
class SlicedSpec:
    case: Case
    sizing: str
    lots: int = 2
    slice_lots: int = 1
    disclosed: int | None = None  # disclosed quantity of every slice (trade units)
    deep: bool = False  # priced DEEP below the last price (option freeze cases)


def _spec(
    role: str,
    product: str,
    ret: str,
    side: str,
    variant: str,
    sizing: str,
    *,
    lots: int = 2,
    slice_lots: int = 1,
    disclosed: int | None = None,
    deep: bool = False,
    outcome: str = REST,
    tier: str = FULL,
) -> SlicedSpec:
    orders = 3 if sizing == FREEZE else -(-lots // slice_lots)
    case = Case(
        id=f"{FAMILY}-{role}-{product}-lmt-{ret}-{side}-{variant}".lower(),
        family=FAMILY,
        role=role,
        product=product,
        prctyp="LMT",
        ret=ret,
        side=side,
        orders=orders,
        outcome=outcome,
        tier=tier,
    )
    return SlicedSpec(case, sizing, lots, slice_lots, disclosed, deep)


# In run order: the cheapest notional first (an RMS limit skips the rest of the family).
SPECS: list[SlicedSpec] = [
    # 20 shares in slices of 10, 1 disclosed per slice (10 %: the documented NSE bound).
    _spec("nse-b", "MIS", "DAY", "BUY", "dscqty", LOTS, lots=20, slice_lots=10, disclosed=1,
          outcome=RECORD, tier=SMOKE),
    _spec("nfo-nifty-ce-atm", "NRML", "DAY", "BUY", "freeze", FREEZE, deep=True),
    _spec("bfo-sensex-ce-atm", "NRML", "DAY", "BUY", "freeze", FREEZE, deep=True),
    _spec("nfo-nifty-f", "NRML", "DAY", "BUY", "2x1lot", LOTS, tier=SMOKE),
    _spec("nfo-nifty-f", "NRML", "IOC", "BUY", "3x1lot", LOTS, lots=3,
          outcome=EXCHANGE_CANCEL),
    _spec("nfo-nifty-f", "NRML", "DAY", "SELL", "3x1lot", LOTS, lots=3),
    _spec("mcx-crudeoil-f", "NRML", "DAY", "BUY", "2x1lot", LOTS),  # trade units (U7)
    _spec("nfo-nifty-f", "NRML", "DAY", "BUY", "freeze", FREEZE),
]  # fmt: skip
SLICED_CASES: list[Case] = [s.case for s in SPECS]
_BY_ID = {s.case.id: s for s in SPECS}


DEEP = Decimal("0.20")  # the freeze cases of non-options rest 20 % below the last price
OPTION_DEEP = Decimal("0.04")  # options: 4 % (the exchange LPP band refuses a farther price)
REGULAR = Decimal("0.10")  # ... never less deep than a regular far BUY
OPTION_REGULAR = Decimal("0.01")  # ... an option: at least 1 % below


def deep_far_buy(snapshot: PriceSnapshot, *, option: bool = False) -> Decimal:
    """A BUY limit 20 % below the last price (an option 4 %), raised to the lower circuit;
    ``Refusal`` when the circuit leaves it less than 10 % below (an option: 1 %) (three
    freeze-size option slices are a large position if the premium falls through a regular far
    price)."""
    last, tick, lower = snapshot.last_price, snapshot.tick_size, snapshot.lower_circuit
    depth, floor = (OPTION_DEEP, OPTION_REGULAR) if option else (DEEP, REGULAR)
    price = floor_to_tick(last * (1 - depth), tick)
    if lower is not None and lower > 0 and price < lower:
        price = ceil_to_tick(lower, tick)
    if price > last * (1 - floor):
        raise Refusal(
            f"{snapshot.tradingsymbol}: the lower circuit {lower} keeps a deep far BUY within "
            f"{floor * 100:.0f} % of the last price {last}; not placed"
        )
    return price


def planned(
    spec: SlicedSpec, contract: Contract, freeze: int | None
) -> tuple[int, tuple[int, ...], int | None]:
    """(quantity, the slices expected, ``max_slice_quantity``) in trade units; ``Refusal``
    (the case skips, nothing sent) when a freeze sizing has no usable freeze quantity."""
    lot = contract.lot_size
    if spec.sizing == LOTS:
        size, quantity = spec.slice_lots * lot, spec.lots * lot
        full, rest = divmod(quantity, size)
        return quantity, (size,) * full + ((rest,) if rest else ()), size
    if freeze is None or freeze < lot:
        raise Refusal(
            f"{spec.case.role}: no usable freeze quantity (security_info frzqty {freeze!r}, lot "
            f"{lot}); the freeze sizing needs one"
        )
    size = (freeze // lot) * lot
    return 2 * size + lot, (size, size, lot), None


def _freeze(run: MatrixRun, contract: Contract) -> int | None:
    try:
        return run.session.client.security_info(contract.exchange, contract.token).frzqty
    except Exception as exc:  # a read: the case skips
        raise Refusal(f"{contract.tradingsymbol}: security_info could not be read ({exc})") from exc


# -- running ------------------------------------------------------------------------------------


def sliced_results(run: MatrixRun, cases: Sequence[Case]) -> dict[str, CaseResult]:
    """The result of every case: from this run's cache, else run now, one case at a time."""
    cache = family.results_of(run)
    todo = [_BY_ID[c.id] for c in cases if c.id not in cache]
    for spec in todo:
        if spec.case.segment in expect.CANARY_SEGMENTS:
            _canary_first(run, spec.case.segment)
        cache[spec.case.id] = _run_one(run, spec)
    return {c.id: cache[c.id] for c in cases if c.id in cache}


def _canary_first(run: MatrixRun, segment: str) -> None:
    issues = run.issues
    if segment in issues.canary_passed or segment in issues.stuck_segments:
        return
    canary = next((c for c in case_table.CASES if c.canary and c.segment == segment), None)
    if canary is not None:
        family.run_cases(run, [canary])


def _run_one(run: MatrixRun, spec: SlicedSpec) -> CaseResult:
    session = run.session
    work = batch._Work(BatchItem(spec.case))
    if not batch._before_round(run):
        batch._skip(run, work, session.guard.stop_reason or STOP_REASON)
    elif batch._prepare(run, work):
        slices: tuple[int, ...] = ()
        try:
            slices = _send(run, work, spec)
        except KeyboardInterrupt:
            batch._interrupt(session)
            raise
        except StopRun as stop:
            work.stop = stop
            if not work.order_ids:
                batch._skip(run, work, stop.reason)
        except Refusal as exc:
            if not work.order_ids:
                batch._skip(run, work, exc.reason)
        except MaxOpenReached as exc:  # raised before any slice is sent: no room, not a finding
            batch._skip(run, work, f"MAX_OPEN: {exc}")
        except Exception as exc:  # the family's code: the slices placed are still cancelled
            batch._worker_error(run, work, exc)
        finally:
            _observe_and_cancel(run, work)
        if work.result is None:
            _judge(run, work, spec, slices)
    assert work.result is not None
    return work.result


def _send(run: MatrixRun, work: batch._Work, spec: SlicedSpec) -> tuple[int, ...]:
    """Re-check, size, quote, price, pace, one ``calls.place_sliced``."""
    session, case, contract = run.session, work.case, work.contract
    assert contract is not None
    batch._recheck(run, work)
    freeze = _freeze(run, contract) if spec.sizing == FREEZE else None
    quantity, slices, cap = planned(spec, contract, freeze)
    if len(slices) != case.orders:
        raise Refusal(f"case {case.id}: {len(slices)} slices planned, {case.orders} declared")
    work.notes.update(slices_planned=list(slices), max_slice_quantity=cap, frzqty=freeze)
    work.snapshot = pricing.fresh_quote(
        session.client,
        contract,
        uat=session.uat,
        sleep=session.sleep,
        pacer=session.uat_quote_pacer(),
    )  # the last read before
    plan = batch.default_plan(case, contract, work.snapshot)
    plan["quantity"] = quantity
    if spec.deep:
        plan["price"] = deep_far_buy(work.snapshot, option=contract.kind == "opt")
    if spec.disclosed is not None:
        plan["disclosed_quantity"] = spec.disclosed
    work.plan = plan
    for _ in slices:
        run.pacer.wait()  # one pacing stamp per slice, right before the call
    outcome = calls.place_sliced(
        session, plan, work.ref, max_slices=case.orders, max_slice_quantity=cap
    )
    work.outcomes.append(outcome)
    batch._early_rms(run, work, outcome)
    return slices


def _observe_and_cancel(run: MatrixRun, work: batch._Work) -> None:
    """Settle the slices (one poll per round), then one cancel per open slice and confirm."""
    session = run.session
    placed = [work] if work.order_ids else []
    if not placed or session.interrupted:
        return
    try:
        batch._settle(run, placed)
    except KeyboardInterrupt:
        batch._interrupt(session)
        raise
    finally:
        if not session.interrupted:
            batch._cancel_and_confirm(run, placed)


# -- judging --------------------------------------------------------------------------------------


def _rejection(work: batch._Work) -> str | None:
    """The refusal message, when the call or a slice in the book was refused."""
    place = work.outcomes[0]
    if place.verdict == calls.REJECTED:
        return batch._message(place) or ""
    for order_id in work.order_ids:
        row = work.settled.get(order_id)
        if status_of(row) == "REJECTED":
            return (row.reject_reason if row is not None else None) or ""
    return None


def _judge(run: MatrixRun, work: batch._Work, spec: SlicedSpec, slices: tuple[int, ...]) -> None:
    case, exp = work.case, work.expectation
    assert exp is not None
    if not work.outcomes:
        batch._finish(run, work, batch.FAIL, f"nothing was placed: {work.error}")
        return
    place = work.outcomes[0]
    work.notes["slices_sent"] = len(work.order_ids)
    if place.verdict in (batch.CHECK, INTERRUPTED):
        batch._finish(run, work, batch.CHECK, f"{batch.CHECK}: a slice's outcome is unknown",
                      sent=True)  # fmt: skip
        return
    if batch._unknown_later(run, work):
        return
    if work.stuck:
        batch._finish(
            run, work, batch.STUCK,
            f"slice {work.stuck[0]} stuck: not final after its cancel; the {case.segment} "
            "circuit breaker tripped", sent=True,
        )  # fmt: skip
        return
    if work.error is not None:
        batch._finish(run, work, batch.FAIL, f"raised after its call: {work.error}", sent=True)
        return
    if batch.outcome_unsent(place):
        batch._judge_not_sent(run, work, exp, place)
        return
    message = _rejection(work)
    if message is not None:
        batch._judge_rejection(run, work, exp, message)
        return
    run.session.guard.note_rejection(case.segment, expected=True)
    problems = _problems(run, work, spec, slices)
    statuses = [status_of(work.settled.get(i)) for i in work.order_ids]
    if exp.verdict == expect.ACCEPT:
        if problems:
            batch._finish(run, work, batch.FAIL, "; ".join(problems), sent=True)
        elif batch.UAT_FILLED_FAR in work.notes:
            filled = work.notes[batch.UAT_FILLED_FAR]["filled"]
            batch._finish(run, work, batch.RECORD, batch.uat_fill_reason(filled),
                          issue=exp.issue, sent=True)  # fmt: skip
        else:
            batch._finish(
                run, work, batch.PASS,
                f"{len(slices)} slices {list(slices)} placed in one call, each {statuses[0]}, "
                f"as documented ({exp.reason})", issue=exp.issue, sent=True,
            )  # fmt: skip
        return
    if exp.verdict == expect.REJECT:
        batch._finish(run, work, batch.RECORD, f"accepted ({statuses}) although a refusal was "
                      f"expected: {exp.reason}", issue=exp.issue, sent=True)  # fmt: skip
        return
    detail = f"; {'; '.join(problems)}" if problems else ""
    batch._finish(run, work, batch.RECORD, f"recorded: {statuses}{detail}", issue=exp.issue,
                  sent=True)  # fmt: skip


def _problems(
    run: MatrixRun, work: batch._Work, spec: SlicedSpec, slices: tuple[int, ...]
) -> list[str]:
    case, place = work.case, work.outcomes[0]
    ids = work.order_ids
    rows = [work.settled.get(i) for i in ids]
    problems: list[str] = []
    if len(ids) != len(slices):
        problems.append(f"{len(ids)} slice orders, {len(slices)} planned")
    quantities = [int(r.quantity or 0) if r is not None else 0 for r in rows]
    work.notes["quantities"] = quantities
    if sorted(quantities) != sorted(slices):
        problems.append(f"slice quantities {quantities}, planned {list(slices)}")
    book = read_book(run.session.client, attempts=1)
    if book is not None:
        tagged = {(o.order_id or "").strip() for o in book if (o.tag or "").strip() == place.tag}
        if tagged != set(ids):
            problems.append(f"orders() shows {sorted(tagged)} with tag {place.tag}, the call "
                            f"returned {ids}")  # fmt: skip
    if spec.disclosed is not None:
        disclosed = [None if r is None else r.disclosed_quantity for r in rows]
        work.notes["disclosed"] = disclosed
    work.notes["norentm"] = [None if r is None or r.norentm is None else str(r.norentm)
                             for r in rows]  # fmt: skip
    want = batch._SETTLED.get(case.outcome)
    statuses = [status_of(r) for r in rows]
    filled = sum((r.filled_quantity or 0) for r in rows if r is not None)
    uat_fill = False
    if filled and work.contract is not None:
        batch._add_fill(run, work, filled, ids[0] if ids else None)
        uat_fill = batch.note_uat_fill(run, work, filled)  # UAT fills at any price: recorded
        if not uat_fill:
            problems.append(f"slices away from the market filled ({filled})")
    seen = [s for s in statuses if not (uat_fill and s == "COMPLETE")]
    if want is not None and any(s != want for s in seen):
        problems.append(f"expected every slice {want}, saw {statuses}")
    if case.outcome == EXCHANGE_CANCEL:
        cancelled = [o.order_id for o in work.outcomes if o.kind == "cancel"]
        if cancelled:
            problems.append(f"the run cancelled {cancelled}; the exchange should have")
    final = [work.final.get(i) for i in ids]
    if not all(is_final(status_of(r)) for r in final):
        problems.append("not every slice is final after the cancel")
    return problems
