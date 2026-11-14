"""UAT matrix family M9: views and stream cross-checks, sync and async (cases and runs).

One order per status path, on NSE ``ITC`` and the NIFTY ATM call (MIS, BUY):

- ``open``: a far LIMIT rests ``OPEN``, is modified (price -1 tick: the book, the newest history
  entry and a stream update must show it) and cancelled (``CANCELED``);
- ``reject``: a far LIMIT half a tick off the tick grid (``REJECTED``; an acceptance is recorded
  and the order cancelled once);
- ``trigger``: a stop BUY rests ``TRIGGER_PENDING`` and is cancelled;
- ``ioc``: a far IOC LIMIT is cancelled by the exchange;
- ``fill``: a BUY at the last price fills (``COMPLETE``), then the closing SELL at the last
  price (the engine's fill rules: one at a time per instrument, before 15:30 IST, a closing SELL
  that does not fill is the ``closing-sell`` xfail and the position stays in the position ledger).

For every order of a case the views must agree with the book at field level once the stream
caught up (bounded wait, the lag recorded): ``order_history()`` (newest first; its newest entry),
``trades()`` (fills add up to ``fillshares``, their average to ``avgprc``), ``positions()`` (the
day buy / sell quantity and amount of the order's product changed by its fills) and the stream
``om`` updates (the last one). A disagreement fails the case (``judge_views``); the record keeps
every view (``views``), the raw bodies of the case's calls (``raw_calls``) and the steps.

``items`` builds the batch items of the sync paths (``TradeSmart`` + ``TradeSmartStream``) and
``run_cases`` runs them through ``family.run_cases`` (the batch engine) on the session's shared
run, then ``judge_views`` (a views failure is also the ledger verdict). ``run_async_family`` runs
the same paths through ``AsyncTradeSmart`` + ``AsyncTradeSmartStream``, one case at a time,
every state-changing call through ``calls_async`` (the same ledger and guard; never resent),
with the same expectations, verdicts and records; its results join the run's results
(``family.results_of``), so nothing is placed twice. A refused modify is recorded with its case
(``notes.modify_refusals``) and never counts for the circuit breaker.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

from tests.live_orders.safety import CHECK_THE_ACCOUNT, Refusal, is_final, status_of
from tests.live_orders.uat_matrix import (
    batch,
    calls,
    calls_async,
    cases_modify,
    expect,
    family,
    pricing,
)
from tests.live_orders.uat_matrix import cases as case_table
from tests.live_orders.uat_matrix import views as views_table
from tests.live_orders.uat_matrix.batch import BatchItem, CaseResult, MatrixRun
from tests.live_orders.uat_matrix.calls import CaseRef, OrderOutcome
from tests.live_orders.uat_matrix.cases import Case
from tests.live_orders.uat_matrix.contracts import (
    DATA_ERRORS,
    QUOTE_RATE_WAITS,
    SESSION_ERRORS,
    Contract,
    is_rate_refusal,
)
from tests.live_orders.uat_matrix.guard import STOP_REASON, MaxOpenReached, StopRun
from tests.live_orders.uat_matrix.ledger import INTERRUPTED, NOT_SENT, check_open_orders
from tests.live_orders.uat_matrix.pricing import PriceSnapshot
from tests.live_orders.uat_matrix.record import Recorder, calls_from_ledger
from tests.live_orders.uat_matrix.views import OrderWatcher
from tradesmart import AsyncTradeSmartStream
from tradesmart.models.market import Quote
from tradesmart.models.orders import Order, OrderHistoryEntry, Trade
from tradesmart.models.portfolio import Position
from tradesmart.models.stream import OrderUpdate, StreamMessage

FAMILY = "views"
ROLES = ("nse-b", "nfo-nifty-ce-atm")
PRODUCT = "MIS"
PATHS = ("open", "reject", "trigger", "ioc", "fill")
# path -> (price type, validity, documented outcome, declared orders)
_SHAPES = {
    "open": ("LMT", "DAY", case_table.REST, 1),
    "reject": ("LMT", "DAY", case_table.REJECT, 1),
    "trigger": ("SL-LMT", "DAY", case_table.TRIGGER_PENDING, 1),
    "ioc": ("LMT", "IOC", case_table.EXCHANGE_CANCEL, 1),
    "fill": ("LMT", "DAY", case_table.FILL_CLOSE, 2),
}
_SETTLED = {"open": "OPEN", "trigger": "TRIGGER_PENDING", "ioc": "CANCELED"}
SUBSCRIBE_SETTLE = 0.5

Timing = cases_modify.Timing


def _case(role: str, path: str, asynchronous: bool) -> Case:
    prctyp, ret, outcome, orders = _SHAPES[path]
    suffix = "-async" if asynchronous else ""
    return Case(
        id=f"{FAMILY}-{role}-{PRODUCT}-{prctyp}-{ret}-buy-{path}{suffix}".lower(),
        family=FAMILY,
        role=role,
        product=PRODUCT,
        prctyp=prctyp,
        ret=ret,
        side="BUY",
        orders=orders,
        outcome=outcome,
        tier=case_table.SMOKE if role == "nse-b" and not asynchronous else case_table.FULL,
    )


CASES: list[Case] = [_case(role, path, False) for role in ROLES for path in PATHS]
ASYNC_CASES: list[Case] = [_case(role, path, True) for role in ROLES for path in PATHS]
_PATH = {_case(r, p, x).id: p for r in ROLES for p in PATHS for x in (False, True)}


def path_of(case: Case) -> str:
    return _PATH[case.id]


def off_tick_plan(case: Case, contract: Contract, snapshot: PriceSnapshot) -> dict[str, Any]:
    """The far LIMIT of the case, half a tick off the tick grid (still far from the market)."""
    plan = batch.default_plan(case, contract, snapshot)
    far = pricing.far_buy(snapshot, option=contract.kind == "opt")
    plan["price"] = far + snapshot.tick_size / 2
    return plan


def judge_views(run: MatrixRun, result: CaseResult) -> CaseResult:
    """A case whose views disagree with the book (or could not be read) fails; the record
    keeps them and the ledger gets a ``views`` note. Skips and failing verdicts stay."""
    if result.verdict not in (*batch.PASSING, batch.XFAIL):
        return result
    record = result.record
    problems = [f"views not read: {record['views_error']}"] if "views_error" in record else []
    problems += list((record.get("views") or {}).get("mismatches") or [])
    if not problems:
        return result
    run.session.ledger.note("views", case_id=result.case_id, mismatches=problems)
    reason = f"views disagree ({result.verdict}: {result.reason}): " + "; ".join(problems)
    run.session.ledger.verdict(result.case_id, batch.FAIL, reason=reason)  # a resume sees it
    return replace(result, verdict=batch.FAIL, reason=reason)


# -- sync: the batch engine ---------------------------------------------------------------------


def items(selected: Iterable[Case], *, timing: Timing | None = None) -> list[BatchItem]:
    """The batch items of the selected sync M9 cases (the off-tick plan, the open path's
    modify-then-cancel hook); fills run in the engine's fill phase."""
    modify_once = cases_modify.ModifySequence(
        timing if timing is not None else Timing(),
        steps=(cases_modify.PRICE_DOWN,),
        after_cancel=False,
    )
    built: list[BatchItem] = []
    for case in selected:
        path = path_of(case)
        built.append(
            BatchItem(
                case,
                plan=off_tick_plan if path == "reject" else None,
                after_settle=modify_once if path == "open" else None,
            )
        )
    return built


