"""The slice planner (``tradesmart._slicing``) and ``SlicedOrderResult``.

Sizes come from the sliced-orders design §2 (live production values, 2026-11-12): NIFTY lot 65 and
freeze 1801 (largest slice 1755), SENSEX option lot 20 / freeze 1000, SENSEX future lot 20 /
freeze 500, MCX CRUDEOIL lot 100 / freeze 2500 (25 lots; MCX quantities are in units, like NFO), ITC
cash lot 1 / freeze 350854.
"""

from __future__ import annotations

import dataclasses
import pickle
from typing import Any

import pytest

import tradesmart
from tradesmart import OrderRejectedError, OrderStatusUnknownError, SlicedOrderResult
from tradesmart._slicing import max_slice_size, plan_slices

# ----------------------------------------------------------------------------- sizing (design §2)


@pytest.mark.parametrize(
    "exchange,quantity,lot,freeze,expected",
    [
        ("NFO", 3965, 65, 1801, (1755, 1755, 455)),  # NIFTY 61 lots: two full slices + remainder
        ("NFO", 1755, 65, 1801, (1755,)),  # exactly one full slice
        ("NFO", 65, 65, 1801, (65,)),  # one lot
        ("BFO", 2000, 20, 1000, (1000, 1000)),  # SENSEX option: a slice may equal frzqty
        ("BFO", 600, 20, 500, (500, 100)),  # SENSEX future
        ("MCX", 6000, 100, 2500, (2500, 2500, 1000)),  # CRUDEOIL: 2500 units = 25 lots a slice
        ("MCX", 1, 1, 1, (1,)),  # GOLD: lot 1, frzqty 1
        ("MCX", 3, 1, 1, (1, 1, 1)),  # GOLD: one lot a slice
        ("NSE", 1, 1, 350854, (1,)),  # ITC cash
        ("nfo", 1755, 65, 1801, (1755,)),  # exchange in any case
        ("mcx", 2500, 100, 2500, (2500,)),
    ],
)
def test_plan_slices_design_table(
    exchange: str, quantity: int, lot: int, freeze: int, expected: tuple[int, ...]
) -> None:
    assert plan_slices(exchange, quantity, lot, freeze) == expected


@pytest.mark.parametrize(
    "exchange,quantity,lot,freeze,cap,expected",
    [
        ("NFO", 65, 65, 1801, None, (65,)),  # one NIFTY lot
        ("NFO", 130, 65, 1801, None, (130,)),  # two NIFTY lots
        ("BFO", 20, 20, 1000, None, (20,)),  # one SENSEX lot
        ("MCX", 300, 100, 2500, None, (300,)),  # three CRUDEOIL lots
        ("NFO", 130, 65, 1801, 650, (130,)),  # below max_slice_quantity too
    ],
)
def test_quantity_below_the_largest_slice_goes_out_as_one_slice(
    exchange: str,
    quantity: int,
    lot: int,
    freeze: int,
    cap: int | None,
    expected: tuple[int, ...],
) -> None:
    # Owner, 2026-11-12: a small order is never refused for being small; it is one slice.
    assert plan_slices(exchange, quantity, lot, freeze, max_slice_quantity=cap) == expected


def test_quantity_not_a_whole_number_of_lots_is_refused_even_when_large() -> None:
    # 4000 is not a multiple of the NIFTY lot (65 * 61 = 3965, 65 * 62 = 4030).
    with pytest.raises(ValueError, match="lot"):
        plan_slices("NFO", 4000, 65, 1801)


@pytest.mark.parametrize(
    "exchange,lot,freeze,cap,expected",
    [
        ("NFO", 65, 1801, None, 1755),
        ("NFO", 65, 1801, 650, 650),  # a smaller cap wins
        ("NFO", 65, 1801, 1820, 1755),  # a larger cap does not raise the freeze limit
        ("BFO", 20, 1000, None, 1000),
        ("MCX", 100, 2500, None, 2500),
        ("MCX", 100, 2500, 700, 700),  # MCX cap is in units, a whole number of lots
        ("NSE", 1, 350854, None, 350854),
        ("NFO", 65, None, 130, 130),  # freeze unknown: the cap decides
        ("NFO", 65, 0, 130, 130),
        ("MCX", 100, None, 300, 300),
    ],
)
def test_max_slice_size(
    exchange: str, lot: int, freeze: int | None, cap: int | None, expected: int
) -> None:
    assert max_slice_size(exchange, lot, freeze, cap) == expected


