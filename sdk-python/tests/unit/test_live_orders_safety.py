"""Mocked unit tests pinning the safety logic of the opt-in live order suite (tests/live_orders).

Nothing here touches the network: the fake client below stands in for ``TradeSmart``.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from tests.live_orders import gtt, safety
from tests.live_orders.safety import (
    CHECK_THE_ACCOUNT,
    CheckTheAccount,
    OrderBudget,
    OrderPlan,
    Recorder,
    Refusal,
    ResponseLog,
)
from tradesmart import (
    ClientConfig,
    NetworkError,
    OrderRejectedError,
    OrderStatusUnknownError,
    RequestValidationError,
)
from tradesmart.enums import GttAlertType, GttValidity, OrderType, TransactionType, Validity
from tradesmart.models.base import IST
from tradesmart.models.market import Quote
from tradesmart.models.orders import Order, OrderHistoryEntry, Trade
from tradesmart.models.portfolio import Limits
from tradesmart.models.user import UserDetails

LIVE_DIR = Path(__file__).resolve().parents[1] / "live_orders"

# -- gates ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "env",
    [
        {},
        {"TRADESMART_LIVE_TESTS": "1"},
        {"TRADESMART_LIVE_ORDER_TESTS": "1"},
        {"TRADESMART_LIVE_TESTS": "1", "TRADESMART_LIVE_ORDER_TESTS": "true"},
        {"TRADESMART_LIVE_TESTS": "yes", "TRADESMART_LIVE_ORDER_TESTS": "1"},
    ],
)
def test_gate_needs_both_variables_set_to_1(env: dict[str, str]) -> None:
    reason = safety.gate_skip_reason(env)
    assert reason is not None
    assert "TRADESMART_LIVE_ORDER_TESTS=1" in reason


def test_gate_opens_only_with_both_variables() -> None:
    env = {"TRADESMART_LIVE_TESTS": "1", "TRADESMART_LIVE_ORDER_TESTS": "1"}
    assert safety.gate_skip_reason(env) is None


@pytest.mark.parametrize(
    ("nodeid", "expected"),
    [
        ("tests/live_orders/test_live_orders.py::test_x[mcx]", True),
        ("tests\\live_orders\\test_live_orders.py::test_x", True),
        ("tests/live/test_live_readonly.py::test_limits", False),
        ("tests/unit/test_live_orders_safety.py::test_gate", False),
        ("tests/unit/test_x.py::test_y[tests/live_orders/z.py]", False),  # path in a param id
    ],
    ids=["orders-file", "orders-windows", "readonly-file", "unit-file", "param-id"],
)
def test_collection_gate_matches_the_live_orders_folder_only(nodeid: str, expected: bool) -> None:
    assert safety.is_live_orders_item(nodeid) is expected


class _Item:
    def __init__(self, nodeid: str) -> None:
        self.nodeid = nodeid
        self.markers: list[Any] = []

    def add_marker(self, marker: Any) -> None:
        self.markers.append(marker)


@pytest.mark.parametrize(
    "env",
    [{}, {"TRADESMART_LIVE_TESTS": "1"}, {"TRADESMART_LIVE_ORDER_TESTS": "1"}],
)
def test_collection_hook_skips_only_live_order_items_without_both_gates(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.live_orders import conftest as live_conftest

    monkeypatch.delenv("TRADESMART_LIVE_TESTS", raising=False)
    monkeypatch.delenv("TRADESMART_LIVE_ORDER_TESTS", raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    order_item = _Item("tests/live_orders/test_live_orders.py::test_x[mcx]")
    other_item = _Item("tests/live/test_live_readonly.py::test_limits")
    live_conftest.pytest_collection_modifyitems(None, [order_item, other_item])  # type: ignore[arg-type,list-item]
    assert [m.name for m in order_item.markers] == ["skip"]
    assert other_item.markers == []


def test_collection_hook_leaves_items_alone_with_both_gates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.live_orders import conftest as live_conftest

    monkeypatch.setenv("TRADESMART_LIVE_TESTS", "1")
    monkeypatch.setenv("TRADESMART_LIVE_ORDER_TESTS", "1")
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)  # a serial run (this suite: parallel)
    item = _Item("tests/live_orders/test_live_orders.py::test_x[mcx]")
    live_conftest.pytest_collection_modifyitems(None, [item])  # type: ignore[arg-type,list-item]
    assert item.markers == []


def test_collection_hook_skips_live_order_items_inside_an_xdist_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.live_orders import conftest as live_conftest

    monkeypatch.setenv("TRADESMART_LIVE_TESTS", "1")
    monkeypatch.setenv("TRADESMART_LIVE_ORDER_TESTS", "1")
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw1")
    item = _Item("tests/live_orders/test_live_orders.py::test_x[mcx]")
    live_conftest.pytest_collection_modifyitems(None, [item])  # type: ignore[arg-type,list-item]
    assert [m.name for m in item.markers] == ["skip"]
    assert "serially" in item.markers[0].kwargs["reason"]


# -- account and funds ------------------------------------------------------------------


def _details(uid: str | None) -> UserDetails:
    return UserDetails.model_validate({"stat": "Ok", "uid": uid} if uid else {"stat": "Ok"})


def test_account_check_passes_for_the_approved_test_account() -> None:
    assert safety.account_refusal(_details("TEST01"), "TEST01", "TEST01") is None


def test_account_check_ignores_case_and_spaces() -> None:
    assert safety.account_refusal(_details("test01 "), " TEST01", "Test01") is None


@pytest.mark.parametrize(
    ("uid", "expected", "client_id"),
    [
        ("REAL123", "TEST01", "TEST01"),  # profile is another account
        ("TEST01", "TEST01", "REAL123"),  # the client sends another uid
        ("TEST01", None, "TEST01"),  # approved account not configured
        ("TEST01", "", "TEST01"),
        (None, "TEST01", "TEST01"),  # profile without a user id
    ],
)
def test_account_check_refuses_anything_else(
    uid: str | None, expected: str | None, client_id: str
) -> None:
    reason = safety.account_refusal(_details(uid), expected, client_id)
    assert reason is not None
    assert "TRADESMART_LIVE_ORDER_ACCOUNT" in reason


def test_account_check_refuses_a_profile_that_is_not_ok() -> None:
    details = UserDetails.model_validate({"stat": "Not_Ok", "uid": "TEST01"})
    assert safety.account_refusal(details, "TEST01", "TEST01") is not None


def test_funds_check_passes_for_zero_and_absent_fields() -> None:
    limits = Limits.model_validate(
        {"stat": "Ok", "cash": "0.00", "payin": "0.00", "brkcollamt": "-5.00", "marginused": "9"}
    )
    assert safety.funds_refusal(limits) is None


@pytest.mark.parametrize("field", safety.FUNDS_FIELDS)
def test_funds_check_refuses_any_positive_cash_or_collateral(field: str) -> None:
    limits = Limits.model_validate({"stat": "Ok", field: "0.01"})
    reason = safety.funds_refusal(limits)
    assert reason is not None
    assert field in reason


def test_funds_fields_cover_the_documented_cash_and_collateral_fields() -> None:
    documented = {
        "cash",
        "payin",
        "remarks_amt",
        "cac_daycash",
        "brkcollamt",
        "unclearedcash",
        "daycash",
        "collateral",
        "cash_coll",
        "grcoll",
    }
    assert documented <= set(safety.FUNDS_FIELDS)


def test_funds_check_refuses_a_limits_answer_that_is_not_ok() -> None:
    assert safety.funds_refusal(Limits.model_validate({"stat": "Not_Ok"})) is not None


# -- price ------------------------------------------------------------------------------


def test_buy_price_is_ten_percent_below_rounded_down_to_tick() -> None:
    # 0.9 * 1234.55 = 1111.095 -> down to the 0.05 tick
    assert safety.buy_limit_price(Decimal("1234.55"), Decimal("0.05"), None) == Decimal("1111.05")


def test_buy_price_never_goes_below_the_lower_circuit() -> None:
    # lc 1180.02 is above 0.9 * lp: round lc UP to the tick so the price stays inside the band.
    price = safety.buy_limit_price(Decimal("1234.55"), Decimal("0.05"), Decimal("1180.02"))
    assert price == Decimal("1180.05")


def test_buy_price_uses_the_lower_circuit_exactly_when_on_tick() -> None:
    price = safety.buy_limit_price(Decimal("6000"), Decimal("1"), Decimal("5760"))
    assert price == Decimal("5760")


def test_buy_price_ignores_a_lower_circuit_below_the_target() -> None:
    assert safety.buy_limit_price(Decimal("100"), Decimal("0.05"), Decimal("80")) == Decimal(
        "90.00"
    )


def test_buy_price_is_quantised_to_the_tick_places() -> None:
    price = safety.buy_limit_price(Decimal("101"), Decimal("0.25"), None)
    assert price == Decimal("90.75")
    assert price.as_tuple().exponent == -2


@pytest.mark.parametrize(
    ("last", "tick", "lc"),
    [
        (None, Decimal("0.05"), None),  # no last price (market closed)
        (Decimal("0"), Decimal("0.05"), None),
        (Decimal("100"), None, None),  # no tick size
        (Decimal("100"), Decimal("0"), None),
        (Decimal("0.05"), Decimal("0.05"), None),  # rounds down to 0
        (Decimal("100"), Decimal("0.05"), Decimal("99.50")),  # band too narrow: not well below
    ],
)
def test_buy_price_refuses_when_no_safe_price_exists(
    last: Decimal | None, tick: Decimal | None, lc: Decimal | None
) -> None:
    with pytest.raises(Refusal):
        safety.buy_limit_price(last, tick, lc)


def test_modify_price_is_one_tick_lower_when_the_band_allows() -> None:
    assert safety.modify_price(
        Decimal("90.00"), Decimal("0.05"), Decimal("80"), Decimal("100")
    ) == (Decimal("89.95"))


def test_modify_price_moves_one_tick_up_at_the_lower_circuit() -> None:
    price = safety.modify_price(Decimal("5760"), Decimal("1"), Decimal("5760"), Decimal("6000"))
    assert price == Decimal("5761")


# -- market hours -----------------------------------------------------------------------


def _ist(y: int, mo: int, d: int, h: int, mi: int) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=IST)


@pytest.mark.parametrize(
    ("segment_id", "now", "is_open"),
    [
        ("mcx", _ist(2026, 11, 10, 21, 0), True),  # Tuesday evening
        ("mcx", _ist(2026, 11, 10, 23, 20), False),  # too close to the 23:30 close
        ("mcx", _ist(2026, 11, 10, 8, 30), False),
        ("nse", _ist(2026, 11, 10, 21, 0), False),
        ("nse", _ist(2026, 11, 11, 10, 0), True),
        ("nfo", _ist(2026, 11, 11, 10, 0), True),
        ("nfo", _ist(2026, 11, 11, 15, 20), False),
        ("nse", _ist(2026, 11, 14, 10, 0), False),  # Saturday
        ("mcx", _ist(2026, 11, 15, 21, 0), False),  # Sunday
    ],
)
def test_market_window(segment_id: str, now: datetime, is_open: bool) -> None:
    reason = safety.market_closed_reason(safety.SEGMENTS[segment_id], now)
    assert (reason is None) is is_open
    if reason is not None:
        assert segment_id.upper() in reason


def test_outside_hours_override_only_when_exactly_1(monkeypatch: pytest.MonkeyPatch) -> None:
    after_close = _ist(2026, 11, 10, 21, 0)
    nse = safety.SEGMENTS["nse"]
    monkeypatch.delenv(safety.OUTSIDE_HOURS_ENV, raising=False)
    assert safety.market_closed_reason(nse, after_close) is not None
    monkeypatch.setenv(safety.OUTSIDE_HOURS_ENV, "yes")
    assert safety.market_closed_reason(nse, after_close) is not None
    monkeypatch.setenv(safety.OUTSIDE_HOURS_ENV, "1")
    assert safety.market_closed_reason(nse, after_close) is None
    # weekends stay closed even with the override
    assert safety.market_closed_reason(nse, _ist(2026, 11, 14, 10, 0)) is not None


def test_segment_ids_and_order_shape() -> None:
    assert list(safety.SEGMENTS) == ["mcx", "nse", "nfo"]
    assert safety.SEGMENTS["mcx"].exchange == "MCX"
    assert safety.SEGMENTS["nse"].exchange == "NSE"
    assert safety.SEGMENTS["nfo"].exchange == "NFO"


# -- instrument from the script master --------------------------------------------------

ROWS = [
    {
        "Exchange": "MCX",
        "Token": "1001",
        "LotSize": "100",
        "Symbol": "CRUDEOIL",
        "TradingSymbol": "CRUDEOIL18NOV26",
        "Expiry": "18-NOV-2026",
        "Instrument": "FUTCOM",
        "TickSize": "1",
    },
    {
        "Exchange": "MCX",
        "Token": "1002",
        "LotSize": "100",
        "Symbol": "CRUDEOIL",
        "TradingSymbol": "CRUDEOIL17DEC26",
        "Expiry": "17-DEC-2026",
        "Instrument": "FUTCOM",
        "TickSize": "1",
    },
    {
        "Exchange": "MCX",
        "Token": "1003",
        "LotSize": "100",
        "Symbol": "CRUDEOIL",
        "TradingSymbol": "CRUDEOIL10NOV26",
        "Expiry": "10-NOV-2026",
        "Instrument": "FUTCOM",
        "TickSize": "1",
    },
    {
        "Exchange": "MCX",
        "Token": "1004",
        "LotSize": "100",
        "Symbol": "CRUDEOIL",
        "TradingSymbol": "CRUDEOIL18NOV26P5000",
        "Expiry": "18-NOV-2026",
        "Instrument": "OPTFUT",
        "TickSize": "0.1",
    },
]


def test_pick_future_takes_the_nearest_expiry_after_today() -> None:
    inst = safety.pick_instrument(safety.SEGMENTS["mcx"], ROWS, date(2026, 11, 10))
    assert inst.tradingsymbol == "CRUDEOIL18NOV26"
    assert inst.token == "1001"
    assert inst.lot_size == 100
    assert inst.tick_size == Decimal("1")


def test_pick_equity_by_trading_symbol() -> None:
    rows = [
        {"Exchange": "NSE", "Token": "3045", "LotSize": "1", "TradingSymbol": "SBIN-EQ"},
        {"Exchange": "NSE", "Token": "1", "LotSize": "1", "TradingSymbol": "SBIN-BE"},
    ]
    inst = safety.pick_instrument(safety.SEGMENTS["nse"], rows, date(2026, 11, 11))
    assert (inst.tradingsymbol, inst.token, inst.lot_size) == ("SBIN-EQ", "3045", 1)
    assert inst.tick_size is None


@pytest.mark.parametrize("lot", ["", "0", "abc", "-1"])
def test_pick_refuses_without_a_usable_lot_size(lot: str) -> None:
    rows = [dict(ROWS[0], LotSize=lot)]
    with pytest.raises(Refusal, match="lot size"):
        safety.pick_instrument(safety.SEGMENTS["mcx"], rows, date(2026, 11, 10))


def test_pick_refuses_when_nothing_matches() -> None:
    with pytest.raises(Refusal):
        safety.pick_instrument(safety.SEGMENTS["nfo"], ROWS, date(2026, 11, 10))


# -- order plan -------------------------------------------------------------------------


def _plan(**overrides: Any) -> OrderPlan:
    values: dict[str, Any] = {
        "segment_id": "mcx",
        "exchange": "MCX",
        "tradingsymbol": "CRUDEOIL18NOV26",
        "token": "1001",
        "quantity": 100,
        "price": Decimal("5400"),
        "product": "M",
        "tag": "sdklive-mcx-abc123",
    }
    values.update(overrides)
    return OrderPlan(**values)


def test_plan_is_a_day_limit_buy_only() -> None:
    kwargs = _plan().place_kwargs()
    assert kwargs == {
        "exchange": "MCX",
        "tradingsymbol": "CRUDEOIL18NOV26",
        "quantity": 100,
        "transaction_type": "BUY",
        "product": "M",
        "order_type": "LIMIT",
        "price": Decimal("5400"),
        "validity": "DAY",
        "tag": "sdklive-mcx-abc123",
    }


def test_tags_are_unique_and_short() -> None:
    tags = {safety.new_tag("mcx") for _ in range(50)}
    assert len(tags) == 50
    assert all(t.startswith(safety.TAG_PREFIX + "mcx-") and len(t) <= 20 for t in tags)


# -- budget -----------------------------------------------------------------------------


def test_budget_allows_three_orders_per_run() -> None:
    budget = OrderBudget()
    assert budget.limit == 3
    for _ in range(3):
        budget.reserve()
    with pytest.raises(Refusal, match="3 orders per run"):
        budget.reserve()
    assert budget.used == 3


# -- recorder and raw responses ---------------------------------------------------------


def test_recorder_writes_json_lines_without_secrets(tmp_path: Path) -> None:
    path = tmp_path / "out" / "live_orders.jsonl"
    rec = Recorder(path)
    rec.write(
        {
            "test": "x",
            "price": Decimal("1.05"),
            "susertoken": "s3cr3t",
            "nested": {"Authorization": "Bearer abc", "uid": "TEST01", "emsg": "no funds"},
            "rows": [{"access_token": "t", "status": "REJECTED", "pan": "ABCDE1234F"}],
        }
    )
    rec.write({"test": "y"})
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["price"] == "1.05"
    assert first["nested"] == {"emsg": "no funds"}
    assert first["rows"] == [{"status": "REJECTED"}]
    assert "s3cr3t" not in lines[0] and "TEST01" not in lines[0] and "ABCDE" not in lines[0]
    assert "recorded_at" in first


def _response(path: str, body: Any, status: int = 200) -> httpx.Response:
    request = httpx.Request("POST", f"https://example.test/NorenWClientAPIv2/{path}")
    return httpx.Response(status, json=body, request=request)


def test_response_log_keeps_order_endpoint_bodies_only() -> None:
    log = ResponseLog()
    log.hook(_response("OrderBook", [{"stat": "Ok"}]))
    log.hook(_response("PlaceOrder", {"stat": "Ok", "norenordno": "1", "uid": "TEST01"}))
    log.hook(_response("ModifyOrder", {"stat": "Ok", "result": "1"}))
    assert log.last("PlaceOrder") == {
        "http_status": 200,
        "body": {"stat": "Ok", "norenordno": "1"},
    }
    assert log.last("ModifyOrder") == {"http_status": 200, "body": {"stat": "Ok", "result": "1"}}
    assert log.last("OrderBook") is None
    assert log.last("CancelOrder") is None


def test_response_log_handles_a_non_json_body() -> None:
    log = ResponseLog()
    request = httpx.Request("POST", "https://example.test/x/CancelOrder")
    log.hook(httpx.Response(502, text="<html>bad gateway</html>", request=request))
    assert log.last("CancelOrder") == {"http_status": 502, "text": "<html>bad gateway</html>"}


def test_response_log_attaches_to_an_httpx_client() -> None:
    log = ResponseLog()
    seen: list[str] = []
    handler = httpx.MockTransport(lambda r: httpx.Response(200, json={"stat": "Ok"}))
    with httpx.Client(
        transport=handler, event_hooks={"response": [lambda r: seen.append("a")]}
    ) as c:
        log.attach(c)
        c.post("https://example.test/x/PlaceOrder")
    assert seen == ["a"]
    assert log.last("PlaceOrder") == {"http_status": 200, "body": {"stat": "Ok"}}


# -- the order lifecycle ----------------------------------------------------------------


def _order(order_id: str, status: str, tag: str = "sdklive-mcx-abc123", **extra: str) -> Order:
    return Order.model_validate(
        {"norenordno": order_id, "status": status, "remarks": tag, "exch": "MCX", **extra}
    )


class FakeClient:
    """Scripted stand-in for TradeSmart: each orders() call pops the next book."""

    client_id = "TEST01"

    def __init__(
        self,
        books: list[list[Order]],
        *,
        place: Any = "1001",
        cancel: Exception | None = None,
        history: list[OrderHistoryEntry] | None = None,
        modify: Any = "1001",
        trades: list[Trade] | Exception | None = None,
        on_place: Any = None,
        positions: list[Any] | None = None,
    ) -> None:
        self.books = books
        self.place_result = place
        self.cancel_error = cancel
        self.history = history if history is not None else []
        self.modify_result = modify
        self.trade_book = trades if trades is not None else []
        self.on_place = on_place
        # positions(): each call pops the next list (the last one repeats); default: no rows
        self.position_books: list[Any] = positions if positions is not None else [[]]
        self.calls: list[tuple[str, Any]] = []

    def positions(self) -> list[Any]:
        self.calls.append(("positions", None))
        rows = (
            self.position_books.pop(0) if len(self.position_books) > 1 else self.position_books[0]
        )
        if isinstance(rows, Exception):
            raise rows
        result: list[Any] = rows
        return result

    def trades(self) -> list[Trade]:
        self.calls.append(("trades", None))
        if isinstance(self.trade_book, Exception):
            raise self.trade_book
        return self.trade_book

    def orders(self) -> list[Order]:
        self.calls.append(("orders", None))
        book = self.books.pop(0) if len(self.books) > 1 else self.books[0]
        if isinstance(book, Exception):
            raise book
        return book

    def place_order(self, **kwargs: Any) -> str:
        self.calls.append(("place_order", kwargs))
        if self.on_place is not None:
            self.on_place()
        if isinstance(self.place_result, BaseException):
            raise self.place_result
        return str(self.place_result)

    def cancel_order(self, order_id: str) -> str:
        self.calls.append(("cancel_order", order_id))
        if self.cancel_error is not None:
            raise self.cancel_error
        return order_id

    def modify_order(self, order_id: str, **kwargs: Any) -> str:
        self.calls.append(("modify_order", (order_id, kwargs)))
        if isinstance(self.modify_result, Exception):
            raise self.modify_result
        return str(self.modify_result)

    def order_history(self, order_id: str) -> list[OrderHistoryEntry]:
        self.calls.append(("order_history", order_id))
        return self.history

    def count(self, name: str) -> int:
        return sum(1 for call, _ in self.calls if call == name)


class FakeTime:
    def __init__(self) -> None:
        self.now = 0.0

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _run(client: FakeClient, budget: OrderBudget | None = None, **kwargs: Any) -> Any:
    t = FakeTime()
    record: dict[str, Any] = {}
    kwargs.setdefault("settle_seconds", 1.0)
    kwargs.setdefault("poll_interval", 1.0)
    outcome = safety.run_order(
        client,  # type: ignore[arg-type]
        _plan(),
        budget if budget is not None else OrderBudget(),
        record,
        sleep=t.sleep,
        clock=t.clock,
        **kwargs,
    )
    return outcome, record


def _rejected(message: str = "RMS: insufficient funds") -> OrderRejectedError:
    return OrderRejectedError(
        f"PlaceOrder failed: {message}",
        endpoint="PlaceOrder",
        http_status=200,
        emsg=message,
        raw={"stat": "Not_Ok", "emsg": message},
    )


def _unknown() -> OrderStatusUnknownError:
    return OrderStatusUnknownError("PlaceOrder", "read timeout")


def test_ack_then_rejected_is_recorded_without_cancel() -> None:
    history = [
        OrderHistoryEntry.model_validate(
            {
                "norenordno": "1001",
                "status": "REJECTED",
                "rejreason": "no funds",
                "norentm": "10:15:02 10-11-2026",
            }
        ),
        OrderHistoryEntry.model_validate({"norenordno": "1001", "status": "PENDING"}),
    ]
    client = FakeClient(
        [[], [_order("1001", "PENDING")], [_order("1001", "REJECTED", rejreason="RMS: no funds")]],
        history=history,
    )
    outcome, record = _run(client)
    assert outcome.order_id == "1001"
    assert outcome.final_status == "REJECTED"
    assert client.count("place_order") == 1
    assert client.count("cancel_order") == 0
    assert record["place"]["outcome"] == "acknowledged"
    assert record["order_book"]["rejreason"] == "RMS: no funds"
    assert [h["status"] for h in record["history"]] == ["REJECTED", "PENDING"]
    assert record["history"][0]["rejreason"] == "no funds"


def test_place_rejection_is_recorded_and_never_resent() -> None:
    client = FakeClient([[]], place=_rejected())
    outcome, record = _run(client)
    assert outcome.order_id is None
    assert outcome.exception == "OrderRejectedError"
    assert client.count("place_order") == 1
    assert client.count("cancel_order") == 0
    assert record["place"] == {
        "outcome": "rejected",
        "exception": "OrderRejectedError",
        "message": "PlaceOrder failed: RMS: insufficient funds",
        "stat": "Not_Ok",
        "emsg": "RMS: insufficient funds",
    }


def test_place_rejection_still_records_a_rejected_row_found_by_tag() -> None:
    client = FakeClient([[], [_order("1009", "REJECTED", rejreason="RMS")]], place=_rejected())
    outcome, record = _run(client)
    assert outcome.order_id == "1009"
    assert outcome.final_status == "REJECTED"
    assert client.count("cancel_order") == 0


@pytest.mark.parametrize("error", [_unknown(), NetworkError("connection reset")])
def test_ambiguous_place_looks_the_order_up_and_never_resends(error: Exception) -> None:
    client = FakeClient([[], [], [_order("1001", "REJECTED", rejreason="RMS")]], place=error)
    outcome, record = _run(client)
    assert client.count("place_order") == 1
    assert outcome.order_id == "1001"
    assert outcome.final_status == "REJECTED"
    assert record["place"]["outcome"] == "unknown"
    assert record["place"]["exception"] == type(error).__name__


def test_ambiguous_place_that_never_shows_up_fails_loudly() -> None:
    client = FakeClient([[]], place=_unknown())
    with pytest.raises(CheckTheAccount, match=CHECK_THE_ACCOUNT):
        _run(client)
    assert client.count("place_order") == 1


def test_validation_error_is_a_test_bug_not_an_order() -> None:
    client = FakeClient([[]], place=RequestValidationError([{"loc": "price"}]))
    with pytest.raises(AssertionError, match="validation"):
        _run(client)
    assert client.count("cancel_order") == 0


def test_open_order_is_cancelled_once_and_reaches_a_final_state() -> None:
    client = FakeClient(
        [[], [_order("1001", "OPEN")], [_order("1001", "OPEN")], [_order("1001", "CANCELED")]]
    )
    outcome, record = _run(client)
    assert client.count("cancel_order") == 1
    assert outcome.final_status == "CANCELED"
    assert outcome.cancelled is True
    assert record["cancel"]["outcome"] == "accepted"


def test_cancel_failure_is_not_retried_and_fails_loudly_when_still_open() -> None:
    client = FakeClient([[], [_order("1001", "OPEN")]], cancel=_unknown())
    with pytest.raises(CheckTheAccount, match="CHECK THE ACCOUNT: order 1001"):
        _run(client)
    assert client.count("cancel_order") == 1
    assert client.count("place_order") == 1


def test_cancel_error_is_fine_when_the_order_got_rejected_meanwhile() -> None:
    error = OrderRejectedError(
        "CancelOrder failed", endpoint="CancelOrder", http_status=200, emsg="Order already rejected"
    )
    client = FakeClient(
        [[], [_order("1001", "OPEN")], [_order("1001", "OPEN")], [_order("1001", "REJECTED")]],
        cancel=error,
    )
    outcome, record = _run(client)
    assert client.count("cancel_order") == 1
    assert outcome.final_status == "REJECTED"
    assert record["cancel"]["exception"] == "OrderRejectedError"


def test_unreadable_status_after_cancel_fails_loudly() -> None:
    client = FakeClient(
        [[], [_order("1001", "OPEN")], [_order("1001", "OPEN")], NetworkError("down")]  # type: ignore[list-item]
    )
    with pytest.raises(CheckTheAccount, match="CHECK THE ACCOUNT: order 1001"):
        _run(client)
    assert client.count("cancel_order") == 1


def test_cancel_runs_in_finally_when_the_open_hook_raises() -> None:
    client = FakeClient(
        [[], [_order("1001", "OPEN")], [_order("1001", "OPEN")], [_order("1001", "CANCELED")]]
    )

    def boom(order: Order, record: dict[str, Any]) -> None:
        raise RuntimeError("hook failed")

    with pytest.raises(RuntimeError, match="hook failed"):
        _run(client, on_open=boom)
    assert client.count("cancel_order") == 1


def test_cancel_runs_when_the_order_book_cannot_be_read_after_an_ack() -> None:
    client = FakeClient(
        [[], NetworkError("down"), NetworkError("down"), [_order("1001", "CANCELED")]]  # type: ignore[list-item]
    )
    outcome, _ = _run(client)
    assert client.count("cancel_order") == 1
    assert outcome.final_status == "CANCELED"


def test_filled_order_fails_loudly() -> None:
    client = FakeClient([[], [_order("1001", "COMPLETE")]])
    with pytest.raises(CheckTheAccount, match="filled"):
        _run(client)


PARTLY = "CHECK THE ACCOUNT: order 1001 was \\(partly\\) filled"


def test_partial_fill_in_the_order_book_fails_loudly_even_when_cancelled() -> None:
    client = FakeClient(
        [
            [],
            [_order("1001", "OPEN", fillshares="1")],
            [_order("1001", "OPEN", fillshares="1")],
            [_order("1001", "CANCELED", fillshares="1")],
        ]
    )
    with pytest.raises(CheckTheAccount, match=PARTLY):
        _run(client)
    assert client.count("cancel_order") == 1  # cleanup still ran


def test_partial_fill_seen_only_after_the_cancel_fails_loudly() -> None:
    client = FakeClient(
        [
            [],
            [_order("1001", "OPEN")],
            [_order("1001", "OPEN")],
            [_order("1001", "CANCELED", fillshares="1")],
        ]
    )
    with pytest.raises(CheckTheAccount, match=PARTLY):
        _run(client)


def test_partial_fill_in_the_history_fails_loudly_even_when_rejected() -> None:
    history = [
        OrderHistoryEntry.model_validate(
            {"norenordno": "1001", "status": "REJECTED", "fillshares": "1"}
        )
    ]
    client = FakeClient([[], [_order("1001", "REJECTED")]], history=history)
    with pytest.raises(CheckTheAccount, match=PARTLY):
        _run(client)


def test_a_trade_for_the_order_fails_loudly() -> None:
    trades = [Trade.model_validate({"norenordno": "1001", "flqty": "1"})]
    client = FakeClient([[], [_order("1001", "REJECTED")]], trades=trades)
    with pytest.raises(CheckTheAccount, match=PARTLY):
        _run(client)
    assert client.count("trades") == 1


def test_trades_of_other_orders_are_ignored_and_checked_once() -> None:
    trades = [Trade.model_validate({"norenordno": "999", "flqty": "1"})]
    client = FakeClient([[], [_order("1001", "REJECTED")]], trades=trades)
    outcome, record = _run(client)
    assert outcome.final_status == "REJECTED"
    assert client.count("trades") == 1
    assert record["trades_for_order"] == 0


def test_unreadable_trade_book_is_recorded() -> None:
    client = FakeClient([[], [_order("1001", "REJECTED")]], trades=NetworkError("down"))
    outcome, record = _run(client)
    assert outcome.final_status == "REJECTED"
    assert record["trades_error"]["exception"] == "NetworkError"


def test_no_trades_check_without_an_order_id() -> None:
    client = FakeClient([[]], place=_rejected())
    _run(client)
    assert client.count("trades") == 0


ALERT = "CHECK THE ACCOUNT: order with tag sdklive-mcx-abc123 may exist; it was interrupted"


def test_keyboard_interrupt_during_place_is_reported_and_reraised(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    client = FakeClient([[]], place=KeyboardInterrupt())
    recorder = Recorder(tmp_path / "r.jsonl")
    with pytest.raises(KeyboardInterrupt):
        _run(client, recorder=recorder)
    assert ALERT in capsys.readouterr().err
    assert client.count("orders") == 1  # the pre-check only: no lookup after Ctrl-C
    assert client.count("cancel_order") == 0
    line = json.loads((tmp_path / "r.jsonl").read_text(encoding="utf-8"))
    assert ALERT in line["alert"]
    assert line["tag"] == "sdklive-mcx-abc123"
    assert line["exception"] == "KeyboardInterrupt"


def test_other_base_exception_during_place_looks_up_and_cancels_once(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    client = FakeClient([[], [_order("1001", "OPEN")]], place=SystemExit(3))
    with pytest.raises(SystemExit):
        _run(client, recorder=Recorder(tmp_path / "r.jsonl"))
    assert ALERT in capsys.readouterr().err
    assert client.count("orders") == 2  # pre-check + one lookup by tag
    assert client.calls[-1] == ("cancel_order", "1001")
    assert client.count("cancel_order") == 1
    assert client.count("place_order") == 1
    line = json.loads((tmp_path / "r.jsonl").read_text(encoding="utf-8"))
    assert line["lookup"]["order_id"] == "1001"
    assert line["cancel"]["outcome"] == "accepted"


def test_base_exception_lookup_failure_still_reraises_the_original(
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = FakeClient([[], NetworkError("down")], place=SystemExit(3))  # type: ignore[list-item]
    with pytest.raises(SystemExit):
        _run(client)
    assert ALERT in capsys.readouterr().err
    assert client.count("cancel_order") == 0


def test_base_exception_does_not_cancel_a_final_order(capsys: pytest.CaptureFixture[str]) -> None:
    client = FakeClient([[], [_order("1001", "REJECTED")]], place=SystemExit(3))
    with pytest.raises(SystemExit):
        _run(client)
    assert client.count("cancel_order") == 0


def _ok(path: str, body: dict[str, Any]) -> httpx.Response:
    return _response(path, body)


def test_place_response_recorded_before_this_call_is_never_attributed() -> None:
    log = ResponseLog()
    log.hook(_ok("PlaceOrder", {"stat": "Ok", "norenordno": "STALE"}))
    client = FakeClient([[]], place=_rejected())
    _, record = _run(client, responses=log)
    assert "response" not in record["place"]


def test_place_response_of_this_call_is_attributed() -> None:
    log = ResponseLog()
    log.hook(_ok("PlaceOrder", {"stat": "Ok", "norenordno": "STALE"}))
    body = {"stat": "Not_Ok", "emsg": "RMS: no funds"}
    client = FakeClient([[]], place=_rejected(), on_place=lambda: log.hook(_ok("PlaceOrder", body)))
    _, record = _run(client, responses=log)
    assert record["place"]["response"]["body"] == body


def test_cancel_response_before_the_cancel_is_never_attributed() -> None:
    log = ResponseLog()
    log.hook(_ok("CancelOrder", {"stat": "Ok", "result": "STALE"}))
    client = FakeClient(
        [[], [_order("1001", "OPEN")], [_order("1001", "OPEN")], [_order("1001", "CANCELED")]]
    )
    _, record = _run(client, responses=log)
    assert "response" not in record["cancel"]


def test_response_log_since_ignores_earlier_entries() -> None:
    log = ResponseLog()
    log.hook(_ok("ModifyOrder", {"stat": "Ok", "result": "OLD"}))
    mark = log.mark()
    assert log.last("ModifyOrder", since=mark) is None
    log.hook(_ok("ModifyOrder", {"stat": "Ok", "result": "NEW"}))
    assert log.last("ModifyOrder", since=mark) == {
        "http_status": 200,
        "body": {"stat": "Ok", "result": "NEW"},
    }


def test_modify_once_ignores_a_stale_modify_body() -> None:
    log = ResponseLog()
    log.hook(_ok("ModifyOrder", {"stat": "Ok", "result": "STALE"}))
    client = FakeClient([[]])
    record: dict[str, Any] = {}
    safety.modify_once(client, _plan(), "1001", Decimal("5399"), record, log)  # type: ignore[arg-type]
    assert "response" not in record["modify"]


def test_cancel_rejected_once_ignores_a_stale_cancel_body() -> None:
    log = ResponseLog()
    log.hook(_ok("CancelOrder", {"stat": "Ok", "result": "STALE"}))
    client = FakeClient([[_order("1001", "REJECTED")]])
    record: dict[str, Any] = {}
    safety.cancel_rejected_once(client, "1001", record, log)  # type: ignore[arg-type]
    assert "response" not in record["cancel"]


def test_existing_open_order_blocks_a_new_one() -> None:
    client = FakeClient([[_order("777", "OPEN", tag="other")]])
    budget = OrderBudget()
    with pytest.raises(Refusal, match="777"):
        _run(client, budget=budget)
    assert client.count("place_order") == 0
    assert budget.used == 0


def test_budget_is_spent_before_the_order_is_sent() -> None:
    budget = OrderBudget(limit=1)
    client = FakeClient([[]], place=_rejected())
    _run(client, budget=budget)
    with pytest.raises(Refusal):
        _run(FakeClient([[]]), budget=budget)


def test_open_hook_is_called_while_the_order_is_open() -> None:
    client = FakeClient(
        [[], [_order("1001", "OPEN")], [_order("1001", "OPEN")], [_order("1001", "CANCELED")]]
    )
    seen: list[str] = []

    def hook(order: Order, record: dict[str, Any]) -> None:
        seen.append(str(order.order_id))
        record["hook"] = "ran"

    outcome, record = _run(client, on_open=hook)
    assert seen == ["1001"]
    assert record["hook"] == "ran"
    assert outcome.was_open is True


def test_modify_once_records_the_ack_and_never_retries() -> None:
    client = FakeClient([[]], modify=_unknown())
    record: dict[str, Any] = {}
    log = ResponseLog()
    safety.modify_once(
        client,  # type: ignore[arg-type]
        _plan(),
        "1001",
        Decimal("5399"),
        record,
        log,
    )
    assert client.count("modify_order") == 1
    assert record["modify"]["exception"] == "OrderStatusUnknownError"
    order_id, kwargs = client.calls[-1][1]
    assert order_id == "1001"
    assert kwargs["order_type"] == "LIMIT" and kwargs["price"] == Decimal("5399")
    assert kwargs["quantity"] == 100


def test_cancel_of_a_rejected_order_is_sent_once_and_recorded() -> None:
    error = OrderRejectedError(
        "CancelOrder failed: Order not open",
        endpoint="CancelOrder",
        http_status=200,
        emsg="Order not open",
        raw={"stat": "Not_Ok", "emsg": "Order not open"},
    )
    client = FakeClient([[_order("1001", "REJECTED")]], cancel=error)
    record: dict[str, Any] = {}
    exc = safety.cancel_rejected_once(client, "1001", record, None)  # type: ignore[arg-type]
    assert exc is error
    assert client.count("cancel_order") == 1
    assert record["cancel"]["exception"] == "OrderRejectedError"
    assert record["cancel"]["emsg"] == "Order not open"
    assert record["status_after_cancel"] == "REJECTED"


def test_cancel_of_a_rejected_order_that_is_accepted_returns_none() -> None:
    client = FakeClient([[_order("1001", "REJECTED")]])
    record: dict[str, Any] = {}
    assert safety.cancel_rejected_once(client, "1001", record, None) is None  # type: ignore[arg-type]
    assert record["cancel"]["outcome"] == "accepted"
    assert client.count("cancel_order") == 1


def test_rejected_order_of_this_suite_is_found_by_tag_prefix() -> None:
    book = [
        _order("1", "REJECTED", tag="someone-else"),
        _order("2", "OPEN", tag="sdklive-mcx-aaaaaa"),
        _order("3", "REJECTED", tag="sdklive-nse-bbbbbb"),
        _order("4", "REJECTED", tag="sdklive-mcx-cccccc"),
    ]
    assert safety.rejected_order_of_suite(book, "mcx") == "4"
    assert safety.rejected_order_of_suite(book, "nfo") is None


# -- preparing a plan -------------------------------------------------------------------


class FakeMarket:
    client_id = "TEST01"

    def __init__(
        self,
        limits: dict[str, Any],
        quote: dict[str, Any],
        rows: list[dict[str, str]] | None = None,
    ) -> None:
        self.limits = limits
        self.quote_body = quote
        self.rows = rows if rows is not None else ROWS
        self.calls: list[str] = []

    def margins(self) -> Limits:
        self.calls.append("margins")
        return Limits.model_validate(self.limits)

    def instruments(self, exchange: str) -> list[dict[str, str]]:
        self.calls.append(f"instruments:{exchange}")
        return self.rows

    def quote(self, exchange: str, token: str) -> Any:
        from tradesmart.models.market import Quote

        self.calls.append(f"quote:{exchange}:{token}")
        return Quote.model_validate(self.quote_body)


def test_prepare_builds_a_one_lot_limit_buy_below_the_market() -> None:
    market = FakeMarket(
        {"stat": "Ok", "cash": "0.00"},
        {"stat": "Ok", "lp": "6000.00", "lc": "5760.00", "ti": "1.00", "ls": "100"},
    )
    record: dict[str, Any] = {}
    plan = safety.prepare_plan(
        market,  # type: ignore[arg-type]
        safety.SEGMENTS["mcx"],
        record,
        now=_ist(2026, 11, 10, 21, 0),
    )
    assert plan.tradingsymbol == "CRUDEOIL18NOV26"
    assert plan.quantity == 100  # MCX qty is in units (F21): one CRUDEOIL lot of 100
    assert record["quantity_rule"] == "script-master LotSize"
    assert plan.price == Decimal("5760")
    assert plan.product == "NRML"
    assert plan.tag.startswith("sdklive-mcx-")
    assert record["instrument"]["lot_size"] == 100
    assert record["quote"]["lc"] == "5760.00"
    assert record["lot_size_check"] == {"script_master": 100, "quote": 100}
    assert market.calls == ["margins", "instruments:MCX", "quote:MCX:1001"]


def test_prepare_nfo_sends_the_script_master_lot_in_units() -> None:
    rows = [
        {
            "Exchange": "NFO",
            "Token": "35001",
            "LotSize": "75",
            "Symbol": "NIFTY",
            "TradingSymbol": "NIFTY25NOV26F",
            "Expiry": "25-NOV-2026",
            "Instrument": "FUTIDX",
            "TickSize": "0.10",
        }
    ]
    market = FakeMarket(
        {"stat": "Ok"}, {"stat": "Ok", "lp": "24000.00", "ti": "0.10", "ls": "75"}, rows
    )
    record: dict[str, Any] = {}
    plan = safety.prepare_plan(
        market,  # type: ignore[arg-type]
        safety.SEGMENTS["nfo"],
        record,
        now=_ist(2026, 11, 11, 10, 0),
    )
    assert plan.quantity == 75
    assert plan.price == Decimal("21600.00")


def test_prepare_nse_equity_sends_lot_one() -> None:
    rows = [{"Exchange": "NSE", "Token": "3045", "LotSize": "1", "TradingSymbol": "SBIN-EQ"}]
    market = FakeMarket({"stat": "Ok"}, {"stat": "Ok", "lp": "800.00", "ti": "0.05"}, rows)
    record: dict[str, Any] = {}
    plan = safety.prepare_plan(
        market,  # type: ignore[arg-type]
        safety.SEGMENTS["nse"],
        record,
        now=_ist(2026, 11, 11, 10, 0),
    )
    assert plan.quantity == 1
    assert record["lot_size_check"] == {"script_master": 1, "quote": None}


def test_prepare_refuses_when_quote_lot_size_disagrees_with_the_script_master() -> None:
    market = FakeMarket(
        {"stat": "Ok"},
        {"stat": "Ok", "lp": "6000.00", "lc": "5760.00", "ti": "1.00", "ls": "10"},
    )
    record: dict[str, Any] = {}
    with pytest.raises(Refusal, match="lot size"):
        safety.prepare_plan(
            market,  # type: ignore[arg-type]
            safety.SEGMENTS["mcx"],
            record,
            now=_ist(2026, 11, 10, 21, 0),
        )
    assert record["lot_size_check"] == {"script_master": 100, "quote": 10}


def test_prepare_refuses_when_the_market_is_closed_without_any_call() -> None:
    market = FakeMarket({"stat": "Ok"}, {"stat": "Ok"})
    with pytest.raises(Refusal, match="MCX"):
        safety.prepare_plan(
            market,  # type: ignore[arg-type]
            safety.SEGMENTS["mcx"],
            {},
            now=_ist(2026, 11, 10, 8, 0),
        )
    assert market.calls == []


def test_prepare_refuses_an_account_with_funds() -> None:
    market = FakeMarket({"stat": "Ok", "cash": "1000.00"}, {"stat": "Ok"})
    with pytest.raises(Refusal, match="cash"):
        safety.prepare_plan(
            market,  # type: ignore[arg-type]
            safety.SEGMENTS["mcx"],
            {},
            now=_ist(2026, 11, 10, 21, 0),
        )
    assert market.calls == ["margins"]


def test_prepare_refuses_without_a_last_price() -> None:
    market = FakeMarket({"stat": "Ok"}, {"stat": "Ok", "ti": "1.00"})
    with pytest.raises(Refusal, match="last price"):
        safety.prepare_plan(
            market,  # type: ignore[arg-type]
            safety.SEGMENTS["mcx"],
            {},
            now=_ist(2026, 11, 10, 21, 0),
        )


# -- UAT mode (a non-production test environment) ---------------------------------------

PRODUCTION_URL = "https://v2api.tradesmartonline.in/NorenWClientAPIv2"
UAT_URL = "https://api-uat.example.test/NorenWClientAPIV2"  # synthetic test-environment host
UAT_ENV = {"TRADESMART_ENV": "uat"}


@pytest.mark.parametrize(
    "url",
    [
        UAT_URL,
        "https://UAT.example.test/api",
        "https://rest.uat.example.test:8443/api",
        "https://api-uat.example.test./api",  # a trailing dot names the same host
    ],
)
def test_uat_mode_allows_a_uat_host(url: str) -> None:
    assert safety.uat_mode(UAT_ENV, url) is True


@pytest.mark.parametrize("name", ["UAT", " uat "])
def test_uat_mode_reads_the_environment_name_like_the_environment_helper(name: str) -> None:
    assert safety.uat_mode({"TRADESMART_ENV": name}, UAT_URL) is True


@pytest.mark.parametrize(
    "env",
    [{}, {"TRADESMART_ENV": ""}, {"TRADESMART_ENV": "production"}, {"TRADESMART_ENV": "stage"}],
)
def test_uat_mode_needs_tradesmart_env_uat(env: dict[str, str]) -> None:
    assert safety.uat_mode(env, UAT_URL) is False


@pytest.mark.parametrize(
    "url",
    [
        PRODUCTION_URL,
        "https://v2api.tradesmartonline.in/uat/NorenWClientAPIv2",  # uat in the path
        "https://v2api.tradesmartonline.in/NorenWClientAPIv2?env=uat",  # ... in the query
        "https://v2api.tradesmartonline.in/NorenWClientAPIv2#uat",  # ... in the fragment
        "https://uat@v2api.tradesmartonline.in/NorenWClientAPIv2",  # ... in the user info
        "https://uat:uat@v2api.tradesmartonline.in/NorenWClientAPIv2",
        "https://uat.example@v2api.tradesmartonline.in/NorenWClientAPIv2",
        "https://uat.example\\@v2api.tradesmartonline.in/NorenWClientAPIv2",
        "https://V2API.tradesmartonline.in/uat",  # case variants of the production host
        "https://V2API.TRADESMARTONLINE.IN/uat",
        "https://v2api.tradesmartonline.in./uat",  # trailing dot: still the production host
        "https://v2api.tradesmartonline.in../uat",
        "https://V2API.tradesmartonline.in.:443/uat",
        "https://v2api.tradesmartonline.in:443/uat",
        "https://uat.v2api.tradesmartonline.in/api",  # under the production host
        "https://v2api.tradesmartonline.in.uat.example/api",  # look-alikes
        "https://v2api.tradesmartonline.in-uat.example/api",
        "https://V2API.TradeSmartOnline.in.uat.example./api",
        "https://v2api.tradesmartonline.in\\.uat.example/api",
        "https://v2api.tradesmartonline.in%2Euat.example/api",
        "https://uat.ｖ2api.tradesmartonline.in/api",  # non-ASCII (IDNA maps it to v2api)
        "https://uat。v2api.tradesmartonline.in/api",  # ideographic full stop
        "//v2api.tradesmartonline.in/uat",
        "v2api.tradesmartonline.in/uat",
    ],
)
def test_uat_mode_refuses_production_host_tricks(url: str) -> None:
    assert safety.uat_mode(UAT_ENV, url) is False


@pytest.mark.parametrize(
    "url",
    [
        "https://rest.example.test/api",  # no uat in the host
        "https://rest.example.test/uat",
        "https://u-a-t.example.test/api",
        "",
        "uat",
        "https://uat.example.test:notaport/api",  # unparseable
        "https://[uat/api",
        "https://[::1]/uat",
    ],
)
def test_uat_mode_refuses_hosts_without_uat_or_unparseable_urls(url: str) -> None:
    assert safety.uat_mode(UAT_ENV, url) is False


def test_max_orders_per_run_is_three_on_production_and_forty_in_uat_mode() -> None:
    assert safety.max_orders_per_run(False) == 3 == safety.MAX_ORDERS_PER_RUN
    assert safety.max_orders_per_run(True) == 40


def test_uat_budget_allows_forty_orders_per_run() -> None:
    budget = OrderBudget(safety.max_orders_per_run(True))
    for _ in range(40):
        budget.reserve()
    with pytest.raises(Refusal, match="40 orders per run"):
        budget.reserve()


@pytest.mark.parametrize(
    ("env", "url"),
    [
        ({}, PRODUCTION_URL),
        ({"TRADESMART_ENV": "production"}, PRODUCTION_URL),
        ({}, UAT_URL),  # production rules (zero funds, cap 3) whatever the base URL
        (UAT_ENV, UAT_URL),
    ],
)
def test_environment_refusal_allows_production_and_uat_mode(env: dict[str, str], url: str) -> None:
    assert safety.environment_refusal(env, url) is None


@pytest.mark.parametrize(
    ("env", "url"),
    [
        (UAT_ENV, PRODUCTION_URL),
        (UAT_ENV, "https://uat@v2api.tradesmartonline.in/NorenWClientAPIv2"),
        (UAT_ENV, "https://rest.example.test/api"),
        ({"TRADESMART_ENV": "stage"}, "https://rest.example.test/api"),
        ({"TRADESMART_ENV": "stage"}, UAT_URL),  # only TRADESMART_ENV=uat opens UAT mode
    ],
)
def test_environment_refusal_refuses_other_non_production_runs(
    env: dict[str, str], url: str
) -> None:
    reason = safety.environment_refusal(env, url)
    assert reason is not None
    assert "refusing" in reason


class _ConfiguredClient:
    """Only a ``config``: any API call during ``open_session`` fails the test."""

    def __init__(self, base_url: str) -> None:
        self.config = ClientConfig(base_url=base_url)
        self.calls: list[str] = []

    def __getattr__(self, name: str) -> Any:
        self.calls.append(name)
        raise AssertionError(f"open_session must not call {name}()")


def test_open_session_keeps_production_rules() -> None:
    client = _ConfiguredClient(PRODUCTION_URL)
    session = safety.open_session(client, {})  # type: ignore[arg-type]
    assert session.uat is False
    assert session.budget.limit == 3
    assert session.client is client


def test_open_session_uses_uat_rules_in_uat_mode() -> None:
    client = _ConfiguredClient(UAT_URL)
    session = safety.open_session(client, UAT_ENV)  # type: ignore[arg-type]
    assert session.uat is True
    assert session.budget.limit == 40


def test_open_session_reads_the_clients_base_url() -> None:
    client = _ConfiguredClient(PRODUCTION_URL)  # where orders would really be sent
    environ = {**UAT_ENV, "TRADESMART_ENV_BASE_URL": UAT_URL}
    with pytest.raises(Refusal, match="refusing"):
        safety.open_session(client, environ)  # type: ignore[arg-type]
    assert client.calls == []


def test_order_session_defaults_to_production_rules() -> None:
    session = safety.OrderSession(FakeClient([[]]))  # type: ignore[arg-type]
    assert session.uat is False
    assert session.budget.limit == 3


_NSE_ROWS = [
    {
        "Exchange": "NSE",
        "Token": "3045",
        "LotSize": "1",
        "TradingSymbol": "SBIN-EQ",
        "TickSize": "0.05",
    }
]
_NSE_QUOTE = {"stat": "Ok", "lp": "800.00", "lc": "720.00", "ti": "0.05", "ls": "1"}
_FUNDED = {"stat": "Ok", "cash": "100000.00"}


def test_prepare_skips_the_funds_check_in_uat_mode_only() -> None:
    market = FakeMarket(_FUNDED, _NSE_QUOTE, _NSE_ROWS)
    record: dict[str, Any] = {}
    plan = safety.prepare_plan(
        market,  # type: ignore[arg-type]
        safety.SEGMENTS["nse"],
        record,
        now=_ist(2026, 11, 11, 10, 0),
        uat=True,
    )
    assert plan.price == Decimal("720.00")
    assert "margins" not in market.calls
    assert record["funds_check"] == "skipped (UAT mode)"


@pytest.mark.parametrize("kwargs", [{}, {"uat": False}], ids=["default", "uat-false"])
def test_prepare_keeps_the_funds_check_outside_uat_mode(kwargs: dict[str, Any]) -> None:
    market = FakeMarket(_FUNDED, _NSE_QUOTE, _NSE_ROWS)
    with pytest.raises(Refusal, match="cash"):
        safety.prepare_plan(
            market,  # type: ignore[arg-type]
            safety.SEGMENTS["nse"],
            {},
            now=_ist(2026, 11, 11, 10, 0),
            **kwargs,
        )
    assert market.calls == ["margins"]


def test_uat_mode_keeps_the_market_hours_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(safety.OUTSIDE_HOURS_ENV, raising=False)
    market = FakeMarket({"stat": "Ok"}, _NSE_QUOTE, _NSE_ROWS)
    with pytest.raises(Refusal, match="closed"):
        safety.prepare_plan(
            market,  # type: ignore[arg-type]
            safety.SEGMENTS["nse"],
            {},
            now=_ist(2026, 11, 14, 10, 0),  # a Saturday
            uat=True,
        )
    assert market.calls == []


def test_live_order_wiring_uses_the_uat_session() -> None:
    conftest = (LIVE_DIR / "conftest.py").read_text(encoding="utf-8")
    tests = (LIVE_DIR / "test_live_orders.py").read_text(encoding="utf-8")
    assert "safety.open_session(client, os.environ)" in conftest
    assert "safety.OrderSession(" not in conftest
    assert "uat=session.uat" in tests


# -- static guards on the live suite ----------------------------------------------------


def _live_sources() -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in sorted(LIVE_DIR.glob("*.py")))


# The UAT order-type matrix (SELL to close a fill, IOC, stop and market orders) and the funded
# tests (IOC, SL-LMT, a SELL closing a fill) live only in these files; their orders go through
# ``lifecycle.run_tracked``, which refuses outside UAT mode or funded mode. The sliced order (two
# orders from one call) goes through ``sliced.run_sliced``, which refuses outside UAT mode.
UAT_ONLY_FILES = (
    "lifecycle.py",
    "test_uat_lifecycle.py",
    "test_funded_live.py",
    "sliced.py",
    "test_uat_sliced.py",
)


def _production_sources() -> str:
    return "\n".join(
        p.read_text(encoding="utf-8")
        for p in sorted(LIVE_DIR.glob("*.py"))
        if p.name not in UAT_ONLY_FILES
    )


def test_the_uat_only_files_exist() -> None:
    assert all((LIVE_DIR / name).is_file() for name in UAT_ONLY_FILES)


@pytest.mark.parametrize(
    "forbidden",
    ['"MARKET"', '"MKT"', '"SELL"', '"S"', ".exit_order(", ".request(", "IOC", "OrderType.MARKET"],
)
def test_production_order_code_never_uses_market_sell_or_raw_calls(forbidden: str) -> None:
    assert forbidden not in _production_sources()


@pytest.mark.parametrize("forbidden", [".exit_order(", ".request(", ".place_order("])
def test_uat_only_files_never_call_the_api_for_orders_directly(forbidden: str) -> None:
    for name in UAT_ONLY_FILES:
        assert forbidden not in (LIVE_DIR / name).read_text(encoding="utf-8"), name


@pytest.mark.parametrize("name", ["test_uat_lifecycle.py", "test_funded_live.py"])
def test_matrix_tests_send_orders_only_through_run_tracked(name: str) -> None:
    source = (LIVE_DIR / name).read_text(encoding="utf-8")
    assert "run_order(" not in source and ".modify_order(" not in source
    assert ".cancel_order(" not in source and "lifecycle.run_tracked(" in source


@pytest.mark.parametrize(
    "forbidden",
    [
        '"MARKET"',
        '"MKT"',
        '"SL-MKT"',
        '"SL-MARKET"',
        "OrderType.MARKET",
        "OrderType.SL_M",  # the stop-market member (also matches OrderType.SL_MARKET)
        '"SL-M"',
        '"SLM"',
        '"SELL"',
        '"S"',
        "transaction_type",
    ],
)
def test_funded_file_sends_no_market_order_and_sells_only_to_close(forbidden: str) -> None:
    """The funded tests build no MARKET / SL-MKT order and no SELL of their own: a SELL is sent
    only by ``lifecycle.close_position`` (closing a fill of the same test)."""
    source = (LIVE_DIR / "test_funded_live.py").read_text(encoding="utf-8")
    assert forbidden not in source
    assert "lifecycle.close_position(" in source


def test_place_order_is_called_in_exactly_one_place() -> None:
    assert _live_sources().count(".place_order(") == 1


def test_sliced_orders_are_sent_only_by_the_uat_sliced_run() -> None:
    """``place_sliced_order`` is called once, in ``sliced.py`` (``run_sliced`` refuses outside UAT
    mode and in funded mode); the sliced test sends and cancels nothing itself."""
    assert _live_sources().count(".place_sliced_order(") == 1
    assert ".place_sliced_order(" in (LIVE_DIR / "sliced.py").read_text(encoding="utf-8")
    test_source = (LIVE_DIR / "test_uat_sliced.py").read_text(encoding="utf-8")
    for forbidden in (".cancel_order(", ".modify_order(", "run_order(", "run_tracked("):
        assert forbidden not in test_source


# -- results are tagged with the environment --------------------------------------------


def test_recorder_tags_each_line_with_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "live_orders.jsonl"
    monkeypatch.delenv("TRADESMART_ENV", raising=False)
    Recorder(path).write({"test": "x"})
    monkeypatch.setenv("TRADESMART_ENV", "uat")
    Recorder(path).write({"test": "y", "environment": "spoofed"})
    lines = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]
    assert [x["environment"] for x in lines] == ["production", "uat"]


# -- UAT lifecycle support: an accepted fill, the upper circuit ---------------------------


def test_allow_fill_accepts_a_filled_order_without_cancelling() -> None:
    trades = [Trade.model_validate({"norenordno": "1001", "flqty": "1"})]
    client = FakeClient([[], [_order("1001", "COMPLETE", fillshares="1")]], trades=trades)
    outcome, record = _run(client, allow_fill=True, uat=True)
    assert outcome.filled is True
    assert outcome.final_status == "COMPLETE"
    assert client.count("cancel_order") == 0
    assert client.count("place_order") == 1
    assert record["trades_for_order"] == 1


def test_allow_fill_accepts_a_partial_fill_after_the_cancel() -> None:
    client = FakeClient(
        [
            [],
            [_order("1001", "OPEN")],
            [_order("1001", "OPEN")],
            [_order("1001", "CANCELED", fillshares="1")],
        ]
    )
    outcome, _ = _run(client, allow_fill=True, uat=True)
    assert outcome.filled is True and outcome.cancelled is True
    assert outcome.final_status == "CANCELED"
    assert client.count("cancel_order") == 1 and client.count("place_order") == 1


def test_allow_fill_reports_no_fill_for_a_rejected_order() -> None:
    client = FakeClient([[], [_order("1001", "REJECTED")]])
    outcome, _ = _run(client, allow_fill=True, uat=True)
    assert outcome.filled is False and outcome.final_status == "REJECTED"


def test_a_fill_still_fails_loudly_by_default() -> None:
    client = FakeClient([[], [_order("1001", "COMPLETE")]])
    with pytest.raises(CheckTheAccount, match="filled"):
        _run(client, allow_fill=False)


def test_allow_fill_still_fails_when_the_cancel_does_not_settle() -> None:
    client = FakeClient([[], [_order("1001", "OPEN")]])
    with pytest.raises(CheckTheAccount, match="still OPEN"):
        _run(client, allow_fill=True, uat=True, cancel_settle_seconds=2.0)
    assert client.count("cancel_order") == 1 and client.count("place_order") == 1


def test_production_order_tests_never_allow_a_fill() -> None:
    assert "allow_fill" not in (LIVE_DIR / "test_live_orders.py").read_text(encoding="utf-8")


def test_prepare_keeps_the_upper_circuit_on_the_plan() -> None:
    market = FakeMarket(
        {"stat": "Ok", "cash": "0.00"},
        {"stat": "Ok", "lp": "6000.00", "lc": "5760.00", "uc": "6240.00", "ti": "1.00"},
    )
    plan = safety.prepare_plan(
        market,  # type: ignore[arg-type]
        safety.SEGMENTS["mcx"],
        {},
        now=_ist(2026, 11, 10, 21, 0),
    )
    assert plan.upper_circuit == Decimal("6240.00")
    assert _plan().upper_circuit is None


# -- order shapes of the UAT matrix (production keeps the DAY LIMIT BUY) ----------------


def test_default_plan_has_the_production_shape() -> None:
    assert _plan().is_production_shape is True


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"transaction_type": "SELL"}, {"transaction_type": "SELL"}),
        ({"validity": "IOC"}, {"validity": "IOC"}),
        (
            {"order_type": "SL-LMT", "trigger_price": Decimal("6100")},
            {"order_type": "SL-LMT", "trigger_price": Decimal("6100")},
        ),
    ],
    ids=["sell", "ioc", "sl-lmt"],
)
def test_plan_sends_its_shape(overrides: dict[str, Any], expected: dict[str, Any]) -> None:
    plan = _plan(**overrides)
    assert plan.is_production_shape is False
    kwargs = plan.place_kwargs()
    assert kwargs == {**_plan().place_kwargs(), **expected}
    assert plan.describe()["order_type"] == kwargs["order_type"]
    assert plan.describe()["transaction_type"] == kwargs["transaction_type"]


@pytest.mark.parametrize(("order_type", "trigger"), [("MARKET", None), ("SL-MKT", Decimal("6100"))])
def test_market_shapes_send_no_price(order_type: str, trigger: Decimal | None) -> None:
    kwargs = _plan(order_type=order_type, trigger_price=trigger).place_kwargs()
    assert "price" not in kwargs
    assert kwargs["order_type"] == order_type
    assert kwargs.get("trigger_price") == trigger


@pytest.mark.parametrize(
    "overrides",
    [
        {"transaction_type": "SELL"},
        {"validity": "IOC"},
        {"order_type": "MARKET"},
        {"order_type": "SL-LMT", "trigger_price": Decimal("6100")},
    ],
)
def test_run_order_refuses_other_shapes_unless_fills_are_allowed(
    overrides: dict[str, Any],
) -> None:
    client = FakeClient([[]])
    budget = OrderBudget()
    with pytest.raises(Refusal, match="DAY LIMIT BUY"):
        safety.run_order(client, _plan(**overrides), budget, {})  # type: ignore[arg-type]
    assert client.calls == [] and budget.used == 0


def test_run_order_sends_another_shape_when_fills_are_allowed() -> None:
    client = FakeClient([[], [_order("1001", "COMPLETE", fillshares="1")]])
    t = FakeTime()
    plan = _plan(transaction_type="SELL")
    outcome = safety.run_order(
        client,  # type: ignore[arg-type]
        plan,
        OrderBudget(),
        {},
        settle_seconds=1.0,
        sleep=t.sleep,
        clock=t.clock,
        allow_fill=True,
        uat=True,
    )
    assert outcome.filled is True
    sent = [kwargs for name, kwargs in client.calls if name == "place_order"]
    assert sent == [plan.place_kwargs()] and sent[0]["transaction_type"] == "SELL"


def test_modify_once_keeps_the_order_type_and_sends_a_trigger() -> None:
    client = FakeClient([[]])
    record: dict[str, Any] = {}
    plan = _plan(order_type="SL-LMT", trigger_price=Decimal("6100"))
    safety.modify_once(
        client,  # type: ignore[arg-type]
        plan,
        "1001",
        Decimal("6130"),
        record,
        None,
        trigger_price=Decimal("6110"),
    )
    _, kwargs = client.calls[-1][1]
    assert kwargs["order_type"] == "SL-LMT" and kwargs["trigger_price"] == Decimal("6110")
    assert kwargs["price"] == Decimal("6130") and kwargs["validity"] == "DAY"
    assert record["modify"]["new_trigger_price"] == Decimal("6110")


def test_modify_once_sends_no_trigger_for_a_limit_order() -> None:
    client = FakeClient([[]])
    safety.modify_once(client, _plan(), "1001", Decimal("5399"), {}, None)  # type: ignore[arg-type]
    _, kwargs = client.calls[-1][1]
    assert "trigger_price" not in kwargs


def test_prepare_uses_the_given_instrument_picker() -> None:
    market = FakeMarket(
        {"stat": "Ok"},
        {"stat": "Ok", "lp": "100.00", "lc": "5.00", "ti": "0.05", "ls": "65"},
    )
    seen: list[Any] = []

    def pick(client: Any, segment: Any, rows: Any, today: date, record: dict[str, Any]) -> Any:
        seen.append((client, segment.id, today))
        record["picked"] = True
        return safety.Instrument("NIFTY25NOV26C26000", "4711", 65, Decimal("0.05"))

    record: dict[str, Any] = {}
    plan = safety.prepare_plan(
        market,  # type: ignore[arg-type]
        safety.SEGMENTS["nfo"],
        record,
        now=_ist(2026, 11, 11, 10, 0),
        uat=True,
        pick=pick,
    )
    assert seen == [(market, "nfo", date(2026, 11, 11))]
    assert record["picked"] is True
    assert (plan.tradingsymbol, plan.token, plan.quantity) == ("NIFTY25NOV26C26000", "4711", 65)
    assert plan.price == Decimal("90.00")


# -- review round 1: fills only in UAT mode, the filled quantity, production records ------


def test_run_order_refuses_a_fill_outside_uat_mode() -> None:
    client = FakeClient([[]])
    budget = OrderBudget()
    with pytest.raises(Refusal, match="UAT mode"):
        _run(client, budget, allow_fill=True)
    assert client.calls == [] and budget.used == 0


def test_run_order_refuses_another_shape_outside_uat_mode_even_with_fills_allowed() -> None:
    client = FakeClient([[]])
    budget = OrderBudget()
    with pytest.raises(Refusal, match="UAT mode"):
        safety.run_order(
            client,  # type: ignore[arg-type]
            _plan(transaction_type="SELL"),
            budget,
            {},
            allow_fill=True,
            uat=False,
        )
    assert client.calls == [] and budget.used == 0


def test_outcome_carries_the_filled_quantity_of_the_latest_book_row() -> None:
    client = FakeClient(
        [
            [],
            [_order("1001", "OPEN", fillshares="10")],
            [_order("1001", "OPEN", fillshares="30")],
            [_order("1001", "CANCELED", fillshares="30")],
        ]
    )
    outcome, _ = _run(client, allow_fill=True, uat=True)
    assert outcome.filled is True and outcome.filled_quantity == 30


def test_outcome_filled_quantity_of_a_complete_order() -> None:
    client = FakeClient([[], [_order("1001", "COMPLETE", fillshares="65")]])
    outcome, _ = _run(client, allow_fill=True, uat=True)
    assert outcome.filled_quantity == 65


def test_outcome_filled_quantity_is_unknown_without_fillshares() -> None:
    client = FakeClient([[], [_order("1001", "REJECTED")]])
    outcome, _ = _run(client, allow_fill=True, uat=True)
    assert outcome.filled_quantity is None


def test_production_records_have_no_trigger_price_key() -> None:
    assert "trigger_price" not in _plan().describe()
    stop = _plan(order_type="SL-LMT", trigger_price=Decimal("6100"))
    assert stop.describe()["trigger_price"] == Decimal("6100")


def test_outcome_filled_quantity_never_comes_from_an_earlier_row() -> None:
    # Re-review round 1: OPEN 10, then a final CANCELED row without fillshares: unknown, not 10.
    client = FakeClient(
        [
            [],
            [_order("1001", "OPEN", fillshares="10")],
            [_order("1001", "OPEN", fillshares="10")],
            [_order("1001", "CANCELED")],
        ]
    )
    outcome, _ = _run(client, allow_fill=True, uat=True)
    assert outcome.filled is True and outcome.filled_quantity is None


def test_outcome_filled_quantity_ignores_an_unparseable_final_value() -> None:
    client = FakeClient(
        [[], [_order("1001", "OPEN", fillshares="10")], [_order("1001", "CANCELED", fillshares="")]]
    )
    outcome, _ = _run(client, allow_fill=True, uat=True, settle_seconds=0.0)
    assert outcome.filled_quantity is None


# -- UAT only: ignore a named order stuck open on UAT (F26) ------------------------------

STUCK = "26111200000058"
IGNORE = safety.IGNORE_OPEN_ENV


def test_ignore_open_env_name() -> None:
    assert IGNORE == "TRADESMART_LIVE_ORDER_UAT_IGNORE_OPEN"


def test_ignored_open_orders_empty_when_unset() -> None:
    assert safety.ignored_open_orders({}, PRODUCTION_URL) == frozenset()
    assert safety.ignored_open_orders(UAT_ENV, UAT_URL) == frozenset()


def test_ignored_open_orders_parsed_in_uat_mode() -> None:
    environ = {**UAT_ENV, IGNORE: f" {STUCK} , 123"}
    assert safety.ignored_open_orders(environ, UAT_URL) == frozenset({STUCK, "123"})


@pytest.mark.parametrize(
    ("environ", "url"),
    [
        ({IGNORE: STUCK}, PRODUCTION_URL),
        ({**UAT_ENV, IGNORE: STUCK}, PRODUCTION_URL),
        ({**UAT_ENV, IGNORE: STUCK, "TRADESMART_LIVE_ORDER_FUNDED": "1"}, UAT_URL),
    ],
)
def test_ignored_open_orders_refused_outside_uat_mode(environ: dict[str, str], url: str) -> None:
    with pytest.raises(Refusal, match="UAT mode"):
        safety.ignored_open_orders(environ, url)


def test_ignored_open_orders_allowed_in_mock_session_mode() -> None:
    # Owner, 14 Nov 2026: orders the API still lists as open in a mock session may be named.
    environ = {IGNORE: f"{STUCK}, 123"}
    assert safety.ignored_open_orders(environ, PRODUCTION_URL, mock=True) == frozenset(
        {STUCK, "123"}
    )


def test_ignored_open_orders_mock_flag_still_refuses_funded_mode() -> None:
    environ = {IGNORE: STUCK, "TRADESMART_LIVE_ORDER_FUNDED": "1"}
    with pytest.raises(Refusal, match="UAT mode"):
        safety.ignored_open_orders(environ, PRODUCTION_URL, mock=True)


@pytest.mark.parametrize("value", ["", " ", "abc", f"{STUCK},", "1 2", "-5", "1;2"])
def test_ignored_open_orders_refuses_anything_but_order_numbers(value: str) -> None:
    with pytest.raises(Refusal, match=IGNORE):
        safety.ignored_open_orders({**UAT_ENV, IGNORE: value}, UAT_URL)


def test_open_session_carries_the_ignored_orders_in_uat_mode() -> None:
    client = _ConfiguredClient(UAT_URL)
    session = safety.open_session(client, {**UAT_ENV, IGNORE: STUCK})  # type: ignore[arg-type]
    assert session.uat is True and session.ignored_open == frozenset({STUCK})
    assert client.calls == []


def test_open_session_refuses_ignored_orders_on_production() -> None:
    client = _ConfiguredClient(PRODUCTION_URL)
    with pytest.raises(Refusal, match="UAT mode"):
        safety.open_session(client, {IGNORE: STUCK})  # type: ignore[arg-type]
    assert client.calls == []


def test_order_session_ignores_nothing_by_default() -> None:
    assert safety.OrderSession(client=None).ignored_open == frozenset()  # type: ignore[arg-type]


def test_run_order_passes_an_ignored_open_order_in_uat_mode() -> None:
    stuck = _order(STUCK, "PENDING", exch="BSE")
    client = FakeClient([[stuck], [stuck, _order("1001", "REJECTED")]])
    budget = OrderBudget()
    record: dict[str, Any] = {}
    t = FakeTime()
    outcome = safety.run_order(
        client,  # type: ignore[arg-type]
        _plan(),
        budget,
        record,
        settle_seconds=1.0,
        sleep=t.sleep,
        clock=t.clock,
        uat=True,
        ignore_open=frozenset({STUCK}),
    )
    assert outcome.order_id == "1001" and budget.used == 1
    assert record["ignored_open_orders"] == [STUCK]
    assert [name for name, _ in client.calls].count("place_order") == 1
    assert not any(name == "cancel_order" and STUCK in str(args) for name, args in client.calls)


def test_run_order_still_refuses_another_open_order() -> None:
    client = FakeClient([[_order(STUCK, "PENDING"), _order("777", "OPEN")]])
    budget = OrderBudget()
    with pytest.raises(Refusal, match="777"):
        _run(client, budget, uat=True, ignore_open=frozenset({STUCK}))
    assert budget.used == 0
    assert all(name == "orders" for name, _ in client.calls)


def test_run_order_refuses_ignored_orders_outside_uat_mode() -> None:
    client = FakeClient([[_order(STUCK, "PENDING")]])
    budget = OrderBudget()
    with pytest.raises(Refusal, match="UAT mode"):
        _run(client, budget, ignore_open=frozenset({STUCK}))
    assert client.calls == [] and budget.used == 0


# -- the strike column: NFO and MCX "StrikePrice", BFO "Strike" (production, 2026-11-12) --


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        ({"StrikePrice": "25300"}, "25300"),
        ({"Strike": "85200"}, "85200"),
        ({"StrikePrice": " 25300 ", "Strike": "1"}, "25300"),
        ({"StrikePrice": "", "Strike": "85200"}, ""),  # the StrikePrice key decides (spec 10.4)
        ({}, ""),
    ],
)
def test_strike_text_reads_strikeprice_else_strike(row: dict[str, str], expected: str) -> None:
    assert safety.strike_text(row) == expected


# -- the GTT live test (tests/live_orders/gtt.py; UAT or the zero-funds production account) --

_BOTH_OPT_INS = {"TRADESMART_LIVE_TESTS": "1", "TRADESMART_LIVE_ORDER_TESTS": "1"}


def _never_opened() -> safety.OrderSession:
    raise AssertionError("open_session must not be called")


@pytest.mark.parametrize(
    "env",
    [
        {},
        {"TRADESMART_LIVE_TESTS": "1"},
        {"TRADESMART_LIVE_ORDER_TESTS": "1"},
        {"TRADESMART_LIVE_TESTS": "1", "TRADESMART_LIVE_ORDER_TESTS": "true"},
        {**_BOTH_OPT_INS, "PYTEST_XDIST_WORKER": "gw0"},
    ],
)
def test_gtt_session_needs_both_order_opt_ins_before_opening_a_session(
    env: dict[str, str],
) -> None:
    with pytest.raises(Refusal):
        gtt.gtt_session(env, _never_opened)


@pytest.mark.parametrize("value", ["1", "0", ""])
def test_gtt_session_refuses_the_outside_hours_flag_with_any_value_on_production(
    value: str,
) -> None:
    """On the zero-funds production account the GTT test stays inside market hours (a DAY GTT
    placed after the close has an undocumented lifetime): the outside-hours opt-in refuses."""
    env = {**_BOTH_OPT_INS, safety.OUTSIDE_HOURS_ENV: value}
    session = safety.OrderSession(FakeClient([]), uat=False)  # type: ignore[arg-type]
    with pytest.raises(Refusal, match="market hours only"):
        gtt.gtt_session(env, lambda: session)


@pytest.mark.parametrize("value", ["1", "0", ""])
def test_gtt_session_on_uat_ignores_the_outside_hours_flag(value: str) -> None:
    """UAT runs at any hour (owner 2026-11-13): the outside-hours flag does not refuse."""
    env = {**_BOTH_OPT_INS, safety.OUTSIDE_HOURS_ENV: value}
    session = safety.OrderSession(FakeClient([]), uat=True)  # type: ignore[arg-type]
    assert gtt.gtt_session(env, lambda: session) is session


@pytest.mark.parametrize(
    "now",
    [_ist(2026, 11, 13, 20, 0), _ist(2026, 11, 14, 10, 0)],
    ids=["20:00 IST", "Saturday"],
)
def test_gtt_plan_on_uat_runs_at_any_hour_and_on_production_refuses_when_closed(
    now: datetime, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(safety.OUTSIDE_HOURS_ENV, raising=False)
    source = (LIVE_DIR / "test_gtt_live.py").read_text(encoding="utf-8")
    assert "uat=session.uat, any_hour=session.uat" in " ".join(source.split())
    market = FakeMarket({"stat": "Ok"}, _NSE_QUOTE, _NSE_ROWS)
    plan = safety.prepare_plan(
        market,  # type: ignore[arg-type]
        safety.SEGMENTS["nse"],
        {},
        now=now,
        uat=True,
        any_hour=True,
    )
    assert plan.tradingsymbol
    with pytest.raises(Refusal, match="closed"):
        safety.prepare_plan(
            FakeMarket({"stat": "Ok"}, _NSE_QUOTE, _NSE_ROWS),  # type: ignore[arg-type]
            safety.SEGMENTS["nse"],
            {},
            now=now,
            uat=False,
            any_hour=False,
        )


@pytest.mark.parametrize("value", ["1", "0", ""])
def test_gtt_session_refuses_the_funded_flag_with_any_value(value: str) -> None:
    env = {**_BOTH_OPT_INS, safety.FUNDED_ENV: value}
    with pytest.raises(Refusal, match="never in funded mode"):
        gtt.gtt_session(env, _never_opened)


def test_gtt_session_refuses_a_funded_session() -> None:
    session = safety.OrderSession(FakeClient([]), funded=True)  # type: ignore[arg-type]
    with pytest.raises(Refusal, match="never in funded mode"):
        gtt.gtt_session(_BOTH_OPT_INS, lambda: session)


@pytest.mark.parametrize("uat", [True, False], ids=["uat", "zero-funds-production"])
def test_gtt_session_accepts_uat_and_production_sessions(uat: bool) -> None:
    session = safety.OrderSession(FakeClient([]), uat=uat)  # type: ignore[arg-type]
    assert gtt.gtt_session(_BOTH_OPT_INS, lambda: session) is session


def test_gtt_live_file_is_behind_the_live_orders_collection_gate() -> None:
    assert (LIVE_DIR / "test_gtt_live.py").is_file()
    assert safety.is_live_orders_item("tests/live_orders/test_gtt_live.py::test_x")
    source = (LIVE_DIR / "test_gtt_live.py").read_text(encoding="utf-8")
    assert "pytestmark = pytest.mark.live_orders" in source
    assert "gtt.gtt_session(os.environ" in source


def test_gtt_tags_are_unique_and_outside_the_suite_prefix() -> None:
    tags = {gtt.new_gtt_tag() for _ in range(20)}
    assert len(tags) == 20
    assert all(t.startswith("sdkgtt-") and not t.startswith(safety.TAG_PREFIX) for t in tags)


def test_gtt_calls_count_place_modify_and_cancel() -> None:
    assert gtt.GTT_CALLS == 3


@pytest.mark.parametrize(
    ("last_price", "tick", "factor", "expected"),
    [
        (Decimal("401.35"), Decimal("0.05"), Decimal("3"), Decimal("1204.05")),
        (Decimal("401.37"), Decimal("0.05"), Decimal("3"), Decimal("1204.15")),  # rounded up
        (Decimal("401.35"), Decimal("0.05"), Decimal("3.1"), Decimal("1244.20")),
    ],
)
def test_gtt_trigger_is_the_factor_times_the_last_price_rounded_up_to_the_tick(
    last_price: Decimal, tick: Decimal, factor: Decimal, expected: Decimal
) -> None:
    assert gtt.gtt_trigger(last_price, tick, factor) == expected


@pytest.mark.parametrize("last_price", [None, Decimal("0"), Decimal("-1")])
def test_gtt_trigger_refuses_without_a_last_price(last_price: Decimal | None) -> None:
    with pytest.raises(Refusal, match="no fresh last price"):
        gtt.gtt_trigger(last_price, Decimal("0.05"), Decimal("3"))


@pytest.mark.parametrize("tick", [None, Decimal("0")])
def test_gtt_trigger_refuses_without_a_tick(tick: Decimal | None) -> None:
    with pytest.raises(Refusal, match="tick"):
        gtt.gtt_trigger(Decimal("400"), tick, Decimal("3"))


@pytest.mark.parametrize("factor", [Decimal("1"), Decimal("0.5")])
def test_gtt_trigger_refuses_when_the_condition_would_already_be_true(factor: Decimal) -> None:
    with pytest.raises(Refusal, match="fire at once"):
        gtt.gtt_trigger(Decimal("400.00"), Decimal("0.05"), factor)


@pytest.mark.parametrize(
    ("alert_type", "last_price", "trigger", "expected"),
    [
        ("LTP_A_O", Decimal("400"), Decimal("1200"), False),
        ("LTP_A_O", Decimal("400"), Decimal("400"), True),  # at the trigger: refused too
        ("LTP_A_O", Decimal("400"), Decimal("399"), True),
        ("LTP_B_O", Decimal("400"), Decimal("100"), False),
        ("LTP_B_O", Decimal("400"), Decimal("400"), True),
        ("LTP_B_O", Decimal("400"), Decimal("500"), True),
    ],
)
def test_condition_already_true_for_the_ltp_alert_types(
    alert_type: str, last_price: Decimal, trigger: Decimal, expected: bool
) -> None:
    assert gtt.condition_already_true(alert_type, last_price, trigger) is expected


def test_condition_already_true_refuses_other_alert_types() -> None:
    with pytest.raises(Refusal, match="CH_PER_A_O"):
        gtt.condition_already_true("CH_PER_A_O", Decimal("400"), Decimal("1200"))


class _QuoteClient:
    def __init__(self, reply: dict[str, str] | Exception) -> None:
        self.reply = reply
        self.calls: list[tuple[str, str]] = []

    def quote(self, exchange: str, token: str) -> Quote:
        self.calls.append((exchange, token))
        if isinstance(self.reply, Exception):
            raise self.reply
        return Quote.model_validate({"stat": "Ok", **self.reply})


def _gtt_plan(**overrides: Any) -> OrderPlan:
    values: dict[str, Any] = {
        "segment_id": "nse",
        "exchange": "NSE",
        "tradingsymbol": "ITC-EQ",
        "token": "1660",
        "quantity": 1,
        "price": Decimal("360.00"),
        "product": "CNC",
        "tag": "sdkgtt-abc123",
        "tick_size": Decimal("0.05"),
        "lower_circuit": Decimal("320.00"),
        "last_price": Decimal("400.00"),
    }
    values.update(overrides)
    return OrderPlan(**values)


@pytest.mark.parametrize(
    "reply",
    [NetworkError("down"), {}, {"lp": "0"}, {"lp": "-2"}],
    ids=["quote-fails", "no-lp", "zero-lp", "negative-lp"],
)
def test_fresh_trigger_refuses_when_the_fresh_quote_is_missing(
    reply: dict[str, str] | Exception,
) -> None:
    client = _QuoteClient(reply)
    session = safety.OrderSession(client, uat=True)  # type: ignore[arg-type]
    record: dict[str, Any] = {}
    with pytest.raises(Refusal, match="not sending"):
        gtt.fresh_trigger(session, _gtt_plan(), Decimal("3"), record, "place_quote")
    assert client.calls == [("NSE", "1660")]


def test_fresh_trigger_reads_a_new_quote_and_records_it() -> None:
    client = _QuoteClient({"lp": "410.10", "ti": "0.05"})
    session = safety.OrderSession(client, uat=True)  # type: ignore[arg-type]
    record: dict[str, Any] = {}
    trigger = gtt.fresh_trigger(session, _gtt_plan(), Decimal("3"), record, "place_quote")
    assert trigger == Decimal("1230.30")  # from the fresh 410.10, not the plan's 400.00
    assert record["place_quote"] == {"lp": "410.10", "ti": "0.05", "trigger_value": "1230.30"}


def test_gtt_kwargs_send_a_far_cnc_limit_buy_with_a_day_alert() -> None:
    kwargs = gtt.gtt_kwargs(_gtt_plan(), Decimal("1200.00"))
    assert kwargs == {
        "exchange": "NSE",
        "tradingsymbol": "ITC-EQ",
        "alert_type": GttAlertType.LTP_ABOVE,
        "trigger_value": Decimal("1200.00"),
        "quantity": 1,
        "transaction_type": TransactionType.BUY,
        "product": "CNC",
        "order_type": OrderType.LIMIT,
        "price": Decimal("360.00"),
        "validity": Validity.DAY,
        "alert_validity": GttValidity.DAY,
        "tag": "sdkgtt-abc123",
    }


def test_response_log_keeps_gtt_write_bodies() -> None:
    log = ResponseLog()
    for name, word in (
        ("PlaceGTTOrder", "Oi created"),
        ("ModifyGTTOrder", "Oi replaced"),
        ("CancelGTTOrder", "Oi delete success"),
    ):
        log.hook(_response(name, {"stat": word, "al_id": "26111200000010", "uid": "TEST01"}))
        assert log.last(name) == {
            "http_status": 200,
            "body": {"stat": word, "al_id": "26111200000010"},
        }
    log.hook(_response("GetPendingGTTOrder", [{"stat": "Ok"}]))
    assert log.last("GetPendingGTTOrder") is None


@pytest.mark.parametrize("method", [".place_gtt(", ".modify_gtt(", ".cancel_gtt("])
def test_gtt_writes_are_sent_only_by_the_gtt_run(method: str) -> None:
    """Each GTT write is called once, in ``gtt.py`` (``run_gtt`` refuses in funded mode and
    counts each call against the caps); the GTT test file sends nothing itself."""
    assert _live_sources().count(method) == 1
    assert method in (LIVE_DIR / "gtt.py").read_text(encoding="utf-8")
    assert method not in (LIVE_DIR / "test_gtt_live.py").read_text(encoding="utf-8")


# -- N4: a per-test budget counts only orders actually sent ----------------------------------


def test_run_tracked_counts_no_order_for_the_test_when_another_order_is_open() -> None:
    """N4: the one-open-order check refuses before sending, so the test's own budget (recorded
    as ``orders_sent_by_test``) stays at 0, like the run's budget."""
    from tests.live_orders import lifecycle

    client = FakeClient([[_order("999", "OPEN", tag="someone-else")]])
    session = safety.OrderSession(client, budget=OrderBudget(10), uat=True)  # type: ignore[arg-type]
    session.test_budget = OrderBudget(lifecycle.PER_TEST_CAP)
    t = FakeTime()
    with pytest.raises(Refusal, match="open order"):
        lifecycle.run_tracked(session, _plan(), {}, sleep=t.sleep, clock=t.clock)
    assert client.count("place_order") == 0
    assert session.budget.used == 0
    assert session.test_budget.used == 0