def run_cases(
    run: MatrixRun, selected: Iterable[Case], *, timing: Timing | None = None
) -> dict[str, CaseResult]:
    """The selected sync M9 cases through ``family.run_cases`` with the views attached, then
    ``judge_views`` (the judged result replaces the engine's in the run's results)."""
    timing = timing if timing is not None else Timing()
    with cases_modify.views_attached(run, timing):
        results = family.run_cases(run, items(selected, timing=timing))
    judged = {case_id: judge_views(run, result) for case_id, result in results.items()}
    family.results_of(run).update(judged)
    return judged


# -- async: the clients and the stream ----------------------------------------------------------


class AsyncClient(calls_async.AsyncOrderClient, Protocol):
    """The ``AsyncTradeSmart`` calls of the async family."""

    async def quote(self, exchange: str, token: str) -> Quote: ...

    async def order_history(self, order_id: str) -> list[OrderHistoryEntry]: ...

    async def trades(self) -> list[Trade]: ...

    async def positions(self) -> list[Position]: ...

    async def holdings(self) -> list[Any]: ...


@asynccontextmanager
async def async_order_stream(client: Any) -> AsyncIterator[OrderWatcher]:
    """An ``AsyncTradeSmartStream`` subscribed to order updates, feeding an ``OrderWatcher``."""
    watcher = OrderWatcher()

    def on_message(message: StreamMessage) -> None:
        if isinstance(message, OrderUpdate):
            watcher.on_order_update(message)

    stream = AsyncTradeSmartStream.from_client(client, on_message=on_message)
    await stream.connect()
    try:
        await stream.subscribe_orders()
        await asyncio.sleep(SUBSCRIBE_SETTLE)
        yield watcher
    finally:
        await stream.close()


class _Quoted:
    """One quote already read with the async client, for ``pricing.fresh_quote``."""

    def __init__(self, quote: Quote) -> None:
        self._quote = quote

    def quote(self, exchange: str, token: str) -> Quote:
        return self._quote


class _Stop(Exception):
    """An unknown outcome (CHECK THE ACCOUNT): no further call for the case's order."""


@dataclass
class _Async:
    run: MatrixRun
    client: AsyncClient
    watcher: OrderWatcher
    timing: Timing
    recorder: Recorder

    @property
    def session(self) -> Any:
        return self.run.session


@dataclass
class _Work:
    case: Case
    expectation: expect.Expectation | None = None
    contract: Contract | None = None
    snapshot: PriceSnapshot | None = None
    plan: dict[str, Any] | None = None
    outcomes: list[OrderOutcome] = field(default_factory=list)
    rows: dict[str, Order | None] = field(default_factory=dict)  # latest row per order id
    baseline: dict[str, views_table.PositionDelta] = field(default_factory=dict)
    notes: dict[str, Any] = field(default_factory=dict)
    steps: list[dict[str, Any]] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    @property
    def ref(self) -> CaseRef:
        return CaseRef(self.case.id, self.case.segment)

    @property
    def order_ids(self) -> list[str]:
        ids: list[str] = []
        for outcome in self.outcomes:
            if outcome.order_id and outcome.order_id not in ids:
                ids.append(outcome.order_id)
        return ids


