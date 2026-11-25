"""The UAT matrix family M16: OCO orders (OCO design §3, marker ``m_oco``; UAT).

Every OCO write goes through the never-resend wrappers ``calls.oco_place`` / ``oco_modify`` /
``oco_cancel`` (ledger intent before, outcome after; never repeated; an unknown outcome is looked
up by the OCO's leg tag, else by tradingsymbol and both triggers, else CHECK THE ACCOUNT and the
segment's breaker trips). Every OCO is sent with ``alert_validity`` DAY (nothing stored survives
the day).

A mock session (production host, owner decisions 2026-11-20 E3-E5) runs this family only with the
owner's switch ``TRADESMART_LIVE_MOCK_OCO`` exactly ``oco in mock session <today's IST date>``
(``session.mock_oco``; else every case skips, ``mock_session.NO_OCO``), and then only the three
far cases, without the ``Invalid Oi`` probe (three writes each, as the production lifecycle test);
the fires and slots cases always skip there (``MOCK_NO_FIRE``). Every mock check (the date, the
typed phrase, the host) is re-checked before each place and modify by the wrappers. After the
cases ``mock_cleanup`` cancels only the run's own OCOs and the orders they placed and confirms
them gone / final, else CHECK THE ACCOUNT; GTT and AMO never run in a mock session.

- ``FAR_BUY`` (smoke): NSE ``ITC-EQ`` (role ``nse-b``), a CNC BUY OCO of 1 share, stop-loss
  trigger 1.10 x and target trigger 0.90 x a fresh last price (tick-rounded), LIMIT legs priced
  far below the market with distinct prices. Read back from ``pending_gtts()`` (``is_oco``,
  stop-loss / target triggers and sides, legs), the stop-loss modified once to 1.15 x, read back,
  cancelled once, confirmed gone; a sign that it fired (a triggered row that is not ``CANCELLED``
  in any letter case, or an order of ours by ``add_ord_id``) or an OCO still pending is CHECK THE
  ACCOUNT. Then one ``modify_oco`` on the cancelled id, which must be ``OrderRejectedError``
  (``Invalid Oi``); recorded.
- ``FAR_SELL`` (full): the mirror (stop-loss 0.90 x, target 1.10 x, modified to 0.85 x, SELL
  legs priced far above). The account normally holds no ITC: an RMS refusal at placement is
  RECORD (neutral to the breaker), as every CNC SELL without holdings.
- ``FAR_SL_LMT`` (full): as ``FAR_BUY`` with the stop-loss leg ``SL-LMT`` and its own ``trgprc``.
- ``FIRES_BUY`` (full): a CNC BUY OCO whose target trigger is already crossed (target 1.02 x,
  stop-loss 1.10 x the last price: both above it, the F35 observation), far LIMIT legs at
  distinct prices. The fired order is looked for by ``add_ord_id`` for ``TRIGGER_WAIT_SECONDS``,
  adopted, cancelled once and confirmed final; which leg fired is told by the order's price and
  recorded (``fired_leg``: one entry per fired order); the book is read once more for the other
  leg's order and the OCO must have left the pending list (else cancelled once, confirmed gone).
  Exactly one leg's order out and cleaned up: pass; both legs' orders, or a fired order that
  cannot be cleaned up: CHECK THE ACCOUNT (a stuck order: STUCK); a fill on UAT goes to the
  run's position ledger and is RECORD. No order within the wait: the OCO is cancelled once,
  confirmed gone and the book read once more; "did not fire" is RECORD, anything left or late
  is CHECK THE ACCOUNT.
- ``FIRES_SELL`` (full): the mirror (target 0.98 x, stop-loss 0.90 x, legs far above). A pass
  needs holdings the run sees (read-only ``holdings()``), else RECORD; a fired SELL refused by
  RMS is RECORD (neutral to the breaker).
- ``SLOTS_REVERSED`` (full; the Python SDK only, skipped for a driver SDK): ``FIRES_BUY`` sent
  through ``request("PlaceOCOOrder", ...)`` with the docs-example slot order (target in ``x`` /
  ``place_order_params``, stop-loss in ``y``; ``calls.oco_place_raw``); records which leg fired
  and the slot order sent. RECORD (never a failure, neutral to the breaker); CHECK THE ACCOUNT
  only when the clean-up fails.

Each case writes one record (``records.jsonl``, with the OCO notes) and, when it sent anything,
its ledger verdict.
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
    is_final,
    status_of,
)
from tests.live_orders.uat_matrix import batch, calls, expect, family, pricing
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
from tests.live_orders.uat_matrix.calls import ACKNOWLEDGED, RESOLVED, OcoOutcome
from tests.live_orders.uat_matrix.cases import FULL, REST, SMOKE, Case
from tests.live_orders.uat_matrix.cases_gtt import (
    TRIGGER_WAIT_SECONDS,
    _cancel_fired_order,
    _confirm_final,
    _finish,
    _message,
    _of_our_gtt,
    _order_row,
    _snapshot,
    _Work,
)
from tests.live_orders.uat_matrix.guard import MaxOpenReached, StopRun
from tests.live_orders.uat_matrix.ledger import NOT_SENT, REJECTED, check_open_orders, read_book
from tests.live_orders.uat_matrix.pricing import PriceSnapshot
from tradesmart import OcoLeg, round_to_tick
from tradesmart.enums import OrderType, TransactionType
from tradesmart.models.gtt import Gtt
from tradesmart.models.orders import Order

FAMILY = "oco"
ROLE = "nse-b"  # NSE ITC-EQ
STOP_FACTOR = Decimal("1.10")  # BUY: the stop-loss trigger; a SELL mirrors it (2 - factor)
TARGET_FACTOR = Decimal("0.90")
FIRES_TARGET_FACTOR = Decimal("1.02")  # BUY: the fires target, already crossed (SELL: 0.98)
# Owner decision E5: no OCO is ever made to fire in a mock session, whatever the OCO switch.
MOCK_NO_FIRE = calls.MOCK_NO_FIRE
MODIFY_FACTOR = Decimal("1.15")  # the stop-loss trigger is modified once to this (SELL: 0.85)
MOCK_NO_PROBE = (
    "not sent in a mock session: on the production host the far OCO keeps to place, modify and "
    "cancel (the Invalid Oi probe runs on UAT)"
)


def _case(case_id: str, side: str, prctyp: str, tier: str) -> Case:
    return Case(
        id=case_id,
        family=FAMILY,
        role=ROLE,
        product="CNC",
        prctyp=prctyp,
        ret="DAY",
        side=side,
        orders=1,  # one OCO place call (its modify, probe and cancel are not places)
        outcome=REST,
        tier=tier,
    )


FAR_BUY = _case("oco-far-buy", "BUY", "LMT", SMOKE)
FAR_SELL = _case("oco-far-sell", "SELL", "LMT", FULL)
FAR_SL_LMT = _case("oco-far-sl-lmt", "BUY", "SL-LMT", FULL)  # prctyp: the stop-loss leg's
FIRES_BUY = _case("oco-fires-buy", "BUY", "LMT", FULL)
FIRES_SELL = _case("oco-fires-sell", "SELL", "LMT", FULL)
SLOTS_REVERSED = _case("oco-slots-reversed", "BUY", "LMT", FULL)
OCO_CASES: list[Case] = [FAR_BUY, FAR_SELL, FAR_SL_LMT, FIRES_BUY, FIRES_SELL, SLOTS_REVERSED]
FAR_CASES = (FAR_BUY.id, FAR_SELL.id, FAR_SL_LMT.id)
# The mock-session clean-up's record (``mock_cleanup``; not a case of the matrix table).
MOCK_CLEANUP = _case("oco-mock-cleanup", "BUY", "LMT", SMOKE)


def write_refusal(
    environ: Mapping[str, str],
    now: datetime | None = None,
    *,
    uat: bool = False,
    mock: bool = False,
    mock_oco: bool = False,
) -> str | None:
    """Why the OCO writes must not run now, else ``None``. In a mock session unless the owner's
    OCO switch is on (``mock_oco``: ``mock_session.NO_OCO``); with it, no hours check (the owner
    starts a mock run once mock trading opens, as every family) and the wrappers re-check the
    mock switch before each place and modify. Elsewhere outside UAT: whenever
    ``TRADESMART_LIVE_ORDER_OUTSIDE_HOURS`` is set and outside market hours (as the GTT cases);
    on UAT never (any hour)."""
    if mock:
        return None if mock_oco else mock_session.NO_OCO
    if uat:
        return None
    if OUTSIDE_HOURS_ENV in environ:
        return f"the OCO cases run inside market hours only; {OUTSIDE_HOURS_ENV} is set: skipped"
    return family.hours_refusal(now)


def _write_hours(work: _Work) -> str | None:
    session = work.run.session
    return write_refusal(
        os.environ, work.run.now(), uat=session.uat, mock=session.mock, mock_oco=session.mock_oco
    )


def _refusal(work: _Work) -> str | None:
    """Why the case must not send anything now (mock, resume, stop, invariant, breaker, role)."""
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
    return batch.ceiling_refusal(work.run, contract)  # a fired order may fill on UAT


# -- the plan -------------------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class _Plan:
    """What one far case sends: the side, both legs and the fields every call carries."""

    side: TransactionType
    stop_loss: OcoLeg
    target: OcoLeg
    fields: dict[str, Any]

    def with_stop_loss(self, stop_loss: OcoLeg) -> _Plan:
        return dataclasses.replace(self, stop_loss=stop_loss)

    def kwargs(self) -> dict[str, Any]:
        return {
            "stop_loss": self.stop_loss,
            "target": self.target,
            "transaction_type": self.side,
            **self.fields,
        }

    def note(self) -> dict[str, Any]:
        return {
            **{k: str(v) for k, v in self.fields.items()},
            "transaction_type": str(self.side),
            "stop_loss": _leg_note(self.stop_loss),
            "target": _leg_note(self.target),
        }


def _leg_note(leg: OcoLeg) -> dict[str, Any]:
    return {
        "trigger_value": str(leg.trigger_value),
        "price": None if leg.price is None else str(leg.price),
        "order_type": str(OrderType(leg.order_type)),
        "trigger_price": None if leg.trigger_price is None else str(leg.trigger_price),
    }


def _triggers(
    side: TransactionType, snapshot: PriceSnapshot, stop_factor: Decimal
) -> tuple[Decimal, Decimal]:
    """``(stop-loss, target)`` triggers: BUY ``stop_factor`` x and 0.90 x the last price, a SELL
    the mirror (2 - factor); tick-rounded and refused when they do not bracket the last price
    (a trigger would be crossed at once)."""
    last, tick = snapshot.last_price, snapshot.tick_size
    if side is TransactionType.BUY:
        stop = round_to_tick(last * stop_factor, tick)
        target = round_to_tick(last * TARGET_FACTOR, tick)
        bracket = target < last < stop
    else:
        stop = round_to_tick(last * (2 - stop_factor), tick)
        target = round_to_tick(last * (2 - TARGET_FACTOR), tick)
        bracket = stop < last < target
    if not bracket:
        raise Refusal(
            f"OCO triggers {stop} / {target} do not bracket the last price {last}; not sending"
        )
    return stop, target


def _legs(
    case: Case, side: TransactionType, snapshot: PriceSnapshot, stop: Decimal, target: Decimal
) -> tuple[OcoLeg, OcoLeg]:
    """The two legs: LIMIT orders far from the market (a BUY well below, a SELL well above) at
    distinct prices; ``FAR_SL_LMT``'s stop-loss leg is ``SL-LMT`` with its own trigger price."""
    tick = snapshot.tick_size
    if side is TransactionType.BUY:
        base = pricing.far_buy(snapshot)
        stop_price, target_price = base, base + tick
        if case.prctyp == "SL-LMT":
            return (
                OcoLeg(
                    trigger_value=stop,
                    order_type=OrderType.SL,
                    trigger_price=base,
                    price=base + 2 * tick,
                ),
                OcoLeg(trigger_value=target, price=target_price),
            )
    else:
        base = pricing.far_sell(snapshot)
        stop_price, target_price = base, base - tick
    return (
        OcoLeg(trigger_value=stop, price=stop_price),
        OcoLeg(trigger_value=target, price=target_price),
    )


