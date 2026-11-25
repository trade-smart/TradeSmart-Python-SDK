"""Option Greeks and implied volatility (estimates computed by the SDK).

Two European models, stdlib ``math`` only:

* **Black-76** on the underlying future's price (the default; ``dividend_yield`` does not enter
  the model because a futures price already reflects dividends);
* **Black-Scholes-Merton** on spot with a continuous annual ``dividend_yield``.

``option_greeks()`` on the clients also uses ``dividend_yield`` before Black-76 when it brings a
future of another expiry to the option's expiry: ``F · e^{−(rate − dividend_yield)·(Tf − To)}``
(not on MCX, where the option is on the future itself).

Conventions: ``years`` is calendar time in years of 365 days; ``rate`` and ``dividend_yield`` are
annual, continuously compounded (``0.065`` = 6.5 %); ``theta`` is per calendar day, ``vega`` per
1 volatility point (0.01) and ``rho`` per 1 % rate (0.01). Black-76 rho holds the future price
fixed (``-years * price``).

Expiry-day safety: time to expiry is floored at one minute for the formulas (``years_to_expiry``
still reports the real value), ``d1``/``d2`` are clamped, and any non-finite value becomes
``None`` -- results never contain NaN or infinity. Every result carries a ``status``:
``"ok"``, ``"near_expiry"`` (under 30 minutes left), ``"expired"``,
``"price_at_or_below_intrinsic"`` or ``"no_price"``. ``"price_at_or_below_intrinsic"`` also
covers a price ABOVE what any volatility up to 500 % gives (typically a stale last price on
expiry day); ``iv`` is ``None`` in both cases. Numbers may be ``float``, ``int`` or ``Decimal``.

These are model estimates; they may differ from other platforms and must not be the sole basis
of risk decisions.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

__all__ = [
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
]

DEFAULT_RATE: float = 0.065
"""Default risk-free rate: 6.5 % a year, continuously compounded."""

DEFAULT_DIVIDEND_YIELD: float = 0.0
"""Default dividend yield (annual, continuous): used by Black-Scholes-Merton and by the carry of
``option_greeks()``; the Black-76 formulas themselves ignore it."""

MIN_YEARS: float = 1 / (365 * 24 * 60)
"""One minute: the floor applied to time to expiry inside the formulas."""

NEAR_EXPIRY_YEARS: float = 30 / (365 * 24 * 60)
"""Thirty minutes: below this a live result is flagged ``status="near_expiry"``."""

_DAYS_PER_YEAR = 365.0
_D_CLAMP = 40.0
_IV_LOW = 1e-4
_IV_HIGH = 5.0
_IV_PRICE_TOL = 1e-8
_IV_MAX_ITER = 200
_SQRT_2 = math.sqrt(2.0)
_SQRT_2PI = math.sqrt(2.0 * math.pi)
_MODELS = ("black76", "bsm")


@dataclass(frozen=True)
class Greeks:
    """Immutable option valuation result.

    ``price`` is the model price (direct model calls), the traded price (``greeks_from_price``)
    or the intrinsic value at expiry. ``iv`` is the volatility used or solved, ``None`` when
    none applies. ``theta`` is per calendar day, ``vega`` per 0.01 volatility, ``rho`` per 0.01
    rate. ``years_to_expiry`` is the real (unfloored) time to expiry.
    """

    price: float | None
    iv: float | None
    delta: float | None
    gamma: float | None
    theta: float | None
    vega: float | None
    rho: float | None
    status: str
    years_to_expiry: float


@dataclass(frozen=True)
class OptionGreeks:
    """``option_greeks()`` result: the Greeks plus every input used, so it can be explained.

    ``price`` .. ``years_to_expiry`` are as in :class:`Greeks`. ``status`` is ``"ok"``,
    ``"near_expiry"``, ``"expired"``, ``"no_price"`` or ``"price_at_or_below_intrinsic"``; the
    last also covers a price ABOVE what any volatility up to 500 % gives (typically a stale
    last price on expiry day), with ``iv=None``. ``option_price`` is the traded
    price used; ``underlying_price`` the future's (``model="black76"``) or the given spot
    (``model="bsm"``) price; ``forward`` the future price brought to the option's expiry
    (``None`` for ``"bsm"``): ``F · e^{−(rate − dividend_yield)·(Tf − To)}`` for an SDK-selected
    future of another expiry (not on MCX), else the future's price as is.
    ``underlying_tradingsymbol`` and ``same_expiry_future`` describe the future the SDK selected
    (``None`` when the caller supplied the underlying price). ``dividend_yield`` is used by
    ``"bsm"`` and by that carry; it has no effect otherwise. ``expiry`` is the expiry instant
    (IST) and ``calculated_at`` the time the calculation used.
    """

    price: float | None
    iv: float | None
    delta: float | None
    gamma: float | None
    theta: float | None
    vega: float | None
    rho: float | None
    status: str
    years_to_expiry: float
    option_tradingsymbol: str
    option_price: float | None
    underlying_tradingsymbol: str | None
    underlying_price: float | None
    forward: float | None
    same_expiry_future: bool | None
    model: str
    rate: float
    dividend_yield: float
    expiry: datetime
    calculated_at: datetime


# --------------------------------------------------------------------------- helpers


def _is_call(option_type: str) -> bool:
    kind = option_type.strip().upper() if isinstance(option_type, str) else ""
    if kind in ("CE", "CALL"):
        return True
    if kind in ("PE", "PUT"):
        return False
    raise ValueError(f"option_type must be 'CE'/'CALL' or 'PE'/'PUT', got {option_type!r}")


def _check_model(model: str) -> str:
    name = model.strip().lower() if isinstance(model, str) else ""
    if name not in _MODELS:
        raise ValueError(f"model must be 'black76' or 'bsm', got {model!r}")
    return name


def _num(value: Any, name: str = "value") -> Any:
    """``Decimal`` and ``int`` become ``float``; ``None`` and floats pass through.

    A ``bool`` or an int too large for a float raises ``ValueError`` naming the parameter.
    """
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite number, got {value!r}")
    if isinstance(value, (Decimal, int)):
        try:
            return float(value)
        except OverflowError:
            raise ValueError(f"{name} must be a finite number, got a number too large") from None
    return value


def _nums(names: str, *values: Any) -> list[Any]:
    """:func:`_num` over ``values``; ``names`` are the space-separated parameter names."""
    return [_num(v, n) for n, v in zip(names.split(), values, strict=True)]


def _finite(value: float) -> float | None:
    return value if math.isfinite(value) else None


def _require_positive(name: str, value: float) -> None:
    if not (math.isfinite(value) and value > 0):
        raise ValueError(f"{name} must be a positive finite number, got {value!r}")


def _require_finite(name: str, value: float) -> None:
    if not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number, got {value!r}")


def _exp_or_value_error(exponent: float, message: Callable[[], str]) -> float:
    """``exp(exponent)``; a result too large for a float raises ``ValueError(message())``."""
    try:
        factor = math.exp(exponent)
    except OverflowError:
        factor = math.inf
    if math.isinf(factor):
        raise ValueError(message())
    return factor


def _discount(name: str, value: float, years: float) -> float:
    """``exp(-value * years)``; an absurd finite ``rate`` or yield is a ``ValueError``."""
    return _exp_or_value_error(
        -value * years,
        lambda: (
            f"{name} is out of range: exp(-{name} * years) overflows a float "
            f"({name}={value!r}, years={years!r})"
        ),
    )


def _cdf(x: float) -> float:
    return 0.5 * math.erfc(-x / _SQRT_2)


def _pdf(x: float) -> float:
    return math.exp(-x * x / 2.0) / _SQRT_2PI


def _clamp(d: float) -> float:
    return max(-_D_CLAMP, min(_D_CLAMP, d))


def _live_status(years: float) -> str:
    return "near_expiry" if years < NEAR_EXPIRY_YEARS else "ok"


def _at_expiry(
    underlying: float, strike: float, is_call: bool, years: float, price: float, status: str
) -> Greeks:
    """At-expiry values: delta 1/0 (call) or -1/0 (put) by moneyness, 0.5/-0.5 at the money."""
    if underlying > strike:
        delta = 1.0 if is_call else 0.0
    elif underlying < strike:
        delta = 0.0 if is_call else -1.0
    else:
        delta = 0.5 if is_call else -0.5
    return Greeks(
        price=_finite(price),
        iv=None,
        delta=delta,
        gamma=0.0,
        theta=0.0,
        vega=0.0,
        rho=0.0,
        status=status,
        years_to_expiry=years,
    )


def _intrinsic(underlying: float, strike: float, is_call: bool) -> float:
    return max(underlying - strike, 0.0) if is_call else max(strike - underlying, 0.0)


def _expired(underlying: float, strike: float, is_call: bool, years: float) -> Greeks:
    return _at_expiry(
        underlying, strike, is_call, years, _intrinsic(underlying, strike, is_call), "expired"
    )


def _validate(underlying: float, strike: float, years: float, rate: float, q: float) -> None:
    _require_positive("underlying price", underlying)
    _require_positive("strike", strike)
    _require_finite("years", years)
    _require_finite("rate", rate)
    _require_finite("dividend_yield", q)


# --------------------------------------------------------------------------- core maths


def _value(
    underlying: float,
    strike: float,
    t: float,
    rate: float,
    vol: float,
    is_call: bool,
    q: float,
    bsm: bool,
) -> tuple[float, float, float, float, float, float]:
    """Price, delta, gamma, annual theta, raw vega and raw rho for ``t`` > 0 (already floored).

    Black-76 is BSM with the carry equal to zero: the underlying is discounted at ``rate``
    (``q = rate``) and rho holds the future fixed. Should ``σ√t`` or ``U·σ√t`` underflow to 0
    (tiny volatility or underlying), ``d1`` takes its limit (±40, or 0) and gamma becomes NaN
    (reported as ``None``): never a raw ``ZeroDivisionError``.
    """
    sqrt_t = math.sqrt(t)
    sig_t = vol * sqrt_t
    disc_k = _discount("rate", rate, t)
    yield_ = q if bsm else rate
    disc_u = _discount("dividend_yield", q, t) if bsm else disc_k
    drift = math.log(underlying / strike) + (rate - yield_ + 0.5 * vol * vol) * t
    limit = math.copysign(_D_CLAMP, drift) if drift else 0.0  # d1 as sigma*sqrt(t) -> 0
    d1 = _clamp(drift / sig_t) if sig_t > 0 else limit
    d2 = _clamp(d1 - sig_t)
    pdf_d1 = _pdf(d1)
    u_disc = underlying * disc_u
    k_disc = strike * disc_k
    decay = -u_disc * pdf_d1 * vol / (2.0 * sqrt_t)
    if is_call:
        nd1, nd2 = _cdf(d1), _cdf(d2)
        price = u_disc * nd1 - k_disc * nd2
        delta = disc_u * nd1
        theta = decay - rate * k_disc * nd2 + yield_ * u_disc * nd1
        rho = k_disc * t * nd2
    else:
        nmd1, nmd2 = _cdf(-d1), _cdf(-d2)
        price = k_disc * nmd2 - u_disc * nmd1
        delta = -disc_u * nmd1
        theta = decay + rate * k_disc * nmd2 - yield_ * u_disc * nmd1
        rho = -k_disc * t * nmd2
    if not bsm:
        rho = -t * price
    price = max(price, 0.0)
    gamma_denominator = underlying * sig_t
    gamma = disc_u * pdf_d1 / gamma_denominator if gamma_denominator > 0 else math.nan
    vega = u_disc * pdf_d1 * sqrt_t
    return price, delta, gamma, theta, vega, rho


def _model_greeks(
    underlying: float,
    strike: float,
    years: float,
    rate: float,
    vol: float,
    is_call: bool,
    q: float,
    bsm: bool,
) -> Greeks:
    if years <= 0:
        return _expired(underlying, strike, is_call, years)
    t = max(years, MIN_YEARS)
    price, delta, gamma, theta, vega, rho = _value(
        underlying, strike, t, rate, vol, is_call, q, bsm
    )
    return Greeks(
        price=_finite(price),
        iv=_finite(vol),
        delta=_finite(delta),
        gamma=_finite(gamma),
        theta=_finite(theta / _DAYS_PER_YEAR),
        vega=_finite(vega * 0.01),
        rho=_finite(rho * 0.01),
        status=_live_status(years),
        years_to_expiry=years,
    )


def _solve_iv(target: float, price_vega: Callable[[float], tuple[float, float]]) -> float | None:
    """Bracketed Newton/bisection on [_IV_LOW, _IV_HIGH]; price is increasing in volatility."""
    lo, hi = _IV_LOW, _IV_HIGH
    f_lo = price_vega(lo)[0] - target
    if f_lo > _IV_PRICE_TOL:
        return None  # below the cheapest price any volatility in range gives
    if abs(f_lo) <= _IV_PRICE_TOL:
        return lo
    f_hi = price_vega(hi)[0] - target
    if f_hi < -_IV_PRICE_TOL:
        return None  # above the dearest price any volatility in range gives
    if abs(f_hi) <= _IV_PRICE_TOL:
        return hi
    sigma = 0.3
    for _ in range(_IV_MAX_ITER):
        price, vega = price_vega(sigma)
        diff = price - target
        if diff > 0:
            hi = sigma
        else:
            lo = sigma
        newton = sigma - diff / vega if vega > 0 else math.nan
        converged = abs(diff) <= _IV_PRICE_TOL
        if converged and not (lo < newton < hi):
            return sigma
        if lo < newton < hi:
            if converged:
                # One polishing Newton step, kept only if it still re-prices within tolerance.
                polished = price_vega(newton)[0] - target
                return newton if abs(polished) <= _IV_PRICE_TOL else sigma
            sigma = newton
        else:
            sigma = 0.5 * (lo + hi)
        if hi - lo <= 1e-15 * hi:
            break
    final = price_vega(sigma)[0] - target
    return sigma if abs(final) <= _IV_PRICE_TOL else None


# --------------------------------------------------------------------------- public API


def black76(
    forward: float,
    strike: float,
    years: float,
    rate: float,
    volatility: float,
    option_type: str,
) -> Greeks:
    """Black-76 price and Greeks for a European option on a future (``forward`` = future price).

    ``delta`` and ``gamma`` are with respect to the future price; ``rho`` holds it fixed.
    ``years <= 0`` returns the at-expiry result (``status="expired"``).
    """
    forward, strike, years, rate, volatility = _nums(
        "forward strike years rate volatility", forward, strike, years, rate, volatility
    )
    is_call = _is_call(option_type)
    _validate(forward, strike, years, rate, 0.0)
    _require_positive("volatility", volatility)
    return _model_greeks(forward, strike, years, rate, volatility, is_call, 0.0, bsm=False)


def black_scholes(
    spot: float,
    strike: float,
    years: float,
    rate: float,
    volatility: float,
    option_type: str,
    dividend_yield: float = DEFAULT_DIVIDEND_YIELD,
) -> Greeks:
    """Black-Scholes-Merton price and Greeks on spot with a continuous ``dividend_yield``.

    ``years <= 0`` returns the at-expiry result (``status="expired"``).
    """
    spot, strike, years, rate, volatility, dividend_yield = _nums(
        "spot strike years rate volatility dividend_yield",
        spot,
        strike,
        years,
        rate,
        volatility,
        dividend_yield,
    )
    is_call = _is_call(option_type)
    _validate(spot, strike, years, rate, dividend_yield)
    _require_positive("volatility", volatility)
    return _model_greeks(spot, strike, years, rate, volatility, is_call, dividend_yield, bsm=True)


def implied_volatility(
    option_price: float,
    underlying: float,
    strike: float,
    years: float,
    rate: float,
    option_type: str,
    *,
    model: str = "black76",
    dividend_yield: float = DEFAULT_DIVIDEND_YIELD,
) -> float | None:
    """Volatility (0.01 %-500 %) that reproduces ``option_price`` within 1e-8, else ``None``.

    ``underlying`` is the future price for ``model="black76"`` and spot for ``model="bsm"``.
    Returns ``None`` for a non-positive or non-finite price, an expired option (``years <= 0``)
    or a price no volatility in range reproduces: a price at or below intrinsic value, or one
    above what any volatility up to 500 % gives (typically a stale last price on expiry day).
    Numbers may be ``float``, ``int`` or ``Decimal``.
    """
    option_price, underlying, strike, years, rate, dividend_yield = _nums(
        "option_price underlying strike years rate dividend_yield",
        option_price,
        underlying,
        strike,
        years,
        rate,
        dividend_yield,
    )
    is_call = _is_call(option_type)
    bsm = _check_model(model) == "bsm"
    _validate(underlying, strike, years, rate, dividend_yield)
    if not (math.isfinite(option_price) and option_price > 0) or years <= 0:
        return None
    t = max(years, MIN_YEARS)

    def price_vega(vol: float) -> tuple[float, float]:
        price, _, _, _, vega, _ = _value(
            underlying, strike, t, rate, vol, is_call, dividend_yield, bsm
        )
        return price, vega

    sigma = _solve_iv(option_price, price_vega)
    return None if sigma is None else _finite(sigma)


def greeks_from_price(
    option_price: float | None,
    underlying: float | None,
    strike: float,
    years: float,
    rate: float,
    option_type: str,
    *,
    model: str = "black76",
    dividend_yield: float = DEFAULT_DIVIDEND_YIELD,
    tick_size: float = 0.0,
) -> Greeks:
    """Implied volatility and Greeks from a traded option price, with the expiry-day rules.

    * no (or non-positive) underlying price → ``status="no_price"``, all values ``None``;
    * ``years <= 0`` → ``status="expired"``: ``price`` = intrinsic value, deterministic delta;
    * no (or non-positive) option price → ``status="no_price"``;
    * price at or below the model's discounted intrinsic value (+ ``tick_size / 2``), or no
      volatility reproduces it → ``status="price_at_or_below_intrinsic"``, ``iv=None`` and the
      at-expiry Greeks for the option's moneyness. This status also covers a price ABOVE what
      any volatility up to 500 % gives (typically a stale last price on expiry day): ``iv`` is
      ``None`` then too;
    * otherwise ``"ok"`` (``"near_expiry"`` under 30 minutes) with the Greeks at the solved IV.

    ``underlying`` is the future price for ``model="black76"`` and spot for ``model="bsm"``
    (``dividend_yield`` applies to ``"bsm"`` only). Numbers may be ``float``, ``int`` or
    ``Decimal``.
    """
    option_price, underlying, strike, years, rate, dividend_yield, tick_size = _nums(
        "option_price underlying strike years rate dividend_yield tick_size",
        option_price,
        underlying,
        strike,
        years,
        rate,
        dividend_yield,
        tick_size,
    )
    is_call = _is_call(option_type)
    bsm = _check_model(model) == "bsm"
    _require_positive("strike", strike)
    _require_finite("years", years)
    _require_finite("rate", rate)
    _require_finite("dividend_yield", dividend_yield)
    _require_finite("tick_size", tick_size)

    def no_price() -> Greeks:
        return Greeks(None, None, None, None, None, None, None, "no_price", years)

    if underlying is None or not (math.isfinite(underlying) and underlying > 0):
        return no_price()
    if years <= 0:
        return _expired(underlying, strike, is_call, years)
    if option_price is None or not (math.isfinite(option_price) and option_price > 0):
        return no_price()

    t = max(years, MIN_YEARS)
    disc_k = _discount("rate", rate, t)
    disc_u = _discount("dividend_yield", dividend_yield, t) if bsm else disc_k
    forward_value = underlying * disc_u - strike * disc_k
    intrinsic = max(forward_value, 0.0) if is_call else max(-forward_value, 0.0)

    iv: float | None = None
    if option_price > intrinsic + max(tick_size, 0.0) / 2.0:
        iv = implied_volatility(
            option_price,
            underlying,
            strike,
            years,
            rate,
            option_type,
            model=model,
            dividend_yield=dividend_yield,
        )
    if iv is None:
        return _at_expiry(
            underlying, strike, is_call, years, option_price, "price_at_or_below_intrinsic"
        )
    result = _model_greeks(underlying, strike, years, rate, iv, is_call, dividend_yield, bsm)
    return Greeks(
        price=option_price,
        iv=result.iv,
        delta=result.delta,
        gamma=result.gamma,
        theta=result.theta,
        vega=result.vega,
        rho=result.rho,
        status=result.status,
        years_to_expiry=years,
    )
