"""The UAT matrix family M13: GTT orders (inventory §3.2 row M13, marker ``m_gtt``; UAT only).

Every GTT write goes through the never-resend wrappers ``calls.gtt_place`` / ``gtt_modify`` /
``gtt_cancel`` (ledger intent before, outcome after; never repeated; an unknown outcome is looked
up by the GTT's tag, else CHECK THE ACCOUNT and the segment's breaker trips); the order a GTT of
ours placed is cancelled through ``calls.cancel`` once the ledger adopted it by its
``add_ord_id`` (= our alert id; its tag blank or the GTT's, its instrument and side the GTT's).
On UAT (``session.uat``) writes run at any hour (owner, 2026-11-13); a session that is not UAT
writes inside market hours only and never while ``TRADESMART_LIVE_ORDER_OUTSIDE_HOURS`` is set
(``write_refusal``). In a mock session (``session.mock``) nothing at all: every case skips
(``mock_session.NO_AMO_GTT``) and ``calls`` refuses every GTT write. Before each case: not done in
this run (resume), the run not
stopped, the open-order invariant (``check_open_orders``; a foreign open order stops the run),
the segment not tripped, the case's declared count (one GTT place).

- ``FAR`` (the GTT design spec §5, smoke): NSE ``ITC-EQ`` (role ``nse-b``), ``LTP_A_O`` with the
  trigger at 3 x a fresh last price (refused when the condition would already be true), its
  order a CNC LIMIT BUY of 1 share far below the market (``pricing.far_buy``), alert validity
  ``DAY``. Read back from ``pending_gtts()`` by its tag (type, trigger, validity, quantity,
  price), the trigger modified once to 3.1 x a fresh last price, read back, cancelled once,
  confirmed gone. A sign that it fired (``triggered_gtts()`` by tag / alert id, or an order of
  ours by ``add_ord_id``; a triggered row whose ``stat`` is ``CANCELLED`` in any letter case is
  recorded, not a sign: UAT lists cancelled GTTs there, spec F32) or a GTT still pending is
  CHECK THE ACCOUNT (the fired order is adopted and cancelled once).
- ``FIRES`` (full, UAT only, owner O2): ``LTP_B_O`` with the trigger 5 % **above** the last price,
  so the condition is already true and the GTT fires at once, placing its far CNC LIMIT BUY of 1
  share. Its order is found in ``orders()`` by ``add_ord_id`` within ``TRIGGER_WAIT_SECONDS``,
  adopted, cancelled once and confirmed final (still open: stuck, the breaker trips). Not
  found in time: CHECK THE ACCOUNT (a GTT still pending is cancelled once first).
- ``read_alert_types`` / ``read_triggered``: ``gtt_alert_types()`` and ``triggered_gtts()``,
  read-only (smoke).

Each case writes one record (``records.jsonl``) and, when it sent anything, its ledger verdict.
"""

from __future__ import annotations

import dataclasses
import os
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from typing import Any

from tests.live_orders import gtt, mock_session
from tests.live_orders.safety import (
    CHECK_THE_ACCOUNT,
    OUTSIDE_HOURS_ENV,
    Refusal,
    ceil_to_tick,
    is_final,
    status_of,
)
from tests.live_orders.uat_matrix import batch, calls, family, pricing
from tests.live_orders.uat_matrix.batch import (
    CHECK,
    FAIL,
    PASS,
    RECORD,
    SKIP,
    STUCK,
    CaseResult,
    MatrixRun,
)
from tests.live_orders.uat_matrix.calls import ACKNOWLEDGED, RESOLVED, CaseRef, GttOutcome
from tests.live_orders.uat_matrix.cases import FULL, REST, SMOKE, Case
from tests.live_orders.uat_matrix.contracts import Contract
from tests.live_orders.uat_matrix.guard import MaxOpenReached, StopRun
from tests.live_orders.uat_matrix.ledger import NOT_SENT, REJECTED, check_open_orders, read_book
from tests.live_orders.uat_matrix.pricing import PriceSnapshot
from tradesmart.enums import GttAlertType, GttValidity, OrderType, TransactionType, Validity
from tradesmart.models.gtt import Gtt
from tradesmart.models.orders import Order

