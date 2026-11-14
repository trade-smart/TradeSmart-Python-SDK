"""Dry run of the UAT matrix family M9 (views and stream, sync and async) on the offline broker.

``cases_views.run_cases`` (``TradeSmart`` + ``TradeSmartStream``, through the batch engine) and
``cases_views.run_async_family`` (``AsyncTradeSmart`` + ``AsyncTradeSmartStream``, through
``calls_async``) are the code the live test runs. One order per status path (OPEN -> modified ->
CANCELED, REJECTED off-tick, TRIGGER_PENDING -> CANCELED, IOC CANCELED, COMPLETE fill with its
closing SELL) on two instruments; every view (book, history newest first, trades, positions,
stream ``om``) must agree with the book at field level once the stream caught up.
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tests.integration.uat_modify_views_rig import ModifyViewsRig as Rig
from tests.integration.uat_modify_views_rig import install_async, modify_views_rig
from tests.live_orders.uat_matrix import batch, calls, cases, cases_views, family, views
from tests.live_orders.uat_matrix.cases import Case
from tradesmart.testing import drills

FAST = cases_views.Timing(stream_wait=5.0)
# The broker's 0.5 s heartbeat reconnects a stream whose loop is busy for 1 s (ledger fsyncs on
# a slow disk): updates sent meanwhile are lost. Live streams use the 30 s default.
STEADY = 5.0


@pytest.fixture
def rig(tmp_path: Path) -> Iterator[Rig]:
    with modify_views_rig(tmp_path) as built:
        yield built


def _cases(role: str, *, asynchronous: bool = False) -> list[Case]:
    table = cases_views.ASYNC_CASES if asynchronous else cases_views.CASES
    return [c for c in table if c.role == role]


def _by_path(results: dict[str, batch.CaseResult], table: list[Case]) -> dict[str, Any]:
    return {cases_views.path_of(c): results[c.id] for c in table}


def test_the_case_table_has_five_paths_on_two_instruments_sync_and_async() -> None:
    assert cases_views.PATHS == ("open", "reject", "trigger", "ioc", "fill")
    for table, suffix in ((cases_views.CASES, ""), (cases_views.ASYNC_CASES, "-async")):
        assert {c.role for c in table} == {"nse-b", "nfo-nifty-ce-atm"}
        assert len(table) == 10 and all(c.family == "views" for c in table)
        assert all(c.id.endswith(suffix) for c in table)
        assert all(c.product == "MIS" and c.side == "BUY" for c in table)
    ids = {c.id for c in cases_views.CASES} | {c.id for c in cases_views.ASYNC_CASES}
    assert len(ids) == 20
    assert "views-nse-b-mis-lmt-day-buy-open" in ids
    assert "views-nfo-nifty-ce-atm-mis-sl-lmt-day-buy-trigger-async" in ids
    smoke = {c.id for c in cases_views.CASES if c.tier == cases.SMOKE}
    assert smoke == {c.id for c in _cases("nse-b")}
    fill = next(c for c in cases_views.CASES if cases_views.path_of(c) == "fill")
    assert fill.orders == 2 and fill.outcome == cases.FILL_CLOSE


def _check_paths(rig: Rig, results: dict[str, batch.CaseResult], table: list[Case]) -> None:
    paths = _by_path(results, table)
    for path in ("open", "reject", "trigger", "ioc"):
        assert paths[path].verdict == batch.PASS, (path, paths[path].reason)
    fill = paths["fill"]
    assert fill.verdict == batch.XFAIL and fill.issue == "closing-sell", fill.reason
    for path, result in paths.items():
        record = result.record
        assert "views_error" not in record, record.get("views_error")
        assert record["views"]["mismatches"] == [], (path, record["views"]["mismatches"])
    assert paths["reject"].status == "REJECTED" and paths["ioc"].status == "CANCELED"
    assert paths["trigger"].status == "CANCELED" and paths["open"].status == "CANCELED"
    buy_id = paths["fill"].order_ids[0]
    fill_views = paths["fill"].record["views"]
    assert fill_views["orders"][buy_id]["trades"], fill_views
    assert "Fill" in fill_views["orders"][buy_id]["stream_reports"]
    [key] = [k for k in fill_views["positions"] if k.endswith("|MIS")]
    quantity = rig.run.contracts.contracts[table[0].role].lot_size
    assert fill_views["positions"][key]["buy_quantity"] == quantity
    assert fill_views["positions"][key]["sell_quantity"] == 0
    for result in paths.values():
        for order_id in result.order_ids:
            assert rig.statuses()[order_id] in ("CANCELED", "REJECTED", "COMPLETE")
    assert len(rig.market.cancels) == len(set(rig.market.cancels))


# -- sync: TradeSmart + TradeSmartStream ---------------------------------------------------------


def test_the_five_status_paths_agree_across_every_view(rig: Rig) -> None:
    results = cases_views.run_cases(rig.run, cases_views.CASES, timing=FAST)
    for role in ("nse-b", "nfo-nifty-ce-atm"):
        _check_paths(rig, results, _cases(role))
    open_path = _by_path(results, _cases("nse-b"))["open"]
    steps = open_path.record["notes"]["steps"]
    assert [s["step"] for s in steps] == ["price-down", "cancel"]
    assert all(s["result"] == "pass" for s in steps)
    lags = [o["stream_lag"] for o in open_path.record["views"]["orders"].values()]
    assert all(lag is not None for lag in lags)


def test_a_closing_sell_that_fills_is_an_xpass_with_agreeing_views(rig: Rig) -> None:
    rig.market.fill_sells = True
    [fill] = [c for c in _cases("nse-b") if cases_views.path_of(c) == "fill"]
    results = cases_views.run_cases(rig.run, [fill], timing=FAST)
    result = results[fill.id]
    assert result.verdict == batch.XPASS, result.reason
    views_ = result.record["views"]
    assert views_["mismatches"] == []
    [key] = list(views_["positions"])
    assert views_["positions"][key]["buy_quantity"] == views_["positions"][key]["sell_quantity"]


def test_a_view_that_disagrees_fails_the_case(rig: Rig) -> None:
    rig.run.watcher = views.OrderWatcher()  # a stream that never delivers an update
    [ioc] = [c for c in _cases("nse-b") if cases_views.path_of(c) == "ioc"]
    result = cases_views.run_cases(rig.run, [ioc], timing=cases_views.Timing(stream_wait=0.2))
    assert result[ioc.id].verdict == batch.FAIL
    assert "views" in result[ioc.id].reason and "stream" in result[ioc.id].reason
    notes = [line for line in rig.ledger_lines() if line["type"] == "views"]
    assert notes and notes[-1]["case_id"] == ioc.id and notes[-1]["mismatches"]


def test_fill_paths_skip_at_1530(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRADESMART_LIVE_ORDER_OUTSIDE_HOURS", "1")  # the cut-off, not the hours
    from tests.integration.uat_family_rig import TODAY
    from tradesmart.models.base import IST

    late = __import__("datetime").datetime(TODAY.year, TODAY.month, TODAY.day, 15, 31, tzinfo=IST)
    with modify_views_rig(tmp_path, now=late) as built:
        [fill] = [c for c in _cases("nse-b") if cases_views.path_of(c) == "fill"]
        result = cases_views.run_cases(built.run, [fill], timing=FAST)[fill.id]
        assert result.verdict == batch.SKIP and "before 15:30" in result.reason
        assert built.count("PlaceOrder") == 0


# -- async: AsyncTradeSmart + AsyncTradeSmartStream ---------------------------------------------


async def _run_async(rig: Rig, table: list[Case], **kwargs: Any) -> dict[str, batch.CaseResult]:
    client = install_async(rig.market, rig.broker.async_client(heartbeat_interval=STEADY))
    try:
        async with cases_views.async_order_stream(client) as watcher:
            return await cases_views.run_async_family(
                rig.run, client, table, watcher=watcher, timing=kwargs.get("timing", FAST)
            )
    finally:
        await client.aclose()


async def test_the_five_status_paths_through_the_async_client_and_stream(rig: Rig) -> None:
    sync_places = rig.count("PlaceOrder")
    results = await _run_async(rig, cases_views.ASYNC_CASES)
    for role in ("nse-b", "nfo-nifty-ce-atm"):
        _check_paths(rig, results, _cases(role, asynchronous=True))
    intents = [line for line in rig.ledger_lines() if line["type"] == "intent"]
    assert intents and all(line["detail"]["client"] == "async" for line in intents)
    places = [line for line in intents if line["kind"] == "place"]
    assert rig.count("PlaceOrder") - sync_places == len(places) == 12  # 10 + 2 closing SELLs
    for line in intents:
        assert line["case_id"].endswith("-async")
    records = rig.records()
    assert {r["case_id"] for r in records} == {c.id for c in cases_views.ASYNC_CASES}
    verdicts = {line["case_id"] for line in rig.ledger_lines() if line["type"] == "verdict"}
    assert verdicts == {c.id for c in cases_views.ASYNC_CASES}  # a resume skips them
    open_steps = _by_path(results, _cases("nse-b", asynchronous=True))["open"].record["notes"]
    assert [s["step"] for s in open_steps["steps"]] == ["price-down", "cancel"]


async def test_an_async_case_done_in_the_run_is_not_placed_again(rig: Rig) -> None:
    [ioc] = [c for c in _cases("nse-b", asynchronous=True) if cases_views.path_of(c) == "ioc"]
    first = await _run_async(rig, [ioc])
    placed = rig.count("PlaceOrder")
    again = await _run_async(rig, [ioc])  # the same run: its cached result
    assert again[ioc.id] is first[ioc.id] and again[ioc.id].verdict == batch.PASS
    family.results_of(rig.run).clear()  # as a resumed process: only the ledger knows
    resumed = await _run_async(rig, [ioc])
    assert resumed[ioc.id].verdict == batch.SKIP and "done in run" in resumed[ioc.id].reason
    assert rig.count("PlaceOrder") == placed


async def test_an_ambiguous_async_cancel_is_check_the_account_and_never_repeated(rig: Rig) -> None:
    [trigger] = [c for c in _cases("nse-b", asynchronous=True)
                 if cases_views.path_of(c) == "trigger"]  # fmt: skip
    rig.broker.fail_next("CancelOrder", drills.connect_error())  # nothing goes out
    results = await _run_async(rig, [trigger])
    result = results[trigger.id]
    assert result.verdict == batch.CHECK, result.reason
    [order_id] = result.order_ids
    assert rig.statuses()[order_id] == "TRIGGER_PENDING"
    assert rig.market.cancels == [order_id]  # one attempt, never repeated
    assert rig.session.guard.tripped("nse") is not None
    assert order_id in rig.session.ledger.flagged()


async def test_an_async_rms_refusal_is_an_environment_limit(rig: Rig) -> None:
    rig.market.rms.add("ITC-EQ")
    [open_] = [c for c in _cases("nse-b", asynchronous=True) if cases_views.path_of(c) == "open"]
    result = (await _run_async(rig, [open_]))[open_.id]
    assert result.verdict == batch.ENV_LIMIT and "RED:" in result.reason


async def test_the_stop_file_before_an_async_case_skips_it_without_a_call(rig: Rig) -> None:
    (rig.root / "STOP").write_text("", encoding="utf-8")
    table = _cases("nse-b", asynchronous=True)
    results = await _run_async(rig, table)
    assert all(results[c.id].verdict == batch.SKIP for c in table)
    assert all("STOP" in results[c.id].reason for c in table)
    assert rig.count("PlaceOrder") == 0


async def test_an_async_off_tick_order_accepted_is_recorded_not_failed(rig: Rig) -> None:
    original = rig.market._invalid
    rig.market._invalid = lambda inst, kwargs, order_type: None  # type: ignore[method-assign]
    try:
        [reject] = [c for c in _cases("nse-b", asynchronous=True)
                    if cases_views.path_of(c) == "reject"]  # fmt: skip
        result = (await _run_async(rig, [reject]))[reject.id]
    finally:
        rig.market._invalid = original  # type: ignore[method-assign]
    assert result.verdict == batch.RECORD, result.reason
    [order_id] = result.order_ids
    assert rig.statuses()[order_id] == "CANCELED"  # accepted, so cancelled once
    plan = result.record["plan"]
    assert Decimal(str(plan["price"])) % Decimal("0.05") != 0  # it was sent off the tick
    assert calls.CHECK not in {c["verdict"] for c in result.record["calls"]}


async def test_an_async_ioc_that_rests_fails_and_is_cancelled_once(rig: Rig) -> None:
    original = rig.market._match
    rig.market._match = lambda *args: None  # type: ignore[method-assign]  # the IOC rests
    try:
        [ioc] = [c for c in _cases("nse-b", asynchronous=True) if cases_views.path_of(c) == "ioc"]
        result = (await _run_async(rig, [ioc]))[ioc.id]
    finally:
        rig.market._match = original  # type: ignore[method-assign]
    assert result.verdict == batch.FAIL and "expected CANCELED, saw OPEN" in result.reason
    [order_id] = result.order_ids
    assert rig.statuses()[order_id] == "CANCELED" and rig.market.cancels == [order_id]


async def test_after_ctrl_c_an_async_case_sends_nothing_more(rig: Rig) -> None:
    [trigger] = [c for c in _cases("nse-b", asynchronous=True)
                 if cases_views.path_of(c) == "trigger"]  # fmt: skip
    original = rig.market._match

    def interrupted(*args: Any) -> None:
        rig.session.interrupted = True  # Ctrl-C right after the place call
        original(*args)

    rig.market._match = interrupted  # type: ignore[method-assign]
    try:
        result = (await _run_async(rig, [trigger]))[trigger.id]
    finally:
        rig.market._match = original  # type: ignore[method-assign]
    assert rig.market.cancels == [] and rig.count("CancelOrder") == 0
    assert result.verdict == batch.SKIP and "interrupted" in result.reason


# == Fix round 1 ===============================================================================


def test_a_views_failure_is_the_cases_ledger_verdict_and_the_runs_result(rig: Rig) -> None:
    rig.run.watcher = views.OrderWatcher()  # a stream that never delivers an update
    [ioc] = [c for c in _cases("nse-b") if cases_views.path_of(c) == "ioc"]
    results = cases_views.run_cases(rig.run, [ioc], timing=cases_views.Timing(stream_wait=0.2))
    assert results[ioc.id].verdict == batch.FAIL
    verdicts = [x for x in rig.ledger_lines() if x["type"] == "verdict" and x["case_id"] == ioc.id]
    assert verdicts[-1]["verdict"] == batch.FAIL  # a resume sees the failure
    assert family.results_of(rig.run)[ioc.id].verdict == batch.FAIL
    assert rig.run.recorder.enrich is None  # the views are attached around the batch only


async def test_an_async_family_error_cancels_the_order_and_fails_the_case(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(*args: Any) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(cases_views, "_modify_down", broken)
    [open_] = [c for c in _cases("nse-b", asynchronous=True) if cases_views.path_of(c) == "open"]
    result = (await _run_async(rig, [open_]))[open_.id]
    assert (
        result.verdict == batch.FAIL and "family error" in result.reason and "boom" in result.reason
    )
    [order_id] = result.order_ids
    assert rig.statuses()[order_id] == "CANCELED" and rig.market.cancels == [order_id]
    verdicts = [x for x in rig.ledger_lines() if x["type"] == "verdict"]
    assert verdicts[-1]["case_id"] == open_.id and verdicts[-1]["verdict"] == batch.FAIL


async def test_a_cleanup_error_never_masks_the_family_error(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(*args: Any) -> None:
        raise RuntimeError("boom")

    async def cleanup_fails(*args: Any) -> None:
        raise ValueError("cleanup failed too")

    monkeypatch.setattr(cases_views, "_modify_down", broken)
    monkeypatch.setattr(cases_views, "_cancel_left_open", cleanup_fails)
    [open_] = [c for c in _cases("nse-b", asynchronous=True) if cases_views.path_of(c) == "open"]
    result = (await _run_async(rig, [open_]))[open_.id]
    assert result.verdict == batch.FAIL and "family error: RuntimeError: boom" in result.reason
    assert "cleanup failed too" in result.record["notes"]["cleanup_error"]
    verdicts = [x for x in rig.ledger_lines() if x["type"] == "verdict"]
    assert verdicts[-1]["case_id"] == open_.id and verdicts[-1]["verdict"] == batch.FAIL


async def test_the_async_fill_checks_the_cut_off_and_room_inside_its_instrument_lock(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    positions = rig.run.positions
    lock = positions.one_at_a_time
    room = positions.check_room

    def locked(tsym: str) -> Any:
        events.append("lock")
        return lock(tsym)

    def checked(*args: Any, **kwargs: Any) -> None:
        events.append("room")
        room(*args, **kwargs)

    monkeypatch.setattr(positions, "one_at_a_time", locked)
    monkeypatch.setattr(positions, "check_room", checked)
    now = rig.run.now
    rig.run.now = lambda: (events.append("now"), now())[1]
    [fill] = [c for c in _cases("nse-b", asynchronous=True) if cases_views.path_of(c) == "fill"]
    result = (await _run_async(rig, [fill]))[fill.id]
    assert result.verdict == batch.XFAIL, result.reason
    assert events[:3] == ["lock", "now", "room"], events


async def test_async_cancels_and_modifies_do_not_count_in_the_minute_window(rig: Rig) -> None:
    flags: list[bool] = []
    real_wait = rig.run.pacer.wait

    def spy(*, place: bool = True) -> None:
        flags.append(place)
        real_wait(place=place)

    rig.run.pacer.wait = spy  # type: ignore[method-assign]
    await _run_async(rig, cases_views.ASYNC_CASES)
    assert flags.count(True) == 12  # the place calls only (10 + 2 closing SELLs)
    assert flags.count(False) >= 1  # the async cancels and modifies: per-second window only (D3)
