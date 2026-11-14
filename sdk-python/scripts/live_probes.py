"""Read-only measurement probes against the live TradeSmart API (developer script, not shipped).

Probes: latency, keepalive (server idle timeout, F6), freshness (script-master headers, F10),
ratelimit (client-side limiter wait). Only profile / quote / script-master GET calls are made.

    TRADESMART_LIVE_TESTS=1 docker compose run --rm dev python scripts/live_probes.py --probe all

Each probe prints a short summary and appends one JSON line to ``.live-results/probes.jsonl``.
Tokens, keys and Authorization headers are never printed or recorded.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from tradesmart import ClientConfig, TradeSmart
from tradesmart._transport.codec import encode_jdata
from tradesmart.config import header_user_agent
from tradesmart.endpoints.user import user_details_call
from tradesmart.errors import TradeSmartError
from tradesmart.instruments import script_master_url

RESULTS_FILE = Path(__file__).resolve().parents[1] / ".live-results" / "probes.jsonl"
DEFAULT_WAITS = (30, 55, 65, 95, 125)
FRESHNESS_EXCHANGES = ("NSE", "NFO", "MCX")
FRESHNESS_HEADERS = ("Last-Modified", "ETag", "Cache-Control")

# --------------------------------------------------------------------------- pure helpers


def percentile(values: Sequence[float], pct: float) -> float:
    """Linear-interpolated percentile (``pct`` in 0..100) of a non-empty sample."""
    if not values:
        raise ValueError("percentile of an empty sample")
    if not 0 <= pct <= 100:
        raise ValueError("pct must be between 0 and 100")
    ordered = sorted(values)
    pos = (len(ordered) - 1) * pct / 100.0
    low = int(pos)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (pos - low)


def latency_stats(samples_ms: Sequence[float]) -> dict[str, float]:
    """p50 / p95 / max / mean (ms) of ``samples_ms``."""
    return {
        "n": float(len(samples_ms)),
        "p50_ms": round(percentile(samples_ms, 50), 2),
        "p95_ms": round(percentile(samples_ms, 95), 2),
        "max_ms": round(max(samples_ms), 2),
        "mean_ms": round(statistics.fmean(samples_ms), 2),
    }


def connection_key(stream: Any) -> str:
    """Stable identity of one TCP connection from httpx's ``network_stream`` extension.

    The local ``(ip, port)`` is unique while the socket lives; the object id is the fallback
    (callers keep the stream referenced, so ids are not recycled during a probe).
    """
    try:
        addr = stream.get_extra_info("client_addr")
    except Exception:  # a missing/odd stream must not break the probe  # noqa: BLE001
        addr = None
    return f"addr:{addr}" if addr else f"id:{id(stream)}"


class ReuseTracker:
    """Records, per request, whether the TCP connection is the same as the previous one."""

    def __init__(self) -> None:
        self._last: str | None = None
        self.events: list[dict[str, Any]] = []

    def observe(self, key: str, *, idle_s: float) -> bool:
        """Register a request on connection ``key`` after ``idle_s`` idle; True if reused."""
        reused = self._last is not None and key == self._last
        self.events.append({"idle_s": idle_s, "reused": reused, "first": self._last is None})
        self._last = key
        return reused


def estimate_idle_timeout(events: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Estimate the server idle timeout from ``{"idle_s", "reused"}`` observations.

    ``largest_reused_idle_s`` is the largest idle time with the connection still reused;
    ``smallest_dropped_idle_s`` the smallest idle time after which a new one was opened. The
    timeout lies in between (when both are known and ordered). The first request has no
    predecessor and is ignored.
    """
    timed = [e for e in events if not e.get("first")]
    reused = [float(e["idle_s"]) for e in timed if e["reused"]]
    dropped = [float(e["idle_s"]) for e in timed if not e["reused"]]
    largest = max(reused) if reused else None
    smallest = min(dropped) if dropped else None
    consistent = largest is None or smallest is None or largest < smallest
    if not timed:
        note = "no idle observations"
    elif smallest is None and largest is not None:
        note = f"connection still reused after {largest:g}s: timeout is at least that"
    elif largest is None and smallest is not None:
        note = f"connection already dropped after {smallest:g}s: timeout is below that"
    elif consistent:
        note = f"server idle timeout between {largest:g}s and {smallest:g}s"
    else:
        note = "inconsistent observations (reuse after a drop); repeat the probe"
    return {
        "largest_reused_idle_s": largest,
        "smallest_dropped_idle_s": smallest,
        "consistent": consistent,
        "note": note,
    }


