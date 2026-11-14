"""Dry runs of the UAT behaviours seen in the second live smoke run of the UAT matrix.

On the offline broker (``UatLikeMarket`` and its test-tree policy knobs):

- a modify that moves a price stays inside the circuit band of a fresh quote (UAT acknowledges a
  modify outside the band and leaves the order unchanged): price -1 tick on a BUY at the lower
  circuit steps +1 tick instead;
- UAT may fill a far order at any price: on UAT the case is recorded (``uat_filled_far``), the
  fill goes to the run's position ledger once, and an instrument at the position ceiling takes
  no further order (closing orders exempt); elsewhere it still fails;
- a stop order seen ``OPEN`` where ``TRIGGER_PENDING`` was expected is recorded on UAT;
- the band refusals (``BEYOND CIRCUIT LIMITS``, ``STOP PRICE IS NOT REASONABLE``) are recorded on
  UAT and never count for the segment's circuit breaker.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tests.integration.test_uat_matrix_gtt_dry import GttMarket
from tests.integration.uat_family_rig import family_rig
from tests.integration.uat_modify_views_rig import ModifyViewsRig, install_async, modify_views_rig
from tests.live_orders.uat_matrix import (
    batch,
    cases,
    cases_burst,
    cases_gtt,
    cases_modify,
    cases_sliced,
    cases_views,
    family,
)
from tests.live_orders.uat_matrix.batch import BatchItem
from tests.live_orders.uat_matrix.cases import Case
from tradesmart import TradeSmart

FAST = cases_modify.Timing(stream_wait=2.0)
SHAPES = [c for c in cases.CASES if c.family == "shapes"]
CE = "NIFTY26000CE"  # the rig's nfo-nifty-ce-atm (last 150.00, lot 65)
SENSEX_F = "BSXFUT27NOV26"
BEYOND = (
    "LIMIT PRICE: 89716.00000000 BEYOND CIRCUIT LIMITS, UPPER CIRCUIT = 79386.20000000 AND "
    "lOWER CIRCUIT = 64952.35000000"
)
NOT_REASONABLE = "STOP PRICE IS NOT REASONABLE"


def _shape(role: str, prctyp: str, side: str, ret: str = "DAY") -> Case:
    return next(
        c
        for c in SHAPES
        if c.role == role and c.prctyp == prctyp and c.side == side and c.ret == ret
        and not c.canary
    )  # fmt: skip


@pytest.fixture
def mv(tmp_path: Path) -> Iterator[ModifyViewsRig]:
    with modify_views_rig(tmp_path) as built:
        yield built


def _at_lower_circuit(rig: ModifyViewsRig) -> Decimal:
    """ITC's far BUY (10 % below 400.00) clamps to a lower circuit of 380.00."""
    inst = rig.market.instruments["ITC-EQ"]
    inst.lower = Decimal("380.00")
    rig.market.set_last("ITC-EQ", "400.00")
    return inst.lower


# -- 1. a modify stays inside the band ----------------------------------------------------------


def test_the_rig_acknowledges_a_modify_outside_the_band_and_leaves_the_order(
    mv: ModifyViewsRig,
) -> None:
    lower = _at_lower_circuit(mv)
    client = mv.session.client
    order_id = client.place_order(
        exchange="NSE", tradingsymbol="ITC-EQ", quantity=1, price=lower, product="MIS",
        transaction_type="BUY", order_type="LMT", validity="DAY",
    )  # fmt: skip
    client.modify_order(order_id, exchange="NSE", tradingsymbol="ITC-EQ", quantity=1,
                        price=lower - Decimal("0.05"), order_type="LMT", product="MIS")  # fmt: skip
    [row] = [o for o in client.orders() if o.order_id == order_id]
    assert row.price == lower  # acknowledged, not applied (as UAT)


def test_price_down_at_the_lower_circuit_steps_up_a_tick_instead(mv: ModifyViewsRig) -> None:
    lower = _at_lower_circuit(mv)
    case = next(c for c in cases_modify.CASES if c.role == "nse-b" and c.prctyp == "LMT")
    [result] = cases_modify.run_cases(mv.run, [case], timing=FAST).values()
    steps = {s["step"]: s for s in result.record["notes"]["steps"]}
    assert result.verdict == batch.PASS, (result.reason, steps)
    down = steps["price-down"]
    assert Decimal(str(down["changes"]["price"])) == lower + Decimal("0.05")
    assert "lower circuit" in down["note"]
    for step in result.record["notes"]["steps"]:
        price = (step.get("changes") or {}).get("price")
        assert price is None or Decimal(str(price)) >= lower, step


