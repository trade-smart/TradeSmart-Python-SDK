"""Fixes after the full live UAT run of the matrix (2026-11-13, third live run).

- a CNC SELL goes out only after a CNC BUY of the same stock and quantity filled in this run, or
  with that sellable quantity in a fresh ``holdings()`` read; otherwise the case skips (no order);
- only account-wide RMS limits (``{Turnover limit}``, ``Margin Shortfall``) skip a family's
  remaining cases; any other ``RED:`` refusal is a verdict of that case only;
- on UAT every quote read is paced (110 in any 60 s, 8 in any second, one session pacer), and a
  ``current minute`` rate refusal waits for the next minute;
- prices are on the contract's tick (a quote reporting a finer tick is not trusted);
- a bracket order's trailing price is at least 1.00 (UAT: ``Set : 1.00``);
- the order pacer allows 6 calls per second;
- UAT modify oddities (a quantity increase not shown, a modify of a cancelled order acknowledged)
  are recorded on UAT;
- on UAT ``Invalid Trading Symbol`` (SENSEX weekly options) is an environment-data skip.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tests.integration.uat_family_rig import family_rig
from tests.integration.uat_modify_views_rig import ModifyViewsRig, modify_views_rig
from tests.live_orders.uat_matrix import (
    batch,
    cases,
    cases_cobo,
    cases_modify,
    contracts,
    expect,
    family,
    order_pacer,
    pricing,
)
from tests.live_orders.uat_matrix.cases import Case
from tests.live_orders.uat_matrix.contracts import Contract
from tests.live_orders.uat_matrix.quote_pacer import QuotePacer, minute_wait
from tests.unit.test_uat_matrix_safety import FakeTime
from tradesmart import APIError
from tradesmart.models.market import Quote

SHAPES = [c for c in cases.CASES if c.family == "shapes"]
FAST = cases_modify.Timing(stream_wait=2.0)
NO_HOLDINGS = "RED:RULE:{No Holdings uploaded} for ITC-EQ"
INVALID_SYMBOL = "Invalid Input :   Invalid Trading Symbol"
MINUTE = "GetQuotes failed: Order Recieved 124 in a current minute exceeds Limit 120"
ITC = {"exch": "NSE", "tsym": "ITC-EQ", "token": "1660", "ls": "1", "ti": "0.05", "lp": "400.00"}


def _shape(role: str, product: str, side: str, prctyp: str = "LMT") -> Case:
    return next(
        c
        for c in SHAPES
        if (c.role, c.product, c.side, c.prctyp, c.ret) == (role, product, side, prctyp, "DAY")
        and not c.canary
    )  # fmt: skip


# -- 1. a CNC SELL needs a holding or this run's CNC BUY fill -----------------------------------


def test_a_cnc_sell_without_holdings_skips_before_sending(tmp_path: Path) -> None:
    case = _shape("nse-b", "CNC", "SELL")
    with family_rig(tmp_path) as rig:
        rig.broker.set_holdings([])
        result = family.run_cases(rig.run, [case])[case.id]
        assert result.verdict == batch.SKIP, result.reason
        assert "holding" in result.reason and "no order" in result.reason
        assert not [p for p in rig.broker.requests if p.endpoint == "PlaceOrder"]


def test_a_cnc_sell_with_enough_sellable_holdings_is_sent(tmp_path: Path) -> None:
    case = _shape("nse-b", "CNC", "SELL")
    with family_rig(tmp_path) as rig:
        rig.broker.set_holdings(
            [{"exch_tsym": [ITC], "holdqty": "5", "dpqty": "0", "usedqty": "0"}]
        )
        result = family.run_cases(rig.run, [case])[case.id]
        assert result.verdict != batch.SKIP, result.reason
        assert [p for p in rig.broker.requests if p.endpoint == "PlaceOrder"]


def test_a_cnc_sell_after_this_runs_cnc_buy_fill_of_that_quantity_is_sent(tmp_path: Path) -> None:
    case = _shape("nse-b", "CNC", "SELL")
    with family_rig(tmp_path) as rig:
        rig.broker.set_holdings([])
        rig.run.positions.add_fill(
            "ITC-EQ", 1, lot_size=1, case_id="fills-x", order_id="1", product="CNC"
        )
        result = family.run_cases(rig.run, [case])[case.id]
        assert result.verdict != batch.SKIP, result.reason


def test_a_mis_fill_does_not_cover_a_cnc_sell(tmp_path: Path) -> None:
    case = _shape("nse-b", "CNC", "SELL")
    with family_rig(tmp_path) as rig:
        rig.broker.set_holdings([])
        rig.run.positions.add_fill(
            "ITC-EQ", 1, lot_size=1, case_id="fills-x", order_id="1", product="MIS"
        )
        result = family.run_cases(rig.run, [case])[case.id]
        assert result.verdict == batch.SKIP, result.reason


# -- 2. only account-wide RMS limits skip the family ---------------------------------------------


def test_account_wide_limits_are_turnover_and_margin_only() -> None:
    assert expect.is_account_limit("RED:RULE:{Turnover limit} Current: INR 2 Set: INR 1")
    assert expect.is_account_limit("RED:Margin Shortfall:INR 10 Available:INR 0")
    for message in (
        NO_HOLDINGS,
        "RED:RULE:{Single Order Value} Current: 5 Set: 1",
        "RED:RULE:{Check circuit limit including square off order}",
        "RED:Block Type:NonSqroff Reason:This contract is blocked due to insufficient volume.",
        "RED:something new",
    ):
        assert expect.is_rms_limit(message) and not expect.is_account_limit(message), message


def test_a_no_holdings_refusal_does_not_skip_the_other_shapes_cases(tmp_path: Path) -> None:
    refused = _shape("nse-b", "MIS", "BUY")
    others = [_shape("nse-b", "MIS", "SELL"), _shape("nse-t", "MIS", "BUY")]
    with family_rig(tmp_path) as rig:
        rig.session.uat = True
        rig.market.band_refusal = lambda kw: (
            NO_HOLDINGS
            if kw["tradingsymbol"] == "ITC-EQ" and str(kw["transaction_type"]) in ("B", "BUY")
            else None
        )
        results = family.run_cases(rig.run, [refused])
        assert results[refused.id].verdict == batch.ENV_LIMIT
        assert "shapes" not in rig.run.issues.rms_families
        later = family.run_cases(rig.run, others)
        for case in others:
            assert later[case.id].verdict != batch.SKIP, (case.id, later[case.id].reason)
        assert rig.session.guard.tripped("nse") is None


def test_a_turnover_refusal_still_skips_the_familys_remaining_cases(tmp_path: Path) -> None:
    turnover = "RED:RULE:{Turnover limit} Current: INR 2 Set: INR 1"
    refused, other = _shape("nse-b", "MIS", "BUY"), _shape("nse-t", "MIS", "BUY")
    with family_rig(tmp_path) as rig:
        rig.market.band_refusal = lambda kw: turnover if kw["tradingsymbol"] == "ITC-EQ" else None
        assert family.run_cases(rig.run, [refused])[refused.id].verdict == batch.ENV_LIMIT
        result = family.run_cases(rig.run, [other])[other.id]
        assert result.verdict == batch.SKIP and "environment limit" in result.reason


# -- 3. UAT quote pacing -------------------------------------------------------------------------


def _max_in(stamps: list[float], window: float) -> int:
    ordered = sorted(stamps)
    return max(
        (sum(1 for s in ordered if start <= s < start + window - 1e-9) for start in ordered),
        default=0,
    )


def test_the_quote_pacer_keeps_110_a_minute_and_8_a_second() -> None:
    t = FakeTime()
    pacer = QuotePacer(clock=t.clock, sleep=t.sleep)
    stamps: list[float] = []
    for _ in range(300):
        pacer.wait()
        stamps.append(t.clock())
    assert _max_in(stamps, 1.0) == 8
    assert _max_in(stamps, 60.0) == 110


def test_the_async_quote_pacer_shares_the_windows() -> None:
    t = FakeTime()
    pacer = QuotePacer(clock=t.clock, sleep=t.sleep)

    async def fake_sleep(seconds: float) -> None:
        t.sleep(seconds)

    async def reads() -> list[float]:
        stamps = []
        for _ in range(20):
            await pacer.wait_async(fake_sleep)
            stamps.append(t.clock())
        return stamps

    assert _max_in(asyncio.run(reads()), 1.0) == 8


def test_a_current_minute_refusal_waits_for_the_next_minute_at_most_60_s() -> None:
    assert minute_wait(
        "Order Recieved 124 in a current minute exceeds Limit 120", wall=lambda: 90.0
    ) == (pytest.approx(30.5))
    assert minute_wait("... current minute ...", wall=lambda: 120.0) <= 60.0
    assert (
        minute_wait("Order Recieved 22 in a current second exceeds Limit 10", wall=lambda: 90.0)
        is None
    )


class _Quotes:
    def __init__(self, *answers: Any) -> None:
        self.answers = list(answers)
        self.reads = 0

    def quote(self, exchange: str, token: str) -> Quote:
        self.reads += 1
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        assert isinstance(answer, Quote)
        return answer


def _quote(ti: str = "0.05") -> Quote:
    return Quote.model_validate(
        {
            "stat": "Ok",
            "exch": "NFO",
            "tsym": "NIFTY23NOV26F",
            "token": "61471",
            "lp": "25127.80",
            "ti": ti,
            "ls": "65",
        }  # fmt: skip
    )


def _future(tick: str = "0.10") -> Contract:
    return Contract(
        role="nfo-nifty-f", exchange="NFO", tradingsymbol="NIFTY23NOV26F", token="61471",
        symbol="NIFTY", kind="fut", lot_size=65, tick_size=Decimal(tick),
        resolved_last_price=Decimal("25127.80"),
    )  # fmt: skip


def test_a_paced_read_counts_in_the_pacer_and_a_minute_refusal_waits_for_the_minute() -> None:
    t = FakeTime()
    pacer = QuotePacer(clock=t.clock, sleep=t.sleep, wall=lambda: 90.0)
    minute = APIError(MINUTE, endpoint="GetQuotes", http_status=200, emsg=MINUTE)
    slept: list[float] = []
    client = _Quotes(minute, _quote())
    quote = contracts.read_quote(client, "NFO", "61471", sleep=slept.append, pacer=pacer)
    assert quote.last_price == Decimal("25127.80")
    assert slept == [pytest.approx(30.5)] and pacer.total == 2


def test_the_session_quote_pacer_is_used_on_uat_only(tmp_path: Path) -> None:
    case = _shape("nse-b", "MIS", "BUY")
    with family_rig(tmp_path) as rig:
        family.run_cases(rig.run, [case])
        assert rig.session.quote_pacer.total == 0
    with family_rig(tmp_path / "uat") as rig:
        rig.session.uat = True
        assert rig.session.uat_quote_pacer() is rig.session.quote_pacer
        family.run_cases(rig.run, [case])
        assert rig.session.quote_pacer.total >= 1


# -- 4. / 5. tick and the BO trailing price ------------------------------------------------------


def test_the_snapshot_tick_is_the_contracts_when_the_quote_reports_a_finer_one() -> None:
    snap = pricing.fresh_quote(_Quotes(_quote("0.05")), _future("0.10"), uat=True)
    assert snap.tick_size == Decimal("0.10")
    snap = pricing.fresh_quote(_Quotes(_quote("0.10")), _future("0.05"), uat=True)
    assert snap.tick_size == Decimal("0.10")  # a multiple of both


@pytest.mark.parametrize("tick", ["0.10", "0.05"])
def test_co_and_bo_prices_are_multiples_of_the_contract_tick(tick: str) -> None:
    contract = _future(tick)
    snap = pricing.fresh_quote(_Quotes(_quote("0.05" if tick == "0.10" else "0.10")), contract,
                               uat=True)  # fmt: skip
    step = Decimal(tick)
    for case in cases_cobo.family_cases():
        if case.role != "nfo-nifty-f" or case.prctyp == "MKT":
            continue
        plan = cases_cobo.plan(case, contract, snap)
        for name in ("price", "trigger_price", "book_loss_price", "book_profit_price",
                     "trail_price"):  # fmt: skip
            value = plan.get(name)
            if value is not None:
                assert Decimal(str(value)) % step == 0, (case.id, name, value, tick)


def test_the_bo_trailing_price_is_at_least_one_rupee() -> None:
    contract = Contract(
        role="nfo-nifty-ce-atm", exchange="NFO", tradingsymbol="NIFTY17NOV26C25150",
        token="1", symbol="NIFTY", kind="opt", lot_size=65, tick_size=Decimal("0.05"),
        resolved_last_price=Decimal("140.00"),
    )  # fmt: skip
    snap = pricing.PriceSnapshot("NIFTY17NOV26C25150", Decimal("140.00"), Decimal("0.05"))
    bo = next(c for c in cases_cobo.family_cases() if c.product == "BO" and c.prctyp == "LMT")
    trail = cases_cobo.legs(bo, contract, snap)["trail_price"]
    assert trail >= Decimal("1.00") and trail % Decimal("0.05") == 0


# -- 6. the order pacer --------------------------------------------------------------------------


def test_the_order_pacer_allows_six_calls_a_second() -> None:
    assert order_pacer.ORDER_CALLS_PER_SECOND == 6


# -- 7. UAT modify oddities ----------------------------------------------------------------------


@pytest.fixture
def mv(tmp_path: Path) -> Iterator[ModifyViewsRig]:
    with modify_views_rig(tmp_path) as built:
        yield built


def _modify_case() -> Case:
    return next(c for c in cases_modify.CASES if c.role == "nse-b" and c.prctyp == "LMT")


def test_on_uat_a_quantity_increase_not_shown_is_recorded(mv: ModifyViewsRig) -> None:
    mv.session.uat = True
    mv.script[3] = "lie"  # qty-up: acknowledged, the book never changes
    [result] = cases_modify.run_cases(mv.run, [_modify_case()], timing=FAST).values()
    steps = {s["step"]: s for s in result.record["notes"]["steps"]}
    assert steps["qty-up"]["result"] == "record" and "UAT" in steps["qty-up"]["note"]
    assert result.verdict == batch.PASS, result.reason


# -- 8. Invalid Trading Symbol on UAT ------------------------------------------------------------


def test_on_uat_an_invalid_trading_symbol_is_an_environment_data_skip(tmp_path: Path) -> None:
    case = _shape("nse-b", "MIS", "BUY")
    with family_rig(tmp_path) as rig:
        rig.session.uat = True
        rig.market.band_refusal = lambda kw: INVALID_SYMBOL
        result = family.run_cases(rig.run, [case])[case.id]
        assert result.verdict == batch.SKIP and "environment data" in result.reason
        assert rig.session.guard.tripped("nse") is None


def test_off_uat_an_invalid_trading_symbol_still_fails(tmp_path: Path) -> None:
    case = _shape("nse-b", "MIS", "BUY")
    with family_rig(tmp_path) as rig:
        rig.market.band_refusal = lambda kw: INVALID_SYMBOL
        result = family.run_cases(rig.run, [case])[case.id]
        assert result.verdict == batch.FAIL
