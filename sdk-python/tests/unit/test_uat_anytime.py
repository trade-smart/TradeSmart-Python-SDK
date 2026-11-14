"""UAT order tests run at any hour (owner, 2026-11-13), offline.

On UAT only (``session.uat`` in the matrix, the session's UAT mode in the older UAT suites) there
is no market-hours refusal and no 15:30 fill cut-off; production, the zero-funds account, funded
mode and the production GTT live test keep every hours rule; the AMO family keeps its windows.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from tests.live_orders import safety
from tests.live_orders.safety import OUTSIDE_HOURS_ENV, Refusal
from tests.live_orders.uat_matrix import batch, cases, cases_gtt, cases_mcx, family
from tests.unit.test_live_orders_safety import FakeMarket
from tradesmart.models.base import IST

LIVE_ORDERS = Path(__file__).resolve().parents[1] / "live_orders"
MATRIX = LIVE_ORDERS / "uat_matrix"
EVENING = datetime(2026, 11, 13, 20, 0, tzinfo=IST)  # a Friday after the close
SATURDAY = datetime(2026, 11, 14, 10, 0, tzinfo=IST)
NIGHT = datetime(2026, 11, 14, 2, 0, tzinfo=IST)  # outside the MCX window too
CLOSED = (EVENING, SATURDAY, NIGHT)
CLOSED_IDS = ["20:00", "saturday", "02:00"]


@pytest.fixture(autouse=True)
def _no_outside_hours_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(OUTSIDE_HOURS_ENV, raising=False)


def _nse_case() -> cases.Case:
    return next(c for c in cases.CASES if c.segment == "nse" and c.family != "amo")


def _mcx_case() -> cases.Case:
    return next(c for c in cases.CASES if c.segment == "mcx")


# -- the matrix -------------------------------------------------------------------------------


@pytest.mark.parametrize("now", CLOSED, ids=CLOSED_IDS)
def test_the_family_hours_rule_is_lifted_on_uat_only(now: datetime) -> None:
    assert family.hours_refusal(now, uat=True) is None
    assert family.hours_refusal(now) is not None
    assert family.hours_refusal(now, uat=False) is not None


@pytest.mark.parametrize("now", [SATURDAY, NIGHT], ids=["saturday", "02:00"])
def test_the_mcx_window_is_lifted_on_uat_only(now: datetime) -> None:
    assert cases_mcx.hours_refusal(now, uat=True) is None
    assert cases_mcx.hours_refusal(now) is not None


@pytest.mark.parametrize("now", CLOSED, ids=CLOSED_IDS)
def test_the_per_place_hours_check_is_lifted_on_uat_only(now: datetime) -> None:
    for case in (_nse_case(), _mcx_case()):
        assert batch.hours_refusal(case, now, uat=True) is None
    assert batch.hours_refusal(_nse_case(), now) is not None


def test_the_fill_cut_off_is_lifted_on_uat_only() -> None:
    late = datetime(2026, 11, 13, 15, 30, tzinfo=IST)
    for now in (late, EVENING):
        assert batch.fill_cutoff_reason(now, uat=True) is None
        assert batch.fill_cutoff_reason(now) is not None


@pytest.mark.parametrize("value", ["1", "0", ""])
def test_gtt_writes_on_uat_run_at_any_hour_whatever_the_outside_hours_opt_in(value: str) -> None:
    assert cases_gtt.write_refusal({}, EVENING, uat=True) is None
    assert cases_gtt.write_refusal({OUTSIDE_HOURS_ENV: value}, EVENING, uat=True) is None
    assert cases_gtt.write_refusal({OUTSIDE_HOURS_ENV: value}, EVENING) is not None
    assert cases_gtt.write_refusal({}, SATURDAY) is not None


@pytest.mark.parametrize(
    "name",
    [
        "test_burst.py",
        "test_m12_hold.py",
        "test_m4_fills.py",
        "test_m5_cobo.py",
        "test_modify.py",
        "test_sliced.py",
        "test_views.py",
    ],
)
def test_the_family_modules_pass_the_sessions_uat_flag_to_the_hours_rule(name: str) -> None:
    source = (MATRIX / name).read_text(encoding="utf-8")
    assert "family.hours_refusal(uat=matrix.uat, mock=matrix.mock)" in source
    assert "family.hours_refusal()" not in source


def test_the_mcx_and_gtt_modules_pass_the_sessions_uat_flag() -> None:
    mcx = (MATRIX / "test_mcx.py").read_text(encoding="utf-8")
    gtt = (MATRIX / "test_gtt.py").read_text(encoding="utf-8")
    assert "cases_mcx.hours_refusal(uat=matrix.uat, mock=matrix.mock)" in mcx
    assert "cases_gtt.write_refusal(os.environ, uat=matrix.uat, mock=matrix.mock)" in gtt


class _Session:
    def __init__(self, uat: bool) -> None:
        self.uat = uat
        self.mock = False  # mock-session mode: ``test_mock_session_rules.py``


@pytest.mark.parametrize("uat", [True, False])
def test_module_results_skips_outside_hours_only_when_not_uat(
    monkeypatch: pytest.MonkeyPatch, uat: bool
) -> None:
    monkeypatch.setattr(safety, "market_closed_reason", lambda segment, now: "market is closed")
    ran: list[Any] = []
    monkeypatch.setattr(family, "run_for", lambda session: "run")
    monkeypatch.setattr(family, "selected_cases", lambda request: [])
    monkeypatch.setattr(family, "run_cases", lambda run, items: ran.append(items) or {})
    session: Any = _Session(uat)
    if uat:
        assert family.module_results(object(), session, lambda run, chosen: chosen) == {}
        assert ran == [[]]
    else:
        with pytest.raises(pytest.skip.Exception, match="closed"):
            family.module_results(object(), session, lambda run, chosen: chosen)
        assert ran == []


# -- the older UAT suites (``safety.prepare_plan``) -------------------------------------------


def _market() -> FakeMarket:
    rows = [{"Exchange": "NSE", "Token": "3045", "LotSize": "1", "TradingSymbol": "SBIN-EQ"}]
    return FakeMarket({"stat": "Ok"}, {"stat": "Ok", "lp": "800.00", "ti": "0.05"}, rows)


@pytest.mark.parametrize("now", [EVENING, SATURDAY], ids=["20:00", "saturday"])
def test_prepare_plan_on_uat_at_any_hour_builds_the_plan(now: datetime) -> None:
    market = _market()
    record: dict[str, Any] = {}
    plan = safety.prepare_plan(
        market,  # type: ignore[arg-type]
        safety.SEGMENTS["nse"],
        record,
        now=now,
        uat=True,
        any_hour=True,
    )
    assert plan.tradingsymbol == "SBIN-EQ"
    assert record["hours_check"] == "skipped (UAT: any hour)"


@pytest.mark.parametrize(
    ("uat", "any_hour"), [(False, True), (True, False), (False, False)],
    ids=["production-any-hour", "uat-default", "production"],
)  # fmt: skip
def test_prepare_plan_keeps_the_hours_rule_unless_uat_and_any_hour(
    uat: bool, any_hour: bool
) -> None:
    market = _market()
    with pytest.raises(Refusal, match="closed"):
        safety.prepare_plan(
            market,  # type: ignore[arg-type]
            safety.SEGMENTS["nse"],
            {},
            now=EVENING,
            uat=uat,
            any_hour=any_hour,
        )
    assert market.calls == []


@pytest.mark.parametrize(
    "name", ["test_uat_lifecycle.py", "test_uat_sliced.py", "test_live_orders.py"]
)
def test_the_uat_order_suites_ask_for_any_hour_in_uat_mode(name: str) -> None:
    source = (LIVE_ORDERS / name).read_text(encoding="utf-8")
    assert "any_hour=session.uat" in source


def test_the_gtt_live_test_runs_at_any_hour_on_uat_only() -> None:
    source = (LIVE_ORDERS / "test_gtt_live.py").read_text(encoding="utf-8")
    # production keeps the window: any_hour is the session's UAT flag, never a constant
    assert "any_hour=session.uat" in " ".join(source.split())
    assert "any_hour=True" not in source


def test_funded_mode_keeps_its_hours_rule() -> None:
    funded = {
        "TRADESMART_LIVE_ORDER_FUNDED": "1",
        OUTSIDE_HOURS_ENV: "1",
    }
    reason = safety.funded_refusal(funded, "https://v2api.tradesmartonline.in/NorenWClientAPIv2")
    assert reason is not None and OUTSIDE_HOURS_ENV in reason
