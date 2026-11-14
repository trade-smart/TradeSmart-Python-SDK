"""Client configuration."""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ._version import __version__
from .errors import ConfigurationError

_HH_MM = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d")
# Printable ASCII (space to ~): no control characters (CR/LF/tab/DEL) and no non-ASCII.
_PRINTABLE_ASCII = re.compile(r"[\x20-\x7e]*")
_SDK_TOKEN = f"tradesmart-python/{__version__}"


def _is_int(value: object) -> bool:
    """An ``int`` that is not a ``bool``."""
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: object) -> bool:
    """An ``int`` or ``float`` that is not a ``bool`` (NaN/infinity are checked separately)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


@dataclass(frozen=True)
class ClientConfig:
    """Immutable settings shared by the sync and async clients."""

    base_url: str = "https://v2api.tradesmartonline.in/NorenWClientAPIv2"
    ws_url: str = "wss://v2api.tradesmartonline.in/NorenWSAPI/"
    authorize_url: str = "https://v2api.tradesmartonline.in/OAuthlogin/authorize/oauth"
    script_master_base_url: str = "https://v2api.tradesmartonline.in"
    connect_timeout: float = 5.0
    read_timeout: float = 10.0
    max_retries: int = 2
    backoff_base: float = 0.5
    backoff_cap: float = 8.0
    rate_limit_mode: Literal["wait", "raise"] = "wait"
    # In "wait" mode an order (place_order group) never waits longer than this for the
    # client-side rate limit; a longer wait raises RateLimitError and nothing is sent.
    order_rate_limit_max_wait: float = 1.0
    # Orders per second the client sends (most TradeSmart clients are limited to 10; 1..30).
    order_rate_limit_per_second: int = 10
    # PlaceOrder calls per rolling 60 s (1..600). Production refused order 201 in a minute
    # (F33), so the default is 200; the docs say 600. Never below order_rate_limit_per_second.
    order_rate_limit_per_minute: int = 200
    # Exchange-approved algo ID, sent as algo_id on every PlaceOrder and ModifyOrder (and so on
    # every place_sliced_order slice); the per-call algo_id= overrides it. The docs require it
    # for more than 10 orders per second, so order_rate_limit_per_second above 10 needs it.
    algo_id: str | None = None
    # The SDK token tradesmart-python/<version> is always sent: see header_user_agent().
    user_agent: str = _SDK_TOKEN
    log_payloads: bool = False
    check_token_expiry: bool = True
    heartbeat_interval: float = 30.0
    ws_auth_timeout: float = 10.0
    reconnect_max_attempts: int | None = 20
    reconnect_backoff_base: float = 1.0
    reconnect_backoff_cap: float = 60.0
    # Placeholder pending confirmation by TradeSmart (server idle timeout / connection limit).
    keepalive_expiry: float = 50.0
    # Placeholder pending confirmation by TradeSmart (server idle timeout / connection limit).
    max_connections: int = 10
    # Placeholder pending confirmation by TradeSmart (server idle timeout / connection limit).
    max_keepalive_connections: int = 5
    stream_backlog_limit: int = 1000
    # Observed refresh time, pending confirmation by TradeSmart.
    script_master_refresh_time: str = "07:00"
    # A cached file the server has not replaced since the last refresh is re-checked with a
    # conditional request at most this often (minutes); in between it is used as is.
    script_master_recheck_minutes: int = 15
    # Folder for the cached script master when no dest_dir is given (None: ~/.cache/tradesmart).
    # A leading ~ is expanded; a relative path is used as given, so it follows the current
    # working folder at each download (an absolute path is recommended).
    instruments_dir: str | Path | None = None

    def __post_init__(self) -> None:
        for name in (
            "base_url",
            "ws_url",
            "authorize_url",
            "script_master_base_url",
            "user_agent",
            "script_master_refresh_time",
        ):
            if not isinstance(getattr(self, name), str):
                raise ConfigurationError(f"{name} must be a string")
        for name in ("log_payloads", "check_token_expiry"):
            if not isinstance(getattr(self, name), bool):
                raise ConfigurationError(f"{name} must be True or False")
        for name in ("base_url", "authorize_url", "script_master_base_url"):
            if not getattr(self, name).startswith(("http://", "https://")):
                raise ConfigurationError(f"{name} must start with http:// or https://")
        if not self.ws_url.startswith(("ws://", "wss://")):
            raise ConfigurationError("ws_url must start with ws:// or wss://")
        for name in (
            "connect_timeout",
            "read_timeout",
            "backoff_base",
            "backoff_cap",
            "heartbeat_interval",
            "ws_auth_timeout",
            "reconnect_backoff_base",
            "reconnect_backoff_cap",
            "keepalive_expiry",
        ):
            value = getattr(self, name)
            # NaN, infinity and non-numbers (str, None, bool ...) are rejected too.
            if not _is_number(value) or not math.isfinite(value) or value <= 0:
                raise ConfigurationError(f"{name} must be a finite positive number")
        for name, minimum in (
            ("max_connections", 1),
            ("max_keepalive_connections", 1),
            ("stream_backlog_limit", 1),
            ("script_master_recheck_minutes", 1),
            ("max_retries", 0),
        ):
            if not _is_int(getattr(self, name)) or getattr(self, name) < minimum:
                # Integers only: a float (NaN included) or a bool is rejected.
                raise ConfigurationError(f"{name} must be an integer >= {minimum}")
        attempts = self.reconnect_max_attempts
        if attempts is not None and (not _is_int(attempts) or attempts < 0):
            raise ConfigurationError("reconnect_max_attempts must be an integer >= 0 or None")
        if self.max_keepalive_connections > self.max_connections:
            raise ConfigurationError("max_keepalive_connections must be <= max_connections")
        if not _PRINTABLE_ASCII.fullmatch(self.user_agent):
            # Never echo the value: it may be an attempted header injection. HTTP header
            # values are ASCII; anything else fails only when the request is built.
            raise ConfigurationError(
                "user_agent must contain only printable ASCII characters (no control characters)"
            )
        if not _HH_MM.fullmatch(self.script_master_refresh_time):
            raise ConfigurationError("script_master_refresh_time must be HH:MM (24-hour, IST)")
        max_wait = self.order_rate_limit_max_wait
        if not _is_number(max_wait) or not math.isfinite(max_wait) or max_wait < 0:
            raise ConfigurationError("order_rate_limit_max_wait must be a finite number >= 0")
        per_second = self.order_rate_limit_per_second
        if not _is_int(per_second) or not 1 <= per_second <= 30:
            raise ConfigurationError("order_rate_limit_per_second must be an integer from 1 to 30")
        per_minute = self.order_rate_limit_per_minute
        if not _is_int(per_minute) or not 1 <= per_minute <= 600:
            raise ConfigurationError("order_rate_limit_per_minute must be an integer from 1 to 600")
        if per_minute < per_second:
            raise ConfigurationError(
                "order_rate_limit_per_minute must be at least order_rate_limit_per_second"
            )
        algo_id = self.algo_id
        if algo_id is not None and (not isinstance(algo_id, str) or not algo_id.strip()):
            raise ConfigurationError("algo_id must be a non-blank string or None")
        if per_second > 10 and algo_id is None:
            raise ConfigurationError(
                "order_rate_limit_per_second above 10 needs algo_id (TradeSmart requires an "
                "algo ID for more than 10 orders per second)"
            )
        if self.rate_limit_mode not in ("wait", "raise"):
            raise ConfigurationError("rate_limit_mode must be 'wait' or 'raise'")
        folder = self.instruments_dir
        if folder is not None and (
            not isinstance(folder, (str, os.PathLike)) or not os.fspath(folder).strip()
        ):
            raise ConfigurationError("instruments_dir must be a non-empty path or None")
        object.__setattr__(self, "base_url", self.base_url.rstrip("/"))


def header_user_agent(config: ClientConfig) -> str:
    """The ``User-Agent`` actually sent: ``config.user_agent`` plus the SDK token.

    A configured value that already contains ``tradesmart-python/<version>`` is sent as
    is; anything else gets the token appended so the SDK version is always identifiable.
    """
    if _SDK_TOKEN in config.user_agent:
        return config.user_agent
    return f"{config.user_agent} {_SDK_TOKEN}".strip()
