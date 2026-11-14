"""Statistics and reuse-detection logic of scripts/live_probes.py (no network)."""

import importlib.util
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "live_probes.py"


@pytest.fixture(scope="module")
def probes() -> ModuleType:
    spec = importlib.util.spec_from_file_location("live_probes", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["live_probes"] = module
    spec.loader.exec_module(module)
    return module


def test_percentile_interpolates(probes: ModuleType) -> None:
    data = [10.0, 20.0, 30.0, 40.0, 50.0]
    assert probes.percentile(data, 50) == 30.0
    assert probes.percentile(data, 0) == 10.0
    assert probes.percentile(data, 100) == 50.0
    assert probes.percentile(data, 95) == pytest.approx(48.0)
    assert probes.percentile([7.0], 95) == 7.0
    with pytest.raises(ValueError):
        probes.percentile([], 50)


def test_latency_stats(probes: ModuleType) -> None:
    stats = probes.latency_stats([float(x) for x in range(1, 31)])
    assert stats["n"] == 30
    assert stats["p50_ms"] == 15.5
    assert stats["max_ms"] == 30.0
    assert stats["p95_ms"] == pytest.approx(28.55)


class _Stream:
    def __init__(self, addr: object) -> None:
        self.addr = addr

    def get_extra_info(self, name: str) -> object:
        return self.addr


def test_connection_key_uses_local_address_or_identity(probes: ModuleType) -> None:
    assert probes.connection_key(_Stream(("10.0.0.2", 5555))) == "addr:('10.0.0.2', 5555)"
    assert probes.connection_key(_Stream(("10.0.0.2", 5555))) == probes.connection_key(
        _Stream(("10.0.0.2", 5555))
    )
    s = _Stream(None)
    assert probes.connection_key(s) == f"id:{id(s)}"
    assert probes.connection_key(object()).startswith("id:")


def test_reuse_tracker_detects_reuse_and_new_connection(probes: ModuleType) -> None:
    t = probes.ReuseTracker()
    assert t.observe("a", idle_s=0) is False  # first request
    assert t.observe("a", idle_s=30) is True
    assert t.observe("b", idle_s=95) is False
    assert t.observe("b", idle_s=30) is True
    assert [e["first"] for e in t.events] == [True, False, False, False]


def _events(probes: ModuleType, *pairs: tuple[float, str]) -> list[dict[str, Any]]:
    t = probes.ReuseTracker()
    t.observe("c0", idle_s=0)
    for idle, key in pairs:
        t.observe(key, idle_s=idle)
    return list(t.events)


def test_estimate_brackets_timeout(probes: ModuleType) -> None:
    ev = _events(probes, (30, "c0"), (55, "c0"), (65, "c1"), (95, "c2"), (125, "c3"))
    est = probes.estimate_idle_timeout(ev)
    assert est["largest_reused_idle_s"] == 55
    assert est["smallest_dropped_idle_s"] == 65
    assert est["consistent"] is True
    assert "between 55s and 65s" in est["note"]


def test_estimate_all_reused_and_all_dropped(probes: ModuleType) -> None:
    kept = probes.estimate_idle_timeout(_events(probes, (30, "c0"), (125, "c0")))
    assert kept["largest_reused_idle_s"] == 125 and kept["smallest_dropped_idle_s"] is None
    assert "at least" in kept["note"]
    gone = probes.estimate_idle_timeout(_events(probes, (30, "c1"), (55, "c2")))
    assert gone["largest_reused_idle_s"] is None and gone["smallest_dropped_idle_s"] == 30
    assert "below" in gone["note"]
    assert probes.estimate_idle_timeout([])["note"] == "no idle observations"


def test_estimate_flags_inconsistent(probes: ModuleType) -> None:
    est = probes.estimate_idle_timeout(_events(probes, (30, "c1"), (95, "c1")))
    assert est["consistent"] is False
    assert "inconsistent" in est["note"]


def test_parse_waits(probes: ModuleType) -> None:
    assert probes.parse_waits("30, 55,65") == [30.0, 55.0, 65.0]
    for bad in ("", "x", "0,5", "-1"):
        with pytest.raises(Exception, match="--waits"):
            probes.parse_waits(bad)


def test_conditional_headers_prefer_etag(probes: ModuleType) -> None:
    now = datetime(2026, 11, 10, 7, 0, tzinfo=UTC)
    both = {"ETag": '"abc"', "Last-Modified": "Tue, 10 Nov 2026 01:00:00 GMT"}
    assert probes.conditional_request_headers(both, now) == {"If-None-Match": '"abc"'}
    lm = {"ETag": None, "Last-Modified": "Tue, 10 Nov 2026 01:00:00 GMT"}
    assert probes.conditional_request_headers(lm, now) == {
        "If-Modified-Since": "Tue, 10 Nov 2026 01:00:00 GMT"
    }
    assert probes.conditional_request_headers({}, now) == {
        "If-Modified-Since": "Tue, 10 Nov 2026 07:00:00 GMT"
    }


def test_pick_headers_and_summarize_waits(probes: ModuleType) -> None:
    assert probes.pick_headers({"ETag": "x", "Server": "s"}) == {
        "Last-Modified": None,
        "ETag": "x",
        "Cache-Control": None,
    }
    s = probes.summarize_waits([0.0, 0.0005, 0.5, 1.5])
    assert s["calls"] == 4 and s["max_wait_s"] == 1.5 and s["calls_that_waited"] == 2
    assert probes.summarize_waits([])["max_wait_s"] == 0.0


def test_record_appends_json_lines(probes: ModuleType, tmp_path: Path) -> None:
    out = tmp_path / "r" / "probes.jsonl"
    probes.record("latency", {"p50_ms": 1.0}, path=out)
    probes.record("keepalive", {"note": "x"}, path=out)
    rows = [json.loads(line) for line in out.read_text().splitlines()]
    assert [r["probe"] for r in rows] == ["latency", "keepalive"]
    assert rows[0]["p50_ms"] == 1.0 and "ts" in rows[0]


def test_refuses_without_opt_in(
    probes: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("TRADESMART_LIVE_TESTS", raising=False)
    assert probes.main(["--probe", "latency"]) == 2
    assert "TRADESMART_LIVE_TESTS=1" in capsys.readouterr().err


def test_script_makes_only_read_only_calls() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    for call in (".place_order(", ".modify_order(", ".cancel_order(", ".exit_order(", ".request("):
        assert call not in text


# --------------------------------------------------------------------------- review fixes


def test_check_response_ok_401_and_not_ok(probes: ModuleType) -> None:
    assert probes.check_response(200, '{"stat":"Ok","request_time":"x"}') == (True, "")
    ok, why = probes.check_response(401, '{"stat":"Not_Ok","emsg":"Session Expired"}')
    assert not ok and "token expired or refused: Session Expired" in why and "login" in why
    ok, why = probes.check_response(200, '{"stat":"Not_Ok","emsg":"Invalid Input"}')
    assert not ok and "Invalid Input" in why
    ok, why = probes.check_response(502, "<html>bad gateway</html>")
    assert not ok and "502" in why


def _offline_client() -> Any:
    from types import SimpleNamespace

    from tradesmart import ClientConfig

    creds = SimpleNamespace(bearer=lambda: "synthetic-token")
    return SimpleNamespace(client_id="TEST1", config=ClientConfig(), _creds=creds)


def _keepalive_runner(
    probes: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    replies: list[tuple[int, str]],
) -> Any:
    import argparse

    import httpx

    seq = iter(replies)

    def handler(request: httpx.Request) -> httpx.Response:
        status, text = next(seq)
        return httpx.Response(status, text=text)

    monkeypatch.setattr(
        probes,
        "make_keepalive_http",
        lambda expiry: httpx.Client(transport=httpx.MockTransport(handler)),
    )
    monkeypatch.setattr(probes.time, "sleep", lambda s: None)
    monkeypatch.setattr(probes, "RESULTS_FILE", tmp_path / "p.jsonl")
    args = argparse.Namespace(waits=[30.0, 55.0])
    return lambda: probes.probe_keepalive(_offline_client(), args)


def test_keepalive_ok_replies_record_status(
    probes: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ok = (200, '{"stat":"Ok"}')
    _keepalive_runner(probes, monkeypatch, tmp_path, [ok, ok, ok])()
    row = json.loads((tmp_path / "p.jsonl").read_text().splitlines()[0])
    assert row["probe"] == "keepalive"
    assert all(e["http_status"] == 200 and e["stat"] == "Ok" for e in row["events"])


@pytest.mark.parametrize(
    "bad",
    [
        (401, '{"stat":"Not_Ok","emsg":"Session Expired"}', "token expired or refused"),
        (200, '{"stat":"Not_Ok","emsg":"x"}', "call failed"),
    ],
)
def test_keepalive_aborts_on_failed_call_without_estimate(
    probes: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, bad: tuple[int, str, str]
) -> None:
    run = _keepalive_runner(probes, monkeypatch, tmp_path, [(200, '{"stat":"Ok"}'), bad[:2]])
    with pytest.raises(probes.ProbeError, match=bad[2]):
        run()
    assert not (tmp_path / "p.jsonl").exists()  # no estimate recorded from failed calls


def test_main_turns_sdk_and_http_errors_into_one_line(
    probes: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import httpx

    from tradesmart.errors import AuthenticationError, ConfigurationError

    monkeypatch.setenv("TRADESMART_LIVE_TESTS", "1")
    for exc in (
        ConfigurationError("No access token set; run login"),
        AuthenticationError("saved login expired", endpoint="UserDetails", http_status=401),
        httpx.ConnectError("boom"),
    ):

        def fail(*a: Any, _e: Exception = exc, **k: Any) -> Any:
            raise _e

        monkeypatch.setattr(probes.TradeSmart, "from_env", fail)
        assert probes.main(["--probe", "latency"]) != 0
        err = capsys.readouterr().err.strip()
        assert len(err.splitlines()) == 1 and "Traceback" not in err


def test_all_runs_every_probe_and_records_failures(
    probes: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from types import SimpleNamespace

    ran: list[str] = []

    def boom(client: Any, args: Any) -> None:
        raise probes.ProbeError("token expired or refused: x")

    def fine(name: str) -> Any:
        return lambda client, args: ran.append(name)

    monkeypatch.setenv("TRADESMART_LIVE_TESTS", "1")
    monkeypatch.setattr(probes, "RESULTS_FILE", tmp_path / "p.jsonl")
    monkeypatch.setattr(
        probes.TradeSmart, "from_env", lambda **k: SimpleNamespace(close=lambda: None)
    )
    monkeypatch.setitem(probes.PROBES, "latency", fine("latency"))
    monkeypatch.setitem(probes.PROBES, "keepalive", boom)
    monkeypatch.setitem(probes.PROBES, "freshness", fine("freshness"))
    monkeypatch.setitem(probes.PROBES, "ratelimit", fine("ratelimit"))
    assert probes.main(["--probe", "all"]) != 0
    assert ran == ["latency", "freshness", "ratelimit"]
    rows = [json.loads(x) for x in (tmp_path / "p.jsonl").read_text().splitlines()]
    assert len(rows) == 1
    assert rows[0]["probe"] == "keepalive"
    assert rows[0]["error"] == "ProbeError: token expired or refused: x"
    assert "Traceback" not in capsys.readouterr().err


def test_all_ok_exits_zero(probes: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    monkeypatch.setenv("TRADESMART_LIVE_TESTS", "1")
    monkeypatch.setattr(
        probes.TradeSmart, "from_env", lambda **k: SimpleNamespace(close=lambda: None)
    )
    for name in list(probes.PROBES):
        monkeypatch.setitem(probes.PROBES, name, lambda c, a: None)
    assert probes.main(["--probe", "all"]) == 0


def test_rapid_default_is_120(probes: ModuleType) -> None:
    assert probes.build_parser().parse_args(["--probe", "ratelimit"]).rapid == 120


def test_ratelimit_note_when_no_waits(probes: ModuleType) -> None:
    assert "no client-side waits" in probes.ratelimit_note(probes.summarize_waits([0.0, 0.0]))
    assert probes.ratelimit_note(probes.summarize_waits([0.0, 0.5])) == ""


def test_script_master_status_flag(probes: ModuleType) -> None:
    assert probes.status_ok(200) and probes.status_ok(304)
    assert not probes.status_ok(403) and not probes.status_ok(500)
