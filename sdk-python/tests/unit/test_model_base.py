from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import Field

from tradesmart.enums import OrderType
from tradesmart.errors import RequestValidationError, ResponseParseError
from tradesmart.models.base import (
    IST,
    EpochTime,
    ISTDateTime,
    Price,
    Quantity,
    RequestModel,
    TradeSmartModel,
    to_wire_value,
    validate_request,
)


class _M(TradeSmartModel):
    last_price: Price = Field(None, alias="lp")
    quantity: Quantity = Field(None, alias="qty")
    feed_time: EpochTime = Field(None, alias="lut")
    request_time: ISTDateTime = None


def test_from_wire_parses_and_keeps_raw_and_extras():
    m = _M.from_wire(
        {
            "lp": "1156.50",
            "qty": "10",
            "lut": "1781948701",
            "request_time": "15:09:30 20-06-2026",
            "new": "x",
        }
    )
    assert m.last_price == Decimal("1156.50") and m.quantity == 10
    assert m.feed_time == datetime(2026, 6, 20, 9, 45, 1, tzinfo=UTC)
    assert m.request_time == datetime(2026, 6, 20, 15, 9, 30, tzinfo=IST)
    assert m.raw["lp"] == "1156.50" and m.model_extra["new"] == "x"


def test_blank_values_become_none():
    m = _M.from_wire({"lp": "", "qty": "", "lut": "0"})
    assert m.last_price is None and m.quantity is None and m.feed_time is None


def test_quantity_variants():
    assert _M.from_wire({"qty": "10.0"}).quantity == 10
    with pytest.raises(ResponseParseError):
        _M.from_wire({"qty": "10.5"})


def test_price_from_int_and_float():
    assert _M.from_wire({"lp": 5}).last_price == Decimal("5")
    assert _M.from_wire({"lp": 1500.1}).last_price == Decimal("1500.1")


def test_ist_formats_and_unparseable():
    assert _M.from_wire({"request_time": "20-06-2026 15:09:30"}).request_time == datetime(
        2026, 6, 20, 15, 9, 30, tzinfo=IST
    )
    assert _M.from_wire({"request_time": "20/06/2026 15:09:30"}).request_time is not None
    assert _M.from_wire({"request_time": "garbage"}).request_time is None


def test_eod_date_format():
    class _D(TradeSmartModel):
        time: ISTDateTime = None

    assert _D.from_wire({"time": "04-AUG-2023"}).time == datetime(2023, 8, 4, tzinfo=IST)


def test_invalid_number_raises_parse_error():
    with pytest.raises(ResponseParseError) as ei:
        _M.from_wire({"lp": "abc"}, endpoint="GetQuotes")
    assert ei.value.endpoint == "GetQuotes" and ei.value.http_status is None


@pytest.mark.parametrize("value", ["inf", "-Infinity", "nan", "1e400"])
def test_non_finite_epoch_is_parse_error(value):
    with pytest.raises(ResponseParseError) as ei:
        _M.from_wire({"lut": value}, endpoint="GetQuotes")
    assert "lut" in str(ei.value)


def test_parse_error_body_is_redacted():
    with pytest.raises(ResponseParseError) as ei:
        _M.from_wire({"lp": "abc", "susertoken": "secret-value"})
    assert "secret-value" not in ei.value.body


def test_parse_error_has_no_validation_error_in_chain():
    with pytest.raises(ResponseParseError) as ei:
        _M.from_wire({"lp": "abc", "susertoken": "secret-value"})
    assert ei.value.__cause__ is None and ei.value.__context__ is None


def test_to_wire_value_float_uses_str():
    assert to_wire_value(1500.1) == "1500.1" and to_wire_value(Decimal("1E+2")) == "100"
    assert to_wire_value(True) == "Yes" and to_wire_value(OrderType.LIMIT) == "LMT"
    assert to_wire_value(False) == "No" and to_wire_value(7) == "7" and to_wire_value("x") == "x"