def test_run_tracked_counts_a_sent_order_for_the_test() -> None:
    from tests.live_orders import lifecycle

    client = FakeClient([[], [_order("1001", "CANCELED")]])
    session = safety.OrderSession(client, budget=OrderBudget(10), uat=True)  # type: ignore[arg-type]
    session.test_budget = OrderBudget(lifecycle.PER_TEST_CAP)
    t = FakeTime()
    lifecycle.run_tracked(
        session, _plan(), {}, sleep=t.sleep, clock=t.clock, settle_seconds=1.0, poll_interval=1.0
    )
    assert client.count("place_order") == 1
    assert session.test_budget.used == 1 == session.budget.used


def test_run_order_reserves_the_test_budget_only_when_sending() -> None:
    test_budget = OrderBudget(2)
    client = FakeClient([[_order("999", "OPEN", tag="someone-else")]])
    with pytest.raises(Refusal, match="open order"):
        _run(client, test_budget=test_budget)
    assert test_budget.used == 0
    client = FakeClient([[], [_order("1001", "REJECTED")]])
    _run(client, test_budget=test_budget)
    assert test_budget.used == 1


def test_response_log_keeps_exit_sno_order_bodies() -> None:
    log = ResponseLog()
    log.hook(_response("ExitSNOOrder", {"stat": "Ok", "uid": "TEST01"}))
    assert "ExitSNOOrder" in safety.ORDER_ENDPOINTS
    assert log.last("ExitSNOOrder") == {"http_status": 200, "body": {"stat": "Ok"}}


