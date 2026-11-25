"""The GTT live test's run (pinned by tests/unit/test_live_orders_safety.py, "GTT", and dry-run on
the offline broker by tests/integration/test_gtt_live_offline_broker.py).

A GTT (good till triggered) order is an alert held on the server: when its condition becomes
true it places a real order, and **a GTT whose condition is already true when it is placed fires
at once** (owner, 2026-11-12). The live GTT test therefore runs only on UAT (UAT mode) or on the
zero-funds production account, never in funded mode, and ``run_gtt`` keeps these rules:

- the GTT: NSE ``ITC-EQ``, alert ``LTP_A_O`` (LTP greater than the trigger) with the trigger at
  3 x a fresh last price (``quote()`` read just before sending); refused before sending when the
  fresh quote has no last price or the condition would already be true. Its order: a CNC LIMIT
  BUY of 1 share well below the market (``safety.prepare_plan``'s price). The alert validity is
  ``DAY``: a GTT left behind ends with the trading day;
- room for all three calls (place, modify, cancel) in the run cap and in the test's own cap
  before anything is sent; each call is counted just before it is sent; none is ever resent;
- a unique ``sdkgtt-`` tag (``remarks``): after an accepted or ambiguous place the GTT is looked
  up in ``pending_gtts()`` by that tag or its acknowledged alert id, up to ``LOOKUPS`` times
  ``LOOKUP_PAUSE_SECONDS`` apart (the list can lag); also after an interruption other than Ctrl-C;
- the trigger is modified once to 3.1 x a fresh last price (the same checks first);
- every GTT of this run is cancelled once in a ``finally`` block: the pending rows of the tag and
  every acknowledged alert id, also one not (yet) visible. A GTT of the tag or of a known id still
  pending, unreadable state, a sign that it fired (``triggered_gtts()`` by tag, or an order of the
  tag or of its alert id in ``orders()``; a triggered row whose ``stat`` is ``CANCELLED`` in any
  letter case is recorded, not a sign: UAT lists cancelled GTTs there, spec F32), or an accepted
  or ambiguous place whose GTT was never seen in ``pending_gtts()`` and not cancelled fails with
  "CHECK THE ACCOUNT". An interruption (Ctrl-C, a test timeout) at any step prints and records a
  "CHECK THE ACCOUNT" alert.

The tag ``sdkgtt-<hex>`` is outside the suite's ``sdklive-`` prefix (the lifecycle tag check).
Records never carry the account id (the ``Gtt`` rows' ``actid`` is not recorded).
"""

from __future__ import annotations

import secrets
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import time as dtime
from decimal import Decimal
from typing import Any

from tests.live_orders import safety
from tests.live_orders.safety import (
    CHECK_THE_ACCOUNT,
    FUNDED_ENV,
    OUTSIDE_HOURS_ENV,
    WELL_BELOW,
    CheckTheAccount,
    OrderPlan,
    OrderSession,
    Refusal,
    Segment,
)
from tradesmart import OcoLeg, round_to_tick
from tradesmart.enums import GttAlertType, GttValidity, OrderType, TransactionType, Validity
from tradesmart.errors import (
    AuthenticationError,
    ConfigurationError,
    OrderRejectedError,
    OrderStatusUnknownError,
    RateLimitError,
    RequestValidationError,
)
from tradesmart.models.gtt import Gtt

GTT_TAG_PREFIX = "sdkgtt-"
LOOKUPS = 5  # after an unknown or unseen place: read pending_gtts() up to 5 times ...
LOOKUP_PAUSE_SECONDS = 2.0  # ... 2 s apart (the list can lag a little)
GTT_CALLS = 3  # place, modify and cancel: each counts against the run cap and the test's cap
PLACE_FACTOR = Decimal("3")  # the trigger: 3 x a fresh last price ...
MODIFY_FACTOR = Decimal("3.1")  # ... modified once to 3.1 x
ALERT_TYPE = GttAlertType.LTP_ABOVE  # LTP_A_O: fires when the LTP is greater than the trigger
SEGMENT = Segment("nse", "NSE", "CNC", dtime(9, 15), dtime(15, 30), tradingsymbol="ITC-EQ")
# The ``stat`` of a ``triggered_gtts()`` row of a cancelled GTT (UAT lists cancelled GTTs there,
# spec F32), compared ignoring letter case only. Any other row of ours counts as fired.
CANCELLED_STAT = "cancelled"
NOT_FUNDED = (
    "the GTT live test runs on UAT or on the zero-funds production account only "
    "(never in funded mode)"
)
# Definite failures of a GTT write (Not_Ok): nothing was created or changed.
_DEFINITE = (OrderRejectedError, AuthenticationError, RateLimitError)
# Refused by the SDK before sending (the GTT methods document no other pre-send error; any
# other exception, a ValueError too, may come after sending and counts as unknown).
_NOT_SENT = (RequestValidationError, ConfigurationError)


def new_gtt_tag() -> str:
    """The GTT's tag (``remarks``): finds it again in ``pending_gtts()``."""
    return f"{GTT_TAG_PREFIX}{secrets.token_hex(3)}"


