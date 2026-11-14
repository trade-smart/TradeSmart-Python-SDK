"""Mocked unit tests pinning funded mode of the opt-in live order suite (tests/live_orders).

Funded mode runs the live order tests on a small funded production account. Every gate must
fail closed. Nothing here touches the network: the fakes below stand in for ``TradeSmart``.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from tests.live_orders import conftest as live_conftest
from tests.live_orders import funded, lifecycle, safety
from tests.live_orders.safety import OrderBudget, OrderPlan, Refusal
from tests.unit.test_live_orders_safety import FakeClient, FakeTime
from tests.unit.test_uat_lifecycle_helpers import (
    _BFO_ROWS,
    _FUTURE_ROW,
    _option_row,
    _QuoteClient,
)
from tradesmart import ClientConfig, OrderStatusUnknownError
from tradesmart.errors import APIError
from tradesmart.models.base import IST
from tradesmart.models.market import Quote
from tradesmart.models.orders import Order
from tradesmart.models.portfolio import Limits

pytest_plugins = ["pytester"]  # drives the stage-1 report hook (tests/live_orders/conftest.py)

PRODUCTION_URL = "https://v2api.tradesmartonline.in/NorenWClientAPIv2"
UAT_URL = "https://api-uat.example.test/NorenWClientAPIV2"  # synthetic test-environment host
FUNDED = {"TRADESMART_LIVE_ORDER_FUNDED": "1"}


def _ist(y: int, mo: int, d: int, h: int, mi: int) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=IST)


def test_constants() -> None:
    assert safety.FUNDED_ENV == "TRADESMART_LIVE_ORDER_FUNDED"
    assert safety.FILLS_ENV == "TRADESMART_LIVE_ORDER_FILLS"
    assert safety.MAX_BALANCE_ENV == "TRADESMART_LIVE_ORDER_MAX_BALANCE"
    assert safety.FUNDED_MAX_ORDERS_PER_RUN == 10
    assert Decimal("25000") == safety.FUNDED_DEFAULT_MAX_BALANCE


# -- the funded gate --------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        PRODUCTION_URL,
        "https://V2API.TradeSmartOnline.in/NorenWClientAPIv2",
        "https://v2api.tradesmartonline.in./NorenWClientAPIv2",
    ],
)
def test_is_production_host(url: str) -> None:
    assert safety.is_production_host(url) is True


@pytest.mark.parametrize(
    "url",
    [
        UAT_URL,
        "https://v2api.tradesmartonline.in.example.com/NorenWClientAPIv2",
        "https://uat@v2api.tradesmartonline.in/NorenWClientAPIv2",
        "https://uat.v2api.tradesmartonline.in/api",
        "https://v2api.tradesmartonline.in%2Eexample.com/api",
        "v2api.tradesmartonline.in/api",
        "",
    ],
)
def test_is_production_host_refuses_other_hosts(url: str) -> None:
    assert safety.is_production_host(url) is False


def test_funded_mode_holds_on_production_with_the_flag() -> None:
    assert safety.funded_mode(FUNDED, PRODUCTION_URL) is True
    assert safety.funded_mode({**FUNDED, "TRADESMART_ENV": "production"}, PRODUCTION_URL) is True
    assert safety.funded_refusal(FUNDED, PRODUCTION_URL) is None


@pytest.mark.parametrize(
    ("env", "url"),
    [
        ({}, PRODUCTION_URL),  # flag missing
        ({"TRADESMART_LIVE_ORDER_FUNDED": "true"}, PRODUCTION_URL),  # flag not exactly 1
        ({"TRADESMART_LIVE_ORDER_FUNDED": "0"}, PRODUCTION_URL),
        ({"TRADESMART_LIVE_ORDER_FUNDED": ""}, PRODUCTION_URL),
        ({**FUNDED, "TRADESMART_ENV": "uat"}, PRODUCTION_URL),  # environment not production
        ({**FUNDED, "TRADESMART_ENV": "staging"}, PRODUCTION_URL),
        ({**FUNDED}, UAT_URL),  # host not production
        ({**FUNDED}, "https://rest.example.test/api"),
        ({**FUNDED, "TRADESMART_LIVE_ORDER_OUTSIDE_HOURS": "1"}, PRODUCTION_URL),  # outside hours
    ],
    ids=[
        "no-flag",
        "flag-true",
        "flag-0",
        "flag-empty",
        "env-uat",
        "env-staging",
        "uat-host",
        "other-host",
        "outside-hours",
    ],
)
def test_funded_mode_needs_every_condition(env: dict[str, str], url: str) -> None:
    assert safety.funded_mode(env, url) is False


@pytest.mark.parametrize(
    ("env", "url", "check"),
    [
        ({**FUNDED, "TRADESMART_ENV": "uat"}, UAT_URL, "TRADESMART_ENV=uat"),
        ({**FUNDED, "TRADESMART_ENV": "uat"}, PRODUCTION_URL, "TRADESMART_ENV=uat"),
        (FUNDED, UAT_URL, "host"),
        (
            FUNDED,
            "https://v2api.tradesmartonline.in.example.com/NorenWClientAPIv2",
            "host",
        ),
        (FUNDED, "https://uat@v2api.tradesmartonline.in/NorenWClientAPIv2", "host"),
        ({**FUNDED, "TRADESMART_ENV": "staging"}, PRODUCTION_URL, "TRADESMART_ENV=staging"),
        ({**FUNDED, "TRADESMART_ENV": "staging"}, "https://rest.example.test/api", "staging"),
    ],
    ids=["uat-env-uat-host", "uat-env", "uat-host", "look-alike", "user-info", "staging", "stg"],
)
def test_funded_mode_refuses_non_production(env: dict[str, str], url: str, check: str) -> None:
    assert safety.funded_mode(env, url) is False
    reason = safety.funded_refusal(env, url)
    assert reason is not None
    assert check in reason
    assert "refusing" in reason
    assert safety.uat_mode(env, url) is False  # never funded and never UAT


def test_funded_refusal_names_the_outside_hours_check() -> None:
    env = {**FUNDED, "TRADESMART_LIVE_ORDER_OUTSIDE_HOURS": "1"}
    reason = safety.funded_refusal(env, PRODUCTION_URL)
    assert reason is not None
    assert "TRADESMART_LIVE_ORDER_OUTSIDE_HOURS" in reason


@pytest.mark.parametrize("value", ["true", "0", "yes", " 1", ""])
def test_funded_refusal_names_a_flag_that_is_not_exactly_1(value: str) -> None:
    reason = safety.funded_refusal({"TRADESMART_LIVE_ORDER_FUNDED": value}, PRODUCTION_URL)
    assert reason is not None
    assert "TRADESMART_LIVE_ORDER_FUNDED" in reason


@pytest.mark.parametrize("url", [PRODUCTION_URL, UAT_URL, "https://rest.example.test/api"])
def test_funded_refusal_none_when_flag_unset(url: str) -> None:
    assert safety.funded_refusal({}, url) is None
    assert safety.funded_refusal({"TRADESMART_ENV": "uat"}, url) is None


def test_uat_mode_unchanged_without_the_flag() -> None:
    assert safety.uat_mode({"TRADESMART_ENV": "uat"}, UAT_URL) is True


# -- the balance ceiling ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({}, Decimal("25000")),
        ({"TRADESMART_LIVE_ORDER_MAX_BALANCE": "10000"}, Decimal("10000")),
        ({"TRADESMART_LIVE_ORDER_MAX_BALANCE": "25000"}, Decimal("25000")),
        ({"TRADESMART_LIVE_ORDER_MAX_BALANCE": "1"}, Decimal("1")),
    ],
)
def test_max_balance_parsing(env: dict[str, str], expected: Decimal) -> None:
    assert safety.max_balance(env) == expected


@pytest.mark.parametrize(
    "value",
    [
        "abc",
        "-1",
        "0",
        "NaN",
        "1e9",
        "30000",
        "25001",
        "",
        "10000.50",
        "+100",
        "01000",
        "１０００",  # full-width digits
        "Infinity",
    ],
)
def test_max_balance_refuses_anything_but_1_to_25000(value: str) -> None:
    with pytest.raises(Refusal, match="TRADESMART_LIVE_ORDER_MAX_BALANCE"):
        safety.max_balance({"TRADESMART_LIVE_ORDER_MAX_BALANCE": value})


CEILING = Decimal("25000")


@pytest.mark.parametrize("cash", ["24999.99", "25000", "25000.00", "0", "0.00"])
def test_balance_refusal_allows_cash_up_to_the_ceiling(cash: str) -> None:
    limits = Limits.model_validate({"stat": "Ok", "cash": cash})
    assert safety.balance_refusal(limits, CEILING) is None


@pytest.mark.parametrize(
    ("body", "check"),
    [
        ({"stat": "Ok", "cash": "25000.01"}, "ceiling"),
        ({"stat": "Ok", "cash": "100000.00"}, "ceiling"),
        ({"stat": "Not_Ok", "emsg": "Session Expired", "cash": "10.00"}, "Ok"),
        ({"stat": "Ok"}, "cash"),  # cash missing
        ({"stat": "Ok", "cash": ""}, "cash"),  # blank: parsed as None
        ({"cash": "10.00"}, "Ok"),  # no stat
    ],
    ids=["just-above", "far-above", "not-ok", "missing", "blank", "no-stat"],
)
def test_balance_refusal_cases(body: dict[str, Any], check: str) -> None:
    limits = Limits.model_validate(body)
    reason = safety.balance_refusal(limits, CEILING)
    assert reason is not None
    assert check in reason
    assert "refusing" in reason


def test_unparseable_cash_never_reaches_the_balance_check() -> None:
    # The SDK refuses the body itself, so margins() raises and no order follows.
    with pytest.raises(ValueError, match="invalid price"):
        Limits.model_validate({"stat": "Ok", "cash": "abc"})


@pytest.mark.parametrize("cash", ["abc", Decimal("NaN"), Decimal("Infinity"), 100, None])
def test_balance_refusal_refuses_cash_that_is_not_a_finite_decimal(cash: Any) -> None:
    limits = Limits.model_construct(stat="Ok", cash=cash)  # bypasses validation on purpose
    reason = safety.balance_refusal(limits, CEILING)
    assert reason is not None and "cash" in reason


@pytest.mark.parametrize(
    ("body", "fields"),
    [
        ({"stat": "Ok", "cash": "0.00", "payin": "25000.01"}, ["payin"]),
        ({"stat": "Ok", "cash": "0.00", "collateral": "100000.00"}, ["collateral"]),
        ({"stat": "Ok", "cash": "0.00", "aux_daycash": "30000.00"}, ["aux_daycash"]),
        (
            {"stat": "Ok", "cash": "10000.00", "payin": "10000.00", "daycash": "5000.01"},
            ["cash", "payin", "daycash"],
        ),
    ],
    ids=["payin", "collateral", "aux", "several-together"],
)
def test_balance_refusal_counts_every_funds_field(body: dict[str, Any], fields: list[str]) -> None:
    limits = Limits.model_validate(body)
    reason = safety.balance_refusal(limits, CEILING)
    assert reason is not None
    assert "refusing" in reason
    assert "ceiling" in reason
    for name in fields:
        assert name in reason
    assert "10000" not in reason and "30000" not in reason  # never the amounts


def test_balance_refusal_accepts_a_funds_total_at_exactly_the_ceiling() -> None:
    limits = Limits.model_validate(
        {
            "stat": "Ok",
            "cash": "10000.00",
            "payin": "10000.00",
            "collateral": "5000.00",
            "marginused": "99999.00",  # not a funds field
            "brkcollamt": "-500.00",  # negative values do not lower the total
        }
    )
    assert safety.balance_refusal(limits, CEILING) is None


def test_balance_refusal_uses_the_given_ceiling() -> None:
    limits = Limits.model_validate({"stat": "Ok", "cash": "10000.01"})
    assert safety.balance_refusal(limits, Decimal("10000")) is not None
    assert safety.balance_refusal(limits, Decimal("10001")) is None


# -- the session ------------------------------------------------------------------------


class _ConfiguredClient:
    """Only a ``config``: any API call during ``open_session`` fails the test."""

    def __init__(self, base_url: str) -> None:
        self.config = ClientConfig(base_url=base_url)
        self.calls: list[str] = []

    def __getattr__(self, name: str) -> Any:
        self.calls.append(name)
        raise AssertionError(f"open_session must not call {name}()")


def test_open_session_funded_cap_and_fields() -> None:
    client = _ConfiguredClient(PRODUCTION_URL)
    session = safety.open_session(client, FUNDED)  # type: ignore[arg-type]
    assert session.funded is True
    assert session.uat is False
    assert session.budget.limit == 10
    assert session.max_balance == Decimal("25000")
    assert session.fills == {}
    assert session.stage1_failed is False
    assert session.money == funded.MoneyBudget()
    assert client.calls == []


def test_open_session_funded_reads_the_ceiling() -> None:
    client = _ConfiguredClient(PRODUCTION_URL)
    env = {**FUNDED, "TRADESMART_LIVE_ORDER_MAX_BALANCE": "5000"}
    session = safety.open_session(client, env)  # type: ignore[arg-type]
    assert session.max_balance == Decimal("5000")


def test_open_session_funded_refuses_a_bad_ceiling() -> None:
    client = _ConfiguredClient(PRODUCTION_URL)
    env = {**FUNDED, "TRADESMART_LIVE_ORDER_MAX_BALANCE": "30000"}
    with pytest.raises(Refusal, match="TRADESMART_LIVE_ORDER_MAX_BALANCE"):
        safety.open_session(client, env)  # type: ignore[arg-type]
    assert client.calls == []


@pytest.mark.parametrize(
    ("env", "url"),
    [
        ({**FUNDED, "TRADESMART_ENV": "uat"}, UAT_URL),
        (FUNDED, UAT_URL),
        (FUNDED, "https://v2api.tradesmartonline.in.example.com/NorenWClientAPIv2"),
        ({**FUNDED, "TRADESMART_LIVE_ORDER_OUTSIDE_HOURS": "1"}, PRODUCTION_URL),
        ({"TRADESMART_LIVE_ORDER_FUNDED": "yes"}, PRODUCTION_URL),
    ],
    ids=["uat-mode", "uat-host", "look-alike", "outside-hours", "flag-yes"],
)
def test_open_session_refuses_a_failed_funded_check(env: dict[str, str], url: str) -> None:
    client = _ConfiguredClient(url)
    with pytest.raises(Refusal, match="refusing"):
        safety.open_session(client, env)  # type: ignore[arg-type]
    assert client.calls == []


def test_open_session_without_flag_is_unchanged() -> None:
    client = _ConfiguredClient(PRODUCTION_URL)
    env = {"TRADESMART_LIVE_ORDER_MAX_BALANCE": "abc"}  # ignored without the flag
    session = safety.open_session(client, env)  # type: ignore[arg-type]
    assert session.funded is False
    assert session.uat is False
    assert session.budget.limit == 3
    assert session.max_balance is None
    assert session.money is None


def test_order_session_defaults_are_not_funded() -> None:
    session = safety.OrderSession(_ConfiguredClient(PRODUCTION_URL))  # type: ignore[arg-type]
    assert session.funded is False
    assert session.max_balance is None
    assert session.fills == {}
    assert session.stage1_failed is False


class _MarginsClient:
    def __init__(self, limits: dict[str, Any]) -> None:
        self.config = ClientConfig(base_url=PRODUCTION_URL)
        self.limits = limits
        self.calls: list[str] = []

    def margins(self) -> Limits:
        self.calls.append("margins")
        return Limits.model_validate(self.limits)


def test_session_balance_check_is_skipped_outside_funded_mode() -> None:
    client = _MarginsClient({"stat": "Ok", "cash": "100000.00"})
    session = safety.OrderSession(client)  # type: ignore[arg-type]
    assert live_conftest.funded_balance_refusal(session) is None
    assert client.calls == []


@pytest.mark.parametrize(
    ("cash", "refused"), [("24999.99", False), ("25000.00", False), ("25000.01", True)]
)
def test_session_balance_check_in_funded_mode(cash: str, refused: bool) -> None:
    client = _MarginsClient({"stat": "Ok", "cash": cash})
    session = safety.open_session(client, FUNDED)  # type: ignore[arg-type]
    reason = live_conftest.funded_balance_refusal(session)
    assert (reason is not None) is refused
    assert client.calls == ["margins"]


def test_session_balance_check_refuses_a_funded_session_without_a_ceiling() -> None:
    client = _MarginsClient({"stat": "Ok", "cash": "1.00"})
    session = safety.OrderSession(client, funded=True)  # type: ignore[arg-type]
    reason = live_conftest.funded_balance_refusal(session)
    assert reason is not None and "refusing" in reason
    assert client.calls == []


def test_order_session_fixture_runs_the_balance_check_after_the_account_check() -> None:
    from pathlib import Path

    text = (Path(live_conftest.__file__)).read_text(encoding="utf-8")
    account = text.index("safety.account_refusal(")
    balance = text.index("funded_balance_refusal(session)")
    assert account < balance


# -- run_order --------------------------------------------------------------------------


def _plan(**overrides: Any) -> OrderPlan:
    values: dict[str, Any] = {
        "segment_id": "nse",
        "exchange": "NSE",
        "tradingsymbol": "SBIN-EQ",
        "token": "3045",
        "quantity": 1,
        "price": Decimal("720.00"),
        "product": "CNC",
        "tag": "sdklive-nse-abc123",
    }
    values.update(overrides)
    return OrderPlan(**values)


class _OrderClient:
    """Records every call; orders() pops the next book (the last one repeats)."""

    def __init__(self, books: list[list[Order]]) -> None:
        self.books = books
        self.calls: list[str] = []

    def orders(self) -> list[Order]:
        self.calls.append("orders")
        return self.books.pop(0) if len(self.books) > 1 else self.books[0]

    def place_order(self, **kwargs: Any) -> str:
        self.calls.append("place_order")
        return "1001"

    def cancel_order(self, order_id: str) -> str:
        self.calls.append("cancel_order")
        return order_id

    def order_history(self, order_id: str) -> list[Any]:
        self.calls.append("order_history")
        return []

    def trades(self) -> list[Any]:
        self.calls.append("trades")
        return []


def _order(status: str, **extra: str) -> Order:
    return Order.model_validate(
        {"norenordno": "1001", "status": status, "remarks": "sdklive-nse-abc123", **extra}
    )


@pytest.mark.parametrize("order_type", ["MARKET", "MKT", "SL-MKT", "SL-M", "market", "BOGUS"])
@pytest.mark.parametrize("allow_fill", [True, False])
def test_run_order_refuses_market_in_funded(order_type: str, allow_fill: bool) -> None:
    client = _OrderClient([[]])
    budget = OrderBudget(10)
    trigger = Decimal("700") if order_type.upper().startswith("SL") else None
    plan = _plan(order_type=order_type, trigger_price=trigger)
    with pytest.raises(Refusal, match="funded mode"):
        safety.run_order(
            client,  # type: ignore[arg-type]
            plan,
            budget,
            {},
            allow_fill=allow_fill,
            funded=True,
        )
    assert client.calls == []
    assert budget.used == 0


def test_run_order_refuses_uat_and_funded_together() -> None:
    client = _OrderClient([[]])
    budget = OrderBudget(10)
    with pytest.raises(Refusal, match="funded"):
        safety.run_order(client, _plan(), budget, {}, uat=True, funded=True)  # type: ignore[arg-type]
    assert client.calls == [] and budget.used == 0


@pytest.mark.parametrize(
    "plan",
    [
        _plan(transaction_type="SELL", price=Decimal("800.00")),
        _plan(order_type="SL-LMT", trigger_price=Decimal("790.00"), price=Decimal("800.00")),
        _plan(order_type="LMT"),
    ],
    ids=["limit-sell", "stop-limit", "wire-code"],
)
def test_run_order_allows_fills_and_limit_shapes_in_funded_mode(plan: OrderPlan) -> None:
    client = _OrderClient([[], [_order("COMPLETE", fillshares="1")]])
    outcome = safety.run_order(
        client,  # type: ignore[arg-type]
        plan,
        OrderBudget(10),
        {},
        allow_fill=True,
        funded=True,
        sleep=lambda _: None,
    )
    assert outcome.filled is True
    assert client.calls.count("place_order") == 1


def test_run_order_keeps_production_rules_without_funded() -> None:
    client = _OrderClient([[]])
    with pytest.raises(Refusal, match="UAT mode"):
        safety.run_order(
            client,  # type: ignore[arg-type]
            _plan(transaction_type="SELL"),
            OrderBudget(),
            {},
            allow_fill=True,
        )
    assert client.calls == []


# -- prepare_plan -----------------------------------------------------------------------

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


class _Market:
    def __init__(self, limits: dict[str, Any]) -> None:
        self.limits = limits
        self.calls: list[str] = []

    def margins(self) -> Limits:
        self.calls.append("margins")
        return Limits.model_validate(self.limits)

    def instruments(self, exchange: str) -> list[dict[str, str]]:
        self.calls.append(f"instruments:{exchange}")
        return _NSE_ROWS

    def quote(self, exchange: str, token: str) -> Any:
        from tradesmart.models.market import Quote

        self.calls.append(f"quote:{exchange}:{token}")
        return Quote.model_validate(_NSE_QUOTE)


def test_prepare_plan_uses_balance_check_when_funded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(safety.OUTSIDE_HOURS_ENV, raising=False)
    market = _Market({"stat": "Ok", "cash": "20000.00"})  # funds_refusal would refuse this
    record: dict[str, Any] = {}
    plan = safety.prepare_plan(
        market,  # type: ignore[arg-type]
        safety.SEGMENTS["nse"],
        record,
        now=_ist(2026, 11, 11, 10, 0),
        funded_ceiling=Decimal("25000"),
    )
    assert plan.tradingsymbol == "SBIN-EQ"
    assert record["funds_check"] == "funded mode: balance <= ceiling"
    assert market.calls[0] == "margins"
    assert market.calls.count("margins") == 1


def test_prepare_plan_refuses_cash_above_the_ceiling_when_funded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(safety.OUTSIDE_HOURS_ENV, raising=False)
    market = _Market({"stat": "Ok", "cash": "25000.01"})
    with pytest.raises(Refusal, match="ceiling"):
        safety.prepare_plan(
            market,  # type: ignore[arg-type]
            safety.SEGMENTS["nse"],
            {},
            now=_ist(2026, 11, 11, 10, 0),
            funded_ceiling=Decimal("25000"),
        )
    assert market.calls == ["margins"]


def test_prepare_plan_refuses_uat_and_funded_together(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(safety.OUTSIDE_HOURS_ENV, raising=False)
    market = _Market({"stat": "Ok", "cash": "0.00"})
    with pytest.raises(Refusal, match="funded"):
        safety.prepare_plan(
            market,  # type: ignore[arg-type]
            safety.SEGMENTS["nse"],
            {},
            now=_ist(2026, 11, 11, 10, 0),
            uat=True,
            funded_ceiling=Decimal("25000"),
        )
    assert market.calls == []


def test_prepare_plan_keeps_the_zero_funds_check_without_a_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(safety.OUTSIDE_HOURS_ENV, raising=False)
    market = _Market({"stat": "Ok", "cash": "20000.00"})
    with pytest.raises(Refusal, match="zero-funds"):
        safety.prepare_plan(
            market,  # type: ignore[arg-type]
            safety.SEGMENTS["nse"],
            {},
            now=_ist(2026, 11, 11, 10, 0),
        )


# -- money budget, close-only SELL, depth prices (tests/live_orders/funded.py) -----------

_SYMBOL = "SBIN-EQ"  # the instrument of _plan


def test_money_constants() -> None:
    assert Decimal("2000") == funded.MAX_ORDER_VALUE
    assert Decimal("5000") == funded.MAX_RUN_VALUE
    assert funded.FUNDED_PER_TEST_CAP == 2


def test_order_value_uses_price_or_trigger() -> None:
    assert funded.order_value(_plan(price=Decimal("720.00"), quantity=2)) == Decimal("1440.00")
    stop = _plan(order_type="SL-LMT", price=Decimal("800.00"), trigger_price=Decimal("790.00"))
    assert funded.order_value(stop) == Decimal("800.00")
    above = _plan(order_type="SL-LMT", price=Decimal("800.00"), trigger_price=Decimal("810.00"))
    assert funded.order_value(above) == Decimal("810.00")
    option = _plan(price=Decimal("26.45"), quantity=75)
    assert funded.order_value(option) == Decimal("1983.75")


def test_money_budget_limits() -> None:
    money = funded.MoneyBudget()
    assert money.per_order == Decimal("2000") and money.per_run == Decimal("5000")
    assert money.committed == 0
    money.check(Decimal("2000"))
    with pytest.raises(Refusal, match="2000"):
        money.check(Decimal("2000.05"))
    money.reserve(Decimal("1800"))
    money.reserve(Decimal("1800"))
    with pytest.raises(Refusal, match="5000"):
        money.reserve(Decimal("1800"))
    assert money.committed == Decimal("3600")
    with pytest.raises(Refusal):
        money.reserve(Decimal("2000.05"))  # over one order's limit: nothing reserved
    assert money.committed == Decimal("3600")
    money.release(Decimal("1800"))
    assert money.committed == Decimal("1800")
    money.reserve(Decimal("1800"))  # release frees room
    assert money.committed == Decimal("3600")


@pytest.mark.parametrize("value", ["0", "-1", "NaN", "Infinity", "sNaN"])
def test_money_budget_refuses_a_value_it_cannot_judge(value: str) -> None:
    money = funded.MoneyBudget()
    with pytest.raises(Refusal):
        money.check(Decimal(value))
    with pytest.raises(Refusal):
        money.reserve(Decimal(value))
    assert money.committed == 0


def test_money_budget_never_releases_more_than_it_holds() -> None:
    money = funded.MoneyBudget()
    money.reserve(Decimal("100"))
    with pytest.raises(ValueError):
        money.release(Decimal("100.01"))
    assert money.committed == Decimal("100")


def test_open_session_funded_sets_a_fresh_money_budget() -> None:
    first = safety.open_session(_ConfiguredClient(PRODUCTION_URL), FUNDED)  # type: ignore[arg-type]
    second = safety.open_session(_ConfiguredClient(PRODUCTION_URL), FUNDED)  # type: ignore[arg-type]
    assert isinstance(first.money, funded.MoneyBudget)
    assert first.money == funded.MoneyBudget()
    assert first.money is not second.money


class _FundedClient(FakeClient):
    """``FakeClient`` on the production URL that also quotes (with depth when given)."""

    def __init__(self, books: list[Any], *, quote: dict[str, str] | None = None, **kw: Any) -> None:
        super().__init__(books, **kw)
        self.config = ClientConfig(base_url=PRODUCTION_URL)
        self.quote_body = quote or {"stat": "Ok", "lp": "812.33", "ti": "0.05"}

    def quote(self, exchange: str, token: str) -> Any:
        self.calls.append(("quote", (exchange, token)))
        return Quote.model_validate(self.quote_body)


def _funded_session(client: FakeClient) -> safety.OrderSession:
    session = safety.open_session(client, FUNDED)  # type: ignore[arg-type]
    session.test_budget = OrderBudget(funded.FUNDED_PER_TEST_CAP)
    return session


def _run(session: safety.OrderSession, plan: OrderPlan, **kwargs: Any) -> Any:
    t = FakeTime()
    record: dict[str, Any] = {}
    kwargs.setdefault("settle_seconds", 1.0)
    kwargs.setdefault("poll_interval", 1.0)
    kwargs.setdefault("cancel_settle_seconds", 1.0)
    outcome = lifecycle.run_tracked(session, plan, record, sleep=t.sleep, clock=t.clock, **kwargs)
    return outcome, record


def _buy(quantity: int = 2, price: str = "800.00") -> OrderPlan:
    return _plan(quantity=quantity, price=Decimal(price), product="MIS")


def _sell(quantity: int = 1, price: str = "800.00") -> OrderPlan:
    return _plan(transaction_type="SELL", quantity=quantity, price=Decimal(price), product="MIS")


def _committed(session: safety.OrderSession) -> Decimal:
    assert session.money is not None
    return session.money.committed


def test_money_budget_partial_fill_keeps_reservation() -> None:
    # Review Focus 4: 1 of 2 filled, then cancelled -> the whole reservation stays.
    client = _FundedClient(
        [
            [],
            [_order("OPEN", fillshares="1")],
            [_order("OPEN", fillshares="1")],
            [_order("CANCELED", fillshares="1")],
        ]
    )
    session = _funded_session(client)
    outcome, _ = _run(session, _buy())
    assert outcome.filled is True and outcome.cancelled is True
    assert _committed(session) == Decimal("1600.00")
    assert session.fills == {_SYMBOL: 1}


def test_money_budget_zero_fill_releases_reservation() -> None:
    client = _FundedClient(
        [[], [_order("OPEN")], [_order("OPEN")], [_order("CANCELED", fillshares="0")]]
    )
    session = _funded_session(client)
    outcome, _ = _run(session, _buy())
    assert outcome.filled is False and outcome.final_status == "CANCELED"
    assert _committed(session) == 0
    assert session.fills == {}


def test_money_budget_full_fill_keeps_reservation_and_records_the_fill() -> None:
    client = _FundedClient([[], [_order("COMPLETE", fillshares="2")]])
    session = _funded_session(client)
    _run(session, _buy())
    assert _committed(session) == Decimal("1600.00")
    assert session.fills == {_SYMBOL: 2}


def test_money_budget_rejected_buy_releases_reservation() -> None:
    rejected = APIError("RMS: blocked", endpoint="PlaceOrder", http_status=200)
    client = _FundedClient([[]], place=rejected)
    session = _funded_session(client)
    outcome, _ = _run(session, _buy())
    assert outcome.order_id is None and outcome.definitive_rejection
    assert _committed(session) == 0


def test_money_budget_unknown_outcome_keeps_reservation() -> None:
    client = _FundedClient([[]], place=OrderStatusUnknownError("PlaceOrder", "read timeout"))
    session = _funded_session(client)
    with pytest.raises(safety.CheckTheAccount):
        _run(session, _buy())
    assert _committed(session) == Decimal("1600.00")
    assert session.fills == {}


def test_money_budget_unknown_fill_quantity_keeps_reservation() -> None:
    # Filled (COMPLETE) but no fill quantity anywhere: kept, and nothing may be sold.
    client = _FundedClient([[], [_order("COMPLETE")]])
    session = _funded_session(client)
    outcome, _ = _run(session, _buy())
    assert outcome.filled is True
    assert _committed(session) == Decimal("1600.00")
    assert session.fills == {}


def test_money_budget_not_sent_releases_reservation() -> None:
    other = Order.model_validate({"norenordno": "9", "status": "OPEN", "remarks": "other"})
    client = _FundedClient([[other]])
    session = _funded_session(client)
    with pytest.raises(Refusal, match="open order"):
        _run(session, _buy())
    assert client.count("place_order") == 0
    assert _committed(session) == 0


def test_money_budget_refuses_a_buy_over_the_order_limit_before_any_call() -> None:
    client = _FundedClient([[]])
    session = _funded_session(client)
    with pytest.raises(Refusal, match="2000"):
        _run(session, _buy(quantity=3))  # 2400
    assert client.calls == []
    assert session.budget.used == 0
    assert session.test_budget is not None and session.test_budget.used == 0


def test_money_budget_refuses_a_buy_over_the_run_limit_before_sending() -> None:
    client = _FundedClient([[]])
    session = _funded_session(client)
    assert session.money is not None
    session.money.reserve(Decimal("1800"))
    session.money.reserve(Decimal("1800"))
    with pytest.raises(Refusal, match="5000"):
        _run(session, _buy())  # 1600 more: 5200
    assert client.count("place_order") == 0
    assert session.budget.used == 0
    assert session.test_budget is not None and session.test_budget.used == 0
    assert _committed(session) == Decimal("3600")


def test_funded_session_without_a_money_budget_is_refused() -> None:
    client = _FundedClient([[]])
    session = safety.OrderSession(client, budget=OrderBudget(10), funded=True)  # type: ignore[arg-type]
    with pytest.raises(Refusal, match="money budget"):
        _run(session, _buy())
    assert client.calls == []


@pytest.mark.parametrize("plan", [_buy(quantity=1), _sell()])
def test_funded_session_without_a_per_test_budget_is_refused(plan: OrderPlan) -> None:
    # The per-test cap of 2 holds only through session.test_budget: no budget, no order.
    client = _FundedClient([[]])
    session = safety.open_session(client, FUNDED)  # type: ignore[arg-type]
    session.fills[plan.tradingsymbol] = 1
    assert session.test_budget is None
    with pytest.raises(Refusal, match="per-test"):
        _run(session, plan)
    assert client.calls == []
    assert session.budget.used == 0
    assert _committed(session) == 0


def test_uat_session_without_a_per_test_budget_keeps_the_run_cap_only() -> None:
    client = _FundedClient([[], [_order("CANCELED", fillshares="0")]])
    session = safety.OrderSession(client, budget=OrderBudget(10), uat=True)  # type: ignore[arg-type]
    assert session.test_budget is None
    _run(session, _buy(quantity=1))
    assert client.count("place_order") == 1


def test_uat_and_funded_session_is_refused_by_run_tracked() -> None:
    client = _FundedClient([[]])
    session = safety.OrderSession(
        client,  # type: ignore[arg-type]
        budget=OrderBudget(10),
        funded=True,
        uat=True,
        money=funded.MoneyBudget(),
    )
    with pytest.raises(Refusal, match="funded"):
        _run(session, _buy())
    assert client.calls == []


@pytest.mark.parametrize("side", ["S", "sell", "B", ""])
def test_funded_mode_refuses_an_unknown_side(side: str) -> None:
    client = _FundedClient([[]])
    session = _funded_session(client)
    with pytest.raises(Refusal, match="BUY or SELL"):
        _run(session, _plan(transaction_type=side))
    assert client.calls == []


def test_sell_refused_unless_closing_same_test_buy() -> None:
    client = _FundedClient(
        [
            [],
            [_order("COMPLETE", fillshares="1")],
            [],
            [_order("COMPLETE", fillshares="1")],
        ]
    )
    session = _funded_session(client)
    with pytest.raises(Refusal, match="close"):
        _run(session, _sell())  # nothing bought: no call at all
    assert client.calls == []
    _run(session, _buy(quantity=1))
    assert session.fills == {_SYMBOL: 1}
    with pytest.raises(Refusal, match="close"):
        _run(session, _sell(quantity=2))
    assert client.count("place_order") == 1
    _run(session, _sell())
    assert client.count("place_order") == 2
    assert session.fills.get(_SYMBOL, 0) == 0
    # The SELL leaves the BUY's money reserved: the run limit counts BUY fills.
    assert _committed(session) == Decimal("800.00")


def test_sell_of_another_instrument_is_refused() -> None:
    client = _FundedClient([[]])
    session = _funded_session(client)
    session.fills["ITC-EQ"] = 5
    with pytest.raises(Refusal, match="close"):
        _run(session, _sell())
    assert client.calls == []


def test_closing_sell_above_the_order_limit_is_sent() -> None:
    # Fix round 1: a SELL that closes a fill is not held to the per-order value (the premium
    # may rise while the BUY fills); its quantity is already capped by session.fills.
    client = _FundedClient([[], [_order("COMPLETE", fillshares="3")]])
    session = _funded_session(client)
    session.fills[_SYMBOL] = 3
    outcome, _ = _run(session, _sell(quantity=3))  # 2400
    assert outcome.filled is True
    assert client.count("place_order") == 1
    assert session.fills.get(_SYMBOL, 0) == 0
    assert _committed(session) == 0  # a SELL never reserves money


@pytest.mark.parametrize("held", [0, 2])
def test_sell_above_the_order_limit_that_closes_no_fill_is_refused(held: int) -> None:
    client = _FundedClient([[]])
    session = _funded_session(client)
    if held:
        session.fills[_SYMBOL] = held
    with pytest.raises(Refusal, match="close"):
        _run(session, _sell(quantity=3))  # 2400, more than was bought
    assert client.calls == []
    assert session.fills.get(_SYMBOL, 0) == held


def test_closing_sell_keeps_the_limit_order_types() -> None:
    client = _FundedClient([[]])
    session = _funded_session(client)
    session.fills[_SYMBOL] = 3
    with pytest.raises(Refusal, match="limit orders only"):
        _run(session, _plan(transaction_type="SELL", quantity=3, order_type="MARKET"))
    assert client.count("place_order") == 0


def test_close_position_sends_a_close_above_the_order_limit() -> None:
    client = _FundedClient(
        [[], [_order("COMPLETE", fillshares="3")]],
        quote={"stat": "Ok", "lp": "812.33", "bp1": "811.12", "ti": "0.05"},
        positions=[[]],
    )
    session = _funded_session(client)
    session.fills[_SYMBOL] = 3
    t = FakeTime()
    lifecycle.close_position(
        session,
        _buy(quantity=3),
        3,
        {},
        net_before=0,
        settle_seconds=2.0,
        sleep=t.sleep,
        clock=t.clock,
        use_depth=True,
    )
    sent = _sent(client)
    assert len(sent) == 1 and sent[0]["quantity"] == 3  # 3 x 811.10 = 2433.30


def test_sell_is_subtracted_when_sent_even_if_its_outcome_is_unknown() -> None:
    client = _FundedClient([[]], place=OrderStatusUnknownError("PlaceOrder", "read timeout"))
    session = _funded_session(client)
    session.fills[_SYMBOL] = 1
    with pytest.raises(safety.CheckTheAccount):
        _run(session, _sell())
    assert session.fills.get(_SYMBOL, 0) == 0


def test_sell_that_fails_client_side_validation_keeps_the_fill() -> None:
    # Task 4 fix round 1: nothing was sent, so the position is still open; the fill stays in
    # session.fills, where the funded tests' end check finds it and stops the run.
    from tradesmart.errors import RequestValidationError

    invalid = RequestValidationError([{"loc": ["qty"], "msg": "bad"}])
    client = _FundedClient([[]], place=invalid)
    session = _funded_session(client)
    session.fills[_SYMBOL] = 1
    with pytest.raises(AssertionError, match="client-side validation"):
        _run(session, _sell())
    assert session.fills == {_SYMBOL: 1}


def test_uat_session_keeps_its_rules() -> None:
    # UAT: no money budget, a SELL needs no earlier fill, nothing is counted.
    client = FakeClient([[], [_order("COMPLETE", fillshares="1")]])
    session = safety.OrderSession(client, budget=OrderBudget(10), uat=True)  # type: ignore[arg-type]
    outcome, _ = _run(session, _sell(quantity=5, price="9999"))
    assert outcome.filled is True
    assert session.money is None and session.fills == {}


def _quote(**fields: str) -> Quote:
    return Quote.model_validate({"stat": "Ok", "lp": "100.00", **fields})


def test_reference_price_depth_and_fallback() -> None:
    depth = _quote(bp1="99.95", sp1="100.05")
    assert lifecycle.reference_price(depth, "BUY", use_depth=True) == Decimal("100.05")
    assert lifecycle.reference_price(depth, "SELL", use_depth=True) == Decimal("99.95")
    assert lifecycle.reference_price(depth, "BUY", use_depth=False) == Decimal("100.00")
    assert lifecycle.reference_price(depth, "SELL", use_depth=False) == Decimal("100.00")
    empty = _quote(bp1="0.00", sp1="0")
    assert lifecycle.reference_price(empty, "BUY", use_depth=True) == Decimal("100.00")
    assert lifecycle.reference_price(empty, "SELL", use_depth=True) == Decimal("100.00")
    assert lifecycle.reference_price(_quote(), "SELL", use_depth=True) == Decimal("100.00")
    nothing = Quote.model_validate({"stat": "Ok"})
    assert lifecycle.reference_price(nothing, "BUY", use_depth=True) is None


def _close(session: safety.OrderSession, **kwargs: Any) -> Any:
    t = FakeTime()
    record: dict[str, Any] = {}
    return lifecycle.close_position(
        session,
        _buy(quantity=1),
        1,
        record,
        net_before=0,
        settle_seconds=2.0,
        sleep=t.sleep,
        clock=t.clock,
        **kwargs,
    )


def _sent(client: FakeClient) -> list[dict[str, Any]]:
    return [kwargs for name, kwargs in client.calls if name == "place_order"]


def test_close_position_uses_best_bid_when_depth() -> None:
    client = _FundedClient(
        [[], [_order("COMPLETE", fillshares="1")]],
        quote={"stat": "Ok", "lp": "812.33", "bp1": "811.12", "sp1": "812.40", "ti": "0.05"},
    )
    session = _funded_session(client)
    session.fills[_SYMBOL] = 1
    _close(session, use_depth=True)
    sent = _sent(client)
    assert len(sent) == 1
    assert sent[0]["transaction_type"] == "SELL" and sent[0]["price"] == Decimal("811.10")
    assert session.fills.get(_SYMBOL, 0) == 0


def test_close_position_falls_back_to_the_last_price_without_a_bid() -> None:
    client = _FundedClient(
        [[], [_order("COMPLETE", fillshares="1")]],
        quote={"stat": "Ok", "lp": "812.33", "bp1": "0.00", "ti": "0.05"},
    )
    session = _funded_session(client)
    session.fills[_SYMBOL] = 1
    _close(session, use_depth=True)
    assert _sent(client)[0]["price"] == Decimal("812.30")


def test_close_position_without_depth_sells_at_the_last_price() -> None:
    client = _FundedClient(
        [[], [_order("COMPLETE", fillshares="1")]],
        quote={"stat": "Ok", "lp": "812.33", "bp1": "811.12", "ti": "0.05"},
    )
    session = _funded_session(client)
    session.fills[_SYMBOL] = 1
    _close(session)
    assert _sent(client)[0]["price"] == Decimal("812.30")


def test_close_position_in_funded_mode_needs_a_same_test_fill() -> None:
    client = _FundedClient([[], [_order("COMPLETE", fillshares="1")]])
    session = _funded_session(client)
    with pytest.raises(safety.CheckTheAccount, match="position left open"):
        _close(session, use_depth=True)
    assert client.count("place_order") == 0


# -- funded instruments, option strike by premium, stage-two gate -----------------------


def test_funded_segments_are_cash_and_options_only() -> None:
    segments = funded.FUNDED_SEGMENTS
    assert list(segments) == ["nse", "bse", "nfoopt", "bfoopt"]
    assert {s.product for s in segments.values()} == {"MIS"}
    assert all(s.instrument not in ("FUTIDX", "FUTCOM") for s in segments.values())
    assert all(s.id == key for key, s in segments.items())
    nse, bse = segments["nse"], segments["bse"]
    assert (nse.exchange, nse.tradingsymbol) == ("NSE", "ITC-EQ")
    assert (bse.exchange, bse.tradingsymbol) == ("BSE", "ITC")
    nfo, bfo = segments["nfoopt"], segments["bfoopt"]
    assert (nfo.exchange, nfo.instrument, nfo.symbol) == ("NFO", "OPTIDX", "NIFTY")
    assert (bfo.exchange, bfo.instrument, bfo.symbol) == ("BFO", "OPTIDX", "BSXOPT")
    assert (bfo.future_symbol, bfo.label) == ("BSXFUT", "SENSEX")
    for key, segment in segments.items():  # the UAT instrument, only the product differs
        uat = lifecycle.UAT_SEGMENTS[key]
        assert dataclasses.replace(segment, product=uat.product) == uat
        assert (segment.opens, segment.closes) == (uat.opens, uat.closes)
    assert {"nfoopt", "bfoopt"} == funded.OPTION_IDS
    assert {"cash": Decimal("0.9"), "option": Decimal("0.5")} == funded.STAGE1_RATIO
    assert funded.PREMIUM_STRIKE_TRIES == 40


@pytest.mark.parametrize(
    ("last", "tick", "kind", "expected"),
    [
        ("400.00", "0.05", "cash", "360.00"),
        ("401.37", "0.05", "cash", "361.20"),  # 361.233 floored to the tick
        ("120.50", "0.05", "option", "60.25"),
        ("120.55", "0.05", "option", "60.25"),  # 60.275 floored
        ("0.15", "0.05", "option", "0.05"),  # 0.075 floored: one tick
        ("0.10", "0.05", "option", "0.05"),
        ("0.08", "0.05", "option", "0.05"),  # 0.04 floors to 0: at least one tick
        ("1234", "1", "cash", "1110"),
    ],
)
def test_far_buy_price(last: str, tick: str, kind: str, expected: str) -> None:
    price = funded.far_buy_price(Decimal(last), Decimal(tick), funded.STAGE1_RATIO[kind])
    assert price == Decimal(expected)
    assert str(price) == expected
    assert price < Decimal(last)


@pytest.mark.parametrize(
    ("last", "tick"),
    [
        (None, Decimal("0.05")),
        (Decimal("0"), Decimal("0.05")),
        (Decimal("-5"), Decimal("0.05")),
        (Decimal("NaN"), Decimal("0.05")),
        (Decimal("Infinity"), Decimal("0.05")),
        (Decimal("100"), None),
        (Decimal("100"), Decimal("0")),
        (Decimal("100"), Decimal("-0.05")),
        (Decimal("100"), Decimal("NaN")),
    ],
)
def test_far_buy_price_refuses_without_a_price_or_tick(
    last: Decimal | None, tick: Decimal | None
) -> None:
    with pytest.raises(Refusal):
        funded.far_buy_price(last, tick, Decimal("0.9"))  # type: ignore[arg-type]


def test_far_buy_price_refuses_a_price_not_below_the_market() -> None:
    # one tick is the last price itself: such a BUY could fill, so no stage-1 order
    with pytest.raises(Refusal, match="below"):
        funded.far_buy_price(Decimal("0.05"), Decimal("0.05"), Decimal("0.5"))


@pytest.mark.parametrize("ratio", ["0", "1", "1.1", "-0.5", "NaN"])
def test_far_buy_price_rejects_a_ratio_outside_0_to_1(ratio: str) -> None:
    with pytest.raises(ValueError, match="ratio"):
        funded.far_buy_price(Decimal("100"), Decimal("0.05"), Decimal(ratio))


def _c(strike: str, last: str | None, lot: int = 65) -> tuple[Decimal, Decimal | None, int]:
    return Decimal(strike), None if last is None else Decimal(last), lot


@pytest.mark.parametrize(
    ("candidates", "expected"),
    [
        ([], None),  # none
        ([_c("26100", "30")], 0),  # one: 30 x 65 = 1950
        ([_c("26300", "10"), _c("26100", "20"), _c("26200", "15")], 1),  # lowest strike first
        ([_c("26100", "31")], None),  # premium too high: 2015
        ([_c("26100", "120"), _c("26200", "40"), _c("26300", "30")], 2),  # nearest affordable
        ([_c("26100", "0"), _c("26200", None), _c("26300", "12")], 2),  # last price 0 / None
        ([_c("26100", "0"), _c("26200", None)], None),
        ([_c("25900", "5"), _c("26000", "4"), _c("26040", "3")], None),  # all at / below ref
        ([_c("26000", "4"), _c("26050", "25")], 1),  # the strike at the reference is not above
        ([_c("26100", "30", lot=0), _c("26200", "20")], 1),  # no lot: value cannot be judged
        ([_c("26100", "NaN"), _c("26200", "20")], 1),  # a price that is not finite
        ([_c("26100", "-1"), _c("26200", "20")], 1),
    ],
)
def test_choose_strike_cases(
    candidates: list[tuple[Decimal, Decimal | None, int]], expected: int | None
) -> None:
    assert funded.choose_strike(candidates, Decimal("26040"), Decimal("2000")) == expected


def test_choose_strike_accepts_a_value_of_exactly_the_limit() -> None:
    assert funded.choose_strike([_c("26100", "40", lot=50)], Decimal("26040"), Decimal("2000")) == 0
    assert (
        funded.choose_strike([_c("26100", "40.05", lot=50)], Decimal("26040"), Decimal("2000"))
        is None
    )


_PREMIUM_ROWS = [
    _FUTURE_ROW,
    _option_row("601", "25900"),
    _option_row("602", "26000"),
    _option_row("603", "26100"),
    _option_row("604", "26200"),
    _option_row("605", "26300"),
    _option_row("606", "26400"),
    _option_row("607", "26300", expiry="02-DEC-2026"),  # a later expiry
    _option_row("608", "26300", OptionType="PE"),
]
_TODAY = date(2026, 11, 11)


def _premium(
    client: _QuoteClient,
    rows: list[dict[str, str]] | None = None,
    record: dict[str, Any] | None = None,
    segment_id: str = "nfoopt",
    **kwargs: Any,
) -> Any:
    return funded.pick_option_by_premium(
        client,  # type: ignore[arg-type]
        funded.FUNDED_SEGMENTS[segment_id],
        _PREMIUM_ROWS if rows is None else rows,
        _TODAY,
        record if record is not None else {},
        **kwargs,
    )


def test_pick_option_by_premium_takes_the_nearest_affordable_strike_above() -> None:
    client = _QuoteClient(
        {"500": "26040.00", "602": "5.00", "603": "120.00", "604": "40.00", "605": "27.00"}
    )
    record: dict[str, Any] = {}
    inst = _premium(client, record=record)
    assert (inst.tradingsymbol, inst.token, inst.lot_size) == ("NIFTY25NOV26C26300", "605", 65)
    assert inst.tick_size == Decimal("0.05") and inst.expiry == date(2026, 11, 25)
    assert client.quoted == ["500", "603", "604", "605"]  # never 602 (below), stops at 605
    option = record["option"]
    assert option["underlying"] == "NIFTY25NOV26F"
    assert option["underlying_last_price"] == "26040.00"
    assert (option["strike"], option["premium"], option["lot"]) == ("26300", "27.00", 65)
    assert option["value"] == "1755.00"
    assert [t["tradingsymbol"] for t in option["tried"]] == [
        "NIFTY25NOV26C26100",
        "NIFTY25NOV26C26200",
        "NIFTY25NOV26C26300",
    ]


def test_pick_option_by_premium_uses_the_given_limit() -> None:
    client = _QuoteClient({"500": "26040.00", "603": "120.00", "604": "40.00"})
    inst = _premium(client, max_value=Decimal("2600"))
    assert inst.token == "604"


def test_strike_premium_ceiling_leaves_headroom_below_the_order_limit() -> None:
    # A stage-2 BUY at the best offer may pay a little more than the last price quoted here.
    assert Decimal("1800") == funded.STRIKE_PREMIUM_CEILING
    assert funded.MAX_ORDER_VALUE * Decimal("0.9") == funded.STRIKE_PREMIUM_CEILING


def test_pick_option_by_premium_defaults_to_the_ceiling_not_the_order_limit() -> None:
    # 30 x 65 = 1950: within the 2000 order limit, above the 1800 ceiling
    prices = {"500": "26040.00", "603": "30.00", "604": "27.70", "605": "27.69"}
    record: dict[str, Any] = {}
    inst = _premium(_QuoteClient(prices), record=record)
    assert inst.token == "605"  # 27.70 x 65 = 1800.50 is above the ceiling; 27.69 x 65 = 1799.85
    assert record["option"]["max_value"] == "1800"
    assert _premium(_QuoteClient(prices), max_value=funded.MAX_ORDER_VALUE).token == "603"


def test_pick_option_by_premium_works_on_bfo_sensex() -> None:
    # the observed BFO shape: future Symbol BSXFUT, options BSXOPT, plus SENSEX 50 (SX50*) rows
    client = _QuoteClient(
        {"1100929": "85020.00", "1102": "150.00", "1103": "85.00", "900": "85020.00"}
        | {"901": "10.00", "902": "10.00"}  # SENSEX 50: cheap, but never SENSEX
    )
    record: dict[str, Any] = {}
    inst = _premium(client, rows=_BFO_ROWS, record=record, segment_id="bfoopt")
    assert (inst.tradingsymbol, inst.token, inst.lot_size) == ("SENSEX26NOV85200CE", "1103", 20)
    assert record["option"]["underlying"] == "SENSEX26NOVFUT"
    assert record["option"]["value"] == "1700.00"  # 85 x 20
    assert client.quoted == ["1100929", "1102", "1103"]


def test_pick_option_by_premium_never_takes_sensex_50_rows_for_sensex() -> None:
    sensex_50 = [r for r in _BFO_ROWS if r["Symbol"].startswith("SX50")]
    client = _QuoteClient({"900": "85020.00", "901": "10.00", "902": "10.00"})
    with pytest.raises(Refusal, match="no SENSEX index future .*BFO script master"):
        _premium(client, rows=sensex_50, segment_id="bfoopt")
    assert client.quoted == []


def _strike_ladder(count: int) -> list[dict[str, str]]:
    """The future (26040) and ``count`` CE strikes above it, 50 apart (tokens 700, 701, ...)."""
    return [_FUTURE_ROW] + [_option_row(str(700 + i), str(26050 + 50 * i)) for i in range(count)]


def test_pick_option_by_premium_finds_an_affordable_strike_beyond_the_12th() -> None:
    # 2026-11-12: NIFTY ~25,280, the 12 nearest strikes all above 1800 per lot
    prices = {"500": "26040.00", **{str(700 + i): "500.00" for i in range(19)}}
    prices["719"] = "20.00"  # the 20th strike: 20 x 65 = 1300
    client = _QuoteClient(prices)
    inst = _premium(client, rows=_strike_ladder(45))
    assert (inst.token, inst.tradingsymbol) == ("719", "NIFTY25NOV26C27000")
    assert client.quoted == ["500"] + [str(700 + i) for i in range(20)]


def test_pick_option_by_premium_quotes_at_most_40_strikes() -> None:
    prices = {"500": "26040.00", **{str(700 + i): "500.00" for i in range(40)}}
    prices.update({"740": "10.00", "741": "10.00"})  # affordable, but beyond the 40th strike
    client = _QuoteClient(prices)
    with pytest.raises(Refusal, match="no NIFTY CE strike above 26040.00 .*1800.*of at most 40"):
        _premium(client, rows=_strike_ladder(42))
    assert client.quoted == ["500"] + [str(700 + i) for i in range(40)]


@pytest.mark.parametrize(
    ("rows", "prices", "words"),
    [
        (  # no future: the BFO script master may lack the SENSEX future
            [r for r in _PREMIUM_ROWS if r is not _FUTURE_ROW],
            {},
            "no NIFTY index future \\(FUTIDX\\) in the NFO script master",
        ),
        (None, {"500": "0.00"}, "no last price for the underlying future NIFTY25NOV26F"),
        (None, {}, "no last price for the underlying future NIFTY25NOV26F"),
        ([_FUTURE_ROW], {"500": "26040.00"}, "no OPTIDX NIFTY CE of a future expiry"),
        (  # no strike above the reference
            [_FUTURE_ROW, _option_row("601", "25900"), _option_row("602", "26000")],
            {"500": "26040.00", "601": "150.00", "602": "60.00"},
            "no NIFTY CE strike above 26040.00",
        ),
        (  # no affordable strike
            None,
            {"500": "26040.00", "603": "120.00", "604": "40.00", "605": "31.00", "606": "0"},
            "no NIFTY CE strike above 26040.00 .*1800",
        ),
    ],
    ids=["no-future", "future-price-0", "future-unquoted", "no-options", "none-above", "too-dear"],
)
def test_pick_option_by_premium_skips_cleanly(
    rows: list[dict[str, str]] | None, prices: dict[str, str], words: str
) -> None:
    client = _QuoteClient(prices)
    with pytest.raises(Refusal, match=words):
        _premium(client, rows=rows)


def test_pick_option_by_premium_skips_a_strike_whose_lot_is_unknown() -> None:
    rows = [_FUTURE_ROW, _option_row("603", "26100", LotSize=""), _option_row("604", "26200")]
    client = _QuoteClient({"500": "26040.00", "603": "10.00", "604": "20.00"})
    assert _premium(client, rows=rows).token == "604"


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({"TRADESMART_LIVE_ORDER_FILLS": "1"}, True),
        ({}, False),
        ({"TRADESMART_LIVE_ORDER_FILLS": "true"}, False),
        ({"TRADESMART_LIVE_ORDER_FILLS": " 1"}, False),
        ({"TRADESMART_LIVE_ORDER_FILLS": "0"}, False),
        ({"TRADESMART_LIVE_ORDER_FILLS": ""}, False),
    ],
)
def test_fills_enabled(env: dict[str, str], expected: bool) -> None:
    assert funded.fills_enabled(env) is expected


def _stage_session(**fields: Any) -> safety.OrderSession:
    return safety.OrderSession(client=None, **{"funded": True, **fields})  # type: ignore[arg-type]


FILLS = {"TRADESMART_LIVE_ORDER_FILLS": "1"}


def test_stage_two_reason_none_when_every_condition_holds() -> None:
    assert funded.stage_two_reason(_stage_session(), FILLS, set()) is None


@pytest.mark.parametrize(
    ("fields", "env", "failures", "words"),
    [
        ({}, {}, (), "TRADESMART_LIVE_ORDER_FILLS"),
        ({}, {"TRADESMART_LIVE_ORDER_FILLS": "yes"}, (), "TRADESMART_LIVE_ORDER_FILLS"),
        ({"funded": False}, FILLS, (), "funded mode"),
        ({"funded": False, "uat": True}, FILLS, (), "funded mode"),
        ({"stage1_failed": True}, FILLS, (), "stage-1"),
        ({"stopped": True}, FILLS, (), "CHECK THE ACCOUNT"),
        ({}, FILLS, ("tests/live_orders/x.py::test_stage1_a[nse]",), "test_stage1_a[nse]"),
    ],
)
def test_stage_two_reason_refuses(
    fields: dict[str, Any], env: dict[str, str], failures: tuple[str, ...], words: str
) -> None:
    reason = funded.stage_two_reason(_stage_session(**fields), env, set(failures))
    assert reason is not None and words in reason


def test_stage_two_reason_names_the_failed_stage1_tests() -> None:
    reason = funded.stage_two_reason(_stage_session(), FILLS, {"m.py::test_stage1_b", "m.py::a"})
    assert reason is not None
    assert "2 stage-1 test(s) failed" in reason and "m.py::a, m.py::test_stage1_b" in reason


@pytest.mark.parametrize(
    ("now", "refused"),
    [
        (_ist(2026, 11, 12, 9, 20), False),
        (_ist(2026, 11, 12, 14, 29), False),
        (_ist(2026, 11, 12, 14, 30), True),
        (_ist(2026, 11, 12, 15, 10), True),
        (datetime(2026, 11, 12, 8, 59, tzinfo=UTC), False),  # 14:29 IST
        (datetime(2026, 11, 12, 9, 0, tzinfo=UTC), True),  # 14:30 IST
        (datetime(2026, 11, 12, 11, 0), True),  # no time zone: cannot be judged
    ],
)
def test_stage_two_time_reason_refuses_from_14_30_ist(now: datetime, refused: bool) -> None:
    reason = funded.stage_two_time_reason(now)
    assert (reason is not None) is refused
    if refused:
        assert reason is not None and "14:30 IST" in reason and "no order" in reason


def test_stage_two_cut_off_is_14_30_ist() -> None:
    cut_off = funded.STAGE2_LAST_BUY_IST
    assert (cut_off.hour, cut_off.minute, cut_off.second) == (14, 30, 0)


def test_now_ist_is_aware_and_in_ist() -> None:
    now = funded.now_ist()
    assert now.tzinfo is not None and now.utcoffset() == IST.utcoffset(now)


def test_order_session_is_not_stopped_by_default() -> None:
    assert safety.OrderSession(client=None).stopped is False  # type: ignore[arg-type]


# The hook keeps each phase's report on the item; a skip is not a failure.


class _Report:
    def __init__(self, when: str, outcome: str) -> None:
        self.when = when
        self.outcome = outcome
        self.failed = outcome == "failed"
        self.skipped = outcome == "skipped"
        self.passed = outcome == "passed"


class _Item:
    def __init__(self, **phases: str) -> None:
        self.stash = pytest.Stash()
        if phases:
            self.stash[live_conftest.REPORTS] = {w: _Report(w, o) for w, o in phases.items()}


@pytest.mark.parametrize(
    ("phases", "failed"),
    [
        ({"setup": "passed", "call": "passed"}, False),
        ({"setup": "skipped"}, False),
        ({"setup": "passed", "call": "skipped"}, False),
        ({"setup": "passed", "call": "failed"}, True),
        ({"setup": "failed"}, True),
        ({"setup": "passed", "call": "passed", "teardown": "failed"}, True),
        ({"setup": "passed", "call": "passed", "teardown": "passed"}, False),
        ({"setup": "skipped", "teardown": "failed"}, True),
        ({}, True),  # no report kept: cannot tell, so failed (fail closed)
        ({"setup": "passed"}, True),  # setup passed but no call report: cannot tell
    ],
)
def test_stage_one_failed_from_the_kept_reports(phases: dict[str, str], failed: bool) -> None:
    assert live_conftest.stage_one_failed(_Item(**phases)) is failed  # type: ignore[arg-type]


def test_stage_one_rule_is_the_test_name_prefix() -> None:
    assert live_conftest.STAGE1_PREFIX == "test_stage1_"
    names = {
        "test_stage1_rest_modify_cancel[nse]": True,
        "test_stage1_ioc": True,
        "test_stage2_fill[nse]": False,
        "test_stage1": False,
        "test_uat_stage1_x": False,
    }
    for name, expected in names.items():
        assert live_conftest.is_stage1_item(SimpleNamespace(name=name)) is expected  # type: ignore[arg-type]


def test_stage_one_failures_are_empty_before_any_report() -> None:
    config = SimpleNamespace(stash=pytest.Stash())
    assert live_conftest.stage1_failures(config) == frozenset()  # type: ignore[arg-type]


# Each inner run drives the real hookwrapper; the gate is read as a stage-2 test would read it:
# when the next test starts, and once more when the run ends.

_SKIP_AND_FAILURE_TESTS = """
import pytest