async def run_async_family(
    run: MatrixRun,
    client: AsyncClient,
    selected: Iterable[Case],
    *,
    watcher: OrderWatcher,
    timing: Timing | None = None,
) -> dict[str, CaseResult]:
    """The selected async M9 cases, one at a time; results by case id. A case with a result
    in this run already (``family.results_of``) is not run again."""
    session = run.session
    a = _Async(
        run,
        client,
        watcher,
        timing if timing is not None else Timing(),
        Recorder(session.run_dir, run_id=session.run_id, forbidden=session.ledger.forbidden),
    )
    done = family.results_of(run)
    results: dict[str, CaseResult] = {}
    for case in selected:
        if case.id not in done:
            done[case.id] = judge_views(run, await _async_case(a, case))
        results[case.id] = done[case.id]
    return results


def _prepare(a: _Async, w: _Work) -> tuple[str | None, str | None]:
    """Every check before anything is sent: (reason to skip, known issue) or (None, None)."""
    session, run, case = a.session, a.run, w.case
    reason = session.case_done(case.id)
    if reason is not None:
        return reason, None
    if session.guard.stopped():
        return session.guard.stop_reason or STOP_REASON, None
    w.expectation = expect.expectation(case, run.profile, run.issues)
    if w.expectation.verdict == expect.SKIP:
        return w.expectation.reason, w.expectation.issue
    tripped = session.guard.tripped(case.segment)
    if tripped is not None:
        return f"circuit breaker tripped for {case.segment}: {tripped}", None
    w.contract = run.contracts.get(case.role)
    if w.contract is None:
        return f"{case.role}: no contract for the role in this run", None
    try:
        session.guard.reserve(case.id, case.orders)
    except ValueError as exc:
        return f"not run twice in one process: {exc}", None
    try:  # the open-order invariant (read-only)
        check_open_orders(
            session.client, session.ledger, session.ignored_open, guard=session.guard,
            sleep=session.sleep,
        )  # fmt: skip
    except StopRun as stop:
        calls.stop_run(session, stop)
        return stop.reason, None
    return None, None


async def _async_case(a: _Async, case: Case) -> CaseResult:
    w = _Work(case)
    reason, issue = _prepare(a, w)
    if reason is not None:
        return await _finish(a, w, batch.SKIP, reason, issue=issue)
    try:
        w.baseline = views_table.position_totals(await a.client.positions())
    except SESSION_ERRORS:
        raise
    except Exception as exc:
        w.notes["positions_baseline"] = f"{type(exc).__name__}: {exc}"
    try:
        if path_of(case) == "fill":
            return await _fill_case(a, w)
        return await _path_case(a, w)
    except _Stop:
        return await _finish(
            a, w, batch.CHECK, f"{CHECK_THE_ACCOUNT}: a call of the case has an unknown "
            "outcome; no further call is made for its order", sent=True,
        )  # fmt: skip
    except (StopRun, Refusal, MaxOpenReached) as exc:
        why = getattr(exc, "reason", None) or f"MAX_OPEN: {exc}"
        await _cancel_left_open(a, w)
        if w.outcomes and a.session.interrupted:
            return await _finish(a, w, batch.SKIP, f"{why}; nothing more was sent", sent=True)
        if w.outcomes:
            return await _finish(a, w, batch.SKIP, f"{why}; its order was cancelled", sent=True)
        return await _finish(a, w, batch.SKIP, why)
    except SESSION_ERRORS:
        raise
    except Exception as exc:  # the family's own code: never leave the order behind
        error = f"{type(exc).__name__}: {exc}"
        try:
            await _cancel_left_open(a, w)
        except Exception as cleanup:  # noted; the family error stays the verdict
            w.notes["cleanup_error"] = f"{type(cleanup).__name__}: {cleanup}"
        return await _finish(a, w, batch.FAIL, f"family error: {error}", sent=bool(w.outcomes))


# -- async: placing, observing, cancelling ------------------------------------------------------


async def _read_quote(a: _Async, contract: Contract) -> Quote:
    """The async quote read; one refused for rate (``exceeds Limit``) is read again after 0.5 s,
    1 s and 2 s (``contracts.read_quote``); any other error propagates."""
    sleep = calls_async.sleeper(a.run.session)
    pacer = a.run.session.uat_quote_pacer()  # UAT: the session's quote pacer (sync reads too)
    for wait in QUOTE_RATE_WAITS:
        if pacer is not None:
            await pacer.wait_async(sleep)
        try:
            return await a.client.quote(contract.exchange, contract.token)
        except Exception as exc:
            if not is_rate_refusal(exc):
                raise
            message = str(exc)
        await sleep(pacer.refusal_wait(message, wait) if pacer is not None else wait)
    if pacer is not None:
        await pacer.wait_async(sleep)
    return await a.client.quote(contract.exchange, contract.token)


