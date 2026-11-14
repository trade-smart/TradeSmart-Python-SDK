"""The UAT matrix family M8: burst and pacing, and the ``stress`` tier (inventory §3.2).

A case is many far LIMIT BUYs of one share of NSE-B (``ITC-EQ``) placed **back to back** under one
family-level reservation (``Guard.reserve(case, n)``: the declared count is the burst size; each
order is its own ``calls.place``, never resent). Every place waits on the run's pacer first
(``batch.Pacer``: ``ORDER_PLACES_PER_MINUTE``, 180 places in any rolling minute, the same window
as every other family's places), then on the session's order pacer (``order_pacer``: at most 6
order calls in any rolling second, TradeSmart's ALGO_CHK counting place, modify, cancel and exit
against 10/s), then the SDK's own order limiter (10/s and 200/min, the defaults, never raised).
So live the burst runs at 6/s, its places at most 180 a minute, and the SDK's windows are not
reached; the notes say so (``matrix_pacer``).

- ``burst-...-50`` (full): 50 orders; ``burst-...-650`` (``stress``, opt-in): 650 orders, meant
  to run at the cap: the run's 180/min pace, under the SDK's 200/min (production refused the
  order once 200 had been received in the minute, 14 Nov 2026, F33).
- **Waves**: at most the ``MAX_OPEN`` room is placed at once (``MAX_OPEN`` is never raised); each
  wave is settled (one book poll per round), every order cancelled once and confirmed final
  before the next wave; the open-order invariant and the STOP file are checked before each wave
  and before each place call.
- **Prices**: a fresh quote right before each wave's first order, and again before an order
  when it is older than ``QUOTE_MAX_AGE`` seconds; every order is 10 % below that last price.
- **Stop at the first problem** (no blind repetition): a refusal, an unknown outcome, an order the
  SDK did not send. An RMS ``RED:`` refusal (U3), an LPP-band refusal and TradeSmart's per-minute
  order-count refusal (``exceeds Limit``) are environment limits (``ENV_LIMIT``), the last two
  neutral to the segment's breaker.

The verdict: every order acknowledged, ``OPEN``, cancelled once and final; no HTTP 429 or rate
refusal (an SDK ``RateLimitError`` here means the pacing failed: ``FAIL``); the client timestamps
keep the SDK's limits (``pacing_problems``: for every order ``i``, the order ``i + 10`` returned
at least one second after order ``i`` was started, and the order ``i + 200`` at least sixty
seconds after). ``limiter_engaged`` records whether each cap was reached (10 returned in a second;
180 started in a minute, the run's pace). A case larger than the run's pace whose starts never
reached 180 in a minute (the waves pause to settle and cancel) is ``record`` "180/min pace not
reached (max N/min)", never a pass. The timestamps and rates are in the case record.
"""

from __future__ import annotations

import time
from collections import Counter
from collections.abc import Callable, Sequence

from tests.live_orders.safety import Refusal, is_final, status_of
from tests.live_orders.uat_matrix import batch, calls, expect, family, pricing
from tests.live_orders.uat_matrix.batch import BatchItem, CaseResult, MatrixRun
from tests.live_orders.uat_matrix.calls import OrderOutcome
from tests.live_orders.uat_matrix.cases import FULL, REST, STRESS, Case
from tests.live_orders.uat_matrix.guard import STOP_REASON, MaxOpenReached, StopRun
from tests.live_orders.uat_matrix.ledger import INTERRUPTED, NOT_SENT
from tests.live_orders.uat_matrix.order_pacer import ORDER_CALLS_PER_SECOND
from tradesmart.config import ClientConfig
from tradesmart.models.orders import Order

