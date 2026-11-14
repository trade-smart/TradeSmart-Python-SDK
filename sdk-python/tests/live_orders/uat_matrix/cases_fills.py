"""UAT matrix family M4, fills and closes (``m_fills``): how its cases run.

The cases live in ``cases.py`` (family ``fills``):

- **fill and close** (``fill_close``): a BUY at the last price per product and segment (NSE-B CNC
  and MIS, NFO-F NRML and MIS, NFO-CE MIS and NRML, NFO-SF MIS, BFO-CE MIS), then a closing SELL
  at the last price. The batch engine's fill phase runs them: one at a time per instrument,
  before 15:30 IST, within the per-instrument position ceiling (2 lots); a closing SELL that does
  not fill is the non-strict xfail ``closing-sell`` and the position stays in the run's position
  ledger;
- **short and cover** (``short_cover``): a MIS SELL at the last price to open, then a covering
  BUY, on NFO-F, NFO-CE and NSE-B (recorded);
- **the partial-fill probe** (recorded): a BUY of ``PARTIAL_LOTS`` (10) lots at the last price
  on ``PARTIAL_ROLE`` (BANKNIFTY future, used by no other fill case), sized down to the room left
  under the position ceiling (with 2 lots per instrument it is never more than 2 lots; the
  sizing is recorded), its fill progress followed in the book for the fill wait, the rest
  cancelled once; then its closing SELL of what filled (recorded, not an xfail: the engine's
  ``closing-sell`` rule belongs to the fill phase).

Every case runs on the session's run (``family.run_cases``: once per run; the BFO canary first
while BFO is undecided, U9). After each batch that sent orders, a read-only ``views``
supplement per case records its ``trades()`` rows and the ``positions()`` rows of its instrument
and product before and after the batch.
"""

from __future__ import annotations

import contextlib
from collections.abc import Mapping, Sequence
from typing import Any

from tests.live_orders.uat_matrix import batch, family
from tests.live_orders.uat_matrix import cases as case_table
from tests.live_orders.uat_matrix import contracts as contract_table
from tests.live_orders.uat_matrix.batch import BatchItem, CaseResult, MatrixRun
from tests.live_orders.uat_matrix.cases import (
    PARTIAL_CLOSE_SUFFIX,
    PARTIAL_LOTS,
    PARTIAL_ROLE,
    PARTIAL_SUFFIX,
    Case,
)
from tests.live_orders.uat_matrix.contracts import Contract
from tests.live_orders.uat_matrix.family_tools import fill_plan, skipped, supplement, wait_for_fill

FAMILY = "fills"
Positions = dict[str, dict[str, Any]]
# The views are read-only records: any read failure (an expired session too) is noted in them;
# the next batch meets a session that is not usable on its own (the engine stops the run).
VIEW_ERRORS = (*contract_table.SESSION_ERRORS, *contract_table.DATA_ERRORS)


def family_cases() -> list[Case]:
    """Every M4 case, in table order."""
    return [c for c in case_table.CASES if c.family == FAMILY]


# -- read-only views after a batch --------------------------------------------------------------


def _key(tradingsymbol: Any, product: Any) -> str:
    return f"{tradingsymbol}/{getattr(product, 'name', product)}"


def _positions(run: MatrixRun) -> Positions | str:
    """``positions()`` rows by ``<tsym>/<product>`` (read-only), or why it could not be read."""
    try:
        rows = run.session.client.positions()
    except VIEW_ERRORS as exc:  # read-only: recorded, never a reason to lose the verdicts
        return f"positions() could not be read ({type(exc).__name__}: {exc})"
    return {_key(r.tradingsymbol, r.product): dict(r.raw) for r in rows}


