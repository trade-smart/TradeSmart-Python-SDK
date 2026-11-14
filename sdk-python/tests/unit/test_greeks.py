"""Option Greeks maths (`tradesmart.greeks`): reference values, parity, finite differences,
IV round trip and the expiry-day rules (spec A2, A3, A5)."""

from __future__ import annotations

import dataclasses
import math
from collections.abc import Callable

import pytest

from tradesmart import greeks as g
from tradesmart.greeks import (
    DEFAULT_DIVIDEND_YIELD,
    DEFAULT_RATE,
    MIN_YEARS,
    NEAR_EXPIRY_YEARS,
    Greeks,
    black76,
    black_scholes,
    greeks_from_price,
    implied_volatility,
)

MINUTE = 1 / (365 * 24 * 60)
NUMERIC_FIELDS = ("price", "iv", "delta", "gamma", "theta", "vega", "rho")
# Raw vega x volatility below which a 1e-8 price tolerance cannot pin the volatility to 1e-6
# (the option has practically no time value: deep ITM/OTM with hours or days left).
_MIN_VEGA_TIMES_VOL = 1e-4


def _model(name: str, q: float = 0.0) -> Callable[[float, float, float, float, float, str], Greeks]:
    """Uniform pricing signature (underlying, strike, years, rate, vol, type) for both models."""
    if name == "black76":
        return black76

    def bsm(u: float, k: float, t: float, r: float, v: float, ot: str) -> Greeks:
        return black_scholes(u, k, t, r, v, ot, dividend_yield=q)

    return bsm


def _price(model: Callable[..., Greeks], *args: float | str) -> float:
    result = model(*args).price
    assert result is not None
    return result


def test_constants() -> None:
    assert DEFAULT_RATE == 0.065
    assert DEFAULT_DIVIDEND_YIELD == 0.0
    assert MIN_YEARS == 1 / (365 * 24 * 60)
    assert NEAR_EXPIRY_YEARS == 30 / (365 * 24 * 60)


def test_greeks_is_frozen() -> None:
    result = black76(100, 100, 0.25, 0.065, 0.2, "CE")
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.price = 1.0  # type: ignore[misc]


def test_black_scholes_textbook() -> None:
    # Hull, Options, Futures and Other Derivatives: S=42, K=40, T=0.5, r=10 %, sigma=20 %.
    assert black_scholes(42, 40, 0.5, 0.10, 0.20, "CE").price == pytest.approx(4.76, abs=0.01)
    assert black_scholes(42, 40, 0.5, 0.10, 0.20, "PE").price == pytest.approx(0.81, abs=0.01)


def test_black76_textbook() -> None:
    # Hull: European put on a futures price, F=K=20, T=4 months, r=9 %, sigma=25 % -> 1.12.
    put = black76(20, 20, 4 / 12, 0.09, 0.25, "PE")
    call = black76(20, 20, 4 / 12, 0.09, 0.25, "CE")
    assert put.price == pytest.approx(1.12, abs=0.01)
    assert call.price == pytest.approx(put.price, abs=1e-12)


def test_direct_results_carry_status_iv_and_years() -> None:
    result = black76(100, 100, 0.25, 0.065, 0.2, "CE")
    assert result.status == "ok"
    assert result.iv == 0.2
    assert result.years_to_expiry == 0.25
    assert black_scholes(100, 100, 5 * MINUTE, 0.065, 0.2, "PE").status == "near_expiry"


@pytest.mark.parametrize(
    ("u", "k", "t", "r", "v", "q"),
    [
        (100, 100, 0.25, 0.065, 0.2, 0.0),
        (100, 90, 1.0, 0.05, 0.35, 0.02),
        (19500, 20000, 7 / 365, 0.065, 0.12, 0.01),
        (3.5, 4.0, 0.1, 0.0, 0.8, 0.0),
    ],
)
def test_put_call_parity(u: float, k: float, t: float, r: float, v: float, q: float) -> None:
    c = _price(black_scholes, u, k, t, r, v, "CE", q)
    p = _price(black_scholes, u, k, t, r, v, "PE", q)
    assert c - p == pytest.approx(u * math.exp(-q * t) - k * math.exp(-r * t), abs=1e-9)

    c76 = _price(black76, u, k, t, r, v, "CE")
    p76 = _price(black76, u, k, t, r, v, "PE")
    assert c76 - p76 == pytest.approx(math.exp(-r * t) * (u - k), abs=1e-9)