# -- reading the rows -----------------------------------------------------------------------------


def _side_note(side: Any) -> dict[str, Any] | None:
    if side is None:
        return None
    leg = side.leg
    return {
        "trigger_value": None if side.trigger_value is None else str(side.trigger_value),
        "transaction_type": None if leg.transaction_type is None else str(leg.transaction_type),
        "order_type": None if leg.order_type is None else str(leg.order_type),
        "price": None if leg.price is None else str(leg.price),
        "trigger_price": None if leg.trigger_price is None else str(leg.trigger_price),
        "quantity": leg.quantity,
        "tag": leg.tag,
    }


def _oco_row(row: Gtt) -> dict[str, Any]:
    """An OCO row for the record (never the account id)."""
    return {
        "alert_id": row.alert_id,
        "stat": row.stat,
        "is_oco": row.is_oco,
        "alert_type": None if row.alert_type is None else str(row.alert_type),
        "alert_validity": None if row.alert_validity is None else str(row.alert_validity),
        "tag": row.tag,
        "leg_tags": calls._leg_tags(row),
        "stop_loss": _side_note(row.stop_loss),
        "target": _side_note(row.target),
    }


def _ours(row: Gtt, tag: str, alert_ids: set[str]) -> bool:
    return (row.alert_id or "").strip() in alert_ids or calls._row_gtt_tag(row, {tag}) == tag


