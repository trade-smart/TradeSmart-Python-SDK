"""The UAT matrix batch engine: place a batch, observe it, act on it, cancel it, record it.

``run_batch(run, cases)`` takes cases (``cases.Case``, or ``BatchItem`` for a family's own plan
or hook) and returns one ``CaseResult`` per case, in order, each also written as one JSON line
(``record.py``). Everything that changes the account goes through ``calls`` (one call per
intent, never resent); the engine adds, per batch:

1. **Before each round**: the run is not stopped, and ``check_open_orders`` (every open order of
   the account is ours or ignored) holds; a foreign order stops the run (``calls.stop_run``:
   ours cancelled once, nothing else touched) and every remaining case skips with the reason.
2. **Canaries first** (BSE, BFO; F26): a canary that rests ``OPEN`` lets its segment run, one
   that stays ``PENDING`` makes the segment skip for the run. Then the other cases, in rounds of
   at most the ``MAX_OPEN`` room, then the fill cases.
3. **Per case, before its place call**: not done in this run (resume), the expectation oracle
   (``expect.expectation``: known issues, unavailable roles, the RMS environment limit, the
   profile), the segment's circuit breaker, the declared order count (``Guard.reserve``); then,
   **immediately before the call**, ``pricing.fresh_quote`` (no last price: skipped, never a
   priceless order) and the plan priced from that snapshot only.
4. **Observe**: one ``orders()`` poll per round for the whole batch (plus the order stream when
   a ``views.OrderWatcher`` is attached), until every order settled or ``settle_seconds``.
5. **Family hooks** (``BatchItem.after_settle``) act on resting orders, e.g. ``Placed.modify``
   (``calls.modify`` with ``before=`` the batch row: no extra read).
6. **Cancel** every order of the batch that is not final (one cancel each), **confirm** with
   polls until ``confirm_seconds``; one still open is stuck: its segment's breaker trips.
7. **Judge** each case against its expectation (``guard.note_rejection`` after every outcome, so
   three unexpected rejections in a row trip the segment); RMS ``RED:`` is an environment limit
   for the family; ``ledger.verdict`` per case that sent anything (a resume skips it).

The run has **one market path**: before a MKT / SL-MKT place call the engine claims it in the
ledger (``market_claim``) and counts a full lot of the instrument as filled (position ledger,
against the ceiling); the claim is released (``market_release``) only when nothing was sent or
the order was refused for certain; an accepted order or an unknown outcome keeps it, so every
remaining market case of the run skips, in this process and after a resume (``_restore``).

The STOP file (``.live-results/uat-matrix/STOP``) mid-batch: no new place call, the batch is
cancelled and confirmed, every remaining case skips "stopped by the STOP file".

Fill cases (``fills`` outcome ``fill_close`` / ``short_cover``) run one at a time per instrument,
only before 15:30 IST (``FILL_CUTOFF``; on UAT at any hour), within the per-instrument position
ceiling (``positions``): an order at the last price, waited for up to ``fill_wait_seconds``, then
the closing order at the last price; a closing SELL that does not fill is an xfail
(``closing-sell``, non-strict) and the position stays in the run's position ledger (no CHECK
THE ACCOUNT on UAT).

UAT behaviours (``session.uat`` only; production judging unchanged): an order away from the
market that fills (UAT fills at any price) is RECORD with the note ``uat_filled_far`` (its fill in
the position ledger once, ``_add_fill``); before each place an instrument whose |net| position
reached the ceiling takes no further order (``ceiling_refusal``; ``BatchItem.closing`` exempt); a
stop-limit order seen ``OPEN`` where ``TRIGGER_PENDING`` was expected is RECORD (``uat_stop_open``);
the band refusals ``BEYOND CIRCUIT LIMITS`` / ``STOP PRICE IS NOT REASONABLE`` are RECORD
(``uat_band_refusal``) and never count for the segment's breaker.

Mock-session behaviours (``session.mock``: the production server during an exchange mock
session): no market-hours check and no 15:30 fill cut-off (``check_hours``,
``fill_cutoff_reason``); the position-ceiling pre-check stays on (``ceiling_refusal``); every
UAT-only recording rule above, the UAT quote pacer and the UAT lot rule are off (``session.uat``
is False), so cases are judged as on production (F27: MARKET and SL-MKT refused as expected).

Concurrency lives here only (threads; one pytest process). Place calls are paced at
``PACE_PER_SECOND`` / ``ORDER_PLACES_PER_MINUTE`` (9/s, 180/min; modifies count per second only),
under the SDK's own order limits (10/s, 200/min by default, never raised), so the SDK never has to
refuse an order for its client-side rate limit; and every
state-changing call of the run (place, modify, cancel, exit, GTT writes) also waits on the
session's ``order_pacer`` (6 in any rolling second: TradeSmart's ALGO_CHK counts them all). A
driver SDK serves one request at a time: its calls take the SDK's send slot before the pacer
wait and give it back after the write (``calls``), so a driver session uses the same worker
count as any other and its writes still cannot burst.
"""

from __future__ import annotations

import dataclasses
import itertools
import json
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime
from datetime import time as day_time
from decimal import Decimal
from pathlib import Path
from typing import Any, TypeVar

import pytest

from tests.live_orders import safety
from tests.live_orders.safety import CHECK_THE_ACCOUNT, Refusal, is_final, status_of
from tests.live_orders.uat_matrix import calls, expect, pricing
from tests.live_orders.uat_matrix import cases as case_table
from tests.live_orders.uat_matrix import contracts as contract_table
from tests.live_orders.uat_matrix.calls import CaseRef, OrderOutcome
from tests.live_orders.uat_matrix.cases import Case
from tests.live_orders.uat_matrix.contracts import Contract, Contracts, Unavailable
from tests.live_orders.uat_matrix.expect import Expectation, KnownIssues, Profile
from tests.live_orders.uat_matrix.gate import MatrixSession
from tests.live_orders.uat_matrix.guard import STOP_REASON, MaxOpenReached, StopRun
from tests.live_orders.uat_matrix.ledger import (
    INTERRUPTED,
    NOT_SENT,
    Ledger,
    check_open_orders,
    nothing_sent,
    read_book,
)
from tests.live_orders.uat_matrix.positions import PositionLedger
from tests.live_orders.uat_matrix.pricing import PriceSnapshot
from tests.live_orders.uat_matrix.quote_pacer import QuotePacer
from tests.live_orders.uat_matrix.record import Recorder
from tests.live_orders.uat_matrix.views import OrderWatcher, cross_check
from tradesmart.models.base import IST
from tradesmart.models.orders import Order

# Case verdicts (``CaseResult.verdict``; also the ledger's verdict of a case that sent anything).
PASS = "pass"
FAIL = "fail"
RECORD = "record"  # undocumented behaviour recorded (U13), or a documented refusal not seen
SKIP = "skip"
XFAIL = "xfail"  # a known UAT issue showed (closing-sell)
XPASS = "xpass"  # a known UAT issue did not show (non-strict)
ENV_LIMIT = "environment limit"  # an RMS RED: refusal (U3)
STUCK = "stuck"  # not final after its cancel: the segment's breaker tripped (F26 for a canary)
CHECK = CHECK_THE_ACCOUNT
MARKET_FILL = "market order filled"  # an accepted MKT / SL-MKT: counted as a fill
PASSING = frozenset({PASS, RECORD, XPASS, MARKET_FILL})

PACE_PER_SECOND = 9  # place calls per second (the SDK allows 10)
# Place calls (one per place, one per slice) per 60 seconds: 180, under the SDK's default of 200
# (the order limits' per-minute window; modifies, cancels and exits are not counted, D3).
ORDER_PLACES_PER_MINUTE = 180
# Fill cases (and the market path) only before this time (IST), outside UAT. On UAT
# (``session.uat``) there is no cut-off and no market-hours check (owner, 2026-11-13: UAT tests run
# at any hour; a case the closed market refuses or leaves unfilled is recorded as on any day).
# Funded mode keeps its own 14:30 (``tests/live_orders/funded.py``).
FILL_CUTOFF = day_time(15, 30)
SETTLE_SECONDS = 8.0  # how long a batch is observed until every order settled
FILL_WAIT_SECONDS = 60.0  # how long an order at the last price is given to fill
ROOM_TIMEOUT = 60.0  # how long a place waits for MAX_OPEN room
WORKERS = 4  # threads placing / cancelling in a batch
WORKING = frozenset({"OPEN", "TRIGGER_PENDING"})
MARKET_TYPES = frozenset({"MKT", "SL-MKT"})
# The run's single market path (I1): claimed in the ledger before a market order is sent,
# released only when nothing went out or it was refused for certain; a resume restores it.
MARKET_CLAIM = "market_claim"
MARKET_RELEASE = "market_release"
FILL_OUTCOMES = frozenset({case_table.FILL_CLOSE, case_table.SHORT_COVER})
# Families whose canary unlocks their own cases only (the AMO canary: an after-market order
# resting says nothing about regular orders); a resume never restores it into the segment's
# canary state (``_restore``), the family keeps it apart (``cases_amo``).
SEPARATE_CANARY_FAMILIES = frozenset({"amo"})
# Families that keep their own placing window (``cases_amo``: outside hours, on UAT too); every
# other case is placed only inside market hours outside UAT, checked right before each place
# (``check_hours``), and at any hour on UAT (owner, 2026-11-13) and in a mock session.
OWN_WINDOW_FAMILIES = frozenset({"amo"})
_SETTLED = {
    case_table.REST: "OPEN",
    case_table.TRIGGER_PENDING: "TRIGGER_PENDING",
    case_table.EXCHANGE_CANCEL: "CANCELED",
}


# -- pacing ---------------------------------------------------------------------------------------


