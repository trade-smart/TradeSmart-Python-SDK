"""UAT matrix family M2: modify variants (cases, the modify sequence and the family run).

Per instrument (cash, index and stock futures, index options on ITM / OTM rungs and BANKNIFTY,
the stock option, SENSEX future and option) one far LIMIT BUY and one stop BUY (``SL-LMT``)
rest, placed by the batch engine (``batch.run_batch``); its hook then modifies each order in
sequence through ``Placed.modify`` (``calls.modify``: one call per intent, never repeated,
``before=`` the order's latest row), paced like the place calls:

- LIMIT: price -1 tick, +1 tick; quantity +1 lot, -1 lot; product (expect a refusal); tag
  (record); LMT -> SL-LMT (priced from a fresh quote: a stop BUY above the market); validity
  DAY -> IOC (record: the exchange may cancel the order);
- SL-LMT: price +1 tick, -1 tick; quantity +1 lot, -1 lot; trigger +1 tick; price and trigger
  +1 tick; product (refusal); tag (record); SL-LMT -> LMT (a far BUY from a fresh quote);
  validity DAY -> IOC (record); a stop entry the book shows as a LIMIT (UAT, 18 Nov 2026)
  skips the trigger, price-trigger and to-lmt steps (recorded ``uat_stop_open`` on UAT, a
  failure elsewhere);
- then one cancel (confirmed final; one CancelOrder per order per session), then a modify of
  the cancelled order: a definitive refusal is expected and its words are recorded.

An accepted modify passes when the book shows the requested fields (and the status of a type
change), the newest ``order_history`` entry shows them and an order-stream update shows them
(bounded wait; the lag is recorded). A refusal is definitive and never retried: an RMS ``RED:``
refusal is an environment limit, a refusal where acceptance was expected fails the step, and a
step that depends on an earlier one that did not apply is skipped. An ambiguous modify or cancel
(CHECK THE ACCOUNT) stops the sequence: the order gets no further call and the case is CHECK THE
ACCOUNT. A failed step fails the case (the hook raises at the end, after every step ran); the
steps are recorded with the case (``notes.steps``; every refusal also in ``notes.modify_refusals``:
modify answers never count for the segment's circuit breaker), with the views of
``views.RecordViews`` (attached around the family's batch only). A +1 tick step on an order at
the upper circuit steps down a tick instead (or skips with the reason). ``items`` builds the
batch items; ``run_cases`` runs them through ``family.run_cases`` (the session's shared run; the
BSE / BFO canaries are ``family``'s).

Every step that moves a price stays inside the circuit band ``[lc, uc]`` of a fresh quote
(``band_price``): UAT acknowledges a modify outside the band ``Ok`` and leaves the order
unchanged (seen live, 13 Nov 2026: a far BUY clamped to ``lc`` and moved a tick below it). A -1
tick that would leave the band steps +1 tick instead (a +1 tick at ``uc`` steps down), noted with
the step; a far LIMIT BUY stays well below the last price; no tick inside the band: skipped.

Ids ``modify-<role>-<product>-<prctyp>-day-buy``; smoke: the LIMIT on NSE ``ITC``, the NIFTY
future and the SENSEX future (one per segment; BFO waits for its canary, F26).
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any

from tests.live_orders import safety
from tests.live_orders.safety import Refusal, is_final, status_of
from tests.live_orders.uat_matrix import batch, calls, expect, family, pricing, views
from tests.live_orders.uat_matrix import cases as case_table
from tests.live_orders.uat_matrix.batch import BatchItem, CaseResult, MatrixRun, Placed
from tests.live_orders.uat_matrix.cases import Case
from tests.live_orders.uat_matrix.guard import StopRun
from tests.live_orders.uat_matrix.ledger import NOT_SENT, read_book
from tradesmart.models.orders import Order

FAMILY = "modify"
ROLES = (
    "nse-b",
    "nse-t",
    "nse-e",
    "nfo-nifty-f",
    "nfo-banknifty-f",
    "nfo-stk-f",
    "nfo-nifty-ce-itm1",
    "nfo-nifty-pe-otm1",
    "nfo-banknifty-ce-atm",
    "nfo-banknifty-pe-itm1",
    "nfo-stk-ce-atm",
    "bfo-sensex-f",
    "bfo-sensex-ce-atm",
)
SMOKE_ROLES = frozenset({"nse-b", "nfo-nifty-f", "bfo-sensex-f"})
OTHER_PRODUCT = {"MIS": "CNC", "NRML": "MIS", "CNC": "MIS"}

# What a step expects, and what it came to.
ACCEPT, RECORD, REJECT = "accept", "record", "reject"
PASS, RECORDED, FAIL, ENV_LIMIT, SKIPPED, CHECK = (
    "pass",
    "record",
    "fail",
    "environment limit",
    "skipped",
    calls.CHECK,
)


def _kind(role: str) -> str:
    if role.endswith("-f"):
        return "fut"
    return "opt" if "-ce-" in role or "-pe-" in role else "eq"


def _case(role: str, prctyp: str) -> Case:
    product = case_table.BASE_PRODUCT[_kind(role)]
    return Case(
        id=f"{FAMILY}-{role}-{product}-{prctyp}-day-buy".lower(),
        family=FAMILY,
        role=role,
        product=product,
        prctyp=prctyp,
        ret="DAY",
        side="BUY",
        orders=1,
        outcome=case_table.REST if prctyp == "LMT" else case_table.TRIGGER_PENDING,
        tier=case_table.SMOKE if role in SMOKE_ROLES and prctyp == "LMT" else case_table.FULL,
    )


CASES: list[Case] = [_case(role, prctyp) for role in ROLES for prctyp in ("LMT", "SL-LMT")]


@dataclass(frozen=True)
class Timing:
    """Real-time waits for the order stream (the engine's book waits use the session clock)."""

    stream_wait: float = views.STREAM_WAIT
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    interval: float = 0.1


# -- the order's state ------------------------------------------------------------------------


@dataclass(frozen=True)
class State:
    """The modify fields of the order as last seen (the book is the source of truth)."""

    price: Decimal | None
    trigger_price: Decimal | None
    quantity: int
    order_type: str  # LMT | SL-LMT
    product: str  # CNC | MIS | NRML
    validity: str  # DAY | IOC | EOS

    @classmethod
    def of_plan(cls, plan: Mapping[str, Any]) -> State:
        trigger = plan.get("trigger_price")
        return cls(
            price=Decimal(str(plan["price"])),
            trigger_price=None if trigger is None else Decimal(str(trigger)),
            quantity=int(plan["quantity"]),
            order_type=str(plan["order_type"]),
            product=str(plan["product"]),
            validity=str(plan.get("validity") or "DAY"),
        )

    def seen(self, row: Order | None) -> State:
        """This state with the fields the book row shows."""
        if row is None:
            return self
        order_type = views.field_value(row, "order_type")
        stop = (order_type or views.normalize("order_type", self.order_type)) == "SL"
        trigger = row.trigger_price if stop and row.trigger_price else None
        return State(
            price=row.price if row.price is not None else self.price,
            trigger_price=trigger if stop else None,
            quantity=row.quantity or self.quantity,
            order_type=_wire_type(order_type) or self.order_type,
            product=views.field_value(row, "product") or self.product,
            validity=views.field_value(row, "validity") or self.validity,
        )

    def changes(self) -> dict[str, Any]:
        return {
            "price": self.price,
            "trigger_price": self.trigger_price,
            "quantity": self.quantity,
            "order_type": self.order_type,
            "product": self.product,
            "validity": self.validity,
        }


def _wire_type(name: Any) -> str | None:
    return {"LIMIT": "LMT", "SL": "SL-LMT", "MARKET": "MKT", "SL_M": "SL-MKT"}.get(str(name))


# -- steps ------------------------------------------------------------------------------------


class _Skip(Exception):
    """A step that cannot be built now (nothing to take back, no fresh quote)."""


@dataclass
class _Ctx:
    placed: Placed
    timing: Timing
    state: State
    row: Order | None
    steps: list[dict[str, Any]] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    applied: dict[str, bool] = field(default_factory=dict)  # step -> its modify took effect
    refusals: list[dict[str, Any]] = field(default_factory=list)  # every modify refusal
    note: str | None = None  # what a step builder changed from the plain step

    @property
    def tick(self) -> Decimal:
        return self.placed.contract.tick_size

    @property
    def lot(self) -> int:
        return self.placed.contract.lot_size


@dataclass(frozen=True)
class Step:
    name: str
    expect: str
    build: Callable[[_Ctx], State | tuple[State, str]]  # the target (and a new tag)
    status: str | None = None  # the status a type change leads to


def band_price(
    price: Decimal, ticks: int, tick: Decimal, snapshot: pricing.PriceSnapshot, *, far_buy: bool
) -> tuple[Decimal, str | None]:
    """``price`` moved by ``ticks`` (+1 / -1), inside the circuit band ``[lc, uc]`` of the fresh
    ``snapshot``; a move that would leave the band goes the other way instead (UAT acknowledges
    a modify outside the band ``Ok`` and leaves the order unchanged). ``far_buy`` (a resting
    LIMIT BUY): the price stays well below the last price (``safety.WELL_BELOW``). Returns the
    price and a note when it was turned; ``Refusal`` when no tick fits."""
    lower, upper = snapshot.lower_circuit, snapshot.upper_circuit

    def beyond(value: Decimal) -> str | None:
        if value <= 0 or (lower is not None and value < lower):
            return f"the lower circuit {lower}"
        if upper is not None and value > upper:
            return f"the upper circuit {upper}"
        return None

    target, note = price + ticks * tick, None
    bound = beyond(target)
    if bound is not None:
        turned = price - ticks * tick
        if beyond(turned) is not None:
            raise Refusal(f"no tick of {price} inside the band [{lower}, {uc_text(upper)}]")
        way = "up" if ticks < 0 else "down"
        note = f"{target} is beyond {bound}: stepped {way} a tick instead"
        target = turned
    if far_buy and target > snapshot.last_price * safety.WELL_BELOW:
        raise Refusal(
            f"a BUY limit of {target} is not well below the last price {snapshot.last_price}"
        )
    return target, note


def uc_text(upper: Decimal | None) -> str:
    return "none" if upper is None else str(upper)


def _price(ticks: int) -> Callable[[_Ctx], State]:
    def build(ctx: _Ctx) -> State:
        assert ctx.state.price is not None
        snapshot = _snapshot(ctx)  # the band from a fresh quote
        far = ctx.state.order_type == "LMT" and ctx.placed.case.side == "BUY"
        try:
            price, ctx.note = band_price(ctx.state.price, ticks, ctx.tick, snapshot, far_buy=far)
        except Refusal as exc:
            raise _Skip(exc.reason) from exc
        trigger = ctx.state.trigger_price
        if trigger is not None and price < trigger:
            raise _Skip(f"a limit of {price} would be below the trigger {trigger}")
        return replace(ctx.state, price=price)

    return build


def _qty_up(ctx: _Ctx) -> State:
    return replace(ctx.state, quantity=ctx.state.quantity + ctx.lot)


def _qty_down(ctx: _Ctx) -> State:
    if not ctx.applied.get("qty-up") or ctx.state.quantity - ctx.lot < ctx.lot:
        raise _Skip("qty-up did not take effect: nothing to take back")
    return replace(ctx.state, quantity=ctx.state.quantity - ctx.lot)


def _trigger(ctx: _Ctx) -> State:
    trigger, price = ctx.state.trigger_price, ctx.state.price
    assert trigger is not None and price is not None
    if trigger + ctx.tick > price:
        raise _Skip(f"no room between the trigger {trigger} and the limit {price}")
    return replace(ctx.state, trigger_price=trigger + ctx.tick)


def _price_trigger(ctx: _Ctx) -> State:
    trigger, price = ctx.state.trigger_price, ctx.state.price
    assert trigger is not None and price is not None
    upper = _snapshot(ctx).upper_circuit  # the band from a fresh quote
    if upper is not None and price + ctx.tick > upper:
        raise _Skip(f"the limit {price} is at the upper circuit {upper}: no tick above it")
    return replace(ctx.state, price=price + ctx.tick, trigger_price=trigger + ctx.tick)


def _product(ctx: _Ctx) -> State:
    return replace(ctx.state, product=OTHER_PRODUCT.get(ctx.state.product, "MIS"))


def _tag(ctx: _Ctx) -> tuple[State, str]:
    return ctx.state, ctx.placed.run.session.ledger.new_tag(ctx.placed.case.id)


def _snapshot(ctx: _Ctx) -> pricing.PriceSnapshot:
    try:
        return pricing.fresh_quote(
            ctx.placed.run.session.client,
            ctx.placed.contract,
            uat=ctx.placed.run.session.uat,
            sleep=ctx.placed.run.session.sleep,
            pacer=ctx.placed.run.session.uat_quote_pacer(),
        )
    except Refusal as exc:
        raise _Skip(exc.reason) from exc


def _to_stop(ctx: _Ctx) -> State:
    snapshot = _snapshot(ctx)
    try:
        trigger, limit = pricing.stop_buy(snapshot, option=ctx.placed.contract.kind == "opt")
    except Refusal as exc:
        raise _Skip(exc.reason) from exc
    return replace(ctx.state, order_type="SL-LMT", price=limit, trigger_price=trigger)


def _to_limit(ctx: _Ctx) -> State:
    snapshot = _snapshot(ctx)
    try:
        price = pricing.far_buy(snapshot, option=ctx.placed.contract.kind == "opt")
    except Refusal as exc:
        raise _Skip(exc.reason) from exc
    return replace(ctx.state, order_type="LMT", price=price, trigger_price=None)


def _ioc(ctx: _Ctx) -> State:
    return replace(ctx.state, validity="IOC")


LMT_STEPS = (
    Step("price-down", ACCEPT, _price(-1)),
    Step("price-up", ACCEPT, _price(+1)),
    Step("qty-up", ACCEPT, _qty_up),
    Step("qty-down", ACCEPT, _qty_down),
    Step("product", REJECT, _product),
    Step("tag", RECORD, _tag),
    Step("to-sl-lmt", ACCEPT, _to_stop, status="TRIGGER_PENDING"),
    Step("validity-ioc", RECORD, _ioc),
)
SL_STEPS = (
    Step("price-up", ACCEPT, _price(+1)),
    Step("price-down", ACCEPT, _price(-1)),
    Step("qty-up", ACCEPT, _qty_up),
    Step("qty-down", ACCEPT, _qty_down),
    Step("trigger", ACCEPT, _trigger),
    Step("price-trigger", ACCEPT, _price_trigger),
    Step("product", REJECT, _product),
    Step("tag", RECORD, _tag),
    Step("to-lmt", ACCEPT, _to_limit, status="OPEN"),
    Step("validity-ioc", RECORD, _ioc),
)
VISIBLE_FIELDS = ("price", "trigger_price", "quantity", "order_type")
RECORDED_FIELDS = {"product": ("product",), "validity-ioc": ("validity",), "tag": ("tag",)}


# -- checks (read-only) -----------------------------------------------------------------------


def shows(view: Any, wanted: Mapping[str, Any]) -> bool:
    return all(
        views.field_value(view, name) == views.normalize(name, value)
        for name, value in wanted.items()
        if value is not None
    )


def _wanted(step: Step, target: State, tag: str | None) -> dict[str, Any]:
    names = RECORDED_FIELDS.get(step.name, VISIBLE_FIELDS)
    values = {**target.changes(), "tag": tag}
    return {name: values[name] for name in names}


def _poll(ctx: _Ctx, read: Callable[[], Any], done: Callable[[Any], bool]) -> Any:
    """Repeat a read-only ``read`` until ``done`` (bounded by ``confirm_seconds``)."""
    session = ctx.placed.run.session
    deadline = session.clock() + session.confirm_seconds
    last: Any = None
    while True:
        with contextlib.suppress(Exception):  # a read; the next one decides
            last = read()
        if last is not None and done(last):
            return last
        if session.clock() >= deadline:
            return last
        session.sleep(session.poll_interval)


def _book_row(ctx: _Ctx) -> Order | None:
    order_id = ctx.placed.order_id
    book = read_book(ctx.placed.run.session.client, attempts=1)
    return next((o for o in book or [] if (o.order_id or "").strip() == order_id), None)


def _await_row(ctx: _Ctx, done: Callable[[Order], bool]) -> Order | None:
    row = _poll(ctx, lambda: _book_row(ctx), done)
    return row if isinstance(row, Order) else None


def _verify(
    ctx: _Ctx,
    wanted: Mapping[str, Any],
    status: str | None,
    before_updates: int,
    *,
    strict: bool = True,
) -> dict[str, Any]:
    """Book, newest history entry and a stream update show ``wanted`` (and ``status``).

    ``strict`` (an accepted modify that must show): each view is waited for (bounded) until it
    shows the change. Otherwise (a recorded answer): the book and the history are read once and
    the first new stream update is taken, and whether each shows the change is recorded."""
    order_id = ctx.placed.order_id
    assert order_id is not None
    client = ctx.placed.run.session.client
    problems: list[str] = []

    def shown(view: Any) -> bool:
        return shows(view, wanted) and (status is None or status_of(view) == status)

    if strict:
        row = _await_row(ctx, shown)
        history = _poll(
            ctx, lambda: client.order_history(order_id), lambda h: bool(h) and shown(h[0])
        )
    else:
        row = _book_row(ctx)
        history = _poll(ctx, lambda: client.order_history(order_id), lambda h: True)
    book_ok = row is not None and shown(row)
    if not book_ok:
        problems.append(f"book: {describe(row)} does not show {_text(wanted, status)}")
    if row is not None:
        ctx.row = row
    history_ok = bool(history) and shown(history[0])
    if not history_ok:
        newest = describe(history[0]) if history else "no entry"
        problems.append(f"history: newest {newest} does not show {_text(wanted, status)}")
    lag: float | None = None
    stream_ok = False
    watcher = ctx.placed.run.watcher
    if watcher is None:
        problems.append("stream: no order stream attached")
    else:
        lag, update = views.wait_for_update(
            watcher, order_id, shown if strict else (lambda u: True), after=before_updates,
            timeout=ctx.timing.stream_wait, clock=ctx.timing.clock, sleep=ctx.timing.sleep,
            interval=ctx.timing.interval,
        )  # fmt: skip
        stream_ok = update is not None and shown(update)
        if not stream_ok:
            seen = "no update" if update is None else f"the update {describe(update)}"
            problems.append(
                f"stream: {seen} within {ctx.timing.stream_wait:g} s does not show "
                f"{_text(wanted, status)}"
            )
    return {
        "book": "ok" if book_ok else "differs",
        "history": "ok" if history_ok else "differs",
        "stream": "ok" if stream_ok else "differs",
        "stream_lag": lag,
        "problems": problems,
    }


def describe(view: Any) -> str:
    if view is None:
        return "nothing"
    fields = {name: views.field_value(view, name) for name in (*VISIBLE_FIELDS, "validity")}
    return f"{status_of(view)} {fields}"


def _text(wanted: Mapping[str, Any], status: str | None) -> str:
    shown = {k: str(v) for k, v in wanted.items() if v is not None}
    return f"{shown}" + (f" and {status}" if status else "")


# -- the sequence -----------------------------------------------------------------------------


class _Stop(Exception):
    """An unknown outcome (CHECK THE ACCOUNT): no further call for this order."""


def _message(outcome: calls.OrderOutcome) -> str:
    error = outcome.error or {}
    return str(error.get("emsg") or error.get("message") or "")


def _updates(ctx: _Ctx) -> int:
    watcher, order_id = ctx.placed.run.watcher, ctx.placed.order_id
    return 0 if watcher is None or order_id is None else len(watcher.updates(order_id))


def running(session: Any) -> None:
    """After Ctrl-C (the session is interrupted) nothing more is sent, not even a cancel."""
    if session.interrupted:
        raise StopRun(batch.INTERRUPTED_REASON)


def _modify_step(ctx: _Ctx, step: Step) -> None:
    running(ctx.placed.run.session)
    entry: dict[str, Any] = {"step": step.name, "expect": step.expect}
    ctx.steps.append(entry)
    ctx.note = None
    try:
        built = step.build(ctx)
    except _Skip as exc:
        entry.update(result=SKIPPED, reason=str(exc))
        return
    if ctx.note is not None:
        entry["note"] = ctx.note
    target, tag = built if isinstance(built, tuple) else (built, None)
    changes = target.changes() | ({"tag": tag} if tag is not None else {})
    entry["changes"] = changes
    before = _updates(ctx)
    ctx.placed.run.pacer.wait(place=False)  # modifies share the per-second pace only (D3)
    ctx.placed.row = ctx.row  # before= for an ambiguous outcome: the latest row
    outcome = ctx.placed.modify(**changes)
    entry.update(verdict=outcome.verdict, words=_message(outcome))
    if outcome.check_the_account:
        entry["result"] = CHECK
        raise _Stop
    if outcome.verdict == NOT_SENT:
        entry["result"] = FAIL
        ctx.failures.append(f"{step.name}: refused by the SDK, nothing sent ({entry['words']})")
        _note_refusal(ctx, entry)
        return
    if outcome.verdict == calls.REJECTED:
        _refused(ctx, step, entry)
        return
    ctx.applied[step.name] = True
    strict = step.expect == ACCEPT
    check = _verify(ctx, _wanted(step, target, tag), step.status, before, strict=strict)
    entry.update(check)
    ctx.state = target if check["book"] == "ok" else ctx.state.seen(ctx.row)
    uat = ctx.placed.run.session.uat
    if step.expect == ACCEPT and check["problems"] and uat and step.name in UAT_RECORDED_STEPS:
        entry["result"] = RECORDED  # UAT (13 Nov 2026): acknowledged Ok, not applied
        entry["note"] = UAT_RECORDED_STEPS[step.name]
        ctx.applied[step.name] = check["book"] == "ok"
    elif step.expect == ACCEPT:
        entry["result"] = PASS if not check["problems"] else FAIL
        if check["problems"]:
            ctx.applied[step.name] = check["book"] == "ok"
            ctx.failures.append(f"{step.name}: {'; '.join(check['problems'])}")
    else:  # recorded: the answer, and whether every view shows it
        entry["result"] = RECORDED
        if step.expect == REJECT:
            entry["reason"] = "acknowledged although a refusal was expected"


def _note_refusal(ctx: _Ctx, entry: Mapping[str, Any]) -> None:
    """A modify refusal is kept with the case (``notes.modify_refusals``); it is not a place
    outcome, so it never counts for the segment's circuit breaker (controller ruling)."""
    ctx.refusals.append({k: entry.get(k) for k in ("step", "expect", "verdict", "words", "result")})


def _refused(ctx: _Ctx, step: Step, entry: dict[str, Any]) -> None:
    words = entry["words"]
    if expect.is_rms_limit(words) or expect.environment_refusal(words) is not None:
        entry["result"] = ENV_LIMIT
    elif step.expect == REJECT:
        entry["result"] = PASS
    elif step.expect == RECORD:
        entry["result"] = RECORDED
    else:
        entry["result"] = FAIL
        ctx.failures.append(f"{step.name}: refused ({words})")
    _note_refusal(ctx, entry)


def _cancel_step(ctx: _Ctx) -> bool:
    """One cancel, confirmed final; ``True`` when the order is final afterwards."""
    running(ctx.placed.run.session)
    entry: dict[str, Any] = {"step": "cancel", "expect": ACCEPT}
    ctx.steps.append(entry)
    placed = ctx.placed
    order_id = placed.order_id
    assert order_id is not None
    row = _book_row(ctx) or ctx.row
    if is_final(status_of(row)):
        entry.update(result=SKIPPED, reason=f"already {status_of(row)} (validity-ioc)")
        ctx.row = row
        return True
    before = _updates(ctx)
    placed.run.pacer.wait(place=False)
    outcome = calls.cancel(placed.run.session, order_id, placed.ref)
    placed.calls.append(outcome)
    entry.update(verdict=outcome.verdict, words=_message(outcome))
    if outcome.check_the_account:
        entry["result"] = CHECK
        raise _Stop
    if outcome.verdict not in (calls.ACKNOWLEDGED, calls.RESOLVED):
        entry["result"] = FAIL
        ctx.failures.append(f"cancel: {outcome.verdict} ({entry['words']})")
        return False
    final = _await_row(ctx, lambda r: is_final(status_of(r)))
    if final is None or not is_final(status_of(final)):
        entry.update(result=FAIL, book=describe(final))
        ctx.failures.append(f"cancel: not final after its cancel ({describe(final)})")
        return False
    ctx.row = final
    check = _verify(ctx, {}, status_of(final), before)
    entry.update(check, result=PASS if not check["problems"] else FAIL)
    if check["problems"]:
        ctx.failures.append(f"cancel: {'; '.join(check['problems'])}")
    return True


def _after_cancel(ctx: _Ctx) -> None:
    """A modify of the final order: refused in the reply, or acknowledged and then judged from the
    order history (``_judge_after_cancel``)."""
    running(ctx.placed.run.session)
    entry: dict[str, Any] = {"step": "modify-after-cancel", "expect": REJECT}
    ctx.steps.append(entry)
    state = ctx.state.seen(ctx.row)
    assert state.price is not None
    try:  # inside the band of a fresh quote: UAT acknowledges a modify outside it
        snapshot = _snapshot(ctx)
        price, note = band_price(state.price, -1, ctx.tick, snapshot, far_buy=False)
    except (_Skip, Refusal) as exc:
        entry.update(result=SKIPPED, reason=str(getattr(exc, "reason", exc)))
        return
    if note is not None:
        entry["note"] = note
    target = replace(state, price=price)
    entry["changes"] = target.changes()
    before = _history_or_none(ctx)  # the entries so far: what the modify adds is judged
    ctx.placed.run.pacer.wait(place=False)
    ctx.placed.row = ctx.row
    outcome = ctx.placed.modify(**target.changes())
    entry.update(verdict=outcome.verdict, words=_message(outcome))
    if outcome.check_the_account:
        entry["result"] = CHECK
        raise _Stop
    if outcome.verdict == calls.REJECTED:
        entry["result"] = PASS
        _note_refusal(ctx, entry)
        return
    if outcome.verdict not in (calls.ACKNOWLEDGED, calls.RESOLVED):
        entry["result"] = FAIL
        ctx.failures.append(
            f"modify-after-cancel: {outcome.verdict} although the order is {status_of(ctx.row)}"
        )
        return
    _judge_after_cancel(ctx, entry, before)


def _history_or_none(ctx: _Ctx) -> list[Any] | None:
    order_id = ctx.placed.order_id
    assert order_id is not None
    try:
        return list(ctx.placed.run.session.client.order_history(order_id))
    except Exception:  # a read-only call; unreadable means unverified
        return None


def _after_cancel_history(ctx: _Ctx, before: list[Any]) -> list[Any] | None:
    """The history once it shows an entry newer than ``before``, or after the settle time with
    whatever it shows; ``None`` when it cannot be read."""
    session = ctx.placed.run.session
    deadline = session.clock() + AFTER_CANCEL_SETTLE_SECONDS
    while True:
        history = _history_or_none(ctx)
        if history is None or len(history) > len(before):
            return history
        if session.clock() >= deadline:
            return history
        session.sleep(session.poll_interval)


def _judge_after_cancel(ctx: _Ctx, entry: dict[str, Any], before: list[Any] | None) -> None:
    """An ``Ok`` reply only acknowledges the request; the order history shows what became of it."""
    history = None if before is None else _after_cancel_history(ctx, before)
    if before is None or history is None or not before:
        entry.update(result=RECORDED, note=AFTER_CANCEL_UNVERIFIED_NOTE)
        return
    added = history[: max(len(history) - len(before), 0)]  # newest first
    refusals = [e for e in added if status_of(e) == "REJECTED" or (e.reject_reason or "").strip()]
    state = next((e for e in added if e not in refusals), before[0])
    book = _book_row(ctx)
    latest = state if book is None else book
    changed = status_of(latest) != "CANCELED" or any(
        views.field_value(latest, name) != views.field_value(before[0], name)
        for name in ("price", "trigger_price", "quantity")
    )
    if changed:
        entry.update(result=FAIL, book=describe(latest), history=describe(history[0]))
        ctx.failures.append("modify-after-cancel: the cancelled order changed")
    elif refusals:
        words = "; ".join(str(e.reject_reason or "").strip() or str(status_of(e)) for e in refusals)
        entry.update(result=PASS, history_words=words)
        _note_refusal(ctx, {**entry, "words": words})
    else:
        entry.update(result=PASS, note=AFTER_CANCEL_IGNORED_NOTE)


PRICE_DOWN = LMT_STEPS[0]
# UAT oddities (full live run, 13 Nov 2026): recorded on UAT, never a failure; production unchanged.
UAT_RECORDED_STEPS = {
    "qty-up": "UAT: a quantity increase is acknowledged Ok but not shown in the book or history",
}
# A modify of a CANCELED order: ``Ok`` only acknowledges it; the order history shows the outcome.
AFTER_CANCEL_SETTLE_SECONDS = 5.0
AFTER_CANCEL_IGNORED_NOTE = "modify_after_cancel_ignored"
AFTER_CANCEL_UNVERIFIED_NOTE = "modify_after_cancel_unverified"


# Steps that need a stop order (a trigger): skipped when the stop entry was booked as a LIMIT.
STOP_ONLY_STEPS = frozenset({"trigger", "price-trigger", "to-lmt"})


def _stored_as_limit(placed: Placed, state: State) -> str | None:
    """Why the stop steps cannot run when a stop entry rests as a LIMIT in the book (UAT, live
    18 Nov 2026: placed ``SL-LMT``, booked ``LMT`` / ``OPEN`` with its trigger kept, N9 (e));
    ``None`` for a stop that stayed a stop and for a LIMIT entry. On UAT the engine records the
    case ``uat_stop_open``; elsewhere the case fails with this reason."""
    if placed.case.prctyp != "SL-LMT" or state.order_type == "SL-LMT":
        return None
    return (
        f"the stop order is stored as a LIMIT ({describe(placed.row)}), not a stop: "
        f"no trigger to modify ({batch.UAT_STOP_OPEN})"
    )


@dataclass
class ModifySequence:
    """The ``after_settle`` hook of one M2 case (see the module docstring). ``steps`` overrides
    the sequence of the order type; ``after_cancel=False`` ends with the confirmed cancel (the
    views family's OPEN -> modified -> CANCELED path)."""

    timing: Timing = field(default_factory=Timing)
    steps: tuple[Step, ...] | None = None
    after_cancel: bool = True

    def __call__(self, placed: Placed) -> None:
        ctx = _Ctx(placed, self.timing, State.of_plan(placed.plan), placed.row)
        placed.notes["steps"] = ctx.steps
        placed.notes["modify_refusals"] = ctx.refusals
        ctx.state = ctx.state.seen(placed.row)
        steps = self.steps
        if steps is None:
            steps = LMT_STEPS if placed.case.prctyp == "LMT" else SL_STEPS
        stored = _stored_as_limit(placed, ctx.state)
        if stored is not None and not placed.run.session.uat:
            ctx.failures.append(stored)  # off UAT a stop must stay a stop; on UAT: uat_stop_open
        try:
            for step in steps:
                if stored is not None and step.name in STOP_ONLY_STEPS:
                    ctx.steps.append(
                        {"step": step.name, "expect": step.expect, "result": SKIPPED,
                         "reason": stored}
                    )  # fmt: skip
                    continue
                _modify_step(ctx, step)
            final = _cancel_step(ctx)
            if final and self.after_cancel:
                _after_cancel(ctx)
            elif self.after_cancel:
                ctx.steps.append(
                    {"step": "modify-after-cancel", "expect": REJECT, "result": SKIPPED,
                     "reason": "the order is not final"}
                )  # fmt: skip
        except _Stop:
            return  # CHECK THE ACCOUNT: the engine judges the case from the outcome
        if ctx.failures:
            raise AssertionError("modify steps failed: " + " | ".join(ctx.failures))


# -- the family's batch -----------------------------------------------------------------------


@contextmanager
def views_attached(run: MatrixRun, timing: Timing) -> Iterator[None]:
    """Around one family batch: every record of it gets its orders' views and the raw bodies
    of its calls (a fresh ``views.RecordViews``, its positions baseline read now); the run's
    previous ``enrich`` is restored afterwards (the run is shared by every family)."""
    session = run.session
    previous = run.recorder.enrich
    run.recorder.enrich = views.RecordViews(
        session.client, session.ledger.path, watcher=run.watcher,
        stream_wait=timing.stream_wait, clock=timing.clock, sleep=timing.sleep,
    )  # fmt: skip
    try:
        yield
    finally:
        run.recorder.enrich = previous


def items(selected: Iterable[Case], *, timing: Timing | None = None) -> list[BatchItem]:
    """The batch items of the selected M2 cases (each with the modify sequence hook)."""
    hook = ModifySequence(timing if timing is not None else Timing())
    return [BatchItem(case, after_settle=hook) for case in selected]


def run_cases(
    run: MatrixRun, selected: Iterable[Case], *, timing: Timing | None = None
) -> dict[str, CaseResult]:
    """The selected M2 cases through ``family.run_cases`` (one batch, the canaries it needs,
    each case at most once per run), with the views attached around the batch."""
    timing = timing if timing is not None else Timing()
    with views_attached(run, timing):
        return family.run_cases(run, items(selected, timing=timing))