def _read_back(work: _Work, tag: str, alert_id: str, key: str) -> Gtt | None:
    """Our OCO's pending row (by alert id, else by leg tag), recorded under ``key``."""
    rows = calls.read_gtts(work.run.session)
    ours = [g for g in rows or [] if _ours(g, tag, {alert_id})]
    work.notes[key] = [_oco_row(g) for g in ours]
    by_id = [g for g in ours if (g.alert_id or "").strip() == alert_id]
    chosen = by_id or ours
    return chosen[0] if len(chosen) == 1 else None


def _row_problems(row: Gtt, plan: _Plan) -> list[str]:
    problems: list[str] = []
    if not row.is_oco:
        return ["the row is not an OCO"]
    if str(row.alert_validity) != str(plan.fields["alert_validity"]):
        problems.append(
            f"alert_validity {row.alert_validity} != sent {plan.fields['alert_validity']}"
        )
    for name, sent in (("stop_loss", plan.stop_loss), ("target", plan.target)):
        side = getattr(row, name)
        if side is None:
            problems.append(f"{name} side not readable")
            continue
        leg = side.leg
        expected = {
            "trigger_value": (side.trigger_value, Decimal(str(sent.trigger_value))),
            "transaction_type": (leg.transaction_type, plan.side),
            "order_type": (leg.order_type, OrderType(sent.order_type)),
            "quantity": (leg.quantity, plan.fields["quantity"]),
            "price": (leg.price, Decimal(str(sent.price))),
        }
        if sent.trigger_price is not None:
            expected["trigger_price"] = (leg.trigger_price, Decimal(str(sent.trigger_price)))
        problems += [
            f"{name} {k} {seen} != sent {want}"
            for k, (seen, want) in expected.items()
            if seen != want
        ]
    return problems


# -- the far cases --------------------------------------------------------------------------------


def _placed_verdict(work: _Work, outcome: OcoOutcome) -> tuple[str, str] | None:
    """(verdict, reason) when the OCO place did not leave an alert id to go on with, else
    ``None``."""
    if outcome.verdict == NOT_SENT:
        return FAIL, f"the SDK refused the OCO before sending: {_message(outcome)}"
    if outcome.verdict == REJECTED:
        message = _message(outcome)
        if work.case.side == "SELL" and expect.is_rms_limit(message):
            # A CNC SELL without holdings: an RMS refusal expected, not documented (RECORD).
            return RECORD, f"the server refused the SELL OCO (RMS): {message}"
        return SKIP, f"the server rejected the OCO ({message}); recorded"
    if outcome.check_the_account:
        return CHECK, f"{CHECK_THE_ACCOUNT}: OCO tag {outcome.tag}: place unknown"
    if outcome.alert_id is None:
        what = f"OCO tag {outcome.tag}: accepted without an alert id"
        work.run.session.guard.note_check_the_account(work.case.segment, what)
        return CHECK, f"{CHECK_THE_ACCOUNT}: {what}"
    return None


def _placed(work: _Work, outcome: OcoOutcome) -> CaseResult | None:
    """The result when the OCO place did not leave an alert id to go on with, else ``None``."""
    stopped = _placed_verdict(work, outcome)
    return None if stopped is None else _finish(work, *stopped)


def run_far(run: MatrixRun, case: Case = FAR_BUY) -> CaseResult:
    """Place the far OCO once, read it back, modify its stop-loss once, cancel it, confirm it is
    gone and never fired, then probe a modify on the cancelled id (see the module doc)."""
    work = _Work(run, case)
    refusal = _refusal(work)
    if refusal is not None:
        return _finish(work, SKIP, refusal)
    session = run.session
    contract = run.contracts.get(ROLE)
    assert contract is not None  # _refusal checked it
    side = TransactionType.BUY if case.side == "BUY" else TransactionType.SELL
    session.guard.reserve(case.id, case.orders)
    try:
        snapshot = _snapshot(work, contract, "place_quote")
        stop, target = _triggers(side, snapshot, STOP_FACTOR)
        stop_leg, target_leg = _legs(case, side, snapshot, stop, target)
    except Refusal as exc:
        return _finish(work, SKIP, exc.reason)
    plan = _Plan(
        side,
        stop_leg,
        target_leg,
        {
            "exchange": contract.exchange,
            "tradingsymbol": contract.tradingsymbol,
            "quantity": contract.quantity(1),
            "product": "CNC",
            "validity": "DAY",
            "alert_validity": "DAY",
        },
    )
    work.notes["plan"] = plan.note()
    hours = _write_hours(work)
    if hours is not None:
        return _finish(work, SKIP, hours)
    try:
        ref = work.ref
        placed = calls.oco_place(session, ref, **plan.kwargs())
    except (StopRun, Refusal) as exc:  # stopped or tripped since the checks: nothing sent
        return _finish(work, SKIP, exc.reason)
    work.calls.append(placed)
    stopped = _placed(work, placed)
    if stopped is not None:
        return stopped
    assert placed.alert_id is not None
    alert_id = placed.alert_id
    # An interruption (Ctrl-C, a test timeout) propagates without a cleanup call here: after
    # Ctrl-C nothing more is sent (the ledger lists the OCO), otherwise the teardown cancels it.
    try:
        verdict, reason, alert_id, plan = _far_steps(work, placed, plan, alert_id)
    except Exception as exc:  # recorded; the OCO is still cancelled below
        verdict, reason = FAIL, f"{type(exc).__name__}: {exc}"
    cleanup = _cancel_and_confirm(work, placed.tag, alert_id)
    ids = {a for a in (placed.alert_id, alert_id) if a}
    fired = _fired(work, placed.tag, ids, plan)
    problems = [p for p in (cleanup, fired) if p]
    if problems:
        verdict, reason = CHECK, f"{CHECK_THE_ACCOUNT}: " + "; ".join(problems)
        session.guard.note_check_the_account(case.segment, "; ".join(problems))
    elif verdict == PASS:
        verdict, reason = _probe(work, alert_id, plan, verdict, reason)
    return _finish(work, verdict, reason)


