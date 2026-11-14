"""A dry run of the funded-mode live test bodies against the offline broker (no network).

The live tests in ``tests/live_orders/test_funded_live.py`` run only in funded mode, on a small
funded production account, with the owner. Here their bodies run unchanged with a funded session
built on an ``OfflineBroker`` client (the REST calls stay in memory and the stream server listens on
127.0.0.1), so a mistake in the test code shows up before the live run. Market hours are lifted
and the order waits run on a fake clock (``FakeClock``); the fake market (``UatMarket``) fills a
LIMIT BUY at or above the last price and a LIMIT SELL at or below it, cancels an IOC order away
from the market and lets everything else rest.

A test body that skips fails here unless the case expects the skip, so a silent skip (for example
an instrument missing from the offline broker) never passes as a dry run.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from tests.integration.test_uat_lifecycle_offline_broker import FakeClock, UatMarket
from tests.live_orders import conftest as live_conftest
from tests.live_orders import funded, lifecycle, safety
from tests.live_orders import test_funded_live as fl
from tests.live_orders import test_uat_lifecycle as uat
from tests.live_orders.safety import (
    CheckTheAccount,
    OrderPlan,
    OrderSession,
    Recorder,
    Refusal,
)
from tradesmart.models.base import IST
from tradesmart.testing import OfflineBroker

pytest_plugins = ["pytester"]  # the inner session of the real fixture chain (end of file)

EXPIRY = date.today() + timedelta(days=14)
MIS = "I"  # the wire code of product MIS
NIFTY_LOT, SENSEX_LOT = 65, 20
NIFTY_PICK, SENSEX_PICK = ("NFO", "603"), ("BFO", "802")  # the strikes the premium rule picks
BEFORE_CUT_OFF = datetime(2026, 11, 12, 11, 0, tzinfo=IST)  # the dry run's clock for stage 2


@dataclass
class Rig:
    broker: OfflineBroker
    session: OrderSession
    market: UatMarket
    stage1_failures: set[str] = field(default_factory=set)

    def request(self) -> Any:
        """A stand-in for pytest's ``request``: only ``config.stash`` is read (the gate)."""
        stash = pytest.Stash()
        stash[live_conftest.STAGE1_FAILURES] = self.stage1_failures
        return SimpleNamespace(config=SimpleNamespace(stash=stash))

    def run(
        self, test: Callable[..., None], segment_id: str, *, may_skip: bool = False
    ) -> dict[str, Any]:
        """Run one funded test as pytest would: the fixture's start and end around the body.

        A stage-1 body that fails is noted as the report hook would note it.
        """
        record: dict[str, Any] = {"test": test.__name__, "funded_mode": True}
        kwargs: dict[str, Any] = {
            "segment_id": segment_id,
            "funded_session": self.session,
            "funded_record": record,
        }
        if "request" in inspect.signature(test).parameters:
            kwargs["request"] = self.request()
        fl.begin_funded_test(self.session)
        try:
            test(**kwargs)
        except pytest.skip.Exception as exc:
            if may_skip:
                raise
            raise AssertionError(f"the test body skipped: {exc}") from exc
        except BaseException:
            if test.__name__.startswith(live_conftest.STAGE1_PREFIX):
                self.stage1_failures.add(f"{test.__name__}[{segment_id}]")
            raise
        finally:
            fl.end_funded_test(self.session, record)
        return record

    def net(self, tradingsymbol: str) -> int:
        positions = self.session.client.positions()
        return sum(p.net_quantity or 0 for p in positions if p.tradingsymbol == tradingsymbol)

    def statuses(self) -> list[tuple[str, str, str]]:
        return [(o.side, o.order_type, o.status) for o in self.broker.orders]


