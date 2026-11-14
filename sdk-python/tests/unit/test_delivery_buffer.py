import asyncio
import logging
import random

import pytest

from tradesmart.models.stream import (
    DepthAck,
    DepthFeed,
    OrderUpdate,
    TouchlineAck,
    TouchlineFeed,
    UnknownMessage,
)
from tradesmart.stream.delivery import DeliveryBuffer, StreamEnded
from tradesmart.stream.state import SnapshotCache


def tf(key: str = "NSE|22", **fields: str) -> TouchlineFeed:
    exchange, token = key.split("|")
    return TouchlineFeed.from_wire({"t": "tf", "e": exchange, "tk": token, **fields})


def om(order_id: int) -> OrderUpdate:
    return OrderUpdate.from_wire({"t": "om", "norenordno": str(order_id), "status": "OPEN"})


async def drain(buffer: DeliveryBuffer, count: int) -> list:
    return [await asyncio.wait_for(buffer.get(), 1) for _ in range(count)]


# --- brief tests -------------------------------------------------------------


async def test_buffer_fifo_below_limit():
    buffer = DeliveryBuffer(10)
    sent = [tf(lp=str(i)) for i in range(5)]
    for message in sent:
        buffer.put(message)
    assert len(buffer) == 5
    assert await drain(buffer, 5) == sent
    assert buffer.merged_count == 0 and len(buffer) == 0


async def test_buffer_merges_feeds_over_limit():
    buffer = DeliveryBuffer(2)
    buffer.put(tf(lp="1"))
    buffer.put(om(1))
    buffer.put(tf(v="5"))
    buffer.put(tf(lp="3"))
    assert len(buffer) == 2
    merged, order = await drain(buffer, 2)
    assert isinstance(merged, TouchlineFeed)
    assert merged.last_price == 3 and merged.volume == 5
    assert merged.raw == {"t": "tf", "e": "NSE", "tk": "22", "lp": "3", "v": "5"}
    assert isinstance(order, OrderUpdate) and order.order_id == "1"
    assert buffer.merged_count == 2


async def test_buffer_never_merges_order_updates():
    buffer = DeliveryBuffer(1)
    for n in (1, 2, 3):
        buffer.put(om(n))
    got = await drain(buffer, 3)
    assert [m.order_id for m in got] == ["1", "2", "3"]
    assert buffer.merged_count == 0


async def test_buffer_end_after_drain():
    buffer = DeliveryBuffer(10)
    buffer.put(om(1))
    buffer.put(om(2))
    buffer.end()
    assert [m.order_id for m in await drain(buffer, 2)] == ["1", "2"]
    with pytest.raises(StreamEnded):
        await buffer.get()
    with pytest.raises(StreamEnded):  # stays ended
        await buffer.get()


# --- additional coverage ------------------------------------------------------


async def test_buffer_merges_per_instrument_and_type_only():
    buffer = DeliveryBuffer(1)
    depth = DepthFeed.from_wire({"t": "df", "e": "NSE", "tk": "22", "bp1": "1"})
    ack = TouchlineAck.from_wire({"t": "tk", "e": "NSE", "tk": "22", "lp": "1"})
    for message in (tf(lp="1"), tf("NSE|23", lp="2"), tf(lp="3"), depth, ack, ack, tf(lp="4")):
        buffer.put(message)
    got = await drain(buffer, len(buffer))
    # NSE|22 touchline merged across the other instrument only; depth and acks kept
    # separate, and the feed after them is appended (never merged in front of them)
    assert [type(m).__name__ for m in got] == [
        "TouchlineFeed",
        "TouchlineFeed",
        "DepthFeed",
        "TouchlineAck",
        "TouchlineAck",
        "TouchlineFeed",
    ]
    assert got[0].last_price == 3 and got[1].key == "NSE|23" and got[-1].last_price == 4
    assert buffer.merged_count == 1


async def test_buffer_get_waits_for_put_and_end():
    buffer = DeliveryBuffer(10)
    waiting = asyncio.ensure_future(buffer.get())
    await asyncio.sleep(0.01)
    assert not waiting.done()
    buffer.put(om(7))
    assert (await asyncio.wait_for(waiting, 1)).order_id == "7"
    ending = asyncio.ensure_future(buffer.get())
    await asyncio.sleep(0.01)
    buffer.end()
    with pytest.raises(StreamEnded):
        await asyncio.wait_for(ending, 1)


