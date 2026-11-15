"""The shared conformance vectors (``specifications/conformance/``) and the Python SDK agree.

Every committed case is evaluated again with the SDK (through the evaluators of
``scripts/gen_conformance.py``) and compared with its ``expected`` value within the file's
tolerance. The test also fails when a listed file is missing, when a file exists that the
README does not list, or when the generator's cases changed without regenerating the files
(``docker compose run --rm dev python scripts/gen_conformance.py``).
"""

from __future__ import annotations

import importlib.util
import json
import math
import re
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

SDK_ROOT = Path(__file__).resolve().parents[2]
CONFORMANCE = SDK_ROOT.parent / "specifications" / "conformance"
README = CONFORMANCE / "README.md"

# The agreed file names (other documents link to them): do not rename.
EXPECTED_FILES = frozenset(
    {
        "checksum.json",
        "not_ok_classification.json",
        "expiry_instants.json",
        "contract_selection.json",
        "greeks_prices.json",
        "implied_volatility.json",
        "greeks_edge_cases.json",
        "carry_forward.json",
        "pcr.json",
        "max_pain.json",
        "buildup.json",
        "ws_connect_ack.json",
        "config_defaults.json",
        "jdata_encoding.json",
        "token_expiry.json",
        "scrip_keys.json",
        "time_conversion.json",
        "round_to_tick.json",
        "retry_schedule.json",
        "order_validation.json",
        "token_file.json",
        "stream_backlog.json",
        "request_names.json",
        "outcome_words.json",
        "transport_failures.json",
        "script_master_parse.json",
        "script_master_freshness.json",
        "read_payloads.json",
        "slice_plan.json",
        "success_words.json",
    }
)


