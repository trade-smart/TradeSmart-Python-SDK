"""The offline broker's REST adapter, through the REAL sync and async clients (no network)."""

from __future__ import annotations

import inspect
import json
import time
from collections.abc import AsyncIterator, Callable
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.conftest import fixture
from tradesmart import AsyncTradeSmart, ClientConfig, TradeSmart
from tradesmart.auth import AccessToken
from tradesmart.errors import (
    APIError,
    AuthenticationError,
    NetworkError,
    OrderRejectedError,
    OrderStatusUnknownError,
    RateLimitError,
    ResponseParseError,
    ServerError,
)
from tradesmart.greeks import OptionGreeks
from tradesmart.models.base import IST
from tradesmart.models.market import (
    Candle,
    DailyCandle,
    OptionContract,
    Quote,
    ScripMatch,
    SecurityInfo,
)
from tradesmart.models.orders import Order, OrderHistoryEntry, OrderMargin, Trade
from tradesmart.models.portfolio import Holding, Limits, Position
from tradesmart.models.user import UserDetails
from tradesmart.testing import drills
from tradesmart.testing._rest import (
    OFFLINE_AUTHORIZE_URL,
    OFFLINE_BASE_URL,
    OFFLINE_SCRIPT_MASTER_URL,
    AsyncOfflineTransport,
    OfflineTransport,
    offline_config,
)
from tradesmart.testing._state import BrokerState

AnyClient = TradeSmart | AsyncTradeSmart
WS_URL = "ws://127.0.0.1:9/NorenWSAPI/"
EXPIRY = datetime.now(IST).date() + timedelta(days=30)
MON = EXPIRY.strftime("%b").upper()
OPTION = f"NIFTY{EXPIRY:%d}{MON}{EXPIRY:%y}C24000"
FUTURE = f"NIFTY{EXPIRY:%d}{MON}{EXPIRY:%y}F"


class Api:
    """Calls a client method the same way on both clients (awaiting the async one)."""

    def __init__(self, client: AnyClient) -> None:
        self.client = client

    async def __call__(self, method: str, *args: Any, **kwargs: Any) -> Any:
        result = getattr(self.client, method)(*args, **kwargs)
        return await result if inspect.isawaitable(result) else result


@pytest.fixture
def state() -> BrokerState:
    return BrokerState()


@pytest.fixture(params=["sync", "async"])
async def connect(
    request: pytest.FixtureRequest, state: BrokerState
) -> AsyncIterator[Callable[..., Api]]:
    """Factory: a real client of the parametrised kind wired to the broker (config overrides)."""
    opened: list[Any] = []

    def _connect(**overrides: Any) -> Api:
        kwargs: dict[str, Any] = {
            "access_token": "offline-token-test",
            "app_key": "offline-app-key",
            "secret_key": "offline-secret-key",
            "config": offline_config(WS_URL, **overrides),
        }
        client: AnyClient
        if request.param == "sync":
            http: Any = httpx.Client(transport=OfflineTransport(state))
            client = TradeSmart("OFFLINE01", http_client=http, **kwargs)
        else:
            http = httpx.AsyncClient(transport=AsyncOfflineTransport(state))
            client = AsyncTradeSmart("OFFLINE01", http_client=http, **kwargs)
        opened.append(http)
        return Api(client)

    yield _connect
    for http in opened:
        closed = http.close() if isinstance(http, httpx.Client) else http.aclose()
        if inspect.isawaitable(closed):
            await closed


@pytest.fixture
def api(connect: Callable[..., Api]) -> Api:
    return connect()


@pytest.fixture
def strict_api(connect: Callable[..., Api]) -> Api:
    """A client that does not retry reads, so a read drill reaches the caller."""
    return connect(max_retries=0)


def add_nifty(state: BrokerState) -> None:
    """NIFTY future and options at 23900/24000/24100 (CE and PE), plus SBIN equity."""
    state.add_instrument(
        "NFO",
        "1001",
        FUTURE,
        lot_size=75,
        instrument="FUTIDX",
        expiry=EXPIRY,
        underlying="NIFTY",
    )
    token = 2000
    for strike in (23900, 24000, 24100):
        for kind in ("CE", "PE"):
            token += 1
            symbol = f"NIFTY{EXPIRY:%d}{MON}{EXPIRY:%y}{kind[0]}{strike}"
            state.add_instrument(
                "NFO",
                str(token),
                symbol,
                lot_size=75,
                instrument="OPTIDX",
                expiry=EXPIRY,
                strike=strike,
                option_type=kind,
                underlying="NIFTY",
            )
    state.add_instrument("NSE", "3045", "SBIN-EQ")