async def test_buffer_cancelled_get_does_not_lose_messages():
    buffer = DeliveryBuffer(10)
    waiting = asyncio.ensure_future(buffer.get())
    await asyncio.sleep(0.01)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    buffer.put(om(1))
    assert (await asyncio.wait_for(buffer.get(), 1)).order_id == "1"


async def test_buffer_logs_one_warning_when_merging_starts_and_one_when_recovered(caplog):
    caplog.set_level(logging.WARNING, logger="tradesmart.stream")
    buffer = DeliveryBuffer(4)
    for n in range(4):
        buffer.put(om(n))
    buffer.put(tf(lp="1"))  # appended (not yet pending when the backlog filled)
    for price in range(2, 6):
        buffer.put(tf(lp=str(price)))
    assert buffer.merged_count == 4
    await drain(buffer, len(buffer))
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2
    assert "merging" in warnings[0] and "caught up" in warnings[1]


async def test_buffer_drain_returns_everything_in_order():
    buffer = DeliveryBuffer(1)
    buffer.put(tf(lp="1"))
    buffer.put(tf(lp="2"))
    buffer.put(om(1))
    got = buffer.drain()
    assert [type(m) for m in got] == [TouchlineFeed, OrderUpdate]
    assert got[0].last_price == 2 and len(buffer) == 0


# --- review 2026-11-10: a merge never moves a tick in front of an ack (Important 1)


def df(key: str = "NSE|22", **fields: str) -> DepthFeed:
    exchange, token = key.split("|")
    return DepthFeed.from_wire({"t": "df", "e": exchange, "tk": token, **fields})


def tk(key: str = "NSE|22", **fields: str) -> TouchlineAck:
    exchange, token = key.split("|")
    return TouchlineAck.from_wire({"t": "tk", "e": exchange, "tk": token, **fields})


def dk(key: str = "NSE|22", **fields: str) -> DepthAck:
    exchange, token = key.split("|")
    return DepthAck.from_wire({"t": "dk", "e": exchange, "tk": token, **fields})


def final_state(messages: list) -> dict:
    """Wire fields per instrument a consumer builds by applying ``messages`` in order."""
    cache = SnapshotCache()
    for message in messages:
        cache.update(message.raw)
    return {key: cache.get(key) for key in {m.key for m in messages}}


def final_last_price(messages: list, key: str) -> object:
    """The ``lp`` a consumer applying the messages in order ends up with for ``key``."""
    latest = None
    for message in messages:
        if getattr(message, "key", None) == key and "lp" in message.raw:
            latest = message.last_price
    return latest


async def test_buffer_touchline_feed_after_ack_is_not_merged_in_front_of_it():
    buffer = DeliveryBuffer(2)
    for message in (tf("NSE|1", lp="1"), tf(lp="100"), tk(lp="101", v="5"), tf(lp="102")):
        buffer.put(message)
    got = await drain(buffer, len(buffer))
    assert [(m.t, m.last_price) for m in got if m.key == "NSE|22"] == [
        ("tf", 100),
        ("tk", 101),
        ("tf", 102),
    ]
    assert final_last_price(got, "NSE|22") == 102
    assert buffer.merged_count == 0


async def test_buffer_depth_feed_after_ack_is_not_merged_in_front_of_it():
    buffer = DeliveryBuffer(2)
    for message in (df("NSE|1", bp1="1"), df(lp="100"), dk(lp="101"), df(lp="102")):
        buffer.put(message)
    got = await drain(buffer, len(buffer))
    assert [(m.t, m.last_price) for m in got if m.key == "NSE|22"] == [
        ("df", 100),
        ("dk", 101),
        ("df", 102),
    ]
    assert final_last_price(got, "NSE|22") == 102
    assert buffer.merged_count == 0


@pytest.mark.parametrize(
    "feed,between",
    [
        (tf, dk),  # a depth ack carries touchline fields (lp) too
        (df, tk),  # and a depth feed carries lp as well
        (tf, tk),
        (df, dk),
    ],
)
async def test_buffer_never_merges_across_an_ack_for_the_instrument(feed, between):
    buffer = DeliveryBuffer(2)
    for message in (tf("NSE|1", lp="1"), feed(lp="100"), between(lp="101"), feed(lp="102")):
        buffer.put(message)
    got = await drain(buffer, len(buffer))
    assert final_last_price(got, "NSE|22") == 102
    assert buffer.merged_count == 0