def parse_waits(text: str) -> list[float]:
    """Parse ``--waits 30,55,65`` into positive floats."""
    try:
        waits = [float(part) for part in text.split(",") if part.strip()]
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--waits must be comma-separated numbers") from exc
    if not waits or any(w <= 0 for w in waits):
        raise argparse.ArgumentTypeError("--waits needs one or more positive numbers")
    return waits


def pick_headers(headers: Any) -> dict[str, str | None]:
    """The freshness-relevant response headers (``None`` when absent)."""
    return {name: headers.get(name) for name in FRESHNESS_HEADERS}


def conditional_request_headers(seen: dict[str, str | None], now: datetime) -> dict[str, str]:
    """Validators to send back: ``If-None-Match`` from ETag, ``If-Modified-Since`` otherwise."""
    if seen.get("ETag"):
        return {"If-None-Match": str(seen["ETag"])}
    if seen.get("Last-Modified"):
        return {"If-Modified-Since": str(seen["Last-Modified"])}
    # No validator offered: try the current time, so a supporting server answers 304.
    return {"If-Modified-Since": now.strftime("%a, %d %b %Y %H:%M:%S GMT")}


def summarize_waits(waits_s: Sequence[float]) -> dict[str, float]:
    """Total / max / count of limiter waits (seconds)."""
    return {
        "calls": float(len(waits_s)),
        "total_wait_s": round(sum(waits_s), 4),
        "max_wait_s": round(max(waits_s), 4) if waits_s else 0.0,
        "calls_that_waited": float(sum(1 for w in waits_s if w > 0.001)),
    }


class ProbeError(Exception):
    """A probe cannot produce a trustworthy result (message is one line, token-free)."""


def parse_stat(text: str) -> tuple[str | None, str | None]:
    """``(stat, emsg)`` from a JSON reply body; ``(None, None)`` when it is not a JSON object."""
    try:
        data = json.loads(text)
    except ValueError:
        return None, None
    if not isinstance(data, dict):
        return None, None
    stat, emsg = data.get("stat"), data.get("emsg")
    return (str(stat) if stat is not None else None), (str(emsg) if emsg is not None else None)


def check_response(status: int, text: str) -> tuple[bool, str]:
    """``(True, "")`` for HTTP 200 with ``stat == "Ok"``; else ``(False, one-line reason)``."""
    stat, emsg = parse_stat(text)
    if status == 200 and stat == "Ok":
        return True, ""
    detail = emsg or f"HTTP {status}, stat={stat}"
    if status in (401, 403) or (emsg and "session" in emsg.lower()) or "token" in detail.lower():
        return False, f"token expired or refused: {detail}; run the login command again"
    return False, f"call failed: HTTP {status}, stat={stat}, emsg={emsg}"


def status_ok(status: int) -> bool:
    """A script-master status is expected to be 200 (download) or 304 (not modified)."""
    return status in (200, 304)


def ratelimit_note(summary: dict[str, float]) -> str:
    """A hint when the limiter never waited (the probe then showed nothing about pacing)."""
    if summary["calls_that_waited"] == 0:
        return "no client-side waits occurred; raise --rapid above 100 to see the limiter pace"
    return ""


def make_keepalive_http(expiry: float) -> httpx.Client:
    """Raw single-connection httpx client whose own keep-alive never expires first."""
    return httpx.Client(
        limits=httpx.Limits(max_connections=1, max_keepalive_connections=1, keepalive_expiry=expiry)
    )


# --------------------------------------------------------------------------- output


def record(probe: str, data: dict[str, Any], path: Path | None = None) -> None:
    """Append one JSON line (never secrets) to the results file."""
    path = path if path is not None else RESULTS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    line = {"ts": datetime.now(UTC).isoformat(timespec="seconds"), "probe": probe, **data}
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(line, default=str) + "\n")


