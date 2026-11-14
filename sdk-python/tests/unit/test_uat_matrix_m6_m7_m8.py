"""Case tables and pure helpers of the UAT matrix families M6 (AMO), M7 (sliced), M8 (burst)."""

from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from tests.live_orders.safety import Refusal
from tests.live_orders.uat_matrix import (
    cases,
    cases_amo,
    cases_burst,
    cases_sliced,
    family,
    pricing,
)
from tests.live_orders.uat_matrix.calls import OrderOutcome
from tests.live_orders.uat_matrix.contracts import Contract
from tests.live_orders.uat_matrix.expect import KnownIssues
from tradesmart.models.base import IST

ALL = [*cases_amo.AMO_CASES, *cases_sliced.SLICED_CASES, *cases_burst.BURST_CASES]
ID = re.compile(r"[a-z0-9&-]+")


def _at(hour: int, minute: int = 0, day: int = 13) -> datetime:
    return datetime(2026, 11, day, hour, minute, tzinfo=IST)  # 13 Nov 2026 is a Friday


def _contract(role: str, lot: int, kind: str = "fut") -> Contract:
    return Contract(
        role=role, exchange="NFO", tradingsymbol="X", token="1", symbol="X", kind=kind,
        lot_size=lot, tick_size=Decimal("0.05"), resolved_last_price=Decimal("100"),
    )  # fmt: skip


# -- tables -----------------------------------------------------------------------------------


def test_case_ids_are_unique_lower_case_and_carry_their_family() -> None:
    ids = [c.id for c in ALL]
    assert len(ids) == len(set(ids))
    assert not set(ids) & {c.id for c in cases.CASES}
    for case in ALL:
        assert ID.fullmatch(case.id), case.id
        assert case.id.startswith(f"{case.family}-{case.role}-"), case.id


def test_amo_cases_cover_four_segments_with_lmt_sl_lmt_and_ioc() -> None:
    plain = [c for c in cases_amo.AMO_CASES if not c.canary and c is not cases_amo.INSIDE_PROBE]
    assert {c.segment for c in plain} == {"nse", "nfo", "bfo", "mcx"}
    for segment in ("nse", "nfo", "bfo", "mcx"):
        shapes = {(c.prctyp, c.ret, c.side) for c in plain if c.segment == segment}
        assert {("LMT", "DAY", "BUY"), ("LMT", "DAY", "SELL"), ("SL-LMT", "DAY", "BUY"),
                ("LMT", "IOC", "BUY")} <= shapes  # fmt: skip
    ioc = [c for c in plain if c.ret == "IOC"]
    assert ioc and all(c.outcome == cases.REJECT for c in ioc)
    assert all(c.outcome == cases.RECORD for c in plain if c.ret != "IOC")
    assert all(c.orders == 1 for c in cases_amo.AMO_CASES)


def test_amo_has_its_own_bfo_canary_and_one_inside_probe() -> None:
    canaries = [c for c in cases_amo.AMO_CASES if c.canary]
    assert [c.segment for c in canaries] == ["bfo"]
    assert canaries[0].family == "amo" and canaries[0].id.endswith("-canary")
    probe = cases_amo.INSIDE_PROBE
    assert probe.segment == "nse" and probe.id.endswith("-inside")
    assert probe.outcome == cases.RECORD


def test_amo_smoke_is_two_cases() -> None:
    smoke = cases.select(cases.SMOKE, cases_amo.AMO_CASES)
    assert len(smoke) == 2
    assert {c.segment for c in smoke} == {"nse", "nfo"}


def test_the_amo_plan_sets_amo_and_prices_a_limit_twelve_percent_away() -> None:
    case = next(c for c in cases_amo.AMO_CASES if c.segment == "nse" and c.prctyp == "LMT")
    contract = _contract(case.role, 1, kind="eq")
    snapshot = pricing.PriceSnapshot("X", Decimal("100"), Decimal("0.05"))
    plan = cases_amo.amo_plan(case, contract, snapshot)
    assert plan["amo"] is True
    assert plan["price"] == Decimal("88.00")  # at least 10 % away overnight (final review M4)


