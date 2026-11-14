"""Bounded, merging hand-off between the stream reader and the code that consumes it."""

from __future__ import annotations

import asyncio
from collections import deque
from typing import Any

from tradesmart._logging import get_logger
from tradesmart.models.stream import DepthFeed, StreamMessage, TouchlineFeed

_log = get_logger("stream")

_Instrument = tuple[str, str]

_FEED_TYPES: tuple[type[StreamMessage], ...] = (TouchlineFeed, DepthFeed)
_IDENTITY = frozenset({"t", "e", "tk"})


def _wire_names(cls: type[StreamMessage]) -> frozenset[str]:
    return frozenset(field.alias or name for name, field in cls.model_fields.items()) - _IDENTITY


_WIRE_FIELDS = {cls: _wire_names(cls) for cls in _FEED_TYPES}  # fields each feed type can carry


class StreamEnded(Exception):
    """Raised by :meth:`DeliveryBuffer.get` once the buffer has ended and is drained."""


class _Entry:
    __slots__ = ("feed", "instrument", "message", "raw")

    def __init__(self, message: StreamMessage, instrument: _Instrument | None) -> None:
        self.message = message  # the latest message merged into this entry
        self.instrument = instrument
        # The feed type for a touchline/depth feed of an instrument; None for anything else.
        self.feed = (
            type(message) if instrument is not None and type(message) in _FEED_TYPES else None
        )
        self.raw: dict[str, Any] | None = None  # merged wire fields; set once merged (dirty)

    def merge(self, fields: dict[str, Any]) -> None:
        base = self.raw if self.raw is not None else self.message.raw
        self.raw = {**base, **fields}

    def wire_keys(self) -> set[str]:
        return set(self.raw if self.raw is not None else self.message.raw)


def _instrument(message: StreamMessage) -> _Instrument | None:
    """``(e, tk)`` of any message that carries both (feeds, acks, unknown types)."""
    exchange, token = message.raw.get("e"), message.raw.get("tk")
    if exchange is None or token is None:
        return None
    return (str(exchange), str(token))


class DeliveryBuffer:
    """FIFO of stream messages for one consumer; ``put`` never blocks or raises.

    Once ``limit`` messages are waiting, a touchline/depth feed (``tf``/``df``)
    may be merged into a waiting entry of its instrument instead of being
    appended (latest value per field wins; the entry keeps its position). Only
    the instrument's two newest waiting entries are considered:

    a. the newest is a feed of the same type: the message is merged into it;
    b. the newest is a feed of the other type and the second newest a feed of
       the same type: the message is merged into the second newest, and its
       fields the other type can carry (``v``, ``lp``, ``bp1``, ...) are also
       copied into the newest.

    Otherwise it is appended. An ack (``tk``/``dk``) or any other message
    carrying the instrument's ``e``/``tk`` is an entry too, so nothing merges
    across it. A lagging consumer therefore holds about two entries per
    instrument, the values of each field are delivered in arrival order (never
    newer, then older), and applying the messages in order ends with the
    latest value of every field. Acks, order and position updates and unknown
    messages are never merged or dropped. One WARNING is logged when merging
    starts and one when the backlog drops below ``limit // 2`` again.
    """

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._entries: deque[_Entry] = deque()
        # The instrument's waiting entries, newest last: at most the two newest.
        self._recent: dict[_Instrument, list[_Entry]] = {}
        self._waiters: list[asyncio.Future[None]] = []
        self._merged = 0
        self._merging = False
        self._ended = False

    def __len__(self) -> int:
        return len(self._entries)

    @property
    def merged_count(self) -> int:
        """How many messages were merged into an earlier waiting entry so far."""
        return self._merged

    def put(self, message: StreamMessage) -> None:
        entry = _Entry(message, _instrument(message))
        if entry.feed is not None and len(self._entries) >= self._limit and self._merge(entry):
            return
        self._entries.append(entry)
        if entry.instrument is not None:
            recent = self._recent.setdefault(entry.instrument, [])
            recent.append(entry)
            if len(recent) > 2:
                del recent[0]
        self._wake()

    def _merge(self, new: _Entry) -> bool:
        """Apply rule a or b to the feed ``new``; False if it must be appended."""
        recent = self._recent.get(new.instrument) if new.instrument is not None else None
        if not recent:
            return False
        newest = recent[-1]
        if newest.feed is new.feed:  # a
            target = newest
        elif newest.feed is not None and len(recent) == 2 and recent[0].feed is new.feed:  # b
            target = recent[0]
            # ``newest`` is delivered after ``target``: give it the newer shared values
            known = _WIRE_FIELDS[newest.feed] | newest.wire_keys()
            fields = new.message.raw
            shared = {k: v for k, v in fields.items() if k in known and k not in _IDENTITY}
            if shared:
                newest.merge(shared)
        else:
            return False
        target.merge(new.message.raw)
        target.message = new.message
        self._merged += 1
        if not self._merging:
            self._merging = True
            _log.warning(
                "Stream consumer is falling behind (%d messages waiting); "
                "merging market-data updates per instrument",
                len(self._entries),
            )
        return True

    async def get(self) -> StreamMessage:
        """Next message in order; raises :class:`StreamEnded` once ended and drained."""
        while not self._entries:
            if self._ended:
                raise StreamEnded
            waiter: asyncio.Future[None] = asyncio.get_running_loop().create_future()
            self._waiters.append(waiter)
            try:
                await waiter
            except asyncio.CancelledError:
                if waiter in self._waiters:
                    self._waiters.remove(waiter)
                raise
        return self._pop()

    def drain(self) -> list[StreamMessage]:
        """Remove and return every waiting message, in order."""
        return [self._pop() for _ in range(len(self._entries))]

    def end(self) -> None:
        """No more messages will arrive: ``get`` raises once the rest is drained."""
        self._ended = True
        self._wake()

    def _pop(self) -> StreamMessage:
        entry = self._entries.popleft()
        if entry.instrument is not None:
            recent = self._recent.get(entry.instrument)
            if recent is not None and recent[0] is entry:  # the oldest is popped first
                del recent[0]
                if not recent:
                    del self._recent[entry.instrument]
        message = entry.message
        if entry.raw is not None:
            try:
                message = type(message).from_wire(entry.raw)
            except Exception:  # defensive: deliver the latest update rather than nothing
                _log.warning("Could not rebuild a merged %r stream message", message.t)
        if self._merging and len(self._entries) < max(1, self._limit // 2):
            self._merging = False
            _log.warning(
                "Stream consumer caught up (%d messages waiting); %d updates merged so far",
                len(self._entries),
                self._merged,
            )
        return message

    def _wake(self) -> None:
        waiters, self._waiters = self._waiters, []
        for waiter in waiters:
            if not waiter.done():
                waiter.set_result(None)
