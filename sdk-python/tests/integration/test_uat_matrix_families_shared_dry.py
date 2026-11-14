"""Several UAT matrix families on the session's one run (``family.run_for``), offline.

M1 (a market case), M12, M4 and M5 run one after the other on the same ``MatrixRun``, as the
live modules do in one pytest session: one BFO canary for the session (the M1 canary item reports
its result), the accepted-market flag and the position ledger shared, one pacer.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.integration.uat_family_rig import family_rig
from tests.live_orders.uat_matrix import batch, cases, cases_cobo, cases_fills, cases_hold, family

BFO_CANARY = "shapes-bfo-sensex-f-nrml-lmt-day-buy-canary"
M1_MARKET = "shapes-nse-b-mis-mkt-day-buy"
M5_MARKET = "cobo-nse-b-co-mkt-day-buy"
ITC = {"exch": "NSE", "tsym": "ITC-EQ", "token": "1660", "ls": "1", "ti": "0.05", "lp": "400.00"}


def test_m1_m12_m4_and_m5_share_one_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with family_rig(tmp_path, market="accept") as rig:
        opened: list[Any] = []

        def open_run(session: Any, **_: Any) -> batch.MatrixRun:
            opened.append(session)
            return rig.run

        monkeypatch.setattr(batch, "open_run", open_run)
        rig.broker.set_holdings([{"exch_tsym": [ITC], "holdqty": "3", "usedqty": "0"}])
        run = family.run_for(rig.session)
        m1 = family.run_cases(run, [cases.case(M1_MARKET)])
        assert m1[M1_MARKET].verdict == batch.MARKET_FILL
        hold = cases_hold.run_family(family.run_for(rig.session), cases_hold.family_cases())
        fills = cases_fills.run_family(family.run_for(rig.session), cases_fills.family_cases())
        cobo = cases_cobo.run_family(family.run_for(rig.session), cases_cobo.family_cases())
        canary = family.run_cases(family.run_for(rig.session), [cases.case(BFO_CANARY)])
        # One run for the session (contracts, oracle, records, positions, pacer).
        assert len(opened) == 1 and family.run_for(rig.session) is run
        assert family.run_for(rig.session).pacer is run.pacer  # one pacer for the session
        # One BFO canary for the session, placed by M4; the M1 canary item reports it.
        places = [
            json.loads(line) for line in rig.ledger_path().read_text(encoding="utf-8").splitlines()
        ]
        intents = [p for p in places if p.get("type") == "intent" and p.get("kind") == "place"]
        canary_places = [p for p in intents if p.get("case_id") == BFO_CANARY]
        assert len(canary_places) == 1
        assert canary[BFO_CANARY].verdict == batch.PASS
        bfo_fill = "fills-bfo-sensex-ce-atm-mis-lmt-day-buy"
        assert fills[bfo_fill].verdict == batch.XFAIL, fills[bfo_fill].reason
        # The accepted M1 market order counts for the whole run: M5's market case skips.
        assert run.market_filled is not None
        assert cobo[M5_MARKET].verdict == batch.SKIP and M1_MARKET in cobo[M5_MARKET].reason
        # One position ledger: M1's ITC fill and M4's ITC fill share the 2-lot ceiling.
        assert run.positions.net("ITC-EQ") == 2
        itc = [fills["fills-nse-b-cnc-lmt-day-buy"], fills["fills-nse-b-mis-lmt-day-buy"]]
        assert sorted(r.verdict for r in itc) == [batch.SKIP, batch.XFAIL]
        assert any("ceiling" in r.reason for r in itc)
        # The holdings sale stays out of the position ledger.
        assert hold["hold-nse-hold-cnc-lmt-day-sell"].verdict == batch.PASS
        # A family's follow-up result replaces the engine's in the run's results.
        fill = "cobo-nse-t-co-lmt-day-buy-fill"
        again = family.run_cases(run, [next(c for c in cases_cobo.family_cases() if c.id == fill)])
        assert again[fill].reason == cobo[fill].reason and "exit" in again[fill].reason