class Pacer:
    """Sliding windows over this run's calls: every call in the per-second window, place calls
    (``place=True``) also in the per-minute window, under the SDK's 10/s and 200/min (D3: modifies,
    cancels and exits never count per minute). Waits, never raises."""

    def __init__(
        self,
        *,
        per_second: int = PACE_PER_SECOND,
        per_minute: int = ORDER_PLACES_PER_MINUTE,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.per_second = per_second
        self.per_minute = per_minute
        self.clock = clock
        self.sleep = sleep
        self._second: deque[float] = deque(maxlen=per_second)
        self._minute: deque[float] = deque(maxlen=per_minute)
        self._lock = threading.Lock()

    def wait(self, *, place: bool = True) -> None:
        """Block until one more call fits its windows, then count it."""
        with self._lock:
            while True:
                now = self.clock()
                wait = 0.0
                windows = [(self._second, self.per_second, 1.0)]
                if place:
                    windows.append((self._minute, self.per_minute, 60.0))
                for stamps, count, window in windows:
                    if len(stamps) >= count and now - stamps[0] < window:
                        wait = max(wait, stamps[0] + window - now)
                if wait <= 0.0:
                    self._second.append(now)
                    if place:
                        self._minute.append(now)
                    return
                self.sleep(wait)


# -- plans --------------------------------------------------------------------------------------

PlanBuilder = Callable[[Case, Contract, PriceSnapshot], dict[str, Any]]


# -- a CNC SELL needs shares (full live run, 13 Nov 2026: "No Holdings uploaded") --------------

HOLD_ROLE = "nse-hold"  # the M12 family's role: it reads holdings itself before each SELL


def is_cnc_sell(plan: Mapping[str, Any]) -> bool:
    """A delivery (CNC) SELL."""
    product = str(plan.get("product", "")).upper()
    return product == "CNC" and str(plan.get("transaction_type", "")).upper() == "SELL"


def cnc_covered(run: MatrixRun, plan: Mapping[str, Any]) -> bool:
    """A CNC BUY of the same stock and at least the same quantity filled earlier in this run."""
    quantity = int(plan.get("quantity", 0))
    return run.positions.cnc_bought(str(plan.get("tradingsymbol"))) >= quantity > 0


def holdings_refusal(plan: Mapping[str, Any], holdings: Sequence[Any]) -> str | None:
    """Why ``plan`` (a CNC SELL) must not go out given a fresh ``holdings()`` read, or ``None``
    when the stock's sellable quantity covers it."""
    from tests.live_orders.uat_matrix.cases_hold import sellable  # noqa: PLC0415 (a cycle)

    tsym, exchange = str(plan.get("tradingsymbol")), str(plan.get("exchange", "")).upper()
    quantity = int(plan.get("quantity", 0))
    held = 0
    for holding in holdings:
        for listing in getattr(holding, "instruments", None) or []:
            if (listing.exchange or "").strip().upper() == exchange and (
                (listing.tradingsymbol or "").strip() == tsym
            ):
                held += sellable(holding)
                break
    if held >= quantity:
        return None
    return (
        f"{tsym}: a CNC SELL of {quantity} needs a CNC BUY of it filled earlier in this run or "
        f"that many sellable shares in holdings() ({held} held); no order"
    )


def cnc_sell_refusal(
    run: MatrixRun, case: Case, plan: Mapping[str, Any], read: Callable[[], Sequence[Any]]
) -> str | None:
    """Why a CNC SELL must not be sent (``None``: send it, or not a CNC SELL): it goes out only
    after a CNC BUY of the same stock and quantity filled in this run, or with that sellable
    quantity in a fresh, read-only ``holdings()`` (``read``). The M12 family checks its own."""
    if not is_cnc_sell(plan) or case.role == HOLD_ROLE or cnc_covered(run, plan):
        return None
    try:
        holdings = read()
    except contract_table.SESSION_ERRORS:
        raise
    except contract_table.DATA_ERRORS as exc:
        return f"holdings() could not be read ({type(exc).__name__}: {exc}); no CNC SELL sent"
    return holdings_refusal(plan, holdings)


def default_plan(
    case: Case, contract: Contract, snapshot: PriceSnapshot, *, side: str | None = None
) -> dict[str, Any]:
    """The ``place_order`` keyword arguments of ``case`` (no tag: ``calls.place`` sets it),
    priced from ``snapshot`` only (a fresh quote): far from the market for a resting case, at the
    last price for a fill case; stops away from the market; market orders without a price."""
    if not isinstance(snapshot, PriceSnapshot):
        raise TypeError(
            f"prices come only from fresh_quote(client, contract), not {type(snapshot).__name__}"
        )
    side = side or case.side
    buy = side == "BUY"
    option = contract.kind == "opt"
    plan: dict[str, Any] = {
        "exchange": contract.exchange,
        "tradingsymbol": contract.tradingsymbol,
        "quantity": contract.quantity(1),
        "transaction_type": side,
        "product": case.product,
        "order_type": case.prctyp,
        "validity": case.ret,
    }
    if case.prctyp == "LMT":
        if case.outcome in FILL_OUTCOMES:
            plan["price"] = pricing.at_last(snapshot, side)
        else:
            plan["price"] = (
                pricing.far_buy(snapshot, option=option)
                if buy
                else pricing.far_sell(snapshot, option=option)
            )
    elif case.prctyp in ("SL-LMT", "SL-MKT"):
        stop = pricing.stop_buy if buy else pricing.stop_sell
        trigger, limit = stop(snapshot, option=option)
        plan["trigger_price"] = trigger
        if case.prctyp == "SL-LMT":
            plan["price"] = limit
    return plan


def fill_cutoff_reason(now: datetime, *, uat: bool = False, mock: bool = False) -> str | None:
    """Why a fill case must not start now (at or after ``FILL_CUTOFF``, 15:30 IST), else None;
    never on UAT (``uat``: the session's ``uat``; owner, 2026-11-13) and never in a mock session
    (``mock``: the session's ``mock``; no hours rule there, mock positions are wiped)."""
    if uat or mock:
        return None
    local = now.astimezone(IST)
    if local.time() >= FILL_CUTOFF:
        return (
            f"fill cases run before {FILL_CUTOFF:%H:%M} IST only (now {local:%H:%M} IST), so an "
            "intraday leftover is not squared off mid-case"
        )
    return None


# -- resume: roles without a last price ---------------------------------------------------------

PRICE_KINDS = frozenset({contract_table.NO_LAST_PRICE})


def refresh_unavailable(
    client: contract_table.MarketReader,
    saved: Contracts,
    today: date,
    *,
    run_dir: Path | None = None,
    uat: bool = False,
    sleep: Callable[[float], None] = time.sleep,
    pacer: QuotePacer | None = None,
) -> Contracts:
    """Resolve again the roles a resumed run's ``contracts.json`` lists without a last price
    (written before the open, say), and the options of a future resolved again; every resolved
    contract is kept as it was. Read-only calls; nothing price-related: no call at all."""
    stale = {role for role, u in saved.unavailable.items() if u.kind in PRICE_KINDS}
    if not stale:
        return saved
    market = contract_table._Market(client, uat=uat, sleep=sleep, pacer=pacer)
    fresh: dict[str, Contract | Unavailable] = {}
    for cash in contract_table.CASH:
        if cash.role in stale:
            fresh[cash.role] = contract_table._cash(market, cash)
    for future in contract_table.FUTURES:
        if future.role in stale:
            fresh[future.role] = contract_table._future(market, future, today)
    used = {c.token for c in saved.contracts.values()}
    for options in contract_table.OPTIONS:
        redo_future = options.future_role in fresh
        roles = [
            role
            for role in options.roles()
            if role in stale
            or (
                redo_future
                and role in saved.unavailable
                and saved.unavailable[role].kind == contract_table.NO_UNDERLYING
            )
        ]
        if not roles:
            continue
        underlying = saved.get(options.future_role) or fresh.get(options.future_role)
        if isinstance(underlying, Contract):
            ladder = contract_table._ladder(market, options, today, underlying, None)
        else:
            why = underlying if isinstance(underlying, Unavailable) else None
            ladder = contract_table._ladder(market, options, today, None, why)
        for role in roles:
            value = ladder[role]
            if isinstance(value, Contract) and value.token in used:
                continue  # that strike is another role's contract already
            fresh[role] = value
    merged = Contracts(saved.day, dict(saved.contracts), dict(saved.unavailable))
    for role, value in fresh.items():
        if isinstance(value, Contract):
            merged.contracts[role] = value
            merged.unavailable.pop(role, None)
        else:
            merged.unavailable[role] = value
    if run_dir is not None:
        path = run_dir / contract_table.CONTRACTS_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(merged.to_dict(), indent=2) + "\n", encoding="utf-8")
    return merged


# -- the run ------------------------------------------------------------------------------------


@dataclass
class MatrixRun:
    """Everything a batch needs beyond the session: contracts, oracle state, outputs."""

    session: MatrixSession
    contracts: Contracts
    profile: Profile
    issues: KnownIssues
    recorder: Recorder
    positions: PositionLedger
    watcher: OrderWatcher | None = None
    now: Callable[[], datetime] = field(default=lambda: datetime.now(IST))
    pacer: Pacer = field(default_factory=Pacer)
    workers: int = WORKERS
    settle_seconds: float = SETTLE_SECONDS
    fill_wait_seconds: float = FILL_WAIT_SECONDS
    room_timeout: float = ROOM_TIMEOUT
    market_filled: str | None = None  # set by the first accepted MKT / SL-MKT order
    _lock: threading.RLock = field(default_factory=threading.RLock)
    _market_lock: threading.Lock = field(default_factory=threading.Lock)
    _round_lock: threading.Lock = field(default_factory=threading.Lock)  # invariant checks
    _persisted_trips: set[str] = field(default_factory=set)  # breaker trips in the ledger


def open_run(
    session: MatrixSession,
    *,
    contracts: Contracts | None = None,
    profile: Profile | None = None,
    issues: KnownIssues | None = None,
    watcher: OrderWatcher | None = None,
    now: Callable[[], datetime] | None = None,
) -> MatrixRun:
    """The run of ``session``: contracts (resolved, or read back and refreshed on a resume),
    the account profile, the known-issue state (restored from the ledger's verdicts on a
    resume), the record file and the position ledger."""
    client = session.client
    if contracts is None:
        contracts = contract_table.resolve_contracts(
            client,
            session.trading_day,
            run_dir=session.run_dir,
            uat=session.uat,
            sleep=session.sleep,
            pacer=session.uat_quote_pacer(),
        )
        if session.resumed:
            contracts = refresh_unavailable(
                client,
                contracts,
                session.trading_day,
                run_dir=session.run_dir,
                uat=session.uat,
                sleep=session.sleep,
                pacer=session.uat_quote_pacer(),
            )
    if profile is None:
        profile = Profile.from_details(client.profile())
    if issues is None:
        issues = KnownIssues(pinned=expect.load_pinned())
    issues.add_unavailable(contracts)
    forbidden = session.ledger.forbidden
    run = MatrixRun(
        session=session,
        contracts=contracts,
        profile=profile,
        issues=issues,
        recorder=Recorder(session.run_dir, run_id=session.run_id, forbidden=forbidden),
        positions=PositionLedger(session.positions_path, forbidden=forbidden),
        watcher=watcher,
    )
    if now is not None:
        run.now = now
    _restore(run)
    # Every later trip, from any path (batch, burst, sliced, async views, GTT), is written to
    # the ledger when it happens, so a resume restores it.
    session.guard.on_trip = lambda segment, reason: _persist_trip(run, segment, reason)
    _persist_trips(run)
    return run