def place_kwargs(**overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "exchange": "NFO",
        "tradingsymbol": OPTION,
        "quantity": 75,
        "transaction_type": "B",
        "product": "M",
        "order_type": "LMT",
        "price": "101.50",
    }
    kwargs.update(overrides)
    return kwargs


def endpoints(state: BrokerState) -> list[str]:
    return [request.endpoint for request in state.requests]


# --- offline_config -----------------------------------------------------------------------------


def test_offline_broker_config_points_every_url_at_the_offline_broker() -> None:
    config = offline_config(WS_URL, read_timeout=3.0)
    assert config.base_url == OFFLINE_BASE_URL
    assert config.script_master_base_url == OFFLINE_SCRIPT_MASTER_URL
    assert config.authorize_url == OFFLINE_AUTHORIZE_URL
    assert config.ws_url == WS_URL
    assert config.read_timeout == 3.0
    for url in (OFFLINE_BASE_URL, OFFLINE_SCRIPT_MASTER_URL, OFFLINE_AUTHORIZE_URL):
        assert httpx.URL(url).host == "offline.tradesmart.invalid"


def test_offline_broker_config_keeps_the_retry_count_but_never_sleeps() -> None:
    config = offline_config(WS_URL)
    assert config.max_retries == ClientConfig().max_retries
    assert config.backoff_base == 0.001 and config.backoff_cap == 0.001
    # By design: streams reconnect within milliseconds and detect silence in about 1 s.
    assert config.reconnect_backoff_base == 0.001 and config.reconnect_backoff_cap == 0.001
    assert config.heartbeat_interval == 0.5
    assert config.reconnect_max_attempts == ClientConfig().reconnect_max_attempts
    assert offline_config(WS_URL, backoff_cap=2.0).backoff_cap == 2.0


# --- login -------------------------------------------------------------------------------------


async def test_login_url_points_at_the_offline_broker(api: Api) -> None:
    url = await api("login_url")
    assert url.startswith(OFFLINE_AUTHORIZE_URL + "?client_id=")


async def test_generate_session_returns_a_synthetic_token(api: Api, state: BrokerState) -> None:
    token = await api("generate_session", "offline-auth-code")
    assert isinstance(token, AccessToken)
    assert token.access_token.get_secret_value().startswith("offline-token-")
    assert token.user_id == "OFFLINE01"
    recorded = state.requests[-1]
    assert recorded.endpoint == "GenAcsTok"
    assert "offline-auth-code" not in json.dumps(recorded.payload)
    assert recorded.payload["checksum"] != ""


# --- account ---------------------------------------------------------------------------------


async def test_profile_returns_user_details(api: Api, state: BrokerState) -> None:
    state.set_user(uname="Asha")
    profile = await api("profile")
    assert isinstance(profile, UserDetails)
    assert profile.user_id == "OFFLINE01"
    assert profile.uname == "Asha"


async def test_margins_returns_limits(api: Api, state: BrokerState) -> None:
    state.set_funds(cash="100000.00")
    limits = await api("margins")
    assert isinstance(limits, Limits)
    assert limits.cash == Decimal("100000.00")


async def test_holdings_returns_the_records_set(api: Api, state: BrokerState) -> None:
    state.set_holdings(
        [{"exch_tsym": [{"exch": "NSE", "tsym": "SBIN-EQ", "token": "3045"}], "holdqty": "10"}]
    )
    rows = await api("holdings")
    assert len(rows) == 1 and isinstance(rows[0], Holding)
    assert rows[0].holdqty == 10


async def test_positions_returns_the_rows_set(api: Api, state: BrokerState) -> None:
    state.set_positions(
        [{"exch": "NSE", "tsym": "SBIN-EQ", "prd": "C", "netqty": "10", "netavgprc": "800"}]
    )
    rows = await api("positions")
    assert len(rows) == 1 and isinstance(rows[0], Position)
    assert rows[0].net_quantity == 10


async def test_empty_books_are_empty_lists(api: Api) -> None:
    assert await api("orders") == []
    assert await api("trades") == []
    assert await api("positions") == []
    assert await api("holdings") == []
    assert await api("order_history", "999") == []