def say(text: str) -> None:
    print(text, flush=True)


# --------------------------------------------------------------------------- probes


def resolve_token(client: TradeSmart, exchange: str, symbol: str) -> str:
    """Token of ``symbol`` on ``exchange`` via ``search_instruments`` (exact match preferred)."""
    matches = [m for m in client.search_instruments(symbol) if m.exchange == exchange and m.token]
    if not matches:
        raise ProbeError(f"no {exchange} instrument found for {symbol!r}; try --symbol")
    exact = [m for m in matches if m.tradingsymbol == symbol]
    token = (exact or matches)[0].token
    if token is None:
        raise ProbeError("instrument has no token")
    return token


def _timed(fn: Callable[[], Any]) -> float:
    start = time.perf_counter()
    fn()
    return (time.perf_counter() - start) * 1000.0


def probe_latency(client: TradeSmart, args: argparse.Namespace) -> None:
    token = resolve_token(client, args.exchange, args.symbol)
    cold = _timed(lambda: client.quote(args.exchange, token))  # includes TCP+TLS connect
    samples = [_timed(lambda: client.quote(args.exchange, token)) for _ in range(args.count)]
    stats = latency_stats(samples)
    say(f"latency {args.exchange}:{args.symbol} first (cold-connect) call {cold:.0f} ms")
    say(
        f"latency {int(stats['n'])} warm calls: p50 {stats['p50_ms']} ms, "
        f"p95 {stats['p95_ms']} ms, max {stats['max_ms']} ms"
    )
    record(
        "latency",
        {"exchange": args.exchange, "symbol": args.symbol, "cold_ms": round(cold, 2), **stats},
    )


def probe_keepalive(client: TradeSmart, args: argparse.Namespace) -> None:
    """Raw httpx client (own pool, expiry above every wait): the SDK's 50 s expiry plays no part."""
    config = client.config
    expiry = max(args.waits) + 120.0
    http = make_keepalive_http(expiry)
    call = user_details_call(client.client_id)  # profile: read-only
    url = config.base_url + call.spec.path
    body = encode_jdata(call.payload)
    tracker = ReuseTracker()
    streams: list[Any] = []  # keep references so object ids are never recycled

    def one_call(idle_s: float) -> bool:
        headers = {
            "User-Agent": header_user_agent(config),
            "Authorization": f"Bearer {client._creds.bearer()}",
        }
        response = http.post(url, content=body, headers=headers, timeout=config.read_timeout)
        ok, why = check_response(response.status_code, response.text)
        if not ok:  # never estimate a timeout from failed calls
            raise ProbeError(why)
        stream = response.extensions.get("network_stream")
        streams.append(stream)
        reused = tracker.observe(connection_key(stream), idle_s=idle_s)
        tracker.events[-1].update(
            http_status=response.status_code, stat=parse_stat(response.text)[0]
        )
        return reused

    try:
        one_call(0.0)
        say(f"keepalive: waits {[f'{w:g}' for w in args.waits]} s (total ~{sum(args.waits):.0f} s)")
        for wait in args.waits:
            time.sleep(wait)
            reused = one_call(wait)
            say(f"keepalive idle {wait:g}s -> {'same connection' if reused else 'NEW connection'}")
    finally:
        http.close()
    est = estimate_idle_timeout(tracker.events)
    say(f"keepalive: {est['note']} (config.keepalive_expiry={config.keepalive_expiry:g}s)")
    record(
        "keepalive",
        {"events": tracker.events, "configured_keepalive_expiry_s": config.keepalive_expiry, **est},
    )