@pytest.mark.parametrize("model_name", ["black76", "bsm"])
@pytest.mark.parametrize("option_type", ["CE", "PE"])
@pytest.mark.parametrize("strike", [85.0, 100.0, 115.0])
def test_greeks_match_finite_differences(model_name: str, option_type: str, strike: float) -> None:
    model = _model(model_name, q=0.02)
    u, t, r, v = 100.0, 0.25, 0.065, 0.3
    base = model(u, strike, t, r, v, option_type)

    def p(uu: float = u, tt: float = t, rr: float = r, vv: float = v) -> float:
        return _price(model, uu, strike, tt, rr, vv, option_type)

    hu, ht, hr, hv = 1e-3, 1e-5, 1e-5, 1e-5
    delta = (p(uu=u + hu) - p(uu=u - hu)) / (2 * hu)
    gamma = (p(uu=u + hu) - 2 * p() + p(uu=u - hu)) / (hu * hu)
    vega = (p(vv=v + hv) - p(vv=v - hv)) / (2 * hv) * 0.01
    theta = -(p(tt=t + ht) - p(tt=t - ht)) / (2 * ht) / 365
    rho = (p(rr=r + hr) - p(rr=r - hr)) / (2 * hr) * 0.01

    assert base.delta == pytest.approx(delta, rel=1e-4, abs=1e-8)
    assert base.gamma == pytest.approx(gamma, rel=1e-4, abs=1e-8)
    assert base.vega == pytest.approx(vega, rel=1e-4, abs=1e-8)
    assert base.theta == pytest.approx(theta, rel=1e-4, abs=1e-8)
    assert base.rho == pytest.approx(rho, rel=1e-4, abs=1e-8)


def test_black76_rho_is_minus_t_times_price() -> None:
    result = black76(100, 95, 0.5, 0.065, 0.25, "CE")
    assert result.price is not None
    assert result.rho == pytest.approx(-0.5 * result.price * 0.01, rel=1e-12)


@pytest.mark.parametrize("model_name", ["black76", "bsm"])
@pytest.mark.parametrize("option_type", ["CE", "PE"])
@pytest.mark.parametrize("moneyness", [0.8, 1.0, 1.2])
@pytest.mark.parametrize("years", [1 / 365, 7 / 365, 0.5])
@pytest.mark.parametrize("vol", [0.1, 0.3, 1.0])
def test_iv_round_trip(
    model_name: str, option_type: str, moneyness: float, years: float, vol: float
) -> None:
    q = 0.01 if model_name == "bsm" else 0.0
    model = _model(model_name, q=q)
    underlying, strike, rate = 100.0 * moneyness, 100.0, 0.065
    priced = model(underlying, strike, years, rate, vol, option_type)
    assert priced.price is not None and priced.vega is not None
    if priced.vega * 100 * vol < _MIN_VEGA_TIMES_VOL:
        # The price barely moves with volatility (deep ITM/OTM, hours to expiry): a 1e-8 price
        # tolerance cannot pin the volatility to 1e-6. Only the price must round-trip.
        iv = implied_volatility(
            priced.price,
            underlying,
            strike,
            years,
            rate,
            option_type,
            model=model_name,
            dividend_yield=q,
        )
        if priced.price > 0:
            assert iv is not None
        if iv is not None:
            assert _price(model, underlying, strike, years, rate, iv, option_type) == pytest.approx(
                priced.price, abs=1e-8
            )
        return
    iv = implied_volatility(
        priced.price,
        underlying,
        strike,
        years,
        rate,
        option_type,
        model=model_name,
        dividend_yield=q,
    )
    assert iv == pytest.approx(vol, rel=1e-6)


def test_iv_round_trip_covers_most_cases() -> None:
    """Guard that the vega filter above only skips options with practically no time value:
    every at-the-money and every half-year case is checked strictly."""
    checked = 0
    for name in ("black76", "bsm"):
        model = _model(name, q=0.01 if name == "bsm" else 0.0)
        for ot in ("CE", "PE"):
            for m in (0.8, 1.0, 1.2):
                for t in (1 / 365, 7 / 365, 0.5):
                    for v in (0.1, 0.3, 1.0):
                        vega = model(100 * m, 100, t, 0.065, v, ot).vega
                        assert vega is not None
                        strict = vega * 100 * v >= _MIN_VEGA_TIMES_VOL
                        if m == 1.0 or t == 0.5:
                            assert strict, (name, ot, m, t, v)
                        checked += strict
    assert checked >= 76