async def test_buffer_never_merges_across_an_unknown_message_for_the_instrument():
    buffer = DeliveryBuffer(2)
    unknown = UnknownMessage.from_wire({"t": "zz", "e": "NSE", "tk": "22", "lp": "101"})
    for message in (tf("NSE|1", lp="1"), tf(lp="100"), unknown, tf(lp="102")):
        buffer.put(message)
    got = await drain(buffer, len(buffer))
    assert [m.t for m in got] == ["tf", "tf", "zz", "tf"]
    assert buffer.merged_count == 0


# --- fix round 1 (task review I1): touchline + depth for one instrument still merge


async def test_buffer_merges_across_the_other_feed_type_and_keeps_shared_fields_latest():
    # tf v=1, df v=2, tf v=3: the tf merges into the first entry, and the newer df entry
    # takes v=3 too, so applying the messages in order ends with v=3
    buffer = DeliveryBuffer(2)
    for message in (tf("NSE|1", lp="1"), tf(v="1"), df(v="2", bp2="5"), tf(v="3")):
        buffer.put(message)
    assert len(buffer) == 3 and buffer.merged_count == 1
    got = await drain(buffer, 3)
    assert [m.t for m in got] == ["tf", "tf", "df"]
    assert got[1].volume == 3 and got[2].volume == 3
    assert got[2].raw["bp2"] == "5"
    assert final_state(got)["NSE|22"]["v"] == "3"


async def test_buffer_copies_only_fields_the_newer_entry_type_has():
    # a df merged into an earlier df: the newer tf takes the shared v but never bp2
    buffer = DeliveryBuffer(2)
    for message in (tf("NSE|1", lp="1"), df(v="1", bp2="5"), tf(v="2"), df(v="3", bp2="6")):
        buffer.put(message)
    got = await drain(buffer, len(buffer))
    assert [m.t for m in got] == ["tf", "df", "tf"]
    assert got[1].raw["bp2"] == "6" and got[1].volume == 3
    assert got[2].volume == 3 and "bp2" not in got[2].raw
    assert final_state(got)["NSE|22"] == {"e": "NSE", "tk": "22", "v": "3", "bp2": "6"}


def delivered_order(messages: list, key: str = "NSE|22") -> list:
    return [(m.t, m.raw.get("v")) for m in messages if m.key == key]


async def test_buffer_does_not_merge_past_an_older_entry_of_the_other_type():
    # tf1, df2, df(bp1) waiting: a tf now would have to jump two depth entries, so it is
    # appended and v is delivered 1, 2, 3 (never newer-then-older)
    buffer = DeliveryBuffer(4)
    for message in (tf(v="1"), df(v="2"), df(bp1="9"), tf("NSE|1", lp="1"), tf(v="3")):
        buffer.put(message)
    got = await drain(buffer, len(buffer))
    assert delivered_order(got) == [("tf", "1"), ("df", "2"), ("df", None), ("tf", "3")]
    assert buffer.merged_count == 0


async def test_buffer_never_delivers_new_old_new_for_a_shared_field():
    # re-review N1: df v=2, tf v=4, tf v=6 waiting; a df v=8 must not be merged into the
    # first df (that delivered v as 8, 4, 6/8)
    buffer = DeliveryBuffer(3)
    for message in (df(v="2"), tf(v="4"), tf(v="6"), df(v="8")):
        buffer.put(message)
    got = await drain(buffer, len(buffer))
    assert delivered_order(got) == [("df", "2"), ("tf", "4"), ("tf", "6"), ("df", "8")]


SHARED = ("lp", "v", "bp1", "sp1", "o")


