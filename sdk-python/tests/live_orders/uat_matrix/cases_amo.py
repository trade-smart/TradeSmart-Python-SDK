"""The UAT matrix family M6: after-market orders (inventory §3.2, owner answer U12; ``m_amo``).

Outside the market session only: far ``amo=True`` orders per segment (NSE ``nse-b``, NFO
``nfo-nifty-f``, BFO ``bfo-sensex-f``, MCX ``mcx-crudeoil-f``): LMT DAY BUY and SELL (placed,
modified one tick, cancelled), SL-LMT DAY BUY (placed, cancelled), LMT IOC BUY (a refusal is
expected; accepted, it is cancelled and recorded); a CNC LMT DAY BUY on NSE. The AMO window is
undocumented, so every accepted AMO is **recorded** (U13): the status and the book's ``amo``
flag are in the case record. One ``amo=True`` probe runs **inside** NSE hours (``INSIDE_PROBE``,
recorded).

**The AMO canary** (F26, U9): BFO AMO cases run after their own AMO canary (a plain DAY order
outside hours is not a canary of anything). Its result is kept apart from the regular BFO canary
state: a resting AMO canary unlocks the BFO AMO cases of its batch only, never regular BFO orders
(``issues.canary_passed`` is restored after the AMO batch; a resume never restores it there:
``batch.SEPARATE_CANARY_FAMILIES``); a later AMO batch, or a resumed run (the ledger's verdict),
reuses its result.

**Windows** (``refusal``, checked when the module starts and again right before each place;
``TRADESMART_LIVE_ORDER_OUTSIDE_HOURS`` lifts none of them): an AMO case runs only outside its
segment's session, kept clear from 30 minutes before the open to 15 minutes after the close on
weekdays: NSE / NFO / BFO 08:45-15:45 IST; MCX 08:30 to 00:10 IST the next morning, because MCX
non-agri contracts trade until 23:55 IST while US daylight saving time is off (November to
March; 23:30 in summer: 23:55 is used all year, the conservative end). So an evening run skips
the MCX cases. The inside probe runs only 09:20-15:15 IST on weekdays. Weekends and holidays:
weekend AMOs are allowed (every AMO is cancelled within the run anyway); special weekend
sessions (e.g. a Saturday trading session) and exchange holidays are not modelled; "before 09:00
IST" in the notices means before the next session's open.

**AMO stops** (SL-LMT) sit 10 % / 12 % from the last price and are never clamped to the
circuit: on an instrument with a ± 10 % circuit the case is an expected, recorded skip.

**Every AMO is cancelled and confirmed final before the run ends** (the batch cancels its own;
the session teardown cancels any left by an exception: they are ours). An AMO still open after
that would reach the exchange at the open: ``left_open_guard`` (around the module's batch and
its tests; it also runs when the batch raised) fails with CHECK THE ACCOUNT, and
``open_amo_notice`` (the end of the session, read-only: after Ctrl-C or a teardown that did not
confirm every order final) lists the run's AMO orders for a human.

**Never in a mock session** (``session.mock``): every case skips with nothing sent
(``mock_session.NO_AMO_GTT``: an AMO is stored past the session), and ``calls`` refuses any
place with ``amo`` set.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timedelta
from datetime import time as day_time
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import pytest

from tests.live_orders import mock_session
from tests.live_orders.safety import (
    CHECK_THE_ACCOUNT,
    Refusal,
    ceil_to_tick,
    floor_to_tick,
    is_final,
    status_of,
)
from tests.live_orders.uat_matrix import batch, family
from tests.live_orders.uat_matrix.batch import BatchItem, CaseResult, MatrixRun, Placed
from tests.live_orders.uat_matrix.cases import FULL, RECORD, REJECT, REST, SMOKE, Case
from tests.live_orders.uat_matrix.contracts import Contract
from tests.live_orders.uat_matrix.gate import MatrixSession
from tests.live_orders.uat_matrix.ledger import Ledger, is_ours, read_book
from tests.live_orders.uat_matrix.pricing import PriceSnapshot
from tradesmart.models.base import IST

if TYPE_CHECKING:
    from tests.live_orders.uat_matrix.calls import CancelReport

FAMILY = "amo"
SEGMENT_ROLES = (
    ("nse-b", "MIS"),
    ("nfo-nifty-f", "NRML"),
    ("bfo-sensex-f", "NRML"),
    ("mcx-crudeoil-f", "NRML"),
)
SMOKE_ROLES = frozenset({"nse-b", "nfo-nifty-f"})  # their LMT DAY BUY (place, modify, cancel)

# The sessions an AMO must stay clear of (IST, weekdays). MCX non-agri trades until 23:55 IST
# in winter (US daylight saving time off) and 23:30 in summer: 23:55 all year (conservative).
SESSIONS: dict[str, tuple[day_time, day_time]] = {
    "nse": (day_time(9, 15), day_time(15, 30)),
    "nfo": (day_time(9, 15), day_time(15, 30)),
    "bfo": (day_time(9, 15), day_time(15, 30)),
    "bse": (day_time(9, 15), day_time(15, 30)),
    "mcx": (day_time(9, 0), day_time(23, 55)),
}
BEFORE_OPEN_MINUTES = 30  # kept clear before the open (NSE pre-open starts 09:00)
AFTER_CLOSE_MINUTES = 15  # kept clear after the close (may run past midnight: MCX)
DAY_MINUTES = 24 * 60
INSIDE_FROM, INSIDE_TO = day_time(9, 20), day_time(15, 15)  # the inside probe (as every family)
# An AMO LIMIT rests overnight and reaches the exchange at the open: priced 12 % from the last
# price (clamped to the circuit), refused (an expected skip) when less than 10 % away; its one
# modify keeps it at least 10 % away too.
AMO_LIMIT_FAR, AMO_MIN_FAR = Decimal("0.12"), Decimal("0.10")
FAR = AMO_MIN_FAR  # a modified AMO price stays at least 10 % away from the last price
# An AMO stop BUY: trigger 10 % and limit 12 % above the last price (a stop SELL: 10 % / 12 %
# below), so the overnight gap does not trigger it at the open.
AMO_STOP_TRIGGER, AMO_STOP_LIMIT = Decimal("0.10"), Decimal("0.12")
NOTICE = (
    f"{CHECK_THE_ACCOUNT}: open AMO orders reach the exchange at the open - cancel before "
    "09:00 IST or run the cleanup tool"
)


def _case(
    role: str,
    product: str,
    prctyp: str,
    ret: str,
    side: str,
    outcome: str,
    *,
    tier: str = FULL,
    suffix: str = "",
    canary: bool = False,
) -> Case:
    return Case(
        id=f"{FAMILY}-{role}-{product}-{prctyp}-{ret}-{side}".lower() + suffix,
        family=FAMILY,
        role=role,
        product=product,
        prctyp=prctyp,
        ret=ret,
        side=side,
        orders=1,
        outcome=outcome,
        tier=tier,
        canary=canary,
    )


def _grid() -> list[Case]:
    result: list[Case] = []
    for role, product in SEGMENT_ROLES:
        result += [
            _case(role, product, "LMT", "DAY", "BUY", RECORD,
                  tier=SMOKE if role in SMOKE_ROLES else FULL),
            _case(role, product, "LMT", "DAY", "SELL", RECORD),
            _case(role, product, "SL-LMT", "DAY", "BUY", RECORD),
            _case(role, product, "LMT", "IOC", "BUY", REJECT),
        ]  # fmt: skip
    return result


CANARY = _case("bfo-sensex-f", "NRML", "LMT", "DAY", "BUY", REST, suffix="-canary", canary=True)
INSIDE_PROBE = _case("nse-b", "MIS", "LMT", "DAY", "BUY", RECORD, suffix="-inside")
AMO_CASES: list[Case] = [
    CANARY,
    *_grid(),
    _case("nse-b", "CNC", "LMT", "DAY", "BUY", RECORD),
    INSIDE_PROBE,
]


# -- windows ----------------------------------------------------------------------------------


def _minutes(t: day_time) -> int:
    return t.hour * 60 + t.minute


def _hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def refusal(case: Case, now: datetime) -> str | None:
    """Why ``case`` must not be placed at ``now``, else ``None`` (see the module docstring)."""
    local = now.astimezone(IST)
    current = _minutes(local.time())
    weekday = local.weekday() < 5
    if case.id == INSIDE_PROBE.id:
        if weekday and _minutes(INSIDE_FROM) <= current <= _minutes(INSIDE_TO):
            return None
        return (
            f"the AMO probe inside market hours runs inside NSE hours only "
            f"({INSIDE_FROM:%H:%M}-{INSIDE_TO:%H:%M} IST on weekdays; now {local:%a %H:%M} IST)"
        )
    opens, closes = SESSIONS[case.segment]
    start = _minutes(opens) - BEFORE_OPEN_MINUTES
    end = _minutes(closes) + AFTER_CLOSE_MINUTES  # past DAY_MINUTES: into the next morning
    after_weekday = (local - timedelta(days=1)).weekday() < 5
    today = weekday and start <= current < end
    spill = end > DAY_MINUTES and after_weekday and current < end - DAY_MINUTES
    if today or spill:
        return (
            f"AMO cases run only outside the {case.segment.upper()} session ({_hhmm(start)}-"
            f"{_hhmm(end % DAY_MINUTES)} IST kept clear after each weekday's open; now "
            f"{local:%a %H:%M} IST)"
        )
    return None


# -- plans and the modify hook ------------------------------------------------------------------


def _amo_stop(snapshot: PriceSnapshot, side: str) -> tuple[Decimal, Decimal]:
    """``(trigger, limit)`` of an AMO stop: 10 % / 12 % from the last price, never clamped to
    the circuit. ``Refusal`` (the case skips, recorded: expected on an instrument with a
    ± 10 % circuit) when the limit lies beyond the circuit."""
    last, tick = snapshot.last_price, snapshot.tick_size
    if side.upper() == "BUY":
        trigger = ceil_to_tick(last * (1 + AMO_STOP_TRIGGER), tick)
        limit = ceil_to_tick(last * (1 + AMO_STOP_LIMIT), tick)
        bound = snapshot.upper_circuit
        beyond = bound is not None and bound > 0 and limit > bound
    else:
        trigger = floor_to_tick(last * (1 - AMO_STOP_TRIGGER), tick)
        limit = floor_to_tick(last * (1 - AMO_STOP_LIMIT), tick)
        bound = snapshot.lower_circuit
        beyond = bound is not None and bound > 0 and limit < bound
    if beyond:
        raise Refusal(
            f"expected skip: an AMO stop {side.upper()} (trigger {trigger}, limit {limit}: 10 % "
            f"/ 12 % from the last price {last}) lies beyond the circuit {bound} (a ± 10 % "
            "circuit instrument); not clamped, not placed"
        )
    return trigger, limit


def _amo_limit(snapshot: PriceSnapshot, side: str) -> Decimal:
    """An AMO LIMIT price: 12 % from the last price, clamped to the circuit; ``Refusal`` (an
    expected skip, recorded) when that leaves it less than 10 % away (a narrow circuit)."""
    last, tick = snapshot.last_price, snapshot.tick_size
    if side.upper() == "BUY":
        price = floor_to_tick(last * (1 - AMO_LIMIT_FAR), tick)
        bound = snapshot.lower_circuit
        if bound is not None and bound > 0 and price < bound:
            price = ceil_to_tick(bound, tick)
        near = price > last * (1 - AMO_MIN_FAR)
    else:
        price = ceil_to_tick(last * (1 + AMO_LIMIT_FAR), tick)
        bound = snapshot.upper_circuit
        if bound is not None and bound > 0 and price > bound:
            price = floor_to_tick(bound, tick)
        near = price < last * (1 + AMO_MIN_FAR)
    if near:
        raise Refusal(
            f"expected skip: an AMO LIMIT {side.upper()} at {price} (the circuit {bound}) is less "
            f"than 10 % from the last price {last}; an AMO left open reaches the exchange at the "
            "open, so it is not placed"
        )
    return price


def amo_plan(case: Case, contract: Contract, snapshot: PriceSnapshot) -> dict[str, Any]:
    """The engine's plan of ``case`` with ``amo=True``: a LIMIT 12 % away (at least 10 %), a
    stop 10 % / 12 % away."""
    plan = batch.default_plan(case, contract, snapshot)
    plan["amo"] = True
    if case.prctyp == "LMT":
        plan["price"] = _amo_limit(snapshot, str(plan.get("transaction_type", case.side)))
    if case.prctyp == "SL-LMT":
        plan["trigger_price"], plan["price"] = _amo_stop(snapshot, case.side)
    return plan


def checked_plan(clock: Callable[[], datetime]) -> batch.PlanBuilder:
    """``amo_plan`` after the window check at ``clock()``, right before the place call (the
    batch may start inside the window and reach a case after it closed)."""

    def plan(case: Case, contract: Contract, snapshot: PriceSnapshot) -> dict[str, Any]:
        reason = refusal(case, clock())
        if reason is not None:
            raise Refusal(f"{reason}; not placed")
        return amo_plan(case, contract, snapshot)

    return plan


def modified_price(price: Decimal, side: str, snapshot: PriceSnapshot) -> Decimal | None:
    """``price`` moved one tick toward the market, or ``None`` when that would leave it less
    than 10 % away from the last price (``AMO_MIN_FAR``: it rests overnight)."""
    tick, last = snapshot.tick_size, snapshot.last_price
    if side.upper() == "BUY":
        moved = price + tick
        return moved if moved <= last * (1 - FAR) else None
    moved = price - tick
    return moved if moved >= last * (1 + FAR) else None


def nudge(placed: Placed) -> None:
    """The AMO's one modify: its price one tick toward the market (``Placed.modify``, never
    repeated); what it did goes to the case record (``notes["modify"]``)."""
    price = placed.plan.get("price")
    side = str(placed.plan.get("transaction_type", placed.case.side))
    moved = None if price is None else modified_price(Decimal(str(price)), side, placed.snapshot)
    if moved is None:
        placed.notes["modify"] = {"skipped": f"no price one tick nearer that stays far ({price})"}
        return
    outcome = placed.modify(price=moved)
    error = outcome.error or {}
    placed.notes["modify"] = {
        "verdict": outcome.verdict,
        "price": str(moved),
        "message": error.get("emsg") or error.get("message"),
    }


def _modified(case: Case) -> bool:
    return (
        case.prctyp == "LMT" and case.ret == "DAY" and not case.canary
        and case.id != INSIDE_PROBE.id
    )  # fmt: skip


def items(cases: Iterable[Case], clock: Callable[[], datetime]) -> list[BatchItem]:
    """The batch items: every order an AMO (the window checked again at ``clock()`` right
    before its place); the LMT DAY ones modified once while resting."""
    plan = checked_plan(clock)
    return [
        BatchItem(case, plan=plan, after_settle=nudge if _modified(case) else None)
        for case in cases
    ]


# -- running ------------------------------------------------------------------------------------


def _run_ready(run: MatrixRun, ready: list[Case]) -> dict[str, CaseResult]:
    """One batch of the ready cases. BFO AMO cases run after the AMO canary (this batch's, or
    one that passed in an earlier AMO batch of the run); the regular BFO canary state is put
    back afterwards, so the AMO canary never unlocks regular BFO orders."""
    issues = run.issues
    results: dict[str, CaseResult] = {}
    had = "bfo" in issues.canary_passed
    prior = _amo_canary(run)
    bfo = [c for c in ready if c.segment == "bfo" and not c.canary]
    unlock = False
    if bfo and "bfo" not in issues.stuck_segments:
        if prior is None:
            if all(c.id != CANARY.id for c in ready):
                ready = [CANARY, *ready]
        elif prior[0] == batch.PASS:
            unlock = True  # the AMO canary rested earlier in this run
        else:
            reason = f"the AMO canary did not pass in this run ({prior[0]}: {prior[1]})"
            for case in bfo:
                results[case.id] = CaseResult(case.id, batch.SKIP, reason)
                family.remember(run, results[case.id])
            ready = [c for c in ready if c not in bfo]
    try:
        if unlock:
            issues.canary_passed.add("bfo")
        if ready:
            results.update(family.run_cases(run, items(ready, run.now)))
    finally:
        if not had:
            issues.canary_passed.discard("bfo")
    _note_final(
        run.session, [i for r in results.values() if is_final(r.status) for i in r.order_ids]
    )
    return results


def _amo_canary(run: MatrixRun) -> tuple[str, str] | None:
    """(verdict, reason) of the AMO canary in this run: this process's result, else the
    ledger's verdict of an earlier process (a resume); ``None`` when it has not run. Its own
    slot: never the segment's canary state (``batch.SEPARATE_CANARY_FAMILIES``)."""
    result = family.results_of(run).get(CANARY.id)
    if result is not None and result.verdict != batch.SKIP:
        return result.verdict, result.reason
    verdict = run.session.ledger.done(CANARY.id)
    if verdict is not None:
        return verdict, f"in an earlier process of run {run.session.run_id}"
    return None if result is None else (result.verdict, result.reason)