FAMILY = "gtt"
ROLE = "nse-b"  # NSE ITC-EQ
FIRE_FACTOR = Decimal("1.05")  # LTP_B_O 5 % above the last price: already true, fires at once
TRIGGER_WAIT_SECONDS = 30.0  # how long the order of a GTT that fires at once is looked for
ALERT_TYPES_ID = "gtt-alert-types"
TRIGGERED_ID = "gtt-triggered"


def _case(suffix: str, tier: str) -> Case:
    return Case(
        id=f"{FAMILY}-{ROLE}-cnc-lmt-day-buy{suffix}",
        family=FAMILY,
        role=ROLE,
        product="CNC",
        prctyp="LMT",
        ret="DAY",
        side="BUY",
        orders=1,  # one GTT place call (its modify and cancel are not places)
        outcome=REST,
        tier=tier,
    )


FAR = _case("", SMOKE)
FIRES = _case("-fires", FULL)
GTT_CASES: list[Case] = [FAR, FIRES]


@dataclasses.dataclass
class _Work:
    """One GTT case while it runs: what it sent and saw."""

    run: MatrixRun
    case: Case
    calls: list[Any] = dataclasses.field(default_factory=list)
    notes: dict[str, Any] = dataclasses.field(default_factory=dict)
    order_ids: list[str] = dataclasses.field(default_factory=list)
    status: str | None = None

    @property
    def ref(self) -> CaseRef:
        return CaseRef(self.case.id, self.case.segment)

    @property
    def sent(self) -> bool:
        return any(getattr(c, "sent", False) for c in self.calls)


def _finish(work: _Work, verdict: str, reason: str) -> CaseResult:
    """One record line; the ledger verdict when anything was sent."""
    run, case = work.run, work.case
    data: dict[str, Any] = {
        "case_id": case.id,
        "family": case.family,
        "role": case.role,
        "segment": case.segment,
        "tier": case.tier,
        "canary": False,
        "calls": [dataclasses.asdict(c) for c in work.calls],
        "notes": work.notes,
        "verdict": verdict,
        "reason": reason,
        "issue": None,
    }
    written = run.recorder.write(data)
    if work.sent:
        run.session.ledger.verdict(case.id, verdict, reason=reason)
    return CaseResult(
        case_id=case.id,
        verdict=verdict,
        reason=reason,
        order_ids=list(work.order_ids),
        status=work.status,
        record=written,
    )


def _write_hours(work: _Work) -> str | None:
    """Right before each GTT write (place, modify): the GTT rule (``write_refusal``) at the
    run's clock (none on UAT); a cancel is never refused (it only removes risk)."""
    session = work.run.session
    return write_refusal(os.environ, work.run.now(), uat=session.uat, mock=session.mock)


def _refusal(work: _Work) -> str | None:
    """Why the case must not send anything now (resume, stop, invariant, breaker, role)."""
    session, case = work.run.session, work.case
    done = session.case_done(case.id)
    if done is not None:
        return done
    hours = _write_hours(work)
    if hours is not None:
        return hours
    if session.guard.stopped():
        return session.guard.stop_reason
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
        return stop.reason
    tripped = session.guard.tripped(case.segment)
    if tripped is not None:
        return f"circuit breaker tripped for {case.segment}: {tripped}"
    contract = work.run.contracts.get(ROLE)
    if contract is None:
        return f"role {ROLE} (NSE ITC-EQ) is not available in this run"
    return batch.ceiling_refusal(work.run, contract)  # a GTT's order may fill on UAT