def probe_freshness(client: TradeSmart, args: argparse.Namespace) -> None:
    config = client.config
    base = {"User-Agent": header_user_agent(config)}
    results: dict[str, Any] = {}
    with httpx.Client(timeout=config.read_timeout, follow_redirects=False) as http:
        for exch in FRESHNESS_EXCHANGES:
            url = script_master_url(exch, base_url=config.script_master_base_url)
            with http.stream("GET", url, headers=base) as response:  # headers only: body unread
                status = response.status_code
                seen = pick_headers(response.headers)
            cond = conditional_request_headers(seen, datetime.now(UTC))
            with http.stream("GET", url, headers={**base, **cond}) as response:
                cond_status = response.status_code
            ok = status_ok(status) and status_ok(cond_status)
            results[exch] = {
                "status": status,
                **seen,
                "conditional_sent": sorted(cond),
                "conditional_status": cond_status,
                "ok": ok,
            }
            flag = "" if ok else "  ** UNEXPECTED STATUS (expected 200 or 304) **"
            say(
                f"freshness {exch}: {status} Last-Modified={seen['Last-Modified']} "
                f"ETag={seen['ETag']} Cache-Control={seen['Cache-Control']}; "
                f"conditional({', '.join(cond)}) -> {cond_status}{flag}"
            )
    record("freshness", {"exchanges": results})


def probe_ratelimit(client: TradeSmart, args: argparse.Namespace) -> None:
    """Observe only: the client's own limiter paces the calls, nothing here bypasses it."""
    token = resolve_token(client, args.exchange, args.symbol)
    registry = client._transport._limiter
    waits: list[float] = []
    real_acquire = registry.acquire

    def timed_acquire(group: Any, **kwargs: Any) -> None:
        start = time.perf_counter()
        real_acquire(group, **kwargs)
        waits.append(time.perf_counter() - start)

    registry.acquire = timed_acquire  # type: ignore[method-assign]
    try:
        started = time.perf_counter()
        for _ in range(args.rapid):
            client.quote(args.exchange, token)
        elapsed = time.perf_counter() - started
    finally:
        registry.acquire = real_acquire  # type: ignore[method-assign]
    summary = summarize_waits(waits)
    say(
        f"ratelimit: {args.rapid} rapid quote() calls in {elapsed:.2f}s; limiter waited "
        f"{summary['total_wait_s']}s in total (max {summary['max_wait_s']}s, "
        f"{int(summary['calls_that_waited'])} calls waited); documented limit 100/s, 1000/min"
    )
    note = ratelimit_note(summary)
    if note:
        say(f"ratelimit: {note}")
    record(
        "ratelimit",
        {"rapid_calls": args.rapid, "elapsed_s": round(elapsed, 3), "note": note, **summary},
    )


PROBES: dict[str, Callable[[TradeSmart, argparse.Namespace], None]] = {
    "latency": probe_latency,
    "keepalive": probe_keepalive,
    "freshness": probe_freshness,
    "ratelimit": probe_ratelimit,
}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Read-only live measurement probes.")
    p.add_argument("--probe", required=True, choices=[*PROBES, "all"])
    p.add_argument("--exchange", default="NSE", help="exchange for quote probes (default NSE)")
    p.add_argument("--symbol", default="RELIANCE-EQ", help="trading symbol to quote")
    p.add_argument(
        "--waits",
        type=parse_waits,
        default=list(DEFAULT_WAITS),
        help="keepalive idle waits in seconds, comma-separated (default 30,55,65,95,125)",
    )
    p.add_argument("--count", type=int, default=30, help="warm quote calls for latency")
    p.add_argument(
        "--rapid", type=int, default=120, help="rapid quote calls for ratelimit (limit is 100/s)"
    )
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if os.environ.get("TRADESMART_LIVE_TESTS") != "1":
        print("refusing to run: set TRADESMART_LIVE_TESTS=1 (live API)", file=sys.stderr)
        return 2
    base_url = os.environ.get("TRADESMART_BASE_URL", ClientConfig().base_url)
    client: TradeSmart | None = None
    failed = 0
    try:
        client = TradeSmart.from_env(config=ClientConfig(base_url=base_url))
        for name in PROBES if args.probe == "all" else [args.probe]:
            try:
                PROBES[name](client, args)
            except (TradeSmartError, httpx.HTTPError, ProbeError) as exc:
                failed += 1
                message = f"{type(exc).__name__}: {exc}"
                print(f"probe {name} failed: {message}", file=sys.stderr)
                record(name, {"error": message})
    except (TradeSmartError, httpx.HTTPError) as exc:
        print(f"cannot start: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        if client is not None:
            client.close()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