async def _snapshot(a: _Async, contract: Contract) -> PriceSnapshot:
    try:
        quote = await _read_quote(a, contract)
    except SESSION_ERRORS:
        raise
    except DATA_ERRORS as exc:
        raise Refusal(
            f"{contract.tradingsymbol}: no fresh quote ({type(exc).__name__}: {exc}); no order"
        ) from exc
    return pricing.fresh_quote(_Quoted(quote), contract, uat=a.run.session.uat)


async def _place(
    a: _Async, w: _Work, *, side: str = "BUY", quantity: int | None = None
) -> OrderOutcome:
    """Re-check, quote, price, pace, place (the quote is the last read before the place)."""
    cases_modify.running(a.session)
    case, contract = w.case, w.contract
    assert contract is not None
    a.session.guard.check_new(case.segment)
    batch.check_hours(a.run, case)  # market hours before each place (any hour on UAT)
    current = expect.expectation(case, a.run.profile, a.run.issues)
    if current.verdict == expect.SKIP:
        raise Refusal(current.reason)
    closing = path_of(case) == "fill" and side == "SELL"  # the fill path's closing SELL
    ceiling = None if closing else batch.ceiling_refusal(a.run, contract)
    if ceiling is not None:
        raise Refusal(ceiling)
    snapshot = await _snapshot(a, contract)
    if path_of(case) == "reject":
        plan = off_tick_plan(case, contract, snapshot)
    else:
        plan = batch.default_plan(case, contract, snapshot, side=side)
    if quantity is not None:
        plan["quantity"] = quantity
    cnc_sell = batch.is_cnc_sell(plan) and case.role != batch.HOLD_ROLE
    if cnc_sell and not batch.cnc_covered(a.run, plan):  # a fresh holdings() read, read-only
        try:
            holdings = await a.client.holdings()
        except SESSION_ERRORS:
            raise
        except DATA_ERRORS as exc:
            raise Refusal(
                f"holdings() could not be read ({type(exc).__name__}: {exc}); no CNC SELL sent"
            ) from exc
        refusal = batch.holdings_refusal(plan, holdings)
        if refusal is not None:
            raise Refusal(refusal)
    if w.plan is None:
        w.snapshot, w.plan = snapshot, plan
    w.notes.setdefault("plans", []).append(plan)
    a.run.pacer.wait()  # the pacing stamp: right before the place call
    outcome = await calls_async.place(a.session, a.client, plan, w.ref)
    w.outcomes.append(outcome)
    if outcome.verdict in (calls.CHECK, INTERRUPTED):
        raise _Stop
    return outcome


async def _rows(a: _Async, w: _Work, order_ids: Sequence[str], done: Any, seconds: float) -> None:
    """Poll ``orders()`` (async, read-only) until ``done(row)`` for each order or the time."""
    session = a.session
    sleep = calls_async.sleeper(session)
    deadline = session.clock() + seconds
    while True:
        try:
            book = await a.client.orders()
        except Exception:  # a read; the next one decides
            book = None
        for row in book or []:
            order_id = (row.order_id or "").strip()
            if order_id in order_ids:
                w.rows[order_id] = row
                if is_final(status_of(row)):
                    session.guard.closed(order_id)
        if all(done(w.rows.get(i)) for i in order_ids) or session.clock() >= deadline:
            return
        await sleep(session.poll_interval)


def _not_pending(row: Order | None) -> bool:
    status = status_of(row)
    return status is not None and status != "PENDING"


def _final(row: Order | None) -> bool:
    return is_final(status_of(row))


def _message(outcome: OrderOutcome) -> str:
    error = outcome.error or {}
    return str(error.get("emsg") or error.get("message") or "")


async def _cancel(a: _Async, w: _Work, order_id: str, *, verify: bool = False) -> bool:
    """One cancel of an order not final, confirmed final; ``True`` when final afterwards (a
    stuck order is noted and trips the segment)."""
    cases_modify.running(a.session)
    if _final(w.rows.get(order_id)):
        return True
    entry: dict[str, Any] = {"step": "cancel", "expect": cases_modify.ACCEPT}
    if verify:
        w.steps.append(entry)
    before = len(a.watcher.updates(order_id))
    a.run.pacer.wait(place=False)
    outcome = await calls_async.cancel(a.session, a.client, order_id, w.ref)
    w.outcomes.append(outcome)
    entry.update(verdict=outcome.verdict, words=_message(outcome))
    if outcome.check_the_account:
        entry["result"] = cases_modify.CHECK
        raise _Stop
    if outcome.verdict == NOT_SENT:
        entry["result"] = cases_modify.FAIL
        w.failures.append(
            f"cancel of {order_id}: not sent ({entry['words']}); left for the teardown"
        )
        return False
    await _rows(a, w, [order_id], _final, a.session.confirm_seconds)
    row = w.rows.get(order_id)
    if not _final(row):
        entry["result"] = cases_modify.FAIL
        w.notes.setdefault("stuck", []).append(order_id)
        a.session.guard.note_stuck(w.case.segment, order_id)
        a.session.ledger.note("stuck", order_id=order_id, segment=w.case.segment)
        return False
    if verify:
        check = await _verify(a, w, order_id, {}, status_of(row), before)
        entry.update(
            check, result=cases_modify.PASS if not check["problems"] else cases_modify.FAIL
        )
        if check["problems"]:
            w.failures.append(f"cancel: {'; '.join(check['problems'])}")
    return True