# --- orders ----------------------------------------------------------------------------------


async def test_place_order_is_booked_and_read_back(api: Api, state: BrokerState) -> None:
    add_nifty(state)
    order_id = await api("place_order", **place_kwargs())
    assert order_id == state.orders[-1].id
    assert state.orders[-1].status == "OPEN"

    book = await api("orders")
    assert [type(row) for row in book] == [Order]
    assert book[0].order_id == order_id and book[0].status == "OPEN"
    history = await api("order_history", order_id)
    assert all(isinstance(row, OrderHistoryEntry) for row in history)
    assert [row.status for row in history] == ["OPEN", "PENDING"]

    state.fill(order_id, price="101.50")
    trades = await api("trades")
    assert len(trades) == 1 and isinstance(trades[0], Trade)
    assert trades[0].flqty == 75 and trades[0].flprc == Decimal("101.50")
    positions = await api("positions")
    assert positions[0].net_quantity == 75


async def test_modify_order_changes_the_book(api: Api, state: BrokerState) -> None:
    order_id = await api("place_order", **place_kwargs())
    result = await api(
        "modify_order",
        order_id,
        exchange="NFO",
        tradingsymbol=OPTION,
        quantity=150,
        price="102.00",
        order_type="LMT",
        product="M",
    )
    assert result == order_id
    order = state.orders[-1]
    assert order.price == Decimal("102.00") and order.quantity == 150


async def test_cancel_order_cancels_and_a_second_cancel_is_rejected(
    api: Api, state: BrokerState
) -> None:
    order_id = await api("place_order", **place_kwargs())
    assert await api("cancel_order", order_id) == order_id
    assert state.orders[-1].status == "CANCELED"
    with pytest.raises(OrderRejectedError) as caught:
        await api("cancel_order", order_id)
    assert caught.value.emsg == "order is not open to cancel"


async def test_modify_of_a_closed_order_is_rejected(api: Api, state: BrokerState) -> None:
    order_id = await api("place_order", **place_kwargs())
    state.cancel(order_id)
    with pytest.raises(OrderRejectedError, match="not open to modify"):
        await api(
            "modify_order",
            order_id,
            exchange="NFO",
            tradingsymbol=OPTION,
            quantity=75,
            price="99.00",
            order_type="LMT",
            product="M",
        )


async def test_exit_order_cancels_a_cover_order(api: Api, state: BrokerState) -> None:
    order_id = await api("place_order", **place_kwargs(product="H", book_loss_price="95.00"))
    assert await api("exit_order", order_id, "H") == order_id
    assert state.orders[-1].status == "CANCELED"
    with pytest.raises(OrderRejectedError, match="order is not open to exit"):
        await api("exit_order", order_id, "H")


async def test_exit_order_of_an_order_that_is_not_cover_or_bracket_is_rejected(
    api: Api, state: BrokerState
) -> None:
    # Documented: ExitSNOOrder is for cover (H) and bracket (B) orders only.
    order_id = await api("place_order", **place_kwargs(product="I"))
    with pytest.raises(OrderRejectedError, match="not a cover or bracket order"):
        await api("exit_order", order_id, "H")
    assert state.orders[-1].status == "OPEN"
    with pytest.raises(OrderRejectedError, match="Order not found to Exit"):
        await api("exit_order", "nosuchorder", "B")


def test_exit_sno_order_reply_has_the_documented_dmsg_shape(state: BrokerState) -> None:
    order = state.place({**MULTI_PAYLOAD, "prd": "B", "blprc": "5", "bpprc": "5"})
    body = "jData=" + json.dumps({"uid": "OFFLINE01", "prd": "B", "norenordno": order.id})
    with httpx.Client(transport=OfflineTransport(state)) as http:
        reply = http.post(f"{OFFLINE_BASE_URL}/ExitSNOOrder", content=body.encode()).json()
    assert (reply["stat"], reply["dmsg"]) == ("Ok", "success")
    assert "request_time" in reply and "result" not in reply


MULTI_PAYLOAD = {
    "exch": "NFO",
    "tsym": OPTION,
    "qty": "75",
    "prc": "101.50",
    "prd": "M",
    "trantype": "B",
    "prctyp": "LMT",
    "ret": "DAY",
}