def test_stage1_a_skip():
    pytest.skip("market closed")

@pytest.mark.skip(reason="not this segment")
def test_stage1_b_marked_skip():
    pass

def test_stage1_c_pass():
    pass

def test_other_fail():  # not a stage-1 test: does not close the gate
    assert False

def test_stage1_d_fail():
    assert False

def test_stage1_e_after_the_failure():
    pass
"""

_SETUP_FAILURE_TESTS = """
import pytest

@pytest.fixture
def broken():
    raise RuntimeError("setup fails")

def test_stage1_a_pass():
    pass

def test_stage1_b_setup_error(broken):
    pass

def test_stage1_c_next():
    pass
"""

_TEARDOWN_FAILURE_TESTS = """
import pytest

@pytest.fixture
def cleanup():
    yield
    raise RuntimeError("cleanup found an order still open")

def test_stage1_a_teardown_error(cleanup):
    pass

def test_stage1_b_next():
    pass
"""


def _drive_hook(pytester: pytest.Pytester, source: str) -> tuple[Any, list[tuple[str, Any]]]:
    """Run ``source`` with the conftest hook and note the stage-two gate before each test
    and at the end of the run."""
    plugin = _GatePlugin()
    pytester.makepyfile(test_stage_one=source)
    result = pytester.runpytest_inprocess(
        "-p", "no:cacheprovider", "-p", "no:xdist", "-p", "no:asyncio", plugins=[plugin]
    )
    return result, plugin.seen


class _GatePlugin:
    """The conftest report hook plus two observers of the stage-two gate. A class instance (not a
    ``SimpleNamespace``): pytest 8.4 keeps registered plugins in a set, so they must hash."""

    pytest_runtest_makereport = staticmethod(live_conftest.pytest_runtest_makereport)

    def __init__(self) -> None:
        self.session = _stage_session()
        self.seen: list[tuple[str, Any]] = []

    def _gate(self, config: pytest.Config) -> str | None:
        return funded.stage_two_reason(self.session, FILLS, live_conftest.stage1_failures(config))

    @pytest.hookimpl(tryfirst=True)
    def pytest_runtest_setup(self, item: pytest.Item) -> None:
        self.seen.append((item.name, self._gate(item.config)))

    def pytest_sessionfinish(self, session: pytest.Session) -> None:
        self.seen.append(("end", self._gate(session.config)))


def _closed(reason: Any, *tests: str) -> bool:
    return isinstance(reason, str) and "stage-1" in reason and all(t in reason for t in tests)


def test_stage_two_gate_after_skip_and_failure(pytester: pytest.Pytester) -> None:
    result, seen = _drive_hook(pytester, _SKIP_AND_FAILURE_TESTS)
    result.assert_outcomes(passed=2, skipped=2, failed=2)
    assert [name for name, _ in seen] == [
        "test_stage1_a_skip",
        "test_stage1_b_marked_skip",
        "test_stage1_c_pass",
        "test_other_fail",
        "test_stage1_d_fail",
        "test_stage1_e_after_the_failure",
        "end",
    ]
    reasons = [reason for _, reason in seen]
    assert reasons[:5] == [None] * 5  # skips, a pass and a non-stage-1 failure keep it open
    assert _closed(reasons[5], "test_stage1_d_fail")
    assert reasons[6] == reasons[5]  # a later pass does not reopen it
    assert "test_other_fail" not in reasons[6]


def test_stage_two_gate_after_a_setup_failure(pytester: pytest.Pytester) -> None:
    result, seen = _drive_hook(pytester, _SETUP_FAILURE_TESTS)
    result.assert_outcomes(passed=2, errors=1)
    assert seen[:2] == [("test_stage1_a_pass", None), ("test_stage1_b_setup_error", None)]
    assert seen[2][0] == "test_stage1_c_next"
    assert _closed(seen[2][1], "test_stage1_b_setup_error")
    assert _closed(seen[3][1], "test_stage1_b_setup_error")


def test_stage_two_gate_after_a_stage1_teardown_failure(pytester: pytest.Pytester) -> None:
    # The test itself passed; its fixture's cleanup raised afterwards. Stage 2 must not run.
    result, seen = _drive_hook(pytester, _TEARDOWN_FAILURE_TESTS)
    result.assert_outcomes(passed=2, errors=1)
    assert seen[0] == ("test_stage1_a_teardown_error", None)
    assert seen[1][0] == "test_stage1_b_next"
    assert _closed(seen[1][1], "test_stage1_a_teardown_error")
    assert _closed(seen[2][1], "test_stage1_a_teardown_error")


# -- compose passes every live-order variable through ----------------------------------------


def test_compose_passes_every_live_order_variable_through() -> None:
    """Without the pass-through, funded mode cannot be switched on inside Docker (it fails
    closed). The bare ``- NAME`` form passes a variable only when set on the host: a
    ``${NAME:-}`` default would set it empty, and an empty value refuses every run."""
    from pathlib import Path

    compose = Path(__file__).resolve().parents[2] / "compose.yaml"
    lines = [line.strip() for line in compose.read_text(encoding="utf-8").splitlines()]
    names = [
        safety.LIVE_ENV,
        safety.ORDER_ENV,
        safety.ACCOUNT_ENV,
        safety.OUTSIDE_HOURS_ENV,
        safety.FUNDED_ENV,
        safety.FILLS_ENV,
        safety.MAX_BALANCE_ENV,
    ]
    for name in names:
        assert f"- {name}" in lines, name
        assert not any(line.startswith(f"- {name}=") for line in lines), name


# -- the production-mode tests never run on the funded account --------------------------------


class _NoCalls:
    """A client that fails the test on any use: the skip must come before any API call."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"API call {name} in funded mode")