def _far_steps(
    work: _Work, placed: OcoOutcome, plan: _Plan, alert_id: str
) -> tuple[str, str, str, _Plan]:
    """Read back and modify the stop-loss once: (verdict, reason, the alert id to cancel, the
    plan as it stands)."""
    session = work.run.session
    row = _read_back(work, placed.tag, alert_id, "after_place")
    if row is None:
        return FAIL, f"the OCO (tag {placed.tag}) is not once in pending_gtts()", alert_id, plan
    problems = _row_problems(row, plan)
    if problems:
        return FAIL, "pending_gtts() row: " + "; ".join(problems), alert_id, plan
    contract = work.run.contracts.get(ROLE)
    assert contract is not None
    try:
        snapshot = _snapshot(work, contract, "modify_quote")
        new_stop, _ = _triggers(plan.side, snapshot, MODIFY_FACTOR)
    except Refusal as exc:
        return SKIP, f"modify not sent: {exc.reason} (the OCO is cancelled)", alert_id, plan
    changed = plan.with_stop_loss(plan.stop_loss.model_copy(update={"trigger_value": new_stop}))
    hours = _write_hours(work)
    if hours is not None:
        return SKIP, f"modify not sent: {hours} (the OCO is cancelled)", alert_id, plan
    try:
        modified = calls.oco_modify(session, work.ref, alert_id, before=row, **changed.kwargs())
    except (StopRun, Refusal) as exc:  # stopped or tripped: not sent; the OCO is cancelled
        return SKIP, exc.reason, alert_id, plan
    work.calls.append(modified)
    if modified.check_the_account:
        what = f"{CHECK_THE_ACCOUNT}: OCO {alert_id}: modify outcome unknown"
        return CHECK, what, alert_id, plan
    if modified.verdict not in (ACKNOWLEDGED, RESOLVED):
        reason = f"ModifyOCOOrder {modified.verdict}: {_message(modified)}"
        return FAIL, reason, alert_id, plan
    after = _read_back(work, placed.tag, alert_id, "after_modify")
    if after is None:
        what = f"the modified OCO (tag {placed.tag}) is not once in pending_gtts()"
        return FAIL, what, alert_id, changed
    listed = (after.alert_id or "").strip()
    if listed and listed != alert_id:  # the modify changed the alert id: found by our tag
        work.notes["alert_id_changed"] = {"from": alert_id, "to": listed}
        if not session.ledger.adopt_gtt(listed, placed.tag):
            return (
                FAIL,
                f"the modified OCO {listed} (tag {placed.tag}) is not ours",
                alert_id,
                changed,
            )
        alert_id = listed
    problems = _row_problems(after, changed)
    if problems:
        return (
            FAIL,
            "pending_gtts() row after the modify: " + "; ".join(problems),
            alert_id,
            changed,
        )
    return PASS, "placed, read back, modified once and cancelled", alert_id, changed


def _cancel_and_confirm(work: _Work, tag: str, alert_id: str) -> str | None:
    """Cancel our OCO once (when it may still be pending), then confirm nothing of the tag is
    pending; a problem (CHECK THE ACCOUNT) as text, else ``None``."""
    session = work.run.session
    ledger = session.ledger
    if (
        ledger.alert_tag(alert_id) is not None
        and not ledger.alert_flagged(alert_id)
        and not ledger.gtt_cancel_sent(alert_id)
    ):
        try:
            work.calls.append(calls.oco_cancel(session, work.ref, alert_id))
        except Refusal as exc:
            work.notes["cancel_refused"] = exc.reason
    for index in range(session.lookup_attempts):
        if index:
            session.sleep(session.lookup_interval)
        pending = calls.read_gtts(session)
        if pending is None:
            continue
        left = [(g.alert_id or "").strip() for g in pending if _ours(g, tag, {alert_id})]
        work.notes["left_pending"] = left
        if not left:
            return None
    if "left_pending" not in work.notes:
        return f"OCO tag {tag}: pending_gtts() unreadable after the cancel"
    return f"OCO(s) {', '.join(work.notes['left_pending'])} (tag {tag}) still pending"


def _fired(work: _Work, tag: str, alert_ids: set[str], plan: _Plan) -> str | None:
    """A far OCO of ours that fired: its order (adopted by ``add_ord_id``) is cancelled once; the
    signs as text (CHECK THE ACCOUNT), else ``None``. A triggered row whose ``stat`` is
    ``CANCELLED`` (UAT lists a cancelled alert there) is recorded, not a sign. Fails closed when
    neither list can be read."""
    session = work.run.session
    signs: list[str] = []
    triggered = calls.read_gtts(session, triggered=True)
    if triggered is not None:
        ours = [g for g in triggered if _ours(g, tag, alert_ids)]
        cancelled = [g for g in ours if gtt.cancelled_row(g)]
        if cancelled:
            work.notes["triggered_cancelled"] = [g.alert_id for g in cancelled]
            work.notes["triggered_rows"] = [_oco_row(g) for g in cancelled]
        signs += [f"triggered OCO {g.alert_id}" for g in ours if g not in cancelled]
    book = read_book(session.client, attempts=1)
    if book is not None:
        for row in book:
            if _of_our_gtt(work, row, alert_ids):
                signs.append(f"order {row.order_id}")
                work.notes.setdefault("fired_leg", []).append(_fired_leg(row, plan))
                _cancel_fired_order(work, row)
    if triggered is None and book is None:
        return f"OCO tag {tag}: neither triggered_gtts() nor orders() could be read"
    if signs:
        work.notes["fired"] = signs
        return f"OCO tag {tag} fired and placed a real order: {', '.join(signs)}"
    return None