def _uat_fill(work: _Work, row: Order, plan: Mapping[str, Any], order_id: str) -> str | None:
    """On UAT (which fills at any price): the fired order's fill goes to the run's position
    ledger once and the case is recorded (``uat_filled_far``); ``None`` elsewhere."""
    run = work.run
    contract = run.contracts.get(ROLE)
    filled = int(row.filled_quantity or 0) or (
        int(row.quantity or 0) if status_of(row) == "COMPLETE" else 0
    )
    if not run.session.uat or contract is None or not filled:
        return None
    if batch.UAT_FILLED_FAR not in work.notes:
        run.positions.add_fill(contract.tradingsymbol, filled, lot_size=contract.lot_size,
                               case_id=work.case.id, order_id=order_id)  # fmt: skip
        quoted = work.notes.get("place_quote")  # the snapshot the GTT was priced from
        snapshot = PriceSnapshot(**quoted) if isinstance(quoted, Mapping) else None
        work.notes[batch.UAT_FILLED_FAR] = batch.uat_fill_note(
            work.case, contract, dict(plan), snapshot, filled
        )
    return batch.uat_fill_reason(filled)


def _plan(
    contract: Contract, alert_type: GttAlertType, trigger: Decimal, price: Decimal
) -> dict[str, Any]:
    """The ``place_gtt`` / ``modify_gtt`` keywords (no tag): a far CNC LIMIT BUY of 1 share."""
    return {
        "exchange": contract.exchange,
        "tradingsymbol": contract.tradingsymbol,
        "alert_type": alert_type,
        "trigger_value": trigger,
        "quantity": contract.quantity(1),
        "transaction_type": TransactionType.BUY,
        "product": "CNC",
        "order_type": OrderType.LIMIT,
        "price": price,
        "validity": Validity.DAY,
        "alert_validity": GttValidity.DAY,
    }


def _snapshot(work: _Work, contract: Contract, key: str) -> PriceSnapshot:
    snapshot = pricing.fresh_quote(
        work.run.session.client,
        contract,
        uat=work.run.session.uat,
        sleep=work.run.session.sleep,
        pacer=work.run.session.uat_quote_pacer(),
    )
    work.notes[key] = dataclasses.asdict(snapshot)
    return snapshot


def _gtt_row(row: Gtt) -> dict[str, Any]:
    """A GTT row for the record (never the account id)."""
    return {
        "alert_id": row.alert_id,
        "stat": row.stat,
        "tag": row.tag,
        "alert_type": None if row.alert_type is None else str(row.alert_type),
        "trigger_value": None if row.trigger_value is None else str(row.trigger_value),
        "alert_validity": None if row.alert_validity is None else str(row.alert_validity),
        "quantity": row.quantity,
        "price": None if row.price is None else str(row.price),
    }


def _order_row(row: Order) -> dict[str, Any]:
    return dict(row.raw) or row.model_dump(mode="json", by_alias=True)


def _placed(work: _Work, outcome: GttOutcome) -> CaseResult | None:
    """The result when the GTT place did not leave an alert id to go on with, else ``None``."""
    if outcome.verdict == NOT_SENT:
        return _finish(work, FAIL, f"the SDK refused the GTT before sending: {_message(outcome)}")
    if outcome.verdict == REJECTED:
        return _finish(work, SKIP, f"the server rejected the GTT ({_message(outcome)}); recorded")
    if outcome.check_the_account:
        return _finish(work, CHECK, f"{CHECK_THE_ACCOUNT}: GTT tag {outcome.tag}: place unknown")
    if outcome.alert_id is None:
        what = f"GTT tag {outcome.tag}: accepted without an alert id"
        work.run.session.guard.note_check_the_account(work.case.segment, what)
        return _finish(work, CHECK, f"{CHECK_THE_ACCOUNT}: {what}")
    return None


def _message(outcome: GttOutcome) -> str:
    error = outcome.error or {}
    return str(error.get("emsg") or error.get("message") or outcome.exception or "")


def _row_problems(row: Gtt, plan: Mapping[str, Any], trigger: Decimal) -> list[str]:
    expected = {
        "alert_type": (row.alert_type, GttAlertType(plan["alert_type"])),
        "trigger_value": (row.trigger_value, trigger),
        "alert_validity": (row.alert_validity, GttValidity(plan["alert_validity"])),
        "quantity": (row.quantity, plan["quantity"]),
        "price": (row.price, plan["price"]),
    }
    return [f"{k} {seen} != sent {sent}" for k, (seen, sent) in expected.items() if seen != sent]