def test_iv_none_when_no_volatility_reproduces_price() -> None:
    # Above the maximum Black-76 call value (discounted forward) and below the minimum time value.
    assert implied_volatility(150, 100, 100, 0.25, 0.065, "CE") is None
    assert implied_volatility(0.0, 100, 100, 0.25, 0.065, "CE") is None
    assert implied_volatility(-1.0, 100, 100, 0.25, 0.065, "CE") is None
    assert implied_volatility(5.0, 110, 100, 0.25, 0.065, "CE") is None  # below intrinsic
    assert implied_volatility(5.0, 100, 100, 0.0, 0.065, "CE") is None  # expired
    assert implied_volatility(5.0, 100, 100, -0.1, 0.065, "CE") is None


def test_iv_bsm_uses_dividend_yield() -> None:
    price = _price(black_scholes, 100, 100, 0.5, 0.065, 0.25, "CE", 0.03)
    iv = implied_volatility(price, 100, 100, 0.5, 0.065, "CE", model="bsm", dividend_yield=0.03)
    assert iv == pytest.approx(0.25, rel=1e-6)
    iv_wrong_q = implied_volatility(price, 100, 100, 0.5, 0.065, "CE", model="bsm")
    assert iv_wrong_q is not None and iv_wrong_q != pytest.approx(0.25, rel=1e-3)


def test_bad_model_name() -> None:
    with pytest.raises(ValueError, match="model"):
        implied_volatility(5.0, 100, 100, 0.25, 0.065, "CE", model="binomial")
    with pytest.raises(ValueError, match="model"):
        greeks_from_price(5.0, 100, 100, 0.25, 0.065, "CE", model="binomial")


@pytest.mark.parametrize("model_name", ["black76", "bsm"])
def test_expired_returns_intrinsic(model_name: str) -> None:
    call = greeks_from_price(5, 105, 100, 0.0, 0.065, "CE", model=model_name)
    assert call == Greeks(
        price=5.0,
        iv=None,
        delta=1.0,
        gamma=0.0,
        theta=0.0,
        vega=0.0,
        rho=0.0,
        status="expired",
        years_to_expiry=0.0,
    )

    put_otm = greeks_from_price(0.05, 105, 100, 0.0, 0.065, "PE", model=model_name)
    assert put_otm.price == 0.0
    assert put_otm.delta == 0.0
    assert put_otm.status == "expired"

    put_itm = greeks_from_price(4, 96, 100, 0.0, 0.065, "PE", model=model_name)
    assert put_itm.price == 4.0
    assert put_itm.delta == -1.0

    call_otm = greeks_from_price(0.05, 96, 100, 0.0, 0.065, "CE", model=model_name)
    assert (call_otm.price, call_otm.delta) == (0.0, 0.0)

    assert greeks_from_price(1, 100, 100, 0.0, 0.065, "CE", model=model_name).delta == 0.5
    assert greeks_from_price(1, 100, 100, 0.0, 0.065, "PE", model=model_name).delta == -0.5

    negative = greeks_from_price(5, 105, 100, -0.01, 0.065, "CE", model=model_name)
    assert dataclasses.replace(negative, years_to_expiry=0.0) == call
    assert negative.years_to_expiry == -0.01


def test_expired_does_not_need_option_price() -> None:
    result = greeks_from_price(None, 105, 100, 0.0, 0.065, "CE")
    assert (result.status, result.price, result.delta) == ("expired", 5.0, 1.0)


def test_direct_model_at_expiry() -> None:
    for model in (black76, black_scholes):
        result = model(105, 100, 0.0, 0.065, 0.2, "CE")
        assert (result.status, result.price, result.delta, result.gamma) == (
            "expired",
            5.0,
            1.0,
            0.0,
        )
        assert result.iv is None


@pytest.mark.parametrize("model_name", ["black76", "bsm"])
def test_near_expiry_floor_and_status(model_name: str) -> None:
    model = _model(model_name)
    ten_minutes = 10 * MINUTE
    price = _price(model, 100, 100, ten_minutes, 0.065, 0.2, "CE")
    result = greeks_from_price(price, 100, 100, ten_minutes, 0.065, "CE", model=model_name)
    assert result.status == "near_expiry"
    assert result.years_to_expiry == ten_minutes
    for name in NUMERIC_FIELDS:
        value = getattr(result, name)
        assert value is not None and math.isfinite(value), name
    assert result.iv == pytest.approx(0.2, rel=1e-6)

    floor_price = _price(model, 100, 100, MIN_YEARS, 0.065, 0.2, "CE")
    half_minute = greeks_from_price(
        floor_price, 100, 100, 0.5 * MINUTE, 0.065, "CE", model=model_name
    )
    at_floor = greeks_from_price(floor_price, 100, 100, MIN_YEARS, 0.065, "CE", model=model_name)
    assert half_minute.years_to_expiry == 0.5 * MINUTE
    assert dataclasses.replace(half_minute, years_to_expiry=MIN_YEARS) == at_floor
    assert at_floor.status == "near_expiry"

    direct_half = model(100, 100, 0.5 * MINUTE, 0.065, 0.2, "CE")
    direct_floor = model(100, 100, MIN_YEARS, 0.065, 0.2, "CE")
    assert dataclasses.replace(direct_half, years_to_expiry=MIN_YEARS) == direct_floor