def test_max_slice_quantity_caps_each_slice() -> None:
    assert plan_slices("NFO", 1755, 65, 1801, max_slice_quantity=650) == (650, 650, 455)


def test_max_slice_quantity_not_a_lot_multiple_is_refused() -> None:
    with pytest.raises(ValueError, match="lot"):
        plan_slices("NFO", 1755, 65, 1801, max_slice_quantity=700)


def test_freeze_unknown_with_a_cap_uses_the_cap() -> None:
    assert plan_slices("NFO", 260, 65, None, max_slice_quantity=130) == (130, 130)
    assert plan_slices("NFO", 325, 65, None, max_slice_quantity=130) == (130, 130, 65)


@pytest.mark.parametrize("freeze", [None, 0, -1])
def test_freeze_unknown_without_a_cap_is_refused(freeze: int | None) -> None:
    with pytest.raises(ValueError, match="freeze quantity unknown"):
        plan_slices("NFO", 65, 65, freeze)


def test_freeze_below_one_lot_is_refused() -> None:
    with pytest.raises(ValueError, match="below one lot"):
        plan_slices("NFO", 65, 65, 50)
    with pytest.raises(ValueError, match="below one lot"):
        plan_slices("MCX", 100, 100, 50)  # 50 units is less than one MCX lot of 100


def test_exactly_max_slices_is_allowed() -> None:
    assert plan_slices("NFO", 25 * 1755, 65, 1801) == (1755,) * 25


def test_more_slices_than_max_slices_is_refused_with_the_count() -> None:
    with pytest.raises(ValueError, match=r"26 slices.*max_slices") as info:
        plan_slices("NFO", 26 * 1755, 65, 1801)
    assert "25" in str(info.value)


def test_max_slices_can_be_raised() -> None:
    assert plan_slices("NFO", 26 * 1755, 65, 1801, max_slices=26) == (1755,) * 26


def test_max_slices_can_be_lowered() -> None:
    with pytest.raises(ValueError, match="3 slices"):
        plan_slices("NFO", 3965, 65, 1801, max_slices=2)


# ----------------------------------------------------------------------------- refusals (design §5)


@pytest.mark.parametrize("quantity", [0, -65, 1.5, True, False, "65", None, 65.0])
def test_quantity_must_be_an_int_of_at_least_one(quantity: Any) -> None:
    with pytest.raises(ValueError, match="quantity"):
        plan_slices("NFO", quantity, 65, 1801)


def test_quantity_not_a_multiple_of_the_lot_is_refused() -> None:
    with pytest.raises(ValueError, match="lot"):
        plan_slices("NFO", 100, 65, 1801)


@pytest.mark.parametrize("quantity", [150, 7, 1])
def test_mcx_quantity_is_in_units_so_it_must_be_a_whole_number_of_lots(quantity: int) -> None:
    # Owner, 2026-11-12 (F21): MCX quantity is in trade units like NFO; UAT rejected CRUDEOIL
    # qty 1 with "Quantity 1 is not a multiple of lot size 100.00".
    with pytest.raises(ValueError, match="whole number of lots"):
        plan_slices("MCX", quantity, 100, 2500)


def test_mcx_max_slice_quantity_must_be_a_whole_number_of_lots() -> None:
    with pytest.raises(ValueError, match="whole number of lots"):
        plan_slices("MCX", 1000, 100, 2500, max_slice_quantity=7)


@pytest.mark.parametrize("max_slices", [0, -1, True, 2.0, None, "25"])
def test_max_slices_must_be_an_int_of_at_least_one(max_slices: Any) -> None:
    with pytest.raises(ValueError, match="max_slices"):
        plan_slices("NFO", 65, 65, 1801, max_slices=max_slices)


@pytest.mark.parametrize("cap", [0, -65, True, 650.0, "650"])
def test_max_slice_quantity_must_be_an_int_of_at_least_one(cap: Any) -> None:
    with pytest.raises(ValueError, match="max_slice_quantity"):
        plan_slices("NFO", 65, 65, 1801, max_slice_quantity=cap)


@pytest.mark.parametrize("lot", [0, -65, True, 65.0, None])
def test_lot_size_must_be_an_int_of_at_least_one(lot: Any) -> None:
    with pytest.raises(ValueError, match="lot size"):
        plan_slices("NFO", 65, lot, 1801)


@pytest.mark.parametrize("exchange", [None, 1, b"NFO"])
def test_exchange_must_be_a_str(exchange: Any) -> None:
    with pytest.raises(ValueError, match="exchange"):
        plan_slices(exchange, 65, 65, 1801)


