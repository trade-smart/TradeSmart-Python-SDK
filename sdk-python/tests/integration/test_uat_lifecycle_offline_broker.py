"""A dry run of the UAT lifecycle test bodies against the offline broker (no network).

The live tests in ``tests/live_orders/test_uat_lifecycle.py`` run only against UAT with the
owner. Here their bodies run unchanged with a UAT-mode session built on an ``OfflineBroker`` client
(the REST calls stay in memory and the stream server listens on 127.0.0.1), so a mistake in the
test code shows up before the live run. Market hours are lifted and the order waits run on a fake
clock. The broker plays UAT as observed on 2026-11-11: a LIMIT order at (or through) the last price
fills at once, an IOC order away from the market is cancelled, MARKET and SL-MKT are refused in
the reply with the API order check message, everything else rests.

A test body that skips fails here unless the case expects the skip, so a silent skip (for example
an instrument missing from the offline broker) never passes as a dry run.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tests.live_orders import lifecycle, safety
from tests.live_orders import test_uat_lifecycle as uat
from tests.live_orders.safety import CheckTheAccount, OrderBudget, OrderSession, Recorder
from tradesmart.errors import APIError
from tradesmart.testing import OfflineBroker, drills

ALGO = "Rejected : ALGO_CHK: MKT Order type not allowed for API order"
ROUND_TRIP_CODE = {"nse": "I", "bse": "I", "nfo": "M", "nfoopt": "I", "bfoopt": "I"}  # MIS / NRML
EXPIRY = date.today() + timedelta(days=14)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


@dataclass
class UatMarket:
    """Decides what happens to each order right after the offline broker books it."""

    broker: OfflineBroker
    last: dict[str, Decimal] = field(default_factory=dict)  # trading symbol -> last price
    fill_sells: bool = True
    # MARKET / SL-MKT: "reject" (ALGO_CHK in the reply), "book" (ALGO_CHK as a REJECTED row),
    # "other" (another message in the reply) or "accept"
    market: str = "reject"
    buy_fills: tuple[int, ...] | None = None  # a fill of a BUY in pieces (None: all at once)
    sell_fills: tuple[int, ...] | None = None  # the same for a SELL
    trigger_stops: bool = False  # an SL-LMT order triggers at once (and fills)
    ioc_fills: bool = False  # an IOC order fills instead of being cancelled
    fill_far: bool = False  # a LIMIT BUY below the market fills too

    def _fill(self, order_id: str, last: Decimal, pieces: tuple[int, ...] | None) -> None:
        if pieces is None:
            self.broker.fill(order_id, last)
        for quantity in pieces or ():
            self.broker.fill(order_id, last, quantity)

    def wrap(self, place: Callable[..., str]) -> Callable[..., str]:
        def placed(**kwargs: Any) -> str:
            order_type = kwargs["order_type"]
            if order_type in ("MARKET", "SL-MKT") and self.market in ("reject", "other"):
                message = ALGO if self.market == "reject" else "Rejected : RMS: not today"
                self.broker.fail_next("PlaceOrder", drills.reject(message))
            order_id = place(**kwargs)
            if self.broker.default_place != "open":
                return order_id
            if order_type in ("MARKET", "SL-MKT") and self.market == "book":
                self.broker.reject(order_id, ALGO)
                return order_id
            last = self.last[kwargs["tradingsymbol"]]
            buy = kwargs["transaction_type"] == "BUY"
            pieces = self.buy_fills if buy else self.sell_fills
            if kwargs.get("validity") == "IOC":
                if self.ioc_fills:
                    self._fill(order_id, last, pieces)
                else:
                    self.broker.cancel(order_id)
            elif order_type == "MARKET":
                self._fill(order_id, last, pieces)
            elif order_type == "SL-LMT" and self.trigger_stops:
                self.broker.trigger(order_id)
                self._fill(order_id, last, pieces)
            elif order_type == "LIMIT":
                price = kwargs["price"]
                if (buy and (price >= last or self.fill_far)) or (
                    not buy and price <= last and self.fill_sells
                ):
                    self._fill(order_id, last, pieces)
            return order_id

        return placed


@dataclass
class Rig:
    broker: OfflineBroker
    session: OrderSession
    market: UatMarket

    def run(
        self, test: Callable[..., None], segment_id: str, *, may_skip: bool = False
    ) -> dict[str, Any]:
        """Run one lifecycle test as pytest would (with its own cap); the record it wrote."""
        record: dict[str, Any] = {"test": test.__name__, "uat_mode": True, "lifecycle": True}
        self.session.test_budget = OrderBudget(lifecycle.PER_TEST_CAP)
        try:
            test(segment_id, self.session, record)
        except pytest.skip.Exception as exc:
            if may_skip:
                raise
            raise AssertionError(f"the test body skipped: {exc}") from exc
        finally:
            self.session.test_budget = None
        return record

    def net(self, tradingsymbol: str) -> int:
        positions = self.session.client.positions()
        return sum(p.net_quantity or 0 for p in positions if p.tradingsymbol == tradingsymbol)

    def statuses(self) -> list[tuple[str, str, str]]:
        return [(o.side, o.order_type, o.status) for o in self.broker.orders]


def _instruments(broker: OfflineBroker, market: UatMarket) -> None:
    def add(exchange: str, token: str, tsym: str, lp: str, **kwargs: Any) -> None:
        broker.add_instrument(exchange, token, tsym, **kwargs)
        lot = str(kwargs.get("lot_size", 1))
        tick = str(kwargs.get("tick_size", "0.05"))
        broker.set_quote(exchange, token, lp=lp, lc="0.05", uc="99999", ti=tick, ls=lot)
        market.last[tsym] = Decimal(lp)

    add("NSE", "1660", "ITC-EQ", "400.00", instrument="EQ")
    add("BSE", "500875", "ITC", "400.10", instrument="A")
    nifty = {"lot_size": 65, "expiry": EXPIRY, "underlying": "NIFTY"}
    add("NFO", "500", "NIFTYFUT", "26040.00", instrument="FUTIDX", tick_size="0.10", **nifty)
    for token, strike, lp in (
        ("601", 25900, "180.00"),
        ("602", 26000, "0.00"),  # the at-the-money strike has no trades on UAT
        ("603", 26100, "75.00"),
    ):
        add(
            "NFO", token, f"NIFTYC{strike}", lp,
            instrument="OPTIDX", strike=strike, option_type="CE", **nifty,
        )  # fmt: skip
    # The BFO script-master shape (2026-11-12): SENSEX future Symbol BSXFUT, options BSXOPT.
    bfo = {"lot_size": 20, "expiry": EXPIRY}
    add("BFO", "700", "SENSEXFUT", "85010.00", instrument="FUTIDX", underlying="BSXFUT", **bfo)
    add(
        "BFO", "801", "SENSEX85000CE", "310.00",
        instrument="OPTIDX", strike=85000, option_type="CE", underlying="BSXOPT", **bfo,
    )  # fmt: skip
    # SENSEX 50 (SX50FUT / SX50OPT): an earlier expiry and a strike right at the money,
    # picked only if the SENSEX lookup were too loose.
    sx50 = {"lot_size": 20, "expiry": EXPIRY - timedelta(days=7)}
    add("BFO", "900", "SENSEX50FUT", "85010.00", instrument="FUTIDX", underlying="SX50FUT", **sx50)
    add(
        "BFO", "901", "SENSEX5085010CE", "100.00",
        instrument="OPTIDX", strike=85010, option_type="CE", underlying="SX50OPT", **sx50,
    )  # fmt: skip


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Rig]:
    monkeypatch.setattr(safety, "market_closed_reason", lambda segment, now: None)
    monkeypatch.setattr(uat, "STREAM_WAIT_SECONDS", 5.0)
    monkeypatch.setattr(uat, "BOOK_WAIT_SECONDS", 3.0)
    monkeypatch.setattr(lifecycle, "NET_WAIT_SECONDS", 1.0)
    fake = FakeClock()
    real_run = lifecycle.run_tracked

    def run_on_fake_time(*args: Any, **kwargs: Any) -> Any:
        kwargs.update(sleep=fake.sleep, clock=fake.clock)
        return real_run(*args, **kwargs)

    monkeypatch.setattr(lifecycle, "run_tracked", run_on_fake_time)
    with OfflineBroker("UAT01") as broker:
        market = UatMarket(broker)
        _instruments(broker, market)
        client = broker.client()
        monkeypatch.setattr(client, "place_order", market.wrap(client.place_order))
        session = OrderSession(
            client,
            budget=OrderBudget(safety.UAT_MAX_ORDERS_PER_RUN),
            recorder=Recorder(tmp_path / "live_orders.jsonl"),
            uat=True,
        )
        try:
            yield Rig(broker, session, market)
        finally:
            client.close()


def _requests(rig: Rig, endpoint: str) -> int:
    return sum(1 for r in rig.broker.requests if r.endpoint == endpoint)


# -- LIMIT at the last price: fill, then close ------------------------------------------


@pytest.mark.parametrize(
    ("segment_id", "symbol", "quantity", "product"),
    [
        ("nse", "ITC-EQ", 1, "I"),
        ("bse", "ITC", 1, "I"),
        ("nfo", "NIFTYFUT", 65, "M"),
        ("nfoopt", "NIFTYC26100", 65, "I"),  # 26000 has no last price: the next nearest
        ("bfoopt", "SENSEX85000CE", 20, "I"),
    ],
)
def test_round_trip_fills_and_leaves_the_position_as_it_was(
    rig: Rig, segment_id: str, symbol: str, quantity: int, product: str
) -> None:
    record = rig.run(uat.test_limit_at_last_price_fills_and_is_closed, segment_id)
    assert rig.statuses() == [("B", "LMT", "COMPLETE"), ("S", "LMT", "COMPLETE")]
    buy, sell = rig.broker.orders
    assert buy.symbol == sell.symbol == symbol
    assert buy.quantity == sell.quantity == quantity and buy.product == sell.product == product
    assert rig.net(symbol) == 0
    assert record["net_after_buy"] == quantity and record["net_after_sell"] == 0
    assert record["trades_listed"] is True and record["close"]["sold_quantity"] == quantity
    assert rig.session.budget.used == 2 and _requests(rig, "CancelOrder") == 0


def test_a_close_that_does_not_fill_is_cancelled_and_reported(rig: Rig) -> None:
    rig.market.fill_sells = False
    with pytest.raises(CheckTheAccount, match="position left open"):
        rig.run(uat.test_limit_at_last_price_fills_and_is_closed, "nse")
    assert rig.statuses() == [("B", "LMT", "COMPLETE"), ("S", "LMT", "CANCELED")]
    assert _requests(rig, "PlaceOrder") == 2 and _requests(rig, "CancelOrder") == 1


def test_an_order_at_the_last_price_that_rests_is_cancelled_and_skipped(rig: Rig) -> None:
    rig.market.last["ITC-EQ"] = Decimal("400.05")  # the offline broker's fill rule now misses it
    with pytest.raises(pytest.skip.Exception, match="did not fill"):
        rig.run(uat.test_limit_at_last_price_fills_and_is_closed, "nse", may_skip=True)
    assert rig.statuses() == [("B", "LMT", "CANCELED")]


def test_a_rejected_order_at_the_last_price_is_skipped_without_a_cancel(rig: Rig) -> None:
    rig.broker.default_place = "reject"
    with pytest.raises(pytest.skip.Exception, match="did not fill: it ended REJECTED"):
        rig.run(uat.test_limit_at_last_price_fills_and_is_closed, "nse", may_skip=True)
    assert _requests(rig, "PlaceOrder") == 1 and _requests(rig, "CancelOrder") == 0


# -- far from the market --------------------------------------------------------------------


@pytest.mark.parametrize("segment_id", ["nse", "bse", "nfo", "nfoopt", "bfoopt"])
def test_far_order_is_modified_cancelled_and_the_three_views_agree(
    rig: Rig, segment_id: str
) -> None:
    record = rig.run(uat.test_open_order_is_modified_cancelled_and_all_views_agree, segment_id)
    assert rig.statuses() == [("B", "LMT", "CANCELED")]
    assert record["views_agree"] == "yes"
    assert record["modify"]["price_in_book"]["matches"] is True
    assert "OPEN" in record["stream_statuses"] and record["stream_statuses"][-1] == "CANCELED"
    product = "C" if segment_id in ("nse", "bse") else "M"
    assert rig.broker.orders[0].product == product
    assert [p.tag for p in rig.session.placed] == [record["plan"]["tag"]]


def test_a_rejected_far_order_skips_the_modify_without_a_cancel(rig: Rig) -> None:
    rig.broker.default_place = "reject"
    with pytest.raises(pytest.skip.Exception, match="no open order to modify"):
        rig.run(uat.test_open_order_is_modified_cancelled_and_all_views_agree, "nse", may_skip=True)
    assert _requests(rig, "PlaceOrder") == 1 and _requests(rig, "CancelOrder") == 0


# -- IOC, stops, MARKET ---------------------------------------------------------------------


@pytest.mark.parametrize("segment_id", uat.BASE_IDS)
def test_ioc_order_is_cancelled_by_the_exchange(rig: Rig, segment_id: str) -> None:
    rig.run(uat.test_ioc_limit_away_from_the_market_never_rests, segment_id)
    assert rig.statuses() == [("B", "LMT", "CANCELED")]
    assert rig.broker.orders[0].payload["ret"] == "IOC"
    assert rig.broker.orders[0].product == ROUND_TRIP_CODE[segment_id]
    assert _requests(rig, "CancelOrder") == 0


@pytest.mark.parametrize("segment_id", uat.TRADED_IDS)
def test_stop_limit_order_is_pending_modified_and_cancelled(rig: Rig, segment_id: str) -> None:
    record = rig.run(uat.test_stop_limit_buy_is_trigger_pending_modified_and_cancelled, segment_id)
    assert rig.statuses() == [("B", "SL-LMT", "CANCELED")]
    order = rig.broker.orders[0]
    assert order.trigger_price is not None and order.price is not None
    trigger_ratio, _ = lifecycle.stop_ratios(segment_id)
    assert rig.market.last[order.symbol] * trigger_ratio <= order.trigger_price <= order.price
    assert order.product == ROUND_TRIP_CODE[segment_id]
    assert record["status_while_open"] == "TRIGGER_PENDING"
    assert record["modify"]["in_book"]["matches"] is True


@pytest.mark.parametrize("segment_id", uat.BASE_IDS)
def test_stop_market_rejection_is_recorded(rig: Rig, segment_id: str) -> None:
    record = rig.run(uat.test_stop_market_buy_is_refused_or_recorded, segment_id)
    assert record["sl_mkt"] == "refused by the API order check"
    assert rig.broker.orders == []


def test_an_accepted_stop_market_order_is_cancelled_and_recorded(rig: Rig) -> None:
    rig.market.market = "accept"
    record = rig.run(uat.test_stop_market_buy_is_refused_or_recorded, "nse")
    assert record["sl_mkt"] == "accepted; ended CANCELED"
    assert rig.statuses() == [("B", "SL-MKT", "CANCELED")]
    assert rig.broker.orders[0].product == "I"


def test_a_stop_market_trigger_above_the_upper_circuit_is_not_sent(rig: Rig) -> None:
    rig.broker.set_quote("NSE", "1660", lp="400.00", lc="0.05", uc="405.00", ti="0.05", ls="1")
    with pytest.raises(pytest.skip.Exception, match="upper circuit 405"):
        rig.run(uat.test_stop_market_buy_is_refused_or_recorded, "nse", may_skip=True)
    assert _requests(rig, "PlaceOrder") == 0


@pytest.mark.parametrize("segment_id", uat.TRADED_IDS)
def test_market_order_rejection_passes(rig: Rig, segment_id: str) -> None:
    record = rig.run(uat.test_market_order_is_refused_by_the_api_order_check, segment_id)
    assert "ALGO_CHK" in record["refused_with"]
    assert rig.broker.orders == []


def test_market_order_rejected_in_the_order_book_passes(rig: Rig) -> None:
    rig.market.market = "book"
    record = rig.run(uat.test_market_order_is_refused_by_the_api_order_check, "nse")
    assert "ALGO_CHK" in record["refused_with"]
    assert rig.statuses() == [("B", "MKT", "REJECTED")]
    assert _requests(rig, "CancelOrder") == 0


def test_market_order_refused_with_another_message_is_skipped(rig: Rig) -> None:
    rig.market.market = "other"
    with pytest.raises(pytest.skip.Exception, match="another message: Rejected : RMS"):
        rig.run(uat.test_market_order_is_refused_by_the_api_order_check, "nse", may_skip=True)


def test_an_accepted_market_order_is_closed_then_fails(rig: Rig) -> None:
    rig.market.market = "accept"
    with pytest.raises(AssertionError, match="expected the ALGO_CHK rejection"):
        rig.run(uat.test_market_order_is_refused_by_the_api_order_check, "nfo")
    assert rig.statuses() == [("B", "MKT", "COMPLETE"), ("S", "LMT", "COMPLETE")]
    buy, sell = rig.broker.orders
    assert buy.quantity == sell.quantity == 65 and buy.product == sell.product == "M"
    assert rig.net("NIFTYFUT") == 0


# -- unexpected and partial fills: every fill is closed with what was bought -----------------


def test_a_partial_buy_fill_then_cancel_closes_only_the_filled_part(rig: Rig) -> None:
    rig.market.buy_fills = (30,)  # 30 of 65, then the order rests and the test cancels it
    with pytest.raises(AssertionError, match="ended CANCELED"):
        rig.run(uat.test_limit_at_last_price_fills_and_is_closed, "nfo")
    buy, sell = rig.broker.orders
    assert (buy.side, buy.status, buy.quantity, buy.filled_quantity) == ("B", "CANCELED", 65, 30)
    assert (sell.side, sell.status, sell.quantity) == ("S", "COMPLETE", 30)
    assert rig.net("NIFTYFUT") == 0
    assert _requests(rig, "CancelOrder") == 1


def test_a_fill_split_over_two_trades_is_closed_with_the_order_quantity(rig: Rig) -> None:
    rig.market.buy_fills = (30, 35)  # two trade rows: fillshares 30, then 65
    record = rig.run(uat.test_limit_at_last_price_fills_and_is_closed, "nfo")
    buy, sell = rig.broker.orders
    assert (buy.status, buy.filled_quantity) == ("COMPLETE", 65)
    assert (sell.status, sell.quantity) == ("COMPLETE", 65)  # not 95 (30 + 65)
    assert record["bought"] == 65 and record["close"]["sold_quantity"] == 65
    assert record["net_change_after_buy"] == 65 and record["net_change_after_sell"] == -65
    assert rig.net("NIFTYFUT") == 0


def test_a_partially_filled_close_is_reported(rig: Rig) -> None:
    rig.market.sell_fills = (30,)  # the closing SELL fills 30 of 65, then rests
    with pytest.raises(CheckTheAccount, match=r"position left open \(30 of 65\)"):
        rig.run(uat.test_limit_at_last_price_fills_and_is_closed, "nfo")
    buy, sell = rig.broker.orders
    assert (buy.status, sell.status, sell.filled_quantity) == ("COMPLETE", "CANCELED", 30)
    assert _requests(rig, "PlaceOrder") == 2 and _requests(rig, "CancelOrder") == 1


@pytest.mark.parametrize("segment_id", ["nse", "nfoopt"])
def test_a_stop_limit_that_triggers_and_fills_is_closed(rig: Rig, segment_id: str) -> None:
    rig.market.trigger_stops = True
    with pytest.raises(pytest.skip.Exception, match="triggered and filled"):
        rig.run(
            uat.test_stop_limit_buy_is_trigger_pending_modified_and_cancelled,
            segment_id,
            may_skip=True,
        )
    assert rig.statuses() == [("B", "SL-LMT", "COMPLETE"), ("S", "LMT", "COMPLETE")]
    buy, sell = rig.broker.orders
    assert buy.quantity == sell.quantity and buy.product == sell.product
    assert sell.product == ROUND_TRIP_CODE[segment_id]
    assert rig.net(buy.symbol) == 0


def test_an_ioc_that_fills_is_closed_then_fails(rig: Rig) -> None:
    rig.market.ioc_fills = True
    with pytest.raises(AssertionError, match="IOC order .* filled"):
        rig.run(uat.test_ioc_limit_away_from_the_market_never_rests, "bse")
    assert rig.statuses() == [("B", "LMT", "COMPLETE"), ("S", "LMT", "COMPLETE")]
    assert [o.product for o in rig.broker.orders] == ["I", "I"]
    assert rig.net("ITC") == 0


# -- skips, tags and the recorder -----------------------------------------------------------


def test_mcx_is_skipped_before_any_order(rig: Rig) -> None:
    with pytest.raises(pytest.skip.Exception, match="MCX"):
        rig.run(uat.test_limit_at_last_price_fills_and_is_closed, "mcx", may_skip=True)
    assert rig.broker.requests == []


@pytest.mark.parametrize(
    "test",
    [
        uat.test_open_order_is_modified_cancelled_and_all_views_agree,
        uat.test_limit_at_last_price_fills_and_is_closed,
        uat.test_stop_limit_buy_is_trigger_pending_modified_and_cancelled,
        uat.test_market_order_is_refused_by_the_api_order_check,
    ],
)
def test_a_missing_bfo_script_master_skips_before_any_order(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, test: Callable[..., None]
) -> None:
    client = rig.session.client
    real = client.instruments

    def instruments(exchange: str) -> list[dict[str, str]]:
        if exchange == "BFO":
            raise APIError("not found", endpoint="BFO_symbols.txt.zip", http_status=404)
        return real(exchange)

    monkeypatch.setattr(client, "instruments", instruments)
    with pytest.raises(pytest.skip.Exception, match="BFO script master is unavailable"):
        rig.run(test, "bfoopt", may_skip=True)
    assert _requests(rig, "PlaceOrder") == 0 and rig.session.budget.used == 0


def test_a_bfo_script_master_without_sensex_skips_before_any_order(rig: Rig) -> None:
    rows = [r for r in rig.session.client.instruments("BFO") if r.get("Instrument") != "FUTIDX"]
    assert rows  # the options stay; only the future (the strike's reference price) is gone
    rig.session.client.instruments = lambda exchange: rows  # type: ignore[method-assign]
    with pytest.raises(pytest.skip.Exception, match="no SENSEX index future"):
        rig.run(uat.test_limit_at_last_price_fills_and_is_closed, "bfoopt", may_skip=True)
    assert _requests(rig, "PlaceOrder") == 0


def test_tags_of_the_run_are_unique_and_in_the_book(rig: Rig) -> None:
    with pytest.raises(pytest.skip.Exception, match="no lifecycle order"):
        rig.run(uat.test_every_order_tag_is_unique_and_in_the_order_book, "nse", may_skip=True)
    rig.run(uat.test_limit_at_last_price_fills_and_is_closed, "nse")
    rig.run(uat.test_open_order_is_modified_cancelled_and_all_views_agree, "nse")
    record = rig.run(uat.test_every_order_tag_is_unique_and_in_the_order_book, "nse")
    assert record["tag_problems"] == []
    assert len(set(record["tags"])) == 3 == _requests(rig, "PlaceOrder")


def test_the_recorder_line_is_tagged_with_the_environment(
    rig: Rig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TRADESMART_ENV", "uat")
    record = rig.run(uat.test_limit_at_last_price_fills_and_is_closed, "nse")
    rig.session.recorder.write(record)
    line = json.loads((tmp_path / "live_orders.jsonl").read_text(encoding="utf-8"))
    assert line["environment"] == "uat" and line["lifecycle"] is True
    assert line["close"]["sold_quantity"] == 1


# -- an under-close is caught by positions() in every test that may fill ---------------------


def _set_up_fill(rig: Rig, case: str) -> None:
    if case == "far":
        rig.market.fill_far = True
    elif case == "ioc":
        rig.market.ioc_fills = True
    elif case == "stop":
        rig.market.trigger_stops = True
    elif case == "market":
        rig.market.market = "accept"


@pytest.mark.parametrize(
    ("case", "test"),
    [
        ("far", uat.test_open_order_is_modified_cancelled_and_all_views_agree),
        ("at-last", uat.test_limit_at_last_price_fills_and_is_closed),
        ("ioc", uat.test_ioc_limit_away_from_the_market_never_rests),
        ("stop", uat.test_stop_limit_buy_is_trigger_pending_modified_and_cancelled),
        ("market", uat.test_market_order_is_refused_by_the_api_order_check),
    ],
    ids=["far-limit", "limit-at-last-price", "ioc", "sl-lmt", "market"],
)
def test_an_under_close_fails_with_check_the_account_in_every_test(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, case: str, test: Callable[..., None]
) -> None:
    """A wrong (too small) fill count closes 35 of 65: positions() shows 30 still open."""
    _set_up_fill(rig, case)
    real = lifecycle.close_quantity

    def stale(*args: Any, **kwargs: Any) -> int:
        return real(*args, **kwargs) - 30

    monkeypatch.setattr(lifecycle, "close_quantity", stale)
    with pytest.raises(CheckTheAccount, match=r"position left open: positions\(\) net quantity"):
        rig.run(test, "nfo")
    buy, sell = rig.broker.orders
    assert (buy.side, buy.filled_quantity, sell.side, sell.quantity) == ("B", 65, "S", 35)
    assert rig.net("NIFTYFUT") == 30


@pytest.mark.parametrize(
    ("case", "test"),
    [
        ("far", uat.test_open_order_is_modified_cancelled_and_all_views_agree),
        ("ioc", uat.test_ioc_limit_away_from_the_market_never_rests),
        ("stop", uat.test_stop_limit_buy_is_trigger_pending_modified_and_cancelled),
        ("market", uat.test_market_order_is_refused_by_the_api_order_check),
    ],
    ids=["far-limit", "ioc", "sl-lmt", "market"],
)
def test_an_unexpected_fill_is_closed_in_full_in_every_test(
    rig: Rig, case: str, test: Callable[..., None]
) -> None:
    _set_up_fill(rig, case)
    with pytest.raises((AssertionError, pytest.skip.Exception)) as caught:
        rig.run(test, "nfo", may_skip=True)
    assert not isinstance(caught.value, CheckTheAccount), str(caught.value)
    buy, sell = rig.broker.orders
    assert (buy.filled_quantity, sell.side, sell.quantity, sell.status) == (65, "S", 65, "COMPLETE")
    assert rig.net("NIFTYFUT") == 0