async def _cancel_left_open(a: _Async, w: _Work) -> None:
    """After a stop or a refusal mid-case: cancel (once) whatever of the case is still open."""
    if a.session.interrupted:
        return  # after Ctrl-C nothing more is sent
    for order_id in w.order_ids:
        if order_id in a.session.ledger.flagged() or a.session.ledger.cancel_sent(order_id):
            continue
        await _rows(a, w, [order_id], lambda r: True, 0.0)
        if not _final(w.rows.get(order_id)):
            try:
                await _cancel(a, w, order_id)
            except (_Stop, Refusal):
                continue


async def _verify(
    a: _Async, w: _Work, order_id: str, wanted: Mapping[str, Any], status: str | None, before: int
) -> dict[str, Any]:
    """The async ``cases_modify._verify``: book, newest history entry and a stream update."""
    session = a.session
    sleep = calls_async.sleeper(session)
    problems: list[str] = []

    def shown(view: Any) -> bool:
        return cases_modify.shows(view, wanted) and (status is None or status_of(view) == status)

    await _rows(a, w, [order_id], shown, session.confirm_seconds)
    row = w.rows.get(order_id)
    book_ok = row is not None and shown(row)
    if not book_ok:
        problems.append(f"book: {cases_modify.describe(row)} does not show {dict(wanted)} {status}")
    history: list[OrderHistoryEntry] = []
    deadline = session.clock() + session.confirm_seconds
    while True:
        with contextlib.suppress(Exception):  # a read; the next one decides
            history = await a.client.order_history(order_id)
        if (history and shown(history[0])) or session.clock() >= deadline:
            break
        await sleep(session.poll_interval)
    history_ok = bool(history) and shown(history[0])
    if not history_ok:
        newest = cases_modify.describe(history[0]) if history else "no entry"
        problems.append(f"history: newest {newest} does not show {dict(wanted)} {status}")
    lag, _ = await _await_update(a, order_id, shown, before)
    if lag is None:
        problems.append(f"stream: no update showing {dict(wanted)} {status}")
    return {
        "book": "ok" if book_ok else "differs",
        "history": "ok" if history_ok else "differs",
        "stream": "ok" if lag is not None else "differs",
        "stream_lag": lag,
        "problems": problems,
    }


async def _await_update(
    a: _Async, order_id: str, match: Any, after: int = 0
) -> tuple[float | None, OrderUpdate | None]:
    """The async ``views.wait_for_update`` (it yields to the stream's tasks while waiting)."""
    clock, timing = a.timing.clock, a.timing
    start = clock()
    while True:
        found = next((u for u in a.watcher.updates(order_id)[after:] if match(u)), None)
        if found is not None:
            return clock() - start, found
        if clock() - start >= timing.stream_wait:
            return None, None
        await asyncio.sleep(timing.interval)


# -- async: the paths -----------------------------------------------------------------------------


def _rejection(a: _Async, w: _Work, message: str) -> tuple[str, str, str | None]:
    """The verdict of a refusal (``batch._judge_rejection``'s rules)."""
    case, exp, guard = w.case, w.expectation, a.session.guard
    assert exp is not None
    kind = expect.environment_refusal(message)
    if kind is not None:  # neutral to the breaker: no note_rejection, the family goes on
        return batch.ENV_LIMIT, f"environment limit ({kind}): {message}", None
    if expect.is_rms_limit(message):
        a.run.issues.rms_limit(case.family, message)
        guard.note_rejection(case.segment, expected=True)
        return batch.ENV_LIMIT, f"environment limit (RMS): {message}", "RMS"
    if (
        a.session.uat
        and expect.is_uat_invalid_symbol(message)
        and exp.judge_rejection(message) != expect.EXPECTED
    ):  # UAT lacks some production contracts: environment data, never the breaker
        return batch.SKIP, f"environment data (UAT does not know the symbol): {message}", exp.issue
    if exp.verdict == expect.RECORD:
        guard.note_rejection(case.segment, expected=True)
        return batch.RECORD, f"recorded refusal: {message}", exp.issue
    judged = exp.judge_rejection(message)
    if judged == expect.UNEXPECTED and a.session.uat and expect.is_uat_band_refusal(message):
        # UAT's own band does not match its quote: recorded, never counted for the breaker.
        w.notes[batch.UAT_BAND_REFUSAL] = {"message": message, **expect.band_of(message)}
        return (batch.RECORD, f"recorded UAT band refusal ({batch.UAT_BAND_REFUSAL}): {message}",
                exp.issue)  # fmt: skip
    guard.note_rejection(case.segment, expected=judged != expect.UNEXPECTED)
    if judged == expect.EXPECTED:
        return batch.PASS, f"refused as expected: {message}", exp.issue
    if judged == expect.RECORD:
        return batch.RECORD, f"refused with another message: {message}", exp.issue
    return batch.FAIL, f"unexpected refusal ({exp.reason}): {message}", None