async def test_place_multi_order_is_not_modelled(api: Api, state: BrokerState) -> None:
    # Its request and reply are undocumented (F1 / F12): the offline broker does not guess them.
    with pytest.raises(OrderRejectedError) as caught:
        await api("request", "PlaceMultiOrder", MULTI_PAYLOAD, state_changing=True)
    assert caught.value.emsg == (
        "OfflineBroker does not model PlaceMultiOrder; set a reply with broker.set_reply(...)"
    )
    assert state.orders == []


async def test_place_multi_order_answers_a_set_reply(api: Api, state: BrokerState) -> None:
    state.set_reply("PlaceMultiOrder", {"stat": "Ok", "norenordno": "1"})
    reply = await api("request", "PlaceMultiOrder", MULTI_PAYLOAD, state_changing=True)
    assert reply == {"stat": "Ok", "norenordno": "1"}
    assert state.orders == []


# --- market data -----------------------------------------------------------------------------


async def test_quote_returns_the_quote_set(api: Api, state: BrokerState) -> None:
    add_nifty(state)
    state.set_quote("NFO", "2003", last_price="101.50")
    quote = await api("quote", "NFO", "2003")
    assert isinstance(quote, Quote)
    assert quote.last_price == Decimal("101.50")
    assert quote.tradingsymbol == OPTION


async def test_search_instruments_matches_a_symbol_substring(api: Api, state: BrokerState) -> None:
    add_nifty(state)
    matches = await api("search_instruments", "sbin")
    assert [type(m) for m in matches] == [ScripMatch]
    assert matches[0].tradingsymbol == "SBIN-EQ"
    assert await api("search_instruments", "NOSUCH") == []


async def test_security_info_returns_the_instrument(api: Api, state: BrokerState) -> None:
    add_nifty(state)
    info = await api("security_info", "NFO", "2003")
    assert isinstance(info, SecurityInfo)
    assert info.tradingsymbol == OPTION


async def test_security_info_for_unknown_instrument_is_an_api_error(api: Api) -> None:
    with pytest.raises(APIError) as caught:
        await api("security_info", "NSE", "424242")
    assert caught.value.http_status == 400


async def test_option_chain_lists_strikes_around_the_mid_strike(
    api: Api, state: BrokerState
) -> None:
    add_nifty(state)
    contracts = await api("option_chain", "NFO", OPTION, "24000", 1)
    assert all(isinstance(c, OptionContract) for c in contracts)
    got = sorted((c.strike_price, c.option_type) for c in contracts)
    assert got == [
        (Decimal("23900"), "CE"),
        (Decimal("23900"), "PE"),
        (Decimal("24000"), "CE"),
        (Decimal("24000"), "PE"),
    ]


async def test_historical_data_serves_the_candles_set(api: Api, state: BrokerState) -> None:
    state.set_candles(
        "NSE",
        "3045",
        [
            {
                "time": datetime(2026, 11, 10, 9, 15),
                "open": 800,
                "high": 801,
                "low": 799,
                "close": 800.5,
            }
        ],
    )
    candles = await api("historical_data", "NSE", "3045")
    assert [type(c) for c in candles] == [Candle]
    assert candles[0].intc == Decimal("800.5")
    assert await api("historical_data", "NSE", "999") == []


async def test_daily_history_serves_the_candles_set(api: Api, state: BrokerState) -> None:
    add_nifty(state)
    state.set_candles("NSE", "3045", [{"time": date(2026, 11, 10), "close": "800.50"}])
    candles = await api("daily_history", "NSE", "SBIN-EQ", date(2026, 11, 1), date(2026, 11, 11))
    assert [type(c) for c in candles] == [DailyCandle]
    assert candles[0].intc == Decimal("800.50")
    assert (
        await api("daily_history", "NSE", "NOSUCH-EQ", date(2026, 11, 1), date(2026, 11, 11)) == []
    )


async def test_historical_data_is_newest_first_and_within_the_range(
    api: Api, state: BrokerState
) -> None:
    # Documented: TPSeries answers newest first, for the requested st/et range.
    times = [datetime(2026, 11, 10, 9, minute, tzinfo=IST) for minute in (15, 16, 17)]
    state.set_candles(
        "NSE", "3045", [{"time": t, "close": 100 + i} for i, t in enumerate(times)]
    )  # set oldest first, the natural order
    closes = [c.intc for c in await api("historical_data", "NSE", "3045")]
    assert closes == [Decimal("102"), Decimal("101"), Decimal("100")]
    closes = [c.intc for c in await api("historical_data", "NSE", "3045", times[1], times[2])]
    assert closes == [Decimal("102"), Decimal("101")]
    later = times[2] + timedelta(days=5)
    assert await api("historical_data", "NSE", "3045", later, later + timedelta(hours=1)) == []