@pytest.mark.parametrize("freeze", [True, 1801.0, "1801"])
def test_freeze_quantity_must_be_an_int_or_none(freeze: Any) -> None:
    with pytest.raises(ValueError, match="freeze quantity"):
        plan_slices("NFO", 65, 65, freeze)


# ----------------------------------------------------------------------------- SlicedOrderResult


def _result(**changes: Any) -> SlicedOrderResult:
    values: dict[str, Any] = {
        "requested_quantity": 3965,
        "slice_quantities": (1755, 1755, 455),
        "order_ids": ("1001", "1002"),
        "placed_quantity": 3510,
        "unsent_quantity": 455,
        "complete": False,
        "error": OrderRejectedError("rejected", endpoint="PlaceOrder", http_status=200),
    }
    values.update(changes)
    return SlicedOrderResult(**values)


def test_sliced_order_result_is_exported() -> None:
    assert "SlicedOrderResult" in tradesmart.__all__
    assert tradesmart.SlicedOrderResult is SlicedOrderResult


def test_sliced_order_result_fields_in_order() -> None:
    assert [f.name for f in dataclasses.fields(SlicedOrderResult)] == [
        "requested_quantity",
        "slice_quantities",
        "order_ids",
        "placed_quantity",
        "unsent_quantity",
        "complete",
        "error",
    ]


def test_sliced_order_result_is_frozen() -> None:
    result = _result()
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.complete = True  # type: ignore[misc]


def test_sliced_order_result_complete_has_no_error() -> None:
    result = _result(
        order_ids=("1001", "1002", "1003"),
        placed_quantity=3965,
        unsent_quantity=0,
        complete=True,
        error=None,
    )
    assert result.complete and result.error is None


# ----------------------------------------------------------------------------- unknown outcome


def test_order_status_unknown_sliced_result_defaults_to_none() -> None:
    err = OrderStatusUnknownError("PlaceOrder", "timed out")
    assert err.sliced_result is None


def test_order_status_unknown_sliced_result_is_keyword_only() -> None:
    result = _result()
    err = OrderStatusUnknownError("PlaceOrder", "timed out", sliced_result=result)
    assert err.sliced_result is result
    with pytest.raises(TypeError):
        OrderStatusUnknownError("PlaceOrder", "timed out", None, None, result)  # type: ignore[misc]


def test_order_status_unknown_sliced_result_can_be_attached_later() -> None:
    err = OrderStatusUnknownError("PlaceOrder", "timed out")
    result = _result()
    err.sliced_result = result
    assert err.sliced_result is result


def test_order_status_unknown_text_unchanged_without_a_sliced_result() -> None:
    err = OrderStatusUnknownError("PlaceOrder", "timed out", http_status=504, body="x")
    assert str(err) == "PlaceOrder: order status unknown: timed out"
    assert repr(err) == "OrderStatusUnknownError('PlaceOrder: order status unknown: timed out')"
    assert vars(err) == {
        "endpoint": "PlaceOrder",
        "message": "timed out",
        "http_status": 504,
        "body": "x",
    }
    assert b"sliced_result" not in pickle.dumps(err)


def test_order_status_unknown_text_unchanged_with_a_sliced_result() -> None:
    err = OrderStatusUnknownError("PlaceOrder", "timed out", sliced_result=_result())
    assert str(err) == "PlaceOrder: order status unknown: timed out"
    assert err.args == ("PlaceOrder: order status unknown: timed out",)


# ------------------------------------------------- client assembly helpers (place_sliced_order)


@pytest.mark.parametrize(
    "row",
    [
        None,
        {"Token": "35001", "LotSize": ""},
        {"Token": "35001", "LotSize": "abc"},
        {"Token": "35001", "LotSize": "0"},
        {"Token": "35001", "LotSize": "65.5"},
        {"Token": "35001", "LotSize": "NaN"},
        {"Token": "  ", "LotSize": "65"},
        {"LotSize": "65"},
    ],
)
def test_contract_token_and_lot_refuses_an_unusable_row(row: dict[str, str] | None) -> None:
    from tradesmart._slicing import contract_token_and_lot

    with pytest.raises(ValueError, match="NIFTY26NOVFUT"):
        contract_token_and_lot(row, "NFO", " NIFTY26NOVFUT ")


def test_contract_token_and_lot_reads_token_and_lot() -> None:
    from tradesmart._slicing import contract_token_and_lot

    row = {"Token": " 35001 ", "LotSize": " 65 "}
    assert contract_token_and_lot(row, "NFO", "NIFTY26NOVFUT") == ("35001", 65)
    assert contract_token_and_lot({**row, "LotSize": "65.0"}, "NFO", "X") == ("35001", 65)