async def run_lagging_consumer(seed: int, *, acks: bool, steps: int = 20000) -> tuple:
    """Random tf/df (and tk/dk) traffic with pops interleaved; every value is its arrival
    sequence number. Returns (delivered, expected snapshot, largest backlog)."""
    rng = random.Random(seed)
    limit, keys = 10, ["NSE|1", "NSE|2", "NFO|3"]
    buffer = DeliveryBuffer(limit)
    expected = SnapshotCache()
    delivered: list = []
    largest = 0
    for seq in range(steps):
        if len(buffer) and rng.random() < 0.45:
            delivered.append(await buffer.get())
            continue
        kinds = [tf, df, tk, dk] if acks else [tf, df]
        weights = [45, 45, 5, 5] if acks else [50, 50]
        make = rng.choices(kinds, weights)[0]
        names = rng.sample(SHARED + (("bp2",) if make in (df, dk) else ()), rng.randint(1, 3))
        message = make(rng.choice(keys), **dict.fromkeys(names, str(seq)))
        expected.update(message.raw)
        buffer.put(message)
        largest = max(largest, len(buffer))
    delivered += await drain(buffer, len(buffer))
    return delivered, expected, largest, len(keys), limit


def assert_never_goes_back(delivered: list) -> None:
    last: dict = {}
    for message in delivered:
        for name, value in message.raw.items():
            if name in ("t", "e", "tk"):
                continue
            field = (message.key, name)
            assert int(value) >= last.get(field, -1), (
                f"{field} went back from {last[field]} to {value} ({message.t})"
            )
            last[field] = int(value)


@pytest.mark.parametrize("seed", range(5))
@pytest.mark.parametrize("acks", [False, True], ids=["feeds", "feeds+acks"])
async def test_buffer_random_lagging_consumer_is_monotonic_bounded_and_ends_latest(seed, acks):
    delivered, expected, largest, instruments, limit = await run_lagging_consumer(seed, acks=acks)
    assert_never_goes_back(delivered)
    state = final_state(delivered)
    for key, fields in state.items():
        assert fields == expected.get(key)
    if not acks:
        assert largest <= limit + 2 * instruments


async def test_buffer_lagging_consumer_with_touchline_and_depth_stays_bounded():
    limit, instruments, rounds = 10, ["NSE|22", "NSE|23", "NFO|35001"], 2000
    buffer = DeliveryBuffer(limit)
    expected = SnapshotCache()
    for n in range(rounds):
        for key in instruments:
            for message in (
                tf(key, lp=str(n), v=str(n), bp1=str(n)),
                df(key, v=str(n + 1), bp1=str(n + 1), bp2=str(n), sq1=str(n)),
            ):
                expected.update(message.raw)
                buffer.put(message)
        assert len(buffer) <= limit + 2 * len(instruments)
    got = await drain(buffer, len(buffer))
    state = final_state(got)
    for key in instruments:
        assert state[key] == expected.get(key)
    assert state["NSE|22"]["v"] == str(rounds)  # the last df's v, sent after the last tf


async def test_buffer_lagging_consumer_with_acks_in_between_ends_with_latest_values():
    buffer = DeliveryBuffer(5)
    expected = SnapshotCache()
    for n in range(300):
        messages = [tf(lp=str(n), v=str(n)), df(v=str(n), bp1=str(n))]
        if n % 50 == 25:  # e.g. the snapshots re-sent after a reconnect
            messages += [tk(lp=str(n), v=str(n)), dk(v=str(n), bp1=str(n))]
        for message in messages:
            expected.update(message.raw)
            buffer.put(message)
    got = await drain(buffer, len(buffer))
    assert final_state(got)["NSE|22"] == expected.get("NSE|22")
    assert len(got) < 5 + 2 + 6 * 4


async def test_buffer_merges_again_after_the_ack():
    buffer = DeliveryBuffer(2)
    for message in (tf("NSE|1", lp="1"), tf(lp="100"), tk(lp="101"), tf(lp="102"), tf(v="7")):
        buffer.put(message)
    got = await drain(buffer, len(buffer))
    assert [m.t for m in got if m.key == "NSE|22"] == ["tf", "tk", "tf"]
    assert got[-1].last_price == 102 and got[-1].volume == 7
    assert buffer.merged_count == 1


async def test_buffer_ack_for_another_instrument_does_not_stop_merging():
    buffer = DeliveryBuffer(2)
    for message in (tf("NSE|1", lp="1"), tf(lp="100"), tk("NSE|1", lp="2"), tf(lp="102")):
        buffer.put(message)
    got = await drain(buffer, len(buffer))
    assert [(m.t, m.key) for m in got] == [("tf", "NSE|1"), ("tf", "NSE|22"), ("tk", "NSE|1")]
    assert got[1].last_price == 102 and buffer.merged_count == 1