def _read_back(work: _Work, tag: str, alert_id: str, key: str) -> Gtt | None:
    """Our GTT's pending row (by tag, else by alert id), recorded under ``key``."""
    rows, _ = calls.gtts_with_tag(work.run.session, tag)
    ours = [g for g in rows if (g.alert_id or "").strip() == alert_id] or rows
    work.notes[key] = [_gtt_row(g) for g in rows]
    return ours[0] if len(ours) == 1 else None


# -- the spec §5 GTT ------------------------------------------------------------------------------


def run_far(run: MatrixRun, case: Case = FAR) -> CaseResult:
    """Place the far GTT once, read it back, modify its trigger once, cancel it, confirm it is
    gone and never fired (see the module doc)."""
    work = _Work(run, case)
    refusal = _refusal(work)
    if refusal is not None:
        return _finish(work, SKIP, refusal)
    session = run.session
    contract = run.contracts.get(ROLE)
    assert contract is not None  # _refusal checked it
    session.guard.reserve(case.id, case.orders)
    try:
        snapshot = _snapshot(work, contract, "place_quote")
        price = pricing.far_buy(snapshot)
        trigger = gtt.gtt_trigger(snapshot.last_price, snapshot.tick_size, gtt.PLACE_FACTOR)
    except Refusal as exc:
        return _finish(work, SKIP, exc.reason)
    plan = _plan(contract, GttAlertType.LTP_ABOVE, trigger, price)
    work.notes["plan"] = plan
    hours = _write_hours(work)
    if hours is not None:
        return _finish(work, SKIP, hours)
    try:
        placed = calls.gtt_place(session, plan, work.ref)
    except (StopRun, Refusal) as exc:  # stopped or tripped since the checks: nothing sent
        return _finish(work, SKIP, exc.reason)
    work.calls.append(placed)
    stopped = _placed(work, placed)
    if stopped is not None:
        return stopped
    assert placed.alert_id is not None
    alert_id = placed.alert_id
    # An interruption (Ctrl-C, a test timeout) propagates without a cleanup call here: after
    # Ctrl-C nothing more is sent (the ledger lists the GTT), otherwise the teardown cancels it.
    try:
        verdict, reason, alert_id = _far_steps(work, placed, plan, trigger, alert_id)
    except Exception as exc:  # recorded; the GTT is still cancelled below
        verdict, reason = FAIL, f"{type(exc).__name__}: {exc}"
    cleanup = _cancel_and_confirm(work, placed.tag, alert_id)
    fired = _fired(work, placed.tag, {a for a in (placed.alert_id, alert_id) if a})
    problems = [p for p in (cleanup, fired) if p]
    if problems:
        verdict, reason = CHECK, f"{CHECK_THE_ACCOUNT}: " + "; ".join(problems)
        session.guard.note_check_the_account(case.segment, "; ".join(problems))
    return _finish(work, verdict, reason)


