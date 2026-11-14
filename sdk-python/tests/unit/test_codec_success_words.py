"""Per-endpoint success words (GTT and iceberg write endpoints).

``PlaceGTTOrder`` -> ``Oi created``, ``ModifyGTTOrder`` -> ``Oi replaced``,
``CancelGTTOrder`` -> ``Oi delete success`` (each also needs a non-blank string ``al_id`` and
no non-blank ``emsg``); the iceberg write endpoints answer ``true`` (``false`` is a definite
rejection). The GTT words compare ignoring letter case only (UAT answers ``OI created``,
spec F32); nothing is trimmed or normalised, and the iceberg words stay exact. Anything else
on these endpoints is ``OrderStatusUnknownError``; every other endpoint still accepts only
``Ok``.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from tradesmart._client_core import (
    ACK_ID_FIELDS,
    STATE_CHANGING_ENDPOINTS,
    SUCCESS_STATS,
    SUCCESS_STATS_IGNORE_CASE,
    build_raw_call,
)
from tradesmart._transport.codec import OK_STATS, parse_response
from tradesmart.endpoints.base import EndpointSpec
from tradesmart.errors import (
    AuthenticationError,
    NotFoundError,
    OrderRejectedError,
    OrderStatusUnknownError,
    RateLimitError,
    ResponseParseError,
)

GTT_WORDS = {
    "PlaceGTTOrder": "Oi created",
    "ModifyGTTOrder": "Oi replaced",
    "CancelGTTOrder": "Oi delete success",
}
ICEBERG = ("PlaceIceBergOrder", "ModifyIceBergOrder", "CancelIceBergOrder")
NEW_ORDER_ENDPOINTS = (*GTT_WORDS, *ICEBERG)


def _gtt(endpoint: str, body: Any, *, status: int = 200) -> Any:
    return parse_response(
        endpoint=endpoint,
        status_code=status,
        text=body if isinstance(body, str) else json.dumps(body),
        expect="object",
        allow_empty=False,
        state_changing=True,
        success_stats=frozenset({GTT_WORDS[endpoint]}),
        requires_ack_id="al_id",
        stats_ignore_case=True,
    )


def _iceberg(endpoint: str, body: Any, *, status: int = 200) -> Any:
    return parse_response(
        endpoint=endpoint,
        status_code=status,
        text=body if isinstance(body, str) else json.dumps(body),
        expect="object",
        allow_empty=False,
        state_changing=True,
        success_stats=frozenset({"true"}),
    )


# --- the spec and the maps -------------------------------------------------------------


def test_endpoint_spec_defaults_accept_only_ok() -> None:
    spec: EndpointSpec[Any] = EndpointSpec(name="Limits", parse=lambda b: b)
    assert spec.success_stats == frozenset({"Ok"}) == OK_STATS
    assert spec.requires_ack_id is None
    assert spec.stats_ignore_case is False


def test_success_stats_map_has_exactly_the_documented_words() -> None:
    assert dict(SUCCESS_STATS) == {
        "PlaceGTTOrder": frozenset({"Oi created"}),
        "ModifyGTTOrder": frozenset({"Oi replaced"}),
        "CancelGTTOrder": frozenset({"Oi delete success"}),
        "PlaceIceBergOrder": frozenset({"true"}),
        "ModifyIceBergOrder": frozenset({"true"}),
        "CancelIceBergOrder": frozenset({"true"}),
    }


@pytest.mark.parametrize("name", NEW_ORDER_ENDPOINTS)
def test_gtt_and_iceberg_writes_are_state_changing(name: str) -> None:
    assert name in STATE_CHANGING_ENDPOINTS


def test_only_the_gtt_writes_ignore_letter_case() -> None:
    assert frozenset(GTT_WORDS) == SUCCESS_STATS_IGNORE_CASE


# --- GTT ---------------------------------------------------------------------------------


@pytest.mark.parametrize("endpoint", GTT_WORDS)
def test_gtt_exact_word_with_al_id_is_success(endpoint: str) -> None:
    body = {
        "request_time": "10:00:00 12-11-2026",
        "stat": GTT_WORDS[endpoint],
        "al_id": "26111200000010",
    }
    assert _gtt(endpoint, body) == body


@pytest.mark.parametrize("endpoint", GTT_WORDS)
def test_gtt_word_with_blank_emsg_is_success(endpoint: str) -> None:
    body = {"stat": GTT_WORDS[endpoint], "al_id": "26111200000010", "emsg": "  "}
    assert _gtt(endpoint, body) == body


def _case_variants(word: str) -> list[str]:
    return [word.upper(), word.lower(), word.swapcase(), word.title()]


@pytest.mark.parametrize(
    ("endpoint", "stat"),
    [
        *[(endpoint, v) for endpoint, word in GTT_WORDS.items() for v in _case_variants(word)],
        ("PlaceGTTOrder", "OI created"),  # the UAT answer (spec F32)
        ("PlaceGTTOrder", "oi CREATED"),
        ("ModifyGTTOrder", "OI replaced"),
        ("CancelGTTOrder", "OI DELETE SUCCESS"),
    ],
)
def test_gtt_word_in_any_letter_case_is_success(endpoint: str, stat: str) -> None:
    body = {"stat": stat, "al_id": "26111300000001"}
    assert _gtt(endpoint, body) == body


@pytest.mark.parametrize(
    "stat",
    ["OI deleted", "Oi deleted", "Oi  created", " Oi created", "Oi created ", "OI  CREATED"],
)
def test_gtt_case_rule_changes_nothing_but_letter_case(stat: str) -> None:
    # Only letter case is ignored: other words, spacing and padding stay unknown.
    endpoint = "CancelGTTOrder" if "delete" in stat.lower() else "PlaceGTTOrder"
    with pytest.raises(OrderStatusUnknownError):
        _gtt(endpoint, {"stat": stat, "al_id": "26111300000001"})


@pytest.mark.parametrize("endpoint", GTT_WORDS)
def test_gtt_case_rule_keeps_the_ack_rules(endpoint: str) -> None:
    word = GTT_WORDS[endpoint].upper()
    for body in (
        {"stat": word},
        {"stat": word, "al_id": " "},
        {"stat": word, "al_id": "1", "emsg": "x"},
    ):
        with pytest.raises(OrderStatusUnknownError):
            _gtt(endpoint, body)
    with pytest.raises(OrderStatusUnknownError):
        _gtt(endpoint, json.dumps([{"stat": word, "al_id": "1"}]))


def test_parse_response_compares_exactly_unless_asked_to_ignore_case() -> None:
    with pytest.raises(OrderStatusUnknownError):
        parse_response(
            endpoint="PlaceGTTOrder",
            status_code=200,
            text='{"stat":"OI created","al_id":"1"}',
            expect="object",
            allow_empty=False,
            state_changing=True,
            success_stats=frozenset({"Oi created"}),
            requires_ack_id="al_id",
        )


def _gtt_unknown_bodies(word: str) -> list[tuple[str, Any]]:
    return [
        ("ok_with_al_id", {"stat": "Ok", "al_id": "1"}),
        ("word_without_al_id", {"stat": word}),
        ("word_with_blank_al_id", {"stat": word, "al_id": "   "}),
        ("word_with_null_al_id", {"stat": word, "al_id": None}),
        ("word_with_numeric_al_id", {"stat": word, "al_id": 26111200000010}),
        ("word_with_emsg", {"stat": word, "al_id": "1", "emsg": "Invalid trigger"}),
        ("word_with_non_string_emsg", {"stat": word, "al_id": "1", "emsg": 5}),
        ("double_space", {"stat": word.replace(" ", "  ", 1), "al_id": "1"}),
        ("tab_for_space", {"stat": word.replace(" ", "\t", 1), "al_id": "1"}),
        ("trailing_space", {"stat": word + " ", "al_id": "1"}),
        ("leading_space", {"stat": " " + word, "al_id": "1"}),
        ("missing_stat", {"al_id": "1"}),
        ("stat_is_a_list", {"stat": [word], "al_id": "1"}),
        ("stat_is_an_object", {"stat": {"w": word}, "al_id": "1"}),
        ("another_endpoint_word", {"stat": "Oi something", "al_id": "1"}),
        ("json_true", {"stat": True, "al_id": "1"}),
    ]


@pytest.mark.parametrize(
    ("endpoint", "case", "body"),
    [
        (endpoint, case, body)
        for endpoint, word in GTT_WORDS.items()
        for case, body in _gtt_unknown_bodies(word)
    ],
)
def test_gtt_anything_else_is_unknown(endpoint: str, case: str, body: Any) -> None:
    with pytest.raises(OrderStatusUnknownError) as info:
        _gtt(endpoint, body)
    assert info.value.endpoint == endpoint
    assert "pending_gtts()" in str(info.value)


@pytest.mark.parametrize("endpoint", GTT_WORDS)
def test_gtt_word_of_another_gtt_endpoint_is_unknown(endpoint: str) -> None:
    for other, word in GTT_WORDS.items():
        if other != endpoint:
            with pytest.raises(OrderStatusUnknownError):
                _gtt(endpoint, {"stat": word, "al_id": "1"})


@pytest.mark.parametrize("endpoint", GTT_WORDS)
def test_gtt_not_ok_session_expired_on_401_is_authentication(endpoint: str) -> None:
    body = {"stat": "Not_Ok", "emsg": "Session Expired :  Invalid Session Key"}
    with pytest.raises(AuthenticationError):
        _gtt(endpoint, body, status=401)


@pytest.mark.parametrize("endpoint", GTT_WORDS)
def test_gtt_not_ok_is_a_definite_rejection(endpoint: str) -> None:
    with pytest.raises(OrderRejectedError) as info:
        _gtt(endpoint, {"stat": "Not_Ok", "emsg": "Invalid Input"})
    assert info.value.message == f"{endpoint} failed: Invalid Input"


def test_gtt_not_ok_on_429_is_rate_limit() -> None:
    with pytest.raises(RateLimitError):
        _gtt("PlaceGTTOrder", {"stat": "Not_Ok", "emsg": "Too many"}, status=429)


@pytest.mark.parametrize("text", ["<html>oops</html>", "", "[]", "null"])
def test_gtt_non_object_bodies_are_unknown(text: str) -> None:
    with pytest.raises(OrderStatusUnknownError):
        _gtt("PlaceGTTOrder", text)


# --- iceberg -------------------------------------------------------------------------------


@pytest.mark.parametrize("endpoint", ICEBERG)
def test_iceberg_true_is_success(endpoint: str) -> None:
    body = {"request_time": "10:00:00 12-11-2026", "stat": "true", "aq_id": "1"}
    assert _iceberg(endpoint, body) == body


@pytest.mark.parametrize("endpoint", ICEBERG)
def test_iceberg_false_is_a_rejection_with_rejreason(endpoint: str) -> None:
    with pytest.raises(OrderRejectedError) as info:
        _iceberg(endpoint, {"stat": "false", "rejreason": "Insufficient margin"})
    assert info.value.message == f"{endpoint} failed: Insufficient margin"
    assert info.value.emsg == "Insufficient margin"


def test_iceberg_false_prefers_a_non_blank_rejreason() -> None:
    with pytest.raises(OrderRejectedError) as info:
        _iceberg("PlaceIceBergOrder", {"stat": "false", "emsg": "Bad", "rejreason": "Other"})
    assert info.value.message == "PlaceIceBergOrder failed: Other"
    assert info.value.emsg == "Other"


def test_iceberg_false_falls_back_to_emsg_when_rejreason_is_blank() -> None:
    with pytest.raises(OrderRejectedError) as info:
        _iceberg("PlaceIceBergOrder", {"stat": "false", "emsg": "Bad", "rejreason": "  "})
    assert info.value.message == "PlaceIceBergOrder failed: Bad"


@pytest.mark.parametrize(
    ("status", "error"),
    [(403, AuthenticationError), (429, RateLimitError), (404, NotFoundError)],
)
def test_iceberg_false_keeps_the_status_classes(status: int, error: type[Exception]) -> None:
    with pytest.raises(error):
        _iceberg("ModifyIceBergOrder", {"stat": "false", "rejreason": "x"}, status=status)


def test_iceberg_false_on_500_is_a_definite_rejection() -> None:
    with pytest.raises(OrderRejectedError):
        _iceberg("CancelIceBergOrder", {"stat": "false", "rejreason": "x"}, status=500)


@pytest.mark.parametrize("endpoint", ["CustomTrueEndpoint", "PlaceOrder", "PlaceGTTOrder"])
def test_false_is_not_a_rejection_outside_the_iceberg_writes(endpoint: str) -> None:
    # A future endpoint whose success word is "true" does not inherit the iceberg "false" rule.
    with pytest.raises(OrderStatusUnknownError):
        parse_response(
            endpoint=endpoint,
            status_code=200,
            text='{"stat":"false","rejreason":"x"}',
            expect="object",
            allow_empty=False,
            state_changing=True,
            success_stats=frozenset({"true"}),
        )


@pytest.mark.parametrize("endpoint", NEW_ORDER_ENDPOINTS)
@pytest.mark.parametrize("text", ["[]", '[{"stat":"Oi created","al_id":"1"}]', '[{"stat":"true"}]'])
@pytest.mark.parametrize("expect", ["object", "list"])
def test_any_list_on_a_gtt_or_iceberg_write_is_unknown(
    endpoint: str, text: str, expect: Any
) -> None:
    with pytest.raises(OrderStatusUnknownError):
        parse_response(
            endpoint=endpoint,
            status_code=200,
            text=text,
            expect=expect,
            allow_empty=True,
            state_changing=True,
            success_stats=SUCCESS_STATS[endpoint],
            requires_ack_id=ACK_ID_FIELDS.get(endpoint),
        )


def test_a_list_on_place_order_with_expect_list_is_unchanged() -> None:
    # Out of scope (PlaceMultiOrder may answer a list): Ok-word endpoints keep row 6.
    assert (
        parse_response(
            endpoint="PlaceMultiOrder",
            status_code=200,
            text="[]",
            expect="list",
            allow_empty=False,
            state_changing=True,
        )
        == []
    )


def test_iceberg_false_without_a_reason() -> None:
    with pytest.raises(OrderRejectedError) as info:
        _iceberg("PlaceIceBergOrder", {"stat": "false", "emsg": " "})
    assert info.value.message == "PlaceIceBergOrder failed (no error message from the server)"


def test_iceberg_false_on_401_is_authentication() -> None:
    with pytest.raises(AuthenticationError):
        _iceberg("PlaceIceBergOrder", {"stat": "false", "rejreason": "x"}, status=401)


def test_iceberg_not_ok_is_a_definite_rejection() -> None:
    with pytest.raises(OrderRejectedError):
        _iceberg("CancelIceBergOrder", {"stat": "Not_Ok", "emsg": "Invalid Input"})


@pytest.mark.parametrize(
    "body",
    [
        {"stat": "True"},
        {"stat": "TRUE"},
        {"stat": "true "},
        {"stat": True},
        {"stat": False},
        {"stat": "False"},
        {"stat": "Ok", "aq_id": "1"},
        {"aq_id": "1"},
    ],
)
def test_iceberg_anything_else_is_unknown(body: Any) -> None:
    with pytest.raises(OrderStatusUnknownError) as info:
        _iceberg("PlaceIceBergOrder", body)
    assert "orders()" in str(info.value)


# --- every other endpoint still accepts only Ok (regression) ---------------------------------


@pytest.mark.parametrize(
    "word", ["Oi created", "Oi replaced", "Oi delete success", "true", "false"]
)
@pytest.mark.parametrize("endpoint", ["PlaceOrder", "ModifyOrder", "CancelOrder", "ExitSNOOrder"])
def test_existing_order_endpoints_accept_only_ok(endpoint: str, word: str) -> None:
    with pytest.raises(OrderStatusUnknownError):
        parse_response(
            endpoint=endpoint,
            status_code=200,
            text=json.dumps({"stat": word, "al_id": "1", "norenordno": "1"}),
            expect="object",
            allow_empty=False,
            state_changing=True,
        )


@pytest.mark.parametrize("word", ["Oi created", "true", "false"])
def test_reads_accept_only_ok(word: str) -> None:
    with pytest.raises(ResponseParseError):
        parse_response(
            endpoint="Limits",
            status_code=200,
            text=json.dumps({"stat": word, "al_id": "1"}),
            expect="object",
            allow_empty=False,
            state_changing=False,
        )


def test_a_non_string_stat_never_breaks_the_lookup() -> None:
    with pytest.raises(OrderStatusUnknownError):
        parse_response(
            endpoint="PlaceOrder",
            status_code=200,
            text='{"stat": ["Ok"]}',
            expect="object",
            allow_empty=False,
            state_changing=True,
        )


# --- request(): names, flags and words ------------------------------------------------------


@pytest.mark.parametrize(
    ("given", "canonical"),
    [
        ("placegttorder", "PlaceGTTOrder"),
        (" /PLACEGTTORDER/ ", "PlaceGTTOrder"),
        ("modifygttorder", "ModifyGTTOrder"),
        ("CancelGttOrder", "CancelGTTOrder"),
        ("PLACEICEBERGORDER", "PlaceIceBergOrder"),
        ("modifyicebergorder", "ModifyIceBergOrder"),
        ("cancelIcebergOrder", "CancelIceBergOrder"),
    ],
)
def test_request_canonicalises_gtt_and_iceberg_names(given: str, canonical: str) -> None:
    call = build_raw_call("TEST123", given, {}, state_changing=False, expect="object")
    assert call.spec.name == canonical
    assert call.spec.state_changing is True
    assert call.spec.success_stats == SUCCESS_STATS[canonical]
    assert call.spec.rate_group is None


@pytest.mark.parametrize("name", GTT_WORDS)
def test_request_gtt_requires_al_id(name: str) -> None:
    call = build_raw_call("TEST123", name.lower(), {}, state_changing=False, expect="object")
    assert call.spec.requires_ack_id == "al_id"
    assert call.spec.stats_ignore_case is True


@pytest.mark.parametrize("name", [*ICEBERG, "PlaceOrder", "Limits", "OrderBook"])
def test_request_other_names_require_no_ack_id(name: str) -> None:
    call = build_raw_call("TEST123", name, {}, state_changing=False, expect="object")
    assert call.spec.requires_ack_id is None
    assert call.spec.stats_ignore_case is False


@pytest.mark.parametrize("name", ["PlaceOrder", "Limits", "OrderBook", "GetPendingGTTOrder"])
def test_request_other_names_accept_only_ok(name: str) -> None:
    call = build_raw_call("TEST123", name, {}, state_changing=False, expect="object")
    assert call.spec.success_stats == frozenset({"Ok"})


def test_gtt_reads_are_not_state_changing() -> None:
    for name in ("GetPendingGTTOrder", "GetTrgOi", "GetEnabledGTTs", "GetIceBergOrder"):
        call = build_raw_call("TEST123", name, {}, state_changing=False, expect="list")
        assert call.spec.state_changing is False, name
