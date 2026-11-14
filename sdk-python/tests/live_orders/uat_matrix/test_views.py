"""UAT matrix family M9: views and stream cross-checks, sync and async (live, UAT only).

One order per status path (OPEN -> modified -> CANCELED, REJECTED off-tick, TRIGGER_PENDING ->
CANCELED, IOC CANCELED, COMPLETE fill with its closing SELL) on NSE ``ITC`` and the NIFTY ATM
call; every view (book, history, trades, positions, the stream) must agree with the book. First
through ``TradeSmart`` + ``TradeSmartStream`` (the batch engine), then the same paths through
``AsyncTradeSmart`` + ``AsyncTradeSmartStream`` (``calls_async``), both on the session's shared
run (``family.run_for``) at any hour on UAT (owner, 2026-11-13). See ``cases_views.py``; the dry
run is ``tests/integration/test_uat_matrix_views_dry.py``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence

import pytest

from tests.live_orders.uat_matrix import calls_async, cases_views, family, gate, views
from tests.live_orders.uat_matrix.batch import CaseResult, MatrixRun
from tests.live_orders.uat_matrix.cases import Case
from tradesmart import AsyncTradeSmart
from tradesmart.stream.async_stream import client_identity

pytestmark = [pytest.mark.live_orders, pytest.mark.uat_matrix, pytest.mark.m_views]


async def _run_async(
    run: MatrixRun, session: gate.MatrixSession, selected: Sequence[Case]
) -> dict[str, CaseResult]:
    client_id, token = client_identity(session.client)
    client = AsyncTradeSmart(client_id, access_token=token, config=session.client.config)
    try:
        calls_async.attach_responses(session, client)
        async with cases_views.async_order_stream(client) as watcher:
            return await cases_views.run_async_family(run, client, selected, watcher=watcher)
    finally:
        await client.aclose()


@pytest.fixture(scope="module")
def results(request: pytest.FixtureRequest, matrix: gate.MatrixSession) -> Mapping[str, CaseResult]:
    reason = family.hours_refusal(uat=matrix.uat, mock=matrix.mock)
    if reason is not None:
        pytest.skip(reason)
    selected = family.selected_cases(request)
    sync = [c for c in selected if c in cases_views.CASES]
    asynchronous = [c for c in selected if c in cases_views.ASYNC_CASES]
    found: dict[str, CaseResult] = {}
    if not selected:
        return found
    run = family.run_for(matrix)
    if sync:
        with views.order_stream(matrix.client) as watcher:
            run.watcher = watcher
            try:
                found.update(cases_views.run_cases(run, sync))
            finally:
                run.watcher = None
    if asynchronous:
        found.update(asyncio.run(_run_async(run, matrix, asynchronous)))
    return found


@pytest.mark.parametrize("case", family.params([*cases_views.CASES, *cases_views.ASYNC_CASES]))
def test_views_and_stream(case: Case, results: Mapping[str, CaseResult]) -> None:
    family.report(results, case)
