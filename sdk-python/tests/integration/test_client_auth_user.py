import traceback
from datetime import datetime

import httpx
import pytest
import respx

from tests.conftest import BASE, fixture
from tradesmart import (
    AccessToken,
    AsyncTradeSmart,
    AuthenticationError,
    ClientConfig,
    ConfigurationError,
    OrderStatusUnknownError,
    TradeSmart,
)
from tradesmart.auth import compute_checksum
from tradesmart.models.base import IST


@respx.mock
def test_generate_token_then_user_details() -> None:
    respx.post(f"{BASE}/GenAcsTok").respond(200, json=fixture("gen_acs_tok_success"))
    ud = respx.post(f"{BASE}/UserDetails").respond(200, json=fixture("user_details_success"))
    with TradeSmart("TEST123", app_key="ABC", secret_key="123") as c:
        tok = c.generate_session("x1y2z3")
        assert tok.user_id == "TEST123"
        details = c.profile()
    assert ud.calls.last.request.headers["Authorization"] == "Bearer test-access-token"
    assert ud.calls.last.request.content == b'jData={"uid":"TEST123"}'
    assert details.exarr is not None and details.exarr[0] == "NSE"
    assert details.prarr is not None and details.prarr[0].product == "C"
    assert details.user_id == "TEST123" and details.request_time is not None


@respx.mock
def test_gen_acs_tok_failure_is_authentication_error() -> None:
    respx.post(f"{BASE}/GenAcsTok").respond(200, json=fixture("errors")["no_data"])
    client = TradeSmart("TEST123", app_key="ABC", secret_key="123")
    with pytest.raises(AuthenticationError) as info:
        client.generate_session("used-code")
    assert info.value.emsg and info.value.http_status == 200
    assert "used-code" not in str(info.value)


_NO_EMSG = "GenAcsTok failed (no error message from the server)"
_GEN_ACS_TOK_FAILURES = [
    pytest.param(200, {"stat": "Not_Ok"}, _NO_EMSG, id="200-no-emsg"),
    pytest.param(500, {"stat": "Not_Ok"}, _NO_EMSG, id="500-no-emsg"),
    pytest.param(401, {"stat": "Not_Ok"}, _NO_EMSG, id="401-no-emsg"),
    pytest.param(401, {"stat": "Not_Ok", "emsg": " "}, _NO_EMSG, id="401-blank-emsg"),
    pytest.param(
        200,
        {"stat": "Not_Ok", "emsg": "Invalid Input"},
        "GenAcsTok failed: Invalid Input",
        id="200-emsg",
    ),
    pytest.param(
        403,
        {"stat": "Not_Ok", "emsg": "Invalid Session"},
        "GenAcsTok failed: Invalid Session",
        id="403-emsg",
    ),
]


@pytest.mark.parametrize(("status", "body", "message"), _GEN_ACS_TOK_FAILURES)
@respx.mock
def test_gen_acs_tok_failure_message_is_never_doubled_or_none(status, body, message) -> None:
    respx.post(f"{BASE}/GenAcsTok").respond(status, json=body)
    client = TradeSmart("TEST123", app_key="ABC", secret_key="123")
    with pytest.raises(AuthenticationError) as info:
        client.generate_session("c")
    assert str(info.value) == info.value.message == message
    assert info.value.http_status == status and info.value.endpoint == "GenAcsTok"


@pytest.mark.parametrize(("status", "body", "message"), _GEN_ACS_TOK_FAILURES)
@respx.mock
async def test_async_gen_acs_tok_failure_message_is_never_doubled_or_none(
    status, body, message
) -> None:
    respx.post(f"{BASE}/GenAcsTok").respond(status, json=body)
    async with AsyncTradeSmart("TEST123", app_key="ABC", secret_key="123") as client:
        with pytest.raises(AuthenticationError) as info:
            await client.generate_session("c")
    assert str(info.value) == info.value.message == message
    assert info.value.http_status == status


@respx.mock
def test_gen_acs_tok_authentication_error_not_double_wrapped() -> None:
    respx.post(f"{BASE}/GenAcsTok").respond(401, json={"stat": "Not_Ok", "emsg": "Invalid Session"})
    client = TradeSmart("TEST123", app_key="ABC", secret_key="123")
    with pytest.raises(AuthenticationError) as info:
        client.generate_session("c")
    assert info.value.__cause__ is None and info.value.http_status == 401


