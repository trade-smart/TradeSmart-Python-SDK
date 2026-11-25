import json

import pytest

from tests.conftest import fixture
from tradesmart._client_core import STATE_CHANGING_ENDPOINTS, build_raw_call
from tradesmart._transport.codec import encode_jdata, parse_response
from tradesmart.errors import (
    APIError,
    AuthenticationError,
    NotFoundError,
    OrderRejectedError,
    OrderStatusUnknownError,
    RateLimitError,
    ResponseParseError,
    ServerError,
)


def test_encode_jdata_compact_and_plain():
    assert encode_jdata({"uid": "TEST123", "exch": "NSE"}) == 'jData={"uid":"TEST123","exch":"NSE"}'


def test_encode_jdata_escapes_form_specials():
    assert (
        encode_jdata({"tsym": "M&M-EQ", "remarks": "a+b 5%"})
        == 'jData={"tsym":"M%26M-EQ","remarks":"a%2Bb 5%25"}'
    )


@pytest.mark.parametrize(
    "name,status,state_changing,exc",
    [
        ("invalid_token", 200, False, AuthenticationError),
        ("session_expired", 401, False, AuthenticationError),
        ("order_reject", 200, True, OrderRejectedError),
        ("order_reject", 200, False, APIError),
        ("search_no_data", 404, False, NotFoundError),
    ],
)
def test_not_ok_classification(name, status, state_changing, exc):
    with pytest.raises(exc) as ei:
        parse_response(
            endpoint="X",
            status_code=status,
            text=json.dumps(fixture("errors")[name]),
            expect="object",
            allow_empty=False,
            state_changing=state_changing,
        )
    assert ei.value.emsg == fixture("errors")[name]["emsg"]


def test_request_time_is_populated():
    with pytest.raises(AuthenticationError) as ei:
        parse_response(
            endpoint="X",
            status_code=200,
            text=json.dumps(fixture("errors")["invalid_token"]),
            expect="object",
            allow_empty=False,
            state_changing=False,
        )
    assert ei.value.request_time == "14:30:01 15-01-2025"


def test_no_data_is_empty_only_when_allowed():
    text = json.dumps(fixture("errors")["no_data"])
    assert (
        parse_response(
            endpoint="OrderBook",
            status_code=200,
            text=text,
            expect="list",
            allow_empty=True,
            state_changing=False,
        )
        == []
    )
    with pytest.raises(APIError):
        parse_response(
            endpoint="Limits",
            status_code=200,
            text=text,
            expect="object",
            allow_empty=False,
            state_changing=False,
        )


def test_list_endpoint_rejects_ok_object():
    with pytest.raises(ResponseParseError):
        parse_response(
            endpoint="TradeBook",
            status_code=200,
            text='{"stat":"Ok"}',
            expect="list",
            allow_empty=True,
            state_changing=False,
        )


def test_empty_list_passes_for_list_endpoint():
    assert (
        parse_response(
            endpoint="EODChartData",
            status_code=200,
            text="[]",
            expect="list",
            allow_empty=True,
            state_changing=False,
        )
        == []
    )


def test_ok_object_returned_for_object_endpoint():
    assert parse_response(
        endpoint="Limits",
        status_code=200,
        text='{"stat":"Ok","cash":"1"}',
        expect="object",
        allow_empty=False,
        state_changing=False,
    ) == {"stat": "Ok", "cash": "1"}


def test_list_for_object_endpoint_is_parse_error():
    with pytest.raises(ResponseParseError):
        parse_response(
            endpoint="Limits",
            status_code=200,
            text="[]",
            expect="object",
            allow_empty=False,
            state_changing=False,
        )


def test_empty_list_allowed_for_object_endpoint_when_allow_empty():
    assert (
        parse_response(
            endpoint="X",
            status_code=200,
            text="[]",
            expect="object",
            allow_empty=True,
            state_changing=False,
        )
        == []
    )


def test_parse_non_json_5xx_is_server_error():
    with pytest.raises(ServerError) as ei:
        parse_response(
            endpoint="X",
            status_code=502,
            text="<html>Bad Gateway</html>",
            expect="object",
            allow_empty=False,
            state_changing=False,
        )
    assert "Bad Gateway" in str(ei.value.raw)


