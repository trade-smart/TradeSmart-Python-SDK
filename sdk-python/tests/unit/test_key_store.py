"""``tradesmart._key_store``: the App Key and Secret key saved per client id by the login command.

All values are synthetic.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from tradesmart import _key_store

APP_KEY = "synthetic-app-key-1"
SECRET = "synthetic-secret-key-1"
OTHER_APP_KEY = "synthetic-app-key-2"
OTHER_SECRET = "synthetic-secret-key-2"


def _read(path: Path) -> dict[str, object]:
    data = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def test_path_for_is_next_to_the_token_file(tmp_path: Path) -> None:
    token = tmp_path / "cfg" / "token.json"
    assert _key_store.path_for(token) == tmp_path / "cfg" / "credentials.json"
    assert _key_store.path_for(token, "uat-credentials.json") == (
        tmp_path / "cfg" / "uat-credentials.json"
    )


def test_missing_file_has_no_keys(tmp_path: Path) -> None:
    path = tmp_path / "credentials.json"
    assert _key_store.get(path, "TEST123") is None
    assert _key_store.client_ids(path) == []


def test_put_then_get_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "sub" / "credentials.json"
    assert _key_store.put(path, "TEST123", app_key=APP_KEY, secret_key=SECRET) == path
    keys = _key_store.get(path, "TEST123")
    assert keys is not None
    assert (keys.app_key, keys.secret_key) == (APP_KEY, SECRET)
    data = _read(path)
    assert data == {
        "version": 1,
        "clients": {"TEST123": {"app_key": APP_KEY, "secret_key": SECRET}},
    }


def test_lookup_ignores_case(tmp_path: Path) -> None:
    path = tmp_path / "credentials.json"
    _key_store.put(path, "TEST123", app_key=APP_KEY, secret_key=SECRET)
    keys = _key_store.get(path, "test123")
    assert keys is not None and keys.app_key == APP_KEY


def test_two_client_ids_are_kept_apart(tmp_path: Path) -> None:
    path = tmp_path / "credentials.json"
    _key_store.put(path, "TEST123", app_key=APP_KEY, secret_key=SECRET)
    _key_store.put(path, "OTHER9", app_key=OTHER_APP_KEY, secret_key=OTHER_SECRET)
    first = _key_store.get(path, "TEST123")
    second = _key_store.get(path, "OTHER9")
    assert first is not None and second is not None
    assert (first.app_key, first.secret_key) == (APP_KEY, SECRET)
    assert (second.app_key, second.secret_key) == (OTHER_APP_KEY, OTHER_SECRET)
    assert _key_store.client_ids(path) == ["OTHER9", "TEST123"]
    assert _key_store.get(path, "NOBODY") is None


def test_put_replaces_one_entry_and_keeps_the_others(tmp_path: Path) -> None:
    path = tmp_path / "credentials.json"
    _key_store.put(path, "TEST123", app_key=APP_KEY, secret_key=SECRET)
    _key_store.put(path, "OTHER9", app_key=OTHER_APP_KEY, secret_key=OTHER_SECRET)
    _key_store.put(path, "test123", app_key="new-app-key", secret_key="new-secret")
    clients = _read(path)["clients"]
    assert clients == {
        "OTHER9": {"app_key": OTHER_APP_KEY, "secret_key": OTHER_SECRET},
        "test123": {"app_key": "new-app-key", "secret_key": "new-secret"},
    }


def test_put_of_one_key_keeps_the_other_saved_key(tmp_path: Path) -> None:
    path = tmp_path / "credentials.json"
    _key_store.put(path, "TEST123", app_key=APP_KEY, secret_key=SECRET)
    _key_store.put(path, "TEST123", app_key="new-app-key")
    keys = _key_store.get(path, "TEST123")
    assert keys is not None
    assert (keys.app_key, keys.secret_key) == ("new-app-key", SECRET)


def test_put_of_one_key_alone_saves_a_partial_entry(tmp_path: Path) -> None:
    path = tmp_path / "credentials.json"
    _key_store.put(path, "TEST123", secret_key=SECRET)
    keys = _key_store.get(path, "TEST123")
    assert keys is not None
    assert (keys.app_key, keys.secret_key) == (None, SECRET)


def test_put_without_keys_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="nothing to save"):
        _key_store.put(tmp_path / "credentials.json", "TEST123")
    assert not (tmp_path / "credentials.json").exists()


def test_repr_never_shows_the_keys(tmp_path: Path) -> None:
    path = tmp_path / "credentials.json"
    _key_store.put(path, "TEST123", app_key=APP_KEY, secret_key=SECRET)
    keys = _key_store.get(path, "TEST123")
    text = repr(keys) + str(keys)
    assert APP_KEY not in text and SECRET not in text


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_file_is_owner_only_and_folder_private(tmp_path: Path) -> None:
    path = tmp_path / "new" / "credentials.json"
    old = os.umask(0o022)
    try:
        _key_store.put(path, "TEST123", app_key=APP_KEY, secret_key=SECRET)
    finally:
        os.umask(old)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_write_is_atomic_and_leaves_no_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "credentials.json"
    _key_store.put(path, "TEST123", app_key=APP_KEY, secret_key=SECRET)
    before = path.read_bytes()

    def broken_replace(src: object, dst: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "replace", broken_replace)
    with pytest.raises(OSError):
        _key_store.put(path, "OTHER9", app_key=OTHER_APP_KEY, secret_key=OTHER_SECRET)
    assert path.read_bytes() == before  # the old file is untouched
    assert sorted(p.name for p in tmp_path.iterdir()) == ["credentials.json"]


@pytest.mark.parametrize(
    "content",
    [
        "not json at all " + SECRET,
        json.dumps(["list", SECRET]),
        json.dumps({"TEST123": {"app_key": APP_KEY, "secret_key": SECRET}}),  # no version
        json.dumps({"version": 2, "clients": {}}),
        json.dumps({"version": 1, "clients": [SECRET]}),
        json.dumps({"version": 1, "clients": {"TEST123": SECRET}}),
        json.dumps({"version": 1, "clients": {"TEST123": {"app_key": 5, "secret_key": SECRET}}}),
        json.dumps({"version": 1, "clients": {"TEST123": {}}}),
        json.dumps({"version": 1, "clients": {"TEST123": {"app_key": " "}}}),
        json.dumps({"version": 1, "clients": {" ": {"app_key": APP_KEY}}}),
    ],
    ids=[
        "not-json",
        "not-object",
        "foreign",
        "newer-version",
        "clients-not-object",
        "entry-not-object",
        "key-not-text",
        "entry-empty",
        "key-blank",
        "client-blank",
    ],
)
def test_corrupt_or_foreign_file_is_refused_without_its_content(
    tmp_path: Path, content: str
) -> None:
    path = tmp_path / "credentials.json"
    path.write_text(content, encoding="utf-8")
    if hasattr(os, "chmod"):
        path.chmod(0o600)
    for call in (
        lambda: _key_store.get(path, "TEST123"),
        lambda: _key_store.client_ids(path),
        lambda: _key_store.put(path, "TEST123", app_key=APP_KEY),
        lambda: _key_store.forget(path, "TEST123"),
    ):
        with pytest.raises(_key_store.KeyFileError) as info:
            call()
        message = str(info.value)
        assert str(path) in message
        assert SECRET not in message and APP_KEY not in message
    assert path.read_text(encoding="utf-8") == content  # never overwritten


def test_deeply_nested_json_is_refused_clearly(tmp_path: Path) -> None:
    """A hostile file nested deeper than the parser's recursion limit is refused, not crashed on."""
    path = tmp_path / "credentials.json"
    path.write_text("[" * 200_000 + "]" * 200_000, encoding="utf-8")
    path.chmod(0o600)
    with pytest.raises(_key_store.KeyFileError, match="not valid JSON") as info:
        _key_store.get(path, "TEST123")
    assert "[[" not in str(info.value)