def _note_final(session: MatrixSession, order_ids: Iterable[str]) -> None:
    """Note AMO orders confirmed final (``amo_confirmed_final``): the end-of-session notice
    leaves them out."""
    ids = sorted(set(order_ids) - confirmed_final(session.ledger))
    if ids:
        session.ledger.note("amo_confirmed_final", order_ids=ids)


def confirmed_final(ledger: Ledger) -> set[str]:
    """The AMO order ids the ledger notes confirmed final (``amo_confirmed_final`` lines)."""
    path = ledger.path
    found: set[str] = set()
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue  # a torn last line
        if isinstance(record, dict) and record.get("type") == "amo_confirmed_final":
            found.update(str(i) for i in record.get("order_ids") or [])
    return found


def _mock_skips(cases: Sequence[Case]) -> dict[str, CaseResult]:
    reason = mock_session.NO_AMO_GTT
    return {c.id: CaseResult(c.id, batch.SKIP, reason, canary=c.canary) for c in cases}


def run_amo(run: MatrixRun, cases: Sequence[Case], now: datetime) -> dict[str, CaseResult]:
    """Every case's result: refused by its window at ``now`` (skip, nothing sent), else from
    one batch (each place checks its window again at ``run.now()``). In a mock session every
    case skips with nothing sent (``mock_session.NO_AMO_GTT``: an AMO outlives the session)."""
    if run.session.mock:
        return _mock_skips(cases)
    results: dict[str, CaseResult] = {}
    ready: list[Case] = []
    for case in cases:
        reason = refusal(case, now)
        if reason is None:
            ready.append(case)
        else:
            results[case.id] = CaseResult(case.id, batch.SKIP, reason, canary=case.canary)
    if ready:
        results.update(_run_ready(run, ready))
    return results