def _post(state: BrokerState, endpoint: str, payload: dict[str, Any]) -> Any:
    with httpx.Client(transport=OfflineTransport(state)) as http:
        body = "jData=" + json.dumps(payload)
        return http.post(f"{OFFLINE_BASE_URL}/{endpoint}", content=body.encode()).json()


def test_candles_without_a_usable_time_are_kept_last_and_bad_bounds_are_refused(
    state: BrokerState,
) -> None:
    add_nifty(state)
    state.set_candles(
        "NSE",
        "3045",
        [
            {"time": "no time", "close": "1"},
            {"time": "blank epoch", "ssboe": "", "close": "2"},
            {"time": datetime(2026, 11, 10, 9, 15, tzinfo=IST), "close": "3"},
        ],
    )
    rows = _post(state, "TPSeries", {"exch": "NSE", "token": "3045", "st": "1", "et": "1"})
    assert [row["intc"] for row in rows] == ["1", "2"]  # never filtered, kept in order
    rows = _post(state, "TPSeries", {"exch": "NSE", "token": "3045"})
    assert [row["intc"] for row in rows] == ["3", "1", "2"]
    bad = _post(state, "TPSeries", {"exch": "NSE", "token": "3045", "st": "today"})
    assert bad["stat"] == "Not_Ok" and "st and et" in bad["emsg"]
    bad = _post(state, "EODChartData", {"sym": "NSE:SBIN-EQ", "from": "x", "to": "1"})
    assert bad["stat"] == "Not_Ok" and "from and to" in bad["emsg"]


async def test_daily_history_is_newest_first_and_within_the_range(
    api: Api, state: BrokerState
) -> None:
    add_nifty(state)
    days = [date(2026, 11, day) for day in (3, 4, 5)]
    state.set_candles("NSE", "3045", [{"time": d, "close": 800 + i} for i, d in enumerate(days)])
    candles = await api("daily_history", "NSE", "SBIN-EQ", days[0], days[2])
    assert [c.intc for c in candles] == [Decimal("802"), Decimal("801"), Decimal("800")]
    candles = await api("daily_history", "NSE", "SBIN-EQ", days[1], days[2])  # both days included
    assert [c.intc for c in candles] == [Decimal("802"), Decimal("801")]
    later = date(2026, 11, 20)
    assert await api("daily_history", "NSE", "SBIN-EQ", later, later) == []


# --- script master ---------------------------------------------------------------------------


async def test_download_instruments_writes_the_added_instruments(
    api: Api, state: BrokerState, tmp_path: Path
) -> None:
    add_nifty(state)
    path = await api("download_instruments", "NFO", tmp_path)
    assert path.name == "NFO_symbols.txt"
    from tradesmart.instruments import load

    rows = load(path)
    by_symbol = {row["TradingSymbol"]: row for row in rows}
    assert set(by_symbol) == {FUTURE} | {
        f"NIFTY{EXPIRY:%d}{MON}{EXPIRY:%y}{k}{s}" for k in "CP" for s in (23900, 24000, 24100)
    }
    option = by_symbol[OPTION]
    assert option["Token"] == "2003" and option["OptionType"] == "CE"
    assert option["Symbol"] == "NIFTY" and option["LotSize"] == "75"
    assert option["Instrument"] == "OPTIDX" and option["StrikePrice"] == "24000.00"
    assert option["Expiry"] == EXPIRY.strftime("%d-%b-%Y").upper()


async def test_instruments_returns_rows(
    api: Api, state: BrokerState, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))  # the default cache dir lives under HOME
    add_nifty(state)
    rows = await api("instruments", "NSE")
    assert [row["TradingSymbol"] for row in rows] == ["SBIN-EQ"]


