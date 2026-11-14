"""Helpers the M4 (fills), M5 (cover / bracket) and M12 (holdings) family modules share.

Nothing here changes the account: orders go through the session's run (``family.run_cases``,
the batch engine) and ``calls`` only. Parametrising, selection and reporting are ``family``'s
(``params``, ``selected_cases``, ``report``). The helpers:

- ``fill_plan``: a plan builder for an order at the last price (the UAT matching rule) that keeps
  the engine's fill rules: before 15:30 IST only, and within the run's per-instrument position
  ceiling (``positions``); a refusal skips the case before any place;
- ``wait_for_fill``: a hook that follows an order at the last price in the book (read-only) for
  up to the run's fill wait and records each change of status or filled quantity; the engine
  then cancels whatever is not final (one cancel);
- ``skipped``, ``supplement``, ``amended``: a result for a case that sends nothing, an extra
  record line of a case (sizing, holdings, exit, views), and a case's verdict amended after a
  family's follow-up step (with its own record line and ledger verdict); a skipped or amended
  result replaces the run's cached one (``family.remember``).
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Mapping
from typing import Any

from tests.live_orders.safety import Refusal, is_final, status_of
from tests.live_orders.uat_matrix import batch, family, pricing
from tests.live_orders.uat_matrix.batch import CaseResult, MatrixRun, Placed, PlanBuilder
from tests.live_orders.uat_matrix.cases import Case
from tests.live_orders.uat_matrix.contracts import Contract
from tests.live_orders.uat_matrix.ledger import read_book
from tests.live_orders.uat_matrix.pricing import PriceSnapshot

# -- results and records ----------------------------------------------------------------------


def _case_data(case: Case) -> dict[str, Any]:
    return {
        "case_id": case.id,
        "family": case.family,
        "role": case.role,
        "segment": case.segment,
        "tier": case.tier,
        "canary": case.canary,
    }


def skipped(run: MatrixRun, case: Case, reason: str, *, issue: str | None = None) -> CaseResult:
    """A case that sends nothing: one record line, no ledger verdict (a resume may run it).
    It replaces the run's cached result only when the case has no verdict in the ledger yet
    (a skip never hides an earlier verdict)."""
    data = {**_case_data(case), "verdict": batch.SKIP, "reason": reason, "issue": issue}
    written = run.recorder.write(data)
    result = CaseResult(case.id, batch.SKIP, reason, issue=issue, canary=case.canary,
                        record=written)  # fmt: skip
    if run.session.ledger.done(case.id) is None:
        family.remember(run, result)
    return result


def supplement(run: MatrixRun, case: Case, kind: str, **detail: Any) -> dict[str, Any]:
    """An extra record line of a case (``supplement`` names what it adds)."""
    return run.recorder.write({**_case_data(case), "supplement": kind, **detail})


def amended(
    run: MatrixRun,
    case: Case,
    result: CaseResult,
    verdict: str,
    reason: str,
    *,
    kind: str,
    issue: str | None = None,
    **detail: Any,
) -> CaseResult:
    """``result`` with the verdict a family's follow-up step decided: a record line of its own
    (``supplement`` = ``kind``, with the engine's verdict), the ledger's verdict when it changed
    (the latest one is the case's), and the run's cached result."""
    issue = result.issue if issue is None else issue
    supplement(run, case, kind, verdict=verdict, reason=reason, issue=issue,
               engine_verdict=result.verdict, **detail)  # fmt: skip
    if verdict != result.verdict:
        run.session.ledger.verdict(case.id, verdict, reason=reason)
    new = dataclasses.replace(result, verdict=verdict, reason=reason, issue=issue)
    family.remember(run, new)
    return new


def calls_of(result: CaseResult, kind: str) -> list[Mapping[str, Any]]:
    """The recorded calls of one kind (``place``, ``modify``, ``cancel``, ``exit``)."""
    return [c for c in result.record.get("calls") or [] if c.get("kind") == kind]


# -- plans and hooks --------------------------------------------------------------------------

Extra = Callable[[Case, Contract, PriceSnapshot], Mapping[str, Any]]


def fill_plan(
    run: MatrixRun,
    *,
    side: str,
    lots: int | Callable[[Contract], int] = 1,
    opening: bool = True,
    ceiling: bool | None = None,
    quantity: int | None = None,
    extra: Extra | None = None,
) -> PlanBuilder:
    """A plan builder for a LIMIT order at the last price (UAT matching rule).

    ``opening``: a fill order of the case (not a close): refused (the case skips, nothing sent)
    at or after 15:30 IST (never on UAT, ``run.session.uat``). ``ceiling`` (default: ``opening``):
    refused when it would pass the run's per-instrument position ceiling. ``quantity`` (trade
    units) overrides ``lots``; ``extra`` adds fields (cover and bracket legs, say), priced from
    the same fresh snapshot.
    """
    check_ceiling = opening if ceiling is None else ceiling

    def build(case: Case, contract: Contract, snapshot: PriceSnapshot) -> dict[str, Any]:
        if opening:
            cutoff = batch.fill_cutoff_reason(run.now(), uat=run.session.uat, mock=run.session.mock)
            if cutoff is not None:
                raise Refusal(cutoff)
        size = quantity
        if size is None:
            count = lots(contract) if callable(lots) else lots
            if count < 1:
                raise Refusal(
                    f"{contract.tradingsymbol}: no room under the run's position ceiling of "
                    f"{run.positions.ceiling_lots} lots; no fill order"
                )
            size = contract.quantity(count)
        if check_ceiling:
            signed = size if side == "BUY" else -size
            run.positions.check_room(contract.tradingsymbol, signed, lot_size=contract.lot_size)
        if case.prctyp != "LMT":
            raise ValueError(f"case {case.id}: an order at the last price is a LIMIT order")
        plan: dict[str, Any] = {  # no far price is computed: a narrow band never refuses it
            "exchange": contract.exchange,
            "tradingsymbol": contract.tradingsymbol,
            "quantity": size,
            "transaction_type": side,
            "product": case.product,
            "order_type": case.prctyp,
            "validity": case.ret,
            "price": pricing.at_last(snapshot, side),
        }
        if extra is not None:
            plan.update(extra(case, contract, snapshot))
        return plan

    return build


def book_row(placed: Placed) -> Any:
    """The order's row in one read of the book (read-only), or ``None``."""
    book = read_book(placed.run.session.client, attempts=1)
    order_id = placed.order_id
    return next((o for o in book or [] if (o.order_id or "").strip() == order_id), None)


def wait_for_fill(seconds: float | None = None) -> batch.Hook:
    """A hook that follows the order in the book until it is final or ``seconds`` (default:
    the run's fill wait) passed, noting each change of status or filled quantity in the case's
    ``fill_progress``; read-only (the engine cancels what is left, once)."""

    def hook(placed: Placed) -> None:
        run, session = placed.run, placed.run.session
        limit = run.fill_wait_seconds if seconds is None else seconds
        start = session.clock()
        progress: list[dict[str, Any]] = placed.notes.setdefault("fill_progress", [])
        last: tuple[str | None, int] | None = None
        while True:
            row = book_row(placed)
            if row is not None:
                point = (status_of(row), int(row.filled_quantity or 0))
                if point != last:
                    progress.append({"after_seconds": round(session.clock() - start, 1),
                                     "status": point[0], "filled": point[1]})  # fmt: skip
                    last = point
                if is_final(point[0]):
                    return
            if session.interrupted or session.guard.stopped() or session.clock() - start >= limit:
                return
            session.sleep(session.poll_interval)

    return hook