def _fired_leg(row: Any, plan: _Plan) -> str:
    """Which leg a fired order is, by its price (both legs carry the same remarks, so the legs
    are priced apart): ``stop_loss``, ``target`` or ``unknown``."""
    price = row.price
    if price is not None and price == Decimal(str(plan.stop_loss.price)):
        return "stop_loss"
    if price is not None and price == Decimal(str(plan.target.price)):
        return "target"
    return "unknown"


def _probe(work: _Work, alert_id: str, plan: _Plan, verdict: str, reason: str) -> tuple[str, str]:
    """One ``modify_oco`` on the cancelled id: it must be ``OrderRejectedError`` (``Invalid Oi``),
    recorded. An accepted one would leave an OCO behind: CHECK THE ACCOUNT. The hours rule is
    re-checked first, as before every place and modify. Not in a mock session: on the
    production host the far OCO keeps to its three writes (place, modify, cancel), as the live
    OCO lifecycle test there does."""
    session = work.run.session
    if session.mock:
        work.notes["probe"] = {"not_sent": MOCK_NO_PROBE}
        return verdict, reason
    hours = _write_hours(work)
    if hours is not None:
        work.notes["probe"] = {"not_sent": hours}
        return verdict, reason
    try:
        probe = calls.oco_modify(session, work.ref, alert_id, **plan.kwargs())
    except (StopRun, Refusal) as exc:  # not sent: recorded, the case still passed
        work.notes["probe"] = {"not_sent": exc.reason}
        return verdict, reason
    work.calls.append(probe)
    work.notes["probe"] = {
        "verdict": probe.verdict,
        "exception": probe.exception,
        "error": probe.error,
    }
    if probe.verdict == REJECTED and probe.exception == "OrderRejectedError":
        return PASS, reason + "; a modify on the cancelled id was OrderRejectedError"
    if probe.check_the_account or probe.verdict in (ACKNOWLEDGED, RESOLVED):
        what = f"OCO {alert_id}: a modify on the cancelled id was not refused ({probe.verdict})"
        session.guard.note_check_the_account(work.case.segment, what)
        return CHECK, f"{CHECK_THE_ACCOUNT}: {what}"
    return FAIL, (
        f"a modify on the cancelled OCO {alert_id}: {probe.verdict} {probe.exception}, "
        "expected OrderRejectedError"
    )


# -- the fires cases and the slot observation -----------------------------------------------------


def _fires_triggers(side: TransactionType, snapshot: PriceSnapshot) -> tuple[Decimal, Decimal]:
    """``(stop-loss, target)`` of a fires case: BUY 1.10 x and 1.02 x the last price (both above
    it: the target is already crossed), SELL 0.90 x and 0.98 x (both below); tick-rounded and
    refused unless ``last < target < stop`` (BUY) / ``stop < target < last`` (SELL)."""
    last, tick = snapshot.last_price, snapshot.tick_size
    if side is TransactionType.BUY:
        stop = round_to_tick(last * STOP_FACTOR, tick)
        target = round_to_tick(last * FIRES_TARGET_FACTOR, tick)
        crossed = last < target < stop
    else:
        stop = round_to_tick(last * (2 - STOP_FACTOR), tick)
        target = round_to_tick(last * (2 - FIRES_TARGET_FACTOR), tick)
        crossed = stop < target < last
    if not crossed:
        raise Refusal(
            f"OCO triggers {stop} / {target} do not put the target across the last price {last} "
            "with the stop-loss beyond it; not sending"
        )
    return stop, target


def _holdings_seen(work: _Work, plan: _Plan) -> bool:
    """Whether the run sees the SELL covered (a CNC BUY of it filled in this run, or sellable
    holdings in a fresh, read-only ``holdings()``); recorded under ``holdings``. A SELL fires
    case passes only when it does (design §3)."""
    run = work.run
    fields = {k: plan.fields[k] for k in ("exchange", "tradingsymbol", "quantity")}
    if batch.cnc_covered(run, fields):
        work.notes["holdings"] = "covered by a CNC BUY filled in this run"
        return True
    try:
        holdings = run.session.client.holdings()
    except Exception as exc:  # read-only; an unreadable list only keeps the case from a pass
        work.notes["holdings"] = f"holdings() could not be read ({type(exc).__name__})"
        return False
    refusal = batch.holdings_refusal(fields, holdings)
    work.notes["holdings"] = refusal or "covered by holdings()"
    return refusal is None


def run_fires(run: MatrixRun, case: Case = FIRES_BUY) -> CaseResult:
    """An OCO whose target trigger is already crossed: one leg's order is expected at once. It
    is found by ``add_ord_id`` within ``TRIGGER_WAIT_SECONDS``, adopted, cancelled once and
    confirmed final; which leg fired is recorded (``fired_leg``, one entry per fired order).
    Exactly one leg's order out and cleaned up: pass (a SELL only when the run sees holdings,
    else RECORD); both legs' orders, or one that cannot be cleaned up: CHECK THE ACCOUNT; a
    fired SELL refused by RMS: RECORD; no order within the wait: the OCO is cancelled once,
    confirmed gone and the book read once more for a late order; "did not fire" is RECORD."""
    return _run_fired(run, case, slots=False)


def run_slots_reversed(run: MatrixRun, case: Case = SLOTS_REVERSED) -> CaseResult:
    """As ``run_fires`` (BUY) but sent through ``request("PlaceOCOOrder", ...)`` with the slots
    in the docs-example order (``calls.oco_place_raw``: target in ``x``, stop-loss in ``y``);
    which leg fires is recorded with the slot order sent. RECORD whatever it shows (never a
    failure, neutral to the breaker); CHECK THE ACCOUNT only when the clean-up fails. The
    Python SDK only: skipped for a driver SDK."""
    if run.session.mock:
        return _finish(_Work(run, case), SKIP, MOCK_NO_FIRE)
    name = run.session.sdk.name
    if name != calls.PYTHON:
        reason = f"{case.id}: the Python SDK only (sent through request()); not run for {name}"
        return _finish(_Work(run, case), SKIP, reason)
    return _run_fired(run, case, slots=True)