def _load_generator() -> ModuleType:
    path = SDK_ROOT / "scripts" / "gen_conformance.py"
    spec = importlib.util.spec_from_file_location("gen_conformance", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["gen_conformance"] = module
    spec.loader.exec_module(module)
    return module


gen = _load_generator()


def _no_constant(name: str) -> Any:
    raise AssertionError(f"NaN/Infinity ({name}) is not allowed in a vector file")


def _read(name: str) -> dict[str, Any]:
    text = (CONFORMANCE / name).read_text(encoding="utf-8")
    document = json.loads(text, parse_constant=_no_constant)
    assert isinstance(document, dict)
    return document


def _all_cases() -> list[Any]:
    """``(file, case)`` pairs of every file that exists (a missing file fails its own test)."""
    params = []
    for name in sorted(EXPECTED_FILES):
        if not (CONFORMANCE / name).is_file():
            continue
        for case in _read(name).get("cases", []):
            params.append(pytest.param(name, case, id=f"{name}::{case['name']}"))
    return params


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _decimal(value: Any) -> Decimal | None:
    try:
        number = Decimal(value)
    except (InvalidOperation, TypeError, ValueError):
        return None
    return number if number.is_finite() else None


def assert_same(actual: Any, expected: Any, tolerance: dict[str, Any], where: str = "$") -> None:
    """Deep comparison: numbers within ``abs``/``rel``, Decimal strings within ``decimal_abs``."""
    if expected is None or isinstance(expected, bool):
        assert actual is expected, f"{where}: {actual!r} != {expected!r}"
    elif _is_number(expected):
        assert _is_number(actual), f"{where}: {actual!r} is not a number (expected {expected!r})"
        allowed = float(tolerance.get("abs", 0.0)) + float(tolerance.get("rel", 0.0)) * abs(
            expected
        )
        assert abs(actual - expected) <= allowed, (
            f"{where}: {actual!r} differs from {expected!r} by more than {allowed!r}"
        )
    elif isinstance(expected, str):
        assert isinstance(actual, str), f"{where}: {actual!r} is not a string"
        decimal_abs = tolerance.get("decimal_abs")
        left, right = _decimal(actual), _decimal(expected)
        if decimal_abs is not None and left is not None and right is not None:
            assert abs(left - right) <= Decimal(decimal_abs), f"{where}: {actual} != {expected}"
        else:
            assert actual == expected, f"{where}: {actual!r} != {expected!r}"
    elif isinstance(expected, dict):
        assert isinstance(actual, dict), f"{where}: {actual!r} is not an object"
        assert set(actual) == set(expected), f"{where}: keys {sorted(actual)} != {sorted(expected)}"
        for key, value in expected.items():
            assert_same(actual[key], value, tolerance, f"{where}.{key}")
    elif isinstance(expected, list):
        assert isinstance(actual, list), f"{where}: {actual!r} is not a list"
        assert len(actual) == len(expected), f"{where}: length {len(actual)} != {len(expected)}"
        for index, value in enumerate(expected):
            assert_same(actual[index], value, tolerance, f"{where}[{index}]")
    else:  # pragma: no cover - the files only hold JSON
        raise AssertionError(f"{where}: unsupported expected value {expected!r}")


# --------------------------------------------------------------------------- the file set


def test_expected_file_set_matches_the_generator() -> None:
    assert set(gen.FILES) == EXPECTED_FILES


@pytest.mark.parametrize("name", sorted(EXPECTED_FILES))
def test_listed_file_exists(name: str) -> None:
    assert (CONFORMANCE / name).is_file(), f"{name} is missing: run scripts/gen_conformance.py"


def test_no_unlisted_json_file_exists() -> None:
    present = {p.name for p in CONFORMANCE.glob("*.json")}
    assert present == EXPECTED_FILES, f"unexpected: {sorted(present - EXPECTED_FILES)}"


def test_readme_lists_exactly_the_files() -> None:
    text = README.read_text(encoding="utf-8")
    listed = set(re.findall(r"`([A-Za-z0-9_]+\.json)`", text))
    assert listed == EXPECTED_FILES, (
        f"README lists {sorted(listed - EXPECTED_FILES)} extra, "
        f"misses {sorted(EXPECTED_FILES - listed)}"
    )


# --------------------------------------------------------------------------- file contents


def _inputs_match(committed: Any, generated: Any) -> bool:
    """Equal, except that two floats may differ in their last digits (rel. 1e-12).

    Some inputs are computed by the generator (a round trip's option price), and their last bits
    depend on the platform's maths library, so an exact comparison fails on other machines.
    """
    if isinstance(committed, float) and isinstance(generated, float):
        return math.isclose(committed, generated, rel_tol=1e-12, abs_tol=0.0)
    if isinstance(committed, dict) and isinstance(generated, dict):
        return committed.keys() == generated.keys() and all(
            _inputs_match(committed[k], generated[k]) for k in committed
        )
    if isinstance(committed, list) and isinstance(generated, list):
        return len(committed) == len(generated) and all(
            _inputs_match(c, g) for c, g in zip(committed, generated, strict=True)
        )
    return type(committed) is type(generated) and bool(committed == generated)


@pytest.mark.parametrize("name", sorted(EXPECTED_FILES))
def test_file_shape_and_up_to_date_with_the_generator(name: str) -> None:
    document = _read(name)
    assert set(document) == {"description", "source", "tolerance", "cases"}
    assert isinstance(document["description"], str) and document["description"]
    assert isinstance(document["source"], str) and document["source"]
    assert isinstance(document["tolerance"], dict)
    cases = document["cases"]
    assert cases, "a vector file needs cases"
    for case in cases:
        assert set(case) == {"name", "input", "expected"}
    names = [case["name"] for case in cases]
    assert len(names) == len(set(names)), "case names must be unique"

    spec = gen.FILES[name]
    assert document["description"] == spec["description"]
    assert document["source"] == spec["source"]
    assert document["tolerance"] == spec["tolerance"]
    generated = [{"name": n, "input": i} for n, i in spec["cases"]()]
    committed = [{"name": c["name"], "input": c["input"]} for c in cases]
    assert _inputs_match(committed, generated), f"{name} is stale: run scripts/gen_conformance.py"


@pytest.mark.parametrize(("name", "case"), _all_cases())
def test_sdk_reproduces_the_expected_value(name: str, case: dict[str, Any]) -> None:
    tolerance = _read(name)["tolerance"]
    produced = gen.FILES[name]["evaluate"](case["input"])
    # A JSON round trip: the SDK result must be expressible in the file (and finite).
    produced = json.loads(json.dumps(produced, allow_nan=False))
    assert_same(produced, case["expected"], tolerance, f"{name}::{case['name']}")


def test_checksum_worked_example_is_the_documented_value() -> None:
    case = next(c for c in _read("checksum.json")["cases"] if c["input"]["code"] == "x1y2z3")
    assert case["expected"] == {
        "checksum": "7b482d7b380a3067eaba4c9c909b19253c4fa0edb5833e246401b8497c99a9c3"
    }


def test_comparison_helper_catches_drift() -> None:
    with pytest.raises(AssertionError):
        assert_same({"price": 1.0001}, {"price": 1.0}, {"abs": 1e-9})
    with pytest.raises(AssertionError):
        assert_same({"price": 1.0}, {"price": 1.0, "iv": None}, {"abs": 1e-9})
    with pytest.raises(AssertionError):
        assert_same("0.33", "0.3333", {"decimal_abs": "1e-12"})
    assert_same("1.50", "1.5", {"decimal_abs": "0"})
    assert_same({"price": 1.0 + 1e-12}, {"price": 1.0}, {"abs": 1e-9})


# --------------------------------------------------------------------------- pinned review cases


def _case(name: str, case: str) -> dict[str, Any]:
    cases = {c["name"]: c for c in _read(name)["cases"]}
    assert case in cases, f"{name} has no case {case!r}: run scripts/gen_conformance.py"
    return cases[case]


@pytest.mark.parametrize(
    ("case", "error"),
    [
        ("redirect_307_empty_body_state_changing_is_unknown", "OrderStatusUnknownError"),
        ("redirect_302_html_read_is_a_parse_error", "ResponseParseError"),
        ("ok_object_where_a_list_is_expected_state_changing_is_unknown", "OrderStatusUnknownError"),
    ],
)
def test_not_ok_classification_review_cases(case: str, error: str) -> None:
    assert _case("not_ok_classification.json", case)["expected"]["error"] == error


def test_no_data_wins_over_http_401() -> None:
    case = _case("not_ok_classification.json", "no_data_on_http_401_is_still_an_empty_list")
    assert case["input"]["http_status"] == 401
    assert case["expected"] == {"returns": []}


def test_not_ok_description_lists_place_multi_order() -> None:
    assert "PlaceMultiOrder" in _read("not_ok_classification.json")["description"]


@pytest.mark.parametrize(
    "case",
    [
        "connect_timeout_nan_is_rejected",
        "read_timeout_infinity_is_rejected",
        "backoff_cap_negative_infinity_is_rejected",
        "order_rate_limit_max_wait_nan_is_rejected",
        "order_rate_limit_max_wait_infinity_is_rejected",
    ],
)
def test_config_rejects_non_finite_values(case: str) -> None:
    expected = _case("config_defaults.json", case)["expected"]
    assert expected["error"] == "ConfigurationError"
    assert "finite" in expected["message"]


def test_retry_after_negative_is_zero_and_non_finite_is_ignored() -> None:
    negative = _case("retry_schedule.json", "delay_retry_after_negative_is_zero")
    assert negative["input"]["retry_after"] == -5
    assert negative["expected"] == {"min": 0.0, "max": 0.0}
    plain = _case("retry_schedule.json", "delay_attempt_0_default_backoff")["expected"]
    for name in ("delay_retry_after_nan_is_ignored", "delay_retry_after_infinity_is_ignored"):
        assert _case("retry_schedule.json", name)["expected"] == plain
    for name in ("retry_after_header_nan_is_ignored", "retry_after_header_inf_is_ignored"):
        assert _case("retry_schedule.json", name)["expected"] == {"retry_after": None}


@pytest.mark.parametrize(
    ("case", "fragment"),
    [
        ("carriage_return_is_short_escaped", "a\\rb"),
        ("backspace_is_short_escaped", "a\\bb"),
        ("form_feed_is_short_escaped", "a\\fb"),
        ("other_control_character_is_lowercase_u_escape", "a\\u001fb\\u0001c"),
        ("delete_character_is_sent_raw", "a\x7fb"),
        ("line_separator_u2028_is_sent_raw", "a b c"),
    ],
)
def test_jdata_control_character_escapes(case: str, fragment: str) -> None:
    assert fragment in _case("jdata_encoding.json", case)["expected"]["body"]


def test_token_expiry_case_names_and_is_expired_boundary() -> None:
    names = {c["name"] for c in _read("token_expiry.json")["cases"]}
    assert "exactly_06_00_ist_is_already_expired" not in names
    issued = _case("token_expiry.json", "issued_exactly_at_06_00_expires_next_day")
    assert issued["expected"] == {"expires_at": "2026-11-11T06:00:00+05:30"}
    at = _case("token_expiry.json", "is_expired_exactly_at_expires_at")
    assert at["expected"] == {"expired": True}
    before = _case("token_expiry.json", "is_expired_one_microsecond_before")
    assert before["expected"] == {"expired": False}


@pytest.mark.parametrize(
    ("case", "issued_at"),
    [
        ("read_z_suffix", "2026-11-10T09:15:30+05:30"),
        ("read_nine_fraction_digits_truncated", "2026-11-10T09:15:30.123456+05:30"),
        ("read_one_fraction_digit", "2026-11-10T09:15:30.100000+05:30"),
    ],
)
def test_token_file_reader_accepts_other_writers(case: str, issued_at: str) -> None:
    assert _case("token_file.json", case)["expected"]["issued_at"] == issued_at


@pytest.mark.parametrize(
    "case",
    [
        "read_offset_without_colon_is_refused",
        "read_basic_format_is_refused",
        "read_space_separator_is_refused",
        "read_ten_fraction_digits_is_refused",
        "read_no_seconds_is_refused",
    ],
)
def test_token_file_reader_refuses_forms_outside_the_pinned_format(case: str) -> None:
    assert _case("token_file.json", case)["expected"] == {"error": "invalid"}


def test_token_file_writer_format_and_refusals() -> None:
    written = _case("token_file.json", "write_seconds_precision_numeric_offset")["expected"]
    assert written["issued_at"] == "2026-11-10T09:15:30+05:30"
    assert _case("token_file.json", "read_naive_is_refused")["expected"] == {"error": "invalid"}
    capped = _case("token_file.json", "read_edited_expiry_is_capped")["expected"]
    assert capped["expires_at"] == "2026-11-11T06:00:00+05:30"


@pytest.mark.parametrize(
    "case",
    [
        "modify_limit",
        "modify_market_price_0_no_amo",
        "modify_validity_and_trigger",
        "cancel",
        "exit_bracket",
        "order_margin_limit",
        "error_modify_without_order_id",
        "error_exit_with_cnc",
    ],
)
def test_order_validation_covers_every_order_call(case: str) -> None:
    _case("order_validation.json", case)


def test_order_validation_modify_market_has_price_0_and_no_amo() -> None:
    body = _case("order_validation.json", "modify_market_price_0_no_amo")["expected"]["body"]
    assert '"prc":"0"' in body and "amo" not in body and '"ret"' not in body


# --------------------------------------------------------------------------- round 1 spec wave


def test_token_file_reader_trims_values() -> None:
    expected = _case("token_file.json", "read_padded_is_accepted")["expected"]
    assert expected["issued_at"] == "2026-11-10T09:15:30+05:30"


@pytest.mark.parametrize(
    "case", ["read_lower_case_t_is_refused", "read_issued_at_in_the_future_is_refused"]
)
def test_token_file_reader_refuses_lower_case_t_and_future_issue(case: str) -> None:
    assert _case("token_file.json", case)["expected"] == {"error": "invalid"}


@pytest.mark.parametrize(
    "case",
    [
        "max_retries_nan_is_rejected",
        "max_retries_float_is_rejected",
        "max_retries_boolean_is_rejected",
        "stream_backlog_limit_nan_is_rejected",
        "reconnect_max_attempts_nan_is_rejected",
        "max_connections_float_is_rejected",
        "connect_timeout_string_is_rejected",
        "log_payloads_string_is_rejected",
        "user_agent_control_character_is_rejected",
    ],
)
def test_config_type_errors_carry_a_message(case: str) -> None:
    expected = _case("config_defaults.json", case)["expected"]
    assert expected["error"] == "ConfigurationError"
    assert isinstance(expected["message"], str) and expected["message"]


def test_config_integer_message() -> None:
    expected = _case("config_defaults.json", "max_retries_nan_is_rejected")["expected"]
    assert expected["message"] == "max_retries must be an integer >= 0"


@pytest.mark.parametrize(
    ("case", "forward_equals_future"),
    [
        ("nfo_index_option_dividend_yield_carries_back", False),
        ("cds_foreign_rate_as_dividend_yield_carries_back", False),
        ("nfo_dividend_yield_equal_to_rate_gives_the_future_price", True),
        ("mcx_dividend_yield_has_no_effect", True),
        ("nfo_same_expiry_future_dividend_yield_has_no_effect", True),
    ],
)
def test_carry_with_dividend_yield(case: str, forward_equals_future: bool) -> None:
    vector = _case("carry_forward.json", case)
    assert "dividend_yield" in vector["input"]
    same = vector["expected"]["forward"] == vector["input"]["future_price"]
    assert same is forward_equals_future


def test_order_validation_errors_name_the_fields() -> None:
    expected = _case("order_validation.json", "error_zero_quantity")["expected"]
    assert expected == {"error": "RequestValidationError", "fields": ["qty"]}
    rule = _case("order_validation.json", "error_mtf_on_mcx")["expected"]
    assert rule == {"error": "RequestValidationError", "fields": []}


def test_not_ok_classification_gives_the_api_error_message() -> None:
    cases = _read("not_ok_classification.json")["cases"]
    messages = [c["expected"].get("message") for c in cases if "emsg" in c["expected"]]
    assert messages and all(isinstance(m, str) and " failed" in m for m in messages)


@pytest.mark.parametrize(
    ("case", "endpoint", "state_changing", "rate_group"),
    [
        ("padded_lower_case_place_order_is_canonical", "PlaceOrder", True, "place_order"),
        ("unknown_name_is_sent_as_given", "limits", False, None),
        ("get_quotes_any_case_has_the_quotes_group", "GetQuotes", False, "quotes"),
    ],
)
def test_request_names(case: str, endpoint: str, state_changing: bool, rate_group: Any) -> None:
    expected = _case("request_names.json", case)["expected"]
    assert expected["endpoint"] == endpoint
    assert expected["state_changing"] is state_changing
    assert expected["rate_group"] == rate_group


def test_request_names_errors() -> None:
    assert _case("request_names.json", "dot_in_name_is_rejected")["expected"] == {
        "error": "ConfigurationError"
    }
    assert _case("request_names.json", "null_value_is_a_type_error")["expected"] == {
        "error": "TypeError"
    }


@pytest.mark.parametrize(
    ("case", "word"),
    [
        ("accepted_ack", "accepted"),
        ("not_ok_is_rejected", "rejected"),
        ("non_json_body_is_unknown", "unknown"),
        ("no_token_is_not_sent", "not_sent"),
        ("closed_client_is_not_sent", "not_sent"),
        ("client_rate_limit_is_not_sent", "not_sent"),
        ("connect_error_is_not_sent", "not_sent"),
        ("place_gtt_accepted_logs_the_al_id", "accepted"),
        ("place_gtt_ok_is_unknown", "unknown"),
        ("place_gtt_not_ok_is_rejected", "rejected"),
        ("place_gtt_read_timeout_is_unknown", "unknown"),
        ("place_gtt_connect_error_is_not_sent", "not_sent"),
        ("modify_gtt_accepted_logs_the_al_id", "accepted"),
        ("modify_gtt_read_timeout_is_unknown", "unknown"),
        ("cancel_gtt_accepted_logs_the_al_id", "accepted"),
        ("cancel_gtt_read_timeout_is_unknown", "unknown"),
    ],
)
def test_outcome_words(case: str, word: str) -> None:
    info = _case("outcome_words.json", case)["expected"]["info"]
    assert info is not None and info.split()[1] == word


def test_invalid_parameters_log_no_outcome_line() -> None:
    expected = _case("outcome_words.json", "invalid_parameters_log_nothing")["expected"]
    assert expected["info"] is None and expected["requests"] == 0


@pytest.mark.parametrize(
    ("case", "error", "requests"),
    [
        ("place_order_read_timeout", "OrderStatusUnknownError", 1),
        ("place_order_connect_error", "NetworkError", 1),
        ("place_order_pool_timeout", "RequestTimeoutError", 1),
        ("read_read_timeout", "RequestTimeoutError", 3),
        ("read_hook_exception", "NetworkError", 1),
        ("place_gtt_read_timeout", "OrderStatusUnknownError", 1),
        ("place_gtt_connection_reset", "OrderStatusUnknownError", 1),
        ("place_gtt_hook_exception", "OrderStatusUnknownError", 1),
        ("place_gtt_connect_error", "NetworkError", 1),
        ("place_gtt_pool_timeout", "RequestTimeoutError", 1),
        ("modify_gtt_read_timeout", "OrderStatusUnknownError", 1),
        ("modify_gtt_connect_error", "NetworkError", 1),
        ("cancel_gtt_read_timeout", "OrderStatusUnknownError", 1),
        ("cancel_gtt_connect_error", "NetworkError", 1),
    ],
)
def test_transport_failures(case: str, error: str, requests: int) -> None:
    expected = _case("transport_failures.json", case)["expected"]
    assert expected == {"error": error, "requests": requests}


def test_script_master_parse_tie_and_trailing_column() -> None:
    tie = _case("script_master_parse.json", "delimiter_tie_first_in_order_wins")["expected"]
    assert tie["rows"] == [{"A": "1", "B|C": "2|3"}]
    trailing = _case("script_master_parse.json", "empty_trailing_header_column")["expected"]
    assert "" in trailing["rows"][0]


def test_script_master_freshness_vanished_file_downloads_once() -> None:
    expected = _case("script_master_freshness.json", "not_modified_but_file_vanished")["expected"]
    assert len(expected["requests"]) == 2
    assert expected["requests"][1]["if_modified_since"] is None


def test_read_payloads_order_book_and_daily_history() -> None:
    orders = _case("read_payloads.json", "orders_sends_uid_only")["expected"]
    assert orders == {"endpoint": "OrderBook", "body": 'jData={"uid":"TEST123"}'}
    daily = _case("read_payloads.json", "daily_history_dates")["expected"]
    assert daily["endpoint"] == "EODChartData" and '"uid"' not in daily["body"]


def test_stream_backlog_never_merges_across_an_ack() -> None:
    case = _case("stream_backlog.json", "no_merge_across_an_ack")
    puts = [op for op in case["input"]["ops"] if "put" in op]
    assert len(case["expected"]["delivered"]) == len(puts)
    assert case["expected"]["merged"] == 0


@pytest.mark.parametrize(
    ("case", "slices"),
    [
        ("nifty_two_full_slices_and_a_remainder", [1755, 1755, 455]),
        ("nifty_exactly_one_full_slice", [1755]),
        ("sensex_option_slice_equals_the_freeze_quantity", [1000, 1000]),
        ("sensex_future", [500, 100]),
        ("mcx_crudeoil_in_units", [2500, 2500, 1000]),
        ("mcx_gold_one_lot_is_one_slice", [1]),
        ("mcx_gold_three_lots_one_lot_a_slice", [1, 1, 1]),
        ("itc_cash_one_share", [1]),
        ("max_slice_quantity_lowers_the_slice", [650, 650, 455]),
        ("freeze_unknown_with_max_slice_quantity", [130, 130]),
        ("freeze_negative_with_max_slice_quantity", [130, 130, 65]),
        ("nifty_one_lot_is_one_slice", [65]),
        ("nifty_two_lots_is_one_slice", [130]),
        ("sensex_option_one_lot_is_one_slice", [20]),
        ("mcx_three_lots_is_one_slice", [300]),
        ("below_max_slice_quantity_is_one_slice", [130]),
    ],
)
def test_slice_plan_design_values(case: str, slices: list[int]) -> None:
    assert _case("slice_plan.json", case)["expected"] == slices


@pytest.mark.parametrize(
    "case",
    [
        "quantity_zero_is_an_error",
        "quantity_negative_is_an_error",
        "quantity_fraction_is_an_error",
        "quantity_boolean_is_an_error",
        "quantity_not_a_whole_number_of_lots_is_an_error",
        "nifty_4000_is_not_a_whole_number_of_lots",
        "mcx_quantity_not_a_whole_number_of_lots_is_an_error",
        "mcx_max_slice_quantity_not_a_whole_number_of_lots_is_an_error",
        "freeze_unknown_without_max_slice_quantity_is_an_error",
        "freeze_zero_without_max_slice_quantity_is_an_error",
        "freeze_negative_without_max_slice_quantity_is_an_error",
        "freeze_below_one_lot_is_an_error",
        "max_slice_quantity_not_a_whole_number_of_lots_is_an_error",
        "more_slices_than_the_default_25_is_an_error",
        "max_slices_zero_is_an_error",
        "max_slices_boolean_is_an_error",
    ],
)
def test_slice_plan_refusals(case: str) -> None:
    assert _case("slice_plan.json", case)["expected"] == {"error": "ValueError"}


def test_slice_plan_inputs_name_every_argument() -> None:
    keys = {
        "exchange",
        "quantity",
        "lot_size",
        "freeze_quantity",
        "max_slice_quantity",
        "max_slices",
    }
    for case in _read("slice_plan.json")["cases"]:
        assert set(case["input"]) == keys, case["name"]


@pytest.mark.parametrize("call", ["place_gtt", "modify_gtt", "cancel_gtt"])
def test_typed_gtt_writes_log_the_al_id_and_name_pending_gtts(call: str) -> None:
    accepted = _case("outcome_words.json", f"{call}_accepted_logs_the_al_id")["expected"]
    assert accepted["error"] is None and accepted["requests"] == 1
    assert accepted["info"].endswith(f"al_id={gen._ALERT_ID}")
    unknown = _case("outcome_words.json", f"{call}_read_timeout_is_unknown")["expected"]
    assert unknown["error"] == "OrderStatusUnknownError" and unknown["requests"] == 1
    assert "pending_gtts()" in unknown["warning"]


def test_inputs_match_ignores_last_digit_float_noise_only() -> None:
    # Round-trip cases store a computed price; its last bits differ between maths libraries.
    stored = {"option_price": 30.521395831033068, "model": "black76", "years": [0.5]}
    assert _inputs_match(stored, {**stored, "option_price": 30.521395831033075})
    assert not _inputs_match(stored, {**stored, "option_price": 30.5214})
    assert not _inputs_match(stored, {**stored, "model": "bsm"})
    assert not _inputs_match(stored, {**stored, "years": [0.25]})
    assert not _inputs_match(stored, {k: v for k, v in stored.items() if k != "model"})
    assert not _inputs_match({"strike": 90}, {"strike": 90.5})