def module_results(
    request: Any, session: MatrixSession, now: datetime | None = None
) -> Mapping[str, CaseResult]:
    """The module's batch: its selected cases at ``now`` (the run opens only when one may
    run now; never in a mock session)."""
    moment = now if now is not None else datetime.now(IST)
    selected = family.selected_cases(request)
    if session.mock:
        return _mock_skips(selected)
    if not any(refusal(c, moment) is None for c in selected):
        return {
            c.id: CaseResult(c.id, batch.SKIP, refusal(c, moment) or "", canary=c.canary)
            for c in selected
        }
    return run_amo(family.run_for(session), selected, moment)


# -- nothing left open ----------------------------------------------------------------------------


def left_open(session: MatrixSession) -> list[str] | None:
    """Our AMO orders the book shows not final (read-only); ``None`` when it is unreadable."""
    book = read_book(
        session.client,
        attempts=session.lookup_attempts,
        interval=session.lookup_interval,
        sleep=session.sleep,
    )
    if book is None:
        return None
    ledger = session.ledger
    found: list[str] = []
    final: list[str] = []
    for row in book:
        order_id = (row.order_id or "").strip()
        if not order_id or order_id in found or not is_ours(row, ledger):
            continue
        tag = ledger.tag_of(order_id) or (row.tag or "").strip()
        if not (ledger.case_of(tag) or "").startswith(f"{FAMILY}-"):
            continue
        (final if is_final(status_of(row)) else found).append(order_id)
    _note_final(session, final)
    return found


