"""The per-user token file written by ``python -m tradesmart login``."""

from __future__ import annotations

import json
import os
import stat
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from tests.conftest import fixture
from tradesmart import AccessToken
from tradesmart import _token_store as store
from tradesmart.models.base import IST


def _token(issued_at: datetime | None = None) -> AccessToken:
    return AccessToken.from_response(
        fixture("gen_acs_tok_success"),
        issued_at=issued_at or datetime(2026, 11, 10, 9, 15, tzinfo=IST),
    )


def _write_private(path: Path, text: str) -> None:
    """Write ``text`` with owner-only permissions, as the login command does."""
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)


def test_env_var_overrides_the_default_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TRADESMART_TOKEN_FILE", str(tmp_path / "t.json"))
    assert store.token_path() == tmp_path / "t.json"


def test_env_var_expands_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("TRADESMART_TOKEN_FILE", "~/t.json")
    assert store.token_path() == tmp_path / "t.json"


def test_default_path_on_windows_uses_appdata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TRADESMART_TOKEN_FILE")
    monkeypatch.setattr(store, "_os_name", lambda: "nt")
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))  # ignored on Windows
    assert store.token_path() == tmp_path / "Roaming" / "tradesmart" / "token.json"


def test_default_path_on_windows_without_appdata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TRADESMART_TOKEN_FILE")
    monkeypatch.setattr(store, "_os_name", lambda: "nt")
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    expected = tmp_path / "AppData" / "Roaming" / "tradesmart" / "token.json"
    assert store.token_path() == expected


def test_default_path_on_posix_uses_xdg_config_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TRADESMART_TOKEN_FILE")
    monkeypatch.setattr(store, "_os_name", lambda: "posix")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert store.token_path() == tmp_path / "xdg" / "tradesmart" / "token.json"


def test_default_path_on_posix_falls_back_to_dot_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TRADESMART_TOKEN_FILE")
    monkeypatch.setattr(store, "_os_name", lambda: "posix")
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert store.token_path() == tmp_path / ".config" / "tradesmart" / "token.json"


def test_save_writes_the_documented_json_and_creates_folders(tmp_path: Path) -> None:
    target = tmp_path / "a" / "b" / "token.json"
    token = _token()
    assert store.save("TEST123", token, target) == target
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data == {
        "client_id": "TEST123",
        "access_token": "test-access-token",
        "issued_at": "2026-11-10T09:15:00+05:30",
        "expires_at": "2026-11-11T06:00:00+05:30",
    }
    assert [p.name for p in target.parent.iterdir()] == ["token.json"]  # no temp file left


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_save_is_owner_only_even_over_an_existing_file(tmp_path: Path) -> None:
    target = tmp_path / "token.json"
    target.write_text("old", encoding="utf-8")
    target.chmod(0o644)
    store.save("TEST123", _token(), target)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_save_failure_leaves_no_temp_file(tmp_path: Path) -> None:
    target = tmp_path / "token.json"
    target.mkdir()  # cannot be replaced by a file
    with pytest.raises(OSError):
        store.save("TEST123", _token(), target)
    assert [p.name for p in tmp_path.iterdir()] == ["token.json"]


def test_load_round_trips_as_access_token(tmp_path: Path) -> None:
    target = tmp_path / "token.json"
    token = _token()
    store.save("TEST123", token, target)
    saved = store.load(target)
    assert saved is not None
    assert saved.client_id == "TEST123"
    assert saved.token.access_token.get_secret_value() == "test-access-token"
    assert saved.token.issued_at == token.issued_at
    assert saved.token.expires_at == token.expires_at
    assert "test-access-token" not in repr(saved)


def test_load_missing_file_is_none(tmp_path: Path) -> None:
    assert store.load(tmp_path / "nothing.json") is None


@pytest.mark.parametrize(
    "content",
    [
        "{not json secret-value",
        "[]",
        '{"client_id": "TEST123", "issued_at": "2026-11-10T09:15:00+05:30",'
        ' "expires_at": "2026-11-11T06:00:00+05:30"}',
        '{"client_id": "", "access_token": "secret-value",'
        ' "issued_at": "2026-11-10T09:15:00+05:30", "expires_at": "2026-11-11T06:00:00+05:30"}',
        '{"client_id": "TEST123", "access_token": "secret-value",'
        ' "issued_at": "yesterday", "expires_at": "2026-11-11T06:00:00+05:30"}',
        '{"client_id": "TEST123", "access_token": "secret-value",'
        ' "issued_at": "2026-11-10T09:15:00", "expires_at": "2026-11-11T06:00:00"}',
    ],
    ids=["not-json", "not-object", "no-token", "empty-client", "bad-date", "naive-date"],
)
def test_load_unusable_file_raises_without_echoing_contents(tmp_path: Path, content: str) -> None:
    target = tmp_path / "token.json"
    _write_private(target, content)
    with pytest.raises(store.TokenFileError) as info:
        store.load(target)
    message = str(info.value)
    assert str(target) in message
    assert "secret-value" not in message
    assert "permission" not in message and "owned" not in message  # the content was rejected