def test_request_model_to_wire_and_validation():
    class _R(RequestModel):
        price: Decimal = Field(alias="prc")
        remarks: str | None = None

    assert validate_request(_R, price=Decimal("10.5")).to_wire() == {"prc": "10.5"}
    with pytest.raises(RequestValidationError) as ei:
        validate_request(_R, price=Decimal("1"), bogus=1)
    assert "bogus" in str(ei.value)


def test_fractional_volume_parses():
    from tradesmart.models.market import Candle, DailyCandle, Quote
    from tradesmart.models.stream import PositionUpdate, TouchlineFeed

    candle = Candle.from_wire({"v": "123.50", "intv": "1.5", "oi": "2.25", "intoi": "3.5"})
    assert candle.volume == Decimal("123.50")
    assert candle.intv == Decimal("1.5")
    assert candle.open_interest == Decimal("2.25")
    assert candle.intoi == Decimal("3.5")
    tick = TouchlineFeed.from_wire(
        {"t": "tf", "v": "10.5", "oi": "1.5", "poi": "2.5", "toi": "3.5"}
    )
    assert (tick.volume, tick.open_interest, tick.poi, tick.toi) == (
        Decimal("10.5"),
        Decimal("1.5"),
        Decimal("2.5"),
        Decimal("3.5"),
    )
    quote = Quote.from_wire({"v": "7.5", "oi": "8.5", "toi": "9.5"})
    assert (quote.volume, quote.open_interest, quote.toi) == (
        Decimal("7.5"),
        Decimal("8.5"),
        Decimal("9.5"),
    )
    assert DailyCandle.from_wire({"intv": "4.5"}).intv == Decimal("4.5")
    assert PositionUpdate is not None  # position models carry no volume fields


def test_ato_price_becomes_none_for_stream_price_only():
    from tradesmart.models.base import ATO_PRICE, StreamPrice

    class _S(TradeSmartModel):
        p: StreamPrice = None

    assert Decimal("42949672.95") == ATO_PRICE
    assert _S.from_wire({"p": "42949672.95"}).p is None
    assert _S.from_wire({"p": "1156.00"}).p == Decimal("1156.00")
    assert _M.from_wire({"lp": "42949672.95"}).last_price == Decimal("42949672.95")


def test_unknown_timestamp_logged_once_per_shape(caplog):
    from tradesmart.models import base

    base._WARNED_TIMESTAMP_SHAPES.clear()
    caplog.set_level("WARNING", logger="tradesmart")
    first = _M.from_wire({"request_time": "2026/11/09T10:00"})
    second = _M.from_wire({"request_time": "2027/12/31T23:59"})
    assert first.request_time is None and second.request_time is None
    records = [r for r in caplog.records if r.name == "tradesmart"]
    assert len(records) == 1
    assert "9999/99/99T99:99" in records[0].getMessage()
    assert "2026" not in records[0].getMessage()