def test_the_views_open_path_modifies_inside_the_band(mv: ModifyViewsRig) -> None:
    lower = _at_lower_circuit(mv)
    [open_] = [c for c in cases_views.CASES
               if c.role == "nse-b" and cases_views.path_of(c) == "open"]  # fmt: skip
    result = cases_views.run_cases(mv.run, [open_], timing=FAST)[open_.id]
    assert result.verdict == batch.PASS, result.reason
    [down] = result.record["notes"]["steps"][:1]
    assert Decimal(str(down["changes"]["price"])) == lower + Decimal("0.05")


async def _run_async(rig: ModifyViewsRig, table: list[Case]) -> dict[str, batch.CaseResult]:
    client = install_async(rig.market, rig.broker.async_client(heartbeat_interval=5.0))
    try:
        async with cases_views.async_order_stream(client) as watcher:
            return await cases_views.run_async_family(
                rig.run, client, table, watcher=watcher, timing=cases_views.Timing(stream_wait=5.0)
            )
    finally:
        await client.aclose()


def _async_case(role: str, path: str) -> Case:
    return next(
        c for c in cases_views.ASYNC_CASES if c.role == role and cases_views.path_of(c) == path
    )


async def test_the_async_views_open_path_modifies_inside_the_band(mv: ModifyViewsRig) -> None:
    lower = _at_lower_circuit(mv)
    case = _async_case("nse-b", "open")
    result = (await _run_async(mv, [case]))[case.id]
    assert result.verdict == batch.PASS, result.reason
    down = result.record["notes"]["steps"][0]
    assert Decimal(str(down["changes"]["price"])) == lower + Decimal("0.05")
    assert "lower circuit" in down["note"]


# -- 2. UAT fills far orders ----------------------------------------------------------------------


def test_a_far_order_filled_on_uat_is_recorded_with_its_gap(tmp_path: Path) -> None:
    case = _shape("nfo-nifty-ce-atm", "LMT", "SELL")
    with family_rig(tmp_path) as rig:
        rig.session.uat = True
        rig.market.fill_far[CE] = None  # UAT fills the far SELL in full
        result = family.run_cases(rig.run, [case])[case.id]
        assert result.verdict == batch.RECORD, result.reason
        assert "uat_filled_far" in result.reason
        note = result.record["notes"]["uat_filled_far"]
        assert note["exchange"] == "NFO" and note["segment"] == "nfo"
        assert note["side"] == "SELL" and note["product"] == case.product
        assert note["order_type"] == "LMT" and note["filled"] == 65
        assert Decimal(str(note["last_price"])) == Decimal("150.00")
        assert Decimal(str(note["price"])) == Decimal("156.00")  # 4 % above (option, LPP band)
        assert Decimal(str(note["gap_pct"])) == Decimal("4.00")
        assert rig.run.positions.net(CE) == -65  # a SELL fill, once
        assert rig.market.cancels == []  # filled: nothing to cancel


def test_a_far_order_filled_off_uat_still_fails(tmp_path: Path) -> None:
    case = _shape("nfo-nifty-ce-atm", "LMT", "SELL")
    with family_rig(tmp_path) as rig:
        rig.market.fill_far[CE] = None
        result = family.run_cases(rig.run, [case])[case.id]
        assert result.verdict == batch.FAIL and "away from the market filled" in result.reason
        assert rig.run.positions.net(CE) == -65


def test_a_part_filled_far_order_on_uat_is_cancelled_and_recorded(tmp_path: Path) -> None:
    case = _shape("nfo-nifty-ce-atm", "LMT", "BUY")
    with family_rig(tmp_path) as rig:
        rig.session.uat = True
        rig.market.fill_far[CE] = 13
        result = family.run_cases(rig.run, [case])[case.id]
        assert result.verdict == batch.RECORD, result.reason
        assert result.record["notes"]["uat_filled_far"]["filled"] == 13
        [order] = rig.broker.orders
        assert order.status == "CANCELED" and rig.market.cancels == [order.id]
        assert rig.run.positions.net(CE) == 13


def test_an_instrument_at_the_ceiling_takes_no_further_order_on_uat(tmp_path: Path) -> None:
    case = _shape("nfo-nifty-ce-atm", "LMT", "BUY")
    with family_rig(tmp_path) as rig:
        rig.session.uat = True
        rig.run.positions.add_fill(CE, -130, lot_size=65, case_id="earlier", order_id=None)
        result = family.run_cases(rig.run, [case])[case.id]
        assert result.verdict == batch.SKIP
        assert "position ceiling reached by UAT fills" in result.reason
        assert rig.requests("PlaceOrder") == []