def _ledger_verdicts(run: MatrixRun) -> dict[str, str]:
    """Every case's latest verdict in the run's ledger file, whatever its family (in order)."""
    return {case_id: verdict for case_id, (verdict, _) in _ledger_lines(run).items()}


def _ledger_lines(run: MatrixRun) -> dict[str, tuple[str, str]]:
    """Every case's latest ``(verdict, reason)`` in the run's ledger file (in order)."""
    return _verdict_lines(run.session.ledger.path)


def _verdict_lines(path: Path) -> dict[str, tuple[str, str]]:
    """Every case's latest ``(verdict, reason)`` in the ledger file at ``path`` (in order)."""
    verdicts: dict[str, tuple[str, str]] = {}
    for line in path.read_text(encoding="utf-8").splitlines() if path.exists() else []:
        try:
            record = json.loads(line)
        except ValueError:
            continue  # a torn last line
        if isinstance(record, dict) and record.get("type") == "verdict" and record.get("case_id"):
            verdicts.pop(str(record["case_id"]), None)  # the latest verdict last
            verdicts[str(record["case_id"])] = (
                str(record.get("verdict")),
                str(record.get("reason") or ""),
            )
    return verdicts


def _market_claims(run: MatrixRun) -> list[str]:
    """The cases whose market claim (``market_claim``) an earlier process never released: the
    run's market path is used (an order went out, or may have)."""
    return _claims_at(run.session.ledger.path)


def _claims_at(path: Path) -> list[str]:
    """The unreleased market claims of the ledger file at ``path``."""
    open_claims: dict[str, None] = {}
    for line in path.read_text(encoding="utf-8").splitlines() if path.exists() else []:
        try:
            record = json.loads(line)
        except ValueError:
            continue  # a torn last line
        if not isinstance(record, dict) or not record.get("case_id"):
            continue
        if record.get("type") == MARKET_CLAIM:
            open_claims[str(record["case_id"])] = None
        elif record.get("type") == MARKET_RELEASE:
            open_claims.pop(str(record["case_id"]), None)
    return list(open_claims)


def _sibling_market(run: MatrixRun) -> str | None:
    """The run's single market path is shared by every SDK of the group (one budget, design
    §9.1): another SDK's run that sent a market order (a ``market fill`` verdict, or a claim it
    never released) uses it for this run too."""
    for path in run.session.sibling_ledgers():
        used = [case_id for case_id, (v, _) in _verdict_lines(path).items() if v == MARKET_FILL]
        used += _claims_at(path)
        if used:
            return f"{_market_reason(used[0])} (the run {path.parent.name} of another SDK)"
    return None


def _restore(run: MatrixRun) -> None:
    """A resumed run: the known-issue state its earlier verdicts imply. The market fill and
    the RMS limits come from every verdict line of the ledger (a case's family is its id's
    prefix), so they never depend on which family modules this process imported; the
    canaries are cases of the table."""
    ledger, issues = run.session.ledger, run.issues
    earlier = f"seen earlier in run {run.session.run_id}"
    for case_id, (verdict, reason) in _ledger_lines(run).items():
        if verdict == ENV_LIMIT:  # only an account-wide limit skips the family again
            issues.rms_limit(case_id.split("-", 1)[0], f"{expect.RMS_MARK} {earlier}: {reason}")
        if verdict == MARKET_FILL and run.market_filled is None:
            run.market_filled = _market_reason(case_id)
        family, _, rest = case_id.partition("-")
        if verdict == STUCK and family in SEPARATE_CANARY_FAMILIES and case_id.endswith("-canary"):
            # A stuck AMO canary blocked its segment in its own process: a resume keeps that
            # (a passed one never unlocks the segment: its canary is the family's own).
            issues.canary_stuck(
                rest.split("-", 1)[0], f"(an earlier process of run {run.session.run_id})"
            )
    claimed = _market_claims(run)
    if claimed and run.market_filled is None:
        run.market_filled = _market_reason(claimed[0])
    if run.market_filled is None:
        run.market_filled = _sibling_market(run)
    for case in case_table.registered():  # CASES and the families' registered cases
        if case.canary and case.family in SEPARATE_CANARY_FAMILIES:
            continue  # its canary unlocks its own family's cases only, never the segment's
        canary_verdict = ledger.done(case.id) if case.canary else None
        if canary_verdict == PASS:
            issues.canary_ok(case.segment)
        elif canary_verdict == STUCK:
            issues.canary_stuck(case.segment, f"(an earlier process of run {run.session.run_id})")
    _restore_trips(run)


def _restore_trips(run: MatrixRun) -> None:
    """A resumed run keeps every segment its earlier processes tripped closed: the persisted
    ``trip`` lines, every ``stuck`` order and every call with an unknown outcome."""
    session = run.session
    ledger, guard, path = session.ledger, session.guard, session.ledger.path
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    earlier = f"earlier in run {session.run_id}"
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue  # a torn last line
        if not isinstance(record, dict):
            continue
        kind, segment = record.get("type"), record.get("segment")
        if kind == "trip" and segment:
            guard.trip(str(segment), f"{record.get('reason')} ({earlier})")
        elif kind == "stuck" and segment:
            guard.trip(str(segment), f"order {record.get('order_id')} stuck ({earlier})")
    # Every tag with an unknown outcome: CHECK THE ACCOUNT / interrupted outcomes, and every
    # intent left without its outcome (a process killed mid-call), place intents included.
    for tag in sorted(ledger.flagged_tags()):
        tagged = ledger.segment_of(tag)
        if tagged:
            unknown = f"{CHECK_THE_ACCOUNT}: tag {tag} has an unknown outcome"
            guard.trip(tagged, f"{unknown} ({earlier})")
    run._persisted_trips = set(guard.trips)


def _persist_trip(run: MatrixRun, segment: str, reason: str) -> None:
    """Write one breaker trip to the ledger, once per segment (a resume restores it)."""
    with run._lock:
        if segment not in run._persisted_trips:
            run.session.ledger.note("trip", segment=segment, reason=reason)
            run._persisted_trips.add(segment)


def _persist_trips(run: MatrixRun) -> None:
    """Write every breaker trip of this process not yet in the ledger (a belt: ``Guard.on_trip``
    writes each when it happens)."""
    for segment, reason in sorted(dict(run.session.guard.trips).items()):  # a copy
        _persist_trip(run, segment, reason)


# -- results and items --------------------------------------------------------------------------


@dataclass
class CaseResult:
    """The verdict of one case (``report`` turns it into the pytest outcome)."""

    case_id: str
    verdict: str
    reason: str
    issue: str | None = None
    order_ids: list[str] = field(default_factory=list)
    status: str | None = None
    canary: bool = False
    record: dict[str, Any] = field(default_factory=dict)


@dataclass
class Placed:
    """One placed order of a batch, as a family hook sees it (``BatchItem.after_settle``)."""

    run: MatrixRun
    case: Case
    contract: Contract
    snapshot: PriceSnapshot
    plan: dict[str, Any]
    outcome: OrderOutcome
    row: Order | None = None  # its order-book row from the batch poll
    notes: dict[str, Any] = field(default_factory=dict)  # recorded with the case
    calls: list[OrderOutcome] = field(default_factory=list)  # the hook's own calls

    @property
    def order_id(self) -> str | None:
        return self.outcome.order_id

    @property
    def ref(self) -> CaseRef:
        return CaseRef(self.case.id, self.case.segment)

    def modify(self, **changes: Any) -> OrderOutcome:
        """One ``calls.modify`` of this order: the plan's fields, overridden by ``changes``;
        ``before=`` is the batch row (no extra book read). Never repeated."""
        if self.order_id is None:
            raise Refusal(f"case {self.case.id}: no order to modify")
        keys = ("exchange", "tradingsymbol", "quantity", "price", "order_type", "product")
        fields = {k: self.plan[k] for k in (*keys, "trigger_price") if k in self.plan}
        fields.update(changes)
        outcome = calls.modify(self.run.session, self.order_id, self.ref, before=self.row, **fields)
        self.calls.append(outcome)
        return outcome


Hook = Callable[[Placed], None]


@dataclass
class BatchItem:
    """A case with the family's own plan builder and / or hook (both optional)."""

    case: Case
    plan: PlanBuilder | None = None
    after_settle: Hook | None = None
    # A closing order of a fill family (it reduces the run's position): exempt from the UAT
    # position-ceiling pre-check (``ceiling_refusal``).
    closing: bool = False


@dataclass(eq=False)  # identity: two items of one batch are never "equal"
class _Work:
    item: BatchItem
    expectation: Expectation | None = None
    contract: Contract | None = None
    snapshot: PriceSnapshot | None = None
    plan: dict[str, Any] | None = None
    outcomes: list[OrderOutcome] = field(default_factory=list)
    settled: dict[str, Order | None] = field(default_factory=dict)  # order id -> row
    latest: dict[str, Order | None] = field(default_factory=dict)  # after a family hook
    final: dict[str, Order | None] = field(default_factory=dict)
    placed: Placed | None = None
    stuck: list[str] = field(default_factory=list)
    stop: StopRun | None = None
    error: str | None = None  # an exception of the engine or the family code
    fill_recorded: bool = False  # its fill is in the position ledger (once per case)
    result: CaseResult | None = None
    notes: dict[str, Any] = field(default_factory=dict)

    @property
    def case(self) -> Case:
        return self.item.case

    @property
    def ref(self) -> CaseRef:
        return CaseRef(self.case.id, self.case.segment)

    @property
    def order_ids(self) -> list[str]:
        ids: list[str] = []
        for outcome in self.outcomes:
            for order_id in (outcome.order_id, *outcome.order_ids):
                if order_id and order_id not in ids:
                    ids.append(order_id)
        return ids


def _item(case: Case | BatchItem) -> BatchItem:
    return case if isinstance(case, BatchItem) else BatchItem(case)


# -- the batch ------------------------------------------------------------------------------------


INTERRUPTED_REASON = "interrupted (Ctrl-C): no further call; run the cleanup tool"
_T = TypeVar("_T")
_R = TypeVar("_R")


def _interrupt(session: MatrixSession) -> None:
    """Ctrl-C: the run stops (no new place or modify) and sends nothing more (the teardown
    cancels nothing either; the ledger lists ours for the cleanup tool)."""
    session.interrupted = True
    session.guard.stop(INTERRUPTED_REASON)