def _run_fired(run: MatrixRun, case: Case, *, slots: bool) -> CaseResult:
    work = _Work(run, case)
    if run.session.mock:  # E5: whatever the OCO switch, before any read, spend or send
        return _finish(work, SKIP, MOCK_NO_FIRE)
    refusal = _refusal(work)
    if refusal is not None:
        return _finish(work, SKIP, refusal)
    session = run.session
    contract = run.contracts.get(ROLE)
    assert contract is not None  # _refusal checked it
    side = TransactionType.BUY if case.side == "BUY" else TransactionType.SELL
    session.guard.reserve(case.id, case.orders)
    try:
        snapshot = _snapshot(work, contract, "place_quote")
        stop, target = _fires_triggers(side, snapshot)
        stop_leg, target_leg = _legs(case, side, snapshot, stop, target)
    except Refusal as exc:
        return _finish(work, SKIP, exc.reason)
    plan = _Plan(
        side,
        stop_leg,
        target_leg,
        {
            "exchange": contract.exchange,
            "tradingsymbol": contract.tradingsymbol,
            "quantity": contract.quantity(1),
            "product": "CNC",
            "validity": "DAY",
            "alert_validity": "DAY",
        },
    )
    work.notes["plan"] = plan.note()
    if slots:
        work.notes["slot_order"] = dict(calls.REVERSED_SLOTS)
    held = side is TransactionType.BUY or _holdings_seen(work, plan)
    hours = _write_hours(work)
    if hours is not None:
        return _finish(work, SKIP, hours)
    try:
        session.guard.check_send(case.segment)  # the order it places needs a MAX_OPEN slot
    except (MaxOpenReached, Refusal) as exc:
        return _finish(work, SKIP, str(exc))
    except StopRun as stop_run:
        return _finish(work, SKIP, stop_run.reason)
    place = calls.oco_place_raw if slots else calls.oco_place
    try:
        placed = place(session, work.ref, **plan.kwargs())
    except (StopRun, Refusal) as exc:  # stopped or tripped since the checks: nothing sent
        return _finish(work, SKIP, exc.reason)
    finally:
        session.guard.release(1)
    work.calls.append(placed)
    early = _placed_verdict(work, placed)
    if early is not None:
        verdict, reason = early
        if slots and verdict in (FAIL, SKIP):  # the server's (or SDK's) answer: an observation
            verdict = RECORD
        return _finish(work, verdict, reason)
    assert placed.alert_id is not None
    try:
        verdict, reason = _fires_steps(work, placed, plan, placed.alert_id, observe=slots)
    except Exception as exc:  # after a confirmed place: the clean-up is not confirmed
        what = f"{type(exc).__name__}: {exc} (OCO {placed.alert_id}; the teardown cancels ours)"
        verdict, reason = CHECK, f"{CHECK_THE_ACCOUNT}: {what}"
    if verdict == PASS and not held:
        verdict = RECORD
        reason += f"; a SELL fires pass needs holdings the run sees ({work.notes['holdings']})"
    if slots:
        verdict, reason = _observed(verdict, reason)
    if verdict == CHECK:  # every CHECK THE ACCOUNT trips the segment (idempotent)
        session.guard.note_check_the_account(case.segment, reason)
    return _finish(work, verdict, reason)


def _observed(verdict: str, reason: str) -> tuple[str, str]:
    """The slot observation never passes or fails: RECORD, except CHECK THE ACCOUNT when the
    clean-up failed (a stuck order included)."""
    if verdict == CHECK:
        return verdict, reason
    if verdict == STUCK:
        return CHECK, f"{CHECK_THE_ACCOUNT}: {reason}"
    return RECORD, f"recorded (slot observation): {reason}"


def _wait_for_orders(work: _Work, alert_id: str) -> list[Order]:
    """The orders of our OCO in ``orders()`` (by ``add_ord_id``), read until at least one shows
    or ``TRIGGER_WAIT_SECONDS`` pass."""
    session = work.run.session
    deadline = session.clock() + TRIGGER_WAIT_SECONDS
    while True:
        book = read_book(session.client, attempts=1)
        rows = [o for o in book or [] if _of_our_gtt(work, o, {alert_id})]
        if rows or session.clock() >= deadline:
            return rows
        session.sleep(session.poll_interval)


def _late_orders(work: _Work, alert_id: str, seen: set[str]) -> list[Order]:
    """One more book read: orders of our OCO not in ``seen``."""
    book = read_book(work.run.session.client, attempts=1)
    return [
        o
        for o in book or []
        if _of_our_gtt(work, o, {alert_id}) and (o.order_id or "").strip() not in seen
    ]


def _fires_steps(
    work: _Work, placed: OcoOutcome, plan: _Plan, alert_id: str, *, observe: bool
) -> tuple[str, str]:
    """After the place: wait for the fired order(s), clean each up, look once more for the
    other leg's order, and make sure the OCO is no longer pending. ``observe`` (the slot case):
    both legs' orders, cleaned up, are an observation (RECORD), not CHECK THE ACCOUNT."""
    session = work.run.session
    rows = _wait_for_orders(work, alert_id)
    triggered = calls.read_gtts(session, triggered=True)
    if triggered is not None:
        work.notes["triggered"] = [
            _oco_row(g) for g in triggered if (g.alert_id or "").strip() == alert_id
        ]
    if not rows:
        return _did_not_fire(work, placed, plan, alert_id)
    results = [_clean_fired(work, row, plan, alert_id) for row in rows]
    seen = {(o.order_id or "").strip() for o in rows}
    late = _late_orders(work, alert_id, seen)  # the other leg's order, if it went out too
    results += [_clean_fired(work, row, plan, alert_id) for row in late]
    fired = [*rows, *late]
    checks = [reason for verdict, reason in results if verdict == CHECK]
    pending = _pending_after_fire(work, placed.tag, alert_id)
    if pending is not None:
        checks.append(pending)
    both = None
    if len(fired) > 1:
        ids = ", ".join(str(o.order_id) for o in fired)
        both = f"both legs' orders of OCO {alert_id} went out ({ids})"
        if not observe:
            checks.insert(0, both)
    if checks:
        return CHECK, f"{CHECK_THE_ACCOUNT}: " + "; ".join(checks)
    stuck = [reason for verdict, reason in results if verdict == STUCK]
    if stuck:
        return STUCK, "; ".join(stuck)
    if both is not None:
        return RECORD, f"{both}; " + "; ".join(reason for _, reason in results)
    [(verdict, reason)] = results
    return verdict, reason