async def _unsent(a: _Async, w: _Work, outcome: OrderOutcome) -> CaseResult:
    exp = w.expectation
    assert exp is not None
    message = _message(outcome)
    if outcome.verdict != NOT_SENT:  # the SDK's client-side rate limit: a resume places it
        return await _finish(a, w, batch.SKIP, f"not sent: the SDK's client-side rate limit "
                             f"({message}); a resume places it")  # fmt: skip
    a.session.guard.note_rejection(w.case.segment, expected=True)
    if exp.verdict == expect.ACCEPT:
        return await _finish(a, w, batch.FAIL, f"the SDK refused it, nothing sent: {message}",
                             sent=True)  # fmt: skip
    return await _finish(a, w, batch.RECORD, f"not sent (refused client-side): {message}",
                         sent=True)  # fmt: skip


async def _modify_down(a: _Async, w: _Work, order_id: str) -> None:
    """The open path's modify: price -1 tick, through the async client, then verified."""
    cases_modify.running(a.session)
    assert w.plan is not None and w.contract is not None
    entry: dict[str, Any] = {"step": cases_modify.PRICE_DOWN.name, "expect": cases_modify.ACCEPT}
    w.steps.append(entry)
    row = w.rows.get(order_id)
    state = cases_modify.State.of_plan(w.plan).seen(row)
    assert state.price is not None
    try:  # inside the band of a fresh quote (UAT acknowledges a modify outside it, unapplied)
        snapshot = await _snapshot(a, w.contract)
        price, note = cases_modify.band_price(
            state.price, -1, w.contract.tick_size, snapshot, far_buy=True
        )
    except Refusal as exc:
        entry.update(result=cases_modify.SKIPPED, reason=exc.reason)
        return
    if note is not None:
        entry["note"] = note
    target = replace(state, price=price)
    entry["changes"] = target.changes()
    before = len(a.watcher.updates(order_id))
    a.run.pacer.wait(place=False)
    outcome = await calls_async.modify(
        a.session, a.client, order_id, w.ref, before=row, exchange=w.plan["exchange"],
        tradingsymbol=w.plan["tradingsymbol"], **target.changes(),
    )  # fmt: skip
    w.outcomes.append(outcome)
    entry.update(verdict=outcome.verdict, words=_message(outcome))
    if outcome.check_the_account:
        entry["result"] = cases_modify.CHECK
        raise _Stop
    if outcome.verdict not in (calls.ACKNOWLEDGED, calls.RESOLVED):
        rms = expect.is_rms_limit(entry["words"]) or (
            expect.environment_refusal(entry["words"]) is not None
        )
        w.notes.setdefault("modify_refusals", []).append(dict(entry))  # never for the breaker
        entry["result"] = cases_modify.ENV_LIMIT if rms else cases_modify.FAIL
        if not rms:
            w.failures.append(f"price-down: {outcome.verdict} ({entry['words']})")
        return
    wanted = {name: target.changes()[name] for name in cases_modify.VISIBLE_FIELDS}
    check = await _verify(a, w, order_id, wanted, None, before)
    entry.update(check, result=cases_modify.PASS if not check["problems"] else cases_modify.FAIL)
    if check["problems"]:
        w.failures.append(f"price-down: {'; '.join(check['problems'])}")


async def _path_case(a: _Async, w: _Work) -> CaseResult:
    """open / reject / trigger / ioc through the async client."""
    path, exp = path_of(w.case), w.expectation
    assert exp is not None
    outcome = await _place(a, w)
    if batch.outcome_unsent(outcome):
        return await _unsent(a, w, outcome)
    if outcome.verdict == calls.REJECTED:
        verdict, reason, issue = _rejection(a, w, _message(outcome))
        return await _finish(a, w, verdict, reason, issue=issue, sent=True)
    order_id = outcome.order_id
    assert order_id is not None
    done = _final if path == "ioc" else _not_pending
    await _rows(a, w, [order_id], done, a.run.settle_seconds)
    settled = w.rows.get(order_id)
    status = status_of(settled)
    w.notes["settled"] = status
    if status == "REJECTED":
        verdict, reason, issue = _rejection(a, w, (settled.reject_reason if settled else "") or "")
        return await _finish(a, w, verdict, reason, issue=issue, sent=True)
    a.session.guard.note_rejection(w.case.segment, expected=True)
    if path == "open" and status == "OPEN":
        await _modify_down(a, w, order_id)
    final = await _cancel(a, w, order_id, verify=path == "open")
    if not final:
        if order_id in w.notes.get("stuck", []):
            return await _finish(a, w, batch.STUCK, f"order {order_id} stuck: not final after "
                                 f"its cancel; the {w.case.segment} circuit breaker tripped",
                                 sent=True)  # fmt: skip
        return await _finish(a, w, batch.CHECK, f"{CHECK_THE_ACCOUNT}: order {order_id} is "
                             f"{status_of(w.rows.get(order_id))} and its cancel was not sent",
                             sent=True)  # fmt: skip
    last = w.rows.get(order_id)
    filled = (last.filled_quantity or 0) if last is not None else 0
    if filled and w.contract is not None:
        a.run.positions.add_fill(w.contract.tradingsymbol, filled, lot_size=w.contract.lot_size,
                                 case_id=w.case.id, order_id=order_id)  # fmt: skip
        if a.session.uat:  # UAT fills at any price: recorded, the fill is in the ledger
            w.notes[batch.UAT_FILLED_FAR] = batch.uat_fill_note(
                w.case, w.contract, w.plan, w.snapshot, filled
            )
            return await _finish(a, w, batch.RECORD, batch.uat_fill_reason(filled),
                                 issue=exp.issue, sent=True)  # fmt: skip
        return await _finish(a, w, batch.FAIL, f"an order away from the market filled ({filled})",
                             sent=True)  # fmt: skip
    if exp.verdict == expect.REJECT:
        return await _finish(a, w, batch.RECORD, f"accepted ({status}) although a refusal was "
                             f"expected: {exp.reason}", issue=exp.issue, sent=True)  # fmt: skip
    want = _SETTLED.get(path)
    if want is not None and status != want:
        if batch.uat_stop_open(a.session.uat, w.case, want, status) and not w.failures:
            w.notes[batch.UAT_STOP_OPEN] = {"status": status, "expected": want,
                                            "segment": w.case.segment}  # fmt: skip
            return await _finish(a, w, batch.RECORD, batch.uat_stop_open_reason(status),
                                 issue=exp.issue, sent=True)  # fmt: skip
        return await _finish(a, w, batch.FAIL, f"expected {want}, saw {status}", sent=True)
    if w.failures:
        return await _finish(a, w, batch.FAIL, "steps failed: " + " | ".join(w.failures),
                             sent=True)  # fmt: skip
    return await _finish(a, w, batch.PASS, f"{status}, as documented ({exp.reason})", sent=True)