async def test_bfo_script_master_names_the_strike_column_strike(
    api: Api, state: BrokerState, tmp_path: Path
) -> None:
    # Production 2026-11-12 (F28): BFO names the strike column "Strike", NFO "StrikePrice".
    add_nifty(state)
    state.add_instrument(
        "BFO",
        "802",
        "SENSEX26N1985200CE",
        lot_size=20,
        instrument="OPTIDX",
        expiry=EXPIRY,
        strike=85200,
        option_type="CE",
        underlying="BSXOPT",
    )
    from tradesmart.instruments import load

    bfo = load(await api("download_instruments", "BFO", tmp_path))
    nfo = load(await api("download_instruments", "NFO", tmp_path))
    assert "Strike" in bfo[0] and "StrikePrice" not in bfo[0]
    assert bfo[0]["Strike"] == "85200.00"
    assert "StrikePrice" in nfo[0] and "Strike" not in nfo[0]


def test_script_master_for_unknown_exchange_is_404(state: BrokerState) -> None:
    with httpx.Client(transport=OfflineTransport(state)) as http:
        assert http.get(OFFLINE_SCRIPT_MASTER_URL + "/XYZ_symbols.txt.zip").status_code == 404
        assert http.get(OFFLINE_SCRIPT_MASTER_URL + "/NSE_symbols.txt.zip").status_code == 200


# --- option analytics ------------------------------------------------------------------------