def test_unknown_timestamp_shapes_are_capped(caplog):
    from tradesmart.models import base

    base._WARNED_TIMESTAMP_SHAPES.clear()
    caplog.set_level("WARNING", logger="tradesmart")
    try:
        for n in range(60):  # 60 distinct shapes ("a", "aa", ..., "ab", "aab", ...)
            _M.from_wire({"request_time": "a" * (n % 30 + 1) + "b" * (n // 30)})
        assert len(base._WARNED_TIMESTAMP_SHAPES) == 50
        records = [r for r in caplog.records if r.name == "tradesmart"]
        shape_lines = [r for r in records if "Unrecognised timestamp format" in r.getMessage()]
        assert len(shape_lines) == 50
        assert len(records) == 51  # exactly one extra line, the last, announcing the cap
        assert records[-1].getMessage() == (
            "50 unrecognised timestamp formats logged; further ones are not logged"
        )
        _M.from_wire({"request_time": "c"})
        assert len(base._WARNED_TIMESTAMP_SHAPES) == 50
    finally:
        base._WARNED_TIMESTAMP_SHAPES.clear()


def test_place_order_ack_parses_request_time():
    from tests.conftest import fixture
    from tradesmart.endpoints.orders import PLACE_ORDER
    from tradesmart.models.base import IST

    ack = PLACE_ORDER.parse(fixture("place_order_success"))  # "15:37:31 20-06-2026"
    assert ack.order_id == "26110700000001"
    assert ack.request_time == datetime(2026, 6, 20, 15, 37, 31, tzinfo=IST)
    assert ack.request_time.utcoffset() == timedelta(hours=5, minutes=30)


def test_nested_models_keep_their_raw_payload():
    from tradesmart.models.portfolio import Holding

    item = {"exch": "NSE", "tsym": "A-EQ", "newfield": "x"}
    holding = Holding.from_wire({"exch_tsym": [item]})
    assert holding.instruments is not None
    assert holding.instruments[0].raw == item
    assert holding.raw == {"exch_tsym": [item]}


def test_nested_user_details_products_keep_their_raw_payload():
    from tests.conftest import fixture
    from tradesmart.models.user import UserDetails

    data = fixture("user_details_success")
    details = UserDetails.from_wire(data)
    assert details.prarr is not None
    assert [p.raw for p in details.prarr] == data["prarr"]
    assert details.raw == data


def test_lenient_top_level_raw_keeps_the_original_payload():
    from tradesmart.models.orders import Order

    data = {"norenordno": "1", "status": "OPEN", "qty": "not-a-number"}
    order = Order.from_wire(data, endpoint="OrderBook")
    assert order.raw == data  # the lenient retry (qty -> None) does not replace raw


def test_raw_of_a_directly_validated_token_masks_the_secret():
    from tradesmart.auth import AccessToken

    now = datetime(2026, 6, 20, 9, 0, tzinfo=UTC)
    token = AccessToken.model_validate(
        {"access_token": "plain-secret-tok", "issued_at": now, "expires_at": now}
    )
    assert "plain-secret-tok" not in repr(token.raw)
    assert token.raw["access_token"] == "***"
    assert token.access_token.get_secret_value() == "plain-secret-tok"


def test_model_validate_of_a_model_instance_keeps_working():
    class _Inner(TradeSmartModel):
        a: str | None = None

    inner = _Inner.from_wire({"a": "1"})
    assert _Inner.model_validate(inner).raw == {"a": "1"}


class _CountingKeys(frozenset):  # type: ignore[type-arg]
    """``SENSITIVE_KEYS`` stand-in counting the lookups made by the nested-raw copy."""

    lookups = 0

    def __contains__(self, key: object) -> bool:
        type(self).lookups += 1
        return super().__contains__(key)


@pytest.fixture
def counting_keys(monkeypatch: pytest.MonkeyPatch) -> type[_CountingKeys]:
    from tradesmart.models import base

    _CountingKeys.lookups = 0
    monkeypatch.setattr(base, "SENSITIVE_KEYS", _CountingKeys(base.SENSITIVE_KEYS))
    return _CountingKeys


def test_top_level_from_wire_skips_the_nested_raw_copy(counting_keys) -> None:
    from tradesmart.models.orders import Order

    data = {"norenordno": "1", "status": "OPEN", "qty": "5", "tsym": "A-EQ"}
    order = Order.from_wire(data, endpoint="OrderBook")
    assert order.raw == data
    assert counting_keys.lookups == 0  # no throw-away masked copy of the top-level payload


def test_lenient_retry_also_skips_the_top_level_copy(counting_keys) -> None:
    from tradesmart.models.orders import Order

    data = {"norenordno": "1", "status": "OPEN", "qty": "not-a-number"}
    assert Order.from_wire(data, endpoint="OrderBook").raw == data
    assert counting_keys.lookups == 0


def test_only_nested_records_get_the_masked_copy(counting_keys) -> None:
    from tradesmart.models.portfolio import Holding

    item = {"exch": "NSE", "tsym": "A-EQ", "access_token": "nested-secret"}
    data = {"exch_tsym": [item], "holdqty": "x"}  # holdqty fails: the lenient retry runs too
    holding = Holding.from_wire(data)
    assert holding.raw == data  # original top-level payload (unmasked, as before)
    assert holding.instruments is not None
    assert holding.instruments[0].raw == {**item, "access_token": "***"}
    assert counting_keys.lookups == len(item) * 2  # nested keys only, once per validation pass