def gtt_session(
    environ: Mapping[str, str], open_session: Callable[[], OrderSession]
) -> OrderSession:
    """The run's session for the GTT test: both order opt-ins, a serial run, and never funded
    mode (the funded flag set to any value refuses). On production (the zero-funds account) it is
    inside market hours only: the outside-hours opt-in set to any value refuses. On UAT it runs at
    any hour (owner 2026-11-13) and that opt-in is ignored. ``open_session`` (the order suite's
    session: UAT mode, or production with the account check; the zero-funds check runs in
    ``safety.prepare_plan``) is not called when the opt-ins or the funded flag already refuse."""
    reason = safety.gate_skip_reason(environ)
    if reason is not None:
        raise Refusal(reason)
    if FUNDED_ENV in environ:
        raise Refusal(f"{NOT_FUNDED}; {FUNDED_ENV} is set: skipped")
    session = open_session()
    if session.funded:
        raise Refusal(f"{NOT_FUNDED}; the session is in funded mode: skipped")
    if OUTSIDE_HOURS_ENV in environ and not session.uat:
        # production: a DAY GTT placed after the close has an undocumented lifetime
        raise Refusal(
            f"the GTT live test runs inside market hours only on production; "
            f"{OUTSIDE_HOURS_ENV} is set: skipped"
        )
    return session


def condition_already_true(
    alert_type: GttAlertType | str, last_price: Decimal, trigger: Decimal
) -> bool:
    """Whether an LTP GTT would fire at once (the trigger reached counts as true: refused too)."""
    kind = GttAlertType(alert_type)
    if kind is GttAlertType.LTP_ABOVE:
        return last_price >= trigger
    if kind is GttAlertType.LTP_BELOW:
        return last_price <= trigger
    raise Refusal(f"the GTT live test checks LTP alerts only, not {kind.value}; refusing")


def gtt_trigger(last_price: Decimal | None, tick: Decimal | None, factor: Decimal) -> Decimal:
    """``factor`` x the last price, rounded up to the tick; refused when there is no usable
    last price or tick, or when ``LTP_A_O`` would already be true (the GTT would fire at once)."""
    if last_price is None or last_price <= 0:
        raise Refusal("no fresh last price from quote(); not sending a GTT")
    if tick is None or tick <= 0:
        raise Refusal("no tick size from quote() or the script master; not sending a GTT")
    trigger = safety.ceil_to_tick(last_price * factor, tick)
    if condition_already_true(ALERT_TYPE, last_price, trigger):
        raise Refusal(
            f"{ALERT_TYPE.value} with trigger {trigger} at last price {last_price} would fire at "
            "once; not sending a GTT"
        )
    return trigger


def fresh_trigger(
    session: OrderSession,
    plan: OrderPlan,
    factor: Decimal,
    record: dict[str, Any],
    key: str,
) -> Decimal:
    """A new ``quote()`` of the plan's instrument, then ``gtt_trigger``; recorded under ``key``."""
    try:
        quote = session.client.quote(plan.exchange, plan.token)
    except Exception as exc:  # any failure means: no fresh price, so no GTT
        raise Refusal(f"quote() failed ({type(exc).__name__}); not sending a GTT") from exc
    tick = quote.tick_size if quote.tick_size is not None and quote.tick_size > 0 else None
    tick = tick if tick is not None else plan.tick_size
    seen: dict[str, Any] = {
        "lp": safety._text(quote.last_price),
        "ti": safety._text(quote.tick_size),
    }
    record[key] = seen
    trigger = gtt_trigger(quote.last_price, tick, factor)
    seen["trigger_value"] = str(trigger)
    return trigger


def gtt_kwargs(plan: OrderPlan, trigger: Decimal) -> dict[str, Any]:
    """The ``place_gtt`` / ``modify_gtt`` keywords: a far CNC LIMIT BUY behind ``LTP_A_O``."""
    return {
        "exchange": plan.exchange,
        "tradingsymbol": plan.tradingsymbol,
        "alert_type": ALERT_TYPE,
        "trigger_value": trigger,
        "quantity": plan.quantity,
        "transaction_type": TransactionType.BUY,
        "product": plan.product,
        "order_type": OrderType.LIMIT,
        "price": plan.price,
        "validity": Validity.DAY,
        "alert_validity": GttValidity.DAY,
        "tag": plan.tag,
    }


@dataclass
class GttOutcome:
    place: str = "not sent"  # accepted / rejected / unknown / not sent
    modify: str | None = None  # accepted / rejected / unknown / not sent / refused (None: none)
    alert_id: str | None = None  # the acknowledged alert id (the modify's when it answered)
    trigger: Decimal | None = None
    modified_trigger: Decimal | None = None
    placed_row: Gtt | None = None  # pending_gtts() by tag after the place
    modified_row: Gtt | None = None  # pending_gtts() by tag after the modify
    refusal: str | None = None  # why the modify was not sent
    exception: str | None = None  # what the place raised, if anything
    cancelled: list[str] = field(default_factory=list)  # alert ids whose cancel was accepted
    seen: list[str] = field(default_factory=list)  # alert ids ever seen in pending_gtts()
    left_pending: list[str] = field(default_factory=list)