class _NoPathStat(type(Path())):  # type: ignore[misc]
    """A path whose ``stat()`` fails: only a check on the open descriptor still works."""

    def stat(self, *args: object, **kwargs: object) -> os.stat_result:
        raise AssertionError("checked by path, not by the open file")


def test_checks_and_reads_the_same_open_file(tmp_path: Path) -> None:
    """No check-then-read race: the owner/mode check uses the open descriptor, not the path."""
    path = tmp_path / "credentials.json"
    _key_store.put(path, "TEST123", app_key=APP_KEY, secret_key=SECRET)
    keys = _key_store.get(_NoPathStat(path), "TEST123")
    assert keys is not None and keys.app_key == APP_KEY


def test_unreadable_bytes_are_refused(tmp_path: Path) -> None:
    path = tmp_path / "credentials.json"
    path.write_bytes(b"\xff\xfe\x00garbage")
    if hasattr(os, "chmod"):
        path.chmod(0o600)
    with pytest.raises(_key_store.KeyFileError, match="cannot be read"):
        _key_store.get(path, "TEST123")


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_file_open_to_others_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "credentials.json"
    _key_store.put(path, "TEST123", app_key=APP_KEY, secret_key=SECRET)
    path.chmod(0o644)
    with pytest.raises(_key_store.KeyFileError, match="unsafe permissions"):
        _key_store.get(path, "TEST123")


def test_forget_removes_one_entry_and_keeps_the_others(tmp_path: Path) -> None:
    path = tmp_path / "credentials.json"
    _key_store.put(path, "TEST123", app_key=APP_KEY, secret_key=SECRET)
    _key_store.put(path, "OTHER9", app_key=OTHER_APP_KEY, secret_key=OTHER_SECRET)
    assert _key_store.forget(path, "test123") is True
    assert _key_store.get(path, "TEST123") is None
    assert _key_store.get(path, "OTHER9") is not None
    assert _key_store.forget(path, "TEST123") is False


def test_forgetting_the_last_entry_removes_the_file(tmp_path: Path) -> None:
    path = tmp_path / "credentials.json"
    _key_store.put(path, "TEST123", app_key=APP_KEY, secret_key=SECRET)
    assert _key_store.forget(path, "TEST123") is True
    assert not path.exists()


def test_forget_without_a_file(tmp_path: Path) -> None:
    assert _key_store.forget(tmp_path / "credentials.json", "TEST123") is False


def test_delete_removes_the_whole_file(tmp_path: Path) -> None:
    path = tmp_path / "credentials.json"
    _key_store.put(path, "TEST123", app_key=APP_KEY, secret_key=SECRET)
    assert _key_store.delete(path) is True
    assert not path.exists()
    assert _key_store.delete(path) is False