_POOL_IDS = itertools.count(1)


def _join_pool_threads(prefix: str) -> None:
    """Wait for every live thread of one pool, by its name prefix. ``ThreadPoolExecutor`` starts
    a worker and only then records it for ``shutdown()``: a Ctrl-C between the two leaves a
    worker (and its call in flight) that ``shutdown(wait=True)`` does not wait for."""
    current = threading.current_thread()
    for thread in threading.enumerate():
        if thread is not current and thread.name.startswith(prefix + "_"):
            thread.join()


def _in_pool(run: MatrixRun, fn: Callable[[_T], _R], items: Sequence[_T]) -> list[_R]:
    """``fn`` over ``items`` on the run's threads. On Ctrl-C (it reaches the waiting main
    thread) the run is stopped first, then every queued item is dropped and only the calls in
    flight finish, every one of them, before the interrupt is re-raised."""
    prefix = f"matrix-pool-{next(_POOL_IDS)}"
    pool = ThreadPoolExecutor(max_workers=max(1, run.workers), thread_name_prefix=prefix)
    try:
        futures = [pool.submit(fn, item) for item in items]
        return [future.result() for future in futures]
    except KeyboardInterrupt:
        _interrupt(run.session)
        pool.shutdown(wait=True, cancel_futures=True)  # only on Ctrl-C: drop what is queued
        _join_pool_threads(prefix)  # a worker started but not yet recorded by the pool
        raise
    finally:
        pool.shutdown(wait=True)


def run_batch(run: MatrixRun, cases: Iterable[Case | BatchItem]) -> list[CaseResult]:
    """Run ``cases`` as batches (see the module docstring); one result per case, in order."""
    works = [_Work(_item(c)) for c in cases]
    seen: set[str] = set()
    for work in works:
        if work.case.id in seen:
            raise ValueError(f"case {work.case.id} is twice in the batch")
        seen.add(work.case.id)
    canaries = [w for w in works if w.case.canary]
    fills = [w for w in works if not w.case.canary and w.case.outcome in FILL_OUTCOMES]
    rest = [w for w in works if not w.case.canary and w.case.outcome not in FILL_OUTCOMES]
    try:
        for queue in (canaries, rest):
            while queue and _before_round(run):
                guard = run.session.guard
                room = max(1, guard.max_open - guard.open_count - guard.pending)
                chunk, queue = queue[:room], queue[room:]
                _round(run, chunk)
                _persist_trips(run)
        if fills:
            _fill_phase(run, fills)
    except KeyboardInterrupt:
        _interrupt(run.session)
        raise
    finally:
        _persist_trips(run)
    for work in works:
        if work.result is None:
            reason = run.session.guard.stop_reason or STOP_REASON
            _skip(run, work, reason)
    return [w.result for w in works if w.result is not None]


def _before_round(run: MatrixRun) -> bool:
    """The run is not stopped and the open-order invariant holds (else the run stops)."""
    session = run.session
    with run._round_lock:  # fill groups check from several threads
        if session.guard.stopped():
            return False
        try:
            check_open_orders(
                session.client,
                session.ledger,
                session.ignored_open,
                guard=session.guard,
                sleep=session.sleep,
            )
        except StopRun as stop:
            calls.stop_run(session, stop)
            return False
        return True


def _prepare(run: MatrixRun, work: _Work) -> bool:
    """Every check before anything is sent; ``False`` (with a skip result) when it must not."""
    session, case = run.session, work.case
    reason = session.case_done(case.id)
    issue: str | None = None
    if reason is None and session.guard.stopped():
        reason = session.guard.stop_reason or STOP_REASON
    if reason is None:
        work.expectation = expect.expectation(case, run.profile, run.issues)
        if work.expectation.verdict == expect.SKIP:
            reason, issue = work.expectation.reason, work.expectation.issue
    if reason is None:
        tripped = session.guard.tripped(case.segment)
        if tripped is not None:
            reason = f"circuit breaker tripped for {case.segment}: {tripped}"
    if reason is None:
        work.contract = run.contracts.get(case.role)
        if work.contract is None:
            reason = f"{case.role}: no contract for the role in this run"
    if reason is None and case.prctyp in MARKET_TYPES and run.market_filled is not None:
        reason = run.market_filled
    if reason is None:
        try:
            session.guard.reserve(case.id, case.orders)
        except ValueError as exc:
            reason = f"not run twice in one process: {exc}"
    if reason is not None:
        _skip(run, work, reason, issue=issue)
        return False
    return True


def hours_refusal(
    case: Case, now: datetime, *, uat: bool = False, mock: bool = False
) -> str | None:
    """Why ``case`` must not be placed at ``now``, else ``None``: the families' market-hours
    rule (``family.hours_refusal``; ``TRADESMART_LIVE_ORDER_OUTSIDE_HOURS=1`` lifts it, as in
    every live suite), checked right before each place: MCX cases use the MCX window, the AMO
    family none here (its own window, ``cases_amo``), every other case the NSE window. On UAT
    (``uat``) none at all: UAT tests run at any hour (owner, 2026-11-13); in a mock session
    (``mock``) none at all either: the owner starts the run once mock trading opens."""
    if uat or mock or case.family in OWN_WINDOW_FAMILIES:
        return None
    segment = safety.SEGMENTS["mcx" if case.segment == "mcx" else "nse"]
    return safety.market_closed_reason(segment, now)


def check_hours(run: MatrixRun, case: Case) -> None:
    """``Refusal`` when ``case`` must not be placed now (``hours_refusal`` at ``run.now()``;
    never on UAT, ``run.session.uat``, nor in a mock session, ``run.session.mock``)."""
    reason = hours_refusal(case, run.now(), uat=run.session.uat, mock=run.session.mock)
    if reason is not None:
        raise Refusal(reason)


CEILING_REACHED = "position ceiling reached by UAT fills"
MOCK_CEILING_REACHED = "position ceiling reached by this mock-session run's fills"


def ceiling_refusal(run: MatrixRun, contract: Contract | None) -> str | None:
    """On UAT (which fills orders at any price) and in a mock session (real fills; the ceiling
    stays on): why no further order may go to an instrument whose |net| position in the run's
    position ledger reached the per-instrument ceiling, else ``None``. Closing orders of the
    fill families do not ask (they reduce the position)."""
    if not (run.session.uat or run.session.mock) or contract is None:
        return None
    tsym = contract.tradingsymbol
    if not run.positions.reached(tsym, lot_size=contract.lot_size):
        return None
    ceiling = run.positions.ceiling_lots
    reached = CEILING_REACHED if run.session.uat else MOCK_CEILING_REACHED
    return (
        f"{reached}: {tsym} net {run.positions.net(tsym)} units, ceiling {ceiling} lots "
        f"({ceiling * contract.lot_size} units); no further order on it"
    )


def _recheck(run: MatrixRun, work: _Work) -> None:
    """Right before a place call: the state may have changed while the batch was placing."""
    case = work.case
    run.session.guard.check_new(case.segment)  # StopRun (STOP file) or a tripped breaker
    check_hours(run, case)
    current = expect.expectation(case, run.profile, run.issues)
    if current.verdict == expect.SKIP:
        raise Refusal(current.reason)
    if not work.item.closing:
        ceiling = ceiling_refusal(run, work.contract)
        if ceiling is not None:
            raise Refusal(ceiling)


def _round(run: MatrixRun, works: Sequence[_Work]) -> None:
    ready = [w for w in works if _prepare(run, w)]
    market = [w for w in ready if w.case.prctyp in MARKET_TYPES]
    others = [w for w in ready if w not in market]

    def place(task: _Work | None) -> None:
        if task is not None:
            _place_one(run, task)
        else:  # the market cases: one at a time, in order (an accepted one stops the others)
            for work in market:
                _place_one(run, work)

    tasks: list[_Work | None] = [*others, *([None] if market else [])]
    try:
        _in_pool(run, place, tasks)
    finally:
        if not run.session.interrupted:  # after Ctrl-C nothing more is sent
            _after_placing(run, ready)


def _after_placing(run: MatrixRun, ready: Sequence[_Work]) -> None:
    """Observe, run the hooks, cancel and confirm (always, whatever raised), then judge."""
    session = run.session
    stop = next((w.stop for w in ready if w.stop is not None), None)
    if stop is not None:
        _stopped_round(run, ready, stop)
        return
    placed = [w for w in ready if w.order_ids]
    try:
        _settle(run, placed)
        _hooks(run, placed)
    except KeyboardInterrupt:
        _interrupt(session)
        raise
    finally:
        if not session.interrupted:
            _cancel_and_confirm(run, placed)
    for work in ready:
        if work.result is None:
            _judge(run, work)


def _place_one(run: MatrixRun, work: _Work) -> None:
    session = run.session
    try:
        for attempt in (1, 2):
            try:
                _send(run, work)
                return
            except MaxOpenReached:
                if attempt == 2:
                    raise
                session.guard.wait_for_room(
                    1,
                    lambda: calls.open_ours(session),
                    timeout=run.room_timeout,
                    poll_interval=session.poll_interval,
                    sleep=session.sleep,
                    clock=session.clock,
                )
    except StopRun as stop:
        work.stop = stop
        if not work.order_ids:
            _skip(run, work, stop.reason)
    except MaxOpenReached as exc:
        _skip(run, work, f"MAX_OPEN: {exc}")
    except Refusal as exc:
        if not work.order_ids:
            _skip(run, work, exc.reason)
    except Exception as exc:  # the engine's or the family's code: never abort the batch
        _worker_error(run, work, exc)


def _worker_error(run: MatrixRun, work: _Work, exc: Exception) -> None:
    """Keep an exception with the case; a session that is not usable stops the run."""
    work.error = f"{type(exc).__name__}: {exc}"
    if isinstance(exc, contract_table.SESSION_ERRORS):
        run.session.guard.stop(f"the session is not usable ({work.error}); stopping the run")
    if work.outcomes:
        return  # judged with its outcomes (the batch cancels its order)
    if run.session.ledger.pending_place(work.case.id):
        # The place intent is written and its outcome is not: the call may have gone out.
        what = f"the place call of case {work.case.id} raised after its intent ({work.error})"
        run.session.guard.note_check_the_account(work.case.segment, what)
        _finish(run, work, CHECK, f"{CHECK_THE_ACCOUNT}: {what}", sent=True)
        return
    _finish(run, work, FAIL, f"raised before any call: {work.error}")  # no ledger verdict