def test_parse_empty_200_is_parse_error():
    with pytest.raises(ResponseParseError) as ei:
        parse_response(
            endpoint="X",
            status_code=200,
            text="",
            expect="object",
            allow_empty=False,
            state_changing=False,
        )
    assert ei.value.body == ""


@pytest.mark.parametrize(
    "status,exc", [(429, RateLimitError), (401, AuthenticationError), (403, AuthenticationError)]
)
def test_non_json_by_status(status, exc):
    with pytest.raises(exc):
        parse_response(
            endpoint="X",
            status_code=status,
            text="nope",
            expect="object",
            allow_empty=False,
            state_changing=False,
        )


def test_429_is_rate_limit():
    with pytest.raises(RateLimitError):
        parse_response(
            endpoint="GetQuotes",
            status_code=429,
            text='{"stat":"Not_Ok","emsg":"Too many"}',
            expect="object",
            allow_empty=False,
            state_changing=False,
        )


def test_5xx_json_not_ok_is_server_error():
    with pytest.raises(ServerError):
        parse_response(
            endpoint="X",
            status_code=500,
            text='{"stat":"Not_Ok","emsg":"boom"}',
            expect="object",
            allow_empty=False,
            state_changing=False,
        )


def test_5xx_json_not_ok_on_state_changing_is_order_rejected():
    with pytest.raises(OrderRejectedError) as info:
        parse_response(
            endpoint="X",
            status_code=500,
            text='{"stat":"Not_Ok","emsg":"boom"}',
            expect="object",
            allow_empty=False,
            state_changing=True,
        )
    assert info.value.http_status == 500 and not isinstance(info.value, ServerError)


@pytest.mark.parametrize(
    "text,status,exc",
    [
        ('{"foo":1}', 200, ResponseParseError),
        ("123", 200, ResponseParseError),
        ('"x"', 200, ResponseParseError),
        ('{"foo":1}', 500, ServerError),
        ('{"foo":1}', 404, ResponseParseError),
    ],
)
def test_other_json(text, status, exc):
    with pytest.raises(exc):
        parse_response(
            endpoint="X",
            status_code=status,
            text=text,
            expect="object",
            allow_empty=False,
            state_changing=False,
        )


# --- ambiguous state-changing responses (final review C1) ----------------------


@pytest.mark.parametrize(
    "text,status",
    [
        ("<html>Gateway Timeout</html>", 504),
        ("<html>Bad Gateway</html>", 502),
        ("<html>Service Unavailable</html>", 503),
        ("", 200),
        ("not json", 429),
        ("not json", 401),
        ('{"foo":1}', 200),
        ('{"foo":1}', 500),
        ('{"stat":"Maybe"}', 200),
        ("123", 200),
        ('"x"', 200),
        ("[]", 200),
        ('[{"stat":"Ok"}]', 200),
        ("[]", 500),
    ],
)
def test_state_changing_ambiguous_body_is_unknown(text, status):
    with pytest.raises(OrderStatusUnknownError) as ei:
        parse_response(
            endpoint="PlaceOrder",
            status_code=status,
            text=text,
            expect="object",
            allow_empty=False,
            state_changing=True,
        )
    assert ei.value.endpoint == "PlaceOrder"
    assert ei.value.http_status == status and ei.value.body == text


def test_state_changing_non_json_keeps_no_decode_error_chain():
    with pytest.raises(OrderStatusUnknownError) as ei:
        parse_response(
            endpoint="PlaceOrder",
            status_code=504,
            text="<html>Gateway Timeout</html>",
            expect="object",
            allow_empty=False,
            state_changing=True,
        )
    # The JSONDecodeError holds the unredacted body: never chained.
    assert ei.value.__cause__ is None and ei.value.__suppress_context__


