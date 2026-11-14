"""The autouse guard in conftest.py must block real outbound network access."""

import socket
from pathlib import Path

import pytest

BLOCKED = "network access is blocked in tests"


def test_non_loopback_connect_is_blocked() -> None:
    # 203.0.113.10 is TEST-NET-3 (RFC 5737): never routable, so no real traffic either way.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        with pytest.raises(RuntimeError, match=BLOCKED) as info:
            sock.connect(("203.0.113.10", 443))
    assert "203.0.113.10" in str(info.value)


def test_non_loopback_connect_ex_is_blocked() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        with pytest.raises(RuntimeError, match=BLOCKED):
            sock.connect_ex(("203.0.113.10", 443))


def test_create_connection_to_non_loopback_is_blocked() -> None:
    with pytest.raises(RuntimeError, match=BLOCKED):
        socket.create_connection(("203.0.113.10", 443), timeout=0.2)


def test_non_loopback_dns_is_blocked() -> None:
    with pytest.raises(RuntimeError, match=BLOCKED):
        socket.getaddrinfo("v2api.tradesmartonline.in", 443)


def test_loopback_connect_is_allowed() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            conn, _ = server.accept()
            conn.close()
    assert socket.getaddrinfo("localhost", port)


def test_live_suite_exemption_matches_the_live_directory_only() -> None:
    from tests.conftest import is_live_suite

    tests_dir = Path(__file__).resolve().parent
    assert is_live_suite(tests_dir / "live" / "test_live_readonly.py")
    assert is_live_suite(tests_dir / "live_orders" / "test_live_orders.py")
    assert not is_live_suite(tests_dir / "live_orders_x" / "test_x.py")
    assert not is_live_suite(tests_dir / "unit" / "test_live_orders_safety.py")
    assert not is_live_suite(tests_dir / "unit" / "live" / "test_x.py")
    assert not is_live_suite(tests_dir / "unit" / "test_live_suite_is_read_only.py")
    assert not is_live_suite(Path("/elsewhere/live/test_x.py"))