def test_status_ok_beyond_thirty_minutes() -> None:
    years = 31 * MINUTE
    price = _price(black76, 100, 100, years, 0.065, 0.2, "PE")
    result = greeks_from_price(price, 100, 100, years, 0.065, "PE")
    assert result.status == "ok"
    assert result.price == price
    assert result.iv == pytest.approx(0.2, rel=1e-6)


@pytest.mark.parametrize("model_name", ["black76", "bsm"])
def test_price_at_or_below_intrinsic(model_name: str) -> None:
    result = greeks_from_price(9.9, 110, 100, 7 / 365, 0.065, "CE", model=model_name)
    assert result == Greeks(
        price=9.9,
        iv=None,
        delta=1.0,
        gamma=0.0,
        theta=0.0,
        vega=0.0,
        rho=0.0,
        status="price_at_or_below_intrinsic",
        years_to_expiry=7 / 365,
    )
    # Below both K e^{-rT} - F e^{-rT} (Black-76) and K e^{-rT} - S (BSM) for S/F=90, K=100.
    put = greeks_from_price(9.8, 90, 100, 7 / 365, 0.065, "PE", model=model_name)
    assert (put.status, put.delta, put.iv) == ("price_at_or_below_intrinsic", -1.0, None)
    otm = greeks_from_price(0.02, 90, 100, 7 / 365, 0.065, "CE", model=model_name, tick_size=0.05)
    assert (otm.status, otm.delta) == ("price_at_or_below_intrinsic", 0.0)


def test_price_within_half_tick_of_intrinsic() -> None:
    years, rate = 7 / 365, 0.065
    discounted = math.exp(-rate * years) * 10  # Black-76 discounted intrinsic, F=110, K=100
    just_above = discounted + 0.02
    no_tick = greeks_from_price(just_above, 110, 100, years, rate, "CE")
    assert no_tick.status == "ok"
    assert no_tick.iv is not None
    within_tick = greeks_from_price(just_above, 110, 100, years, rate, "CE", tick_size=0.05)
    assert within_tick.status == "price_at_or_below_intrinsic"
    assert within_tick.price == just_above


def test_bsm_intrinsic_uses_dividend_and_discounting() -> None:
    # S e^{-qT} - K e^{-rT} for S=110, K=100, T=0.5: about 7.83 with q=10 %, 13.2 with q=0.
    years, rate, q = 0.5, 0.065, 0.10
    intrinsic = 110 * math.exp(-q * years) - 100 * math.exp(-rate * years)
    price = intrinsic + 0.5
    result = greeks_from_price(price, 110, 100, years, rate, "CE", model="bsm", dividend_yield=q)
    assert result.status == "ok"
    assert result.iv is not None
    without_q = greeks_from_price(price, 110, 100, years, rate, "CE", model="bsm")
    assert without_q.status == "price_at_or_below_intrinsic"


def test_unsolvable_price_above_maximum() -> None:
    result = greeks_from_price(150, 100, 100, 0.25, 0.065, "CE")
    assert result.status == "price_at_or_below_intrinsic"
    assert result.iv is None
    assert result.delta == 0.5


@pytest.mark.parametrize(
    ("option_price", "underlying"),
    [(None, 100.0), (0.0, 100.0), (-1.0, 100.0), (5.0, None), (5.0, 0.0), (math.nan, 100.0)],
)
def test_no_price(option_price: float | None, underlying: float | None) -> None:
    result = greeks_from_price(option_price, underlying, 100, 0.25, 0.065, "CE")
    assert result.status == "no_price"
    assert result.years_to_expiry == 0.25
    for name in NUMERIC_FIELDS:
        assert getattr(result, name) is None, name


def test_no_underlying_when_expired_is_no_price() -> None:
    assert greeks_from_price(5, None, 100, 0.0, 0.065, "CE").status == "no_price"


def _assert_finite_or_none(result: Greeks) -> None:
    for name in NUMERIC_FIELDS:
        value = getattr(result, name)
        assert value is None or math.isfinite(value), (name, result)
    assert math.isfinite(result.years_to_expiry)