# -- guards: the UAT matrix package ---------------------------------------------------------

MATRIX_DIR = LIVE_DIR / "uat_matrix"
MATRIX_SAFETY_FILES = (
    "__init__.py",
    "gate.py",
    "ledger.py",
    "guard.py",
    "calls.py",
    "calls_async.py",
    "conftest.py",
)
# The never-resend wrappers: the only matrix files that may make a state-changing call, each
# method at most once per file (calls_async.py has no sliced variant).
MATRIX_CALL_FILES = {
    ".place_order(": {"calls.py": 1, "calls_async.py": 1},
    ".modify_order(": {"calls.py": 1, "calls_async.py": 1},
    ".cancel_order(": {"calls.py": 1, "calls_async.py": 1},
    ".exit_order(": {"calls.py": 1, "calls_async.py": 1},
    ".place_sliced_order(": {"calls.py": 1},
    ".place_gtt(": {"calls.py": 1},
    ".modify_gtt(": {"calls.py": 1},
    ".cancel_gtt(": {"calls.py": 1},
}


def _matrix_files() -> list[Path]:
    return sorted(MATRIX_DIR.rglob("*.py"))


def test_the_matrix_safety_files_exist() -> None:
    assert all((MATRIX_DIR / name).is_file() for name in MATRIX_SAFETY_FILES)


