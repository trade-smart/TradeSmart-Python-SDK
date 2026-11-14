"""The run's single market path is claimed in the ledger before a MKT / SL-MKT order is sent
(final review I1; offline broker).

- A process killed after its market order went out and before the case's verdict: the resumed
  run still knows the market path is used (the ``market_claim`` line), counts the pending fill
  against the instrument's position ceiling, and sends no second market order.
- A market place with an unknown outcome (CHECK THE ACCOUNT) keeps the claim: another segment's
  market case in the same batch is skipped.
- A definite refusal (ALGO_CHK) or nothing sent releases the claim (``market_release``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.integration.uat_family_rig import family_rig
from tests.integration.uat_fill_rig import ACCOUNT, MORNING, RUN_ID, TODAY, FakeTime, make_rig
from tests.live_orders.uat_matrix import batch, cases, family, gate
from tests.live_orders.uat_matrix.expect import Profile
from tests.live_orders.uat_matrix.guard import Guard
from tests.live_orders.uat_matrix.ledger import LEDGER_NAME, Ledger
from tradesmart.testing import drills

NSE_MARKET = "shapes-nse-b-mis-mkt-day-buy"
NFO_MARKET = "shapes-nfo-nifty-f-nrml-mkt-day-buy"


def _lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]


def _resumed(rig: Any, root: Path) -> batch.MatrixRun:
    """A second process of the same run on the same broker (resume)."""
    run_dir = root / RUN_ID
    t = FakeTime()
    session = gate.MatrixSession(
        client=rig.session.client,
        run_id=RUN_ID,
        trading_day=TODAY,
        run_dir=run_dir,
        ledger=Ledger(run_dir / LEDGER_NAME, RUN_ID, forbidden=[ACCOUNT]),
        guard=Guard(root / "STOP2", clock=t.clock),
        sleep=t.sleep,
        clock=t.clock,
        resumed=True,
    )
    return batch.open_run(
        session,
        contracts=rig.market.contracts(TODAY),
        profile=Profile(None, None, None),
        now=lambda: MORNING,
    )


def _killed_after_the_place(monkeypatch: pytest.MonkeyPatch) -> None:
    def killed(*args: Any, **kwargs: Any) -> None:
        raise KeyboardInterrupt  # the process dies after the place reply, before the verdict

    monkeypatch.setattr(batch, "_market_settle", killed)


def test_a_resume_after_a_kill_mid_round_sends_no_second_market_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for rig in make_rig(tmp_path, market="accept", profile=None):
        with monkeypatch.context() as patch:
            _killed_after_the_place(patch)
            with pytest.raises(KeyboardInterrupt):
                batch.run_batch(rig.run, [cases.case(NSE_MARKET)])
        placed = rig.requests("PlaceOrder")
        assert placed == 1
        assert rig.session.ledger.done(NSE_MARKET) is None  # no verdict was written
        run = _resumed(rig, tmp_path)
        assert run.market_filled is not None and NSE_MARKET in run.market_filled
        result = family.run_cases(run, [cases.case(NFO_MARKET)])[NFO_MARKET]
        assert result.verdict == batch.SKIP and NSE_MARKET in result.reason
        assert rig.requests("PlaceOrder") == placed  # no second market order
        # The pending market fill counts against the instrument's ceiling until it is known.
        assert run.positions.net("ITC-EQ") == 1


def test_the_claim_is_written_before_the_market_order_is_sent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[list[str]] = []
    for rig in make_rig(tmp_path, market="accept", profile=None):
        ledger_path = rig.session.ledger.path

        def on_place(kwargs: dict[str, Any], ledger_path: Path = ledger_path) -> None:
            seen.append([x["type"] for x in _lines(ledger_path)])

        rig.market.on_place = on_place
        [result] = batch.run_batch(rig.run, [cases.case(NSE_MARKET)])
        assert result.verdict == batch.MARKET_FILL
        assert seen and "market_claim" in seen[0]
        assert rig.run.positions.leftovers() == {"ITC-EQ": 1}  # counted once, not twice


def test_an_unknown_market_outcome_keeps_the_claim_for_every_segment(tmp_path: Path) -> None:
    with family_rig(tmp_path, market="accept") as rig:
        first = [True]

        def first_unknown(kwargs: dict[str, Any]) -> None:
            if first[0]:
                first[0] = False
                rig.broker.fail_next("PlaceOrder", drills.connect_error())

        rig.market.on_place = first_unknown
        results = batch.run_batch(rig.run, [cases.case(NSE_MARKET), cases.case(NFO_MARKET)])
        verdicts = {r.case_id: r for r in results}
        assert verdicts[NSE_MARKET].verdict == batch.CHECK
        assert verdicts[NFO_MARKET].verdict == batch.SKIP
        assert NSE_MARKET in verdicts[NFO_MARKET].reason
        assert all(p["exch"] != "NFO" for p in rig.requests("PlaceOrder"))  # no NFO market order
        assert rig.run.market_filled is not None
        types = [x["type"] for x in _lines(rig.session.ledger.path)]
        assert "market_claim" in types and "market_release" not in types


def test_a_definite_refusal_releases_the_claim(tmp_path: Path) -> None:
    for rig in make_rig(tmp_path, profile=None):  # UAT as observed: MKT refused (ALGO_CHK)
        results = batch.run_batch(rig.run, [cases.case(NSE_MARKET), cases.case(NFO_MARKET)])
        assert [r.verdict for r in results] == [batch.PASS, batch.PASS]
        assert rig.requests("PlaceOrder") == 2
        assert rig.run.market_filled is None
        assert rig.run.positions.leftovers() == {}
        types = [x["type"] for x in _lines(rig.session.ledger.path)]
        assert types.count("market_claim") == 2 and types.count("market_release") == 2
        assert _resumed(rig, tmp_path).market_filled is None  # a resume sees it released


def test_a_market_order_refused_before_sending_releases_the_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for rig in make_rig(tmp_path, market="accept", profile=None):

        def not_sent(session: Any, plan: Any, ref: Any, **kwargs: Any) -> Any:
            from tests.live_orders.safety import Refusal

            raise Refusal("refused before anything was sent")

        monkeypatch.setattr(batch.calls, "place", not_sent)
        [result] = batch.run_batch(rig.run, [cases.case(NSE_MARKET)])
        assert result.verdict == batch.SKIP
        assert rig.run.market_filled is None
        assert rig.run.positions.leftovers() == {}


def test_a_claimed_market_order_cancelled_unfilled_is_corrected_in_the_position_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.unit.test_uat_matrix_safety import _order

    for rig in make_rig(tmp_path, market="accept", profile=None):
        original = batch._market_settle

        def cancelled_unfilled(
            run: Any, work: Any, order_id: str, original: Any = original
        ) -> None:
            original(run, work, order_id)
            work.settled[order_id] = _order(order_id, "CANCELED", fillshares="0")

        monkeypatch.setattr(batch, "_market_settle", cancelled_unfilled)
        [result] = batch.run_batch(rig.run, [cases.case(NSE_MARKET)])
        assert result.verdict == batch.MARKET_FILL
        assert rig.run.positions.leftovers() == {}  # the claimed lot is taken back
        assert rig.run.market_filled is not None  # the market path stays used