FAMILY = "burst"
ROLE = "nse-b"
# The SDK's own order limits (the client defaults, never raised): 10/s and 200/min (F33).
PER_SECOND = ClientConfig.order_rate_limit_per_second
PER_MINUTE = ClientConfig.order_rate_limit_per_minute
LIMITS = ((PER_SECOND, 1.0), (PER_MINUTE, 60.0))
RUN_PACE = batch.ORDER_PLACES_PER_MINUTE  # the run's places per minute (180, under the SDK's 200)
Stamp = tuple[float, float]  # (started, returned) of one place call, client monotonic seconds
QUOTE_MAX_AGE = 30.0  # seconds a wave's quote prices orders before it is taken again
MATRIX_PACER_NOTE = (
    f"places paced at {RUN_PACE}/min by the run's pacer and every order call at "
    f"{ORDER_CALLS_PER_SECOND}/s by the matrix order pacer (TradeSmart's ALGO_CHK counts place, "
    f"modify, cancel and exit against 10/s): the SDK's {PER_SECOND}/s and {PER_MINUTE}/min are "
    "not reached; the stress case passes at the run's pace"
)


def _case(count: int, tier: str) -> Case:
    return Case(
        id=f"{FAMILY}-{ROLE}-mis-lmt-day-buy-{count}",
        family=FAMILY,
        role=ROLE,
        product="MIS",
        prctyp="LMT",
        ret="DAY",
        side="BUY",
        orders=count,
        outcome=REST,
        tier=tier,
    )


BURST_CASES: list[Case] = [_case(50, FULL), _case(650, STRESS)]


# -- pure helpers -------------------------------------------------------------------------------


def pacing_problems(
    stamps: Sequence[Sequence[float]], limits: Sequence[tuple[int, float]] = LIMITS
) -> list[str]:
    """What breaks the limits: for ``(count, window)``, the call ``i + count`` must return at
    least ``window`` seconds after the call ``i`` started (the SDK sends a call between its start
    and its return, so this always holds when the limiter paces the sends). One line per limit."""
    problems: list[str] = []
    for count, window in limits:
        for i in range(len(stamps) - count):
            span = stamps[i + count][1] - stamps[i][0]
            if span < window:
                problems.append(
                    f"{count + 1} place calls (from #{i + 1}) within {span:.3f} s: more than "
                    f"{count} per {window:g} s"
                )
                break
    return problems


def limiter_engaged(stamps: Sequence[Sequence[float]]) -> dict[str, bool]:
    """Whether the burst reached each cap: ``per_second`` when the SDK's 10 calls returned within
    a second, ``per_minute`` when the run's pace (180) started within a minute (its pacer held
    the places there)."""
    returned = [s[1] for s in stamps]
    started = [s[0] for s in stamps]
    return {
        "per_second": max_in_window(returned, 1.0) >= PER_SECOND,
        "per_minute": max_in_window(started, 60.0) >= RUN_PACE,
    }


def max_in_window(times: Sequence[float], window: float) -> int:
    """The most ``times`` that fall within any ``window`` seconds."""
    ordered = sorted(times)
    best, start = 0, 0
    for end, moment in enumerate(ordered):
        while moment - ordered[start] >= window:
            start += 1
        best = max(best, end - start + 1)
    return best


def is_rate_rejection(outcome: OrderOutcome) -> bool:
    """An HTTP 429, a ``RateLimitError`` (the server's or the SDK's own) or rate wording."""
    error = outcome.error or {}
    if error.get("http_status") == 429 or error.get("exception") == "RateLimitError":
        return True
    text = f"{error.get('emsg') or ''} {error.get('message') or ''}".lower()
    return "too many" in text or "rate limit" in text


# -- running ------------------------------------------------------------------------------------


def run_burst(
    run: MatrixRun, cases: Sequence[Case], *, clock: Callable[[], float] = time.monotonic
) -> dict[str, CaseResult]:
    """The result of every case: from this run's cache, else run now (``clock``: the client
    timestamps, real time like the SDK limiter)."""
    cache = family.results_of(run)
    for case in cases:
        if case.id not in cache:
            cache[case.id] = _run_one(run, case, clock)
    return {c.id: cache[c.id] for c in cases if c.id in cache}


def _places(work: batch._Work) -> list[OrderOutcome]:
    return [o for o in work.outcomes if o.kind == "place"]