def _instruments(broker: OfflineBroker, market: UatMarket) -> None:
    """ITC on NSE and BSE, and NIFTY / SENSEX calls: the nearest strike within the premium
    ceiling (premium x lot <= 1800) is 26300 (25 x 65) and 85200 (80 x 20)."""

    def add(exchange: str, token: str, tsym: str, lp: str, offer: str, bid: str, **kw: Any) -> None:
        broker.add_instrument(exchange, token, tsym, **kw)
        lot = str(kw.get("lot_size", 1))
        tick = str(kw.get("tick_size", "0.05"))
        broker.set_quote(
            exchange, token, lp=lp, lc="0.05", uc="99999", ti=tick, ls=lot, sp1=offer, bp1=bid
        )
        market.last[tsym] = Decimal(lp)

    add("NSE", "1660", "ITC-EQ", "400.00", "400.05", "399.95", instrument="EQ")
    add("BSE", "500875", "ITC", "400.10", "400.15", "400.05", instrument="A")
    nifty = {"lot_size": NIFTY_LOT, "expiry": EXPIRY, "underlying": "NIFTY"}
    add("NFO", "500", "NIFTYFUT", "26040.00", "0", "0", instrument="FUTIDX", **nifty)
    for token, strike, lp in (
        ("600", 26000, "90.00"),  # in the money: never picked
        ("601", 26100, "75.00"),  # 75 x 65 = 4875: above the ceiling
        ("602", 26200, "40.00"),  # 2600: above the ceiling
        ("603", 26300, "25.00"),  # 1625: picked
        ("604", 26400, "12.00"),  # cheaper, further out of the money
    ):
        offer, bid = str(Decimal(lp) + Decimal("0.05")), str(Decimal(lp) - Decimal("0.05"))
        add(
            "NFO", token, f"NIFTYC{strike}", lp, offer, bid,
            instrument="OPTIDX", strike=strike, option_type="CE", **nifty,
        )  # fmt: skip
    # The BFO script-master shape (2026-11-12): SENSEX future Symbol BSXFUT, options BSXOPT.
    bfo = {"lot_size": SENSEX_LOT, "expiry": EXPIRY}
    add(
        "BFO",
        "700",
        "SENSEXFUT",
        "85010.00",
        "0",
        "0",
        instrument="FUTIDX",
        underlying="BSXFUT",
        **bfo,
    )
    for token, strike, lp in (("801", 85100, "150.00"), ("802", 85200, "80.00")):
        offer, bid = str(Decimal(lp) + Decimal("0.05")), str(Decimal(lp) - Decimal("0.05"))
        add(
            "BFO", token, f"SENSEX{strike}CE", lp, offer, bid,
            instrument="OPTIDX", strike=strike, option_type="CE", underlying="BSXOPT", **bfo,
        )  # fmt: skip
    # SENSEX 50 (SX50FUT / SX50OPT): an earlier expiry and a cheap strike nearer the money,
    # picked only if the SENSEX lookup were too loose.
    sx50 = {"lot_size": SENSEX_LOT, "expiry": EXPIRY - timedelta(days=7)}
    add(
        "BFO",
        "900",
        "SENSEX50FUT",
        "85010.00",
        "0",
        "0",
        instrument="FUTIDX",
        underlying="SX50FUT",
        **sx50,
    )
    add(
        "BFO", "901", "SENSEX5085050CE", "10.00", "10.05", "9.95",
        instrument="OPTIDX", strike=85050, option_type="CE", underlying="SX50OPT", **sx50,
    )  # fmt: skip


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Rig]:
    monkeypatch.setattr(safety, "market_closed_reason", lambda segment, now: None)
    monkeypatch.setattr(funded, "now_ist", lambda: BEFORE_CUT_OFF)  # stage-2 BUYs allowed
    monkeypatch.setattr(uat, "BOOK_WAIT_SECONDS", 3.0)
    monkeypatch.setattr(fl, "STREAM_WAIT_SECONDS", 5.0)
    monkeypatch.setattr(lifecycle, "NET_WAIT_SECONDS", 1.0)
    monkeypatch.setenv(safety.FILLS_ENV, "1")
    fake = FakeClock()
    real_run = lifecycle.run_tracked

    def run_on_fake_time(*args: Any, **kwargs: Any) -> Any:
        kwargs.update(sleep=fake.sleep, clock=fake.clock)
        return real_run(*args, **kwargs)

    monkeypatch.setattr(lifecycle, "run_tracked", run_on_fake_time)
    with OfflineBroker("FUND01") as broker:
        market = UatMarket(broker)
        _instruments(broker, market)
        broker.set_funds(cash="9000.00")
        client = broker.client()
        monkeypatch.setattr(client, "place_order", market.wrap(client.place_order))
        session = OrderSession(
            client,
            budget=safety.OrderBudget(safety.FUNDED_MAX_ORDERS_PER_RUN),
            recorder=Recorder(tmp_path / "live_orders.jsonl"),
            funded=True,
            max_balance=safety.FUNDED_DEFAULT_MAX_BALANCE,
            money=funded.MoneyBudget(),
        )
        try:
            yield Rig(broker, session, market)
        finally:
            client.close()


def _requests(rig: Rig, endpoint: str) -> int:
    return sum(1 for r in rig.broker.requests if r.endpoint == endpoint)


def _order_endpoints(rig: Rig) -> int:
    return sum(_requests(rig, e) for e in ("PlaceOrder", "ModifyOrder", "CancelOrder"))


SYMBOL = {
    "nse": ("ITC-EQ", 1),
    "bse": ("ITC", 1),
    "nfoopt": ("NIFTYC26300", NIFTY_LOT),
    "bfoopt": ("SENSEX85200CE", SENSEX_LOT),
}


# -- the funded tests are what the plan says ------------------------------------------------