@pytest.mark.parametrize(
    "body",
    [
        {"stat": "Not_Ok"},
        {"stat": "Not_Ok", "emsg": None},
        {"stat": "Not_Ok", "emsg": ""},
        {"stat": "Not_Ok", "emsg": "   "},
        {"stat": "Not_Ok", "emsg": 5},
    ],
)
@pytest.mark.parametrize("status", [200, 401, 500])
def test_not_ok_without_a_usable_emsg_says_so(body: dict, status: int) -> None:
    with pytest.raises(APIError) as ei:
        parse_response(
            endpoint="Limits",
            status_code=status,
            text=json.dumps(body),
            expect="object",
            allow_empty=False,
            state_changing=False,
        )
    assert str(ei.value) == "Limits failed (no error message from the server)"
    assert "None" not in str(ei.value)


def test_not_ok_with_an_emsg_keeps_it_in_the_message() -> None:
    with pytest.raises(APIError) as ei:
        parse_response(
            endpoint="Limits",
            status_code=200,
            text='{"stat":"Not_Ok","emsg":"Invalid Input"}',
            expect="object",
            allow_empty=False,
            state_changing=False,
        )
    assert str(ei.value) == "Limits failed: Invalid Input"


@pytest.mark.parametrize("status", [200, 500])
def test_state_changing_list_call_with_an_ok_object_is_unknown(status: int) -> None:
    """``request(..., state_changing=True, expect="list")`` may have run: never a parse error."""
    with pytest.raises(OrderStatusUnknownError) as ei:
        parse_response(
            endpoint="PlaceMultiOrder",
            status_code=status,
            text='{"stat":"Ok","norenordno":"1"}',
            expect="list",
            allow_empty=False,
            state_changing=True,
        )
    assert ei.value.http_status == status
    assert "expected a JSON list but received an object" in ei.value.message


def test_read_list_call_with_an_ok_object_is_still_a_parse_error() -> None:
    with pytest.raises(ResponseParseError):
        parse_response(
            endpoint="OrderBook",
            status_code=200,
            text='{"stat":"Ok"}',
            expect="list",
            allow_empty=False,
            state_changing=False,
        )


@pytest.mark.parametrize(
    "status,exc",
    [(200, OrderRejectedError), (500, OrderRejectedError), (401, AuthenticationError)],
)
def test_state_changing_json_not_ok_stays_definitive(status, exc):
    with pytest.raises(exc):
        parse_response(
            endpoint="PlaceOrder",
            status_code=status,
            text='{"stat":"Not_Ok","emsg":"rejected"}',
            expect="object",
            allow_empty=False,
            state_changing=True,
        )


# --- list body on an error status (final review I1) ----------------------------


@pytest.mark.parametrize(
    "status,exc",
    [(500, ServerError), (404, NotFoundError), (400, APIError), (429, RateLimitError)],
)
def test_list_body_on_error_status_raises(status, exc):
    with pytest.raises(exc) as ei:
        parse_response(
            endpoint="OrderBook",
            status_code=status,
            text="[]",
            expect="list",
            allow_empty=True,
            state_changing=False,
        )
    assert isinstance(ei.value, APIError) and ei.value.http_status == status


def test_list_body_on_200_still_returned():
    assert (
        parse_response(
            endpoint="OrderBook",
            status_code=200,
            text="[]",
            expect="list",
            allow_empty=True,
            state_changing=False,
        )
        == []
    )


def test_ambiguous_body_is_redacted():
    with pytest.raises(OrderStatusUnknownError) as info:
        parse_response(
            endpoint="GenAcsTok",
            status_code=200,
            text='{"access_token":"SECRET-XYZ"}',
            expect="object",
            allow_empty=False,
            state_changing=True,
        )
    assert "SECRET-XYZ" not in info.value.body
    assert "access_token" in info.value.body


def test_unexpected_body_is_redacted():
    with pytest.raises(ResponseParseError) as info:
        parse_response(
            endpoint="GenAcsTok",
            status_code=200,
            text='{"access_token":"SECRET-XYZ"}',
            expect="object",
            allow_empty=False,
            state_changing=False,
        )
    assert "SECRET-XYZ" not in info.value.body
    assert "access_token" in info.value.body