async def _fill_leg(a: _Async, w: _Work, side: str, quantity: int) -> tuple[OrderOutcome, int]:
    """One order at the last price, waited for; cancelled once when not filled."""
    outcome = await _place(a, w, side=side, quantity=quantity)
    if outcome.order_id is None:
        return outcome, 0
    order_id = outcome.order_id
    await _rows(a, w, [order_id], _final, a.run.fill_wait_seconds)
    if not _final(w.rows.get(order_id)):
        await _cancel(a, w, order_id)
    row = w.rows.get(order_id)
    filled = (row.filled_quantity or 0) if row is not None else 0
    if status_of(row) == "COMPLETE" and not filled:
        filled = quantity
    return outcome, filled


async def _fill_case(a: _Async, w: _Work) -> CaseResult:
    """BUY at the last price, then the closing SELL (the engine's fill rules)."""
    contract, exp, positions = w.contract, w.expectation, a.run.positions
    assert contract is not None and exp is not None
    tsym, quantity = contract.tradingsymbol, contract.quantity(1)
    with positions.one_at_a_time(tsym):
        # Right before the BUY, under the instrument's lock: the 15:30 cut-off (none on UAT), room.
        cutoff = batch.fill_cutoff_reason(
            a.run.now(), uat=a.run.session.uat, mock=a.run.session.mock
        )
        if cutoff is not None:
            raise Refusal(cutoff)
        positions.check_room(tsym, quantity, lot_size=contract.lot_size)
        outcome, filled = await _fill_leg(a, w, "BUY", quantity)
        if batch.outcome_unsent(outcome):
            return await _unsent(a, w, outcome)
        row = w.rows.get(outcome.order_id) if outcome.order_id else None
        if outcome.verdict == calls.REJECTED or status_of(row) == "REJECTED":
            message = _message(outcome) or (row.reject_reason if row else None) or ""
            verdict, reason, issue = _rejection(a, w, message)
            return await _finish(a, w, verdict, reason, issue=issue, sent=True)
        a.session.guard.note_rejection(w.case.segment, expected=True)
        stuck = w.notes.get("stuck")
        if filled:
            positions.add_fill(tsym, filled, lot_size=contract.lot_size, case_id=w.case.id,
                               order_id=outcome.order_id)  # fmt: skip
        if stuck:
            return await _finish(a, w, batch.STUCK, f"the BUY order {stuck[0]} is stuck: not "
                                 "final after its cancel", sent=True)  # fmt: skip
        if not filled:
            return await _finish(a, w, batch.SKIP, f"the BUY at the last price did not fill "
                                 f"({status_of(row)}); cancelled", sent=True)  # fmt: skip
        kept = f"the position ({filled}) stays in the run's position ledger"
        try:
            close, closed = await _fill_leg(a, w, "SELL", filled)
        except (Refusal, StopRun) as exc:
            positions.close_attempt(tsym, 0, case_id=w.case.id, order_id=None, not_sent=exc.reason)
            return await _finish(a, w, batch.SKIP, f"close not sent: {exc.reason}; {kept}",
                                 sent=True)  # fmt: skip
        positions.close_attempt(tsym, closed, case_id=w.case.id, order_id=close.order_id)
        w.notes.update(filled=filled, closed=closed, leftover=positions.net(tsym))
        close_row = w.rows.get(close.order_id) if close.order_id else None
        if batch.outcome_unsent(close):
            return await _finish(a, w, batch.SKIP, f"close not sent: {_message(close)}; {kept}",
                                 sent=True)  # fmt: skip
        if close.verdict == calls.REJECTED or status_of(close_row) == "REJECTED":
            message = _message(close) or (close_row.reject_reason if close_row else None) or ""
            kind = expect.environment_refusal(message)
            if kind is not None:  # neutral to the breaker (no note_rejection)
                return await _finish(a, w, batch.ENV_LIMIT, f"environment limit ({kind}) on the "
                                     f"closing SELL: {message}; {kept}", sent=True)  # fmt: skip
            if expect.is_rms_limit(message):
                a.run.issues.rms_limit(w.case.family, message)
                a.session.guard.note_rejection(w.case.segment, expected=True)
                return await _finish(a, w, batch.ENV_LIMIT, f"environment limit (RMS) on the "
                                     f"closing SELL: {message}; {kept}", issue="RMS",
                                     sent=True)  # fmt: skip
            a.session.guard.note_rejection(w.case.segment, expected=False)
            return await _finish(a, w, batch.FAIL, f"the closing SELL was refused: {message}; "
                                 f"{kept}", sent=True)  # fmt: skip
        a.session.guard.note_rejection(w.case.segment, expected=True)
        if w.notes.get("stuck"):
            return await _finish(a, w, batch.STUCK, f"the closing SELL {w.notes['stuck'][0]} is "
                                 f"stuck: not final after its cancel; {kept}",
                                 sent=True)  # fmt: skip
        if closed >= filled:
            return await _finish(a, w, batch.XPASS, f"the closing SELL filled ({filled}): "
                                 "closing-sell did not show", issue="closing-sell",
                                 sent=True)  # fmt: skip
        return await _finish(a, w, batch.XFAIL, f"the closing SELL did not fill "
                             f"({status_of(close_row)}); the position ({filled - closed}) stays "
                             "in the run's position ledger", issue="closing-sell",
                             sent=True)  # fmt: skip