def left_open_failure(session: MatrixSession) -> str | None:
    """CHECK THE ACCOUNT when an AMO of this run is still open (or the book cannot tell),
    noted in the ledger; ``None`` when every AMO is final."""
    ids = left_open(session)
    if ids == []:
        return None
    if ids is None:
        session.ledger.note("amo_left_open", book_unreadable=True)
        return (
            f"{CHECK_THE_ACCOUNT}: the order book could not be read, so it is not confirmed that "
            f"every AMO of run {session.run_id} is final; an AMO left open reaches the exchange "
            "at the open"
        )
    session.ledger.note("amo_left_open", order_ids=ids)
    return (
        f"{CHECK_THE_ACCOUNT}: AMO order(s) {', '.join(ids)} of run {session.run_id} still open "
        "after their cancel; an AMO left open reaches the exchange at the open: cancel it by "
        "hand (or with the cleanup tool) before then"
    )


@contextmanager
def left_open_guard(session: MatrixSession) -> Iterator[None]:
    """Around the AMO module's batch and its tests: afterwards (also when the batch raised)
    every AMO of the run must be final, else the module fails with ``left_open_failure``.
    After Ctrl-C, or when the ledger holds no AMO not yet confirmed final, it reads nothing (the
    session-end notice lists the AMOs after Ctrl-C)."""
    try:
        yield
    except KeyboardInterrupt:
        raise
    except BaseException:
        failure = _guard_check(session)
        if failure is not None:
            pytest.fail(failure)  # chained to the batch's own exception (``__context__``)
        raise
    failure = _guard_check(session)
    if failure is not None:
        pytest.fail(failure)


