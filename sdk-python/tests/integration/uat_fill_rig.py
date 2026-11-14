"""A dry-run rig for the UAT matrix families M4 fills, M5 cover / bracket and M12 holdings.

A real ``MatrixSession`` (ledger, guard, STOP file) in ``tmp_path`` over an ``OfflineBroker`` that
plays UAT as observed (``uat_like.UatLikeMarket``: a BUY at the last price fills, a SELL does
not, ALGO_CHK for MKT / SL-MKT); the engine's waits run on a fake clock, its pacing on the real
one. Test helper only: no network, no live call.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from tests.integration.uat_like import UatLikeMarket
from tests.live_orders.uat_matrix import batch, gate
from tests.live_orders.uat_matrix.expect import Profile
from tests.live_orders.uat_matrix.guard import Guard
from tests.live_orders.uat_matrix.ledger import LEDGER_NAME, Ledger
from tradesmart.models.base import IST
from tradesmart.testing import OfflineBroker

TODAY = date(2026, 11, 13)
MORNING = datetime(2026, 11, 13, 10, 0, tzinfo=IST)
LATE = datetime(2026, 11, 13, 15, 30, tzinfo=IST)
EXPIRY = TODAY + timedelta(days=14)
RUN_ID = "1113fam"
ACCOUNT = "UAT01"
UNKNOWN_PROFILE = Profile(None, None, None)


class FakeTime:
    def __init__(self) -> None:
        self.now = 0.0

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


@dataclass
class Rig:
    broker: OfflineBroker
    market: UatLikeMarket
    session: gate.MatrixSession
    run: batch.MatrixRun
    root: Path

    def requests(self, endpoint: str) -> int:
        return sum(1 for r in self.broker.requests if r.endpoint == endpoint)

    def payloads(self, endpoint: str) -> list[dict[str, Any]]:
        return [dict(r.payload) for r in self.broker.requests if r.endpoint == endpoint]

    def records(self) -> list[dict[str, Any]]:
        path = self.session.run_dir / "records.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def record(self, case_id: str, supplement: str | None = None) -> dict[str, Any]:
        found = [
            r
            for r in self.records()
            if r["case_id"] == case_id and r.get("supplement") == supplement
        ]
        assert found, (case_id, supplement)
        return found[-1]

    def ledger_lines(self) -> list[dict[str, Any]]:
        path = self.session.run_dir / LEDGER_NAME
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def add_instruments(market: UatLikeMarket) -> None:
    """The roles the M4, M5 and M12 families use, with synthetic prices."""
    market.add("NSE", "1660", "ITC-EQ", last="400.00", lower="320.00", upper="480.00",
               role="nse-b", instrument="EQ")  # fmt: skip
    market.add("NSE", "11915", "YESBANK-EQ", last="20.00", tick="0.01", lower="16.00",
               upper="24.00", role="nse-t", instrument="EQ")  # fmt: skip
    market.add(
        "NFO", "500", "NIFTYFUT", last="26040.00", tick="0.10", lot=65, role="nfo-nifty-f",
        kind="fut", instrument="FUTIDX", expiry=EXPIRY, underlying="NIFTY",
    )  # fmt: skip
    market.add(
        "NFO", "510", "BANKNIFTYFUT", last="52000.00", tick="0.20", lot=30,
        role="nfo-banknifty-f", kind="fut", instrument="FUTIDX", expiry=EXPIRY,
        underlying="BANKNIFTY",
    )  # fmt: skip
    market.add(
        "NFO", "601", "NIFTYC26000", last="180.00", lot=65, role="nfo-nifty-ce-atm", kind="opt",
        instrument="OPTIDX", expiry=EXPIRY, strike=26000, option_type="CE", underlying="NIFTY",
    )  # fmt: skip
    market.add(
        "NFO", "700", "RELIANCEFUT", last="1500.00", lot=500, role="nfo-stk-f", kind="fut",
        instrument="FUTSTK", expiry=EXPIRY, underlying="RELIANCE",
    )  # fmt: skip
    market.add(
        "BFO", "800", "BSXFUT", last="85000.00", tick="0.05", lot=20, role="bfo-sensex-f",
        kind="fut", instrument="FUTIDX", expiry=EXPIRY, underlying="BSXFUT",
    )  # fmt: skip
    market.add(
        "BFO", "801", "BSXC85000", last="300.00", lot=20, role="bfo-sensex-ce-atm", kind="opt",
        instrument="OPTIDX", expiry=EXPIRY, strike=85000, option_type="CE", underlying="BSXOPT",
    )  # fmt: skip


def make_rig(
    tmp_path: Path,
    *,
    now: datetime = MORNING,
    profile: Profile | None = UNKNOWN_PROFILE,
    market_class: type[UatLikeMarket] = UatLikeMarket,
    uat: bool = False,
    **market_options: Any,
) -> Iterator[Rig]:
    """A rig (a generator: use ``for rig in make_rig(...)`` or ``yield from``); ``profile=None``
    reads the offline broker account's profile (every product enabled on every exchange)."""
    t = FakeTime()
    with OfflineBroker(ACCOUNT) as broker:
        market = market_class(broker, **market_options)
        add_instruments(market)
        client = market.install(broker.client())
        client._transport.use_time(t.clock, t.sleep)  # the SDK order limiter on fake time (D8)
        run_dir = tmp_path / RUN_ID
        run_dir.mkdir()
        session = gate.MatrixSession(
            client=client,
            run_id=RUN_ID,
            trading_day=TODAY,
            run_dir=run_dir,
            ledger=Ledger(run_dir / LEDGER_NAME, RUN_ID, forbidden=[ACCOUNT]),
            guard=Guard(tmp_path / "STOP", clock=t.clock),
            sleep=t.sleep,
            clock=t.clock,
            uat=uat,
        )
        run = batch.open_run(
            session, contracts=market.contracts(TODAY), profile=profile, now=lambda: now
        )
        try:
            yield Rig(broker, market, session, run, tmp_path)
        finally:
            client.close()
