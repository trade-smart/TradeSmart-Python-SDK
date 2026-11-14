"""Dry run of the UAT matrix family M3 (rejection paths) on the offline broker.

The real family code (``cases_reject``: case table, probe plans, the expired-contract lookup;
``family.run_cases``; the batch engine) runs end to end on a UAT-like market that refuses what
UAT is expected to refuse; every case gets its expected verdict, and a probe UAT accepts by
mistake is cancelled once and recorded "accepted", never a pass.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from tests.integration.uat_family_rig import TODAY, family_rig
from tests.integration.uat_like import UatLikeMarket
from tests.live_orders.uat_matrix import batch, cases, cases_reject, family
from tests.live_orders.uat_matrix.cases import Case
from tradesmart.enums import OrderType, Product, TransactionType
from tradesmart.testing import drills

UNKNOWN = "Rejected : ORA:Invalid Trading Symbol"
EXPIRED_FUTURE = "BANKNIFTY30OCT26F"


@dataclass
class StrictUatMarket(UatLikeMarket):
    """UAT-like, plus the refusals the M3 probes expect (``lenient``: accept them instead)."""

    lenient: bool = False

    def _decide(self, place: Callable[..., str], kwargs: dict[str, Any]) -> str:
        if str(kwargs["tradingsymbol"]) not in self.instruments:
            self.broker.fail_next("PlaceOrder", drills.reject(UNKNOWN))
            return place(**kwargs)
        reason = None if self.lenient else self._probe_refusal(kwargs)
        if reason is None:
            return super()._decide(place, kwargs)
        with self._lock:
            order_id = place(**kwargs)
            self.broker.reject(order_id, reason)
            return order_id

    def _probe_refusal(self, kwargs: dict[str, Any]) -> str | None:
        exchange = str(kwargs["exchange"])
        product = Product(str(kwargs["product"]))
        if exchange in ("NFO", "BFO") and product in (Product.CNC, Product.MTF):
            return f"Rejected : RMS:product {product.name} not allowed on {exchange}"
        if exchange == "NSE" and product is Product.NRML:
            return "Rejected : RMS:product NRML not allowed on NSE"
        buy = TransactionType(str(kwargs["transaction_type"])) is TransactionType.BUY
        stop_buy = OrderType(str(kwargs["order_type"])) is OrderType.SL and buy
        if stop_buy and Decimal(str(kwargs["price"])) < Decimal(str(kwargs["trigger_price"])):
            return "Rejected : ORA:Limit price below trigger price for a stop BUY"
        return None


def _add_expired(market: UatLikeMarket) -> None:
    """Yesterday's BANKNIFTY future, still listed in the master (not tradable)."""
    market.broker.add_instrument(
        "NFO", "499", EXPIRED_FUTURE, lot_size=30, tick_size="0.20", instrument="FUTIDX",
        expiry=TODAY - timedelta(days=14), underlying="BANKNIFTY",
    )  # fmt: skip


SKIPPED = {
    # Options and BFO: no expired contract listed in this master.
    "reject-nfo-nifty-pe-atm-nrml-lmt-day-buy-expired",
    "reject-bfo-sensex-pe-atm-nrml-lmt-day-buy-expired",
}


def expected(case: Case) -> str:
    if case.id in SKIPPED:
        return batch.SKIP
    return batch.RECORD if case.outcome == cases.RECORD else batch.PASS


def _run(rig_run: batch.MatrixRun, selected: list[Case]) -> dict[str, batch.CaseResult]:
    expired = cases_reject.expired_symbols(
        rig_run.session.client, rig_run.contracts, selected, TODAY
    )
    return family.run_cases(rig_run, cases_reject.items(selected, expired=expired))


