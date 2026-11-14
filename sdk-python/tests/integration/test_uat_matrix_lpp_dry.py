"""LPP-band and order-rate refusals are environment limits, neutral to the segment breaker.

Production refused orders with ``17070: The Price is out of the current LPP range.`` (the order
book, after an acknowledged place) and with ``Order Recieved 202 in a current minute exceeds
Limit 200 for user`` (the REST reply). Both finish ``ENV_LIMIT`` and neither adds to nor resets the
segment's unexpected-rejection count; neither skips the rest of the family.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.integration.uat_family_rig import family_rig
from tests.live_orders.uat_matrix import batch, cases, family
from tests.live_orders.uat_matrix.cases import Case
from tradesmart.testing import drills

LPP = "17070: The Price is out of the current LPP range."
RATE = "Invalid Input :  Order Recieved 202 in a current minute exceeds Limit 200 for user"
REAL = "Rejected : ORA:something the case did not expect"
SHAPES = [c for c in cases.CASES if c.family == "shapes"]


def _nse_cases() -> list[Case]:
    wanted = [("nse-b", "MIS", "BUY"), ("nse-b", "MIS", "SELL"), ("nse-t", "MIS", "BUY"),
              ("nse-t", "MIS", "SELL")]  # fmt: skip
    return [
        next(
            c
            for c in SHAPES
            if (c.role, c.product, c.side, c.prctyp, c.ret) == (*w, "LMT", "DAY") and not c.canary
        )
        for w in wanted
    ]


def _run_sequence(tmp_path: Path, script: list[str]) -> tuple[list[str], str | None]:
    """The nse cases one by one, the n-th placed order refused with ``script[n]``."""
    verdicts: list[str] = []
    calls: list[str] = []

    def refusal(kwargs: dict[str, Any]) -> str | None:
        calls.append("x")
        return script[len(calls) - 1]

    with family_rig(tmp_path) as rig:
        rig.market.band_refusal = refusal
        for case in _nse_cases()[: len(script)]:
            verdicts.append(family.run_cases(rig.run, [case])[case.id].verdict)
        return verdicts, rig.session.guard.tripped("nse")


def test_an_lpp_refusal_in_the_order_book_is_an_environment_limit(tmp_path: Path) -> None:
    case = _nse_cases()[0]
    with family_rig(tmp_path) as rig:
        rig.market.band_refusal = lambda kwargs: LPP
        result = family.run_cases(rig.run, [case])[case.id]
        assert result.verdict == batch.ENV_LIMIT, result.reason
        assert result.reason.startswith("environment limit (LPP band): ")
        assert LPP in result.reason
        assert "shapes" not in rig.run.issues.rms_families  # the family is not skipped


def test_an_order_rate_refusal_in_the_rest_reply_is_an_environment_limit(tmp_path: Path) -> None:
    case = _nse_cases()[0]
    with family_rig(tmp_path) as rig:
        rig.market.on_place = lambda kwargs: rig.broker.fail_next("PlaceOrder", drills.reject(RATE))
        result = family.run_cases(rig.run, [case])[case.id]
        assert result.verdict == batch.ENV_LIMIT, result.reason
        assert result.reason.startswith("environment limit (order rate): ")
        assert "shapes" not in rig.run.issues.rms_families


def test_the_family_continues_after_an_lpp_refusal(tmp_path: Path) -> None:
    first, second = _nse_cases()[:2]
    with family_rig(tmp_path) as rig:
        buy = ("B", "BUY")
        rig.market.band_refusal = lambda kw: LPP if str(kw["transaction_type"]) in buy else None
        assert family.run_cases(rig.run, [first])[first.id].verdict == batch.ENV_LIMIT
        later = family.run_cases(rig.run, [second])[second.id]
        assert later.verdict != batch.SKIP, later.reason


@pytest.mark.parametrize("message", [LPP, RATE])
def test_an_environment_refusal_between_real_ones_does_not_reset_the_breaker(
    tmp_path: Path, message: str
) -> None:
    verdicts, tripped = _run_sequence(tmp_path, [REAL, REAL, message, REAL])
    assert verdicts[2] == batch.ENV_LIMIT, verdicts
    assert tripped is not None  # real, real, (neutral), real: the third real one trips


@pytest.mark.parametrize("message", [LPP, RATE])
def test_an_environment_refusal_does_not_count_for_the_breaker(
    tmp_path: Path, message: str
) -> None:
    verdicts, tripped = _run_sequence(tmp_path, [REAL, REAL, message])
    assert verdicts[2] == batch.ENV_LIMIT, verdicts
    assert tripped is None  # two real ones and a neutral one are not three
