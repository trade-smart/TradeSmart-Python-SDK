"""Pure helpers of the UAT lifecycle tests (``tests/live_orders/lifecycle.py``), offline."""

from __future__ import annotations

import threading
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.live_orders import lifecycle, safety
from tests.live_orders.safety import (
    CheckTheAccount,
    OrderBudget,
    OrderOutcome,
    OrderSession,
    PlacedOrder,
    Refusal,
)
from tests.unit.test_live_orders_safety import FakeClient, FakeTime, _order, _plan
from tradesmart.errors import APIError
from tradesmart.models.orders import Trade
from tradesmart.models.portfolio import Position
from tradesmart.models.stream import OrderUpdate

UAT = {"TRADESMART_ENV": "uat"}


# -- UAT only -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "environ",
    [{}, {"TRADESMART_ENV": "production"}, {"TRADESMART_ENV": "stage"}, {"TRADESMART_ENV": " "}],
)
def test_lifecycle_refuses_outside_uat_without_opening_a_session(environ: dict[str, str]) -> None:
    opened: list[str] = []

    def open_session() -> OrderSession:
        opened.append("session")
        raise AssertionError("no session may be opened")

    assert lifecycle.uat_only_reason(environ) is not None
    with pytest.raises(Refusal, match="UAT"):
        lifecycle.uat_session(environ, open_session)
    assert opened == []


def test_lifecycle_refuses_a_session_that_is_not_in_uat_mode() -> None:
    session = OrderSession(FakeClient([[]]))  # type: ignore[arg-type]
    with pytest.raises(Refusal, match="UAT"):
        lifecycle.uat_session(UAT, lambda: session)


def test_lifecycle_uses_a_uat_mode_session() -> None:
    session = OrderSession(FakeClient([[]]), uat=True)  # type: ignore[arg-type]
    assert lifecycle.uat_only_reason({"TRADESMART_ENV": " UAT "}) is None
    assert lifecycle.uat_session(UAT, lambda: session) is session


# -- stream order updates ---------------------------------------------------------------


def _update(order_id: str, status: str, **extra: str) -> OrderUpdate:
    return OrderUpdate.model_validate(
        {"t": "om", "norenordno": order_id, "status": status, **extra}
    )


def test_order_updates_keep_statuses_per_order_in_arrival_order() -> None:
    updates = lifecycle.OrderUpdates()
    for update in [
        _update("1", "PENDING"),
        _update("2", "OPEN"),
        _update("1", "open"),
        _update("1", "CANCELLED"),
        OrderUpdate.model_validate({"t": "om", "norenordno": "1"}),  # no status: ignored
    ]:
        updates.add(update)
    assert updates.statuses("1") == ["PENDING", "OPEN", "CANCELED"]
    assert updates.statuses(" 2 ") == ["OPEN"]
    assert updates.statuses("3") == []


