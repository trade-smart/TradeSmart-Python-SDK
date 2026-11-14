import pytest

from tradesmart.config import ClientConfig
from tradesmart.enums import Exchange, OrderStatus, OrderType, Product, TransactionType, Validity
from tradesmart.errors import (
    APIError,
    ConfigurationError,
    OrderRejectedError,
    OrderStatusUnknownError,
    ResponseParseError,
)
from tradesmart.stream import StreamState


def test_api_error_carries_context():
    e = APIError(
        "boom",
        endpoint="PlaceOrder",
        http_status=200,
        emsg="Rejected",
        request_time="t",
        raw={"stat": "Not_Ok"},
    )
    assert (e.endpoint, e.http_status, e.emsg) == ("PlaceOrder", 200, "Rejected")
    assert isinstance(OrderRejectedError("x", endpoint="e", http_status=200), APIError)


def test_parse_error_truncates_body():
    err = ResponseParseError("bad", endpoint="e", http_status=200, body="x" * 5000)
    assert len(err.body) == 2000


def test_order_status_unknown_error_signature_and_context():
    legacy = OrderStatusUnknownError("PlaceOrder", "ReadTimeout")
    assert (legacy.endpoint, legacy.message) == ("PlaceOrder", "ReadTimeout")
    assert legacy.http_status is None and legacy.body is None
    assert str(legacy) == "PlaceOrder: order status unknown: ReadTimeout"
    err = OrderStatusUnknownError("PlaceOrder", "bad body", http_status=504, body="x" * 5000)
    assert err.http_status == 504 and err.body is not None and len(err.body) == 2000


def test_enum_wire_values():
    assert OrderType.SL.value == "SL-LMT"
    assert Product.MTF.value == "F"
    assert TransactionType.SELL.value == "S"


def test_config_defaults_and_validation():
    c = ClientConfig()
    assert c.base_url == "https://v2api.tradesmartonline.in/NorenWClientAPIv2"
    assert c.heartbeat_interval == 30.0
    assert ClientConfig(base_url="https://x/").base_url == "https://x"
    for bad in (
        {"base_url": "ftp://x"},
        {"read_timeout": 0},
        {"max_retries": -1},
        {"rate_limit_mode": "drop"},
        {"ws_url": "https://x"},
    ):
        with pytest.raises(ConfigurationError):
            ClientConfig(**bad)  # type: ignore[arg-type]


def test_enum_aliases() -> None:
    assert TransactionType("buy") is TransactionType.BUY
    assert TransactionType("s") is TransactionType.SELL
    assert OrderType("sl-m") is OrderType.SL_M
    assert OrderType("SLM") is OrderType.SL_M
    assert OrderType("SL-LMT") is OrderType.SL
    assert OrderType("market") is OrderType.MARKET
    assert Product("mis") is Product.MIS
    assert Product("H") is Product.CO
    assert Product("bo") is Product.BO
    assert Validity("ioc") is Validity.IOC
    with pytest.raises(ValueError):
        OrderType("LIMITT")


def test_config_rejects_bad_pool_values() -> None:
    for bad in (
        {"max_keepalive_connections": 6, "max_connections": 5},
        {"keepalive_expiry": 0},
        {"max_connections": 0},
        {"max_keepalive_connections": 0},
        {"stream_backlog_limit": 0},
    ):
        with pytest.raises(ConfigurationError):
            ClientConfig(**bad)  # type: ignore[arg-type]
    c = ClientConfig()
    assert (c.keepalive_expiry, c.max_connections, c.max_keepalive_connections) == (50.0, 10, 5)
    assert c.stream_backlog_limit == 1000


_POSITIVE_SETTINGS = (
    "connect_timeout",
    "read_timeout",
    "backoff_base",
    "backoff_cap",
    "heartbeat_interval",
    "ws_auth_timeout",
    "reconnect_backoff_base",
    "reconnect_backoff_cap",
    "keepalive_expiry",
)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
@pytest.mark.parametrize("name", _POSITIVE_SETTINGS)
def test_config_positive_settings_must_be_finite(name: str, value: float) -> None:
    with pytest.raises(ConfigurationError, match=f"{name} must be a finite positive number"):
        ClientConfig(**{name: value})  # type: ignore[arg-type]