def _row(gtt: Gtt) -> dict[str, Any]:
    """A pending GTT for the record (never the account id)."""
    return {
        "alert_id": gtt.alert_id,
        "alert_type": safety._text(gtt.alert_type),
        "trigger_value": safety._text(gtt.trigger_value),
        "alert_validity": safety._text(gtt.alert_validity),
        "tradingsymbol": gtt.tradingsymbol,
        "transaction_type": safety._text(gtt.transaction_type),
        "order_type": safety._text(gtt.order_type),
        "product": safety._text(gtt.product),
        "validity": safety._text(gtt.validity),
        "quantity": gtt.quantity,
        "price": safety._text(gtt.price),
    }


@dataclass
class OcoMatch:
    """How this run's OCO is recognised in ``pending_gtts()`` besides its alert id: an OCO row
    carries no top-level tag (``remarks`` is empty, F35 c), so the tag is looked for in the legs
    and, in case a leg tag does not come back, the row is matched by its tradingsymbol and both
    trigger values sent (by any place or modify of this run). OCOs already pending before the
    place are never taken for this run's."""

    tradingsymbol: str
    triggers: list[tuple[Decimal, Decimal]] = field(default_factory=list)  # (stop-loss, target)
    preexisting: set[str] = field(default_factory=set)

    def sent(self, stop_loss: Decimal, target: Decimal) -> None:
        if (stop_loss, target) not in self.triggers:
            self.triggers.append((stop_loss, target))

    def path(self, row: Gtt, tag: str) -> str | None:
        """``leg_tag`` or ``symbol_and_triggers`` when the row is this run's OCO, else ``None``."""
        if not row.is_oco or (row.alert_id or "").strip() in self.preexisting:
            return None
        if _tag_in_legs([row], tag):
            return "leg_tag"
        stop, target = row.stop_loss, row.target
        if stop is None or target is None:
            return None
        if (row.tradingsymbol or "").strip() != self.tradingsymbol:
            return None
        if (stop.trigger_value, target.trigger_value) in self.triggers:
            return "symbol_and_triggers"
        return None


def _found_path(row: Gtt, tag: str, ids: list[str], oco: OcoMatch | None) -> str | None:
    if (row.alert_id or "").strip() in ids:
        return "alert_id"
    if (row.tag or "").strip() == tag:
        return "tag"
    return oco.path(row, tag) if oco is not None else None


def _pending(
    session: OrderSession,
    tag: str,
    ids: list[str],
    entry: dict[str, Any],
    key: str,
    oco: OcoMatch | None = None,
) -> list[Gtt] | None:
    """The pending GTTs carrying ``tag`` or one of the alert ``ids`` (``None`` when
    ``pending_gtts()`` could not be read); the alert ids seen are added to ``entry["seen"]``.
    With ``oco`` an OCO row is also found by its leg tag or by its symbol and triggers, and how
    each row was found is recorded in ``entry["found_by"][key]``."""
    try:
        rows = session.client.pending_gtts()
    except Exception as exc:
        entry[key] = {"error": safety._error_record(exc)}
        return None
    if oco is not None and not oco.triggers:  # nothing sent yet: these OCOs are not this run's
        oco.preexisting.update(i for i in ((g.alert_id or "").strip() for g in rows) if i)
    paths = [(g, _found_path(g, tag, ids, oco)) for g in rows]
    found = [g for g, path in paths if path is not None]
    if oco is not None:
        found_by: dict[str, Any] = entry.setdefault("found_by", {})
        found_by[key] = {
            (g.alert_id or "").strip() or "(no alert id)": path
            for g, path in paths
            if path is not None
        }
    entry[key] = [_row(g) for g in found]
    seen: list[str] = entry.setdefault("seen", [])
    seen.extend(i for i in _ids(found) if i not in seen)
    return found


def _ids(rows: list[Gtt]) -> list[str]:
    return [i for i in ((g.alert_id or "").strip() for g in rows) if i]


def _add(known: list[str], ids: list[str]) -> None:
    for alert_id in ids:
        if alert_id not in known:
            known.append(alert_id)


def _reserve(session: OrderSession) -> None:
    assert session.test_budget is not None  # checked before any call
    session.budget.reserve()
    session.test_budget.reserve()


def _refusal_before_any_call(session: OrderSession, plan: OrderPlan) -> None:
    if session.funded:
        raise Refusal(NOT_FUNDED)
    if not plan.is_production_shape or plan.product != "CNC":
        raise Refusal(
            "the GTT's order is a CNC DAY LIMIT BUY only; refusing "
            f"{plan.product} {plan.validity} {plan.order_type} {plan.transaction_type}"
        )
    if plan.last_price is None or plan.price > plan.last_price * WELL_BELOW:
        raise Refusal(
            f"the GTT order's price {plan.price} is not well below the last price "
            f"{plan.last_price}; refusing"
        )
    if not plan.tag.startswith(GTT_TAG_PREFIX):
        raise Refusal(f"the GTT needs a {GTT_TAG_PREFIX} tag (new_gtt_tag()); refusing")
    if session.test_budget is None:
        raise Refusal("no per-test order budget (session.test_budget); refusing")
    for budget, what in ((session.budget, "order cap"), (session.test_budget, "per-test cap")):
        if budget.limit - budget.used < GTT_CALLS:
            raise Refusal(
                f"{what} reached: the GTT test needs {GTT_CALLS} calls (place, modify, cancel), "
                f"{budget.limit - budget.used} left"
            )