def _send(run: MatrixRun, work: _Work) -> None:
    """Re-check, quote, price, pace, place: the quote is the last read before the place."""
    case, contract = work.case, work.contract
    assert contract is not None
    _recheck(run, work)
    market = case.prctyp in MARKET_TYPES
    if market:
        run._market_lock.acquire()
    try:
        if market:  # an accepted market order fills: the fill rules apply first
            if run.market_filled is not None:
                raise Refusal(run.market_filled)
            cutoff = fill_cutoff_reason(run.now(), uat=run.session.uat, mock=run.session.mock)
            if cutoff is not None:
                raise Refusal(cutoff)
        work.snapshot = pricing.fresh_quote(
            run.session.client,
            contract,
            uat=run.session.uat,
            sleep=run.session.sleep,
            pacer=run.session.uat_quote_pacer(),
        )
        builder = work.item.plan or default_plan
        work.plan = builder(case, contract, work.snapshot)
        holding = cnc_sell_refusal(run, case, work.plan, run.session.client.holdings)
        if holding is not None:
            raise Refusal(holding)
        if not market:
            run.pacer.wait()  # the pacing stamp: right before the place call
            outcome = calls.place(run.session, work.plan, work.ref)
            work.outcomes.append(outcome)
            _early_rms(run, work, outcome)
            return
        quantity = int(work.plan.get("quantity", contract.quantity(1)))
        sell = str(work.plan.get("transaction_type", case.side)).upper() == "SELL"
        signed = -quantity if sell else quantity
        run.positions.check_room(contract.tradingsymbol, signed, lot_size=contract.lot_size)
        _claim_market(run, work, signed)  # in the ledger before anything is sent
        _send_market(run, work)
    finally:
        if market:
            run._market_lock.release()


def _send_market(run: MatrixRun, work: _Work) -> None:
    """The claimed market place: released only when nothing went out or it was refused for
    certain; an accepted order or an unknown outcome keeps the claim."""
    assert work.plan is not None
    ledger = run.session.ledger
    before = _case_tags(ledger, work.case.id)
    try:
        run.pacer.wait()  # the pacing stamp: right before the place call
        outcome = calls.place(run.session, work.plan, work.ref)
    except BaseException:
        if _case_tags(ledger, work.case.id) == before:  # no place intent: nothing went out
            _release_market(run, work, "the place call raised before its intent")
        raise
    work.outcomes.append(outcome)
    _early_rms(run, work, outcome)
    if outcome_unsent(outcome):
        _release_market(run, work, f"not sent ({outcome.verdict})")
    elif outcome.verdict == calls.REJECTED:
        _release_market(run, work, "refused in the place reply")
    elif outcome.order_id is not None:
        _market_settle(run, work, outcome.order_id)


def _case_tags(ledger: Ledger, case_id: str) -> set[str]:
    """The place tags of ``case_id`` in the ledger (an intent adds one)."""
    return {tag for tag in ledger.tags() if ledger.case_of(tag) == case_id}


def _claim_market(run: MatrixRun, work: _Work, signed: int) -> None:
    """Take the run's single market path before the place call: the ledger line first (a
    resume restores it), then the pending fill in the position ledger (a full lot against the
    instrument's ceiling until the outcome is known)."""
    contract = work.contract
    assert contract is not None
    case = work.case
    run.session.ledger.note(
        MARKET_CLAIM,
        case_id=case.id,
        segment=case.segment,
        tradingsymbol=contract.tradingsymbol,
        quantity=signed,
    )
    run.market_filled = _market_reason(case.id)
    run.positions.add_fill(
        contract.tradingsymbol,
        signed,
        lot_size=contract.lot_size,
        case_id=case.id,
        order_id=None,
        market_claim=True,
    )
    work.fill_recorded = True  # the claim is the case's fill in the position ledger
    work.notes["market_claim"] = signed


def _correct_market_claim(run: MatrixRun, work: _Work, row: Order | None, filled: int) -> None:
    """A claimed market order the book shows final and not ``COMPLETE`` (cancelled after a
    part fill, say): the position ledger keeps what it really filled, not the claimed lot."""
    claimed = int(work.notes.get("market_claim", 0))
    status = status_of(row)
    if not claimed or not is_final(status) or status == "COMPLETE" or work.contract is None:
        return
    real = filled if claimed > 0 else -filled
    if real != claimed:
        run.positions.add_fill(
            work.contract.tradingsymbol,
            real - claimed,
            lot_size=work.contract.lot_size,
            case_id=work.case.id,
            order_id=work.outcomes[0].order_id if work.outcomes else None,
            market_correction=status,
        )


def _release_market(run: MatrixRun, work: _Work, why: str) -> None:
    """The claimed market order went nowhere (nothing sent, or refused for certain): the
    market path and the pending fill are released."""
    contract = work.contract
    assert contract is not None
    signed = int(work.notes.get("market_claim", 0))
    run.session.ledger.note(MARKET_RELEASE, case_id=work.case.id, reason=why)
    if signed:
        run.positions.add_fill(
            contract.tradingsymbol,
            -signed,
            lot_size=contract.lot_size,
            case_id=work.case.id,
            order_id=None,
            market_release=True,
        )
    work.fill_recorded = False
    work.notes["market_released"] = why
    run.market_filled = None


def _early_rms(run: MatrixRun, work: _Work, outcome: OrderOutcome) -> None:
    """An RMS refusal in the place reply: the family's later cases skip at once."""
    message = _message(outcome)
    if outcome.verdict == calls.REJECTED and expect.is_rms_limit(message):
        with run._lock:
            run.issues.rms_limit(work.case.family, message or "")


def _market_reason(case_id: str) -> str:
    return (
        f"a market order of this run (case {case_id}) was sent and counts as a fill (accepted, "
        "or its outcome is not known): the remaining market cases of this run are skipped"
    )


def _market_settle(run: MatrixRun, work: _Work, order_id: str) -> None:
    """An accepted MKT / SL-MKT: wait for its settled row; refused there: the claim is
    released, else it stays (a fill)."""
    rows = _await(run, [order_id], lambda row: _not_pending(row), run.settle_seconds)
    row = rows.get(order_id)
    work.settled[order_id] = row
    if status_of(row) == "REJECTED":
        _release_market(run, work, f"order {order_id} REJECTED in the book")


# -- observing ------------------------------------------------------------------------------------


def _not_pending(row: Order | None) -> bool:
    status = status_of(row)
    return status is not None and status != "PENDING"


def _await(
    run: MatrixRun,
    order_ids: Sequence[str],
    done: Callable[[Order | None], bool],
    seconds: float,
) -> dict[str, Order | None]:
    """Poll ``orders()`` (one read per round) until ``done(row)`` for every order or the time
    is up; the rows last seen (``None``: not listed)."""
    session = run.session
    rows: dict[str, Order | None] = dict.fromkeys(order_ids)
    if not order_ids:
        return rows
    deadline = session.clock() + seconds
    while True:
        book = read_book(session.client, attempts=1)
        if book is not None:
            for row in book:
                order_id = (row.order_id or "").strip()
                if order_id in rows:
                    rows[order_id] = row
                    if is_final(status_of(row)):
                        session.guard.closed(order_id)
        if all(done(rows[i]) for i in order_ids) or session.clock() >= deadline:
            return rows
        session.sleep(session.poll_interval)


def _settled(work: _Work) -> Callable[[Order | None], bool]:
    def done(row: Order | None) -> bool:
        if work.case.outcome == case_table.EXCHANGE_CANCEL and not work.case.canary:
            return is_final(status_of(row))
        return _not_pending(row)

    return done


def _settle(run: MatrixRun, works: Sequence[_Work]) -> None:
    waiting = {i: w for w in works for i in w.order_ids if i not in w.settled}
    if not waiting:
        return

    def done(row: Order | None) -> bool:
        work = waiting.get((row.order_id or "").strip()) if row is not None else None
        return work is not None and _settled(work)(row)

    rows = _await(run, list(waiting), done, run.settle_seconds)
    for order_id, row in rows.items():
        waiting[order_id].settled[order_id] = row


def _hooks(run: MatrixRun, works: Sequence[_Work]) -> None:
    hooked = []
    for work in works:
        hook = work.item.after_settle
        order_id = next(iter(work.order_ids), None)
        row = work.settled.get(order_id) if order_id else None
        if hook is None or order_id is None or status_of(row) not in WORKING:
            continue
        assert work.contract and work.snapshot and work.plan is not None
        work.placed = Placed(
            run, work.case, work.contract, work.snapshot, work.plan, work.outcomes[0], row
        )
        hooked.append(work)
    if not hooked:
        return
    session = run.session

    def call(work: _Work) -> None:
        assert work.placed is not None and work.item.after_settle is not None
        if session.interrupted:
            return  # Ctrl-C: no more calls
        try:
            work.item.after_settle(work.placed)
        except (StopRun, Refusal) as exc:
            work.notes["hook_refused"] = exc.reason
        except Exception as exc:
            work.notes["hook_error"] = f"{type(exc).__name__}: {exc}"
        finally:
            work.outcomes.extend(work.placed.calls)

    _in_pool(run, call, hooked)
    # One poll after the hooks: the cancel step acts on the rows as they are now.
    rows = _await(run, [i for w in hooked for i in w.order_ids], lambda row: True, 0.0)
    for work in hooked:
        for order_id in work.order_ids:
            work.latest[order_id] = rows.get(order_id)


def _cancel_and_confirm(run: MatrixRun, works: Sequence[_Work]) -> None:
    """Cancel every order of the batch not final (once), then confirm each becomes final.

    A cancel never sent (refused client-side) is not confirmed nor noted stuck: the order keeps
    its one CancelOrder for the teardown. After Ctrl-C nothing is sent.
    """
    session = run.session
    targets: list[tuple[_Work, str]] = []
    for work in works:
        for order_id in work.order_ids:
            row = work.latest.get(order_id, work.settled.get(order_id))
            if not is_final(status_of(row)):
                targets.append((work, order_id))

    def cancel(target: tuple[_Work, str]) -> tuple[_Work, str, bool]:
        work, order_id = target
        if session.interrupted:
            return work, order_id, False
        try:
            outcome = calls.cancel(session, order_id, work.ref)
        except Refusal as exc:  # flagged (unknown) or cancelled once already
            work.notes.setdefault("cancel_refused", []).append(exc.reason)
            return work, order_id, order_id not in session.ledger.flagged()
        except Exception as exc:  # the engine's code: the other cancels still go out
            work.error = work.error or f"{type(exc).__name__}: {exc}"
            work.notes.setdefault("cancel_error", []).append(f"{order_id}: {work.error}")
            return work, order_id, False
        work.outcomes.append(outcome)
        if outcome.verdict == NOT_SENT:
            work.notes.setdefault("cancel_not_sent", []).append(order_id)
            return work, order_id, False
        return work, order_id, not outcome.check_the_account

    sent = _in_pool(run, cancel, targets)
    if session.interrupted:
        return
    confirm = [(w, i) for w, i, ok in sent if ok]
    rows = _await(run, [i for _, i in confirm], lambda r: is_final(status_of(r)),
                  session.confirm_seconds)  # fmt: skip
    for work, order_id in confirm:
        row = rows.get(order_id)
        work.final[order_id] = row
        if not is_final(status_of(row)):
            _note_stuck(run, work, order_id)
    for work in works:
        for order_id in work.order_ids:
            work.final.setdefault(order_id, work.latest.get(order_id, work.settled.get(order_id)))