@pytest.mark.parametrize("tradingsymbol", [None, 7, "", "   "])
def test_check_slice_arguments_refuses_a_bad_tradingsymbol(tradingsymbol: Any) -> None:
    from tradesmart._slicing import check_slice_arguments

    with pytest.raises(ValueError, match="tradingsymbol"):
        check_slice_arguments(65, None, 25, tradingsymbol)


def test_freeze_quantity_without_ls_trusts_the_script_master() -> None:
    from tradesmart._slicing import freeze_quantity
    from tradesmart.models.market import SecurityInfo

    info = SecurityInfo.from_wire({"stat": "Ok", "frzqty": "1801"})
    assert freeze_quantity(info, 65, "NIFTY26NOVFUT") == 1801
    blank = SecurityInfo.from_wire({"stat": "Ok", "ls": "65", "frzqty": ""})
    assert freeze_quantity(blank, 65, "NIFTY26NOVFUT") is None


def test_check_disclosed_quantity_ignores_none_and_allows_the_smallest_slice() -> None:
    from tradesmart._slicing import check_disclosed_quantity

    check_disclosed_quantity(None, (1755, 455))
    check_disclosed_quantity(455, (1755, 455))
    with pytest.raises(ValueError, match="smallest slice 455"):
        check_disclosed_quantity(520, (1755, 455))


# ------------------------------------------------------------------- final review fixes (M1, M2)


def test_sliced_order_result_type_hints_resolve_at_runtime() -> None:
    import typing

    from tradesmart.errors import TradeSmartError

    hints = typing.get_type_hints(SlicedOrderResult)
    assert hints["error"] == TradeSmartError | None
    assert hints["order_ids"] == tuple[str, ...]


@pytest.mark.parametrize("frzqty", ["abc", "1801.5"])
def test_a_non_integer_frzqty_fails_the_security_info_read(frzqty: str) -> None:
    from tradesmart.errors import ResponseParseError
    from tradesmart.models.market import SecurityInfo

    with pytest.raises((ResponseParseError, ValueError)):
        SecurityInfo.from_wire({"stat": "Ok", "ls": "65", "frzqty": frzqty})


def test_a_negative_freeze_quantity_is_unknown_like_zero() -> None:
    assert plan_slices("NFO", 325, 65, -5, max_slice_quantity=130) == (130, 130, 65)


# --------------------------------------------------------------- the order rate limit (I2)


@pytest.mark.parametrize(
    ("count", "mode", "per_second", "max_wait"),
    [
        (25, "wait", 10, 1.0),  # the default config: 25 slices wait their turn
        (10, "raise", 10, 1.0),  # within one second's allowance
        (10, "wait", 10, 0.0),
        (30, "wait", 30, 0.5),
        (11, "wait", 10, 2.5),
    ],
)
def test_slices_that_fit_the_order_rate_limit_pass(
    count: int, mode: str, per_second: int, max_wait: float
) -> None:
    from tradesmart._slicing import check_order_rate_fit
    from tradesmart.config import ClientConfig

    config = ClientConfig(
        rate_limit_mode=mode,  # type: ignore[arg-type]
        order_rate_limit_per_second=per_second,
        order_rate_limit_max_wait=max_wait,
        algo_id="ALGO1" if per_second > 10 else None,  # above 10 needs an algo ID
    )
    check_order_rate_fit(count, config)


@pytest.mark.parametrize(
    ("count", "mode", "per_second", "max_wait", "reason"),
    [
        (11, "raise", 10, 1.0, 'rate_limit_mode="raise"'),
        (3, "raise", 2, 5.0, 'rate_limit_mode="raise"'),
        (11, "wait", 10, 0.0, "order_rate_limit_max_wait=0.0"),
        (25, "wait", 10, 0.99, "order_rate_limit_max_wait=0.99"),
    ],
)
def test_slices_the_order_rate_limit_would_stop_are_refused(
    count: int, mode: str, per_second: int, max_wait: float, reason: str
) -> None:
    from tradesmart._slicing import check_order_rate_fit
    from tradesmart.config import ClientConfig
    from tradesmart.errors import ConfigurationError

    config = ClientConfig(
        rate_limit_mode=mode,  # type: ignore[arg-type]
        order_rate_limit_per_second=per_second,
        order_rate_limit_max_wait=max_wait,
    )
    with pytest.raises(ConfigurationError) as caught:
        check_order_rate_fit(count, config)
    message = str(caught.value)
    assert reason in message
    assert f"{count} slices" in message and f"order_rate_limit_per_second={per_second}" in message
    assert "nothing was sent" in message


