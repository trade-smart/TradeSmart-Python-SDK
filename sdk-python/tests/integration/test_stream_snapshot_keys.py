"""``snapshot(key)`` normalises the key like subscriptions do (exchange in any case)."""

import tradesmart.stream.async_stream as async_stream_module
from tests.integration.ws_server import MockWsServer, wait_until, wait_until_sync
from tradesmart import ClientConfig
from tradesmart.stream import AsyncTradeSmartStream, TradeSmartStream


def cfg(srv: MockWsServer) -> ClientConfig:
    return ClientConfig(ws_url=srv.url, heartbeat_interval=0.2, ws_auth_timeout=1.0)


async def test_async_snapshot_finds_key_in_any_case():
    async with MockWsServer() as srv:
        stream = AsyncTradeSmartStream("TEST123", "tok", config=cfg(srv))
        async with stream:
            await stream.subscribe_touchline("nse|22")
            await wait_until(lambda: stream.snapshot("NSE|22") is not None)
            assert stream.snapshot("nse|22") == stream.snapshot("NSE|22")
            assert stream.snapshot("Nse|22")["lp"] == "100.50"
            assert stream.snapshot("nse|99") is None


def test_sync_snapshot_finds_key_in_any_case(threaded_server):
    stream = TradeSmartStream("TEST123", "tok", config=cfg(threaded_server))
    with stream:
        stream.subscribe_touchline(["NSE|22"])
        wait_until_sync(lambda: stream.snapshot("NSE|22") is not None)
        assert stream.snapshot("nse|22") == stream.snapshot("NSE|22")
        assert stream.snapshot("nse|22")["lp"] == "100.50"


def test_snapshot_exact_key_skips_normalisation(monkeypatch):
    stream = AsyncTradeSmartStream("TEST123", "tok")
    stream._snapshots.update({"e": "NSE", "tk": "22", "lp": "100.50"})
    seen: list[str] = []
    real = async_stream_module.format_keys

    def spy(key: str) -> list[str]:
        seen.append(key)
        return real(key)

    monkeypatch.setattr(async_stream_module, "format_keys", spy)
    assert stream.snapshot("NSE|22")["lp"] == "100.50"
    assert seen == []  # exact hit: no normalisation
    assert TradeSmartStream("TEST123", "tok", config=None).snapshot("NSE|22") is None
    assert seen == ["NSE|22"]  # a miss is normalised (and validated)
    assert stream.snapshot("nse|22")["lp"] == "100.50"
    assert seen == ["NSE|22", "nse|22"]


def test_snapshot_non_str_keys_skip_the_fast_path():
    stream = AsyncTradeSmartStream("TEST123", "tok")
    stream._snapshots.update({"e": "NSE", "tk": "22", "lp": "100.50"})
    assert stream.snapshot(["nse|22"])["lp"] == "100.50"  # type: ignore[arg-type]  # unhashable
    assert stream.snapshot(("NSE", "22"))["lp"] == "100.50"  # type: ignore[arg-type]
    assert TradeSmartStream("TEST123", "tok").snapshot(["NSE|23"]) is None  # type: ignore[arg-type]