def _far_steps(
    work: _Work, placed: GttOutcome, plan: dict[str, Any], trigger: Decimal, alert_id: str
) -> tuple[str, str, str]:
    """Read back and modify once: (verdict, reason, the alert id to cancel)."""
    session = work.run.session
    row = _read_back(work, placed.tag, alert_id, "after_place")
    if row is None:
        return FAIL, f"the GTT (tag {placed.tag}) is not once in pending_gtts()", alert_id
    problems = _row_problems(row, plan, trigger)
    if problems:
        return FAIL, "pending_gtts() row: " + "; ".join(problems), alert_id
    contract = work.run.contracts.get(ROLE)
    assert contract is not None
    try:
        snapshot = _snapshot(work, contract, "modify_quote")
        new_trigger = gtt.gtt_trigger(snapshot.last_price, snapshot.tick_size, gtt.MODIFY_FACTOR)
    except Refusal as exc:
        return SKIP, f"modify not sent: {exc.reason} (the GTT is cancelled)", alert_id
    changed = {**plan, "trigger_value": new_trigger}
    hours = _write_hours(work)
    if hours is not None:
        return SKIP, f"modify not sent: {hours} (the GTT is cancelled)", alert_id
    try:
        modified = calls.gtt_modify(session, alert_id, work.ref, before=row, **changed)
    except (StopRun, Refusal) as exc:  # stopped or tripped: not sent; the GTT is cancelled
        return SKIP, exc.reason, alert_id
    work.calls.append(modified)
    if modified.check_the_account:
        return CHECK, f"{CHECK_THE_ACCOUNT}: GTT {alert_id}: modify outcome unknown", alert_id
    if modified.verdict not in (ACKNOWLEDGED, RESOLVED):
        return FAIL, f"ModifyGTTOrder {modified.verdict}: {_message(modified)}", alert_id
    after = _read_back(work, placed.tag, alert_id, "after_modify")
    if after is None:
        return FAIL, f"the modified GTT (tag {placed.tag}) is not once in pending_gtts()", alert_id
    listed = (after.alert_id or "").strip()
    if listed and listed != alert_id:  # the modify changed the alert id: found by our tag
        work.notes["alert_id_changed"] = {"from": alert_id, "to": listed}
        if not session.ledger.adopt_gtt(listed, placed.tag):
            return FAIL, f"the modified GTT {listed} (tag {placed.tag}) is not ours", alert_id
        alert_id = listed
    problems = _row_problems(after, changed, new_trigger)
    if problems:
        return FAIL, "pending_gtts() row after the modify: " + "; ".join(problems), alert_id
    return PASS, "placed, read back, modified once and cancelled", alert_id


def _cancel_and_confirm(work: _Work, tag: str, alert_id: str) -> str | None:
    """Cancel our GTT once (when it may still be pending), then confirm nothing of the tag is
    pending; a problem (CHECK THE ACCOUNT) as text, else ``None``."""
    session = work.run.session
    ledger = session.ledger
    if (
        ledger.alert_tag(alert_id) is not None
        and not ledger.alert_flagged(alert_id)
        and not ledger.gtt_cancel_sent(alert_id)
    ):
        try:
            work.calls.append(calls.gtt_cancel(session, alert_id, work.ref))
        except Refusal as exc:
            work.notes["cancel_refused"] = exc.reason
    for index in range(session.lookup_attempts):
        if index:
            session.sleep(session.lookup_interval)
        pending = calls.read_gtts(session)
        if pending is None:
            continue
        left = [
            (g.alert_id or "").strip()
            for g in pending
            if (g.tag or "").strip() == tag or (g.alert_id or "").strip() == alert_id
        ]
        work.notes["left_pending"] = left
        if not left:
            return None
    if "left_pending" not in work.notes:
        return f"GTT tag {tag}: pending_gtts() unreadable after the cancel"
    return f"GTT(s) {', '.join(work.notes['left_pending'])} (tag {tag}) still pending"


def _fired(work: _Work, tag: str, alert_ids: set[str]) -> str | None:
    """A far GTT of ours that fired: its order (adopted by ``add_ord_id``) is cancelled once;
    the signs as text (CHECK THE ACCOUNT), else ``None``. Fails closed when neither list can
    be read."""
    session = work.run.session
    signs: list[str] = []
    triggered = calls.read_gtts(session, triggered=True)
    if triggered is not None:
        ours = [
            g
            for g in triggered
            if (g.tag or "").strip() == tag or (g.alert_id or "").strip() in alert_ids
        ]
        cancelled = [g for g in ours if gtt.cancelled_row(g)]
        if cancelled:  # UAT lists a cancelled GTT as triggered (spec F32): recorded only
            work.notes["triggered_cancelled"] = [g.alert_id for g in cancelled]
        signs += [f"triggered GTT {g.alert_id}" for g in ours if not gtt.cancelled_row(g)]
    book = read_book(session.client, attempts=1)
    if book is not None:
        for row in book:
            if _of_our_gtt(work, row, alert_ids):
                signs.append(f"order {row.order_id}")
                _cancel_fired_order(work, row)
    if triggered is None and book is None:
        return f"GTT tag {tag}: neither triggered_gtts() nor orders() could be read"
    if signs:
        work.notes["fired"] = signs
        return f"GTT tag {tag} fired and placed a real order: {', '.join(signs)}"
    return None


