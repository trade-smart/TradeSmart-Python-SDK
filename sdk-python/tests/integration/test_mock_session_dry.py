"""Dry runs of the matrix in mock-session mode on the offline broker (no network).

A mock session is the production server during an exchange mock trading session (on a
Saturday here). The rigs below are the family rigs with the session switched to mock mode: the
switch date and IST date are Saturday 14 Nov 2026 and the offline broker's host stands in for the
production host (``mock_session.PRODUCTION_HOST`` patched), so every place, sliced place and
modify goes through the real switch re-check (``MatrixSession.mock_refusal``).

Pinned: the smoke tier places at any hour (11:00 and 16:00 IST on a Saturday) and passes with no
AMO and no GTT call; MARKET and SL-MKT are refused as expected (F27, the production oracle); the
UAT-only recording rules are off (a far fill, a stop seen open, a band refusal and an unknown
symbol are judged as production judges them); the position ceiling stays on; the AMO and GTT
families send nothing; the date rolling over mid-batch stops new places while the cancels go out.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest

from tests.integration.test_uat_matrix_gtt_dry import GttMarket
from tests.integration.uat_family_rig import Rig, family_rig
from tests.integration.uat_fill_rig import make_rig
from tests.integration.uat_t7_rig import add_mcx, set_freeze
from tests.live_orders import mock_session, safety
from tests.live_orders.uat_matrix import (
    batch,
    cases,
    cases_amo,
    cases_burst,
    cases_fills,
    cases_gtt,
    cases_sliced,
    contracts,
    family,
    gate,
)
from tests.live_orders.uat_matrix.cases import Case
from tradesmart.models.base import IST

SATURDAY = date(2026, 11, 14)
SUNDAY = date(2026, 11, 15)
SAT_11 = datetime(2026, 11, 14, 11, 0, tzinfo=IST)
SAT_16 = datetime(2026, 11, 14, 16, 0, tzinfo=IST)
SWITCH = {
    mock_session.SESSION_ENV: "2026-11-14",
    mock_session.CONFIRM_ENV: "mock session 2026-11-14",
}
SHAPES = [c for c in cases.CASES if c.family == "shapes"]
CE = "NIFTY26000CE"  # the rig's nfo-nifty-ce-atm (last 150.00, lot 65)
SENSEX_F = "BSXFUT27NOV26"
BEYOND = "LIMIT PRICE: 89716.00 BEYOND CIRCUIT LIMITS, UPPER CIRCUIT = 79386.20"
INVALID_SYMBOL = "Invalid Input :   Invalid Trading Symbol"
FILL = "fills-nfo-nifty-f-nrml-lmt-day-buy"
GTT_ENDPOINTS = ("PlaceGTTOrder", "ModifyGTTOrder", "CancelGTTOrder")


def make_mock(session: gate.MatrixSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """Switch a rig's session to mock mode on Saturday; the offline broker host plays production."""
    host = safety._rest_host(session.client.config.base_url)
    monkeypatch.setattr(mock_session, "PRODUCTION_HOST", host)
    session.mock = True
    session.mock_date = SATURDAY
    session.mock_environ = dict(SWITCH)
    session.today = lambda: SATURDAY
    assert session.mock_refusal() is None


@contextmanager
def mock_rig(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, now: datetime = SAT_11, **options: Any
) -> Iterator[Rig]:
    with family_rig(tmp_path, now=now, **options) as rig:
        make_mock(rig.session, monkeypatch)
        yield rig


def _shape(role: str, prctyp: str, side: str, ret: str = "DAY") -> Case:
    return next(
        c
        for c in SHAPES
        if c.role == role and c.prctyp == prctyp and c.side == side and c.ret == ret
        and not c.canary
    )  # fmt: skip


def _no_amo_or_gtt(rig: Rig) -> None:
    assert all(p.get("amo") != "Yes" for p in rig.requests("PlaceOrder"))
    for endpoint in GTT_ENDPOINTS:
        assert rig.requests(endpoint) == [], endpoint


def _not_for_hours(result: batch.CaseResult) -> None:
    assert "closed" not in result.reason and "15:30" not in result.reason, result.reason


# -- the smoke tier --------------------------------------------------------------------------------