def test_no_matrix_module_imports_funded_mode() -> None:
    """Funded mode (a funded production account) never meets the matrix: no matrix module
    imports ``funded.py`` or any funded name, at module level or inside a function."""
    import ast

    assert _matrix_files()
    for path in _matrix_files():
        source = path.read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(source)):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or "", *(alias.name for alias in node.names)]
            assert not any("funded" in name.lower() for name in names), (path.name, names)
        assert "MoneyBudget" not in source and "funded_mode(" not in source, path.name


WRITE_METHODS = frozenset(m.strip(".(") for m in MATRIX_CALL_FILES)
RAW_SENDS = frozenset({"request", "execute", "send"})  # the client's / transport's raw paths
TRANSPORT_FILES = frozenset({"conftest.py", "calls_async.py"})  # the response-log hooks only


def _write_uses(name: str, source: str) -> tuple[dict[str, int], list[str]]:
    """(the calls of each SDK write method in ``source``, every other use that could reach one:
    an attribute used without a call (an alias, ``functools.partial``), the method's name as a
    string (``getattr(client, "place_order")``) or a bare name / import, a raw send
    (``.request`` / ``.execute`` / ``.send``), ``_transport`` outside the response-log hooks)."""
    import ast

    tree = ast.parse(source)
    called = {id(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}
    counts: dict[str, int] = {}
    problems: list[str] = []
    for node in ast.walk(tree):
        where = f"{name}:{getattr(node, 'lineno', '?')}"
        if isinstance(node, ast.Attribute):
            if node.attr in WRITE_METHODS:
                if id(node) in called:
                    counts[node.attr] = counts.get(node.attr, 0) + 1
                else:
                    problems.append(f"{where}: .{node.attr} used without a call (an alias)")
            elif node.attr in RAW_SENDS:
                problems.append(f"{where}: .{node.attr} (a raw send)")
            elif node.attr == "_transport" and name not in TRANSPORT_FILES:
                problems.append(f"{where}: ._transport")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value in WRITE_METHODS:
                problems.append(f"{where}: the string {node.value!r} (getattr?)")
            elif node.value == "_transport" and name not in TRANSPORT_FILES:
                problems.append(f"{where}: the string '_transport'")
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) > 1
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value in RAW_SENDS
        ):
            problems.append(f"{where}: getattr(..., {node.args[1].value!r}) (a raw send)")
        elif isinstance(node, ast.Name) and node.id in WRITE_METHODS:
            problems.append(f"{where}: the bare name {node.id}")
        elif isinstance(node, ast.alias) and node.name.rsplit(".", 1)[-1] in WRITE_METHODS:
            problems.append(f"{where}: imports {node.name}")
    return counts, problems