@pytest.mark.parametrize("name", _POSITIVE_SETTINGS)
def test_config_positive_settings_reject_zero(name: str) -> None:
    with pytest.raises(ConfigurationError, match=f"{name} must be a finite positive number"):
        ClientConfig(**{name: 0})  # type: ignore[arg-type]


def test_config_max_connections_rejects_zero() -> None:
    with pytest.raises(ConfigurationError, match="max_connections must be an integer >= 1"):
        ClientConfig(max_connections=0)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), -0.5])
def test_config_order_rate_limit_max_wait_must_be_finite_and_not_negative(value: float) -> None:
    with pytest.raises(ConfigurationError, match="order_rate_limit_max_wait"):
        ClientConfig(order_rate_limit_max_wait=value)


def test_config_order_rate_limit_max_wait_zero_is_still_allowed() -> None:
    assert ClientConfig(order_rate_limit_max_wait=0).order_rate_limit_max_wait == 0


@pytest.mark.parametrize(
    "member",
    [
        OrderStatus.OPEN,
        TransactionType.BUY,
        Product.MIS,
        OrderType.SL_M,
        Validity.IOC,
        Exchange.NSE,
        StreamState.CONNECTED,
    ],
)
def test_enum_text_is_the_wire_value(member) -> None:
    # StrEnum semantics: the text is the wire value.
    assert str(member) == member.value
    assert f"{member}" == member.value
    assert format(member, ">8") == format(member.value, ">8")


def test_order_rate_limit_per_second_defaults_to_10() -> None:
    assert ClientConfig().order_rate_limit_per_second == 10


@pytest.mark.parametrize("value", [1, 10])
def test_order_rate_limit_per_second_up_to_10_needs_no_algo_id(value: int) -> None:
    assert ClientConfig(order_rate_limit_per_second=value).order_rate_limit_per_second == value


@pytest.mark.parametrize("value", [11, 30])
def test_order_rate_limit_per_second_above_10_with_algo_id_is_accepted(value: int) -> None:
    config = ClientConfig(order_rate_limit_per_second=value, algo_id="ALGO1")
    assert config.order_rate_limit_per_second == value and config.algo_id == "ALGO1"


@pytest.mark.parametrize("value", [11, 30])
def test_order_rate_limit_per_second_above_10_without_algo_id_is_refused(value: int) -> None:
    # Docs (PlaceOrder): algo_id is "Required when placing more than 10 orders per second".
    with pytest.raises(ConfigurationError, match="algo_id") as caught:
        ClientConfig(order_rate_limit_per_second=value)
    assert "order_rate_limit_per_second" in str(caught.value)


def test_algo_id_defaults_to_none() -> None:
    assert ClientConfig().algo_id is None


def test_algo_id_is_kept() -> None:
    assert ClientConfig(algo_id="153763").algo_id == "153763"


@pytest.mark.parametrize("value", ["", "   ", "\t", True, False, 153763, 1.5, b"153763"])
def test_algo_id_must_be_a_non_blank_string_or_none(value: object) -> None:
    with pytest.raises(ConfigurationError, match="algo_id") as caught:
        ClientConfig(algo_id=value)  # type: ignore[arg-type]
    assert "153763" not in str(caught.value)  # the value is never echoed


@pytest.mark.parametrize("value", [0, 31, 1.5, True, "10", None, float("nan")])
def test_order_rate_limit_per_second_is_validated(value: object) -> None:
    with pytest.raises(ConfigurationError, match="order_rate_limit_per_second"):
        ClientConfig(order_rate_limit_per_second=value)  # type: ignore[arg-type]


def test_order_rate_limit_per_minute_default():
    assert ClientConfig().order_rate_limit_per_minute == 200


@pytest.mark.parametrize("value", [0, 601, True, 2.5, "200"])
def test_order_rate_limit_per_minute_refused(value):
    with pytest.raises(
        ConfigurationError, match="order_rate_limit_per_minute must be an integer from 1 to 600"
    ):
        ClientConfig(order_rate_limit_per_minute=value)


def test_per_minute_below_per_second_refused():
    with pytest.raises(ConfigurationError, match="order_rate_limit_per_minute"):
        ClientConfig(order_rate_limit_per_second=10, order_rate_limit_per_minute=5)