def _note_stuck(run: MatrixRun, work: _Work, order_id: str) -> None:
    session = run.session
    work.stuck.append(order_id)
    session.guard.note_stuck(work.case.segment, order_id)
    session.ledger.note("stuck", order_id=order_id, segment=work.case.segment)


def _stopped_round(run: MatrixRun, ready: Sequence[_Work], stop: StopRun) -> None:
    """The STOP file (or a stop) mid-batch: cancel ours once and confirm (``calls.stop_run``);
    then each case keeps what is known: an unknown outcome is CHECK THE ACCOUNT, a refusal its
    judged verdict, an order not final after the cancel is stuck, a confirmed final one skips
    "<reason>; cancelled"; a case that sent nothing skips with the reason."""
    session = run.session
    reason = session.guard.stop_reason or stop.reason
    report = calls.stop_run(session, StopRun(reason, stop.foreign))
    book = read_book(session.client, attempts=1) or []
    rows = {(o.order_id or "").strip(): o for o in book}
    for work in ready:
        if work.result is not None:
            continue
        if not work.outcomes:
            _skip(run, work, reason)
            continue
        for order_id in work.order_ids:
            row = rows.get(order_id)
            work.final[order_id] = row
            work.settled.setdefault(order_id, row)
        work.notes["stop"] = {"cancelled": report.cancelled, "not_final": report.not_final}
        _judge_stopped(run, work, reason, report)


def _judge_stopped(run: MatrixRun, work: _Work, reason: str, report: calls.CancelReport) -> None:
    place = work.outcomes[0]
    exp = work.expectation
    assert exp is not None
    if place.verdict in (CHECK, INTERRUPTED):
        _finish(run, work, CHECK, f"{CHECK_THE_ACCOUNT}: the place outcome is unknown", sent=True)
        return
    if _unknown_later(run, work):
        return
    if outcome_unsent(place):
        _judge_not_sent(run, work, exp, place)
        return
    row = work.final.get(place.order_id) if place.order_id else None
    if place.verdict == calls.REJECTED or status_of(row) == "REJECTED":
        message = _message(place) if place.verdict == calls.REJECTED else None
        _judge_rejection(run, work, exp, message or (row.reject_reason if row else None) or "")
        return
    ids = work.order_ids
    finals = [work.final.get(i) for i in ids]
    filled = sum((r.filled_quantity or 0) for r in finals if r is not None)
    stuck = [i for i in ids if i in report.not_final or i in report.stuck]
    if (stuck or work.error is not None) and filled and work.contract is not None:
        _add_fill(run, work, filled, place.order_id)  # a real fill is never lost
    if stuck:
        work.stuck.extend(i for i in stuck if i not in work.stuck)
        _finish(run, work, STUCK, f"{reason}; order {stuck[0]} stuck: not final after its "
                f"cancel; the {work.case.segment} circuit breaker tripped", sent=True)  # fmt: skip
        return
    flagged = [i for i in ids if i in report.flagged]
    if flagged:
        _finish(run, work, CHECK, f"{reason}; {CHECK_THE_ACCOUNT}: order {flagged[0]} has an "
                "unknown outcome", sent=True)  # fmt: skip
        return
    if work.error is not None:
        _finish(run, work, FAIL, f"{reason}; it raised after its place call: {work.error}",
                sent=True)  # fmt: skip
        return
    if not all(is_final(status_of(r)) for r in finals):
        _finish(run, work, CHECK, f"{reason}; {CHECK_THE_ACCOUNT}: not confirmed final after "
                "the stop", sent=True)  # fmt: skip
        return
    if filled and work.contract is not None:
        _add_fill(run, work, filled, place.order_id)
        _finish(run, work, SKIP, f"{reason}; it filled {filled} before the stop (kept in the "
                "run's position ledger)", sent=True)  # fmt: skip
        return
    _finish(run, work, SKIP, f"{reason}; cancelled", sent=True)


# -- judging --------------------------------------------------------------------------------------


def _message(outcome: OrderOutcome) -> str | None:
    error = outcome.error or {}
    message = error.get("emsg") or error.get("message")
    return None if message is None else str(message)


def outcome_unsent(outcome: OrderOutcome) -> bool:
    """Whether a place outcome means nothing went out (``ledger.nothing_sent``: no order id,
    not unknown, refused client-side or by the SDK's own rate limit with no server answer)."""
    return nothing_sent(
        outcome.verdict,
        outcome.error,
        kind=outcome.kind,
        order_id=outcome.order_id,
        order_ids=outcome.order_ids,
    )


def _judge_not_sent(run: MatrixRun, work: _Work, exp: Expectation, outcome: OrderOutcome) -> None:
    """A place call refused before sending (``nothing_sent``). The SDK's own rate limit is not
    a finding: the case skips with no ledger verdict, so a resume places it. Client-side
    validation is the SDK's answer: never counted for the breaker (no broker answer)."""
    message = _message(outcome)
    if outcome.verdict != NOT_SENT:  # the client-side rate limit
        _finish(run, work, SKIP, f"not sent: the SDK's client-side rate limit ({message}); a "
                "resume places it")  # fmt: skip
        return
    run.session.guard.note_rejection(work.case.segment, expected=True)
    if exp.verdict == expect.ACCEPT:
        _finish(run, work, FAIL, f"the SDK refused it, nothing sent: {message}", sent=True)
    else:
        _finish(run, work, RECORD, f"not sent (refused client-side): {message}", sent=True)


def _judge(run: MatrixRun, work: _Work) -> None:
    case = work.case
    exp = work.expectation
    assert exp is not None
    guard = run.session.guard
    place = work.outcomes[0]
    order_id = place.order_id
    settled = work.settled.get(order_id) if order_id else None
    final = work.final.get(order_id) if order_id else None
    status = status_of(settled)
    issue = exp.issue
    if place.verdict in (CHECK, INTERRUPTED):
        _finish(run, work, CHECK, f"{CHECK_THE_ACCOUNT}: the place outcome is unknown", sent=True)
        return
    last_row = final if final is not None else settled
    filled = (last_row.filled_quantity or 0) if last_row is not None else 0
    if _unknown_later(run, work):
        if filled and work.contract is not None:
            _add_fill(run, work, filled, order_id)  # a real fill is never lost
        return
    if work.error is not None and not work.stuck:  # a stuck order is reported as STUCK
        if filled and work.contract is not None:
            _add_fill(run, work, filled, order_id)  # a real fill is never lost
        _finish(run, work, FAIL, f"raised after its place call: {work.error}", sent=True)
        return
    if outcome_unsent(place):
        _judge_not_sent(run, work, exp, place)
        return
    rejected = place.verdict == calls.REJECTED or status == "REJECTED"
    if rejected:
        message = _message(place) if place.verdict == calls.REJECTED else None
        if message is None and settled is not None:
            message = settled.reject_reason or ""
        _judge_rejection(run, work, exp, message or "")
        return
    guard.note_rejection(case.segment, expected=True)
    if case.canary:
        _judge_canary(run, work, settled)
        return
    if work.stuck:
        if filled and work.contract is not None:
            _add_fill(run, work, filled, order_id)  # a real fill is never lost
        _finish(
            run, work, STUCK,
            f"order {work.stuck[0]} stuck: not final after its cancel; the {case.segment} "
            "circuit breaker tripped", sent=True,
        )  # fmt: skip
        return
    if filled and work.contract is not None and case.prctyp not in MARKET_TYPES:
        _add_fill(run, work, filled, order_id)
    if "hook_error" in work.notes:
        _finish(run, work, FAIL, f"the family hook raised: {work.notes['hook_error']}", sent=True)
        return
    if "hook_refused" in work.notes:
        _finish(run, work, SKIP, f"the family hook refused: {work.notes['hook_refused']}",
                sent=True)  # fmt: skip
        return
    if case.prctyp in MARKET_TYPES:
        if filled and work.contract is not None:
            _add_fill(run, work, filled, order_id)  # no-op: the market claim counted it
        _correct_market_claim(run, work, last_row, filled)
        work.notes["filled"] = filled or 0
        reason = (
            f"an accepted {case.prctyp} order ({status}) counted as a fill; the remaining "
            "market cases of this run skip"
        )
        _finish(run, work, MARKET_FILL, reason, issue="F27", sent=True)
        return
    uat_fill = bool(filled) and note_uat_fill(run, work, filled)
    if exp.verdict == expect.ACCEPT:
        want = _SETTLED.get(case.outcome)
        if uat_fill:
            _finish(run, work, RECORD, uat_fill_reason(filled), issue=issue, sent=True)
        elif filled:
            _finish(run, work, FAIL, f"an order away from the market filled ({filled})", sent=True)
        elif want is None or status == want:
            _finish(run, work, PASS, f"{status}, as documented ({exp.reason})", issue=issue,
                    sent=True)  # fmt: skip
        elif note_uat_stop_open(run, work, want, status):
            _finish(run, work, RECORD, uat_stop_open_reason(status), issue=issue, sent=True)
        else:
            _finish(run, work, FAIL, f"expected {want}, saw {status}", issue=issue, sent=True)
        return
    if exp.verdict == expect.REJECT:
        reason = f"accepted ({status}) although a refusal was expected: {exp.reason}"
        _finish(run, work, RECORD, reason, issue=issue, sent=True)
        return
    _finish(run, work, RECORD, f"recorded: {status}", issue=issue, sent=True)


UAT_FILLED_FAR = "uat_filled_far"
UAT_STOP_OPEN = "uat_stop_open"
UAT_BAND_REFUSAL = "uat_band_refusal"