def _write(
    session: OrderSession, endpoint: str, entry: dict[str, Any], send: Callable[[], str]
) -> tuple[str, str | None, BaseException | None]:
    """Send one GTT write once: ``(outcome word, alert id, exception)``; interruptions re-raise."""
    _reserve(session)
    mark = session.responses.mark()
    try:
        try:
            returned = send()
        except OrderStatusUnknownError as exc:
            entry.update({"outcome": "unknown", **safety._error_record(exc)})
            return "unknown", None, exc
        except _NOT_SENT as exc:
            entry.update({"outcome": "not sent", **safety._error_record(exc)})
            return "not sent", None, exc
        except _DEFINITE as exc:
            entry.update({"outcome": "rejected", **safety._error_record(exc)})
            return "rejected", None, exc
        except Exception as exc:  # it may have reached the server
            entry.update({"outcome": "unknown", **safety._error_record(exc)})
            return "unknown", None, exc
        entry["outcome"] = "accepted"
        return "accepted", returned, None
    finally:
        safety._attach_response(entry, session.responses, endpoint, mark)


def _modify(
    session: OrderSession,
    plan: OrderPlan,
    entry: dict[str, Any],
    outcome: GttOutcome,
    known: list[str],
) -> None:
    modify: dict[str, Any] = {}
    entry["modify"] = modify
    assert outcome.alert_id is not None
    try:
        trigger = fresh_trigger(session, plan, MODIFY_FACTOR, entry, "modify_quote")
    except Refusal as exc:
        outcome.modify = "refused"
        outcome.refusal = exc.reason
        modify.update({"outcome": "refused", "reason": exc.reason})
        return
    outcome.modified_trigger = trigger
    alert_id = outcome.alert_id
    word, returned, _ = _write(
        session,
        "ModifyGTTOrder",
        modify,
        lambda: session.client.modify_gtt(alert_id, **gtt_kwargs(plan, trigger)),
    )
    outcome.modify = word
    if returned is not None:
        modify["returned_differs"] = returned != alert_id
        outcome.alert_id = returned
        _add(known, [returned])
    rows = _pending(session, plan.tag, known, entry, "after_modify")
    if rows:
        _add(known, _ids(rows))
        outcome.modified_row = rows[0] if len(rows) == 1 else None


def _look_up(
    session: OrderSession,
    plan: OrderPlan,
    entry: dict[str, Any],
    known: list[str],
    sleep: Callable[[float], None],
    oco: OcoMatch | None = None,
) -> list[Gtt] | None:
    """Read ``pending_gtts()`` after the place, up to ``LOOKUPS`` times ``LOOKUP_PAUSE_SECONDS``
    apart, until the GTT (its tag, or the acknowledged alert id) shows (the list can lag)."""
    rows: list[Gtt] | None = None
    for lookup in range(LOOKUPS):
        if lookup:
            sleep(LOOKUP_PAUSE_SECONDS)
        rows = _pending(session, plan.tag, known, entry, "after_place", oco)
        entry["place_lookups"] = lookup + 1
        if rows:
            return rows
    return rows


def _cancel_pending(
    session: OrderSession,
    plan: OrderPlan,
    entry: dict[str, Any],
    outcome: GttOutcome | OcoOutcome,
    known: list[str],
    *,
    endpoint: str,
    cancel_call: Callable[[str], str],
) -> None:
    """Cancel every GTT of this run once (never retried): the pending rows of the tag and every
    alert id known from an acknowledgement, also one not (yet) visible. Then nothing of the tag
    or of a known id may still be pending."""
    oco = outcome.match if isinstance(outcome, OcoOutcome) else None
    rows = _pending(session, plan.tag, known, entry, "before_cancel", oco)
    to_cancel: list[str] = []
    _add(to_cancel, _ids(rows or []))
    _add(to_cancel, known)
    cancels: dict[str, Any] = {}
    for alert_id in to_cancel:

        def send(alert_id: str = alert_id) -> str:
            return cancel_call(alert_id)

        cancel: dict[str, Any] = {}
        cancels[alert_id] = cancel
        try:
            word, _, _ = _write(session, endpoint, cancel, send)
        except Refusal as exc:  # the cap: not sent; the check below fails the test
            cancel.update({"outcome": "not sent", "reason": exc.reason})
            continue
        if word == "accepted":
            outcome.cancelled.append(alert_id)
    if cancels:
        entry["cancel"] = cancels
    after = _pending(session, plan.tag, to_cancel, entry, "after_cancel", oco)
    if after is None:
        raise CheckTheAccount(
            f"{CHECK_THE_ACCOUNT}: GTT tag {plan.tag}: pending_gtts() unreadable after the cancel"
        )
    outcome.left_pending = _ids(after) or (["(no alert id)"] if after else [])
    entry["left_pending"] = outcome.left_pending
    if after:
        raise CheckTheAccount(
            f"{CHECK_THE_ACCOUNT}: GTT(s) {', '.join(outcome.left_pending)} (tag {plan.tag}) "
            "still pending after one cancel request each"
        )