# -- async: views and the record ------------------------------------------------------------------


async def _views(a: _Async, w: _Work) -> dict[str, Any]:
    """The case's views through the async client (``views.views_record``)."""
    rows = {i: r for i, r in w.rows.items() if r is not None}
    lags: dict[str, float | None] = {}
    for order_id, row in rows.items():
        status = status_of(row)
        lag, _ = await _await_update(a, order_id, lambda u, s=status: status_of(u) == s)
        lags[order_id] = lag
    updates = {order_id: a.watcher.updates(order_id) for order_id in rows}
    history = {order_id: await a.client.order_history(order_id) for order_id in rows}
    trades = await a.client.trades() if rows else []
    deltas: dict[str, views_table.PositionDelta] = {}
    if rows:
        after = views_table.position_totals(await a.client.positions())
        for row in rows.values():
            key = views_table.position_key(row.exchange, row.tradingsymbol, row.product)
            deltas[key] = views_table.position_delta(w.baseline, after, key)
    return views_table.views_record(
        rows, history=history, trades=trades, updates=updates, deltas=deltas, lags=lags,
        want_stream=True,
    )  # fmt: skip


def _row_data(row: Order | None) -> dict[str, Any] | None:
    return None if row is None else dict(row.raw) or row.model_dump(mode="json", by_alias=True)


async def _finish(
    a: _Async,
    w: _Work,
    verdict: str,
    reason: str,
    *,
    issue: str | None = None,
    sent: bool = False,
) -> CaseResult:
    """The case's record (with its views when it sent anything) and ledger verdict."""
    case, contract, exp = w.case, w.contract, w.expectation
    data: dict[str, Any] = {
        "case_id": case.id,
        "family": case.family,
        "role": case.role,
        "segment": case.segment,
        "tier": case.tier,
        "canary": case.canary,
        "client": "async",
        "contract": None
        if contract is None
        else {
            k: contract.to_dict()[k]
            for k in ("exchange", "tradingsymbol", "token", "lot_size", "tick_size")
        },  # fmt: skip
        "snapshot": None if w.snapshot is None else dataclasses.asdict(w.snapshot),
        "plan": w.plan,
        "expectation": None
        if exp is None
        else {"verdict": exp.verdict, "reason": exp.reason, "issue": exp.issue, "words": exp.words},
        "calls": [dataclasses.asdict(o) for o in w.outcomes],
        "orders": {i: {"final": _row_data(w.rows.get(i))} for i in w.order_ids},
        "notes": {**w.notes, "steps": w.steps} if w.steps else dict(w.notes),
        "verdict": verdict,
        "reason": reason,
        "issue": issue,
    }
    if w.outcomes:
        try:
            data["views"] = await _views(a, w)
        except SESSION_ERRORS:
            raise
        except Exception as exc:
            data["views_error"] = f"{type(exc).__name__}: {exc}"
        call_ids = [o.call for o in w.outcomes if o.call]
        data["raw_calls"] = calls_from_ledger(a.session.ledger.path, call_ids)
    written = a.recorder.write(data)
    if sent:
        a.session.ledger.verdict(case.id, verdict, reason=reason)
    order_ids = w.order_ids
    row = w.rows.get(order_ids[0]) if order_ids else None
    return CaseResult(
        case_id=case.id,
        verdict=verdict,
        reason=reason,
        issue=issue,
        order_ids=order_ids,
        status=status_of(row),
        canary=case.canary,
        record=written,
    )
