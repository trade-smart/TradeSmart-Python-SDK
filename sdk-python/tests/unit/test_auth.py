import hashlib
import traceback
from datetime import UTC, datetime

import pytest

from tests.conftest import fixture
from tradesmart.auth import (
    GEN_ACS_TOK,
    AccessToken,
    build_authorize_url,
    compute_checksum,
    gen_acs_tok_call,
    next_expiry,
)
from tradesmart.errors import ConfigurationError, ResponseParseError
from tradesmart.models.base import IST


def test_checksum_doc_worked_example():
    assert compute_checksum("ABC", "123", "x1y2z3") == hashlib.sha256(b"ABC123x1y2z3").hexdigest()


@pytest.mark.parametrize("args", [("", "b", "c"), ("a", "", "c"), ("a", "b", "")])
def test_checksum_rejects_empty(args):
    with pytest.raises(ConfigurationError):
        compute_checksum(*args)


def test_authorize_url():
    assert (
        build_authorize_url("ABC123")
        == "https://v2api.tradesmartonline.in/OAuthlogin/authorize/oauth?client_id=ABC123"
    )


def test_authorize_url_quotes_and_custom_base():
    assert (
        build_authorize_url("a b&c", authorize_url="https://x/y")
        == "https://x/y?client_id=a%20b%26c"
    )


def test_authorize_url_empty_key():
    with pytest.raises(ConfigurationError):
        build_authorize_url("")


@pytest.mark.parametrize(
    "issued,expected",
    [
        (datetime(2026, 11, 7, 5, 59, 59, tzinfo=IST), datetime(2026, 11, 7, 6, 0, tzinfo=IST)),
        (datetime(2026, 11, 7, 6, 0, 0, tzinfo=IST), datetime(2026, 11, 8, 6, 0, tzinfo=IST)),
        (
            datetime(2026, 11, 7, 23, 0, tzinfo=UTC),
            datetime(2026, 11, 8, 6, 0, tzinfo=IST),
        ),
    ],
)
def test_next_expiry_boundaries(issued, expected):
    result = next_expiry(issued)
    assert result == expected
    assert result.utcoffset() == IST.utcoffset(None)


def test_next_expiry_rejects_naive():
    with pytest.raises(ValueError):
        next_expiry(datetime(2026, 11, 7, 9, 0))


def test_access_token_from_response_hides_secrets():
    tok = AccessToken.from_response(
        fixture("gen_acs_tok_success"), issued_at=datetime(2026, 11, 7, 9, 0, tzinfo=IST)
    )
    assert tok.access_token.get_secret_value() == "test-access-token"
    assert "test-access-token" not in repr(tok) and tok.raw["access_token"] == "***"
    assert "test-refresh-token" not in repr(tok) and "test-access-token" not in str(tok)
    assert tok.user_id == "TEST123" and tok.account_id == "TEST123" and tok.uname == "Test User"
    assert tok.expires_at == datetime(2026, 11, 8, 6, 0, tzinfo=IST)
    assert tok.is_expired(datetime(2026, 11, 8, 6, 0, tzinfo=IST))
    assert not tok.is_expired(datetime(2026, 11, 8, 5, 59, tzinfo=IST))


def test_access_token_default_issued_at_is_now():
    tok = AccessToken.from_response({"access_token": "t", "stat": "Ok"})
    assert tok.issued_at.tzinfo is not None and tok.expires_at > tok.issued_at
    assert not tok.is_expired()


def test_access_token_missing_token_is_parse_error_without_secrets():
    with pytest.raises(ResponseParseError) as info:
        AccessToken.from_response({"refresh_token": "secret-refresh", "stat": "Ok"})
    assert "secret-refresh" not in str(info.value.body)


def test_access_token_parse_error_chain_has_no_secrets():
    data = {"stat": "Ok", "susertoken": "secret-suser-xyz", "refresh_token": "secret-refresh-xyz"}
    with pytest.raises(ResponseParseError) as info:
        AccessToken.from_response(data)
    e = info.value
    assert e.__cause__ is None and e.__context__ is None
    texts = [
        "".join(traceback.format_exception(type(e), e, e.__traceback__)),
        str(e),
        e.body,
        repr(e.__cause__),
        repr(e.__context__),
    ]
    for text in texts:
        assert "secret-suser-xyz" not in text and "secret-refresh-xyz" not in text


def test_gen_acs_tok_call():
    call = gen_acs_tok_call("ABC", "123", "x1y2z3")
    assert call.spec is GEN_ACS_TOK
    assert (
        call.spec.name == "GenAcsTok" and not call.spec.authenticated and call.spec.state_changing
    )
    assert call.payload == {"code": "x1y2z3", "checksum": compute_checksum("ABC", "123", "x1y2z3")}


def test_gen_acs_tok_numeric_fields():
    body = {**fixture("gen_acs_tok_success"), "expires_in": 1730000000, "uid": 123}
    token = AccessToken.from_response(body)
    assert token.expires_in == "1730000000"
    assert token.user_id == "123"