@respx.mock
def test_gen_acs_tok_ok_without_access_token_leaks_no_secret() -> None:
    respx.post(f"{BASE}/GenAcsTok").respond(
        200, json={"stat": "Ok", "susertoken": "secret-suser-xyz", "refresh_token": "secret-r-xyz"}
    )
    client = TradeSmart("TEST123", app_key="ABC", secret_key="123")
    with pytest.raises(AuthenticationError) as info:
        client.generate_session("c")
    e = info.value
    rendered = "".join(traceback.format_exception(type(e), e, e.__traceback__))
    assert e.__cause__ is None and e.__context__ is None
    for text in (rendered, str(e)):
        assert "secret-suser-xyz" not in text and "secret-r-xyz" not in text


_UNKNOWN_MSG = "GenAcsTok outcome unknown: the authorisation code is single-use; restart the login"
_GEN_UNKNOWN = [  # builders: a fresh response/exception per test
    pytest.param(lambda: {"side_effect": httpx.ReadTimeout("slow")}, None, id="read-timeout"),
    pytest.param(
        lambda: {"return_value": httpx.Response(504, text="<html>Gateway Timeout</html>")},
        504,
        id="504-html",
    ),
]


@pytest.mark.parametrize("mock,status", _GEN_UNKNOWN)
@respx.mock
def test_gen_acs_tok_unknown_outcome_is_authentication_error(mock, status) -> None:
    route = respx.post(f"{BASE}/GenAcsTok").mock(**mock())
    # built at runtime so the literals never appear in the traceback's source lines
    secret_key, code = "-".join(("secret", "key", "xyz")), "-".join(("code", "xyz"))
    client = TradeSmart("TEST123", app_key="ABC", secret_key=secret_key)
    with pytest.raises(AuthenticationError) as info:
        client.generate_session(code)
    e = info.value
    assert str(e) == _UNKNOWN_MSG and e.endpoint == "GenAcsTok" and e.http_status == status
    assert route.call_count == 1
    rendered = "".join(traceback.format_exception(type(e), e, e.__traceback__))
    for secret in (code, secret_key, compute_checksum("ABC", secret_key, code)):
        assert secret not in rendered


@pytest.mark.parametrize("mock,status", _GEN_UNKNOWN)
@respx.mock
async def test_async_gen_acs_tok_unknown_outcome_is_authentication_error(mock, status) -> None:
    route = respx.post(f"{BASE}/GenAcsTok").mock(**mock())
    async with AsyncTradeSmart("TEST123", app_key="ABC", secret_key="secret-key-xyz") as c:
        with pytest.raises(AuthenticationError) as info:
            await c.generate_session("code-xyz")
    e = info.value
    assert str(e) == _UNKNOWN_MSG and e.endpoint == "GenAcsTok" and e.http_status == status
    assert route.call_count == 1


def test_missing_token_and_expired_token() -> None:
    with pytest.raises(AuthenticationError, match="No access token set"):
        TradeSmart("TEST123").profile()
    expired = AccessToken.from_response(
        fixture("gen_acs_tok_success"), issued_at=datetime(2020, 1, 1, tzinfo=IST)
    )
    with pytest.raises(AuthenticationError, match="06:00 IST"):
        TradeSmart("TEST123", access_token=expired).profile()


@respx.mock
def test_expired_token_allowed_when_expiry_check_disabled() -> None:
    respx.post(f"{BASE}/UserDetails").respond(200, json=fixture("user_details_success"))
    expired = AccessToken.from_response(
        fixture("gen_acs_tok_success"), issued_at=datetime(2020, 1, 1, tzinfo=IST)
    )
    client = TradeSmart(
        "TEST123", access_token=expired, config=ClientConfig(check_token_expiry=False)
    )
    assert client.profile().user_id == "TEST123"


def test_generate_token_requires_keys() -> None:
    with pytest.raises(ConfigurationError):
        TradeSmart("TEST123").generate_session("c")


def test_authorize_url() -> None:
    url = TradeSmart("TEST123", app_key="ABC").login_url()
    assert url.endswith("?client_id=ABC")
    with pytest.raises(ConfigurationError):
        TradeSmart("TEST123").login_url()


def test_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRADESMART_CLIENT_ID", "TEST123")
    monkeypatch.setenv("TRADESMART_ACCESS_TOKEN", "test-access-token")
    c = TradeSmart.from_env()
    assert c.client_id == "TEST123" and "test-access-token" not in repr(c)
    assert repr(c) == "TradeSmart(client_id='TEST123')"