def _cancel_fired_order(work: _Work, row: Order) -> None:
    session = work.run.session
    order_id = (row.order_id or "").strip()
    if not order_id or not session.ledger.adopt(row):
        return
    work.order_ids.append(order_id)
    if is_final(status_of(row)) or session.ledger.cancel_sent(order_id):
        return
    try:
        work.calls.append(calls.cancel(session, order_id, work.ref))
    except Refusal as exc:
        work.notes["order_cancel_refused"] = exc.reason


# -- the GTT that fires at once (UAT only) --------------------------------------------------------


def run_fires(run: MatrixRun, case: Case = FIRES) -> CaseResult:
    """A GTT whose condition is already true when placed: it fires at once and places its far
    BUY, which is found by ``add_ord_id``, adopted, cancelled once and confirmed final."""
    work = _Work(run, case)
    refusal = _refusal(work)
    if refusal is not None:
        return _finish(work, SKIP, refusal)
    session = run.session
    contract = run.contracts.get(ROLE)
    assert contract is not None
    session.guard.reserve(case.id, case.orders)
    try:
        snapshot = _snapshot(work, contract, "place_quote")
        price = pricing.far_buy(snapshot)
        trigger = ceil_to_tick(snapshot.last_price * FIRE_FACTOR, snapshot.tick_size)
        if not gtt.condition_already_true(GttAlertType.LTP_BELOW, snapshot.last_price, trigger):
            raise Refusal(f"LTP_B_O at {trigger} would not fire at once; not sending")
    except Refusal as exc:
        return _finish(work, SKIP, exc.reason)
    plan = _plan(contract, GttAlertType.LTP_BELOW, trigger, price)
    work.notes["plan"] = plan
    hours = _write_hours(work)
    if hours is not None:
        return _finish(work, SKIP, hours)
    try:
        session.guard.check_send(case.segment)  # the order it places needs a MAX_OPEN slot
    except (MaxOpenReached, Refusal) as exc:
        return _finish(work, SKIP, str(exc))
    except StopRun as stop:
        return _finish(work, SKIP, stop.reason)
    try:
        placed = calls.gtt_place(session, plan, work.ref)
    except (StopRun, Refusal) as exc:  # stopped or tripped since the checks: nothing sent
        return _finish(work, SKIP, exc.reason)
    finally:
        session.guard.release(1)
    work.calls.append(placed)
    stopped = _placed(work, placed)
    if stopped is not None:
        return stopped
    assert placed.alert_id is not None
    try:
        verdict, reason = _fires_steps(work, placed, plan, placed.alert_id)
    except Exception as exc:  # recorded; the teardown still cancels what is ours
        verdict, reason = FAIL, f"{type(exc).__name__}: {exc}"
    if verdict == CHECK:  # every CHECK THE ACCOUNT trips the segment (idempotent)
        session.guard.note_check_the_account(case.segment, reason)
    return _finish(work, verdict, reason)


def _of_our_gtt(work: _Work, row: Order, alert_ids: set[str]) -> bool:
    """Whether ``row`` is the order one of ``alert_ids`` (ours) placed: ``add_ord_id`` alone is
    not enough (an iceberg leg may carry the same id), so ``Ledger.gtt_order_tag`` decides."""
    parent = (row.parent_id or "").strip()
    return parent in alert_ids and work.run.session.ledger.gtt_order_tag(row) is not None


def _wait_for_order(work: _Work, alert_id: str) -> Order | None:
    session = work.run.session
    deadline = session.clock() + TRIGGER_WAIT_SECONDS
    while True:
        book = read_book(session.client, attempts=1)
        row = next((o for o in book or [] if _of_our_gtt(work, o, {alert_id})), None)
        if row is not None or session.clock() >= deadline:
            return row
        session.sleep(session.poll_interval)