def test_never_nan_or_inf() -> None:
    for model_name in ("black76", "bsm"):
        model = _model(model_name, q=0.02)
        for ot in ("CE", "PE"):
            for m in (0.5, 0.8, 1.0, 1.25, 2.0):
                for t in (0.0, 1e-9, 1 / 365 / 24 / 60, 1 / 365, 0.25, 1.0):
                    for v in (0.001, 0.2, 5.0):
                        for r in (0.0, 0.065):
                            u = 100.0 * m
                            direct = model(u, 100.0, t, r, v, ot)
                            _assert_finite_or_none(direct)
                            for price in (direct.price, 1e-6, 0.05, u * 0.5):
                                _assert_finite_or_none(
                                    greeks_from_price(
                                        price,
                                        u,
                                        100.0,
                                        t,
                                        r,
                                        ot,
                                        model=model_name,
                                        dividend_yield=0.02,
                                        tick_size=0.05,
                                    )
                                )


def test_extreme_inputs_stay_finite() -> None:
    _assert_finite_or_none(black76(1e6, 1.0, 1e-12, 0.065, 1e-6, "CE"))
    _assert_finite_or_none(black76(1.0, 1e6, 1e-12, 0.065, 1e-6, "PE"))
    _assert_finite_or_none(black_scholes(100, 100, 50.0, 0.065, 5.0, "CE", 0.0))


@pytest.mark.parametrize("option_type", ["ce", " CE ", "Call", "CALL"])
def test_call_aliases(option_type: str) -> None:
    assert black76(100, 100, 0.25, 0.065, 0.2, option_type) == black76(
        100, 100, 0.25, 0.065, 0.2, "CE"
    )


@pytest.mark.parametrize("option_type", ["pe", "Put", "PUT"])
def test_put_aliases(option_type: str) -> None:
    assert black76(100, 100, 0.25, 0.065, 0.2, option_type) == black76(
        100, 100, 0.25, 0.065, 0.2, "PE"
    )


@pytest.mark.parametrize("option_type", ["XX", "", "FUT", "C"])
def test_bad_option_type(option_type: str) -> None:
    with pytest.raises(ValueError, match="option_type"):
        black76(100, 100, 0.25, 0.065, 0.2, option_type)
    with pytest.raises(ValueError, match="option_type"):
        black_scholes(100, 100, 0.25, 0.065, 0.2, option_type)
    with pytest.raises(ValueError, match="option_type"):
        implied_volatility(5.0, 100, 100, 0.25, 0.065, option_type)
    with pytest.raises(ValueError, match="option_type"):
        greeks_from_price(5.0, 100, 100, 0.25, 0.065, option_type)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"forward": 0.0},
        {"forward": -1.0},
        {"strike": 0.0},
        {"volatility": 0.0},
        {"volatility": math.nan},
        {"rate": math.inf},
        {"years": math.nan},
    ],
)
def test_direct_models_reject_invalid_inputs(kwargs: dict[str, float]) -> None:
    args: dict[str, float | str] = {
        "forward": 100.0,
        "strike": 100.0,
        "years": 0.25,
        "rate": 0.065,
        "volatility": 0.2,
        "option_type": "CE",
    }
    args.update(kwargs)
    with pytest.raises(ValueError):
        black76(**args)  # type: ignore[arg-type]


def test_greeks_from_price_rejects_bad_strike() -> None:
    with pytest.raises(ValueError, match="strike"):
        greeks_from_price(5.0, 100, 0.0, 0.25, 0.065, "CE")


def test_module_all() -> None:
    assert set(g.__all__) == {
        "DEFAULT_DIVIDEND_YIELD",
        "DEFAULT_RATE",
        "MIN_YEARS",
        "NEAR_EXPIRY_YEARS",
        "Greeks",
        "OptionGreeks",
        "black76",
        "black_scholes",
        "greeks_from_price",
        "implied_volatility",
    }


def test_implied_volatility_polish_step_never_worsens_the_reprice() -> None:
    # Final review C5: a polishing Newton step that re-prices outside the tolerance is dropped.
    forward, strike, years = 1382.9825364707337, 987.2391214529914, 0.0001518957394038944
    p = black_scholes(forward, strike, years, 0.0, 4.5818546609108415, "CE").price
    assert p is not None

    iv = implied_volatility(p, forward, strike, years, 0.0, "CE", model="bsm")

    assert iv is not None
    repriced = black_scholes(forward, strike, years, 0.0, iv, "CE").price
    assert repriced == pytest.approx(p, abs=1e-8)