@pytest.mark.parametrize(
    "name",
    [
        "test_limit_buy_is_rejected_or_cancelled",
        "test_modify_of_an_open_order",
        "test_cancel_of_a_rejected_order_is_definitive",
    ],
)
def test_production_order_tests_skip_in_funded_mode_before_any_call(name: str) -> None:
    from tests.live_orders import test_live_orders

    session = safety.OrderSession(_NoCalls(), funded=True)  # type: ignore[arg-type]
    record: dict[str, Any] = {}
    test = getattr(test_live_orders, name)
    with pytest.raises(pytest.skip.Exception, match="funded mode"):
        test("nse", session, record)
    assert "funded mode" in record["skipped"]


def test_pick_option_by_premium_reads_the_bfo_strike_column() -> None:
    # production BFO names the column "Strike" (NFO and MCX: "StrikePrice"), 2026-11-12
    bfo_rows = [
        {("Strike" if k == "StrikePrice" else k): v for k, v in row.items()} for row in _BFO_ROWS
    ]
    assert all("StrikePrice" not in row for row in bfo_rows)
    client = _QuoteClient({"1100929": "85020.00", "1102": "150.00", "1103": "85.00"})
    record: dict[str, Any] = {}
    inst = _premium(client, rows=bfo_rows, record=record, segment_id="bfoopt")
    assert inst.tradingsymbol == "SENSEX26NOV85200CE"
    assert record["option"]["strike"] == "85200"