def _pending_after_fire(work: _Work, tag: str, alert_id: str) -> str | None:
    """Our OCO should leave the pending list once a leg fired: still listed, it is cancelled
    once and confirmed gone (recorded); a problem as text (CHECK THE ACCOUNT), else ``None``.
    An unreadable list is recorded and left to the teardown (it cancels our pending OCOs)."""
    pending = calls.read_gtts(work.run.session)
    if pending is None:
        work.notes["pending_after_fire"] = "pending_gtts() unreadable"
        return None
    ours = [g for g in pending if _ours(g, tag, {alert_id})]
    work.notes["pending_after_fire"] = [_oco_row(g) for g in ours]
    if not ours:
        return None
    return _cancel_and_confirm(work, tag, alert_id)


def _clean_fired(work: _Work, row: Order, plan: _Plan, alert_id: str) -> tuple[str, str]:
    """One fired order of our OCO: recorded (which leg, the row), adopted, cancelled once and
    confirmed final, as the GTT FIRES case (a fill goes to the position ledger)."""
    session = work.run.session
    order_id = (row.order_id or "").strip()
    leg = _fired_leg(row, plan)
    work.notes.setdefault("fired_leg", []).append(leg)
    work.notes.setdefault("orders", []).append(_order_row(row))
    if not session.ledger.adopt(row):  # only without an order id (_of_our_gtt checked the rest)
        return CHECK, f"order {order_id} of OCO {alert_id} not adopted"
    work.order_ids.append(order_id)
    session.guard.opened(order_id)
    problems = _order_problems(row, plan)
    status = status_of(row)
    if is_final(status):
        session.guard.closed(order_id)
        work.status = status
        return _final_on_sight(work, row, plan, order_id, alert_id)
    try:
        cancel = calls.cancel(session, order_id, work.ref)
    except Refusal as exc:  # not sent: the fired order stays open (as GTT _cancel_fired_order)
        work.notes["order_cancel_refused"] = exc.reason
        return CHECK, f"order {order_id} of OCO {alert_id}: cancel refused ({exc.reason})"
    work.calls.append(cancel)
    if cancel.check_the_account:
        return CHECK, f"order {order_id} of OCO {alert_id}: cancel outcome unknown"
    final = _confirm_final(work, order_id)
    if final is None:
        session.guard.note_stuck(work.case.segment, order_id)
        session.ledger.note("stuck", order_id=order_id, segment=work.case.segment)
        return STUCK, f"the order {order_id} of OCO {alert_id} is not final after its cancel"
    work.status = status_of(final)
    session.guard.closed(order_id)
    fill = _uat_fill(work, final, plan, order_id)  # filled before the cancel (part or all)
    if fill is not None:
        return RECORD, f"the order {order_id} of OCO {alert_id}: {fill}"
    if work.status == "COMPLETE":
        return CHECK, f"the order {order_id} of OCO {alert_id} filled"
    if cancel.verdict not in (ACKNOWLEDGED, RESOLVED):
        problems.append(f"CancelOrder {cancel.verdict}")
    if problems:
        return FAIL, f"the order {order_id} of OCO {alert_id}: " + "; ".join(problems)
    return PASS, f"OCO {alert_id} fired its {leg} leg; its order {order_id} was cancelled"


def _final_on_sight(
    work: _Work, row: Order, plan: _Plan, order_id: str, alert_id: str
) -> tuple[str, str]:
    """A fired order already final when first seen: a UAT fill is recorded (position ledger),
    any other fill is CHECK THE ACCOUNT; a server refusal is RECORD (a SELL refused by RMS
    included: neutral to the breaker)."""
    fill = _uat_fill(work, row, plan, order_id)
    if fill is not None:
        return RECORD, f"the order {order_id} of OCO {alert_id}: {fill}"
    status = status_of(row)
    if status == "COMPLETE":
        return CHECK, f"the order {order_id} of OCO {alert_id} filled"
    message = (row.reject_reason or "").strip()
    if work.case.side == "SELL" and expect.is_rms_limit(message):
        return RECORD, f"the fired SELL order {order_id} was refused by RMS: {message}"
    detail = f": {message}" if message else ""
    return RECORD, f"the order {order_id} of OCO {alert_id} was {status} by the server{detail}"


def _uat_fill(work: _Work, row: Order, plan: _Plan, order_id: str) -> str | None:
    """On UAT (which fills at any price): a fired order's fill goes to the run's position
    ledger once per order (a SELL negative) and is recorded (``uat_filled_far``); ``None``
    elsewhere or without a fill."""
    run = work.run
    contract = run.contracts.get(ROLE)
    filled = int(row.filled_quantity or 0) or (
        int(row.quantity or 0) if status_of(row) == "COMPLETE" else 0
    )
    if not run.session.uat or contract is None or not filled:
        return None
    fills: dict[str, int] = work.notes.setdefault("uat_fills", {})
    if order_id not in fills:
        fills[order_id] = filled
        change = filled if plan.side is TransactionType.BUY else -filled
        run.positions.add_fill(contract.tradingsymbol, change, lot_size=contract.lot_size,
                               case_id=work.case.id, order_id=order_id)  # fmt: skip
        quoted = work.notes.get("place_quote")  # the snapshot the OCO was priced from
        snapshot = PriceSnapshot(**quoted) if isinstance(quoted, Mapping) else None
        fill_plan = {
            "exchange": contract.exchange,
            "transaction_type": work.case.side,
            "product": "CNC",
            "order_type": "LMT",
            "price": row.price,
        }
        work.notes[batch.UAT_FILLED_FAR] = batch.uat_fill_note(
            work.case, contract, fill_plan, snapshot, filled
        )
    return batch.uat_fill_reason(filled)


