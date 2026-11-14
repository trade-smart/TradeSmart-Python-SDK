"""What the family test modules share: one run per session, one batch per module, hours.

A family module parametrises one test per case (``params``: id = case id, the tier marker) and
runs **all** its selected cases in one batch from a module-scoped fixture, then each test
reports its own case's result (``batch.report``)::

    @pytest.fixture(scope="module")
    def results(request, matrix):
        return family.module_results(request, matrix, build=lambda cases: cases)

- ``run_for(session)``: the session's ``MatrixRun`` (opened once: contracts, profile, oracle
  state, records and the position ledger are shared by every family of the session).
- ``selected_cases(request)``: the cases of this module's items that will run (not deselected
  by ``-k`` / ``-m``, not skipped at collection), in order.
- ``run_cases(run, items)``: one ``run_batch`` for the items without a result yet in this run
  (a case is placed at most once per run; its result is reused), with the BSE / BFO canary
  added first when a segment's canary has not been decided yet (U9: the canary is a case of its
  own family, which then reports the same result).
- ``remember(run, result)``: a family's own result of a case (amended after a follow-up step,
  or skipped without a batch) replaces the cached one.
- ``hours_refusal(now, uat=..., mock=...)``: outside UAT and mock sessions, NSE / NFO / BFO
  families place only inside market hours (``safety.market_closed_reason``); on UAT
  (``session.uat``) at any hour (owner, 2026-11-13: a case the closed market refuses is recorded
  as on any day); in a mock session (``session.mock``) at any hour too (the owner starts the run
  once mock trading opens). AMO (outside hours) is its own family with its own windows (never in
  a mock session).
"""

from __future__ import annotations

import threading
import weakref
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import datetime
from typing import Any

import pytest

from tests.live_orders import safety
from tests.live_orders.uat_matrix import batch, expect
from tests.live_orders.uat_matrix import cases as case_table
from tests.live_orders.uat_matrix.batch import BatchItem, CaseResult, MatrixRun
from tests.live_orders.uat_matrix.cases import Case
from tests.live_orders.uat_matrix.gate import MatrixSession
from tradesmart.models.base import IST

_LOCK = threading.Lock()
_RUNS: dict[int, tuple[weakref.ref[MatrixSession], MatrixRun]] = {}
_RESULTS: dict[int, tuple[weakref.ref[MatrixRun], dict[str, CaseResult]]] = {}


def run_for(session: MatrixSession) -> MatrixRun:
    """The session's run, opened once (``batch.open_run``) and shared by every family."""
    with _LOCK:
        entry = _RUNS.get(id(session))
        if entry is not None and entry[0]() is session:
            return entry[1]
        run = batch.open_run(session)
        _RUNS[id(session)] = (weakref.ref(session), run)
        return run


def _cache(run: MatrixRun) -> dict[str, CaseResult]:
    with _LOCK:
        entry = _RESULTS.get(id(run))
        if entry is None or entry[0]() is not run:
            entry = (weakref.ref(run), {})
            _RESULTS[id(run)] = entry
        return entry[1]


def results_of(run: MatrixRun) -> dict[str, CaseResult]:
    """The run's results by case id (``run_cases`` fills it; a family that runs cases its own
    way, e.g. the async views paths, keeps its results here too, so none is placed twice)."""
    return _cache(run)


# -- pytest parametrisation and selection -----------------------------------------------------


def params(cases: Iterable[Case]) -> list[Any]:
    """``pytest.param`` per case: id = the case id, the tier marker (``full`` is unmarked)."""
    result = []
    for case in cases:
        marks = [] if case.tier == case_table.FULL else [getattr(pytest.mark, case.tier)]
        result.append(pytest.param(case, id=case.id, marks=marks))
    return result


def selected_cases(request: Any, argname: str = "case") -> list[Case]:
    """The cases of this module's items that will run (collected, not deselected, not
    skipped at collection), in collection order, each once."""
    module = request.module
    chosen: list[Case] = []
    for item in request.session.items:
        if getattr(item, "module", None) is not module:
            continue
        if any(marker.name == "skip" for marker in item.iter_markers()):
            continue
        callspec = getattr(item, "callspec", None)
        case = None if callspec is None else callspec.params.get(argname)
        if isinstance(case, Case) and case not in chosen:
            chosen.append(case)
    return chosen


# -- running --------------------------------------------------------------------------------


def _case(item: Case | BatchItem) -> Case:
    return item.case if isinstance(item, BatchItem) else item


def _canaries(run: MatrixRun, items: Sequence[Case | BatchItem]) -> list[Case]:
    """The canaries the items' BSE / BFO segments still wait for (U9); none for a segment whose
    canary is already among the items (a family's own canary, e.g. the AMO one)."""
    issues = run.issues
    present = {_case(i).id for i in items}
    own = {_case(i).segment for i in items if _case(i).canary}
    needed: list[Case] = []
    for segment in sorted({_case(i).segment for i in items} & expect.CANARY_SEGMENTS):
        if segment in issues.canary_passed or segment in issues.stuck_segments or segment in own:
            continue
        canary = next((c for c in case_table.CASES if c.canary and c.segment == segment), None)
        if canary is not None and canary.id not in present:
            needed.append(canary)
    return needed


def run_cases(run: MatrixRun, items: Sequence[Case | BatchItem]) -> dict[str, CaseResult]:
    """The result of every item: from this run's earlier batches, else from one new batch."""
    cache = _cache(run)
    todo: list[Case | BatchItem] = [i for i in items if _case(i).id not in cache]
    if todo:
        todo = [*_canaries(run, todo), *todo]
        for result in batch.run_batch(run, todo):
            cache[result.case_id] = result
    return {_case(i).id: cache[_case(i).id] for i in items if _case(i).id in cache}


def remember(run: MatrixRun, result: CaseResult) -> None:
    """Keep ``result`` as the run's result of its case, replacing the engine's (a family's
    follow-up step amended it, or the family skipped the case without a batch)."""
    _cache(run)[result.case_id] = result


def hours_refusal(
    now: datetime | None = None, *, uat: bool = False, mock: bool = False
) -> str | None:
    """Why NSE / NFO / BFO orders must not be placed now (outside 09:20-15:15 IST, weekends),
    else ``None`` (``TRADESMART_LIVE_ORDER_OUTSIDE_HOURS=1`` lifts it, as in every live suite).
    On UAT (``uat``: the session's ``uat``) never: UAT tests run at any hour (owner, 2026-11-13).
    In a mock session (``mock``: the session's ``mock``) never: the owner starts the run once
    mock trading opens (owner, 2026-11-13)."""
    if uat or mock:
        return None
    moment = now if now is not None else datetime.now(IST)
    return safety.market_closed_reason(safety.SEGMENTS["nse"], moment)


def module_results(
    request: Any,
    session: MatrixSession,
    build: Callable[[MatrixRun, list[Case]], Sequence[Case | BatchItem]],
) -> Mapping[str, CaseResult]:
    """A family module's batch: refuse outside hours (every item skips; never on UAT or in a mock
    session), else run the module's selected cases (``build`` turns them into batch items) once."""
    reason = hours_refusal(uat=session.uat, mock=session.mock)
    if reason is not None:
        pytest.skip(reason)
    run = run_for(session)
    return run_cases(run, build(run, selected_cases(request)))


def report(results: Mapping[str, CaseResult], case: Case) -> None:
    """The pytest outcome of ``case`` (``batch.report``)."""
    result = results.get(case.id)
    if result is None:
        pytest.fail(f"case {case.id} was not run by its module's batch")
    batch.report(result)