def _fires_steps(
    work: _Work, placed: GttOutcome, plan: Mapping[str, Any], alert_id: str
) -> tuple[str, str]:
    session = work.run.session
    row = _wait_for_order(work, alert_id)
    triggered = calls.read_gtts(session, triggered=True)
    if triggered is not None:
        work.notes["triggered"] = [
            _gtt_row(g) for g in triggered if (g.alert_id or "").strip() == alert_id
        ]
    if row is None:
        return _did_not_fire(work, placed, alert_id)
    order_id = (row.order_id or "").strip()
    if not session.ledger.adopt(row):  # only without an order id (_of_our_gtt checked the rest)
        return CHECK, f"{CHECK_THE_ACCOUNT}: order {order_id} of GTT {alert_id} not adopted"
    work.order_ids.append(order_id)
    work.notes["order"] = _order_row(row)
    session.guard.opened(order_id)
    problems = _order_problems(row, plan)
    status = status_of(row)
    if is_final(status):
        session.guard.closed(order_id)
        work.status = status
        uat_fill = _uat_fill(work, row, plan, order_id)
        if uat_fill is not None:
            return RECORD, f"the order {order_id} of GTT {alert_id}: {uat_fill}"
        if status == "COMPLETE":
            return CHECK, f"{CHECK_THE_ACCOUNT}: the order {order_id} of GTT {alert_id} filled"
        return RECORD, f"the order {order_id} of GTT {alert_id} was {status} by the server"
    cancel = calls.cancel(session, order_id, work.ref)
    work.calls.append(cancel)
    if cancel.check_the_account:
        return CHECK, f"{CHECK_THE_ACCOUNT}: order {order_id}: cancel outcome unknown"
    final = _confirm_final(work, order_id)
    if final is None:
        session.guard.note_stuck(work.case.segment, order_id)
        session.ledger.note("stuck", order_id=order_id, segment=work.case.segment)
        return STUCK, f"the order {order_id} of GTT {alert_id} is not final after its cancel"
    work.status = status_of(final)
    session.guard.closed(order_id)
    uat_fill = _uat_fill(work, final, plan, order_id)  # filled before the cancel (part or all)
    if uat_fill is not None:
        return RECORD, f"the order {order_id} of GTT {alert_id}: {uat_fill}"
    if work.status == "COMPLETE":
        return CHECK, f"{CHECK_THE_ACCOUNT}: the order {order_id} of GTT {alert_id} filled"
    if cancel.verdict not in (ACKNOWLEDGED, RESOLVED):
        problems.append(f"CancelOrder {cancel.verdict}")
    if problems:
        return FAIL, f"the order {order_id} of GTT {alert_id}: " + "; ".join(problems)
    return PASS, f"GTT {alert_id} fired at once; its order {order_id} was cancelled"


def _order_problems(row: Order, plan: Mapping[str, Any]) -> list[str]:
    expected = {
        "tradingsymbol": (row.tradingsymbol, plan["tradingsymbol"]),
        "quantity": (row.quantity, plan["quantity"]),
        "price": (row.price, plan["price"]),
        "transaction_type": (row.transaction_type, TransactionType(plan["transaction_type"])),
    }
    return [f"{k} {seen} != sent {sent}" for k, (seen, sent) in expected.items() if seen != sent]


def _confirm_final(work: _Work, order_id: str) -> Order | None:
    session = work.run.session
    deadline = session.clock() + session.confirm_seconds
    while True:
        book = read_book(session.client, attempts=1)
        row = next((o for o in book or [] if (o.order_id or "").strip() == order_id), None)
        if row is not None and is_final(status_of(row)):
            return row
        if session.clock() >= deadline:
            return None
        session.sleep(session.poll_interval)


def _did_not_fire(work: _Work, placed: GttOutcome, alert_id: str) -> tuple[str, str]:
    """No order of the GTT within the wait: cancel it once if still pending; CHECK THE ACCOUNT."""
    what = f"GTT {alert_id} (tag {placed.tag}) did not fire within {TRIGGER_WAIT_SECONDS:.0f} s"
    problem = _cancel_and_confirm(work, placed.tag, alert_id)
    late = _wait_late_order(work, alert_id)
    details = [what]
    if problem:
        details.append(problem)
    if late:
        details.append(late)
    return CHECK, f"{CHECK_THE_ACCOUNT}: " + "; ".join(details)