@pytest.mark.parametrize(
    "snippet",
    [
        "getattr(client, 'place_order')(**plan)",
        "place = client.place_order\nplace(**plan)",
        "functools.partial(client.cancel_order, '1')()",
        "client._transport.execute(request)",
        "getattr(client, 'request')('PlaceOrder', {})",
        "client.request('PlaceOrder', {})",
        "from tradesmart import place_order",
        "session.client.modify_order (order_id)",  # a space before the call is still a call
    ],
)
def test_the_never_resend_guard_catches_indirect_writes(snippet: str) -> None:
    counts, problems = _write_uses("cases_x.py", snippet)
    assert problems or counts, snippet


def test_matrix_state_changing_calls_live_only_in_calls_py() -> None:
    """Every place / modify / cancel / exit / sliced / GTT call of the matrix is made once, in
    ``uat_matrix/calls.py`` (the never-resend wrappers), or once in their async mirror
    ``uat_matrix/calls_async.py`` (orders only); no other matrix file calls them, aliases
    them, names them in a string (``getattr``), sends a raw request, or reaches the transport
    (the response-log hooks in ``conftest.py`` / ``calls_async.py`` excepted). AST-based: a
    space before ``(``, an alias or ``functools.partial`` are caught too."""
    used: dict[str, dict[str, int]] = {}
    problems: list[str] = []
    for path in _matrix_files():
        counts, found = _write_uses(path.name, path.read_text(encoding="utf-8"))
        problems.extend(found)
        for method, count in counts.items():
            used.setdefault(f".{method}(", {})[path.name] = count
    assert not problems, problems
    assert used == MATRIX_CALL_FILES, used