def test_a_closing_item_at_the_ceiling_is_still_placed(tmp_path: Path) -> None:
    case = _shape("nfo-nifty-ce-atm", "LMT", "BUY")
    with family_rig(tmp_path) as rig:
        rig.session.uat = True
        rig.run.positions.add_fill(CE, -130, lot_size=65, case_id="earlier", order_id=None)
        [result] = batch.run_batch(rig.run, [BatchItem(case, closing=True)])
        assert result.verdict == batch.PASS, result.reason
        assert len(rig.requests("PlaceOrder")) == 1


def test_the_ceiling_check_is_uat_only(tmp_path: Path) -> None:
    case = _shape("nfo-nifty-ce-atm", "LMT", "BUY")
    with family_rig(tmp_path) as rig:
        rig.run.positions.add_fill(CE, -130, lot_size=65, case_id="earlier", order_id=None)
        result = family.run_cases(rig.run, [case])[case.id]
        assert result.verdict == batch.PASS, result.reason


async def test_an_async_case_at_the_ceiling_skips_on_uat(mv: ModifyViewsRig) -> None:
    mv.session.uat = True
    mv.run.positions.add_fill(CE, 130, lot_size=65, case_id="earlier", order_id=None)
    case = _async_case("nfo-nifty-ce-atm", "open")
    result = (await _run_async(mv, [case]))[case.id]
    assert result.verdict == batch.SKIP
    assert "position ceiling reached by UAT fills" in result.reason
    assert mv.count("PlaceOrder") == 0


async def test_an_async_far_fill_on_uat_is_recorded(mv: ModifyViewsRig) -> None:
    mv.session.uat = True
    mv.market.fill_far[CE] = None
    case = _async_case("nfo-nifty-ce-atm", "open")
    result = (await _run_async(mv, [case]))[case.id]
    assert result.verdict == batch.RECORD, result.reason
    assert result.record["notes"]["uat_filled_far"]["filled"] == 65
    assert mv.run.positions.net(CE) == 65


def test_a_gtt_at_the_ceiling_is_not_placed_on_uat(tmp_path: Path) -> None:
    with family_rig(tmp_path, market_type=GttMarket) as rig:
        rig.session.uat = True
        rig.run.positions.add_fill("ITC-EQ", 2, lot_size=1, case_id="earlier", order_id=None)
        result = cases_gtt.run_case(rig.run, cases_gtt.FIRES)
        assert result.verdict == batch.SKIP
        assert "position ceiling reached by UAT fills" in result.reason
        assert rig.requests("PlaceGTTOrder") == []


class FillingGttMarket(GttMarket):
    """The GTT's fired order fills at once (UAT fills at any price)."""

    def install(self, client: TradeSmart) -> TradeSmart:
        client = super().install(client)
        place_gtt: Callable[..., str] = client.place_gtt

        def placed_gtt(**kwargs: Any) -> str:
            alert_id = place_gtt(**kwargs)
            for order_id in self.fired or []:
                order = next(o for o in self.broker.orders if o.id == order_id)
                if order.status == "OPEN":
                    self.broker.fill(order_id, self.instruments["ITC-EQ"].last)
            return alert_id

        client.place_gtt = placed_gtt  # type: ignore[method-assign]
        return client


def test_a_fired_gtt_order_filled_on_uat_is_recorded_not_check_the_account(
    tmp_path: Path,
) -> None:
    with family_rig(tmp_path, market_type=FillingGttMarket) as rig:
        rig.session.uat = True
        result = cases_gtt.run_case(rig.run, cases_gtt.FIRES)
        assert result.verdict == batch.RECORD, result.reason
        assert "uat_filled_far" in result.record["notes"]
        assert rig.run.positions.net("ITC-EQ") == 1
        assert rig.session.guard.tripped("nse") is None


def test_a_fired_gtt_order_filled_off_uat_is_still_check_the_account(tmp_path: Path) -> None:
    with family_rig(tmp_path, market_type=FillingGttMarket) as rig:
        result = cases_gtt.run_case(rig.run, cases_gtt.FIRES)
        assert result.verdict == batch.CHECK, result.reason


# -- 3. a stop order seen OPEN ------------------------------------------------------------------


def test_a_stop_order_seen_open_on_uat_is_recorded_and_cancelled_once(tmp_path: Path) -> None:
    case = _shape("nfo-nifty-ce-atm", "SL-LMT", "BUY")
    with family_rig(tmp_path) as rig:
        rig.session.uat = True
        rig.market.stop_open.add(CE)
        result = family.run_cases(rig.run, [case])[case.id]
        assert result.verdict == batch.RECORD, result.reason
        assert result.record["notes"]["uat_stop_open"]["status"] == "OPEN"
        [order] = rig.broker.orders
        assert order.status == "CANCELED" and rig.market.cancels == [order.id]


