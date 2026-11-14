"""ClientConfig rejects wrongly typed values with ConfigurationError (review round 1).

Spec §12: invalid values raise ``ConfigurationError``, never a raw ``TypeError``, and an
integer setting never accepts a float (NaN included) or a bool.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tradesmart import ClientConfig, ConfigurationError

_INT_SETTINGS = (
    "max_retries",
    "stream_backlog_limit",
    "reconnect_max_attempts",
    "max_connections",
    "max_keepalive_connections",
    "script_master_recheck_minutes",
)
_FLOAT_SETTINGS = (
    "connect_timeout",
    "read_timeout",
    "backoff_base",
    "backoff_cap",
    "heartbeat_interval",
    "ws_auth_timeout",
    "reconnect_backoff_base",
    "reconnect_backoff_cap",
    "keepalive_expiry",
    "order_rate_limit_max_wait",
)


@pytest.mark.parametrize("value", [float("nan"), 1.5, 2.0, True, "2", None])
@pytest.mark.parametrize("name", _INT_SETTINGS)
def test_integer_settings_reject_non_integers(name: str, value: Any) -> None:
    if name == "reconnect_max_attempts" and value is None:
        value = float("inf")  # None is valid there ("retry forever"); infinity is not
    with pytest.raises(ConfigurationError, match=name):
        ClientConfig(**{name: value})


@pytest.mark.parametrize("value", ["10", None, True, b"1", [1]])
@pytest.mark.parametrize("name", _FLOAT_SETTINGS)
def test_number_settings_reject_non_numbers(name: str, value: Any) -> None:
    with pytest.raises(ConfigurationError, match=name):
        ClientConfig(**{name: value})


def test_integer_settings_keep_their_valid_values() -> None:
    config = ClientConfig(
        max_retries=0,
        stream_backlog_limit=1,
        reconnect_max_attempts=None,
        max_connections=3,
        max_keepalive_connections=3,
    )
    assert (config.max_retries, config.stream_backlog_limit) == (0, 1)
    assert config.reconnect_max_attempts is None
    assert ClientConfig(reconnect_max_attempts=0).reconnect_max_attempts == 0


def test_number_settings_accept_ints() -> None:
    assert ClientConfig(read_timeout=3).read_timeout == 3


@pytest.mark.parametrize(
    "name", ["base_url", "ws_url", "authorize_url", "script_master_base_url", "user_agent"]
)
@pytest.mark.parametrize("value", [None, 1, b"https://x"])
def test_text_settings_reject_non_strings(name: str, value: Any) -> None:
    with pytest.raises(ConfigurationError, match=name):
        ClientConfig(**{name: value})


@pytest.mark.parametrize("value", [None, 700, b"07:00"])
def test_refresh_time_rejects_non_strings(value: Any) -> None:
    with pytest.raises(ConfigurationError, match="script_master_refresh_time"):
        ClientConfig(script_master_refresh_time=value)


@pytest.mark.parametrize("name", ["log_payloads", "check_token_expiry"])
@pytest.mark.parametrize("value", [None, 1, "yes"])
def test_flags_must_be_bools(name: str, value: Any) -> None:
    with pytest.raises(ConfigurationError, match=name):
        ClientConfig(**{name: value})


@pytest.mark.parametrize(
    "agent",
    ["MyBot/1.0 é", "bot ", "bot\x7f", "bot\tx", "bot\r\nX-Evil: 1"],
    ids=["latin", "line-separator", "del", "tab", "crlf"],
)
def test_user_agent_must_be_printable_ascii(agent: str) -> None:
    with pytest.raises(ConfigurationError, match="user_agent") as info:
        ClientConfig(user_agent=agent)
    assert agent not in str(info.value)  # never echoed


def test_user_agent_with_spaces_and_punctuation_is_fine() -> None:
    assert ClientConfig(user_agent="my-algo/1.0 (+ops; v2)").user_agent == "my-algo/1.0 (+ops; v2)"


def test_instruments_dir_path_still_accepted(tmp_path: Path) -> None:
    assert ClientConfig(instruments_dir=tmp_path).instruments_dir == tmp_path