# ------------------------------------------------------------- interruption mid-loop (I1, M3)


def _progress(*ids: str) -> Any:
    from tradesmart._slicing import SliceProgress

    progress = SliceProgress(3965, (1755, 1755, 455))
    progress.order_ids.extend(ids)
    return progress


def test_an_interruption_gets_a_note_and_the_partial_result(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    caplog.set_level(logging.INFO, logger="tradesmart")
    interruption = KeyboardInterrupt()
    _progress("26111200000101").interrupted(interruption)

    notes = " ".join(interruption.__notes__)
    assert "26111200000101" in notes
    assert "slice 2 of 3" in notes and "may have been sent" in notes
    assert "1755 of 3965 placed" in notes and "2210 unsent" in notes
    assert "orders()" in notes
    partial = interruption.sliced_result  # type: ignore[attr-defined]
    assert partial.order_ids == ("26111200000101",) and partial.unsent_quantity == 2210
    assert partial.complete is False and partial.error is None
    [record] = [r for r in caplog.records if r.name == "tradesmart.orders"]
    assert record.levelno == logging.WARNING
    assert "interrupted (KeyboardInterrupt)" in record.getMessage()
    assert "1/3" in record.getMessage()


def test_an_interruption_before_any_slice_lists_no_order_ids() -> None:
    interruption = SystemExit(1)
    _progress().interrupted(interruption)
    notes = " ".join(interruption.__notes__)
    assert "order ids: none" in notes and "slice 1 of 3" in notes


def test_an_exception_that_refuses_attributes_still_gets_the_note() -> None:
    class Frozen(BaseException):
        def __setattr__(self, name: str, value: Any) -> None:
            if name != "__notes__":
                raise AttributeError(name)
            super().__setattr__(name, value)

    interruption = Frozen()
    _progress("1").interrupted(interruption)  # never fails
    assert "slice 2 of 3" in " ".join(interruption.__notes__)
    assert not hasattr(interruption, "sliced_result")


def test_an_exception_whose_notes_cannot_be_added_is_left_alone() -> None:
    interruption = KeyboardInterrupt()
    interruption.__notes__ = "not a list"  # type: ignore[assignment]
    _progress("1").interrupted(interruption)  # never fails
    assert interruption.__notes__ == "not a list"


def test_stopped_and_unknown_summaries_are_warnings_complete_is_info(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    caplog.set_level(logging.INFO, logger="tradesmart")
    rejected = OrderRejectedError("RMS", endpoint="PlaceOrder", http_status=200, emsg="RMS")
    _progress("1").stopped(rejected)
    _progress("1").unknown(OrderStatusUnknownError("PlaceOrder", "read timeout"))
    _progress("1", "2", "3").done()
    levels = {
        r.getMessage().split(":")[0]: r.levelno
        for r in caplog.records
        if r.name == "tradesmart.orders"
    }
    assert levels == {
        "place_sliced_order stopped (OrderRejectedError)": logging.WARNING,
        "place_sliced_order unknown": logging.WARNING,
        "place_sliced_order complete": logging.INFO,
    }


def test_an_interruption_after_the_last_slice_is_still_a_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    caplog.set_level(logging.INFO, logger="tradesmart")
    interruption = KeyboardInterrupt()
    _progress("1", "2", "3").interrupted(interruption)
    notes = " ".join(interruption.__notes__)
    assert "3 slice(s) placed" in notes and "0 unsent" in notes
    assert "may have been sent" not in notes
    [record] = [r for r in caplog.records if r.name == "tradesmart.orders"]
    assert record.levelno == logging.WARNING


def test_check_order_rate_fit_refuses_more_slices_than_per_minute() -> None:
    from tradesmart._slicing import check_order_rate_fit
    from tradesmart.config import ClientConfig
    from tradesmart.errors import ConfigurationError

    with pytest.raises(ConfigurationError, match="order_rate_limit_per_minute=200") as caught:
        check_order_rate_fit(201, ClientConfig())
    assert "201 slices" in str(caught.value) and "nothing was sent" in str(caught.value)


def test_check_order_rate_fit_accepts_exactly_per_minute_slices() -> None:
    from tradesmart._slicing import check_order_rate_fit
    from tradesmart.config import ClientConfig

    check_order_rate_fit(200, ClientConfig())
    check_order_rate_fit(201, ClientConfig(order_rate_limit_per_minute=300))
