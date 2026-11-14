"""A dry-run rig for the UAT matrix family modules on the offline broker (test helper).

``family_rig(tmp_path)`` builds a real ``MatrixSession`` (ledger, guard, STOP file) in
``tmp_path`` over an ``OfflineBroker`` that plays UAT as observed (``uat_like.UatLikeMarket``, or a
subclass with more rules), with one instrument for every role the families M1, M3, M10 and M11
use, and a ``MatrixRun`` whose waits and pacing run on a fake clock (no real sleeping).
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from tests.integration.uat_like import UatLikeMarket, hold_every_stock
from tests.live_orders.uat_matrix import batch, gate
from tests.live_orders.uat_matrix.contracts import Contracts
from tests.live_orders.uat_matrix.expect import Profile
from tests.live_orders.uat_matrix.guard import Guard
from tests.live_orders.uat_matrix.ledger import LEDGER_NAME, Ledger
from tradesmart.models.base import IST
from tradesmart.testing import OfflineBroker

TODAY = date(2026, 11, 13)
MORNING = datetime(2026, 11, 13, 10, 0, tzinfo=IST)
EXPIRY = TODAY + timedelta(days=14)
RUN_ID = "1113fam0"
ACCOUNT = "UAT01"
RUNG_STEPS = {"atm": 0, "itm1": -1, "itm2": -2, "otm1": 1, "otm2": 2}  # strikes from ATM


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

    def requests(self, endpoint: str) -> list[dict[str, Any]]:
        return [dict(r.payload) for r in self.broker.requests if r.endpoint == endpoint]

    def records(self) -> list[dict[str, Any]]:
        path = self.session.run_dir / "records.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def ledger_path(self) -> Path:
        return self.session.run_dir / LEDGER_NAME


def _option(
    market: UatLikeMarket,
    exchange: str,
    token: str,
    name: str,
    role: str,
    *,
    last: str,
    lot: int,
    strike: int,
    option_type: str,
    underlying: str,
) -> None:
    market.add(
        exchange, token, name, last=last, lot=lot, role=role, kind="opt",
        instrument="OPTIDX", expiry=EXPIRY, strike=strike, option_type=option_type,
        underlying=underlying,
    )  # fmt: skip


def add_instruments(market: UatLikeMarket) -> None:
    """One instrument per role of M1, M3, M10 and M11 (prices are synthetic)."""
    market.add("NSE", "1660", "ITC-EQ", last="400.00", lower="320.00", upper="480.00",
               role="nse-b", instrument="EQ")  # fmt: skip
    market.add("NSE", "11915", "YESBANK-EQ", last="20.00", tick="0.01", lower="16.00",
               upper="24.00", role="nse-t", instrument="EQ")  # fmt: skip
    market.add("NSE", "2031", "M&M-EQ", last="3000.00", lower="2700.00", upper="3300.00",
               role="nse-e", instrument="EQ")  # fmt: skip
    market.add(
        "NFO", "500", "NIFTY25NOV26F", last="26040.00", tick="0.10", lot=65, role="nfo-nifty-f",
        kind="fut", instrument="FUTIDX", expiry=EXPIRY, underlying="NIFTY",
    )  # fmt: skip
    market.add(
        "NFO", "501", "BANKNIFTY25NOV26F", last="52000.00", tick="0.20", lot=30,
        role="nfo-banknifty-f", kind="fut", instrument="FUTIDX", expiry=EXPIRY,
        underlying="BANKNIFTY",
    )  # fmt: skip
    market.add(
        "NFO", "502", "RELIANCE25NOV26F", last="1400.00", lot=500, role="nfo-stk-f",
        kind="fut", instrument="FUTSTK", expiry=EXPIRY, underlying="RELIANCE",
    )  # fmt: skip
    token = 600
    for index, name in (("NIFTY", "nifty"), ("BANKNIFTY", "banknifty")):
        lot = 65 if index == "NIFTY" else 30
        base = 26000 if index == "NIFTY" else 52000
        step = 50 if index == "NIFTY" else 100
        for option_type in ("CE", "PE"):
            sign = 1 if option_type == "CE" else -1  # a CE is in the money below the future
            for rung, steps in RUNG_STEPS.items():
                token += 1
                strike = base + step * steps * sign
                _option(
                    market, "NFO", str(token), f"{index}{strike}{option_type}",
                    f"nfo-{name}-{option_type.lower()}-{rung}", last=f"{150 - steps * 20}.00",
                    lot=lot, strike=strike, option_type=option_type, underlying=index,
                )  # fmt: skip
    _option(market, "NFO", "700", "RELIANCE1400CE", "nfo-stk-ce-atm", last="30.00", lot=500,
            strike=1400, option_type="CE", underlying="RELIANCE")  # fmt: skip
    market.add(
        "BFO", "800", "BSXFUT27NOV26", last="85000.00", tick="0.05", lot=20,
        role="bfo-sensex-f", kind="fut", instrument="FUTIDX", expiry=EXPIRY, underlying="BSXFUT",
    )  # fmt: skip
    _option(market, "BFO", "801", "BSX85000CE", "bfo-sensex-ce-atm", last="400.00", lot=20,
            strike=85000, option_type="CE", underlying="BSXOPT")  # fmt: skip
    _option(market, "BFO", "802", "BSX85000PE", "bfo-sensex-pe-atm", last="380.00", lot=20,
            strike=85000, option_type="PE", underlying="BSXOPT")  # fmt: skip


def _underlying(role: str) -> str:
    if role.startswith("bfo-sensex"):
        return "BSXFUT" if role.endswith("-f") else "BSXOPT"
    for prefix, name in (("nfo-banknifty", "BANKNIFTY"), ("nfo-nifty", "NIFTY")):
        if role.startswith(prefix):
            return name
    return "RELIANCE"


def contracts_of(market: UatLikeMarket) -> Contracts:
    """The market's contracts with the script-master ``Symbol`` and option type a real
    resolution records (``UatLikeMarket.contracts`` leaves them at the trading symbol / None)."""
    result = market.contracts(TODAY)
    for role, contract in list(result.contracts.items()):
        if contract.kind == "eq":
            continue
        option_type = "CE" if "-ce-" in role else "PE" if "-pe-" in role else None
        result.contracts[role] = dataclasses.replace(
            contract, symbol=_underlying(role), option_type=option_type, expiry=EXPIRY
        )
    return result


@contextmanager
def family_rig(
    tmp_path: Path,
    *,
    market_type: type[UatLikeMarket] = UatLikeMarket,
    profile: Profile | None = None,
    max_open: int = 50,
    now: datetime = MORNING,
    run_id: str = RUN_ID,
    uat: bool = False,
    simulated_order_clock: bool = True,
    pace_per_minute: int = batch.ORDER_PLACES_PER_MINUTE,
    **market_options: Any,
) -> Iterator[Rig]:
    t = FakeTime()
    with OfflineBroker(ACCOUNT) as broker:
        market = market_type(broker, **market_options)
        add_instruments(market)
        hold_every_stock(market)  # CNC SELL cases need holdings (a test may clear them)
        client = market.install(broker.client())
        # D8: the SDK order limiter (200/min) runs on the rig's fake time (no real waiting);
        # a test that measures real-time pacing (the burst cases) passes simulated_order_clock=False
        if simulated_order_clock:
            client._transport.use_time(t.clock, t.sleep)
        run_dir = tmp_path / run_id
        run_dir.mkdir(parents=True)
        session = gate.MatrixSession(
            client=client,
            run_id=run_id,
            trading_day=TODAY,
            run_dir=run_dir,
            ledger=Ledger(run_dir / LEDGER_NAME, run_id, forbidden=[ACCOUNT]),
            guard=Guard(tmp_path / "STOP", max_open=max_open, clock=t.clock),
            sleep=t.sleep,
            clock=t.clock,
            uat=uat,
        )
        run = batch.open_run(
            session,
            contracts=contracts_of(market),
            profile=profile if profile is not None else Profile(None, None, None),
            now=lambda: now,
        )
        run.pacer = batch.Pacer(clock=t.clock, sleep=t.sleep, per_minute=pace_per_minute)
        try:
            yield Rig(broker, market, session, run, tmp_path)
        finally:
            client.close()