def _guard_check(session: MatrixSession) -> str | None:
    if session.interrupted or amo_orders(session.ledger) == ([], []):
        return None
    return left_open_failure(session)


# -- the end of the session (read-only) ---------------------------------------------------------


def amo_orders(ledger: Ledger) -> tuple[list[str], list[str]]:
    """(the run's AMO order ids not noted confirmed final, its AMO place tags with an unknown
    outcome), from the ledger."""
    done = confirmed_final(ledger)
    ids = sorted(
        order_id
        for order_id in ledger.ours()
        if order_id not in done
        and (ledger.case_of(ledger.tag_of(order_id) or "") or "").startswith(f"{FAMILY}-")
    )
    tags = sorted(
        tag for tag in ledger.flagged_tags() if (ledger.case_of(tag) or "").startswith(f"{FAMILY}-")
    )
    return ids, tags


def open_amo_notice(session: MatrixSession, report: CancelReport | None) -> str | None:
    """The end-of-session notice (ledger only: never a call) when an AMO of the run may still be
    open: after Ctrl-C or a teardown that did not run (``report`` ``None``): every AMO of the
    run; after a teardown that left orders not final, flagged or stuck, or could not read the
    book: those that are AMOs (all of them when the book was unreadable). ``None`` otherwise."""
    ids, tags = amo_orders(session.ledger)
    if report is None or session.interrupted or report.book_unreadable:
        why = (
            "the run was interrupted (Ctrl-C): nothing was cancelled"
            if session.interrupted
            else "the teardown did not confirm the run's orders final"
        )
        listed = ids
    else:
        doubtful = {*report.not_final, *report.flagged, *report.stuck, *report.pending}
        listed = [i for i in ids if i in doubtful]
        why = "not confirmed final at the end of the session"
    if not listed and not tags:
        return None
    unknown = f"; AMO place tags with an unknown outcome: {', '.join(tags)}" if tags else ""
    shown = ", ".join(listed) if listed else "none listed"
    return f"{NOTICE} (run {session.run_id}; {why}): AMO order(s) {shown}{unknown}"