def test_amo_items_modify_only_the_lmt_day_cases() -> None:
    items = cases_amo.items(cases_amo.AMO_CASES, lambda: _at(20))
    hooked = {i.case.id for i in items if i.after_settle is not None}
    expected = {
        c.id
        for c in cases_amo.AMO_CASES
        if c.prctyp == "LMT" and c.ret == "DAY" and not c.canary
        and c is not cases_amo.INSIDE_PROBE
    }  # fmt: skip
    assert hooked == expected
    assert all(i.plan is not None for i in items)


def test_each_amo_place_checks_the_window_again() -> None:
    case = next(c for c in cases_amo.AMO_CASES if c.segment == "nse" and c.prctyp == "LMT")
    contract = _contract(case.role, 1, kind="eq")
    snapshot = pricing.PriceSnapshot("X", Decimal("100"), Decimal("0.05"))
    moments = [_at(20), _at(9, 0)]  # the window closed between the batch start and the place
    (item,) = cases_amo.items([case], lambda: moments.pop(0))
    assert item.plan is not None
    assert item.plan(case, contract, snapshot)["amo"] is True
    with pytest.raises(Refusal, match="outside"):
        item.plan(case, contract, snapshot)


def test_an_amo_stop_is_at_least_ten_percent_from_the_last_price() -> None:
    case = next(c for c in cases_amo.AMO_CASES if c.segment == "nse" and c.prctyp == "SL-LMT")
    contract = _contract(case.role, 1, kind="eq")
    snapshot = pricing.PriceSnapshot("X", Decimal("100"), Decimal("0.05"))
    plan = cases_amo.amo_plan(case, contract, snapshot)
    assert plan["trigger_price"] >= Decimal("110") and plan["price"] >= plan["trigger_price"]
    narrow = pricing.PriceSnapshot("X", Decimal("100"), Decimal("0.05"), upper_circuit=Decimal(105))
    with pytest.raises(Refusal):
        cases_amo.amo_plan(case, contract, narrow)


@pytest.mark.parametrize("upper", [Decimal("110"), Decimal("111"), Decimal("111.95")])
def test_an_amo_stop_on_a_ten_percent_circuit_is_an_expected_skip_never_clamped(
    upper: Decimal,
) -> None:
    case = next(c for c in cases_amo.AMO_CASES if c.segment == "nse" and c.prctyp == "SL-LMT")
    contract = _contract(case.role, 1, kind="eq")
    snapshot = pricing.PriceSnapshot("X", Decimal("100"), Decimal("0.05"), upper_circuit=upper)
    with pytest.raises(Refusal, match="circuit"):
        cases_amo.amo_plan(case, contract, snapshot)


@pytest.mark.parametrize(
    ("side", "lower", "upper", "expected"),
    [("BUY", None, None, "88.00"), ("SELL", None, None, "112.00"),
     ("BUY", "89.00", None, "89.00"), ("SELL", None, "111.00", "111.00"),
     ("BUY", "90.00", None, "90.00"), ("SELL", None, "110.00", "110.00")],
)  # fmt: skip
def test_an_amo_limit_is_priced_twelve_percent_away_within_the_circuit(
    side: str, lower: str | None, upper: str | None, expected: str
) -> None:
    case = next(
        c for c in cases_amo.AMO_CASES
        if c.segment == "nse" and c.prctyp == "LMT" and c.side == side and not c.canary
    )  # fmt: skip
    contract = _contract(case.role, 1, kind="eq")
    snapshot = pricing.PriceSnapshot(
        "X", Decimal("100"), Decimal("0.05"),
        lower_circuit=None if lower is None else Decimal(lower),
        upper_circuit=None if upper is None else Decimal(upper),
    )  # fmt: skip
    assert cases_amo.amo_plan(case, contract, snapshot)["price"] == Decimal(expected)


@pytest.mark.parametrize(
    ("side", "lower", "upper"), [("BUY", "95.00", None), ("SELL", None, "104")]
)
def test_an_amo_limit_less_than_ten_percent_away_is_an_expected_skip(
    side: str, lower: str | None, upper: str | None
) -> None:
    case = next(
        c for c in cases_amo.AMO_CASES
        if c.segment == "nse" and c.prctyp == "LMT" and c.side == side and not c.canary
    )  # fmt: skip
    contract = _contract(case.role, 1, kind="eq")
    snapshot = pricing.PriceSnapshot(
        "X", Decimal("100"), Decimal("0.05"),
        lower_circuit=None if lower is None else Decimal(lower),
        upper_circuit=None if upper is None else Decimal(upper),
    )  # fmt: skip
    with pytest.raises(Refusal, match="expected skip.*10 %"):
        cases_amo.amo_plan(case, contract, snapshot)