def test_load_unreadable_file_raises(tmp_path: Path) -> None:
    target = tmp_path / "token.json"
    target.write_bytes(b"\xff\xfe\xfa")
    target.chmod(0o600)
    with pytest.raises(store.TokenFileError):
        store.load(target)


_VALID = (
    '{"client_id": "TEST123", "access_token": "secret-value",'
    ' "issued_at": "2026-11-10T09:15:00+05:30", "expires_at": "%s"}'
)


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
@pytest.mark.parametrize("mode", [0o640, 0o604, 0o660])
def test_load_refuses_a_file_others_can_access(tmp_path: Path, mode: int) -> None:
    target = tmp_path / "token.json"
    target.write_text(_VALID % "2026-11-11T06:00:00+05:30", encoding="utf-8")
    target.chmod(mode)
    with pytest.raises(store.TokenFileError) as info:
        store.load(target)
    message = str(info.value)
    assert str(target) in message and "permission" in message
    assert "secret-value" not in message


@pytest.mark.skipif(os.name != "posix", reason="POSIX file ownership")
def test_load_refuses_a_file_owned_by_another_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "token.json"
    _write_private(target, _VALID % "2026-11-11T06:00:00+05:30")
    monkeypatch.setattr(store.os, "geteuid", lambda: target.stat().st_uid + 1)
    with pytest.raises(store.TokenFileError) as info:
        store.load(target)
    assert str(target) in str(info.value) and "owned" in str(info.value)