def test_from_env_requires_client_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRADESMART_CLIENT_ID", raising=False)
    with pytest.raises(ConfigurationError):
        TradeSmart.from_env()


@respx.mock
def test_low_level_request_adds_uid_and_never_retries_state_changing() -> None:
    route = respx.post(f"{BASE}/PlaceMultiOrder").respond(503, text="down")
    with pytest.raises(OrderStatusUnknownError):
        TradeSmart("TEST123", access_token="tok").request(
            "PlaceMultiOrder", {"x": 1}, state_changing=True
        )
    assert route.call_count == 1
    assert route.calls.last.request.content == b'jData={"x":"1","uid":"TEST123"}'


@respx.mock
def test_low_level_request_returns_raw_list_and_keeps_explicit_uid() -> None:
    route = respx.post(f"{BASE}/SomeNewList").respond(200, json=[{"a": "1"}])
    result = TradeSmart("TEST123", access_token="tok").request(
        "SomeNewList", {"uid": "OTHER"}, state_changing=False, expect="list"
    )
    assert result == [{"a": "1"}]
    assert route.calls.last.request.content == b'jData={"uid":"OTHER"}'


@respx.mock
async def test_async_client_user_details() -> None:
    respx.post(f"{BASE}/UserDetails").respond(200, json=fixture("user_details_success"))
    async with AsyncTradeSmart("TEST123", access_token="tok") as c:
        assert (await c.profile()).user_id == "TEST123"


@respx.mock
async def test_async_generate_token_and_request() -> None:
    respx.post(f"{BASE}/GenAcsTok").respond(200, json=fixture("gen_acs_tok_success"))
    route = respx.post(f"{BASE}/Limits").respond(200, json={"stat": "Ok"})
    async with AsyncTradeSmart("TEST123", app_key="ABC", secret_key="123") as c:
        await c.generate_session("x1y2z3")
        assert await c.request("Limits", {}, state_changing=False) == {"stat": "Ok"}
        assert repr(c) == "AsyncTradeSmart(client_id='TEST123')"
    assert route.calls.last.request.headers["Authorization"] == "Bearer test-access-token"


@respx.mock
async def test_async_gen_acs_tok_failure_is_authentication_error() -> None:
    respx.post(f"{BASE}/GenAcsTok").respond(200, json=fixture("errors")["no_data"])
    async with AsyncTradeSmart("TEST123", app_key="ABC", secret_key="123") as c:
        with pytest.raises(AuthenticationError):
            await c.generate_session("used-code")


@respx.mock
def test_gen_acs_tok_parse_failure_is_authentication_error() -> None:
    respx.post(f"{BASE}/GenAcsTok").respond(200, json={"stat": "Ok"})
    client = TradeSmart("TEST123", app_key="ABC", secret_key="123")
    with pytest.raises(AuthenticationError, match="restart the login") as info:
        client.generate_session("c")
    assert info.value.__cause__ is None and info.value.__context__ is None


@respx.mock
async def test_async_gen_acs_tok_parse_failure_is_authentication_error() -> None:
    respx.post(f"{BASE}/GenAcsTok").respond(200, json={"stat": "Ok"})
    async with AsyncTradeSmart("TEST123", app_key="ABC", secret_key="123") as client:
        with pytest.raises(AuthenticationError, match="restart the login"):
            await client.generate_session("c")


@respx.mock
async def test_async_gen_acs_tok_authentication_error_not_double_wrapped() -> None:
    respx.post(f"{BASE}/GenAcsTok").respond(401, json={"stat": "Not_Ok", "emsg": "Invalid Session"})
    async with AsyncTradeSmart("TEST123", app_key="ABC", secret_key="123") as client:
        with pytest.raises(AuthenticationError) as info:
            await client.generate_session("c")
    assert info.value.__cause__ is None and info.value.http_status == 401


def test_async_login_url_and_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    assert AsyncTradeSmart("TEST123", app_key="ABC").login_url().endswith("?client_id=ABC")
    monkeypatch.setenv("TRADESMART_CLIENT_ID", "TEST123")
    monkeypatch.setenv("TRADESMART_ACCESS_TOKEN", "test-access-token")
    assert repr(AsyncTradeSmart.from_env()) == "AsyncTradeSmart(client_id='TEST123')"
