"""TradeSmart API V2 client."""

from __future__ import annotations

from tradesmart._slicing import SlicedOrderResult
from tradesmart._ticks import round_to_tick
from tradesmart._version import __version__
from tradesmart.analytics import Buildup, BuildupReading, OptionChainSnapshot
from tradesmart.async_client import AsyncTradeSmart
from tradesmart.auth import AccessToken, build_authorize_url, compute_checksum
from tradesmart.client import TradeSmart
from tradesmart.config import ClientConfig
from tradesmart.enums import (
    Exchange,
    GttAlertType,
    GttValidity,
    OrderStatus,
    OrderType,
    Product,
    TransactionType,
    Validity,
)
from tradesmart.errors import (
    APIError,
    AuthenticationError,
    ConfigurationError,
    NetworkError,
    NotFoundError,
    OrderRejectedError,
    OrderStatusUnknownError,
    RateLimitError,
    RequestTimeoutError,
    RequestValidationError,
    ResponseParseError,
    ServerError,
    SubscriptionLimitError,
    TradeSmartError,
    TransportError,
)
from tradesmart.greeks import OptionGreeks
from tradesmart.models.gtt import Gtt, TriggeredGtt
from tradesmart.models.oco import OcoLeg
from tradesmart.models.oco_rows import OcoLegRow, OcoSide, OcoTrigger
from tradesmart.models.stream import (
    ConnectAck,
    DepthAck,
    DepthFeed,
    HeartbeatAck,
    OrderUpdate,
    PositionAck,
    PositionUpdate,
    StreamMessage,
    TouchlineAck,
    TouchlineFeed,
    UnknownMessage,
    UnsubscribeAck,
)
from tradesmart.stream import AsyncTradeSmartStream, StreamState, TradeSmartStream

__all__ = [
    "APIError",
    "AccessToken",
    "AsyncTradeSmart",
    "AsyncTradeSmartStream",
    "AuthenticationError",
    "Buildup",
    "BuildupReading",
    "ClientConfig",
    "ConfigurationError",
    "ConnectAck",
    "DepthAck",
    "DepthFeed",
    "Exchange",
    "Gtt",
    "GttAlertType",
    "GttValidity",
    "HeartbeatAck",
    "NetworkError",
    "NotFoundError",
    "OptionChainSnapshot",
    "OptionGreeks",
    "OcoLeg",
    "OcoLegRow",
    "OcoSide",
    "OcoTrigger",
    "OrderRejectedError",
    "OrderStatus",
    "OrderStatusUnknownError",
    "OrderUpdate",
    "PositionAck",
    "PositionUpdate",
    "OrderType",
    "Product",
    "RateLimitError",
    "RequestTimeoutError",
    "RequestValidationError",
    "ResponseParseError",
    "Validity",
    "ServerError",
    "SlicedOrderResult",
    "StreamMessage",
    "StreamState",
    "SubscriptionLimitError",
    "TouchlineAck",
    "TouchlineFeed",
    "TradeSmart",
    "TradeSmartError",
    "TradeSmartStream",
    "TransactionType",
    "TransportError",
    "TriggeredGtt",
    "UnknownMessage",
    "UnsubscribeAck",
    "__version__",
    "build_authorize_url",
    "compute_checksum",
    "round_to_tick",
]
