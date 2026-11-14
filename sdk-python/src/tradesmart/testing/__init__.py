"""Offline TradeSmart broker for testing strategy code without an account or network.

Import it explicitly (``from tradesmart.testing import OfflineBroker``); ``import tradesmart`` never
loads it. Optional pytest fixtures: ``pytest_plugins = ["tradesmart.testing.pytest_plugin"]``.
"""

from __future__ import annotations

from tradesmart.testing import drills
from tradesmart.testing._broker import OfflineBroker
from tradesmart.testing._state import OfflineOrder, OfflineRequest
from tradesmart.testing.drills import Drill

__all__ = ["Drill", "OfflineBroker", "OfflineOrder", "OfflineRequest", "drills"]