def _order_problems(row: Order, plan: _Plan) -> list[str]:
    """The fired order against the plan: symbol, quantity, side, and a price of one leg."""
    expected = {
        "tradingsymbol": (row.tradingsymbol, plan.fields["tradingsymbol"]),
        "quantity": (row.quantity, plan.fields["quantity"]),
        "transaction_type": (row.transaction_type, plan.side),
    }
    problems = [f"{k} {seen} != sent {w}" for k, (seen, w) in expected.items() if seen != w]
    if _fired_leg(row, plan) == "unknown":
        problems.append(f"price {row.price} is neither leg's")
    return problems


def _did_not_fire(work: _Work, placed: OcoOutcome, plan: _Plan, alert_id: str) -> tuple[str, str]:
    """No order of the OCO within the wait: cancel it once, confirm it gone, read the book once
    more for a late order (adopted and cancelled once); "did not fire" is RECORD, anything left
    or late is CHECK THE ACCOUNT."""
    what = f"OCO {alert_id} (tag {placed.tag}) did not fire within {TRIGGER_WAIT_SECONDS:.0f} s"
    work.notes["did_not_fire"] = True
    problem = _cancel_and_confirm(work, placed.tag, alert_id)
    late = _late_orders(work, alert_id, set())
    for row in late:
        work.notes.setdefault("fired_leg", []).append(_fired_leg(row, plan))
        _cancel_fired_order(work, row)
    details = [what]
    if problem:
        details.append(problem)
    if late:
        details.append(f"it fired late: order(s) {', '.join(str(o.order_id) for o in late)}")
    if problem or late:
        return CHECK, f"{CHECK_THE_ACCOUNT}: " + "; ".join(details)
    return RECORD, f"{what}; cancelled once and confirmed gone"


# -- the mock-session clean-up (owner decision E4) ------------------------------------------------


def mock_cleanup(run: MatrixRun) -> CaseResult:
    """E4, in a mock session only, after the OCO cases: cancel the OCOs this run placed
    (``calls.cancel_our_gtts``: ours by the ledger's alert ids and the run's tags, once each;
    never a GTT call, never another alert) and the orders those OCOs placed (ours by
    ``add_ord_id`` and the ledger, once each), then read the pending list and the order book
    again and confirm each OCO gone and each order final. Anything unconfirmed is CHECK THE
    ACCOUNT (the segment's breaker trips); nothing else in the account is ever touched. The
    session teardown repeats it for the whole run. After Ctrl-C nothing is sent (CHECK THE
    ACCOUNT: the cleanup tool); outside a mock session, or when the run placed no OCO, nothing
    is read or sent (a skip). One record, ``MOCK_CLEANUP``."""
    work = _Work(run, MOCK_CLEANUP)
    session = run.session
    if not session.mock:
        return _finish(work, SKIP, "not a mock session: the session teardown cleans up")
    if not session.ledger.oco_tags():
        return _finish(work, SKIP, "the run placed no OCO: nothing to clean up")
    if session.interrupted:
        what = "interrupted (Ctrl-C): nothing cancelled; run the cleanup tool for the run's OCOs"
        return _cleanup_check(work, [what])
    gtts = calls.cancel_our_gtts(session)
    work.notes["ocos_cancelled"] = gtts.cancelled
    work.notes["alerts_untouched"] = len(gtts.untouched)  # not the run's: never touched
    problems: list[str] = []
    if gtts.unreadable:
        problems.append("pending_gtts() could not be read: the run's OCOs are not confirmed gone")
    unconfirmed = sorted(set(gtts.left) | set(gtts.flagged))
    if unconfirmed:
        problems.append(f"OCO(s) {', '.join(unconfirmed)} of the run not confirmed gone")
    problems += _clean_oco_orders(work)
    if problems:
        return _cleanup_check(work, problems)
    return _finish(
        work,
        PASS,
        f"cancelled {len(gtts.cancelled)} OCO(s) and {len(work.order_ids)} order(s) of the run "
        "and confirmed them gone / final; nothing else touched",
    )


def _clean_oco_orders(work: _Work) -> list[str]:
    """Cancel once each open order an OCO of the run placed and confirm it final; the problems
    (CHECK THE ACCOUNT) as text."""
    session = work.run.session
    ledger = session.ledger
    alert_ids = {a for a in ledger.alerts() if ledger.alert_is_oco(a)}
    if not alert_ids:
        return []
    book = read_book(session.client, sleep=session.sleep)
    if book is None:
        return ["orders() could not be read: the orders of the run's OCOs are not confirmed"]
    problems: list[str] = []
    for row in book:
        if not _of_our_gtt(work, row, alert_ids):
            continue  # not placed by an OCO of the run: never touched
        _cancel_fired_order(work, row)
        order_id = (row.order_id or "").strip()
        if order_id not in work.order_ids:
            problems.append(f"order {order_id} of an OCO of the run could not be adopted")
    refused = work.notes.get("order_cancel_refused")
    if refused:
        problems.append(f"a cancel was refused: {refused}")
    for order_id in work.order_ids:
        if _confirm_final(work, order_id) is None:
            problems.append(f"order {order_id} of an OCO of the run is not final")
    return problems


def _cleanup_check(work: _Work, problems: list[str]) -> CaseResult:
    what = "; ".join(problems)
    work.run.session.guard.note_check_the_account(work.case.segment, what)
    return _finish(work, CHECK, f"{CHECK_THE_ACCOUNT}: {what}")


def run_case(run: MatrixRun, case: Case) -> CaseResult:
    """Run one OCO case. In a mock session only a far case, and only with the owner's OCO
    switch (``session.mock_oco``); anything else skips with nothing sent (E3, E5)."""
    session = run.session
    if session.mock and not session.mock_oco:
        return _finish(_Work(run, case), SKIP, mock_session.NO_OCO)
    if session.mock and case.id not in mock_session.MOCK_OCO_CASES:
        return _finish(_Work(run, case), SKIP, MOCK_NO_FIRE)
    if case.id in FAR_CASES:
        return run_far(run, case)
    if case.id in (FIRES_BUY.id, FIRES_SELL.id):
        return run_fires(run, case)
    if case.id == SLOTS_REVERSED.id:
        return run_slots_reversed(run, case)
    raise ValueError(f"not an OCO case: {case.id}")


def report(result: CaseResult) -> None:
    """The pytest outcome (``batch.report``)."""
    batch.report(result)