def _run_one(run: MatrixRun, case: Case, clock: Callable[[], float]) -> CaseResult:
    session = run.session
    work = batch._Work(BatchItem(case))
    if not batch._before_round(run):
        batch._skip(run, work, session.guard.stop_reason or STOP_REASON)
    elif batch._prepare(run, work):  # reserves the whole burst for the case once
        stamps: list[Stamp] = []
        waves, halt = 0, None
        try:
            while halt is None and len(_places(work)) < case.orders:
                if waves and not batch._before_round(run):
                    halt = session.guard.stop_reason or STOP_REASON
                    break
                halt = _wave(run, work, case.orders - len(_places(work)), stamps, clock)
                waves += 1
                environment = _environment(work) if halt is None else None
                if environment is not None:  # rejected in the book: no next wave into it
                    halt = f"environment limit ({environment[0]}): {environment[1]}"
        except KeyboardInterrupt:
            batch._interrupt(session)
            raise
        _judge(run, work, halt, stamps, waves)
    assert work.result is not None
    return work.result


def _wave(
    run: MatrixRun,
    work: batch._Work,
    remaining: int,
    stamps: list[Stamp],
    clock: Callable[[], float],
) -> str | None:
    """Place up to the ``MAX_OPEN`` room back to back, then settle, cancel and confirm the wave
    (always). The reason the burst must stop, else ``None``."""
    session, guard, case, contract = run.session, run.session.guard, work.case, work.contract
    assert contract is not None
    wave = batch._Work(work.item, expectation=work.expectation, contract=contract)
    try:
        room = guard.max_open - guard.open_count - guard.pending
        if room < 1:
            guard.wait_for_room(
                1,
                lambda: calls.open_ours(session),
                timeout=run.room_timeout,
                poll_interval=session.poll_interval,
                sleep=session.sleep,
                clock=session.clock,
            )
            room = max(1, guard.max_open - guard.open_count - guard.pending)
        batch._recheck(run, work)
        plan: dict[str, object] | None = None
        quoted = 0.0
        for _ in range(min(room, remaining)):
            # the run's 180/min of places, shared with every family; before the quote check, so a
            # long wait at the cap never leaves the price stale
            run.pacer.wait()
            if plan is None or clock() - quoted >= QUOTE_MAX_AGE:  # the wave's first, or stale
                snapshot = pricing.fresh_quote(
                    session.client,
                    contract,
                    uat=session.uat,
                    sleep=session.sleep,
                    pacer=session.uat_quote_pacer(),
                )
                quoted = clock()
                plan = batch.default_plan(case, contract, snapshot)
                if work.plan is None:
                    work.snapshot, work.plan = snapshot, plan
            batch.check_hours(run, case)  # market hours before each place (any hour on UAT)
            started = clock()
            outcome = calls.place(session, plan, work.ref)
            stamps.append((started, clock()))
            wave.outcomes.append(outcome)
            work.outcomes.append(outcome)
            batch._early_rms(run, work, outcome)
            reason = _halt(run, work, outcome, len(stamps))
            if reason is not None:
                return reason
        return None
    except (StopRun, Refusal) as exc:
        return exc.reason
    except MaxOpenReached as exc:
        return f"MAX_OPEN: {exc}"
    except KeyboardInterrupt:
        batch._interrupt(session)
        raise
    except Exception as exc:  # the family's code: what was placed is still cancelled
        work.error = work.error or f"{type(exc).__name__}: {exc}"
        return f"raised: {work.error}"
    finally:
        if not session.interrupted:
            _end_wave(run, work, wave)


def _halt(run: MatrixRun, work: batch._Work, outcome: OrderOutcome, number: int) -> str | None:
    """Why the burst stops after this place call (no blind repetition), else ``None``."""
    guard, segment = run.session.guard, work.case.segment
    message = batch._message(outcome) or ""
    if outcome.verdict in (batch.CHECK, INTERRUPTED):
        return f"{batch.CHECK}: place #{number} has an unknown outcome"
    if outcome.verdict == NOT_SENT or batch.outcome_unsent(outcome):
        return f"place #{number} was not sent: {message}"
    if outcome.verdict == calls.REJECTED:
        kind = expect.environment_refusal(message)
        if kind is not None:  # neutral to the breaker (no note_rejection)
            return f"environment limit ({kind}) at place #{number}: {message}"
        if expect.is_rms_limit(message):
            return f"environment limit (RMS) at place #{number}: {message}"
        guard.note_rejection(segment, expected=False)
        return f"place #{number} refused: {message}"
    guard.note_rejection(segment, expected=True)
    return None


