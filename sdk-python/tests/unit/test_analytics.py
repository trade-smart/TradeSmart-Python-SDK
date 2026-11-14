from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from tradesmart.analytics import (
    Buildup,
    BuildupReading,
    ChainRow,
    MaxPain,
    OptionChainSnapshot,
    buildup_from_snapshot,
    classify_buildup,
    max_pain,
    pcr,
)


def _row(strike: int, typ: str, oi: int | None, vol: int | None = None) -> ChainRow:
    return ChainRow(
        strike=Decimal(strike),
        option_type=typ,
        tradingsymbol=f"X{strike}{typ}",
        token=f"{strike}{typ}",
        last_price=None,
        open_interest=None if oi is None else Decimal(oi),
        volume=None if vol is None else Decimal(vol),
        close=None,
    )


def _snap(rows: list[ChainRow]) -> OptionChainSnapshot:
    return OptionChainSnapshot(
        exchange="NFO",
        tradingsymbol="NIFTY",
        fetched_at=datetime(2026, 11, 10, tzinfo=UTC),
        rows=tuple(rows),
        missing=(),
    )


def test_pcr_by_oi_and_volume() -> None:
    s = _snap(
        [
            _row(100, "CE", 100, 10),
            _row(110, "CE", 200, None),
            _row(100, "PE", 150, 30),
            _row(110, "PE", None, 20),
        ]
    )
    assert pcr(s) == Decimal(150) / Decimal(300)
    assert pcr(s, by="volume") == Decimal(50) / Decimal(10)


def test_pcr_none_when_no_calls() -> None:
    assert pcr(_snap([_row(100, "PE", 5)])) is None


def test_pcr_bad_by() -> None:
    with pytest.raises(ValueError):
        pcr(_snap([]), by="price")


def _five() -> OptionChainSnapshot:
    ce = {100: 50, 110: 40, 120: 30, 130: 20, 140: 10}
    pe = {100: 10, 110: 20, 120: 30, 130: 40, 140: 50}
    rows = [_row(k, "CE", v) for k, v in ce.items()] + [_row(k, "PE", v) for k, v in pe.items()]
    return _snap(rows)


def test_max_pain_hand_computed() -> None:
    # S=120: CE 50*20+40*10=1400, PE 40*10+50*20=1400 -> 2800
    # S=110: CE 50*10=500, PE 30*10+40*20+50*30=2600 -> 3100 (S=130 symmetric)
    mp = max_pain(_five())
    assert isinstance(mp, MaxPain)
    assert mp.strike == Decimal(120)
    assert mp.payouts[Decimal(120)] == Decimal(2800)
    assert mp.payouts[Decimal(110)] == Decimal(3100)
    assert len(mp.payouts) == 5


def test_max_pain_tie_lowest_strike() -> None:
    s = _snap([_row(100, "CE", 10), _row(110, "PE", 10)])
    # S=100: PE 10*10=100; S=110: CE 10*10=100 -> tie -> 100
    mp = max_pain(s)
    assert mp is not None and mp.strike == Decimal(100)


def test_max_pain_none_without_oi() -> None:
    assert max_pain(_snap([_row(100, "CE", None), _row(100, "PE", None)])) is None


def test_classify_all_states() -> None:
    assert classify_buildup(1, 1) is Buildup.LONG_BUILDUP
    assert classify_buildup(Decimal("-1"), 2.0) is Buildup.SHORT_BUILDUP
    assert classify_buildup(-1, -1) is Buildup.LONG_UNWINDING
    assert classify_buildup(1, -1) is Buildup.SHORT_COVERING
    assert classify_buildup(0, 1) is Buildup.NEUTRAL
    assert classify_buildup(1, 0) is Buildup.NEUTRAL
    assert classify_buildup(None, 1) is Buildup.NEUTRAL
    assert classify_buildup(1, None) is Buildup.NEUTRAL


def test_buildup_from_stream_snapshot() -> None:
    r = buildup_from_snapshot({"lp": "105", "c": "100", "oi": "1200", "poi": "1000"})
    assert isinstance(r, BuildupReading)
    assert r.state is Buildup.LONG_BUILDUP
    assert r.price_change == Decimal(5)
    assert r.oi_change == Decimal(200)
    assert r.basis == "previous_close_and_previous_oi"


def test_buildup_from_snapshot_missing_keys() -> None:
    r = buildup_from_snapshot({"lp": "", "oi": "abc"})
    assert r.state is Buildup.NEUTRAL
    assert r.price_change is None and r.oi_change is None


def test_snapshot_methods_delegate() -> None:
    s = _five()
    assert s.pcr() == pcr(s)
    assert s.pcr(by="volume") == pcr(s, by="volume")
    assert s.max_pain() == max_pain(s)


def test_buildup_prints_value() -> None:
    assert f"{Buildup.SHORT_COVERING}" == "short_covering"


@pytest.mark.parametrize(
    ("price", "oi"),
    [
        (float("nan"), 1),
        (1, float("nan")),
        (float("inf"), 1),
        (1, float("-inf")),
        (Decimal("NaN"), Decimal(1)),
        (Decimal(1), Decimal("Infinity")),
        (Decimal("-Infinity"), Decimal("-1")),
    ],
)
def test_classify_buildup_non_finite_is_neutral(price: object, oi: object) -> None:
    assert classify_buildup(price, oi) is Buildup.NEUTRAL  # type: ignore[arg-type]


def test_classify_buildup_huge_int_is_finite() -> None:
    # Python ints are always finite; one too large for a float must not overflow.
    assert classify_buildup(10**400, 1) is Buildup.LONG_BUILDUP
    assert classify_buildup(-1, -(10**400)) is Buildup.LONG_UNWINDING