def _views(
    run: MatrixRun, done: Sequence[tuple[Case, CaseResult]], before: Positions | str
) -> None:
    """One ``views`` supplement per case that sent orders: its trades, its positions change."""
    sent = [(c, r) for c, r in done if r.order_ids]
    if not sent:
        return
    after = _positions(run)
    try:
        trades: list[Any] | str = list(run.session.client.trades())
    except VIEW_ERRORS as exc:  # read-only: recorded, never a reason to lose the verdicts
        trades = f"trades() could not be read ({type(exc).__name__}: {exc})"
    for case, result in sent:
        contract = run.contracts.get(case.role)
        key = _key(contract.tradingsymbol if contract else case.role, case.product)
        shared = [c.id for c, _ in done if c.role == case.role and c.product == case.product]
        positions: Mapping[str, Any] = {
            key: {
                "before": before.get(key) if isinstance(before, dict) else before,
                "after": after.get(key) if isinstance(after, dict) else after,
            }
        }
        rows = (
            [dict(t.raw) for t in trades if (t.order_id or "").strip() in result.order_ids]
            if isinstance(trades, list)
            else trades
        )
        supplement(run, case, "views", trades=rows, positions=positions,
                   positions_shared_by=shared)  # fmt: skip


# -- the family -------------------------------------------------------------------------------


def run_family(run: MatrixRun, selected: Sequence[Case]) -> dict[str, CaseResult]:
    """Run the selected M4 cases; one result per case, by id, in the order given."""
    results: dict[str, CaseResult] = {}
    fills = [c for c in selected if c.outcome in batch.FILL_OUTCOMES]
    if fills:
        before = _positions(run)
        results.update(family.run_cases(run, fills))
        _views(run, [(c, results[c.id]) for c in fills], before)
    probe = next((c for c in selected if c.id.endswith(PARTIAL_SUFFIX)), None)
    close = next((c for c in selected if c.id.endswith(PARTIAL_CLOSE_SUFFIX)), None)
    results.update(_partial(run, probe, close))
    return {c.id: results[c.id] for c in selected}


def _one(run: MatrixRun, item: BatchItem) -> CaseResult:
    """One case through the session's run, with its read-only views."""
    before = _positions(run)
    result = family.run_cases(run, [item])[item.case.id]
    _views(run, [(item.case, result)], before)
    return result


def _partial(run: MatrixRun, probe: Case | None, close: Case | None) -> dict[str, CaseResult]:
    """The partial-fill probe and its closing SELL (each only when selected)."""
    results: dict[str, CaseResult] = {}
    contract = run.contracts.get(PARTIAL_ROLE)
    tsym = contract.tradingsymbol if contract is not None else None
    filled = 0
    if probe is not None:
        sizing: dict[str, Any] = {}

        def lots(c: Contract) -> int:
            count = PARTIAL_LOTS
            while count > 0 and not run.positions.room(
                c.tradingsymbol, c.quantity(count), lot_size=c.lot_size
            ):
                count -= 1
            ceiling = run.positions.ceiling_lots
            reason = (
                f"{PARTIAL_LOTS} lots asked; the run's position ceiling is {ceiling} lots per "
                f"instrument (leftover {run.positions.net(c.tradingsymbol)} units now): sized "
                f"to {count} lots"
                if count < PARTIAL_LOTS
                else f"{PARTIAL_LOTS} lots, within the position ceiling"
            )
            sizing.update(asked_lots=PARTIAL_LOTS, lots=count, ceiling_lots=ceiling,
                          lot_size=c.lot_size, reason=reason)  # fmt: skip
            return count

        before = run.positions.net(tsym) if tsym is not None else 0
        hold = run.positions.one_at_a_time(tsym) if tsym else contextlib.nullcontext()
        with hold:
            item = BatchItem(
                probe, plan=fill_plan(run, side="BUY", lots=lots), after_settle=wait_for_fill()
            )
            result = _one(run, item)
        if sizing:
            supplement(run, probe, "sizing", **sizing)
        results[probe.id] = result
        if tsym is not None:
            filled = run.positions.net(tsym) - before
    if close is not None:
        if probe is None:
            results[close.id] = skipped(run, close, "runs only after its partial-fill probe")
        elif filled <= 0 or tsym is None:
            reason = "the partial-fill probe filled nothing: nothing to close"
            results[close.id] = skipped(run, close, reason)
        else:
            with run.positions.one_at_a_time(tsym):
                plan = fill_plan(run, side="SELL", opening=False, quantity=filled)
                results[close.id] = _one(
                    run, BatchItem(close, plan=plan, after_settle=wait_for_fill(), closing=True)
                )
    return results