def cancelled_row(row: Gtt) -> bool:
    """A ``triggered_gtts()`` row of a GTT that was cancelled, not fired: its ``stat`` is
    ``CANCELLED`` in any letter case (nothing trimmed; anything else counts as fired)."""
    stat = row.stat
    return isinstance(stat, str) and stat.casefold() == CANCELLED_STAT


def carries_tag(remarks: str | None, tag: str) -> bool:
    """Whether an order's remarks carry the GTT ``tag``: exactly that tag (whitespace trimmed),
    or that tag as one exact token of the remarks split on ``": "`` (UAT writes the order a GTT
    placed as ``<ai_t>: <tag>: <text>: <tag>``, live 13 Nov 2026). Never a substring match:
    ``...-005-10`` does not carry ``...-005-1``."""
    tag = tag.strip()
    text = (remarks or "").strip()
    if not tag or not text:
        return False
    return text == tag or any(part.strip() == tag for part in text.split(": "))


def _fired(session: OrderSession, plan: OrderPlan, entry: dict[str, Any], known: list[str]) -> None:
    """A GTT of this run that fired placed a real order: CHECK THE ACCOUNT. Fails closed when
    neither ``triggered_gtts()`` nor ``orders()`` can be read."""
    signs: list[str] = []
    readable = 0
    try:
        triggered = session.client.triggered_gtts()
    except Exception as exc:
        entry["triggered_error"] = safety._error_record(exc)
    else:
        readable += 1
        ours = [
            g
            for g in triggered
            if (g.tag or "").strip() == plan.tag or (g.alert_id or "").strip() in known
        ]
        cancelled = [g for g in ours if cancelled_row(g)]
        if cancelled:
            entry["triggered_cancelled"] = [g.alert_id for g in cancelled]
        signs += [f"triggered GTT {g.alert_id}" for g in ours if not cancelled_row(g)]
    try:
        book = session.client.orders()
    except Exception as exc:
        entry["orders_error"] = safety._error_record(exc)
    else:
        readable += 1
        signs += [
            f"order {o.order_id}"
            for o in book
            if carries_tag(o.tag, plan.tag) or (o.parent_id or "").strip() in known
        ]
    if signs:
        entry["fired"] = signs
        raise CheckTheAccount(
            f"{CHECK_THE_ACCOUNT}: GTT tag {plan.tag} fired and placed a real order: "
            f"{', '.join(signs)}"
        )
    if not readable:
        raise CheckTheAccount(
            f"{CHECK_THE_ACCOUNT}: GTT tag {plan.tag}: neither triggered_gtts() nor orders() "
            "could be read, so a fired GTT cannot be ruled out"
        )


def _alert(entry: dict[str, Any], plan: OrderPlan, when: str, exc: BaseException) -> None:
    """Print and record that a GTT of the tag may be left behind (an interruption)."""
    alert = (
        f"{CHECK_THE_ACCOUNT}: a GTT with tag {plan.tag} may exist; the run was interrupted "
        f"{when} ({type(exc).__name__})"
    )
    print(alert, file=sys.stderr, flush=True)
    entry["alert"] = alert