def test_the_funded_tests_are_live_order_tests_in_stage_order() -> None:
    names = [n for n in vars(fl) if n.startswith("test_")]
    assert names == [
        "test_stage1_far_limit_open_modified_cancelled",
        "test_stage1_ioc_far_never_rests",
        "test_stage1_stop_limit_not_hit",
        "test_stage2_tiny_fill_closed",
    ]
    assert fl.pytestmark.name == "live_orders"
    assert fl.SEGMENT_IDS == ["nse", "bse", "nfoopt", "bfoopt"]
    assert fl.CASH_IDS == ["nse", "bse"]
    assert set(fl.SEGMENT_IDS) == set(funded.FUNDED_SEGMENTS)


# -- stage 1: far from the market ------------------------------------------------------------


@pytest.mark.parametrize("segment_id", fl.SEGMENT_IDS)
def test_far_limit_rests_is_modified_cancelled_and_the_views_agree(
    rig: Rig, segment_id: str
) -> None:
    record = rig.run(fl.test_stage1_far_limit_open_modified_cancelled, segment_id)
    assert rig.statuses() == [("B", "LMT", "CANCELED")]
    order = rig.broker.orders[0]
    symbol, quantity = SYMBOL[segment_id]
    assert (order.symbol, order.quantity, order.product) == (symbol, quantity, MIS)
    ratio = funded.STAGE1_RATIO["option" if segment_id in funded.OPTION_IDS else "cash"]
    sent = Decimal(record["plan"]["price"])
    assert sent <= rig.market.last[symbol] * ratio
    tick = Decimal(record["plan"]["tick_size"])
    assert Decimal(record["modify"]["new_price"]) == sent - tick
    assert record["modify"]["outcome"] == "acknowledged"
    assert record["modify"]["price_in_book"]["matches"] is True
    assert record["views_agree"] == "yes"
    assert "OPEN" in record["stream_statuses"] and record["stream_statuses"][-1] == "CANCELED"
    assert rig.session.money is not None and rig.session.money.committed == 0  # released
    assert rig.session.test_budget is None and rig.session.fills == {}


def test_far_option_price_is_clamped_up_to_the_lower_circuit(rig: Rig) -> None:
    rig.broker.set_quote(*NIFTY_PICK, lc="20.00")  # -50 % (12.50) is below the circuit
    record = rig.run(fl.test_stage1_far_limit_open_modified_cancelled, "nfoopt")
    assert Decimal(record["plan"]["price"]) == Decimal("20.00")
    assert Decimal(record["modify"]["new_price"]) == Decimal("20.05")  # one tick up, not below lc
    assert rig.statuses() == [("B", "LMT", "CANCELED")]


@pytest.mark.parametrize(
    ("last", "tick", "lc", "ratio", "expected"),
    [
        ("400.00", "0.05", None, "0.9", ("360.00", "359.95")),
        ("400.00", "0.05", "0", "0.9", ("360.00", "359.95")),  # lc 0: no circuit
        ("25.00", "0.05", "20.00", "0.5", ("20.00", "20.05")),  # clamped; the modify goes up
        ("25.00", "0.05", "19.98", "0.5", ("20.00", "20.05")),  # lc rounded up to the tick
        ("25.00", "0.05", "10.00", "0.5", ("12.50", "12.45")),
        ("25.00", "0.05", "24.00", "0.5", ("24.00", "24.05")),  # up, still 3 % below (24.25)
    ],
)
def test_stage1_prices(
    last: str, tick: str, lc: str | None, ratio: str, expected: tuple[str, str]
) -> None:
    prices = fl.stage1_prices(
        Decimal(last), Decimal(tick), None if lc is None else Decimal(lc), Decimal(ratio)
    )
    assert prices == (Decimal(expected[0]), Decimal(expected[1]))


@pytest.mark.parametrize(
    ("last", "tick", "lc", "match"),
    [
        ("25.00", "0.05", "24.98", "not at least 3 % below"),  # clamp gives 25.00
        ("25.00", "0.05", "24.50", "not at least 3 % below"),  # 24.50 > 24.25 (25 x 0.97)
        ("25.00", "0.05", "24.26", "not at least 3 % below"),  # clamp gives 24.30
        ("25.00", "0.05", "24.25", "no second price"),  # 24.25 fits; 24.20 < lc, 24.30 > 24.25
        ("0.10", "0.05", "0.05", "no second price"),  # 0.05: no lower tick, 0.10 is the last
        ("0", "0.05", None, "no last price"),
    ],
)
def test_stage1_prices_refuse_a_price_near_the_market(
    last: str, tick: str, lc: str | None, match: str
) -> None:
    with pytest.raises(Refusal, match=match):
        fl.stage1_prices(
            Decimal(last), Decimal(tick), None if lc is None else Decimal(lc), Decimal("0.5")
        )


def test_a_lower_circuit_near_the_market_skips_without_an_order(rig: Rig) -> None:
    rig.broker.set_quote(*NIFTY_PICK, lc="24.50")  # the clamp would sit 2 % below 25.00
    with pytest.raises(pytest.skip.Exception, match="not at least 3 % below"):
        rig.run(fl.test_stage1_far_limit_open_modified_cancelled, "nfoopt", may_skip=True)
    assert _order_endpoints(rig) == 0 and rig.session.budget.used == 0