def _end_wave(run: MatrixRun, work: batch._Work, wave: batch._Work) -> None:
    """Settle the wave, cancel each open order once and confirm (``batch``), keep what it saw."""
    session = run.session
    if wave.order_ids:
        try:
            batch._settle(run, [wave])
        except KeyboardInterrupt:
            batch._interrupt(session)
            raise
        finally:
            if not session.interrupted:
                batch._cancel_and_confirm(run, [wave])
    work.settled.update(wave.settled)
    work.latest.update(wave.latest)
    work.final.update(wave.final)
    work.stuck.extend(wave.stuck)
    work.outcomes.extend(o for o in wave.outcomes if o.kind != "place")
    for key, value in wave.notes.items():
        work.notes.setdefault(key, []).extend(value)
    if wave.error is not None and work.error is None:
        work.error = wave.error


# -- judging --------------------------------------------------------------------------------------


def _rms(work: batch._Work) -> str | None:
    for outcome in _places(work):
        message = batch._message(outcome)
        if outcome.verdict == calls.REJECTED and expect.is_rms_limit(message):
            return message
    for row in work.settled.values():
        if (
            status_of(row) == "REJECTED"
            and row is not None
            and expect.is_rms_limit(row.reject_reason)
        ):
            return row.reject_reason
    return None


def _environment_refused(outcome: OrderOutcome) -> bool:
    """A place refused by an LPP-band or order-rate limit (``expect.environment_refusal``)."""
    return outcome.verdict == calls.REJECTED and (
        expect.environment_refusal(batch._message(outcome)) is not None
    )


def _environment_rejected(row: Order | None) -> bool:
    """An order the book rejected for an LPP-band or order-rate limit."""
    reason = row.reject_reason if row is not None else None
    return status_of(row) == "REJECTED" and expect.environment_refusal(reason) is not None


def _environment(work: batch._Work) -> tuple[str, str] | None:
    """The first LPP-band or order-rate refusal of the burst (kind, text), else ``None``: an
    environment limit at the cap, neutral to the segment's breaker."""
    for outcome in _places(work):
        if _environment_refused(outcome):
            message = batch._message(outcome) or ""
            return expect.environment_refusal(message) or "", message
    for row in work.settled.values():
        if _environment_rejected(row):
            reason = str(row.reject_reason if row is not None else "")
            return expect.environment_refusal(reason) or "", reason
    return None