def uat_fill_note(
    case: Case,
    contract: Contract | None,
    plan: dict[str, Any] | None,
    snapshot: PriceSnapshot | None,
    filled: int,
) -> dict[str, Any]:
    """What UAT filled away from the market: the order, the last price and the gap (%)."""
    plan = plan or {}
    price = plan.get("price")
    last = snapshot.last_price if snapshot is not None else None
    gap = None
    if price is not None and last:
        gap = ((Decimal(str(price)) - last) / last * 100).quantize(Decimal("0.01"))
    return {
        "exchange": plan.get("exchange") or (contract.exchange if contract else case.exchange),
        "segment": case.segment,
        "tradingsymbol": contract.tradingsymbol if contract is not None else None,
        "side": str(plan.get("transaction_type", case.side)).upper(),
        "product": plan.get("product", case.product),
        "order_type": plan.get("order_type", case.prctyp),
        "price": None if price is None else str(price),
        "trigger_price": None if plan.get("trigger_price") is None else str(plan["trigger_price"]),
        "last_price": None if last is None else str(last),
        "gap_pct": None if gap is None else str(gap),
        "filled": filled,
    }


def uat_fill_reason(filled: int) -> str:
    return (
        f"recorded: UAT filled an order away from the market ({filled}; {UAT_FILLED_FAR}): UAT "
        "fills at any price; the fill is in the run's position ledger"
    )


def note_uat_fill(run: MatrixRun, work: _Work, filled: int) -> bool:
    """On UAT: note an order away from the market that filled (the fill itself is already in
    the position ledger, ``_add_fill``, once); ``False`` elsewhere (a failure there)."""
    if not run.session.uat or work.case.prctyp in MARKET_TYPES:
        return False
    work.notes[UAT_FILLED_FAR] = uat_fill_note(
        work.case, work.contract, work.plan, work.snapshot, filled
    )
    return True


def uat_stop_open(uat: bool, case: Case, want: str | None, status: str | None) -> bool:
    """On UAT a stop-limit order seen ``OPEN`` (not filled) where ``TRIGGER_PENDING`` was
    expected is recorded, on every segment."""
    return uat and case.prctyp == "SL-LMT" and want == "TRIGGER_PENDING" and status == "OPEN"


def uat_stop_open_reason(status: str | None) -> str:
    return f"recorded: the stop order rests {status} on UAT, not TRIGGER_PENDING ({UAT_STOP_OPEN})"


def note_uat_stop_open(run: MatrixRun, work: _Work, want: str | None, status: str | None) -> bool:
    if not uat_stop_open(run.session.uat, work.case, want, status):
        return False
    work.notes[UAT_STOP_OPEN] = {"status": status, "expected": want, "segment": work.case.segment}
    return True


def _unknown_later(run: MatrixRun, work: _Work) -> bool:
    """A later call of the case (modify, cancel, a closing order) with an unknown outcome:
    the case is CHECK THE ACCOUNT, whatever the rest showed (``True`` when finished so)."""
    unknown = next((o for o in work.outcomes[1:] if o.verdict in (CHECK, INTERRUPTED)), None)
    if unknown is None:
        return False
    _finish(
        run, work, CHECK,
        f"{CHECK_THE_ACCOUNT}: the {unknown.kind} of order {unknown.order_id or unknown.tag} "
        "has an unknown outcome; no further call is made for it", sent=True,
    )  # fmt: skip
    return True


def _judge_rejection(run: MatrixRun, work: _Work, exp: Expectation, message: str) -> None:
    case, guard = work.case, run.session.guard
    kind = expect.environment_refusal(message)
    if kind is not None:
        # An exchange LPP band or the per-minute order count: environment, neutral to the breaker
        # (no ``note_rejection``: not added, not reset), the family goes on.
        _finish(run, work, ENV_LIMIT, f"environment limit ({kind}): {message}", sent=True)
        return
    if expect.is_rms_limit(message):
        with run._lock:
            run.issues.rms_limit(case.family, message)
        guard.note_rejection(case.segment, expected=True)
        _finish(run, work, ENV_LIMIT, f"environment limit (RMS): {message}", issue="RMS",
                sent=True)  # fmt: skip
        return
    if (
        run.session.uat
        and expect.is_uat_invalid_symbol(message)
        and exp.judge_rejection(message) != expect.EXPECTED
    ):
        # UAT lacks some production contracts (SENSEX weekly options): environment data, a skip
        # of this case only, never counted for the breaker.
        _finish(run, work, SKIP, f"environment data (UAT does not know the symbol): {message}",
                issue=exp.issue, sent=True)  # fmt: skip
        return
    if exp.verdict == expect.RECORD:
        guard.note_rejection(case.segment, expected=True)
        _finish(run, work, RECORD, f"recorded refusal: {message}", issue=exp.issue, sent=True)
        return
    judged = exp.judge_rejection(message)
    if judged == expect.UNEXPECTED and run.session.uat and expect.is_uat_band_refusal(message):
        # UAT's own band does not match its quote: recorded, never counted for the breaker.
        work.notes[UAT_BAND_REFUSAL] = {"message": message, **expect.band_of(message)}
        _finish(run, work, RECORD, f"recorded UAT band refusal ({UAT_BAND_REFUSAL}): {message}",
                issue=exp.issue, sent=True)  # fmt: skip
        return
    guard.note_rejection(case.segment, expected=judged != expect.UNEXPECTED)
    if judged == expect.EXPECTED:
        _finish(run, work, PASS, f"refused as expected: {message}", issue=exp.issue, sent=True)
    elif judged == expect.RECORD:
        _finish(run, work, RECORD, f"refused with another message: {message}", issue=exp.issue,
                sent=True)  # fmt: skip
    else:
        _finish(run, work, FAIL, f"unexpected refusal ({exp.reason}): {message}", sent=True)


def _judge_canary(run: MatrixRun, work: _Work, settled: Order | None) -> None:
    case = work.case
    status = status_of(settled)
    order_id = work.order_ids[0] if work.order_ids else "?"
    with run._lock:
        if status in WORKING and not work.stuck:
            run.issues.canary_ok(case.segment)
            verdict, reason = PASS, f"the {case.segment.upper()} canary rested {status}"
        else:
            run.issues.canary_stuck(case.segment, order_id)
            verdict = STUCK
            reason = (
                f"F26: the {case.segment.upper()} canary order {order_id} stayed {status}; "
                f"{case.segment.upper()} is skipped for the run"
            )
    _finish(run, work, verdict, reason, issue="F26", sent=True)


def _add_fill(run: MatrixRun, work: _Work, filled: int, order_id: str | None) -> None:
    """The case's fill in the run's position ledger, once per case."""
    if work.fill_recorded:
        return
    work.fill_recorded = True
    contract = work.contract
    assert contract is not None
    plan = work.plan or {}
    sign = -1 if str(plan.get("transaction_type", work.case.side)).upper() == "SELL" else 1
    run.positions.add_fill(
        contract.tradingsymbol,
        sign * filled,
        lot_size=contract.lot_size,
        case_id=work.case.id,
        order_id=order_id,
        product=str(plan.get("product", work.case.product)).upper(),
    )


# -- fills ----------------------------------------------------------------------------------------


def _fill_phase(run: MatrixRun, works: Sequence[_Work]) -> None:
    """Fill cases: one at a time per instrument (instruments in parallel), the open-order
    invariant checked before each case; an exception keeps the case and cancels its order."""
    ready = [w for w in works if _prepare(run, w)]
    groups: dict[str, list[_Work]] = {}
    for work in ready:
        assert work.contract is not None
        groups.setdefault(work.contract.tradingsymbol, []).append(work)
    session = run.session

    def run_group(group: list[_Work]) -> None:
        for work in group:
            assert work.contract is not None
            if session.interrupted:
                return
            with run.positions.one_at_a_time(work.contract.tradingsymbol):
                if not _before_round(run):
                    _skip(run, work, session.guard.stop_reason or STOP_REASON)
                    continue
                try:
                    _fill_case(run, work)
                except Exception as exc:  # the engine's code: never leave the order behind
                    _fill_error(run, work, exc)
                finally:
                    _persist_trips(run)

    _in_pool(run, run_group, list(groups.values()))


def _fill_error(run: MatrixRun, work: _Work, exc: Exception) -> None:
    work.error = f"{type(exc).__name__}: {exc}"
    if isinstance(exc, contract_table.SESSION_ERRORS):
        run.session.guard.stop(f"the session is not usable ({work.error}); stopping the run")
    if work.order_ids and not run.session.interrupted:
        _cancel_and_confirm(run, [work])
    if work.result is not None:
        return
    if work.outcomes and work.outcomes[0].verdict in (CHECK, INTERRUPTED):
        _finish(run, work, CHECK, f"{CHECK_THE_ACCOUNT}: the place outcome is unknown", sent=True)
    elif not _unknown_later(run, work):
        _finish(run, work, FAIL, f"raised: {work.error}", sent=bool(work.outcomes))


def _fill_leg(
    run: MatrixRun, work: _Work, side: str, quantity: int
) -> tuple[OrderOutcome, Order | None, int]:
    """One order at the last price: placed, waited for, cancelled when not filled (once)."""
    session, contract = run.session, work.contract
    assert contract is not None
    snapshot = pricing.fresh_quote(
        session.client,
        contract,
        uat=session.uat,
        sleep=session.sleep,
        pacer=session.uat_quote_pacer(),
    )  # the last read before the place
    plan = default_plan(work.case, contract, snapshot, side=side)
    plan["quantity"] = quantity
    holding = cnc_sell_refusal(run, work.case, plan, session.client.holdings)
    if holding is not None:
        raise Refusal(holding)
    if work.plan is None:
        work.snapshot, work.plan = snapshot, plan
    run.pacer.wait()  # the pacing stamp: right before the place call
    outcome = calls.place(session, plan, work.ref)
    work.outcomes.append(outcome)
    order_id = outcome.order_id
    if order_id is None:
        return outcome, None, 0
    rows = _await(run, [order_id], lambda r: is_final(status_of(r)), run.fill_wait_seconds)
    row = rows[order_id]
    work.settled[order_id] = row
    open_left = not is_final(status_of(row)) and order_id not in session.ledger.flagged()
    if open_left and not session.interrupted:  # after Ctrl-C: no further call
        confirm = True
        try:
            cancel = calls.cancel(session, order_id, work.ref)
            work.outcomes.append(cancel)
            confirm = cancel.verdict != NOT_SENT and not cancel.check_the_account
            if cancel.verdict == NOT_SENT:
                work.notes.setdefault("cancel_not_sent", []).append(order_id)
        except Refusal as exc:
            work.notes.setdefault("cancel_refused", []).append(exc.reason)
        if confirm:
            rows = _await(
                run, [order_id], lambda r: is_final(status_of(r)), session.confirm_seconds
            )
            row = rows[order_id]
            if not is_final(status_of(row)):
                _note_stuck(run, work, order_id)
    work.final[order_id] = row
    filled = (row.filled_quantity or 0) if row is not None else 0
    if status_of(row) == "COMPLETE" and not filled:
        filled = quantity
    return outcome, row, filled