@pytest.mark.parametrize(
    "forbidden",
    ['"MARKET"', '"MKT"', '"SL-MKT"', '"SELL"', '"S"', "IOC", ".exit_order(", "OrderType.MARKET"],
)
def test_only_matrix_and_uat_files_name_non_production_shapes(forbidden: str) -> None:
    """Every file under tests/live_orders (subfolders included) that is neither a UAT-only file
    nor part of the UAT matrix package sends the production shape only."""
    for path in sorted(LIVE_DIR.rglob("*.py")):
        if path.name in UAT_ONLY_FILES and path.parent == LIVE_DIR:
            continue
        if MATRIX_DIR in path.parents:
            continue
        assert forbidden not in path.read_text(encoding="utf-8"), path.name


class _MatrixItem(_Item):
    """A collected item with its own markers (the tier markers decide the matrix gate)."""

    def __init__(self, nodeid: str, *names: str) -> None:
        super().__init__(nodeid)
        self.own = [SimpleNamespace(name=name) for name in names]

    def iter_markers(self) -> list[Any]:
        return [*self.own, *self.markers]


class _Config:
    class option:  # noqa: N801 - pytest's config.option namespace
        markexpr = "uat_matrix"  # the matrix selected explicitly (else it is deselected)


@pytest.mark.parametrize("tier", [(), ("smoke",), ("stress",)], ids=["full", "smoke", "stress"])
def test_production_or_funded_sessions_skip_every_matrix_item_at_collection(
    monkeypatch: pytest.MonkeyPatch, tier: tuple[str, ...]
) -> None:
    from tests.live_orders.uat_matrix import conftest as matrix_conftest

    monkeypatch.setenv("TRADESMART_LIVE_TESTS", "1")
    monkeypatch.setenv("TRADESMART_LIVE_ORDER_TESTS", "1")
    monkeypatch.setenv("TRADESMART_LIVE_ORDER_UAT_MATRIX", "1")
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    monkeypatch.delenv("TRADESMART_LIVE_ORDER_FUNDED", raising=False)
    monkeypatch.delenv("TRADESMART_ENV", raising=False)  # production
    item = _MatrixItem("tests/live_orders/uat_matrix/test_m1_shapes.py::test_x[nse]", *tier)
    other = _MatrixItem("tests/live_orders/test_live_orders.py::test_x[nse]", *tier)
    matrix_conftest.pytest_collection_modifyitems(_Config(), [item, other])  # type: ignore[arg-type,list-item]
    assert [m.name for m in item.markers] == ["skip"]
    assert other.markers == []
    monkeypatch.setenv("TRADESMART_ENV", "uat")
    monkeypatch.setenv("TRADESMART_ENV_BASE_URL", "https://uat.example.test/NorenWClientAPIv2")
    monkeypatch.setenv("TRADESMART_LIVE_ORDER_FUNDED", "0")
    item = _MatrixItem("tests/live_orders/uat_matrix/test_m1_shapes.py::test_x[nse]", *tier)
    matrix_conftest.pytest_collection_modifyitems(_Config(), [item])  # type: ignore[arg-type,list-item]
    assert [m.name for m in item.markers] == ["skip"]
    assert "TRADESMART_LIVE_ORDER_FUNDED" in item.markers[0].kwargs["reason"]


@pytest.mark.parametrize(
    "helper",
    [
        "run_order(",
        "run_tracked(",
        "run_gtt(",
        "run_sliced(",
        "modify_once(",
        "cancel_rejected_once(",
    ],
)
def test_no_matrix_module_but_calls_uses_the_older_live_order_helpers(helper: str) -> None:
    """M7: the matrix sends through ``calls.py`` only, never through the lifecycle, GTT or sliced
    live helpers (which have their own caps and one-open-order rule)."""
    for path in _matrix_files():
        if path.name == "calls.py":
            continue
        assert helper not in path.read_text(encoding="utf-8"), (path.name, helper)
