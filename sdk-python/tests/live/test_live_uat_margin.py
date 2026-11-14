"""Live, read-only, UAT only: the UAT matrix family M10, order margin (``GetOrderMargin``).

For every M1 shape, cover and bracket orders with ``blprc``, market price types and invalid
products (``tests/live_orders/uat_matrix/cases_margin.py``), one ``order_margin`` read priced
from a fresh quote of the run's contract (``uat_matrix.contracts``, resolved read-only). Nothing
is placed: this is the live suite's one exception to "no order-family call"
(``tests/unit/test_live_suite_is_read_only.py``), a documented read.

Gates: the read-only live opt-in (``TRADESMART_LIVE_TESTS=1``) and UAT mode
(``safety.uat_mode``: ``TRADESMART_ENV=uat``, a UAT REST host, funded mode unset); everything
else skips before any call. Results go to ``.live-results/uat_margin.jsonl`` (local only).
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import pytest

from tests._environment import is_uat
from tests.live_orders import safety
from tests.live_orders.safety import Refusal
from tests.live_orders.uat_matrix import batch, cases_margin, gate, pricing
from tests.live_orders.uat_matrix import contracts as contract_table
from tests.live_orders.uat_matrix.batch import CaseResult
from tests.live_orders.uat_matrix.cases import FULL
from tests.live_orders.uat_matrix.cases_margin import MarginCase
from tests.live_orders.uat_matrix.contracts import Contracts
from tests.live_orders.uat_matrix.expect import Profile
from tests.live_orders.uat_matrix.pricing import PriceSnapshot
from tradesmart import TradeSmart
from tradesmart.models.base import IST

pytestmark = [pytest.mark.live, pytest.mark.uat_matrix, pytest.mark.m_margin]


@dataclass
class MarginRun:
    """The client, the run's contracts and profile, and one fresh quote per role."""

    client: TradeSmart
    contracts: Contracts
    profile: Profile
    snapshots: dict[str, PriceSnapshot | str] = field(default_factory=dict)  # or why not


def _snapshot(run: MarginRun, role: str, contract: contract_table.Contract) -> PriceSnapshot:
    cached = run.snapshots.get(role)
    if isinstance(cached, str):
        raise Refusal(cached)
    if cached is not None:
        return cached
    try:
        snapshot = pricing.fresh_quote(run.client, contract)
    except Refusal as exc:
        run.snapshots[role] = exc.reason
        raise
    run.snapshots[role] = snapshot
    return snapshot


def check_margin(run: MarginRun, case: MarginCase) -> CaseResult:
    """One margin read for ``case`` and its verdict (``SKIP`` without a contract or a price)."""
    want = cases_margin.expectation(case, run.profile)
    contract = run.contracts.get(case.role)
    if contract is None:
        missing = run.contracts.unavailable.get(case.role)
        why = missing.reason if missing is not None else "not resolved"
        return CaseResult(case.id, batch.SKIP, f"{case.role}: no contract ({why})")
    try:
        snapshot = _snapshot(run, case.role, contract)
    except Refusal as exc:
        return CaseResult(case.id, batch.SKIP, exc.reason)
    kwargs = cases_margin.margin_kwargs(case, contract, snapshot)
    record: dict[str, Any] = {"expect": want, "kwargs": kwargs}
    result: Any
    try:
        result = run.client.order_margin(**kwargs)
    except contract_table.SESSION_ERRORS:
        raise
    except contract_table.DATA_ERRORS as exc:
        result = exc
        record["error"] = {
            "exception": type(exc).__name__,
            "message": str(exc),
            "emsg": getattr(exc, "emsg", None),
        }
    else:
        record["margin"] = dict(result.raw)
    verdict, reason = cases_margin.judge(case, want, result)
    return CaseResult(case.id, verdict, reason, record=record)


@pytest.fixture(scope="module")
def margin_run(request: pytest.FixtureRequest) -> MarginRun:
    if not is_uat(os.environ):
        pytest.skip("UAT only: set TRADESMART_ENV=uat")
    client: TradeSmart = request.getfixturevalue("shared_client")
    if not safety.uat_mode(os.environ, client.config.base_url):
        pytest.skip("UAT mode only (TRADESMART_ENV=uat, a UAT REST host, funded mode unset)")
    today = gate.trading_day(datetime.now(IST))
    contracts = contract_table.resolve_contracts(client, today)  # read-only
    return MarginRun(client, contracts, Profile.from_details(client.profile()))


def _params() -> list[Any]:
    return [
        pytest.param(
            case, id=case.id, marks=[] if case.tier == FULL else [getattr(pytest.mark, case.tier)]
        )
        for case in cases_margin.MARGIN_CASES
    ]


@pytest.mark.parametrize("case", _params())
def test_uat_order_margin(
    case: MarginCase, margin_run: MarginRun, observe: Callable[..., object]
) -> None:
    result = check_margin(margin_run, case)
    observe(case_id=case.id, verdict=result.verdict, reason=result.reason, **result.record)
    batch.report(result)