def test_a_stop_order_seen_open_off_uat_still_fails(tmp_path: Path) -> None:
    case = _shape("nfo-nifty-ce-atm", "SL-LMT", "BUY")
    with family_rig(tmp_path) as rig:
        rig.market.stop_open.add(CE)
        result = family.run_cases(rig.run, [case])[case.id]
        assert result.verdict == batch.FAIL and "saw OPEN" in result.reason


async def test_an_async_stop_order_seen_open_on_uat_is_recorded(mv: ModifyViewsRig) -> None:
    mv.session.uat = True
    mv.market.stop_open.add("ITC-EQ")
    case = _async_case("nse-b", "trigger")
    result = (await _run_async(mv, [case]))[case.id]
    assert result.verdict == batch.RECORD, result.reason
    assert result.record["notes"]["uat_stop_open"]["status"] == "OPEN"


# -- 4. band refusals ---------------------------------------------------------------------------


def _band(kwargs: dict[str, Any]) -> str | None:
    """UAT's own band: every SENSEX future order but a LIMIT BUY is refused."""
    if kwargs["tradingsymbol"] != SENSEX_F:
        return None
    if str(kwargs["transaction_type"]) in ("S", "SELL") and str(kwargs["order_type"]) in (
        "SL-LMT", "SL",
    ):  # fmt: skip
        return NOT_REASONABLE
    if str(kwargs["transaction_type"]) in ("S", "SELL") or str(kwargs["order_type"]) != "LMT":
        return BEYOND
    return None


def test_band_refusals_on_uat_are_recorded_and_never_trip_the_breaker(tmp_path: Path) -> None:
    refused = [
        _shape("bfo-sensex-f", "LMT", "SELL"),
        _shape("bfo-sensex-f", "SL-LMT", "BUY"),
        _shape("bfo-sensex-f", "SL-LMT", "SELL"),
    ]
    with family_rig(tmp_path) as rig:
        rig.session.uat = True
        rig.market.band_refusal = _band
        results = family.run_cases(rig.run, refused)
        for case in refused:
            result = results[case.id]
            assert result.verdict == batch.RECORD, (case.id, result.reason)
            assert "uat_band_refusal" in result.record["notes"], case.id
        band = results[refused[0].id].record["notes"]["uat_band_refusal"]
        assert band["upper"] == "79386.20000000" and band["lower"] == "64952.35000000"
        assert results[refused[2].id].record["notes"]["uat_band_refusal"]["upper"] is None
        assert rig.session.guard.tripped("bfo") is None


def test_band_refusals_off_uat_still_fail(tmp_path: Path) -> None:
    case = _shape("bfo-sensex-f", "LMT", "SELL")
    with family_rig(tmp_path) as rig:
        rig.market.band_refusal = _band
        result = family.run_cases(rig.run, [case])[case.id]
        assert result.verdict == batch.FAIL and "unexpected refusal" in result.reason


async def test_an_async_band_refusal_on_uat_is_recorded(mv: ModifyViewsRig) -> None:
    mv.session.uat = True
    mv.market.band_refusal = lambda kwargs: BEYOND
    case = _async_case("nse-b", "open")
    result = (await _run_async(mv, [case]))[case.id]
    assert result.verdict == batch.RECORD, result.reason
    assert "uat_band_refusal" in result.record["notes"]
    assert mv.session.guard.tripped("nse") is None


def test_a_sliced_order_filled_on_uat_is_recorded(tmp_path: Path) -> None:
    case = next(
        c
        for c in cases_sliced.SLICED_CASES
        if c.role == "nfo-nifty-f" and c.ret == "DAY" and c.outcome == cases.REST
    )
    with family_rig(tmp_path) as rig:
        rig.session.uat = True
        rig.market.fill_far["NIFTY25NOV26F"] = None
        result = cases_sliced.sliced_results(rig.run, [case])[case.id]
        assert result.verdict == batch.RECORD, result.reason
        filled = result.record["notes"]["uat_filled_far"]["filled"]
        assert filled > 0 and rig.run.positions.net("NIFTY25NOV26F") == filled


def test_a_burst_filled_on_uat_is_recorded(tmp_path: Path) -> None:
    burst = cases_burst.BURST_CASES[0]
    with family_rig(tmp_path, simulated_order_clock=False) as rig:
        rig.session.uat = True
        rig.market.fill_far["ITC-EQ"] = None
        result = cases_burst.run_burst(rig.run, [burst])[burst.id]
        assert result.verdict == batch.RECORD, result.reason
        assert rig.run.positions.net("ITC-EQ") == result.record["notes"]["uat_filled_far"]["filled"]