async def test_option_greeks_runs_on_offline_broker_data(
    api: Api, state: BrokerState, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    add_nifty(state)
    state.set_quote("NFO", "1001", last_price="24050.00")
    state.set_quote("NFO", "2003", last_price="320.00")
    greeks = await api("option_greeks", "NFO", OPTION)
    assert isinstance(greeks, OptionGreeks)
    assert greeks.status == "ok"


# --- drills ----------------------------------------------------------------------------------


async def test_reject_drill_raises_order_rejected_and_places_nothing(
    api: Api, state: BrokerState
) -> None:
    state.fail_next("PlaceOrder", drills.reject("RMS:Margin Exceeds"))
    with pytest.raises(OrderRejectedError) as caught:
        await api("place_order", **place_kwargs())
    assert caught.value.emsg == "RMS:Margin Exceeds"
    assert state.orders == []
    assert endpoints(state) == ["PlaceOrder"]
    # The drill fired once: the next order goes through.
    await api("place_order", **place_kwargs())
    assert len(state.orders) == 1


async def test_timeout_after_send_places_the_order_but_status_is_unknown(
    api: Api, state: BrokerState
) -> None:
    state.fail_next("PlaceOrder", drills.timeout_after_send())
    with pytest.raises(OrderStatusUnknownError):
        await api("place_order", **place_kwargs())
    assert len(state.orders) == 1 and state.orders[0].status == "OPEN"
    await api("place_order", **place_kwargs())  # fired once
    assert len(state.orders) == 2


async def test_connect_error_is_a_network_error_and_records_nothing(
    api: Api, state: BrokerState
) -> None:
    state.fail_next("PlaceOrder", drills.connect_error())
    with pytest.raises(NetworkError):
        await api("place_order", **place_kwargs())
    assert state.orders == []
    assert state.requests == []
    await api("place_order", **place_kwargs())  # fired once
    assert len(state.orders) == 1


async def test_rate_limit_drill_raises_rate_limit_error(api: Api, state: BrokerState) -> None:
    state.fail_next("PlaceOrder", drills.rate_limit(retry_after=2))
    with pytest.raises(RateLimitError) as caught:
        await api("place_order", **place_kwargs())
    assert caught.value.http_status == 429
    assert caught.value.retry_after == 2.0
    assert state.orders == []
    await api("place_order", **place_kwargs())  # fired once
    assert len(state.orders) == 1


@pytest.mark.parametrize("as_json", [False, True], ids=["gateway_page", "json"])
async def test_server_error_drill_raises_server_error(
    strict_api: Api, state: BrokerState, as_json: bool
) -> None:
    state.fail_next("Limits", drills.server_error(502, json=as_json))
    with pytest.raises(ServerError) as caught:
        await strict_api("margins")
    assert caught.value.http_status == 502
    assert isinstance(await strict_api("margins"), Limits)  # fired once


async def test_server_error_drill_on_an_order_is_status_unknown(
    api: Api, state: BrokerState
) -> None:
    # By default a 5xx carries a gateway page (not JSON), like a real gateway error: the order
    # may have been placed, so the SDK raises OrderStatusUnknownError and never retries it.
    state.fail_next("PlaceOrder", drills.server_error(503))
    with pytest.raises(OrderStatusUnknownError) as caught:
        await api("place_order", **place_kwargs())
    assert caught.value.http_status == 503
    assert state.orders == []
    assert endpoints(state).count("PlaceOrder") == 1


async def test_server_error_json_drill_on_an_order_is_a_definitive_rejection(
    api: Api, state: BrokerState
) -> None:
    # json=True: the 5xx carries a JSON Not_Ok body: on a state-changing call the SDK classifies
    # it as OrderRejectedError (not ServerError, not unknown) and never retries it.
    state.fail_next("PlaceOrder", drills.server_error(503, json=True))
    with pytest.raises(OrderRejectedError) as caught:
        await api("place_order", **place_kwargs())
    assert caught.value.http_status == 503
    assert not isinstance(caught.value, ServerError)
    assert state.orders == []
    assert endpoints(state).count("PlaceOrder") == 1


async def test_bad_body_drill_raises_response_parse_error(
    strict_api: Api, state: BrokerState
) -> None:
    state.fail_next("Limits", drills.bad_body())
    with pytest.raises(ResponseParseError):
        await strict_api("margins")
    assert isinstance(await strict_api("margins"), Limits)  # fired once


async def test_bad_body_on_an_order_is_status_unknown(api: Api, state: BrokerState) -> None:
    state.fail_next("PlaceOrder", drills.bad_body())
    with pytest.raises(OrderStatusUnknownError):
        await api("place_order", **place_kwargs())
    await api("place_order", **place_kwargs())  # fired once
    assert len(state.orders) == 1


async def test_session_expired_drill_raises_authentication_error(
    api: Api, state: BrokerState
) -> None:
    state.fail_next("OrderBook", drills.session_expired())
    with pytest.raises(AuthenticationError) as caught:
        await api("orders")
    assert caught.value.emsg == "Session Expired :  Invalid Session Key"  # documented text
    assert await api("orders") == []  # fired once


@pytest.mark.parametrize(
    "drill",
    [drills.rate_limit(retry_after=5), drills.server_error(), drills.connect_error()],
    ids=["rate_limit", "server_error", "connect_error"],
)
async def test_a_read_drill_is_retried_by_the_sdk_without_waiting(
    api: Api, state: BrokerState, drill: drills.Drill
) -> None:
    state.fail_next("Limits", drill)
    started = time.perf_counter()
    assert isinstance(await api("margins"), Limits)
    assert (
        time.perf_counter() - started < 4
    )  # generous: Retry-After 5 s must be clamped, not waited
    assert endpoints(state).count("Limits") == (1 if drill.kind == "connect_error" else 2)


async def test_max_retries_0_surfaces_the_read_drill(
    connect: Callable[..., Api], state: BrokerState
) -> None:
    api = connect(max_retries=0)
    state.fail_next("Limits", drills.rate_limit(retry_after=5))
    with pytest.raises(RateLimitError):
        await api("margins")
    assert endpoints(state) == ["Limits"]


# --- escape hatch and plan Review Focus 4 / 5 ------------------------------------------------


async def test_unmodelled_endpoint_answers_not_ok_with_hint(api: Api) -> None:
    with pytest.raises(APIError) as caught:
        await api(
            "order_margin",
            exchange="NFO",
            tradingsymbol=OPTION,
            quantity=75,
            price="101.50",
            product="M",
            transaction_type="B",
            order_type="LMT",
        )
    assert caught.value.emsg == (
        "OfflineBroker does not model GetOrderMargin; set a reply with broker.set_reply(...)"
    )


async def test_set_reply_answers_an_unmodelled_endpoint(api: Api, state: BrokerState) -> None:
    state.set_reply("GetOrderMargin", fixture("order_margin_success"))
    margin = await api(
        "order_margin",
        exchange="NFO",
        tradingsymbol=OPTION,
        quantity=75,
        price="101.50",
        product="M",
        transaction_type="B",
        order_type="LMT",
    )
    assert isinstance(margin, OrderMargin)
    assert margin.raw["ordermargin"] == "1598.00"


async def test_set_reply_wins_over_a_modelled_endpoint(api: Api, state: BrokerState) -> None:
    state.set_funds(cash="1.00")
    state.set_reply("Limits", {"stat": "Ok", "cash": "7.00"})
    assert (await api("margins")).cash == Decimal("7.00")


async def test_quote_for_unknown_instrument_is_no_data(api: Api) -> None:
    with pytest.raises(APIError) as caught:
        await api("quote", "NSE", "424242")
    assert type(caught.value) is APIError
    assert caught.value.emsg is not None and "no data" in caught.value.emsg


async def test_order_for_unknown_instrument_is_accepted(api: Api, state: BrokerState) -> None:
    order_id = await api("place_order", **place_kwargs(tradingsymbol="NEVERADDED-EQ"))
    assert state.orders[-1].id == order_id
    assert state.orders[-1].symbol == "NEVERADDED-EQ"
    assert (await api("orders"))[0].tradingsymbol == "NEVERADDED-EQ"


# --- wire-level edges ------------------------------------------------------------------------


def test_undecodable_jdata_answers_not_ok(state: BrokerState) -> None:
    with httpx.Client(transport=OfflineTransport(state)) as http:
        reply = http.post(OFFLINE_BASE_URL + "/Limits", content="jData={not json").json()
    assert reply["stat"] == "Not_Ok"


@pytest.mark.parametrize(
    ("endpoint", "body", "emsg"),
    [
        ("Limits", b"jData=\xff", "Invalid Input : jData is not a JSON object"),
        ("Limits", b"jKey=x", "Invalid Input : jData is not a JSON object"),
        ("Limits", b"jData=[1]", "Invalid Input : jData is not a JSON object"),
        ("PlaceOrder", b'jData={"qty":"abc"}', "Invalid Input : qty must be a number"),
        ("ModifyOrder", b'jData={"norenordno":"%s","qty":"x"}', "Invalid Input : qty must be"),
        ("GetOptionChain", b'jData={"strprc":"x","cnt":"1"}', "Invalid Input : strprc and cnt"),
        ("GetOptionChain", b'jData={"exch":"NFO","tsym":"NO","strprc":"1","cnt":"1"}', "no data"),
    ],
)
def test_requests_the_sdk_never_sends_answer_not_ok(
    state: BrokerState, endpoint: str, body: bytes, emsg: str
) -> None:
    order = state.place(
        {"exch": "NSE", "tsym": "SBIN-EQ", "qty": "1", "trantype": "B", "prctyp": "MKT"}
    )
    with httpx.Client(transport=OfflineTransport(state)) as http:
        content = body.replace(b"%s", order.id.encode()) if endpoint == "ModifyOrder" else body
        reply = http.post(f"{OFFLINE_BASE_URL}/{endpoint}", content=content).json()
    assert reply["stat"] == "Not_Ok"
    assert emsg in reply["emsg"]
    assert len(state.orders) == 1


def post(state: BrokerState, endpoint: str, content: str) -> httpx.Response:
    """One raw POST through the sync transport (for bodies the SDK never sends)."""
    with httpx.Client(transport=OfflineTransport(state)) as http:
        return http.post(f"{OFFLINE_BASE_URL}/{endpoint}", content=content)


def test_unknown_drill_kind_is_a_clear_error(state: BrokerState) -> None:
    state.fail_next("Limits", drills.Drill("nonsense"))
    with pytest.raises(ValueError, match="unknown drill kind 'nonsense'"):
        post(state, "Limits", 'jData={"uid":"OFFLINE01"}')


def test_an_undecodable_request_leaves_the_drill_armed(state: BrokerState) -> None:
    state.fail_next("Limits", drills.reject("armed"))
    with httpx.Client(transport=OfflineTransport(state)) as http:
        first = http.post(OFFLINE_BASE_URL + "/Limits", content="jData={not json").json()
        second = http.post(OFFLINE_BASE_URL + "/Limits", content='jData={"uid":"OFFLINE01"}').json()
    assert first["emsg"] == "Invalid Input : jData is not a JSON object"
    assert second["emsg"] == "armed"


def test_an_invalid_default_place_raises_instead_of_rejecting(state: BrokerState) -> None:
    state.default_place = "bogus"
    with pytest.raises(ValueError, match="default_place"):
        post(state, "PlaceOrder", 'jData={"qty":"1"}')
    assert state.orders == []


def test_unknown_path_is_404(state: BrokerState) -> None:
    with httpx.Client(transport=OfflineTransport(state)) as http:
        assert http.post("https://offline.tradesmart.invalid/other", content="").status_code == 404


async def test_requests_are_recorded_in_order(api: Api, state: BrokerState) -> None:
    await api("margins")
    await api("orders")
    assert endpoints(state) == ["Limits", "OrderBook"]
    assert state.requests[0].payload == {"uid": "OFFLINE01", "actid": "OFFLINE01"}