def test_load_skips_the_permission_check_on_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows has no POSIX mode bits; the per-user profile folder protects the file."""
    target = tmp_path / "token.json"
    target.write_text(_VALID % "2026-11-11T06:00:00+05:30", encoding="utf-8")
    target.chmod(0o644)
    monkeypatch.setattr(store, "_os_name", lambda: "nt")
    saved = store.load(target)
    assert saved is not None and saved.client_id == "TEST123"


def test_load_caps_an_edited_expiry_at_the_0600_rule(tmp_path: Path) -> None:
    target = tmp_path / "token.json"
    _write_private(target, _VALID % "2030-01-01T06:00:00+05:30")
    saved = store.load(target)
    assert saved is not None
    assert saved.token.expires_at == datetime(2026, 11, 11, 6, 0, tzinfo=IST)


def test_load_keeps_an_earlier_expiry(tmp_path: Path) -> None:
    target = tmp_path / "token.json"
    _write_private(target, _VALID % "2026-11-10T12:00:00+05:30")
    saved = store.load(target)
    assert saved is not None
    assert saved.token.expires_at == datetime(2026, 11, 10, 12, 0, tzinfo=IST)


def _issued(offset: timedelta) -> str:
    issued = (datetime.now(IST) + offset).isoformat(timespec="seconds")
    return (
        '{"client_id": "TEST123", "access_token": "secret-value",'
        f' "issued_at": "{issued}", "expires_at": "2099-01-01T06:00:00+05:30"}}'
    )


@pytest.mark.parametrize("offset", [timedelta(hours=1), timedelta(days=30000)])
def test_load_refuses_an_issue_time_in_the_future(tmp_path: Path, offset: timedelta) -> None:
    """An edited file (or a wrong clock at login) cannot keep a token valid past 06:00 IST."""
    target = tmp_path / "token.json"
    _write_private(target, _issued(offset))
    with pytest.raises(store.TokenFileError) as info:
        store.load(target)
    message = str(info.value)
    assert str(target) in message and "'issued_at' in the future" in message
    assert "clock" in message and "secret-value" not in message


def test_load_allows_a_small_clock_skew_in_the_issue_time(tmp_path: Path) -> None:
    target = tmp_path / "token.json"
    _write_private(target, _issued(timedelta(minutes=2)))
    saved = store.load(target)
    assert saved is not None and not saved.token.is_expired()


def test_relative_env_var_is_made_absolute(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The printed path and from_env() must name the same file from any working directory."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TRADESMART_TOKEN_FILE", os.path.join("rel", "token.json"))
    path = store.token_path()
    assert path.is_absolute()
    assert path == tmp_path / "rel" / "token.json"


def test_delete(tmp_path: Path) -> None:
    target = tmp_path / "token.json"
    store.save("TEST123", _token(), target)
    assert store.delete(target) is True
    assert not target.exists()
    assert store.delete(target) is False


def test_default_location_is_used_when_no_path_given(
    _no_saved_login: Path,
) -> None:
    store.save("TEST123", _token())
    assert _no_saved_login.exists()
    saved = store.load()
    assert saved is not None and saved.client_id == "TEST123"
    assert store.delete() is True


def test_save_writes_seconds_precision_timestamps_with_offset(tmp_path: Path) -> None:
    target = tmp_path / "token.json"
    issued = datetime(2026, 11, 10, 9, 15, 30, 123456, tzinfo=IST)
    store.save("TEST123", _token(issued), target)
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["issued_at"] == "2026-11-10T09:15:30+05:30"
    assert data["expires_at"] == "2026-11-11T06:00:00+05:30"


@pytest.mark.parametrize(
    "issued",
    [
        "2026-11-10T03:45:00Z",
        "2026-11-10T03:45:00.1Z",
        "2026-11-10T03:45:00.123456789Z",
        "2026-11-10T09:15:00.1234567+05:30",
        "2026-11-10T09:15:00.12+05:30",
        "2026-11-10T09:15:00+05:30",
    ],
)
def test_load_accepts_z_suffix_and_any_fraction_digits(tmp_path: Path, issued: str) -> None:
    target = tmp_path / "token.json"
    _write_private(
        target,
        json.dumps(
            {
                "client_id": "TEST123",
                "access_token": "test-access-token",
                "issued_at": issued,
                "expires_at": "2026-11-10T22:30:00Z",
            }
        ),
    )
    saved = store.load(target)
    assert saved is not None
    got = saved.token.issued_at.replace(microsecond=0)
    assert got == datetime(2026, 11, 10, 9, 15, tzinfo=IST)


def test_env_var_is_stripped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRADESMART_TOKEN_FILE", f"  {tmp_path / 't.json'}\n")
    assert store.token_path() == tmp_path / "t.json"


@pytest.mark.parametrize("blank", ["", "   ", "\n", " \t "])
def test_blank_env_var_counts_as_unset(
    blank: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(store, "_os_name", lambda: "posix")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("TRADESMART_TOKEN_FILE", blank)
    assert store.token_path() == tmp_path / "xdg" / "tradesmart" / "token.json"


def _write_login(target: Path, issued: str) -> None:
    _write_private(
        target,
        json.dumps(
            {
                "client_id": "TEST123",
                "access_token": "test-access-token",
                "issued_at": issued,
                "expires_at": "2026-11-11T06:00:00+05:30",
            }
        ),
    )


@pytest.mark.parametrize(
    "issued",
    [
        "2026-11-10T09:15:00.1234567890+05:30",  # 10 fraction digits
        "2026-11-10T09:15:00+0530",  # offset without a colon
        "20261110T091500+05:30",  # basic format
        "2026-11-10 09:15:00+05:30",  # space separator
        "2026-11-10t09:15:00+05:30",  # lower-case separator
        "2026-11-10T09:15+05:30",  # no seconds
        "2026-11-10T09:15:00.+05:30",  # dot without digits
        "2026-11-10T09:15:00+05:30:00",  # offset with seconds
        "2026-W46-2T09:15:00+05:30",  # week date
        " 2026-11-10T09:15:00+05:30x",  # trailing garbage
    ],
)
def test_load_refuses_forms_outside_the_pinned_format(tmp_path: Path, issued: str) -> None:
    """The same forms are refused on every Python version (3.11+ fromisoformat accepts more)."""
    target = tmp_path / "token.json"
    _write_login(target, issued)
    with pytest.raises(store.TokenFileError):
        store.load(target)


def test_load_still_rejects_a_timestamp_without_offset(tmp_path: Path) -> None:
    target = tmp_path / "token.json"
    _write_private(
        target,
        json.dumps(
            {
                "client_id": "TEST123",
                "access_token": "test-access-token",
                "issued_at": "2026-11-10T09:15:00.5",
                "expires_at": "2026-11-11T06:00:00+05:30",
            }
        ),
    )
    with pytest.raises(store.TokenFileError):
        store.load(target)