def _judge(
    run: MatrixRun, work: batch._Work, halt: str | None, stamps: list[Stamp], waves: int
) -> None:
    case, guard = work.case, run.session.guard
    places = _places(work)
    returned = [s[1] for s in stamps]
    work.notes.update(
        placed=len(places),
        waves=waves,
        max_open=guard.max_open,
        stamps=[list(s) for s in stamps],
        max_per_second=max_in_window(returned, 1.0),
        max_per_minute=max_in_window(returned, 60.0),
        max_starts_per_minute=max_in_window([s[0] for s in stamps], 60.0),
        limiter_engaged=limiter_engaged(stamps),
        matrix_pacer=MATRIX_PACER_NOTE,
        seconds=round(stamps[-1][1] - stamps[0][0], 3) if stamps else 0.0,
        halt=halt,
    )
    if not places:
        batch._skip(run, work, halt or "nothing was placed")
        return
    if any(o.verdict in (batch.CHECK, INTERRUPTED) for o in places):
        batch._finish(run, work, batch.CHECK, f"{batch.CHECK}: {halt}", sent=True)
        return
    if batch._unknown_later(run, work):
        return
    if work.stuck:
        batch._finish(
            run, work, batch.STUCK,
            f"order {work.stuck[0]} stuck: not final after its cancel; the {case.segment} "
            "circuit breaker tripped", sent=True,
        )  # fmt: skip
        return
    rms = _rms(work)
    if rms is not None:
        with run._lock:
            run.issues.rms_limit(case.family, rms)
        guard.note_rejection(case.segment, expected=True)
        batch._finish(run, work, batch.ENV_LIMIT, f"environment limit (RMS) after "
                      f"{len(places)} orders: {rms}", issue="RMS", sent=True)  # fmt: skip
        return
    problems = _problems(run, work, places, stamps)
    environment = _environment(work)
    if problems:
        batch._finish(run, work, batch.FAIL, "; ".join(problems), sent=True)
    elif environment is not None:  # LPP band or order rate: neutral to the breaker (no note)
        kind, message = environment
        batch._finish(run, work, batch.ENV_LIMIT, f"environment limit ({kind}) after "
                      f"{len(places)} orders: {message}", sent=True)  # fmt: skip
    elif batch.UAT_FILLED_FAR in work.notes:  # UAT fills at any price: recorded
        filled = work.notes[batch.UAT_FILLED_FAR]["filled"]
        batch._finish(run, work, batch.RECORD, f"{batch.uat_fill_reason(filled)}; {len(places)} "
                      "placed", sent=True)  # fmt: skip
    elif halt is not None:  # a stop (STOP file, foreign order, breaker, MAX_OPEN), not a finding
        batch._finish(run, work, batch.SKIP, f"{halt}; {len(places)} of {case.orders} placed "
                      "and cancelled", sent=True)  # fmt: skip
    else:
        per_minute = work.notes["max_starts_per_minute"]
        done = (
            f"{len(places)} far LIMIT BUYs placed back to back in {waves} wave(s) (MAX_OPEN "
            f"{guard.max_open}), each acknowledged, OPEN, cancelled once and final; no rate "
            f"refusal; at most {work.notes['max_per_second']} returned in any second and "
            f"{per_minute} started in any minute (the SDK allows {PER_MINUTE})"
        )
        if case.orders <= RUN_PACE:
            batch._finish(run, work, batch.PASS, done, sent=True)
        elif work.notes["limiter_engaged"]["per_minute"]:  # the pacer held it at the cap
            batch._finish(run, work, batch.PASS, f"at the run's {RUN_PACE}/min pace: {done}",
                          sent=True)  # fmt: skip
        else:  # the waves' settle and cancel pauses kept the places under the pace
            batch._finish(run, work, batch.RECORD, f"{RUN_PACE}/min pace not reached (max "
                          f"{per_minute}/min): {done}", sent=True)  # fmt: skip


def _problems(
    run: MatrixRun, work: batch._Work, places: list[OrderOutcome], stamps: list[Stamp]
) -> list[str]:
    problems: list[str] = []
    rate = [n for n, o in enumerate(places, 1) if is_rate_rejection(o)]
    if rate:
        problems.append(f"HTTP 429 / rate-limit refusal at place #{rate[0]} ({len(rate)} in all)")
    acknowledged = (calls.ACKNOWLEDGED, calls.FOUND)
    refused = [
        (n, o)
        for n, o in enumerate(places, 1)
        if o.verdict not in acknowledged and not _environment_refused(o)  # judged apart
    ]
    if refused:
        n, first = refused[0]
        problems.append(
            f"{len(refused)} place call(s) not acknowledged; #{n}: {first.verdict} "
            f"({batch._message(first)})"
        )
    ids = work.order_ids
    settled = Counter(
        status_of(work.settled.get(i))
        for i in ids
        if not _environment_rejected(work.settled.get(i))
    )  # an LPP-band or order-rate rejection is judged apart (``_environment``)
    filled = sum((r.filled_quantity or 0) for r in work.final.values() if r is not None)
    uat_fill = False
    if filled and work.contract is not None:
        batch._add_fill(run, work, filled, ids[0] if ids else None)
        uat_fill = batch.note_uat_fill(run, work, filled)  # UAT fills at any price: recorded
        if not uat_fill:
            problems.append(f"orders away from the market filled ({filled})")
    if set(settled) - ({"OPEN", "COMPLETE"} if uat_fill else {"OPEN"}):
        problems.append(f"orders not OPEN after placing: {dict(settled)}")
    if not all(is_final(status_of(work.final.get(i))) for i in ids):
        problems.append("not every order is final after its cancel")
    problems += pacing_problems(stamps)
    if work.error is not None:
        problems.append(f"raised: {work.error}")
    return problems