@pytest.mark.parametrize("now", [SAT_11, SAT_16], ids=["sat-11:00", "sat-16:00"])
def test_the_smoke_shapes_pass_at_any_hour_with_no_amo_or_gtt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, now: datetime
) -> None:
    smoke = cases.select(cases.SMOKE, SHAPES)
    with mock_rig(tmp_path, monkeypatch, now=now) as rig:
        results = family.run_cases(rig.run, smoke)
        wrong = {k: (r.verdict, r.reason) for k, r in results.items() if r.verdict != batch.PASS}
        assert not wrong, wrong
        assert len(rig.requests("PlaceOrder")) == len(smoke)  # one place per case
        assert all(o.status in ("CANCELED", "REJECTED") for o in rig.broker.orders)
        _no_amo_or_gtt(rig)


def test_the_smoke_fill_runs_after_the_1530_cut_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    [case] = [c for c in cases_fills.family_cases() if c.id == FILL]
    for rig in make_rig(tmp_path, now=SAT_16):
        make_mock(rig.session, monkeypatch)
        result = cases_fills.run_family(rig.run, [case])[case.id]
        _not_for_hours(result)
        assert result.verdict != batch.SKIP, result.reason
        assert rig.requests("PlaceOrder") >= 1


def test_the_partial_fill_probe_runs_after_the_1530_cut_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe = "fills-nfo-banknifty-f-mis-lmt-day-buy-partial"  # its plan: family_tools.fill_plan
    [case] = [c for c in cases_fills.family_cases() if c.id == probe]
    for rig in make_rig(tmp_path, now=SAT_16):
        make_mock(rig.session, monkeypatch)
        result = cases_fills.run_family(rig.run, [case])[case.id]
        _not_for_hours(result)
        assert rig.requests("PlaceOrder") >= 1


async def test_the_views_fill_paths_run_after_the_1530_cut_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.integration.test_uat_matrix_views_dry import FAST, _cases, _run_async
    from tests.integration.uat_modify_views_rig import modify_views_rig
    from tests.live_orders.uat_matrix import cases_views

    with modify_views_rig(tmp_path, now=SAT_16) as rig:
        make_mock(rig.session, monkeypatch)
        [fill] = [c for c in _cases("nse-b") if cases_views.path_of(c) == "fill"]
        _not_for_hours(cases_views.run_cases(rig.run, [fill], timing=FAST)[fill.id])
        [afill] = [
            c for c in _cases("nse-b", asynchronous=True) if cases_views.path_of(c) == "fill"
        ]
        _not_for_hours((await _run_async(rig, [afill]))[afill.id])
        assert rig.count("PlaceOrder") >= 2


@pytest.mark.parametrize("prctyp", ["MKT", "SL-MKT"])
def test_market_orders_are_refused_as_expected_f27(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, prctyp: str
) -> None:
    case = _shape("nse-b", prctyp, "BUY")
    with mock_rig(tmp_path, monkeypatch) as rig:
        result = family.run_cases(rig.run, [case])[case.id]
        assert result.verdict == batch.PASS, result.reason
        assert "ALGO_CHK" in result.reason