def run_gtt(
    session: OrderSession,
    plan: OrderPlan,
    record: dict[str, Any],
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> GttOutcome:
    """Place the GTT once, read it back by tag, modify its trigger once, cancel it, and confirm
    it is gone and never fired (see the module doc).

    A refusal before sending (here, a missing fresh quote, a condition already true, or the
    SDK validation) is a ``Refusal``; anything left behind is "CHECK THE ACCOUNT", also an
    accepted or unknown place whose GTT was never seen in ``pending_gtts()`` and not
    cancelled by its id.
    """
    _refusal_before_any_call(session, plan)
    entry: dict[str, Any] = {
        "plan": plan.describe(),
        "alert_type": ALERT_TYPE.value,
        "alert_validity": GttValidity.DAY.value,
        "calls_needed": GTT_CALLS,
    }
    record["gtt"] = entry
    if _pending(session, plan.tag, [], entry, "before_place") is None:
        raise Refusal("pending_gtts() could not be read before placing; not sending a GTT")
    entry.pop("seen", None)  # nothing of this fresh tag can exist yet
    trigger = fresh_trigger(session, plan, PLACE_FACTOR, entry, "place_quote")
    outcome = GttOutcome(trigger=trigger)
    known: list[str] = []
    place: dict[str, Any] = {}
    entry["place"] = place
    interrupted = False
    try:
        try:
            word, alert_id, exc = _write(
                session,
                "PlaceGTTOrder",
                place,
                lambda: session.client.place_gtt(**gtt_kwargs(plan, trigger)),
            )
        except BaseException as base:  # Ctrl-C, SystemExit, a test timeout: a GTT may exist
            interrupted = isinstance(base, KeyboardInterrupt)
            place.update({"outcome": "interrupted", "exception": type(base).__name__})
            _alert(entry, plan, "during the place", base)
            raise
        outcome.place = word
        outcome.exception = None if exc is None else type(exc).__name__
        if alert_id is not None:
            outcome.alert_id = alert_id
            _add(known, [alert_id])
        if word in ("accepted", "unknown"):
            rows = _look_up(session, plan, entry, known, sleep)
        else:  # rejected or not sent: one look, nothing should carry the tag
            rows = _pending(session, plan.tag, known, entry, "after_place")
        if rows:
            found = [i for i in _ids(rows) if i not in known]
            if found:
                entry["found_by_tag"] = found
                _add(known, found)
            outcome.placed_row = rows[0] if len(rows) == 1 else None
        if word == "accepted" and outcome.placed_row is not None:
            _modify(session, plan, entry, outcome, known)
    except BaseException as base:
        if not isinstance(base, Exception) and "alert" not in entry:
            _alert(entry, plan, "after the place", base)
        raise
    finally:
        if not interrupted:
            try:
                _cancel_pending(
                    session,
                    plan,
                    entry,
                    outcome,
                    known,
                    endpoint="CancelGTTOrder",
                    cancel_call=lambda alert_id: session.client.cancel_gtt(alert_id),
                )
                _fired(session, plan, entry, known)
            except BaseException as base:
                if not isinstance(base, Exception):
                    _alert(entry, plan, "during the clean-up", base)
                raise
        outcome.seen = list(entry.get("seen", []))
    if outcome.place in ("accepted", "unknown") and not outcome.seen and not outcome.cancelled:
        raise CheckTheAccount(
            f"{CHECK_THE_ACCOUNT}: GTT tag {plan.tag}: PlaceGTTOrder outcome {outcome.place}, but "
            f"the GTT was never seen in pending_gtts() ({LOOKUPS} reads) and no cancel was "
            "accepted; it may exist or have fired"
        )
    if outcome.place == "not sent":
        raise Refusal(f"the SDK refused the GTT before sending: {place.get('message')}")
    return outcome


# -- OCO (one cancels the other): the same gates, its own run -----------------------------------

OCO_CALLS_PRODUCTION = 3  # place, modify, cancel: the whole production run budget (-k oco)
OCO_CALLS_UAT = 4  # ... and a modify on the cancelled id (expected: OrderRejectedError)
OCO_STOP_FACTOR = Decimal("1.10")  # the stop-loss trigger: 1.10 x a fresh last price (BUY OCO)
OCO_TARGET_FACTOR = Decimal("0.90")  # the target trigger: 0.90 x a fresh last price
OCO_MODIFY_STOP_FACTOR = Decimal("1.15")  # the stop-loss trigger is modified once to 1.15 x


@dataclass
class OcoOutcome:
    place: str = "not sent"  # accepted / rejected / unknown / not sent
    modify: str | None = None  # accepted / rejected / unknown / not sent / refused
    probe: str | None = None  # the modify on the cancelled id (UAT only)
    alert_id: str | None = None
    stop_loss: Decimal | None = None
    target: Decimal | None = None
    modified_stop_loss: Decimal | None = None
    placed_row: Gtt | None = None
    modified_row: Gtt | None = None
    match: OcoMatch | None = None  # how its rows are found besides the alert id
    refusal: str | None = None
    exception: str | None = None
    cancelled: list[str] = field(default_factory=list)
    seen: list[str] = field(default_factory=list)
    left_pending: list[str] = field(default_factory=list)


def oco_calls_needed(session: OrderSession) -> int:
    """4 writes on UAT, 3 on production."""
    return OCO_CALLS_UAT if session.uat else OCO_CALLS_PRODUCTION


def oco_triggers(
    last_price: Decimal | None, tick: Decimal | None, stop_factor: Decimal
) -> tuple[Decimal, Decimal]:
    """``(stop-loss, target)`` triggers of a BUY OCO: ``stop_factor`` x and 0.90 x the last
    price, each rounded to the tick; refused with no usable price or tick, or when the rounding
    does not leave the target below the last price and the stop-loss above it (a trigger would
    be crossed at once)."""
    if last_price is None or last_price <= 0:
        raise Refusal("no fresh last price from quote(); not sending an OCO")
    if tick is None or tick <= 0:
        raise Refusal("no tick size from quote() or the script master; not sending an OCO")
    stop = round_to_tick(last_price * stop_factor, tick)
    target = round_to_tick(last_price * OCO_TARGET_FACTOR, tick)
    if not target < last_price < stop:
        raise Refusal(
            f"OCO triggers {stop} / {target} do not bracket the last price {last_price}; "
            "not sending an OCO"
        )
    return stop, target


def _oco_fresh(
    session: OrderSession,
    plan: OrderPlan,
    stop_factor: Decimal,
    record: dict[str, Any],
    key: str,
) -> tuple[Decimal, Decimal]:
    """A new ``quote()`` of the plan's instrument, then ``oco_triggers``; recorded under ``key``."""
    try:
        quote = session.client.quote(plan.exchange, plan.token)
    except Exception as exc:  # any failure means: no fresh price, so no OCO
        raise Refusal(f"quote() failed ({type(exc).__name__}); not sending an OCO") from exc
    tick = quote.tick_size if quote.tick_size is not None and quote.tick_size > 0 else None
    tick = tick if tick is not None else plan.tick_size
    seen: dict[str, Any] = {
        "lp": safety._text(quote.last_price),
        "ti": safety._text(quote.tick_size),
    }
    record[key] = seen
    stop, target = oco_triggers(quote.last_price, tick, stop_factor)
    seen.update({"stop_loss": str(stop), "target": str(target)})
    return stop, target


def oco_kwargs(plan: OrderPlan, stop_loss: Decimal, target: Decimal) -> dict[str, Any]:
    """The ``place_oco`` / ``modify_oco`` keywords: BUY, CNC, two LIMIT legs well below the market
    (the plan's price), alert validity ``DAY`` (a left-behind OCO ends with the trading day)."""
    return {
        "exchange": plan.exchange,
        "tradingsymbol": plan.tradingsymbol,
        "transaction_type": TransactionType.BUY,
        "quantity": plan.quantity,
        "product": plan.product,
        "stop_loss": OcoLeg(trigger_value=stop_loss, price=plan.price, order_type=OrderType.LIMIT),
        "target": OcoLeg(trigger_value=target, price=plan.price, order_type=OrderType.LIMIT),
        "tag": plan.tag,
        "validity": Validity.DAY,
        "alert_validity": GttValidity.DAY,
    }


def _oco_refusal_before_any_call(session: OrderSession, plan: OrderPlan) -> None:
    if session.funded:
        raise Refusal(NOT_FUNDED)
    if not plan.is_production_shape or plan.product != "CNC":
        raise Refusal(
            "the OCO's orders are CNC DAY LIMIT BUYs only; refusing "
            f"{plan.product} {plan.validity} {plan.order_type} {plan.transaction_type}"
        )
    if plan.last_price is None or plan.price > plan.last_price * WELL_BELOW:
        raise Refusal(
            f"the OCO order price {plan.price} is not well below the last price "
            f"{plan.last_price}; refusing"
        )
    if not plan.tag.startswith(GTT_TAG_PREFIX):
        raise Refusal(f"the OCO needs a {GTT_TAG_PREFIX} tag (new_gtt_tag()); refusing")
    if session.test_budget is None:
        raise Refusal("no per-test order budget (session.test_budget); refusing")
    need = oco_calls_needed(session)
    left = session.budget.limit - session.budget.used
    if not session.uat and left != OCO_CALLS_PRODUCTION:
        raise Refusal(
            f"on production the OCO test needs the whole run budget of {OCO_CALLS_PRODUCTION} "
            f"writes ({left} left): run it on its own with -k oco"
        )
    for budget, what in ((session.budget, "order cap"), (session.test_budget, "per-test cap")):
        if budget.limit - budget.used < need:
            raise Refusal(
                f"{what} reached: the OCO test needs {need} calls, "
                f"{budget.limit - budget.used} left"
            )


def _tag_in_legs(rows: list[Gtt], tag: str) -> bool:
    """Whether the tag came back as the ``remarks`` of an order leg of a pending OCO row."""
    for row in rows:
        for leg in (row.oco_first, row.oco_second):
            if leg is not None and (leg.tag or "").strip() == tag:
                return True
    return False


def _oco_modify(
    session: OrderSession,
    plan: OrderPlan,
    entry: dict[str, Any],
    outcome: OcoOutcome,
    known: list[str],
) -> None:
    modify: dict[str, Any] = {}
    entry["modify"] = modify
    assert outcome.alert_id is not None and outcome.target is not None
    try:
        stop, _ = _oco_fresh(session, plan, OCO_MODIFY_STOP_FACTOR, entry, "modify_quote")
    except Refusal as exc:
        outcome.modify = "refused"
        outcome.refusal = exc.reason
        modify.update({"outcome": "refused", "reason": exc.reason})
        return
    outcome.modified_stop_loss = stop
    alert_id, target = outcome.alert_id, outcome.target  # the target stays as placed
    if outcome.match is not None:
        outcome.match.sent(stop, target)  # before sending: an unknown outcome may still apply
    word, returned, _ = _write(
        session,
        "ModifyOCOOrder",
        modify,
        lambda: session.client.modify_oco(alert_id, **oco_kwargs(plan, stop, target)),
    )
    outcome.modify = word
    if returned is not None:
        modify["returned_differs"] = returned != alert_id
        outcome.alert_id = returned
        _add(known, [returned])
    rows = _pending(session, plan.tag, known, entry, "after_modify", outcome.match)
    if rows:
        _add(known, _ids(rows))
        outcome.modified_row = rows[0] if len(rows) == 1 else None


def _oco_probe(
    session: OrderSession, plan: OrderPlan, entry: dict[str, Any], outcome: OcoOutcome
) -> None:
    """UAT only: one more modify on the cancelled id, expected ``OrderRejectedError``
    (``Invalid Oi``). Anything else is recorded and the OCO read again: still pending is
    CHECK THE ACCOUNT."""
    assert outcome.alert_id is not None
    assert outcome.modified_stop_loss is not None and outcome.target is not None
    probe: dict[str, Any] = {}
    entry["probe_after_cancel"] = probe
    alert_id = outcome.alert_id
    stop, target = outcome.modified_stop_loss, outcome.target
    word, returned, _ = _write(
        session,
        "ModifyOCOOrder",
        probe,
        lambda: session.client.modify_oco(alert_id, **oco_kwargs(plan, stop, target)),
    )
    outcome.probe = word
    ids: list[str] = []
    _add(ids, [alert_id])
    if returned and returned.strip():
        _add(ids, [returned.strip()])
    if word != "rejected":
        # accepted or unknown: the server may have made a new OCO (maybe under another alert id)
        rows = _pending(session, plan.tag, ids, entry, "after_probe", outcome.match)
        if rows is None or rows:
            raise CheckTheAccount(
                f"{CHECK_THE_ACCOUNT}: OCO {', '.join(ids)} (tag {plan.tag}): the modify on the "
                f"cancelled id was {word}, not rejected; pending_gtts() "
                f"{'could not be read' if rows is None else 'lists ' + ', '.join(_ids(rows))}"
            )
    _fired(session, plan, entry, ids)


def run_oco_lifecycle(
    session: OrderSession,
    plan: OrderPlan,
    record: dict[str, Any],
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> OcoOutcome:
    """Place a BUY OCO once, read it back by its alert id, modify its stop-loss trigger once,
    cancel it, confirm it is gone and never fired, and (UAT only) modify the cancelled id once
    (expected: rejected). Production runs place, modify and cancel only and needs the whole run
    budget (run it with ``-k oco``).

    A refusal before sending is a ``Refusal``; anything left behind or fired is "CHECK THE
    ACCOUNT". Nothing is ever resent.
    """
    _oco_refusal_before_any_call(session, plan)
    entry: dict[str, Any] = {
        "plan": plan.describe(),
        "alert_validity": GttValidity.DAY.value,
        "calls_needed": oco_calls_needed(session),
    }
    record["oco"] = entry
    match = OcoMatch(plan.tradingsymbol)
    if _pending(session, plan.tag, [], entry, "before_place", match) is None:
        raise Refusal("pending_gtts() could not be read before placing; not sending an OCO")
    entry.pop("seen", None)
    stop, target = _oco_fresh(session, plan, OCO_STOP_FACTOR, entry, "place_quote")
    outcome = OcoOutcome(stop_loss=stop, target=target, match=match)
    match.sent(stop, target)  # before sending: an unknown outcome may still have placed it
    known: list[str] = []
    place: dict[str, Any] = {}
    entry["place"] = place
    interrupted = False
    try:
        try:
            word, alert_id, exc = _write(
                session,
                "PlaceOCOOrder",
                place,
                lambda: session.client.place_oco(**oco_kwargs(plan, stop, target)),
            )
        except BaseException as base:  # Ctrl-C, SystemExit, a test timeout: an OCO may exist
            interrupted = isinstance(base, KeyboardInterrupt)
            place.update({"outcome": "interrupted", "exception": type(base).__name__})
            _alert(entry, plan, "during the place", base)
            raise
        outcome.place = word
        outcome.exception = None if exc is None else type(exc).__name__
        if alert_id is not None:
            outcome.alert_id = alert_id
            _add(known, [alert_id])
        if word in ("accepted", "unknown"):
            rows = _look_up(session, plan, entry, known, sleep, match)
        else:
            rows = _pending(session, plan.tag, known, entry, "after_place", match)
        if rows:
            found = [i for i in _ids(rows) if i not in known]
            if found:
                entry["found_by_tag"] = found
                _add(known, found)
            outcome.placed_row = rows[0] if len(rows) == 1 else None
            entry["tag_in_pending_row"] = _tag_in_legs(rows, plan.tag)
        if word == "accepted" and outcome.placed_row is not None:
            _oco_modify(session, plan, entry, outcome, known)
    except BaseException as base:
        if not isinstance(base, Exception) and "alert" not in entry:
            _alert(entry, plan, "after the place", base)
        raise
    finally:
        if not interrupted:
            try:
                _cancel_pending(
                    session,
                    plan,
                    entry,
                    outcome,
                    known,
                    endpoint="CancelOCOOrder",
                    cancel_call=session.client.cancel_oco,
                )
                _fired(session, plan, entry, known)
            except BaseException as base:
                if not isinstance(base, Exception):
                    _alert(entry, plan, "during the clean-up", base)
                raise
        outcome.seen = list(entry.get("seen", []))
    if outcome.place in ("accepted", "unknown") and not outcome.seen and not outcome.cancelled:
        raise CheckTheAccount(
            f"{CHECK_THE_ACCOUNT}: OCO tag {plan.tag}: PlaceOCOOrder outcome {outcome.place}, but "
            f"the OCO was never seen in pending_gtts() ({LOOKUPS} reads) and no cancel was "
            "accepted; it may exist or have fired"
        )
    if outcome.place == "not sent":
        raise Refusal(f"the SDK refused the OCO before sending: {place.get('message')}")
    if session.uat and outcome.modify == "accepted" and outcome.cancelled:
        _oco_probe(session, plan, entry, outcome)
    return outcome
