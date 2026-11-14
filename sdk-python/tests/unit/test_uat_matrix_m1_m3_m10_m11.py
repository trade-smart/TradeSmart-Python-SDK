"""Case tables and pure helpers of the UAT matrix families M1 (shapes), M3 (rejections),
M10 (order margin, read-only) and M11 (tags and encoding), offline.

Synthetic contracts, quotes and script-master rows only; nothing here touches TradeSmart.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tests.live_orders.safety import Refusal
from tests.live_orders.uat_matrix import cases, cases_margin, cases_reject, cases_tags, contracts
from tests.live_orders.uat_matrix import family as family_helpers
from tests.live_orders.uat_matrix.cases import Case
from tests.live_orders.uat_matrix.contracts import Contract
from tests.live_orders.uat_matrix.expect import Profile
from tests.live_orders.uat_matrix.pricing import PriceSnapshot
from tradesmart.models.orders import OrderMargin

TODAY = date(2026, 11, 13)


def _contract(role: str, *, kind: str, lot: int = 1, tick: str = "0.05", **extra: Any) -> Contract:
    exchange = cases.SEGMENT_EXCHANGE[role.split("-", 1)[0]]
    return Contract(
        role=role,
        exchange=exchange,
        tradingsymbol=extra.pop("tradingsymbol", f"{role.upper()}-SYM"),
        token="1",
        symbol=extra.pop("symbol", "" if kind == "eq" else "BANKNIFTY"),
        kind=kind,
        lot_size=lot,
        tick_size=Decimal(tick),
        resolved_last_price=Decimal("100"),
        **extra,
    )


def _snapshot(
    last: str = "400.00", tick: str = "0.05", lc: str | None = "320.00", uc: str | None = "480.00"
) -> PriceSnapshot:
    return PriceSnapshot(
        tradingsymbol="X",
        last_price=Decimal(last),
        tick_size=Decimal(tick),
        lower_circuit=None if lc is None else Decimal(lc),
        upper_circuit=None if uc is None else Decimal(uc),
    )


# -- M1: the shape grid -----------------------------------------------------------------------


def _shapes() -> list[Case]:
    return [c for c in cases.CASES if c.family == "shapes"]


def test_m1_covers_banknifty_options_and_sensex_pe_with_the_full_grid() -> None:
    for role in ("nfo-banknifty-ce-atm", "nfo-banknifty-pe-atm", "bfo-sensex-pe-atm"):
        grid = [c for c in _shapes() if c.role == role]
        combos = {(c.product, c.prctyp, c.ret, c.side) for c in grid}
        assert len(grid) == 32, role
        for product in ("NRML", "MIS"):
            for prctyp in ("LMT", "SL-LMT"):
                for ret in ("DAY", "IOC", "EOS"):
                    for side in ("BUY", "SELL"):
                        assert (product, prctyp, ret, side) in combos, role
            for prctyp in ("MKT", "SL-MKT"):
                for side in ("BUY", "SELL"):
                    assert (product, prctyp, "DAY", side) in combos, role


def test_m1_uses_every_itm_and_otm_rung_with_resting_day_shapes() -> None:
    rungs = [
        role for role in contracts.ROLES if role.split("-")[-1] in ("itm1", "itm2", "otm1", "otm2")
    ]
    assert len(rungs) == 16
    for role in rungs:
        grid = [c for c in _shapes() if c.role == role]
        assert {(c.product, c.prctyp, c.ret, c.side) for c in grid} == {
            ("NRML", "LMT", "DAY", "BUY"),
            ("NRML", "LMT", "DAY", "SELL"),
            ("NRML", "SL-LMT", "DAY", "BUY"),
            ("NRML", "SL-LMT", "DAY", "SELL"),
        }, role
        assert {c.outcome for c in grid} == {cases.REST, cases.TRIGGER_PENDING}
        assert all(c.tier == cases.FULL and c.orders == 1 for c in grid)


def test_m1_smoke_is_the_six_smoke_shapes_once_per_segment() -> None:
    smoke = [c for c in _shapes() if c.tier == cases.SMOKE and not c.canary]
    by_segment: dict[str, list[Case]] = {}
    for case in smoke:
        by_segment.setdefault(case.segment, []).append(case)
    assert set(by_segment) == {"nse", "nfo", "bfo"}
    for segment, group in by_segment.items():
        assert len(group) == 6, segment
        assert {(c.prctyp, c.ret, c.side) for c in group} == cases.SMOKE_SHAPES, segment
    nfo_roles = {c.role for c in by_segment["nfo"]}
    assert nfo_roles == {"nfo-nifty-f", "nfo-nifty-ce-atm"}  # a future and an option


def test_m1_ids_stay_unique_and_existing_ids_are_kept() -> None:
    ids = [c.id for c in cases.CASES]
    assert len(ids) == len(set(ids))
    assert cases.case("shapes-nfo-nifty-f-nrml-lmt-day-buy").tier == cases.SMOKE
    assert cases.case("shapes-bfo-sensex-pe-atm-nrml-lmt-eos-sell").outcome == cases.REST
    assert cases.case("shapes-nfo-banknifty-pe-otm2-nrml-sl-lmt-day-sell").outcome == (
        cases.TRIGGER_PENDING
    )


def test_every_role_the_dispatch_names_is_used_by_m1_m3_or_m11() -> None:
    used = {c.role for c in cases.CASES} | {c.role for c in cases_reject.REJECT_CASES}
    used |= {c.role for c in cases_tags.TAG_CASES}
    named = {
        role
        for role in contracts.ROLES
        if role.startswith(("nfo-nifty-ce", "nfo-nifty-pe", "nfo-banknifty-ce", "nfo-banknifty-pe"))
        or role in ("bfo-sensex-pe-atm", "nse-e")
    }
    assert named <= used, sorted(named - used)


# -- M3: rejection paths ----------------------------------------------------------------------


def test_m3_cases_are_unique_and_follow_the_id_scheme() -> None:
    ids = [c.id for c in cases_reject.REJECT_CASES]
    assert len(ids) == len(set(ids))
    assert not set(ids) & {c.id for c in cases.CASES}
    for case in cases_reject.REJECT_CASES:
        probe = cases_reject.probe_of(case)
        assert case.family == "reject"
        assert (
            case.id
            == (
                f"reject-{case.role}-{case.product}-{case.prctyp}-{case.ret}-{case.side}-{probe.name}"
            ).lower()
        )
        assert case.outcome in (cases.REJECT, cases.RECORD), case.id
        assert case.orders == 1
        assert case.role in contracts.ROLES


def test_m3_covers_every_rejection_path_of_the_inventory() -> None:
    names = {cases_reject.probe_of(c).name for c in cases_reject.REJECT_CASES}
    assert names >= {
        "offtick",
        "lot",
        "below-lc",
        "above-uc",
        "sl-limit-below-trigger",
        "sl-trigger-crossed",
        "cnc",
        "mtf",
        "nrml",
        "eos",
        "dscqty-low",
        "dscqty-high",
        "dscqty",
        "unknown",
        "expired",
    }
    roles = {c.role for c in cases_reject.REJECT_CASES}
    assert roles == {"nse-e", "nfo-banknifty-f", "nfo-nifty-pe-atm", "bfo-sensex-pe-atm"}


def test_m3_documented_refusals_and_recorded_probes() -> None:
    by_name = {cases_reject.probe_of(c).name: c for c in cases_reject.REJECT_CASES}
    for name in ("offtick", "lot", "below-lc", "above-uc", "cnc", "mtf", "nrml", "unknown"):
        assert by_name[name].outcome == cases.REJECT, name
    for name in ("sl-trigger-crossed", "dscqty-low", "dscqty-high", "dscqty"):
        assert by_name[name].outcome == cases.RECORD, name


def test_m3_never_probes_a_marketable_price() -> None:
    # A probe accepted by mistake must rest, never fill: below lc only BUYs, above uc only SELLs.
    for case in cases_reject.REJECT_CASES:
        name = cases_reject.probe_of(case).name
        if name == "below-lc":
            assert case.side == "BUY"
        if name == "above-uc":
            assert case.side == "SELL"


def test_m3_lot_and_product_probes_apply_where_they_mean_something() -> None:
    for case in cases_reject.REJECT_CASES:
        name = cases_reject.probe_of(case).name
        kind = "eq" if case.segment == "nse" else "fno"
        if name in ("lot", "cnc", "mtf", "expired", "dscqty"):
            assert kind == "fno", case.id
        if name in ("nrml", "eos", "dscqty-low", "dscqty-high"):
            assert kind == "eq", case.id
        if name == "cnc":
            assert case.product == "CNC"
        if name == "mtf":
            assert case.product == "MTF"
        if name == "nrml":
            assert case.product == "NRML"
        if name == "eos":
            assert case.ret == "EOS"


def test_m3_smoke_is_off_tick_lot_and_circuit_on_nse_and_nfo() -> None:
    smoke = [c for c in cases_reject.REJECT_CASES if c.tier == cases.SMOKE]
    assert {(c.role, cases_reject.probe_of(c).name) for c in smoke} == {
        ("nse-e", "offtick"),
        ("nse-e", "below-lc"),
        ("nfo-banknifty-f", "offtick"),
        ("nfo-banknifty-f", "lot"),
        ("nfo-banknifty-f", "below-lc"),
        ("nfo-nifty-pe-atm", "lot"),
    }


def _case(name: str, role: str) -> Case:
    return next(
        c
        for c in cases_reject.REJECT_CASES
        if c.role == role and cases_reject.probe_of(c).name == name
    )


EQ = _contract("nse-e", kind="eq", tradingsymbol="M&M-EQ")
FUT = _contract("nfo-banknifty-f", kind="fut", lot=30, tradingsymbol="BANKNIFTY25NOV26F")


def _plan(name: str, contract: Contract, snapshot: PriceSnapshot, **kw: Any) -> dict[str, Any]:
    return cases_reject.build_plan(_case(name, contract.role), contract, snapshot, **kw)


def test_offtick_breaks_only_the_tick() -> None:
    plan = _plan("offtick", EQ, _snapshot())
    price = Decimal(str(plan["price"]))
    assert price % Decimal("0.05") != 0
    assert price < Decimal("400") * Decimal("0.95")  # still far below the market
    assert plan["transaction_type"] == "BUY" and plan["order_type"] == "LMT"


def test_offtick_refuses_a_one_paisa_tick() -> None:
    with pytest.raises(Refusal, match="tick"):
        _plan("offtick", EQ, _snapshot(tick="0.01"))


def test_lot_probe_sends_one_more_than_a_lot() -> None:
    plan = _plan("lot", FUT, _snapshot("52000", tick="0.10", lc="46800", uc="57200"))
    assert plan["quantity"] == 31


def test_circuit_probes_cross_the_circuit_by_one_tick_without_being_marketable() -> None:
    below = _plan("below-lc", EQ, _snapshot())
    assert Decimal(str(below["price"])) == Decimal("319.95") and below["transaction_type"] == "BUY"
    above = _plan("above-uc", EQ, _snapshot())
    assert Decimal(str(above["price"])) == Decimal("480.05") and above["transaction_type"] == "SELL"


def test_circuit_probes_refuse_without_a_circuit_or_at_the_minimum_price() -> None:
    with pytest.raises(Refusal, match="circuit"):
        _plan("below-lc", EQ, _snapshot(lc=None))
    with pytest.raises(Refusal, match="circuit"):
        _plan("above-uc", EQ, _snapshot(uc=None))
    with pytest.raises(Refusal, match="price"):
        _plan("below-lc", EQ, _snapshot(last="1.00", lc="0.05", uc="1.20"))


def test_stop_probes() -> None:
    bad = _plan("sl-limit-below-trigger", EQ, _snapshot())
    assert bad["order_type"] == "SL-LMT" and bad["transaction_type"] == "BUY"
    assert Decimal(str(bad["price"])) < Decimal(str(bad["trigger_price"]))
    assert Decimal(str(bad["trigger_price"])) > Decimal("400")  # the stop is above the market
    crossed = _plan("sl-trigger-crossed", EQ, _snapshot())
    assert Decimal(str(crossed["trigger_price"])) < Decimal("400")  # already crossed
    assert Decimal(str(crossed["price"])) <= Decimal("360")  # rests far below if it triggers
    assert Decimal(str(crossed["price"])) <= Decimal(str(crossed["trigger_price"]))


def test_product_validity_and_disclosed_quantity_probes() -> None:
    assert _plan("cnc", FUT, _snapshot("52000", tick="0.10", lc=None, uc=None))["product"] == "CNC"
    assert _plan("nrml", EQ, _snapshot())["product"] == "NRML"
    assert _plan("eos", EQ, _snapshot())["validity"] == "EOS"
    low = _plan("dscqty-low", EQ, _snapshot())
    assert (low["quantity"], low["disclosed_quantity"]) == (20, 1)
    high = _plan("dscqty-high", EQ, _snapshot())
    assert (high["quantity"], high["disclosed_quantity"]) == (20, 10)
    fno = _plan("dscqty", FUT, _snapshot("52000", tick="0.10", lc=None, uc=None))
    assert (fno["quantity"], fno["disclosed_quantity"]) == (30, 1)


def test_unknown_and_expired_symbols() -> None:
    unknown = _plan("unknown", EQ, _snapshot())
    assert unknown["tradingsymbol"] == cases_reject.UNKNOWN_SYMBOL["eq"]
    snap = _snapshot("52000", tick="0.10", lc=None, uc=None)
    expired = _plan("expired", FUT, snap, expired={"nfo-banknifty-f": "BANKNIFTY30OCT26F"})
    assert expired["tradingsymbol"] == "BANKNIFTY30OCT26F"
    with pytest.raises(Refusal, match="expired"):
        _plan("expired", FUT, snap, expired={})


def test_expired_symbol_is_the_latest_expired_contract_of_the_same_kind() -> None:
    rows = [
        {"TradingSymbol": "BANKNIFTY30OCT26F", "Symbol": "BANKNIFTY", "Instrument": "FUTIDX",
         "Expiry": "30-OCT-2026", "OptionType": "XX"},
        {"TradingSymbol": "BANKNIFTY25SEP26F", "Symbol": "BANKNIFTY", "Instrument": "FUTIDX",
         "Expiry": "25-SEP-2026", "OptionType": "XX"},
        {"TradingSymbol": "BANKNIFTY30OCT26C52000", "Symbol": "BANKNIFTY", "Instrument": "OPTIDX",
         "Expiry": "30-OCT-2026", "OptionType": "CE"},
        {"TradingSymbol": "BANKNIFTY25NOV26F", "Symbol": "BANKNIFTY", "Instrument": "FUTIDX",
         "Expiry": "25-NOV-2026", "OptionType": "XX"},
        {"TradingSymbol": "NIFTY30OCT26F", "Symbol": "NIFTY", "Instrument": "FUTIDX",
         "Expiry": "30-OCT-2026", "OptionType": "XX"},
    ]  # fmt: skip
    assert cases_reject.expired_symbol(rows, FUT, TODAY) == "BANKNIFTY30OCT26F"
    option = _contract("nfo-nifty-pe-atm", kind="opt", lot=65, symbol="BANKNIFTY", option_type="CE")
    assert cases_reject.expired_symbol(rows, option, TODAY) == "BANKNIFTY30OCT26C52000"
    assert cases_reject.expired_symbol(rows[3:], FUT, TODAY) is None


# -- M11: tags and encoding -------------------------------------------------------------------


def test_m11_cases_are_far_orders_on_the_ampersand_symbol() -> None:
    assert {c.role for c in cases_tags.TAG_CASES} == {"nse-e"}
    assert all(c.family == "tags" and c.orders == 1 for c in cases_tags.TAG_CASES)
    assert {c.outcome for c in cases_tags.TAG_CASES} <= {cases.REST, cases.TRIGGER_PENDING}
    smoke = [c for c in cases_tags.TAG_CASES if c.tier == cases.SMOKE]
    assert [c.id for c in smoke] == ["tags-nse-e-mis-lmt-day-buy"]
    assert len({c.id for c in cases_tags.TAG_CASES}) == len(cases_tags.TAG_CASES) >= 4


def _ledger(path: Path, lines: list[dict[str, Any]]) -> Path:
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    return path


def test_m11_tag_check_passes_unique_short_run_tags(tmp_path: Path) -> None:
    path = _ledger(
        tmp_path / "ledger.jsonl",
        [
            {"type": "intent", "kind": "place", "tag": "sdkuat-1113abcd-001-1"},
            {"type": "intent", "kind": "cancel", "tag": "sdkuat-1113abcd-001-1"},
            {"type": "intent", "kind": "place", "tag": "sdkuat-1113abcd-002-1"},
        ],
    )
    assert cases_tags.tag_problems(path, "1113abcd") == []


def test_m11_tag_check_finds_reused_long_or_foreign_tags(tmp_path: Path) -> None:
    path = _ledger(
        tmp_path / "ledger.jsonl",
        [
            {"type": "intent", "kind": "place", "tag": "sdkuat-1113abcd-001-1"},
            {"type": "intent", "kind": "place", "tag": "sdkuat-1113abcd-001-1"},
            {"type": "intent", "kind": "place", "tag": "sdkuat-1113abcd-002-1-way-too-long-tag"},
            {"type": "intent", "kind": "place", "tag": "sdklive-x"},
        ],
    )
    problems = cases_tags.tag_problems(path, "1113abcd")
    assert len(problems) == 3
    assert any("twice" in p for p in problems)
    assert any("30" in p for p in problems)
    assert any("sdklive-x" in p for p in problems)


# -- M10: order margin ------------------------------------------------------------------------


def test_m10_has_one_case_per_m1_shape_without_validity() -> None:
    margin = cases_margin.MARGIN_CASES
    ids = [c.id for c in margin]
    assert len(ids) == len(set(ids))
    shapes = {(c.role, c.product, c.prctyp, c.side) for c in cases.CASES if c.family == "shapes"}
    covered = {(c.role, c.product, c.prctyp, c.side) for c in margin}
    assert shapes <= covered
    assert all(c.id.startswith("margin-") for c in margin)


def test_m10_adds_cover_bracket_and_invalid_products() -> None:
    by_id = {c.id: c for c in cases_margin.MARGIN_CASES}
    for role in ("nse-b", "nfo-nifty-f"):
        assert f"margin-{role}-co-lmt-buy" in by_id
        assert f"margin-{role}-bo-lmt-buy" in by_id
    invalid = [c for c in cases_margin.MARGIN_CASES if c.invalid]
    assert {(c.role, c.product) for c in invalid} >= {
        ("nfo-nifty-f", "CNC"),
        ("nfo-nifty-f", "MTF"),
        ("nfo-nifty-ce-atm", "CNC"),
    }


def _mcase(case_id: str) -> cases_margin.MarginCase:
    return next(c for c in cases_margin.MARGIN_CASES if c.id == case_id)


NO_PROFILE = Profile(None, None, None)


def test_m10_expectations() -> None:
    assert cases_margin.expectation(_mcase("margin-nse-b-mis-lmt-buy"), NO_PROFILE) == "ok"
    assert cases_margin.expectation(_mcase("margin-nse-b-mis-mkt-buy"), NO_PROFILE) == "record"
    invalid = _mcase("margin-nfo-nifty-f-cnc-lmt-buy")
    assert cases_margin.expectation(invalid, NO_PROFILE) == "record"
    co = _mcase("margin-nse-b-co-lmt-buy")
    assert cases_margin.expectation(co, NO_PROFILE) == "record"  # the profile does not say
    enabled = Profile(None, None, {"CO": frozenset({"NSE"})})
    assert cases_margin.expectation(co, enabled) == "ok"
    disabled = Profile(None, None, {"MIS": frozenset({"NSE"})})
    assert cases_margin.expectation(_mcase("margin-nse-b-mis-lmt-buy"), disabled) == "ok"
    assert cases_margin.expectation(co, disabled) == "record"


def test_m10_request_is_priced_from_the_snapshot_and_never_zero() -> None:
    contract = _contract("nse-b", kind="eq", tradingsymbol="ITC-EQ")
    snap = _snapshot()
    lmt = cases_margin.margin_kwargs(_mcase("margin-nse-b-mis-lmt-buy"), contract, snap)
    assert lmt["price"] == Decimal("360.00") and "validity" not in lmt
    assert lmt["quantity"] == 1 and lmt["tradingsymbol"] == "ITC-EQ"
    mkt = cases_margin.margin_kwargs(_mcase("margin-nse-b-mis-mkt-buy"), contract, snap)
    assert mkt["price"] == Decimal("400.00")  # docs: "Order price cannot be zero"
    stop = cases_margin.margin_kwargs(_mcase("margin-nse-b-mis-sl-lmt-sell"), contract, snap)
    assert stop["trigger_price"] > stop["price"]
    co = cases_margin.margin_kwargs(_mcase("margin-nse-b-co-lmt-buy"), contract, snap)
    assert co["product"] == "CO" and co["book_loss_price"] > 0
    assert co["book_loss_price"] % Decimal("0.05") == 0


def _margin(**fields: Any) -> OrderMargin:
    return OrderMargin.model_validate({"stat": "Ok", "remarks": "Order Success", **fields})


def test_m10_judging() -> None:
    ok_case = _mcase("margin-nse-b-mis-lmt-buy")
    verdict, _ = cases_margin.judge(ok_case, "ok", _margin(ordermargin="120.50"))
    assert verdict == "pass"
    verdict, reason = cases_margin.judge(ok_case, "ok", _margin(ordermargin="-1"))
    assert verdict == "fail" and "ordermargin" in reason
    verdict, reason = cases_margin.judge(ok_case, "ok", _margin(ordermargin=None))
    assert verdict == "fail"
    verdict, reason = cases_margin.judge(ok_case, "ok", _margin(remarks=None, ordermargin="1"))
    assert verdict == "fail" and "remarks" in reason
    verdict, reason = cases_margin.judge(ok_case, "ok", RuntimeError("Not_Ok: bad"))
    assert verdict == "fail" and "bad" in reason
    rec = _mcase("margin-nse-b-mis-mkt-buy")
    assert cases_margin.judge(rec, "record", RuntimeError("Not_Ok"))[0] == "record"
    assert cases_margin.judge(rec, "record", _margin(ordermargin="1"))[0] == "record"


# -- family helpers ---------------------------------------------------------------------------


class _Mark:
    def __init__(self, name: str) -> None:
        self.name = name


class _CallSpec:
    def __init__(self, params: dict[str, Any]) -> None:
        self.params = params


class _Item:
    def __init__(self, module: object, case: Case | None, *marks: str) -> None:
        self.module = module
        self.callspec = None if case is None else _CallSpec({"case": case})
        self._marks = [_Mark(m) for m in marks]

    def iter_markers(self) -> list[_Mark]:
        return self._marks


class _Session:
    def __init__(self, items: list[_Item]) -> None:
        self.items = items


class _Request:
    def __init__(self, module: object, items: list[_Item]) -> None:
        self.module = module
        self.session = _Session(items)


def test_selected_cases_are_this_modules_items_that_will_run_in_order() -> None:
    mine, other = object(), object()
    a, b, c = _shapes()[:3]
    request = _Request(
        mine,
        [
            _Item(mine, b),
            _Item(other, a),
            _Item(mine, a, "skip"),
            _Item(mine, None),
            _Item(mine, c, "smoke"),
            _Item(mine, b),
        ],
    )
    assert family_helpers.selected_cases(request) == [b, c]


def test_params_carry_the_case_id_and_tier_marker() -> None:
    smoke = next(c for c in _shapes() if c.tier == cases.SMOKE)
    full = next(c for c in _shapes() if c.tier == cases.FULL)
    params = family_helpers.params([smoke, full])
    assert [p.id for p in params] == [smoke.id, full.id]
    assert [m.name for m in params[0].marks] == ["smoke"]
    assert list(params[1].marks) == []


def test_option_reject_probes_use_the_four_percent_far_price() -> None:
    opt = _contract("nfo-nifty-pe-atm", kind="opt", lot=75)
    snap = _snapshot("400.00", lc="100.00", uc="900.00")
    assert _plan("sl-limit-below-trigger", opt, snap)["price"] == Decimal("384.00")
    crossed = _plan("sl-trigger-crossed", opt, snap)
    assert crossed["price"] == Decimal("384.00")
    assert _plan("offtick", opt, snap)["price"] == Decimal("384.00") + Decimal("0.01")


def test_the_modify_to_limit_step_of_an_option_prices_four_percent_below(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from tests.live_orders.uat_matrix import cases_modify

    opt = _contract("nfo-nifty-pe-atm", kind="opt", lot=75)
    monkeypatch.setattr(cases_modify, "_snapshot", lambda ctx: _snapshot("400.00", lc="100.00"))
    state = cases_modify.State(
        price=Decimal("1"), trigger_price=Decimal("2"), quantity=75,
        order_type="SL-LMT", product="NRML", validity="DAY",
    )  # fmt: skip
    ctx: Any = SimpleNamespace(placed=SimpleNamespace(contract=opt), state=state)
    assert cases_modify._to_limit(ctx).price == Decimal("384.00")