@pytest.mark.parametrize(
    ("price", "side", "expected"),
    [("88.00", "BUY", "88.05"), ("112.00", "SELL", "111.95"), ("90.00", "BUY", None),
     ("110.00", "SELL", None), ("99.10", "BUY", None), ("100.90", "SELL", None)],
)  # fmt: skip
def test_the_amo_modify_price_moves_one_tick_and_stays_far(
    price: str, side: str, expected: str | None
) -> None:
    snapshot = pricing.PriceSnapshot("X", Decimal("100"), Decimal("0.05"))
    moved = cases_amo.modified_price(Decimal(price), side, snapshot)
    assert moved == (None if expected is None else Decimal(expected))


# -- AMO windows -------------------------------------------------------------------------------


def _amo(segment: str) -> cases.Case:
    return next(
        c for c in cases_amo.AMO_CASES
        if c.segment == segment and not c.canary and c is not cases_amo.INSIDE_PROBE
    )  # fmt: skip


@pytest.mark.parametrize("when", [_at(10), _at(9, 0), _at(8, 45), _at(8, 50), _at(15, 40)])
def test_amo_cases_refuse_inside_and_near_the_session(
    when: datetime, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TRADESMART_LIVE_ORDER_OUTSIDE_HOURS", "1")  # never lifts the AMO rule
    reason = cases_amo.refusal(_amo("nse"), when)
    assert reason is not None and "outside" in reason


@pytest.mark.parametrize("when", [_at(20), _at(7), _at(8, 44), _at(15, 46), _at(10, day=14)])
def test_amo_cases_run_outside_the_nse_session(when: datetime) -> None:
    assert cases_amo.refusal(_amo("nse"), when) is None
    assert cases_amo.refusal(_amo("nfo"), when) is None
    assert cases_amo.refusal(_amo("bfo"), when) is None


@pytest.mark.parametrize(
    "when",
    [_at(20), _at(23, 30), _at(23, 50), _at(23, 59), _at(0, 0, day=14), _at(0, 5, day=14),
     _at(0, 9, day=14), _at(8, 30), _at(8, 40)],
)  # fmt: skip
def test_mcx_amo_refuses_the_winter_session_up_to_ten_past_midnight(when: datetime) -> None:
    # MCX non-agri trades until 23:55 IST while US daylight saving time is off (Nov-Mar).
    reason = cases_amo.refusal(_amo("mcx"), when)
    assert reason is not None and "MCX" in reason


@pytest.mark.parametrize("when", [_at(0, 15, day=14), _at(6), _at(8, 29), _at(3, day=15)])
def test_mcx_amo_runs_after_ten_past_midnight_and_before_the_open(when: datetime) -> None:
    assert cases_amo.refusal(_amo("mcx"), when) is None


def test_after_a_weekend_day_no_mcx_session_wraps_past_midnight() -> None:
    assert cases_amo.refusal(_amo("mcx"), _at(0, 5, day=15)) is None  # Sunday after Saturday
    assert cases_amo.refusal(_amo("mcx"), _at(0, 5, day=16)) is None  # Monday after Sunday


def test_the_inside_probe_runs_inside_hours_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRADESMART_LIVE_ORDER_OUTSIDE_HOURS", "1")  # does not make 20:00 inside
    probe = cases_amo.INSIDE_PROBE
    assert cases_amo.refusal(probe, _at(10)) is None
    for when in (_at(20), _at(9, 16), _at(15, 20), _at(10, day=14)):
        reason = cases_amo.refusal(probe, when)
        assert reason is not None and "inside" in reason


# -- sliced ------------------------------------------------------------------------------------


def test_every_sliced_case_has_a_spec_and_declares_its_slices() -> None:
    assert [s.case for s in cases_sliced.SPECS] == cases_sliced.SLICED_CASES
    for spec in cases_sliced.SPECS:
        if spec.sizing == cases_sliced.FREEZE:
            assert spec.case.orders == 3
        else:
            assert spec.case.orders == -(-spec.lots // spec.slice_lots)


def test_sliced_cases_cover_the_inventory() -> None:
    by_role = {(s.case.role, s.case.ret, s.case.side, s.sizing) for s in cases_sliced.SPECS}
    assert ("nfo-nifty-f", "DAY", "BUY", cases_sliced.FREEZE) in by_role
    assert ("nfo-nifty-ce-atm", "DAY", "BUY", cases_sliced.FREEZE) in by_role
    assert ("bfo-sensex-ce-atm", "DAY", "BUY", cases_sliced.FREEZE) in by_role
    assert ("nfo-nifty-f", "IOC", "BUY", cases_sliced.LOTS) in by_role
    assert ("nfo-nifty-f", "DAY", "SELL", cases_sliced.LOTS) in by_role
    assert ("mcx-crudeoil-f", "DAY", "BUY", cases_sliced.LOTS) in by_role
    assert any(s.disclosed for s in cases_sliced.SPECS)
    ioc = [s.case for s in cases_sliced.SPECS if s.case.ret == "IOC"]
    assert all(c.outcome == cases.EXCHANGE_CANCEL for c in ioc)
    assert len(cases.select(cases.SMOKE, cases_sliced.SLICED_CASES)) == 2


def test_the_two_by_one_lot_case_slices_in_whole_lots() -> None:
    spec = next(s for s in cases_sliced.SPECS if s.case.id.endswith("-2x1lot")
                and s.case.role == "nfo-nifty-f")  # fmt: skip
    assert cases_sliced.planned(spec, _contract(spec.case.role, 65), None) == (130, (65, 65), 65)


def test_mcx_is_sliced_in_trade_units() -> None:
    spec = next(s for s in cases_sliced.SPECS if s.case.segment == "mcx")
    assert cases_sliced.planned(spec, _contract(spec.case.role, 100), None) == (
        200, (100, 100), 100,
    )  # fmt: skip


def test_freeze_sizing_is_two_freeze_slices_and_one_lot() -> None:
    spec = next(s for s in cases_sliced.SPECS if s.sizing == cases_sliced.FREEZE)
    contract = _contract(spec.case.role, 65)
    assert cases_sliced.planned(spec, contract, 1800) == (3575, (1755, 1755, 65), None)
    for freeze in (None, 0, 64):
        with pytest.raises(Refusal):
            cases_sliced.planned(spec, contract, freeze)


def test_the_disclosed_case_discloses_ten_percent_of_each_slice() -> None:
    spec = next(s for s in cases_sliced.SPECS if s.disclosed)
    quantity, slices, cap = cases_sliced.planned(spec, _contract(spec.case.role, 1, "eq"), None)
    assert spec.disclosed is not None
    assert all(spec.disclosed * 10 == size for size in slices)
    assert cap == slices[0] and quantity == sum(slices)


@pytest.mark.parametrize(
    ("lower", "expected"), [(None, "80.00"), (Decimal("70"), "80.00"), (Decimal("85"), "85.00")]
)
def test_the_option_freeze_cases_price_twenty_percent_below_within_the_circuit(
    lower: Decimal | None, expected: str
) -> None:
    snapshot = pricing.PriceSnapshot("X", Decimal("100"), Decimal("0.05"), lower_circuit=lower)
    assert cases_sliced.deep_far_buy(snapshot) == Decimal(expected)


def test_the_deep_price_is_refused_when_the_circuit_keeps_it_near_the_market() -> None:
    snapshot = pricing.PriceSnapshot(
        "X", Decimal("100"), Decimal("0.05"), lower_circuit=Decimal(95)
    )
    with pytest.raises(Refusal):
        cases_sliced.deep_far_buy(snapshot)


def test_only_the_option_freeze_cases_price_deep() -> None:
    deep = {s.case.role for s in cases_sliced.SPECS if s.deep}
    assert deep == {"nfo-nifty-ce-atm", "bfo-sensex-ce-atm"}
    assert all(s.sizing == cases_sliced.FREEZE for s in cases_sliced.SPECS if s.deep)


# -- burst ---------------------------------------------------------------------------------------


def test_the_burst_cases() -> None:
    burst, stress = cases_burst.BURST_CASES
    assert (burst.orders, burst.tier) == (50, cases.FULL)
    assert (stress.orders, stress.tier) == (650, cases.STRESS)
    assert burst.role == stress.role == "nse-b"
    assert burst.prctyp == "LMT" and burst.side == "BUY" and burst.outcome == cases.REST


def _stamps(gap: float, n: int, latency: float = 0.01) -> list[tuple[float, float]]:
    return [(i * gap, i * gap + latency) for i in range(n)]


def test_pacing_at_ten_per_second_passes() -> None:
    assert cases_burst.pacing_problems(_stamps(0.1, 50)) == []


def test_pacing_faster_than_ten_per_second_is_reported() -> None:
    problems = cases_burst.pacing_problems(_stamps(0.05, 30))
    assert problems and "10" in problems[0]


def test_the_burst_judges_the_sdks_defaults_and_the_runs_pace() -> None:
    assert cases_burst.LIMITS == ((10, 1.0), (200, 60.0))  # the SDK's 10/s and 200/min (F33)
    assert cases_burst.RUN_PACE == 180  # the run's pacer, under the SDK's 200


def test_pacing_over_the_minute_window_is_reported() -> None:
    problems = cases_burst.pacing_problems(_stamps(0.2995, 250))  # 201 starts in 59.9 s
    assert problems and "200" in problems[0]
    assert cases_burst.pacing_problems(_stamps(0.3005, 250)) == []


def test_the_limiter_is_engaged_per_window_when_the_burst_reaches_it() -> None:
    fast = _stamps(0.1, 700)  # 10/s: the second window is full, 180 starts in a minute
    assert cases_burst.limiter_engaged(fast) == {"per_second": True, "per_minute": True}
    waves = _stamps(0.1, 50) + [(s + 30, r + 30) for s, r in _stamps(0.1, 50)]
    assert cases_burst.limiter_engaged(waves) == {"per_second": True, "per_minute": False}
    slow = _stamps(0.2, 30)
    assert cases_burst.limiter_engaged(slow) == {"per_second": False, "per_minute": False}


def test_max_in_window() -> None:
    assert cases_burst.max_in_window([0.0, 0.1, 0.2, 1.5], 1.0) == 3
    assert cases_burst.max_in_window([], 1.0) == 0


def _outcome(verdict: str, error: dict[str, object] | None) -> OrderOutcome:
    return OrderOutcome("place", "c", "t", verdict, error=error)


def test_rate_rejections() -> None:
    assert cases_burst.is_rate_rejection(_outcome("rejected", {"http_status": 429}))
    assert cases_burst.is_rate_rejection(
        _outcome("rejected", {"http_status": None, "exception": "RateLimitError"})
    )
    assert cases_burst.is_rate_rejection(_outcome("rejected", {"emsg": "Too many requests"}))
    assert not cases_burst.is_rate_rejection(_outcome("acknowledged", None))
    assert not cases_burst.is_rate_rejection(_outcome("rejected", {"emsg": "RED:RULE:{x}"}))


# -- family: a batch's own canary ------------------------------------------------------------------


def test_a_batch_with_its_own_canary_gets_no_other_canary() -> None:
    run = SimpleNamespace(issues=KnownIssues())
    canary = next(c for c in cases_amo.AMO_CASES if c.canary)
    bfo = next(c for c in cases_amo.AMO_CASES if c.segment == "bfo" and not c.canary)
    assert family._canaries(run, [canary, bfo]) == []  # type: ignore[arg-type]
    shapes_canary = next(c for c in cases.CASES if c.canary and c.segment == "bfo")
    assert family._canaries(run, [bfo]) == [shapes_canary]  # type: ignore[arg-type]


def test_the_deep_option_price_is_four_percent_below_and_others_stay_twenty() -> None:
    snapshot = pricing.PriceSnapshot("X", Decimal("300"), Decimal("0.05"), Decimal("1"))
    assert cases_sliced.deep_far_buy(snapshot, option=True) == Decimal("288.00")
    assert cases_sliced.deep_far_buy(snapshot) == Decimal("240.00")


def test_the_deep_option_price_is_refused_unless_one_percent_below() -> None:
    near = pricing.PriceSnapshot("X", Decimal("100"), Decimal("0.05"), Decimal("99.5"))
    with pytest.raises(Refusal):
        cases_sliced.deep_far_buy(near, option=True)
    ok = pricing.PriceSnapshot("X", Decimal("100"), Decimal("0.05"), Decimal("97"))
    assert cases_sliced.deep_far_buy(ok, option=True) == Decimal("97.00")