@pytest.mark.parametrize("segment_id", fl.CASH_IDS)
def test_ioc_far_below_is_cancelled_by_the_exchange(rig: Rig, segment_id: str) -> None:
    rig.run(fl.test_stage1_ioc_far_never_rests, segment_id)
    assert rig.statuses() == [("B", "LMT", "CANCELED")]
    order = rig.broker.orders[0]
    assert order.payload["ret"] == "IOC" and order.product == MIS and order.quantity == 1
    assert order.price is not None and order.price <= rig.market.last[order.symbol] * 9 / 10
    assert _requests(rig, "CancelOrder") == 0


@pytest.mark.parametrize("segment_id", fl.CASH_IDS)
def test_stop_limit_is_trigger_pending_modified_and_cancelled(rig: Rig, segment_id: str) -> None:
    record = rig.run(fl.test_stage1_stop_limit_not_hit, segment_id)
    assert rig.statuses() == [("B", "SL-LMT", "CANCELED")]
    order = rig.broker.orders[0]
    last = rig.market.last[order.symbol]
    assert order.trigger_price is not None and order.price is not None
    assert last * Decimal("1.03") <= order.trigger_price <= order.price
    assert order.price >= last * Decimal("1.04") and order.product == MIS
    assert record["status_while_open"] == "TRIGGER_PENDING"
    assert record["modify"]["outcome"] == "acknowledged"
    assert record["modify"]["in_book"]["matches"] is True


def test_a_rejected_far_order_fails_stage_one(rig: Rig) -> None:
    rig.broker.default_place = "reject"
    with pytest.raises(AssertionError, match="never open"):
        rig.run(fl.test_stage1_far_limit_open_modified_cancelled, "nse")
    assert _requests(rig, "PlaceOrder") == 1 and _requests(rig, "CancelOrder") == 0
    assert rig.stage1_failures == {"test_stage1_far_limit_open_modified_cancelled[nse]"}


# -- stage 1: an unexpected fill is closed, then the test fails ------------------------------


@pytest.mark.parametrize(
    ("case", "test"),
    [
        ("far", fl.test_stage1_far_limit_open_modified_cancelled),
        ("ioc", fl.test_stage1_ioc_far_never_rests),
        ("stop", fl.test_stage1_stop_limit_not_hit),
    ],
    ids=["far-limit", "ioc", "sl-lmt"],
)
def test_a_stage1_fill_is_closed_at_the_best_bid_then_fails(
    rig: Rig, case: str, test: Callable[..., None]
) -> None:
    if case == "far":
        rig.market.fill_far = True
    elif case == "ioc":
        rig.market.ioc_fills = True
    else:
        rig.market.trigger_stops = True
    with pytest.raises(AssertionError, match="filled unexpectedly") as caught:
        rig.run(test, "nse")
    assert not isinstance(caught.value, CheckTheAccount)
    buy, sell = rig.broker.orders
    assert (buy.side, buy.status, sell.side, sell.status) == ("B", "COMPLETE", "S", "COMPLETE")
    assert sell.order_type == "LMT" and sell.quantity == 1 and sell.product == MIS
    assert sell.price == Decimal("399.95")  # the best bid (use_depth)
    assert rig.net("ITC-EQ") == 0
    assert rig.session.stopped is False
    assert rig.stage1_failures == {f"{test.__name__}[nse]"}
    # the stage-two gate is now closed: a stage-2 test skips before any call
    before = len(rig.broker.requests)
    with pytest.raises(pytest.skip.Exception, match="stage-1 test"):
        rig.run(fl.test_stage2_tiny_fill_closed, "nse", may_skip=True)
    assert len(rig.broker.requests) == before


# -- stage 2: tiny fills, closed at once -----------------------------------------------------


@pytest.mark.parametrize("segment_id", fl.SEGMENT_IDS)
def test_tiny_fill_is_closed_and_the_position_is_back(rig: Rig, segment_id: str) -> None:
    record = rig.run(fl.test_stage2_tiny_fill_closed, segment_id)
    assert rig.statuses() == [("B", "LMT", "COMPLETE"), ("S", "LMT", "COMPLETE")]
    buy, sell = rig.broker.orders
    symbol, quantity = SYMBOL[segment_id]
    assert buy.symbol == sell.symbol == symbol
    assert buy.quantity == sell.quantity == quantity and buy.product == sell.product == MIS
    last = rig.market.last[symbol]
    assert buy.price == last + Decimal("0.05")  # the best offer
    assert sell.price == last - Decimal("0.05")  # the best bid
    assert buy.price is not None and buy.price * quantity <= funded.MAX_ORDER_VALUE
    assert rig.net(symbol) == 0
    assert record["net_after_buy"] == quantity and record["net_after_sell"] == 0
    assert record["trades_listed"] is True and record["close"]["sold_quantity"] == quantity
    assert rig.session.money is not None
    assert rig.session.money.committed == buy.price * quantity  # a filled BUY keeps its value
    assert rig.session.budget.used == 2 and _requests(rig, "CancelOrder") == 0


