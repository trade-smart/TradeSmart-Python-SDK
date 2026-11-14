"""UAT matrix family M12, holdings SELL (``m_hold``, owner answer U10).

The account's holdings are read first (``holdings()``, read-only); the first NSE listing with at
least one share to sell (``holdqty`` + ``dpqty`` + ``btstqty`` + ``npoadqty`` + ``benqty`` -
``usedqty``; recorded), a lot size of 1 and a quote becomes the ``nse-hold`` contract of the
session's run (lot, tick and last price from a quote of it; every order is still priced from a
fresh quote). No such holding: every case skips with the reason, nothing sent. Every order is
**one share** (a listing whose lot is not one share is never used).

- ``hold-nse-hold-cnc-lmt-day-sell`` (smoke): a CNC SELL of 1 share far **above** the market;
  it rests ``OPEN`` and the engine cancels it (once);
- ``hold-nse-hold-cnc-lmt-day-sell-fill`` (U10): a CNC SELL of 1 share at the last price (the
  UAT matching rule), followed in the book for the fill wait, the rest cancelled once; before
  15:30 IST only; only when the holdings read just before it shows at least one share. It sells
  an existing holding, so it is **not** the ``closing-sell`` case and never an xfail: the fill (or
  not) and the holdings read before and after are recorded (``supplement`` ``holdings``). A
  holdings sale is not a position of the run: the engine's entry for it in the position ledger
  is offset at once (a ``holding_sale`` line), so it is no leftover and never nets with another
  family's position on the stock (the position ceiling does not apply to it).

Both run on the session's run (``family.run_cases``); the cases are registered for resume.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from tests.live_orders.uat_matrix import batch, family
from tests.live_orders.uat_matrix import cases as case_table
from tests.live_orders.uat_matrix import contracts as contract_table
from tests.live_orders.uat_matrix.batch import BatchItem, CaseResult, MatrixRun
from tests.live_orders.uat_matrix.cases import FULL, RECORD, REST, SMOKE, Case
from tests.live_orders.uat_matrix.contracts import Contract
from tests.live_orders.uat_matrix.family_tools import (
    fill_plan,
    skipped,
    supplement,
    wait_for_fill,
)
from tests.live_orders.uat_matrix.pricing import PriceSnapshot
from tradesmart.models.portfolio import Holding, HoldingInstrument

FAMILY = "hold"
ROLE = "nse-hold"  # set on the session's run by this family only (not a contracts.ROLES role)
EXCHANGE = "NSE"
FILL_SUFFIX = "-fill"
SHARES = 1  # every holdings order sells one share
_HELD = ("holdqty", "dpqty", "btstqty", "npoadqty", "benqty")


def _case(outcome: str, suffix: str, tier: str) -> Case:
    return Case(
        id=f"{FAMILY}-{ROLE}-cnc-lmt-day-sell{suffix}",
        family=FAMILY,
        role=ROLE,
        product="CNC",
        prctyp="LMT",
        ret="DAY",
        side="SELL",
        orders=1,
        outcome=outcome,
        tier=tier,
    )


def family_cases() -> list[Case]:
    """The far SELL (smoke) and the SELL at the last price (full)."""
    return [_case(REST, "", SMOKE), _case(RECORD, FILL_SUFFIX, FULL)]


case_table.register(family_cases())  # a resumed run restores this family's state too


def sellable(holding: Holding) -> int:
    """Shares of a holding that may be sold today (the documented quantity fields)."""
    held = sum(int(getattr(holding, name) or 0) for name in _HELD)
    return held - int(holding.usedqty or 0)


@dataclass(frozen=True)
class Held:
    contract: Contract
    quantity: int
    raw: dict[str, Any]


def _listing(holding: Holding) -> HoldingInstrument | None:
    for listing in holding.instruments or []:
        exchange = (listing.exchange or "").strip().upper()
        if exchange == EXCHANGE and listing.tradingsymbol and listing.token:
            return listing
    return None


def find_held(run: MatrixRun) -> Held | str:
    """The held stock to sell, or why there is none (read-only calls only)."""
    client = run.session.client
    try:
        holdings = client.holdings()
    except contract_table.SESSION_ERRORS:
        raise
    except contract_table.DATA_ERRORS as exc:
        return f"holdings() could not be read ({type(exc).__name__}: {exc})"
    passed: list[str] = []
    for holding in holdings:
        listing = _listing(holding)
        quantity = sellable(holding)
        if listing is None or quantity < SHARES:
            continue
        name, token = str(listing.tradingsymbol), str(listing.token)
        try:
            quote = contract_table.read_quote(
                client,
                EXCHANGE,
                token,
                sleep=run.session.sleep,
                pacer=run.session.uat_quote_pacer(),
            )
        except contract_table.SESSION_ERRORS:
            raise
        except contract_table.DATA_ERRORS:
            continue
        last = quote.last_price or listing.last_price
        tick = quote.tick_size or listing.tick_size
        lot = quote.lot_size or listing.lot_size or 1
        if int(lot) != SHARES:
            passed.append(f"{name} (lot size {lot})")
            continue
        if not last or not tick or last <= 0 or tick <= 0:
            continue
        contract = Contract(
            role=ROLE, exchange=EXCHANGE, tradingsymbol=name, token=token, symbol="", kind="eq",
            lot_size=SHARES, tick_size=Decimal(tick), resolved_last_price=Decimal(last),
            resolved_lower_circuit=quote.lc, resolved_upper_circuit=quote.uc,
        )  # fmt: skip
        return Held(contract, quantity, dict(holding.raw))
    not_one = f"; not used, lot size not 1 share: {', '.join(passed)}" if passed else ""
    return (
        f"no holdings to sell: no {EXCHANGE} holding with at least one share, a lot of one "
        f"share and a last price (U10 needs a held stock on the UAT account){not_one}"
    )


def _held_quantity(run: MatrixRun, tradingsymbol: str) -> int | None:
    try:
        holdings = run.session.client.holdings()
    except contract_table.SESSION_ERRORS:
        raise
    except contract_table.DATA_ERRORS:
        return None
    for holding in holdings:
        listing = _listing(holding)
        if listing is not None and listing.tradingsymbol == tradingsymbol:
            return sellable(holding)
    return 0


def _far_plan(case: Case, contract: Contract, snapshot: PriceSnapshot) -> dict[str, Any]:
    plan = batch.default_plan(case, contract, snapshot)
    plan["quantity"] = SHARES
    return plan


def run_family(run: MatrixRun, selected: Sequence[Case]) -> dict[str, CaseResult]:
    """Run the selected M12 cases; one result per case, by id, in the order given."""
    if not selected:
        return {}
    wanted = list(selected)  # the results come back in this order
    results: dict[str, CaseResult] = {}
    # A case done earlier in the run (or this session) reports the engine's "done in run ..."
    # (or its cached result) before any holdings check: a resume never re-judges it.
    done = [c for c in selected if run.session.case_done(c.id) is not None]
    results.update(family.run_cases(run, done))
    selected = [c for c in selected if c not in done]
    if not selected:
        return {c.id: results[c.id] for c in wanted}
    held = find_held(run)
    if isinstance(held, str):
        results.update({c.id: skipped(run, c, held) for c in selected})
        return {c.id: results[c.id] for c in wanted}
    # The session's run is shared by every family; only this family uses the role.
    run.contracts.contracts[ROLE] = held.contract
    far = [c for c in selected if not c.id.endswith(FILL_SUFFIX)]
    results.update(family.run_cases(run, [BatchItem(c, plan=_far_plan) for c in far]))
    tsym = held.contract.tradingsymbol
    for case in (c for c in selected if c.id.endswith(FILL_SUFFIX)):
        before = _held_quantity(run, tsym)
        if before is None or before < SHARES:
            why = "could not be read" if before is None else f"show {before} shares"
            results[case.id] = skipped(
                run, case, f"the holdings of {tsym} {why} just before the SELL; nothing sent"
            )
            continue
        earlier = run.session.ledger.done(case.id)
        net = run.positions.net(tsym)
        with run.positions.one_at_a_time(tsym):
            plan = fill_plan(run, side="SELL", quantity=SHARES, ceiling=False)
            item = BatchItem(case, plan=plan, after_settle=wait_for_fill(), closing=True)
            result = family.run_cases(run, [item])[case.id]
            sold = net - run.positions.net(tsym)
            if sold > 0 and not earlier:  # a holdings sale is no position of the run
                run.positions.add_fill(
                    tsym, sold, lot_size=SHARES, case_id=case.id,
                    order_id=result.order_ids[0] if result.order_ids else None,
                    holding_sale=True,
                    note="offsets the engine's entry: a holdings sale is not a run position",
                )  # fmt: skip
        results[case.id] = result
        if result.order_ids and not earlier:
            supplement(run, case, "holdings", tradingsymbol=tsym, before=before,
                       after=_held_quantity(run, tsym), sold=sold,
                       holding=held.raw)  # fmt: skip
    return {c.id: results[c.id] for c in wanted}