def test_mcx_burst_and_sliced_place_on_a_saturday_evening(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mcx = [c for c in cases.CASES if c.segment == "mcx" and c.prctyp == "LMT"][:1]
    burst = cases_burst.BURST_CASES[0]
    spec = next(s for s in cases_sliced.SPECS if s.case.segment == "nse")
    with mock_rig(tmp_path, monkeypatch, now=SAT_16) as rig:
        add_mcx(rig)
        set_freeze(rig, "nfo-nifty-f", 1800)
        _not_for_hours(batch.run_batch(rig.run, mcx)[0])
        _not_for_hours(cases_burst.run_burst(rig.run, [burst])[burst.id])
        _not_for_hours(cases_sliced.sliced_results(rig.run, [spec.case])[spec.case.id])
        assert len(rig.requests("PlaceOrder")) > 1 + burst.orders
        _no_amo_or_gtt(rig)


async def test_an_async_views_place_on_a_saturday_evening_is_sent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.integration.test_uat_matrix_views_dry import _cases, _run_async
    from tests.integration.uat_modify_views_rig import modify_views_rig
    from tests.live_orders.uat_matrix import cases_views

    with modify_views_rig(tmp_path, now=SAT_16) as rig:
        make_mock(rig.session, monkeypatch)
        [ioc] = [c for c in _cases("nse-b", asynchronous=True) if cases_views.path_of(c) == "ioc"]
        result = (await _run_async(rig, [ioc]))[ioc.id]
        _not_for_hours(result)
        assert rig.count("PlaceOrder") == 1


# -- UAT-only rules: off (production judging) ---------------------------------------------------


def test_a_far_fill_fails_and_counts_for_the_ceiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = _shape("nfo-nifty-ce-atm", "LMT", "SELL")
    with mock_rig(tmp_path, monkeypatch) as rig:
        rig.market.fill_far[CE] = None
        result = family.run_cases(rig.run, [case])[case.id]
        assert result.verdict == batch.FAIL and "away from the market filled" in result.reason
        assert "uat_filled_far" not in result.record["notes"]
        assert rig.run.positions.net(CE) == -65


def test_an_instrument_at_the_ceiling_takes_no_further_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = _shape("nfo-nifty-ce-atm", "LMT", "BUY")
    with mock_rig(tmp_path, monkeypatch) as rig:
        rig.run.positions.add_fill(CE, -130, lot_size=65, case_id="earlier", order_id=None)
        result = family.run_cases(rig.run, [case])[case.id]
        assert result.verdict == batch.SKIP and "ceiling" in result.reason
        assert rig.requests("PlaceOrder") == []


def test_a_stop_order_seen_open_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    case = _shape("nfo-nifty-ce-atm", "SL-LMT", "BUY")
    with mock_rig(tmp_path, monkeypatch) as rig:
        rig.market.stop_open.add(CE)
        result = family.run_cases(rig.run, [case])[case.id]
        assert result.verdict == batch.FAIL and "saw OPEN" in result.reason


@pytest.mark.parametrize("message", [BEYOND, INVALID_SYMBOL], ids=["band", "invalid-symbol"])
def test_a_band_or_unknown_symbol_refusal_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, message: str
) -> None:
    case = _shape("nse-b", "LMT", "BUY")
    with mock_rig(tmp_path, monkeypatch) as rig:
        rig.market.band_refusal = lambda kwargs: message
        result = family.run_cases(rig.run, [case])[case.id]
        assert result.verdict == batch.FAIL and "unexpected refusal" in result.reason


def test_contracts_resolve_with_production_lots_and_no_quote_pacer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    def resolve(*args: Any, **kwargs: Any) -> Any:
        seen.update(kwargs)
        raise RuntimeError("stop here")

    monkeypatch.setattr(contracts, "resolve_contracts", resolve)
    with mock_rig(tmp_path, monkeypatch) as rig, pytest.raises(RuntimeError, match="stop here"):
        batch.open_run(rig.session)
    assert seen["uat"] is False and seen["pacer"] is None


# -- AMO and GTT: never sent ----------------------------------------------------------------------


def test_the_amo_family_sends_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with mock_rig(tmp_path, monkeypatch, now=SAT_16) as rig:
        results = cases_amo.run_amo(rig.run, cases_amo.AMO_CASES, SAT_16)
        assert {r.verdict for r in results.values()} == {batch.SKIP}
        assert {r.reason for r in results.values()} == {mock_session.NO_AMO_GTT}
        assert rig.requests("PlaceOrder") == []


@pytest.mark.parametrize("case", [cases_gtt.FAR, cases_gtt.FIRES], ids=["far", "fires"])
def test_the_gtt_family_sends_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: Case
) -> None:
    with mock_rig(tmp_path, monkeypatch, market_type=GttMarket) as rig:
        before = len(rig.broker.requests)
        result = cases_gtt.run_case(rig.run, case)
        assert result.verdict == batch.SKIP and result.reason == mock_session.NO_AMO_GTT
        assert len(rig.broker.requests) == before  # not even a read: the case never starts
        _no_amo_or_gtt(rig)


# -- the date rolls over mid-batch ----------------------------------------------------------------


def _rolls_over_after(places: int, rig: Rig) -> Callable[[], date]:
    def today() -> date:
        return SATURDAY if len(rig.requests("PlaceOrder")) < places else SUNDAY

    return today


def test_after_midnight_no_new_place_goes_out_and_ours_are_cancelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    resting = [
        c for c in SHAPES
        if c.role == "nse-b" and c.prctyp == "LMT" and c.outcome == cases.REST and not c.canary
    ][:3]  # fmt: skip
    with mock_rig(tmp_path, monkeypatch) as rig:
        rig.run.workers = 1  # one at a time, so the rollover falls between two places
        rig.session.today = _rolls_over_after(1, rig)
        results = batch.run_batch(rig.run, resting)
        assert len(rig.requests("PlaceOrder")) == 1
        assert rig.session.guard.stopped()
        skipped = [r for r in results if r.verdict == batch.SKIP]
        assert len(skipped) >= 2 and all("today" in r.reason for r in skipped)
        [order] = rig.broker.orders
        assert order.status == "CANCELED" and rig.market.cancels == [order.id]