def test_stage2_without_the_fills_opt_in_is_skipped_before_any_call(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(safety.FILLS_ENV)
    with pytest.raises(pytest.skip.Exception, match=safety.FILLS_ENV):
        rig.run(fl.test_stage2_tiny_fill_closed, "nse", may_skip=True)
    assert rig.broker.requests == []


@pytest.mark.parametrize("hour, minute", [(14, 30), (15, 0)])
def test_stage2_from_14_30_ist_is_skipped_before_any_call(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, hour: int, minute: int
) -> None:
    monkeypatch.setattr(funded, "now_ist", lambda: datetime(2026, 11, 12, hour, minute, tzinfo=IST))
    with pytest.raises(pytest.skip.Exception, match="14:30 IST"):
        rig.run(fl.test_stage2_tiny_fill_closed, "nse", may_skip=True)
    assert rig.broker.requests == []
    assert rig.session.budget.used == 0 and rig.session.stopped is False


def test_stage2_reaching_14_30_ist_before_the_buy_sends_nothing(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cut-off is read again just before the BUY (after the read-only calls)."""
    times = iter([datetime(2026, 11, 12, 14, 29, tzinfo=IST)])
    late = datetime(2026, 11, 12, 14, 30, tzinfo=IST)
    monkeypatch.setattr(funded, "now_ist", lambda: next(times, late))
    with pytest.raises(pytest.skip.Exception, match="14:30 IST"):
        rig.run(fl.test_stage2_tiny_fill_closed, "nse", may_skip=True)
    assert _order_endpoints(rig) == 0 and rig.session.budget.used == 0
    assert rig.broker.requests != []  # the read-only calls ran: it was the second check


def test_a_tiny_order_that_does_not_fill_is_cancelled_and_skipped(rig: Rig) -> None:
    rig.broker.set_quote("NSE", "1660", sp1="399.95")  # the offer is below the broker's fill price
    with pytest.raises(pytest.skip.Exception, match="did not fill"):
        rig.run(fl.test_stage2_tiny_fill_closed, "nse", may_skip=True)
    assert rig.statuses() == [("B", "LMT", "CANCELED")]
    assert rig.session.money is not None and rig.session.money.committed == 0
    assert rig.session.stopped is False


def test_a_tiny_buy_rejected_by_the_broker_fails_the_test(rig: Rig) -> None:
    """A REJECTED stage-2 BUY (an exchange not enabled, an RMS block) is a failure of the live
    run, never an acceptable "did not fill" skip."""
    rig.broker.default_place = "reject"
    with pytest.raises(AssertionError, match="rejected") as caught:
        rig.run(fl.test_stage2_tiny_fill_closed, "nse")
    assert not isinstance(caught.value, CheckTheAccount)
    assert rig.statuses() == [("B", "LMT", "REJECTED")]
    assert _requests(rig, "PlaceOrder") == 1 and _requests(rig, "CancelOrder") == 0
    assert rig.session.money is not None and rig.session.money.committed == 0  # released
    assert rig.session.stopped is False and rig.session.fills == {}


def test_a_tiny_buy_refused_at_place_order_fails_the_test(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tradesmart.errors import APIError

    def reject(**kwargs: Any) -> str:
        raise APIError("RMS: blocked (synthetic)", endpoint="PlaceOrder", http_status=200)

    monkeypatch.setattr(rig.session.client, "place_order", reject)
    with pytest.raises(AssertionError, match="rejected") as caught:
        rig.run(fl.test_stage2_tiny_fill_closed, "nse")
    assert not isinstance(caught.value, CheckTheAccount)
    assert rig.broker.orders == [] and _requests(rig, "CancelOrder") == 0
    assert rig.session.money is not None and rig.session.money.committed == 0
    assert rig.session.stopped is False and rig.session.fills == {}


def test_a_tiny_order_over_the_order_value_is_not_sent(rig: Rig) -> None:
    rig.broker.set_quote(*SENSEX_PICK, sp1="100.05")  # 100.05 x 20 = 2001 > 2000
    with pytest.raises(pytest.skip.Exception, match="above the limit of 2000 per order"):
        rig.run(fl.test_stage2_tiny_fill_closed, "bfoopt", may_skip=True)
    assert _order_endpoints(rig) == 0 and rig.session.budget.used == 0


def test_close_that_never_fills_stops_the_run(rig: Rig) -> None:
    rig.market.fill_sells = False
    with pytest.raises(CheckTheAccount, match="position left open"):
        rig.run(fl.test_stage2_tiny_fill_closed, "nse")
    assert rig.statuses() == [("B", "LMT", "COMPLETE"), ("S", "LMT", "CANCELED")]
    assert _requests(rig, "PlaceOrder") == 2 and _requests(rig, "CancelOrder") == 1
    assert rig.session.stopped is True
    # every later funded test fails at its start, before any API call
    before = len(rig.broker.requests)
    for test, segment_id in (
        (fl.test_stage2_tiny_fill_closed, "bse"),
        (fl.test_stage1_far_limit_open_modified_cancelled, "nse"),
    ):
        with pytest.raises(CheckTheAccount, match="an earlier test"):
            rig.run(test, segment_id)
    assert len(rig.broker.requests) == before
    assert _requests(rig, "PlaceOrder") == 2


def test_an_open_order_in_the_account_stops_the_run(rig: Rig) -> None:
    rig.session.client.place_order(  # someone else's order, resting far below the market
        exchange="NSE",
        tradingsymbol="ITC-EQ",
        quantity=1,
        transaction_type="BUY",
        product="MIS",
        order_type="LIMIT",
        price=Decimal("300.00"),
    )
    with pytest.raises(CheckTheAccount, match="already has open order"):
        rig.run(fl.test_stage1_far_limit_open_modified_cancelled, "bse")
    assert rig.session.stopped is True
    assert _requests(rig, "PlaceOrder") == 1  # only the other order


def test_money_budget_stops_a_fifth_option_buy(rig: Rig) -> None:
    rig.broker.set_quote(*NIFTY_PICK, lp="18.00", sp1="18.05", bp1="17.95")
    rig.market.last["NIFTYC26300"] = Decimal("18.00")
    for _ in range(4):  # 4 x 18.05 x 65 = 4693.00 of the 5000 per run
        rig.run(fl.test_stage2_tiny_fill_closed, "nfoopt")
    assert rig.session.money is not None
    assert rig.session.money.committed == Decimal("4693.00")
    assert _requests(rig, "PlaceOrder") == 8 and rig.session.budget.used == 8
    with pytest.raises(pytest.skip.Exception, match="above the limit of 5000 per run"):
        rig.run(fl.test_stage2_tiny_fill_closed, "nfoopt", may_skip=True)
    assert _requests(rig, "PlaceOrder") == 8  # the fifth BUY was never sent
    assert rig.net("NIFTYC26300") == 0


def _plan(segment_id: str = "nse", **changes: Any) -> OrderPlan:
    base = OrderPlan(
        segment_id=segment_id,
        exchange="NSE",
        tradingsymbol="ITC-EQ",
        token="1660",
        quantity=1,
        price=Decimal("360.00"),
        product="MIS",
        tag=safety.new_tag(segment_id),
        tick_size=Decimal("0.05"),
        last_price=Decimal("400.00"),
    )
    return OrderPlan(**{**base.__dict__, **changes})


@pytest.mark.parametrize("order_type", ["MARKET", "SL-MKT"])
def test_market_never_sent_in_funded_mode(rig: Rig, order_type: str) -> None:
    trigger = Decimal("412.00") if order_type == "SL-MKT" else None
    plan = _plan(order_type=order_type, trigger_price=trigger)
    fl.begin_funded_test(rig.session)  # a funded order needs the test's own budget
    with pytest.raises(Refusal, match="limit orders only"):
        lifecycle.run_tracked(rig.session, plan, {})
    assert _order_endpoints(rig) == 0 and rig.session.budget.used == 0
    assert rig.session.money is not None and rig.session.money.committed == 0


# -- one whole run in file order -------------------------------------------------------------


def test_a_whole_run_in_file_order_stops_at_the_order_cap(rig: Rig) -> None:
    """Stage 1 sends 8 orders; stage 2 then has room for one round trip of the cap of 10."""
    for segment_id in fl.SEGMENT_IDS:
        rig.run(fl.test_stage1_far_limit_open_modified_cancelled, segment_id)
    for segment_id in fl.CASH_IDS:
        rig.run(fl.test_stage1_ioc_far_never_rests, segment_id)
    for segment_id in fl.CASH_IDS:
        rig.run(fl.test_stage1_stop_limit_not_hit, segment_id)
    assert rig.session.budget.used == 8 and rig.stage1_failures == set()
    rig.run(fl.test_stage2_tiny_fill_closed, "nse")
    for segment_id in ("bse", "nfoopt", "bfoopt"):
        with pytest.raises(pytest.skip.Exception, match="order cap reached"):
            rig.run(fl.test_stage2_tiny_fill_closed, segment_id, may_skip=True)
    assert rig.session.budget.used == 10 == _requests(rig, "PlaceOrder")
    assert {o.order_type for o in rig.broker.orders} == {"LMT", "SL-LMT"}
    assert {o.product for o in rig.broker.orders} == {MIS}
    assert [o.status for o in rig.broker.orders].count("COMPLETE") == 2
    assert rig.net("ITC-EQ") == 0 and rig.session.stopped is False
    tags = [p.tag for p in rig.session.placed]
    assert len(set(tags)) == 10
    assert lifecycle.tag_problems(rig.session.placed, rig.session.client.orders()) == []


def test_a_stage_two_only_run_closes_every_fill_within_the_cap(rig: Rig) -> None:
    """The second command (``-k stage2`` with the fills opt-in) on a fresh session: all four
    segments fit the cap of 10 (8 orders) and the run's 5000; every fill is sold back."""
    for segment_id in fl.SEGMENT_IDS:
        rig.run(fl.test_stage2_tiny_fill_closed, segment_id)
    assert rig.session.budget.used == 8 == _requests(rig, "PlaceOrder")
    assert rig.statuses() == [("B", "LMT", "COMPLETE"), ("S", "LMT", "COMPLETE")] * 4
    assert {o.product for o in rig.broker.orders} == {MIS}
    for symbol, _ in SYMBOL.values():
        assert rig.net(symbol) == 0
    assert rig.session.money is not None
    bought = sum((o.price or 0) * o.quantity for o in rig.broker.orders if o.side == "B")
    assert rig.session.money.committed == bought <= funded.MAX_RUN_VALUE
    assert rig.session.fills == {} and rig.session.stopped is False
    tags = [p.tag for p in rig.session.placed]
    assert len(set(tags)) == 8
    assert lifecycle.tag_problems(rig.session.placed, rig.session.client.orders()) == []


# -- the fixture's start and end -------------------------------------------------------------


def test_each_funded_test_starts_with_its_own_cap(rig: Rig) -> None:
    fl.begin_funded_test(rig.session)
    assert rig.session.fills == {}
    assert rig.session.test_budget is not None
    assert rig.session.test_budget.limit == funded.FUNDED_PER_TEST_CAP == 2
    record: dict[str, Any] = {}
    fl.end_funded_test(rig.session, record)
    assert rig.session.test_budget is None
    assert record["orders_sent_by_test"] == 0 and record["money_committed"] == "0"
    assert record["stopped"] is False


def test_a_fill_left_by_an_earlier_test_stops_the_run_at_the_next_start(rig: Rig) -> None:
    rig.session.fills["ITC-EQ"] = 1  # bought and never sold: never forgotten
    with pytest.raises(CheckTheAccount, match=r"position left open: ITC-EQ \(1\)"):
        fl.begin_funded_test(rig.session)
    assert rig.session.stopped is True
    assert rig.session.fills == {"ITC-EQ": 1} and rig.session.test_budget is None
    assert rig.broker.requests == []


def test_a_fill_not_closed_by_the_end_of_the_test_stops_the_run(rig: Rig) -> None:
    fl.begin_funded_test(rig.session)
    rig.session.fills["NIFTYC26300"] = 65
    record: dict[str, Any] = {}
    with pytest.raises(CheckTheAccount, match=r"position left open: NIFTYC26300 \(65\)"):
        fl.end_funded_test(rig.session, record)
    assert rig.session.stopped is True and record["stopped"] is True
    assert record["left_open"] == {"NIFTYC26300": 65}
    assert rig.session.test_budget is None


def test_a_closing_sell_that_fails_before_sending_stops_the_run(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The SELL fails client-side validation (a plain AssertionError, not CHECK THE ACCOUNT):
    the fill stays in ``session.fills``, the test's end stops the run, nothing more is sent."""
    from tradesmart.errors import RequestValidationError

    client = rig.session.client
    place = client.place_order

    def refuse_sells(**kwargs: Any) -> str:
        if kwargs["transaction_type"] == "SELL":
            raise RequestValidationError([{"loc": ["qty"], "msg": "synthetic"}])
        return place(**kwargs)

    monkeypatch.setattr(client, "place_order", refuse_sells)
    with pytest.raises(CheckTheAccount, match=r"position left open: ITC-EQ \(1\)"):
        rig.run(fl.test_stage2_tiny_fill_closed, "nse")
    assert rig.statuses() == [("B", "LMT", "COMPLETE")]
    assert rig.session.stopped is True and rig.net("ITC-EQ") == 1
    before = len(rig.broker.requests)
    with pytest.raises(CheckTheAccount, match="an earlier test"):
        rig.run(fl.test_stage2_tiny_fill_closed, "bse")
    assert len(rig.broker.requests) == before


def test_a_stopped_session_refuses_the_next_test_at_its_start(rig: Rig) -> None:
    rig.session.stopped = True
    with pytest.raises(CheckTheAccount, match="CHECK THE ACCOUNT: an earlier test"):
        fl.begin_funded_test(rig.session)
    assert rig.broker.requests == []


# -- the real fixture chain under pytest (an inner session) ----------------------------------
# The live file's tests and fixtures (``funded_session`` -> ``order_session`` ->
# ``funded_record``) run in an inner pytest session with the conftest report hook; only
# ``order_session`` is replaced, by the rig's OfflineBroker session (the real one logs in).

_SMOKE_SOURCE = """
from tests.live_orders.test_funded_live import (  # noqa: F401
    funded_record,
    funded_session,
    test_stage1_far_limit_open_modified_cancelled,
    test_stage1_ioc_far_never_rests,
    test_stage1_stop_limit_not_hit,
    test_stage2_tiny_fill_closed,
)
"""


def _inner_run(pytester: pytest.Pytester, rig: Rig, *args: str) -> pytest.RunResult:
    plugin = ModuleType("funded_smoke_plugin")  # a module: pytest 8.4 needs a hashable plugin

    @pytest.fixture(scope="session")
    def order_session() -> OrderSession:
        return rig.session

    plugin.order_session = order_session  # type: ignore[attr-defined]
    plugin.pytest_runtest_makereport = live_conftest.pytest_runtest_makereport  # type: ignore[attr-defined]
    pytester.makepyfile(test_funded_smoke=_SMOKE_SOURCE)
    return pytester.runpytest_inprocess(
        "-p", "no:cacheprovider", "-p", "no:xdist", "-p", "no:asyncio", "-rs", *args,
        plugins=[plugin],
    )  # fmt: skip


def _records(rig: Rig) -> list[dict[str, Any]]:
    path = rig.session.recorder.path
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.fixture
def funded_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The funded opt-in for the inner session only (its ``order_session`` is the OfflineBroker
    one; nothing here can reach a real server)."""
    monkeypatch.setenv(safety.FUNDED_ENV, "1")


@pytest.mark.usefixtures("funded_env")
def test_smoke_stage_one_run_sends_8_orders_and_skips_stage_two(
    pytester: pytest.Pytester, rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(safety.FILLS_ENV)  # the first command: stage 1 without FILLS
    result = _inner_run(pytester, rig)
    result.assert_outcomes(passed=8, skipped=4)
    assert _requests(rig, "PlaceOrder") == 8 == rig.session.budget.used
    assert [o.status for o in rig.broker.orders] == ["CANCELED"] * 8
    assert rig.session.stage1_failed is False and rig.session.stopped is False
    records = [r for r in _records(rig) if r["test"].startswith("test_stage2_")]
    assert len(records) == 4
    assert all(safety.FILLS_ENV in r["skipped"] for r in records)
    assert all(r["orders_sent_by_test"] == 0 for r in records)


@pytest.mark.usefixtures("funded_env")
def test_smoke_stage_two_run_fills_and_closes_within_the_cap(
    pytester: pytest.Pytester, rig: Rig
) -> None:
    result = _inner_run(pytester, rig, "-k", "stage2")  # the second command
    result.assert_outcomes(passed=4, deselected=8)
    assert _requests(rig, "PlaceOrder") == 8 == rig.session.budget.used
    assert rig.statuses() == [("B", "LMT", "COMPLETE"), ("S", "LMT", "COMPLETE")] * 4
    for symbol, _ in SYMBOL.values():
        assert rig.net(symbol) == 0
    assert rig.session.fills == {} and rig.session.stopped is False
    assert [r["orders_sent_by_test"] for r in _records(rig)] == [2, 2, 2, 2]


@pytest.mark.usefixtures("funded_env")
def test_smoke_a_stopped_run_sends_nothing_more(pytester: pytest.Pytester, rig: Rig) -> None:
    rig.market.fill_sells = False  # the first close never fills: CHECK THE ACCOUNT
    result = _inner_run(pytester, rig, "-k", "stage2")
    result.assert_outcomes(failed=1, errors=3, deselected=8)
    result.stdout.fnmatch_lines(["*CHECK THE ACCOUNT*position left open*"])
    assert _requests(rig, "PlaceOrder") == 2 and _requests(rig, "CancelOrder") == 1
    assert rig.session.stopped is True
    refused = [r for r in _records(rig) if "refused" in r]
    assert len(refused) == 3 and all("CHECK THE ACCOUNT" in r["refused"] for r in refused)


@pytest.mark.usefixtures("funded_env")
def test_smoke_a_failed_stage_one_closes_the_stage_two_gate(
    pytester: pytest.Pytester, rig: Rig
) -> None:
    """FILLS set on a whole run: every stage-1 order is rejected (each test fails), so the real
    report hook closes the gate and no stage-2 BUY is sent."""
    rig.broker.default_place = "reject"
    result = _inner_run(pytester, rig)
    result.assert_outcomes(failed=8, skipped=4)
    assert _requests(rig, "PlaceOrder") == 8  # one rejected order per stage-1 test, no more
    assert rig.session.stage1_failed is True
    records = [r for r in _records(rig) if r["test"].startswith("test_stage2_")]
    assert len(records) == 4 and all("stage-1 test(s) failed" in r["skipped"] for r in records)
