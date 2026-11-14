"""The public surface of ``tradesmart.testing`` stays small and separate (design §2)."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import tradesmart.testing
from tradesmart.testing import OfflineBroker

OFFLINE_BROKER_MEMBERS = {
    # state set by the test
    "add_instrument",
    "set_quote",
    "set_candles",
    "set_funds",
    "set_holdings",
    "set_positions",
    "set_user",
    "set_reply",
    "tick",
    # orders, driven by the test
    "accept",
    "reject",
    "fill",
    "cancel",
    "trigger",
    "trigger_gtt",
    # REST drills
    "fail_next",
    # read access and settings
    "orders",
    "requests",
    "client_id",
    "default_place",
    "default_reject_message",
    # stream drills
    "drop_stream",
    "refuse_stream_login",
    "silence_stream",
    "resume_stream",
    # real clients and streams wired to the offline broker
    "client",
    "async_client",
    "stream",
    "async_stream",
    # lifecycle (also a context manager)
    "close",
}


def test_public_surface_budget() -> None:
    public = {name for name in dir(OfflineBroker) if not name.startswith("_")}
    assert public == OFFLINE_BROKER_MEMBERS
    assert len(public) <= 30


def test_package_exports() -> None:
    assert set(tradesmart.testing.__all__) == {
        "Drill",
        "OfflineBroker",
        "OfflineOrder",
        "OfflineRequest",
        "drills",
    }


def test_import_tradesmart_does_not_import_testing() -> None:
    code = "import sys, tradesmart; assert 'tradesmart.testing' not in sys.modules"
    subprocess.run([sys.executable, "-c", code], check=True, timeout=300)  # noqa: S603 - fixed code


PLUGIN_TEST = textwrap.dedent(
    """
    import asyncio
    from pathlib import Path

    import pytest

    from tradesmart import AsyncTradeSmart, TradeSmart
    from tradesmart.testing import OfflineBroker

    pytest_plugins = ["tradesmart.testing.pytest_plugin"]

    SEEN = {}


    def test_fixtures_share_one_broker(offline_broker, offline_client, async_offline_client):
        assert isinstance(offline_broker, OfflineBroker)
        assert isinstance(offline_client, TradeSmart)
        assert isinstance(async_offline_client, AsyncTradeSmart)
        offline_broker.add_instrument("NSE", "22", "ACC-EQ")
        offline_client.place_order(
            exchange="NSE", tradingsymbol="ACC-EQ", quantity=1, transaction_type="B",
            product="I", order_type="MKT",
        )
        assert len(offline_broker.orders) == 1
        orders = asyncio.run(async_offline_client.orders())
        assert [o.order_id for o in orders] == [offline_broker.orders[0].id]
        SEEN.update(
            broker=offline_broker,
            client=offline_client,
            async_client=async_offline_client,
            cache=Path(str(offline_client.config.instruments_dir)),
        )
        assert SEEN["cache"].is_dir()


    def test_fixtures_are_closed_after_the_test(offline_broker):
        assert offline_broker is not SEEN["broker"]
        assert offline_broker.orders == []
        with pytest.raises(RuntimeError, match="closed"):
            SEEN["broker"].client()
        with pytest.raises(Exception, match="closed"):
            SEEN["client"].profile()
        with pytest.raises(Exception, match="closed"):
            asyncio.run(SEEN["async_client"].profile())
        assert not SEEN["cache"].exists()
    """
)


def test_pytest_plugin_fixtures(tmp_path: Path) -> None:
    (tmp_path / "test_uses_plugin.py").write_text(PLUGIN_TEST, encoding="utf-8")
    env = {**os.environ, "HOME": str(tmp_path / "home")}
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "no:randomly"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "2 passed" in result.stdout


def test_no_public_name_says_fake() -> None:
    import tradesmart.testing.pytest_plugin as plugin

    names = [*tradesmart.testing.__all__, *plugin.__all__]
    assert [name for name in names if "fake" in name.lower()] == []