def test_every_rejection_path_gets_its_expected_verdict(tmp_path: Path) -> None:
    probes = cases_reject.REJECT_CASES
    with family_rig(tmp_path, market_type=StrictUatMarket) as rig:
        _add_expired(rig.market)
        results = _run(rig.run, probes)
        wrong = {
            c.id: (results[c.id].verdict, results[c.id].reason)
            for c in probes
            if results[c.id].verdict != expected(c)
        }
        assert not wrong, wrong
        for case_id in SKIPPED:
            assert "expired" in results[case_id].reason
        sent = rig.requests("PlaceOrder")
        bfo_canary = 1
        assert len(sent) == len(probes) - len(SKIPPED) + bfo_canary  # one place per case
        symbols = {p["tsym"] for p in sent}
        assert EXPIRED_FUTURE in symbols and cases_reject.UNKNOWN_SYMBOL["eq"] in symbols
        assert all(o.status in ("CANCELED", "REJECTED") for o in rig.broker.orders)
        assert len(rig.market.cancels) == len(set(rig.market.cancels))


def test_each_probe_breaks_exactly_its_rule_on_the_wire(tmp_path: Path) -> None:
    with family_rig(tmp_path, market_type=StrictUatMarket) as rig:
        _add_expired(rig.market)
        _run(rig.run, cases_reject.REJECT_CASES)
        by_tag: dict[str, dict[str, Any]] = {p["remarks"]: p for p in rig.requests("PlaceOrder")}
        ledger = rig.session.ledger
        sent = {ledger.case_of(tag): payload for tag, payload in by_tag.items()}
        offtick = sent["reject-nse-e-mis-lmt-day-buy-offtick"]
        assert Decimal(offtick["prc"]) % Decimal("0.05") != 0
        assert sent["reject-nfo-banknifty-f-nrml-lmt-day-buy-lot"]["qty"] == "31"
        assert Decimal(sent["reject-nse-e-mis-lmt-day-buy-below-lc"]["prc"]) < Decimal("2700")
        above = sent["reject-nse-e-mis-lmt-day-sell-above-uc"]
        assert Decimal(above["prc"]) > Decimal("3300") and above["trantype"] == "S"
        assert sent["reject-nfo-banknifty-f-cnc-lmt-day-buy-cnc"]["prd"] == "C"
        assert sent["reject-nse-e-mis-lmt-day-buy-dscqty-low"]["dscqty"] == "1"


def test_smoke_is_six_probes_that_all_pass(tmp_path: Path) -> None:
    smoke = cases.select(cases.SMOKE, cases_reject.REJECT_CASES)
    with family_rig(tmp_path, market_type=StrictUatMarket) as rig:
        results = _run(rig.run, smoke)
        assert len(smoke) == 6
        assert {r.verdict for r in results.values()} == {batch.PASS}


def test_a_probe_accepted_by_mistake_is_cancelled_once_and_recorded_never_a_pass(
    tmp_path: Path,
) -> None:
    probe = next(
        c for c in cases_reject.REJECT_CASES if c.id == "reject-nfo-banknifty-f-cnc-lmt-day-buy-cnc"
    )
    with family_rig(tmp_path, market_type=StrictUatMarket, lenient=True) as rig:
        results = _run(rig.run, [probe])
        result = results[probe.id]
        assert result.verdict == batch.RECORD
        assert result.reason.startswith("accepted (OPEN)")
        [order] = rig.broker.orders
        assert order.status == "CANCELED"
        assert rig.market.cancels == [order.id]


def test_the_expired_lookup_reads_the_master_once_per_exchange(tmp_path: Path) -> None:
    with family_rig(tmp_path, market_type=StrictUatMarket) as rig:
        _add_expired(rig.market)
        found = cases_reject.expired_symbols(
            rig.session.client, rig.run.contracts, cases_reject.REJECT_CASES, TODAY
        )
        assert found == {
            "nfo-banknifty-f": EXPIRED_FUTURE,
            "nfo-nifty-pe-atm": None,
            "bfo-sensex-pe-atm": None,
        }
        assert not rig.requests("PlaceOrder")