def test_order_updates_accept_callbacks_from_another_thread() -> None:
    updates = lifecycle.OrderUpdates()
    threads = [
        threading.Thread(target=updates.add, args=(_update("1", "OPEN"),)) for _ in range(20)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert updates.statuses("1") == ["OPEN"] * 20


@pytest.mark.parametrize("raw", ["CANCELLED", "CANCELED", "cancelled", " Canceled "])
def test_normal_status_spells_cancelled_one_way(raw: str) -> None:
    assert lifecycle.normal_status(raw) == "CANCELED"


def test_normal_status_keeps_other_words_and_none() -> None:
    assert lifecycle.normal_status("complete") == "COMPLETE"
    assert lifecycle.normal_status(None) is None


def test_wait_until_polls_until_true_or_timeout() -> None:
    t = FakeTime()
    calls: list[float] = []

    def ready() -> bool:
        calls.append(t.now)
        return t.now >= 2

    assert lifecycle.wait_until(ready, 5, sleep=t.sleep, clock=t.clock, poll=1) is True
    assert calls == [0, 1, 2]
    t2 = FakeTime()
    assert lifecycle.wait_until(lambda: False, 3, sleep=t2.sleep, clock=t2.clock, poll=1) is False
    assert t2.now == 3


# -- the three sources agree ------------------------------------------------------------


def test_states_agree_for_open_then_cancelled() -> None:
    problems = lifecycle.states_agree(
        "CANCELED",
        ["CANCELED", "OPEN", "OPEN", "PENDING"],
        ["PENDING", "OPEN", "OPEN", "CANCELLED"],
    )
    assert problems == []


def test_states_disagree_when_the_stream_ends_elsewhere() -> None:
    problems = lifecycle.states_agree("CANCELED", ["OPEN", "CANCELED"], ["OPEN", "REJECTED"])
    assert any("stream" in p and "REJECTED" in p for p in problems)


def test_states_disagree_without_stream_updates() -> None:
    problems = lifecycle.states_agree("CANCELED", ["OPEN", "CANCELED"], [])
    assert any("stream" in p for p in problems)


def test_states_disagree_when_open_is_missing() -> None:
    problems = lifecycle.states_agree("CANCELED", ["CANCELED"], ["CANCELED"])
    assert any("order_history" in p and "OPEN" in p for p in problems)
    assert any("stream" in p and "OPEN" in p for p in problems)


def test_states_disagree_when_the_history_has_another_final_state() -> None:
    problems = lifecycle.states_agree(
        "CANCELED", ["OPEN", "REJECTED", "CANCELED"], ["OPEN", "CANCELED"]
    )
    assert any("order_history" in p and "REJECTED" in p for p in problems)


def test_states_disagree_when_the_book_is_not_final() -> None:
    problems = lifecycle.states_agree("OPEN", ["OPEN"], ["OPEN"])
    assert any("order book" in p for p in problems)


# -- tags -------------------------------------------------------------------------------


def test_tags_agree_when_unique_and_in_the_book() -> None:
    placed = [
        PlacedOrder("mcx", "sdklive-mcx-aaa", "1"),
        PlacedOrder("mcx", "sdklive-mcx-bbb", None),
    ]
    book = [_order("1", "CANCELED", tag="sdklive-mcx-aaa"), _order("9", "OPEN", tag="other")]
    assert lifecycle.tag_problems(placed, book) == []


def test_tags_report_a_duplicate_in_this_run() -> None:
    placed = [
        PlacedOrder("mcx", "sdklive-mcx-aaa", "1"),
        PlacedOrder("mcx", "sdklive-mcx-aaa", "2"),
    ]
    book = [
        _order("1", "CANCELED", tag="sdklive-mcx-aaa"),
        _order("2", "CANCELED", tag="sdklive-mcx-aaa"),
    ]
    problems = lifecycle.tag_problems(placed, book)
    assert any("this run" in p for p in problems)
    assert any("more than one order" in p for p in problems)


def test_tags_report_a_suite_tag_shared_by_two_orders_in_the_book() -> None:
    book = [
        _order("1", "REJECTED", tag="sdklive-nse-x"),
        _order("2", "REJECTED", tag="sdklive-nse-x"),
    ]
    problems = lifecycle.tag_problems([], book)
    assert problems == ["the order book has more than one order with tag sdklive-nse-x"]


def test_tags_report_a_placed_order_missing_from_the_book() -> None:
    placed = [PlacedOrder("mcx", "sdklive-mcx-aaa", "1")]
    problems = lifecycle.tag_problems(placed, [_order("7", "OPEN", tag="other")])
    assert problems == ["tag sdklive-mcx-aaa (order 1) is not in orders()"]


def test_tags_report_a_tag_on_another_order_id() -> None:
    placed = [PlacedOrder("mcx", "sdklive-mcx-aaa", "1")]
    problems = lifecycle.tag_problems(placed, [_order("2", "OPEN", tag="sdklive-mcx-aaa")])
    assert problems == ["tag sdklive-mcx-aaa belongs to order 2 in orders(), not 1"]


# -- positions --------------------------------------------------------------------------


def _position(**fields: str) -> Position:
    return Position.model_validate({"exch": "NSE", "tsym": "SBIN-EQ", **fields})


# -- tracked run ------------------------------------------------------------------------


def _uat_session(client: FakeClient) -> OrderSession:
    return OrderSession(client, budget=OrderBudget(10), uat=True)  # type: ignore[arg-type]


def _tracked(session: OrderSession, plan: Any = None, **kwargs: Any) -> Any:
    t = FakeTime()
    record: dict[str, Any] = {}
    kwargs.setdefault("settle_seconds", 1.0)
    kwargs.setdefault("poll_interval", 1.0)
    outcome = lifecycle.run_tracked(
        session,
        plan if plan is not None else _plan(),
        record,
        sleep=t.sleep,
        clock=t.clock,
        **kwargs,
    )
    return outcome, record


def test_run_tracked_records_the_tag_and_order_id_and_accepts_a_fill() -> None:
    client = FakeClient([[], [_order("1001", "COMPLETE", fillshares="1")]])
    session = _uat_session(client)
    outcome, _ = _tracked(session)
    assert outcome.filled is True
    assert session.placed == [PlacedOrder("mcx", "sdklive-mcx-abc123", "1001")]
    assert client.count("place_order") == 1


def test_run_tracked_records_the_tag_when_the_run_fails_after_sending() -> None:
    client = FakeClient([[], [_order("1001", "OPEN")]])
    session = _uat_session(client)
    with pytest.raises(CheckTheAccount):
        _tracked(session, cancel_settle_seconds=1.0)
    assert session.placed == [PlacedOrder("mcx", "sdklive-mcx-abc123", "1001")]
    assert client.count("place_order") == 1 and client.count("cancel_order") == 1


def test_run_tracked_records_nothing_when_nothing_was_sent() -> None:
    client = FakeClient([[_order("9", "OPEN", tag="other")]])
    session = _uat_session(client)
    with pytest.raises(Refusal, match="open order"):
        _tracked(session)
    assert session.placed == [] and client.count("place_order") == 0


def test_run_tracked_refuses_outside_uat_mode() -> None:
    client = FakeClient([[]])
    session = OrderSession(client)  # type: ignore[arg-type]
    with pytest.raises(Refusal, match="UAT"):
        _tracked(session)
    assert client.calls == []


def test_order_session_starts_with_no_placed_orders() -> None:
    assert OrderSession(FakeClient([[]])).placed == []  # type: ignore[arg-type]


# -- the lifecycle test module: skips cleanly, wired to the tracked run -----------------


class _Request:
    """Stand-in for ``pytest.FixtureRequest``: records which fixtures were asked for."""

    def __init__(self, session: OrderSession | None = None) -> None:
        self.session = session
        self.asked: list[str] = []

    def getfixturevalue(self, name: str) -> Any:
        self.asked.append(name)
        if self.session is None:
            raise AssertionError("no session may be opened")
        return self.session


def _uat_session_fixture() -> Any:
    from tests.live_orders import test_uat_lifecycle

    return test_uat_lifecycle.uat_session._get_wrapped_function()


@pytest.mark.parametrize("name", [None, "production", "stage"])
def test_uat_fixture_skips_outside_uat_without_opening_the_session(
    monkeypatch: pytest.MonkeyPatch, name: str | None
) -> None:
    if name is None:
        monkeypatch.delenv("TRADESMART_ENV", raising=False)
    else:
        monkeypatch.setenv("TRADESMART_ENV", name)
    request = _Request()
    with pytest.raises(pytest.skip.Exception, match="UAT"):
        _uat_session_fixture()(request)
    assert request.asked == []


def test_uat_fixture_skips_a_session_that_is_not_in_uat_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRADESMART_ENV", "uat")
    request = _Request(OrderSession(FakeClient([[]])))  # type: ignore[arg-type]
    with pytest.raises(pytest.skip.Exception, match="UAT"):
        _uat_session_fixture()(request)
    assert request.asked == ["order_session"]


def test_uat_fixture_returns_the_uat_mode_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRADESMART_ENV", "uat")
    session = _uat_session(FakeClient([[]]))
    assert _uat_session_fixture()(_Request(session)) is session


def test_uat_fixture_refuses_an_invalid_environment_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRADESMART_ENV", "../uat")
    with pytest.raises(OSError, match="TRADESMART_ENV"):
        _uat_session_fixture()(_Request())


def test_lifecycle_tests_only_use_the_uat_fixtures() -> None:
    """No lifecycle test asks for ``order_session`` or ``record`` directly: a session-scoped
    fixture would be set up (and call the API) before the UAT check could skip."""
    import inspect

    from tests.live_orders import test_uat_lifecycle

    tests = [
        value
        for name, value in vars(test_uat_lifecycle).items()
        if name.startswith("test_") and callable(value)
    ]
    assert len(tests) == 7
    for test in tests:
        assert set(inspect.signature(test).parameters) == {
            "segment_id",
            "uat_session",
            "uat_record",
        }
    assert test_uat_lifecycle.pytestmark.name == "live_orders"  # deselected by default


def test_lifecycle_tests_send_orders_only_through_the_tracked_run() -> None:
    from tests.live_orders import test_uat_lifecycle

    source = Path(test_uat_lifecycle.__file__).read_text(encoding="utf-8")
    assert "lifecycle.run_tracked(" in source
    assert "run_order(" not in source and "allow_fill" not in source


# -- the UAT matrix: segments ----------------------------------------------------------


def test_uat_segments_cover_the_matrix() -> None:
    segments = lifecycle.UAT_SEGMENTS
    assert list(segments) == ["mcx", "nse", "bse", "nfo", "nfoopt", "bfoopt"]
    assert segments["nse"].tradingsymbol == "ITC-EQ" and segments["nse"].product == "CNC"
    assert (segments["bse"].exchange, segments["bse"].tradingsymbol) == ("BSE", "ITC")
    assert (segments["nfo"].instrument, segments["nfo"].symbol) == ("FUTIDX", "NIFTY")
    nfo_opt, bfo_opt = segments["nfoopt"], segments["bfoopt"]
    assert (nfo_opt.exchange, nfo_opt.instrument, nfo_opt.symbol) == ("NFO", "OPTIDX", "NIFTY")
    # BFO script master: SENSEX options have Symbol BSXOPT, its futures BSXFUT (NFO: both NIFTY)
    assert (bfo_opt.exchange, bfo_opt.instrument, bfo_opt.symbol) == ("BFO", "OPTIDX", "BSXOPT")
    assert (bfo_opt.future_symbol, bfo_opt.label) == ("BSXFUT", "SENSEX")
    assert (nfo_opt.future_symbol, nfo_opt.label) == (None, "NIFTY")
    assert {nfo_opt.product, bfo_opt.product} == {"NRML"}


def test_future_segment_looks_up_the_index_future_by_its_own_symbol() -> None:
    bfo = safety.future_segment(lifecycle.UAT_SEGMENTS["bfoopt"])
    assert (bfo.exchange, bfo.instrument, bfo.symbol, bfo.label) == (
        "BFO",
        "FUTIDX",
        "BSXFUT",
        "SENSEX",
    )
    nfo = safety.future_segment(lifecycle.UAT_SEGMENTS["nfoopt"])
    assert (nfo.instrument, nfo.symbol, nfo.label) == ("FUTIDX", "NIFTY", "NIFTY")


def test_round_trips_use_intraday_on_cash_and_options_and_nrml_on_futures() -> None:
    assert lifecycle.ROUND_TRIP_PRODUCT == {
        "nse": "MIS",
        "bse": "MIS",
        "nfo": "NRML",
        "nfoopt": "MIS",
        "bfoopt": "MIS",
    }


def test_mcx_is_skipped_in_uat_with_a_reason() -> None:
    assert "MCX" in lifecycle.uat_segment_skip_reason("mcx")
    assert all(lifecycle.uat_segment_skip_reason(s) is None for s in lifecycle.ROUND_TRIP_PRODUCT)


def test_production_segments_are_unchanged() -> None:
    assert list(safety.SEGMENTS) == ["mcx", "nse", "nfo"]
    assert safety.SEGMENTS["nse"].tradingsymbol == "SBIN-EQ"


# -- prices of the matrix ---------------------------------------------------------------


def test_at_last_price_stays_within_the_circuit_limits() -> None:
    tick = Decimal("0.05")
    buy = lifecycle.at_last_price(Decimal("100.03"), tick, "BUY", upper_circuit=Decimal("100.00"))
    assert buy == Decimal("100.00")
    sell = lifecycle.at_last_price(Decimal("100.07"), tick, "SELL", lower_circuit=Decimal("100.10"))
    assert sell == Decimal("100.10")
    inside = lifecycle.at_last_price(
        Decimal("100.03"), tick, "BUY", lower_circuit=Decimal("90"), upper_circuit=Decimal("110")
    )
    assert inside == Decimal("100.05")


def test_run_tracked_reads_the_net_position_before_a_buy() -> None:
    client = FakeClient([[], [_order("1001", "COMPLETE", fillshares="1")]], positions=[[]])
    session = _uat_session(client)
    _, record = _tracked(session)
    assert record["net_before"] == 0
    assert client.calls.index(("positions", None)) < [n for n, _ in client.calls].index(
        "place_order"
    )


def test_run_tracked_sends_no_buy_when_positions_cannot_be_read() -> None:
    client = FakeClient([[]], positions=[RuntimeError("down")])
    session = _uat_session(client)
    with pytest.raises(Refusal, match="positions\\(\\) could not be read"):
        _tracked(session)
    assert client.count("place_order") == 0 and session.budget.used == 0


def test_run_tracked_reads_no_positions_for_a_sell() -> None:
    client = FakeClient([[], [_order("1001", "COMPLETE", fillshares="1")]])
    _tracked(_uat_session(client), plan=_plan(transaction_type="SELL"))
    assert client.count("positions") == 0


def test_at_last_price_rounds_toward_a_fill() -> None:
    assert lifecycle.at_last_price(Decimal("812.35"), Decimal("0.05"), "BUY") == Decimal("812.35")
    assert lifecycle.at_last_price(Decimal("812.33"), Decimal("0.05"), "BUY") == Decimal("812.35")
    assert lifecycle.at_last_price(Decimal("812.33"), Decimal("0.05"), "SELL") == Decimal("812.30")


@pytest.mark.parametrize(("last", "tick"), [(None, Decimal("1")), (Decimal("0"), Decimal("1"))])
def test_at_last_price_refuses_without_a_price(last: Decimal | None, tick: Decimal) -> None:
    with pytest.raises(Refusal, match="last price"):
        lifecycle.at_last_price(last, tick, "BUY")


def test_at_last_price_refuses_without_a_tick() -> None:
    with pytest.raises(Refusal, match="tick size"):
        lifecycle.at_last_price(Decimal("10"), None, "BUY")


def test_stop_buy_triggers_above_the_market_with_a_limit_above_the_trigger() -> None:
    trigger, price = lifecycle.stop_buy_prices(Decimal("400"), Decimal("0.05"), Decimal("480"))
    assert (trigger, price) == (Decimal("412.00"), Decimal("416.00"))


def test_option_stops_sit_further_from_the_market() -> None:
    assert lifecycle.stop_ratios("nse") == (Decimal("1.03"), Decimal("1.04"))
    assert lifecycle.stop_ratios("nfo") == (Decimal("1.03"), Decimal("1.04"))
    for segment_id in ("nfoopt", "bfoopt"):
        assert lifecycle.stop_ratios(segment_id) == (Decimal("1.10"), Decimal("1.12"))
    trigger, price = lifecycle.stop_buy_prices(
        Decimal("100"), Decimal("0.05"), None, ratios=lifecycle.stop_ratios("nfoopt")
    )
    assert (trigger, price) == (Decimal("110.00"), Decimal("112.00"))


def test_stop_buy_stays_within_the_upper_circuit() -> None:
    trigger, price = lifecycle.stop_buy_prices(Decimal("400"), Decimal("0.05"), Decimal("414"))
    assert (trigger, price) == (Decimal("412.00"), Decimal("414.00"))


def test_stop_buy_refuses_when_the_circuit_leaves_no_room() -> None:
    with pytest.raises(Refusal, match="upper circuit"):
        lifecycle.stop_buy_prices(Decimal("400"), Decimal("0.05"), Decimal("405"))


def test_stop_modify_moves_trigger_and_price_up_one_tick_within_the_circuit() -> None:
    tick = Decimal("0.05")
    assert lifecycle.stop_modify(Decimal("412"), Decimal("416"), tick, None) == (
        Decimal("412.05"),
        Decimal("416.05"),
    )
    assert lifecycle.stop_modify(Decimal("412"), Decimal("414"), tick, Decimal("414")) == (
        Decimal("412.05"),
        Decimal("414"),
    )


# -- options: the at-the-money strike with a last price ---------------------------------


def _option_row(
    token: str, strike: str, expiry: str = "25-NOV-2026", **extra: str
) -> dict[str, str]:
    return {
        "Exchange": "NFO",
        "Token": token,
        "LotSize": "65",
        "Symbol": "NIFTY",
        "TradingSymbol": f"NIFTY{expiry[:2]}NOV26C{strike}",
        "Expiry": expiry,
        "Instrument": "OPTIDX",
        "OptionType": "CE",
        "StrikePrice": strike,
        "TickSize": "0.05",
        **extra,
    }


_FUTURE_ROW = {
    "Exchange": "NFO",
    "Token": "500",
    "LotSize": "65",
    "Symbol": "NIFTY",
    "TradingSymbol": "NIFTY25NOV26F",
    "Expiry": "25-NOV-2026",
    "Instrument": "FUTIDX",
    "TickSize": "0.10",
}
_OPTION_ROWS = [
    _FUTURE_ROW,
    _option_row("601", "25900"),
    _option_row("602", "26000"),
    _option_row("603", "26100"),
    _option_row("604", "26000", expiry="02-DEC-2026"),  # a later expiry
    _option_row("605", "26050", expiry="11-NOV-2026"),  # expires today: never used
    _option_row("606", "26000", OptionType="PE"),
]


class _QuoteClient:
    def __init__(self, prices: dict[str, str]) -> None:
        self.prices = prices
        self.quoted: list[str] = []

    def quote(self, exchange: str, token: str) -> Any:
        from tradesmart.models.market import Quote

        self.quoted.append(token)
        if token not in self.prices:
            raise RuntimeError("no such token")
        return Quote.model_validate({"stat": "Ok", "lp": self.prices[token], "ti": "0.05"})


def _pick(client: _QuoteClient, record: dict[str, Any] | None = None) -> Any:
    return lifecycle.pick_option(
        client,  # type: ignore[arg-type]
        lifecycle.UAT_SEGMENTS["nfoopt"],
        _OPTION_ROWS,
        date(2026, 11, 11),
        record if record is not None else {},
    )


def _bfo_row(
    token: str,
    tradingsymbol: str,
    symbol: str,
    instrument: str = "OPTIDX",
    strike: str = "0",
    option_type: str = "CE",
    expiry: str = "26-NOV-2026",
) -> dict[str, str]:
    """A BFO script-master row as observed on production (2026-11-12)."""
    return {
        "Exchange": "BFO",
        "Token": token,
        "LotSize": "20",
        "Symbol": symbol,
        "TradingSymbol": tradingsymbol,
        "Expiry": expiry,
        "Instrument": instrument,
        "OptionType": option_type if instrument == "OPTIDX" else "XX",
        "StrikePrice": strike,
        "TickSize": "0.05",
    }


def _sensex_ce(token: str, strike: int) -> dict[str, str]:
    return _bfo_row(token, f"SENSEX26NOV{strike}CE", "BSXOPT", strike=str(strike))


# SENSEX: future Symbol BSXFUT, options Symbol BSXOPT. SENSEX 50 (SX50FUT / SX50OPT) has an
# earlier expiry and strikes right at the money: picked only if the lookup were too loose.
_BFO_ROWS = [
    _bfo_row("1100929", "SENSEX26NOVFUT", "BSXFUT", instrument="FUTIDX"),
    _sensex_ce("1101", 85000),
    _sensex_ce("1102", 85100),
    _sensex_ce("1103", 85200),
    _bfo_row("1104", "SENSEX26NOV85100PE", "BSXOPT", strike="85100", option_type="PE"),
    _bfo_row("900", "SENSEX5026NOVFUT", "SX50FUT", instrument="FUTIDX", expiry="19-NOV-2026"),
    _bfo_row("901", "SENSEX5026NOV85020CE", "SX50OPT", strike="85020", expiry="19-NOV-2026"),
    _bfo_row("902", "SENSEX5026DEC85020CE", "SX50OPT", strike="85020", expiry="24-DEC-2026"),
]


def test_pick_option_finds_the_sensex_future_and_ce_by_their_bfo_symbols() -> None:
    client = _QuoteClient({"1100929": "85020.00", "1101": "310.00", "1102": "0.00"})
    record: dict[str, Any] = {}
    inst = lifecycle.pick_option(
        client,  # type: ignore[arg-type]
        lifecycle.UAT_SEGMENTS["bfoopt"],
        _BFO_ROWS,
        date(2026, 11, 11),
        record,
    )
    assert (inst.tradingsymbol, inst.token, inst.lot_size) == ("SENSEX26NOV85000CE", "1101", 20)
    assert inst.expiry == date(2026, 11, 26)
    assert record["option"]["underlying"] == "SENSEX26NOVFUT"
    assert client.quoted == ["1100929", "1101"]  # never a SENSEX 50 (SX50FUT / SX50OPT) row


def test_pick_option_never_takes_sensex_50_rows_for_sensex() -> None:
    sensex_50 = [r for r in _BFO_ROWS if r["Symbol"].startswith("SX50")]
    client = _QuoteClient({"900": "85020.00", "901": "100.00", "902": "100.00"})
    with pytest.raises(Refusal, match="no SENSEX index future .*BFO script master"):
        lifecycle.pick_option(
            client,  # type: ignore[arg-type]
            lifecycle.UAT_SEGMENTS["bfoopt"],
            sensex_50,
            date(2026, 11, 11),
            {},
        )
    assert client.quoted == []


def test_pick_option_names_sensex_when_the_bfo_options_are_missing() -> None:
    rows = [r for r in _BFO_ROWS if r["Symbol"] != "BSXOPT"]
    client = _QuoteClient({"1100929": "85020.00", "901": "100.00"})
    with pytest.raises(Refusal, match="no OPTIDX SENSEX CE of a future expiry in the BFO"):
        lifecycle.pick_option(
            client,  # type: ignore[arg-type]
            lifecycle.UAT_SEGMENTS["bfoopt"],
            rows,
            date(2026, 11, 11),
            {},
        )
    assert client.quoted == ["1100929"]


def test_pick_option_takes_the_nearest_expiry_strike_closest_to_the_future() -> None:
    client = _QuoteClient({"500": "26040.00", "602": "120.50"})
    record: dict[str, Any] = {}
    inst = _pick(client, record)
    assert (inst.tradingsymbol, inst.token, inst.lot_size) == ("NIFTY25NOV26C26000", "602", 65)
    assert inst.tick_size == Decimal("0.05") and inst.expiry == date(2026, 11, 25)
    assert client.quoted == ["500", "602"]
    assert record["option"]["underlying_last_price"] == "26040.00"
    assert record["option"]["strike"] == "26000"


def test_pick_option_moves_to_the_next_strike_without_a_last_price() -> None:
    client = _QuoteClient({"500": "26040.00", "602": "0.00", "603": "75.00"})
    record: dict[str, Any] = {}
    inst = _pick(client, record)
    assert inst.token == "603"
    assert client.quoted == ["500", "602", "603"]
    assert record["option"]["tried"] == [
        {"tradingsymbol": "NIFTY25NOV26C26000", "lp": "0.00"},
        {"tradingsymbol": "NIFTY25NOV26C26100", "lp": "75.00"},
    ]


def test_pick_option_skips_when_no_strike_has_a_last_price() -> None:
    client = _QuoteClient({"500": "26040.00", "601": "0", "602": "0", "603": "0"})
    with pytest.raises(Refusal, match="no NIFTY CE strike .* last price"):
        _pick(client)


def test_pick_option_skips_without_a_future_price() -> None:
    with pytest.raises(Refusal, match="NIFTY25NOV26F"):
        _pick(_QuoteClient({"500": "0.00"}))


def test_pick_option_skips_without_the_underlying_future() -> None:
    client = _QuoteClient({})
    rows = [r for r in _OPTION_ROWS if r is not _FUTURE_ROW]
    with pytest.raises(Refusal, match="no NIFTY index future .*at-the-money"):
        lifecycle.pick_option(
            client,  # type: ignore[arg-type]
            lifecycle.UAT_SEGMENTS["nfoopt"],
            rows,
            date(2026, 11, 11),
            {},
        )
    assert client.quoted == []


class _ScriptMasterClient:
    def __init__(self, result: Any) -> None:
        self.result = result
        self.asked: list[str] = []

    def instruments(self, exchange: str) -> list[dict[str, str]]:
        self.asked.append(exchange)
        if isinstance(self.result, BaseException):
            raise self.result
        rows: list[dict[str, str]] = self.result
        return rows


def test_script_master_reason_is_none_with_rows() -> None:
    client = _ScriptMasterClient([_FUTURE_ROW])
    assert lifecycle.script_master_reason(client, "BFO") is None  # type: ignore[arg-type]
    assert client.asked == ["BFO"]


@pytest.mark.parametrize(
    ("result", "words"),
    [
        ([], "is empty"),
        (APIError("no file", endpoint="BFO_symbols.txt.zip", http_status=404), "APIError"),
        (httpx.ConnectError("refused"), "ConnectError"),
        (ValueError("bad zip"), "ValueError"),
        (OSError("disk"), "OSError"),
    ],
    ids=["empty", "api-error", "http-error", "unreadable", "os-error"],
)
def test_script_master_reason_names_what_is_missing(result: Any, words: str) -> None:
    reason = lifecycle.script_master_reason(_ScriptMasterClient(result), "BFO")  # type: ignore[arg-type]
    assert reason is not None
    assert reason.startswith("the BFO script master is unavailable on this environment")
    assert words in reason


def test_script_master_reason_lets_a_bug_through() -> None:
    with pytest.raises(KeyError):
        lifecycle.script_master_reason(_ScriptMasterClient(KeyError("x")), "BFO")  # type: ignore[arg-type]


def test_pick_option_skips_when_the_script_master_has_no_options() -> None:
    client = _QuoteClient({"500": "26040.00"})
    with pytest.raises(Refusal, match="OPTIDX NIFTY CE"):
        lifecycle.pick_option(
            client,  # type: ignore[arg-type]
            lifecycle.UAT_SEGMENTS["nfoopt"],
            [_FUTURE_ROW],
            date(2026, 11, 11),
            {},
        )


# -- positions and trades ---------------------------------------------------------------


def test_net_quantity_sums_the_instrument_rows() -> None:
    rows = [
        _position(netqty="10", prd="I"),
        _position(netqty="-4", prd="C"),
        _position(tsym="INFY-EQ", netqty="7"),
        _position(),  # no netqty
    ]
    assert lifecycle.net_quantity(rows, "nse", "SBIN-EQ") == 6
    assert lifecycle.net_quantity([], "NSE", "SBIN-EQ") == 0


def _trade(order_id: str, **fields: str) -> Trade:
    return Trade.model_validate({"norenordno": order_id, **fields})


def test_trade_book_fill_is_the_largest_running_total_never_the_sum() -> None:
    # fillshares is the order's running total, repeated on each trade row (flqty: this trade).
    trades = [
        _trade("1", flqty="30", fillshares="30"),
        _trade("1", flqty="35", fillshares="65"),
        _trade("2", flqty="65", fillshares="65"),
    ]
    assert lifecycle.trade_book_fill(trades, "1") == 65
    assert lifecycle.trade_book_fill(trades, "2") == 65
    assert lifecycle.trade_book_fill(trades, "3") is None
    assert lifecycle.trade_book_fill([_trade("1", flqty="30")], "1") is None


def test_the_summing_helper_is_gone() -> None:
    assert not hasattr(lifecycle, "filled_quantity")


# -- how much of a fill to close ---------------------------------------------------------


def _quantity(client: FakeClient, outcome: OrderOutcome, plan_quantity: int = 65) -> Any:
    t = FakeTime()
    record: dict[str, Any] = {}
    quantity = lifecycle.close_quantity(
        _uat_session(client),
        _plan(quantity=plan_quantity),
        outcome,
        record,
        wait=3.0,
        sleep=t.sleep,
        clock=t.clock,
    )
    return quantity, record


def _filled(quantity: int | None) -> OrderOutcome:
    return OrderOutcome(order_id="1001", filled=True, filled_quantity=quantity)


def test_close_quantity_is_the_filled_quantity_of_the_order_book_row() -> None:
    client = FakeClient([[]])
    quantity, record = _quantity(client, _filled(30))
    assert quantity == 30 and record["filled_quantity"] == 30
    # Known from run_order's final order-book row: no re-read of the book; trades() is
    # consulted once (the larger of the two counts).
    assert client.calls == [("trades", None)]


@pytest.mark.parametrize(
    ("book", "totals", "expected"),
    [
        (30, ("30", "65"), 65),  # the book row lags the trade book
        (65, ("30",), 65),  # the trade book lags the order book
        (40, ("95",), 65),  # never more than the order quantity
        (None, ("10", "30"), 30),  # no usable book value: the trade book alone
    ],
)
def test_close_quantity_is_the_larger_of_the_book_and_the_trade_book(
    book: int | None, totals: tuple[str, ...], expected: int
) -> None:
    trades = [_trade("1001", fillshares=total) for total in totals]
    client = FakeClient([[_order("1001", "CANCELED")]], trades=trades)
    quantity, _ = _quantity(client, _filled(book))
    assert quantity == expected


def test_a_stale_earlier_row_never_under_closes() -> None:
    # Re-review round 1, probe A5: OPEN 10, final CANCELED without fillshares, trades total 30.
    trades = [
        _trade("1001", flqty="10", fillshares="10"),
        _trade("1001", flqty="20", fillshares="30"),
    ]
    client = FakeClient(
        [
            [],
            [_order("1001", "OPEN", fillshares="10")],
            [_order("1001", "OPEN", fillshares="10")],
            [_order("1001", "CANCELED")],
        ],
        trades=trades,
    )
    session = _uat_session(client)
    outcome, record = _tracked(session, plan=_plan(quantity=65))
    assert outcome.filled is True and outcome.filled_quantity is None
    t = FakeTime()
    quantity = lifecycle.close_quantity(
        session, _plan(quantity=65), outcome, record, wait=3.0, sleep=t.sleep, clock=t.clock
    )
    assert quantity == 30


def test_close_quantity_never_exceeds_the_order_quantity() -> None:
    quantity, record = _quantity(FakeClient([[]]), _filled(95))
    assert quantity == 65 and record["fill_above_order_quantity"] == 95


def test_close_quantity_rereads_the_order_book_while_unknown() -> None:
    client = FakeClient(
        [[_order("1001", "CANCELED")], [_order("1001", "CANCELED", fillshares="30")]]
    )
    quantity, _ = _quantity(client, _filled(None))
    assert quantity == 30 and client.count("orders") == 2


def test_close_quantity_falls_back_to_the_largest_trade_book_total() -> None:
    trades = [
        _trade("1001", flqty="30", fillshares="30"),
        _trade("1001", flqty="35", fillshares="65"),
    ]
    client = FakeClient([[_order("1001", "COMPLETE")]], trades=trades)
    quantity, _ = _quantity(client, _filled(None))
    assert quantity == 65


@pytest.mark.parametrize("known", [None, 0])
def test_close_quantity_unknown_fails_without_a_close(known: int | None) -> None:
    client = FakeClient([[_order("1001", "COMPLETE", fillshares="0")]])
    with pytest.raises(CheckTheAccount, match="fill quantity unknown for order 1001"):
        _quantity(client, _filled(known))
    assert client.count("place_order") == 0 and client.count("orders") >= 2


# -- tracked run: caps that leave room to close a fill ----------------------------------


def _shaped(**overrides: Any) -> Any:
    return _plan(**overrides)


def test_a_buy_is_sent_only_when_an_order_is_left_to_close_it() -> None:
    client = FakeClient([[]])
    session = OrderSession(client, budget=OrderBudget(2, used=1), uat=True)  # type: ignore[arg-type]
    with pytest.raises(Refusal, match="close"):
        _tracked(session)
    assert client.count("place_order") == 0 and session.budget.used == 1


def test_a_closing_sell_may_use_the_last_order() -> None:
    client = FakeClient([[], [_order("1001", "COMPLETE", fillshares="1")]])
    session = OrderSession(client, budget=OrderBudget(2, used=1), uat=True)  # type: ignore[arg-type]
    outcome, _ = _tracked(session, plan=_shaped(transaction_type="SELL"))
    assert outcome.filled and session.budget.used == 2


def test_the_per_test_cap_is_two_orders() -> None:
    assert lifecycle.PER_TEST_CAP == 2
    client = FakeClient([[], [_order("1001", "COMPLETE", fillshares="1")]])
    session = _uat_session(client)
    session.test_budget = OrderBudget(lifecycle.PER_TEST_CAP)
    _tracked(session)
    assert session.test_budget.used == 1
    _tracked(session, plan=_shaped(transaction_type="SELL"))
    assert session.test_budget.used == 2
    with pytest.raises(Refusal, match="per test"):
        _tracked(session, plan=_shaped(transaction_type="SELL"))
    assert client.count("place_order") == 2


# -- closing a fill ---------------------------------------------------------------------


class _ClosingClient(FakeClient):
    """A fake that also quotes; ``trades`` lists the closing order's fills."""

    def __init__(self, books: list[Any], *, lp: str = "812.33", **kwargs: Any) -> None:
        super().__init__(books, **kwargs)
        self.lp = lp

    def quote(self, exchange: str, token: str) -> Any:
        from tradesmart.models.market import Quote

        self.calls.append(("quote", (exchange, token)))
        return Quote.model_validate({"stat": "Ok", "lp": self.lp, "ti": "0.05"})


def _close(client: _ClosingClient, quantity: int = 1, net_before: int = 0) -> Any:
    t = FakeTime()
    session = _uat_session(client)
    record: dict[str, Any] = {}
    plan = _plan(product="I", order_type="SL-LMT", trigger_price=Decimal("1"))
    outcome = lifecycle.close_position(
        session,
        plan,
        quantity,
        record,
        net_before=net_before,
        settle_seconds=2.0,
        sleep=t.sleep,
        clock=t.clock,
    )
    return outcome, record, session


def _net_rows(net: int) -> list[Position]:
    return [Position.model_validate({"exch": "MCX", "tsym": "CRUDEOIL18NOV26", "netqty": str(net)})]


def test_close_position_checks_the_net_position_against_the_one_before_the_buy() -> None:
    client = _ClosingClient(
        [[], [_order("1001", "COMPLETE", fillshares="10")]], positions=[_net_rows(5)]
    )
    _, record, _ = _close(client, quantity=10, net_before=5)
    assert record["close"]["net_after_close"] == 5


def test_close_position_reports_an_under_close_seen_in_positions() -> None:
    # Probe A6: 30 were bought, a stale count closed 10; the SELL itself filled in full.
    client = _ClosingClient(
        [[], [_order("1001", "COMPLETE", fillshares="10")]], positions=[_net_rows(20)]
    )
    with pytest.raises(
        CheckTheAccount, match=r"position left open: positions\(\) net quantity .* is 20, 0 before"
    ):
        _close(client, quantity=10, net_before=0)
    assert client.count("place_order") == 1


def test_close_position_reports_unreadable_positions_after_the_close() -> None:
    client = _ClosingClient(
        [[], [_order("1001", "COMPLETE", fillshares="1")]],
        positions=[RuntimeError("down")],
    )
    with pytest.raises(CheckTheAccount, match="position left open: positions\\(\\) unreadable"):
        _close(client)


def test_close_position_sells_the_filled_quantity_at_the_last_price() -> None:
    # Judged by the SELL's own order-book row: an empty (lagging) trade book does not matter.
    client = _ClosingClient([[], [_order("1001", "COMPLETE", fillshares="1")]], trades=[])
    outcome, record, session = _close(client)
    sent = [kwargs for name, kwargs in client.calls if name == "place_order"]
    assert len(sent) == 1
    assert sent[0]["transaction_type"] == "SELL" and sent[0]["order_type"] == "LIMIT"
    assert sent[0]["price"] == Decimal("812.30") and sent[0]["quantity"] == 1
    assert sent[0]["product"] == "I" and sent[0]["validity"] == "DAY"
    assert "trigger_price" not in sent[0] and sent[0]["tag"] != "sdklive-mcx-abc123"
    assert outcome.filled and record["close"]["sold_quantity"] == 1
    assert session.placed[-1].tag == sent[0]["tag"]


def test_close_position_reports_a_position_left_open_when_the_sell_does_not_fill() -> None:
    client = _ClosingClient(
        [[], [_order("1001", "OPEN")], [_order("1001", "OPEN")], [_order("1001", "CANCELED")]]
    )
    with pytest.raises(CheckTheAccount, match=r"position left open \(0 of 1\)"):
        _close(client)
    assert client.count("place_order") == 1 and client.count("cancel_order") == 1


def test_close_position_reports_a_partly_closed_position() -> None:
    # The trade book repeats the running total on each row: it must not make 30 look like 60.
    trades = [
        _trade("1001", flqty="10", fillshares="30"),
        _trade("1001", flqty="20", fillshares="30"),
    ]
    client = _ClosingClient(
        [
            [],
            [_order("1001", "OPEN", fillshares="30")],
            [_order("1001", "OPEN", fillshares="30")],
            [_order("1001", "CANCELED", fillshares="30")],
        ],
        trades=trades,
    )
    with pytest.raises(CheckTheAccount, match=r"position left open \(30 of 60\)"):
        _close(client, quantity=60)
    assert client.count("place_order") == 1 and client.count("cancel_order") == 1


def test_close_position_with_an_unknown_sold_quantity_reports_the_position() -> None:
    client = _ClosingClient([[], [_order("1001", "COMPLETE")]], trades=[])
    with pytest.raises(CheckTheAccount, match=r"position left open \(unknown of 1\)"):
        _close(client)
    assert client.count("place_order") == 1


def test_close_position_reports_a_refused_close() -> None:
    client = _ClosingClient([[_order("9", "OPEN", tag="other")]])
    with pytest.raises(CheckTheAccount, match="position left open"):
        _close(client)
    assert client.count("place_order") == 0


def test_close_position_reports_a_missing_last_price() -> None:
    client = _ClosingClient([[]], lp="0.00")
    with pytest.raises(CheckTheAccount, match="position left open"):
        _close(client)
    assert client.count("place_order") == 0