@pytest.mark.parametrize("name", sorted(STATE_CHANGING_ENDPOINTS))
def test_request_forces_state_changing_for_order_endpoints(name):
    call = build_raw_call("TEST123", name, {}, state_changing=False, expect="object")
    assert call.spec.state_changing is True


def _parse(text, status, *, state_changing, expect="object"):
    return parse_response(
        endpoint="GenAcsTok",
        status_code=status,
        text=text,
        expect=expect,
        allow_empty=False,
        state_changing=state_changing,
    )


def test_redacts_json_list_body():
    with pytest.raises(OrderStatusUnknownError) as info:
        _parse('[{"access_token":"SECRET-XYZ"}]', 200, state_changing=True)
    assert "SECRET-XYZ" not in info.value.body
    assert "access_token" in info.value.body


def test_redacts_plain_text_token_on_state_changing_502():
    with pytest.raises(OrderStatusUnknownError) as info:
        _parse("bad gateway access_token=SECRET-XYZ", 502, state_changing=True)
    assert "SECRET-XYZ" not in info.value.body


def test_not_ok_raw_is_redacted_but_classified():
    body = '{"stat":"Not_Ok","emsg":"x","access_token":"SECRET-XYZ"}'
    with pytest.raises(AuthenticationError) as info:
        _parse(body, 401, state_changing=True)
    assert "SECRET-XYZ" not in str(info.value.raw)
    assert info.value.emsg == "x"


def test_non_json_503_raw_is_redacted():
    with pytest.raises(ServerError) as info:
        _parse('oops "access_token":"SECRET-XYZ"', 503, state_changing=False)
    assert "SECRET-XYZ" not in str(info.value.raw)


def test_error_status_list_raw_is_redacted():
    with pytest.raises(APIError) as info:
        _parse('[{"access_token":"SECRET-XYZ"}]', 400, state_changing=False, expect="list")
    assert "SECRET-XYZ" not in str(info.value.raw)


def test_encode_jdata_nested_keeps_order_and_escapes():
    payload = {
        "tsym": "M&M-EQ",
        "oivariable": [{"var_name": "x", "d": "1+2"}, {"var_name": "y", "d": "50%"}],
        "place_order_params": {"remarks": '₹ "a"\n', "qty": "1"},
        "empty": [],
        "none": {},
    }
    assert encode_jdata(payload) == (
        'jData={"tsym":"M%26M-EQ","oivariable":[{"var_name":"x","d":"1%2B2"},'
        '{"var_name":"y","d":"50%25"}],"place_order_params":{"remarks":"₹ \\"a\\"\\n","qty":"1"},'
        '"empty":[],"none":{}}'
    )


def test_nested_lone_surrogate_refused_on_top_key():
    from tradesmart._transport.codec import encode_request_body
    from tradesmart.errors import RequestValidationError

    payload = {
        "uid": "TEST123",
        "place_order_params": {"remarks": "\ud800"},
        "oivariable": [{"d": "\udc00"}, {"d": "\ud800"}],
    }
    with pytest.raises(RequestValidationError) as ei:
        encode_request_body(payload)
    assert [e["loc"] for e in ei.value.errors] == [("oivariable",), ("place_order_params",)]
    assert "\ud800" not in str(ei.value) and "remarks" not in str(ei.value)


def test_nested_lone_surrogate_in_nested_key_refused_on_top_key():
    from tradesmart._transport.codec import encode_request_body
    from tradesmart.errors import RequestValidationError

    with pytest.raises(RequestValidationError) as ei:
        encode_request_body({"p": {"\ud800": "x"}})
    assert [e["loc"] for e in ei.value.errors] == [("p",)]


def test_build_raw_call_nested_none_is_type_error_naming_top_key_only():
    with pytest.raises(TypeError) as ei:
        build_raw_call(
            "TEST123",
            "GetQuotes",
            {"x": [{"y": None, "secret": "S3CR3T"}]},
            state_changing=False,
            expect="object",
        )
    assert "x" in str(ei.value) and "secret" not in str(ei.value) and "S3CR3T" not in str(ei.value)
