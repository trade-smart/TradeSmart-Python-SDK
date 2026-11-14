"""Subscription registry, connection state and snapshot cache (no I/O)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from tradesmart.enums import _StrEnum
from tradesmart.errors import SubscriptionLimitError
from tradesmart.stream.protocol import (
    depth_msg,
    order_updates_msg,
    position_updates_msg,
    touchline_msg,
)

MAX_SUBSCRIPTIONS = 1000


class StreamState(_StrEnum):
    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    AUTHENTICATING = "AUTHENTICATING"
    CONNECTED = "CONNECTED"
    RECONNECTING = "RECONNECTING"
    CLOSED = "CLOSED"


class SubscriptionRegistry:
    """Tracks what is subscribed so it can be restored after a reconnect."""

    def __init__(self) -> None:
        self.touchline: set[str] = set()
        self.depth: set[str] = set()
        self.orders: bool = False
        self.positions: bool = False

    def ensure_capacity(
        self, touchline_add: Iterable[str] = (), depth_add: Iterable[str] = ()
    ) -> None:
        total = len(self.touchline | set(touchline_add)) + len(self.depth | set(depth_add))
        if total > MAX_SUBSCRIPTIONS:
            raise SubscriptionLimitError(
                f"Subscription limit exceeded: {total} > {MAX_SUBSCRIPTIONS} per connection"
            )

    def add_touchline(self, keys: Iterable[str]) -> None:
        self.touchline.update(keys)

    def remove_touchline(self, keys: Iterable[str]) -> None:
        self.touchline.difference_update(keys)

    def add_depth(self, keys: Iterable[str]) -> None:
        self.depth.update(keys)

    def remove_depth(self, keys: Iterable[str]) -> None:
        self.depth.difference_update(keys)

    def restore_messages(self, uid: str) -> list[dict[str, str]]:
        """Messages that re-establish every subscription (sorted for determinism)."""
        messages: list[dict[str, str]] = []
        if self.touchline:
            messages.append(touchline_msg(sorted(self.touchline)))
        if self.depth:
            messages.append(depth_msg(sorted(self.depth)))
        if self.orders:
            messages.append(order_updates_msg(uid))
        if self.positions:
            messages.append(position_updates_msg(uid))
        return messages


class SnapshotCache:
    """Latest merged state per instrument, built from ack and delta messages."""

    def __init__(self) -> None:
        self._data: dict[str, dict[str, Any]] = {}

    def update(self, msg: Mapping[str, Any]) -> None:
        exchange, token = msg.get("e"), msg.get("tk")
        if not exchange or not token:
            return
        entry = self._data.setdefault(f"{exchange}|{token}", {})
        entry.update({k: v for k, v in msg.items() if k != "t"})

    def get(self, key: str) -> dict[str, Any] | None:
        entry = self._data.get(key)
        return dict(entry) if entry is not None else None
