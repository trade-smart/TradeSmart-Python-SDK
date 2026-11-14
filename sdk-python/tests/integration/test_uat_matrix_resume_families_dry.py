"""A resumed UAT matrix run restores every family's market fill and RMS limit from its ledger,
whatever family modules the resuming process imported (offline broker).

Phase 1 (this process): an accepted MKT cover order (M5, ``market order filled``) and an RMS
refusal of the holdings family (M12, ``environment limit``). Phase 2 (a fresh Python process that
imports only ``batch``, ``cases`` and ``family``, never ``cases_cobo`` / ``cases_hold``): the
resumed run knows both, so an M1 MKT case is refused before any place call.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from tests.integration.uat_fill_rig import make_rig
from tests.live_orders.uat_matrix import batch, cases_cobo, cases_hold

SDK_DIR = Path(__file__).resolve().parents[2]
ITC = {"exch": "NSE", "tsym": "ITC-EQ", "token": "1660", "ls": "1", "ti": "0.05", "lp": "400.00"}
M5_MARKET = "cobo-nfo-nifty-f-co-mkt-day-buy"
M12_FAR = "hold-nse-hold-cnc-lmt-day-sell"
M1_MARKET = "shapes-nfo-nifty-f-nrml-mkt-day-buy"

RESUME = r"""
import json, sys
from pathlib import Path

from tests.integration.uat_fill_rig import (
    ACCOUNT, MORNING, RUN_ID, TODAY, FakeTime, add_instruments,
)
from tests.integration.uat_like import UatLikeMarket
from tests.live_orders.uat_matrix import batch, cases, family, gate
from tests.live_orders.uat_matrix.expect import Profile
from tests.live_orders.uat_matrix.guard import Guard
from tests.live_orders.uat_matrix.ledger import LEDGER_NAME, Ledger
from tradesmart.testing import OfflineBroker

FAMILY_MODULES = "tests.live_orders.uat_matrix.cases_"
root = Path(sys.argv[1])
run_dir = root / RUN_ID
t = FakeTime()
with OfflineBroker(ACCOUNT) as broker:
    market = UatLikeMarket(broker, market="accept")
    add_instruments(market)
    client = market.install(broker.client())
    session = gate.MatrixSession(
        client=client, run_id=RUN_ID, trading_day=TODAY, run_dir=run_dir,
        ledger=Ledger(run_dir / LEDGER_NAME, RUN_ID, forbidden=[ACCOUNT]),
        guard=Guard(root / "STOP", clock=t.clock), sleep=t.sleep, clock=t.clock, resumed=True,
    )
    run = batch.open_run(session, contracts=market.contracts(TODAY),
                         profile=Profile(None, None, None), now=lambda: MORNING)
    result = family.run_cases(run, [cases.case(sys.argv[2])])[sys.argv[2]]
    print(json.dumps({
        "loaded": sorted(m for m in sys.modules if m.startswith(FAMILY_MODULES)),
        "market_filled": run.market_filled,
        "rms": sorted(run.issues.rms_families),
        "verdict": result.verdict,
        "reason": result.reason,
        "places": sum(1 for r in broker.requests if r.endpoint == "PlaceOrder"),
    }))
    client.close()
"""


def test_a_resume_importing_no_family_module_keeps_their_market_fill_and_rms(
    tmp_path: Path,
) -> None:
    for rig in make_rig(tmp_path, market="accept", rms={"ITC-EQ"}, profile=None):
        rig.broker.set_holdings([{"exch_tsym": [ITC], "holdqty": "3", "usedqty": "0"}])
        m5 = next(c for c in cases_cobo.family_cases() if c.id == M5_MARKET)
        assert cases_cobo.run_family(rig.run, [m5])[M5_MARKET].verdict == batch.MARKET_FILL
        far = cases_hold.family_cases()[:1]
        assert cases_hold.run_family(rig.run, far)[M12_FAR].verdict == batch.ENV_LIMIT
    done = subprocess.run(  # noqa: S603 - this interpreter, a fixed script, test-made args
        [sys.executable, "-c", RESUME, str(tmp_path), M1_MARKET],
        cwd=SDK_DIR, capture_output=True, text=True, timeout=120, check=False,
    )  # fmt: skip
    assert done.returncode == 0, done.stderr
    seen = json.loads(done.stdout.strip().splitlines()[-1])
    assert seen["loaded"] == []  # no family module was imported by the resuming process
    assert seen["market_filled"] is not None and M5_MARKET in seen["market_filled"]
    assert seen["rms"] == ["hold"]
    assert seen["verdict"] == batch.SKIP and M5_MARKET in seen["reason"]
    assert seen["places"] == 0  # no second market fill