def _wait_late_order(work: _Work, alert_id: str) -> str | None:
    """One more book read after the cancel: an order of the GTT placed meanwhile is adopted and
    cancelled once."""
    book = read_book(work.run.session.client, attempts=1)
    rows = [o for o in book or [] if _of_our_gtt(work, o, {alert_id})]
    for row in rows:
        _cancel_fired_order(work, row)
    return f"it fired late: order(s) {', '.join(str(o.order_id) for o in rows)}" if rows else None


# -- read-only --------------------------------------------------------------------------------


def _read_only_case(case_id: str) -> Case:
    return Case(
        id=case_id, family=FAMILY, role=ROLE, product="CNC", prctyp="LMT", ret="DAY",
        side="BUY", orders=1, outcome="record", tier=SMOKE,
    )  # fmt: skip


def read_alert_types(run: MatrixRun) -> CaseResult:
    """Read-only: ``gtt_alert_types()`` is a list of non-blank words (e.g. ``["ATP", "LTP"]``)."""
    work = _Work(run, _read_only_case(ALERT_TYPES_ID))
    try:
        families = run.session.client.gtt_alert_types()
    except Exception as exc:
        return _finish(work, FAIL, f"gtt_alert_types() failed: {type(exc).__name__}: {exc}")
    work.notes["alert_types"] = families
    words = isinstance(families, list) and all(isinstance(f, str) and f.strip() for f in families)
    if not words:
        return _finish(work, FAIL, f"gtt_alert_types() is not a list of words: {families!r}")
    return _finish(work, PASS, f"{len(families)} alert families")


def read_triggered(run: MatrixRun) -> CaseResult:
    """Read-only: ``triggered_gtts()`` answers (an empty list when none fired); the GTTs of this
    run among them are recorded."""
    work = _Work(run, _read_only_case(TRIGGERED_ID))
    try:
        triggered = run.session.client.triggered_gtts()
    except Exception as exc:
        return _finish(work, FAIL, f"triggered_gtts() failed: {type(exc).__name__}: {exc}")
    ours = run.session.ledger.gtt_tags()
    work.notes["triggered_count"] = len(triggered)
    work.notes["triggered_of_this_run"] = [
        _gtt_row(g) for g in triggered if (g.tag or "").strip() in ours
    ]
    return _finish(work, PASS, f"{len(triggered)} triggered GTTs listed")


def write_refusal(
    environ: Mapping[str, str],
    now: datetime | None = None,
    *,
    uat: bool = False,
    mock: bool = False,
) -> str | None:
    """Why the GTT writes must not run now, else ``None``. In a mock session (``mock``: the
    session's ``mock``) always (``mock_session.NO_AMO_GTT``: a GTT outlives the session). Outside
    UAT the GTT rule (as in the GTT live test, ``gtt.gtt_session``) refuses whenever
    ``TRADESMART_LIVE_ORDER_OUTSIDE_HOURS`` is set, to any value (a DAY GTT placed after the close
    has an undocumented lifetime), and outside market hours (``family.hours_refusal``). On UAT
    (``uat``: the session's ``uat``) never: UAT tests run at any hour (owner, 2026-11-13). New GTT
    writes only: the teardown's GTT cancels (``calls.cancel_our_gtts``) are allowed at any time,
    as a cancel only removes risk."""
    if mock:
        return mock_session.NO_AMO_GTT
    if uat:
        return None
    if OUTSIDE_HOURS_ENV in environ:
        return f"the GTT cases run inside market hours only; {OUTSIDE_HOURS_ENV} is set: skipped"
    return family.hours_refusal(now)


def run_case(run: MatrixRun, case: Case) -> CaseResult:
    """Run one GTT case (``FAR`` or ``FIRES``); in a mock session neither (a skip, nothing
    sent: ``mock_session.NO_AMO_GTT``)."""
    if run.session.mock:
        return _finish(_Work(run, case), SKIP, mock_session.NO_AMO_GTT)
    if case.id == FAR.id:
        return run_far(run, case)
    if case.id == FIRES.id:
        return run_fires(run, case)
    raise ValueError(f"not a GTT case: {case.id}")


def report(result: CaseResult) -> None:
    """The pytest outcome (``batch.report``)."""
    batch.report(result)
