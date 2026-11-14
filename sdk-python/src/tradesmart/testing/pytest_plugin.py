"""Optional pytest fixtures for the offline broker.

Load them in the root ``conftest.py`` (pytest accepts ``pytest_plugins`` only there or in a
test module) with::

    pytest_plugins = ["tradesmart.testing.pytest_plugin"]

``offline_broker`` is a started ``OfflineBroker``; ``offline_client`` and
``async_offline_client`` are real clients wired to it. All are function-scoped and closed after
the test. They need no pytest plugin beyond pytest itself (``async_offline_client`` is a
plain fixture; run the async test with the async plugin you already use).
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest

from tradesmart.async_client import AsyncTradeSmart
from tradesmart.client import TradeSmart
from tradesmart.testing._broker import OfflineBroker

__all__ = ["async_offline_client", "offline_broker", "offline_client"]


@pytest.fixture
def offline_broker() -> Iterator[OfflineBroker]:
    """A started ``OfflineBroker``, closed after the test."""
    with OfflineBroker() as broker:
        yield broker


@pytest.fixture
def offline_client(offline_broker: OfflineBroker) -> Iterator[TradeSmart]:
    """A real ``TradeSmart`` wired to ``offline_broker``, closed after the test."""
    with offline_broker.client() as client:
        yield client


@pytest.fixture
def async_offline_client(offline_broker: OfflineBroker) -> Iterator[AsyncTradeSmart]:
    """A real ``AsyncTradeSmart`` wired to ``offline_broker``, closed after the test."""
    client = offline_broker.async_client()
    yield client
    # Teardown runs outside the test's event loop; closing needs no I/O (in-memory transport).
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(client.aclose())
    finally:
        loop.close()