def _left_open(run: MatrixRun, work: _Work, leg: str, row: Order | None) -> None:
    """A fill leg not final whose cancel was not sent (refused client-side, or Ctrl-C): it may
    still be open: CHECK THE ACCOUNT (the teardown keeps its one cancel)."""
    order_id = (row.order_id if row is not None else None) or work.order_ids[-1]
    reason = (
        f"{CHECK_THE_ACCOUNT}: the {leg} order {order_id} is {status_of(row)} and its cancel "
        "was not sent; it may still be open"
    )
    _finish(run, work, CHECK, reason, sent=True)


def _stuck_fill(run: MatrixRun, work: _Work, leg: str, leftover: int) -> None:
    order_id = work.stuck[0]
    kept = f"; the position ({leftover}) stays in the run's position ledger" if leftover else ""
    reason = (
        f"the {leg} order {order_id} is stuck: not final after its cancel; the "
        f"{work.case.segment} circuit breaker tripped{kept}"
    )
    _finish(run, work, STUCK, reason, sent=True)


def _fill_case(run: MatrixRun, work: _Work) -> None:
    case, contract, exp = work.case, work.contract, work.expectation
    assert contract is not None and exp is not None
    session = run.session
    opening, closing = case.side, ("SELL" if case.side == "BUY" else "BUY")
    quantity = contract.quantity(1)
    signed = quantity if opening == "BUY" else -quantity
    tsym = contract.tradingsymbol
    try:
        cutoff = fill_cutoff_reason(run.now(), uat=run.session.uat, mock=run.session.mock)
        if cutoff is not None:
            raise Refusal(cutoff)
        run.positions.check_room(tsym, signed, lot_size=contract.lot_size)
        _recheck(run, work)
        outcome, row, filled = _fill_leg(run, work, opening, quantity)
    except (Refusal, StopRun) as exc:
        if work.outcomes:
            _finish(run, work, SKIP, exc.reason, sent=True)
        else:
            _skip(run, work, exc.reason)
        return
    if outcome.verdict in (CHECK, INTERRUPTED):
        _finish(run, work, CHECK, f"{CHECK_THE_ACCOUNT}: the {opening} outcome is unknown",
                sent=True)  # fmt: skip
        return
    if outcome_unsent(outcome):
        _judge_not_sent(run, work, exp, outcome)
        return
    if outcome.verdict == calls.REJECTED or status_of(row) == "REJECTED":
        message = _message(outcome) or (row.reject_reason if row is not None else None) or ""
        _judge_rejection(run, work, exp, message)
        return
    session.guard.note_rejection(case.segment, expected=True)
    if filled:
        run.positions.add_fill(
            tsym, filled if opening == "BUY" else -filled, lot_size=contract.lot_size,
            case_id=case.id, order_id=outcome.order_id, product=case.product,
        )  # fmt: skip
    if work.stuck:
        _stuck_fill(run, work, opening, filled)
        return
    if _unknown_later(run, work):
        return
    if not is_final(status_of(row)):
        _left_open(run, work, opening, row)
        return
    if not filled:
        _finish(run, work, SKIP,
                f"the {opening} at the last price did not fill ({status_of(row)}); cancelled",
                sent=True)  # fmt: skip
        return
    _close(run, work, closing, filled)


def _close(run: MatrixRun, work: _Work, closing: str, filled: int) -> None:
    """The closing order of a fill case and the case's verdict (the position stays in the
    position ledger whenever the close does not fill)."""
    case, contract, exp = work.case, work.contract, work.expectation
    assert contract is not None and exp is not None
    guard, tsym = run.session.guard, contract.tradingsymbol
    kept = f"the position ({filled}) stays in the run's position ledger"
    try:
        close, close_row, closed = _fill_leg(run, work, closing, filled)
    except (Refusal, StopRun) as exc:
        run.positions.close_attempt(tsym, 0, case_id=case.id, order_id=None, not_sent=exc.reason)
        _finish(run, work, SKIP, f"close not sent: {exc.reason}; {kept}", sent=True)
        return
    run.positions.close_attempt(
        tsym, closed, case_id=case.id, order_id=close.order_id, product=case.product
    )
    work.notes.update(filled=filled, closed=closed, leftover=run.positions.net(tsym))
    if close.verdict in (CHECK, INTERRUPTED) or _unknown_later(run, work):
        if work.result is None:
            _finish(run, work, CHECK, f"{CHECK_THE_ACCOUNT}: the closing {closing} outcome is "
                    "unknown", sent=True)  # fmt: skip
        return
    message = _message(close)
    if outcome_unsent(close):
        _finish(run, work, SKIP, f"close not sent: {message}; {kept}", sent=True)
        return
    status = status_of(close_row)
    if close.verdict == calls.REJECTED or status == "REJECTED":
        message = message or (close_row.reject_reason if close_row is not None else None) or ""
        kind = expect.environment_refusal(message)
        if kind is not None:  # neutral to the breaker (no note_rejection)
            _finish(run, work, ENV_LIMIT, f"environment limit ({kind}) on the closing "
                    f"{closing}: {message}; {kept}", sent=True)  # fmt: skip
            return
        if expect.is_rms_limit(message):
            with run._lock:
                run.issues.rms_limit(case.family, message)
            guard.note_rejection(case.segment, expected=True)
            _finish(run, work, ENV_LIMIT, f"environment limit (RMS) on the closing {closing}: "
                    f"{message}; {kept}", issue="RMS", sent=True)  # fmt: skip
            return
        record = case.outcome == case_table.SHORT_COVER  # undocumented: recorded
        guard.note_rejection(case.segment, expected=record)
        _finish(run, work, RECORD if record else FAIL,
                f"the closing {closing} was refused: {message}; {kept}", sent=True)  # fmt: skip
        return
    guard.note_rejection(case.segment, expected=True)
    if work.stuck:
        _stuck_fill(run, work, f"closing {closing}", filled - closed)
        return
    if not is_final(status):
        _left_open(run, work, f"closing {closing}", close_row)
        return
    if case.outcome == case_table.SHORT_COVER:
        _finish(run, work, RECORD, f"short {case.side} filled {filled}; the covering {closing} "
                f"filled {closed} ({status})", issue=exp.issue, sent=True)  # fmt: skip
        return
    if closed >= filled:
        _finish(run, work, XPASS, f"the closing {closing} filled ({filled}): closing-sell did "
                "not show", issue="closing-sell", sent=True)  # fmt: skip
        return
    close_exp = exp.close
    _finish(
        run, work, XFAIL,
        f"the closing {closing} did not fill ({status}); the position ({filled - closed}) "
        "stays in the run's position ledger"
        + (f" ({close_exp.reason})" if close_exp is not None else ""),
        issue="closing-sell", sent=True,
    )  # fmt: skip


# -- finishing a case -----------------------------------------------------------------------------


def _skip(run: MatrixRun, work: _Work, reason: str, *, issue: str | None = None) -> None:
    _finish(run, work, SKIP, reason, issue=issue, sent=bool(work.outcomes))


def _row_data(row: Order | None) -> dict[str, Any] | None:
    return None if row is None else dict(row.raw) or row.model_dump(mode="json", by_alias=True)


def _finish(
    run: MatrixRun,
    work: _Work,
    verdict: str,
    reason: str,
    *,
    issue: str | None = None,
    sent: bool = False,
) -> None:
    """The case's result: one record line, and a ledger verdict when anything was sent."""
    if work.result is not None:
        return
    case = work.case
    orders: dict[str, Any] = {}
    for order_id in work.order_ids:
        final = work.final.get(order_id) or work.settled.get(order_id)
        entry: dict[str, Any] = {
            "settled": _row_data(work.settled.get(order_id)),
            "final": _row_data(work.final.get(order_id)),
        }
        if run.watcher is not None:
            updates = run.watcher.updates(order_id)
            entry["stream"] = [dict(u.raw) for u in updates]
            if final is not None and updates:
                entry["views"] = cross_check(final, updates=updates)
        orders[order_id] = entry
    contract = work.contract
    snapshot = work.snapshot
    exp = work.expectation
    notes = dict(work.notes)
    if work.placed is not None:
        notes.update(work.placed.notes)
    if snapshot is not None and snapshot.lot_mismatch is not None:  # UAT: the master lot used
        notes.setdefault("lot_size_mismatch", snapshot.lot_mismatch)
    data: dict[str, Any] = {
        "case_id": case.id,
        "family": case.family,
        "role": case.role,
        "segment": case.segment,
        "tier": case.tier,
        "canary": case.canary,
        "contract": None
        if contract is None
        else {
            k: contract.to_dict()[k]
            for k in ("exchange", "tradingsymbol", "token", "lot_size", "tick_size")
        },  # fmt: skip
        "snapshot": None if snapshot is None else dataclasses.asdict(snapshot),
        "plan": work.plan,
        "expectation": None
        if exp is None
        else {"verdict": exp.verdict, "reason": exp.reason, "issue": exp.issue, "words": exp.words},
        "calls": [dataclasses.asdict(o) for o in work.outcomes],
        "orders": orders,
        "notes": notes,
        "verdict": verdict,
        "reason": reason,
        "issue": issue,
    }
    written = run.recorder.write(data)
    if sent:
        run.session.ledger.verdict(case.id, verdict, reason=reason)
    order_ids = work.order_ids
    last = order_ids[0] if order_ids else None
    row = (work.final.get(last) or work.settled.get(last)) if last else None
    work.result = CaseResult(
        case_id=case.id,
        verdict=verdict,
        reason=reason,
        issue=issue,
        order_ids=order_ids,
        status=status_of(row),
        canary=case.canary,
        record=written,
    )


# -- pytest ---------------------------------------------------------------------------------------


def report(result: CaseResult) -> None:
    """The pytest outcome of a case: passing verdicts return; a skip or an environment limit
    skips; the closing-sell known issue xfails (non-strict); a stuck canary skips (F26);
    anything else fails (an xfail of any other issue too)."""
    if result.verdict in PASSING:
        return None
    if result.verdict in (SKIP, ENV_LIMIT) or (result.verdict == STUCK and result.canary):
        pytest.skip(result.reason)
    if result.verdict == XFAIL and result.issue == "closing-sell":
        pytest.xfail(result.reason)
    pytest.fail(f"{result.verdict}: {result.reason}")
